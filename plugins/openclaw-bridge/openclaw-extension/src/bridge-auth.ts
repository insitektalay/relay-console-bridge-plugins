import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { platform } from "node:os";
import { fileURLToPath } from "node:url";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";
import { listClawChatAccountIds, resolveClawChatAccount } from "./types.js";

export const STRUCTURED_PROMPT_CAPABILITY = "claude.cli.structured_prompt";
export const RUNTIME_STRUCTURED_JOBS_CAPABILITY = "clawchat.runtime.structured_jobs";
export const RUNTIME_STRUCTURED_OUTPUT_CAPABILITY = "clawchat.runtime.structured_output";
export const ATTACHMENTS_LOCAL_MEDIA_CAPABILITY = "clawchat.attachments.local_media";
export const LIBRARY_CONTROL_CAPABILITY = "clawchat.library.control";
export const AGENT_WORKSPACE_CONTROL_CAPABILITY = "clawchat.agent_workspace.control";
export const AGENT_REPLICA_SYNC_CAPABILITY = "clawchat.agent_replica_sync";
export const RUNTIME_CONNECTOR_V2_CAPABILITY = "clawchat.runtime_connector.v2";
export const RUNTIME_CONNECTOR_V3_CAPABILITY = "clawchat.runtime_connector.v3";
export const ROTATING_CREDENTIALS_CAPABILITY = "clawchat.bridge.rotating_credentials.v1";
export const MARKETPLACE_LOCAL_REPO_DOCS_READ_CAPABILITY = "marketplaceLocalRepoDocsRead";

const DEFAULT_CAPABILITIES = [
 "clawchat.runtime.openclaw",
 STRUCTURED_PROMPT_CAPABILITY,
 RUNTIME_STRUCTURED_JOBS_CAPABILITY,
 RUNTIME_STRUCTURED_OUTPUT_CAPABILITY,
 ATTACHMENTS_LOCAL_MEDIA_CAPABILITY,
 LIBRARY_CONTROL_CAPABILITY,
 AGENT_WORKSPACE_CONTROL_CAPABILITY,
 AGENT_REPLICA_SYNC_CAPABILITY,
 RUNTIME_CONNECTOR_V3_CAPABILITY,
 RUNTIME_CONNECTOR_V2_CAPABILITY,
 ROTATING_CREDENTIALS_CAPABILITY,
 MARKETPLACE_LOCAL_REPO_DOCS_READ_CAPABILITY,
] as const;

export type BridgeAuthResponse = {
 tokens?: {
  accessToken?: string;
  wsToken?: string;
 };
 wsToken?: string;
 token?: string;
 accessToken?: string;
 credentials?: {
  devicePublicId?: string;
  deviceToken?: string;
 };
};

type BridgeCredentialPersistence = {
 loadConfig: () => OpenClawConfig;
 loadCredential: (input: {
  apiUrl: string;
  devicePublicId: string;
  configuredCredential: string;
 }) => Promise<string | null>;
 saveCredential: (input: {
  apiUrl: string;
  devicePublicId: string;
  configuredCredential: string;
  replacementCredential: string;
 }) => Promise<void>;
 withCredentialLock: <T>(
  apiUrl: string,
  devicePublicId: string,
  operation: () => Promise<T>,
 ) => Promise<T>;
};

let credentialPersistence: BridgeCredentialPersistence | null = null;
let authenticationTail: Promise<unknown> = Promise.resolve();
const volatileCredentials = new Map<string, string>();

export function configureBridgeCredentialPersistence(
 persistence: BridgeCredentialPersistence,
): () => void {
 credentialPersistence = persistence;
 return () => {
  if (credentialPersistence === persistence) credentialPersistence = null;
  authenticationTail = Promise.resolve();
  volatileCredentials.clear();
 };
}

export type BridgeEnrollmentResponse = {
 workspace?: {
  id?: string;
  name?: string;
 };
 device?: {
  id?: string;
  devicePublicId?: string;
  label?: string;
  compatibility?: {
   level?: "verified" | "compatible" | "unsupported";
   operatingMode?: "full" | "safe" | "blocked";
   enabledCapabilities?: string[];
   disabledCapabilities?: string[];
   warnings?: string[];
  };
 };
 credentials?: {
  devicePublicId?: string;
  deviceToken?: string;
 };
};

function loadPluginVersion(): string | null {
 try {
  const packageJsonUrl = new URL("../package.json", import.meta.url);
  const raw = readFileSync(fileURLToPath(packageJsonUrl), "utf8");
  const parsed = JSON.parse(raw) as { version?: string };
  return typeof parsed.version === "string" && parsed.version.trim()
   ? parsed.version.trim()
   : null;
 } catch {
  return null;
 }
}

const PLUGIN_VERSION = loadPluginVersion();

function loadOpenClawVersion(): string | null {
 const configured = process.env.OPENCLAW_SERVICE_VERSION?.trim();
 if (configured) return configured;
 try {
  const output = execFileSync("openclaw", ["--version"], {
   encoding: "utf8",
   timeout: 2_000,
   stdio: ["ignore", "pipe", "ignore"],
  }).trim();
  return output.match(/v?\d{4}\.\d+\.\d+(?:[-+.][A-Za-z0-9.-]+)?/)?.[0] ?? null;
 } catch {
  return null;
 }
}

function bridgeHostType(): string {
 if (platform() === "darwin") return "macos-launchd";
 if (platform() === "linux") return "linux-systemd";
 return `unsupported-${platform()}`;
}

const OPENCLAW_VERSION = loadOpenClawVersion();

export function requireSecureRelayApiUrl(value: string): string {
 const url = new URL(value);
 const insecureDevelopmentAllowed = ["1", "true", "yes"].includes(
  (process.env.RELAY_CONSOLE_BRIDGE_ALLOW_INSECURE_HTTP ?? "").toLowerCase(),
 );
 if (url.protocol !== "https:" && !(insecureDevelopmentAllowed && url.protocol === "http:")) {
  throw new Error("Relay Console bridge requires an https:// API URL");
 }
 if (url.username || url.password) {
  throw new Error("Relay Console API URL must not contain credentials");
 }
 return url.toString().replace(/\/$/, "");
}

export function getBridgeClientCapabilities(extraCapabilities: string[] = []): string[] {
 return [...new Set([...DEFAULT_CAPABILITIES, ...extraCapabilities].filter(Boolean))];
}

export function getBridgeClientMetadata(extraCapabilities: string[] = []) {
 return {
  pluginVersion: PLUGIN_VERSION ?? undefined,
  openCoreVersion: OPENCLAW_VERSION ?? undefined,
  runtimeType: "openclaw",
  hostType: bridgeHostType(),
  apiContractVersion: "v2",
  websocketContractVersion: "bridge.v1",
  capabilities: getBridgeClientCapabilities(extraCapabilities),
 };
}

export function buildBridgeDeviceAuthPayload(input: {
 devicePublicId: string;
 deviceToken: string;
 extraCapabilities?: string[];
}) {
 return {
  devicePublicId: input.devicePublicId,
  deviceToken: input.deviceToken,
  ...getBridgeClientMetadata(input.extraCapabilities),
 };
}

export async function redeemBridgeEnrollment(input: {
 apiUrl: string;
 code: string;
 deviceLabel: string;
 extraCapabilities?: string[];
}): Promise<BridgeEnrollmentResponse> {
 const apiUrl = requireSecureRelayApiUrl(input.apiUrl);
 const code = input.code.trim();
 if (!code) {
  throw new Error("Relay Console enrollment code is required");
 }
 const resp = await fetch(`${apiUrl}/api/v1/bridge/enroll`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
   code,
   deviceLabel: input.deviceLabel.trim() || "OpenClaw Relay Console bridge",
   ...getBridgeClientMetadata(input.extraCapabilities),
  }),
 });

 if (!resp.ok) {
  throw new Error(`[clawchat] bridge enrollment failed: ${resp.status}`);
 }

 return (await resp.json()) as BridgeEnrollmentResponse;
}

async function requestBridgeDeviceAuthentication(input: {
 apiUrl: string;
 devicePublicId: string;
 deviceToken: string;
 extraCapabilities?: string[];
}): Promise<BridgeAuthResponse> {
 const apiUrl = requireSecureRelayApiUrl(input.apiUrl);
 const resp = await fetch(`${apiUrl}/api/v1/bridge/device/auth`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(
   buildBridgeDeviceAuthPayload({
    devicePublicId: input.devicePublicId,
    deviceToken: input.deviceToken,
    extraCapabilities: input.extraCapabilities,
   }),
  ),
 });

 if (!resp.ok) {
  throw new Error(`[clawchat] bridge auth failed: ${resp.status}`);
 }

 return (await resp.json()) as BridgeAuthResponse;
}

function resolveCredentialAccount(
 cfg: OpenClawConfig,
 apiUrl: string,
 devicePublicId: string,
) {
 const matches = listClawChatAccountIds(cfg)
  .map((accountId) => resolveClawChatAccount(cfg, accountId))
  .filter((account) => {
   if (account.devicePublicId !== devicePublicId || !account.apiUrl || !account.deviceToken) {
    return false;
   }
   try {
    return requireSecureRelayApiUrl(account.apiUrl) === apiUrl;
   } catch {
    return false;
   }
  });
 if (matches.length !== 1) {
  throw new Error(
   `Relay Console API v2 authentication requires exactly one saved account for device ${devicePublicId}`,
  );
 }
 return matches[0];
}

async function authenticateAndPersistReplacement(input: {
 apiUrl: string;
 devicePublicId: string;
 deviceToken: string;
 extraCapabilities?: string[];
}): Promise<BridgeAuthResponse> {
 const persistence = credentialPersistence;
 if (!persistence) {
  throw new Error("Relay Console API v2 credential persistence is not configured");
 }
 const cfg = persistence.loadConfig();
 const account = resolveCredentialAccount(cfg, input.apiUrl, input.devicePublicId);
 const credentialKey = `${input.apiUrl}\n${input.devicePublicId}`;
 const currentCredential = volatileCredentials.get(credentialKey)
  ?? await persistence.loadCredential({
   apiUrl: input.apiUrl,
   devicePublicId: input.devicePublicId,
   configuredCredential: account.deviceToken!,
  })
  ?? account.deviceToken!;
 const response = await requestBridgeDeviceAuthentication({
  ...input,
  deviceToken: currentCredential,
 });
 const replacementPublicId = response.credentials?.devicePublicId?.trim();
 const replacementCredential = response.credentials?.deviceToken?.trim();
 if (replacementPublicId !== input.devicePublicId || !replacementCredential) {
  throw new Error(
   "Relay Console API v2 authentication response did not include matching replacement credentials",
  );
 }
 // Keep rotation state outside OpenClaw's watched configuration. Rewriting
 // channels.clawchat here reloads the channel while authentication is still
 // completing, which can start a second process with the consumed credential
 // and cause Railway to revoke the bridge as a replay.
 volatileCredentials.set(credentialKey, replacementCredential);
 try {
  await persistence.saveCredential({
   apiUrl: input.apiUrl,
   devicePublicId: input.devicePublicId,
   configuredCredential: account.deviceToken!,
   replacementCredential,
  });
 } catch (error) {
  throw new Error(
   "Relay Console API v2 authentication rotated the device credential but durable persistence failed; retry without restarting or re-enroll the device",
   { cause: error },
  );
 }
 return response;
}

export function authenticateBridgeDevice(input: {
 apiUrl: string;
 devicePublicId: string;
 deviceToken: string;
 extraCapabilities?: string[];
}): Promise<BridgeAuthResponse> {
 const apiUrl = requireSecureRelayApiUrl(input.apiUrl);
 const persistence = credentialPersistence;
 if (!persistence) {
  return Promise.reject(new Error("Relay Console API v2 credential persistence is not configured"));
 }
 // Serialize in-process and across overlapping OpenClaw gateway lifecycles.
 const current = authenticationTail.catch(() => undefined)
  .then(() => persistence.withCredentialLock(
   apiUrl,
   input.devicePublicId,
   () => authenticateAndPersistReplacement({ ...input, apiUrl }),
  ));
 authenticationTail = current;
 return current;
}

export async function rotateBridgeDeviceCredential(input: {
 apiUrl: string;
 devicePublicId: string;
 deviceToken: string;
 extraCapabilities?: string[];
}): Promise<BridgeEnrollmentResponse & BridgeAuthResponse> {
 const apiUrl = requireSecureRelayApiUrl(input.apiUrl);
 const resp = await fetch(`${apiUrl}/api/v1/bridge/device/rotate`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(
   buildBridgeDeviceAuthPayload({
    devicePublicId: input.devicePublicId,
    deviceToken: input.deviceToken,
    extraCapabilities: input.extraCapabilities,
   }),
  ),
 });

 if (!resp.ok) {
  throw new Error(`[clawchat] bridge credential rotation failed: ${resp.status}`);
 }
 return (await resp.json()) as BridgeEnrollmentResponse & BridgeAuthResponse;
}
