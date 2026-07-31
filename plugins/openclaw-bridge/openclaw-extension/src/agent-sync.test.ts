import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
 AGENT_REPLICA_V1,
 exchangeAgentReplicas,
  RELAY_CONNECTOR_V3,
  RELAY_CONNECTOR_V2,
  runAgentReplicaSyncLoop,
 scanDocuments,
} from "./agent-sync.js";

async function connectorFixture(name: string): Promise<Record<string, any>> {
 return JSON.parse(
  await readFile(
   new URL(`../../../../contracts/fixtures/${name}`, import.meta.url),
   "utf8",
  ),
 ) as Record<string, any>;
}

function context(workspace: string, warnings: string[] = []) {
 return {
  account: {
   accountId: "account-1",
   apiUrl: "https://relay.example.com",
  },
  cfg: {
   agents: {
    list: [{ id: "main", name: "Main", model: { primary: "gpt-5" }, workspace }],
   },
  },
  log: { warn: (message: string) => warnings.push(message) },
 } as never;
}

test("TypeScript validates the shared connector v3 fixtures", async () => {
 const inventory = await connectorFixture("connector-v3-inventory-request.json");
 const response = await connectorFixture("connector-v3-inventory-response.json");
 const connect = await connectorFixture("connector-v3-connect-directive.json");
 const request = await connectorFixture("connector-v3-provision-request.json");
 const result = await connectorFixture("connector-v3-provision-result.json");

 assert.equal(inventory.protocolVersion, RELAY_CONNECTOR_V3);
 assert.equal(inventory.agents[0].documents.length, 0);
 assert.equal(response.discoveries[0].directive, "metadata_only");
 assert.equal(response.discoveries[0].documentSync, false);
 assert.equal(connect.directive, "connect");
 assert.equal(connect.documentConsentVersion, 1);
 for (const key of ["commandId", "jobId", "runtimeHostId", "idempotencyKey"]) {
  assert.equal(result[key], request[key]);
 }
 assert.equal(result.externalAgentId, request.payload.slug);
});

test("connector v2 sends a complete manifest and persists canonical identity", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-v2-"));
 const stateRoot = join(root, "state");
 const workspace = join(root, "workspace");
 await mkdir(join(workspace, "memory"), { recursive: true });
 await writeFile(join(workspace, "SOUL.md"), "Be useful.\n");
 await writeFile(join(workspace, "memory", "today.md"), "Remember this.\n");
 await writeFile(join(workspace, "api-token.md"), "must not sync\n");
 await writeFile(join(workspace, "notes.md"), "not part of the native document contract\n");
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = stateRoot;
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });

 const requests: Array<Record<string, unknown>> = [];
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
  requests.push(request);
  return new Response(JSON.stringify({
   protocolVersion: RELAY_CONNECTOR_V2,
   agents: [{
    externalId: "main",
    canonicalAgentId: "agt-canonical-1",
    profileServerVersion: "4",
    documents: [],
   }],
   conflicts: [],
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 await exchangeAgentReplicas(context(workspace), "access-token", undefined, RELAY_CONNECTOR_V2);
 await exchangeAgentReplicas(context(workspace), "access-token", undefined, RELAY_CONNECTOR_V2);

 assert.equal(requests[0].protocolVersion, RELAY_CONNECTOR_V2);
 assert.equal(requests[0].completeManifest, true);
 assert.match(String(requests[0].manifestHash), /^[0-9a-f]{64}$/);
 assert.deepEqual((requests[0].host as { protocolVersion: string }).protocolVersion, "2");
 const firstAgent = (requests[0].agents as Array<Record<string, unknown>>)[0];
 assert.equal(firstAgent.canonicalAgentId, undefined);
 assert.deepEqual(
  (firstAgent.documents as Array<{ filename: string }>).map((document) => document.filename).sort(),
  ["SOUL.md", "today.md"],
 );
 const secondAgent = (requests[1].agents as Array<Record<string, unknown>>)[0];
 assert.equal(secondAgent.canonicalAgentId, "agt-canonical-1");

 const stateFiles = await readdir(join(stateRoot, "clawchat"));
 const state = JSON.parse(await readFile(join(stateRoot, "clawchat", stateFiles[0]), "utf8")) as {
  version: number;
  profiles: Record<string, { canonicalAgentId?: string }>;
 };
 assert.equal(state.version, 2);
 assert.equal(state.profiles.main.canonicalAgentId, "agt-canonical-1");
});

test("the scanner accepts its exact document limit and ignores unrelated root files", async () => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-scan-limit-"));
 await writeFile(join(root, "SOUL.md"), "Native instructions.\n");
 await writeFile(join(root, "notes.md"), "not allowlisted\n");

 await expectScan(root, 1, ["SOUL.md"], true);
 await writeFile(join(root, "MEMORY.md"), "A second allowlisted document.\n");
 await expectScan(root, 1, ["MEMORY.md"], false);
});

test("a failed workspace scan is never reported as a complete manifest", async () => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-scan-failure-"));
 const missing = join(root, "missing");
 const scan = await scanDocuments(missing, 2_000);
 assert.deepEqual(scan.documents, []);
 assert.equal(scan.complete, false);
});

test("an incomplete scan never turns previously synchronized documents into tombstones", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-incomplete-tombstone-"));
 const workspace = join(root, "workspace");
 await mkdir(workspace);
 await writeFile(join(workspace, "SOUL.md"), "Native instructions.\n");
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = join(root, "state");
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });
 const requests: Array<Record<string, any>> = [];
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const request = JSON.parse(String(init?.body)) as Record<string, any>;
  requests.push(request);
  return new Response(JSON.stringify({
   protocolVersion: RELAY_CONNECTOR_V2,
   agents: [{
    externalId: "main",
    canonicalAgentId: "agent-1",
    profileServerVersion: "1",
    documents: [{
     objectId: "document-1",
     folder: "",
     filename: "SOUL.md",
     content: "Native instructions.\n",
     contentHash: "server-hash",
     serverVersion: "1",
     deleted: false,
    }],
   }],
   conflicts: [],
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 await exchangeAgentReplicas(
  context(workspace),
  "access-token",
  undefined,
  RELAY_CONNECTOR_V2,
 );
 await rm(workspace, { recursive: true, force: true });
 await exchangeAgentReplicas(
  context(workspace),
  "access-token",
  undefined,
  RELAY_CONNECTOR_V2,
 );

 assert.equal(requests[1].completeManifest, false);
 assert.deepEqual(requests[1].agents[0].documents, []);
 await assert.rejects(readFile(join(workspace, "SOUL.md"), "utf8"));
});

async function expectScan(
 root: string,
 limit: number,
 filenames: string[],
 complete: boolean,
) {
 const scan = await scanDocuments(root, limit);
 assert.deepEqual(scan.documents.map((document) => document.filename), filenames);
 assert.equal(scan.complete, complete);
}

test("connector v3 falls back one version at a time only on an explicit protocol rejection", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-fallback-"));
 const workspace = join(root, "workspace");
 await mkdir(workspace, { recursive: true });
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = join(root, "state");
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });
 const warnings: string[] = [];
 const protocols: string[] = [];
 const controller = new AbortController();
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const request = JSON.parse(String(init?.body)) as { protocolVersion: string };
  protocols.push(request.protocolVersion);
  if (
   request.protocolVersion === RELAY_CONNECTOR_V3 ||
   request.protocolVersion === RELAY_CONNECTOR_V2
  ) {
   return new Response("UNSUPPORTED_AGENT_REPLICA_PROTOCOL", { status: 400 });
  }
  return new Response(JSON.stringify({
   protocolVersion: AGENT_REPLICA_V1,
   agents: [{ externalId: "main", profileServerVersion: "1", documents: [] }],
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 await runAgentReplicaSyncLoop({
  ctx: context(workspace, warnings),
  accessToken: "access-token",
  signal: controller.signal,
  onSynchronized: () => controller.abort(),
 });

 assert.deepEqual(protocols, [
  RELAY_CONNECTOR_V3,
  RELAY_CONNECTOR_V2,
  AGENT_REPLICA_V1,
 ]);
 assert.ok(warnings.some((message) => message.includes("using relay-connector.v2")));
 assert.ok(warnings.some((message) => message.includes("using agent-replica.v1")));
});

test("authentication failures never trigger a protocol downgrade", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-auth-"));
 const workspace = join(root, "workspace");
 await mkdir(workspace, { recursive: true });
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = join(root, "state");
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });
 const protocols: string[] = [];
 const controller = new AbortController();
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  protocols.push((JSON.parse(String(init?.body)) as { protocolVersion: string }).protocolVersion);
  controller.abort();
  return new Response("unauthorized", { status: 401 });
 });

 await runAgentReplicaSyncLoop({
  ctx: context(workspace),
  accessToken: "bad-token",
  signal: controller.signal,
 });

 assert.deepEqual(protocols, [RELAY_CONNECTOR_V3]);
});

test("connector v3 does not read documents until Relay returns a connected canonical agent", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-v3-"));
 const stateRoot = join(root, "state");
 const workspace = join(root, "workspace");
 await mkdir(workspace, { recursive: true });
 await writeFile(join(workspace, "SOUL.md"), "Connected instructions.\n");
 await writeFile(join(workspace, "api-token.md"), "must never sync\n");
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = stateRoot;
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });

 const requests: Array<Record<string, unknown>> = [];
 t.mock.method(globalThis, "fetch", async (_input: string | URL | Request, init?: RequestInit) => {
  const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
  requests.push(request);
  const exchangeNumber = requests.length;
  if (exchangeNumber === 1) {
   return new Response(JSON.stringify({
    protocolVersion: RELAY_CONNECTOR_V3,
    agents: [],
    discoveries: [{
     externalId: "main",
     observationId: "observation-1",
     canonicalAgentId: null,
     directive: "metadata_only",
     connectionState: "discovered",
     documentSync: false,
    }],
    conflicts: [],
   }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  return new Response(JSON.stringify({
   protocolVersion: RELAY_CONNECTOR_V3,
   agents: [{
   externalId: "main",
   canonicalAgentId: "agent-1",
    bindingEpoch: "4",
    profileServerVersion: "1",
    documents: [],
   }],
   discoveries: [],
   conflicts: [],
  }), { status: 200, headers: { "Content-Type": "application/json" } });
 });

 await exchangeAgentReplicas(context(workspace), "access-token");
 await exchangeAgentReplicas(context(workspace), "access-token");
 await exchangeAgentReplicas(context(workspace), "access-token");

 const firstAgent = (requests[0].agents as Array<Record<string, unknown>>)[0];
 const secondAgent = (requests[1].agents as Array<Record<string, unknown>>)[0];
 const thirdAgent = (requests[2].agents as Array<Record<string, unknown>>)[0];
 assert.deepEqual(firstAgent.documents, []);
 assert.deepEqual(secondAgent.documents, []);
 assert.equal(secondAgent.bindingEpoch, undefined);
 assert.equal(thirdAgent.bindingEpoch, "4");
 assert.deepEqual(
  (thirdAgent.documents as Array<{ filename: string }>).map((document) => document.filename),
  ["SOUL.md"],
 );
 assert.equal(requests[0].completeInventory, true);
 assert.equal(requests[0].inventoryGeneration !== undefined, true);
 assert.equal(
  (requests[0].host as { protocolVersion: string }).protocolVersion,
  "3",
 );
});

test("connector v3 refuses a Railway document write through a workspace symlink", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-symlink-"));
 const stateRoot = join(root, "state");
 const workspace = join(root, "workspace");
 const outside = join(root, "outside");
 await mkdir(workspace, { recursive: true });
 await mkdir(outside, { recursive: true });
 await symlink(outside, join(workspace, "memory"), "dir");
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = stateRoot;
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });

 t.mock.method(globalThis, "fetch", async () =>
  new Response(JSON.stringify({
   protocolVersion: RELAY_CONNECTOR_V3,
   agents: [{
    externalId: "main",
    canonicalAgentId: "agent-1",
    profileServerVersion: "1",
    documents: [{
     objectId: "document-1",
     folder: "memory",
     filename: "outside.md",
     content: "must not escape\n",
     contentHash: "remote-hash",
     serverVersion: "1",
     deleted: false,
    }],
   }],
   discoveries: [],
   conflicts: [],
  }), { status: 200, headers: { "Content-Type": "application/json" } }),
 );

 await assert.rejects(
  exchangeAgentReplicas(context(workspace), "access-token"),
  /symbolic link/,
 );
 await assert.rejects(readFile(join(outside, "outside.md"), "utf8"));
});

test("connector refuses non-allowlisted Railway document writes", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-connector-allowlist-"));
 const workspace = join(root, "workspace");
 const previousStateDir = process.env.OPENCLAW_STATE_DIR;
 process.env.OPENCLAW_STATE_DIR = join(root, "state");
 await mkdir(workspace, { recursive: true });
 t.after(() => {
  if (previousStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
  else process.env.OPENCLAW_STATE_DIR = previousStateDir;
 });

 t.mock.method(globalThis, "fetch", async () =>
  new Response(JSON.stringify({
   protocolVersion: RELAY_CONNECTOR_V3,
   agents: [{
    externalId: "main",
    canonicalAgentId: "agent-1",
    profileServerVersion: "1",
    documents: [{
     objectId: "document-1",
     folder: "",
     filename: "notes.md",
     content: "must not be written\n",
     contentHash: "remote-hash",
     serverVersion: "1",
     deleted: false,
    }],
   }],
   discoveries: [],
   conflicts: [],
  }), { status: 200, headers: { "Content-Type": "application/json" } }),
 );

 await assert.rejects(
  exchangeAgentReplicas(context(workspace), "access-token"),
  /not allowlisted/,
 );
 await assert.rejects(readFile(join(workspace, "notes.md"), "utf8"));
});
