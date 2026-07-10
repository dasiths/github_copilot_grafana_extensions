# Dashboards

The stack provisions six dashboards into the **GitHub Copilot** folder in
Grafana. This page describes each one, explains the Cost & Sessions data model,
covers Prometheus metric naming, and shows how to add your own dashboards.

**Overview** is the home dashboard, and every dashboard has a **Dashboards**
dropdown in the top bar (a dashboard link keyed on the `copilot` tag) to navigate
between them while keeping the selected time range.

## Overview of the provisioned dashboards

| Dashboard | File | Source | Surface | Highlights |
|-----------|------|--------|---------|------------|
| GitHub Copilot - Overview | [copilot-overview.json](../grafana/dashboards/copilot-overview.json) | Metrics (Prometheus) | Both (CLI + VS Code) | **Home dashboard.** Sessions, input/output tokens, token rate by model, LLM call duration, time to first token/chunk, tool calls. Tool calls, duration, and time-to-first are cross-surface; token/session **counts** come from VS Code metrics (CLI token detail is on the span dashboards). Has a **Source (service)** filter |
| GitHub Copilot - Tools & Agent Activity | [copilot-tools-activity.json](../grafana/dashboards/copilot-tools-activity.json) | Metrics (Prometheus) | VS Code (+ CLI tool calls) | Tool call counts/latency (cross-surface), plus VS Code editor metrics with no CLI equivalent: edit accept/reject decisions, lines of code changed, agent invocation duration |
| GitHub Copilot - Cost & Sessions | [copilot-cost-sessions.json](../grafana/dashboards/copilot-cost-sessions.json) | Spans (Tempo) | Both (CLI + VS Code) | Estimated USD cost + tokens up top, then Sessions / Agent invocations / Requests tables (tokens, cache, `cost_usd`), with **Source**, **Model**, **Repository**, **Branch**, and **Session** filters |
| GitHub Copilot - Agents | [copilot-agents.json](../grafana/dashboards/copilot-agents.json) | Metrics (Prometheus) | Both (CLI + VS Code) | Per-agent breakdown (`gen_ai.agent.name`): invocations, cost, tokens, duration p95, and activity over time. Most useful with multi-agent CLI runs (subagents via the `task` tool) |
| GitHub Copilot - Agent Graph | [copilot-agent-graph.json](../grafana/dashboards/copilot-agent-graph.json) | Traces via the `agent-insights` sidecar (Infinity) | Both (CLI + VS Code) | Node Graph of the agent topology: nodes = agents, directed edges = parent agent → subagent, with per-node invocations, cost, tokens, and tool calls |
| GitHub Copilot - Agent Timeline | [copilot-agent-timeline.json](../grafana/dashboards/copilot-agent-timeline.json) | Traces via the `agent-insights` sidecar (Infinity) | Both (CLI + VS Code) | Conversation-first view: per-conversation summary table (duration, model/tool calls, agents, cost, **failures**) with a **Failures only** toggle and **Repository** / **Branch** filters, plus a per-agent swim-lane timeline |
| GitHub Copilot - Cost by Repo & Branch | [copilot-branches.json](../grafana/dashboards/copilot-branches.json) | Traces via the `agent-insights` sidecar (Infinity) | Both (CLI + VS Code) | Cost, tokens, cache reuse, and sessions grouped by git **repository** (primary) and **branch** (secondary), with a cost bar chart, a **cost-over-time** series, and a filterable repo/branch table that links into the Agent Timeline |

VS Code and the CLI emit the **same `gen_ai.*` token and duration metrics**, and
the metric dashboards union the surface-specific names where they differ
(`copilot_chat_tool_call_count_total` + `github_copilot_tool_call_count_total`
for tool calls; the `gen_ai.client.operation.*` variants with/without the
`_seconds` suffix for durations and time-to-first). So the **Overview** dashboard
covers VS Code and the CLI together, and its **Source (service)** filter isolates
`copilot-chat` (VS Code) from `github-copilot` (CLI). A few concepts exist on
only one surface: token/session **counts as metrics** and the editor metrics
(edit decisions, lines of code) are VS Code only, while CLI token/cost detail
lives on the span-based dashboards. A dedicated CLI dashboard is not needed.

> **CLI agent naming:** the CLI's top-level agent leaves `gen_ai.agent.name`
> unset and only sets `gen_ai.agent.id` (`github.copilot.default`). The collector
> ([otelcol/otelcol-config.yaml](../otelcol/otelcol-config.yaml)) and the
> `agent-insights` sidecar both fall back to the id (surfaced as **Copilot CLI**) so
> CLI activity is attributed to a named agent instead of `unknown`.

## GitHub Copilot - Cost & Sessions

This is a span-based dashboard that leads with **estimated USD cost and
tokens**. Cost, cache tokens, and per-session detail live only on trace spans
(not metrics), so the dashboard reads them from Tempo. See
[Cost estimation and Prometheus metrics](cost-and-metrics.md) for how the cost
figure is computed.

- The **Session** variable filters by `gen_ai.conversation.id` (regex; copy an id
  from the table, or click a **Session** cell in the Sessions table to filter to
  just that session — this scopes the invocations and requests tables too). Use
  the **Clear session filter** link in the dashboard's top bar (or reset the
  Session textbox to `.*`) to clear the filter. **Source** and **Model** filter
  by service and model.
- The **Repository** and **Branch** variables scope the cost/token/session tables
  to one git repo or branch. Their options come from the `agent-insights` sidecar's
  `/branches.json`, and each value is a ready-made TraceQL predicate that
  coalesces the two surfaces (VS Code's `github.copilot.git.branch` span
  attribute OR the CLI's `vcs.ref.head.name` resource attribute). With **All**
  selected the predicate collapses to `true`, so totals are unfiltered and match
  the pre-filter behavior. The **Requests** table (chat spans) is intentionally
  not branch-scoped, because the branch lives on the `invoke_agent` span, not on
  child `chat` spans. The `unknown` bucket (telemetry with no git enrichment) is
  omitted from these dropdowns because Tempo cannot search for an absent
  attribute; use the **Cost by Repo & Branch** dashboard to see it.
- The telemetry is hierarchical: a **session** (`gen_ai.conversation.id`) contains
  one or more **agent invocations** (`invoke_agent` spans, one per user message),
  each of which makes several **requests** (`chat` spans, one LLM call each). The
  dashboard mirrors this with three tables, namely **Sessions** (grouped by
  `conversation.id`, totals per session), **Agent invocations** (one row per
  `invoke_agent` span), and **Requests** (one row per `chat` span), plus stat
  cards for cost, tokens, cache, and sessions. Everything is computed from **Tempo
  Search** (full trace retention), so it stays populated as long as traces exist.
- Span names include a model suffix (for example `chat claude-opus-4.8`), so the
  queries match by regex (`name =~ "chat.*"` / `name =~ "invoke_agent.*"`) to
  catch both surfaces. Per-request input tokens overlap (context is re-sent each
  turn), so trust the invocation-level totals rather than summing requests.

## GitHub Copilot - Agents

The Agents dashboard is metric-based and breaks activity down by
`gen_ai.agent.name`: agent count, invocations, estimated cost, tool calls,
per-agent cost and token barcharts, invocation duration p95, and activity over
time. It is most useful with multi-agent CLI runs, where subagents (invoked via
the `task` tool) each report under their own agent name. The metrics behind it,
and the windowed-delta query that keeps its totals in step with the Cost &
Sessions dashboard, are documented in
[Cost estimation and Prometheus metrics](cost-and-metrics.md#cost-as-a-prometheus-metric).

## GitHub Copilot - Agent Graph

The Agent Graph dashboard renders the **agent topology** as a Grafana Node Graph:
nodes are agents (`gen_ai.agent.name`) and directed edges are *parent agent →
subagent*, with per-node stats (invocations, estimated cost, tokens, tool calls).

No LGTM datasource can produce agent edges natively: every Copilot agent shares
one `service.name`, subagent spans are span-kind `INTERNAL`, and no span
attribute names the parent agent. The parent → child relationship is a
*grandparent hop* in the trace tree:

```text
invoke_agent (parent agent)
  └── execute_tool (runSubagent / task)
        └── invoke_agent (subagent)
```

The [agent-insights](../agent-insights/) sidecar walks each Tempo trace for the
selected time range, derives that hop into directed edges, aggregates per-agent
stats, and serves them as `{"nodes":[...],"edges":[...]}` JSON. The
[Grafana Infinity datasource](../grafana/provisioning/datasources/infinity.yaml)
(preinstalled via `GF_PLUGINS_PREINSTALL_SYNC` in
[docker-compose.yml](../docker-compose.yml)) reads that JSON into the Node Graph
panel with two queries (`nodes` and `edges`).

- **Windowed:** the panel passes the dashboard time range to the sidecar via
  Infinity's backend time macros (`${__timeFrom:date:seconds}` /
  `${__timeTo:date:seconds}` → Tempo `start`/`end`), so the graph stays in step
  with the other range-windowed dashboards.
- **Freshness:** the sidecar caches each window for ~30s (a single-flight,
  short-TTL cache) so the 30s auto-refresh and the two simultaneous
  `nodes`+`edges` requests collapse to one Tempo walk.
- **Reach-back:** limited by Tempo's block retention (~48h by default in this
  image).
- Most useful with multi-agent runs, where subagents (VS Code `runSubagent` or
  the CLI `task` tool) report under their own agent name.

## GitHub Copilot - Agent Timeline

The Agent Timeline dashboard is a **conversation-first** view, inspired by
agent-timeline tools: it makes whole multi-agent conversations, and their
failures, the unit of investigation.

- **Conversation summary cards** — headline stats for the conversation selected
  in the **Conversation** variable: duration, traces, LLM calls, tool calls,
  **failures** (red when > 0), and total tokens. With the box empty they
  aggregate across all conversations in range.
- **Conversations table** — one row per conversation, grouped by the *root*
  agent's `gen_ai.conversation.id` (a trace holds one turn: a root `invoke_agent`
  plus nested subagent `invoke_agent` spans that each carry their own
  conversation id, so the sidecar keys on the root). Columns: agents involved,
  invocations, model calls (`chat`), tool calls (`execute_tool`), **failures**
  (spans with `STATUS_CODE_ERROR`, highlighted red), duration, cost, and tokens.
- **Failures only** — the **View** variable adds `failures_only=1` to the sidecar
  request so the table shows only conversations that contain a failure.
- **Repository / Branch** — the **Repository** and **Branch** variables (options
  from the sidecar's `/branches.json`) scope the whole dashboard to one git repo
  or branch. They are passed to the sidecar as `?repo=&branch=`, which filters
  each trace by its coalesced repo/branch before aggregating. **All** leaves them
  empty (no filter). Unlike the Cost & Sessions filters, `unknown` is selectable
  here because the sidecar matches it by exact name server-side.
- **Click to filter** — click a conversation id in the table to filter the
  whole dashboard (table, summary cards, swim lanes, and traces) to that
  conversation (a table data link sets the `conversation` variable); the **Show
  all conversations** link in the About panel clears it again. With no
  conversation selected the table lists every conversation and the traces panel
  shows all traces in range.
- **Swim lanes** — paste a conversation id into the **Conversation** variable to
  render a per-agent [State timeline](https://grafana.com/docs/grafana/latest/panels-visualizations/visualizations/state-timeline/).
  Each agent decomposes into three lanes — **Agent invocation** (green), **LLM
  operations** (purple), and **Tool calls** (blue) — active over the matching
  spans, with failing spans in **red**. This shows parallel execution and
  parent → subagent handoffs over time. **Click a block** to open that segment's
  trace **span waterfall** in Explore (the sidecar tags each segment with its
  `traceid`, and a panel data link opens it on the Tempo datasource).
- **Traces in conversation** — a Tempo table of the conversation's traces
  (matched on `gen_ai.conversation.id`); click a Trace ID to open the full span
  **waterfall**.

The data comes from the [agent-insights](../agent-insights/) sidecar, which serves
these JSON endpoints consumed via Infinity, all windowed to the dashboard time
range (`?from=&to=` unix seconds):

| Endpoint | Feeds | Shape |
|----------|-------|-------|
| `/conversations.json` (opt. `?conversation=`, `?failures_only=1`, `?repo=`, `?branch=`) | Summary cards + Conversations table | `{"conversations": [ ... ]}` |
| `/timeline_states.json?conversation=<id>&detail=1` (opt. `?repo=`, `?branch=`) | Swim-lane State timeline | wide `{"states": [ {"time", "<agent> · <cat>": "invoke"/"llm"/"tool"/"error"/null } ]}` |
| `/timeline.json?conversation=<id>` | raw per-span intervals | `{"timeline": [ ... ]}` |
| `/graph.json` | Agent Graph Node Graph | `{"nodes": [], "edges": []}` |

The conversation's trace **waterfall** itself comes straight from the Tempo
datasource (not the sidecar), via the Traces panel's TraceQL query.

The swim lanes use the built-in State timeline panel rather than a Gantt plugin,
because the community Gantt panel is unmaintained and does not load on current
Grafana.

## GitHub Copilot - Cost by Repo & Branch

This dashboard answers "**where is my Copilot spend going?**" by attributing
cost, tokens, cache reuse, and sessions to the git **repository** (primary
grouping) and **branch** (secondary grouping) the work happened on.

- **Summary cards** — totals across every repo/branch in range: cost, input and
  output tokens, cache-read tokens, sessions, and invocations.
- **Cost by repo · branch** — a horizontal bar chart, one bar per `repo · branch`,
  colored by cost, so heavy branches stand out at a glance.
- **Cost over time by repo · branch** — a stacked bar time series of cost per
  time bucket, one series per `repo · branch`, to spot *when* a branch got
  expensive. Bucket width scales with the selected range.
- **Repos & branches table** — one row per `repo · branch`, with the agents
  involved, invocations, sessions (distinct `gen_ai.conversation.id`), cost, and
  token breakdown. Each column has a **filter icon**, so you can filter to a
  single repository or branch; the table footer sums the visible rows. **Click a
  branch** to open the [Agent Timeline](#github-copilot---agent-timeline) scoped
  to that repo and branch (a data link sets its `repo`/`branch` variables).

The repo/branch key is **coalesced across surfaces** by the
[agent-insights](../agent-insights/) sidecar, because the two Copilot surfaces tag it
differently:

- **VS Code** sets it per `invoke_agent` **span**: `github.copilot.git.repository`
  (a clone URL) and `github.copilot.git.branch`. No setup required.
- **The CLI** sets it on the OTel **resource** (`vcs.repository.name` /
  `vcs.ref.head.name`) once [the enrichment script](../scripts/) is sourced —
  `_index_spans` in the sidecar discards resource attributes, so `build_branches`
  reads `trace["batches"][*]["resource"]` directly for CLI rows.

The sidecar normalizes both repo shapes to a bare repo name (basename minus
`.git`) so the same repository groups identically no matter which surface
produced the telemetry. Rows with no git enrichment (a CLI run before the script
is sourced) fall into **`unknown · unknown`**.

The data comes from two windowed Infinity endpoints:

| Endpoint | Feeds | Shape |
|----------|-------|-------|
| `/branches.json` (opt. `?exclude_unknown=1`) | Summary cards + bar chart + table (and the Cost & Sessions / Agent Timeline filter dropdowns) | `{"branches": [ {"repo", "branch", "repo_branch", "repo_clause", "branch_clause", "agents", "sessions", "invocations", "cost_usd", "tokens_in", "tokens_out", "cache_read_tokens", ... } ]}` |
| `/branch_series.json` | Cost-over-time series | wide `{"series": [ {"time", "<repo · branch>": cost, ... } ], "lanes": [ ... ]}` |

The `repo_clause` / `branch_clause` fields are prebuilt TraceQL predicates the
Cost & Sessions filters use as variable values (see that dashboard above);
`?exclude_unknown=1` drops the non-filterable `unknown` bucket for those
dropdowns.

> **Why a sidecar and not TraceQL?** A single TraceQL query cannot coalesce a
> branch that lives on a **span attribute** (VS Code) with one that lives on a
> **resource attribute** (CLI). The sidecar merges both into one grouping key in
> Python, and does it without touching the collector — so no `deltatocumulative`
> metric reset. See [telemetry.md](telemetry.md) for how the CLI branch is set
> and its per-launch freshness boundary.

## Metric naming note

The Copilot SDK emits OpenTelemetry metrics such as `gen_ai.client.token.usage`
and `copilot_chat.tool.call.count`. The LGTM container forwards these to
Prometheus, which normalizes the names: dots become underscores, counters gain a
`_total` suffix, histograms expand into `_bucket` / `_sum` / `_count`, and unit
suffixes (for example `_seconds`, `_milliseconds`) are appended. The provisioned
dashboards use these normalized names, for example:

| OpenTelemetry instrument | Prometheus series used in dashboards |
|--------------------------|--------------------------------------|
| `gen_ai.client.token.usage` | `gen_ai_client_token_usage_sum`, `_count`, `_bucket` |
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_bucket` (VS Code) / `gen_ai_client_operation_duration_seconds_bucket` (CLI) |
| `copilot_chat.session.count` | `copilot_chat_session_count_total` |
| `copilot_chat.tool.call.count` | `copilot_chat_tool_call_count_total` |
| `copilot_chat.tool.call.duration` | `copilot_chat_tool_call_duration_bucket` (values in ms) |
| `copilot_chat.time_to_first_token` | `copilot_chat_time_to_first_token_bucket` |
| `copilot_chat.agent.invocation.duration` | `copilot_chat_agent_invocation_duration_bucket` |
| `github.copilot.tool.call.count` (CLI) | `github_copilot_tool_call_count_total` |
| `github.copilot.tool.call.duration` (CLI) | `github_copilot_tool_call_duration_seconds_bucket` |
| `gen_ai.client.operation.time_to_first_chunk` (CLI) | `gen_ai_client_operation_time_to_first_chunk_seconds_bucket` |

Whether Prometheus appends a unit suffix (for example `_seconds`) depends on the
unit metadata the emitter attaches. The VS Code extension's `copilot_chat.*`
duration histograms arrive without a unit suffix, while the CLI's `gen_ai.*`
durations arrive as `_seconds`. If a panel shows "No data", confirm the real
names in Grafana **Explore** with the Prometheus data source (use the metrics
browser, or query `{__name__=~"copilot.*|gen_ai.*"}`), then adjust the query.

## Add your own dashboards

Drop any Grafana dashboard JSON file into
[../grafana/dashboards/](../grafana/dashboards/) and restart the container:

```bash
docker compose restart lgtm
```

The provisioning provider in
[../grafana/provisioning/dashboards/copilot.yaml](../grafana/provisioning/dashboards/copilot.yaml)
auto-loads every JSON in that folder into the **GitHub Copilot** folder. Each
dashboard needs a unique `uid` and `title`. Reference Prometheus via the
`datasource` template variable (type `datasource`, query `prometheus`) so the
dashboard works regardless of the data source UID.
