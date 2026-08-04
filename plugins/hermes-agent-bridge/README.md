# Relay Console Hermes Agent bridge

This preview plugin connects an existing, user-managed Hermes Agent source
installation to Relay Cloud. Relay Console does not own the Hermes installation
or its authentication and lifecycle.

Source: `src/main.py`. Contract tests live in `tests/` in this repository and
are not copied into the user's Hermes checkout.

Use `../../docs/INSTALL.md` for install, enrollment, service, health, logs,
update, rollback, and uninstall commands. Real configuration belongs only at
`~/.hermes/clawchat_bridge/config.json`; `config.example.json` contains no usable
credentials. The lifecycle creates a bridge-owned Python environment, installs
`aiohttp>=3.10,<4` there, and links the selected Hermes checkout and environment
for runtime APIs. It does not install or upgrade packages in the existing Hermes
environment. The lifecycle verifies its private dependency range and never
updates dependencies in the user's Hermes environment.

Bridge API v2 rotates the device credential during every authentication. The
bridge atomically saves the replacement to its owner-only config before using
the returned HTTP or websocket tokens.

After device authentication, the bridge reads the OpenAI Codex model catalogue
from the connected Hermes installation and publishes that catalogue to Relay
Cloud. Relay's web and mobile model pickers therefore follow the models Hermes
actually makes available instead of maintaining a separate hard-coded list.
