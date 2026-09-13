import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { App } from "./App";
import { fixtureConfig } from "./test-fixtures";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function stubDiscoveryApp(includeLoopback = false) {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const config = fixtureConfig();
  config.discovery.include_loopback = includeLoopback;
  const network = {
    phase: "not_started",
    hosts: [],
    error: "",
    completed_at: 0,
  };
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(`${init?.method || "GET"} ${url}`);
      if (url.startsWith("/api/v1/network")) return jsonResponse(network);
      if (url === "/api/v1/discover")
        return jsonResponse({ id: "fixture-scan" });
      if (url === "/api/v1/state")
        return jsonResponse({
          setup_required: false,
          config,
          engines: [],
          clients: [],
          events: [],
          server_time: 0,
          base_url: "http://router.test/v1",
          environment_label: "Fixture",
          discovery: {
            scanning: false,
            last_scan: 0,
            error: "",
            pending: [],
          },
          network,
        });
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
  return requests;
}

it("puts operator access and route gates on reachable Settings and Routes pages", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const config = fixtureConfig();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            setup_required: true,
            config,
            engines: [],
            clients: [],
            events: [],
            server_time: 0,
            base_url: "http://router.test/v1",
            environment_label: "Fixture",
            discovery: {
              scanning: false,
              last_scan: 0,
              error: "",
              pending: [],
            },
            network: {
              phase: "not_started",
              hosts: [],
              error: "",
              completed_at: 0,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    ),
  );
  render(<App />);
  const user = userEvent.setup();
  expect(await screen.findByText("First-use setup")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Review access" }),
  ).toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "Settings" }));
  expect(
    screen.getByRole("switch", { name: "Operator sign-in" }),
  ).toHaveAttribute("aria-checked", "true");
  expect(
    screen.queryByRole("switch", { name: "Require caller API keys" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: "Discovery" }));
  expect(
    screen.getByRole("switch", { name: "Automatic discovery" }),
  ).toHaveAttribute("aria-checked", "false");
  expect(
    screen.getByRole("switch", { name: "Inspect all open ports as HTTP" }),
  ).toHaveAttribute("aria-checked", "false");
});

it("takes an unscoped discovery request to the exact Settings control without scanning", async () => {
  const requests = stubDiscoveryApp();
  render(<App />);
  const user = userEvent.setup();
  const overviewAction = (
    await screen.findByRole("heading", {
      name: "Your engines",
    })
  ).closest<HTMLElement>(".section-heading");
  expect(overviewAction).not.toBeNull();
  expect(
    within(overviewAction!).getByRole("button", {
      name: "Set discovery scope",
    }),
  ).toBeVisible();
  await user.click(await screen.findByRole("button", { name: "Network" }));
  expect(
    await screen.findByText("Set a discovery scope before scanning"),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Set discovery scope" }));
  expect(
    await screen.findByRole("heading", { name: "Settings" }),
  ).toBeVisible();
  expect(
    screen.getByRole("tab", { name: "Discovery", selected: true }),
  ).toBeVisible();
  expect(screen.getByLabelText("Where to look")).toHaveFocus();
  expect(requests).not.toContain("POST /api/v1/discover");
});

it("keeps Discover available for a loopback-only discovery scope", async () => {
  const requests = stubDiscoveryApp(true);
  render(<App />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Network" }));
  await user.click(screen.getByRole("button", { name: "Discover" }));
  expect(requests).toContain("POST /api/v1/discover");
});

it("requires an explicit operation before connecting a pending discovery with no proof", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const config = fixtureConfig();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/network"))
        return jsonResponse({
          phase: "complete",
          hosts: [],
          error: "",
          completed_at: 0,
        });
      if (url === "/api/v1/state")
        return jsonResponse({
          setup_required: false,
          config,
          engines: [],
          clients: [],
          events: [],
          server_time: 0,
          base_url: "http://router.test/v1",
          environment_label: "Fixture",
          discovery: {
            scanning: false,
            last_scan: 0,
            error: "",
            pending: [
              {
                name: "Pending catalog",
                base_url: "https://pending.example/v1",
                models: [],
                capabilities: ["text"],
                catalog_protocol: "openai",
                completion_paths: [],
                seen_at: 0,
              },
            ],
          },
          network: {
            phase: "complete",
            hosts: [],
            error: "",
            completed_at: 0,
          },
        });
      throw new Error(`Unexpected request: ${url}`);
    }),
  );

  render(<App />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Connect" }));

  expect(
    screen.getByRole("button", {
      name: "Chat completions (/chat/completions)",
    }),
  ).toHaveAttribute("aria-pressed", "false");
  expect(
    screen.getByRole("button", {
      name: "Text completions (/completions)",
    }),
  ).toHaveAttribute("aria-pressed", "false");
  expect(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "Connect engine",
    }),
  ).toBeDisabled();
});

it("opens a catalog-less completion endpoint with an empty declared inventory", async () => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const config = fixtureConfig();
  config.discovery.targets = ["198.51.100.0/24"];
  const network = {
    phase: "complete",
    hosts: [
      {
        address: "198.51.100.20",
        name: "discovered-host.example",
        status: "up",
        scope: "network",
        evidence: "responded",
        ports: [8123],
        scan_complete: true,
        services: [
          {
            origin: "http://198.51.100.20:8123",
            base_url: "http://198.51.100.20:8123/v1",
            compatible_base_url: "http://198.51.100.20:8123/v1",
            completion_paths: ["/chat/completions"],
            port: 8123,
            status: "model_surface",
            protocol: "openai",
            models: [],
            capabilities: ["text", "streaming"],
            detail: "Serving operations found; no model catalog was published",
            checked_at: 1,
            needs_model_identity: true,
            registration_eligible: false,
            catalog_attempts: [{ path: "/v1/models", status: 404 }],
          },
        ],
      },
    ],
    error: "",
    completed_at: 1,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/network")) return jsonResponse(network);
      if (url === "/api/v1/state")
        return jsonResponse({
          setup_required: false,
          config,
          engines: [],
          clients: [],
          events: [],
          server_time: 0,
          base_url: "http://router.test/v1",
          environment_label: "Fixture",
          discovery: {
            scanning: false,
            last_scan: 0,
            error: "",
            pending: [],
          },
          network,
        });
      throw new Error(`Unexpected request: ${url}`);
    }),
  );

  render(<App />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Network" }));
  await user.click(
    await screen.findByRole("button", { name: /198\.51\.100\.20/i }),
  );
  await user.click(screen.getByRole("button", { name: "Review and connect" }));

  expect(
    await screen.findByRole("heading", { name: "Connect an engine" }),
  ).toBeVisible();
  expect(screen.getByRole("textbox", { name: /^Preferred URL/ })).toHaveValue(
    "http://198.51.100.20:8123/v1",
  );
  expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue(
    "discovered-host.example",
  );
  expect(
    screen.getByRole("textbox", { name: /^Declared model IDs/ }),
  ).toHaveValue("");
  expect(
    screen.getByRole("button", {
      name: "Chat completions (/chat/completions)",
    }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(
    screen.getByRole("button", { name: "Text completions (/completions)" }),
  ).toHaveAttribute("aria-pressed", "false");
  expect(screen.getByRole("button", { name: "Connect engine" })).toBeDisabled();
});
