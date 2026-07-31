# Relay Console Bridge Plugins

MIT-licensed preview bridge plugins that connect user-managed Hermes Agent and
OpenClaw runtimes to an operator's Relay Console Railway backend.

Relay Console does not install, authenticate, update, start, stop, or remove the
runtime itself. The bridge runs beside or inside the runtime and makes an
outbound authenticated connection to Relay's Railway backend; it never requires
an inbound public port on the runtime host.

## Plugins

- `plugins/hermes-agent-bridge`
  - Standalone Hermes Agent runtime bridge for macOS and Linux.
  - Receives Relay Console runtime dispatches over authenticated WSS.
  - Executes dispatches through Hermes Agent.
  - Sends runtime lifecycle events back to Relay Cloud.
  - Includes reconnect backfill for missed dispatch recovery.

- `plugins/openclaw-bridge`
  - OpenClaw channel extension that connects the user's existing gateway to
    Relay Cloud.
  - Receives dispatches, reconnects, backfills, and returns responses.
  - Keeps bridge device credentials on the OpenClaw host.

## What Is Not Included

This repo deliberately excludes machine-specific and secret-bearing files:

- `.hermes/clawchat_bridge/config.json`
- bridge device tokens
- workspace-specific credentials
- terminal event outboxes
- dispatch state ledgers
- logs
- local app `.env` files
- Relay Console database state

Each developer or machine must enroll/register its own bridge device.

## When these plugins are needed

Relay Console Swift can dispatch directly to Hermes and OpenClaw runtimes that
the macOS app manages or connects on the same Mac. It also contains its own
optional Relay Connect device transport and Marketplace tool bridge. Those
components are part of the main Relay Console repository and are not copies of
the plugins here.

Use this repository when Hermes or OpenClaw is user-managed independently of
Relay Console Swift, including another Mac, a Linux host, or a VPS. Each bridge
connects outbound to the operator's own HTTPS Railway backend; no shared Relay
Console service or inbound runtime-host port is required.

## Install and operate

Use [the install guide](docs/INSTALL.md) for exact install, enrollment, update,
rollback, health, logs, and uninstall commands. Security and credential handling
are documented in [the security guide](docs/SECURITY.md).

The internal `clawchat_bridge`, `.clawchat`, channel, and capability identifiers
are retained for compatibility with existing runtimes and backend contracts.
They do not identify the public repository or product name.

## Release status

This repository is **preview**, not stable. The compatibility manifest is the
source of truth. Do not advertise a host/runtime combination until its acceptance
row passes, and do not create a stable tag while `knownGaps` is non-empty.

## Relay backend contract

The Relay Railway backend exposes the bridge endpoints and websocket message
contracts documented under `contracts/`.

Most important for durable Hermes dispatch delivery:

```text
POST /api/v1/bridge/runtime-dispatches/backfill
```

That endpoint returns pending or started-but-unaccepted dispatches scoped to
the authenticated bridge/device and registered external agent IDs.

Both plugins use `relay-connector.v3` for metadata-only native-agent discovery,
explicit connection consent, runtime-host observations, stable
canonical agent mappings, and complete managed-document manifests. The legacy
`agent-replica.v1` and `relay-connector.v2` exchanges are retained only as
explicit, connection-scoped fallbacks for older Relay backends. They cannot
create a new canonical mapping or send documents before explicit Relay
consent. The compatibility manifest sunsets v1 on 30 September 2026 and v2 on
31 October 2026. The websocket transport remains `bridge.v1`; connector v3 is
not a websocket breaking change.

## Local Verification

From this repo:

```bash
scripts/verify-sanitized.sh
node --test scripts/bridge-acceptance-gate.test.mjs
scripts/bridge-acceptance-gate.mjs
node --test scripts/bridge-release-gate.test.mjs
scripts/bridge-release-gate.mjs
scripts/test-bridge-lifecycle.sh
```

For exact pinned-harness conformance after preparing the harness checkout, run:

```bash
scripts/test-hermes-bridge-against-checkout.sh /path/to/pinned/hermes-agent
scripts/test-openclaw-bridge-against-checkout.sh /path/to/pinned/openclaw
```

The exact pinned-harness commands, machine-readable acceptance records, and the
fail-closed stable release process are documented in
[the release guide](docs/RELEASING.md). The isolated lifecycle contract is
repository evidence only; it does not change any clean-host acceptance row in
the compatibility manifest.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md) for the focused verification workflow and
[SECURITY.md](SECURITY.md) for private vulnerability reporting. The repository
is released under the [MIT License](LICENSE).
