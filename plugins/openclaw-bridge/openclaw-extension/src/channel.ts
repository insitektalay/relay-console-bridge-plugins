import type { ChannelPlugin } from "openclaw/plugin-sdk/core";
import { startClawChatGatewayAccount } from "./gateway.js";
import { sendClawChatMessage } from "./outbound.js";
import { listClawChatAccountIds, resolveClawChatAccount } from "./types.js";
import type { ClawChatResolvedAccount } from "./types.js";

const CHANNEL_ID = "clawchat" as const;

export const clawChatPlugin: ChannelPlugin<ClawChatResolvedAccount> = {
 id: CHANNEL_ID,

 meta: {
 id: CHANNEL_ID,
 label: "Relay Console",
 selectionLabel: "Relay Console",
 docsPath: "/channels/clawchat",
 blurb: "Receive and reply to messages in Relay Console workspaces",
 order: 50,
 },

 capabilities: {
 chatTypes: ["direct"],
 },

 reload: { configPrefixes: ["channels.clawchat"] },

 config: {
 listAccountIds: (cfg) => listClawChatAccountIds(cfg),
 resolveAccount: (cfg, accountId) => resolveClawChatAccount(cfg, accountId),
 defaultAccountId: () => "default",
 isEnabled: (account) => account.enabled,
 isConfigured: (account) => account.configured,

 setAccountEnabled: ({ cfg, accountId, enabled }) => {
 const channels = (cfg.channels ?? {}) as Record<string, unknown>;
 const section = (channels.clawchat ?? {}) as Record<string, unknown>;
 const accounts = (section.accounts ?? {}) as Record<string, Record<string, unknown>>;

 if (accountId === "default") {
 return { ...cfg, channels: { ...channels, clawchat: { ...section, enabled } } };
 }

 return {
 ...cfg,
 channels: {
 ...channels,
 clawchat: {
 ...section,
 accounts: { ...accounts, [accountId]: { ...(accounts[accountId] ?? {}), enabled } },
 },
 },
 };
 },

 describeAccount: (account) => ({
 accountId: account.accountId,
 enabled: account.enabled,
 configured: account.configured,
 apiUrl: account.apiUrl,
 workspaceId: account.workspaceId,
 }),
 },

 setup: {
 applyAccountConfig: ({ cfg, accountId, input }) => {
 const channels = (cfg.channels ?? {}) as Record<string, unknown>;
 const section = (channels.clawchat ?? {}) as Record<string, unknown>;
 const accounts = (section.accounts ?? {}) as Record<string, Record<string, unknown>>;

 const values = input as unknown as Record<string, unknown>;
 const patch: Record<string, unknown> = {};
 if (typeof values.apiUrl === "string" && values.apiUrl) patch.apiUrl = values.apiUrl;
 if (typeof values.devicePublicId === "string" && values.devicePublicId) patch.devicePublicId = values.devicePublicId;
 if (typeof values.deviceToken === "string" && values.deviceToken) patch.deviceToken = values.deviceToken;
 if (typeof values.workspaceId === "string" && values.workspaceId) patch.workspaceId = values.workspaceId;
 if (typeof values.openclawAgentId === "string" && values.openclawAgentId) patch.openclawAgentId = values.openclawAgentId;
 if (typeof values.enabled === "boolean") patch.enabled = values.enabled;

 if (accountId === "default") {
 return { ...cfg, channels: { ...channels, clawchat: { ...section, ...patch } } };
 }

 return {
 ...cfg,
 channels: {
 ...channels,
 clawchat: {
 ...section,
 accounts: {
 ...accounts,
 [accountId]: { ...(accounts[accountId] ?? {}), ...patch },
 },
 },
 },
 };
 },
 },

 messaging: {
 normalizeTarget: (raw) => raw.trim(),
 inferTargetChatType: () => "direct",
 resolveOutboundSessionRoute: ({ cfg, agentId, accountId, target }) => {
 return {
 sessionKey: `agent:${agentId}:${CHANNEL_ID}:${accountId ?? "default"}:direct:${target}`,
 baseSessionKey: `agent:${agentId}:${CHANNEL_ID}:${accountId ?? "default"}:direct:${target}`,
 peer: { kind: "direct", id: target },
 chatType: "direct",
 from: target,
 to: target,
 };
 },
 },

 outbound: {
 deliveryMode: "direct",
 textChunkLimit: 4000,

 resolveTarget: ({ to }) => {
 if (!to || !to.trim()) {
 return { ok: false, error: new Error("Relay Console: missing thread ID") };
 }
 return { ok: true, to: to.trim() };
 },

 sendText: async (ctx) => {
 const account = resolveClawChatAccount(ctx.cfg, ctx.accountId);
 const messageId = await sendClawChatMessage(account, ctx);
 // eslint-disable-next-line @typescript-eslint/no-explicit-any
 return { channel: CHANNEL_ID as any, messageId };
 },
 },

 status: {
 defaultRuntime: {
 accountId: "default",
 running: false,
 lastStartAt: null,
 lastStopAt: null,
 lastError: null,
 },

 collectStatusIssues: (accounts) =>
 accounts.flatMap((a) =>
 !a.configured
 ? [
 {
 channel: CHANNEL_ID,
 accountId: a.accountId,
 kind: "config" as const,
 message: "Account not configured (missing apiUrl, devicePublicId, deviceToken, or workspaceId)",
 },
 ]
 : [],
 ),

 buildChannelSummary: ({ snapshot }) => ({
 configured: (snapshot as { configured?: boolean }).configured ?? false,
 workspaceId: (snapshot as { workspaceId?: string }).workspaceId ?? null,
 }),
 },

 gateway: {
 startAccount: async (ctx) => startClawChatGatewayAccount(ctx),
 },
};
