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
  ).toHaveAttribute("aria-checked", "false");
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
  const overviewAction = (await screen.findByRole("heading", {
    name: "Your engines",
  })).closest<HTMLElement>(".section-heading");
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
  await user.click(
    screen.getByRole("button", { name: "Set discovery scope" }),
  );
  expect(await screen.findByRole("heading", { name: "Settings" })).toBeVisible();
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
