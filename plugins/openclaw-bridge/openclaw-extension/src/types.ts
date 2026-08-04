import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

export type ClawChatRepoMapping = {
 repoKey: string;
 repoPath: string;
};

export type ClawChatResolvedAccount = {
 accountId: string;
 enabled: boolean;
 configured: boolean;
 /** Base URL of the ClawChat backend (no trailing slash), e.g. https://my-app.up.railway.app */
 apiUrl: string | null;
 /** Device public ID from ClawChat bridge enrollment */
 devicePublicId: string | null;
 /** Device token from ClawChat bridge enrollment */
 deviceToken: string | null;
 /** The ClawChat workspaceId to subscribe to for inbound messages */
 workspaceId: string | null;
 /** Server-negotiated runtime compatibility tier */
 compatibilityLevel: string | null;
 /** Full or capability-restricted safe operation */
 operatingMode: string | null;
 /** Capabilities authorized by Relay for this runtime version */
 enabledCapabilities: string[];
 /** The OpenClaw agentId that handles messages arriving from this workspace */
 openclawAgentId: string | null;
 /** Optional Codex launcher for structured prompt bridge-control work */
 structuredPromptCommand: string | null;
 /** Optional default cwd when a structured prompt request does not provide cwd/repoKey */
 structuredPromptDefaultCwd: string | null;
 /** Optional standalone runtime config path used as a secondary source of repo mappings */
 runtimeConfigPath: string | null;
 /** Optional repoKey → repoPath mappings for structured prompt execution */
 repoMappings: ClawChatRepoMapping[];
};

function pickString(...values: unknown[]): string | null {
 for (const value of values) {
  if (typeof value === "string" && value.trim()) {
   return value.trim();
  }
 }
 return null;
}

function normalizeRepoMappings(value: unknown): ClawChatRepoMapping[] {
 if (Array.isArray(value)) {
  return value.flatMap((entry) => {
   if (!entry || typeof entry !== "object") return [];
   const repoKey = pickString((entry as Record<string, unknown>).repoKey);
   const repoPath = pickString(
    (entry as Record<string, unknown>).repoPath,
    (entry as Record<string, unknown>).path,
    (entry as Record<string, unknown>).cwd,
   );
   return repoKey && repoPath ? [{ repoKey, repoPath }] : [];
  });
 }

 if (value && typeof value === "object") {
  return Object.entries(value as Record<string, unknown>).flatMap(([repoKey, repoPath]) => {
   const normalizedPath = pickString(repoPath);
   return normalizedPath ? [{ repoKey, repoPath: normalizedPath }] : [];
  });
 }

 return [];
}

export function resolveClawChatAccount(
 cfg: OpenClawConfig,
 accountId?: string | null,
): ClawChatResolvedAccount {
 const base = (cfg.channels as Record<string, unknown>)?.clawchat as
 | Record<string, unknown>
 | undefined;

 const empty: ClawChatResolvedAccount = {
 accountId: accountId || "default",
 enabled: false,
 configured: false,
 apiUrl: null,
 devicePublicId: null,
 deviceToken: null,
 workspaceId: null,
 compatibilityLevel: null,
 operatingMode: null,
 enabledCapabilities: [],
 openclawAgentId: null,
 structuredPromptCommand: null,
 structuredPromptDefaultCwd: null,
 runtimeConfigPath: null,
 repoMappings: [],
 };

 if (!base) return empty;

 const useDefault = !accountId || accountId === "default";
 const account = useDefault
 ? base
 : ((base.accounts as Record<string, unknown> | undefined)?.[accountId] ?? base);

 const apiUrl = ((account as Record<string, unknown>)?.apiUrl ??
 base.apiUrl ??
 null) as string | null;
 const devicePublicId = ((account as Record<string, unknown>)?.devicePublicId ??
 base.devicePublicId ??
 null) as string | null;
 const deviceToken = ((account as Record<string, unknown>)?.deviceToken ??
 base.deviceToken ??
 null) as string | null;
 const workspaceId = ((account as Record<string, unknown>)?.workspaceId ??
 base.workspaceId ??
 null) as string | null;
 const compatibilityLevel = pickString(
  (account as Record<string, unknown>)?.compatibilityLevel,
  base.compatibilityLevel,
 );
 const operatingMode = pickString(
  (account as Record<string, unknown>)?.operatingMode,
  base.operatingMode,
 );
 const enabledCapabilities = [
  ...new Set(
   (((account as Record<string, unknown>)?.enabledCapabilities ?? base.enabledCapabilities ?? []) as unknown[])
    .filter((value): value is string => typeof value === "string" && Boolean(value.trim()))
    .map((value) => value.trim()),
  ),
 ];
 const openclawAgentId = ((account as Record<string, unknown>)?.openclawAgentId ??
 base.openclawAgentId ??
 null) as string | null;
 const structuredPromptCommand = pickString(
  (account as Record<string, unknown>)?.structuredPromptCommand,
  base.structuredPromptCommand,
 );
 const structuredPromptDefaultCwd = pickString(
  (account as Record<string, unknown>)?.structuredPromptDefaultCwd,
  (account as Record<string, unknown>)?.defaultProjectPath,
  (account as Record<string, unknown>)?.defaultWorkspacePath,
  base.structuredPromptDefaultCwd,
  base.defaultProjectPath,
  base.defaultWorkspacePath,
 );
 const runtimeConfigPath = pickString(
  (account as Record<string, unknown>)?.runtimeConfigPath,
  base.runtimeConfigPath,
 );
 const repoMappings = normalizeRepoMappings(
  (account as Record<string, unknown>)?.repoMappings ??
   (account as Record<string, unknown>)?.repos ??
   base.repoMappings ??
   base.repos,
 );

 return {
 accountId: accountId || "default",
 enabled: ((account as Record<string, unknown>)?.enabled ?? base.enabled ?? true) !== false,
 configured: Boolean(apiUrl && devicePublicId && deviceToken && workspaceId),
 apiUrl,
 devicePublicId,
 deviceToken,
 workspaceId,
 compatibilityLevel,
 operatingMode,
 enabledCapabilities,
 openclawAgentId,
 structuredPromptCommand,
 structuredPromptDefaultCwd,
 runtimeConfigPath,
 repoMappings,
 };
}

export function listClawChatAccountIds(cfg: OpenClawConfig): string[] {
 const base = (cfg.channels as Record<string, unknown>)?.clawchat as
 | Record<string, unknown>
 | undefined;
 if (!base) return [];
 const accounts = (base.accounts as Record<string, unknown> | undefined) ?? {};
 return [...(base.apiUrl ? ["default"] : []), ...Object.keys(accounts)];
}
