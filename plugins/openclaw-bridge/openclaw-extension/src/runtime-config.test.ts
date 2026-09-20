import assert from "node:assert/strict";
import test from "node:test";
import { readBridgeRuntimeConfig, writeBridgeRuntimeConfig } from "./runtime-config.js";

test("current config is detached and only the Relay channel is written", async () => {
 const saved: any = { channels: { clawchat: { token: "fixture-old" }, other: { enabled: true } }, agents: { entries: { main: {} } } };
 const config = readBridgeRuntimeConfig({ current: () => saved });
 (config as any).channels.clawchat.token = "fixture-new";
 assert.equal(saved.channels.clawchat.token, "fixture-old");
 // A concurrent native agent edit must survive credential refresh.
 saved.agents.entries.newAgent = {};
 await writeBridgeRuntimeConfig({ mutateConfigFile: async ({ mutate, afterWrite }) => {
  mutate(saved);
  assert.equal(afterWrite.mode, "none");
 } }, config);
 assert.equal(saved.channels.clawchat.token, "fixture-new");
 assert.ok(saved.agents.entries.newAgent);
 assert.deepEqual(saved.channels.other, { enabled: true });
});

test("legacy runtime config API remains supported", async () => {
 const config: any = { channels: { clawchat: { enabled: true } } };
 assert.equal(readBridgeRuntimeConfig({ loadConfig: () => config }), config);
 let written: unknown;
 await writeBridgeRuntimeConfig({ writeConfigFile: async value => { written = value; } }, config);
 assert.equal(written, config);
});

test("missing config APIs fail explicitly", async () => {
 assert.throws(() => readBridgeRuntimeConfig({}), /unavailable/);
 await assert.rejects(writeBridgeRuntimeConfig({}, {} as any), /unavailable/);
});
