import { vi } from "vitest";
import type { AccountSummary, Config } from "./types";
export function fixtureConfig(): Config {
  return {
    schema_version: 5,
    revision: 1,
    engines: [],
    routes: [],
    clients: [],
    security: {
      operator_auth_enabled: false,
      client_auth_enabled: true,
      anonymous_client_id: null,
      operator_networks: ["127.0.0.1/32", "::1/128"],
      session_hours: 168,
    },
    discovery: {
      enabled: false,
      targets: [],
      port_range: "8000-8100",
      scanner: "connect",
      inspect_all_open_ports: false,
      http_ports: [],
      include_loopback: false,
      max_addresses: 4096,
      network_interval_seconds: 1800,
      packets_per_second: 1500,
      interval_seconds: 60,
      refresh_seconds: 10,
      stale_seconds: 35,
      auto_register: false,
      mdns: false,
      ignored_urls: [],
    },
    accounts: {
      enabled: false,
      device_registration_enabled: false,
      session_hours: 168,
    },
    account_levels: [],
    updates: {
      check_enabled: true,
      repository: "MaverickKB/model-router",
    },
  };
}
export function fixtureAccount(
  overrides: Partial<AccountSummary> = {},
): AccountSummary {
  return {
    id: "acct-1",
    username: "dana",
    name: "Dana",
    level_id: "level-1",
    status: "active",
    created: 1800000000,
    activated_at: 1800000100,
    last_login: 1800000200,
    activation_pending: false,
    activation_expires: null,
    key_count: 1,
    device_count: 0,
    usage: {
      window_start: 1800000000,
      window_seconds: 86400,
      max_tokens: 200000,
      used: 42100,
      reserved: 0,
      active_requests: 1,
      max_concurrency: 2,
      resets_at: 1800086400,
    },
    ...overrides,
  };
}
export type FetchCall = { url: string; method: string; body?: unknown };
/** Stub fetch with a JSON handler; returns every call in order for assertions. */
export function mockJsonFetch(
  handler: (call: FetchCall) => unknown,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const call: FetchCall = {
        url,
        method: init?.method || "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      };
      calls.push(call);
      const value = handler(call);
      if (value instanceof Response) return value;
      return new Response(JSON.stringify(value ?? {}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}
