import assert from "node:assert/strict";
import test from "node:test";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

import {
 authenticateBridgeDevice,
 buildBridgeDeviceAuthPayload,
 configureBridgeCredentialPersistence,
 redeemBridgeEnrollment,
 requireSecureRelayApiUrl,
 rotateBridgeDeviceCredential,
} from "./bridge-auth.js";

function configureTestCredentialPersistence(
 loadConfig: () => OpenClawConfig,
 saveOverride?: () => Promise<void>,
) {
 const credentials = new Map<string, string>();
 const restore = configureBridgeCredentialPersistence({
  loadConfig,
  loadCredential: async ({ apiUrl, devicePublicId }) =>
   credentials.get(`${apiUrl}\n${devicePublicId}`) ?? null,
  saveCredential: async ({ apiUrl, devicePublicId, replacementCredential }) => {
   if (saveOverride) await saveOverride();
   credentials.set(`${apiUrl}\n${devicePublicId}`, replacementCredential);
  },
  withCredentialLock: async (_apiUrl, _devicePublicId, operation) => operation(),
 });
 return { credentials, restore };
}

test("Relay API URLs require HTTPS and reject embedded credentials", () => {
 assert.equal(requireSecureRelayApiUrl("https://relay.example.com/"), "https://relay.example.com");
 assert.throws(
  () => requireSecureRelayApiUrl("http://relay.example.com"),
  /requires an https:\/\/ API URL/,
 );
 assert.throws(
  () => requireSecureRelayApiUrl("https://user:secret@relay.example.com"),
  /must not contain credentials/,
 );
});

test("device auth payload includes capabilities and preserves the supplied credential", () => {
 const payload = buildBridgeDeviceAuthPayload({
  devicePublicId: "device-public-id",
  deviceToken: "test-token",
 });
 assert.equal(payload.devicePublicId, "device-public-id");
 assert.equal(payload.deviceToken, "test-token");
 assert.ok(Array.isArray(payload.capabilities));
 assert.equal(payload.runtimeType, "openclaw");
 assert.ok(["macos-launchd", "linux-systemd"].includes(payload.hostType));
 assert.equal(payload.apiContractVersion, "v2");
 assert.equal(payload.websocketContractVersion, "bridge.v1");
 assert.ok(payload.capabilities.includes("clawchat.runtime_connector.v3"));
 assert.ok(payload.capabilities.includes("clawchat.runtime_connector.v2"));
 assert.ok(payload.capabilities.includes("clawchat.bridge.rotating_credentials.v1"));
});

test("API v2 authentication durably saves the replacement before returning tokens", async (t) => {
 const savedConfig = {
  channels: {
   clawchat: {
    apiUrl: "https://relay.example.com",
    workspaceId: "workspace-1",
    devicePublicId: "bdev_public",
    deviceToken: "current-secret",
   },
  },
 };
 let requestCredential = "";
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
  requestCredential = String(body.deviceToken);
  return new Response(JSON.stringify({
   tokens: { accessToken: "access", wsToken: "websocket" },
   credentials: { devicePublicId: "bdev_public", deviceToken: "replacement-secret" },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });
 const persistence = configureTestCredentialPersistence(() => savedConfig);
 const { restore } = persistence;
 t.after(restore);

 const response = await authenticateBridgeDevice({
  apiUrl: "https://relay.example.com",
  devicePublicId: "bdev_public",
  deviceToken: "stale-caller-value",
 });

 assert.equal(requestCredential, "current-secret");
 assert.equal(savedConfig.channels.clawchat.deviceToken, "current-secret");
 assert.equal(
  persistence.credentials.get("https://relay.example.com\nbdev_public"),
  "replacement-secret",
 );
 assert.equal(response.tokens?.accessToken, "access");
});

test("concurrent API v2 authentication preserves the rotating credential chain", async (t) => {
 const savedConfig = {
  channels: {
   clawchat: {
    apiUrl: "https://relay.example.com",
    workspaceId: "workspace-1",
    devicePublicId: "bdev_public",
    deviceToken: "initial-secret",
   },
  },
 };
 const requestCredentials: string[] = [];
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
  requestCredentials.push(String(body.deviceToken));
  const sequence = requestCredentials.length;
  return new Response(JSON.stringify({
   tokens: { accessToken: `access-${sequence}`, wsToken: `websocket-${sequence}` },
   credentials: { devicePublicId: "bdev_public", deviceToken: `replacement-${sequence}` },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });
 const persistence = configureTestCredentialPersistence(() => savedConfig);
 const { restore } = persistence;
 t.after(restore);

 await Promise.all([
  authenticateBridgeDevice({
   apiUrl: "https://relay.example.com",
   devicePublicId: "bdev_public",
   deviceToken: "initial-secret",
  }),
  authenticateBridgeDevice({
   apiUrl: "https://relay.example.com",
   devicePublicId: "bdev_public",
   deviceToken: "initial-secret",
  }),
 ]);

 assert.deepEqual(requestCredentials, ["initial-secret", "replacement-1"]);
 assert.equal(savedConfig.channels.clawchat.deviceToken, "initial-secret");
 assert.equal(
  persistence.credentials.get("https://relay.example.com\nbdev_public"),
  "replacement-2",
 );
});

test("concurrent accounts cannot overwrite each other's replacement credential", async (t) => {
 const savedConfig = {
  channels: {
   clawchat: {
    apiUrl: "https://relay.example.com",
    workspaceId: "workspace-1",
    devicePublicId: "device-a",
    deviceToken: "secret-a",
    accounts: {
     team: {
      apiUrl: "https://relay.example.com",
      workspaceId: "workspace-1",
      devicePublicId: "device-b",
      deviceToken: "secret-b",
     },
    },
   },
  },
 };
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
  const devicePublicId = String(body.devicePublicId);
  return new Response(JSON.stringify({
   tokens: { accessToken: `access-${devicePublicId}` },
   credentials: {
    devicePublicId,
    deviceToken: `replacement-${devicePublicId}`,
   },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });
 const persistence = configureTestCredentialPersistence(() => savedConfig);
 const { restore } = persistence;
 t.after(restore);

 await Promise.all([
  authenticateBridgeDevice({
   apiUrl: "https://relay.example.com",
   devicePublicId: "device-a",
   deviceToken: "secret-a",
  }),
  authenticateBridgeDevice({
   apiUrl: "https://relay.example.com",
   devicePublicId: "device-b",
   deviceToken: "secret-b",
  }),
 ]);

 assert.equal(savedConfig.channels.clawchat.deviceToken, "secret-a");
 assert.equal(savedConfig.channels.clawchat.accounts.team.deviceToken, "secret-b");
 assert.equal(
  persistence.credentials.get("https://relay.example.com\ndevice-a"),
  "replacement-device-a",
 );
 assert.equal(
  persistence.credentials.get("https://relay.example.com\ndevice-b"),
  "replacement-device-b",
 );
});

test("API v2 authentication withholds bearer tokens when persistence fails", async (t) => {
 const savedConfig = {
  channels: {
   clawchat: {
    apiUrl: "https://relay.example.com",
    workspaceId: "workspace-1",
    devicePublicId: "bdev_public",
    deviceToken: "current-secret",
   },
  },
 };
 t.mock.method(globalThis, "fetch", async () => new Response(JSON.stringify({
  tokens: { accessToken: "must-not-be-used" },
  credentials: { devicePublicId: "bdev_public", deviceToken: "replacement-secret" },
 }), { status: 200, headers: { "Content-Type": "application/json" } }));
 const { restore } = configureTestCredentialPersistence(
  () => savedConfig,
  async () => { throw new Error("disk full"); },
 );
 t.after(restore);

 await assert.rejects(
  authenticateBridgeDevice({
   apiUrl: "https://relay.example.com",
   devicePublicId: "bdev_public",
   deviceToken: "current-secret",
  }),
  /durable persistence failed/,
 );
});

test("one-time enrollment uses the canonical bridge endpoint without logging credentials", async (t) => {
 let requestUrl = "";
 let requestBody: Record<string, unknown> = {};
 t.mock.method(globalThis, "fetch", async (input: string | URL | Request, init?: RequestInit) => {
  requestUrl = String(input);
  requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
  return new Response(JSON.stringify({
   workspace: { id: "workspace-1", name: "Example" },
   device: { id: "device-1", label: "Office OpenClaw" },
   credentials: { devicePublicId: "bdev_public", deviceToken: "device-secret" },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 const response = await redeemBridgeEnrollment({
  apiUrl: "https://relay.example.com/",
  code: "one-time-code",
  deviceLabel: "Office OpenClaw",
 });

 assert.equal(requestUrl, "https://relay.example.com/api/v1/bridge/enroll");
 assert.equal(requestBody.code, "one-time-code");
 assert.equal(requestBody.deviceLabel, "Office OpenClaw");
 assert.ok(Array.isArray(requestBody.capabilities));
 assert.equal(response.credentials?.deviceToken, "device-secret");
});

test("credential rotation uses the dedicated endpoint and returns the replacement only to the caller", async (t) => {
 let requestUrl = "";
 let requestBody: Record<string, unknown> = {};
 t.mock.method(globalThis, "fetch", async (input: string | URL | Request, init?: RequestInit) => {
  requestUrl = String(input);
  requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
  return new Response(JSON.stringify({
   credentials: { devicePublicId: "bdev_public", deviceToken: "replacement-secret" },
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 const response = await rotateBridgeDeviceCredential({
  apiUrl: "https://relay.example.com",
  devicePublicId: "bdev_public",
  deviceToken: "current-secret",
 });

 assert.equal(requestUrl, "https://relay.example.com/api/v1/bridge/device/rotate");
 assert.equal(requestBody.devicePublicId, "bdev_public");
 assert.equal(requestBody.deviceToken, "current-secret");
 assert.equal(requestBody.runtimeType, "openclaw");
 assert.equal(response.credentials?.deviceToken, "replacement-secret");
});
