#!/usr/bin/env bash
# Route GitHub Copilot CLI telemetry to the local LGTM stack.
#
# The Copilot CLI reads OpenTelemetry configuration from environment variables
# (there is no settings.json equivalent), so this file must be *sourced* so the
# variables persist in your current shell.
#
# Usage:
#   source scripts/copilot-cli-otel.sh                     # enable telemetry
#   COPILOT_CAPTURE_CONTENT=1 source scripts/copilot-cli-otel.sh   # also capture prompts/responses
#
# Then run `copilot` in the same shell. Data appears in Grafana
# (http://localhost:3000) under service.name "github-copilot".

# Guard: warn if executed instead of sourced (exports would not persist).
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  echo "This script must be sourced so the variables persist in your shell:" >&2
  echo "    source ${BASH_SOURCE[0]:-scripts/copilot-cli-otel.sh}" >&2
  exit 1
fi

# OTLP endpoint of the LGTM container's OpenTelemetry Collector (HTTP).
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://localhost:4318}"

# The Copilot CLI only supports OTLP over HTTP (not gRPC).
export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-http/protobuf}"

# Setting the endpoint already enables OTel; set this explicitly for clarity.
export COPILOT_OTEL_ENABLED="${COPILOT_OTEL_ENABLED:-true}"

# service.name used to identify CLI telemetry in Grafana / Tempo / Prometheus.
export OTEL_SERVICE_NAME="${OTEL_SERVICE_NAME:-github-copilot}"

# Capture full prompts and responses into trace span attributes
# (gen_ai.input.messages / gen_ai.output.messages). Off by default because it
# can include source code and sensitive data. Enable with COPILOT_CAPTURE_CONTENT=1.
if [[ "${COPILOT_CAPTURE_CONTENT:-0}" == "1" || "${COPILOT_CAPTURE_CONTENT:-}" == "true" ]]; then
  export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
else
  export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT="${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT:-false}"
fi

# Repo and branch are emitted in-band by the Copilot CLI on the invoke_agent
# span (github.copilot.git.repository / github.copilot.git.branch), so the Cost
# by Repo & Branch dashboard works with no OTEL_RESOURCE_ATTRIBUTES enrichment
# here. (The sidecar keeps a vcs.* resource fallback for pre-1.0.71 CLIs.)

echo "GitHub Copilot CLI telemetry -> ${OTEL_EXPORTER_OTLP_ENDPOINT} (service.name=${OTEL_SERVICE_NAME})"
echo "Content capture: ${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT}"
