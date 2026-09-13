import type { CallerSource } from "../caller-sources";
import type { CallerRoute, Job, RouteMap } from "../types";

export type SourceAccess = {
  state: "ready" | "mixed" | "blocked" | "unknown";
  allowed: number;
  total: number;
  edges: CallerRoute[];
  policies: string[];
};

/** Summarize connection decisions without sharing their permissions. */
export function sourceRouteAccess(
  source: CallerSource,
  routeId: string,
  topology: RouteMap | undefined,
  engineId?: string,
  tier?: "primary" | "fallback",
): SourceAccess {
  const decisions = new Map(
    (topology?.caller_routes || [])
      .filter((edge) => edge.route_id === routeId)
      .map((edge) => [edge.caller_id, edge]),
  );
  const edges = source.callers.flatMap((caller) => {
    const edge = decisions.get(caller.id);
    return edge ? [edge] : [];
  });
  const total = source.callers.length;
  const allowed = edges.filter((edge) =>
    engineId && tier
      ? edge.ready_paths?.some(
          (path) => path.engine_id === engineId && path.tier === tier,
        )
      : engineId
        ? edge.ready_engines.includes(engineId)
        : edge.ready_engines.length > 0,
  ).length;
  const complete =
    total > 0 &&
    edges.length === total &&
    (!tier || edges.every((edge) => edge.ready_paths !== undefined));
  return {
    state:
      allowed === total && complete
        ? "ready"
        : allowed > 0
          ? "mixed"
          : complete
            ? "blocked"
            : "unknown",
    allowed,
    total,
    edges,
    policies: [
      ...new Set(
        edges.flatMap((edge) => (edge.policy_id ? [edge.policy_id] : [])),
      ),
    ],
  };
}

/** Attribute each request to its recorded source, independently of policy. */
export function activeJobsForSource(
  source: CallerSource,
  routeName: string,
  jobs: Job[],
): Job[] {
  return [
    ...new Map(
      jobs
        .filter(
          (job) =>
            ["running", "routing", "waiting"].includes(job.status) &&
            Boolean(job.caller) &&
            (job.caller!.source_key
              ? job.caller!.source_key === source.key
              : job.caller!.source_address === source.address) &&
            (job.decision.route || job.requested) === routeName,
        )
        .map((job) => [job.id, job]),
    ).values(),
  ];
}
