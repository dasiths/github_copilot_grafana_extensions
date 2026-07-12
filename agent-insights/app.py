#!/usr/bin/env python3
"""Agent-insights sidecar: trace-derived views over Tempo.

Walks Tempo traces and serves aggregated JSON frames that no LGTM datasource can
produce directly, consumed by the Agent Graph, Agent Timeline, and Cost by Repo
& Branch dashboards via the Grafana Infinity datasource:

  * /graph.json            agent topology (parent agent -> subagent edges)
  * /conversations.json    per-conversation rollups (Agent Timeline)
  * /timeline.json,
    /timeline_states.json  per-agent swim-lane state frames
  * /branches.json         cost / tokens / sessions per git repo · branch
  * /branch_series.json    cost over time per git repo · branch
  * /cost_distribution.json  cost distribution + percentiles per agent (estimator)

Branch attribution (see _trace_repo_branch) coalesces the in-band invoke_agent
span attrs (github.copilot.git.*, emitted by both VS Code and the current CLI)
with the older CLI's OTel resource vcs.* fallback.

The agent *graph* is the hardest case and motivates the whole sidecar: all
GitHub Copilot agents share one service.name, subagent spans are span-kind
INTERNAL, and no span attribute names the parent agent. The parent -> child
relationship is a GRANDPARENT hop in the trace tree:

    invoke_agent (parent agent)
      └── execute_tool (runSubagent / task)
            └── invoke_agent (subagent)          <- child

Each view walks the traces for the requested window, derives the relationship or
aggregate it needs, and serves it as JSON for Infinity.

Design (see docs/dashboards.md and the research notes):
  * Compute-on-request, WINDOWED to ?from=&to= (unix seconds) so the views stay
    consistent with the range-windowed Cost & Sessions / Agents dashboards.
    Infinity passes the panel range via ${__timeFrom:date:seconds} /
    ${__timeTo:date:seconds}.
  * Short-TTL in-memory cache keyed by the rounded window, with a single-flight
    lock, so the 30s auto-refresh and simultaneous requests for one view
    collapse to one Tempo walk.
  * Always answers 200 with an empty frame on empty/error so panels degrade to
    "No data" instead of erroring.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Config (env-overridable) ----------------------------------------------
TEMPO_URL = os.environ.get("TEMPO_URL", "http://lgtm:3200").rstrip("/")
LISTEN_ADDR = os.environ.get("LISTEN_ADDR", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8099"))
# Default window (seconds) when a request omits from/to.
DEFAULT_LOOKBACK = int(os.environ.get("DEFAULT_LOOKBACK_SECONDS", "10800"))  # 3h
# Cache time-to-live and the bucket the window is rounded to for the cache key.
CACHE_TTL = float(os.environ.get("CACHE_TTL_SECONDS", "30"))
CACHE_BUCKET = int(os.environ.get("CACHE_BUCKET_SECONDS", "15"))
# Max traces to pull per window; per-trace fetch is required for ancestry.
SEARCH_LIMIT = int(os.environ.get("TEMPO_SEARCH_LIMIT", "200"))
HTTP_TIMEOUT = float(os.environ.get("TEMPO_HTTP_TIMEOUT_SECONDS", "20"))
TRACEQL = os.environ.get("TRACEQL", '{ name=~"invoke_agent.*" }')
# Optional regex to extract a work-item / story key from a branch name (e.g.
# "^(?:feature|fix)/([A-Z]+-\\d+)"); when it matches, story = capture group 1,
# else story = the branch name.
STORY_PATTERN = os.environ.get("STORY_PATTERN", "").strip()


# --- Tempo access ----------------------------------------------------------
def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _search_trace_ids(start: int, end: int) -> list[str]:
    url = f"{TEMPO_URL}/api/search?" + urllib.parse.urlencode(
        {"q": TRACEQL, "start": start, "end": end, "limit": SEARCH_LIMIT}
    )
    data = _get_json(url)
    return [t["traceID"] for t in data.get("traces", []) if t.get("traceID")]


def _fetch_trace(trace_id: str, start: int, end: int) -> dict:
    url = f"{TEMPO_URL}/api/traces/{trace_id}?" + urllib.parse.urlencode(
        {"start": start, "end": end}
    )
    return _get_json(url)


def _story(branch: str) -> str:
    """Work-item key for a branch: STORY_PATTERN capture group if it matches,
    else the branch itself."""
    if branch and branch != "unknown" and STORY_PATTERN:
        try:
            m = re.search(STORY_PATTERN, branch)
            if m and m.groups():
                return m.group(1)
        except re.error:
            pass
    return branch


# --- OTLP span helpers -----------------------------------------------------
def _attr(span: dict, key: str):
    for a in span.get("attributes", []):
        if a.get("key") == key:
            v = a.get("value", {})
            if "stringValue" in v:
                return v["stringValue"]
            if "intValue" in v:
                try:
                    return int(v["intValue"])
                except (TypeError, ValueError):
                    return None
            if "doubleValue" in v:
                return v["doubleValue"]
    return None


def _num(span: dict, key: str) -> float:
    v = _attr(span, key)
    return float(v) if isinstance(v, (int, float)) else 0.0


def _start_seconds(span: dict) -> float:
    try:
        return int(span.get("startTimeUnixNano", "0")) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _agent_name(span: dict) -> str:
    """Agent identity. VS Code sets gen_ai.agent.name; the CLI top-level agent
    leaves it unset and only sets gen_ai.agent.id (github.copilot.default), so
    fall back to the id to avoid empty/\"unknown\" agents."""
    name = _attr(span, "gen_ai.agent.name") or _attr(span, "gen_ai.agent.id")
    if not name:
        return "unknown"
    if name == "github.copilot.default":
        return "Copilot CLI"
    return str(name)


def _agent_match(span: dict) -> str:
    """Raw identifier used to filter this agent's spans in Tempo: the literal
    gen_ai.agent.name if present, else gen_ai.agent.id (e.g. the CLI's
    github.copilot.default). Unlike _agent_name this is NOT remapped, so a data
    link can match on span.gen_ai.agent.name OR span.gen_ai.agent.id."""
    return str(_attr(span, "gen_ai.agent.name") or _attr(span, "gen_ai.agent.id") or "")


def _index_spans(trace: dict) -> dict:
    spans: dict[str, dict] = {}
    for batch in trace.get("batches", []):
        for scope in batch.get("scopeSpans", []):
            for sp in scope.get("spans", []):
                sid = sp.get("spanId")
                if sid:
                    spans[sid] = sp
    return spans


def _owner_agent(span: dict, spans: dict) -> str | None:
    """Nearest ancestor invoke_agent's agent name (for attributing tool calls)."""
    cur = span
    seen = set()
    while cur is not None:
        sid = cur.get("spanId")
        if sid in seen:
            break
        seen.add(sid)
        if _attr(cur, "gen_ai.operation.name") == "invoke_agent":
            return _agent_name(cur)
        parent = cur.get("parentSpanId")
        cur = spans.get(parent) if parent else None
    return None


# --- Graph builder ---------------------------------------------------------
# Grafana's nodeGraph panel requires the edges data frame to expose an `id`
# field. The Infinity datasource drops all columns when the source JSON array is
# empty, so a run with no parent->subagent hops makes the panel fail with
# "id field is required for edges data frame". When there are no real edges we
# emit one schema-only placeholder edge to keep the `id` column present. The
# placeholder must reference an EXISTING node id (an edge pointing at a missing
# node crashes the panel with "cannot read nodeRadius"), so we anchor it to the
# first node as a zero-thickness self-loop; if the graph is completely empty we
# add a hidden sentinel node to anchor it.
_SENTINEL_NODE_ID = "\u2205"  # empty-set glyph; unlikely to collide with an agent name


def _with_edge_schema(graph: dict) -> dict:
    """Guarantee a non-empty, well-formed edges frame so the nodeGraph `id`
    field survives even when there are no parent->subagent relationships."""
    if graph.get("edges"):
        return graph
    nodes = list(graph.get("nodes") or [])
    if not nodes:
        nodes = [{
            "id": _SENTINEL_NODE_ID,
            "title": "no agent activity",
            "mainstat": "",
            "secondarystat": "",
            "detail__invocations": 0,
            "detail__cost_usd": 0,
            "detail__tokens_in": 0,
            "detail__tokens_out": 0,
            "detail__tool_calls": 0,
            "detail__match": "",
        }]
    anchor = nodes[0]["id"]
    placeholder = {
        "id": "__schema__",
        "source": anchor,
        "target": anchor,
        "mainstat": "",
        "detail__invocations": 0,
        "detail__tool": "",
        "thickness": 0,
    }
    return {**graph, "nodes": nodes, "edges": [placeholder]}


def build_graph(start: int, end: int) -> dict:
    nodes: dict[str, dict] = {}
    edges: dict[tuple, dict] = {}

    def node(agent: str) -> dict:
        n = nodes.get(agent)
        if n is None:
            n = {
                "invocations": 0,
                "cost_usd": 0.0,
                "tokens_in": 0.0,
                "tokens_out": 0.0,
                "tool_calls": 0,
                "match": "",
            }
            nodes[agent] = n
        return n

    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        for sp in spans.values():
            # Only count spans whose start falls inside the window, matching the
            # windowing of the Tempo-based Cost & Sessions dashboard.
            if not (start <= _start_seconds(sp) <= end):
                continue
            op = _attr(sp, "gen_ai.operation.name")
            if op == "invoke_agent":
                agent = _agent_name(sp)
                n = node(agent)
                if not n["match"]:
                    n["match"] = _agent_match(sp)
                n["invocations"] += 1
                n["cost_usd"] += _num(sp, "gen_ai.usage.cost_usd")
                n["tokens_in"] += _num(sp, "gen_ai.usage.input_tokens")
                n["tokens_out"] += _num(sp, "gen_ai.usage.output_tokens")
                # Grandparent hop: parent execute_tool, grandparent invoke_agent.
                parent = spans.get(sp.get("parentSpanId")) if sp.get("parentSpanId") else None
                if parent is not None and _attr(parent, "gen_ai.operation.name") == "execute_tool":
                    gp = spans.get(parent.get("parentSpanId")) if parent.get("parentSpanId") else None
                    if gp is not None and _attr(gp, "gen_ai.operation.name") == "invoke_agent":
                        src = _agent_name(gp)
                        nsrc = node(src)
                        if not nsrc["match"]:
                            nsrc["match"] = _agent_match(gp)
                        # Skip self-edges (agent invoking a subagent of the same
                        # name): nodeGraph cannot lay out a node->itself line and
                        # renders it with NaN coordinates.
                        if src != agent:
                            key = (src, agent)
                            e = edges.get(key)
                            if e is None:
                                e = {"count": 0, "tool": _attr(parent, "gen_ai.tool.name") or ""}
                                edges[key] = e
                            e["count"] += 1
            elif op == "execute_tool":
                owner = _owner_agent(sp, spans)
                if owner:
                    node(owner)["tool_calls"] += 1

    node_frame = [
        {
            "id": agent,
            "title": agent,
            "mainstat": f"{s['invocations']} inv · ${s['cost_usd']:.2f}",
            "secondarystat": f"{int(s['tokens_in'] + s['tokens_out'])} tok · {s['tool_calls']} tools",
            "detail__invocations": s["invocations"],
            "detail__cost_usd": round(s["cost_usd"], 4),
            "detail__tokens_in": int(s["tokens_in"]),
            "detail__tokens_out": int(s["tokens_out"]),
            "detail__tool_calls": s["tool_calls"],
            "detail__match": s.get("match", ""),
        }
        for agent, s in sorted(nodes.items())
    ]
    edge_frame = [
        {
            "id": f"{src}->{dst}",
            "source": src,
            "target": dst,
            "mainstat": f"{e['count']}x {e['tool']}".strip(),
            "detail__invocations": e["count"],
            "detail__tool": e["tool"],
            "thickness": e["count"],
        }
        for (src, dst), e in sorted(edges.items())
    ]
    return _with_edge_schema({"nodes": node_frame, "edges": edge_frame})


# --- Conversation + timeline builders (Agent Timeline dashboard) ------------
def _end_seconds(span: dict) -> float:
    try:
        return int(span.get("endTimeUnixNano", "0")) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _is_error(span: dict) -> bool:
    return span.get("status", {}).get("code") == "STATUS_CODE_ERROR"


def _root_conversation(spans: dict):
    """(conversation_id, root_agent) of the top-level invoke_agent in a trace.

    A trace holds one user turn: a root invoke_agent (the foreground agent) plus
    nested subagent invoke_agent spans that each carry their OWN conversation id.
    The episode key is the ROOT invoke_agent's conversation id.
    """
    fallback = (None, None)
    for sp in spans.values():
        if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
            continue
        fallback = (_attr(sp, "gen_ai.conversation.id"), _agent_name(sp))
        cur = spans.get(sp.get("parentSpanId")) if sp.get("parentSpanId") else None
        seen = set()
        is_root = True
        while cur is not None and cur.get("spanId") not in seen:
            seen.add(cur.get("spanId"))
            if _attr(cur, "gen_ai.operation.name") == "invoke_agent":
                is_root = False
                break
            cur = spans.get(cur.get("parentSpanId")) if cur.get("parentSpanId") else None
        if is_root:
            return _attr(sp, "gen_ai.conversation.id"), _agent_name(sp)
    return fallback


def build_conversations(start: int, end: int, failures_only: bool = False,
                        conversation: str = "", repo: str = "",
                        branch: str = "") -> dict:
    convs: dict[str, dict] = {}
    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        if repo or branch:
            t_repo, t_branch = _trace_repo_branch(trace, spans)
            if repo and t_repo != repo:
                continue
            if branch and t_branch != branch:
                continue
        cid, root_agent = _root_conversation(spans)
        if not cid:
            continue
        if conversation and cid != conversation:
            continue
        c = convs.get(cid)
        if c is None:
            c = {
                "conversation_id": cid, "root_agent": root_agent or "unknown",
                "agents": set(), "traces": 0, "invocations": 0, "model_calls": 0,
                "tool_calls": 0, "failures": 0, "cost_usd": 0.0,
                "tokens_in": 0.0, "tokens_out": 0.0,
                "start_ms": None, "end_ms": None,
            }
            convs[cid] = c
        c["traces"] += 1
        for sp in spans.values():
            s = _start_seconds(sp)
            if not (start <= s <= end):
                continue
            e = _end_seconds(sp)
            st_ms, en_ms = int(s * 1000), int(e * 1000)
            c["start_ms"] = st_ms if c["start_ms"] is None else min(c["start_ms"], st_ms)
            c["end_ms"] = en_ms if c["end_ms"] is None else max(c["end_ms"], en_ms)
            if _is_error(sp):
                c["failures"] += 1
            op = _attr(sp, "gen_ai.operation.name")
            if op == "invoke_agent":
                c["invocations"] += 1
                c["agents"].add(_agent_name(sp))
                c["cost_usd"] += _num(sp, "gen_ai.usage.cost_usd")
                c["tokens_in"] += _num(sp, "gen_ai.usage.input_tokens")
                c["tokens_out"] += _num(sp, "gen_ai.usage.output_tokens")
            elif op == "chat":
                c["model_calls"] += 1
            elif op == "execute_tool":
                c["tool_calls"] += 1
    rows = []
    for c in convs.values():
        dur = ((c["end_ms"] or 0) - (c["start_ms"] or 0)) / 1000.0
        rows.append({
            "conversation_id": c["conversation_id"],
            "root_agent": c["root_agent"],
            "agents": ", ".join(sorted(c["agents"])),
            "agent_count": len(c["agents"]),
            "traces": c["traces"],
            "invocations": c["invocations"],
            "model_calls": c["model_calls"],
            "tool_calls": c["tool_calls"],
            "failures": c["failures"],
            "duration_s": round(dur, 2),
            "cost_usd": round(c["cost_usd"], 4),
            "tokens_in": int(c["tokens_in"]),
            "tokens_out": int(c["tokens_out"]),
            "tokens_total": int(c["tokens_in"] + c["tokens_out"]),
            "start_ms": c["start_ms"] or 0,
            "end_ms": c["end_ms"] or 0,
        })
    rows.sort(key=lambda r: r["end_ms"], reverse=True)
    if failures_only:
        rows = [r for r in rows if r["failures"] > 0]
    return {"conversations": rows}


# --- Repo / branch builder (Cost by Branch dashboard) ----------------------
def _resource_attr(trace: dict, key: str):
    """Read a resource-level attribute from any batch of the trace.

    The CLI carries git enrichment on the OTel resource (vcs.*), which
    _index_spans() discards, so branch/repo grouping must reach into
    trace["batches"][*]["resource"] directly.
    """
    for batch in trace.get("batches", []):
        v = _attr(batch.get("resource", {}), key)
        if v is not None:
            return v
    return None


def _repo_name(raw) -> str | None:
    """Normalize a repo URL or name to a bare repo name (basename minus .git).

    VS Code sets github.copilot.git.repository to a full clone URL; the CLI sets
    vcs.repository.name (already a basename) or vcs.repository.url.full. Both
    collapse to the same short name so a repo groups identically across surfaces.
    """
    if not raw:
        return None
    s = str(raw).rstrip("/")
    s = s.rsplit("/", 1)[-1].rsplit(":", 1)[-1]  # basename (https + git@host:owner/repo)
    if s.endswith(".git"):
        s = s[:-4]
    return s or None


def _trace_repo_branch(trace: dict, spans: dict) -> tuple[str, str]:
    """(repo, branch) for a trace, coalescing the enrichment shapes into one key.

    Both VS Code and the current Copilot CLI tag the repo/branch in-band on the
    invoke_agent span (github.copilot.git.*); older CLIs carry it on the OTel
    resource (vcs.*) via the CLI script. Precedence: span attrs -> resource
    fallback -> unknown.
    """
    repo = branch = None
    for sp in spans.values():
        if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
            continue
        repo = repo or _attr(sp, "github.copilot.git.repository")
        branch = branch or _attr(sp, "github.copilot.git.branch")
        if repo and branch:
            break
    if not repo:
        repo = _resource_attr(trace, "vcs.repository.name") or \
            _resource_attr(trace, "vcs.repository.url.full")
    if not branch:
        branch = _resource_attr(trace, "vcs.ref.head.name")
    return (_repo_name(repo) or "unknown", str(branch) if branch else "unknown")


def _traceql_clause(kind: str, value: str) -> str:
    """A TraceQL predicate that matches a repo/branch across both surfaces.

    VS Code tags the repo/branch on the invoke_agent span; the CLI tags it on
    the OTel resource. This ORs the two so a single value scopes both. The
    'unknown' bucket is not expressible (Tempo cannot search for attribute
    absence: `attr = nil` returns 400), so it maps to `true` and is excluded
    from the dashboard filter dropdowns instead. RE2-escaped for safety.
    """
    if not value or value == "unknown":
        return "true"
    esc = re.escape(value)
    if kind == "repo":
        # VS Code repo is a full clone URL (substring); CLI repo is a basename.
        return (f'(span.github.copilot.git.repository =~ ".*{esc}.*" '
                f'|| resource.vcs.repository.name =~ "^{esc}$")')
    return (f'(span.github.copilot.git.branch =~ "^{esc}$" '
            f'|| resource.vcs.ref.head.name =~ "^{esc}$")')


def build_branches(start: int, end: int) -> dict:
    """Aggregate cost / tokens / sessions per (repo, branch).

    Repo is the primary grouping and branch the secondary. Costs and tokens come
    off invoke_agent spans; sessions count distinct conversation ids.
    """
    buckets: dict[tuple, dict] = {}
    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        repo, branch = _trace_repo_branch(trace, spans)
        key = (repo, branch)
        b = buckets.get(key)
        if b is None:
            b = {
                "repo": repo, "branch": branch, "agents": set(), "sessions": set(),
                "invocations": 0, "cost_usd": 0.0, "tokens_in": 0.0,
                "tokens_out": 0.0, "cache_read": 0.0, "cache_write": 0.0,
            }
            buckets[key] = b
        for sp in spans.values():
            if not (start <= _start_seconds(sp) <= end):
                continue
            if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
                continue
            b["invocations"] += 1
            b["agents"].add(_agent_name(sp))
            b["cost_usd"] += _num(sp, "gen_ai.usage.cost_usd")
            b["tokens_in"] += _num(sp, "gen_ai.usage.input_tokens")
            b["tokens_out"] += _num(sp, "gen_ai.usage.output_tokens")
            b["cache_read"] += _num(sp, "gen_ai.usage.cache_read.input_tokens")
            b["cache_write"] += _num(sp, "gen_ai.usage.cache_creation.input_tokens")
            cid = _attr(sp, "gen_ai.conversation.id")
            if cid:
                b["sessions"].add(cid)
    rows = []
    for b in buckets.values():
        if b["invocations"] == 0:
            continue
        rows.append({
            "repo": b["repo"],
            "branch": b["branch"],
            "story": _story(b["branch"]),
            "repo_branch": f"{b['repo']} \u00b7 {b['branch']}",
            "repo_clause": _traceql_clause("repo", b["repo"]),
            "branch_clause": _traceql_clause("branch", b["branch"]),
            "agents": ", ".join(sorted(b["agents"])),
            "agent_count": len(b["agents"]),
            "sessions": len(b["sessions"]),
            "invocations": b["invocations"],
            "cost_usd": round(b["cost_usd"], 4),
            "tokens_in": int(b["tokens_in"]),
            "tokens_out": int(b["tokens_out"]),
            "tokens_total": int(b["tokens_in"] + b["tokens_out"]),
            "cache_read_tokens": int(b["cache_read"]),
            "cache_write_tokens": int(b["cache_write"]),
        })
    rows.sort(key=lambda r: (r["repo"], -r["cost_usd"]))
    return {"branches": rows}


def build_branch_series(start: int, end: int, buckets: int = 48) -> dict:
    """Wide time-bucketed cost per `repo · branch` for a time series panel.

    One row per time bucket; one column per `repo · branch` holding the cost
    (USD) of the invoke_agent spans that started in that bucket. Missing cells
    are 0 so the series draw a continuous line.
    """
    span_s = max(1, (end - start))
    step = max(60, span_s // max(1, buckets))  # >= 60s buckets
    grid: dict[int, dict[str, float]] = {}
    series: set[str] = set()
    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        repo, branch = _trace_repo_branch(trace, spans)
        key = f"{repo} \u00b7 {branch}"
        for sp in spans.values():
            st = _start_seconds(sp)
            if not (start <= st <= end):
                continue
            if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
                continue
            cost = _num(sp, "gen_ai.usage.cost_usd")
            if cost == 0:
                continue
            bucket = start + int((st - start) // step) * step
            grid.setdefault(bucket, {})
            grid[bucket][key] = grid[bucket].get(key, 0.0) + cost
            series.add(key)
    series_list = sorted(series)
    rows = []
    for bucket in sorted(grid.keys()):
        row = {"time": bucket * 1000}
        for s in series_list:
            row[s] = round(grid[bucket].get(s, 0.0), 4)
        rows.append(row)
    return {"series": rows, "lanes": series_list}


# --- Cost distribution / estimator (Agent Cost Estimator dashboard) ---------
# Half-decade log bucket edges ($): heavy-tailed cost reads better on a log axis
# than on the linear buckets Grafana's native histogram panel would use.
_COST_BUCKET_EDGES = [
    0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30, 100, 300, 1000,
]


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] * (1 - (idx - lo)) + sorted_vals[hi] * (idx - lo)


def _money(x: float) -> str:
    if x < 0.01:
        return f"${x:.3f}"
    if x < 1:
        return f"${x:.2f}"
    return f"${x:,.0f}"


def _dist_stats(vals: list[float], per_day: float) -> dict:
    """n / mean / stddev / min / percentiles / max / total of a cost sample set,
    plus an optional monthly projection band from p50 and p90."""
    vals = sorted(vals)
    n = len(vals)
    total = sum(vals)
    mean = total / n if n else 0.0
    var = sum((v - mean) ** 2 for v in vals) / n if n > 1 else 0.0
    p50 = _percentile(vals, 0.5)
    p90 = _percentile(vals, 0.9)
    row = {
        "n": n,
        "min": round(vals[0], 4) if vals else 0.0,
        "p50": round(p50, 4),
        "p90": round(p90, 4),
        "p95": round(_percentile(vals, 0.95), 4),
        "p99": round(_percentile(vals, 0.99), 4),
        "max": round(vals[-1], 4) if vals else 0.0,
        "mean": round(mean, 4),
        "stddev": round(var ** 0.5, 4),
        "total": round(total, 4),
    }
    if per_day > 0:
        row["proj_monthly_p50"] = round(p50 * per_day * 30, 2)
        row["proj_monthly_p90"] = round(p90 * per_day * 30, 2)
    return row


def build_cost_distribution(start: int, end: int, unit: str = "session",
                            agent: str = "", model: str = "",
                            per_day: float = 0.0) -> dict:
    """Distribution of cost per agent run, for the Agent Cost Estimator.

    unit="session": one sample per conversation (root gen_ai.conversation.id),
    cost summed across its invoke_agent spans (so sub-agent cost is folded into
    the task), attributed to the session's root agent. The model is the single
    model used, or "(mixed)" when a task spanned models. This is the "cost per
    task" unit and only shows foreground agents.
    unit="invocation": one sample per invoke_agent span (cost per agent turn);
    agent and model are unambiguous, so every agent — including sub-agents like
    Researcher Subagent or Explore — appears with its own model.

    Returns a pooled `summary` for the current filter (headline stat cards), a
    per agent x model `stats` breakdown (comparison table), and a log-bucketed
    `histogram` of the pooled filtered samples (distribution shape).
    """
    samples: list[tuple[str, str, float]] = []  # (agent, model, cost)
    if unit == "invocation":
        for tid in _search_trace_ids(start, end):
            try:
                trace = _fetch_trace(tid, start, end)
            except Exception:
                continue
            for sp in _index_spans(trace).values():
                if not (start <= _start_seconds(sp) <= end):
                    continue
                if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
                    continue
                cost = _num(sp, "gen_ai.usage.cost_usd")
                if cost <= 0:
                    continue
                m = str(_attr(sp, "gen_ai.request.model") or "unknown")
                samples.append((_agent_name(sp), m, cost))
    else:
        unit = "session"
        sess: dict[str, dict] = {}  # root cid -> {agent, model_costs, cost}
        for tid in _search_trace_ids(start, end):
            try:
                trace = _fetch_trace(tid, start, end)
            except Exception:
                continue
            spans = _index_spans(trace)
            cid, root_agent = _root_conversation(spans)
            if not cid:
                continue
            s = sess.setdefault(cid, {"agent": root_agent or "unknown",
                                      "model_costs": {}, "cost": 0.0})
            for sp in spans.values():
                if not (start <= _start_seconds(sp) <= end):
                    continue
                if _attr(sp, "gen_ai.operation.name") != "invoke_agent":
                    continue
                cost = _num(sp, "gen_ai.usage.cost_usd")
                if cost <= 0:
                    continue
                s["cost"] += cost
                m = str(_attr(sp, "gen_ai.request.model") or "unknown")
                s["model_costs"][m] = s["model_costs"].get(m, 0.0) + cost
        for s in sess.values():
            if s["cost"] <= 0:
                continue
            mc = s["model_costs"]
            # A task can span models (root on opus, a subagent on haiku, ...).
            # Don't pretend it was one model: label multi-model sessions "(mixed)".
            if not mc:
                model_label = "unknown"
            elif len(mc) == 1:
                model_label = next(iter(mc))
            else:
                model_label = "(mixed)"
            samples.append((s["agent"], model_label, s["cost"]))

    fsamples = [(a, m, c) for (a, m, c) in samples
                if (not agent or a == agent) and (not model or m == model)]

    # Per agent x model breakdown.
    groups: dict[tuple, list] = {}
    for a, m, c in fsamples:
        groups.setdefault((a, m), []).append(c)
    # Per-group monthly projections are intentionally omitted: percentile
    # projections do not sum across agents/models (the pool's p90 is not the sum
    # of per-group p90s), so a per-row projection would mislead when added up.
    # Only the pooled summary cards project cost; per-group rows show the
    # observed distribution and actual total.
    stats = []
    for (a, m), vals in groups.items():
        row = {"agent": a, "model": m, "group": f"{a} \u00b7 {m}", "unit": unit}
        row.update(_dist_stats(vals, 0.0))
        stats.append(row)
    stats.sort(key=lambda r: -r["total"])

    # Pooled summary for the current filter (headline cards).
    pooled = [c for _, _, c in fsamples]
    summary = {"unit": unit, "agent": agent or "All", "model": model or "All"}
    summary.update(_dist_stats(pooled, per_day))

    # Log-bucketed histogram of pooled samples, trimmed to the populated range.
    costs = sorted(pooled)
    hist = []
    if costs:
        edges = _COST_BUCKET_EDGES
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            cnt = sum(1 for c in costs if lo <= c < hi)
            hist.append({"bucket": f"{_money(lo)}\u2013{_money(hi)}",
                         "lo": lo, "hi": hi, "count": cnt})
        nz = [i for i, b in enumerate(hist) if b["count"] > 0]
        if nz:
            hist = hist[nz[0]:nz[-1] + 1]
    return {"summary": [summary], "stats": stats, "histogram": hist,
            "unit": unit, "n": len(fsamples)}


def build_timeline(conversation: str, start: int, end: int) -> dict:
    items = []
    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        cid, _ = _root_conversation(spans)
        if conversation and cid != conversation:
            continue
        for sp in spans.values():
            op = _attr(sp, "gen_ai.operation.name")
            if op not in ("invoke_agent", "execute_tool", "chat"):
                continue
            s = _start_seconds(sp)
            e = _end_seconds(sp)
            if e < s:
                e = s
            owner = _owner_agent(sp, spans) or "unknown"
            label = {
                "invoke_agent": _agent_name(sp),
                "execute_tool": _attr(sp, "gen_ai.tool.name") or "tool",
                "chat": _attr(sp, "gen_ai.request.model") or "chat",
            }.get(op, op)
            items.append({
                "agent": owner,
                "op": op,
                "label": label,
                "status": "error" if _is_error(sp) else "ok",
                "start_ms": int(s * 1000),
                "end_ms": int(e * 1000),
                "duration_ms": int((e - s) * 1000),
            })
    items.sort(key=lambda r: r["start_ms"])
    return {"timeline": items}


def build_timeline_states(conversation: str, start: int, end: int,
                          detail: bool = False, repo: str = "",
                          branch: str = "") -> dict:
    """Wide, time-indexed frame for the State timeline panel.

    Default: one lane per agent, active/error over its invoke_agent intervals.
    detail=True: one lane per (agent, category) where category is invoke / llm /
    tool, so each agent decomposes into Agent Invocation, LLM Operations, and
    Tool Calls sub-lanes (colored green/purple/blue, error red).
    """
    # category -> value string shown (and color-mapped) in the panel
    cat_of = {"invoke_agent": "invoke", "chat": "llm", "execute_tool": "tool"}
    intervals = []  # (lane, start_ms, end_ms, value, trace_id)
    lanes = set()
    for tid in _search_trace_ids(start, end):
        try:
            trace = _fetch_trace(tid, start, end)
        except Exception:
            continue
        spans = _index_spans(trace)
        if repo or branch:
            t_repo, t_branch = _trace_repo_branch(trace, spans)
            if repo and t_repo != repo:
                continue
            if branch and t_branch != branch:
                continue
        cid, _ = _root_conversation(spans)
        if conversation and cid != conversation:
            continue
        for sp in spans.values():
            op = _attr(sp, "gen_ai.operation.name")
            if detail:
                if op not in cat_of:
                    continue
                owner = _owner_agent(sp, spans) or "unknown"
                cat = cat_of[op]
                lane = f"{owner} \u00b7 {cat}"
                value = "error" if _is_error(sp) else cat
            else:
                if op != "invoke_agent":
                    continue
                lane = _agent_name(sp)
                value = "error" if _is_error(sp) else "active"
            s = int(_start_seconds(sp) * 1000)
            e = int(_end_seconds(sp) * 1000)
            if e < s:
                e = s
            intervals.append((lane, s, e, value, tid))
            lanes.add(lane)
    if not intervals:
        return {"states": [], "lanes": []}
    points = sorted({p for iv in intervals for p in (iv[1], iv[2])})
    lanes = sorted(lanes)
    rows = []
    for i in range(len(points) - 1):
        t0, t1 = points[i], points[i + 1]
        row = {"time": t0}
        seg_trace = None
        for lane in lanes:
            value = None
            for (ln, s, e, v, tr) in intervals:
                if ln == lane and s <= t0 and e >= t1:
                    value = "error" if (v == "error" or value == "error") else v
                    seg_trace = tr
            row[lane] = value
        row["traceid"] = seg_trace
        rows.append(row)
    rows.append({"time": points[-1], "traceid": None, **{lane: None for lane in lanes}})
    return {"states": rows, "lanes": lanes}


# --- Cache (short TTL + single-flight) -------------------------------------
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple] = {}  # key -> (expires_at, payload)


def _cached(key: tuple, builder, empty):
    """Short-TTL, single-flight cache; serve stale on error, else `empty`."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            payload = builder()
            _cache[key] = (now + CACHE_TTL, payload)
            return payload
        except Exception:
            if hit:
                return hit[1]
            return empty


def graph_for(start: int, end: int) -> dict:
    key = ("graph", start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_graph(start, end), _with_edge_schema({"nodes": [], "edges": []}))


def conversations_for(start: int, end: int, failures_only: bool = False,
                      conversation: str = "", repo: str = "", branch: str = "") -> dict:
    key = ("conv", failures_only, conversation, repo, branch,
           start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_conversations(start, end, failures_only,
                                                    conversation, repo, branch),
                   {"conversations": []})


def branches_for(start: int, end: int) -> dict:
    key = ("branches", start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_branches(start, end), {"branches": []})


def branch_series_for(start: int, end: int) -> dict:
    key = ("branch_series", start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_branch_series(start, end),
                   {"series": [], "lanes": []})


def cost_distribution_for(start: int, end: int, unit: str, agent: str,
                          model: str, per_day: float) -> dict:
    key = ("cost_dist", unit, agent, model, round(per_day, 3),
           start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key,
                   lambda: build_cost_distribution(start, end, unit, agent, model, per_day),
                   {"summary": [], "stats": [], "histogram": [], "unit": unit, "n": 0})


def timeline_for(conversation: str, start: int, end: int) -> dict:
    key = ("timeline", conversation, start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_timeline(conversation, start, end), {"timeline": []})


def timeline_states_for(conversation: str, start: int, end: int, detail: bool = False,
                        repo: str = "", branch: str = "") -> dict:
    key = ("states", detail, conversation, repo, branch,
           start // CACHE_BUCKET, end // CACHE_BUCKET)
    return _cached(key, lambda: build_timeline_states(conversation, start, end, detail,
                                                      repo, branch),
                   {"states": [], "lanes": []})


# --- HTTP server -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            self._send(200, b'{"status":"ok"}')
            return
        qs = urllib.parse.parse_qs(parsed.query)
        now = int(time.time())
        end = _coerce_epoch(qs.get("to", [None])[0], now)
        start = _coerce_epoch(qs.get("from", [None])[0], end - DEFAULT_LOOKBACK)
        if start >= end:
            start = end - DEFAULT_LOOKBACK

        if parsed.path in ("/graph.json", "/graph"):
            payload = graph_for(start, end)
        elif parsed.path in ("/conversations.json", "/conversations"):
            failures_only = qs.get("failures_only", [""])[0] in ("1", "true", "yes")
            conversation = qs.get("conversation", [""])[0]
            repo = _no_filter_if_all(qs.get("repo", [""])[0])
            branch = _no_filter_if_all(qs.get("branch", [""])[0])
            payload = conversations_for(start, end, failures_only, conversation, repo, branch)
        elif parsed.path in ("/branches.json", "/branches"):
            payload = branches_for(start, end)
            if qs.get("exclude_unknown", [""])[0] in ("1", "true", "yes"):
                payload = {"branches": [r for r in payload["branches"]
                                        if not (r["repo"] == "unknown" and r["branch"] == "unknown")]}
        elif parsed.path in ("/branch_series.json", "/branch_series"):
            payload = branch_series_for(start, end)
        elif parsed.path in ("/cost_distribution.json", "/cost_distribution"):
            unit = "invocation" if qs.get("unit", ["session"])[0] == "invocation" else "session"
            agent = _no_filter_if_all(qs.get("agent", [""])[0])
            model = _no_filter_if_all(qs.get("model", [""])[0])
            try:
                per_day = float(qs.get("per_day", ["0"])[0])
            except (TypeError, ValueError):
                per_day = 0.0
            payload = cost_distribution_for(start, end, unit, agent, model, per_day)
        elif parsed.path in ("/timeline.json", "/timeline"):
            conversation = qs.get("conversation", [""])[0]
            payload = timeline_for(conversation, start, end)
        elif parsed.path in ("/timeline_states.json", "/timeline_states"):
            conversation = qs.get("conversation", [""])[0]
            detail = qs.get("detail", [""])[0] in ("1", "true", "yes")
            repo = _no_filter_if_all(qs.get("repo", [""])[0])
            branch = _no_filter_if_all(qs.get("branch", [""])[0])
            payload = timeline_states_for(conversation, start, end, detail, repo, branch)
        else:
            self._send(404, b'{"error":"not found"}')
            return
        self._send(200, json.dumps(payload).encode("utf-8"))

    def log_message(self, *args) -> None:  # keep logs quiet
        return


def _no_filter_if_all(value: str) -> str:
    """Grafana sends the literal `$__all` for an includeAll variable whose
    allValue is empty; treat those sentinels as 'no filter'."""
    if value in ("$__all", "${__all}", "__all__", "all", "All"):
        return ""
    return value


def _coerce_epoch(raw, default: int) -> int:
    """Accept unix seconds or milliseconds; fall back to default."""
    if raw is None:
        return int(default)
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    if val > 10_000_000_000:  # looks like milliseconds
        val //= 1000
    return val


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_ADDR, PORT), Handler)
    print(f"agent-insights listening on {LISTEN_ADDR}:{PORT}, Tempo={TEMPO_URL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
