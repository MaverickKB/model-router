import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { NetworkView } from "./NetworkView";
import type { NetworkReport } from "./types";

it("shows the observed API base and lets the operator connect it", async () => {
  const service = {
    origin: "http://provider.test:8000",
    base_url: "http://provider.test:8000/api/v1",
    port: 8000,
    status: "model_service",
    protocol: "openai",
    models: [{ id: "listed-model", capabilities: ["text", "streaming"] }],
    capabilities: ["text", "streaming"],
    detail: "",
    checked_at: 1,
  };
  const report: NetworkReport = {
    phase: "complete",
    hosts: [
      {
        address: "192.0.2.8",
        name: "provider.test",
        status: "up",
        scope: "network",
        evidence: "responded",
        ports: [8000],
        services: [service],
        scan_complete: true,
      },
    ],
    error: "",
    completed_at: 1,
  };
  const onConnect = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(report))),
  );
  render(
    <NetworkView
      report={report}
      scopeReady
      onDiscover={() => {}}
      onConfigureDiscovery={() => {}}
      onConnect={onConnect}
    />,
  );

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /provider\.test/ }));
  expect(
    screen.getByText("Observed API base: http://provider.test:8000/api/v1"),
  ).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Connect engine" }));
  expect(onConnect).toHaveBeenCalledWith(service);
});
