#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 /path/to/pinned/openclaw-checkout" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCLAW="$(cd "$1" && pwd)"
EXPECTED_COMMIT="$(node -e 'const m=require(process.argv[1]); console.log(m.plugins.find((p)=>p.id==="openclaw-bridge").supportedHarness.commit)' "$ROOT/compatibility-manifest.json")"
EXPECTED_VERSION="$(node -e 'const m=require(process.argv[1]); console.log(m.plugins.find((p)=>p.id==="openclaw-bridge").supportedHarness.version.replace(/^v/, ""))' "$ROOT/compatibility-manifest.json")"
ACTUAL_COMMIT="$(git -C "$OPENCLAW" rev-parse HEAD)"
ACTUAL_VERSION="$(node -e 'console.log(require(process.argv[1]).version)' "$OPENCLAW/package.json")"
[[ "$ACTUAL_COMMIT" == "$EXPECTED_COMMIT" ]] || {
  echo "OpenClaw checkout is $ACTUAL_COMMIT; compatibility manifest requires $EXPECTED_COMMIT" >&2
  exit 1
}
[[ "$ACTUAL_VERSION" == "$EXPECTED_VERSION" ]] || {
  echo "OpenClaw checkout version is $ACTUAL_VERSION; compatibility manifest requires $EXPECTED_VERSION" >&2
  exit 1
}

OVERLAY="$(mktemp -d "${TMPDIR:-/tmp}/relay-openclaw-conformance.XXXXXX")"
cleanup() {
  rm -rf "$OVERLAY"
}
trap cleanup EXIT

cp "$ROOT/plugins/openclaw-bridge/openclaw-extension/index.ts" "$OVERLAY/index.ts"
cp "$ROOT/plugins/openclaw-bridge/openclaw-extension/package.json" "$OVERLAY/package.json"
cp "$ROOT/plugins/openclaw-bridge/openclaw-extension/package-lock.json" "$OVERLAY/package-lock.json"
mkdir -p "$OVERLAY/src"
for source_file in "$ROOT"/plugins/openclaw-bridge/openclaw-extension/src/*.ts; do
  [[ "$source_file" == *.test.ts ]] && continue
  cp "$source_file" "$OVERLAY/src/$(basename "$source_file")"
done

(
  cd "$OVERLAY"
  npm ci --ignore-scripts --no-audit --no-fund >/dev/null
  archive="$(npm pack --silent --pack-destination "$OVERLAY" "openclaw@$EXPECTED_VERSION")"
  mkdir -p node_modules/openclaw
  tar -xzf "$archive" -C node_modules/openclaw --strip-components=1
  node -e 'const p=require("./node_modules/openclaw/package.json"); if (p.version !== process.argv[1]) throw new Error(`unexpected published SDK ${p.version}`)' "$EXPECTED_VERSION"
)

cat > "$OVERLAY/tsconfig.json" <<'JSON'
{
  "compilerOptions": {
    "allowSyntheticDefaultImports": true,
    "esModuleInterop": true,
    "forceConsistentCasingInFileNames": true,
    "lib": ["DOM", "DOM.Iterable", "ES2023"],
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "noEmit": true,
    "noImplicitOverride": true,
    "noImplicitReturns": true,
    "resolveJsonModule": true,
    "skipLibCheck": true,
    "strict": true,
    "target": "ES2023",
    "types": ["node"],
    "verbatimModuleSyntax": true
  },
  "include": ["./index.ts", "./src/**/*.ts"]
}
JSON

"$OVERLAY/node_modules/.bin/tsc" -p "$OVERLAY/tsconfig.json" --pretty false
echo "OpenClaw bridge passed strict TypeScript conformance against published openclaw@$EXPECTED_VERSION at $EXPECTED_COMMIT."
