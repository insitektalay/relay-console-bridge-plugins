#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { validateAcceptanceRepository } from "./bridge-acceptance-gate.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIR, "..");
const PRERELEASE_PATTERN = /-(?:alpha|beta|preview|rc)(?:[.\d-]|$)/i;
const SEMVER_PATTERN = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const FULL_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const PUBLIC_REPOSITORY = "insitektalay/relay-console-bridge-plugins";
const BACKEND_ORIGIN_POLICY = "operator-configured-https";
const SUPPORTED_HOSTS = new Set(["linux-systemd", "macos-launchd"]);
const RUNTIME_CONNECTOR_V3 = "relay-connector.v3";
const RUNTIME_CONNECTOR_PROTOCOLS = ["agent-replica.v1", "relay-connector.v2", RUNTIME_CONNECTOR_V3];
const RUNTIME_CONNECTOR_FIXTURES = [
  "connector-v3-inventory-request.json",
  "connector-v3-inventory-response.json",
  "connector-v3-connect-directive.json",
  "connector-v3-provision-request.json",
  "connector-v3-provision-result.json",
];

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function add(errors, condition, message) {
  if (!condition) errors.push(message);
}

function pluginById(manifest, id) {
  return manifest.plugins?.find((plugin) => plugin.id === id);
}

export function validateManifest({
  manifest,
  hermesPlugin,
  openclawPlugin,
  openclawPackage,
  stable = false,
}) {
  const errors = [];
  const stableRequired = stable || manifest.releaseStatus === "stable";
  add(errors, manifest.schemaVersion === "relay.bridge-compatibility.v1", "unexpected compatibility schema");
  add(errors, /^v\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?$/.test(manifest.release ?? ""), "release must be a v-prefixed semantic version");
  add(errors, ["preview", "stable"].includes(manifest.releaseStatus), "releaseStatus must be preview or stable");
  add(errors, manifest.repository === PUBLIC_REPOSITORY, "repository identity is incorrect");
  add(errors, manifest.apiContract === "v1", "API contract must remain v1 for this release lane");
  add(errors, manifest.websocketContract === "bridge.v1", "websocket contract must remain bridge.v1");
  add(errors, manifest.runtimeConnectorContract === RUNTIME_CONNECTOR_V3, "runtime connector contract must be relay-connector.v3");
  add(
    errors,
    JSON.stringify(manifest.supportedRuntimeConnectorProtocols) === JSON.stringify(RUNTIME_CONNECTOR_PROTOCOLS),
    "runtime connector protocols must preserve v1/v2 fallbacks and make v3 current",
  );
  const compatibilityWindow = manifest.runtimeConnectorCompatibilityWindow ?? {};
  add(
    errors,
    compatibilityWindow["agent-replica.v1"]?.mode === "existing-bindings-only" &&
      compatibilityWindow["agent-replica.v1"]?.newConnectionConsent === false &&
      Number.isFinite(Date.parse(compatibilityWindow["agent-replica.v1"]?.sunsetAt ?? "")),
    "agent-replica.v1 must be bounded to existing bindings with a sunset",
  );
  add(
    errors,
    compatibilityWindow["relay-connector.v2"]?.mode === "existing-bindings-and-metadata-discovery" &&
      compatibilityWindow["relay-connector.v2"]?.newConnectionConsent === false &&
      Number.isFinite(Date.parse(compatibilityWindow["relay-connector.v2"]?.sunsetAt ?? "")),
    "relay-connector.v2 must not establish consent and must have a sunset",
  );
  add(
    errors,
    compatibilityWindow[RUNTIME_CONNECTOR_V3]?.mode === "current" &&
      compatibilityWindow[RUNTIME_CONNECTOR_V3]?.newConnectionConsent === true &&
      compatibilityWindow[RUNTIME_CONNECTOR_V3]?.sunsetAt === null,
    "relay-connector.v3 must be the current consent-capable protocol",
  );
  add(errors, manifest.marketplaceContract === "swift-marketplace.v1", "Marketplace contract must remain swift-marketplace.v1");
  add(errors, SEMVER_PATTERN.test(manifest.supportedBackend?.version ?? ""), "supported backend version must be an exact semantic version");
  add(
    errors,
    manifest.supportedBackend?.commit === null || FULL_COMMIT_PATTERN.test(manifest.supportedBackend?.commit ?? ""),
    "supported backend commit must be null for preview or a full SHA",
  );
  add(
    errors,
    manifest.supportedBackend?.originPolicy === BACKEND_ORIGIN_POLICY &&
      manifest.supportedBackend?.origin === undefined,
    "supported backend must require an operator-configured HTTPS origin",
  );
  add(errors, Array.isArray(manifest.plugins) && manifest.plugins.length === 2, "manifest must contain exactly the Hermes and OpenClaw bridges");

  const ids = manifest.plugins?.map((plugin) => plugin.id) ?? [];
  add(errors, new Set(ids).size === ids.length, "plugin IDs must be unique");
  const hermes = pluginById(manifest, "hermes-agent-bridge");
  const openclaw = pluginById(manifest, "openclaw-bridge");
  add(errors, Boolean(hermes), "Hermes bridge compatibility entry is missing");
  add(errors, Boolean(openclaw), "OpenClaw bridge compatibility entry is missing");

  if (hermes) {
    add(errors, hermes.version === hermesPlugin.version, "Hermes manifest and plugin versions differ");
    add(errors, hermes.runtimeDependencies?.python?.aiohttp === "3.14.1", "Hermes bridge aiohttp compatibility pin is missing or incorrect");
    add(errors, hermesPlugin.pythonDependencies?.aiohttp === hermes.runtimeDependencies?.python?.aiohttp, "Hermes manifest and plugin aiohttp pins differ");
    add(errors, hermes.capabilities?.includes("runtime-connector-v3"), "Hermes runtime connector v3 capability is missing");
    add(errors, hermes.capabilities?.includes("runtime-connector-v2-fallback"), "Hermes runtime connector v2 fallback capability is missing");
    add(errors, hermesPlugin.capabilities?.includes("clawchat.runtime_connector.v3"), "Hermes plugin runtime connector v3 capability is missing");
    add(errors, hermesPlugin.capabilities?.includes("clawchat.runtime_connector.v2"), "Hermes plugin runtime connector v2 fallback capability is missing");
  }
  if (openclaw) {
    add(errors, openclaw.version === openclawPlugin.version, "OpenClaw manifest and plugin versions differ");
    add(errors, openclaw.version === openclawPackage.version, "OpenClaw manifest and package versions differ");
    add(errors, openclaw.capabilities?.includes("runtime-connector-v3"), "OpenClaw runtime connector v3 capability is missing");
    add(errors, openclaw.capabilities?.includes("runtime-connector-v2-fallback"), "OpenClaw runtime connector v2 fallback capability is missing");
    add(errors, openclawPlugin.capabilities?.includes("clawchat.runtime_connector.v3"), "OpenClaw plugin runtime connector v3 capability is missing");
    add(errors, openclawPlugin.capabilities?.includes("clawchat.runtime_connector.v2"), "OpenClaw plugin runtime connector v2 fallback capability is missing");
  }

  for (const plugin of manifest.plugins ?? []) {
    add(errors, typeof plugin.supportedHarness?.version === "string", `${plugin.id}: supported harness version is missing`);
    add(errors, FULL_COMMIT_PATTERN.test(plugin.supportedHarness?.commit ?? ""), `${plugin.id}: supported harness commit must be a full SHA`);
    add(errors, Array.isArray(plugin.candidateHostOS) && plugin.candidateHostOS.length > 0, `${plugin.id}: candidate hosts are missing`);
    add(errors, plugin.hostAcceptance?.windows === "unsupported", `${plugin.id}: Windows must remain explicitly unsupported`);
    for (const host of plugin.candidateHostOS ?? []) {
      add(errors, SUPPORTED_HOSTS.has(host), `${plugin.id}: unsupported candidate host ${host}`);
      add(errors, typeof plugin.hostAcceptance?.[host] === "string", `${plugin.id}: acceptance status is missing for ${host}`);
      if (stableRequired) {
        add(errors, plugin.hostAcceptance?.[host] === "passed", `${plugin.id}: clean-host acceptance has not passed for ${host}`);
      }
    }
  }

  add(errors, Array.isArray(manifest.knownGaps), "knownGaps must be an array");
  for (const gap of manifest.knownGaps ?? []) {
    add(errors, typeof gap === "string" && gap.trim().length > 0, "knownGaps entries must be non-empty strings");
  }

  if (stableRequired) {
    add(errors, manifest.releaseStatus === "stable", "stable release gate requires releaseStatus=stable");
    add(errors, !PRERELEASE_PATTERN.test(manifest.release ?? ""), "stable release cannot use a prerelease release version");
    add(errors, FULL_COMMIT_PATTERN.test(manifest.supportedBackend?.commit ?? ""), "stable release requires the exact supported backend commit");
    add(errors, !PRERELEASE_PATTERN.test(manifest.supportedBackend?.version ?? ""), "stable release cannot use a prerelease backend version");
    add(errors, (manifest.knownGaps ?? []).length === 0, "stable release requires knownGaps to be empty");
    for (const plugin of manifest.plugins ?? []) {
      add(errors, !PRERELEASE_PATTERN.test(plugin.version ?? ""), `${plugin.id}: stable release cannot use a prerelease plugin version`);
    }
  }

  return errors;
}

export function validateGitRelease({ release, status, tags, tagType }) {
  const errors = [];
  add(errors, status.trim() === "", "stable release requires a clean git worktree");
  add(errors, tags.split(/\r?\n/).filter(Boolean).includes(release), `HEAD must have the exact ${release} tag`);
  add(errors, tagType === "tag", `the exact ${release} tag must be annotated`);
  return errors;
}

export function validateRepository({ root = REPOSITORY_ROOT, stable = false } = {}) {
  const manifest = readJson(resolve(root, "compatibility-manifest.json"));
  const stableRequired = stable || manifest.releaseStatus === "stable";
  const hermesPlugin = readJson(resolve(root, "plugins/hermes-agent-bridge/plugin.json"));
  const openclawPlugin = readJson(resolve(root, "plugins/openclaw-bridge/plugin.json"));
  const openclawPackage = readJson(resolve(root, "plugins/openclaw-bridge/openclaw-extension/package.json"));
  const errors = validateManifest({ manifest, hermesPlugin, openclawPlugin, openclawPackage, stable: stableRequired });
  const acceptance = validateAcceptanceRepository({ root, stable: stableRequired });
  errors.push(...acceptance.errors.map((error) => `acceptance: ${error}`));
  add(
    errors,
    existsSync(resolve(root, "docs", "releases", `${manifest.release}.md`)),
    `release notes are missing for ${manifest.release}`,
  );
  for (const name of RUNTIME_CONNECTOR_FIXTURES) {
    const path = resolve(root, "contracts", "fixtures", name);
    add(errors, existsSync(path), `runtime connector fixture is missing: ${name}`);
    if (existsSync(path)) {
      try {
        const value = readJson(path);
        add(
          errors,
          value.protocolVersion === RUNTIME_CONNECTOR_V3,
          `runtime connector fixture has the wrong protocol: ${name}`,
        );
      } catch {
        errors.push(`runtime connector fixture is invalid JSON: ${name}`);
      }
    }
  }

  if (stableRequired) {
    const status = execFileSync("git", ["status", "--porcelain"], { cwd: root, encoding: "utf8" });
    const tags = execFileSync("git", ["tag", "--points-at", "HEAD"], { cwd: root, encoding: "utf8" });
    const exactTagAtHead = tags.split(/\r?\n/).filter(Boolean).includes(manifest.release);
    const tagType = exactTagAtHead
      ? execFileSync("git", ["cat-file", "-t", `refs/tags/${manifest.release}`], { cwd: root, encoding: "utf8" }).trim()
      : null;
    errors.push(...validateGitRelease({ release: manifest.release, status, tags, tagType }));
  }
  return { errors, manifest };
}

function main() {
  const unknown = process.argv.slice(2).filter((argument) => argument !== "--stable");
  if (unknown.length > 0) {
    console.error(`unknown argument: ${unknown.join(" ")}`);
    process.exitCode = 2;
    return;
  }
  const stable = process.argv.includes("--stable");
  const { errors, manifest } = validateRepository({ stable });
  if (errors.length > 0) {
    for (const error of errors) console.error(`- ${error}`);
    process.exitCode = 1;
    return;
  }
  const stableRequired = stable || manifest.releaseStatus === "stable";
  console.log(`${stableRequired ? "Stable" : "Declared-status"} bridge release gate passed for ${manifest.release} (${manifest.releaseStatus}).`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
