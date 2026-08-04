import { createHash } from "node:crypto";
import { createWriteStream } from "node:fs";
import { mkdir, readdir, rename, rm, stat } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

const LOCAL_MEDIA_CAPABILITY = "clawchat.attachments.local_media";
const DEFAULT_STALE_UPLOAD_MS = 60 * 60 * 1000;
const DEFAULT_MAX_CHUNK_BYTES = 16 * 1024 * 1024;
const MAX_REF_LENGTH = 4096;

export const CLAWCHAT_ATTACHMENTS_LOCAL_MEDIA_CAPABILITY = LOCAL_MEDIA_CAPABILITY;

type GatewayLogger = {
 info?: (message: string) => void;
 warn?: (message: string) => void;
 error?: (message: string) => void;
};

export type ClawChatAttachment = {
 id?: string;
 attachmentId?: string;
 filename?: string;
 name?: string;
 mimeType?: string;
 contentType?: string;
 sizeBytes?: number;
 byteSize?: number;
 kind?: string;
 localMediaRef?: string;
 sha256?: string;
 text?: string;
};

type UploadSession = {
 key: string;
 workspaceId: string;
 threadId: string;
 attachmentId: string;
 filename: string;
 mimeType: string;
 expectedSizeBytes?: number;
 expectedSha256?: string;
 dir: string;
 finalPath: string;
 partPath: string;
 localMediaRef: string;
 receivedBytes: number;
 createdAt: number;
 updatedAt: number;
 stream: ReturnType<typeof createWriteStream>;
};

type UploadResult = Record<string, unknown>;

const uploads = new Map<string, UploadSession>();
let cleanupTimer: ReturnType<typeof setInterval> | null = null;

export function resolveClawChatMediaRoot(): string {
 const openclawHome = process.env.OPENCLAW_HOME?.trim() || homedir();
 const stateRoot = process.env.OPENCLAW_STATE_DIR?.trim() || path.join(openclawHome, ".openclaw");
 return path.join(stateRoot, "media", "clawchat");
}

function normalizeIdSegment(value: unknown, label: string): string {
 const raw = typeof value === "string" ? value.trim() : "";
 if (!raw) throw new Error(`${label} is required`);
 if (raw === "." || raw === ".." || raw.includes("/") || raw.includes("\\") || path.isAbsolute(raw)) {
  throw new Error(`${label} contains an unsafe path segment`);
 }
 const safe = raw.replace(/[^a-zA-Z0-9._-]/g, "_").replace(/^\.+/, "_").slice(0, 180);
 if (!safe || safe === "." || safe === "..") throw new Error(`${label} is unsafe`);
 return safe;
}

function sanitizeFilename(value: unknown): string {
 const raw = typeof value === "string" && value.trim() ? value.trim() : "attachment.bin";
 const basename = path.basename(raw.replace(/\\/g, "/"));
 const safe = basename
  .replace(/[\x00-\x1f\x7f]/g, "")
  .replace(/[^a-zA-Z0-9._ -]/g, "_")
  .replace(/^\.+/, "_")
  .slice(0, 240)
  .trim();
 return safe && safe !== "." && safe !== ".." ? safe : "attachment.bin";
}

function uploadKey(workspaceId: string, threadId: string, attachmentId: string): string {
 return `${workspaceId}\n${threadId}\n${attachmentId}`;
}

function asOptionalPositiveInteger(value: unknown, label: string): number | undefined {
 if (value === undefined || value === null || value === "") return undefined;
 const n = typeof value === "number" ? value : Number(value);
 if (!Number.isSafeInteger(n) || n < 0) throw new Error(`${label} must be a non-negative integer`);
 return n;
}

function normalizeMimeType(value: unknown): string {
 const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
 return raw || "application/octet-stream";
}

function normalizeSha256(value: unknown): string | undefined {
 const raw = typeof value === "string" ? value.trim().toLowerCase() : "";
 if (!raw) return undefined;
 if (!/^[a-f0-9]{64}$/.test(raw)) throw new Error("sha256 must be a 64-character hex digest");
 return raw;
}

function makeUploadPaths(input: {
 workspaceId: string;
 threadId: string;
 attachmentId: string;
 filename: string;
}) {
 const root = resolveClawChatMediaRoot();
 const dir = path.join(root, input.workspaceId, input.threadId, input.attachmentId);
 const finalPath = path.join(dir, input.filename);
 const partPath = `${finalPath}.part`;
 const localMediaRef = [input.workspaceId, input.threadId, input.attachmentId, input.filename].join("/");
 return { root, dir, finalPath, partPath, localMediaRef };
}

function decodeChunk(data: Record<string, unknown>): Buffer {
 const value = data.chunkBase64 ?? data.dataBase64 ?? data.base64 ?? data.chunk ?? data.data;
 if (typeof value === "string") {
  const encoding = typeof data.encoding === "string" ? data.encoding.toLowerCase() : "base64";
  if (encoding !== "base64") throw new Error(`unsupported chunk encoding: ${encoding}`);
  const cleaned = value.includes(",") ? value.slice(value.indexOf(",") + 1) : value;
  return Buffer.from(cleaned, "base64");
 }
 if (Array.isArray(value) && value.every((n) => Number.isInteger(n) && n >= 0 && n <= 255)) {
  return Buffer.from(value as number[]);
 }
 throw new Error("chunk payload is missing; expected base64 chunk data");
}

async function closeStream(session: UploadSession): Promise<void> {
 await new Promise<void>((resolve, reject) => {
  session.stream.end((err?: Error | null) => (err ? reject(err) : resolve()));
 });
}

async function cleanupSession(session: UploadSession, removeFinal = false): Promise<void> {
 uploads.delete(session.key);
 try {
  await closeStream(session);
 } catch {
  // Stream may already be closed; cleanup should be best-effort.
 }
 await rm(session.partPath, { force: true }).catch(() => undefined);
 if (removeFinal) await rm(session.finalPath, { force: true }).catch(() => undefined);
 await rm(session.dir, { recursive: true, force: true }).catch(() => undefined);
}

async function handleInit(data: Record<string, unknown>): Promise<UploadResult> {
 const workspaceId = normalizeIdSegment(data.workspaceId, "workspaceId");
 const threadId = normalizeIdSegment(data.threadId, "threadId");
 const attachmentId = normalizeIdSegment(data.attachmentId ?? data.id, "attachmentId");
 const filename = sanitizeFilename(data.filename ?? data.name);
 const mimeType = normalizeMimeType(data.mimeType ?? data.contentType);
 const expectedSizeBytes = asOptionalPositiveInteger(data.sizeBytes ?? data.byteSize ?? data.totalSizeBytes, "sizeBytes");
 const expectedSha256 = normalizeSha256(data.sha256);
 const key = uploadKey(workspaceId, threadId, attachmentId);
 const previous = uploads.get(key);
 if (previous) await cleanupSession(previous, false);
 const paths = makeUploadPaths({ workspaceId, threadId, attachmentId, filename });
 await mkdir(paths.dir, { recursive: true, mode: 0o700 });
 await rm(paths.partPath, { force: true }).catch(() => undefined);
 const stream = createWriteStream(paths.partPath, { flags: "wx", mode: 0o600 });
 const now = Date.now();
 const session: UploadSession = {
  key,
  workspaceId,
  threadId,
  attachmentId,
  filename,
  mimeType,
  expectedSizeBytes,
  expectedSha256,
  dir: paths.dir,
  finalPath: paths.finalPath,
  partPath: paths.partPath,
  localMediaRef: paths.localMediaRef,
  receivedBytes: 0,
  createdAt: now,
  updatedAt: now,
  stream,
 };
 uploads.set(key, session);
 return {
  workspaceId,
  threadId,
  attachmentId,
  filename,
  mimeType,
  sizeBytes: expectedSizeBytes,
  localMediaRef: session.localMediaRef,
  receivedBytes: 0,
 };
}

async function handleChunk(data: Record<string, unknown>): Promise<UploadResult> {
 const workspaceId = normalizeIdSegment(data.workspaceId, "workspaceId");
 const threadId = normalizeIdSegment(data.threadId, "threadId");
 const attachmentId = normalizeIdSegment(data.attachmentId ?? data.id, "attachmentId");
 const session = uploads.get(uploadKey(workspaceId, threadId, attachmentId));
 if (!session) throw new Error("upload session not found");
 const chunk = decodeChunk(data);
 if (chunk.length > DEFAULT_MAX_CHUNK_BYTES) throw new Error(`chunk exceeds ${DEFAULT_MAX_CHUNK_BYTES} bytes`);
 const offset = asOptionalPositiveInteger(data.offset ?? data.startByte, "offset");
 if (offset !== undefined && offset !== session.receivedBytes) {
  throw new Error(`unexpected chunk offset ${offset}; expected ${session.receivedBytes}`);
 }
 await new Promise<void>((resolve, reject) => {
  session.stream.write(chunk, (err?: Error | null) => (err ? reject(err) : resolve()));
 });
 session.receivedBytes += chunk.length;
 session.updatedAt = Date.now();
 if (session.expectedSizeBytes !== undefined && session.receivedBytes > session.expectedSizeBytes) {
  throw new Error(`received ${session.receivedBytes} bytes, expected ${session.expectedSizeBytes}`);
 }
 return {
  workspaceId,
  threadId,
  attachmentId,
  localMediaRef: session.localMediaRef,
  receivedBytes: session.receivedBytes,
 };
}

async function handleComplete(data: Record<string, unknown>): Promise<UploadResult> {
 const workspaceId = normalizeIdSegment(data.workspaceId, "workspaceId");
 const threadId = normalizeIdSegment(data.threadId, "threadId");
 const attachmentId = normalizeIdSegment(data.attachmentId ?? data.id, "attachmentId");
 const session = uploads.get(uploadKey(workspaceId, threadId, attachmentId));
 if (!session) throw new Error("upload session not found");
 const finalSizeBytes = asOptionalPositiveInteger(data.sizeBytes ?? data.byteSize ?? data.totalSizeBytes, "sizeBytes") ?? session.expectedSizeBytes;
 const finalSha256 = normalizeSha256(data.sha256) ?? session.expectedSha256;
 await closeStream(session);
 uploads.delete(session.key);
 if (finalSizeBytes !== undefined && session.receivedBytes !== finalSizeBytes) {
  await rm(session.partPath, { force: true }).catch(() => undefined);
  throw new Error(`upload size mismatch: received ${session.receivedBytes}, expected ${finalSizeBytes}`);
 }
 if (finalSha256) {
  const digest = await hashFileSha256(session.partPath);
  if (digest !== finalSha256) {
   await rm(session.partPath, { force: true }).catch(() => undefined);
   throw new Error(`upload sha256 mismatch: received ${digest}, expected ${finalSha256}`);
  }
 }
 await rm(session.finalPath, { force: true }).catch(() => undefined);
 await rename(session.partPath, session.finalPath);
 return {
  workspaceId,
  threadId,
  attachmentId,
  filename: session.filename,
  mimeType: session.mimeType,
  sizeBytes: session.receivedBytes,
  sha256: finalSha256,
  localMediaRef: session.localMediaRef,
 };
}

async function handleCancel(data: Record<string, unknown>): Promise<UploadResult> {
 const workspaceId = normalizeIdSegment(data.workspaceId, "workspaceId");
 const threadId = normalizeIdSegment(data.threadId, "threadId");
 const attachmentId = normalizeIdSegment(data.attachmentId ?? data.id, "attachmentId");
 const key = uploadKey(workspaceId, threadId, attachmentId);
 const session = uploads.get(key);
 if (session) await cleanupSession(session, false);
 return { workspaceId, threadId, attachmentId, cancelled: true };
}

async function cleanupUploadFromData(data: Record<string, unknown>): Promise<void> {
 try {
  const workspaceId = normalizeIdSegment(data.workspaceId, "workspaceId");
  const threadId = normalizeIdSegment(data.threadId, "threadId");
  const attachmentId = normalizeIdSegment(data.attachmentId ?? data.id, "attachmentId");
  const session = uploads.get(uploadKey(workspaceId, threadId, attachmentId));
  if (session) await cleanupSession(session, false);
 } catch {
  // If the identifiers are invalid, there is no safe session path to clean.
 }
}

async function hashFileSha256(filePath: string): Promise<string> {
 const { createReadStream } = await import("node:fs");
 const hash = createHash("sha256");
 await new Promise<void>((resolve, reject) => {
  const stream = createReadStream(filePath);
  stream.on("data", (chunk) => hash.update(chunk));
  stream.on("error", reject);
  stream.on("end", () => resolve());
 });
 return hash.digest("hex");
}

function resultTypeFor(eventType: string): string {
 return `${eventType}.result`;
}

export async function handleAttachmentUploadEvent(input: {
 type: string;
 data: Record<string, unknown>;
 send: (message: Record<string, unknown>) => void;
 log?: GatewayLogger;
}): Promise<boolean> {
 if (!input.type.startsWith("clawchat.attachment.upload.")) return false;
 const requestId = typeof input.data.requestId === "string" ? input.data.requestId : undefined;
 try {
  let result: UploadResult;
  if (input.type === "clawchat.attachment.upload.init") result = await handleInit(input.data);
  else if (input.type === "clawchat.attachment.upload.chunk") result = await handleChunk(input.data);
  else if (input.type === "clawchat.attachment.upload.complete") result = await handleComplete(input.data);
  else if (input.type === "clawchat.attachment.upload.cancel") result = await handleCancel(input.data);
  else return false;
  input.send({
   type: resultTypeFor(input.type),
   data: { requestId, ...result },
  });
 } catch (error) {
  await cleanupUploadFromData(input.data);
  const message = error instanceof Error ? error.message : String(error);
  input.log?.error?.(`[clawchat] attachment upload ${input.type} failed requestId=${requestId ?? "<missing>"}: ${message}`);
  input.send({
   type: "clawchat.attachment.upload.error",
   data: {
    requestId,
    sourceType: input.type,
    error: message,
   },
  });
 }
 return true;
}

export function resolveLocalMediaAttachment(input: {
 attachment: ClawChatAttachment;
 log?: GatewayLogger;
}): { path: string; mimeType: string; summary: string } | null {
 const ref = typeof input.attachment.localMediaRef === "string" ? input.attachment.localMediaRef.trim() : "";
 if (!ref) return null;
 if (ref.length > MAX_REF_LENGTH) throw new Error("localMediaRef is too long");
 if (path.isAbsolute(ref) || ref.includes("\\")) throw new Error("localMediaRef must be a relative POSIX path");
 const segments = ref.split("/");
 if (segments.some((segment) => !segment || segment === "." || segment === "..")) {
  throw new Error("localMediaRef contains unsafe path segments");
 }
 const root = resolveClawChatMediaRoot();
 const resolved = path.resolve(root, ...segments);
 const relative = path.relative(root, resolved);
 if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
  throw new Error("localMediaRef escapes the ClawChat media root");
 }
 const filename = input.attachment.filename || input.attachment.name || path.basename(resolved);
 const mimeType = normalizeMimeType(input.attachment.mimeType ?? input.attachment.contentType);
 const size = input.attachment.sizeBytes ?? input.attachment.byteSize;
 return {
  path: resolved,
  mimeType,
  summary: `- ${filename} (${mimeType}${typeof size === "number" ? `, ${size} bytes` : ""}) localMediaRef=${ref}`,
 };
}

export async function prepareDispatchAttachments(input: {
 attachments?: ClawChatAttachment[];
 log?: GatewayLogger;
}): Promise<{
 mediaPaths: string[];
 mediaTypes: string[];
 mediaWorkspaceDir?: string;
 summaryText: string;
}> {
 const mediaPaths: string[] = [];
 const mediaTypes: string[] = [];
 const summaries: string[] = [];
 const root = resolveClawChatMediaRoot();
 for (const attachment of input.attachments ?? []) {
  try {
   const resolved = resolveLocalMediaAttachment({ attachment, log: input.log });
   if (resolved) {
    const st = await stat(resolved.path);
    if (!st.isFile()) throw new Error("resolved attachment is not a file");
    mediaPaths.push(resolved.path);
    mediaTypes.push(resolved.mimeType);
    summaries.push(resolved.summary);
    continue;
   }
   const filename = attachment.filename || attachment.name || attachment.id || attachment.attachmentId || "attachment";
   const mimeType = normalizeMimeType(attachment.mimeType ?? attachment.contentType);
   const text = typeof attachment.text === "string" && attachment.text.trim() ? `\n  extractedText: ${attachment.text.slice(0, 12000)}` : "";
   summaries.push(`- ${filename} (${mimeType}) unavailable as local media${text}`);
  } catch (error) {
   const filename = attachment.filename || attachment.name || attachment.id || attachment.attachmentId || "attachment";
   const message = error instanceof Error ? error.message : String(error);
   input.log?.warn?.(`[clawchat] rejected attachment ${filename}: ${message}`);
   summaries.push(`- ${filename} rejected: ${message}`);
  }
 }
 return {
  mediaPaths,
  mediaTypes,
  mediaWorkspaceDir: mediaPaths.length ? root : undefined,
  summaryText: summaries.length ? `[Attachments]\n${summaries.join("\n")}` : "",
 };
}

async function cleanupStalePartFiles(root: string, cutoffMs: number, log?: GatewayLogger): Promise<void> {
 async function walk(dir: string): Promise<void> {
  let entries;
  try {
   entries = await readdir(dir, { withFileTypes: true });
  } catch {
   return;
  }
  for (const entry of entries) {
   const entryPath = path.join(dir, entry.name);
   if (entry.isDirectory()) {
    await walk(entryPath);
    continue;
   }
   if (!entry.isFile() || !entry.name.endsWith(".part")) continue;
   try {
    const st = await stat(entryPath);
    if (Date.now() - st.mtimeMs > cutoffMs) {
     await rm(entryPath, { force: true });
     log?.info?.(`[clawchat] removed stale attachment upload part ${entryPath}`);
    }
   } catch {
    // best effort
   }
  }
 }
 await walk(root);
}

export function startAttachmentUploadCleanup(log?: GatewayLogger): void {
 if (cleanupTimer) return;
 const root = resolveClawChatMediaRoot();
 cleanupStalePartFiles(root, DEFAULT_STALE_UPLOAD_MS, log).catch((error) => {
  log?.warn?.(`[clawchat] stale attachment cleanup failed: ${String(error)}`);
 });
 cleanupTimer = setInterval(() => {
  const now = Date.now();
  for (const session of [...uploads.values()]) {
   if (now - session.updatedAt > DEFAULT_STALE_UPLOAD_MS) {
    cleanupSession(session, false).catch((error) => {
     log?.warn?.(`[clawchat] stale in-memory attachment cleanup failed: ${String(error)}`);
    });
   }
  }
  cleanupStalePartFiles(root, DEFAULT_STALE_UPLOAD_MS, log).catch((error) => {
   log?.warn?.(`[clawchat] stale attachment cleanup failed: ${String(error)}`);
  });
 }, 10 * 60 * 1000);
 cleanupTimer.unref?.();
}
