import type { ChannelGatewayContext } from "openclaw/plugin-sdk";
import { isDiagnosticsEnabled, onInternalDiagnosticEvent } from "openclaw/plugin-sdk/diagnostic-runtime";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ClawChatResolvedAccount } from "./types.js";
import { authenticateBridgeDevice, getBridgeClientCapabilities } from "./bridge-auth.js";
import { executeStructuredPrompt } from "./structured-prompt.js";
import { provisionAgent } from "./provisioner.js";
import type { ProvisionRequest } from "./provisioner.js";
import { handleLibraryList, handleLibraryRead, handleLibraryWrite, handleLibraryDelete } from "./library.js";
import {
 handleAgentWorkspaceList,
 handleAgentWorkspaceRead,
 handleAgentWorkspaceWrite,
 handleAgentWorkspaceDelete,
} from "./agent-workspace.js";
import {
 handleMarketplaceReadLocalRepoDocs,
 type MarketplaceReadLocalRepoDocsRequest,
} from "./marketplace-local-repo-docs.js";
import type {
 AgentWorkspaceListRequest,
 AgentWorkspaceReadRequest,
 AgentWorkspaceWriteRequest,
 AgentWorkspaceDeleteRequest,
} from "./agent-workspace.js";
import { collectDocumentReferences } from "./document-references.js";
import {
 handleAttachmentUploadEvent,
 prepareDispatchAttachments,
 startAttachmentUploadCleanup,
 type ClawChatAttachment,
} from "./attachments.js";
import { handleAgentStructuredJob } from "./structured-job.js";
import { DispatchJournal } from "./dispatch-journal.js";
import { runAgentReplicaSyncLoop } from "./agent-sync.js";

const CHANNEL_ID = "clawchat";
const MAX_RECENT_CONTEXT_MESSAGES = 12;
const MAX_RECENT_CONTEXT_MESSAGE_CHARS = 2_000;
const MAX_RECENT_CONTEXT_TOTAL_CHARS = 18_000;

type ContextUsageLevel = "unknown" | "ok" | "warn" | "critical" | "overflow";

type ContextUsageSnapshot = {
 totalTokens: number | null;
 contextTokens: number | null;
 percentUsed: number | null;
 level: ContextUsageLevel;
 fresh: boolean;
 sessionId?: string;
 model?: string;
 modelProvider?: string;
};

type ClawChatRecentMessage = {
 senderName: string;
 senderId: string;
 content: string;
 timestamp: string;
 isFromUser: boolean;
 provenance?: string;
};

type AgentDispatchPayload = {
 threadId: string;
 threadSessionId?: string;
 dispatchId?: string;
 messageId: string;
 content: string;
 userId: string;
 senderName: string;
 workspaceId: string;
 recentMessages?: ClawChatRecentMessage[];
 isFromAgent?: boolean;
 messageProvenance?: string;
 externalAgentId: string;
 meetingId?: string | null;
 meetingStatus?: string | null;
 briefMarkdown?: string | null;
 advisoryRules?: string | null;
 hardRestrictionFlagsRequested?: string[];
 sourceMeetingRulePackSnapshotId?: string | null;
 scheduledMessageId?: string | null;
 attachments?: ClawChatAttachment[];
};

/** Build a thread context preamble from recent messages so the agent sees the full conversation. */
function buildThreadContext(recentMessages: ClawChatRecentMessage[] | undefined): string {
 if (!recentMessages?.length) return "";

 let remainingChars = MAX_RECENT_CONTEXT_TOTAL_CHARS;
 const selectedMessages = recentMessages.slice(-MAX_RECENT_CONTEXT_MESSAGES);
 const lines: string[] = [];

 for (const message of selectedMessages) {
  if (remainingChars <= 0) break;

  const senderPrefix = `${message.senderName}: `;
  const availableContentChars = Math.max(
   0,
   Math.min(
    MAX_RECENT_CONTEXT_MESSAGE_CHARS,
    remainingChars - senderPrefix.length,
   ),
  );
  if (availableContentChars <= 0) break;

  const content = message.content.length > availableContentChars
   ? `${message.content.slice(0, Math.max(0, availableContentChars - 24)).trimEnd()}\n[truncated]`
   : message.content;
  const line = `${senderPrefix}${content}`;
  lines.push(line);
  remainingChars -= line.length + 1;
 }

 if (!lines.length) return "";
 return `[Thread context — recent messages]\n${lines.join("\n")}\n\n`;
}

/** Authenticate to the ClawChat bridge and return tokens for WS and HTTP auth. */
async function getBridgeTokens(account: ClawChatResolvedAccount): Promise<{ wsToken: string; accessToken: string }> {
 const body = await authenticateBridgeDevice({
  apiUrl: account.apiUrl!,
  devicePublicId: account.devicePublicId!,
  deviceToken: account.deviceToken!,
 });

 const wsToken = body.tokens?.wsToken ?? body.wsToken ?? body.token;
 const accessToken = body.tokens?.accessToken ?? body.accessToken ?? wsToken;
 if (!wsToken) {
  throw new Error("[clawchat] bridge auth response missing token");
 }
 return { wsToken: wsToken!, accessToken: accessToken! };
}

/** Get all agent IDs from the OpenClaw config. */
function getOwnedAgentIds(cfg: Record<string, unknown>): string[] {
 const agents = (cfg as { agents?: { list?: { id: string }[] } }).agents;
 if (!agents?.list) return [];
 return agents.list.map((a) => a.id);
}

/** Get display name for an agent from config. */
function getAgentDisplayName(cfg: Record<string, unknown>, agentId: string): string {
 const agents = (cfg as { agents?: { list?: { id: string; name?: string }[] } }).agents;
 if (!agents?.list) return agentId;
 const agent = agents.list.find((a) => a.id === agentId);
 return agent?.name || agentId;
}

type RuntimeDispatchEvent =
 | {
  type: "run.thinking";
  seq: number;
  thinking: string;
  kind: "thinking";
 }
 | {
  type: "run.delta";
  seq: number;
  text: string;
 }
 | {
  type: "run.status";
  code: string;
  message: string;
 }
 | {
  type: "run.context";
  totalTokens: number | null;
  contextTokens: number | null;
  percentUsed: number | null;
  level: ContextUsageLevel;
  fresh: boolean;
  sessionId?: string;
  model?: string;
  modelProvider?: string;
 }
 | {
  type: "run.tool";
  toolName: string;
  phase: string;
  summary: string;
 };

type GatewayLogger = {
 info?: (message: string) => void;
 warn?: (message: string) => void;
 error?: (message: string) => void;
};

type SessionStoreRuntime = {
 resolveStorePath?: (store?: string, opts?: { agentId?: string; env?: NodeJS.ProcessEnv }) => string;
};

type RelayChannelRuntime = {
 routing: {
  buildAgentSessionKey: (input: Record<string, unknown>) => string;
 };
 reply: {
  finalizeInboundContext: (input: Record<string, unknown>) => unknown;
  dispatchReplyWithBufferedBlockDispatcher: (input: unknown) => Promise<void>;
 };
 session?: SessionStoreRuntime;
};

type RelayReplyPayload = { text?: string | null };

type ActiveRuntimeDispatch = {
 account: ClawChatResolvedAccount;
 accessToken: string;
 dispatchId: string;
 agentId: string;
 threadId: string;
 sessionKey: string;
 sessionRuntime?: SessionStoreRuntime;
 log?: GatewayLogger;
 pendingContextPost?: Promise<void>;
};

const activeDispatchesBySessionKey = new Map<string, ActiveRuntimeDispatch>();
const dispatchJournal = new DispatchJournal();
let runtimeDiagnosticsListenerRegistered = false;

type SessionStoreEntry = {
 sessionId?: string;
 inputTokens?: number;
 outputTokens?: number;
 totalTokens?: number;
 totalTokensFresh?: boolean;
 contextTokens?: number;
 model?: string;
 modelProvider?: string;
};

function sendWsMessage(
 ws: WebSocket,
 log: GatewayLogger | undefined,
 message: Record<string, unknown>,
): void {
 try {
  ws.send(JSON.stringify(message));
 } catch (error) {
  log?.error?.(`[clawchat] websocket send failed: ${String(error)}`);
 }
}

async function sendBridgeRuntimeEvent(
 account: ClawChatResolvedAccount,
 accessToken: string,
 dispatchId: string | undefined,
 event: RuntimeDispatchEvent,
 log: GatewayLogger | undefined,
 agentId: string,
 threadId: string,
): Promise<void> {
 if (!dispatchId) return;

 const path = `/api/v1/bridge/runtime-dispatches/${encodeURIComponent(dispatchId)}/events`;
 log?.info?.(`[clawchat] POST ${path} started agent="${agentId}" thread=${threadId} event=${event.type}`);

 const resp = await fetch(`${account.apiUrl}${path}`, {
  method: "POST",
  headers: {
   "Content-Type": "application/json",
   Authorization: `Bearer ${accessToken}`,
  },
  body: JSON.stringify(event),
 });

 if (resp.status === 403) {
  log?.warn?.(`[clawchat] agent "${agentId}" is not allowed to post runtime dispatch events for thread ${threadId} — ${event.type} dropped (403)`);
  return;
 }

 if (!resp.ok) {
  const body = await resp.text().catch(() => "");
  throw new Error(`[clawchat] runtime dispatch event postback failed for agent "${agentId}" (${event.type}): ${resp.status} ${body}`);
 }

 log?.info?.(`[clawchat] POST ${path} completed agent="${agentId}" thread=${threadId} event=${event.type} status=${resp.status}`);
}

function queueContextUsageForActiveDispatch(active: ActiveRuntimeDispatch, reason: string): void {
 if (active.pendingContextPost) {
  active.log?.info?.(`[clawchat] runtime diagnostic ${reason} matched active dispatch dispatchId=${active.dispatchId} sessionKey=${active.sessionKey}; context post already pending`);
  return;
 }

 active.pendingContextPost = (async () => {
  const snapshot = await readContextUsageSnapshot({
   agentId: active.agentId,
   sessionKey: active.sessionKey,
   sessionRuntime: active.sessionRuntime,
   log: active.log,
  });
  active.log?.info?.(
   `[clawchat] runtime diagnostic ${reason} matched active dispatch dispatchId=${active.dispatchId} sessionKey=${active.sessionKey}; posting run.context percentUsed=${snapshot.percentUsed ?? "unknown"} totalTokens=${snapshot.totalTokens ?? "unknown"} contextTokens=${snapshot.contextTokens ?? "unknown"}`,
  );
  await sendBridgeRuntimeEvent(active.account, active.accessToken, active.dispatchId, {
   type: "run.context",
   ...snapshot,
  }, active.log, active.agentId, active.threadId);
 })().catch((err) => {
  active.log?.error?.(`[clawchat] runtime diagnostic context post failed dispatchId=${active.dispatchId} sessionKey=${active.sessionKey}: ${err instanceof Error ? err.message : String(err)}`);
 }).finally(() => {
  active.pendingContextPost = undefined;
 });
}

function ensureRuntimeDiagnosticsListener(log?: GatewayLogger): void {
 if (runtimeDiagnosticsListenerRegistered) return;
 runtimeDiagnosticsListenerRegistered = true;

 onInternalDiagnosticEvent((event) => {
  const type = typeof event.type === "string" ? event.type : "";
  // OpenClaw currently emits trusted model.call.* diagnostic events for model usage/progress.
  // Keep model.usage here too so this continues to work if the event name is split later.
  if (type !== "model.usage" && type !== "model.call.completed" && type !== "model.call.error") return;

  const sessionKeyValue = "sessionKey" in event ? event.sessionKey : undefined;
  const sessionKey = typeof sessionKeyValue === "string" ? sessionKeyValue : undefined;
  if (!sessionKey) return;

  const active = activeDispatchesBySessionKey.get(sessionKey);
  if (!active) {
   if (sessionKey.includes(":clawchat:")) {
    log?.info?.(`[clawchat] runtime diagnostic ${type} had no active dispatch for sessionKey=${sessionKey}`);
   }
   return;
  }

  queueContextUsageForActiveDispatch(active, type);
 });

 log?.info?.("[clawchat] runtime diagnostics listener registered for model.usage/model.call.* context events");
}

function summarizeToolProgress(name?: string, phase?: string): string {
 if (name && phase) return `${name} ${phase}`;
 if (name) return `${name} started`;
 if (phase) return `Tool phase ${phase}`;
 return "Tool activity updated";
}

function finiteNumber(value: unknown): number | null {
 return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function resolveContextUsageLevel(percentUsed: number | null): ContextUsageLevel {
 if (percentUsed === null) return "unknown";
 if (percentUsed >= 100) return "overflow";
 if (percentUsed >= 90) return "critical";
 if (percentUsed >= 75) return "warn";
 return "ok";
}

function buildContextUsageSnapshot(entry: SessionStoreEntry | undefined): ContextUsageSnapshot {
 const totalTokens = finiteNumber(entry?.totalTokens);
 const contextTokens = finiteNumber(entry?.contextTokens);
 const percentUsed = totalTokens !== null && contextTokens !== null && contextTokens > 0
  ? Math.round((totalTokens / contextTokens) * 1000) / 10
  : null;

 return {
  totalTokens,
  contextTokens,
  percentUsed,
  level: resolveContextUsageLevel(percentUsed),
  fresh: entry?.totalTokensFresh === true,
  sessionId: entry?.sessionId,
  model: entry?.model,
  modelProvider: entry?.modelProvider,
 };
}

async function readContextUsageSnapshot(params: {
 agentId: string;
 sessionKey: string;
 sessionRuntime?: SessionStoreRuntime;
 log?: GatewayLogger;
}): Promise<ContextUsageSnapshot> {
 const storeCandidates: string[] = [];
 try {
  const resolved = params.sessionRuntime?.resolveStorePath?.(undefined, { agentId: params.agentId, env: process.env });
  if (resolved) storeCandidates.push(resolved);
 } catch {}
 storeCandidates.push(join(homedir(), ".openclaw", "agents", params.agentId, "sessions", "sessions.json"));

 for (const storePath of [...new Set(storeCandidates)]) {
  try {
   const raw = await readFile(storePath, "utf8");
   const store = JSON.parse(raw) as Record<string, SessionStoreEntry>;
   return buildContextUsageSnapshot(store[params.sessionKey]);
  } catch (err) {
   params.log?.warn?.(`[clawchat] context usage read failed for ${params.agentId}: ${err instanceof Error ? err.message : String(err)}`);
  }
 }

 return buildContextUsageSnapshot(undefined);
}

function formatContextUsageMessage(snapshot: ContextUsageSnapshot): string {
 if (snapshot.percentUsed === null || snapshot.totalTokens === null || snapshot.contextTokens === null) {
  return "Context usage unknown";
 }
 return `Context ${snapshot.percentUsed}% used (${snapshot.totalTokens.toLocaleString()}/${snapshot.contextTokens.toLocaleString()} tokens)`;
}

/** Dispatch an agent.dispatch event to the targeted OpenClaw agent. */
async function dispatchAgentWork(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
 payload: AgentDispatchPayload,
): Promise<void> {
 const { account, cfg, log } = ctx;
 const channelRuntime = ctx.channelRuntime as RelayChannelRuntime | undefined;
 const agentId = payload.externalAgentId;

 if (!channelRuntime) {
  log?.warn?.("[clawchat] channelRuntime not available - cannot dispatch AI reply");
  return;
 }

 // Obtain a valid accessToken for outbound reply postback.
 // Previously this referenced `accessToken` from the outer gateway scope which
 // was not in scope here, causing ReferenceError: accessToken is not defined.
 const { accessToken } = await getBridgeTokens(account);

 const dispatchStartedAtMs = Date.now();
 const dispatchStartedAtIso = new Date(dispatchStartedAtMs).toISOString();
 const logTiming = (stage: string, details?: string) => {
  const elapsedMs = Date.now() - dispatchStartedAtMs;
  const suffix = details ? ` ${details}` : "";
  log?.info?.(
   `[clawchat][timing] dispatchId=${payload.dispatchId ?? "<missing>"} agent="${agentId}" thread=${payload.threadId} stage="${stage}" elapsedMs=${elapsedMs} startedAt=${dispatchStartedAtIso}${suffix}`,
  );
 };

 log?.info?.(
  `[clawchat] agent.dispatch: message ${payload.messageId} → agent "${agentId}" in thread ${payload.threadId} dispatchId=${payload.dispatchId ?? "<missing>"} (provenance: ${payload.messageProvenance ?? "unknown"})`,
 );
 logTiming("dispatch handler entered", `messageId=${payload.messageId}`);

 // Build the full message body with thread context prepended
 const threadContext = buildThreadContext(payload.recentMessages);
 const senderLabel = payload.senderName || "User";
 const messageHeader = payload.isFromAgent
  ? `[Message from ${senderLabel}]`
  : `[New message from ${senderLabel}]`;
 const attachmentContext = await prepareDispatchAttachments({
  attachments: payload.attachments,
  log,
 });

 const bodyWithoutAttachments = threadContext
  ? `${threadContext}${messageHeader}\n${payload.content}`
  : payload.isFromAgent
   ? `${messageHeader}\n${payload.content}`
   : payload.content;
 const bodyWithContext = attachmentContext.summaryText
  ? `${bodyWithoutAttachments}\n\n${attachmentContext.summaryText}`
  : bodyWithoutAttachments;

 // dmScope "per-account-channel-peer" produces:
 //   agent:<agentId>:clawchat:<accountId>:direct:<threadId>
 // Without it, the default is "main" which discards threadId and collapses
 // all ClawChat threads into a single session per agent.
 const sessionKey = channelRuntime.routing.buildAgentSessionKey({
  agentId,
  channel: CHANNEL_ID,
  accountId: account.accountId,
  peer: { kind: "direct", id: payload.threadId },
  dmScope: "per-account-channel-peer",
 });

 const ctxPayload = channelRuntime.reply.finalizeInboundContext({
  Body: bodyWithContext,
  BodyForAgent: bodyWithContext,
  RawBody: payload.content,
  CommandBody: payload.content,
  From: payload.threadId,
  To: payload.threadId,
  SessionKey: sessionKey,
  AccountId: account.accountId,
  ChatType: "direct",
  ConversationLabel: senderLabel,
  SenderName: senderLabel,
  SenderId: payload.userId,
  Provider: CHANNEL_ID,
  Surface: CHANNEL_ID,
  MessageSid: `${payload.messageId}:${agentId}`,
  CommandAuthorized: true,
  OriginatingChannel: CHANNEL_ID,
  OriginatingTo: payload.threadId,
  MediaPaths: attachmentContext.mediaPaths,
  MediaTypes: attachmentContext.mediaTypes,
  MediaWorkspaceDir: attachmentContext.mediaWorkspaceDir,
 });

 const agentDisplayName = getAgentDisplayName(cfg as Record<string, unknown>, agentId);

 let progressClosed = false;
 let progressSeq = 0;
 let lastReasoningText = "";
 let lastPartialText = "";
 let lastStatusKey = "";
 let sawFirstThinking = false;
 let sawFirstReplyDelta = false;

 const sendRuntimeEvent = async (event: RuntimeDispatchEvent): Promise<void> => {
  if (progressClosed || !payload.dispatchId) return;

  try {
   await sendBridgeRuntimeEvent(account, accessToken, payload.dispatchId, event, log, agentId, payload.threadId);
  } catch (err) {
   log?.error?.(String(err));
  }
 };

 const sendStatus = async (code: string, message: string): Promise<void> => {
  const key = `${code}:${message}`;
  if (key === lastStatusKey) return;
  lastStatusKey = key;
  await sendRuntimeEvent({ type: "run.status", code, message });
 };

 const sendContextUsage = async (): Promise<void> => {
  const snapshot = await readContextUsageSnapshot({
   agentId,
   sessionKey,
   sessionRuntime: channelRuntime.session as SessionStoreRuntime | undefined,
   log,
  });

  log?.info?.(
   `[clawchat] activeDispatchesBySessionKey lookup sessionKey=${sessionKey} dispatchId=${payload.dispatchId ?? "<missing>"} active=${activeDispatchesBySessionKey.has(sessionKey)}; posting run.context percentUsed=${snapshot.percentUsed ?? "unknown"} totalTokens=${snapshot.totalTokens ?? "unknown"} contextTokens=${snapshot.contextTokens ?? "unknown"}`,
  );
  await sendRuntimeEvent({
   type: "run.context",
   ...snapshot,
  });
  await sendStatus(`context_${snapshot.level}`, formatContextUsageMessage(snapshot));
 };

 const diagnosticsEnabled = isDiagnosticsEnabled(cfg);
 log?.info?.(`[clawchat] diagnostics.enabled effective=${diagnosticsEnabled}`);
 ensureRuntimeDiagnosticsListener(log);
 if (payload.dispatchId) {
  activeDispatchesBySessionKey.set(sessionKey, {
   account,
   accessToken,
   dispatchId: payload.dispatchId,
   agentId,
   threadId: payload.threadId,
   sessionKey,
   sessionRuntime: channelRuntime.session as SessionStoreRuntime | undefined,
   log,
  });
  log?.info?.(`[clawchat] activeDispatchesBySessionKey registered sessionKey=${sessionKey} dispatchId=${payload.dispatchId} agent="${agentId}" thread=${payload.threadId}`);
 }

 try {
  await sendContextUsage();
  await channelRuntime.reply.dispatchReplyWithBufferedBlockDispatcher({
   ctx: ctxPayload,
   cfg,
   dispatcherOptions: {
    deliver: async (replyPayload: RelayReplyPayload) => {
     const text = replyPayload.text ?? "";
     if (!text) return;

     logTiming("final response produced", `chars=${text.length}`);
     logTiming("POST /bridge/messages started", `chars=${text.length}`);

     const documentReferences = await collectDocumentReferences({
      agentId,
      sessionKey,
      runStartedAtMs: dispatchStartedAtMs,
      finalText: text,
      sessionRuntime: channelRuntime.session,
      log,
     });

     const deliverResp = await fetch(`${account.apiUrl}/api/v1/bridge/messages`, {
      method: "POST",
      headers: {
       "Content-Type": "application/json",
       Authorization: `Bearer ${accessToken}`,
      },
      body: JSON.stringify({
       threadId: payload.threadId,
       threadSessionId: payload.threadSessionId,
       dispatchId: payload.dispatchId,
       content: text,
       senderId: agentId,
       senderName: agentDisplayName,
       metadata: {
        documentReferences,
       },
      }),
     });

     if (deliverResp.status === 403) {
      logTiming("POST /bridge/messages failed", "status=403");
      log?.warn?.(`[clawchat] agent "${agentId}" is not a member of thread ${payload.threadId} — reply dropped (403)`);
      return;
     }

     if (!deliverResp.ok) {
      const body = await deliverResp.text().catch(() => "");
      logTiming("POST /bridge/messages failed", `status=${deliverResp.status}`);
      throw new Error(`[clawchat] deliver failed for agent "${agentId}": ${deliverResp.status} ${body}`);
     }

     logTiming("POST /bridge/messages completed", `status=${deliverResp.status}`);
    },
    onError: (err: unknown, info: { kind?: string }) => {
     logTiming("reply dispatch error", `kind=${info.kind}`);
     log?.error?.(`[clawchat] reply dispatch error for agent "${agentId}" (${info.kind}): ${String(err)}`);
    },
   },
   replyOptions: {
    onAgentRunStart: async () => {
     logTiming("OpenClaw run started");
     await sendStatus("started", "Starting agent run");
    },
    onReplyStart: async () => {
     await sendStatus("planning", "Planning response");
    },
    onAssistantMessageStart: async () => {
     lastPartialText = "";
     await sendStatus("composing", "Composing reply");
    },
    onReasoningStream: async (replyPayload: RelayReplyPayload) => {
     const reasoningText = replyPayload.text ?? "";
     if (!reasoningText) return;

     const delta = reasoningText.startsWith(lastReasoningText)
      ? reasoningText.slice(lastReasoningText.length)
      : reasoningText;
     lastReasoningText = reasoningText;

     if (!delta) return;

     if (!sawFirstThinking) {
      sawFirstThinking = true;
      logTiming("first thinking token produced", `chars=${delta.length}`);
     }

     await sendRuntimeEvent({
      type: "run.thinking",
      seq: ++progressSeq,
      thinking: delta,
      kind: "thinking",
     });
    },
    onReasoningEnd: async () => {
     await sendStatus("planned", "Reasoning complete");
    },
    onPartialReply: async (replyPayload: RelayReplyPayload) => {
     const partialText = replyPayload.text ?? "";
     if (!partialText) return;

     const delta = partialText.startsWith(lastPartialText)
      ? partialText.slice(lastPartialText.length)
      : partialText;
     lastPartialText = partialText;

     if (!delta) return;

     if (!sawFirstReplyDelta) {
      sawFirstReplyDelta = true;
      logTiming("first response token produced", `chars=${delta.length}`);
     }

     await sendRuntimeEvent({
      type: "run.delta",
      seq: ++progressSeq,
      text: delta,
     });
    },
    onToolStart: async (toolPayload: { name?: string; phase?: string }) => {
     await sendRuntimeEvent({
      type: "run.tool",
      toolName: toolPayload.name ?? "tool",
      phase: toolPayload.phase ?? "started",
      summary: summarizeToolProgress(toolPayload.name, toolPayload.phase),
     });
    },
    onCompactionStart: async () => {
     await sendStatus("compacting", "Compacting context to keep the run moving");
    },
    onCompactionEnd: async () => {
     await sendContextUsage();
     await sendStatus("resumed", "Compaction complete, continuing run");
    },
   },
  });
  await sendContextUsage();
  logTiming("OpenClaw run completed");
 } catch (err) {
  logTiming("OpenClaw run failed", `error=${err instanceof Error ? err.message : String(err)}`);
  throw err;
 } finally {
  progressClosed = true;
  if (payload.dispatchId && activeDispatchesBySessionKey.get(sessionKey)?.dispatchId === payload.dispatchId) {
   activeDispatchesBySessionKey.delete(sessionKey);
   log?.info?.(`[clawchat] activeDispatchesBySessionKey cleared sessionKey=${sessionKey} dispatchId=${payload.dispatchId}`);
  }
 }
}

async function executeDispatchOnce(ctx: ChannelGatewayContext<ClawChatResolvedAccount>, payload: AgentDispatchPayload): Promise<void> {
 const dispatchId = payload.dispatchId;
 if (!dispatchId) throw new Error("[clawchat] dispatch missing durable dispatchId");
 if (!await dispatchJournal.claim(dispatchId)) {
  ctx.log?.warn?.(`[clawchat] duplicate dispatch skipped dispatchId=${dispatchId}`);
  return;
 }
 try { await dispatchAgentWork(ctx, payload); await dispatchJournal.complete(dispatchId); }
 catch (error) { await dispatchJournal.release(dispatchId); throw error; }
}

async function fetchPendingDispatches(account: ClawChatResolvedAccount, accessToken: string, externalAgentIds: string[]): Promise<AgentDispatchPayload[]> {
 const query = new URLSearchParams();
 query.set("externalAgentIds", externalAgentIds.join(","));
 const response = await fetch(`${account.apiUrl}/api/v1/bridge/runtime-dispatches/pending?${query}`, { headers: { Authorization: `Bearer ${accessToken}` } });
 if (!response.ok) throw new Error(`[clawchat] pending dispatch backfill failed: ${response.status}`);
 const body = await response.json() as { data?: { dispatches?: AgentDispatchPayload[] }; dispatches?: AgentDispatchPayload[] };
 return body.data?.dispatches ?? body.dispatches ?? [];
}

/**
 * Connect to the ClawChat backend via WebSocket, authenticate as a bridge client,
 * register all owned agent IDs, and consume agent.dispatch events for targeted work.
 */
export async function startClawChatGatewayAccount(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
): Promise<void> {
 const { account, cfg, log, abortSignal } = ctx;

 if (!account.configured) {
  log?.warn?.(
   `[clawchat] account ${account.accountId} not configured - skipping gateway start`,
  );
  return;
 }
 if (!ctx.channelRuntime) {
  log?.warn?.("[clawchat] channelRuntime unavailable - AI dispatch disabled");
  return;
 }

 startAttachmentUploadCleanup(log);

 const capabilities = getBridgeClientCapabilities();
 log?.info?.(`[clawchat] bridge auth metadata capabilities=[${capabilities.join(", ")}]`);
 const { wsToken, accessToken } = await getBridgeTokens(account);

 // Derive WebSocket URL from apiUrl (https → wss, http → ws)
 const wsUrl = account.apiUrl!.replace(/^https/, "wss").replace(/^http/, "ws");

 log?.info?.(`[clawchat] connecting to ${wsUrl}`);

 const ws = new WebSocket(wsUrl);

 // Tear down on abort
 const onAbort = () => ws.close(1000, "shutdown");
 abortSignal.addEventListener("abort", onAbort);

 // Phase 1: authenticate and register agents
 // Guard: only process dispatches after the bridge is fully ready (authed + agents registered).
 // Any dispatches arriving before that are queued and drained once ready.
 let bridgeReady = false;
 const pendingDispatches: AgentDispatchPayload[] = [];
 let requestAgentInventoryScan: (() => void) | null = null;
 let pendingAgentInventoryScan = false;

 let settled = false;
 await new Promise<void>((resolve, reject) => {
  const settle = (fn: () => void) => {
   if (settled) return;
   settled = true;
   fn();
  };

  ws.addEventListener("open", () => {
   log?.info?.("[clawchat] websocket open; authenticating bridge client");
   sendWsMessage(ws, log, { type: "authenticate", token: wsToken, capabilities });
  });

  ws.addEventListener("message", async (event: MessageEvent) => {
   let msg: Record<string, unknown>;
   try {
    msg = JSON.parse(event.data as string) as Record<string, unknown>;
   } catch {
    return;
   }

   const type = msg.type as string | undefined;

   if (type === "authenticated") {
    const data = msg.data as { kind?: string } | undefined;
    log?.info?.(`[clawchat] authenticated as kind=${data?.kind ?? "unknown"}`);

    // Register all owned agent IDs — one message per agent, no ack expected
    const agentIds = getOwnedAgentIds(cfg as Record<string, unknown>);
    for (const agentId of agentIds) {
     sendWsMessage(ws, log, { type: "register_bridge_agent", externalAgentId: agentId, capabilities });
    }
    log?.info?.(`[clawchat] registered ${agentIds.length} bridge agent(s): [${agentIds.join(", ")}]`);

    // Subscribe to bridge control stream for provisioning + structured prompt requests.
    // Include capabilities on the websocket subscription as well as HTTP device auth so
    // Railway can classify this socket as structured-prompt capable even after backend
    // restarts or when it does not hydrate capability metadata from the device record.
    sendWsMessage(ws, log, {
     type: "subscribe_bridge_control",
     workspaceId: account.workspaceId,
     capabilities,
    });
    log?.info?.(
     `[clawchat] subscribed to bridge control for workspace ${account.workspaceId} capabilities=[${capabilities.join(", ")}]`,
    );

    // Mark bridge ready and drain any dispatches that arrived before auth completed
    bridgeReady = true;
    if (pendingDispatches.length > 0) {
     log?.info?.(`[clawchat] draining ${pendingDispatches.length} dispatch(es) queued before bridge ready`);
     for (const queued of pendingDispatches) {
      executeDispatchOnce(ctx, queued).catch((err) => {
       log?.error?.(`[clawchat] agent.dispatch error for "${queued.externalAgentId}": ${String(err)}`);
      });
     }
    pendingDispatches.length = 0;
    }

    fetchPendingDispatches(account, accessToken, agentIds).then((dispatches) => {
     log?.info?.(`[clawchat] reconnect backfill returned ${dispatches.length} dispatch(es)`);
     for (const dispatch of dispatches) executeDispatchOnce(ctx, dispatch).catch((error) => log?.error?.(`[clawchat] backfill dispatch failed: ${String(error)}`));
    }).catch((error) => log?.error?.(`[clawchat] reconnect backfill failed: ${String(error)}`));

    settle(resolve);
    return;
   }

   if (type === "auth_error") {
    const authMessage = ((msg.data as { error?: string } | undefined)?.error ?? msg.error ?? "unknown") as string;
    log?.error?.(`[clawchat] websocket auth_error: ${authMessage}`);
    settle(() => reject(new Error(`[clawchat] WS auth_error: ${authMessage}`)));
    return;
   }

   if (typeof type === "string" && type.startsWith("clawchat.attachment.upload.") && msg.data) {
    const handled = await handleAttachmentUploadEvent({
     type,
     data: msg.data as Record<string, unknown>,
     send: (message) => sendWsMessage(ws, log, message),
     log,
    });
    if (handled) return;
   }

   if (type === "claude.cli.structured_prompt" && msg.data) {
    const data = msg.data as Parameters<typeof executeStructuredPrompt>[0]["request"];
    log?.info?.(
     `[clawchat] structured prompt received requestId=${data.requestId ?? "<missing>"} repoKey=${data.repoKey ?? "-"} cwd=${data.cwd ?? "-"}`,
    );

    executeStructuredPrompt({
     account,
     cfg,
     request: data,
     log,
    }).then((result) => {
     log?.info?.(`[clawchat] structured prompt result ready requestId=${data.requestId}`);
     sendWsMessage(ws, log, {
      type: "claude.cli.structured_prompt.result",
      data: {
       requestId: data.requestId,
       output: result.output,
       model: result.model,
      },
     });
    }).catch((error) => {
     const message = error instanceof Error ? error.message : String(error);
     sendWsMessage(ws, log, {
      type: "claude.cli.structured_prompt.error",
      data: {
       requestId: data.requestId,
       error: message,
      },
     });
    });
    return;
   }

   // Consume targeted agent.dispatch events
   if (type === "agent.dispatch" && msg.data) {
    const data = msg.data as AgentDispatchPayload;
    log?.info?.(
     `[clawchat] websocket received agent.dispatch dispatchId=${data.dispatchId ?? "<missing>"} message=${data.messageId} agent="${data.externalAgentId ?? "<missing>"}" thread=${data.threadId}`,
    );
    if (!data.externalAgentId) {
     log?.warn?.(`[clawchat] agent.dispatch missing externalAgentId — dropping message ${data.messageId}`);
     return;
    }
    // Queue if bridge not ready yet; dispatch immediately otherwise
    if (!bridgeReady) {
     log?.info?.(`[clawchat] agent.dispatch queued (bridge not ready): message ${data.messageId} for "${data.externalAgentId}"`);
     pendingDispatches.push(data);
     return;
    }
   executeDispatchOnce(ctx, data).catch((err) => {
     log?.error?.(`[clawchat] agent.dispatch error for "${data.externalAgentId}": ${String(err)}`);
    });
    return;
   }

   if (type === "agent.structured_job" && msg.data) {
    const data = msg.data as Parameters<typeof handleAgentStructuredJob>[1];
    log?.info?.(
     `[clawchat] websocket received agent.structured_job jobId=${data.jobId ?? "<missing>"} correlationId=${data.correlationId ?? "<none>"} agent="${data.externalAgentId ?? "<missing>"}" jobType=${data.jobType ?? "<missing>"}`,
    );
    handleAgentStructuredJob(ctx, data).catch((err) => {
     log?.error?.(`[clawchat] agent.structured_job error for jobId=${data.jobId ?? "<missing>"}: ${String(err)}`);
    });
    return;
   }

   // Handle provisioning requests from ClawChat
   if (type === "agent.provision.request" && msg.data) {
    const data = msg.data as ProvisionRequest;
    if (!data.jobId || !data.payload?.slug) {
     log?.warn?.(`[clawchat] agent.provision.request missing jobId or slug — ignoring`);
     return;
    }
    log?.info?.(`[clawchat] received provision request: job=${data.jobId} slug="${data.payload.slug}"`);

    provisionAgent(data, {
     apiUrl: account.apiUrl!,
     devicePublicId: account.devicePublicId!,
     deviceToken: account.deviceToken!,
     log,
    }).then((slug) => {
     // Register the newly created agent on the existing WS connection
     sendWsMessage(ws, log, { type: "register_bridge_agent", externalAgentId: slug, capabilities });
     log?.info?.(`[clawchat] registered newly provisioned agent "${slug}" on bridge`);
    }).catch((err) => {
     log?.error?.(`[clawchat] provision failed for "${data.payload.slug}": ${String(err)}`);
     // Failure already reported to ClawChat via /fail callback inside provisionAgent
    });
    return;
   }

   if (type === "agent.inventory.request") {
    if (requestAgentInventoryScan) {
     requestAgentInventoryScan();
    } else {
     pendingAgentInventoryScan = true;
    }
    log?.info?.("[clawchat] requested an immediate native-agent inventory scan");
    return;
   }

   // Library CRUD operations
   const wsSend = (d: string) => {
    try {
     ws.send(d);
    } catch (error) {
     log?.error?.(`[clawchat] websocket send failed: ${String(error)}`);
    }
   };

   if (type === "library.list" && msg.data) {
    log?.info?.(`[clawchat] bridge control received library.list requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleLibraryList(wsSend, msg.data as Parameters<typeof handleLibraryList>[1], log);
    return;
   }

   if (type === "library.read" && msg.data) {
    log?.info?.(`[clawchat] bridge control received library.read requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleLibraryRead(wsSend, msg.data as Parameters<typeof handleLibraryRead>[1], log);
    return;
   }

   if (type === "library.write" && msg.data) {
    log?.info?.(`[clawchat] bridge control received library.write requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleLibraryWrite(wsSend, msg.data as Parameters<typeof handleLibraryWrite>[1], log);
    return;
   }

   if (type === "library.delete" && msg.data) {
    log?.info?.(`[clawchat] bridge control received library.delete requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleLibraryDelete(wsSend, msg.data as Parameters<typeof handleLibraryDelete>[1], log);
    return;
   }

   // Agent workspace CRUD operations (sync — direct filesystem, no RPC)
   if (type === "agent.workspace.list" && msg.data) {
    log?.info?.(`[clawchat] bridge control received agent.workspace.list requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleAgentWorkspaceList(wsSend, msg.data as AgentWorkspaceListRequest, cfg, log);
    return;
   }

   if (type === "agent.workspace.read" && msg.data) {
    log?.info?.(`[clawchat] bridge control received agent.workspace.read requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleAgentWorkspaceRead(wsSend, msg.data as AgentWorkspaceReadRequest, cfg, log);
    return;
   }

   if (type === "agent.workspace.write" && msg.data) {
    log?.info?.(`[clawchat] bridge control received agent.workspace.write requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleAgentWorkspaceWrite(wsSend, msg.data as AgentWorkspaceWriteRequest, cfg, log);
    return;
   }

   if (type === "agent.workspace.delete" && msg.data) {
    log?.info?.(`[clawchat] bridge control received agent.workspace.delete requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleAgentWorkspaceDelete(wsSend, msg.data as AgentWorkspaceDeleteRequest, cfg, log);
    return;
   }

   if (type === "marketplace.readLocalRepoDocs" && msg.data) {
    log?.info?.(`[clawchat] bridge control received marketplace.readLocalRepoDocs requestId=${(msg.data as { requestId?: string }).requestId ?? "<missing>"}`);
    handleMarketplaceReadLocalRepoDocs(wsSend, msg.data as MarketplaceReadLocalRepoDocsRequest, log);
    return;
   }
  });

  ws.addEventListener("error", (event: Event) => {
   log?.error?.(`[clawchat] websocket error${bridgeReady ? "" : " before authentication"}: ${String(event)}`);
   if (!bridgeReady) {
    settle(() => reject(new Error(`[clawchat] WS error: ${String(event)}`)));
   }
  });

  ws.addEventListener("close", (event: CloseEvent) => {
   log?.warn?.(
    `[clawchat] websocket closed code=${event.code} reason=${event.reason || "<none>"} clean=${event.wasClean}`,
   );
   if (!bridgeReady) {
    settle(() => reject(new Error("[clawchat] WS closed before authentication completed")));
   }
  });
 });

 const replicaAbort = new AbortController();
 const stopReplicaSync = () => replicaAbort.abort();
 abortSignal.addEventListener("abort", stopReplicaSync, { once: true });
 ws.addEventListener("close", stopReplicaSync, { once: true });
 const replicaSync = runAgentReplicaSyncLoop({
  ctx,
  accessToken,
 signal: replicaAbort.signal,
  onScanTriggerReady: (trigger) => {
   requestAgentInventoryScan = trigger;
   if (pendingAgentInventoryScan) {
    pendingAgentInventoryScan = false;
    trigger();
   }
  },
 onSynchronized: (externalAgentIds) => {
   if (ws.readyState !== WebSocket.OPEN) return;
   for (const externalAgentId of externalAgentIds) {
    sendWsMessage(ws, log, { type: "register_bridge_agent", externalAgentId, capabilities });
   }
  },
 }).catch((error) => log?.warn?.(`[clawchat] agent replica loop stopped: ${String(error)}`));

 // Gateway is up — wait until aborted
 await new Promise<void>((resolve) => {
  abortSignal.addEventListener("abort", () => resolve());
  ws.addEventListener("close", () => resolve());
 });

 abortSignal.removeEventListener("abort", onAbort);
 abortSignal.removeEventListener("abort", stopReplicaSync);
 replicaAbort.abort();
 await replicaSync;
 log?.info?.("[clawchat] gateway stopped");
 if (!abortSignal.aborted) {
  log?.warn?.("[clawchat] websocket closed unexpectedly; reconnecting bridge in 1s");
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  return startClawChatGatewayAccount(ctx);
 }
}
