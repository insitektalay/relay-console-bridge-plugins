#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 /path/to/pinned/hermes-agent-checkout" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES="$(cd "$1" && pwd)"
EXPECTED_COMMIT="$(node -e 'const m=require(process.argv[1]); console.log(m.plugins.find((p)=>p.id==="hermes-agent-bridge").supportedHarness.commit)' "$ROOT/compatibility-manifest.json")"
ACTUAL_COMMIT="$(git -C "$HERMES" rev-parse HEAD)"
[[ "$ACTUAL_COMMIT" == "$EXPECTED_COMMIT" ]] || {
  echo "Hermes checkout is $ACTUAL_COMMIT; compatibility manifest requires $EXPECTED_COMMIT" >&2
  exit 1
}

PYTHON="${HERMES_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  for candidate in "$HERMES/.venv/bin/python" "$HERMES/venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      PYTHON="$candidate"
      break
    fi
  done
fi
[[ -n "$PYTHON" && -x "$PYTHON" ]] || {
  echo "Set HERMES_PYTHON or create the pinned checkout's Python environment" >&2
  exit 1
}

OVERLAY="$(mktemp -d "${TMPDIR:-/tmp}/relay-hermes-conformance.XXXXXX")"
cleanup() {
  rm -rf "$OVERLAY"
}
trap cleanup EXIT

mkdir -p "$OVERLAY/clawchat_bridge" "$OVERLAY/tests" "$OVERLAY/contracts/fixtures"
cp "$ROOT/plugins/hermes-agent-bridge/src/"*.py "$OVERLAY/clawchat_bridge/"
cp "$ROOT/plugins/hermes-agent-bridge/tests/"*.py "$OVERLAY/tests/"
cp "$ROOT/contracts/fixtures/"*.json "$OVERLAY/contracts/fixtures/"
touch "$OVERLAY/clawchat_bridge/__init__.py"

(
  cd "$HERMES"
  RELAY_CONNECTOR_FIXTURE_DIR="$OVERLAY/contracts/fixtures" \
    PYTHONPATH="$OVERLAY:$HERMES${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m pytest -q "$OVERLAY/tests"
)
