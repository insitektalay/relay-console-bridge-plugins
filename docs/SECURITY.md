# Relay Console bridge security

Bridge plugins are privileged software on the user's runtime host. They can
submit work to Hermes Agent or OpenClaw and must be treated like a local agent
runtime credential.

## Credential rules

- Enroll each runtime host as a separate device with a one-time code.
- Keep the resulting device token only on that runtime host.
- Store Hermes config and its parent directory with owner-only permissions.
- Keep OpenClaw configuration owner-readable only.
- Never print device tokens, enrollment codes, OAuth tokens, cookies, API keys,
  passwords, private keys, or provider credentials in logs or support bundles.
- Revoke the device in Relay Console when a host is retired, lost, or suspected
  compromised. Uninstalling local bridge files is not a substitute for server
  revocation.
- Rotate a healthy device credential only from that authenticated runtime host.
  Rotation invalidates the old device credential and every previously issued
  HTTP/websocket token; it is not a substitute for revoking a compromised host.

## Transport and scope

- Connect outbound only to an `https://` Relay API and authenticated `wss://`
  websocket endpoint. No inbound runtime-host port is required.
- Scope every device to its Relay user/workspace, runtime type, device identity,
  and registered external agent IDs.
- Reject dispatches for unregistered agents and fail closed on incompatible
  backend, bridge, or runtime versions.
- Keep terminal delivery idempotent and persist only the minimum reconnect and
  duplicate-suppression state.
- Keep Hermes Marketplace tools in a dispatch-local context. Never register
  Relay handlers into the process-global native tool map or allow a Relay tool
  name to shadow a native Hermes tool.
- Local OpenClaw Agent API proxying is limited to approved loopback routes; it
  must never become a general network proxy.

## Files that must never be committed

- `~/.hermes/clawchat_bridge/config.json`
- bridge enrollment codes or device tokens
- runtime or provider `.env` files
- bridge logs, outboxes, dispatch ledgers, or local databases
- cookies, OAuth grants, private keys, or customer data

Run `scripts/verify-sanitized.sh` before every release. A clean scan is necessary
but does not replace code review, dependency review, host acceptance, or device
revocation testing.
