import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { newClient, newEngine, newRoute } from "../editors/defaults";
import { fixtureConfig } from "../test-fixtures";
import type { Config, EngineView } from "../types";
import { RoutesMap } from "./RoutesMap";
import { engineHost } from "../engine-addresses";

it("links a caller and a cloud backup without changing access mode or caller permissions", async () => {
  const config = fixtureConfig();
  const caller = {
    ...newClient(),
    name: "Writer",
    kind: "agent" as const,
    route_names: [],
    model_patterns: ["allowed-*"],
  };
  const route = { ...newRoute(), name: "writing" };
  const engine = {
    ...newEngine(),
    name: "Provider",
    base_url: "https://provider.test/v1",
    kind: "cloud" as const,
  };
  config.clients = [caller];
  config.routes = [route];
  config.engines = [engine];
  const save = vi.fn(async (value: Config) => value);
  render(
    <RoutesMap
      config={config}
      callers={[
        {
          id: "observed",
          policy_id: caller.id,
          name: "Writer",
          source_address: "192.0.2.9",
          software: "agent-client",
          reported_name: "",
          identity_basis: "api_key",
          last_seen: 0,
          last_path: "/v1/models",
        },
      ]}
      engines={[
        {
          ...engine,
          models: [],
          status: "checking",
          has_credential: false,
          checked_at: 0,
          observed_at: 0,
          latency_ms: null,
          error: "",
          inflight: 0,
          last_success: null,
        } satisfies EngineView,
      ]}
      topology={{ caller_routes: [], route_engines: [] }}
      jobs={[]}
      save={save}
      editCaller={() => {}}
      editRoute={() => {}}
      editEngine={() => {}}
      inspectJob={() => {}}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Select permission policy Writer" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  expect(save).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save link" }));
  expect(save.mock.calls[0][0].clients[0]).toEqual({
    ...caller,
    route_names: ["writing"],
  });
  expect(save.mock.calls[0][0].security).toEqual(config.security);
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Select engine Provider" }),
  );
  await user.click(screen.getByRole("button", { name: "Backup" }));
  expect(screen.getByText(/replaces automatic selection/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Save link" }));
  expect(save.mock.calls[1][0].routes[0].fallback).toEqual({
    kind: "cloud",
    engine_ids: [engine.id],
    model_patterns: ["*"],
    tags: [],
  });
  expect(save.mock.calls[1][0].clients[0].allow_cloud).toBe(false);
});

it("uses a known hostname for display while the preferred transport stays operator-selected", () => {
  const engine = {
    ...newEngine(),
    base_url: "http://192.0.2.8:8000/v1",
    aliases: ["http://model.test:8000/v1"],
  };
  expect(engineHost(engine)).toBe("model.test:8000");
  expect(engine.base_url).toBe("http://192.0.2.8:8000/v1");
});

it("shows a permission policy before any connection is observed", async () => {
  const config = fixtureConfig();
  const policy = { ...newClient(), name: "Agent profile" };
  config.clients = [policy];
  config.routes = [{ ...newRoute(), name: "auto" }];
  render(
    <RoutesMap
      config={config}
      callers={[]}
      engines={[]}
      topology={{ caller_routes: [], route_engines: [] }}
      jobs={[]}
      save={vi.fn(async (value: Config) => value)}
      editCaller={() => {}}
      editRoute={() => {}}
      editEngine={() => {}}
      inspectJob={() => {}}
    />,
  );
  await userEvent
    .setup()
    .click(
      screen.getByRole("button", {
        name: "Select permission policy Agent profile",
      }),
    );
  expect(
    screen.getByText(
      "No client connection has been observed for this policy yet.",
    ),
  ).toBeVisible();
});
