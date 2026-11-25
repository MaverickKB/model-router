import { useState } from "react";
import type { EngineView, Job } from "../types";
import { callerDisplayName } from "../caller-identity";
import { sourceTimeLabel } from "../source-time";
import { durationLabel, rateLabel } from "../performance-format";
import { DebugPanel } from "./DebugPanel";
import "./traffic.css";

type Filter = "All" | "Active" | "Errors" | "Completed";

export function TrafficView({
  events,
  engines,
}: {
  events: Job[];
  engines: EngineView[];
}) {
  const [filter, setFilter] = useState<Filter>("All");
  const [search, setSearch] = useState("");
  const visible: Job[] = [];
  for (const event of events) {
    if (filter === "Active" && !isActive(event.status)) continue;
    if (filter === "Errors" && !isError(event.status)) continue;
    if (filter === "Completed" && event.status !== "completed") continue;
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      let matched = false;
      const fields: (string | undefined)[] = [
        event.id,
        event.requested,
        event.client,
        event.engine,
        event.model,
        event.caller?.name,
        event.caller?.source_address,
      ];
      for (const field of fields) {
        if (field && field.toLowerCase().includes(needle)) {
          matched = true;
          break;
        }
      }
      if (!matched) continue;
    }
    visible.push(event);
  }

  return (
    <section className="traffic-page">
      <div className="section-heading">
        <div>
          <h1>Traffic</h1>
          <p>
            Requests passing through the router. Metadata stays on; payload
            capture is opt-in and bounded.
          </p>
        </div>
        <div className="traffic-filters">
          <input
            className="traffic-search"
            aria-label="Search traffic"
            placeholder="Search traffic"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <div className="traffic-filter-buttons">
            {(["All", "Active", "Errors", "Completed"] as Filter[]).map(
              (value) => (
                <button
                  key={value}
                  className={filter === value ? "active" : ""}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {value}
                </button>
              ),
            )}
          </div>
        </div>
      </div>

      <section className="paper traffic-table-card">
        <div className="traffic-table-scroll">
          <table className="traffic-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Requested</th>
                <th>Route</th>
                <th>Engine</th>
                <th>Status</th>
                <th>Kind</th>
                <th>Elapsed</th>
                <th>Queue</th>
                <th>Upstream</th>
                <th>Tokens</th>
                <th>First chunk</th>
                <th>Rate</th>
                <th>Attempts</th>
                <th>Last attempt</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((event) => (
                <tr key={event.id}>
                  <td>{sourceTimeLabel(event.ts)}</td>
                  <td>
                    {event.caller
                      ? callerDisplayName(event.caller)
                      : event.client}
                  </td>
                  <td>{event.requested}</td>
                  <td>{event.decision?.route ?? ""}</td>
                  <td>
                    {engineName(event, engines)}
                    {event.model ? (
                      <small className="traffic-model">{event.model}</small>
                    ) : null}
                  </td>
                  <td>
                    <span className={`traffic-status ${event.status}`}>
                      {event.status}
                    </span>
                    {event.http_status ? (
                      <small className="traffic-http">{event.http_status}</small>
                    ) : null}
                  </td>
                  <td>{event.stream ? "stream" : "json"}</td>
                  <td>
                    {event.elapsed_ms !== undefined
                      ? durationLabel(event.elapsed_ms)
                      : ""}
                  </td>
                  <td>{queueLabel(event)}</td>
                  <td>
                    {event.performance?.upstream_ms !== undefined
                      ? durationLabel(event.performance.upstream_ms)
                      : ""}
                  </td>
                  <td>{event.usage?.completion_tokens ?? ""}</td>
                  <td>
                    {event.performance?.first_chunk_ms !== null &&
                    event.performance?.first_chunk_ms !== undefined
                      ? durationLabel(event.performance.first_chunk_ms)
                      : ""}
                  </td>
                  <td>
                    {event.performance?.tokens_per_second !== null &&
                    event.performance?.tokens_per_second !== undefined
                      ? rateLabel(
                          event.performance.tokens_per_second,
                          event.performance.tokens_estimated,
                        )
                      : ""}
                  </td>
                  <td>{event.attempts.length}</td>
                  <td>{lastAttemptLabel(event)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length === 0 && (
            <p className="hint traffic-empty">No requests match this filter.</p>
          )}
        </div>
      </section>

      <DebugPanel />
    </section>
  );
}

function isActive(status: string) {
  return (
    status === "routing" ||
    status === "waiting" ||
    status === "running" ||
    status === "recovering"
  );
}

function isError(status: string) {
  return status === "failed" || status === "cancelled" || status === "denied";
}

function engineName(event: Job, engines: EngineView[]) {
  if (event.engine) return event.engine;
  if (!event.engine_id) return "";
  for (const engine of engines) {
    if (engine.id === event.engine_id) return engine.name;
  }
  return event.engine_id;
}

function queueLabel(event: Job) {
  if (
    event.elapsed_ms === undefined ||
    event.performance?.upstream_ms === undefined
  ) {
    return "";
  }
  const queueMs = event.elapsed_ms - event.performance.upstream_ms;
  if (queueMs < 0) return "0 ms";
  return durationLabel(queueMs);
}

function lastAttemptLabel(event: Job) {
  if (event.attempts.length === 0) return "";
  const last = event.attempts[event.attempts.length - 1];
  return last.status ?? "";
}
