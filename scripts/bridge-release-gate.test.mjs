import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { validateGitRelease, validateManifest, validateRepository } from "./bridge-release-gate.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const readJson = (path) => JSON.parse(readFileSync(resolve(root, path), "utf8"));

function currentInputs() {
  return {
    manifest: readJson("compatibility-manifest.json"),
    hermesPlugin: readJson("plugins/hermes-agent-bridge/plugin.json"),
    openclawPlugin: readJson("plugins/openclaw-bridge/plugin.json"),
    openclawPackage: readJson("plugins/openclaw-bridge/openclaw-extension/package.json"),
  };
}

test("the current preview declaration is internally consistent", () => {
  assert.deepEqual(validateRepository({ root }).errors, []);
});

test("the stable gate rejects preview versions, gaps, and missing host acceptance", () => {
  const errors = validateManifest({ ...currentInputs(), stable: true });
  assert(errors.some((error) => error.includes("releaseStatus=stable")));
  assert(errors.some((error) => error.includes("knownGaps")));
  assert(errors.some((error) => error.includes("clean-host acceptance")));
  assert(errors.some((error) => error.includes("prerelease plugin version")));
  assert(errors.some((error) => error.includes("exact supported backend commit")));
});

test("declaring a manifest stable automatically activates stable content checks", () => {
  const inputs = structuredClone(currentInputs());
  inputs.manifest.releaseStatus = "stable";
  const errors = validateManifest(inputs);
  assert(errors.some((error) => error.includes("knownGaps")));
  assert(errors.some((error) => error.includes("clean-host acceptance")));
  assert(errors.some((error) => error.includes("exact supported backend commit")));
});

test("ordinary repository validation cannot bypass acceptance, clean-tree, or tag gates after stable relabelling", () => {
  const temporaryRoot = mkdtempSync(join(tmpdir(), "relay-bridge-stable-gate-"));
  try {
    const inputs = currentInputs();
    const files = new Map([
      ["compatibility-manifest.json", JSON.stringify(inputs.manifest, null, 2)],
      ["plugins/hermes-agent-bridge/plugin.json", JSON.stringify(inputs.hermesPlugin, null, 2)],
      ["plugins/openclaw-bridge/plugin.json", JSON.stringify(inputs.openclawPlugin, null, 2)],
      ["plugins/openclaw-bridge/openclaw-extension/package.json", JSON.stringify(inputs.openclawPackage, null, 2)],
      [`docs/releases/${inputs.manifest.release}.md`, "preview release notes\n"],
    ]);
    for (const [relativePath, source] of files) {
      const path = resolve(temporaryRoot, relativePath);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, `${source}\n`);
    }
    execFileSync("git", ["init", "--quiet"], { cwd: temporaryRoot });
    execFileSync("git", ["config", "user.email", "release-gate@example.invalid"], { cwd: temporaryRoot });
    execFileSync("git", ["config", "user.name", "Release Gate"], { cwd: temporaryRoot });
    execFileSync("git", ["add", "."], { cwd: temporaryRoot });
    execFileSync("git", ["commit", "--quiet", "-m", "preview fixture"], { cwd: temporaryRoot });

    const relabelled = structuredClone(inputs.manifest);
    relabelled.releaseStatus = "stable";
    writeFileSync(
      resolve(temporaryRoot, "compatibility-manifest.json"),
      `${JSON.stringify(relabelled, null, 2)}\n`,
    );

    const errors = validateRepository({ root: temporaryRoot }).errors;
    assert(errors.some((error) => error.includes("acceptance record is required")));
    assert(errors.some((error) => error.includes("clean git worktree")));
    assert(errors.some((error) => error.includes(`exact ${inputs.manifest.release} tag`)));
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
});

test("a fully accepted stable manifest passes the content gate", () => {
  const inputs = structuredClone(currentInputs());
  inputs.manifest.release = "v0.2.0";
  inputs.manifest.releaseStatus = "stable";
  inputs.manifest.supportedBackend.commit = "a".repeat(40);
  inputs.manifest.knownGaps = [];
  inputs.manifest.plugins[0].version = "0.2.0";
  inputs.manifest.plugins[1].version = "2026.7.12";
  for (const plugin of inputs.manifest.plugins) {
    for (const host of plugin.candidateHostOS) plugin.hostAcceptance[host] = "passed";
  }
  inputs.hermesPlugin.version = "0.2.0";
  inputs.openclawPlugin.version = "2026.7.12";
  inputs.openclawPackage.version = "2026.7.12";
  assert.deepEqual(validateManifest({ ...inputs, stable: true }), []);
});

test("version drift and accidental Windows advertising fail closed", () => {
  const inputs = structuredClone(currentInputs());
  inputs.openclawPackage.version = "different";
  inputs.manifest.plugins[0].candidateHostOS.push("windows");
  const errors = validateManifest(inputs);
  assert(errors.some((error) => error.includes("package versions differ")));
  assert(errors.some((error) => error.includes("unsupported candidate host windows")));
});

test("runtime connector v3 declaration and plugin capabilities fail closed on drift", () => {
  const inputs = structuredClone(currentInputs());
  inputs.manifest.runtimeConnectorContract = "agent-replica.v1";
  inputs.manifest.supportedRuntimeConnectorProtocols = ["agent-replica.v1"];
  inputs.hermesPlugin.capabilities = inputs.hermesPlugin.capabilities.filter((value) => value !== "clawchat.runtime_connector.v3");
  inputs.openclawPlugin.capabilities = inputs.openclawPlugin.capabilities.filter((value) => value !== "clawchat.runtime_connector.v3");
  const errors = validateManifest(inputs);
  assert(errors.some((error) => error.includes("must be relay-connector.v3")));
  assert(errors.some((error) => error.includes("preserve v1/v2 fallbacks")));
  assert(errors.some((error) => error.includes("Hermes plugin runtime connector v3 capability")));
  assert(errors.some((error) => error.includes("OpenClaw plugin runtime connector v3 capability")));
});

test("unsupported backend identity fails closed", () => {
  const inputs = structuredClone(currentInputs());
  inputs.manifest.supportedBackend.version = "latest";
  inputs.manifest.supportedBackend.originPolicy = "fixed-private-origin";
  inputs.manifest.supportedBackend.origin = "http://127.0.0.1:3000";
  inputs.manifest.supportedBackend.commit = "short";
  const errors = validateManifest(inputs);
  assert(errors.some((error) => error.includes("exact semantic version")));
  assert(errors.some((error) => error.includes("operator-configured HTTPS origin")));
  assert(errors.some((error) => error.includes("full SHA")));
});

test("Hermes runtime dependency drift fails closed", () => {
  const inputs = structuredClone(currentInputs());
  inputs.hermesPlugin.pythonDependencies.aiohttp = "3.13.0";
  const errors = validateManifest(inputs);
  assert(errors.some((error) => error.includes("aiohttp pins differ")));

  inputs.manifest.plugins[0].runtimeDependencies.python.aiohttp = "latest";
  const unpinnedErrors = validateManifest(inputs);
  assert(unpinnedErrors.some((error) => error.includes("compatibility pin is missing or incorrect")));
});

test("stable git evidence requires a clean exact-tagged HEAD", () => {
  assert.deepEqual(validateGitRelease({ release: "v1.0.0", status: "", tags: "v1.0.0\n", tagType: "tag" }), []);
  const errors = validateGitRelease({ release: "v1.0.0", status: " M file\n", tags: "v0.9.0\n", tagType: null });
  assert.equal(errors.length, 3);
  const lightweight = validateGitRelease({ release: "v1.0.0", status: "", tags: "v1.0.0\n", tagType: "commit" });
  assert(lightweight.some((error) => error.includes("must be annotated")));
});
