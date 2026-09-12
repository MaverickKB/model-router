import type { Client, Engine, Route } from "../types";

function newId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}
export const newEngine = (): Engine => ({
  id: newId(),
  name: "",
  base_url: "",
  aliases: [],
  kind: "local",
  protocol: "openai",
  catalog_protocol: "openai",
  name_source: "operator",
  enabled: true,
  draining: false,
  source: "manual",
  tags: [],
  members: [],
  capabilities: ["text", "streaming"],
  model_settings: {},
  model_patterns: ["*"],
  unsupported_parameters: [],
  value_mappings: {},
  timeout_seconds: 300,
  max_inflight: 32,
  max_response_bytes: 8388608,
  failure_cooldown_seconds: 15,
  rate_limit_cooldown_seconds: 5,
});
export const newRoute = (): Route => ({
  id: newId(),
  name: "",
  purpose: "",
  enabled: true,
  primary: { kind: "local", engine_ids: [], model_patterns: ["*"], tags: [] },
  fallback: null,
  strategy: "least_busy",
  require_caller_key: false,
  defaults: {},
});
export const newClient = (): Client => ({
  id: newId(),
  name: "",
  kind: null,
  enabled: true,
  route_names: ["auto"],
  engine_ids: [],
  model_patterns: ["*"],
  allow_cloud: false,
  allow_direct_models: false,
  allow_network_auth: false,
  source_networks: [],
});
