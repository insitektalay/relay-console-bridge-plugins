# Relay Console OpenClaw bridge

This preview OpenClaw channel extension connects an existing, user-managed
OpenClaw gateway to Relay Cloud. Relay Console does not install, configure,
authenticate, start, stop, update, or remove OpenClaw.

The internal extension path and channel ID remain `clawchat` for protocol and
upgrade compatibility. User-facing labels use Relay Console.

Bridge API v2 authentication is serialized across configured accounts. The
extension uses OpenClaw's native config writer to save each replacement device
credential before using the returned HTTP or websocket tokens.

Use `../../docs/INSTALL.md` for installation, secure one-time enrollment, and
operations. Stable release status still depends on the compatibility manifest
and its clean-host and cross-client acceptance evidence.
