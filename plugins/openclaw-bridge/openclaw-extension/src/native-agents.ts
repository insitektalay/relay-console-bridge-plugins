import { listAgentIds, resolveAgentConfig } from "openclaw/plugin-sdk/agent-scope-runtime";
import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

type Entry = { id: string; name?: string; workspace?: string; model?: string | { primary?: string } };
export function nativeAgentEntries(cfg: OpenClawConfig): Entry[] {
 const legacy = (cfg as { agents?: { list?: Entry[] } }).agents?.list;
 if (Array.isArray(legacy)) return legacy;
 return listAgentIds(cfg).map(id => ({ id, ...resolveAgentConfig(cfg, id) }));
}
