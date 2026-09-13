import type { TokenBudget } from "../types";

export interface PortalStatus {
  enabled: boolean;
  device_registration_enabled: boolean;
  signed_in: boolean;
}
export interface PortalAccount {
  id: string;
  username: string;
  name: string;
  status: "pending" | "active" | "suspended";
  created: number;
  activated_at: number | null;
  last_login: number | null;
}
export interface PortalRoute {
  name: string;
  purpose: string;
  ready: boolean;
}
export interface PortalLevel {
  id: string;
  name: string;
  description: string;
  routes: PortalRoute[];
  model_patterns: string[];
  allow_cloud: boolean;
  allow_direct_models: boolean;
  token_budget: TokenBudget | null;
  max_concurrency: number | null;
}
export interface PortalUsage {
  window_start: number;
  window_seconds: number;
  max_tokens: number | null;
  used: number;
  reserved: number;
  active_requests: number;
  max_concurrency: number | null;
  resets_at: number;
}
export interface PortalKey {
  id: string;
  name: string;
  created: number;
  last_used: number | null;
}
export interface ObservedDevice {
  source_address: string;
  software: string;
  reported_name: string;
  last_seen: number;
  last_path: string;
  via: "key" | "device";
  credential_name: string;
}
export interface RegisteredDevice {
  id: string;
  address: string;
  name: string;
  enabled: boolean;
  created: number;
  last_matched: number | null;
  shadowed: boolean;
}
export interface PortalMe {
  account: PortalAccount;
  level: PortalLevel | null;
  usage: PortalUsage | null;
  keys: PortalKey[];
  devices: {
    observed: ObservedDevice[];
    // A list only while device pre-registration is on; null hides the surface.
    registered: RegisteredDevice[] | null;
  };
  source_address: string;
  base_url: string;
}
