import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

type JournalEntry = { state: "running" | "completed"; updatedAt: string };
type Journal = Record<string, JournalEntry>;

export class DispatchJournal {
 private data: Journal = {};
 private loaded = false;
 private readonly path: string;
 constructor(path = process.env.CLAWCHAT_DISPATCH_JOURNAL || join(homedir(), ".openclaw", "clawchat", "dispatch-journal.json")) { this.path = path; }

 async claim(dispatchId: string): Promise<boolean> {
  await this.load();
  const existing = this.data[dispatchId];
  if (existing?.state === "completed") return false;
  if (existing?.state === "running" && Date.now() - Date.parse(existing.updatedAt) < 30 * 60_000) return false;
  this.data[dispatchId] = { state: "running", updatedAt: new Date().toISOString() };
  await this.persist();
  return true;
 }

 async complete(dispatchId: string): Promise<void> { await this.load(); this.data[dispatchId] = { state: "completed", updatedAt: new Date().toISOString() }; this.prune(); await this.persist(); }
 async release(dispatchId: string): Promise<void> { await this.load(); if (this.data[dispatchId]?.state === "running") { delete this.data[dispatchId]; await this.persist(); } }

 private async load() {
  if (this.loaded) return;
  try { this.data = JSON.parse(await readFile(this.path, "utf8")) as Journal; } catch { this.data = {}; }
  this.loaded = true;
 }
 private prune() {
  const cutoff = Date.now() - 30 * 24 * 60 * 60_000;
  for (const [id, entry] of Object.entries(this.data)) if (Date.parse(entry.updatedAt) < cutoff) delete this.data[id];
 }
 private async persist() {
  await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
  const temporary = `${this.path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(this.data)}\n`, { mode: 0o600 });
  await rename(temporary, this.path);
 }
}
