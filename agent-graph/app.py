#!/usr/bin/env python3
"""Agent-graph edge-builder sidecar.

Grafana's Node Graph panel needs two data frames (nodes + edges), but no LGTM
datasource can produce *agent* edges: all GitHub Copilot agents share one
service.name, subagent spans are span-kind INTERNAL, and no span attribute names
the parent agent. The parent -> child relationship is a GRANDPARENT hop in the
trace tree:

    invoke_agent (parent agent)
      └── execute_tool (runSubagent / task)
            └── invoke_agent (subagent)          <- child

This service walks each Tempo trace for the requested time window, derives that
hop into directed agent edges, aggregates per-agent stats (invocations, cost,
tokens, tool calls), and serves it as JSON for the Grafana Infinity datasource.

Design (see docs/dashboards.md and the research notes):
  * Compute-on-request, WINDOWED to ?from=&to= (unix seconds) so the graph stays
    consistent with the range-windowed Cost & Sessions / Agents dashboards.
    Infinity passes the panel range via ${__timeFrom:date:seconds} /
    ${__timeTo:date:seconds}.
  * Short-TTL in-memory cache keyed by the rounded window, with a single-flight
    lock, so the 30s auto-refresh and the two simultaneous nodes+edges requests
    collapse to one Tempo walk.
  * Always answers 200 with {"nodes":[],"edges":[]} on empty/error so the panel
    degrades to "No data" instead of erroring.

Stdlib only.
"""
from __future__ import annotations

import json
import os
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
            return _attr(cur, "gen_ai.agent.name")
        parent = cur.get("parentSpanId")
        cur = spans.get(parent) if parent else None
    return None


# --- Graph builder ---------------------------------------------------------
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
                agent = _attr(sp, "gen_ai.agent.name") or "unknown"
                n = node(agent)
                n["invocations"] += 1
                n["cost_usd"] += _num(sp, "gen_ai.usage.cost_usd")
                n["tokens_in"] += _num(sp, "gen_ai.usage.input_tokens")
                n["tokens_out"] += _num(sp, "gen_ai.usage.output_tokens")
                # Grandparent hop: parent execute_tool, grandparent invoke_agent.
                parent = spans.get(sp.get("parentSpanId")) if sp.get("parentSpanId") else None
                if parent is not None and _attr(parent, "gen_ai.operation.name") == "execute_tool":
                    gp = spans.get(parent.get("parentSpanId")) if parent.get("parentSpanId") else None
                    if gp is not None and _attr(gp, "gen_ai.operation.name") == "invoke_agent":
                        src = _attr(gp, "gen_ai.agent.name") or "unknown"
                        node(src)
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
    return {"nodes": node_frame, "edges": edge_frame}


# --- Cache (short TTL + single-flight) -------------------------------------
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple] = {}  # key -> (expires_at, payload)


def graph_for(start: int, end: int) -> dict:
    key = (start // CACHE_BUCKET, end // CACHE_BUCKET)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            payload = build_graph(start, end)
            _cache[key] = (now + CACHE_TTL, payload)
            return payload
        except Exception:
            # Serve stale on error if we have anything; else empty frames.
            if hit:
                return hit[1]
            return {"nodes": [], "edges": []}


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
        if parsed.path not in ("/graph.json", "/graph"):
            self._send(404, b'{"error":"not found"}')
            return
        qs = urllib.parse.parse_qs(parsed.query)
        now = int(time.time())
        end = _coerce_epoch(qs.get("to", [None])[0], now)
        start = _coerce_epoch(qs.get("from", [None])[0], end - DEFAULT_LOOKBACK)
        if start >= end:
            start = end - DEFAULT_LOOKBACK
        payload = graph_for(start, end)
        self._send(200, json.dumps(payload).encode("utf-8"))

    def log_message(self, *args) -> None:  # keep logs quiet
        return


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
    print(f"agent-graph listening on {LISTEN_ADDR}:{PORT}, Tempo={TEMPO_URL}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
