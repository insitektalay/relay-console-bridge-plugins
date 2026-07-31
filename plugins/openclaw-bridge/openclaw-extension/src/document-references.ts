import { readFile } from "node:fs/promises";
import { basename, extname, join } from "node:path";
import { homedir } from "node:os";

export type DocumentReference = {
 id?: string;
 kind:
  | "workspace_file"
  | "memory_file"
  | "skill"
  | "workflow"
  | "library_doc"
  | "system_doc"
  | "web"
  | "artifact"
  | "unknown";
 title?: string;
 displayPath?: string;
 uri?: string;
 mimeType?: string;
 role?: "knowledge" | "routing" | "rule" | "memory" | "evidence" | "artifact";
 action?: "consulted" | "read" | "routed_to" | "used" | "generated" | "modified";
 source?:
  | "tool_call"
  | "tool_result"
  | "prompt_context"
  | "skill_router"
  | "workflow_router"
  | "agent_declared"
  | "parsed_markdown";
 confidence?: "observed" | "injected" | "inferred" | "agent_declared";
 sensitive?: boolean;
 redacted?: boolean;
};

type LogSink = {
 warn?: (message: string) => void;
 error?: (message: string) => void;
};

type SessionRuntime = {
 resolveStorePath?: (store?: string, opts?: { agentId?: string; env?: NodeJS.ProcessEnv }) => string;
};

type CollectParams = {
 agentId: string;
 sessionKey: string;
 runStartedAtMs: number;
 finalText?: string;
 sessionRuntime?: SessionRuntime;
 log?: LogSink;
};

type TranscriptEntry = {
 type?: string;
 timestamp?: string;
 message?: {
  role?: string;
  content?: unknown;
  toolName?: string;
  details?: unknown;
 };
};

type ToolCallLike = {
 name?: string;
 arguments?: unknown;
};

const MAX_REFERENCES = 80;
const RECENT_RUN_SKEW_MS = 15_000;
const MARKDOWN_EXTS = new Set([".md", ".mdx", ".markdown", ".qmd"]);
const TEXT_EXTS = new Set([".txt", ".json", ".json5", ".yaml", ".yml", ".toml", ".ts", ".tsx", ".js", ".jsx", ".py", ".sh"]);
const SYSTEM_DOC_NAMES = new Set(["AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md", "HEARTBEAT.md", "BOOTSTRAP.md"]);

export async function collectDocumentReferences(params: CollectParams): Promise<DocumentReference[]> {
 const refs = new ReferenceAccumulator();

 try {
  const sessionFile = await resolveSessionFile(params);
  if (sessionFile) {
   const entries = await readTranscriptEntries(sessionFile, params.runStartedAtMs - RECENT_RUN_SKEW_MS);
   for (const entry of entries) collectFromTranscriptEntry(entry, refs);
  }
 } catch (err) {
  params.log?.warn?.(`[clawchat] document reference transcript collection failed: ${err instanceof Error ? err.message : String(err)}`);
 }

 if (params.finalText) {
  for (const ref of parseAgentDeclaredReferences(params.finalText)) refs.add(ref);
 }

 return refs.list().slice(0, MAX_REFERENCES).map((ref, index) => ({ id: ref.id ?? `ref_${String(index + 1).padStart(3, "0")}`, ...ref }));
}

class ReferenceAccumulator {
 private readonly map = new Map<string, DocumentReference>();

 add(ref: DocumentReference | undefined): void {
  if (!ref) return;
  const normalized = normalizeReference(ref);
  const key = referenceKey(normalized);
  const existing = this.map.get(key);
  if (!existing) {
   this.map.set(key, normalized);
   return;
  }
  this.map.set(key, mergeReference(existing, normalized));
 }

 list(): DocumentReference[] {
  return [...this.map.values()];
 }
}

function normalizeReference(ref: DocumentReference): DocumentReference {
 const next: DocumentReference = { ...ref };
 if (next.displayPath) next.displayPath = sanitizeDisplayPath(next.displayPath);
 if (!next.title && next.displayPath) next.title = basename(next.displayPath);
 if (!next.mimeType && next.displayPath) next.mimeType = inferMimeType(next.displayPath);
 if (next.kind === "memory_file" || next.kind === "system_doc") next.sensitive ??= true;
 return next;
}

function mergeReference(a: DocumentReference, b: DocumentReference): DocumentReference {
 const confidenceRank = { observed: 4, injected: 3, inferred: 2, agent_declared: 1 } as const;
 const aRank = a.confidence ? confidenceRank[a.confidence] : 0;
 const bRank = b.confidence ? confidenceRank[b.confidence] : 0;
 const preferred = bRank > aRank ? b : a;
 const other = preferred === a ? b : a;
 return {
  ...other,
  ...preferred,
  sensitive: Boolean(a.sensitive || b.sensitive),
  redacted: Boolean(a.redacted || b.redacted),
 };
}

function referenceKey(ref: DocumentReference): string {
 return [ref.kind, ref.uri ?? "", ref.displayPath ?? "", ref.title ?? ""].join("|").toLowerCase();
}

async function resolveSessionFile(params: CollectParams): Promise<string | undefined> {
 const storeCandidates: string[] = [];
 try {
  const resolved = params.sessionRuntime?.resolveStorePath?.(undefined, { agentId: params.agentId, env: process.env });
  if (resolved) storeCandidates.push(resolved);
 } catch {}
 storeCandidates.push(join(homedir(), ".openclaw", "agents", params.agentId, "sessions", "sessions.json"));

 for (const storePath of unique(storeCandidates)) {
  try {
   const raw = await readFile(storePath, "utf8");
   const store = JSON.parse(raw) as Record<string, { sessionFile?: string; sessionId?: string }>;
   const row = store[params.sessionKey];
   if (row?.sessionFile) return row.sessionFile;
   if (row?.sessionId) return join(homedir(), ".openclaw", "agents", params.agentId, "sessions", `${row.sessionId}.jsonl`);
  } catch {}
 }
 return undefined;
}

async function readTranscriptEntries(sessionFile: string, sinceMs: number): Promise<TranscriptEntry[]> {
 const raw = await readFile(sessionFile, "utf8");
 const out: TranscriptEntry[] = [];
 for (const line of raw.split(/\r?\n/)) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  try {
   const entry = JSON.parse(trimmed) as TranscriptEntry;
   const ts = timestampMs(entry);
   if (ts !== undefined && ts < sinceMs) continue;
   out.push(entry);
  } catch {}
 }
 return out;
}

function timestampMs(entry: TranscriptEntry): number | undefined {
 const nested = (entry.message as { timestamp?: unknown } | undefined)?.timestamp;
 if (typeof nested === "number") return nested;
 if (typeof entry.timestamp === "string") {
  const parsed = Date.parse(entry.timestamp);
  if (Number.isFinite(parsed)) return parsed;
 }
 return undefined;
}

function collectFromTranscriptEntry(entry: TranscriptEntry, refs: ReferenceAccumulator): void {
 const message = entry.message;
 if (!message) return;

 if (message.role === "system") {
  for (const ref of parsePromptContextReferences(message.content)) refs.add(ref);
 }

 if (message.role === "assistant") {
  for (const call of extractToolCalls(message.content)) {
   for (const ref of refsFromToolCall(call.name, call.arguments)) refs.add(ref);
  }
 }

 if (message.role === "toolResult") {
  for (const ref of refsFromToolResult(message.toolName, message.details, message.content)) refs.add(ref);
 }
}

function extractToolCalls(content: unknown): ToolCallLike[] {
 const calls: ToolCallLike[] = [];
 if (!Array.isArray(content)) return calls;
 for (const item of content) {
  if (!item || typeof item !== "object") continue;
  const record = item as Record<string, unknown>;
  if (record.type === "toolCall") calls.push({ name: stringOrUndefined(record.name), arguments: record.arguments });
 }
 return calls;
}

function refsFromToolCall(toolName: string | undefined, args: unknown): DocumentReference[] {
 const name = toolName ?? "";
 const simpleName = name.includes(".") ? name.split(".").pop() ?? name : name;
 const obj = isRecord(args) ? args : {};
 const refs: DocumentReference[] = [];

 if (simpleName === "read" || simpleName === "edit" || simpleName === "write") {
  const path = firstString(obj, ["path", "file_path", "filePath", "file"]);
  if (path) refs.push(refFromPath(path, { source: "tool_call", confidence: "observed", action: simpleName === "read" ? "read" : simpleName === "write" ? "generated" : "modified" }));
 }

 if (name === "library.read" || name === "library.write" || name === "library.delete") {
  const folder = firstString(obj, ["folder"]) ?? "";
  const filename = firstString(obj, ["filename"]);
  if (filename) refs.push(refFromPath(`library/${folder ? `${folder}/` : ""}${filename}`, { source: "tool_call", confidence: "observed", action: name === "library.write" ? "modified" : "read" }));
  const files = Array.isArray(obj.files) ? obj.files : [];
  for (const file of files) {
   if (isRecord(file) && typeof file.filename === "string") refs.push(refFromPath(`library/${folder ? `${folder}/` : ""}${file.filename}`, { source: "tool_call", confidence: "observed", action: "modified" }));
  }
 }

 if (name === "agent.workspace.read" || name === "agent.workspace.write" || name === "agent.workspace.delete") {
  const folder = firstString(obj, ["folder"]) ?? "";
  const filename = firstString(obj, ["filename"]);
  if (filename) refs.push(refFromPath(folder ? `${folder}/${filename}` : filename, { source: "tool_call", confidence: "observed", action: name === "agent.workspace.write" ? "modified" : "read" }));
  const files = Array.isArray(obj.files) ? obj.files : [];
  for (const file of files) {
   if (isRecord(file) && typeof file.filename === "string") refs.push(refFromPath(folder ? `${folder}/${file.filename}` : file.filename, { source: "tool_call", confidence: "observed", action: "modified" }));
  }
 }

 if (name === "apply_patch") {
  const patch = firstString(obj, ["input", "patch"]);
  for (const path of patch ? extractPatchPaths(patch) : []) refs.push(refFromPath(path, { source: "tool_call", confidence: "observed", action: "modified" }));
  if (!patch) refs.push({ kind: "artifact", title: "Patch", displayPath: "patch", role: "artifact", action: "modified", source: "tool_call", confidence: "observed" });
 }

 if (name === "web_fetch") {
  const url = firstString(obj, ["url"]);
  if (url) refs.push(refFromUrl(url, { source: "tool_call", confidence: "observed", action: "read" }));
 }

 if (name === "web_search") {
  const query = firstString(obj, ["query"]);
  if (query) refs.push({ kind: "web", title: `Web search: ${truncate(query, 80)}`, displayPath: `web/search?q=${truncate(query, 80)}`, role: "evidence", action: "consulted", source: "tool_call", confidence: "observed" });
 }

 if (name === "memory_search") {
  const query = firstString(obj, ["query"]);
  refs.push({ kind: "memory_file", title: query ? `Memory search: ${truncate(query, 80)}` : "Memory search", displayPath: "memory/search", role: "memory", action: "consulted", source: "tool_call", confidence: "observed", sensitive: true });
 }

 if (name === "image_generate") {
  const filename = firstString(obj, ["filename"]);
  refs.push({ kind: "artifact", title: filename ?? "Generated image", displayPath: filename ? sanitizeDisplayPath(filename) : "generated-image", role: "artifact", action: "generated", source: "tool_call", confidence: "observed" });
 }

 return refs.filter(Boolean);
}

function refsFromToolResult(toolName: string | undefined, details: unknown, content: unknown): DocumentReference[] {
 const name = toolName ?? "";
 const refs: DocumentReference[] = [];
 if (isRecord(details)) {
  const path = firstString(details, ["path", "file_path", "filePath", "file"]);
  const url = firstString(details, ["url"]);
  if (path) refs.push(refFromPath(path, { source: "tool_result", confidence: "observed", action: name === "read" ? "read" : "used" }));
  if (url) refs.push(refFromUrl(url, { source: "tool_result", confidence: "observed", action: "read" }));
 }

 if (simpleToolName(name) === "read") {
  const text = contentToText(content);
  const maybePath = text.match(/^(?:#\s*)?(?:Path|File):\s*`?([^`\n]+)`?/im)?.[1]?.trim();
  if (maybePath) refs.push(refFromPath(maybePath, { source: "tool_result", confidence: "observed", action: "read" }));
 }
 return refs;
}


function extractPatchPaths(patch: string): string[] {
 const paths: string[] = [];
 for (const line of patch.split(/\r?\n/)) {
  const match = line.match(/^\*\*\*\s+(?:Update File|Add File|Delete File):\s+(.+)$/);
  if (match?.[1]) paths.push(match[1].trim());
 }
 return unique(paths);
}

function simpleToolName(name: string): string {
 return name.includes(".") ? name.split(".").pop() ?? name : name;
}

function parsePromptContextReferences(content: unknown): DocumentReference[] {
 const text = contentToText(content);
 if (!text) return [];
 const refs: DocumentReference[] = [];
 const pathHeadingRe = /^##\s+([^\n]+)$/gm;
 for (const match of text.matchAll(pathHeadingRe)) {
  const candidate = match[1]?.trim();
  if (!candidate || !candidate.includes("/")) continue;
  refs.push(refFromPath(candidate, { source: "prompt_context", confidence: "injected", action: "consulted" }));
 }
 return refs;
}

function parseAgentDeclaredReferences(markdown: string): DocumentReference[] {
 const sections = [
  { label: "Knowledge used", role: "knowledge" as const, action: "used" as const },
  { label: "Routed to", role: "routing" as const, action: "routed_to" as const },
  { label: "Rules applied", role: "rule" as const, action: "used" as const },
  { label: "Evidence", role: "evidence" as const, action: "used" as const },
 ];
 const refs: DocumentReference[] = [];
 for (const section of sections) {
  const escaped = section.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(?:^|\\n)#{0,4}\\s*(?:\\*\\*)?${escaped}(?:\\*\\*)?\\s*:?\\s*\\n([\\s\\S]*?)(?=\\n#{1,4}\\s|\\n\\*\\*[^\\n]+\\*\\*\\s*:?\\s*\\n|\\n[A-Z][A-Za-z ]{2,40}:\\s*\\n|$)`, "i");
  const body = markdown.match(re)?.[1];
  if (!body) continue;
  for (const line of body.split(/\r?\n/).slice(0, 20)) {
   const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.+)$/)?.[1]?.trim();
   if (!item) continue;
   const token = extractReferenceToken(item);
   if (!token) continue;
   const base = token.startsWith("http://") || token.startsWith("https://")
    ? refFromUrl(token, { source: "parsed_markdown", confidence: "agent_declared", action: section.action })
    : refFromPath(token, { source: "parsed_markdown", confidence: "agent_declared", action: section.action });
   refs.push({ ...base, role: section.role });
  }
 }
 return refs;
}

function extractReferenceToken(text: string): string | undefined {
 const backtick = text.match(/`([^`]+)`/)?.[1]?.trim();
 if (backtick) return backtick;
 const url = text.match(/https?:\/\/[^\s)]+/)?.[0];
 if (url) return url;
 const pathy = text.match(/(?:[\w.-]+\/)+[\w .@()[\]-]+\.[A-Za-z0-9]+|[A-Z_]+\.md|SKILL\.md|WORKFLOWS?\.md|MEMORY\.md/i)?.[0];
 if (pathy) return pathy.trim();
 const words = text.replace(/[*_]/g, "").trim();
 return words ? truncate(words, 80) : undefined;
}

function refFromUrl(url: string, opts: Pick<DocumentReference, "source" | "confidence" | "action">): DocumentReference {
 let title = url;
 let displayPath = url;
 try {
  const parsed = new URL(url);
  title = parsed.hostname;
  displayPath = `${parsed.hostname}${parsed.pathname === "/" ? "" : parsed.pathname}`;
 } catch {}
 return { kind: "web", title, displayPath: sanitizeDisplayPath(displayPath), uri: url, role: "evidence", ...opts };
}

function refFromPath(rawPath: string, opts: Pick<DocumentReference, "source" | "confidence" | "action">): DocumentReference {
 const displayPath = safeDisplayPathForRawPath(rawPath);
 const classification = classifyDisplayPath(displayPath);
 return {
  ...classification,
  title: basename(displayPath) || displayPath,
  displayPath,
  uri: displayPath ? `openclaw://${displayPath}` : undefined,
  mimeType: inferMimeType(displayPath),
  ...opts,
 };
}

function classifyDisplayPath(displayPath: string): Pick<DocumentReference, "kind" | "role" | "sensitive" | "redacted"> {
 const normalized = displayPath.replace(/\\/g, "/");
 const base = basename(normalized);
 if (normalized.startsWith("memory/") || base === "MEMORY.md" || /^memory\/search$/i.test(normalized)) return { kind: "memory_file", role: "memory", sensitive: true };
 if (normalized.startsWith("skills/") || /(^|\/)SKILL\.md$/i.test(normalized)) return { kind: "skill", role: "routing" };
 if (normalized.startsWith("workflows/") || /WORKFLOWS?\.md$/i.test(base)) return { kind: "workflow", role: "routing" };
 if (normalized.startsWith("library/")) return { kind: "library_doc", role: "knowledge" };
 if (SYSTEM_DOC_NAMES.has(base)) return { kind: "system_doc", role: "rule", sensitive: true };
 if (normalized === "patch") return { kind: "artifact", role: "artifact" };
 return { kind: "workspace_file", role: "knowledge" };
}

function safeDisplayPathForRawPath(rawPath: string): string {
 const path = rawPath.trim().replace(/^file:\/\//, "").replace(/\\/g, "/");
 if (!path) return "unknown";
 if (/^https?:\/\//i.test(path)) return path;

 const home = homedir().replace(/\\/g, "/");
 const mappings: Array<[string, string]> = [
  [`${home}/.openclaw/workspace/memory/`, "memory/"],
  [`${home}/.openclaw/workspace/skills/`, "skills/"],
  [`${home}/.openclaw/workspace/workflows/`, "workflows/"],
  [`${home}/.openclaw/workspace/`, ""],
  [`${home}/.openclaw/library/`, "library/"],
  [`${home}/.agents/skills/`, "skills/"],
  [`${home}/.openclaw/workspace/skills/`, "skills/"],
 ];
 for (const [prefix, replacement] of mappings) {
  if (!path.startsWith(prefix)) continue;
  const rel = sanitizeDisplayPath(`${replacement}${path.slice(prefix.length)}`);
  if (rel === "MEMORY.md") return "memory/MEMORY.md";
  return rel;
 }

 const skillMatch = path.match(/(?:^|\/)skills\/([^/]+)\/SKILL\.md$/i);
 if (skillMatch) return `skills/${skillMatch[1]}/SKILL.md`;

 const workflowIndex = path.toLowerCase().lastIndexOf("/workflows/");
 if (workflowIndex >= 0) return sanitizeDisplayPath(`workflows/${path.slice(workflowIndex + "/workflows/".length)}`);

 const memoryIndex = path.toLowerCase().lastIndexOf("/memory/");
 if (memoryIndex >= 0) return sanitizeDisplayPath(`memory/${path.slice(memoryIndex + "/memory/".length)}`);

 if (path.startsWith("/")) {
  const parts = path.split("/").filter(Boolean);
  const tail = parts.slice(-3).join("/");
  return sanitizeDisplayPath(tail || basename(path));
 }
 return sanitizeDisplayPath(path);
}

function sanitizeDisplayPath(input: string): string {
 const normalized = input.replace(/\\/g, "/").replace(/^~\//, "").replace(/^\/+/, "");
 const parts = normalized.split("/").filter((part) => part && part !== "." && part !== "..");
 return parts.join("/") || basename(normalized) || "unknown";
}

function inferMimeType(path: string): string | undefined {
 const ext = extname(path).toLowerCase();
 if (MARKDOWN_EXTS.has(ext)) return "text/markdown";
 if (TEXT_EXTS.has(ext)) return "text/plain";
 return undefined;
}

function contentToText(content: unknown): string {
 if (typeof content === "string") return content;
 if (!Array.isArray(content)) return "";
 return content.map((item) => {
  if (typeof item === "string") return item;
  if (isRecord(item) && typeof item.text === "string") return item.text;
  return "";
 }).filter(Boolean).join("\n");
}

function firstString(obj: Record<string, unknown>, keys: string[]): string | undefined {
 for (const key of keys) {
  const value = obj[key];
  if (typeof value === "string" && value.trim()) return value.trim();
 }
 return undefined;
}

function stringOrUndefined(value: unknown): string | undefined {
 return typeof value === "string" ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
 return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function unique(values: string[]): string[] {
 return [...new Set(values.filter(Boolean))];
}

function truncate(value: string, max: number): string {
 return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}
