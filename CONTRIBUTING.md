# Contributing

Relay Console Bridge Plugins is an early technical preview. Keep changes
bounded to the Hermes bridge, OpenClaw extension, shared contracts, or release
gates, and never commit runtime credentials or machine configuration.

Before opening a pull request, run the pull-request commands in
[`docs/RELEASING.md`](docs/RELEASING.md). Changes to a runtime adapter must also
pass the corresponding exact pinned-harness conformance script.

Do not mark a host or client combination supported without the independently
reviewed acceptance records required by the release gate.
