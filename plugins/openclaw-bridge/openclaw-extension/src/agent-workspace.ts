/**
 * Agent workspace handler — mirrors library.ts in approach.
 *
 * Workspace path resolution is done entirely locally:
 *   1. Check openclaw.json agents.list[].workspace for an explicit path.
 *   2. For the default agent ("main") with no explicit workspace, fall back
 *      to ~/.openclaw/workspace (same logic as resolveAgentWorkspaceDir).
 *   3. For any other slug with no explicit entry, fall back to
 *      ~/.openclaw/workspace-<slug>.
 *
 * No subprocess, no gateway RPC, no network. Same reliability as library.ts.
 */

import {
 readdirSync,
 readFileSync,
 writeFileSync,
 mkdirSync,
 unlinkSync,
 rmSync,
 statSync,
 existsSync,
} from "node:fs";
import { basename, join, normalize, relative, resolve, dirname } from "node:path";
import { homedir } from "node:os";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type WsSend = (data: string) => void;

type LogSink = {
 info?: (message: string, ...args: unknown[]) => void;
 warn?: (message: string, ...args: unknown[]) => void;
 error?: (message: string, ...args: unknown[]) => void;
};

export type AgentWorkspaceListRequest = {
 requestId: string;
 agentId: string;
 folder?: string;
};

export type AgentWorkspaceReadRequest = {
 requestId: string;
 agentId: string;
 folder?: string;
 filename: string;
};

export type AgentWorkspaceWriteRequest = {
 requestId: string;
 agentId: string;
 folder?: string;
 files: FilePayload[];
};

type FilePayload = {
 filename: string;
 content: string;
 contentEncoding?: "utf8" | "base64";
 contentType?: string;
};

export type AgentWorkspaceDeleteRequest = {
 requestId: string;
 agentId: string;
 folder?: string;
 filename?: string;
};

// ---------------------------------------------------------------------------
// Workspace path resolution — pure local, no RPC
// ---------------------------------------------------------------------------

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Resolve the real filesystem workspace root for a given agent slug.
 *
 * Resolution order (mirrors OpenClaw's resolveAgentWorkspaceDir):
 *   1. openclaw.json agents.list[].workspace  — explicit, highest priority
 *   2. openclaw.json agents.defaults.workspace — default fallback
 *   3. "main" / default agent                 — ~/.openclaw/workspace
 *   4. Any other slug                         — ~/.openclaw/workspace-<slug>
 */
export function resolveWorkspaceRoot(cfg: OpenClawConfig, agentSlug: string): string {
 const home = homedir();
 const agents = (cfg as { agents?: { list?: Array<{ id: string; workspace?: string }>; defaults?: { workspace?: string } } }).agents;

 // 1. Explicit workspace in agents.list
 if (agents?.list && Array.isArray(agents.list)) {
  const entry = agents.list.find((a) => a.id === agentSlug);
  if (entry?.workspace?.trim()) {
   return entry.workspace.trim().replace(/^~/, home);
  }
 }

 // 2. Is this the default/main agent?
 // "main" is the built-in default slug; also check if it's the only agent
 if (agentSlug === "main" || agentSlug === "default") {
  // Check agents.defaults.workspace first
  const defaultWorkspace = agents?.defaults?.workspace?.trim();
  if (defaultWorkspace) return defaultWorkspace.replace(/^~/, home);
  return join(home, ".openclaw", "workspace");
 }

 // 3. Fallback: ~/.openclaw/workspace-<slug>
 return join(home, ".openclaw", `workspace-${agentSlug}`);
}

/**
 * Resolve a ClawChat agentId to an OpenClaw agent slug.
 *
 * ClawChat should be sending the externalId (slug), but historically sent
 * internal UUIDs. If a UUID arrives, scan workspace-state.json files for a
 * matching clawchatAgentId. If still not found, throw a clear error.
 */
function resolveAgentSlug(cfg: OpenClawConfig, rawAgentId: string, log?: LogSink): string {
 const trimmed = rawAgentId.trim();

 // Already a slug — fast path
 if (!UUID_RE.test(trimmed)) return trimmed;

 log?.warn?.(`[clawchat:workspace] received UUID agentId "${trimmed}" — scanning workspace-state files`);

 const home = homedir();
 const agents = (cfg as { agents?: { list?: Array<{ id: string }> } }).agents;
 const agentList: Array<{ id: string }> = agents?.list ?? [];

 for (const agent of agentList) {
  if (!agent.id) continue;
  const wsRoot = resolveWorkspaceRoot(cfg, agent.id);
  const statePath = join(wsRoot, ".openclaw", "workspace-state.json");
  if (existsSync(statePath)) {
   try {
    const state = JSON.parse(readFileSync(statePath, "utf-8")) as Record<string, unknown>;
    const stored = state?.clawchatAgentId ?? state?.externalId ?? state?.clawchatId;
    if (stored === trimmed) {
     log?.info?.(`[clawchat:workspace] resolved UUID "${trimmed}" → slug "${agent.id}"`);
     return agent.id;
    }
   } catch { /* ignore */ }
  }
 }

 // Last resort: check if workspace-<uuid> happens to exist (shouldn't, but defensive)
 const directPath = join(home, ".openclaw", `workspace-${trimmed}`);
 if (existsSync(directPath)) {
  log?.warn?.(`[clawchat:workspace] found direct workspace for UUID "${trimmed}" — using as slug`);
  return trimmed;
 }

 const knownSlugs = agentList.map((a) => a.id).join(", ");
 throw new Error(
  `Cannot resolve agentId "${trimmed}" to an OpenClaw slug. ` +
  `Known slugs: [${knownSlugs}]. ` +
  `If this agent was recently provisioned, it may not be registered in openclaw.json yet.`,
 );
}

// ---------------------------------------------------------------------------
// Path safety
// ---------------------------------------------------------------------------

/**
 * Resolve a subpath inside a workspace root and block escape attempts.
 * Identical logic to library.ts safePath.
 */
function safePath(workspaceRoot: string, subpath: string): string {
 const absRoot = resolve(workspaceRoot);
 const resolved = resolve(normalize(join(absRoot, subpath)));
 const rel = relative(absRoot, resolved);
 if (rel.startsWith("..") || !resolved.startsWith(absRoot)) {
  throw new Error(`Path escape attempt blocked: "${subpath}"`);
 }
 return resolved;
}

function safeFilename(filename: string): string {
 const normalized = filename.replace(/\\/g, "/");
 const name = basename(normalized);
 if (!name || name === "." || name === "..") {
  throw new Error(`Invalid filename: "${filename}"`);
 }
 if (name !== normalized) {
  throw new Error(`Path separators are not allowed in filename: "${filename}"`);
 }
 return name;
}

function fileContentBuffer(file: FilePayload): Buffer | string {
 const encoding = file.contentEncoding ?? "utf8";
 if (encoding === "base64") {
  return Buffer.from(file.content, "base64");
 }
 if (encoding !== "utf8") {
  throw new Error(`Unsupported contentEncoding for ${file.filename}: ${String(encoding)}`);
 }
 return file.content;
}

// ---------------------------------------------------------------------------
// Response helpers
// ---------------------------------------------------------------------------

function sendResult(ws: WsSend, type: string, requestId: string, data: Record<string, unknown>): void {
 ws(JSON.stringify({ type, data: { requestId, ...data } }));
}

function sendError(ws: WsSend, requestId: string, error: string): void {
 ws(JSON.stringify({ type: "agent.workspace.error", data: { requestId, error } }));
}

// ---------------------------------------------------------------------------
// Handlers — all pure filesystem, no RPC, same as library.ts
// ---------------------------------------------------------------------------

/**
 * agent.workspace.list
 * Lists a folder (or workspace root) for a given agent.
 */
export function handleAgentWorkspaceList(
 ws: WsSend,
 req: AgentWorkspaceListRequest,
 cfg: OpenClawConfig,
 log?: LogSink,
): void {
 const { requestId } = req;
 const folder = req.folder?.trim() || "";

 try {
  const agentSlug = resolveAgentSlug(cfg, req.agentId, log);
  const workspaceRoot = resolveWorkspaceRoot(cfg, agentSlug);
  const targetPath = folder ? safePath(workspaceRoot, folder) : resolve(workspaceRoot);

  log?.info?.(`[clawchat:workspace] list: agent="${agentSlug}" workspace="${workspaceRoot}" folder="${folder || "/"}"`);

  if (!existsSync(targetPath)) {
   sendResult(ws, "agent.workspace.list.result", requestId, {
    agentId: req.agentId,
    workspace: workspaceRoot,
    folder,
    folders: [],
    files: [],
   });
   return;
  }

  const dirents = readdirSync(targetPath, { withFileTypes: true });

  const folders = dirents
   .filter((e) => e.isDirectory())
   .map((e) => ({
    name: e.name,
    path: folder ? `${folder}/${e.name}` : e.name,
   }));

  const files = dirents
   .filter((e) => e.isFile())
   .map((e) => {
    const fullPath = join(targetPath, e.name);
    const st = statSync(fullPath);
    return {
     filename: e.name,
     path: folder ? `${folder}/${e.name}` : e.name,
     size: st.size,
     updatedAt: new Date(st.mtimeMs).toISOString(),
    };
   });

  sendResult(ws, "agent.workspace.list.result", requestId, {
   agentId: req.agentId,
   workspace: workspaceRoot,
   folder,
   folders,
   files,
  });

  log?.info?.(`[clawchat:workspace] list result: ${folders.length} folders, ${files.length} files`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, requestId, msg);
  log?.error?.(`[clawchat:workspace] list error (agent="${req.agentId}"): ${msg}`);
 }
}

/**
 * agent.workspace.read
 * Reads a single file from an agent workspace.
 */
export function handleAgentWorkspaceRead(
 ws: WsSend,
 req: AgentWorkspaceReadRequest,
 cfg: OpenClawConfig,
 log?: LogSink,
): void {
 const { requestId, filename } = req;
 const folder = req.folder?.trim() || "";

 try {
  const agentSlug = resolveAgentSlug(cfg, req.agentId, log);
  const workspaceRoot = resolveWorkspaceRoot(cfg, agentSlug);
  const filePath = safePath(workspaceRoot, folder ? join(folder, filename) : filename);

  if (!existsSync(filePath)) {
   sendError(ws, requestId, `File not found: ${folder ? folder + "/" : ""}${filename}`);
   return;
  }

  const content = readFileSync(filePath, "utf-8");
  const st = statSync(filePath);

  sendResult(ws, "agent.workspace.read.result", requestId, {
   agentId: req.agentId,
   workspace: workspaceRoot,
   folder,
   filename,
   content,
   size: st.size,
   updatedAt: new Date(st.mtimeMs).toISOString(),
  });

  log?.info?.(`[clawchat:workspace] read: agent="${agentSlug}" file="${folder ? folder + "/" : ""}${filename}" (${st.size} bytes)`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, requestId, msg);
  log?.error?.(`[clawchat:workspace] read error (agent="${req.agentId}" file="${folder ? folder + "/" : ""}${filename}"): ${msg}`);
 }
}

/**
 * agent.workspace.write
 * Writes one or more files into an agent workspace folder.
 */
export function handleAgentWorkspaceWrite(
 ws: WsSend,
 req: AgentWorkspaceWriteRequest,
 cfg: OpenClawConfig,
 log?: LogSink,
): void {
 const { requestId, files } = req;
 const folder = req.folder?.trim() || "";

 try {
  const agentSlug = resolveAgentSlug(cfg, req.agentId, log);
  const workspaceRoot = resolveWorkspaceRoot(cfg, agentSlug);

  let createdFolder = false;

  if (folder) {
   const folderPath = safePath(workspaceRoot, folder);
   if (!existsSync(folderPath)) {
    mkdirSync(folderPath, { recursive: true });
    createdFolder = true;
   }
  }

  const written: string[] = [];
  for (const file of files) {
   const filename = safeFilename(file.filename);
   const relPath = folder ? join(folder, filename) : filename;
   const filePath = safePath(workspaceRoot, relPath);
   mkdirSync(dirname(filePath), { recursive: true });
   const content = fileContentBuffer(file);
   if (Buffer.isBuffer(content)) {
    writeFileSync(filePath, content);
   } else {
    writeFileSync(filePath, content, "utf-8");
   }
   written.push(filename);
  }

  sendResult(ws, "agent.workspace.write.result", requestId, {
   agentId: req.agentId,
   workspace: workspaceRoot,
   folder,
   written,
   createdFolder,
  });

  log?.info?.(`[clawchat:workspace] write: agent="${agentSlug}" folder="${folder || "/"}" → ${written.length} file(s): [${written.join(", ")}]`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, requestId, msg);
  log?.error?.(`[clawchat:workspace] write error (agent="${req.agentId}"): ${msg}`);
 }
}

/**
 * agent.workspace.delete
 * Deletes a file or entire folder from an agent workspace.
 */
export function handleAgentWorkspaceDelete(
 ws: WsSend,
 req: AgentWorkspaceDeleteRequest,
 cfg: OpenClawConfig,
 log?: LogSink,
): void {
 const { requestId, filename } = req;
 const folder = req.folder?.trim() || "";

 try {
  const agentSlug = resolveAgentSlug(cfg, req.agentId, log);
  const workspaceRoot = resolveWorkspaceRoot(cfg, agentSlug);

  if (filename) {
   const filePath = safePath(workspaceRoot, folder ? join(folder, filename) : filename);

   if (!existsSync(filePath)) {
    sendError(ws, requestId, `File not found: ${folder ? folder + "/" : ""}${filename}`);
    return;
   }

   unlinkSync(filePath);

   sendResult(ws, "agent.workspace.delete.result", requestId, {
    agentId: req.agentId,
    workspace: workspaceRoot,
    folder,
    deleted: filename,
    type: "file",
   });

   log?.info?.(`[clawchat:workspace] delete: agent="${agentSlug}" file="${folder ? folder + "/" : ""}${filename}"`);
  } else if (folder) {
   const folderPath = safePath(workspaceRoot, folder);

   if (!existsSync(folderPath)) {
    sendError(ws, requestId, `Folder not found: ${folder}`);
    return;
   }

   rmSync(folderPath, { recursive: true });

   sendResult(ws, "agent.workspace.delete.result", requestId, {
    agentId: req.agentId,
    workspace: workspaceRoot,
    folder,
    deleted: folder,
    type: "folder",
   });

   log?.info?.(`[clawchat:workspace] delete: agent="${agentSlug}" folder="${folder}"`);
  } else {
   sendError(ws, requestId, "Delete requires either a filename or a folder");
  }
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, requestId, msg);
  log?.error?.(`[clawchat:workspace] delete error (agent="${req.agentId}"): ${msg}`);
 }
}
