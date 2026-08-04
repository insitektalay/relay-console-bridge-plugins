#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/relay-guided-installer.XXXXXX")"
cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

HOME="$TEST_ROOT/home"
RUNTIME="$TEST_ROOT/hermes agent"
FAKE_BIN="$TEST_ROOT/bin"
export HOME RUNTIME
mkdir -p "$HOME" "$RUNTIME/.venv/bin" "$FAKE_BIN"

cat >"$RUNTIME/.venv/bin/python" <<'PYTHON'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  runtime="${3:?missing bridge runtime path}"
  mkdir -p "$runtime/bin" "$runtime/lib/python-test/site-packages"
  cp "$0" "$runtime/bin/python"
  chmod +x "$runtime/bin/python"
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  runtime="$(cd "$(dirname "$0")/.." && pwd)"
  [[ ! -f "$runtime/lib/python-test/site-packages/relay-console-hermes.pth" ]]
  printf '%s\n' "$*" >>"$HOME/bridge-pip-invocations"
  exit 0
fi
if [[ "${1:-}" == "-c" && "${2:-}" == *"sysconfig.get_paths"* ]]; then
  printf '%s\n' "$RUNTIME/.venv/lib/python-test/site-packages"
  exit 0
fi
if [[ "${1:-}" == "-c" && "${2:-}" == *"site.getsitepackages"* ]]; then
  runtime="$(cd "$(dirname "$0")/.." && pwd)"
  printf '%s\n' "$runtime/lib/python-test/site-packages"
  exit 0
fi
if [[ "${1:-}" == "-c" ]]; then
  printf '0.15.1\n'
  exit 0
fi
if [[ " $* " == *" -m clawchat_bridge.main enroll "* ]]; then
  IFS= read -r code
  [[ "$code" == "TEST-PAIRING-CODE" ]]
  [[ " $* " == *" --code-stdin "* ]]
  [[ " $* " != *" TEST-PAIRING-CODE "* ]]
  mkdir -p "$HOME/.hermes/clawchat_bridge"
  printf '{}\n' >"$HOME/.hermes/clawchat_bridge/config.json"
  printf '%s\n' "$*" >"$HOME/enroll-arguments"
  printf 'enroll\n' >>"$HOME/hermes-order"
  exit 0
fi
if [[ "${1:-}" == "-" ]]; then
  if [[ "${2:-}" == https://* ]]; then
    cat >/dev/null
    printf 'Compatibility: compatible (safe mode) for Hermes 0.15.1\n'
    printf 'preflight\n' >>"$HOME/hermes-order"
    exit 0
  fi
  plist_path="$2"
  cat >/dev/null
  : >"$plist_path"
  exit 0
fi
exit 0
PYTHON
chmod +x "$RUNTIME/.venv/bin/python"

cat >"$FAKE_BIN/uname" <<'UNAME'
#!/usr/bin/env bash
printf 'Darwin\n'
UNAME
cat >"$FAKE_BIN/launchctl" <<'LAUNCHCTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$HOME/launchctl-invocations"
LAUNCHCTL
chmod +x "$FAKE_BIN/uname" "$FAKE_BIN/launchctl"

bash -n "$ROOT/install.sh"
"$ROOT/install.sh" --help >/dev/null
if printf 'code\n' | "$ROOT/install.sh" --runtime hermes --api-url http://relay.example.com --runtime-path "$RUNTIME" >/dev/null 2>&1; then
  echo "installer accepted an insecure backend URL" >&2
  exit 1
fi

printf 'TEST-PAIRING-CODE\n' | env HOME="$HOME" PATH="$FAKE_BIN:/usr/bin:/bin" \
  "$ROOT/install.sh" \
    --runtime hermes \
    --api-url https://api.relayconsole.work \
    --runtime-path "$RUNTIME" \
    --label "Test Mac · Hermes bridge" \
    --agent "hugo-prototype" \
    --agent "leo-metrics"

test -f "$RUNTIME/clawchat_bridge/main.py"
test -x "$HOME/.hermes/clawchat_bridge/runtime/bin/python"
grep -F 'aiohttp>=3.10,<4' "$HOME/bridge-pip-invocations" >/dev/null
grep -q -- '--code-stdin' "$HOME/enroll-arguments"
grep -q -- '--agent hugo-prototype' "$HOME/enroll-arguments"
grep -q -- '--agent leo-metrics' "$HOME/enroll-arguments"
grep -q 'bootstrap' "$HOME/launchctl-invocations"
test "$(paste -sd, "$HOME/hermes-order")" = "preflight,enroll"
printf 'Guided Hermes installer smoke test passed.\n'

OPENCLAW_STATE_DIR="$TEST_ROOT/openclaw home"
mkdir -p "$OPENCLAW_STATE_DIR"
cat >"$FAKE_BIN/node" <<'NODE'
#!/usr/bin/env bash
cat >/dev/null
printf 'preflight\n' >>"$HOME/openclaw-order"
exit 0
NODE
cat >"$FAKE_BIN/npm" <<'NPM'
#!/usr/bin/env bash
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
  mkdir -p dist
  touch dist/index.js
fi
exit 0
NPM
cat >"$FAKE_BIN/openclaw" <<'OPENCLAW'
#!/usr/bin/env bash
set -euo pipefail
[[ -n "${OPENCLAW_STATE_DIR:-}" ]]
[[ -z "${OPENCLAW_HOME:-}" ]]
if [[ " $* " == *" relay-console enroll "* ]]; then
  IFS= read -r code
  [[ "$code" == "OPENCLAW-PAIRING-CODE" ]]
  [[ " $* " != *" OPENCLAW-PAIRING-CODE "* ]]
  printf 'enroll\n' >>"$HOME/openclaw-order"
fi
printf '%s\n' "$*" >>"$HOME/openclaw-invocations"
OPENCLAW
chmod +x "$FAKE_BIN/node" "$FAKE_BIN/npm" "$FAKE_BIN/openclaw"

printf 'OPENCLAW-PAIRING-CODE\n' | env HOME="$HOME" PATH="$FAKE_BIN:/usr/bin:/bin" \
  "$ROOT/install.sh" \
    --runtime openclaw \
    --api-url https://api.relayconsole.work \
    --runtime-path "$OPENCLAW_STATE_DIR" \
    --label "Test Mac · OpenClaw bridge"

test -f "$OPENCLAW_STATE_DIR/extensions/clawchat/openclaw.plugin.json"
test -f "$OPENCLAW_STATE_DIR/extensions/clawchat/dist/index.js"
grep -q 'relay-console enroll' "$HOME/openclaw-invocations"
grep -q 'gateway restart' "$HOME/openclaw-invocations"
test "$(head -n 1 "$HOME/openclaw-order")" = "preflight"
test "$(tail -n 1 "$HOME/openclaw-order")" = "enroll"
printf 'Guided OpenClaw installer smoke test passed.\n'
