# Bridge acceptance records

Stable bridge releases require direct, independently reviewed evidence. Put
records for the current manifest release under:

```text
acceptance/records/<manifest-release>/
```

For each plugin, record exactly:

- one clean-host record for every `candidateHostOS` in
  `compatibility-manifest.json`, with `runtimeLocation` set to `same-mac` for
  `macos-launchd` and `linux-vps` for `linux-systemd`; and
- one cross-client record covering macOS, web, iPhone, and iPad dispatch, with
  the runtime on a `second-computer`.

These locations prove the three supported launch placements: a runtime beside
Relay Console on the Mac, a runtime on another computer reached through Relay
Cloud, and a runtime on a Linux VPS reached through Relay Cloud.

Start from `templates/clean-host.json` or `templates/cross-client.json`. Replace
every placeholder, store only redacted evidence under `acceptance/evidence/`,
calculate its SHA-256, and have a second person review the result. Never record
device tokens, API keys, OAuth tokens, passwords, cookies, authorization
headers, customer content, or unredacted logs.

Validate the current preview records with:

```bash
scripts/bridge-acceptance-gate.mjs
```

The stable release gate additionally requires the complete six-record matrix:

```bash
scripts/bridge-acceptance-gate.mjs --stable
scripts/bridge-release-gate.mjs --stable
```

An isolated test fixture, CI runner, compilation, screenshot, or prose statement
is not a clean-host acceptance record.
