import type { OpenClawConfig, OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { hostname } from "node:os";

import {
 redeemBridgeEnrollment,
 requireSecureRelayApiUrl,
 rotateBridgeDeviceCredential,
 type BridgeEnrollmentResponse,
} from "./bridge-auth.js";

const MAX_ENROLLMENT_CODE_BYTES = 1024;

type EnrollmentConfigInput = {
 apiUrl: string;
 accountId: string;
 openclawAgentId?: string;
 response: BridgeEnrollmentResponse;
};

type RotationConfigInput = {
 accountId: string;
 response: BridgeEnrollmentResponse;
};

function requiredEnrollmentString(value: unknown, label: string): string {
 if (typeof value !== "string" || !value.trim()) {
  throw new Error(`Relay Console enrollment response is missing ${label}`);
 }
 return value.trim();
}

export function applyBridgeEnrollmentToConfig(
 cfg: OpenClawConfig,
 input: EnrollmentConfigInput,
): OpenClawConfig {
 const apiUrl = requireSecureRelayApiUrl(input.apiUrl);
 const workspaceId = requiredEnrollmentString(input.response.workspace?.id, "workspace ID");
 const devicePublicId = requiredEnrollmentString(
  input.response.credentials?.devicePublicId ?? input.response.device?.devicePublicId,
  "device public ID",
 );
 const credentialValue = requiredEnrollmentString(
  input.response.credentials?.deviceToken,
  "device credential",
 );
 const accountId = input.accountId.trim() || "default";
 const channels = (cfg.channels ?? {}) as Record<string, unknown>;
 const current = (channels.clawchat ?? {}) as Record<string, unknown>;
 const enrollment = {
  apiUrl,
  workspaceId,
  devicePublicId,
  deviceToken: credentialValue,
  enabled: true,
  compatibilityLevel: input.response.device?.compatibility?.level ?? null,
  operatingMode: input.response.device?.compatibility?.operatingMode ?? null,
  enabledCapabilities: input.response.device?.compatibility?.enabledCapabilities ?? [],
  ...(input.openclawAgentId?.trim()
   ? { openclawAgentId: input.openclawAgentId.trim() }
   : {}),
 };

 if (accountId === "default") {
  return {
   ...cfg,
   channels: {
    ...channels,
    clawchat: { ...current, ...enrollment },
   },
  } as OpenClawConfig;
 }

 const accounts = (current.accounts ?? {}) as Record<string, Record<string, unknown>>;
 return {
  ...cfg,
  channels: {
   ...channels,
   clawchat: {
    ...current,
    accounts: {
     ...accounts,
     [accountId]: { ...(accounts[accountId] ?? {}), ...enrollment },
    },
   },
  },
 } as OpenClawConfig;
}

function relayAccountConfig(
 cfg: OpenClawConfig,
 accountId: string,
): Record<string, unknown> {
 const channels = (cfg.channels ?? {}) as Record<string, unknown>;
 const current = (channels.clawchat ?? {}) as Record<string, unknown>;
 if (accountId === "default") return current;
 const accounts = (current.accounts ?? {}) as Record<string, Record<string, unknown>>;
 return accounts[accountId] ?? {};
}

export function applyBridgeCredentialRotationToConfig(
 cfg: OpenClawConfig,
 input: RotationConfigInput,
): OpenClawConfig {
 const accountId = input.accountId.trim() || "default";
 const rotatedValue = requiredEnrollmentString(
  input.response.credentials?.deviceToken,
  "rotated device credential",
 );
 const channels = (cfg.channels ?? {}) as Record<string, unknown>;
 const current = (channels.clawchat ?? {}) as Record<string, unknown>;
 if (accountId === "default") {
  return {
   ...cfg,
   channels: {
    ...channels,
    clawchat: { ...current, deviceToken: rotatedValue },
   },
  } as OpenClawConfig;
 }
 const accounts = (current.accounts ?? {}) as Record<string, Record<string, unknown>>;
 if (!accounts[accountId]) {
  throw new Error(`Relay Console account ${accountId} is not configured`);
 }
 return {
  ...cfg,
  channels: {
   ...channels,
   clawchat: {
    ...current,
    accounts: {
     ...accounts,
     [accountId]: { ...accounts[accountId], deviceToken: rotatedValue },
    },
   },
  },
 } as OpenClawConfig;
}

export async function readEnrollmentCodeFromStdin(
 stdin: AsyncIterable<string | Buffer> = process.stdin,
): Promise<string> {
 let value = "";
 for await (const chunk of stdin) {
  value += Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
  if (Buffer.byteLength(value, "utf8") > MAX_ENROLLMENT_CODE_BYTES) {
   throw new Error("Relay Console enrollment code input is too large");
  }
 }
 const code = value.trim();
 if (!code) {
  throw new Error("Relay Console enrollment code was not provided on standard input");
 }
 return code;
}

export function registerRelayConsoleCli(api: OpenClawPluginApi): void {
 api.registerCli(
  ({ program }) => {
   const relay = program
    .command("relay-console")
    .description("Manage the Relay Console bridge");

   relay
    .command("enroll")
    .description("Redeem a one-time Relay Console bridge code from standard input")
    .requiredOption("--api-url <url>", "Relay Console Railway API origin")
    .option("--label <label>", "Runtime device label", `${hostname()} OpenClaw bridge`)
    .option("--account <id>", "Relay Console channel account ID", "default")
    .option("--agent <id>", "OpenClaw agent ID used for outbound attribution")
    .action(async (options: {
     apiUrl: string;
     label: string;
     account: string;
     agent?: string;
    }) => {
     if (process.stdin.isTTY) {
      throw new Error(
       "Read the one-time code without echo, then pipe it to this command; see the Relay Console bridge install guide.",
      );
     }
     const code = await readEnrollmentCodeFromStdin();
     const apiUrl = requireSecureRelayApiUrl(options.apiUrl);
     const response = await redeemBridgeEnrollment({
      apiUrl,
      code,
      deviceLabel: options.label,
     });
     const nextConfig = applyBridgeEnrollmentToConfig(api.runtime.config.loadConfig(), {
      apiUrl,
      accountId: options.account,
      openclawAgentId: options.agent,
      response,
     });
     await api.runtime.config.writeConfigFile(nextConfig);

     const workspaceName = response.workspace?.name?.trim() || response.workspace?.id?.trim();
     const deviceLabel = response.device?.label?.trim() || options.label;
     process.stdout.write(
      `Relay Console bridge enrolled${workspaceName ? ` for ${workspaceName}` : ""} as ${deviceLabel}.\n`,
     );
     process.stdout.write("Restart OpenClaw using your existing runtime lifecycle.\n");
   });

   relay
    .command("rotate-credential")
    .description("Rotate the saved Relay Console bridge device credential")
    .option("--account <id>", "Relay Console channel account ID", "default")
    .action(async (options: { account: string }) => {
     const cfg = api.runtime.config.loadConfig();
     const accountId = options.account.trim() || "default";
     const account = relayAccountConfig(cfg, accountId);
     const apiUrl = requiredEnrollmentString(account.apiUrl, "API URL");
     const devicePublicId = requiredEnrollmentString(
      account.devicePublicId,
      "device public ID",
     );
     const savedValue = requiredEnrollmentString(
      account.deviceToken,
      "device credential",
     );
     const response = await rotateBridgeDeviceCredential({
      apiUrl,
      devicePublicId,
      deviceToken: savedValue,
     });
     const nextConfig = applyBridgeCredentialRotationToConfig(cfg, {
      accountId,
      response,
     });
     await api.runtime.config.writeConfigFile(nextConfig);
     process.stdout.write(
      "Relay Console bridge credential rotated and saved. Restart OpenClaw using your existing runtime lifecycle.\n",
     );
    });
  },
  { commands: ["relay-console"] },
 );
}
