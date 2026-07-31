import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DispatchJournal } from "./dispatch-journal.js";

test("claims a dispatch once and persists completion across restart", async () => {
 const directory = await mkdtemp(join(tmpdir(), "clawchat-journal-"));
 const path = join(directory, "journal.json");
 try {
  const first = new DispatchJournal(path);
  assert.equal(await first.claim("dispatch-1"), true);
  assert.equal(await first.claim("dispatch-1"), false);
  await first.complete("dispatch-1");
  const restarted = new DispatchJournal(path);
  assert.equal(await restarted.claim("dispatch-1"), false);
 } finally { await rm(directory, { recursive: true, force: true }); }
});
