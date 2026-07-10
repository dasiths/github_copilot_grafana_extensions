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

# --- Git repo / branch / commit enrichment (dev-env details that change) -----
# Tag CLI telemetry with the current git repository, branch and commit so you
# can group and filter by repo (primary) and branch (secondary) — see the Cost
# by Branch dashboard. The Copilot CLI honors
# OTEL_RESOURCE_ATTRIBUTES, but the OTel resource is read ONCE at process start,
# so we refresh the variable before each shell prompt. You can still run
# `copilot` directly (no wrapper) — it inherits the freshly-set value.
#
# Uses the vendor-neutral OpenTelemetry VCS semantic conventions
# (vcs.ref.head.name / .revision / vcs.repository.url.full). service.name stays
# github-copilot because OTEL_SERVICE_NAME takes precedence.

# Preserve any resource attributes the user set before sourcing this script.
_COPILOT_OTEL_BASE_RESOURCE="${OTEL_RESOURCE_ATTRIBUTES:-}"

_copilot_otel_refresh() {
  local br rev url repo extra
  br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  rev="$(git rev-parse --short HEAD 2>/dev/null)"
  url="$(git config --get remote.origin.url 2>/dev/null)"
  repo="${url##*/}"      # basename of remote URL (handles https + git@host:owner/repo)
  repo="${repo%.git}"    # strip trailing .git
  extra=""
  [ -n "$repo" ] && extra="${extra}${extra:+,}vcs.repository.name=${repo}"
  [ -n "$br" ]   && extra="${extra}${extra:+,}vcs.ref.head.name=${br}"
  [ -n "$rev" ]  && extra="${extra}${extra:+,}vcs.ref.head.revision=${rev}"
  [ -n "$url" ]  && extra="${extra}${extra:+,}vcs.repository.url.full=${url}"
  if [ -n "$_COPILOT_OTEL_BASE_RESOURCE" ] && [ -n "$extra" ]; then
    export OTEL_RESOURCE_ATTRIBUTES="${_COPILOT_OTEL_BASE_RESOURCE},${extra}"
  elif [ -n "$extra" ]; then
    export OTEL_RESOURCE_ATTRIBUTES="${extra}"
  elif [ -n "$_COPILOT_OTEL_BASE_RESOURCE" ]; then
    export OTEL_RESOURCE_ATTRIBUTES="${_COPILOT_OTEL_BASE_RESOURCE}"
  fi
}
_copilot_otel_refresh

# Refresh before every prompt so a `git checkout` is reflected in the next
# `copilot` launch. zsh uses precmd_functions; bash uses PROMPT_COMMAND. Both
# registrations are idempotent so re-sourcing is safe.
if [ -n "${ZSH_VERSION:-}" ]; then
  typeset -ga precmd_functions
  case " ${precmd_functions[*]} " in
    *" _copilot_otel_refresh "*) ;;
    *) precmd_functions+=(_copilot_otel_refresh) ;;
  esac
elif [ -n "${BASH_VERSION:-}" ]; then
  case ";${PROMPT_COMMAND:-};" in
    *";_copilot_otel_refresh;"*) ;;
    *) PROMPT_COMMAND="_copilot_otel_refresh;${PROMPT_COMMAND:-}" ;;
  esac
fi

echo "GitHub Copilot CLI telemetry -> ${OTEL_EXPORTER_OTLP_ENDPOINT} (service.name=${OTEL_SERVICE_NAME})"
echo "Content capture: ${OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT}"
echo "Git enrichment: OTEL_RESOURCE_ATTRIBUTES=${OTEL_RESOURCE_ATTRIBUTES:-<none>}"
