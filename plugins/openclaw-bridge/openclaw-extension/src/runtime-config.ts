import type { OpenClawConfig } from "openclaw/plugin-sdk/core";

type ConfigRuntime = {
 current?: () => unknown;
 loadConfig?: () => OpenClawConfig;
 mutateConfigFile?: (params: { mutate: (draft: OpenClawConfig) => void; afterWrite: { mode: "none"; reason: string } }) => Promise<unknown>;
 writeConfigFile?: (config: OpenClawConfig) => Promise<void>;
};

export function readBridgeRuntimeConfig(runtime: ConfigRuntime): OpenClawConfig {
 if (runtime.current) return structuredClone(runtime.current()) as OpenClawConfig;
 if (runtime.loadConfig) return runtime.loadConfig();
 throw new Error("OpenClaw config read API is unavailable");
}

export async function writeBridgeRuntimeConfig(runtime: ConfigRuntime, config: OpenClawConfig): Promise<void> {
 if (runtime.mutateConfigFile) {
  await runtime.mutateConfigFile({
   mutate(draft) {
    draft.channels ??= {};
    draft.channels.clawchat = structuredClone(config.channels?.clawchat);
   },
   afterWrite: { mode: "none", reason: "Bridge owns credential refresh; enrollment requests an explicit runtime restart." },
  });
  return;
 }
 if (runtime.writeConfigFile) return runtime.writeConfigFile(config);
 throw new Error("OpenClaw config write API is unavailable");
}
