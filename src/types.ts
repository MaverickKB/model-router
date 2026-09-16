export type Kind = "local" | "cloud";
export interface Model {
  enabled?: boolean;
  id: string;
  capabilities: string[];
  context_length: number | null;
}
export interface ModelSettings {
  enabled: boolean;
  capabilities: string[];
  context_length: number | null;
}
export interface Engine {
  id: string;
  name: string;
  base_url: string;
  aliases: string[];
  kind: Kind;
  protocol: "openai";
  catalog_protocol: string;
  name_source: "operator" | "discovered";
  enabled: boolean;
  draining: boolean;
  source: string;
  tags: string[];
  members: string[];
  capabilities: string[];
  model_settings: Record<string, ModelSettings>;
  model_patterns: string[];
  unsupported_parameters: string[];
  value_mappings: Record<string, Record<string, string>>;
  timeout_seconds: number;
  max_inflight: number;
  max_response_bytes: number;
  failure_cooldown_seconds: number;
  rate_limit_cooldown_seconds: number;
  credential_type: "static" | "xai_oauth" | "codex_oauth";
}
export interface EngineView extends Engine {
  models: Model[];
  has_credential: boolean;
  status: string;
  checked_at: number;
  observed_at: number;
  latency_ms: number | null;
  error: string;
  inflight: number;
  last_success: number | null;
}
export interface Selector {
  kind: Kind | "any";
  engine_ids: string[];
  model_patterns: string[];
  tags: string[];
}
export interface Route {
  id: string;
  name: string;
  purpose: string;
  enabled: boolean;
  primary: Selector;
  fallback: Selector | null;
  strategy: "ordered" | "least_busy";
  require_caller_key: boolean;
  defaults: Record<string, unknown>;
}
export interface Client {
  id: string;
  name: string;
  kind?: "shared" | "agent" | "machine" | "person" | null;
  enabled: boolean;
  route_names: string[];
  engine_ids: string[];
  model_patterns: string[];
  allow_cloud: boolean;
  allow_direct_models: boolean;
  allow_network_auth: boolean;
  source_networks: string[];
  has_key?: boolean;
}
export interface Discovery {
  enabled: boolean;
  targets: string[];
  port_range: string;
  scanner: "connect" | "nmap";
  inspect_all_open_ports: boolean;
  http_ports: number[];
  include_loopback: boolean;
  max_addresses: number;
  network_interval_seconds: number;
  packets_per_second: number;
  interval_seconds: number;
  refresh_seconds: number;
  stale_seconds: number;
  auto_register: boolean;
  mdns: boolean;
  ignored_urls: string[];
}
export interface Security {
  operator_auth_enabled: boolean;
  client_auth_enabled: boolean;
  operator_networks: string[];
  anonymous_client_id: string | null;
  session_hours: number;
}
export interface TokenBudget {
  max_tokens: number;
  window_seconds: number;
}
export interface AccountLevel {
  id: string;
  name: string;
  description: string;
  route_names: string[];
  engine_ids: string[];
  model_patterns: string[];
  allow_cloud: boolean;
  allow_direct_models: boolean;
  token_budget: TokenBudget | null;
  max_concurrency: number | null;
}
export interface AccountsSettings {
  enabled: boolean;
  device_registration_enabled: boolean;
  session_hours: number;
}
export interface UpdatesSettings {
  check_enabled: boolean;
  repository: string;
}
export interface ReleaseStatus {
  current: string;
  check_enabled: boolean;
  repository: string;
  available: boolean;
  latest: string | null;
  notes: string;
  html_url: string;
  error: string;
}
export interface Config {
  schema_version: 5;
  upgraded_from_schema?: number | null;
  security: Security;
  revision: number;
  engines: Engine[];
  routes: Route[];
  clients: Client[];
  discovery: Discovery;
  accounts: AccountsSettings;
  account_levels: AccountLevel[];
  updates: UpdatesSettings;
}
export interface Candidate {
  engine_id: string;
  engine: string;
  model: string;
  kind: Kind;
  tier: string;
  reason: string;
  skipped_defaults?: string[];
  status?: string;
  http_status?: number;
  error?: string;
}
export interface Decision {
  route?: string;
  client?: string;
  candidates: Candidate[];
  rejections: {
    engine_id: string;
    engine: string;
    model: string | null;
    tier: string;
    reason: string;
  }[];
  error?: string;
  required_capabilities?: string[];
}
export interface ObservedCaller {
  id: string;
  policy_id: string | null;
  account_id?: string | null;
  key_id?: string | null;
  device_id?: string | null;
  name: string;
  source_key?: string;
  source_label?: string;
  source_label_source?:
    | "operator"
    | "reported_hostname"
    | "discovered_hostname"
    | "address";
  source_hostname?: string;
  source_identity_quality?: "network_hardware" | "reported_device" | "address";
  source_address: string;
  source_port?: number | null;
  software: string;
  reported_name: string;
  reported_name_source?: string;
  identity_quality?:
    "policy_key" | "self_reported" | "runtime_hints" | "transport_only";
  client_family?: string;
  client_version?: string;
  client_runtime?: string;
  client_os?: string;
  client_arch?: string;
  identity_hints?: Record<string, string>;
  identity_basis:
    | "api_key"
    | "account_key"
    | "registered_device"
    | "source_network"
    | "shared_access"
    | "unassigned"
    | "operator_test";
  first_seen?: number;
  request_count?: number;
  last_seen: number;
  last_method?: string;
  last_path: string;
  recent_source_ports?: number[];
  recent_source_addresses?: string[];
}
export interface Job {
  caller?: ObservedCaller | null;
  id: string;
  ts: number;
  client_id: string;
  client: string;
  requested: string;
  status: string;
  attempts: Candidate[];
  decision: Decision;
  engine_id?: string;
  engine?: string;
  model?: string;
  tier?: string;
  elapsed_ms?: number;
  http_status?: number;
  stream: boolean;
  usage?: {
    prompt_tokens: number;
    completion_tokens: number;
    estimated: boolean;
  };
  limit?: { code: string; retry_after: number | null };
}
export interface State {
  setup_required: boolean;
  observed_callers?: ObservedCaller[];
  route_map?: RouteMap;
  warnings?: string[];
  account_levels_in_use?: Record<string, number>;
  network: import("./network/types").NetworkReport;
  environment_label: string;
  operator_url?: string | null;
  base_url: string;
  config: Config;
  engines: EngineView[];
  engine_merge_suggestions?: EngineMergeSuggestion[];
  clients: Client[];
  events: Job[];
  discovery: {
    scanning: boolean;
    last_scan: number;
    error: string;
    pending: {
      capabilities: string[];
      catalog_protocol: Engine["catalog_protocol"];
      name: string;
      base_url: string;
      models: Model[];
      seen_at: number;
    }[];
  };
  server_time: number;
}
export interface EngineMergeSuggestion {
  source_id: string;
  target_id: string;
  source_name: string;
  target_name: string;
  reasons: string[];
}
export interface ReadyPath {
  engine_id: string;
  tier: "primary" | "fallback";
}
export interface CallerRoute {
  caller_id: string;
  policy_id?: string | null;
  route_id: string;
  ready_engines: string[];
  ready_paths?: ReadyPath[];
  reason: string;
}
export interface PolicyRoute {
  policy_id: string;
  route_id: string;
  ready_engines: string[];
  ready_paths?: ReadyPath[];
  reason: string;
}
export interface RouteMap {
  policies?: {
    policy_id: string;
    observed_callers: ObservedCaller[];
  }[];
  account_callers?: {
    account_id: string;
    name: string;
    level_id: string | null;
    observed_callers: ObservedCaller[];
  }[];
  caller_routes: CallerRoute[];
  policy_routes?: PolicyRoute[];
  route_engines: {
    route_id: string;
    engine_id: string;
    tier: "primary" | "fallback";
    dynamic: boolean;
    models: string[];
    ready: boolean;
  }[];
}
export interface AccountUsage {
  window_start: number;
  window_seconds: number;
  max_tokens: number | null;
  used: number;
  reserved: number;
  active_requests: number;
  max_concurrency: number | null;
  resets_at: number;
}
export interface AccountSummary {
  id: string;
  username: string;
  name: string;
  level_id: string;
  status: "pending" | "active" | "suspended";
  created: number;
  activated_at: number | null;
  last_login: number | null;
  activation_pending: boolean;
  activation_expires: number | null;
  key_count: number;
  device_count: number;
  usage: AccountUsage | null;
}
export interface AccountKey {
  id: string;
  name: string;
  created: number;
  last_used: number | null;
}
export interface RegisteredDevice {
  id: string;
  account_id: string;
  address: string;
  name: string;
  enabled: boolean;
  created: number;
  last_matched: number | null;
  account_name?: string;
  shadows?: { client_id: string; client_name: string; network: string }[];
}
export interface UsageWindow {
  window_start: number;
  window_seconds: number;
  prompt_tokens: number;
  completion_tokens: number;
  estimated_tokens: number;
  requests: number;
}
export interface AccountDetail {
  account: AccountSummary;
  keys: AccountKey[];
  devices: RegisteredDevice[];
  usage_windows: UsageWindow[];
}
export interface ActivationLink {
  token: string;
  url: string;
  expires: number;
}
