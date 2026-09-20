import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { handleAgentFile } from "./agent-files.js";

test("OpenClaw claims and completes a native file write, then reads that file", async () => {
 const temp = await realpath(await mkdtemp(join(tmpdir(), "relay-file-protocol-")));
 try {
  const root = join(temp, "agent"), stateDir = join(temp, "bridge"), workspaceId = randomUUID();
  await mkdir(root);
  const command: Record<string, any> = { operationId: randomUUID(), requestHash: "a".repeat(64), workspaceId, workspaceGeneration: "27", runtimeType: "openclaw", externalAgentId: "example", action: "create", path: "AGENTS.md", content: "# Instructions", contentHash: createHash("sha256").update("# Instructions").digest("hex"), baseVersion: "absent", expiresAt: "2099-01-01T00:00:00Z" };
  const completions: any[] = [];
  const post = async (path: string, body: Record<string, any>) => {
   if (path.endsWith("/claim")) return { ...command, allowed: true };
   completions.push(body); return { result: body };
  };
  const created = await handleAgentFile({ root, stateDir, workspaceId, command, post });
  assert.equal(created.status, "applied");
  assert.equal(await readFile(join(root, "AGENTS.md"), "utf8"), command.content);
  assert.equal(completions[0].nativeInactive, true);
  assert.equal(completions[0].content, undefined);
  command.operationId = randomUUID(); command.requestHash = "b".repeat(64); command.action = "read";
  delete command.content; delete command.contentHash; delete command.baseVersion;
  assert.equal((await handleAgentFile({ root, stateDir, workspaceId, command, post })).content, "# Instructions");
 } finally { await rm(temp, { recursive: true, force: true }); }
});

test("a changed claim cannot write to the runtime", async () => {
 const temp = await realpath(await mkdtemp(join(tmpdir(), "relay-file-denial-")));
 try {
  const root = join(temp, "agent"), stateDir = join(temp, "bridge"), workspaceId = randomUUID();
  await mkdir(root);
  const command = { operationId: randomUUID(), requestHash: "a".repeat(64), workspaceId, workspaceGeneration: "27", runtimeType: "openclaw", action: "create", path: "AGENTS.md", content: "text", contentHash: createHash("sha256").update("text").digest("hex"), baseVersion: "absent", expiresAt: "2099-01-01T00:00:00Z" };
  await assert.rejects(handleAgentFile({ root, stateDir, workspaceId, command, post: async () => ({ ...command, path: "MEMORY.md", allowed: true }) }), /claim mismatch/);
  await assert.rejects(readFile(join(root, "AGENTS.md")));
 } finally { await rm(temp, { recursive: true, force: true }); }
});

test("concurrent requests cannot recover a live claim as an abandoned operation", async () => {
 const temp = await realpath(await mkdtemp(join(tmpdir(), "relay-file-concurrent-")));
 try {
  const root = join(temp, "agent"), stateDir = join(temp, "bridge"), workspaceId = randomUUID();
  await mkdir(root);
  let release!: () => void, entered!: () => void;
  const waitForRelease = new Promise<void>(resolve => { release = resolve; });
  const firstClaim = new Promise<void>(resolve => { entered = resolve; });
  const commands = ["AGENTS.md", "MEMORY.md"].map((path, index) => ({ operationId: randomUUID(), requestHash: String(index + 1).repeat(64), workspaceId, workspaceGeneration: "27", runtimeType: "openclaw", action: "create", path, content: "text", contentHash: createHash("sha256").update("text").digest("hex"), baseVersion: "absent", expiresAt: "2099-01-01T00:00:00Z" }));
  const start = (index: number) => handleAgentFile({ root, stateDir, workspaceId, command: commands[index], post: async (path, body) => {
   if (path.endsWith("/claim")) {
    if (index === 0) { entered(); await waitForRelease; }
    return { ...commands[index], allowed: true };
   }
   return body;
  } });
  const first = start(0);
  await firstClaim;
  const second = start(1);
  await new Promise(resolve => setTimeout(resolve, 50));
  release();
  const results = await Promise.all([first, second]);
  assert.deepEqual(results.map(result => result.status), ["applied", "applied"]);
 } finally { await rm(temp, { recursive: true, force: true }); }
});
