import type { ChannelPlugin, OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import { emptyPluginConfigSchema } from "openclaw/plugin-sdk/core";
import { clawChatPlugin } from "./src/channel.js";
import { registerRelayConsoleCli } from "./src/enrollment.js";

const plugin = {
  id: "clawchat",
  name: "Relay Console",
  description: "Relay Console bridge channel — receive and reply to AI agent threads",
  configSchema: emptyPluginConfigSchema(),
  register(api: OpenClawPluginApi) {
    api.registerChannel({ plugin: clawChatPlugin as ChannelPlugin });
    registerRelayConsoleCli(api);
  },
};

export default plugin;
