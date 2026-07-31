# Relay Console bridge install and operations

Release status: **preview**. Do not use this guide as evidence that a host/runtime
combination is production-supported; check `compatibility-manifest.json` first.
The install and update commands stage and validate replacement bridge files
before activation. If service activation fails, the Hermes lifecycle restores
the previous bridge; OpenClaw dependency-install failure leaves the active
extension untouched.

The pinned Hermes release does not expose a native dispatch-scoped tool-registry
API. The bridge supplies a context-local compatibility overlay at runtime so
concurrent Marketplace dispatches cannot overwrite one another. It does not
edit Hermes source files, replace native tools, or change ordinary Hermes
sessions; a descriptor that collides with a native tool is rejected.

Relay Console never installs Hermes Agent or OpenClaw. Install, authenticate,
update, and start your chosen runtime using its official documentation before
installing a bridge.

## How many bridges to install

This repository contains two bridge implementations: one for Hermes and one
for OpenClaw. Install one instance beside each independent runtime installation
that Relay Connect should reach:

- one Hermes bridge process for one Hermes installation, even when that process
  registers several Hermes agent IDs;
- one OpenClaw extension instance for one OpenClaw gateway, even when that
  gateway owns several OpenClaw agents; and
- another instance on another Mac, PC/WSL host, or VPS only when that machine
  runs a separate runtime that should connect to Relay.

Every installed instance enrolls as its own Relay bridge device and must keep a
distinct device credential. Never copy a device config between hosts or share a
device ID between the Hermes and OpenClaw implementations. A rollback directory
is an inactive recovery copy, not another bridge instance.

Relay Console for macOS can dispatch directly to runtimes managed by the Swift
app. It also contains its own optional Relay Connect device transport and a
separate Marketplace tool bridge. Those components are not installations of
the Hermes or OpenClaw plugins from this repository.

## Hermes Agent on macOS or Linux

The current bridge requires a source installation of Hermes Agent with either
`.venv/bin/python` (preferred) or the legacy `venv/bin/python`. It also requires
`aiohttp==3.14.1`, matching the supported Hermes release's own `messaging`
extra. Relay's bridge scripts verify this dependency but never install or
upgrade packages in the user-managed Hermes environment.

1. If your Hermes installation does not already include its `messaging` extra,
   install the exact bridge dependency yourself using that environment's package
   manager. For a standard source checkout:

   ```bash
   cd /path/to/hermes-agent
   .venv/bin/python -m pip install 'aiohttp==3.14.1'
   ```

   This is a user-authorized Hermes-environment change, not an action Relay
   Console performs.

2. Copy the bridge code into the existing Hermes checkout:

   ```bash
   cd /path/to/relay-console-bridge-plugins
   scripts/install-hermes-agent-bridge.sh /path/to/hermes-agent
   ```

3. In Relay Console, create a one-time runtime-device enrollment code. Then
   enroll the runtime without putting the code in shell history:

   ```bash
   cd /path/to/hermes-agent
   read -r -s RELAY_ENROLLMENT_CODE
   .venv/bin/python -m clawchat_bridge.main enroll \
     --api-url https://your-relay-backend.up.railway.app \
     --code "$RELAY_ENROLLMENT_CODE" \
     --device-label "Office Mac Hermes bridge"
   unset RELAY_ENROLLMENT_CODE
   ```

   The bridge discovers the Hermes default profile and named profiles through
   the supported Hermes profile API. `--agent` remains available only for a
   legacy explicit registration during the compatibility window; it is not
   required for native profile discovery. The bridge writes its device credential to
   `~/.hermes/clawchat_bridge/config.json` with owner-only permissions. The
   legacy directory name is retained as a protocol compatibility detail.

4. Install the bridge service. This manages only the bridge process, not Hermes:

   ```bash
   HERMES_HOME=/path/to/hermes-agent \
     scripts/manage-hermes-agent-bridge.sh install
   ```

   If the checkout contains both `.venv` and legacy `venv` directories, set
   `HERMES_PYTHON` to the interpreter used by the existing Hermes/bridge
   service. The lifecycle validates that exact environment and records it in
   the service definition instead of guessing between two installations:

   ```bash
   HERMES_HOME=/path/to/hermes-agent \
   HERMES_PYTHON=/path/to/hermes-agent/venv/bin/python \
     scripts/manage-hermes-agent-bridge.sh update
   ```

5. Operate it:

   ```bash
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh status
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh health
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh logs
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh update
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh rollback
   HERMES_HOME=/path/to/hermes-agent scripts/manage-hermes-agent-bridge.sh uninstall
   ```

   Rotate the per-device credential from the runtime host, then restart the
   bridge service. The replacement is written atomically and is never printed:

   ```bash
   HERMES_HOME=/path/to/hermes-agent \
     scripts/manage-hermes-agent-bridge.sh rotate-credential
   ```

`uninstall` removes the bridge code and its service. It deliberately leaves
Hermes and the local device credential untouched. Revoke the device in Relay
Console; delete the config only when you deliberately want to remove the local
credential.

## OpenClaw on macOS or Linux

The extension is installed into the user's existing OpenClaw home. The script
does not start, stop, configure, update, or uninstall OpenClaw.
Validated rollback files are stored outside OpenClaw's scanned `extensions`
directory under `.relay-console-bridge/clawchat/rollback`; they are recovery
files, never a second loadable plugin. Older sibling `clawchat.rollback`
directories are migrated there before the next lifecycle operation.

```bash
cd /path/to/relay-console-bridge-plugins
scripts/manage-openclaw-bridge.sh install
read -r -s RELAY_ENROLLMENT_CODE
printf '%s\n' "$RELAY_ENROLLMENT_CODE" | openclaw relay-console enroll \
  --api-url https://your-relay-backend.up.railway.app \
  --label "Office OpenClaw bridge"
unset RELAY_ENROLLMENT_CODE
scripts/manage-openclaw-bridge.sh status
scripts/manage-openclaw-bridge.sh health
scripts/manage-openclaw-bridge.sh logs
openclaw relay-console rotate-credential
scripts/manage-openclaw-bridge.sh update
scripts/manage-openclaw-bridge.sh rollback
scripts/manage-openclaw-bridge.sh uninstall
```

The internal channel ID and configuration prefix remain `clawchat` for backward
compatibility, while the user-facing channel name is Relay Console. The command
reads the short-lived code from standard input, redeems it over HTTPS, and uses
OpenClaw's atomic owner-only config writer for the returned per-device
credential. It prints neither the code nor the credential. Restart OpenClaw
using the lifecycle you already use for your runtime.

Enrollment, authentication, and rotation report the normalized host type,
runtime type, bridge version, runtime version, API contract, and websocket
contract. Relay rejects missing or non-matching values before issuing a token.
With bridge API v2, every successful authentication also rotates the device
credential. The replacement is saved with owner-only configuration before any
returned bearer token is used. Ordinary operation therefore needs no manual
rotation command; use the explicit command only during a controlled credential
maintenance operation, then restart the existing bridge lifecycle.
The runtime checkout/package must match `compatibility-manifest.json`; an
unknown version is treated as incompatible rather than guessed.

After authentication, both plugins try `relay-connector.v3` for metadata-only
native-agent discovery, canonical agent mapping, and managed-document
reconciliation. They try `relay-connector.v2`, then `agent-replica.v1`, only
when Relay explicitly reports that the preceding protocol is unsupported.
Authentication, validation, conflict, rate-limit, and server failures never
trigger a downgrade. The next gateway connection tries v3 again. The websocket
contract remains `bridge.v1`. Fallback clients cannot establish a new
canonical mapping or upload documents before an administrator connects the
candidate in Relay. The manifest sunsets v1 on 30 September 2026 and v2 on
31 October 2026.

For a named channel account, add `--account <id>`. For explicit outbound sender
attribution, add `--agent <existing-openclaw-agent-id>`. Never paste an
enrollment code or device credential into chat, an issue, or a support
transcript.

## Network boundary

The Relay API URL must use `https://`; the bridge derives `wss://` for websocket
traffic. `RELAY_CONSOLE_BRIDGE_ALLOW_INSECURE_HTTP=1` exists only for isolated
development tests and must never be set on a customer or production host. Local
OpenClaw Agent API calls may use loopback because they never leave the runtime
host.

## Windows

Windows is unsupported for the launch preview. Do not advertise it until the
installer, service lifecycle, logs, uninstall, and full acceptance journey pass
on a clean Windows host.
