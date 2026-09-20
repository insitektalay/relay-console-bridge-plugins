/** Report configured choices only. Do not claim a hard-coded model is installed. */
export function configuredModelCatalog(config: any, observedAt = new Date().toISOString()) {
 const defaults = config?.agents?.defaults ?? {};
 const configured = defaults.model;
 const primary = typeof configured === "string" ? configured : configured?.primary;
 const safe = (value: unknown): value is string => typeof value === "string" && value.length <= 200 && /^[A-Za-z0-9._:/-]+$/.test(value);
 const models = [...new Set([primary, ...(Array.isArray(configured?.fallbacks) ? configured.fallbacks : []), ...Object.keys(defaults.models ?? {})].filter(safe))] as string[];
 if (!safe(primary) || !models.length) return null;
 return { runtimeType: "openclaw", defaultModel: primary, models, source: "openclaw-configured-models", observedAt,
  entries: models.map(id => ({ id, model: id.startsWith("openrouter/") ? id.slice("openrouter/".length) : id,
   provider: id.startsWith("openrouter/") ? "openrouter" : id.split("/")[0], label: id, availability: "unknown" })) };
}
