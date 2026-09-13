import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { newClient, newEngine, newRoute } from "../editors/defaults";
import { fixtureConfig } from "../test-fixtures";
import type { Config, EngineView, Job, ObservedCaller } from "../types";
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
  await userEvent.setup().click(
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

it("keeps policy cards concise and reveals all observed evidence on selection", async () => {
  const config = fixtureConfig();
  const policy = {
    ...newClient(),
    name: "Writing account",
    kind: "person" as const,
  };
  config.clients = [policy];
  const callers: ObservedCaller[] = [
    "Desktop editor",
    "Terminal editor",
    "Notebook editor",
  ].map((name, index) => ({
    id: `observed-${index}`,
    policy_id: policy.id,
    name,
    reported_name: name,
    software: "editor-sdk/1.0",
    source_address: "192.0.2.44",
    source_port: 51000 + index,
    identity_basis: "api_key",
    last_seen: 1800000000,
    last_path: "/v1/chat/completions",
  }));
  render(
    <RoutesMap
      config={config}
      callers={callers}
      engines={[]}
      topology={{ caller_routes: [], route_engines: [] }}
      jobs={[]}
      save={vi.fn(async (value: Config) => value)}
      editCaller={vi.fn()}
      editRoute={vi.fn()}
      editEngine={vi.fn()}
      inspectJob={vi.fn()}
    />,
  );
  const card = screen.getByRole("button", {
    name: "Select permission policy Writing account",
  });
  expect(within(card).getByText("Kind: person · 3 observations")).toBeVisible();
  for (const caller of callers)
    expect(screen.queryByText(caller.name)).not.toBeInTheDocument();
  await userEvent.setup().click(card);
  const details = screen.getByRole("region", {
    name: "Permission policy details for Writing account",
  });
  for (const caller of callers) {
    const observation = within(details).getByRole("region", {
      name: `Connection details for ${caller.name}`,
    });
    expect(
      within(observation).getByText(
        `${caller.source_address}:${caller.source_port}`,
      ),
    ).toBeVisible();
  }
});

it("groups observations by source while preserving individual access, inspection, and activity", async () => {
  const config = fixtureConfig();
  const policy = { ...newClient(), name: "Saved account" };
  const route = { ...newRoute(), name: "writing" };
  config.clients = [policy];
  config.routes = [route];
  const observation = (
    id: string,
    fields: Partial<ObservedCaller> = {},
  ): ObservedCaller => ({
    id,
    policy_id: null,
    name: "Unidentified caller",
    source_address: "192.0.2.40",
    source_port: 51000,
    software: "python-requests/2.33.0",
    reported_name: "",
    identity_basis: "unassigned",
    last_seen: 1800000000,
    last_path: "/v1/chat/completions",
    ...fields,
  });
  const callers = [
    observation("requests"),
    observation("openai", {
      software: "OpenAI/Python/2.24.0",
      source_port: 51001,
    }),
    observation("named", {
      name: "Drafting assistant",
      reported_name: "Drafting assistant",
      source_port: 51002,
    }),
    observation("other", { source_address: "192.0.2.41" }),
  ];
  const active: Job = {
    id: "active-request",
    ts: 1800000000,
    client_id: "unkeyed",
    client: "Unkeyed access",
    requested: route.name,
    status: "running",
    attempts: [],
    decision: { route: route.name, candidates: [], rejections: [] },
    stream: true,
    caller: callers[0],
  };
  const save = vi.fn(async (value: Config) => value);
  const { container } = render(
    <RoutesMap
      config={config}
      callers={callers}
      engines={[]}
      topology={{
        caller_routes: callers.map((caller) => ({
          caller_id: caller.id,
          policy_id: null,
          route_id: route.id,
          ready_engines: caller.id === "openai" ? [] : ["available-engine"],
          reason:
            caller.id === "openai"
              ? "Last request rejected: key revoked"
              : `Eligible text path for ${caller.id}`,
        })),
        route_engines: [],
      }}
      jobs={[active]}
      save={save}
      editCaller={vi.fn()}
      editRoute={vi.fn()}
      editEngine={vi.fn()}
      inspectJob={vi.fn()}
    />,
  );
  const source = screen.getByRole("group", {
    name: "Observed source 192.0.2.40",
  });
  expect(within(source).getAllByRole("heading")).toHaveLength(1);
  expect(within(source).getByText("3 observations")).toBeVisible();
  expect(within(source).getAllByRole("button")).toHaveLength(3);
  expect(
    screen.getByRole("group", { name: "Observed source 192.0.2.41" }),
  ).toBeVisible();
  expect(
    within(source).getByRole("button", {
      name: "Select observed caller Drafting assistant",
    }),
  ).toBeVisible();
  expect(
    within(source).queryByRole("button", {
      name: "Select observed caller Unidentified caller",
    }),
  ).not.toBeInTheDocument();

  const allowedEdge = container.querySelector(
    `[data-edge-id="requests-${route.id}"]`,
  )!;
  const rejectedEdge = container.querySelector(
    `[data-edge-id="openai-${route.id}"]`,
  )!;
  const otherSourceEdge = container.querySelector(
    `[data-edge-id="other-${route.id}"]`,
  )!;
  expect(allowedEdge).toHaveClass("ready", "active");
  expect(rejectedEdge).toHaveClass("waiting");
  expect(rejectedEdge).not.toHaveClass("ready", "active");
  expect(
    within(allowedEdge as HTMLElement).getByText("1 active"),
  ).toBeVisible();
  expect(allowedEdge.querySelector("path")).toHaveAttribute(
    "d",
    expect.stringMatching(/^M280,240 /),
  );
  expect(otherSourceEdge.querySelector("path")).toHaveAttribute(
    "d",
    expect.stringMatching(/^M280,596 /),
  );

  const user = userEvent.setup();
  await user.click(
    within(source).getByRole("button", {
      name: "Select observed caller OpenAI Python 2.24.0",
    }),
  );
  const details = screen.getByRole("region", {
    name: "Connection details for Unidentified caller",
  });
  expect(within(details).getByText("192.0.2.40:51001")).toBeVisible();
  expect(
    screen.getByText("Last request rejected: key revoked", { selector: "p" }),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(save).not.toHaveBeenCalled();
  expect(config.clients).toEqual([policy]);
});
