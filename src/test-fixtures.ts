import type { Config } from "./types";
export function fixtureConfig(): Config {
  return {
    schema_version: 3,
    revision: 1,
    engines: [],
    routes: [],
    clients: [],
    security: {
      operator_auth_enabled: true,
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
  };
}
