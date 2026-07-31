import type { ChannelGatewayContext } from "openclaw/plugin-sdk";
import type { ClawChatResolvedAccount } from "./types.js";
import { authenticateBridgeDevice } from "./bridge-auth.js";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";

type StructuredJobPayload = {
 jobId: string;
 workspaceId: string;
 jobType: "thread_wrap_up_report" | "condensed_team_chat_message";
 agentId: string;
 externalAgentId: string;
 runtimeType: "openclaw" | "hermes";
 prompt: string;
 schema: Record<string, unknown>;
 input: Record<string, unknown>;
 model?: string | null;
 timeoutMs: number;
 correlationId?: string | null;
 metadata?: Record<string, unknown>;
};

type StructuredJobErrorCode =
 | "structured_job_unsupported"
 | "structured_output_unsupported"
 | "agent_not_live"
 | "model_unavailable"
 | "timeout"
 | "schema_validation_failed"
 | "malformed_output"
 | "runtime_error"
 | "cancelled";

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

type StructuredJobRunResult = {
 text: string;
 model: string | null;
 metadata: Record<string, unknown>;
};

type RuntimeTerminalStatus = "success" | "failed" | "timeout" | "cancelled" | "unknown";

function errorStack(error: unknown): string {
 if (error instanceof Error) return error.stack || error.message;
 return String(error);
}

function previewBody(value: string): string {
 return value.length > 500 ? `${value.slice(0, 500)}...` : value;
}

function structuredJobClientTimeoutMs(payloadTimeoutMs: unknown): number {
 const raw = typeof payloadTimeoutMs === "number" && Number.isFinite(payloadTimeoutMs)
  ? payloadTimeoutMs
  : 60_000;
 // Leave time for the error callback to reach Railway before the backend's own
 // structured-job watchdog closes the row.
 return Math.max(1_000, Math.floor(raw) - 15_000);
}

function sleep(ms: number): Promise<void> {
 return new Promise((resolve) => setTimeout(resolve, Math.max(1, ms)));
}

function assertBeforeDeadline(deadlineAtMs: number, jobId: string, phase: string): void {
 if (Date.now() <= deadlineAtMs) return;
 throw Object.assign(new Error(`structured job deadline exceeded during ${phase}`), { code: "timeout", jobId });
}

function isObject(value: unknown): value is Record<string, unknown> {
 return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function extractJsonObject(text: string): unknown {
 const trimmed = text.trim();
 if (!trimmed) throw new Error("empty model output");
 try {
  return JSON.parse(trimmed);
 } catch {}

 const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i);
 if (fenced?.[1]) {
  return JSON.parse(fenced[1].trim());
 }

 const start = trimmed.indexOf("{");
 const end = trimmed.lastIndexOf("}");
 if (start >= 0 && end > start) {
  return JSON.parse(trimmed.slice(start, end + 1));
 }

 throw new Error("model output did not contain a JSON object");
}

function validateJsonSchema(value: unknown, schema: unknown, path = "$"): string[] {
 if (schema === true || schema == null) return [];
 if (schema === false) return [`${path}: schema is false`];
 if (!isObject(schema)) return [];

 const errors: string[] = [];
 const schemaType = schema.type;
 const allowedTypes = Array.isArray(schemaType)
  ? schemaType.filter((item): item is string => typeof item === "string")
  : typeof schemaType === "string"
   ? [schemaType]
   : [];

 if (allowedTypes.length > 0 && !allowedTypes.some((type) => matchesJsonType(value, type))) {
  errors.push(`${path}: expected ${allowedTypes.join(" or ")}`);
  return errors;
 }

 if (Array.isArray(schema.enum) && !schema.enum.some((item) => JSON.stringify(item) === JSON.stringify(value))) {
  errors.push(`${path}: value is not in enum`);
 }

 if (Array.isArray(schema.oneOf) || Array.isArray(schema.anyOf)) {
  const variants = (Array.isArray(schema.oneOf) ? schema.oneOf : schema.anyOf) as unknown[];
  if (!variants.some((variant) => validateJsonSchema(value, variant, path).length === 0)) {
   errors.push(`${path}: did not match any schema variant`);
  }
 }

 if (isObject(value)) {
  const required = Array.isArray(schema.required) ? schema.required.filter((item): item is string => typeof item === "string") : [];
  for (const key of required) {
   if (!(key in value)) errors.push(`${path}.${key}: required property missing`);
  }
  const properties = isObject(schema.properties) ? schema.properties : {};
  for (const [key, childSchema] of Object.entries(properties)) {
   if (key in value) {
    errors.push(...validateJsonSchema(value[key], childSchema, `${path}.${key}`));
   }
  }
 }

 if (Array.isArray(value) && schema.items !== undefined) {
  value.forEach((item, index) => {
   errors.push(...validateJsonSchema(item, schema.items, `${path}[${index}]`));
  });
 }

 return errors;
}

function matchesJsonType(value: unknown, type: string): boolean {
 switch (type) {
  case "object":
   return isObject(value);
  case "array":
   return Array.isArray(value);
  case "string":
   return typeof value === "string";
  case "number":
   return typeof value === "number" && Number.isFinite(value);
  case "integer":
   return Number.isInteger(value);
  case "boolean":
   return typeof value === "boolean";
  case "null":
   return value === null;
  default:
   return true;
 }
}

function extractTextFromMessageContent(content: unknown): string {
 if (typeof content === "string") return content;
 if (!Array.isArray(content)) return "";
 const textParts: string[] = [];
 for (const part of content) {
  if (isObject(part) && part.type === "text" && typeof part.text === "string") {
   textParts.push(part.text);
  }
 }
 return textParts.join("\n").trim();
}

function extractLatestAssistantTextFromTranscript(raw: string): string {
 let latest = "";
 for (const line of raw.split(/\r?\n/)) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  try {
   const entry = JSON.parse(trimmed) as Record<string, unknown>;
   const message = isObject(entry.message) ? entry.message : undefined;
   if (entry.type !== "message" || message?.role !== "assistant") continue;
   const text = extractTextFromMessageContent(message.content);
   if (text.trim()) latest = text.trim();
  } catch {}
 }
 return latest;
}

function extractAssistantTextFromTrajectoryEvent(entry: Record<string, unknown>): string {
 const data = isObject(entry.data) ? entry.data : {};
 const assistantTexts = Array.isArray(data.assistantTexts) ? data.assistantTexts : [];
 for (let i = assistantTexts.length - 1; i >= 0; i -= 1) {
  const candidate = assistantTexts[i];
  if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  if (isObject(candidate)) {
   const text = typeof candidate.text === "string" ? candidate.text : "";
   if (text.trim()) return text.trim();
  }
 }
 return "";
}

async function resolveStructuredJobSessionFile(input: {
 channelRuntime: RelayChannelRuntime;
 agentId: string;
 sessionKey: string;
}): Promise<string | undefined> {
 const storeCandidates: string[] = [];
 try {
  const resolved = input.channelRuntime.session?.resolveStorePath?.(undefined, { agentId: input.agentId, env: process.env });
  if (resolved) storeCandidates.push(resolved);
 } catch {}
 storeCandidates.push(join(homedir(), ".openclaw", "agents", input.agentId, "sessions", "sessions.json"));

 for (const storePath of Array.from(new Set(storeCandidates))) {
  try {
   const raw = await readFile(storePath, "utf8");
   const store = JSON.parse(raw) as Record<string, { sessionFile?: string; sessionId?: string }>;
   const row = store[input.sessionKey];
   if (row?.sessionFile) return row.sessionFile;
   if (row?.sessionId) return join(homedir(), ".openclaw", "agents", input.agentId, "sessions", `${row.sessionId}.jsonl`);
  } catch {}
 }
 return undefined;
}

function trajectoryPathForSessionFile(sessionFile: string): string {
 return sessionFile.endsWith(".jsonl")
  ? `${sessionFile.slice(0, -".jsonl".length)}.trajectory.jsonl`
  : `${sessionFile}.trajectory.jsonl`;
}

async function readRuntimeTerminalFromTrajectory(input: {
 trajectoryFile: string;
}): Promise<{ status?: RuntimeTerminalStatus; text?: string; terminalEvent?: string }> {
 let raw = "";
 try {
  raw = await readFile(input.trajectoryFile, "utf8");
 } catch {
  return {};
 }

 let latestText = "";
 for (const line of raw.split(/\r?\n/)) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  try {
   const entry = JSON.parse(trimmed) as Record<string, unknown>;
   const type = typeof entry.type === "string" ? entry.type : "";
   if (type === "model.completed") {
    const text = extractAssistantTextFromTrajectoryEvent(entry);
    if (text) latestText = text;
   }
   if (type === "session.ended") {
    const data = isObject(entry.data) ? entry.data : {};
    const rawStatus = typeof data.status === "string" ? data.status : "unknown";
    const status: RuntimeTerminalStatus = rawStatus === "success" || rawStatus === "failed" || rawStatus === "timeout" || rawStatus === "cancelled"
     ? rawStatus
     : "unknown";
    return { status, text: latestText, terminalEvent: type };
   }
  } catch {}
 }
 return latestText ? { text: latestText, terminalEvent: "model.completed" } : {};
}

async function observeStructuredJobTerminalOutput(input: {
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>;
 payload: StructuredJobPayload;
 sessionKey: string;
 deadlineAtMs: number;
 log?: GatewayLogger;
}): Promise<StructuredJobRunResult> {
 const { ctx, payload, sessionKey, deadlineAtMs, log } = input;
 if (!ctx.channelRuntime) {
  throw Object.assign(new Error("OpenClaw channel runtime is not available"), { code: "structured_job_unsupported" });
 }

 let sessionFile: string | undefined;
 let capturedLength = -1;
 let loggedTerminal = false;
 while (true) {
  assertBeforeDeadline(deadlineAtMs, payload.jobId, "terminal output observation");

  sessionFile ??= await resolveStructuredJobSessionFile({
   channelRuntime: ctx.channelRuntime as unknown as RelayChannelRuntime,
   agentId: payload.externalAgentId,
   sessionKey,
  });
  assertBeforeDeadline(deadlineAtMs, payload.jobId, "session file lookup");

  if (sessionFile) {
   let transcriptText = "";
   try {
    transcriptText = extractLatestAssistantTextFromTranscript(await readFile(sessionFile, "utf8"));
   } catch {}
   assertBeforeDeadline(deadlineAtMs, payload.jobId, "session transcript read");

   if (transcriptText && transcriptText.length !== capturedLength) {
    capturedLength = transcriptText.length;
    log?.info?.(`[clawchat] structured_job final assistant output captured jobId=${payload.jobId} length=${transcriptText.length} source=transcript`);
    log?.info?.(`[clawchat] structured_job runtime terminal status observed jobId=${payload.jobId} status=success event=assistant_output source=transcript`);
    return {
     text: transcriptText,
     model: payload.model ?? null,
     metadata: {
      runtimeType: "openclaw",
      externalAgentId: payload.externalAgentId,
      sessionKey,
      sessionFile,
      completionSource: "transcript",
     },
    };
   }

   const terminal = await readRuntimeTerminalFromTrajectory({ trajectoryFile: trajectoryPathForSessionFile(sessionFile) });
   assertBeforeDeadline(deadlineAtMs, payload.jobId, "runtime trajectory read");
   if (terminal.text && terminal.text.length !== capturedLength) {
    capturedLength = terminal.text.length;
    log?.info?.(`[clawchat] structured_job final assistant output captured jobId=${payload.jobId} length=${terminal.text.length} source=trajectory`);
   }
   if (terminal.text && !terminal.status) {
    log?.info?.(`[clawchat] structured_job runtime terminal status observed jobId=${payload.jobId} status=success event=${terminal.terminalEvent ?? "model.completed"} source=trajectory`);
    return {
     text: terminal.text,
     model: payload.model ?? null,
     metadata: {
      runtimeType: "openclaw",
      externalAgentId: payload.externalAgentId,
      sessionKey,
      sessionFile,
      completionSource: "trajectory",
     },
    };
   }
   if (terminal.status && !loggedTerminal) {
    loggedTerminal = true;
    log?.info?.(`[clawchat] structured_job runtime terminal status observed jobId=${payload.jobId} status=${terminal.status} event=${terminal.terminalEvent ?? "<unknown>"}`);
   }
   if (terminal.status) {
    if (terminal.status !== "success") {
     throw Object.assign(new Error(`OpenClaw structured job runtime ended with status ${terminal.status}`), { code: "runtime_error" });
    }
    const text = terminal.text || transcriptText;
    if (!text.trim()) {
     throw Object.assign(new Error("OpenClaw structured job produced no final assistant output"), { code: "malformed_output" });
    }
    return {
     text,
     model: payload.model ?? null,
     metadata: {
      runtimeType: "openclaw",
      externalAgentId: payload.externalAgentId,
      sessionKey,
      sessionFile,
      completionSource: terminal.text ? "trajectory" : "transcript_terminal",
     },
    };
   }
  }

  await sleep(250);
  assertBeforeDeadline(deadlineAtMs, payload.jobId, "terminal output poll sleep");
 }
}

async function getAccessToken(account: ClawChatResolvedAccount): Promise<string> {
 const authBody = await authenticateBridgeDevice({
  apiUrl: account.apiUrl!,
  devicePublicId: account.devicePublicId!,
  deviceToken: account.deviceToken!,
 });
 const token = authBody.tokens?.accessToken ?? authBody.accessToken ?? authBody.tokens?.wsToken ?? authBody.wsToken ?? authBody.token;
 if (!token) throw new Error("[clawchat] structured job auth response missing token");
 return token;
}

async function postStructuredJobResult(input: {
 account: ClawChatResolvedAccount;
 accessToken: string;
 jobId: string;
 body: Record<string, unknown>;
 kind: "result" | "error";
 log?: GatewayLogger;
}): Promise<void> {
 const url = `${input.account.apiUrl}/api/v1/bridge/structured-jobs/${encodeURIComponent(input.jobId)}/${input.kind}`;
 input.log?.info?.(`[clawchat] structured_job postback start jobId=${input.jobId} kind=${input.kind} url=${url}`);
 const resp = await fetch(url, {
  method: "POST",
  headers: {
   "Content-Type": "application/json",
   Authorization: `Bearer ${input.accessToken}`,
  },
  body: JSON.stringify(input.body),
  signal: typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
   ? AbortSignal.timeout(15_000)
   : undefined,
 });
 const text = await resp.text().catch(() => "");
 input.log?.info?.(
  `[clawchat] structured_job postback completed jobId=${input.jobId} kind=${input.kind} status=${resp.status} body=${previewBody(text)}`,
 );
 if (!resp.ok) {
  throw new Error(`[clawchat] structured job ${input.kind} postback failed: ${resp.status} ${text}`);
 }
}

async function withTimeout<T>(
 promise: Promise<T>,
 timeoutMs: number,
 log?: GatewayLogger,
 jobId?: string,
): Promise<T> {
 let timeout: ReturnType<typeof setTimeout> | undefined;
 try {
  log?.info?.(`[clawchat] structured_job watchdog armed jobId=${jobId ?? "<missing>"} timeoutMs=${timeoutMs}`);
  return await Promise.race([
   promise,
   new Promise<T>((_, reject) => {
    timeout = setTimeout(() => {
     log?.warn?.(`[clawchat] structured_job watchdog fired jobId=${jobId ?? "<missing>"} timeoutMs=${timeoutMs}`);
     reject(Object.assign(new Error("structured job timed out"), { code: "timeout" }));
    }, Math.max(1, timeoutMs));
   }),
  ]);
 } finally {
  if (timeout) clearTimeout(timeout);
 }
}

async function runOpenClawStructuredPrompt(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
 payload: StructuredJobPayload,
 deadlineAtMs: number,
): Promise<{ text: string; model: string | null; metadata: Record<string, unknown> }> {
 const { account, cfg, log } = ctx;
 const channelRuntime = ctx.channelRuntime as RelayChannelRuntime | undefined;
 if (!channelRuntime) {
  throw Object.assign(new Error("OpenClaw channel runtime is not available"), { code: "structured_job_unsupported" });
 }

 const sessionKey = channelRuntime.routing.buildAgentSessionKey({
  agentId: payload.externalAgentId,
  channel: "clawchat",
  accountId: account.accountId,
  peer: { kind: "direct", id: `structured-job:${payload.jobId}` },
  dmScope: "per-account-channel-peer",
 });

 const schemaInstruction = [
  "This is a hidden ClawChat structured job.",
  "Return only a JSON object that conforms to the supplied JSON Schema.",
  "Do not include markdown, code fences, commentary, or a visible chat reply.",
  `JSON Schema:\n${JSON.stringify(payload.schema)}`,
 ].join("\n\n");

 let finalText = "";
 const ctxPayload = channelRuntime.reply.finalizeInboundContext({
  Body: `${schemaInstruction}\n\n${payload.prompt}`,
  BodyForAgent: `${schemaInstruction}\n\n${payload.prompt}`,
  RawBody: payload.prompt,
  CommandBody: payload.prompt,
  From: `structured-job:${payload.jobId}`,
  To: `structured-job:${payload.jobId}`,
  SessionKey: sessionKey,
  AccountId: account.accountId,
  ChatType: "direct",
  ConversationLabel: "ClawChat Structured Job",
  SenderName: "ClawChat Structured Job",
  SenderId: "clawchat-structured-job",
  Provider: "clawchat",
  Surface: "clawchat",
  MessageSid: `structured-job:${payload.jobId}:${payload.externalAgentId}`,
  CommandAuthorized: true,
  OriginatingChannel: "clawchat",
  OriginatingTo: `structured-job:${payload.jobId}`,
 });

 const dispatcherPromise = channelRuntime.reply.dispatchReplyWithBufferedBlockDispatcher({
  ctx: ctxPayload,
  cfg,
  dispatcherOptions: {
   deliver: async (replyPayload: RelayReplyPayload) => {
    finalText = replyPayload.text ?? "";
    if (finalText.trim()) {
     log?.info?.(`[clawchat] structured_job final assistant output captured jobId=${payload.jobId} length=${finalText.length} source=dispatcher`);
    }
   },
   onError: (err: unknown) => {
    throw err instanceof Error ? err : new Error(String(err));
   },
  },
 }).then((): StructuredJobRunResult => {
  assertBeforeDeadline(deadlineAtMs, payload.jobId, "dispatcher completion");
  if (!finalText.trim()) {
   throw Object.assign(new Error("OpenClaw structured job produced no output"), { code: "malformed_output" });
  }
  return {
   text: finalText,
   model: payload.model ?? null,
   metadata: {
    runtimeType: "openclaw",
    externalAgentId: payload.externalAgentId,
    sessionKey,
    completionSource: "dispatcher",
   },
  };
 });

 log?.info?.(`[clawchat] structured_job runtime started jobId=${payload.jobId} sessionKey=${sessionKey}`);
 const observerPromise = observeStructuredJobTerminalOutput({
  ctx,
  payload,
  sessionKey,
  deadlineAtMs,
  log,
 });

 const result = await Promise.race([dispatcherPromise, observerPromise]);
 if (result.metadata.completionSource !== "dispatcher") {
  dispatcherPromise
   .then(() => {
    log?.warn?.(`[clawchat] structured_job stale dispatcher promise ignored after result postback jobId=${payload.jobId}`);
   })
   .catch((error) => {
    log?.warn?.(`[clawchat] structured_job stale dispatcher promise rejected after result postback jobId=${payload.jobId} error=${String(error)}`);
   });
 }
 return result;
}

export async function handleAgentStructuredJob(
 ctx: ChannelGatewayContext<ClawChatResolvedAccount>,
 payload: StructuredJobPayload,
): Promise<void> {
 const { account, cfg, log } = ctx;
 const logger = log as GatewayLogger | undefined;
 const jobId = payload.jobId;
 let posted = false;
 let accessToken = "";

 logger?.info?.(
  `[clawchat] agent.structured_job handler entered jobId=${jobId ?? "<missing>"} correlationId=${payload.correlationId ?? "<none>"} type=${payload.jobType ?? "<missing>"} agent="${payload.externalAgentId ?? "<missing>"}" timeoutMs=${payload.timeoutMs ?? "<missing>"}`,
 );

 const postError = async (
  code: StructuredJobErrorCode,
  message: string,
  retryable: boolean,
  metadata: Record<string, unknown> | null = null,
 ) => {
  if (posted) return;
 if (!accessToken) {
  logger?.error?.(`[clawchat] structured_job cannot post error without access token jobId=${jobId ?? "<missing>"} code=${code} message=${message}`);
  return;
 }
  posted = true;
  logger?.warn?.(
   `[clawchat] structured_job posting error jobId=${jobId} code=${code} retryable=${retryable} message=${message}`,
  );
  try {
   await postStructuredJobResult({
    account,
    accessToken,
    jobId,
    kind: "error",
    body: { code, message, retryable, metadata },
    log: logger,
   });
  } catch (error) {
   logger?.error?.(`[clawchat] structured_job error postback failed jobId=${jobId} stack=${errorStack(error)}`);
   throw error;
  }
 };

 try {
  if (!jobId || !payload.externalAgentId || !payload.prompt || !isObject(payload.schema)) {
   await postError("runtime_error", "Malformed structured job payload", false);
   return;
  }
  accessToken = await getAccessToken(account);
  logger?.info?.(`[clawchat] structured_job bridge auth token acquired jobId=${jobId}`);
  const agentIds = ((cfg as Record<string, unknown>).agents as { list?: { id: string }[] } | undefined)?.list?.map((agent) => agent.id) ?? [];
  if (!agentIds.includes(payload.externalAgentId)) {
   await postError("agent_not_live", `OpenClaw agent is not registered on this bridge: ${payload.externalAgentId}`, false, {
    externalAgentId: payload.externalAgentId,
   });
   return;
  }

  const timeoutMs = structuredJobClientTimeoutMs(payload.timeoutMs);
  logger?.info?.(
   `[clawchat] agent.structured_job executing jobId=${jobId} type=${payload.jobType} agent="${payload.externalAgentId}" payloadTimeoutMs=${payload.timeoutMs ?? "<missing>"} clientTimeoutMs=${timeoutMs}`,
  );
  const deadlineAtMs = Date.now() + timeoutMs;
  const result = await withTimeout(runOpenClawStructuredPrompt(ctx, payload, deadlineAtMs), timeoutMs, logger, jobId);
  logger?.info?.(`[clawchat] structured_job runtime completed jobId=${jobId} chars=${result.text.length}`);
  let output: unknown;
  try {
   output = extractJsonObject(result.text);
   logger?.info?.(`[clawchat] structured_job JSON parse ok jobId=${jobId}`);
  } catch (error) {
   logger?.warn?.(`[clawchat] structured_job JSON parse failed jobId=${jobId} error=${String(error)}`);
   await postError("malformed_output", error instanceof Error ? error.message : String(error), false, result.metadata);
   return;
  }
  const validationErrors = validateJsonSchema(output, payload.schema);
  if (validationErrors.length > 0) {
   logger?.warn?.(`[clawchat] structured_job schema validation failed jobId=${jobId} errors=${validationErrors.slice(0, 6).join("; ")}`);
   await postError("schema_validation_failed", validationErrors.slice(0, 6).join("; "), false, result.metadata);
   return;
  }
  logger?.info?.(`[clawchat] structured_job schema validation ok jobId=${jobId}`);
  if (posted) return;
  posted = true;
  await postStructuredJobResult({
   account,
   accessToken,
   jobId,
   kind: "result",
   body: {
    output,
    model: result.model,
    usage: null,
    metadata: result.metadata,
   },
   log: logger,
  });
  logger?.info?.(`[clawchat] agent.structured_job result posted jobId=${jobId}`);
 } catch (error) {
  const err = error as Error & { code?: string };
  const code = err.code === "timeout" ? "timeout" : "runtime_error";
  logger?.error?.(`[clawchat] structured_job failed jobId=${jobId ?? "<missing>"} code=${code} stack=${errorStack(error)}`);
  await postError(code, err.message || String(error), code === "timeout");
 }
}
