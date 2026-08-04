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
mkdir -p "$HOME" "$RUNTIME/.venv/bin" "$FAKE_BIN"

cat >"$RUNTIME/.venv/bin/python" <<'PYTHON'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" -m clawchat_bridge.main enroll "* ]]; then
  IFS= read -r code
  [[ "$code" == "TEST-PAIRING-CODE" ]]
  [[ " $* " == *" --code-stdin "* ]]
  [[ " $* " != *" TEST-PAIRING-CODE "* ]]
  mkdir -p "$HOME/.hermes/clawchat_bridge"
  printf '{}\n' >"$HOME/.hermes/clawchat_bridge/config.json"
  printf '%s\n' "$*" >"$HOME/enroll-arguments"
  exit 0
fi
if [[ "${1:-}" == "-" ]]; then
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
    --label "Test Mac · Hermes bridge"

test -f "$RUNTIME/clawchat_bridge/main.py"
grep -q -- '--code-stdin' "$HOME/enroll-arguments"
grep -q 'bootstrap' "$HOME/launchctl-invocations"
printf 'Guided Hermes installer smoke test passed.\n'

OPENCLAW_HOME="$TEST_ROOT/openclaw home"
mkdir -p "$OPENCLAW_HOME"
cat >"$FAKE_BIN/node" <<'NODE'
#!/usr/bin/env bash
exit 0
NODE
cat >"$FAKE_BIN/npm" <<'NPM'
#!/usr/bin/env bash
exit 0
NPM
cat >"$FAKE_BIN/openclaw" <<'OPENCLAW'
#!/usr/bin/env bash
set -euo pipefail
if [[ " $* " == *" relay-console enroll "* ]]; then
  IFS= read -r code
  [[ "$code" == "OPENCLAW-PAIRING-CODE" ]]
  [[ " $* " != *" OPENCLAW-PAIRING-CODE "* ]]
fi
printf '%s\n' "$*" >>"$HOME/openclaw-invocations"
OPENCLAW
chmod +x "$FAKE_BIN/node" "$FAKE_BIN/npm" "$FAKE_BIN/openclaw"

printf 'OPENCLAW-PAIRING-CODE\n' | env HOME="$HOME" PATH="$FAKE_BIN:/usr/bin:/bin" \
  "$ROOT/install.sh" \
    --runtime openclaw \
    --api-url https://api.relayconsole.work \
    --runtime-path "$OPENCLAW_HOME" \
    --label "Test Mac · OpenClaw bridge"

test -f "$OPENCLAW_HOME/extensions/clawchat/openclaw.plugin.json"
grep -q 'relay-console enroll' "$HOME/openclaw-invocations"
grep -q 'gateway restart' "$HOME/openclaw-invocations"
printf 'Guided OpenClaw installer smoke test passed.\n'
