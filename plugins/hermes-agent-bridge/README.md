# Relay Console Hermes Agent bridge

This preview plugin connects an existing, user-managed Hermes Agent source
installation to Relay Cloud. Relay Console does not own the Hermes installation
or its authentication and lifecycle.

Source: `src/main.py`. Contract tests live in `tests/` in this repository and
are not copied into the user's Hermes checkout.

Use `../../docs/INSTALL.md` for install, enrollment, service, health, logs,
update, rollback, and uninstall commands. Real configuration belongs only at
`~/.hermes/clawchat_bridge/config.json`; `config.example.json` contains no usable
credentials. The bridge requires `aiohttp==3.14.1` in the existing Hermes
environment. The lifecycle script verifies that pin and never installs or
updates the dependency for the user.

Bridge API v2 rotates the device credential during every authentication. The
bridge atomically saves the replacement to its owner-only config before using
the returned HTTP or websocket tokens.

After device authentication, the bridge reads the OpenAI Codex model catalogue
from the connected Hermes installation and publishes that catalogue to Relay
Cloud. Relay's web and mobile model pickers therefore follow the models Hermes
actually makes available instead of maintaining a separate hard-coded list.
