import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
 copyFileSync,
 existsSync,
 lstatSync,
 mkdirSync,
 readFileSync,
 renameSync,
 rmSync,
 writeFileSync,
} from "node:fs";
import { dirname, extname, join, resolve, sep } from "node:path";
import { homedir } from "node:os";
import { authenticateBridgeDevice } from "./bridge-auth.js";
import { isAllowedNativeDocumentPath } from "./native-document-policy.js";

type ProvisionPayload = {
 slug: string;
 name: string;
 role?: string;
 modelPrimary?: string;
};

type ProvisionFile = {
 filename: string;
 content: string;
};

export type ProvisionRequest = {
 jobId: string;
 idempotencyKey?: string;
 runtimeHostId?: string;
 runtimeType?: string;
 payload: ProvisionPayload;
 files?: ProvisionFile[];
};

type ProvisionCallbacks = {
 apiUrl: string;
 devicePublicId: string;
 deviceToken: string;
 log?: {
  info?: (message: string, ...args: unknown[]) => void;
  warn?: (message: string, ...args: unknown[]) => void;
  error?: (message: string, ...args: unknown[]) => void;
 };
};

/** Fetch an access token from device credentials for provisioner HTTP calls. */
async function getAccessToken(cb: ProvisionCallbacks): Promise<string> {
 const body = await authenticateBridgeDevice({
  apiUrl: cb.apiUrl,
  devicePublicId: cb.devicePublicId,
  deviceToken: cb.deviceToken,
 });
 const token = body.tokens?.accessToken ?? body.accessToken;
 if (!token) throw new Error("[clawchat] provisioner device auth response missing accessToken");
 return token;
}

async function reportProgress(
 cb: ProvisionCallbacks,
 jobId: string,
 stage: string,
 message: string,
): Promise<void> {
 try {
  const token = await getAccessToken(cb);
  await fetch(`${cb.apiUrl}/api/v1/bridge/provision-jobs/${jobId}/progress`, {
   method: "POST",
   headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
   },
   body: JSON.stringify({ status: "running", stage, message }),
  });
 } catch (err) {
  cb.log?.warn?.(`[clawchat] progress callback unavailable for job ${jobId}`);
 }
}

async function reportComplete(
 cb: ProvisionCallbacks,
 jobId: string,
 agent: {
  externalAgentId: string;
  name: string;
  role?: string;
  status: string;
  model?: string;
 },
): Promise<void> {
 try {
  const token = await getAccessToken(cb);
  const resp = await fetch(`${cb.apiUrl}/api/v1/bridge/provision-jobs/${jobId}/complete`, {
   method: "POST",
   headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
   },
   body: JSON.stringify({
    externalAgentId: agent.externalAgentId,
    agent: {
     externalId: agent.externalAgentId,
     name: agent.name,
     role: agent.role ?? "",
     status: agent.status,
     metadata: agent.model ? { modelPrimary: agent.model } : undefined,
    },
   }),
  });
  if (!resp.ok) {
   cb.log?.error?.(`[clawchat] complete callback failed for job ${jobId}: HTTP ${resp.status}`);
  }
 } catch {
  cb.log?.error?.(`[clawchat] complete callback unavailable for job ${jobId}`);
 }
}

async function reportFail(
 cb: ProvisionCallbacks,
 jobId: string,
 error: string,
 stage: string,
): Promise<void> {
 try {
  const token = await getAccessToken(cb);
  await fetch(`${cb.apiUrl}/api/v1/bridge/provision-jobs/${jobId}/fail`, {
   method: "POST",
   headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
   },
   body: JSON.stringify({ error, stage }),
  });
 } catch {
  cb.log?.warn?.(`[clawchat] fail callback unavailable for job ${jobId}`);
 }
}

function runCli(args: string[]): string {
 return execFileSync("openclaw", args, {
  encoding: "utf-8",
  timeout: 30_000,
  env: { ...process.env, HOME: homedir() },
  stdio: ["ignore", "pipe", "pipe"],
 }).trim();
}

const SAFE_SLUG = /^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$/;
const MAX_FILE_BYTES = 500_000;
const ALLOWED_FILE_EXTENSIONS = new Set([".md", ".markdown"]);
const SENSITIVE_FILE_NAME = /(^|[._-])(auth|credential|password|secret|token|keychain)([._-]|$)/i;

type ProvisionMarker = {
 version: 1;
 idempotencyKey: string;
 slug: string;
 status: "started" | "completed";
};

function validateProvisionRequest(req: ProvisionRequest): {
 slug: string;
 idempotencyKey: string;
} {
 const slug = req.payload.slug?.trim().toLowerCase();
 if (!SAFE_SLUG.test(slug)) {
  throw new Error("OpenClaw agent slug must contain only lowercase letters, numbers, hyphens, or underscores");
 }
 const idempotencyKey = req.idempotencyKey?.trim() || req.jobId.trim();
 if (!idempotencyKey || idempotencyKey.length > 500) {
  throw new Error("A bounded provisioning idempotency key is required");
 }
 if (req.runtimeType && req.runtimeType !== "openclaw") {
  throw new Error(`Provisioning request targeted unsupported runtime "${req.runtimeType}"`);
 }
 if (req.payload.modelPrimary && (
  req.payload.modelPrimary.length > 300 ||
  /[\0\r\n]/.test(req.payload.modelPrimary)
 )) {
  throw new Error("Invalid OpenClaw model identifier");
 }
 return { slug, idempotencyKey };
}

function markerPath(idempotencyKey: string) {
 const digest = createHash("sha256").update(idempotencyKey).digest("hex");
 return join(homedir(), ".openclaw", "clawchat", "provisioning", `${digest}.json`);
}

function readMarker(file: string): ProvisionMarker | null {
 try {
  const marker = JSON.parse(readFileSync(file, "utf8")) as ProvisionMarker;
  return marker?.version === 1 ? marker : null;
 } catch {
  return null;
 }
}

function writeMarker(file: string, marker: ProvisionMarker): void {
 mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
 const temporary = `${file}.${process.pid}.tmp`;
 writeFileSync(temporary, `${JSON.stringify(marker, null, 2)}\n`, {
  encoding: "utf8",
  mode: 0o600,
 });
 renameSync(temporary, file);
}

export function safeProvisionFilePath(workspacePath: string, filename: string): string {
 const normalized = filename.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
 const parts = normalized.split("/");
 if (
  !normalized ||
  parts.length > 7 ||
  parts.some((part) => !part || part === "." || part === "..") ||
  !ALLOWED_FILE_EXTENSIONS.has(extname(parts.at(-1)!).toLowerCase()) ||
  parts.some((part) => SENSITIVE_FILE_NAME.test(part))
 ) {
  throw new Error("OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED");
 }
 const documentFilename = parts.at(-1)!;
 const documentFolder = parts.slice(0, -1).join("/");
 if (!isAllowedNativeDocumentPath(documentFolder, documentFilename)) {
  throw new Error("OPENCLAW_DOCUMENT_PATH_NOT_ALLOWED");
 }
 const root = resolve(workspacePath);
 if (existsSync(root) && lstatSync(root).isSymbolicLink()) {
  throw new Error("Agent workspace root must not be a symbolic link");
 }
 const target = resolve(root, ...parts);
 if (target !== root && !target.startsWith(`${root}${sep}`)) {
  throw new Error("OPENCLAW_DOCUMENT_PATH_ESCAPED_WORKSPACE");
 }
 let current = root;
 for (const part of parts.slice(0, -1)) {
  current = join(current, part);
  if (existsSync(current) && lstatSync(current).isSymbolicLink()) {
   throw new Error("OPENCLAW_DOCUMENT_PATH_SYMBOLIC_LINK");
  }
 }
 return target;
}

function writeDefaultAuthProfiles(targetPath: string): void {
 writeFileSync(
  targetPath,
  `${JSON.stringify({ version: 1, profiles: {} }, null, 2)}\n`,
  { encoding: "utf-8", mode: 0o600 },
 );
}

function bootstrapAuthFromMainAgent(slug: string): string[] {
 const openClawHome = join(homedir(), ".openclaw");
 const sourceAgentDir = join(openClawHome, "agents", "main", "agent");
 const targetAgentDir = join(openClawHome, "agents", slug, "agent");
 const bootstrapped: string[] = [];

 mkdirSync(targetAgentDir, { recursive: true, mode: 0o700 });

 const authProfilesFilename = "auth-profiles.json";
 const authProfilesSourcePath = join(sourceAgentDir, authProfilesFilename);
 const authProfilesTargetPath = join(targetAgentDir, authProfilesFilename);

 if (!existsSync(authProfilesTargetPath)) {
  if (existsSync(authProfilesSourcePath)) {
   copyFileSync(authProfilesSourcePath, authProfilesTargetPath);
   bootstrapped.push(authProfilesFilename);
  } else {
   writeDefaultAuthProfiles(authProfilesTargetPath);
   bootstrapped.push(`${authProfilesFilename} (default)`);
  }
 }

 const legacyAuthFilename = "auth.json";
 const legacyAuthSourcePath = join(sourceAgentDir, legacyAuthFilename);
 const legacyAuthTargetPath = join(targetAgentDir, legacyAuthFilename);

 if (existsSync(legacyAuthSourcePath) && !existsSync(legacyAuthTargetPath)) {
  copyFileSync(legacyAuthSourcePath, legacyAuthTargetPath);
  bootstrapped.push(legacyAuthFilename);
 }

 return bootstrapped;
}

/**
 * Provision a new OpenClaw agent locally from a ClawChat provisioning request.
 * Returns the externalAgentId (slug) on success.
 */
export async function provisionAgent(
 req: ProvisionRequest,
 cb: ProvisionCallbacks,
): Promise<string> {
 const { jobId, payload, files } = req;
 const { slug, idempotencyKey } = validateProvisionRequest(req);
 const displayName = payload.name || slug;
 const workspacePath = join(homedir(), ".openclaw", `workspace-${slug}`);
 const agentDir = join(homedir(), ".openclaw", "agents", slug);
 const provisionMarkerPath = markerPath(idempotencyKey);
 const existingMarker = readMarker(provisionMarkerPath);

 if (existingMarker && existingMarker.slug !== slug) {
  throw new Error("Provisioning idempotency key was already used for another agent");
 }
 if (existsSync(agentDir) && !existingMarker) {
  throw new Error("OPENCLAW_AGENT_ALREADY_EXISTS");
 }
 if (existingMarker?.status === "completed") {
  await reportComplete(cb, jobId, {
   externalAgentId: slug,
   name: displayName,
   role: payload.role,
   status: "active",
   model: payload.modelPrimary,
  });
  return slug;
 }
 writeMarker(provisionMarkerPath, {
  version: 1,
  idempotencyKey,
  slug,
  status: "started",
 });

 cb.log?.info?.(`[clawchat] provisioning agent "${slug}" (job ${jobId})`);

 let currentStage = "checking";

 try {
  // Stage 1: Check if agent already exists
  await reportProgress(cb, jobId, "checking", `Checking if agent "${slug}" already exists`);

  // Stage 2: Create agent via CLI
  currentStage = "creating_agent";
  await reportProgress(cb, jobId, "creating_agent", `Creating agent "${slug}"`);

  if (!existsSync(agentDir)) {
   const addArgs = ["agents", "add", slug, "--workspace", workspacePath, "--non-interactive"];
   if (payload.modelPrimary) {
    addArgs.push("--model", payload.modelPrimary);
   }
   runCli(addArgs);
  }

  // Stage 3: Set display name
  currentStage = "setting_identity";
  if (displayName !== slug) {
   await reportProgress(cb, jobId, "setting_identity", `Setting display name to "${displayName}"`);
   runCli(["agents", "set-identity", "--agent", slug, "--name", displayName]);
  }

  // Stage 4: Bootstrap auth from the main agent when available
  currentStage = "bootstrapping_auth";
  await reportProgress(cb, jobId, "bootstrapping_auth", "Bootstrapping auth from the main OpenClaw agent");
  const copiedAuthFiles = bootstrapAuthFromMainAgent(slug);
  if (copiedAuthFiles.length) {
   cb.log?.info?.(`[clawchat] copied auth for "${slug}": ${copiedAuthFiles.join(", ")}`);
  }

  // Stage 5: Write markdown files
  currentStage = "writing_files";
  if (files?.length) {
   await reportProgress(cb, jobId, "writing_files", `Writing ${files.length} workspace file(s)`);
   mkdirSync(workspacePath, { recursive: true });

   for (const file of files) {
    if (Buffer.byteLength(file.content, "utf8") > MAX_FILE_BYTES) {
     throw new Error("OPENCLAW_DOCUMENT_TOO_LARGE");
    }
    const filePath = safeProvisionFilePath(workspacePath, file.filename);
    mkdirSync(dirname(filePath), { recursive: true, mode: 0o700 });
    if (existsSync(filePath) && lstatSync(filePath).isSymbolicLink()) {
     throw new Error("OPENCLAW_DOCUMENT_PATH_SYMBOLIC_LINK");
    }
    const temporary = `${filePath}.${process.pid}.relay.tmp`;
    try {
     writeFileSync(temporary, file.content, { encoding: "utf8", mode: 0o600 });
     if (existsSync(filePath) && lstatSync(filePath).isSymbolicLink()) {
      throw new Error("OPENCLAW_DOCUMENT_PATH_SYMBOLIC_LINK");
     }
     renameSync(temporary, filePath);
    } finally {
     rmSync(temporary, { force: true });
    }
    cb.log?.info?.(`[clawchat] wrote allowlisted agent document ${file.filename}`);
   }
  }

  // Stage 6: Verify
  currentStage = "verifying";
  await reportProgress(cb, jobId, "verifying", "Verifying agent creation");

  const verifyOutput = runCli(["agents", "list", "--json"]);
  if (!listedAgentIds(verifyOutput).has(slug)) {
   throw new Error("OPENCLAW_AGENT_VERIFY_FAILED");
  }

  // Stage 7: Report complete
  currentStage = "complete";
  await reportComplete(cb, jobId, {
   externalAgentId: slug,
   name: displayName,
   role: payload.role,
   status: "active",
   model: payload.modelPrimary,
  });
  writeMarker(provisionMarkerPath, {
   version: 1,
   idempotencyKey,
   slug,
   status: "completed",
  });

  // Restart gateway so the new agent is available for dispatch
  currentStage = "restarting_gateway";
  await reportProgress(cb, jobId, "restarting_gateway", "Restarting gateway to activate new agent");
  try {
   execFileSync("systemctl", ["--user", "restart", "openclaw-gateway"], {
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, HOME: homedir() },
    stdio: ["ignore", "pipe", "pipe"],
   });
  } catch {
   // systemctl restart kills our own process, so this may throw — that's expected
   cb.log?.info?.(`[clawchat] gateway restart triggered for new agent "${slug}"`);
  }

  cb.log?.info?.(`[clawchat] agent "${slug}" provisioned successfully (job ${jobId})`);
  return slug;

 } catch (err) {
  const errorCode = safeProvisionErrorCode(err);
  cb.log?.error?.(`[clawchat] provisioning failed for "${slug}" at stage "${currentStage}" code=${errorCode}`);
  await reportFail(cb, jobId, errorCode, currentStage);
  throw new Error(errorCode);
 }
}

function listedAgentIds(output: string): Set<string> {
 const ids = new Set<string>();
 const visit = (value: unknown): void => {
  if (Array.isArray(value)) {
   value.forEach(visit);
   return;
  }
  if (!value || typeof value !== "object") return;
  const record = value as Record<string, unknown>;
  for (const key of ["id", "agentId", "slug"]) {
   if (typeof record[key] === "string") ids.add(record[key].trim());
  }
  Object.values(record).forEach(visit);
 };
 try {
  visit(JSON.parse(output));
 } catch {
  return ids;
 }
 return ids;
}

function safeProvisionErrorCode(error: unknown): string {
 const candidate = error instanceof Error ? error.message : String(error);
 return /^[A-Z][A-Z0-9_]{0,119}$/.test(candidate)
  ? candidate
  : "OPENCLAW_PROVISIONING_FAILED";
}
