import { expect, it } from "vitest";
import type { Job } from "../types";
import {
  failureReason,
  groupRequestHistory,
  isActive,
  isError,
} from "./request-history";

function request(id: string, options: Partial<Job> = {}): Job {
  return {
    id,
    ts: 100,
    client_id: "permission",
    client: "Permission policy",
    requested: "conversation",
    status: "completed",
    attempts: [],
    decision: { candidates: [], rejections: [] },
    stream: false,
    caller: {
      id: "observation",
      policy_id: "permission",
      name: "Python requests",
      source_address: "192.0.2.10",
      source_port: 50001,
      software: "python-requests/2.33.0",
      reported_name: "",
      identity_basis: "shared_access",
      last_seen: 100,
      last_path: "/v1/chat/completions",
    },
    ...options,
  };
}

const rejection = {
  engine_id: "engine",
  engine: "Engine",
  model: null,
  tier: "primary",
  reason: "Missing capability: tools",
};

it("groups 99 requests from one direct source across software, policies and ports", () => {
  const events = Array.from({ length: 99 }, (_, index) => {
    const job = request(`request-${index}`, { ts: index });
    return {
      ...job,
      client_id: `policy-${index % 3}`,
      caller: {
        ...job.caller!,
        id: `observation-${index}`,
        policy_id: index % 2 ? null : "key-policy",
        source_port: 50000 + index,
        software: index % 2 ? "OpenAI/Python" : "python-requests",
        identity_basis: index % 2 ? "shared_access" : "api_key",
      },
    } satisfies Job;
  });
  const result = groupRequestHistory(events, "All", "");
  expect(result).toMatchObject({
    total: 99,
    sources: 1,
    missingSource: 0,
    matched: 99,
  });
  expect(result.groups).toHaveLength(1);
  expect(result.groups[0]).toMatchObject({
    address: "192.0.2.10",
    total: 99,
    counts: { completed: 99 },
    latestSeen: 98,
  });
  expect(result.groups[0].jobs.map((job) => job.id)).toEqual(
    [...events].reverse().map((job) => job.id),
  );
  expect(events[0].id).toBe("request-0");
});

it("keeps missing source evidence separate from named policies and real sources", () => {
  const missing = [
    request("missing", { caller: null }),
    request("absent", { caller: undefined }),
    request("blank", {
      caller: { ...request("template").caller!, source_address: "  " },
    }),
  ];
  const result = groupRequestHistory([...missing, request("known")], "All", "");
  expect(result).toMatchObject({ total: 4, sources: 1, missingSource: 3 });
  expect(result.groups.find((group) => group.address === null)?.total).toBe(3);
  expect(result.groups.map((group) => group.address)).toEqual([
    "192.0.2.10",
    null,
  ]);
  expect(
    groupRequestHistory(missing, "All", "Permission policy").groups[0].address,
  ).toBeNull();
});

it("keeps full history counts and latest failure while filtering and searching", () => {
  const events = [
    request("old-failure", {
      ts: 1,
      status: "failed",
      decision: { candidates: [], rejections: [], error: "Old failure" },
    }),
    request("latest-failure", { ts: 2, status: "denied" }),
    request("completed", {
      ts: 3,
      model: "chat-model",
      decision: { candidates: [], rejections: [rejection] },
    }),
    request("running", { ts: 4, status: "running" }),
    request("cancelled", { ts: 5, status: "cancelled" }),
    request("other", { ts: 6, status: "unknown" }),
  ];
  const result = groupRequestHistory(events, "Errors", "Old failure");
  expect(result).toMatchObject({ total: 6, sources: 1, matched: 1 });
  expect(result.groups[0]).toMatchObject({
    total: 6,
    counts: { active: 1, error: 2, completed: 1, cancelled: 1, other: 1 },
    latestFailure: "denied",
    latestSeen: 6,
  });
  expect(result.groups[0].jobs.map((job) => job.id)).toEqual(["old-failure"]);
  expect(groupRequestHistory(events, "Active", "").groups[0].jobs[0].id).toBe(
    "running",
  );
  expect(groupRequestHistory(events, "Errors", "chat-model")).toMatchObject({
    total: 6,
    sources: 1,
    matched: 0,
    groups: [],
  });
});

it("uses the first copy of each request ID and orders equal timestamps by ID", () => {
  const active = request("b", { status: "running" });
  const persisted = request("b", { status: "completed", ts: 101 });
  const otherSource = request("c", {
    caller: { ...active.caller!, source_address: "192.0.2.20" },
  });
  const result = groupRequestHistory(
    [active, persisted, otherSource, request("a")],
    "All",
    "",
  );
  expect(result).toMatchObject({ total: 3, matched: 3, sources: 2 });
  expect(result.groups.map((group) => group.address)).toEqual([
    "192.0.2.10",
    "192.0.2.20",
  ]);
  expect(result.groups[0].jobs.map((job) => job.id)).toEqual(["a", "b"]);
  expect(result.groups[0].jobs[1]).toBe(active);
  expect(result.groups[0].counts.active).toBe(1);
});

it("keeps source order stable when traffic changes and places missing sources last", () => {
  const first = request("first", { ts: 100 });
  const second = request("second", {
    ts: 200,
    caller: { ...first.caller!, source_address: "192.0.2.20" },
  });
  const unknown = request("unknown", { ts: 300, caller: null });
  const events = [second, unknown, first];
  const expectedOrder = ["192.0.2.10", "192.0.2.20", null];
  expect(
    groupRequestHistory(events, "All", "").groups.map((group) => group.address),
  ).toEqual(expectedOrder);

  const newRequest = { ...first, id: "new-first", ts: 400 };
  const updated = groupRequestHistory([newRequest, ...events], "All", "");
  expect(updated.groups.map((group) => group.address)).toEqual(expectedOrder);
  expect(updated.groups[0].jobs.map((job) => job.id)).toEqual([
    "new-first",
    "first",
  ]);
  expect(updated.groups[0].latestSeen).toBe(400);
});

it("finds the friendly software label and direct peer port shown in request details", () => {
  const job = request("visible-evidence");
  expect(job.caller?.client_family).toBeUndefined();
  for (const query of [
    "Python requests",
    "Python requests 2.33.0",
    "50001",
    "192.0.2.10:50001",
  ]) {
    expect(groupRequestHistory([job], "All", query).matched, query).toBe(1);
  }
});

it("searches explicit request evidence without indexing credentials or making policy an identity", () => {
  const job = request("evidence", {
    requested: "requested-route",
    model: "selected-model",
    engine: "Selected engine",
    status: "failed",
    decision: {
      candidates: [],
      rejections: [],
      route: "resolved-route",
      error: "Explicit failure",
    },
    caller: {
      ...request("template").caller!,
      name: "Saved observation name",
      reported_name: "Notebook assistant",
      client_family: "Example SDK",
      client_version: "9.8.7",
      identity_hints: { api_key: "do-not-index" },
    },
  });
  for (const query of [
    "192.0.2.10",
    "python-requests",
    "EXAMPLE SDK",
    "9.8.7",
    "Notebook assistant",
    "Saved observation name",
    "Permission policy",
    "requested-route",
    "resolved-route",
    "selected-model",
    "Selected engine",
    "Explicit failure",
    "failed",
  ]) {
    expect(groupRequestHistory([job], "All", query).matched, query).toBe(1);
  }
  expect(groupRequestHistory([job], "All", "do-not-index").matched).toBe(0);
  expect(
    groupRequestHistory([job], "All", "Permission policy").groups[0].address,
  ).toBe("192.0.2.10");
});

it("reports failures only for failed outcomes and does not blame selected-model failures on alternatives", () => {
  const job = request("failure", {
    status: "failed",
    decision: { candidates: [], rejections: [rejection, rejection] },
  });
  expect(failureReason(job)).toBe("Missing capability: tools");
  expect(failureReason({ ...job, model: "selected-model" })).toBe("");
  expect(failureReason({ ...job, status: "completed" })).toBe("");
  const explicit = {
    ...job,
    model: "selected-model",
    decision: { ...job.decision, error: "Upstream failed" },
  };
  expect(failureReason(explicit)).toBe("Upstream failed");
  expect(failureReason({ ...explicit, status: "completed" })).toBe("");
  expect(failureReason({ ...explicit, status: "running" })).toBe("");
  expect(
    groupRequestHistory([{ ...job, status: "completed" }], "All", "").groups[0]
      .latestFailure,
  ).toBe("");
});

it("classifies active and error states without treating cancellation as either", () => {
  for (const status of ["running", "routing", "waiting"])
    expect(isActive(request(status, { status }))).toBe(true);
  for (const status of ["failed", "denied", "unavailable"])
    expect(isError(request(status, { status }))).toBe(true);
  for (const status of ["completed", "cancelled", "unknown"]) {
    expect(isActive(request(status, { status }))).toBe(false);
    expect(isError(request(status, { status }))).toBe(false);
  }
  expect(groupRequestHistory([], "All", "")).toEqual({
    total: 0,
    sources: 0,
    missingSource: 0,
    matched: 0,
    groups: [],
  });
});
