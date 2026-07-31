import assert from "node:assert/strict";
import { mkdtemp, mkdir, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { safeProvisionFilePath } from "./provisioner.js";

test("provisioning files remain Markdown files inside the native workspace", async () => {
 const workspace = await mkdtemp(join(tmpdir(), "relay-openclaw-provision-"));

 assert.equal(
  safeProvisionFilePath(workspace, "memory/customer-notes.md"),
  join(workspace, "memory", "customer-notes.md"),
 );
 assert.throws(
  () => safeProvisionFilePath(workspace, "../outside.md"),
  /OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED/,
 );
 assert.throws(
  () => safeProvisionFilePath(workspace, "api-token.md"),
  /OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED/,
 );
 assert.throws(
  () => safeProvisionFilePath(workspace, "settings.json"),
  /OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED/,
 );
 assert.throws(
  () => safeProvisionFilePath(workspace, "notes.md"),
  /OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED/,
 );
});

test("provisioning refuses symbolic-link traversal", async () => {
 const root = await mkdtemp(join(tmpdir(), "relay-openclaw-provision-link-"));
 const workspace = join(root, "workspace");
 const outside = join(root, "outside");
 await mkdir(workspace);
 await mkdir(outside);
 await symlink(outside, join(workspace, "memory"));

 assert.throws(
  () => safeProvisionFilePath(workspace, "memory/notes.md"),
  /OPENCLAW_DOCUMENT_PATH_SYMBOLIC_LINK/,
 );
});
