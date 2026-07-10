#!/usr/bin/env bash
# Bootstrap installer for the GitHub Copilot telemetry + Grafana LGTM stack.
#
# Designed to be run straight from the network:
#
#   curl -fsSL https://raw.githubusercontent.com/dasiths/github_copilot_grafana_extensions/main/scripts/install.sh | bash
#
# It is interactive: even when piped through `curl | bash` it reads answers from
# /dev/tty, so you get prompted at each step. In a non-interactive context (no
# tty, or with --yes) it falls back to sensible defaults and prints the manual
# steps instead of editing anything.
#
# What it does, step by step:
#   1. Checks prerequisites (curl, tar, and optionally Docker).
#   2. Downloads the repo assets (compose file, Grafana provisioning, otel
#      collector config, agent-insights sidecar, CLI scripts) into
#      ~/.agents/telemetry/copilot-extensions (override with INSTALL_DIR).
#   3. Optionally starts the LGTM stack with `docker compose up -d`.
#   4. Prints the VS Code User settings to paste in (it does NOT edit your
#      settings.json).
#   5. Optionally adds `source .../copilot-cli-otel.sh` to your shell profile so
#      Copilot CLI telemetry is enabled in new shells.
#
# Useful flags / env vars:
#   --uninstall        Remove the profile block, stop the stack, delete assets.
#   -y, --yes          Assume "yes"/defaults; no prompts.
#   -h, --help         Show this help.
#   INSTALL_DIR=...     Where to place the assets.
#   REPO=owner/name     Source repository (default dasiths/github_copilot_grafana_extensions).
#   REF=branch          Git ref / branch to download (default main).

set -euo pipefail

# --- Configuration (all overridable via environment) -------------------------
REPO="${REPO:-dasiths/github_copilot_grafana_extensions}"
REF="${REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.agents/telemetry/copilot-extensions}"
ASSUME_YES="${ASSUME_YES:-0}"

MARKER_BEGIN="# >>> copilot-grafana telemetry >>>"
MARKER_END="# <<< copilot-grafana telemetry <<<"

# --- Terminal styling (only when stdout is a real terminal) ------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'
  YLW=$'\033[33m'; BLU=$'\033[34m'; RST=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; BLU=""; RST=""
fi

info()  { printf '%s\n' "${BLU}==>${RST} $*"; }
step()  { printf '\n%s\n' "${BOLD}${BLU}==> $*${RST}"; }
ok()    { printf '%s\n' "${GRN}✓${RST} $*"; }
warn()  { printf '%s\n' "${YLW}!${RST} $*" >&2; }
err()   { printf '%s\n' "${RED}✗ $*${RST}" >&2; }
die()   { err "$*"; exit 1; }

# --- Interactivity: read from /dev/tty so `curl | bash` can still prompt ------
INTERACTIVE=0
if [ "$ASSUME_YES" != "1" ] && { : > /dev/tty; } 2>/dev/null; then
  INTERACTIVE=1
fi

# ask "prompt" "default" -> echoes the answer (or the default)
ask() {
  local prompt="$1" default="${2:-}" ans
  if [ "$INTERACTIVE" -ne 1 ]; then
    printf '%s' "$default"
    return
  fi
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" > /dev/tty
  else
    printf '%s: ' "$prompt" > /dev/tty
  fi
  IFS= read -r ans < /dev/tty || ans=""
  printf '%s' "${ans:-$default}"
}

# confirm "question" "y|n" -> returns 0 for yes, 1 for no
confirm() {
  local q="$1" def="${2:-y}" ans hint
  if [ "$def" = "y" ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  if [ "$ASSUME_YES" = "1" ]; then
    [ "$def" = "y" ]; return
  fi
  if [ "$INTERACTIVE" -ne 1 ]; then
    [ "$def" = "y" ]; return
  fi
  printf '%s %s ' "$q" "$hint" > /dev/tty
  IFS= read -r ans < /dev/tty || ans=""
  ans="${ans:-$def}"
  case "$ans" in
    [Yy] | [Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

have() { command -v "$1" > /dev/null 2>&1; }

has_docker() {
  have docker && docker compose version > /dev/null 2>&1
}

# --- Shell profile detection --------------------------------------------------
detect_profile() {
  local shell_name os
  shell_name="$(basename "${SHELL:-bash}")"
  os="$(uname -s 2>/dev/null || echo unknown)"
  case "$shell_name" in
    zsh) printf '%s' "${ZDOTDIR:-$HOME}/.zshrc" ;;
    bash)
      if [ "$os" = "Darwin" ] && [ ! -f "$HOME/.bashrc" ]; then
        printf '%s' "$HOME/.bash_profile"
      else
        printf '%s' "$HOME/.bashrc"
      fi
      ;;
    *) printf '%s' "$HOME/.profile" ;;
  esac
}

# Remove the managed marker block from a profile file (used by --uninstall).
remove_block() {
  local file="$1" tmp
  [ -f "$file" ] || return 0
  grep -qF "$MARKER_BEGIN" "$file" || return 0
  tmp="$(mktemp)"
  awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" '
    $0 == b { skip = 1 }
    skip == 0 { print }
    $0 == e { skip = 0 }
  ' "$file" > "$tmp"
  # Trim a trailing blank line left behind, then replace the file.
  cat "$tmp" > "$file"
  rm -f "$tmp"
  ok "Removed telemetry block from $file"
}

# --- Steps -------------------------------------------------------------------
check_prereqs() {
  step "Checking prerequisites"
  have curl || die "curl is required."
  have tar  || die "tar is required."
  ok "curl and tar found."
  if has_docker; then
    ok "Docker with Compose v2 found."
  else
    warn "Docker (with 'docker compose') not found — you can still download the"
    warn "assets now and start the stack later with 'docker compose up -d'."
  fi
}

download_assets() {
  step "Downloading assets"
  local target
  target="$(ask "Install directory" "$INSTALL_DIR")"
  INSTALL_DIR="${target/#\~/$HOME}"

  if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
    if ! confirm "$INSTALL_DIR already exists. Update it in place?" y; then
      die "Aborted."
    fi
  fi

  local tmp src tar_url
  tmp="$(mktemp -d)"
  tar_url="https://codeload.github.com/${REPO}/tar.gz/refs/heads/${REF}"
  info "Fetching ${REPO}@${REF} ..."
  if ! curl -fsSL "$tar_url" | tar -xzf - -C "$tmp"; then
    rm -rf "$tmp"
    die "Download or extraction failed from $tar_url"
  fi
  src="$(find "$tmp" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n1)"
  [ -n "$src" ] || { rm -rf "$tmp"; die "Unexpected archive layout."; }

  mkdir -p "$INSTALL_DIR"
  local item
  for item in docker-compose.yml grafana otelcol agent-insights scripts; do
    if [ -e "$src/$item" ]; then
      cp -R "$src/$item" "$INSTALL_DIR/"
    else
      warn "Asset '$item' missing from download; skipping."
    fi
  done
  rm -rf "$tmp"
  chmod +x "$INSTALL_DIR/scripts/copilot-cli-otel.sh" 2>/dev/null || true
  ok "Assets installed to $INSTALL_DIR"
}

start_stack() {
  step "Docker stack"
  if ! has_docker; then
    warn "Docker not available; skipping. Start later with:"
    printf '    cd %q && docker compose up -d\n' "$INSTALL_DIR"
    return
  fi
  if confirm "Start the LGTM stack now with 'docker compose up -d'?" y; then
    if (cd "$INSTALL_DIR" && docker compose up -d); then
      ok "Stack started. Grafana: http://localhost:3000 (admin / admin)"
    else
      warn "Failed to start the stack. You can retry with:"
      printf '    cd %q && docker compose up -d\n' "$INSTALL_DIR"
    fi
  else
    info "Skipped. Start it later with:"
    printf '    cd %q && docker compose up -d\n' "$INSTALL_DIR"
  fi
}

print_vscode_settings() {
  step "VS Code Copilot Chat settings"
  cat <<'EOF'
These settings are APPLICATION-SCOPED — add them to your USER settings.json
(Command Palette -> "Preferences: Open User Settings (JSON)"), not a workspace
file. Merge in the following keys:

  {
    "github.copilot.chat.otel.enabled": true,
    "github.copilot.chat.otel.exporterType": "otlp-http",
    "github.copilot.chat.otel.otlpEndpoint": "http://localhost:4318",

    // Optional: capture full prompt/response/tool content into spans.
    // WARNING: may include source code / sensitive data. Local, trusted use only.
    "github.copilot.chat.otel.captureContent": false
  }
EOF
}

setup_profile() {
  step "Copilot CLI telemetry (shell profile)"
  local script_path="$INSTALL_DIR/scripts/copilot-cli-otel.sh"
  if [ ! -f "$script_path" ]; then
    warn "CLI script not found at $script_path; skipping profile setup."
    return
  fi

  info "To send Copilot CLI telemetry to the stack, this line must run in your shell:"
  printf '    source %q\n' "$script_path"
  echo
  if ! confirm "Add that line to your shell profile so it loads in new shells?" y; then
    info "Skipped. Run the 'source' line above manually when you want CLI telemetry,"
    info "or add it to your profile yourself later."
    return
  fi

  local profile
  profile="$(ask "Profile file to edit" "$(detect_profile)")"
  profile="${profile/#\~/$HOME}"

  if [ -f "$profile" ] && grep -qF "$MARKER_BEGIN" "$profile"; then
    ok "Profile $profile already has the telemetry block; leaving it unchanged."
    return
  fi

  mkdir -p "$(dirname "$profile")"
  {
    printf '\n%s\n' "$MARKER_BEGIN"
    printf '# Added by copilot-grafana telemetry install.sh — remove with install.sh --uninstall\n'
    printf 'source %q\n' "$script_path"
    printf '%s\n' "$MARKER_END"
  } >> "$profile"
  ok "Added telemetry block to $profile"
  info "Open a new shell (or run the 'source' line above) to enable it now."
}

summary() {
  step "Done"
  cat <<EOF
Assets:     $INSTALL_DIR
Grafana:    http://localhost:3000  (admin / admin)
Stack:      cd "$INSTALL_DIR" && docker compose up -d   |   docker compose down
Uninstall:  bash "$INSTALL_DIR/scripts/install.sh" --uninstall

Next: generate some Copilot activity (VS Code chat/agent, or 'copilot' in a
shell where the telemetry script is sourced), then open Grafana.
EOF
}

uninstall() {
  step "Uninstall"
  info "This removes the shell-profile block, optionally stops the stack, and"
  info "optionally deletes the assets at $INSTALL_DIR."

  if [ -f "$INSTALL_DIR/docker-compose.yml" ] && has_docker; then
    if confirm "Stop and remove the Docker stack (docker compose down)?" y; then
      (cd "$INSTALL_DIR" && docker compose down) || warn "docker compose down failed."
    fi
  fi

  local f
  for f in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile" "${ZDOTDIR:-$HOME}/.zshrc"; do
    remove_block "$f"
  done

  if [ -d "$INSTALL_DIR" ] && confirm "Delete assets directory $INSTALL_DIR?" n; then
    rm -rf "$INSTALL_DIR"
    ok "Deleted $INSTALL_DIR"
  fi

  ok "Uninstall complete. Restart your shell to clear the exported OTEL_* vars."
}

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d;s/^# \{0,1\}//'
}

main() {
  case "${1:-}" in
    -h | --help) usage; exit 0 ;;
    --uninstall) uninstall; exit 0 ;;
    -y | --yes) ASSUME_YES=1 ;;
    "" ) ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac

  printf '%s\n' "${BOLD}GitHub Copilot telemetry → Grafana LGTM — installer${RST}"
  printf '%s\n' "${DIM}Repo ${REPO}@${REF}${RST}"
  [ "$INTERACTIVE" -eq 1 ] || info "Non-interactive mode: using defaults."

  check_prereqs
  download_assets
  start_stack
  print_vscode_settings
  setup_profile
  summary
}

main "$@"
