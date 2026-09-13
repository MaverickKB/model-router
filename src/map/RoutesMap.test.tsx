import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { newClient, newEngine, newRoute } from "../editors/defaults";
import { fixtureConfig } from "../test-fixtures";
import type {
  Config,
  EngineView,
  Job,
  ObservedCaller,
  RouteMap,
} from "../types";
import { RoutesMap } from "./RoutesMap";
import { engineHost } from "../engine-addresses";

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
const engineView = (engine: ReturnType<typeof newEngine>): EngineView => ({
  ...engine,
  models: [],
  status: "available",
  has_credential: false,
  checked_at: 0,
  observed_at: 0,
  latency_ms: null,
  error: "",
  inflight: 0,
  last_success: null,
});
function fixture(callers: ObservedCaller[] = []) {
  const config = fixtureConfig();
  const route = { ...newRoute(), name: "writing" };
  const engine = {
    ...newEngine(),
    name: "Provider",
    base_url: "https://provider.test/v1",
  };
  config.routes = [route];
  config.engines = [engine];
  config.clients = [];
  const topology: RouteMap = {
    caller_routes: callers.map((caller) => ({
      caller_id: caller.id,
      policy_id: caller.policy_id,
      route_id: route.id,
      ready_engines: [engine.id],
      ready_paths: [{ engine_id: engine.id, tier: "primary" }],
      reason: "Eligible text path",
    })),
    policy_routes: [],
    route_engines: [
      {
        route_id: route.id,
        engine_id: engine.id,
        tier: "primary",
        dynamic: true,
        models: ["available-model"],
        ready: true,
      },
    ],
  };
  const props = {
    config,
    callers,
    engines: [engineView(engine)],
    topology,
    jobs: [] as Job[],
    save: vi.fn(async (value: Config) => value),
    editCaller: vi.fn(),
    editRoute: vi.fn(),
    editEngine: vi.fn(),
    inspectJob: vi.fn(),
  };
  return { props, route, engine };
}
const runningJob = (
  caller: ObservedCaller | undefined,
  route: string,
  engineId: string,
): Job => ({
  id: "active-request",
  ts: 1800000000,
  client_id: caller?.policy_id || "unkeyed",
  client: "Shared access",
  requested: route,
  status: "running",
  attempts: [],
  decision: { route, candidates: [], rejections: [] },
  stream: true,
  caller,
  engine_id: engineId,
  engine: "Provider",
  tier: "primary",
});

it("shows one source card for assigned and unassigned SDK observations, with metadata only on inspection", async () => {
  const policy = { ...newClient(), name: "Writing account" };
  const callers = [
    observation("requests", { policy_id: policy.id }),
    observation("openai", {
      software: "OpenAI/Python/2.24.0",
      source_port: 51001,
    }),
  ];
  const { props } = fixture(callers);
  props.config.clients = [policy];
  const { container } = render(<RoutesMap {...props} />);
  const column = screen.getByLabelText("Observed sources");
  const card = within(column).getByRole("button", {
    name: "Select source 192.0.2.40",
  });
  expect(within(column).getAllByRole("button")).toHaveLength(1);
  expect(card).not.toHaveTextContent("51000");
  expect(card).not.toHaveTextContent("Python");
  expect(container.querySelector(".route-map-board")).not.toHaveTextContent(
    "Writing account",
  );
  expect(
    screen.queryByText("Software: OpenAI Python 2.24.0"),
  ).not.toBeInTheDocument();

  await userEvent.setup().click(card);
  const details = screen.getByRole("region", {
    name: "Source details for 192.0.2.40",
  });
  expect(
    within(details).getByText("Software: OpenAI Python 2.24.0"),
  ).toBeVisible();
  expect(
    within(details).getByText("Software: Python requests 2.33.0"),
  ).toBeVisible();
  expect(
    within(details).getByText("Policy identified by request: Writing account"),
  ).toBeVisible();
  expect(
    within(details).getByText("No named policy identified by the request", {
      selector: "span",
    }),
  ).toBeVisible();
});

it("keeps the same policy on different sources as separate source cards", () => {
  const policy = {
    ...newClient(),
    name: "One account",
    kind: "person" as const,
  };
  const callers = [
    observation("one", { policy_id: policy.id }),
    observation("two", { policy_id: policy.id, source_address: "192.0.2.41" }),
  ];
  const { props } = fixture(callers);
  props.config.clients = [policy];
  const { container } = render(<RoutesMap {...props} />);
  expect(
    within(screen.getByLabelText("Observed sources")).getAllByRole("button"),
  ).toHaveLength(2);
  expect(
    screen.getByRole("button", { name: "Select source 192.0.2.40" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Select source 192.0.2.41" }),
  ).toBeVisible();
  expect(container.querySelector(".route-map-board")).not.toHaveTextContent(
    "One account",
  );
});

it("attributes an active request once to each hop and never to a permission policy", async () => {
  const policy = { ...newClient(), name: "Default access" };
  const callers = [
    observation("requests", { policy_id: policy.id }),
    observation("openai", { software: "OpenAI/Python/2.24.0" }),
  ];
  const { props, route, engine } = fixture(callers);
  props.config.clients = [policy];
  props.topology.policy_routes = [
    {
      policy_id: policy.id,
      route_id: route.id,
      ready_engines: [engine.id],
      reason: "Eligible text path",
    },
  ];
  const job = runningJob(callers[0], route.name, engine.id);
  props.jobs = [job, { ...job }];
  const { container } = render(<RoutesMap {...props} />);
  const sourceEdge = container.querySelector(
    `[data-edge-id="source:addr:192.0.2.40-${route.id}"]`,
  )!;
  const engineEdge = container.querySelector(
    `[data-edge-id="${route.id}-${engine.id}-primary"]`,
  )!;
  expect(container.querySelectorAll(".map-edge.active")).toHaveLength(2);
  expect(within(sourceEdge as HTMLElement).getByText("1 active")).toBeVisible();
  expect(within(engineEdge as HTMLElement).getByText("1 active")).toBeVisible();
  expect(
    container.querySelector(`[data-edge-id="${policy.id}-${route.id}"]`),
  ).toBeNull();
  const active = screen.getByLabelText("Active requests");
  const actions = within(active).getAllByRole("button");
  expect(actions).toHaveLength(1);
  expect(actions[0]).toHaveTextContent("192.0.2.40");
  expect(actions[0]).not.toHaveTextContent("Unidentified caller");
  await userEvent.setup().click(actions[0]);
  expect(props.inspectJob).toHaveBeenCalledWith(job);
});

it("shows partial route access in amber and source-to-route selection only inspects access", async () => {
  const callers = [
    observation("allowed"),
    observation("denied", { software: "OpenAI/Python/2.24.0" }),
  ];
  const { props, route, engine } = fixture(callers);
  props.topology.caller_routes[1] = {
    ...props.topology.caller_routes[1],
    ready_engines: [],
    ready_paths: [],
    reason: "Last request rejected: key revoked",
  };
  const original = JSON.parse(JSON.stringify(props.config));
  const { container } = render(<RoutesMap {...props} />);
  const edge = container.querySelector(
    `[data-edge-id="source:addr:192.0.2.40-${route.id}"]`,
  )!;
  expect(edge).toHaveClass("mixed");
  expect(edge).not.toHaveClass("ready");
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Select source 192.0.2.40" }),
  );
  const engineEdge = container.querySelector(
    `[data-edge-id="${route.id}-${engine.id}-primary"]`,
  )!;
  expect(engineEdge).toHaveClass("mixed");
  const details = screen.getByRole("region", {
    name: "Routing details for 192.0.2.40",
  });
  expect(within(details).getByText("Mixed access")).toBeVisible();
  expect(
    within(details).getByText(
      "1 of 2 client records have an eligible destination.",
    ),
  ).toBeVisible();
  expect(
    within(details).getByText("Last request rejected: key revoked"),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(props.save).not.toHaveBeenCalled();
  expect(props.config).toEqual(original);
  expect(
    screen.getByRole("region", { name: "Routing details for 192.0.2.40" }),
  ).toBeVisible();
});

it("keeps engine-specific access partial even when every source client can use the route", async () => {
  const { props, route, engine } = fixture([
    observation("one"),
    observation("two", { software: "OpenAI/Python/2.24.0" }),
  ]);
  const other = {
    ...newEngine(),
    name: "Other engine",
    base_url: "https://second-provider.test/v1",
  };
  props.engines.push(engineView(other));
  props.config.engines.push(other);
  props.topology.caller_routes[1].ready_engines = [other.id];
  props.topology.caller_routes[1].ready_paths = [
    { engine_id: other.id, tier: "primary" },
  ];
  props.topology.route_engines.push({
    ...props.topology.route_engines[0],
    engine_id: other.id,
  });
  const { container } = render(<RoutesMap {...props} />);
  expect(
    container.querySelector(`[data-edge-id="source:addr:192.0.2.40-${route.id}"]`),
  ).toHaveClass("ready");
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Select source 192.0.2.40" }));
  for (const id of [engine.id, other.id])
    expect(
      container.querySelector(`[data-edge-id="${route.id}-${id}-primary"]`),
    ).toHaveClass("mixed");
});

it("keeps requests without a source inspectable without manufacturing a source card", async () => {
  const { props, route, engine } = fixture();
  props.jobs = [runningJob(undefined, route.name, engine.id)];
  const { container } = render(<RoutesMap {...props} />);
  expect(
    within(screen.getByLabelText("Observed sources")).queryAllByRole("button"),
  ).toHaveLength(0);
  expect(
    screen.getByText("Sources appear here when they make a request."),
  ).toBeVisible();
  expect(container.querySelectorAll('[data-edge-id^="source:"]')).toHaveLength(
    0,
  );
  const job = within(screen.getByLabelText("Active requests")).getByRole(
    "button",
  );
  expect(job).toHaveTextContent("Source not recorded");
  await userEvent.setup().click(job);
  expect(props.inspectJob).toHaveBeenCalledWith(props.jobs[0]);
});

it("labels a genuinely missing direct address without merging it into a known source", () => {
  const { props } = fixture([
    observation("missing", { source_address: "" }),
    observation("known"),
  ]);
  render(<RoutesMap {...props} />);
  expect(
    screen.getByRole("button", {
      name: "Select source Source address unavailable",
    }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Select source 192.0.2.40" }),
  ).toBeVisible();
});

it("keeps unused policies out of the graph and preserves explicit policy and engine linking", async () => {
  const { props, route, engine } = fixture();
  const policy = {
    ...newClient(),
    name: "Writer",
    kind: "agent" as const,
    route_names: [],
    model_patterns: ["allowed-*"],
  };
  props.config.clients = [policy];
  props.config.engines[0].kind = "cloud";
  props.engines[0].kind = "cloud";
  props.topology.policy_routes = [
    {
      policy_id: policy.id,
      route_id: route.id,
      ready_engines: [],
      reason: "Route not allowed by this policy",
    },
  ];
  const { container } = render(<RoutesMap {...props} />);
  expect(container.querySelector(".route-map-board")).not.toHaveTextContent(
    "Writer",
  );
  expect(
    screen.getByRole("button", { name: "Select permission policy Writer" }),
  ).not.toBeVisible();
  const user = userEvent.setup();
  await user.click(
    screen.getByText("Permission policies", { selector: "summary" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Select permission policy Writer" }),
  );
  expect(
    screen.getByText(
      "No client connection has been observed for this policy yet.",
    ),
  ).toBeVisible();
  expect(screen.getByText("Route not allowed by this policy")).toBeVisible();
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  expect(props.save).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save link" }));
  expect(props.save.mock.calls[0][0].clients[0]).toEqual({
    ...policy,
    route_names: [route.name],
  });
  expect(props.save.mock.calls[0][0].security).toEqual(props.config.security);
  await user.click(
    screen.getByRole("button", { name: "Select route writing" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Select engine Provider" }),
  );
  await user.click(screen.getByRole("button", { name: "Backup" }));
  expect(screen.getByText(/replaces automatic selection/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Save link" }));
  expect(props.save.mock.calls[1][0].routes[0].fallback).toEqual({
    kind: "cloud",
    engine_ids: [engine.id],
    model_patterns: ["*"],
    tags: [],
  });
  expect(props.save.mock.calls[1][0].clients[0].allow_cloud).toBe(false);
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

it("keeps a forbidden backup blocked when the same engine serves an allowed primary model", async () => {
  const { props, route, engine } = fixture([observation("primary-only")]);
  props.topology.route_engines.push({
    ...props.topology.route_engines[0],
    tier: "fallback",
    models: ["restricted-model"],
  });
  const { container } = render(<RoutesMap {...props} />);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Select source 192.0.2.40" }));
  const primary = container.querySelector(
    `[data-edge-id="${route.id}-${engine.id}-primary"]`,
  )!;
  const backup = container.querySelector(
    `[data-edge-id="${route.id}-${engine.id}-fallback"]`,
  )!;
  expect(primary).toHaveClass("ready");
  expect(backup).toHaveClass("blocked");
  expect(backup).not.toHaveClass("ready");
  expect(backup.querySelector("title")).toHaveTextContent(
    "Backup · Unavailable",
  );
});

it("keeps an unavailable engine edge blocked despite a source path entry", async () => {
  const { props, route, engine } = fixture([observation("recent-client")]);
  props.topology.route_engines[0].ready = false;
  const { container } = render(<RoutesMap {...props} />);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Select source 192.0.2.40" }));
  const edge = container.querySelector(
    `[data-edge-id="${route.id}-${engine.id}-primary"]`,
  )!;
  expect(edge).toHaveClass("blocked");
  expect(edge).not.toHaveClass("ready");
  expect(edge.querySelector("title")).toHaveTextContent(
    "Primary · Unavailable",
  );
});

it("focuses the selected source inspector once, preserves focus across polling, and returns to the map", async () => {
  const previous = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "scrollIntoView",
  );
  const scroll = vi.fn();
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: scroll,
  });
  try {
    const { props } = fixture([observation("client")]);
    const { rerender } = render(<RoutesMap {...props} />);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Select source 192.0.2.40" }),
    );
    const inspector = screen.getByRole("region", {
      name: "Routing details for 192.0.2.40",
    });
    expect(inspector).toHaveFocus();
    expect(scroll).toHaveBeenCalledTimes(1);
    expect(scroll.mock.contexts[0]).toBe(inspector);

    await user.click(
      screen.getByRole("button", { name: "Select route writing" }),
    );
    expect(inspector).toHaveFocus();
    expect(scroll).toHaveBeenCalledTimes(2);
    rerender(
      <RoutesMap
        {...props}
        callers={props.callers.map((caller) => ({
          ...caller,
          last_seen: caller.last_seen + 3,
        }))}
        topology={{ ...props.topology }}
      />,
    );
    expect(inspector).toHaveFocus();
    expect(scroll).toHaveBeenCalledTimes(2);

    await user.click(screen.getByRole("button", { name: "Back to map" }));
    const graph = screen.getByRole("group", { name: "Route map" });
    expect(graph).toHaveFocus();
    expect(scroll).toHaveBeenCalledTimes(3);
    expect(scroll.mock.contexts[2]).toBe(graph);
    expect(
      screen.queryByRole("region", { name: "Routing details for 192.0.2.40" }),
    ).not.toBeInTheDocument();
  } finally {
    if (previous)
      Object.defineProperty(Element.prototype, "scrollIntoView", previous);
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  }
});

it("attributes each mixed-access decision to its software and effective policy, including missing evidence", async () => {
  const policy = { ...newClient(), name: "Household account" };
  const callers = [
    observation("allowed", { policy_id: policy.id }),
    observation("rejected", {
      policy_id: policy.id,
      software: "OpenAI/Python/2.24.0",
    }),
    observation("pending", { policy_id: policy.id, software: "httpx/0.28.0" }),
  ];
  const { props } = fixture(callers);
  props.config.clients = [policy];
  props.topology.caller_routes = props.topology.caller_routes.slice(0, 2);
  props.topology.caller_routes[1] = {
    ...props.topology.caller_routes[1],
    ready_engines: [],
    ready_paths: [],
    reason: "Last request rejected: key revoked",
  };
  render(<RoutesMap {...props} />);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Select source 192.0.2.40" }));
  const allowed = screen.getByRole("listitem", {
    name: "Route decision for Python requests 2.33.0",
  });
  expect(within(allowed).getByText("Available")).toBeVisible();
  expect(within(allowed).getByText("Eligible text path")).toBeVisible();
  expect(
    within(allowed).getByText("Permission policy: Household account"),
  ).toBeVisible();
  expect(allowed).not.toHaveTextContent("key revoked");

  const rejected = screen.getByRole("listitem", {
    name: "Route decision for OpenAI Python 2.24.0",
  });
  expect(within(rejected).getByText("Unavailable")).toBeVisible();
  expect(
    within(rejected).getByText("Last request rejected: key revoked"),
  ).toBeVisible();
  expect(
    within(rejected).getByText("Permission policy: Household account"),
  ).toBeVisible();

  const pending = screen.getByRole("listitem", {
    name: "Route decision for HTTPX 0.28.0",
  });
  expect(within(pending).getByText("Access not yet known")).toBeVisible();
  expect(
    within(pending).getByText(
      "Current route access has not been reported for this client.",
    ),
  ).toBeVisible();
  expect(
    within(pending).getByText("Permission policy: Not yet reported"),
  ).toBeVisible();
});

it("renders an Accounts group for account callers without looking them up as clients", async () => {
  const caller = observation("dana-key", {
    policy_id: "acct-1",
    account_id: "acct-1",
    key_id: "key-1",
    name: "Dana · laptop",
    identity_basis: "account_key",
  });
  const { props, route } = fixture([caller]);
  props.config.clients = [];
  props.config.account_levels = [
    {
      id: "level-1",
      name: "Basic",
      description: "",
      route_names: ["writing"],
      engine_ids: [],
      model_patterns: ["*"],
      allow_cloud: false,
      allow_direct_models: false,
      token_budget: null,
      max_concurrency: null,
    },
  ];
  props.topology.account_callers = [
    {
      account_id: "acct-1",
      name: "Dana",
      level_id: "level-1",
      observed_callers: [caller],
    },
  ];
  const { container } = render(<RoutesMap {...props} />);
  expect(
    within(screen.getByLabelText("Observed sources")).getAllByRole("button"),
  ).toHaveLength(1);
  expect(
    container.querySelector(`[data-edge-id="source:addr:192.0.2.40-${route.id}"]`),
  ).toHaveClass("ready");
  const user = userEvent.setup();
  await user.click(screen.getByText("Accounts", { selector: "summary" }));
  const account = screen.getByRole("button", { name: "Select account Dana" });
  expect(account).toHaveTextContent("Account · 1 connection");
  expect(
    screen.queryByRole("button", { name: "Edit account Dana" }),
  ).not.toBeInTheDocument();
  await user.click(account);
  expect(screen.getByText("Basic")).toBeVisible();
  expect(screen.getByText("writing: Eligible text path")).toBeVisible();
  expect(container).not.toHaveTextContent("Policy no longer configured");
  expect(props.save).not.toHaveBeenCalled();
});
