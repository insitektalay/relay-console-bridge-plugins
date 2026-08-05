import assert from "node:assert/strict";
import { mkdtemp, readdir, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { BridgeCredentialStore } from "./credential-store.js";

test("rotating credentials persist outside watched OpenClaw configuration", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "clawchat-credentials-"));
 t.after(async () => {
  const { rm } = await import("node:fs/promises");
  await rm(root, { recursive: true, force: true });
 });
 const store = new BridgeCredentialStore(root);
 const identity = {
  apiUrl: "https://relay.example.com",
  devicePublicId: "bdev_public",
  configuredCredential: "enrollment-secret",
 };

 await store.save({ ...identity, replacementCredential: "replacement-secret" });

 assert.equal(await store.load(identity), "replacement-secret");
 assert.equal(
  await store.load({ ...identity, configuredCredential: "manually-rotated-secret" }),
  null,
  "a deliberate config credential change must invalidate the sidecar chain",
 );
 const files = (await readdir(root)).filter((file) => file.endsWith(".json"));
 assert.equal(files.length, 1);
 assert.equal((await stat(join(root, files[0]!))).mode & 0o777, 0o600);
});

test("overlapping gateway lifecycles serialize credential rotation across store instances", async (t) => {
 const root = await mkdtemp(join(tmpdir(), "clawchat-credential-lock-"));
 t.after(async () => {
  const { rm } = await import("node:fs/promises");
  await rm(root, { recursive: true, force: true });
 });
 const firstStore = new BridgeCredentialStore(root);
 const secondStore = new BridgeCredentialStore(root);
 const order: string[] = [];
 let releaseFirst!: () => void;
 const firstCanFinish = new Promise<void>((resolve) => { releaseFirst = resolve; });

 const first = firstStore.withLock("https://relay.example.com", "bdev_public", async () => {
  order.push("first-start");
  await firstCanFinish;
  order.push("first-end");
 });
 await new Promise((resolve) => setTimeout(resolve, 20));
 const second = secondStore.withLock("https://relay.example.com", "bdev_public", async () => {
  order.push("second-start");
  order.push("second-end");
 });
 await new Promise((resolve) => setTimeout(resolve, 20));
 assert.deepEqual(order, ["first-start"]);
 releaseFirst();
 await Promise.all([first, second]);
 assert.deepEqual(order, ["first-start", "first-end", "second-start", "second-end"]);
});
