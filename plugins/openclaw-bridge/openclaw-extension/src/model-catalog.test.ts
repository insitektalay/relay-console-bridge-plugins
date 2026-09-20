import assert from "node:assert/strict";
import test from "node:test";
import { configuredModelCatalog } from "./model-catalog.js";
test("reports the configured OpenRouter DeepSeek default", () => {
 const catalog = configuredModelCatalog({ agents: { defaults: { model: { primary: "openrouter/deepseek/deepseek-v4.1-flash" } } } });
 assert.equal(catalog?.defaultModel, "openrouter/deepseek/deepseek-v4.1-flash");
 assert.deepEqual(catalog?.entries[0], { id: "openrouter/deepseek/deepseek-v4.1-flash", model: "deepseek/deepseek-v4.1-flash", provider: "openrouter", label: "openrouter/deepseek/deepseek-v4.1-flash", availability: "unknown" });
});
test("does not invent model choices when the runtime is unconfigured", () => {
 assert.equal(configuredModelCatalog({}), null);
});
