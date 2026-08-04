#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/install-hermes-agent-bridge.sh /path/to/hermes-agent" >&2
  exit 2
fi

TARGET="$1"
if [ ! -d "$TARGET" ]; then
  echo "target Hermes Agent checkout does not exist: $TARGET" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTIVE="$TARGET/clawchat_bridge"
STAGING="$TARGET/.clawchat_bridge.install.$$"
PREVIOUS="$TARGET/.clawchat_bridge.previous.$$"

cleanup() {
  rm -rf "$STAGING"
  if [[ -d "$PREVIOUS" && ! -e "$ACTIVE" ]]; then
    mv "$PREVIOUS" "$ACTIVE"
  fi
}
trap cleanup EXIT

rm -rf "$STAGING" "$PREVIOUS"
mkdir -p "$STAGING"
for source in "$ROOT"/plugins/hermes-agent-bridge/src/*.py; do
  install -m 600 "$source" "$STAGING/$(basename "$source")"
done

if [[ -d "$ACTIVE" ]]; then
  mv "$ACTIVE" "$PREVIOUS"
fi
if ! mv "$STAGING" "$ACTIVE"; then
  [[ -d "$PREVIOUS" ]] && mv "$PREVIOUS" "$ACTIVE"
  exit 1
fi
rm -rf "$PREVIOUS"
chmod -R go-rwx "$ACTIVE"

echo "Installed Hermes Agent bridge into: $TARGET"
echo "Next:"
echo "  cd $TARGET"
echo "  scripts/manage-hermes-agent-bridge.sh prepare-runtime"
echo "Relay Console did not install, update, authenticate, or start Hermes Agent."
