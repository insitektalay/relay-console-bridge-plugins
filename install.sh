#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME=""
API_URL=""
RUNTIME_PATH=""
DEVICE_LABEL="Relay Console bridge"
MAX_ENROLLMENT_CODE_BYTES=4096

usage() {
  cat <<'USAGE'
Install, enroll, start, and check a pinned Relay Console bridge.

Usage:
  ./install.sh --runtime hermes --api-url https://api.example.com --runtime-path /path/to/hermes --label "Office Mac · Hermes bridge"
  ./install.sh --runtime openclaw --api-url https://api.example.com [--runtime-path ~/.openclaw] --label "Office Mac · OpenClaw bridge"

The one-time enrollment code is read from standard input. When run in an
interactive terminal, the installer prompts without echoing the code.
USAGE
}

fail() {
  printf 'Relay bridge installation failed: %s\n' "$*" >&2
  exit 1
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --runtime)
      [[ "$#" -ge 2 ]] || fail "--runtime requires a value"
      RUNTIME="$2"
      shift 2
      ;;
    --api-url)
      [[ "$#" -ge 2 ]] || fail "--api-url requires a value"
      API_URL="$2"
      shift 2
      ;;
    --runtime-path)
      [[ "$#" -ge 2 ]] || fail "--runtime-path requires a value"
      RUNTIME_PATH="$2"
      shift 2
      ;;
    --label)
      [[ "$#" -ge 2 ]] || fail "--label requires a value"
      DEVICE_LABEL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

case "$RUNTIME" in
  hermes|openclaw) ;;
  "") fail "--runtime is required" ;;
  *) fail "unsupported runtime: $RUNTIME" ;;
esac

case "$API_URL" in
  https://*) ;;
  "") fail "--api-url is required" ;;
  *) fail "--api-url must use https://" ;;
esac

case "$API_URL" in
  https://localhost|https://localhost:*|https://localhost/*|https://127.*|https://0.0.0.0*|https://\[::1\]*)
    fail "--api-url must use the Relay Railway backend, not a loopback address"
    ;;
esac

[[ -n "${DEVICE_LABEL//[[:space:]]/}" ]] || fail "--label must not be empty"

if [[ -t 0 ]]; then
  printf 'One-time Relay bridge pairing code: ' >&2
  IFS= read -r -s ENROLLMENT_CODE
  printf '\n' >&2
else
  IFS= read -r ENROLLMENT_CODE || [[ -n "${ENROLLMENT_CODE:-}" ]]
fi

[[ -n "${ENROLLMENT_CODE:-}" ]] || fail "a one-time pairing code is required on standard input"
[[ "$(printf '%s' "$ENROLLMENT_CODE" | wc -c | tr -d ' ')" -le "$MAX_ENROLLMENT_CODE_BYTES" ]] \
  || fail "the one-time pairing code is too large"

install_hermes() {
  [[ -n "$RUNTIME_PATH" ]] || fail "--runtime-path is required for Hermes Agent"
  [[ -d "$RUNTIME_PATH" ]] || fail "Hermes Agent checkout does not exist: $RUNTIME_PATH"

  local python=""
  local candidate
  for candidate in "$RUNTIME_PATH/.venv/bin/python" "$RUNTIME_PATH/venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      python="$candidate"
      break
    fi
  done
  [[ -n "$python" ]] || fail "Hermes Agent needs .venv/bin/python or venv/bin/python"

  "$ROOT/scripts/install-hermes-agent-bridge.sh" "$RUNTIME_PATH"
  printf '%s\n' "$ENROLLMENT_CODE" | (
    cd "$RUNTIME_PATH"
    "$python" -m clawchat_bridge.main enroll \
      --api-url "$API_URL" \
      --code-stdin \
      --device-label "$DEVICE_LABEL"
  )
  unset ENROLLMENT_CODE
  HERMES_HOME="$RUNTIME_PATH" HERMES_PYTHON="$python" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" install
  HERMES_HOME="$RUNTIME_PATH" HERMES_PYTHON="$python" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" health
}

install_openclaw() {
  local openclaw_home="${RUNTIME_PATH:-${OPENCLAW_HOME:-$HOME/.openclaw}}"
  [[ -d "$openclaw_home" ]] || fail "OpenClaw home does not exist: $openclaw_home"
  command -v openclaw >/dev/null || fail "OpenClaw is not available on PATH"

  OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" install
  printf '%s\n' "$ENROLLMENT_CODE" | OPENCLAW_HOME="$openclaw_home" \
    openclaw relay-console enroll --api-url "$API_URL" --label "$DEVICE_LABEL"
  unset ENROLLMENT_CODE
  OPENCLAW_HOME="$openclaw_home" openclaw gateway restart
  OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" health
}

case "$RUNTIME" in
  hermes) install_hermes ;;
  openclaw) install_openclaw ;;
esac
