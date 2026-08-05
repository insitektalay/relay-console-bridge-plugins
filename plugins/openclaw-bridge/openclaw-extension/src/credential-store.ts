import { createHash } from "node:crypto";
import { mkdir, readFile, rename, rmdir, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

type StoredCredential = {
 version: 1;
 apiUrl: string;
 devicePublicId: string;
 configuredCredentialHash: string;
 deviceToken: string;
 updatedAt: string;
};

const LOCK_WAIT_MS = 50;
const LOCK_TIMEOUT_MS = 10_000;
const STALE_LOCK_MS = 120_000;

function digest(value: string): string {
 return createHash("sha256").update(value).digest("hex");
}

function sleep(ms: number): Promise<void> {
 return new Promise((resolve) => setTimeout(resolve, ms));
}

export class BridgeCredentialStore {
 private readonly root: string;

 constructor(root = join(
  process.env.OPENCLAW_STATE_DIR?.trim() || process.env.OPENCLAW_HOME?.trim() || join(homedir(), ".openclaw"),
  "clawchat",
  "credentials",
 )) {
  this.root = root;
 }

 async load(input: {
  apiUrl: string;
  devicePublicId: string;
  configuredCredential: string;
 }): Promise<string | null> {
  try {
   const parsed = JSON.parse(await readFile(this.credentialPath(input), "utf8")) as StoredCredential;
   if (
    parsed.version !== 1
    || parsed.apiUrl !== input.apiUrl
    || parsed.devicePublicId !== input.devicePublicId
    || parsed.configuredCredentialHash !== digest(input.configuredCredential)
    || typeof parsed.deviceToken !== "string"
    || !parsed.deviceToken.trim()
   ) return null;
   return parsed.deviceToken;
  } catch {
   return null;
  }
 }

 async save(input: {
  apiUrl: string;
  devicePublicId: string;
  configuredCredential: string;
  replacementCredential: string;
 }): Promise<void> {
  await mkdir(this.root, { recursive: true, mode: 0o700 });
  const target = this.credentialPath(input);
  const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
  const record: StoredCredential = {
   version: 1,
   apiUrl: input.apiUrl,
   devicePublicId: input.devicePublicId,
   configuredCredentialHash: digest(input.configuredCredential),
   deviceToken: input.replacementCredential,
   updatedAt: new Date().toISOString(),
  };
  await writeFile(temporary, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(temporary, target);
 }

 async withLock<T>(apiUrl: string, devicePublicId: string, operation: () => Promise<T>): Promise<T> {
  await mkdir(this.root, { recursive: true, mode: 0o700 });
  const lockPath = join(this.root, `${digest(`${apiUrl}\n${devicePublicId}`)}.lock`);
  const deadline = Date.now() + LOCK_TIMEOUT_MS;
  while (true) {
   try {
    await mkdir(lockPath, { mode: 0o700 });
    break;
   } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    try {
     const lock = await stat(lockPath);
     if (Date.now() - lock.mtimeMs > STALE_LOCK_MS) {
      await rmdir(lockPath);
      continue;
     }
    } catch (lockError) {
     if ((lockError as NodeJS.ErrnoException).code !== "ENOENT") throw lockError;
     continue;
    }
    if (Date.now() >= deadline) {
     throw new Error("Relay Console credential rotation is already in progress in another OpenClaw process");
    }
    await sleep(LOCK_WAIT_MS);
   }
  }
  try {
   return await operation();
  } finally {
   await rmdir(lockPath).catch(() => undefined);
  }
 }

 private credentialPath(input: { apiUrl: string; devicePublicId: string }): string {
  return join(this.root, `${digest(`${input.apiUrl}\n${input.devicePublicId}`)}.json`);
 }
}
