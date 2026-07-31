import { createHash } from "node:crypto";
import type { Dirent } from "node:fs";
import { lstat, mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";
import type { ChannelGatewayContext } from "openclaw/plugin-sdk";
import type { ClawChatResolvedAccount } from "./types.js";
import { resolveWorkspaceRoot } from "./agent-workspace.js";
import { getBridgeClientMetadata } from "./bridge-auth.js";
import {
 isAllowedNativeDocumentPath,
 isSensitiveNativeDocumentName,
} from "./native-document-policy.js";

export const AGENT_REPLICA_SYNC_CAPABILITY = "clawchat.agent_replica_sync";
export const RUNTIME_CONNECTOR_V2_CAPABILITY = "clawchat.runtime_connector.v2";
export const RUNTIME_CONNECTOR_V3_CAPABILITY = "clawchat.runtime_connector.v3";
export const RELAY_CONNECTOR_V3 = "relay-connector.v3";
export const RELAY_CONNECTOR_V2 = "relay-connector.v2";
export const AGENT_REPLICA_V1 = "agent-replica.v1";
const MAX_FILE_BYTES = 500_000;
const MAX_DOCUMENTS = 2_000;
const MAX_AGENTS = 250;
const INTERVAL_MS = 10_000;
const ALLOWED_TREES = new Set(["memory", "skills"]);

type LocalDocument = { folder: string; filename: string; content: string; contentHash: string };
type StateEntry = { objectId: string; serverVersion: string; contentHash: string; deleted: boolean };
type ConnectorProtocol =
 | typeof RELAY_CONNECTOR_V3
 | typeof RELAY_CONNECTOR_V2
 | typeof AGENT_REPLICA_V1;
type SyncState = {
 version: 2;
 profiles: Record<string, {
  serverVersion: string;
  localHash: string;
  canonicalAgentId?: string;
  bindingEpoch?: string;
 }>;
 documents: Record<string, StateEntry>;
};
type RemoteDocument = StateEntry & { folder: string; filename: string; content?: string };
type ExchangeResponse = {
 protocolVersion: ConnectorProtocol;
 agents: Array<{
  externalId: string;
  canonicalAgentId?: string;
  bindingEpoch?: string;
  profileServerVersion: string;
  documents: RemoteDocument[];
 }>;
 conflicts?: Array<Record<string, unknown>>;
 discoveries?: Array<{
  externalId: string;
  observationId: string;
  canonicalAgentId?: string | null;
  directive: "metadata_only" | "connect" | "synchronize" | "disconnect" | "quarantine";
  connectionState: string;
  documentSync: boolean;
 }>;
};

class ConnectorProtocolUnsupportedError extends Error {}

export function ownedAgentIds(cfg: Record<string, unknown>): string[] {
 const list = (cfg as { agents?: { list?: Array<{ id?: string }> } }).agents?.list ?? [];
 return [...new Set(list.map((entry) => entry.id?.trim()).filter((id): id is string => Boolean(id)))];
}

export async function runAgentReplicaSyncLoop(params: {
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>;
 accessToken: string;
 signal: AbortSignal;
 onSynchronized?: (externalAgentIds: string[]) => void;
 onScanTriggerReady?: (trigger: () => void) => void;
}): Promise<void> {
 let protocol: ConnectorProtocol = RELAY_CONNECTOR_V3;
 let scanRequested = false;
 let wakeScan: (() => void) | null = null;
 params.onScanTriggerReady?.(() => {
  scanRequested = true;
  wakeScan?.();
 });
 while (!params.signal.aborted) {
  scanRequested = false;
  try {
   let response: ExchangeResponse | null = null;
   while (!response) {
    try {
     response = await exchangeAgentReplicas(params.ctx, params.accessToken, params.signal, protocol);
    } catch (error) {
     if (!(error instanceof ConnectorProtocolUnsupportedError)) throw error;
     if (protocol === RELAY_CONNECTOR_V3) {
      protocol = RELAY_CONNECTOR_V2;
      params.ctx.log?.warn?.("[clawchat] Relay backend does not support relay-connector.v3; using relay-connector.v2 until the gateway reconnects");
      continue;
     }
     if (protocol === RELAY_CONNECTOR_V2) {
      protocol = AGENT_REPLICA_V1;
      params.ctx.log?.warn?.("[clawchat] Relay backend does not support relay-connector.v2; using agent-replica.v1 until the gateway reconnects");
      continue;
     }
     throw error;
    }
   }
   protocol = response.protocolVersion;
   params.onSynchronized?.(response.agents.map((agent) => agent.externalId));
  } catch (error) {
   params.ctx.log?.warn?.(
    `[clawchat] agent replica sync failed code=${safeSyncErrorCode(error)}`,
   );
  }
  if (!scanRequested) {
   await wait(params.signal, (wake) => {
    wakeScan = wake;
   });
  }
  wakeScan = null;
 }
}

export async function exchangeAgentReplicas(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
 accessToken: string,
 signal?: AbortSignal,
 protocolVersion: ConnectorProtocol = RELAY_CONNECTOR_V3,
): Promise<ExchangeResponse> {
 const stateRoot = process.env.OPENCLAW_STATE_DIR?.trim() || join(homedir(), ".openclaw");
 const stateFile = join(stateRoot, "clawchat", `agent-sync-${hash(ctx.account.accountId).slice(0, 16)}.json`);
 const state = await loadState(stateFile);
 const entries = (ctx.cfg as { agents?: { list?: Array<{ id: string; name?: string; model?: string | { primary?: string } }> } }).agents?.list ?? [];
 const agents = [];
 const liveKeys = new Set<string>();
 const incompleteScanAgentIds = new Set<string>();
 let completeManifest = true;
 let completeInventory = true;
 const ownedIds = ownedAgentIds(ctx.cfg as Record<string, unknown>).sort();
 if (ownedIds.length > MAX_AGENTS) {
  completeManifest = false;
  completeInventory = false;
 }
 for (const externalId of ownedIds.slice(0, MAX_AGENTS)) {
  const entry = entries.find((candidate) => candidate.id === externalId);
  const previousProfile = state.profiles[externalId];
  const documentSyncAllowed =
   protocolVersion !== RELAY_CONNECTOR_V3 ||
   Boolean(previousProfile?.canonicalAgentId);
  let documents: LocalDocument[] = [];
  let scanComplete = true;
  if (documentSyncAllowed) {
   const workspaceRoot = resolveWorkspaceRoot(ctx.cfg, externalId);
   const scan = await scanDocuments(workspaceRoot, MAX_DOCUMENTS);
   documents = scan.documents;
   scanComplete = scan.complete;
   if (!scan.complete) {
    completeManifest = false;
    incompleteScanAgentIds.add(externalId);
   }
  }
  const localProfile = {
   externalId,
   name: entry?.name?.trim() || externalId,
   role: "assistant",
   status: "active",
   modelPrimary: typeof entry?.model === "string" ? entry.model : entry?.model?.primary,
   nativeKind: "openclaw_agent",
  };
  const localProfileHash = hash(JSON.stringify(localProfile));
  const documentInputs: Array<LocalDocument & { objectId?: string; baseServerVersion?: string; deleted?: boolean }> = documents.map((document) => {
   const key = stateKey(externalId, document.folder, document.filename);
   liveKeys.add(key);
   const prior = state.documents[key];
   return { ...document, objectId: prior?.objectId, baseServerVersion: prior?.serverVersion };
  });
  for (const [key, prior] of Object.entries(state.documents)) {
   if (!documentSyncAllowed || !scanComplete) continue;
   if (!key.startsWith(`${externalId}:`) || prior.deleted || liveKeys.has(key)) continue;
   if (documentInputs.length >= MAX_DOCUMENTS) { completeManifest = false; break; }
   const path = splitPath(key.slice(externalId.length + 1));
   if (!isAllowedNativeDocumentPath(path.folder, path.filename)) continue;
   documentInputs.push({ ...path, content: "", contentHash: prior.contentHash, objectId: prior.objectId, baseServerVersion: prior.serverVersion, deleted: true });
  }
  const agent = {
   ...localProfile,
   bindingEpoch: previousProfile?.bindingEpoch,
   profileBaseServerVersion: previousProfile && previousProfile.localHash !== localProfileHash ? previousProfile.serverVersion : undefined,
   documents: documentInputs,
  } as typeof localProfile & {
   canonicalAgentId?: string;
   profileBaseServerVersion?: string;
   documents: Array<LocalDocument & { objectId?: string; baseServerVersion?: string; deleted?: boolean }>;
  };
  if (protocolVersion === RELAY_CONNECTOR_V2 && previousProfile?.canonicalAgentId) {
   agent.canonicalAgentId = previousProfile.canonicalAgentId;
  }
  agents.push(agent);
 }
 const manifestHash = hash(JSON.stringify(agents.map((agent) => ({
  externalId: agent.externalId,
  canonicalAgentId: agent.canonicalAgentId,
  profileHash: hash(JSON.stringify({
   externalId: agent.externalId,
   name: agent.name,
   role: agent.role,
   status: agent.status,
   modelPrimary: agent.modelPrimary,
  })),
  documents: agent.documents.filter((document) => !document.deleted).map((document) => ({
   folder: document.folder,
   filename: document.filename,
   contentHash: document.contentHash,
  })).sort((left, right) => pathFor(left.folder, left.filename).localeCompare(pathFor(right.folder, right.filename))),
 }))));
 const inventoryGeneration = hash(JSON.stringify(agents.map((agent) => ({
  externalId: agent.externalId,
  name: agent.name,
  modelPrimary: agent.modelPrimary,
 }))));
 const metadata = getBridgeClientMetadata();
 const versionedFields =
  protocolVersion === RELAY_CONNECTOR_V2 ||
  protocolVersion === RELAY_CONNECTOR_V3
   ? {
  manifestHash,
  completeManifest,
  completeInventory,
  inventoryGeneration,
  host: {
   softwareVersion: metadata.pluginVersion ?? metadata.openCoreVersion,
   protocolVersion: protocolVersion === RELAY_CONNECTOR_V3 ? "3" : "2",
   capabilities: {
    connectorProtocol: protocolVersion,
    pluginVersion: metadata.pluginVersion,
    runtimeVersion: metadata.openCoreVersion,
    completeManifest,
    completeInventory,
    metadataOnlyDiscovery: protocolVersion === RELAY_CONNECTOR_V3,
   },
  },
 } : {};
 const response = await fetch(`${ctx.account.apiUrl}/api/v1/bridge/agent-sync/exchange`, {
 method: "POST",
  signal,
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
  body: JSON.stringify({
   protocolVersion,
   runtimeType: "openclaw",
   ...versionedFields,
   agents,
   acknowledgements: Object.values(state.documents).map((entry) => ({ ...entry, status: "applied" })),
  }),
 });
 if (!response.ok) {
  const text = await response.text();
  if (
   (protocolVersion === RELAY_CONNECTOR_V3 ||
    protocolVersion === RELAY_CONNECTOR_V2) &&
   [400, 422].includes(response.status) &&
   /UNSUPPORTED_AGENT_REPLICA_PROTOCOL/.test(text)
  ) {
   throw new ConnectorProtocolUnsupportedError(text);
  }
  throw new Error(`OPENCLAW_AGENT_SYNC_HTTP_${response.status}`);
 }
 const body = await response.json() as ExchangeResponse;
 if (body.protocolVersion !== protocolVersion) {
  throw new Error("OPENCLAW_AGENT_SYNC_PROTOCOL_MISMATCH");
 }
 await applyResponse(ctx, body, state, incompleteScanAgentIds);
 await saveState(stateFile, state);
 if (body.conflicts?.length) ctx.log?.warn?.(`[clawchat] ${body.conflicts.length} agent document conflict(s) retained locally`);
 return body;
}

async function applyResponse(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
 response: ExchangeResponse,
 state: SyncState,
 incompleteScanAgentIds: Set<string>,
) {
 for (const discovery of response.discoveries ?? []) {
  if (
   discovery.connectionState === "connected" &&
   discovery.canonicalAgentId
  ) {
   continue;
  }
  const profile = state.profiles[discovery.externalId];
  if (profile?.canonicalAgentId) delete profile.canonicalAgentId;
 }
 for (const agent of response.agents) {
  const workspaceRoot = resolveWorkspaceRoot(ctx.cfg, agent.externalId);
  const entry = (ctx.cfg as { agents?: { list?: Array<{ id: string; name?: string; model?: string | { primary?: string } }> } }).agents?.list?.find((candidate) => candidate.id === agent.externalId);
  state.profiles[agent.externalId] = {
   serverVersion: String(agent.profileServerVersion),
   localHash: hash(JSON.stringify({ externalId: agent.externalId, name: entry?.name?.trim() || agent.externalId, role: "assistant", status: "active", modelPrimary: typeof entry?.model === "string" ? entry.model : entry?.model?.primary })),
   canonicalAgentId: agent.canonicalAgentId ?? state.profiles[agent.externalId]?.canonicalAgentId,
   bindingEpoch: agent.bindingEpoch ?? state.profiles[agent.externalId]?.bindingEpoch,
  };
  if (incompleteScanAgentIds.has(agent.externalId)) continue;
  for (const remote of agent.documents) {
   const key = stateKey(agent.externalId, remote.folder, remote.filename);
   const prior = state.documents[key];
  const target = await safeTarget(workspaceRoot, remote.folder, remote.filename);
   const localContent = await readText(target);
   const localHash = localContent === null ? null : hash(localContent);
   if (prior && !prior.deleted && localHash !== null && localHash !== prior.contentHash && localHash !== remote.contentHash && String(remote.serverVersion) !== prior.serverVersion) {
    ctx.log?.warn?.(`[clawchat] preserving conflicting local edit ${agent.externalId}/${pathFor(remote.folder, remote.filename)}`);
    continue;
   }
   if (remote.deleted) {
    if (localContent !== null) {
     await assertNoSymbolicLinkTraversal(workspaceRoot, remote.folder, remote.filename);
     await rm(target, { force: true });
    }
    state.documents[key] = { objectId: remote.objectId, serverVersion: String(remote.serverVersion), contentHash: remote.contentHash, deleted: true };
    continue;
   }
   if (typeof remote.content !== "string" || Buffer.byteLength(remote.content, "utf8") > MAX_FILE_BYTES) continue;
   if (localHash !== remote.contentHash) {
    await assertNoSymbolicLinkTraversal(workspaceRoot, remote.folder, remote.filename);
    await mkdir(dirname(target), { recursive: true });
    await assertNoSymbolicLinkTraversal(workspaceRoot, remote.folder, remote.filename);
    const temporary = `${target}.${process.pid}.tmp`;
    try {
     await writeFile(temporary, remote.content, { encoding: "utf8", mode: 0o600 });
     await assertNoSymbolicLinkTraversal(workspaceRoot, remote.folder, remote.filename);
     await rename(temporary, target);
    } finally {
     await rm(temporary, { force: true }).catch(() => undefined);
    }
   }
   state.documents[key] = { objectId: remote.objectId, serverVersion: String(remote.serverVersion), contentHash: remote.contentHash, deleted: false };
  }
 }
}

export async function scanDocuments(workspaceRoot: string, limit: number): Promise<{ documents: LocalDocument[]; complete: boolean }> {
 const documents: LocalDocument[] = [];
 let complete = true;
 const walk = async (directory: string, relativeDirectory: string, depth: number): Promise<void> => {
  if (depth > 6) return;
  let entries: Dirent[];
  try {
   entries = await readdir(directory, { withFileTypes: true });
  } catch {
   complete = false;
   return;
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
   if (entry.isSymbolicLink() || entry.name.startsWith(".") || isSensitiveNativeDocumentName(entry.name)) continue;
   const relative = relativeDirectory ? `${relativeDirectory}/${entry.name}` : entry.name;
   const absolute = join(directory, entry.name);
   if (entry.isDirectory()) {
    const rootTree = relative.split("/")[0]?.toLowerCase();
    if (ALLOWED_TREES.has(rootTree)) await walk(absolute, relative, depth + 1);
    continue;
   }
   const parsed = splitPath(relative);
   if (!entry.isFile() || !isAllowedNativeDocumentPath(parsed.folder, parsed.filename)) continue;
   if (documents.length >= limit) {
    complete = false;
    break;
   }
   const metadata = await stat(absolute).catch(() => null);
   if (!metadata) {
    complete = false;
    continue;
   }
   if (metadata.size > MAX_FILE_BYTES) continue;
   const content = await readFile(absolute, "utf8").catch(() => null);
   if (content === null) {
    complete = false;
    continue;
   }
   if (content.includes("\0")) continue;
   documents.push({ ...parsed, content, contentHash: hash(content) });
  }
 };
 await walk(resolve(workspaceRoot), "", 0);
 return { documents, complete };
}

async function safeTarget(rootValue: string, folder: string, filename: string): Promise<string> {
 const root = resolve(rootValue);
 const parsed = splitPath(pathFor(folder, filename));
 if (!isAllowedNativeDocumentPath(parsed.folder, parsed.filename)) {
  throw new Error("agent document path is not allowlisted");
 }
 const target = resolve(root, parsed.folder, parsed.filename);
 if (target !== root && !target.startsWith(`${root}${sep}`)) throw new Error("agent document escaped workspace root");
 await assertNoSymbolicLinkTraversal(root, parsed.folder, parsed.filename);
 return target;
}

async function assertNoSymbolicLinkTraversal(
 rootValue: string,
 folder: string,
 filename: string,
): Promise<void> {
 const root = resolve(rootValue);
 const rootInfo = await lstat(root).catch(() => null);
 if (rootInfo?.isSymbolicLink()) throw new Error("agent workspace root must not be a symbolic link");
 const parsed = splitPath(pathFor(folder, filename));
 let current = root;
 for (const part of [...(parsed.folder ? parsed.folder.split("/") : []), parsed.filename]) {
  current = join(current, part);
  const info = await lstat(current).catch(() => null);
  if (info?.isSymbolicLink()) throw new Error("agent document path traversed a symbolic link");
 }
}
function splitPath(value: string) {
 const parts = value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").split("/");
 const filename = parts.pop()?.trim() ?? "";
 if (!filename || filename === "." || filename === ".." || filename.includes("\\")) throw new Error("invalid agent document filename");
 if (parts.length > 6 || parts.some((part) => !part || part === "." || part === "..")) throw new Error("invalid agent document folder");
 return { folder: parts.join("/"), filename };
}
function pathFor(folder: string, filename: string) { return folder ? `${folder}/${filename}` : filename; }
function stateKey(agentId: string, folder: string, filename: string) { return `${agentId}:${pathFor(folder, filename)}`; }
function hash(value: string) { return createHash("sha256").update(value).digest("hex"); }
function safeSyncErrorCode(error: unknown) {
 const candidate = error instanceof Error ? error.message : String(error);
 return /^[A-Z][A-Z0-9_]{0,119}$/.test(candidate)
  ? candidate
  : "OPENCLAW_AGENT_SYNC_FAILED";
}
async function readText(file: string) {
 try { const info = await lstat(file); return info.isFile() && !info.isSymbolicLink() && info.size <= MAX_FILE_BYTES ? await readFile(file, "utf8") : null; } catch { return null; }
}
async function loadState(file: string): Promise<SyncState> {
 try { const parsed = JSON.parse(await readFile(file, "utf8")) as Partial<SyncState>; return { version: 2, profiles: parsed.profiles ?? {}, documents: parsed.documents ?? {} }; }
 catch { return { version: 2, profiles: {}, documents: {} }; }
}
async function saveState(file: string, state: SyncState) {
 await mkdir(dirname(file), { recursive: true });
 const temporary = `${file}.${process.pid}.tmp`;
 await writeFile(temporary, JSON.stringify(state, null, 2), { encoding: "utf8", mode: 0o600 });
 await rename(temporary, file);
}
function wait(signal: AbortSignal, registerWake: (wake: () => void) => void) {
 return new Promise<void>((resolveWait) => {
  if (signal.aborted) return resolveWait();
  const timer = setTimeout(done, INTERVAL_MS);
  function done() {
   clearTimeout(timer);
   signal.removeEventListener("abort", done);
   resolveWait();
  }
  registerWake(done);
  signal.addEventListener("abort", done, { once: true });
 });
}
