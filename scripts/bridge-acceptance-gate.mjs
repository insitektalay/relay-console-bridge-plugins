#!/usr/bin/env node

import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SCRIPT_DIR, "..");
const CLEAN_HOST_JOURNEYS = [
  "install",
  "enroll",
  "dispatch",
  "stream",
  "finalResponse",
  "restartReconnect",
  "backfillNoDuplicate",
  "rollback",
  "health",
  "logs",
  "revoke",
  "invalidToken",
  "uninstallPreservesRuntime",
];
const CROSS_CLIENT_JOURNEYS = [
  "macosDispatch",
  "webDispatch",
  "iphoneDispatch",
  "ipadDispatch",
  "streamingVisible",
  "finalResponseConverged",
  "runtimeOfflineVisible",
  "restartReconnect",
  "backfillNoDuplicate",
  "revokedDeviceBlocked",
  "invalidTokenBlocked",
];
const CLIENT_KEYS = ["macos", "web", "iphone", "ipad"];
const FORBIDDEN_SECRET_KEYS = new Set([
  "accesstoken",
  "apikey",
  "authorization",
  "clientsecret",
  "cookie",
  "devicetoken",
  "password",
  "privatekey",
  "refreshtoken",
  "secret",
  "sessioncookie",
  "token",
]);
const TOP_LEVEL_KEYS = new Set(["schemaVersion", "recordId", "release", "pluginId", "pluginVersion", "harness", "scope", "result", "executedAt", "operator", "reviewedBy", "environment", "clients", "journeys", "evidence"]);
const HARNESS_KEYS = new Set(["version", "commit"]);
const SCOPE_KEYS = new Set(["kind", "hostOS", "runtimeLocation"]);
const ENVIRONMENT_KEYS = new Set(["backendOrigin", "backendDeploymentId", "runtimeInstalledBeforeRelayBridge", "relayInstalledRuntime", "cleanHost"]);
const CLIENT_KEYS_SET = new Set(CLIENT_KEYS);
const EVIDENCE_KEYS = new Set(["path", "sha256", "redacted", "containsSecrets"]);

function add(errors, condition, message) {
  if (!condition) errors.push(message);
}

function pluginById(manifest, id) {
  return manifest.plugins?.find((plugin) => plugin.id === id);
}

function isIsoDate(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value)) && value.includes("T");
}

function isSecureBackendOrigin(value) {
  if (typeof value !== "string") return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      url.pathname === "/" &&
      !url.search &&
      !url.hash;
  } catch {
    return false;
  }
}

function secretKeyPaths(value, path = "$") {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => secretKeyPaths(item, `${path}[${index}]`));
  }
  if (!value || typeof value !== "object") return [];
  const found = [];
  for (const [key, child] of Object.entries(value)) {
    const childPath = `${path}.${key}`;
    const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (FORBIDDEN_SECRET_KEYS.has(normalizedKey)) found.push(childPath);
    found.push(...secretKeyPaths(child, childPath));
  }
  return found;
}

function unexpectedKeys(value, allowed, path) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.keys(value)
    .filter((key) => !allowed.has(key))
    .map((key) => `${path}.${key}`);
}

function evidencePathIsSafe(path) {
  if (typeof path !== "string" || !path.trim() || isAbsolute(path)) return false;
  return !path.split(/[\\/]+/).includes("..");
}

export function validateAcceptanceRecord(record, manifest) {
  const errors = [];
  const plugin = pluginById(manifest, record.pluginId);

  add(errors, record.schemaVersion === "relay.bridge-acceptance.v1", "unexpected acceptance schema");
  add(errors, typeof record.recordId === "string" && /^[a-z0-9][a-z0-9._-]+$/.test(record.recordId), "recordId is invalid");
  add(errors, record.release === manifest.release, "record release does not match the compatibility manifest");
  add(errors, Boolean(plugin), "record pluginId is not present in the compatibility manifest");
  if (plugin) {
    add(errors, record.pluginVersion === plugin.version, "record plugin version does not match the compatibility manifest");
    add(errors, record.harness?.version === plugin.supportedHarness?.version, "record harness version does not match the compatibility manifest");
    add(errors, record.harness?.commit === plugin.supportedHarness?.commit, "record harness commit does not match the compatibility manifest");
  }

  add(errors, ["clean-host", "cross-client"].includes(record.scope?.kind), "scope kind must be clean-host or cross-client");
  add(errors, record.result === "passed", "acceptance result must be passed");
  add(errors, isIsoDate(record.executedAt), "executedAt must be an ISO timestamp");
  add(errors, typeof record.operator === "string" && record.operator.trim().length > 0, "operator is required");
  add(errors, typeof record.reviewedBy === "string" && record.reviewedBy.trim().length > 0, "reviewedBy is required");
  add(errors, record.operator !== record.reviewedBy, "acceptance requires an independent reviewer");
  add(errors, isSecureBackendOrigin(record.environment?.backendOrigin), "acceptance must use an operator-configured HTTPS backend origin without credentials, paths, query parameters, or fragments");
  add(errors, typeof record.environment?.backendDeploymentId === "string" && record.environment.backendDeploymentId.trim().length > 0, "backend deployment ID is required");
  add(errors, record.environment?.runtimeInstalledBeforeRelayBridge === true, "the user-managed runtime must pre-exist the Relay bridge");
  add(errors, record.environment?.relayInstalledRuntime === false, "Relay must not install the runtime during acceptance");
  for (const path of unexpectedKeys(record, TOP_LEVEL_KEYS, "$")) errors.push(`unexpected acceptance field ${path}`);
  for (const path of unexpectedKeys(record.harness, HARNESS_KEYS, "$.harness")) errors.push(`unexpected acceptance field ${path}`);
  for (const path of unexpectedKeys(record.scope, SCOPE_KEYS, "$.scope")) errors.push(`unexpected acceptance field ${path}`);
  for (const path of unexpectedKeys(record.environment, ENVIRONMENT_KEYS, "$.environment")) errors.push(`unexpected acceptance field ${path}`);
  for (const path of unexpectedKeys(record.clients, CLIENT_KEYS_SET, "$.clients")) errors.push(`unexpected acceptance field ${path}`);

  const requiredJourneys = record.scope?.kind === "clean-host" ? CLEAN_HOST_JOURNEYS : CROSS_CLIENT_JOURNEYS;
  for (const journey of requiredJourneys) {
    add(errors, record.journeys?.[journey] === "passed", `${journey} journey has not passed`);
  }
  for (const path of unexpectedKeys(record.journeys, new Set(requiredJourneys), "$.journeys")) errors.push(`unexpected acceptance field ${path}`);

  if (record.scope?.kind === "clean-host") {
    add(errors, plugin?.candidateHostOS?.includes(record.scope?.hostOS), "clean-host OS is not advertised for this plugin");
    add(errors, record.environment?.cleanHost === true, "clean-host acceptance must declare a clean host");
    const expectedRuntimeLocation = record.scope?.hostOS === "macos-launchd" ? "same-mac" :
      record.scope?.hostOS === "linux-systemd" ? "linux-vps" : null;
    add(errors, record.scope?.runtimeLocation === expectedRuntimeLocation, `${record.scope?.hostOS ?? "unknown host"} acceptance must use runtimeLocation ${expectedRuntimeLocation ?? "for a supported host"}`);
  }

  if (record.scope?.kind === "cross-client") {
    add(errors, record.scope?.hostOS == null, "cross-client records must not claim one host OS");
    add(errors, record.scope?.runtimeLocation === "second-computer", "cross-client acceptance must use runtimeLocation second-computer");
    for (const client of CLIENT_KEYS) {
      add(errors, typeof record.clients?.[client] === "string" && record.clients[client].trim().length > 0, `${client} client version/deployment is required`);
    }
  }

  add(errors, Array.isArray(record.evidence) && record.evidence.length > 0, "at least one evidence artifact is required");
  for (const artifact of record.evidence ?? []) {
    add(errors, evidencePathIsSafe(artifact.path), "evidence path must be a repository-relative path without traversal");
    add(errors, artifact.path?.startsWith(`acceptance/evidence/${record.recordId}/`), "evidence must be stored beneath this record's evidence directory");
    add(errors, /^[0-9a-f]{64}$/.test(artifact.sha256 ?? ""), "evidence SHA-256 is invalid");
    add(errors, artifact.redacted === true, "evidence must be reviewed as redacted");
    add(errors, artifact.containsSecrets === false, "evidence must explicitly contain no secrets");
    for (const path of unexpectedKeys(artifact, EVIDENCE_KEYS, "$.evidence[]")) errors.push(`unexpected acceptance field ${path}`);
  }

  for (const path of secretKeyPaths(record)) {
    errors.push(`acceptance record contains forbidden secret-bearing field ${path}`);
  }
  return errors;
}

export function validateAcceptanceMatrix(records, manifest, { stable = false } = {}) {
  const errors = [];
  const stableRequired = stable || manifest.releaseStatus === "stable";
  const ids = records.map((record) => record.recordId);
  add(errors, new Set(ids).size === ids.length, "acceptance record IDs must be unique");

  for (const record of records) {
    for (const error of validateAcceptanceRecord(record, manifest)) {
      errors.push(`${record.recordId ?? "unknown-record"}: ${error}`);
    }
  }

  if (stableRequired) {
    for (const plugin of manifest.plugins ?? []) {
      for (const hostOS of plugin.candidateHostOS ?? []) {
        const matches = records.filter((record) =>
          record.pluginId === plugin.id &&
          record.scope?.kind === "clean-host" &&
          record.scope?.hostOS === hostOS &&
          record.scope?.runtimeLocation === (hostOS === "macos-launchd" ? "same-mac" : "linux-vps")
        );
        add(errors, matches.length === 1, `${plugin.id}: exactly one ${hostOS} clean-host acceptance record is required`);
      }
      const crossClient = records.filter((record) =>
        record.pluginId === plugin.id &&
        record.scope?.kind === "cross-client" &&
        record.scope?.runtimeLocation === "second-computer"
      );
      add(errors, crossClient.length === 1, `${plugin.id}: exactly one cross-client acceptance record is required`);
    }
  }
  return errors;
}

function readCurrentRecords(root, release) {
  const directory = resolve(root, "acceptance", "records", release);
  if (!existsSync(directory)) return [];
  return readdirSync(directory)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => ({
      file: resolve(directory, name),
      record: JSON.parse(readFileSync(resolve(directory, name), "utf8")),
    }));
}

function verifyEvidenceArtifacts(entries, root) {
  const errors = [];
  const realRoot = realpathSync(root);
  for (const { record } of entries) {
    for (const artifact of record.evidence ?? []) {
      if (!evidencePathIsSafe(artifact.path)) continue;
      const absolute = resolve(root, artifact.path);
      const escaped = relative(root, absolute).startsWith("..");
      if (escaped || !existsSync(absolute)) {
        errors.push(`${record.recordId}: evidence artifact is missing: ${artifact.path}`);
        continue;
      }
      const realEvidence = realpathSync(absolute);
      if (relative(realRoot, realEvidence).startsWith("..") || !statSync(realEvidence).isFile()) {
        errors.push(`${record.recordId}: evidence artifact escapes the repository or is not a file: ${artifact.path}`);
        continue;
      }
      const digest = createHash("sha256").update(readFileSync(realEvidence)).digest("hex");
      if (digest !== artifact.sha256) {
        errors.push(`${record.recordId}: evidence digest differs for ${artifact.path}`);
      }
    }
  }
  return errors;
}

export function validateAcceptanceRepository({ root = REPOSITORY_ROOT, stable = false } = {}) {
  const manifest = JSON.parse(readFileSync(resolve(root, "compatibility-manifest.json"), "utf8"));
  const entries = readCurrentRecords(root, manifest.release);
  const errors = validateAcceptanceMatrix(entries.map(({ record }) => record), manifest, { stable });
  errors.push(...verifyEvidenceArtifacts(entries, root));
  return { errors, manifest, recordCount: entries.length };
}

function main() {
  const unknown = process.argv.slice(2).filter((argument) => argument !== "--stable");
  if (unknown.length > 0) {
    console.error(`unknown argument: ${unknown.join(" ")}`);
    process.exitCode = 2;
    return;
  }
  const stable = process.argv.includes("--stable");
  const { errors, manifest, recordCount } = validateAcceptanceRepository({ stable });
  if (errors.length > 0) {
    for (const error of errors) console.error(`- ${error}`);
    process.exitCode = 1;
    return;
  }
  const stableRequired = stable || manifest.releaseStatus === "stable";
  console.log(`${stableRequired ? "Stable" : "Declared-status"} acceptance gate passed for ${manifest.release} with ${recordCount} current-release record(s).`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
