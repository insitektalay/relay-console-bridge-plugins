import assert from "node:assert/strict";
import test from "node:test";

import {
 buildBridgeDeviceAuthPayload,
 redeemBridgeEnrollment,
 requireSecureRelayApiUrl,
 rotateBridgeDeviceCredential,
} from "./bridge-auth.js";

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
 assert.equal(payload.apiContractVersion, "v1");
 assert.equal(payload.websocketContractVersion, "bridge.v1");
 assert.ok(payload.capabilities.includes("clawchat.runtime_connector.v3"));
 assert.ok(payload.capabilities.includes("clawchat.runtime_connector.v2"));
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
