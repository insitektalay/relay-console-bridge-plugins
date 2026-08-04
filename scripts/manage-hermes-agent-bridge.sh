#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-}"
HERMES="${HERMES_HOME:-${2:-}}"
[[ -n "$HERMES" ]] || {
  echo "Set HERMES_HOME or pass the Hermes checkout as the second argument" >&2
  exit 2
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="relay-hermes-bridge"
LAUNCHD_LABEL="work.relayconsole.hermes-bridge"
LAUNCHD_PLIST="$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
CONFIG="${HERMES_BRIDGE_CONFIG:-$HOME/.hermes/clawchat_bridge/config.json}"
LOG_DIR="${HERMES_BRIDGE_LOG_DIR:-$HOME/.hermes/clawchat_bridge/logs}"
RUNTIME_DIR="${HERMES_BRIDGE_RUNTIME_DIR:-$HOME/.hermes/clawchat_bridge/runtime}"
ACTIVE="$HERMES/clawchat_bridge"
ROLLBACK="$HERMES/clawchat_bridge.rollback"
STAGING="$HERMES/.clawchat_bridge.candidate.$$"
PREVIOUS="$HERMES/.clawchat_bridge.previous.$$"
AIOHTTP_REQUIREMENT="aiohttp>=3.10,<4"

cleanup() {
  rm -rf "$STAGING" "$RUNTIME_DIR.candidate.$$" "$RUNTIME_DIR.previous.$$"
}
trap cleanup EXIT

resolve_python() {
  if [[ -n "${HERMES_PYTHON:-}" ]]; then
    [[ -x "$HERMES_PYTHON" ]] || {
      echo "HERMES_PYTHON is not executable: $HERMES_PYTHON" >&2
      return 1
    }
    printf '%s\n' "$HERMES_PYTHON"
    return 0
  fi
  local candidate
  for candidate in "$HERMES/.venv/bin/python" "$HERMES/venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "Set HERMES_PYTHON or provide $HERMES/.venv/bin/python or $HERMES/venv/bin/python" >&2
  return 1
}

HERMES_PYTHON_EXECUTABLE="$(resolve_python)"
BRIDGE_PYTHON="$RUNTIME_DIR/bin/python"

platform_name() {
  uname -s
}

service_kind() {
  case "$(platform_name)" in
    Darwin)
      command -v launchctl >/dev/null || {
        echo "launchctl is required for the macOS bridge service" >&2
        return 1
      }
      printf '%s\n' launchd
      ;;
    Linux)
      command -v systemctl >/dev/null || {
        echo "systemctl is required for the Linux bridge service" >&2
        return 1
      }
      printf '%s\n' systemd
      ;;
    *)
      echo "Automatic service installation is supported on Linux systemd and macOS launchd." >&2
      echo "Run manually: $BRIDGE_PYTHON -m clawchat_bridge.main --config $CONFIG run" >&2
      return 1
      ;;
  esac
}

stage_files() {
  [[ -d "$HERMES" ]] || { echo "Hermes checkout does not exist: $HERMES" >&2; return 1; }
  rm -rf "$STAGING"
  mkdir -p "$STAGING"
  for source in "$ROOT"/plugins/hermes-agent-bridge/src/*.py; do
    install -m 600 "$source" "$STAGING/$(basename "$source")"
  done
  "$BRIDGE_PYTHON" -m py_compile "$STAGING"/*.py
}

hermes_import_paths() {
  "$HERMES_PYTHON_EXECUTABLE" -c '
import sysconfig

paths = sysconfig.get_paths()
for key in ("purelib", "platlib"):
    value = paths.get(key)
    if value:
        print(value)
'
}

validate_bridge_environment() {
  local python="${1:-$BRIDGE_PYTHON}"
  [[ -x "$python" ]] || return 1
  "$python" -c '
import importlib.metadata
import re

try:
    installed = importlib.metadata.version("aiohttp")
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(1)
match = re.match(r"^(\d+)\.(\d+)", installed)
if not match or int(match.group(1)) != 3 or int(match.group(2)) < 10:
    raise SystemExit(2)
import aiohttp
'
}

create_bridge_environment() {
  local candidate="$RUNTIME_DIR.candidate.$$"
  local previous="$RUNTIME_DIR.previous.$$"
  local bridge_site_packages
  local hermes_paths
  local path

  rm -rf "$candidate" "$previous"
  mkdir -p "$(dirname "$RUNTIME_DIR")"
  chmod 700 "$(dirname "$RUNTIME_DIR")"

  if ! "$HERMES_PYTHON_EXECUTABLE" -m venv "$candidate"; then
    rm -rf "$candidate"
    if command -v uv >/dev/null; then
      uv venv --python "$HERMES_PYTHON_EXECUTABLE" "$candidate"
    else
      echo "Could not create the isolated Relay bridge Python environment." >&2
      echo "Install Python venv support (or uv), then retry. Hermes was not changed." >&2
      return 1
    fi
  fi

  bridge_site_packages="$("$candidate/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
  [[ -n "$bridge_site_packages" ]] || {
    echo "Could not locate the isolated bridge site-packages directory." >&2
    return 1
  }
  mkdir -p "$bridge_site_packages"

  if "$candidate/bin/python" -m pip --version >/dev/null 2>&1; then
    "$candidate/bin/python" -m pip install --disable-pip-version-check "$AIOHTTP_REQUIREMENT"
  elif command -v uv >/dev/null; then
    uv pip install --python "$candidate/bin/python" "$AIOHTTP_REQUIREMENT"
  else
    echo "The isolated Relay bridge environment has no package installer." >&2
    echo "Install uv or ensure Python venv includes pip, then retry. Hermes was not changed." >&2
    return 1
  fi

  hermes_paths="$(hermes_import_paths)"
  {
    printf '%s\n' "$HERMES"
    while IFS= read -r path; do
      [[ -z "$path" || "$path" == "$HERMES" ]] || printf '%s\n' "$path"
    done <<< "$hermes_paths"
  } > "$bridge_site_packages/relay-console-hermes.pth"

  validate_bridge_environment "$candidate/bin/python" || {
    echo "The isolated Relay bridge environment did not install a compatible aiohttp 3.x release." >&2
    return 1
  }

  [[ -d "$RUNTIME_DIR" ]] && mv "$RUNTIME_DIR" "$previous"
  if ! mv "$candidate" "$RUNTIME_DIR" || ! validate_bridge_environment; then
    rm -rf "$RUNTIME_DIR"
    [[ -d "$previous" ]] && mv "$previous" "$RUNTIME_DIR"
    echo "Could not activate the isolated Relay bridge environment." >&2
    return 1
  fi
  rm -rf "$previous"
  chmod -R go-rwx "$RUNTIME_DIR"
}

ensure_bridge_environment() {
  local pth=""
  local required_path=""
  if validate_bridge_environment; then
    pth="$("$BRIDGE_PYTHON" -c 'import site; print(site.getsitepackages()[0])')/relay-console-hermes.pth"
    if [[ -f "$pth" ]] && grep -Fx "$HERMES" "$pth" >/dev/null; then
      while IFS= read -r required_path; do
        if [[ -n "$required_path" ]] && ! grep -Fx "$required_path" "$pth" >/dev/null; then
          create_bridge_environment
          return
        fi
      done <<< "$(hermes_import_paths)"
      return 0
    fi
  fi
  create_bridge_environment
}

validate_service_prerequisites() {
  ensure_bridge_environment
  [[ -f "$CONFIG" ]] || {
    echo "Bridge config is missing at $CONFIG. Enroll the device before enabling the service." >&2
    return 1
  }
  chmod 700 "$(dirname "$CONFIG")"
  chmod 600 "$CONFIG"
  service_kind >/dev/null
}

write_systemd_service() {
  local unit="$HOME/.config/systemd/user/$SERVICE.service"
  mkdir -p "$(dirname "$unit")"
  "$HERMES_PYTHON_EXECUTABLE" - "$unit" "$HERMES" "$BRIDGE_PYTHON" "$CONFIG" <<'PY'
import os
import pathlib
import sys
import tempfile

unit_path, working_directory, python, config = sys.argv[1:]

def quote_command_arg(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SystemExit("service paths must not contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

def path_value(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise SystemExit("service paths must not contain newlines")
    if not value.startswith("/"):
        raise SystemExit("systemd service paths must be absolute")
    # Path-valued directives consume the complete value and do not use the
    # command-line quoting rules of ExecStart=. Quotes would become part of
    # the path. Percent signs still need escaping from specifier expansion.
    return value.replace("%", "%%")

content = "\n".join([
    "[Unit]",
    "Description=Relay Console Hermes runtime bridge",
    "After=network-online.target",
    "Wants=network-online.target",
    "[Service]",
    "Type=simple",
    f"WorkingDirectory={path_value(working_directory)}",
    f"ExecStart={quote_command_arg(python)} -m clawchat_bridge.main --config {quote_command_arg(config)} run",
    "Restart=always",
    "RestartSec=5",
    "UMask=0077",
    "[Install]",
    "WantedBy=default.target",
    "",
])

destination = pathlib.Path(unit_path)
fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  if command -v systemd-analyze >/dev/null; then
    systemd-analyze --user verify "$unit"
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE.service"
}

write_launchd_service() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  chmod 700 "$LOG_DIR"
  "$HERMES_PYTHON_EXECUTABLE" - "$LAUNCHD_PLIST" "$LAUNCHD_LABEL" "$BRIDGE_PYTHON" "$CONFIG" "$HERMES" "$LOG_DIR" <<'PY'
import os
import pathlib
import plistlib
import sys
import tempfile

plist_path, label, python, config, working_directory, log_directory = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [python, "-m", "clawchat_bridge.main", "--config", config, "run"],
    "WorkingDirectory": working_directory,
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(pathlib.Path(log_directory) / "bridge.log"),
    "StandardErrorPath": str(pathlib.Path(log_directory) / "bridge.error.log"),
}

destination = pathlib.Path(plist_path)
fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
try:
    with os.fdopen(fd, "wb") as handle:
        plistlib.dump(payload, handle, fmt=plistlib.FMT_XML, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  launchctl bootout "gui/$UID/$LAUNCHD_LABEL" 2>/dev/null || true
  bootstrap_launchd_service
}

bootstrap_launchd_service() {
  local attempt
  local detail=""
  for attempt in 1 2 3; do
    if detail="$(launchctl bootstrap "gui/$UID" "$LAUNCHD_PLIST" 2>&1)"; then
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then
      # launchd can briefly retain the old job after bootout and return EIO.
      # Repeating bootout is safe and gives its per-user domain time to settle.
      launchctl bootout "gui/$UID/$LAUNCHD_LABEL" 2>/dev/null || true
      sleep "0.$((attempt * 2))"
    fi
  done
  [[ -z "$detail" ]] || printf '%s\n' "$detail" >&2
  return 1
}

install_service() {
  validate_service_prerequisites
  case "$(service_kind)" in
    systemd) write_systemd_service ;;
    launchd) write_launchd_service ;;
  esac
}

stop_service() {
  case "$(platform_name)" in
    Darwin)
      command -v launchctl >/dev/null && launchctl bootout "gui/$UID/$LAUNCHD_LABEL" 2>/dev/null || true
      ;;
    Linux)
      command -v systemctl >/dev/null && systemctl --user stop "$SERVICE.service" 2>/dev/null || true
      ;;
  esac
}

restore_service_best_effort() {
  if [[ -d "$ACTIVE" && -f "$CONFIG" ]]; then
    install_service >/dev/null 2>&1 || true
  fi
}

run_bridge() {
  (
    cd "$HERMES"
    "$BRIDGE_PYTHON" -m clawchat_bridge.main --config "$CONFIG" "$@"
  )
}

activate_candidate() {
  validate_service_prerequisites
  stop_service
  rm -rf "$PREVIOUS"
  if [[ -d "$ACTIVE" ]]; then
    mv "$ACTIVE" "$PREVIOUS"
  fi

  if ! mv "$STAGING" "$ACTIVE"; then
    [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$ACTIVE"
    restore_service_best_effort
    return 1
  fi

  if ! install_service; then
    stop_service
    rm -rf "$ACTIVE"
    [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$ACTIVE"
    restore_service_best_effort
    echo "Bridge activation failed; the previous bridge was restored." >&2
    return 1
  fi

  rm -rf "$ROLLBACK"
  if [[ -d "$PREVIOUS" ]]; then
    mv "$PREVIOUS" "$ROLLBACK"
  fi
}

install_or_update() {
  ensure_bridge_environment
  stage_files
  activate_candidate
}

rollback_active() {
  [[ -d "$ROLLBACK" ]] || { echo "No rollback version" >&2; return 1; }
  validate_service_prerequisites
  stop_service
  rm -rf "$PREVIOUS"
  [[ -d "$ACTIVE" ]] && mv "$ACTIVE" "$PREVIOUS"
  mv "$ROLLBACK" "$ACTIVE"

  if ! "$BRIDGE_PYTHON" -m py_compile "$ACTIVE/main.py" || ! install_service; then
    stop_service
    rm -rf "$ACTIVE"
    [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$ACTIVE"
    restore_service_best_effort
    echo "Rollback activation failed; the previous bridge was restored." >&2
    return 1
  fi

  if [[ -d "$PREVIOUS" ]]; then
    mv "$PREVIOUS" "$ROLLBACK"
  fi
}

case "$COMMAND" in
  prepare-runtime)
    ensure_bridge_environment
    echo "Prepared isolated Relay Console bridge environment at $RUNTIME_DIR. Hermes packages were not changed."
    ;;
  install|update)
    install_or_update
    ;;
  rollback)
    rollback_active
    ;;
  rotate-credential)
    validate_service_prerequisites
    stop_service
    if ! run_bridge rotate-credential; then
      restore_service_best_effort
      exit 1
    fi
    install_service
    ;;
  status)
    run_bridge status
    case "$(service_kind)" in
      systemd) systemctl --user status "$SERVICE.service" --no-pager ;;
      launchd) launchctl print "gui/$UID/$LAUNCHD_LABEL" ;;
    esac
    ;;
  health)
    validate_service_prerequisites
    "$BRIDGE_PYTHON" -m py_compile "$ACTIVE/main.py"
    run_bridge status >/dev/null
    echo "Relay Console Hermes bridge files and redacted configuration are valid."
    ;;
  logs)
    case "$(service_kind)" in
      systemd) journalctl --user -u "$SERVICE.service" -f ;;
      launchd)
        mkdir -p "$LOG_DIR"
        touch "$LOG_DIR/bridge.log" "$LOG_DIR/bridge.error.log"
        tail -F "$LOG_DIR/bridge.log" "$LOG_DIR/bridge.error.log"
        ;;
    esac
    ;;
  uninstall)
    stop_service
    if [[ "$(platform_name)" == "Linux" ]] && command -v systemctl >/dev/null; then
      systemctl --user disable "$SERVICE.service" 2>/dev/null || true
    fi
    rm -f "$HOME/.config/systemd/user/$SERVICE.service" "$LAUNCHD_PLIST"
    if [[ "$(platform_name)" == "Linux" ]] && command -v systemctl >/dev/null; then
      systemctl --user daemon-reload || true
    fi
    rm -rf "$ACTIVE" "$ROLLBACK" "$RUNTIME_DIR"
    echo "Removed Relay Console bridge code, isolated environment, and service. Hermes and $CONFIG were not removed."
    ;;
  *)
    echo "usage: $0 prepare-runtime|install|update|rollback|rotate-credential|status|health|logs|uninstall /path/to/hermes" >&2
    exit 2
    ;;
esac
