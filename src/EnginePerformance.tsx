import { when } from "./components";
import {
  PERFORMANCE_WINDOWS,
  durationLabel,
  failureLabel,
  rateLabel,
  windowLabel,
} from "./performance-format";
import type { PerformanceRow } from "./types";

export interface EnginePerformanceProps {
  rows: PerformanceRow[] | null;
  hours: number;
  error: string;
  onHours: (hours: number) => void;
}

export function EnginePerformance({
  rows,
  hours,
  error,
  onHours,
}: EnginePerformanceProps) {
  let message = "";
  if (error) message = error;
  else if (rows === null) message = "Loading request history…";
  else if (rows.length === 0)
    message = `No requests reached this engine in the last ${windowLabel(hours)}.`;

  return (
    <section className="engine-performance" aria-label="Performance">
      <header>
        <strong>Performance</strong>
        <div
          className="segmented performance-window"
          role="group"
          aria-label="Performance window"
        >
          {PERFORMANCE_WINDOWS.map((option) => (
            <button
              key={option.hours}
              type="button"
              aria-pressed={option.hours === hours}
              className={option.hours === hours ? "active" : ""}
              onClick={() => onHours(option.hours)}
            >
              {option.short}
            </button>
          ))}
        </div>
      </header>
      {message && <p className="hint">{message}</p>}
      {!message &&
        rows !== null &&
        rows.map((row) => <PerformanceModel key={row.model} row={row} />)}
    </section>
  );
}

function catalogTag(inCatalog: boolean | null) {
  if (inCatalog === true) return "Listed now";
  if (inCatalog === false) return "No longer listed";
  return "";
}

function PerformanceModel({ row }: { row: PerformanceRow }) {
  const tag = catalogTag(row.in_catalog);
  const stream = row.stream;
  const plain = row.non_stream;
  // A rate is marked estimated when any of its samples used estimated tokens.
  const estimatedRate = stream.estimated_samples > 0;

  let firstChunk = "No streamed requests";
  if (stream.first_chunk_ms_p50 !== null) {
    firstChunk = durationLabel(stream.first_chunk_ms_p50);
  }
  let generation = "Not measured";
  if (stream.tokens_per_second_p50 !== null) {
    generation = rateLabel(stream.tokens_per_second_p50, estimatedRate);
  }
  let response = "No plain requests";
  if (plain.upstream_ms_p50 !== null) {
    response = durationLabel(plain.upstream_ms_p50);
  }

  return (
    <div className="performance-model" data-model={row.model}>
      <div className="performance-name">
        <strong>{row.model}</strong>
        {tag && (
          <span className={"tag" + (row.in_catalog === false ? " muted" : "")}>
            {tag}
          </span>
        )}
        <small>
          {row.served} served · {failureLabel(row)} · last used{" "}
          {when(row.last_seen)}
        </small>
      </div>
      <dl className="performance-stats">
        <div>
          <dt>First chunk (median)</dt>
          <dd>
            {firstChunk}
            {stream.first_chunk_ms_p95 !== null && (
              <small>p95 {durationLabel(stream.first_chunk_ms_p95)}</small>
            )}
          </dd>
        </div>
        <div>
          <dt>Generation (median)</dt>
          <dd>{generation}</dd>
        </div>
        <div>
          <dt>Non-stream response (median)</dt>
          <dd>
            {response}
            {plain.upstream_ms_p95 !== null && (
              <small>p95 {durationLabel(plain.upstream_ms_p95)}</small>
            )}
          </dd>
        </div>
      </dl>
    </div>
  );
}
