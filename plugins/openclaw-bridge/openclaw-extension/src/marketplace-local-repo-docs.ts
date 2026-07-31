import {
 existsSync,
 readdirSync,
 readFileSync,
 realpathSync,
 statSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { isAbsolute, join, normalize, relative, resolve } from "node:path";
import { spawnSync } from "node:child_process";

type WsSend = (data: string) => void;

type LogSink = {
 info?: (message: string, ...args: unknown[]) => void;
 warn?: (message: string, ...args: unknown[]) => void;
 error?: (message: string, ...args: unknown[]) => void;
};

export type MarketplaceReadLocalRepoDocsRequest = {
 requestId: string;
 workspaceId?: string;
 sourceHostId?: string;
 bridgeDeviceId?: string;
 repoPath: string;
 docsSourcePath?: string;
 includeGlobs?: string[];
};

type LocalRepoDocsFile = {
 relativePath: string;
 content: string;
 sha256: string;
 sizeBytes: number;
};

const DEFAULT_DOCS_SOURCE_PATH = ".clawchat/";
const RESULT_TYPE = "marketplace.readLocalRepoDocs.result";

const DEFAULT_INCLUDE_GLOBS = [
 ".clawchat/app_manifest.json",
 ".clawchat/clawchat.config.json",
 ".clawchat/api/openapi.json",
 ".clawchat/api/endpoints.md",
 ".clawchat/agent-docs-source/**/*.md",
] as const;

const STATIC_DOC_FILES = [
 "app_manifest.json",
 "clawchat.config.json",
 "api/openapi.json",
 "api/endpoints.md",
] as const;

const SECRET_KEY_RE = /(?:^|[_-])(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|passwd|private[_-]?key|client[_-]?secret|webhook[_-]?secret)(?:$|[_-])/i;
const PRIVATE_KEY_BLOCK_RE = /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g;
const BEARER_RE = /\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})\b/g;
const ASSIGNMENT_SECRET_RE = /\b([A-Za-z0-9_.-]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|ID[_-]?TOKEN|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|WEBHOOK[_-]?SECRET)[A-Za-z0-9_.-]*)\s*([:=])\s*(['"]?)([^<>'"\s,}\]]{4,})(\3)/gi;
const JSON_SECRET_VALUE = "[REDACTED_SECRET_VALUE]";

function sendResult(ws: WsSend, requestId: string, data: Record<string, unknown>): void {
 ws(JSON.stringify({ type: RESULT_TYPE, data: { requestId, ...data } }));
}

function normalizeSlash(value: string): string {
 return value.replace(/\\/g, "/").replace(/^\/+/, "");
}

function trimTrailingSlash(value: string): string {
 return value.replace(/[\\/]+$/, "");
}

function isInside(root: string, candidate: string): boolean {
 const rel = relative(root, candidate);
 return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function resolveRepoRoot(repoPath: string): string {
 if (typeof repoPath !== "string" || !repoPath.trim()) {
  throw new Error("repoPath is required");
 }
 const root = realpathSync(resolve(repoPath.trim()));
 const st = statSync(root);
 if (!st.isDirectory()) throw new Error(`repoPath is not a directory: ${repoPath}`);
 return root;
}

function resolveDocsRoot(repoRoot: string, docsSourcePath: string | undefined): { docsSourcePath: string; docsRoot: string } {
 const raw = docsSourcePath?.trim() || DEFAULT_DOCS_SOURCE_PATH;
 if (isAbsolute(raw)) {
  throw new Error("docsSourcePath must be relative to repoPath");
 }
 const normalized = normalizeSlash(trimTrailingSlash(normalize(raw)));
 if (!normalized || normalized === "." || normalized.startsWith("../") || normalized === "..") {
  throw new Error(`Invalid docsSourcePath: ${raw}`);
 }
 const docsRoot = resolve(repoRoot, normalized);
 if (!isInside(repoRoot, docsRoot)) {
  throw new Error(`docsSourcePath escapes repoPath: ${raw}`);
 }
 return { docsSourcePath: `${normalized}/`, docsRoot };
}

function allowedRelativePathFromInclude(glob: string, docsSourcePath: string): string | null {
 const normalized = normalizeSlash(normalize(glob.trim()));
 const docsPrefix = normalizeSlash(trimTrailingSlash(docsSourcePath));
 if (normalized === docsPrefix) return null;
 if (normalized.startsWith(`${docsPrefix}/`)) return normalized.slice(docsPrefix.length + 1);
 if (normalized.startsWith(".clawchat/")) return normalized.slice(".clawchat/".length);
 return normalized;
}

function wantsMarkdownDocs(includeGlobs: string[], docsSourcePath: string): boolean {
 return includeGlobs.some((glob) => {
  const rel = allowedRelativePathFromInclude(glob, docsSourcePath);
  return rel === "agent-docs-source/**/*.md" || rel === "agent-docs-source/**.md" || rel?.startsWith("agent-docs-source/");
 });
}

function requestedStaticFiles(includeGlobs: string[], docsSourcePath: string): Set<string> {
 const requested = new Set<string>();
 for (const glob of includeGlobs) {
  const rel = allowedRelativePathFromInclude(glob, docsSourcePath);
  if (!rel) continue;
  if ((STATIC_DOC_FILES as readonly string[]).includes(rel)) requested.add(rel);
 }
 return requested;
}

function listMarkdownFiles(root: string, subdir = "agent-docs-source"): string[] {
 const start = join(root, subdir);
 if (!existsSync(start)) return [];
 const out: string[] = [];
 const walk = (dir: string) => {
  for (const ent of readdirSync(dir, { withFileTypes: true })) {
   if (ent.name === "node_modules" || ent.name === ".git" || ent.name === "dist" || ent.name === "build" || ent.name === ".cache") continue;
   const full = join(dir, ent.name);
   if (ent.isDirectory()) {
    walk(full);
   } else if (ent.isFile() && ent.name.endsWith(".md")) {
    out.push(normalizeSlash(relative(root, full)));
   }
  }
 };
 walk(start);
 return out.sort();
}

function redactJsonSecrets(value: unknown, parentKey = ""): { value: unknown; redacted: boolean } {
 if (Array.isArray(value)) {
  let redacted = false;
  const next = value.map((item) => {
   const result = redactJsonSecrets(item, parentKey);
   redacted ||= result.redacted;
   return result.value;
  });
  return { value: next, redacted };
 }
 if (value && typeof value === "object") {
  let redacted = false;
  const next: Record<string, unknown> = {};
  for (const [key, child] of Object.entries(value)) {
   if (SECRET_KEY_RE.test(key) && typeof child === "string" && child.trim()) {
    next[key] = JSON_SECRET_VALUE;
    redacted = true;
    continue;
   }
   const result = redactJsonSecrets(child, key);
   next[key] = result.value;
   redacted ||= result.redacted;
  }
  return { value: next, redacted };
 }
 if (SECRET_KEY_RE.test(parentKey) && typeof value === "string" && value.trim()) {
  return { value: JSON_SECRET_VALUE, redacted: true };
 }
 return { value, redacted: false };
}

function redactSecretLookingValues(content: string, relativePath: string): { content: string; redacted: boolean } {
 let redacted = false;
 let next = content;
 if (relativePath.endsWith(".json")) {
  try {
   const parsed = JSON.parse(content) as unknown;
   const result = redactJsonSecrets(parsed);
   if (result.redacted) {
    redacted = true;
    next = `${JSON.stringify(result.value, null, 2)}\n`;
   }
  } catch {
   // Fall through to text redaction for malformed JSON rather than returning raw secret-looking values.
  }
 }
 next = next.replace(PRIVATE_KEY_BLOCK_RE, () => {
  redacted = true;
  return "[REDACTED_PRIVATE_KEY]";
 });
 next = next.replace(BEARER_RE, (_match, prefix) => {
  redacted = true;
  return `${prefix}[REDACTED_BEARER_TOKEN]`;
 });
 next = next.replace(ASSIGNMENT_SECRET_RE, (_match, key, sep, quote) => {
  redacted = true;
  return `${key}${sep}${quote}[REDACTED_SECRET_VALUE]${quote}`;
 });
 return { content: next, redacted };
}

function sha256(content: string): string {
 return createHash("sha256").update(content, "utf8").digest("hex");
}

function readAllowedFile(repoRoot: string, docsRoot: string, relativePath: string): { file?: LocalRepoDocsFile; error?: string; redacted?: boolean } {
 const normalizedRel = normalizeSlash(normalize(relativePath));
 if (normalizedRel.startsWith("../") || normalizedRel === ".." || normalizedRel.startsWith("/") || normalizedRel.includes("\0")) {
  return { error: `Blocked unsafe relative path: ${relativePath}` };
 }
 const filePath = resolve(docsRoot, normalizedRel);
 if (!isInside(docsRoot, filePath) || !isInside(repoRoot, filePath)) {
  return { error: `Blocked path escape for ${relativePath}` };
 }
 if (!existsSync(filePath)) return {};
 const real = realpathSync(filePath);
 if (!isInside(docsRoot, real) || !isInside(repoRoot, real)) {
  return { error: `Blocked symlink escape for ${relativePath}` };
 }
 const st = statSync(real);
 if (!st.isFile()) return { error: `Not a regular file: ${relativePath}` };
 if (st.size > 1_000_000) return { error: `File too large to return safely: ${relativePath}` };
 const raw = readFileSync(real, "utf8");
 const redacted = redactSecretLookingValues(raw, normalizedRel);
 return {
  redacted: redacted.redacted,
  file: {
   relativePath: normalizedRel,
   content: redacted.content,
   sha256: sha256(redacted.content),
   sizeBytes: Buffer.byteLength(redacted.content, "utf8"),
  },
 };
}

function gitValue(repoRoot: string, args: string[]): string | null {
 const result = spawnSync("git", args, {
  cwd: repoRoot,
  encoding: "utf8",
  timeout: 5_000,
  stdio: ["ignore", "pipe", "ignore"],
 });
 if (result.status !== 0) return null;
 const value = result.stdout.trim();
 return value || null;
}

function gitDirtyState(repoRoot: string): string | null {
 const porcelain = gitValue(repoRoot, ["status", "--short"]);
 if (porcelain == null) return null;
 if (!porcelain) return "clean";
 return "dirty";
}

export function handleMarketplaceReadLocalRepoDocs(
 ws: WsSend,
 req: MarketplaceReadLocalRepoDocsRequest,
 log?: LogSink,
): void {
 const requestId = typeof req.requestId === "string" && req.requestId.trim() ? req.requestId : "<missing>";
 try {
  const repoRoot = resolveRepoRoot(req.repoPath);
  const { docsSourcePath, docsRoot } = resolveDocsRoot(repoRoot, req.docsSourcePath);
  const includeGlobs = Array.isArray(req.includeGlobs) && req.includeGlobs.length > 0
   ? req.includeGlobs.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
   : [...DEFAULT_INCLUDE_GLOBS];

  const gitCommit = gitValue(repoRoot, ["rev-parse", "HEAD"]);
  const dirtyState = gitDirtyState(repoRoot);

  if (!existsSync(docsRoot)) {
   sendResult(ws, requestId, {
    status: "not_found",
    repoPath: repoRoot,
    docsSourcePath,
    files: [],
    missingFiles: [...STATIC_DOC_FILES],
    errors: [`Docs source path not found: ${docsSourcePath}`],
    gitCommit,
    dirtyState,
   });
   return;
  }

  const realDocsRoot = realpathSync(docsRoot);
  if (!isInside(repoRoot, realDocsRoot)) {
   throw new Error(`docsSourcePath resolves outside repoPath: ${docsSourcePath}`);
  }

  const requested = requestedStaticFiles(includeGlobs, docsSourcePath);
  const candidates = new Set<string>(requested);
  if (wantsMarkdownDocs(includeGlobs, docsSourcePath)) {
   for (const file of listMarkdownFiles(realDocsRoot)) candidates.add(file);
  }

  const files: LocalRepoDocsFile[] = [];
  const missingFiles: string[] = [];
  const errors: string[] = [];
  for (const file of candidates) {
   const result = readAllowedFile(repoRoot, realDocsRoot, file);
   if (result.error) {
    errors.push(result.error);
    continue;
   }
   if (!result.file) {
    if ((STATIC_DOC_FILES as readonly string[]).includes(file)) missingFiles.push(file);
    continue;
   }
   if (result.redacted) errors.push(`Redacted secret-looking value(s) in ${file}`);
   files.push(result.file);
  }

  sendResult(ws, requestId, {
   status: files.length > 0 ? "ok" : "not_found",
   repoPath: repoRoot,
   docsSourcePath,
   files,
   missingFiles,
   errors,
   gitCommit,
   dirtyState,
  });
  log?.info?.(`[clawchat] marketplace.readLocalRepoDocs repo="${repoRoot}" docs="${docsSourcePath}" files=${files.length} missing=${missingFiles.length} errors=${errors.length}`);
 } catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  sendResult(ws, requestId, {
   status: "failed",
   repoPath: typeof req.repoPath === "string" ? req.repoPath : null,
   docsSourcePath: req.docsSourcePath || DEFAULT_DOCS_SOURCE_PATH,
   files: [],
   missingFiles: [],
   errors: [message],
   gitCommit: null,
   dirtyState: null,
  });
  log?.error?.(`[clawchat] marketplace.readLocalRepoDocs error: ${message}`);
 }
}
