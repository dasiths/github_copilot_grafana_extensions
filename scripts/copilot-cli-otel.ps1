# Route GitHub Copilot CLI telemetry to the local LGTM stack (PowerShell).
#
# The Copilot CLI reads OpenTelemetry configuration from environment variables,
# so dot-source this file so the variables persist in your current session.
#
# Usage:
#   . .\scripts\copilot-cli-otel.ps1                 # enable telemetry
#   . .\scripts\copilot-cli-otel.ps1 -CaptureContent # also capture prompts/responses
#
# Then run `copilot` in the same session. Data appears in Grafana
# (http://localhost:3000) under service.name "github-copilot".

param(
    [string]$Endpoint = "http://localhost:4318",
    [switch]$CaptureContent
)

# OTLP endpoint of the LGTM container's OpenTelemetry Collector (HTTP).
$env:OTEL_EXPORTER_OTLP_ENDPOINT = $Endpoint

# The Copilot CLI only supports OTLP over HTTP (not gRPC).
$env:OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"

# Setting the endpoint already enables OTel; set this explicitly for clarity.
$env:COPILOT_OTEL_ENABLED = "true"

# service.name used to identify CLI telemetry in Grafana / Tempo / Prometheus.
$env:OTEL_SERVICE_NAME = "github-copilot"

# Capture full prompts and responses into trace span attributes. Off by default
# because it can include source code and sensitive data.
$env:OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = if ($CaptureContent) { "true" } else { "false" }

# --- Git repo / branch / commit enrichment (dev-env details that change) -----
# Tag CLI telemetry with the current git repository, branch and commit so you
# can group and filter by repo (primary) and branch (secondary). The Copilot CLI
# honors OTEL_RESOURCE_ATTRIBUTES, but the
# OTel resource is read ONCE at process start, so we refresh the variable before
# each prompt by wrapping the prompt function. You can still run `copilot`
# directly. Uses the OpenTelemetry VCS semantic conventions; service.name stays
# github-copilot because OTEL_SERVICE_NAME takes precedence.
$script:CopilotOtelBaseResource = $env:OTEL_RESOURCE_ATTRIBUTES

function global:Update-CopilotOtelResource {
    $br  = (git rev-parse --abbrev-ref HEAD 2>$null)
    $rev = (git rev-parse --short HEAD 2>$null)
    $url = (git config --get remote.origin.url 2>$null)
    $repo = if ($url) { ($url -split '/')[-1] -replace '\.git$', '' } else { $null }
    $parts = @()
    if ($repo) { $parts += "vcs.repository.name=$repo" }
    if ($br)   { $parts += "vcs.ref.head.name=$br" }
    if ($rev)  { $parts += "vcs.ref.head.revision=$rev" }
    if ($url)  { $parts += "vcs.repository.url.full=$url" }
    $extra = ($parts -join ",")
    $base = $script:CopilotOtelBaseResource
    if ($base -and $extra) { $env:OTEL_RESOURCE_ATTRIBUTES = "$base,$extra" }
    elseif ($extra)        { $env:OTEL_RESOURCE_ATTRIBUTES = $extra }
    elseif ($base)         { $env:OTEL_RESOURCE_ATTRIBUTES = $base }
}
Update-CopilotOtelResource

# Wrap the prompt function once so the branch refreshes before each prompt.
if (-not (Get-Variable -Name CopilotOtelPromptWrapped -Scope Global -ErrorAction SilentlyContinue)) {
    $global:CopilotOtelOrigPrompt = $function:prompt
    function global:prompt {
        Update-CopilotOtelResource
        if ($global:CopilotOtelOrigPrompt) { & $global:CopilotOtelOrigPrompt }
        else { "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
    }
    $global:CopilotOtelPromptWrapped = $true
}

Write-Host "GitHub Copilot CLI telemetry -> $($env:OTEL_EXPORTER_OTLP_ENDPOINT) (service.name=$($env:OTEL_SERVICE_NAME))"
Write-Host "Content capture: $($env:OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT)"
Write-Host "Git enrichment: OTEL_RESOURCE_ATTRIBUTES=$($env:OTEL_RESOURCE_ATTRIBUTES)"
