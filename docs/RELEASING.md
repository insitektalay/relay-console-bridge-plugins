# Bridge release process

The bridge remains a preview until every gate below has direct evidence. Running
an isolated lifecycle contract or compiling against a pinned harness does not
count as clean-host or cross-client acceptance.

## Pull-request gate

Every bridge pull request must pass:

```bash
npm ci --prefix plugins/openclaw-bridge/openclaw-extension
npm test --prefix plugins/openclaw-bridge/openclaw-extension
node --test scripts/bridge-acceptance-gate.test.mjs
scripts/bridge-acceptance-gate.mjs
node --test scripts/bridge-release-gate.test.mjs
scripts/bridge-release-gate.mjs
scripts/test-bridge-lifecycle.sh
scripts/verify-sanitized.sh
bash -n scripts/*.sh
```

CI additionally checks the exact Hermes source revision and the published
OpenClaw SDK named in `compatibility-manifest.json`. The lifecycle contract uses
isolated fake service managers; it proves transactional behavior and packaging,
not a real clean machine.

## Stable-release prerequisites

Before changing `releaseStatus` to `stable`:

1. Review and merge the hardening pull request.
2. Run the complete install, enrollment, dispatch, reconnect, rollback, and
   uninstall journey on clean supported macOS and Linux hosts for both runtimes.
3. Create one machine-readable clean-host record for every plugin/host pair and
   one cross-client record for each plugin under
   `acceptance/records/<manifest-release>/`. Start from the templates in
   `acceptance/templates/`, bind every record to the exact backend deployment,
   bridge, harness and client versions, attach only redacted repository evidence
   with verified SHA-256 digests, and require an independent reviewer.
4. Run `scripts/bridge-acceptance-gate.mjs --stable`; only then record each
   candidate host as `passed` in the compatibility manifest.
5. Complete web, iPhone/iPad, and macOS dispatch acceptance through Relay Cloud.
6. Clear every `knownGaps` entry, remove prerelease suffixes, and add release
   notes at `docs/releases/<release>.md`.
7. Set `supportedBackend.version` to the exact backend package version and
   `supportedBackend.commit` to the full immutable backend compatibility
   baseline commit. Record the operator-configured HTTPS backend used for each
   acceptance run. The unified
   product release manifest separately records the final product source commit
   and deployment identity; do not try to embed a repository commit inside that
   same commit.
8. Copy the reviewed manifest into the Railway backend, run its compatibility
   policy tests, deploy from `backend/`, and verify Railway before advertising
   the release.

Declaring `releaseStatus` as `stable` automatically activates every stable
content, acceptance, clean-worktree, and exact-tag check in the ordinary CI
commands. The `--stable` option is an explicit preflight and cannot be bypassed
by changing only the manifest label.

## Tag and artifacts

From a clean reviewed commit, create an exact annotated tag matching the
manifest's `release`, then run:

```bash
scripts/bridge-release-gate.mjs --stable
scripts/build-bridge-release-artifacts.sh /path/to/release-output
```

The stable gate refuses dirty trees, prerelease versions, a lightweight or
missing exact tag, missing or malformed
clean-host/cross-client evidence, secret-bearing record fields, evidence digest
drift, an unpinned backend release, known gaps, missing release notes, and a
HEAD without the exact tag.
The artifact command uses `git archive`, checks the archive for forbidden local
state, and emits `SHA256SUMS`. Upload the archive, checksum file, and matching
release notes to the reviewed GitHub release. Do not publish from a draft pull
request or from an uncommitted worktree.

## Rollback

Do not move or delete the previous stable release. If acceptance fails after
publication, mark the affected compatibility row unsupported, restore the
previous backend manifest and backend deployment, and direct runtime hosts to
the documented bridge rollback command. Record the incident and the exact
backend, bridge, Hermes, and OpenClaw versions involved.
