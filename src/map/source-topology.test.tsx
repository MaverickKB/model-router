import { describe, expect, it } from "vitest";
import type { CallerSource } from "../caller-sources";
import type { CallerRoute, Job, ObservedCaller, RouteMap } from "../types";
import { activeJobsForSource, sourceRouteAccess } from "./source-topology";

const source: CallerSource = {
  address: "192.0.2.10",
  lastSeen: 2,
  callers: ["keyed", "unkeyed"].map(
    (id) => ({ id, source_address: "192.0.2.10" }) as ObservedCaller,
  ),
};
const edge = (id: string, engines: string[]): CallerRoute => ({
  caller_id: id,
  policy_id: "one-policy",
  route_id: "route",
  ready_engines: engines,
  reason: "decision",
});
const topology = (...edges: CallerRoute[]): RouteMap => ({
  caller_routes: edges,
  route_engines: [],
});

describe("source route projection", () => {
  it("preserves mixed access within one address and permission policy", () => {
    const result = sourceRouteAccess(
      source,
      "route",
      topology(edge("keyed", ["engine"]), edge("unkeyed", [])),
    );
    expect(result.state).toBe("mixed");
    expect(result.allowed).toBe(1);
    expect(result.total).toBe(2);
  });
  it("does not turn separate engine grants into universal source access", () => {
    const map = topology(edge("keyed", ["a"]), edge("unkeyed", ["b"]));
    expect(sourceRouteAccess(source, "route", map).state).toBe("ready");
    expect(sourceRouteAccess(source, "route", map, "a").state).toBe("mixed");
    expect(sourceRouteAccess(source, "route", map, "b").allowed).toBe(1);
    expect(sourceRouteAccess(source, "route", map, "c").state).toBe("blocked");
  });
  it("does not guess missing observation decisions or use a policy preview", () => {
    const map = topology(edge("one-policy", ["engine"]));
    map.policy_routes = [
      {
        policy_id: "one-policy",
        route_id: "route",
        ready_engines: ["engine"],
        reason: "preview",
      },
    ];
    expect(sourceRouteAccess(source, "route", map).state).toBe("unknown");
    expect(
      sourceRouteAccess(source, "route", topology(edge("keyed", []))).state,
    ).toBe("unknown");
    expect(
      sourceRouteAccess(source, "route", topology(edge("keyed", ["engine"])))
        .state,
    ).toBe("mixed");
    expect(sourceRouteAccess(source, "route", undefined).state).toBe("unknown");
  });
});

it("counts a request once by direct source even when policy and observation disagree", () => {
  const job = {
    id: "request",
    status: "running",
    caller: { ...source.callers[0], policy_id: "other-policy" },
    client_id: "one-policy",
    requested: "auto",
    decision: { route: "auto" },
  } as Job;
  expect(activeJobsForSource(source, "auto", [job, job])).toHaveLength(1);
  expect(
    activeJobsForSource(source, "auto", [{ ...job, caller: null }]),
  ).toHaveLength(0);
  expect(
    activeJobsForSource(source, "auto", [
      { ...job, caller: { ...job.caller!, source_address: "192.0.2.11" } },
    ]),
  ).toHaveLength(0);
  expect(activeJobsForSource(source, "other", [job])).toHaveLength(0);
  expect(
    activeJobsForSource(source, "auto", [{ ...job, status: "completed" }]),
  ).toHaveLength(0);
});

it("keeps a permitted primary model from granting the same engine's fallback path", () => {
  const map = topology(
    ...source.callers.map((caller) => ({
      ...edge(caller.id, ["engine"]),
      ready_paths: [{ engine_id: "engine", tier: "primary" as const }],
    })),
  );
  expect(
    sourceRouteAccess(source, "route", map, "engine", "primary").state,
  ).toBe("ready");
  expect(
    sourceRouteAccess(source, "route", map, "engine", "fallback").state,
  ).toBe("blocked");
  expect(
    sourceRouteAccess(
      source,
      "route",
      topology(edge("keyed", ["engine"])),
      "engine",
      "primary",
    ).state,
  ).toBe("unknown");
});
