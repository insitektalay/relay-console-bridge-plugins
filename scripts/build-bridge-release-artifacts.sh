#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$ROOT/dist/release}"

"$ROOT/scripts/bridge-acceptance-gate.mjs" --stable
"$ROOT/scripts/bridge-release-gate.mjs" --stable

RELEASE="$(node -e 'console.log(require(process.argv[1]).release)' "$ROOT/compatibility-manifest.json")"
VERSION="${RELEASE#v}"
ARCHIVE="relay-console-bridge-plugins-$VERSION.tar.gz"
TEMPORARY="$(mktemp -d "${TMPDIR:-/tmp}/relay-bridge-release.XXXXXX")"

cleanup() {
  rm -rf "$TEMPORARY"
}
trap cleanup EXIT

git -C "$ROOT" archive \
  --format=tar.gz \
  --prefix="relay-console-bridge-plugins-$VERSION/" \
  --output="$TEMPORARY/$ARCHIVE" \
  "$RELEASE"

tar -tzf "$TEMPORARY/$ARCHIVE" > "$TEMPORARY/archive-contents.txt"
if grep -E '(^|/)(\.git|node_modules|\.venv|venv|config\.json)(/|$)|(^|/)\.env([./]|$)' "$TEMPORARY/archive-contents.txt"; then
  echo "release archive contains a forbidden path" >&2
  exit 1
fi

mkdir -p "$OUTPUT"
install -m 644 "$TEMPORARY/$ARCHIVE" "$OUTPUT/$ARCHIVE"
(
  cd "$OUTPUT"
  if command -v sha256sum >/dev/null; then
    sha256sum "$ARCHIVE" > SHA256SUMS
  else
    shasum -a 256 "$ARCHIVE" > SHA256SUMS
  fi
)

echo "Built stable bridge release artifacts:"
echo "  $OUTPUT/$ARCHIVE"
echo "  $OUTPUT/SHA256SUMS"
