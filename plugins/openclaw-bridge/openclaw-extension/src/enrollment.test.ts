import assert from "node:assert/strict";
import test from "node:test";
import { Readable } from "node:stream";

import {
 applyBridgeEnrollmentToConfig,
 applyBridgeCredentialRotationToConfig,
 readEnrollmentCodeFromStdin,
 registerRelayConsoleCli,
} from "./enrollment.js";

const response = {
 workspace: { id: "workspace-1", name: "Example" },
 device: {
  id: "device-1",
  label: "Office OpenClaw",
  compatibility: {
   level: "compatible" as const,
   operatingMode: "safe" as const,
   enabledCapabilities: ["clawchat.runtime.openclaw"],
  },
 },
 credentials: { devicePublicId: "bdev_public", deviceToken: "device-secret" },
};

test("enrollment writes the default channel account without exposing credentials elsewhere", () => {
 const config = applyBridgeEnrollmentToConfig({ agents: { list: [] } }, {
  apiUrl: "https://relay.example.com/",
  accountId: "default",
  openclawAgentId: "main",
  response,
 });
 const channel = (config.channels as Record<string, Record<string, unknown>>).clawchat;

 assert.equal(channel.apiUrl, "https://relay.example.com");
 assert.equal(channel.workspaceId, "workspace-1");
 assert.equal(channel.devicePublicId, "bdev_public");
 assert.equal(channel.deviceToken, "device-secret");
 assert.equal(channel.openclawAgentId, "main");
 assert.equal(channel.enabled, true);
 assert.equal(channel.compatibilityLevel, "compatible");
 assert.equal(channel.operatingMode, "safe");
 assert.deepEqual(channel.enabledCapabilities, ["clawchat.runtime.openclaw"]);
});

test("enrollment preserves existing accounts and scopes a named account", () => {
 const config = applyBridgeEnrollmentToConfig({
  channels: {
   clawchat: {
    accounts: { existing: { apiUrl: "https://existing.example" } },
   },
  },
 }, {
  apiUrl: "https://relay.example.com",
  accountId: "team",
  response,
 });
 const channel = (config.channels as Record<string, Record<string, unknown>>).clawchat;
 const accounts = channel.accounts as Record<string, Record<string, unknown>>;

 assert.equal(accounts.existing.apiUrl, "https://existing.example");
 assert.equal(accounts.team.deviceToken, "device-secret");
 assert.equal(accounts.team.workspaceId, "workspace-1");
});

test("credential rotation replaces only the selected saved account credential", () => {
 const config = applyBridgeCredentialRotationToConfig({
  channels: {
   clawchat: {
    accounts: {
     existing: { deviceToken: "keep-me" },
     team: { deviceToken: "old-secret", workspaceId: "workspace-1" },
    },
   },
  },
 }, {
  accountId: "team",
  response: {
   credentials: {
    devicePublicId: "bdev_public",
    deviceToken: "replacement-secret",
   },
  },
 });
 const channel = (config.channels as Record<string, Record<string, unknown>>).clawchat;
 const accounts = channel.accounts as Record<string, Record<string, unknown>>;
 assert.equal(accounts.existing.deviceToken, "keep-me");
 assert.equal(accounts.team.deviceToken, "replacement-secret");
 assert.equal(accounts.team.workspaceId, "workspace-1");
});

test("enrollment code is read from standard input and bounded", async () => {
 assert.equal(await readEnrollmentCodeFromStdin(Readable.from(["  one-time-code\n"])), "one-time-code");
 await assert.rejects(
  readEnrollmentCodeFromStdin(Readable.from(["x".repeat(1025)])),
  /too large/,
 );
});

test("OpenClaw registers the Relay Console enrollment command", () => {
 let registrar: ((context: { program: unknown }) => void) | undefined;
 let registrationOptions: { commands?: string[] } | undefined;
 const commandNames: string[] = [];
 let actionRegistered = false;
 let requiredApiUrlRegistered = false;

 const enrollmentCommand = {
  description() { return this; },
  option() { return this; },
  requiredOption(flags: string) {
   if (flags === "--api-url <url>") requiredApiUrlRegistered = true;
   return this;
  },
  action() { actionRegistered = true; return this; },
 };
 const relayCommand = {
  description() { return this; },
  command(name: string) { commandNames.push(name); return enrollmentCommand; },
 };
 const program = {
  command(name: string) { commandNames.push(name); return relayCommand; },
 };
 const api = {
  registerCli(nextRegistrar: typeof registrar, options: typeof registrationOptions) {
   registrar = nextRegistrar;
   registrationOptions = options;
  },
 };

 registerRelayConsoleCli(api as never);
 assert.ok(registrar);
 registrar({ program });
 assert.deepEqual(commandNames, ["relay-console", "enroll", "rotate-credential"]);
 assert.deepEqual(registrationOptions?.commands, ["relay-console"]);
 assert.equal(actionRegistered, true);
 assert.equal(requiredApiUrlRegistered, true);
});
