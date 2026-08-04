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

read_enrollment_code() {
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
}

host_type() {
  case "$(uname -s)" in
    Darwin) printf 'macos-launchd\n' ;;
    Linux) printf 'linux-systemd\n' ;;
    *) fail "automatic bridge installation requires macOS or Linux" ;;
  esac
}

preflight_hermes() {
  local python="$1"
  local runtime_version="$2"
  "$python" - "$API_URL" "$runtime_version" "$(host_type)" <<'PY'
import json
import sys
import urllib.error
import urllib.request

api_url, runtime_version, host_type = sys.argv[1:]
payload = {
    "pluginVersion": "0.3.0-rc.4",
    "openCoreVersion": runtime_version or None,
    "runtimeType": "hermes",
    "hostType": host_type,
    "apiContractVersion": "v2",
    "websocketContractVersion": "bridge.v1",
    "capabilities": [
        "clawchat.runtime.hermes",
        "clawchat.bridge.rotating_credentials.v1",
        "clawchat.agent_replica_sync",
        "clawchat.runtime.structured_jobs",
        "clawchat.runtime.structured_output",
        "clawchat.marketplace.tools",
    ],
}
request = urllib.request.Request(
    api_url.rstrip("/") + "/api/v1/bridge/compatibility/check",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
except urllib.error.HTTPError as error:
    if error.code == 404:
        print("Compatibility preflight is not deployed yet; enrollment will enforce the current policy.")
        raise SystemExit(0)
    raise
level = result.get("level", "unsupported")
mode = result.get("operatingMode", "blocked")
print(f"Compatibility: {level} ({mode} mode) for Hermes {runtime_version or 'unknown'}")
disabled = result.get("disabledCapabilities") or []
if disabled:
    print("Safe mode disables: " + ", ".join(disabled))
if not result.get("compatible"):
    raise SystemExit("This Hermes version is not compatible with the current Relay bridge policy.")
PY
}

preflight_openclaw() {
  local runtime_version="$1"
  node --input-type=module - "$API_URL" "$runtime_version" "$(host_type)" <<'JS'
const [apiUrl, runtimeVersion, hostType] = process.argv.slice(2);
const response = await fetch(`${apiUrl.replace(/\/$/, "")}/api/v1/bridge/compatibility/check`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    pluginVersion: "2026.7.31-rc.1",
    openCoreVersion: runtimeVersion || undefined,
    runtimeType: "openclaw",
    hostType,
    apiContractVersion: "v2",
    websocketContractVersion: "bridge.v1",
    capabilities: [
      "clawchat.runtime.openclaw",
      "clawchat.bridge.rotating_credentials.v1",
      "clawchat.agent_replica_sync",
      "clawchat.runtime.structured_jobs",
      "clawchat.runtime.structured_output",
      "clawchat.attachments.local_media",
    ],
  }),
});
if (response.status === 404) {
  console.log("Compatibility preflight is not deployed yet; enrollment will enforce the current policy.");
  process.exit(0);
}
if (!response.ok) throw new Error(`Compatibility preflight failed: HTTP ${response.status}`);
const result = await response.json();
console.log(`Compatibility: ${result.level} (${result.operatingMode} mode) for OpenClaw ${runtimeVersion || "unknown"}`);
if (result.disabledCapabilities?.length) console.log(`Safe mode disables: ${result.disabledCapabilities.join(", ")}`);
if (!result.compatible) throw new Error("This OpenClaw version is not compatible with the current Relay bridge policy.");
JS
}

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

  local runtime_version
  runtime_version="$(git -C "$RUNTIME_PATH" describe --tags --exact-match 2>/dev/null || "$python" -c 'import importlib.metadata; print(importlib.metadata.version("hermes-agent"))' 2>/dev/null || true)"
  preflight_hermes "$python" "$runtime_version"
  read_enrollment_code

  "$ROOT/scripts/install-hermes-agent-bridge.sh" "$RUNTIME_PATH"
  HERMES_HOME="$RUNTIME_PATH" HERMES_PYTHON="$python" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" prepare-runtime
  local bridge_python="${HERMES_BRIDGE_RUNTIME_DIR:-$HOME/.hermes/clawchat_bridge/runtime}/bin/python"
  printf '%s\n' "$ENROLLMENT_CODE" | (
    cd "$RUNTIME_PATH"
    "$bridge_python" -m clawchat_bridge.main enroll \
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

  local runtime_version
  runtime_version="$(openclaw --version 2>/dev/null | grep -Eo 'v?[0-9]{4}\.[0-9]+\.[0-9]+([-.+][A-Za-z0-9.-]+)?' | head -n 1 || true)"
  preflight_openclaw "$runtime_version"
  read_enrollment_code

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
