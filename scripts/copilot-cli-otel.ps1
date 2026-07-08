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

Write-Host "GitHub Copilot CLI telemetry -> $($env:OTEL_EXPORTER_OTLP_ENDPOINT) (service.name=$($env:OTEL_SERVICE_NAME))"
Write-Host "Content capture: $($env:OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT)"
