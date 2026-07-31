import type { ChannelPlugin, OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk/core";
import { clawChatPlugin } from "./src/channel.js";
import { registerRelayConsoleCli } from "./src/enrollment.js";
import { configureBridgeCredentialPersistence } from "./src/bridge-auth.js";

const plugin = {
  id: "clawchat",
  name: "Relay Console",
  description: "Relay Console bridge channel — receive and reply to AI agent threads",
  configSchema: emptyPluginConfigSchema(),
  register(api: OpenClawPluginApi) {
    configureBridgeCredentialPersistence({
      loadConfig: () => api.runtime.config.loadConfig(),
      writeConfigFile: (config) => api.runtime.config.writeConfigFile(config),
    });
    api.registerChannel({ plugin: clawChatPlugin as ChannelPlugin });
    registerRelayConsoleCli(api);
  },
};

export default plugin;
