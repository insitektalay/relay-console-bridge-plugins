import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

type Entry = { id: string; name?: string; workspace?: string; model?: string | { primary?: string }; identity?: { name?: string; theme?: string; avatar?: string } };
/** Read both official roster representations without importing a version-specific SDK path. */
export function nativeAgentEntries(cfg: OpenClawConfig): Entry[] {
 const agents = (cfg as { agents?: { list?: Entry[]; entries?: Record<string, Omit<Entry,"id">> } }).agents;
 if (agents?.entries && typeof agents.entries === "object") return Object.entries(agents.entries).map(([id,entry])=>({...entry,id}));
 if (Array.isArray(agents?.list)) return agents.list;
 return [{id:"main"}];
}
