# Enabling Copilot telemetry

Both Copilot surfaces can export OpenTelemetry traces, metrics, and events to the
local LGTM stack. This page covers how to turn that on for each surface.

- **VS Code Copilot Chat** reports as service name `copilot-chat`.
- **GitHub Copilot CLI** reports as service name `github-copilot`.

The service name becomes the Prometheus `job` label and the Tempo
`service.name`, so you can filter one surface from the other in Grafana.

## VS Code Copilot Chat

The `github.copilot.chat.otel.*` settings are *application-scoped*, so they only
take effect in your **User** settings. VS Code ignores them (with a warning) in a
workspace `.vscode/settings.json`.

Open the Command Palette, run **Preferences: Open User Settings (JSON)**, then
merge in the keys from [../examples/vscode-user-settings.jsonc](../examples/vscode-user-settings.jsonc):

```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318"
}
```

### Environment variables

Instead of (or in addition to) VS Code settings, you can enable export with
environment variables, which take precedence:

```bash
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# Optional: tag telemetry so you can filter by team/dept in Grafana
export OTEL_RESOURCE_ATTRIBUTES="team.id=platform,department=engineering"
```

### Capturing prompt and response content

To capture full prompt, response, and tool content into spans (visible in Tempo),
set `github.copilot.chat.otel.captureContent` to `true`, or
`COPILOT_OTEL_CAPTURE_CONTENT=true`. This can include source code and sensitive
data, so only enable it in a trusted local environment.

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

### Tagging telemetry with the git repository and branch

The script also tags CLI telemetry with the current git repository and branch so
you can group and filter cost by repo (primary) and branch (secondary) on the
**Cost by Repo & Branch** dashboard. It sets the vendor-neutral OpenTelemetry
[VCS resource attributes](https://opentelemetry.io/docs/specs/semconv/attributes-registry/vcs/):

| Attribute | Source | Example |
|-----------|--------|---------|
| `vcs.repository.name` | `basename` of the remote URL | `github_copilot_grafana_extensions` |
| `vcs.ref.head.name` | `git rev-parse --abbrev-ref HEAD` | `main` |
| `vcs.ref.head.revision` | `git rev-parse --short HEAD` | `6c8261c` |
| `vcs.repository.url.full` | `git config remote.origin.url` | `https://github.com/...` |

These go on the OTel **resource** via `OTEL_RESOURCE_ATTRIBUTES`, which the CLI
reads once at process start. Because a resource is immutable per process, the
script refreshes the variable before **every shell prompt** (bash `PROMPT_COMMAND`,
zsh `precmd_functions`, PowerShell wraps the `prompt` function). This means you
can run `copilot` directly with no wrapper — each launch picks up the branch you
are currently on. Switch branches with `git checkout`, and the next `copilot`
run is tagged with the new branch. Any `OTEL_RESOURCE_ATTRIBUTES` you set before
sourcing the script (for example `team.id=platform`) is preserved and merged.

> Freshness boundary: the branch is captured at CLI **launch**, not per span. If
> you switch branches mid-session, restart `copilot` to re-tag. Values are read
> from whatever directory the shell is in; run `copilot` from inside the repo.

Until the script is sourced, CLI runs carry no git attributes and appear under
`unknown` on the Cost by Repo & Branch dashboard. VS Code needs no setup — it
already tags each `invoke_agent` span with `github.copilot.git.repository` and
`github.copilot.git.branch`.

### How the CLI differs from the VS Code extension

- **Service name** is `github-copilot` (the extension is `copilot-chat`), which
  becomes the Prometheus `job` label and the Tempo `service.name`.
- **Content capture** uses `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`,
  not `COPILOT_OTEL_CAPTURE_CONTENT`.
- **Vendor metrics** use the `github.copilot.*` namespace (for example
  `github.copilot.tool.call.count`) instead of `copilot_chat.*`. Shared
  `gen_ai.*` metrics (token usage, operation duration) are emitted by both.
- **Prompts and responses** land as span attributes (`gen_ai.input.messages` /
  `gen_ai.output.messages`) on the `invoke_agent` and `chat` traces in Tempo,
  not in logs. Explore them in Grafana with the Tempo data source, filtering by
  `service.name = github-copilot`.

## Inspecting raw traces

Traces (the `invoke_agent` -> `chat` -> `execute_tool` span tree) are stored in
Tempo. Open **Explore** in Grafana, select the Tempo data source, and filter by
`service.name = copilot-chat` (VS Code) or `service.name = github-copilot` (CLI)
to inspect individual agent runs.
