import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

function store(input: Record<string, unknown>): Promise<any> {
 return new Promise((resolve, reject) => {
  const child = spawn("python3", [fileURLToPath(new URL("./agent_file_store.py", import.meta.url))], { stdio: ["pipe", "pipe", "pipe"] });
  let output = "";
  const timer = setTimeout(() => child.kill(), 20_000);
  child.stdout.on("data", bytes => { output += bytes; if (Buffer.byteLength(output) > 4_000_000) child.kill(); });
  child.stderr.resume();
  child.on("error", error => { clearTimeout(timer); reject(error); });
  child.on("close", code => { clearTimeout(timer); try { if (code !== 0) throw new Error("Native file operation unavailable"); resolve(JSON.parse(output)); } catch (error) { reject(error); } });
  child.stdin.on("error", () => {});
  child.stdin.end(JSON.stringify(input));
 });
}

type AgentFileInput = {
 root: string; stateDir: string; workspaceId: string; command: Record<string, any>;
 post: (path: string, body: Record<string, any>) => Promise<any>;
};

// Recovery must not interrupt another request while it awaits its claim.
const queues = new Map<string, Promise<unknown>>();
export function handleAgentFile(input: AgentFileInput): Promise<Record<string, any>> {
 const previous = queues.get(input.stateDir) ?? Promise.resolve();
 const current = previous.catch(() => {}).then(() => performAgentFile(input));
 queues.set(input.stateDir, current);
 void current.finally(() => {
  if (queues.get(input.stateDir) === current) queues.delete(input.stateDir);
 }).catch(() => {});
 return current;
}

async function performAgentFile(input: AgentFileInput): Promise<Record<string, any>> {
 const { command, root, stateDir, post, workspaceId } = input;
 if (command.workspaceId !== workspaceId || command.runtimeType !== "openclaw" ||
     !/^[0-9a-f-]{36}$/i.test(command.operationId ?? "") ||
     !/^[0-9a-f-]{36}$/i.test(workspaceId)) throw new Error("File command scope mismatch");
 for (const receipt of await store({ mode: "recover", stateDir })) {
  await post(`bridge/agent-files/${receipt.operationId}/complete`, receipt);
  await store({ mode: "settle", stateDir, operationId: receipt.operationId });
 }
 await store({ mode: "reserve", stateDir, command });
 const claim = await post(`bridge/agent-files/${command.operationId}/claim`, { requestHash: command.requestHash });
 for (const key of ["operationId", "requestHash", "workspaceGeneration", "action", "path", "baseVersion", "contentHash"]) {
  if ((claim[key] ?? null) !== (command[key] ?? null)) throw new Error("File claim mismatch");
 }
 const result = await store({ stateDir, root, command: { ...command, _claimAllowed: claim.allowed === true } });
 const receipt = Object.fromEntries(Object.entries(result).filter(([key]) => !["content", "files", "folders"].includes(key)));
 await post(`bridge/agent-files/${command.operationId}/complete`, receipt);
 await store({ mode: "settle", stateDir, operationId: command.operationId });
 return result;
}
