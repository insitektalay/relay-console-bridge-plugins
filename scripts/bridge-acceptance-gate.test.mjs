import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  validateAcceptanceMatrix,
  validateAcceptanceRecord,
  validateAcceptanceRepository,
} from "./bridge-acceptance-gate.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(resolve(root, "compatibility-manifest.json"), "utf8"));

function record(plugin, scope) {
  const cleanHost = scope.kind === "clean-host";
  const resolvedScope = {
    ...scope,
    runtimeLocation: scope.runtimeLocation ?? (cleanHost
      ? scope.hostOS === "macos-launchd" ? "same-mac" : "linux-vps"
      : "second-computer"),
  };
  const recordId = `${plugin.id}-${scope.kind}-${scope.hostOS ?? "clients"}`;
  const journeys = Object.fromEntries((cleanHost
    ? ["install", "enroll", "dispatch", "stream", "finalResponse", "restartReconnect", "backfillNoDuplicate", "rollback", "health", "logs", "revoke", "invalidToken", "uninstallPreservesRuntime"]
    : ["macosDispatch", "webDispatch", "iphoneDispatch", "ipadDispatch", "streamingVisible", "finalResponseConverged", "runtimeOfflineVisible", "restartReconnect", "backfillNoDuplicate", "revokedDeviceBlocked", "invalidTokenBlocked"]
  ).map((name) => [name, "passed"]));
  return {
    schemaVersion: "relay.bridge-acceptance.v1",
    recordId,
    release: manifest.release,
    pluginId: plugin.id,
    pluginVersion: plugin.version,
    harness: { ...plugin.supportedHarness },
    scope: resolvedScope,
    result: "passed",
    executedAt: "2026-07-14T21:00:00.000Z",
    operator: "acceptance-operator",
    reviewedBy: "release-reviewer",
    environment: {
      backendOrigin: "https://relay.example.com",
      backendDeploymentId: "deployment-123",
      runtimeInstalledBeforeRelayBridge: true,
      relayInstalledRuntime: false,
      ...(cleanHost ? { cleanHost: true } : {}),
    },
    ...(cleanHost ? {} : { clients: { macos: "1.0/1", web: "deployment-456", iphone: "1.0/1", ipad: "1.0/1" } }),
    journeys,
    evidence: [{
      path: `acceptance/evidence/${recordId}/redacted.txt`,
      sha256: "a".repeat(64),
      redacted: true,
      containsSecrets: false,
    }],
  };
}

function completeMatrix() {
  return manifest.plugins.flatMap((plugin) => [
    ...plugin.candidateHostOS.map((hostOS) => record(plugin, { kind: "clean-host", hostOS })),
    record(plugin, { kind: "cross-client" }),
  ]);
}

test("the preview repository does not invent clean-host acceptance", () => {
  const result = validateAcceptanceRepository({ root });
  assert.deepEqual(result.errors, []);
  assert.equal(result.recordCount, 0);
});

test("a complete independently reviewed matrix satisfies the stable acceptance contract", () => {
  assert.deepEqual(validateAcceptanceMatrix(completeMatrix(), manifest, { stable: true }), []);
});

test("stable acceptance requires every plugin host and cross-client record", () => {
  const records = completeMatrix();
  records.pop();
  const errors = validateAcceptanceMatrix(records, manifest, { stable: true });
  assert(errors.some((error) => error.includes("cross-client acceptance record is required")));
});

test("records bind Mac, Linux, and cross-client runs to the required runtime locations", () => {
  const mac = record(manifest.plugins[0], { kind: "clean-host", hostOS: "macos-launchd", runtimeLocation: "second-computer" });
  const linux = record(manifest.plugins[0], { kind: "clean-host", hostOS: "linux-systemd", runtimeLocation: "same-mac" });
  const crossClient = record(manifest.plugins[0], { kind: "cross-client", runtimeLocation: "same-mac" });

  assert(validateAcceptanceRecord(mac, manifest).some((error) => error.includes("runtimeLocation same-mac")));
  assert(validateAcceptanceRecord(linux, manifest).some((error) => error.includes("runtimeLocation linux-vps")));
  assert(validateAcceptanceRecord(crossClient, manifest).some((error) => error.includes("runtimeLocation second-computer")));
});

test("declaring the manifest stable automatically requires the complete acceptance matrix", () => {
  const stableManifest = structuredClone(manifest);
  stableManifest.releaseStatus = "stable";
  const errors = validateAcceptanceMatrix([], stableManifest);
  assert(errors.some((error) => error.includes("clean-host acceptance record is required")));
  assert(errors.some((error) => error.includes("cross-client acceptance record is required")));
});

test("records fail closed on version drift, self-review, missing journeys and secrets", () => {
  const candidate = record(manifest.plugins[0], { kind: "clean-host", hostOS: "macos-launchd" });
  candidate.pluginVersion = "wrong";
  candidate.reviewedBy = candidate.operator;
  candidate.journeys.dispatch = "failed";
  candidate.deviceToken = "redacted";
  const errors = validateAcceptanceRecord(candidate, manifest);
  assert(errors.some((error) => error.includes("plugin version")));
  assert(errors.some((error) => error.includes("independent reviewer")));
  assert(errors.some((error) => error.includes("dispatch journey")));
  assert(errors.some((error) => error.includes("forbidden secret-bearing field")));
  assert(errors.some((error) => error.includes("unexpected acceptance field")));
});

test("records reject insecure backends and unsafe evidence paths", () => {
  const candidate = record(manifest.plugins[0], { kind: "cross-client" });
  candidate.environment.backendOrigin = "http://127.0.0.1:3000";
  candidate.evidence[0].path = "../../secret.txt";
  const errors = validateAcceptanceRecord(candidate, manifest);
  assert(errors.some((error) => error.includes("operator-configured HTTPS backend origin")));
  assert(errors.some((error) => error.includes("repository-relative path")));
});

test("records reject evidence outside their own record directory", () => {
  const candidate = record(manifest.plugins[0], { kind: "cross-client" });
  candidate.evidence[0].path = "acceptance/evidence/someone-elses-record/redacted.txt";
  const errors = validateAcceptanceRecord(candidate, manifest);
  assert(errors.some((error) => error.includes("this record's evidence directory")));
});
