#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/plugins/openclaw-bridge/openclaw-extension"
OPENCLAW_ROOT="${OPENCLAW_STATE_DIR:-${OPENCLAW_HOME:-$HOME}/.openclaw}"
TARGET="$OPENCLAW_ROOT/extensions/clawchat"
STATE_ROOT="${OPENCLAW_BRIDGE_STATE_ROOT:-$OPENCLAW_ROOT/.relay-console-bridge/clawchat}"
ROLLBACK="$STATE_ROOT/rollback"
STAGING="$STATE_ROOT/candidate.$$"
PREVIOUS="$STATE_ROOT/previous.$$"
LEGACY_ROLLBACK="$TARGET.rollback"

cleanup() {
  rm -rf "$STAGING"
}
trap cleanup EXIT

prepare_state_root() {
  mkdir -p "$STATE_ROOT"
  chmod 700 "$(dirname "$STATE_ROOT")" "$STATE_ROOT"
  if [[ -e "$LEGACY_ROLLBACK" ]]; then
    [[ ! -e "$ROLLBACK" ]] || {
      echo "Both legacy and protected OpenClaw rollback directories exist; refusing to guess which one to keep." >&2
      return 1
    }
    mv "$LEGACY_ROLLBACK" "$ROLLBACK"
  fi
}

stage_files() {
  command -v node >/dev/null || { echo "Node.js is required" >&2; return 1; }
  command -v npm >/dev/null || { echo "npm is required" >&2; return 1; }
  command -v openclaw >/dev/null || { echo "OpenClaw is required" >&2; return 1; }

  rm -rf "$STAGING"
  mkdir -p "$STAGING"
  install -m 600 "$SOURCE/index.ts" "$STAGING/index.ts"
  install -m 600 "$SOURCE/openclaw.plugin.json" "$STAGING/openclaw.plugin.json"
  install -m 600 "$SOURCE/package.json" "$STAGING/package.json"
  install -m 600 "$SOURCE/package-lock.json" "$STAGING/package-lock.json"
  mkdir -m 700 "$STAGING/src"
  local source_file
  for source_file in "$SOURCE"/src/*.ts; do
    [[ "$source_file" == *.test.ts ]] && continue
    install -m 600 "$source_file" "$STAGING/src/$(basename "$source_file")"
  done

  (
    cd "$STAGING"
    npm ci --ignore-scripts
    npm run build
    npm prune --omit=dev --ignore-scripts
    node -e 'JSON.parse(require("node:fs").readFileSync("package.json", "utf8")); JSON.parse(require("node:fs").readFileSync("openclaw.plugin.json", "utf8"));'
  )
  chmod -R go-rwx "$STAGING"
}

activate_candidate() {
  rm -rf "$PREVIOUS"
  if [[ -d "$TARGET" ]]; then
    mv "$TARGET" "$PREVIOUS"
  fi
  if ! mv "$STAGING" "$TARGET"; then
    [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$TARGET"
    return 1
  fi
  rm -rf "$ROLLBACK"
  if [[ -d "$PREVIOUS" ]]; then
    mv "$PREVIOUS" "$ROLLBACK"
  fi
}

install_or_update() {
  mkdir -p "$(dirname "$TARGET")"
  prepare_state_root
  stage_files
  activate_candidate
  OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw plugins install "$TARGET" --force
  OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw plugins enable clawchat
}

rollback_active() {
  prepare_state_root
  [[ -d "$ROLLBACK" ]] || { echo "No rollback version" >&2; return 1; }
  rm -rf "$PREVIOUS"
  [[ -d "$TARGET" ]] && mv "$TARGET" "$PREVIOUS"
  if ! mv "$ROLLBACK" "$TARGET"; then
    [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$TARGET"
    return 1
  fi
  if [[ -d "$PREVIOUS" ]]; then
    mv "$PREVIOUS" "$ROLLBACK"
  fi
}

finish_message() {
  echo "Relay Console bridge files are installed at $TARGET"
  echo "Relay Console did not install, update, start, stop, or configure OpenClaw."
  echo "Configure the existing clawchat channel with your Relay bridge device credentials,"
  echo "then restart OpenClaw using the lifecycle you already use for your runtime."
}

case "$COMMAND" in
  install|update)
    install_or_update
    finish_message
    ;;
  rollback)
    rollback_active
    finish_message
    ;;
  status)
    test -f "$TARGET/openclaw.plugin.json" || { echo "Relay Console bridge is not installed" >&2; exit 1; }
    OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw gateway status
    ;;
  logs)
    OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw logs --follow
    ;;
  health)
    test -f "$TARGET/openclaw.plugin.json"
    OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw --version
    OPENCLAW_STATE_DIR="$OPENCLAW_ROOT" openclaw gateway status
    echo "Relay Console bridge files are present at $TARGET"
    ;;
  uninstall)
    rm -rf "$TARGET" "$STATE_ROOT" "$LEGACY_ROLLBACK"
    echo "Removed Relay Console bridge files. OpenClaw and its configuration were not removed."
    ;;
  *)
    echo "usage: $0 install|update|rollback|status|logs|health|uninstall" >&2
    exit 2
    ;;
esac
