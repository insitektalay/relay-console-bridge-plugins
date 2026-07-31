import type { ClawChatResolvedAccount } from "./types.js";
import { authenticateBridgeDevice } from "./bridge-auth.js";

type ClawChatOutboundContext = {
 to?: string | null;
 text: string;
 accountId?: string | null;
};

/** Send a reply message back to a ClawChat thread via the bridge REST API. Returns the message ID. */
export async function sendClawChatMessage(
 account: ClawChatResolvedAccount,
 ctx: ClawChatOutboundContext,
): Promise<string> {
 const apiUrl = account.apiUrl;

 if (!apiUrl || !account.devicePublicId || !account.deviceToken) {
 throw new Error("[clawchat] outbound: account not configured (missing apiUrl, devicePublicId, or deviceToken)");
 }

 const threadId = ctx.to;
 if (!threadId) {
 throw new Error("[clawchat] outbound: missing thread ID (ctx.to is empty)");
 }

 // Authenticate to get a fresh access token
 const authBody = await authenticateBridgeDevice({
 apiUrl,
 devicePublicId: account.devicePublicId,
 deviceToken: account.deviceToken,
 });
 const accessToken = authBody.tokens?.accessToken ?? authBody.accessToken;
 if (!accessToken) {
 throw new Error("[clawchat] outbound: device auth response missing accessToken");
 }

 const resp = await fetch(`${apiUrl}/api/v1/bridge/messages`, {
 method: "POST",
 headers: {
 "Content-Type": "application/json",
 Authorization: `Bearer ${accessToken}`,
 },
 body: JSON.stringify({
 threadId,
 content: ctx.text,
 senderId: account.openclawAgentId ?? "openclaw",
 senderName: "Agent",
 }),
 });

 if (!resp.ok) {
 const body = await resp.text().catch(() => "");
 throw new Error(`[clawchat] outbound: send failed ${resp.status} ${body}`);
 }

 const data = (await resp.json().catch(() => ({}))) as { id?: string };
 return data.id ?? "";
}
