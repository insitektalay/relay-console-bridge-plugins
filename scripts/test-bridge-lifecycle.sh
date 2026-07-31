#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/relay-bridge-lifecycle.XXXXXX")"
FAKE_BIN="$TEST_ROOT/fake-bin"
STATE="$TEST_ROOT/state"
REAL_PYTHON="$(command -v python3)"
export TEST_STATE="$STATE" TEST_PLATFORM=Darwin REAL_PYTHON

cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
  echo "lifecycle contract failed: $*" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_dir() {
  [[ -d "$1" ]] || fail "expected directory: $1"
}

assert_absent() {
  [[ ! -e "$1" ]] || fail "expected absence: $1"
}

mkdir -p "$FAKE_BIN" "$STATE"

cat > "$FAKE_BIN/uname" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$TEST_PLATFORM"
SH

cat > "$FAKE_BIN/launchctl" <<'SH'
#!/usr/bin/env bash
printf 'launchctl %s\n' "$*" >> "$TEST_STATE/service.log"
if [[ "${1:-}" == "bootstrap" && -f "$TEST_STATE/fail-service" ]]; then
  exit 41
fi
SH

cat > "$FAKE_BIN/systemctl" <<'SH'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$TEST_STATE/service.log"
if [[ "$*" == *"enable --now"* && -f "$TEST_STATE/fail-service" ]]; then
  exit 42
fi
SH

cat > "$FAKE_BIN/systemd-analyze" <<'SH'
#!/usr/bin/env bash
unit="${@: -1}"
if grep -F 'WorkingDirectory="' "$unit" >/dev/null; then
  echo "quoted WorkingDirectory path is invalid" >&2
  exit 44
fi
printf 'systemd-analyze %s\n' "$*" >> "$TEST_STATE/service.log"
SH

cat > "$FAKE_BIN/journalctl" <<'SH'
#!/usr/bin/env bash
exit 0
SH

cat > "$FAKE_BIN/python" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" && "${2:-}" == *"importlib.metadata"* ]]; then
  [[ ! -f "$TEST_STATE/fail-python-dependency" ]]
  exit
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "clawchat_bridge.main" ]]; then
  [[ ! -f "$TEST_STATE/fail-runtime" ]]
  exit
fi
exec "$REAL_PYTHON" "$@"
SH

cat > "$FAKE_BIN/npm" <<'SH'
#!/usr/bin/env bash
if [[ -f "$TEST_STATE/fail-npm" ]]; then
  exit 43
fi
mkdir -p node_modules
touch node_modules/installed-by-lifecycle-contract
SH

cat > "$FAKE_BIN/node" <<'SH'
#!/usr/bin/env bash
exit 0
SH

cat > "$FAKE_BIN/openclaw" <<'SH'
#!/usr/bin/env bash
printf 'openclaw %s\n' "$*" >> "$TEST_STATE/openclaw.log"
exit 0
SH

chmod +x "$FAKE_BIN"/*
export PATH="$FAKE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"

make_hermes_fixture() {
  local hermes="$1"
  mkdir -p "$hermes/.venv/bin"
  ln -s "$FAKE_BIN/python" "$hermes/.venv/bin/python"
  touch "$hermes/existing-hermes-installation"
}

run_macos_hermes_contract() {
  local home="$TEST_ROOT/Mac Home & Credentials"
  local hermes="$TEST_ROOT/Hermes & 100% Runtime"
  local config="$home/.hermes/clawchat_bridge/config.json"
  local explicit_python="$hermes/explicit-python/bin/python"
  mkdir -p "$(dirname "$config")"
  printf '{}\n' > "$config"
  chmod 600 "$config"
  make_hermes_fixture "$hermes"
  mkdir -p "$(dirname "$explicit_python")"
  ln -s "$FAKE_BIN/python" "$explicit_python"

  HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" install
  assert_file "$hermes/clawchat_bridge/main.py"
  assert_file "$home/Library/LaunchAgents/work.relayconsole.hermes-bridge.plist"

  "$REAL_PYTHON" - "$home/Library/LaunchAgents/work.relayconsole.hermes-bridge.plist" "$hermes" "$config" "$explicit_python" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    value = plistlib.load(handle)
assert value["WorkingDirectory"] == sys.argv[2]
assert value["ProgramArguments"][4] == sys.argv[3]
assert value["ProgramArguments"][0] == sys.argv[4]
PY

  touch "$hermes/clawchat_bridge/previous-version-marker"
  HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" update
  assert_absent "$hermes/clawchat_bridge/previous-version-marker"
  assert_file "$hermes/clawchat_bridge.rollback/previous-version-marker"

  touch "$hermes/clawchat_bridge/dependency-failure-preserved-marker"
  touch "$STATE/fail-python-dependency"
  if HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" update; then
    fail "Hermes update unexpectedly succeeded without its pinned bridge dependency"
  fi
  assert_file "$hermes/clawchat_bridge/dependency-failure-preserved-marker"
  rm -f "$STATE/fail-python-dependency"

  HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" rollback
  assert_file "$hermes/clawchat_bridge/previous-version-marker"

  touch "$STATE/fail-service"
  if HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" update; then
    fail "Hermes update unexpectedly succeeded when service activation failed"
  fi
  assert_file "$hermes/clawchat_bridge/previous-version-marker"
  rm -f "$STATE/fail-service"

  HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" health
  HOME="$home" HERMES_HOME="$hermes" HERMES_PYTHON="$explicit_python" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" uninstall
  assert_absent "$hermes/clawchat_bridge"
  assert_absent "$hermes/clawchat_bridge.rollback"
  assert_file "$hermes/existing-hermes-installation"
  assert_file "$config"
}

run_linux_hermes_contract() {
  local home="$TEST_ROOT/Linux Home"
  local hermes="$TEST_ROOT/Linux Hermes 50% Runtime"
  local config="$home/.hermes/clawchat_bridge/config.json"
  local unit="$home/.config/systemd/user/relay-hermes-bridge.service"
  mkdir -p "$(dirname "$config")"
  printf '{}\n' > "$config"
  make_hermes_fixture "$hermes"

  TEST_PLATFORM=Linux HOME="$home" HERMES_HOME="$hermes" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" install
  assert_file "$unit"
  local systemd_hermes="${hermes//%/%%}"
  grep -F "WorkingDirectory=$systemd_hermes" "$unit" >/dev/null || fail "systemd working directory path is not serialized literally"
  grep -F '50%% Runtime' "$unit" >/dev/null || fail "systemd percent specifier was not escaped"
  grep -F "systemd-analyze --user verify $unit" "$STATE/service.log" >/dev/null || fail "systemd unit was not verified before activation"
  TEST_PLATFORM=Linux HOME="$home" HERMES_HOME="$hermes" HERMES_BRIDGE_CONFIG="$config" \
    "$ROOT/scripts/manage-hermes-agent-bridge.sh" uninstall
  assert_file "$hermes/existing-hermes-installation"
  assert_file "$config"
}

run_openclaw_contract() {
  local home="$TEST_ROOT/OpenClaw Home"
  local openclaw_home="$home/.openclaw"
  local target="$openclaw_home/extensions/clawchat"
  local rollback="$openclaw_home/.relay-console-bridge/clawchat/rollback"
  mkdir -p "$openclaw_home"
  printf '{}\n' > "$openclaw_home/openclaw.json"

  HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" install
  assert_file "$target/openclaw.plugin.json"
  assert_file "$target/node_modules/installed-by-lifecycle-contract"
  assert_absent "$target/node_modules/.package-lock.json"
  if find "$target/src" -name '*.test.ts' -print -quit | grep -q .; then
    fail "OpenClaw production install contains test sources"
  fi

  touch "$target/previous-version-marker"
  mkdir -p "$target.rollback"
  touch "$target.rollback/legacy-rollback-marker"
  HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" update
  assert_absent "$target/previous-version-marker"
  assert_file "$rollback/previous-version-marker"
  assert_absent "$target.rollback"
  HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" rollback
  assert_file "$target/previous-version-marker"

  touch "$STATE/fail-npm"
  if HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" update; then
    fail "OpenClaw update unexpectedly succeeded when dependency installation failed"
  fi
  assert_file "$target/previous-version-marker"
  rm -f "$STATE/fail-npm"

  HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" health
  HOME="$home" OPENCLAW_HOME="$openclaw_home" "$ROOT/scripts/manage-openclaw-bridge.sh" uninstall
  assert_absent "$target"
  assert_absent "$rollback"
  assert_absent "$target.rollback"
  assert_file "$openclaw_home/openclaw.json"
}

run_macos_hermes_contract
run_linux_hermes_contract
run_openclaw_contract

echo "Isolated bridge lifecycle contract passed for macOS launchd, Linux systemd, and OpenClaw packaging."
