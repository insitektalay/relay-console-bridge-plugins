import { readdirSync, readFileSync, writeFileSync, mkdirSync, unlinkSync, rmSync, statSync, existsSync } from "node:fs";
import { basename, dirname, join, normalize, relative, resolve } from "node:path";
import { homedir } from "node:os";

const LIBRARY_ROOT = join(homedir(), ".openclaw", "library");

/** Ensure a path stays within the library root. Throws on escape attempts. */
function safePath(subpath: string): string {
 const absRoot = resolve(LIBRARY_ROOT);
 const resolved = resolve(normalize(join(absRoot, subpath)));
 const rel = relative(absRoot, resolved);
 if (rel.startsWith("..") || !resolved.startsWith(absRoot)) {
  throw new Error(`Path escape attempt blocked: "${subpath}"`);
 }
 return resolved;
}

type LibraryListRequest = {
 requestId: string;
 folder?: string;
};

type LibraryReadRequest = {
 requestId: string;
 folder: string;
 filename: string;
};

type LibraryWriteRequest = {
 requestId: string;
 folder: string;
 files: FilePayload[];
};

type FilePayload = {
 filename: string;
 content: string;
 contentEncoding?: "utf8" | "base64";
 contentType?: string;
};

type LibraryDeleteRequest = {
 requestId: string;
 folder: string;
 filename?: string;
};

type WsSend = (data: string) => void;

function sendResult(ws: WsSend, type: string, requestId: string, data: Record<string, unknown>): void {
 ws(JSON.stringify({ type, data: { requestId, ...data } }));
}

function sendError(ws: WsSend, requestId: string, error: string): void {
 ws(JSON.stringify({ type: "library.error", data: { requestId, error } }));
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

/** List folders at root, or files within a folder. */
export function handleLibraryList(ws: WsSend, req: LibraryListRequest, log?: Record<string, Function>): void {
 try {
  mkdirSync(LIBRARY_ROOT, { recursive: true });

  const folder = req.folder?.trim() || "";
  const targetPath = folder ? safePath(folder) : LIBRARY_ROOT;

  if (!existsSync(targetPath)) {
   sendResult(ws, "library.list.result", req.requestId, { folder, entries: [] });
   return;
  }

  const dirents = readdirSync(targetPath, { withFileTypes: true });

  const entries = dirents.map((ent) => ({
   name: ent.name,
   type: ent.isDirectory() ? "folder" as const : "file" as const,
   size: ent.isFile() ? statSync(join(targetPath, ent.name)).size : undefined,
  }));

  const folders = dirents.filter((e) => e.isDirectory()).map((e) => ({
   name: e.name,
   path: folder ? `${folder}/${e.name}` : e.name,
  }));

  const files = dirents.filter((e) => e.isFile()).map((e) => ({
   filename: e.name,
   path: folder ? `${folder}/${e.name}` : e.name,
   size: statSync(join(targetPath, e.name)).size,
  }));

  sendResult(ws, "library.list.result", req.requestId, { folder, folders, files, entries });
  log?.info?.(`[clawchat] library.list: ${folder || "/"} → ${entries.length} entries`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, req.requestId, msg);
  log?.error?.(`[clawchat] library.list error: ${msg}`);
 }
}

/** Read a single file's contents. */
export function handleLibraryRead(ws: WsSend, req: LibraryReadRequest, log?: Record<string, Function>): void {
 try {
  const filePath = safePath(join(req.folder, req.filename));

  if (!existsSync(filePath)) {
   sendError(ws, req.requestId, `File not found: ${req.folder}/${req.filename}`);
   return;
  }

  const content = readFileSync(filePath, "utf-8");
  sendResult(ws, "library.read.result", req.requestId, {
   folder: req.folder,
   filename: req.filename,
   content,
  });
  log?.info?.(`[clawchat] library.read: ${req.folder}/${req.filename} (${content.length} chars)`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, req.requestId, msg);
  log?.error?.(`[clawchat] library.read error: ${msg}`);
 }
}

/** Write files into a folder (creates folder if needed). */
export function handleLibraryWrite(ws: WsSend, req: LibraryWriteRequest, log?: Record<string, Function>): void {
 try {
  const folderPath = safePath(req.folder);
  const createdFolder = !existsSync(folderPath);
  mkdirSync(folderPath, { recursive: true });

  const written: string[] = [];
  for (const file of req.files) {
   const filename = safeFilename(file.filename);
   const filePath = safePath(join(req.folder, filename));
   mkdirSync(dirname(filePath), { recursive: true });
   const content = fileContentBuffer(file);
   if (Buffer.isBuffer(content)) {
    writeFileSync(filePath, content);
   } else {
    writeFileSync(filePath, content, "utf-8");
   }
   written.push(filename);
  }

  sendResult(ws, "library.write.result", req.requestId, {
   folder: req.folder,
   written,
   createdFolder,
   count: written.length,
  });
  log?.info?.(`[clawchat] library.write: ${req.folder}/ → ${written.length} file(s): [${written.join(", ")}]`);
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, req.requestId, msg);
  log?.error?.(`[clawchat] library.write error: ${msg}`);
 }
}

/** Delete a file or an entire folder. */
export function handleLibraryDelete(ws: WsSend, req: LibraryDeleteRequest, log?: Record<string, Function>): void {
 try {
  if (req.filename) {
   // Delete single file
   const filePath = safePath(join(req.folder, req.filename));
   if (!existsSync(filePath)) {
    sendError(ws, req.requestId, `File not found: ${req.folder}/${req.filename}`);
    return;
   }
   unlinkSync(filePath);
   sendResult(ws, "library.delete.result", req.requestId, {
    folder: req.folder,
    deleted: req.filename,
    type: "file",
   });
   log?.info?.(`[clawchat] library.delete: file ${req.folder}/${req.filename}`);
  } else {
   // Delete entire folder
   const folderPath = safePath(req.folder);
   if (!existsSync(folderPath)) {
    sendError(ws, req.requestId, `Folder not found: ${req.folder}`);
    return;
   }
   rmSync(folderPath, { recursive: true });
   sendResult(ws, "library.delete.result", req.requestId, {
    folder: req.folder,
    deleted: req.folder,
    type: "folder",
   });
   log?.info?.(`[clawchat] library.delete: folder ${req.folder}/`);
  }
 } catch (err) {
  const msg = err instanceof Error ? err.message : String(err);
  sendError(ws, req.requestId, msg);
  log?.error?.(`[clawchat] library.delete error: ${msg}`);
 }
}
