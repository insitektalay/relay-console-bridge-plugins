import { extname } from "node:path";

const ALLOWED_EXTENSIONS = new Set([".md", ".markdown"]);
const ROOT_DOCUMENTS = new Set([
 "AGENTS.md",
 "HEARTBEAT.md",
 "IDENTITY.md",
 "MEMORY.md",
 "SOUL.md",
 "TOOLS.md",
 "USER.md",
]);
const ALLOWED_TREES = new Set(["memory", "skills"]);
const SENSITIVE_NAME = /(^|[._-])(auth|credential|password|secret|token|keychain)([._-]|$)/i;

export function isSensitiveNativeDocumentName(value: string): boolean {
 return SENSITIVE_NAME.test(value);
}

export function isAllowedNativeDocumentPath(folder: string, filename: string): boolean {
 const normalizedFolder = folder.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
 const parts = normalizedFolder ? normalizedFolder.split("/") : [];
 if (
  !filename ||
  filename.includes("/") ||
  filename.includes("\\") ||
  parts.length > 6 ||
  parts.some((part) => !part || part === "." || part === ".." || part.startsWith(".")) ||
  parts.some(isSensitiveNativeDocumentName) ||
  isSensitiveNativeDocumentName(filename) ||
  !ALLOWED_EXTENSIONS.has(extname(filename).toLowerCase())
 ) {
  return false;
 }
 if (parts.length === 0) return ROOT_DOCUMENTS.has(filename);
 return ALLOWED_TREES.has(parts[0].toLowerCase());
}
