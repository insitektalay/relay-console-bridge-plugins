#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

secret_value_pattern='(-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[0-9]{8,}-[A-Za-z0-9-]{10,}|(sk|rk)_live_[0-9A-Za-z]{16,}|whsec_[0-9A-Za-z]{16,}|sk-proj-[0-9A-Za-z_-]{20,}|sk-ant-api03-[0-9A-Za-z_-]{20,}|Bearer[[:space:]]+[A-Za-z0-9._~+/=-]{20,}|(deviceToken|accessToken|refreshToken|api[_-]?key|client[_-]?secret|webhook[_-]?secret|password|cookie)[[:space:]]*[:=][[:space:]]*["'\'']?[A-Za-z0-9._~+/=-]{20,})'
SCAN_OUTPUT="$(mktemp "${TMPDIR:-/tmp}/relay-bridge-secret-scan.XXXXXX")"
cleanup() {
  unlink "$SCAN_OUTPUT" 2>/dev/null || true
}
trap cleanup EXIT

if grep -RIE "$secret_value_pattern" "$ROOT" \
  --exclude-dir='.git' \
  --exclude-dir='node_modules' \
  --exclude-dir='.venv' \
  --exclude-dir='venv' \
  --exclude-dir='.pytest_cache' \
  --exclude-dir='__pycache__' \
  --exclude-dir='dist' \
  --exclude-dir='build' \
  --exclude='*.example.json' \
  --exclude='verify-sanitized.sh' >"$SCAN_OUTPUT"; then
  echo "Potential secret-like content found. Review:" >&2
  cat "$SCAN_OUTPUT" >&2
  exit 1
fi

echo "Sanitization scan passed."
