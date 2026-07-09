# Dashboards

The stack provisions four dashboards into the **GitHub Copilot** folder in
Grafana. This page describes each one, explains the Cost & Sessions data model,
covers Prometheus metric naming, and shows how to add your own dashboards.

## Overview of the provisioned dashboards

| Dashboard | File | Source | Surface | Highlights |
|-----------|------|--------|---------|------------|
| GitHub Copilot - Overview | [copilot-overview.json](../grafana/dashboards/copilot-overview.json) | Metrics (Prometheus) | Shared + VS Code | Sessions, input/output tokens, token rate by model, LLM call duration, time to first token, tool calls. Has a **Source (service)** filter to isolate VS Code vs CLI |
| GitHub Copilot - Tools & Agent Activity | [copilot-tools-activity.json](../grafana/dashboards/copilot-tools-activity.json) | Metrics (Prometheus) | VS Code extension | Tool call counts and latency, edit accept/reject decisions, lines of code changed, agent invocation duration |
| GitHub Copilot - Cost & Sessions | [copilot-cost-sessions.json](../grafana/dashboards/copilot-cost-sessions.json) | Spans (Tempo) | Both (CLI + VS Code) | **Home dashboard.** Estimated USD cost + tokens up top, then Sessions / Agent invocations / Requests tables (tokens, cache, `cost_usd`), with **Source**, **Model**, and **Session** filters |
| GitHub Copilot - Agents | [copilot-agents.json](../grafana/dashboards/copilot-agents.json) | Metrics (Prometheus) | Both (CLI + VS Code) | Per-agent breakdown (`gen_ai.agent.name`): invocations, cost, tokens, duration p95, and activity over time. Most useful with multi-agent CLI runs (subagents via the `task` tool) |

Both metric surfaces share the same `gen_ai.*` token and duration metrics, so the
Overview dashboard covers VS Code and the CLI together; its **Source (service)**
filter isolates `copilot-chat` (VS Code) from `github-copilot` (CLI). A dedicated
CLI dashboard is not needed: the shared metrics plus the source filter, together
with the span-based Cost & Sessions dashboard, cover the CLI.

## GitHub Copilot - Cost & Sessions

This is the default/home dashboard and leads with **estimated USD cost and
tokens**. Cost, cache tokens, and per-session detail live only on trace spans
(not metrics), so the dashboard reads them from Tempo. See
[Cost estimation and Prometheus metrics](cost-and-metrics.md) for how the cost
figure is computed.

- The **Session** variable filters by `gen_ai.conversation.id` (regex; copy an id
  from the table). **Source** and **Model** filter by service and model.
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
and the reason per-agent totals use `max_over_time`, are documented in
[Cost estimation and Prometheus metrics](cost-and-metrics.md#cost-as-a-prometheus-metric).

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
