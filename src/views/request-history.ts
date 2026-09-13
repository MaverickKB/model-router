import type { Job } from "../types";
import { callerSummary } from "../caller-identity";

export type RequestHistoryFilter = "All" | "Active" | "Errors";

export interface RequestSourceGroup {
  id: string;
  address: string | null;
  jobs: Job[];
  total: number;
  counts: {
    active: number;
    error: number;
    cancelled: number;
    completed: number;
    other: number;
  };
  latestFailure: string;
  latestSeen: number;
}

export function isActive(job: Job): boolean {
  return ["running", "routing", "waiting"].includes(job.status);
}

export function isError(job: Job): boolean {
  return ["failed", "denied", "unavailable"].includes(job.status);
}

export function failureReason(job: Job): string {
  if (!isError(job)) return "";
  if (job.decision.error) return job.decision.error;
  // Rejected alternatives do not explain what happened to a selected model.
  if (job.model) return "";
  return [...new Set(job.decision.rejections.map((item) => item.reason))]
    .filter(Boolean)
    .join("; ");
}

function searchText(job: Job): string {
  const caller = job.caller;
  // Select display evidence explicitly; credential or identity-hint values do not
  // belong in search. Policy names are searchable, never grouping identities.
  return [
    caller ? callerSummary(caller) : "",
    caller?.source_address,
    caller?.software,
    caller?.client_family,
    caller?.client_version,
    caller?.reported_name,
    caller?.name,
    job.client,
    job.requested,
    job.decision.route,
    job.model,
    job.engine,
    failureReason(job),
    job.status,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

function byId(a: { id: string }, b: { id: string }): number {
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}

export function groupRequestHistory(
  events: Job[],
  filter: RequestHistoryFilter,
  search: string,
): {
  total: number;
  sources: number;
  missingSource: number;
  matched: number;
  groups: RequestSourceGroup[];
} {
  const seen = new Set<string>();
  const history = new Map<string | null, Job[]>();
  for (const job of events) {
    // The live request list precedes persisted history and owns duplicate IDs.
    if (seen.has(job.id)) continue;
    seen.add(job.id);
    const address = job.caller?.source_address?.trim() || null;
    const jobs = history.get(address) || [];
    jobs.push(job);
    history.set(address, jobs);
  }

  const query = search.trim().toLowerCase();
  const groups: RequestSourceGroup[] = [];
  let matched = 0;
  for (const [address, historyJobs] of history) {
    historyJobs.sort((a, b) => b.ts - a.ts || byId(a, b));
    const counts = {
      active: 0,
      error: 0,
      cancelled: 0,
      completed: 0,
      other: 0,
    };
    for (const job of historyJobs) {
      if (isActive(job)) counts.active++;
      else if (isError(job)) counts.error++;
      else if (job.status === "cancelled") counts.cancelled++;
      else if (job.status === "completed") counts.completed++;
      else counts.other++;
    }
    const jobs = historyJobs.filter(
      (job) =>
        (filter === "All" ||
          (filter === "Active" ? isActive(job) : isError(job))) &&
        (!query || searchText(job).includes(query)),
    );
    if (!jobs.length) continue;
    const latestError = historyJobs.find(isError);
    matched += jobs.length;
    groups.push({
      id: address === null ? "missing-source" : `source:${address}`,
      address,
      jobs,
      total: historyJobs.length,
      counts,
      latestFailure: latestError
        ? failureReason(latestError) || latestError.status
        : "",
      latestSeen: historyJobs[0].ts,
    });
  }
  // Keep expanded sources in place when polling adds newer requests elsewhere.
  groups.sort((a, b) =>
    a.address === b.address
      ? 0
      : a.address === null
        ? 1
        : b.address === null
          ? -1
          : byId(a, b),
  );

  return {
    total: seen.size,
    sources: history.size - (history.has(null) ? 1 : 0),
    missingSource: history.get(null)?.length || 0,
    matched,
    groups,
  };
}
