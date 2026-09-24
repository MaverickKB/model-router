import type {
  PerformanceRow,
  PerformanceSummary,
  RequestPerformance,
  State,
} from "./types";

export const PERFORMANCE_WINDOWS = [
  { hours: 1, short: "1h", label: "hour" },
  { hours: 24, short: "24h", label: "24 hours" },
  { hours: 168, short: "7d", label: "7 days" },
];

export function windowLabel(hours: number) {
  for (const option of PERFORMANCE_WINDOWS) {
    if (option.hours === hours) return option.label;
  }
  return `${hours} hours`;
}

export function durationLabel(ms: number) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export function rateLabel(tokensPerSecond: number, estimated: boolean) {
  const digits = tokensPerSecond < 10 ? 1 : 0;
  const prefix = estimated ? "≈ " : "";
  return `${prefix}${tokensPerSecond.toFixed(digits)} tok/s`;
}

export function failureLabel(row: PerformanceRow) {
  if (row.failed === 0) return "no failures";
  const percent = Math.round((row.failure_rate ?? 0) * 100);
  return `${row.failed} failed (${percent}%)`;
}

/** Null while the requested window has not loaded yet. */
export function rowsForEngine(
  summary: PerformanceSummary | null,
  hours: number,
  engineId: string,
): PerformanceRow[] | null {
  if (summary === null || summary.window_hours !== hours) return null;
  const rows: PerformanceRow[] = [];
  for (const row of summary.rows) {
    if (row.engine_id === engineId) rows.push(row);
  }
  return rows;
}

export function requestTimingLabel(performance: RequestPerformance) {
  const tokens = `${performance.tokens_estimated ? "≈ " : ""}${performance.completion_tokens} completion tokens`;
  if (!performance.stream) {
    return `Engine response ${durationLabel(performance.upstream_ms)} · ${tokens}`;
  }
  const parts: string[] = [];
  if (performance.first_chunk_ms !== null) {
    parts.push(`First chunk ${durationLabel(performance.first_chunk_ms)}`);
  }
  if (performance.tokens_per_second !== null) {
    parts.push(
      rateLabel(performance.tokens_per_second, performance.tokens_estimated),
    );
  }
  parts.push(tokens);
  return parts.join(" · ");
}

/**
 * Changes whenever the performance summary could change: any loaded
 * request's status, or any engine's status or model list. Catalog check
 * times are left out so the idle state poll never triggers a reload.
 */
export function performanceRefreshKey(state: State | null) {
  if (state === null) return "";
  const parts: string[] = [];
  for (const event of state.events ?? []) {
    parts.push(`${event.id}:${event.status}`);
  }
  for (const engine of state.engines ?? []) {
    const modelIds: string[] = [];
    for (const model of engine.models) {
      modelIds.push(model.id);
    }
    parts.push(`${engine.id}:${engine.status}:${modelIds.join(",")}`);
  }
  return parts.join("|");
}
