# Bridge Plugin Contract

This document defines the high-level contract expected between ClawChat and
local bridge plugins.

## Bridge Identity

A bridge device has:

- `devicePublicId`
- `deviceToken`
- `workspaceId`
- `externalAgentIds`
- `capabilities`
- `runtimeType` (`hermes` or `openclaw`)
- `hostType` (`macos-launchd` or `linux-systemd`)
- exact bridge/runtime and API/websocket contract versions

The device token is local secret material.

## Enrollment

Expected endpoint:

```text
POST /api/v1/bridge/enroll
```

Request:

```json
{
  "code": "one-time-enrollment-code",
  "deviceLabel": "Hermes bridge / local PC",
  "pluginVersion": "plugin-version",
  "openCoreVersion": "runtime-version",
  "runtimeType": "hermes",
  "hostType": "macos-launchd",
  "apiContractVersion": "v1",
  "websocketContractVersion": "bridge.v1",
  "capabilities": ["clawchat.runtime.hermes"]
}
```

Response includes generated bridge credentials and workspace metadata.

## Device Auth

Expected endpoint:

```text
POST /api/v1/bridge/device/auth
```

Request:

```json
{
  "devicePublicId": "bridge-device-id",
  "deviceToken": "bridge-device-token",
  "pluginVersion": "plugin-version",
  "openCoreVersion": "runtime-version",
  "runtimeType": "hermes",
  "hostType": "macos-launchd",
  "apiContractVersion": "v1",
  "websocketContractVersion": "bridge.v1",
  "capabilities": ["clawchat.runtime.hermes"]
}
```

## Credential Rotation

Expected endpoint:

```text
POST /api/v1/bridge/device/rotate
```

The request uses the current device ID/credential plus the complete compatibility
metadata shown above. A successful response returns the replacement credential
once. The bridge atomically replaces its owner-only local configuration and
never prints the value. Relay increments the device credential generation,
disconnects existing sockets, and rejects HTTP or websocket tokens minted under
the previous generation.

Response:

```json
{
  "tokens": {
    "accessToken": "http-api-token",
    "wsToken": "websocket-token"
  }
}
```

## Websocket Auth

Bridge connects to the ClawChat websocket root derived from `apiUrl`, then sends:

```json
{
  "type": "authenticate",
  "token": "websocket-token",
  "capabilities": ["clawchat.runtime.hermes"]
}
```

ClawChat responds:

```json
{ "type": "authenticated" }
```

Bridge then registers each agent:

```json
{
  "type": "register_hermes_agent",
  "externalAgentId": "social_hermes",
  "capabilities": ["clawchat.runtime.hermes"]
}
```

## Runtime Connector Protocol v3

Bridges advertising `clawchat.runtime_connector.v3` use
`relay-connector.v3` by default. The first exchange reports bounded safe native
agent metadata only. After an administrator explicitly connects a candidate,
later exchanges synchronize the allowlisted managed-document inventory with
Railway after authentication and every ten seconds while connected:

```text
POST /api/v1/bridge/agent-sync/exchange
Authorization: Bearer <http-api-token>
```

The request identifies the runtime (`hermes` or `openclaw`), its software and
protocol version, each stable external agent ID, any previously confirmed
canonical Relay agent ID and binding epoch, the versioned profile, and—only
after connection consent—safe managed documents.
When every configured agent and allowed document was scanned within the hard
limits, the bridge sends `completeManifest: true` and a deterministic SHA-256
manifest hash. A partial or bounded scan must send `completeManifest: false` so
Railway never infers tombstones from an incomplete inventory.

Railway returns canonical agent IDs, profiles, document contents, tombstones,
and server versions. The bridge persists each canonical mapping and supplies it
on later v3 exchanges. This prevents name or external-ID rediscovery from
silently changing execution ownership. Only agents accepted in the exchange
are subsequently registered on the websocket. A bridge writes accepted remote
versions back to the runtime workspace atomically and acknowledges them on its
next exchange.

The first post-consent document exchange imports files that do not yet exist in Railway. If both a
host and Railway already have the same path, Railway wins the first link; later
updates require the last observed server version so concurrent offline edits
are retained as conflicts rather than overwritten. Files are limited to 500 KB,
safe relative paths, and the documented text formats; credentials, hidden
directories, logs, sessions, and symlinks are never synchronized.

### Legacy negotiation

`relay-connector.v2` and `agent-replica.v1` remain temporary compatibility
fallbacks. A bridge tries v3 on every new gateway connection and falls back for
that connection only when Railway explicitly returns
`UNSUPPORTED_AGENT_REPLICA_PROTOCOL` for the preceding request.
Fallback inventory cannot establish a new canonical mapping or upload
documents before explicit Relay consent. v1 is bounded to existing bindings
until 30 September 2026; v2 supports existing bindings and metadata-only
discovery until 31 October 2026. Authentication failures, validation failures,
conflicts, rate limits, and server errors must not cause a downgrade.
