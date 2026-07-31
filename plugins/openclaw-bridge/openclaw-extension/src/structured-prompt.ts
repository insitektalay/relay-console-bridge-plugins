import { spawn, spawnSync } from "node:child_process";
import { access, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";
import type { ClawChatRepoMapping, ClawChatResolvedAccount } from "./types.js";

const DEFAULT_STRUCTURED_PROMPT_COMMAND = "codex exec --sandbox danger-full-access";
const DEFAULT_TIMEOUT_MS = 120_000;
const MAX_TIMEOUT_MS = 30 * 60 * 1000;
const DEFAULT_WORKSPACE_PATH = path.join(homedir(), ".openclaw", "workspace");
const DEFAULT_RUNTIME_CONFIG_PATH = path.join(homedir(), ".clawchat", "claude-runtime", "config.json");

export type StructuredPromptRequest = {
 requestId: string;
 prompt: string;
 schema: Record<string, unknown>;
 model?: string | null;
 timeoutMs?: number;
 cwd?: string | null;
 repoKey?: string | null;
 maxTurns?: number;
};

type RuntimeRepoConfig = {
 repoKey?: string;
 repoPath?: string;
 path?: string;
 cwd?: string;
};

type RuntimeConfigFile = {
 workspaceId?: string;
 structuredPromptCommand?: string;
 structuredPromptDefaultCwd?: string;
 defaultProjectPath?: string;
 defaultWorkspacePath?: string;
 repos?: RuntimeRepoConfig[];
};

type StructuredPromptLogger = {
 info?: (message: string) => void;
 warn?: (message: string) => void;
 error?: (message: string) => void;
};

type ResolvedExecutionContext = {
 cwd: string;
 command: string;
 model: string | null;
 prompt: string;
 timeoutMs: number;
 repoKey: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
 return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function shellQuote(value: string): string {
 return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function normalizeTimeoutMs(value: unknown): number {
 if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
  return DEFAULT_TIMEOUT_MS;
 }
 return Math.max(1_000, Math.min(Math.floor(value), MAX_TIMEOUT_MS));
}

function normalizeModel(value: unknown): string | null {
 return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeRepoMappings(value: unknown): ClawChatRepoMapping[] {
 if (Array.isArray(value)) {
  return value.flatMap((entry) => {
   if (!entry || typeof entry !== "object") return [];
   const repoKey = typeof (entry as RuntimeRepoConfig).repoKey === "string"
    ? (entry as RuntimeRepoConfig).repoKey!.trim()
    : "";
   const repoPath = [
    (entry as RuntimeRepoConfig).repoPath,
    (entry as RuntimeRepoConfig).path,
    (entry as RuntimeRepoConfig).cwd,
   ].find((candidate) => typeof candidate === "string" && candidate.trim()) as string | undefined;
   return repoKey && repoPath ? [{ repoKey, repoPath: repoPath.trim() }] : [];
  });
 }

 if (isRecord(value)) {
  return Object.entries(value).flatMap(([repoKey, repoPath]) => {
   if (typeof repoPath !== "string" || !repoPath.trim()) return [];
   return [{ repoKey, repoPath: repoPath.trim() }];
  });
 }

 return [];
}

async function pathExists(targetPath: string): Promise<boolean> {
 try {
  await access(targetPath);
  return true;
 } catch {
  return false;
 }
}

async function loadRuntimeConfig(
 account: ClawChatResolvedAccount,
 log: StructuredPromptLogger | undefined,
): Promise<RuntimeConfigFile | null> {
 const configPath = account.runtimeConfigPath?.trim() || DEFAULT_RUNTIME_CONFIG_PATH;
 try {
  const raw = await readFile(configPath, "utf8");
  const parsed = JSON.parse(raw) as RuntimeConfigFile;
  if (parsed.workspaceId && account.workspaceId && parsed.workspaceId !== account.workspaceId) {
   log?.warn?.(
    `[clawchat] structured prompt runtime config workspace mismatch: ${parsed.workspaceId} !== ${account.workspaceId} (${configPath})`,
   );
  }
  return parsed;
 } catch {
  return null;
 }
}

function resolveRepoPath(
 repoKey: string,
 account: ClawChatResolvedAccount,
 runtimeConfig: RuntimeConfigFile | null,
): string | null {
 const combinedMappings = [
  ...account.repoMappings,
  ...normalizeRepoMappings(runtimeConfig?.repos),
 ];

 for (const mapping of combinedMappings) {
  if (mapping.repoKey === repoKey) {
   return path.resolve(mapping.repoPath);
  }
 }

 return null;
}

function resolveDefaultCwd(
 account: ClawChatResolvedAccount,
 runtimeConfig: RuntimeConfigFile | null,
): string {
 const configured = [
  account.structuredPromptDefaultCwd,
  runtimeConfig?.structuredPromptDefaultCwd,
  runtimeConfig?.defaultProjectPath,
  runtimeConfig?.defaultWorkspacePath,
 ].find((value) => typeof value === "string" && value.trim());

 return path.resolve(configured ?? DEFAULT_WORKSPACE_PATH);
}

function isGitRepo(cwd: string): boolean {
 const result = spawnSync("git", ["rev-parse", "--is-inside-work-tree"], {
  cwd,
  stdio: "ignore",
 });
 return result.status === 0;
}

function buildPrompt(request: StructuredPromptRequest): string {
 const parts = [
  request.prompt.trim(),
  "Return only JSON that matches the provided output schema. Do not wrap the JSON in markdown or add any extra prose.",
 ];

 if (typeof request.maxTurns === "number" && Number.isFinite(request.maxTurns) && request.maxTurns > 0) {
  parts.push(`Hard limit: complete the task within at most ${Math.floor(request.maxTurns)} turns.`);
 }

 return parts.join("\n\n");
}

function buildCommand(input: {
 launcher: string;
 schemaPath: string;
 outputPath: string;
 model: string | null;
 cwd: string;
}): string {
 const extraArgs: string[] = [];

 if (!/\b--output-schema\b/.test(input.launcher)) {
  extraArgs.push("--output-schema", shellQuote(input.schemaPath));
 }

 if (!/\b--output-last-message\b/.test(input.launcher)) {
  extraArgs.push("--output-last-message", shellQuote(input.outputPath));
 }

 if (input.model && !/\b(--model|-m)\b/.test(input.launcher)) {
  extraArgs.push("--model", shellQuote(input.model));
 }

 if (!isGitRepo(input.cwd) && !/\b--skip-git-repo-check\b/.test(input.launcher)) {
  extraArgs.push("--skip-git-repo-check");
 }

 extraArgs.push("-");

 return `${input.launcher.trim()} ${extraArgs.join(" ")}`.trim();
}

function tailText(value: string, maxChars = 4_000): string {
 if (!value) return "<empty>";
 return value.length > maxChars ? value.slice(value.length - maxChars) : value;
}

function formatCodexDiagnostics(input: {
 message: string;
 command: string;
 cwd: string;
 schemaPath: string;
 outputPath: string;
 exitCode: number | null;
 signal: NodeJS.Signals | null;
 stdout: string;
 stderr: string;
}): string {
 return [
  input.message,
  `cwd: ${input.cwd}`,
  `command: ${input.command}`,
  `schemaPath: ${input.schemaPath}`,
  `outputPath: ${input.outputPath}`,
  `exitCode: ${input.exitCode ?? "null"}`,
  `signal: ${input.signal ?? "null"}`,
  `stderr tail:\n${tailText(input.stderr)}`,
  `stdout tail:\n${tailText(input.stdout)}`,
 ].join("\n");
}

async function resolveExecutionContext(
 account: ClawChatResolvedAccount,
 _cfg: OpenClawConfig,
 request: StructuredPromptRequest,
 log: StructuredPromptLogger | undefined,
): Promise<ResolvedExecutionContext> {
 const runtimeConfig = await loadRuntimeConfig(account, log);

 let cwd: string | null = null;
 if (typeof request.cwd === "string" && request.cwd.trim()) {
  cwd = path.resolve(request.cwd.trim());
 } else if (typeof request.repoKey === "string" && request.repoKey.trim()) {
  cwd = resolveRepoPath(request.repoKey.trim(), account, runtimeConfig);
  if (!cwd) {
   throw new Error(`Unknown repoKey "${request.repoKey.trim()}"`);
  }
 } else {
  cwd = resolveDefaultCwd(account, runtimeConfig);
 }

 if (!(await pathExists(cwd))) {
  throw new Error(`Structured prompt cwd does not exist: ${cwd}`);
 }

 const launcher = account.structuredPromptCommand?.trim()
  || runtimeConfig?.structuredPromptCommand?.trim()
  || DEFAULT_STRUCTURED_PROMPT_COMMAND;

 return {
  cwd,
  command: launcher,
  model: normalizeModel(request.model),
  prompt: buildPrompt(request),
  timeoutMs: normalizeTimeoutMs(request.timeoutMs),
  repoKey: typeof request.repoKey === "string" && request.repoKey.trim() ? request.repoKey.trim() : null,
 };
}

export async function executeStructuredPrompt(input: {
 account: ClawChatResolvedAccount;
 cfg: OpenClawConfig;
 request: StructuredPromptRequest;
 log?: StructuredPromptLogger;
}): Promise<{ output: Record<string, unknown>; model: string | null }> {
 const { account, cfg, request, log } = input;

 if (!request.requestId?.trim()) {
  throw new Error("Structured prompt request is missing requestId");
 }
 if (!request.prompt?.trim()) {
  throw new Error("Structured prompt request is missing prompt");
 }
 if (!isRecord(request.schema)) {
  throw new Error("Structured prompt request is missing a valid JSON schema object");
 }

 const resolved = await resolveExecutionContext(account, cfg, request, log);
 const startedAt = Date.now();
 const tempDir = await mkdtemp(path.join(tmpdir(), "clawchat-structured-prompt-"));
 const schemaPath = path.join(tempDir, "schema.json");
 const outputPath = path.join(tempDir, "output.json");
 const command = buildCommand({
  launcher: resolved.command,
  schemaPath,
  outputPath,
  model: resolved.model,
  cwd: resolved.cwd,
 });

 await writeFile(schemaPath, `${JSON.stringify(request.schema, null, 2)}\n`, "utf8");

 log?.info?.(
  `[clawchat] structured prompt start requestId=${request.requestId} cwd=${resolved.cwd} repoKey=${resolved.repoKey ?? "-"} model=${resolved.model ?? "default"} timeoutMs=${resolved.timeoutMs}`,
 );

 try {
  const child = spawn("bash", ["-lc", command], {
   cwd: resolved.cwd,
   env: {
    ...process.env,
    HOME: homedir(),
   },
   // Give the shell and its Codex child the same process group so timeout
   // cleanup can kill the whole tree. Killing only bash can otherwise leave
   // Codex running and produce a misleading missing-output ENOENT.
   detached: process.platform !== "win32",
   stdio: ["pipe", "pipe", "pipe"],
  });

  const stdoutChunks: Buffer[] = [];
  const stderrChunks: Buffer[] = [];
  let timedOut = false;

  child.stdout.on("data", (chunk: Buffer) => {
   stdoutChunks.push(chunk);
  });
  child.stderr.on("data", (chunk: Buffer) => {
   stderrChunks.push(chunk);
  });

  child.stdin.end(resolved.prompt);

  const exit = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
   const killChild = (signal: NodeJS.Signals) => {
    try {
     if (process.platform !== "win32" && child.pid) {
      process.kill(-child.pid, signal);
     } else {
      child.kill(signal);
     }
    } catch {
     try {
      child.kill(signal);
     } catch { /* ignore */ }
    }
   };

   const timeout = setTimeout(() => {
    timedOut = true;
    log?.warn?.(`[clawchat] structured prompt timeout requestId=${request.requestId} after ${resolved.timeoutMs}ms`);
    killChild("SIGTERM");
    setTimeout(() => killChild("SIGKILL"), 10_000).unref();
   }, resolved.timeoutMs);

   child.on("error", (error) => {
    clearTimeout(timeout);
    reject(error);
   });
   child.on("close", (code, signal) => {
    clearTimeout(timeout);
    resolve({ code, signal });
   });
  });

  const stdout = Buffer.concat(stdoutChunks).toString("utf8").trim();
  const stderr = Buffer.concat(stderrChunks).toString("utf8").trim();

  if (timedOut || exit.signal === "SIGTERM" || exit.signal === "SIGKILL") {
   throw new Error(formatCodexDiagnostics({
    message: `Codex timed out after ${resolved.timeoutMs}ms`,
    command,
    cwd: resolved.cwd,
    schemaPath,
    outputPath,
    exitCode: exit.code,
    signal: exit.signal,
    stdout,
    stderr,
   }));
  }

  if (exit.code !== 0) {
   throw new Error(formatCodexDiagnostics({
    message: `Codex exited with code ${exit.code ?? "unknown"}`,
    command,
    cwd: resolved.cwd,
    schemaPath,
    outputPath,
    exitCode: exit.code,
    signal: exit.signal,
    stdout,
    stderr,
   }));
  }

  let rawOutput: string;
  try {
   rawOutput = await readFile(outputPath, "utf8");
  } catch (error) {
   const message = error instanceof Error ? error.message : String(error);
   throw new Error(formatCodexDiagnostics({
    message: `Codex exited successfully but did not create output file: ${message}`,
    command,
    cwd: resolved.cwd,
    schemaPath,
    outputPath,
    exitCode: exit.code,
    signal: exit.signal,
    stdout,
    stderr,
   }));
  }

  const parsed = JSON.parse(rawOutput) as unknown;
  if (!isRecord(parsed)) {
   throw new Error("Codex output was not a JSON object");
  }

  log?.info?.(
   `[clawchat] structured prompt complete requestId=${request.requestId} durationMs=${Date.now() - startedAt}`,
  );

  return {
   output: parsed,
   model: resolved.model,
  };
 } catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  log?.error?.(`[clawchat] structured prompt failed requestId=${request.requestId}: ${message}`);
  throw error;
 } finally {
  await rm(tempDir, { recursive: true, force: true }).catch(() => undefined);
 }
}
