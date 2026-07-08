# GitHub Copilot telemetry on Grafana LGTM

Grafana dashboards for GitHub Copilot agent telemetry, running on the
[`grafana/otel-lgtm`](https://github.com/grafana/docker-otel-lgtm) all-in-one
OpenTelemetry backend (Loki, Grafana, Tempo, Mimir/Prometheus, plus an
OpenTelemetry Collector).

Both Copilot surfaces can emit OpenTelemetry traces, metrics, and events for
agent interactions, LLM calls, tool executions, and token usage:

- **VS Code Copilot Chat** (service name `copilot-chat`) — configured via VS Code
  settings.
- **GitHub Copilot CLI** (service name `github-copilot`) — configured via
  environment variables (a source-able script is included).

This repo wires that telemetry into a local LGTM container and provisions
ready-made dashboards for both.

## How it works

```mermaid
flowchart LR
  A["VS Code<br/>Copilot Chat"] -- "OTLP/HTTP :4318" --> B["OTel Collector"]
  H["Copilot CLI"] -- "OTLP/HTTP :4318" --> B
  subgraph LGTM["grafana/otel-lgtm container"]
    B -- metrics --> C["Prometheus :9090"]
    B -- traces --> D["Tempo :3200"]
    B -- logs --> E["Loki :3100"]
    C --> F["Grafana :3000"]
    D --> F
    E --> F
  end
  F --> G["Provisioned<br/>Copilot dashboards"]
```

## Prerequisites

- Docker with Compose v2 (`docker compose`)
- VS Code with GitHub Copilot Chat, and/or the GitHub Copilot CLI (`copilot`)

## Quick start

1. Start the LGTM stack:

   ```bash
   docker compose up -d
   ```

2. Enable Copilot telemetry export for the surface(s) you use:

   - **VS Code Copilot Chat** — the `github.copilot.chat.otel.*` settings are
     *application-scoped*, so they only take effect in your **User** settings
     (VS Code ignores them, with a warning, in a workspace
     `.vscode/settings.json`). Open the Command Palette and run
     **Preferences: Open User Settings (JSON)**, then merge in the keys from
     [examples/vscode-user-settings.jsonc](examples/vscode-user-settings.jsonc):

     ```json
     {
       "github.copilot.chat.otel.enabled": true,
       "github.copilot.chat.otel.exporterType": "otlp-http",
       "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318"
     }
     ```

   - **GitHub Copilot CLI** — source the env script before running `copilot`
     (see [GitHub Copilot CLI](#github-copilot-cli) below):

     ```bash
     source scripts/copilot-cli-otel.sh
     ```

3. Use Copilot Chat / agent mode (or the CLI) to generate some activity.

4. Open Grafana at <http://localhost:3000> (default login `admin` / `admin`).
   The **GitHub Copilot** dashboard folder contains the provisioned dashboards,
   and the overview dashboard is set as the home dashboard.

To stop the stack:

```bash
docker compose down          # keep collected data
docker compose down -v       # also delete the persisted data volume
```

## Enabling telemetry with environment variables

Instead of (or in addition to) VS Code settings, you can enable export with
environment variables, which take precedence:

```bash
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# Optional: tag telemetry so you can filter by team/dept in Grafana
export OTEL_RESOURCE_ATTRIBUTES="team.id=platform,department=engineering"
```

To capture full prompt/response/tool content into spans (visible in Tempo), set
`github.copilot.chat.otel.captureContent` to `true` or
`COPILOT_OTEL_CAPTURE_CONTENT=true`. This can include source code and sensitive
data — only enable it in a trusted local environment.

## GitHub Copilot CLI

The Copilot CLI is a separate surface from the VS Code extension. It reads its
OpenTelemetry configuration from environment variables (there is no
settings.json equivalent), so this repo ships a source-able script:

```bash
# Route CLI telemetry to the local LGTM stack for the current shell.
source scripts/copilot-cli-otel.sh

# Also capture prompts and responses (into trace span attributes).
COPILOT_CAPTURE_CONTENT=1 source scripts/copilot-cli-otel.sh

# Then run the CLI in the same shell.
copilot
```

On Windows PowerShell, dot-source the equivalent script:

```powershell
. .\scripts\copilot-cli-otel.ps1                 # enable telemetry
. .\scripts\copilot-cli-otel.ps1 -CaptureContent # also capture prompts/responses
```

The script sets the variables documented in the
[Copilot CLI OpenTelemetry reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#opentelemetry-monitoring):

| Variable | Value set by the script | Purpose |
|----------|-------------------------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | LGTM collector; also enables OTel |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | CLI supports HTTP only (no gRPC) |
| `OTEL_SERVICE_NAME` | `github-copilot` | Identifies CLI telemetry |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `false` (opt-in) | Capture prompts/responses |

How the CLI differs from the VS Code extension:

- **Service name** is `github-copilot` (the extension is `copilot-chat`), which
  becomes the Prometheus `job` label and the Tempo `service.name`.
- **Content capture** uses `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`,
  not `COPILOT_OTEL_CAPTURE_CONTENT`.
- **Vendor metrics** use the `github.copilot.*` namespace (for example
  `github.copilot.tool.call.count`) instead of `copilot_chat.*`. Shared
  `gen_ai.*` metrics (token usage, operation duration) are emitted by both.
- **Prompts and responses** land as span attributes (`gen_ai.input.messages` /
  `gen_ai.output.messages`) on the `invoke_agent` and `chat` traces in Tempo —
  not in logs. Explore them in Grafana with the Tempo data source, filtering by
  `service.name = github-copilot`.

## Dashboards

| Dashboard | File | Surface | Highlights |
|-----------|------|---------|------------|
| GitHub Copilot - Overview | [copilot-overview.json](grafana/dashboards/copilot-overview.json) | Shared + VS Code | Sessions, input/output tokens, token rate by model, LLM call duration, time to first token, tool calls |
| GitHub Copilot - Tools & Agent Activity | [copilot-tools-activity.json](grafana/dashboards/copilot-tools-activity.json) | VS Code extension | Tool call counts and latency, edit accept/reject decisions, lines of code changed, agent invocation duration |
| GitHub Copilot - CLI | [copilot-cli.json](grafana/dashboards/copilot-cli.json) | Copilot CLI | Tokens, LLM/agent duration, time to first chunk, tool calls by tool/outcome, tool latency |

The token and LLM-duration panels on the overview dashboard use shared `gen_ai.*`
metrics, so they include both surfaces. Use the CLI dashboard's **Service (job)**
variable to isolate `github-copilot` telemetry.

Traces (the `invoke_agent` -> `chat` -> `execute_tool` span tree) are stored in
Tempo. Open **Explore** in Grafana, select the Tempo data source, and filter by
`service.name = copilot-chat` (VS Code) or `service.name = github-copilot` (CLI)
to inspect individual agent runs.

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
| `gen_ai.client.operation.duration` | `gen_ai_client_operation_duration_seconds_bucket` |
| `copilot_chat.session.count` | `copilot_chat_session_count_total` |
| `copilot_chat.tool.call.count` | `copilot_chat_tool_call_count_total` |
| `github.copilot.tool.call.count` (CLI) | `github_copilot_tool_call_count_total` |
| `github.copilot.tool.call.duration` (CLI) | `github_copilot_tool_call_duration_seconds_bucket` |
| `gen_ai.client.operation.time_to_first_chunk` (CLI) | `gen_ai_client_operation_time_to_first_chunk_seconds_bucket` |
| `copilot_chat.tool.call.duration` | `copilot_chat_tool_call_duration_milliseconds_bucket` |
| `copilot_chat.time_to_first_token` | `copilot_chat_time_to_first_token_seconds_bucket` |

If a panel shows "No data", the exact series name or a label may differ in your
setup. Confirm the real names in Grafana **Explore** with the Prometheus data
source (use the metrics browser, or query `{__name__=~"copilot.*|gen_ai.*"}`),
then adjust the panel query.

## Add your own dashboards

Drop any Grafana dashboard JSON file into
[grafana/dashboards/](grafana/dashboards/) and restart the container:

```bash
docker compose restart lgtm
```

The provisioning provider in
[grafana/provisioning/dashboards/copilot.yaml](grafana/provisioning/dashboards/copilot.yaml)
auto-loads every JSON in that folder into the **GitHub Copilot** folder. Each
dashboard needs a unique `uid` and `title`. Reference Prometheus via the
`datasource` template variable (type `datasource`, query `prometheus`) so the
dashboard works regardless of the data source UID.

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 3000 | Grafana | UI, `admin` / `admin` |
| 4317 | OTLP/gRPC | Telemetry ingest |
| 4318 | OTLP/HTTP | Telemetry ingest (Copilot default) |
| 9090 | Prometheus | Metrics, optional |
| 3200 | Tempo | Traces, optional |

## References

- [Monitor agent usage with OpenTelemetry](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
- [grafana/docker-otel-lgtm](https://github.com/grafana/docker-otel-lgtm)
