import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { api } from "../api";
import type { NetworkReport, NetworkService } from "./types";
import { NetworkView } from "./NetworkView";

vi.mock("../api", () => ({ api: vi.fn() }));

function reportFor(service: NetworkService, hostName = ""): NetworkReport {
  return {
    phase: "complete",
    hosts: [
      {
        address: "198.51.100.20",
        name: hostName,
        status: "up",
        scope: "network",
        evidence: "responded",
        ports: [8123],
        services: [service],
        scan_complete: true,
      },
    ],
    error: "",
    completed_at: 1,
  };
}

it("separates a catalog-less completion endpoint from verified model catalogs", async () => {
  const service: NetworkService = {
    origin: "http://198.51.100.20:8123",
    base_url: "http://198.51.100.20:8123/v1",
    compatible_base_url: "http://198.51.100.20:8123/v1",
    port: 8123,
    status: "model_surface",
    protocol: "openai",
    models: [],
    capabilities: ["text", "streaming"],
    detail: "Serving operations found; no model catalog was published",
    checked_at: 1,
    needs_model_identity: true,
    registration_eligible: false,
    catalog_attempts: [
      { path: "/v1/models", status: 404 },
      { path: "/models", status: 404 },
    ],
  };
  const report = reportFor(service);
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Verified catalogs" }));
  expect(
    screen.queryByRole("button", { name: /198\.51\.100\.20/i }),
  ).not.toBeInTheDocument();

  await user.click(
    screen.getByRole("button", { name: "Completion endpoints" }),
  );
  await user.click(screen.getByRole("button", { name: /198\.51\.100\.20/i }));

  expect(
    screen.getByText(/Completion endpoint, no model catalog/),
  ).toBeVisible();
  expect(
    screen.getByText(
      /This API base documents a compatible JSON completion request and OpenAI choices response, but did not publish a readable model catalog/,
    ),
  ).toBeVisible();
  expect(
    screen.getAllByText("http://198.51.100.20:8123/v1").length,
  ).toBeGreaterThan(0);
  expect(screen.getByText("/v1/models: HTTP 404")).toBeVisible();

  await user.click(screen.getByRole("button", { name: "Review and connect" }));
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(service, "");
});

it("offers review when the completion endpoint published explicit model identities", async () => {
  const service: NetworkService = {
    origin: "https://completion.example",
    base_url: "https://completion.example/v1",
    compatible_base_url: "https://completion.example/v1",
    port: 443,
    status: "model_surface",
    protocol: "openai",
    models: [
      {
        id: "published-model-id",
        capabilities: ["text"],
      },
    ],
    capabilities: ["text"],
    detail: "Completion operation with an explicit model identity",
    checked_at: 1,
    needs_model_identity: false,
    registration_eligible: false,
  };
  const report = reportFor(service);
  vi.mocked(api).mockResolvedValue(report);
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={vi.fn()}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /198\.51\.100\.20/i }));

  expect(
    screen.getByRole("button", { name: "Review and connect" }),
  ).toBeVisible();
  expect(
    screen.getByText("Published identity, not a catalog · text"),
  ).toBeVisible();
});

it("does not fabricate an OpenAI URL when inspection could not derive one", async () => {
  const service: NetworkService = {
    origin: "https://unknown-completion.example",
    base_url: "",
    port: 443,
    status: "model_surface",
    protocol: "openapi",
    models: [],
    capabilities: ["text"],
    detail: "Completion operation found, but no compatible base URL",
    checked_at: 1,
    needs_model_identity: true,
    registration_eligible: false,
  };
  const report = reportFor(service);
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /198\.51\.100\.20/i }));

  expect(
    screen.getByText(
      /No compatible OpenAI API base was derived\. Review a documented endpoint manually before connecting it\./,
    ),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Review endpoint manually" }),
  );
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(service, "");
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();
});

it("keeps native inventory visible without presenting it as a verified catalog", async () => {
  const service: NetworkService = {
    origin: "https://native-inventory.example",
    base_url: "",
    port: 443,
    status: "native_inventory",
    protocol: "native",
    models: [{ id: "native-model-id", capabilities: [] }],
    capabilities: [],
    detail: "Native model inventory found; transport was not verified",
    checked_at: 1,
    registration_eligible: false,
  };
  const report = reportFor(service, "native-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Verified catalogs" }));
  expect(
    screen.queryByRole("button", { name: /198\.51\.100\.20/i }),
  ).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Responding" }));
  await user.click(
    screen.getByRole("button", { name: /native-host\.example/i }),
  );
  expect(
    screen.getByText(/Native model inventory, OpenAI transport unverified/),
  ).toBeVisible();
  expect(screen.getByText("native-model-id")).toBeVisible();
  expect(
    screen.getByText(
      /Native inventory, OpenAI transport unverified · Inventory only, chat routing unavailable/,
    ),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Review endpoint manually" }),
  );
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    service,
    "native-host.example",
  );
});

it("keeps a nonstandard model catalog visible with a manual transport path", async () => {
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
    catalog_attempts: [{ path: "/models", status: 200 }],
  };
  const report = reportFor(service, "data-catalog-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Verified catalogs" }));
  expect(
    screen.queryByRole("button", { name: /data-catalog-host\.example/i }),
  ).not.toBeInTheDocument();

  await user.click(
    screen.getByRole("button", { name: "Completion endpoints" }),
  );
  expect(
    screen.queryByRole("button", { name: /data-catalog-host\.example/i }),
  ).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Responding" }));
  await user.click(
    screen.getByRole("button", { name: /data-catalog-host\.example/i }),
  );
  expect(
    screen.getByText(/Published model inventory, OpenAI transport unverified/),
  ).toBeVisible();
  expect(screen.getByText("published-data-model")).toBeVisible();
  expect(
    screen.getByText(
      /Published inventory, OpenAI transport unverified · Inventory only, chat routing unavailable/,
    ),
  ).toBeVisible();
  expect(
    screen.getByText(
      /This API base published model identities, but discovery did not prove a compatible JSON completion request and OpenAI choices response/,
    ),
  ).toBeVisible();
  expect(screen.getByText("/models: HTTP 200")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();

  await user.click(
    screen.getByRole("button", { name: "Review endpoint manually" }),
  );
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    service,
    "data-catalog-host.example",
  );
});

it("does not treat catalog refresh bookkeeping as a verified catalog", async () => {
  const service: NetworkService = {
    origin: "https://refresh-bookkeeping.example",
    base_url: "https://refresh-bookkeeping.example/metadata",
    port: 443,
    status: "http_service",
    protocol: "http",
    models: [],
    capabilities: [],
    detail: "A refresh target was recorded without a verified model catalog",
    checked_at: 1,
    registration_eligible: false,
    catalog_tracked: true,
  };
  const report = reportFor(service, "refresh-bookkeeping-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Verified catalogs" }));
  expect(
    screen.queryByRole("button", {
      name: /refresh-bookkeeping-host\.example/i,
    }),
  ).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Responding" }));
  await user.click(
    screen.getByRole("button", { name: /refresh-bookkeeping-host\.example/i }),
  );
  expect(
    screen.getByText(/HTTP service, no compatible completion transport/),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Review endpoint manually" }),
  );
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    service,
    "refresh-bookkeeping-host.example",
  );
});

it("keeps an unrouteable authentication challenge out of completion routing", async () => {
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
  const report = reportFor(service, "access-gated-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();

  await user.click(
    screen.getByRole("button", { name: "Completion endpoints" }),
  );
  expect(
    screen.queryByRole("button", { name: /access-gated-host\.example/i }),
  ).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Responding" }));
  await user.click(
    screen.getByRole("button", { name: /access-gated-host\.example/i }),
  );
  expect(screen.getByText(/Access required before inspection/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();
  await user.click(
    screen.getByRole("button", { name: "Review access requirements" }),
  );
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    service,
    "access-gated-host.example",
  );
});

it("keeps independent API bases at one origin distinct and opens the correct review path", async () => {
  const origin = "https://198.51.100.20:8443";
  const unregisteredCatalog: NetworkService = {
    surface_id: `${origin}/v1`,
    origin,
    observed_base_url: `${origin}/v1`,
    base_url: `${origin}/v1`,
    port: 8443,
    status: "model_service",
    protocol: "openai",
    models: [{ id: "catalog-model", capabilities: ["text"] }],
    capabilities: ["text"],
    detail: "Catalog and completion contract are declared for this API base",
    checked_at: 1,
    registration_eligible: true,
  };
  const connectedCatalog: NetworkService = {
    ...unregisteredCatalog,
    surface_id: `${origin}/internal/v1`,
    observed_base_url: `${origin}/internal/v1`,
    base_url: `${origin}/internal/v1`,
    models: [{ id: "connected-model", capabilities: ["text"] }],
    engine_id: "existing-engine",
    engine_name: "Production endpoint",
  };
  const report: NetworkReport = {
    phase: "complete",
    hosts: [
      {
        address: "198.51.100.20",
        name: "model-host.example",
        status: "up",
        scope: "network",
        evidence: "responded",
        ports: [8443],
        services: [unregisteredCatalog, connectedCatalog],
        scan_complete: true,
      },
    ],
    error: "",
    completed_at: 1,
  };
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  const onOpenEngine = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
      onOpenEngine={onOpenEngine}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: /model-host\.example/i }),
  );

  expect(screen.getByText(`${origin}/v1`)).toBeVisible();
  expect(screen.getByText(`${origin}/internal/v1`)).toBeVisible();
  expect(
    screen.getByText("Connected engine Production endpoint"),
  ).toBeVisible();

  await user.click(screen.getByRole("button", { name: "Review and connect" }));
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    unregisteredCatalog,
    "model-host.example",
  );
  await user.click(
    screen.getByRole("button", { name: "Review connected engine" }),
  );
  expect(onOpenEngine).toHaveBeenCalledExactlyOnceWith("existing-engine");
});

it("puts compatible protected endpoints in the completion view with an access review", async () => {
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
    detail:
      "Credentials are required before this API base can publish its model catalog",
    checked_at: 1,
    registration_eligible: false,
    catalog_attempts: [{ path: "/v1/models", status: 401 }],
  };
  const report = reportFor(service, "protected-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConnect = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={onConnect}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Completion endpoints" }),
  );
  await user.click(
    screen.getByRole("button", { name: /protected-host\.example/i }),
  );

  expect(
    screen.getByText(/Completion endpoint, access required/),
  ).toBeVisible();
  expect(
    screen.getByText(
      /credentials are required before its model catalog can be read/i,
    ),
  ).toBeVisible();
  expect(
    screen.getAllByText("https://protected.example/v1").length,
  ).toBeGreaterThan(0);
  await user.click(screen.getByRole("button", { name: "Configure access" }));
  expect(onConnect).toHaveBeenCalledExactlyOnceWith(
    service,
    "protected-host.example",
  );
});

it("shows bounded catalog coverage instead of treating unprobed paths as absent", async () => {
  const service: NetworkService = {
    surface_id: "https://coverage.example/v1",
    origin: "https://coverage.example",
    observed_base_url: "https://coverage.example/v1",
    compatible_base_url: "https://coverage.example/v1",
    base_url: "",
    port: 443,
    status: "model_surface",
    protocol: "openai",
    models: [],
    capabilities: ["text"],
    detail: "Completion operation found without a readable catalog",
    checked_at: 1,
    needs_model_identity: true,
    registration_eligible: false,
    catalog_paths_unprobed: 2,
  };
  const report = reportFor(service, "coverage-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConfigureDiscovery = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={onConfigureDiscovery}
      onConnect={vi.fn()}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: /coverage-host\.example/i }),
  );

  expect(
    screen.getByText(/2 documented catalog paths not checked/),
  ).toBeVisible();
  expect(
    screen.getByText(/did not conclude that those paths have no catalog/),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Review discovery coverage" }),
  );
  expect(onConfigureDiscovery).toHaveBeenCalledExactlyOnceWith();
});

it("does not offer a duplicate connection for a surface already linked to an engine", async () => {
  const service: NetworkService = {
    surface_id: "https://linked.example/v1",
    origin: "https://linked.example",
    observed_base_url: "https://linked.example/v1",
    base_url: "https://linked.example/v1",
    port: 443,
    status: "model_service",
    protocol: "openai",
    models: [{ id: "linked-model", capabilities: ["text"] }],
    capabilities: ["text"],
    detail: "Verified catalog",
    checked_at: 1,
    registration_eligible: true,
    engine_id: "linked-engine",
    engine_name: "Existing engine",
  };
  const report = reportFor(service, "linked-host.example");
  vi.mocked(api).mockResolvedValue(report);
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={vi.fn()}
      onConnect={vi.fn()}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: /linked-host\.example/i }),
  );

  expect(screen.getByText("Connected engine Existing engine")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Review and connect" }),
  ).not.toBeInTheDocument();
});

it("shows catalog coverage limits for a verified catalog too", async () => {
  const service: NetworkService = {
    surface_id: "https://catalog-coverage.example/v1",
    origin: "https://catalog-coverage.example",
    observed_base_url: "https://catalog-coverage.example/v1",
    base_url: "https://catalog-coverage.example/v1",
    port: 443,
    status: "model_service",
    protocol: "openai",
    models: [{ id: "catalog-model", capabilities: ["text"] }],
    capabilities: ["text"],
    detail: "Verified catalog",
    checked_at: 1,
    registration_eligible: true,
    catalog_paths_unprobed: 1,
  };
  const report = reportFor(service, "catalog-coverage-host.example");
  vi.mocked(api).mockResolvedValue(report);
  const onConfigureDiscovery = vi.fn();
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={vi.fn()}
      onConfigureDiscovery={onConfigureDiscovery}
      onConnect={vi.fn()}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", {
      name: /catalog-coverage-host\.example/i,
    }),
  );

  expect(
    screen.getByText(/1 documented catalog path not checked/),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Review discovery coverage" }),
  );
  expect(onConfigureDiscovery).toHaveBeenCalledExactlyOnceWith();
});
