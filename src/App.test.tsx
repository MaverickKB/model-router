import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { App } from "./App";
import { fixtureConfig } from "./test-fixtures";

it("puts independent auth controls on the reachable Settings page", async () => {
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
  await user.click(await screen.findByRole("button", { name: "Settings" }));
  expect(
    screen.getByRole("switch", { name: "Operator sign-in" }),
  ).toHaveAttribute("aria-checked", "true");
  expect(
    screen.getByRole("switch", { name: "Require caller API keys" }),
  ).toHaveAttribute("aria-checked", "true");
  await user.click(screen.getByRole("tab", { name: "Discovery" }));
  expect(
    screen.getByRole("switch", { name: "Automatic discovery" }),
  ).toHaveAttribute("aria-checked", "false");
  expect(
    screen.getByRole("switch", { name: "Inspect all open ports as HTTP" }),
  ).toHaveAttribute("aria-checked", "false");
});
