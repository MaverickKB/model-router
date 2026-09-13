import { expect, it } from "vitest";
import { engineDraftFromNetworkService } from "./adoption";
import type { NetworkService } from "./types";

it("keeps published completion identities as an exact declared routing allowlist", () => {
  const service: NetworkService = {
    origin: "https://completion.example",
    name: "OpenAPI document title",
    base_url: "https://completion.example/v1",
    compatible_base_url: "https://completion.example/v1",
    port: 443,
    status: "model_surface",
    protocol: "openai",
    models: [
      { id: "published-model", capabilities: ["text"] },
      { id: "published-model", capabilities: ["text"] },
    ],
    capabilities: ["text", "streaming"],
    detail: "No catalog",
    checked_at: 1,
    needs_model_identity: false,
    registration_eligible: false,
    completion_paths: ["/completions"],
  };

  expect(
    engineDraftFromNetworkService(service, "discovered-host.example"),
  ).toMatchObject({
    name: "discovered-host.example",
    source: "discovery",
    name_source: "discovered",
    base_url: "https://completion.example/v1",
    capabilities: ["text", "streaming"],
    model_inventory_source: "declared",
    declared_models: ["published-model"],
    model_patterns: ["published-model"],
    completion_paths: ["/completions"],
  });
});

it("opens a manual engine with a blank transport when no compatible API URL was derived", () => {
  const service: NetworkService = {
    origin: "https://unknown-completion.example",
    base_url: "https://unknown-completion.example/v1",
    port: 443,
    status: "model_surface",
    protocol: "openapi",
    models: [],
    capabilities: ["text"],
    detail: "Completion operation found without a compatible API URL",
    checked_at: 1,
    needs_model_identity: true,
    registration_eligible: false,
  };

  expect(engineDraftFromNetworkService(service)).toMatchObject({
    name: "unknown-completion.example",
    base_url: "",
    source: "manual",
    name_source: "discovered",
    model_inventory_source: "catalog",
    declared_models: [],
    model_patterns: ["*"],
    completion_paths: [],
  });
});

it.each([
  [
    "a verified catalog",
    {
      origin: "https://catalog-without-operations.example",
      observed_base_url: "https://catalog-without-operations.example/v1",
      base_url: "https://catalog-without-operations.example/v1",
      compatible_base_url: "https://catalog-without-operations.example/v1",
      port: 443,
      status: "model_service",
      protocol: "openai",
      models: [{ id: "catalog-model", capabilities: ["text"] }],
      capabilities: ["text"],
      detail: "Catalog observed without an exact completion operation",
      checked_at: 1,
      registration_eligible: true,
    },
  ],
  [
    "a catalog-less completion surface",
    {
      origin: "https://surface-without-operations.example",
      observed_base_url: "https://surface-without-operations.example/v1",
      base_url: "https://surface-without-operations.example/v1",
      compatible_base_url: "https://surface-without-operations.example/v1",
      port: 443,
      status: "model_surface",
      protocol: "openai",
      models: [{ id: "surface-model", capabilities: ["text"] }],
      capabilities: ["text"],
      detail: "Published identity without an exact completion operation",
      checked_at: 1,
      needs_model_identity: false,
      registration_eligible: false,
      completion_paths: [],
    },
  ],
  [
    "a compatible protected surface",
    {
      origin: "https://protected-without-operations.example",
      observed_base_url: "https://protected-without-operations.example/v1",
      base_url: "",
      compatible_base_url: "https://protected-without-operations.example/v1",
      port: 443,
      status: "authentication_required",
      protocol: "openai",
      models: [],
      capabilities: ["text"],
      detail: "Credentials required before an exact operation was proven",
      checked_at: 1,
      registration_eligible: false,
    },
  ],
] satisfies [string, NetworkService][])(
  "opens $0 without assuming OpenAI operations",
  (_label, service) => {
    expect(engineDraftFromNetworkService(service)).toMatchObject({
      name_source: "discovered",
      base_url: "",
      source: "manual",
      completion_paths: [],
    });
  },
);

it("opens a manual engine with a blank transport for native inventory", () => {
  const service: NetworkService = {
    origin: "https://native-inventory.example",
    base_url: "https://native-inventory.example/v1",
    port: 443,
    status: "native_inventory",
    protocol: "native",
    models: [{ id: "native-model", capabilities: [] }],
    capabilities: [],
    detail: "Native model inventory found; transport was not verified",
    checked_at: 1,
    registration_eligible: false,
  };

  expect(
    engineDraftFromNetworkService(service, "native-host.example"),
  ).toMatchObject({
    name: "native-host.example",
    base_url: "",
    source: "manual",
    name_source: "discovered",
    model_inventory_source: "catalog",
    declared_models: [],
    model_patterns: ["*"],
  });
});

it("opens a manual engine with a blank transport for a nonstandard data catalog", () => {
  const service: NetworkService = {
    origin: "https://data-catalog.example",
    base_url: "https://data-catalog.example/models",
    port: 443,
    status: "http_service",
    protocol: "openai",
    models: [{ id: "published-data-model", capabilities: [] }],
    capabilities: [],
    detail:
      "Model catalog found, but no non-root OpenAI-compatible API base was declared",
    checked_at: 1,
    registration_eligible: false,
  };

  expect(
    engineDraftFromNetworkService(service, "data-catalog-host.example"),
  ).toMatchObject({
    name: "data-catalog-host.example",
    base_url: "",
    source: "manual",
    name_source: "discovered",
    model_inventory_source: "catalog",
    declared_models: [],
    model_patterns: ["*"],
  });
});

it("prepopulates a protected compatible API base only after an operation is proven", () => {
  const service: NetworkService = {
    surface_id: "https://protected.example/v1",
    origin: "https://protected.example",
    observed_base_url: "https://protected.example/v1",
    compatible_base_url: "https://protected.example/v1",
    base_url: "",
    port: 443,
    status: "authentication_required",
    protocol: "openai",
    models: [],
    capabilities: ["text", "streaming"],
    detail: "Credentials are required before the catalog can be read",
    checked_at: 1,
    registration_eligible: false,
    completion_paths: ["/chat/completions"],
  };

  expect(
    engineDraftFromNetworkService(service, "protected-host.example"),
  ).toMatchObject({
    name: "protected-host.example",
    base_url: "https://protected.example/v1",
    source: "manual",
    name_source: "discovered",
    model_inventory_source: "catalog",
    declared_models: [],
    model_patterns: ["*"],
    completion_paths: ["/chat/completions"],
  });
});

it("leaves an access-gated API blank when discovery did not prove a routeable transport", () => {
  const service: NetworkService = {
    origin: "https://access-gated.example",
    observed_base_url: "https://access-gated.example/v1",
    base_url: "https://access-gated.example/v1",
    compatible_base_url: "",
    port: 443,
    status: "authentication_required",
    protocol: "openapi",
    models: [],
    capabilities: [],
    detail:
      "Credentials were required before the transport contract was proven",
    checked_at: 1,
    registration_eligible: false,
  };

  expect(
    engineDraftFromNetworkService(service, "access-gated-host.example"),
  ).toMatchObject({
    name: "access-gated-host.example",
    base_url: "",
    source: "manual",
    name_source: "discovered",
    model_inventory_source: "catalog",
    declared_models: [],
    model_patterns: ["*"],
  });
});
