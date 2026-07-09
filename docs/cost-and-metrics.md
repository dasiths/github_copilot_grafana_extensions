# Cost estimation and Prometheus metrics

Cost, cache tokens, and per-session detail live on trace spans, and the OTel
Collector also projects them into Prometheus metrics. This page explains how cost
is computed and which metrics are available.

## Estimated cost

Cost is shown as **`gen_ai.usage.cost_usd`**, computed at ingest by the OTel
Collector, not by the dashboard. The collector's transform
([../otelcol/otelcol-config.yaml](../otelcol/otelcol-config.yaml)) applies, per
span:

```text
billed_input = max(0, input_tokens - cache_read - cache_creation)
cost_usd = (billed_input   * rate_in
          + output_tokens  * rate_out
          + cache_read     * rate_cache_read
          + cache_creation * rate_cache_write) / 1e6
```

Rates are **USD per 1,000,000 tokens**, matched per model (with a `default`
fallback). This mirrors the approach of the
[Aspire CopilotCost extension](https://github.com/cicorias/copilot-cost-dashshboard-aspire/tree/main/extension/CopilotCost).
The rates in the config are **illustrative list prices, so edit them** to match
your plan, then restart the container (`docker compose restart lgtm`).

> The provider does emit a raw `github.copilot.cost` (CLI only, unit-opaque, not
> USD) and `github.copilot.nano_aiu` (÷1e9 = the CLI's "AI credits"), but the
> dashboards use the collector-computed USD estimate instead.

## Cost as a Prometheus metric

The collector runs a **`sum` connector** that projects the per-span cost,
AI-unit, and token attributes into Prometheus metrics, so they are available to
*any* dashboard with full retention (not just Tempo Search).

Each metric carries **exactly one grouping attribute**. This is deliberate: the
`sum` connector multiplies the summed value by the number of declared
attributes, so a metric with two attributes would double every value. To keep
counts exact, cost is emitted twice, as a **model-scoped** series and an
**agent-scoped** series:

| Metric | Meaning | Labels |
|--------|---------|--------|
| `copilot_cost_usd_total` | Summed `gen_ai.usage.cost_usd` over `invoke_agent` spans | `gen_ai_request_model`, `job` (source) |
| `copilot_nano_aiu_total` | Summed `github.copilot.nano_aiu` (÷1e9 = AI credits) | `gen_ai_request_model`, `job` (source) |
| `copilot_agent_cost_usd_total` | Same cost, grouped by agent | `gen_ai_agent_name`, `job` |
| `copilot_agent_tokens_input_total` | Summed `gen_ai.usage.input_tokens` | `gen_ai_agent_name`, `job` |
| `copilot_agent_tokens_output_total` | Summed `gen_ai.usage.output_tokens` | `gen_ai_agent_name`, `job` |
| `copilot_agent_tokens_cache_read_total` | Summed `gen_ai.usage.cache_read.input_tokens` | `gen_ai_agent_name`, `job` |
| `copilot_agent_tokens_cache_write_total` | Summed `gen_ai.usage.cache_creation.input_tokens` | `gen_ai_agent_name`, `job` |
| `copilot_agent_calls_total` | Span counts per agent (`spanmetrics` connector) | `gen_ai_agent_name`, `gen_ai_operation_name`, `job` |
| `copilot_agent_duration_seconds_*` | Span duration histogram per agent | `gen_ai_agent_name`, `gen_ai_operation_name`, `job` |

The `copilot_agent_*` metrics power the [Agents dashboard](dashboards.md#github-copilot---agents).
Filter `gen_ai_operation_name="invoke_agent"` for agent invocations,
`"execute_tool"` for tool calls, or `"chat"` for LLM calls, grouped by
`gen_ai_agent_name` (for example `GitHub Copilot Chat`, or CLI subagents like
`rpiv-research`).

> These `sum`-connector counters are cumulative but stop updating when an agent
> goes idle, so Prometheus marks them *stale* after ~5 minutes. Dashboard panels
> that show per-agent totals use `max_over_time(<metric>[$__range])` instead of
> an instant read, so idle agents still appear across the selected time window.

> The `sum` connector emits *delta* metrics that the `deltatocumulative`
> processor turns into cumulative counters for Prometheus. Its default
> `max_stale` (5 minutes) would **drop and reset** a stream once its agent goes
> idle, permanently undercounting cost and tokens for bursty/intermittent
> agents. The collector config sets `max_stale: 720h` so the running totals
> survive idle gaps. If per-agent totals ever read *lower* than the same
> metric's `max_over_time`, a reset has occurred — check this setting.

For example, cost added per interval by model:
`sum by (gen_ai_request_model) (increase(copilot_cost_usd_total[$__rate_interval]))`.
Because these are metrics, they work on the Overview dashboard too and are not
subject to Tempo's span/metrics retention limits.

> Two caveats: (1) cost (`cost_usd` on spans and the `copilot_*_total` metrics)
> is only produced for telemetry **ingested after** the collector was configured,
> so historical data shows blank cost until new activity flows. (2) The Cost &
> Sessions *tables* are Search-based (not TraceQL *metrics*), because this image's
> TraceQL metrics only retain a short recent window; Search keeps the per-session
> tables populated when a session goes idle.
