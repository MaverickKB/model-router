import { ChevronRight, Clock3, Route as RouteIcon, Search } from "lucide-react";
import { useState } from "react";
import type { Job } from "../types";
import { callerDisplayName, callerSummary } from "../caller-identity";

function failureReason(job: Job) {
  if (job.decision.error) return job.decision.error;
  if (!["failed", "denied", "unavailable"].includes(job.status) || job.model)
    return "";
  const reasons = [...new Set(job.decision.rejections.map((r) => r.reason))];
  return reasons.join("; ");
}

export function ActivityRail({
  events,
  hover,
  onHover,
  onSelect,
}: {
  events: Job[];
  hover: Job | null;
  onHover: (job: Job | null) => void;
  onSelect: (job: Job) => void;
}) {
  const [filter, setFilter] = useState("All"),
    [search, setSearch] = useState("");
  const jobs = events.filter(
    (j) =>
      (filter === "All" ||
        (filter === "Errors"
          ? ["failed", "denied", "unavailable"].includes(j.status)
          : ["running", "routing", "waiting"].includes(j.status))) &&
      `${j.client} ${j.caller ? callerSummary(j.caller) : ""} ${j.requested} ${j.model || ""} ${j.engine || ""} ${failureReason(j)}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );

  return (
    <aside className="activity-rail">
      <div className="rail-head">
        <h2>
          Requests<span>{events.length}</span>
        </h2>
        <div className="rail-filters">
          {["All", "Active", "Errors"].map((f) => (
            <button
              key={f}
              className={f === filter ? "active" : ""}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
        <label className="search">
          <Search size={14} />
          <input
            aria-label="Search requests"
            placeholder="Search requests"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
      </div>
      <div className="jobs">
        {jobs.map((j) => (
          <button
            key={j.id}
            data-job-id={j.id}
            className={"job-card " + (hover?.id === j.id ? "highlighted" : "")}
            onMouseEnter={() => onHover(j)}
            onFocus={() => onHover(j)}
            onMouseLeave={() => onHover(null)}
            onBlur={() => onHover(null)}
            onClick={() => onSelect(j)}
          >
            <div className="job-top">
              <span className={"status-dot " + j.status} />
              <strong>
                {j.caller ? callerDisplayName(j.caller) : "Caller not recorded"}
              </strong>
              <span>
                {new Date(j.ts * 1000).toLocaleTimeString([], {
                  hour: "numeric",
                  minute: "2-digit",
                })}
              </span>
            </div>
            <div className="job-route">
              <RouteIcon size={14} />
              {j.requested}
            </div>
            {j.caller && <p>{callerSummary(j.caller)}</p>}
            <p className={failureReason(j) ? "request-error" : undefined}>
              {failureReason(j) || j.model || "No model selected"}
            </p>
            <div className="job-bottom">
              <span>{j.status}</span>
              <span>
                {j.elapsed_ms != null
                  ? `${(j.elapsed_ms / 1000).toFixed(1)}s`
                  : ""}
                <ChevronRight size={13} />
              </span>
            </div>
          </button>
        ))}
        {!jobs.length && (
          <div className="rail-empty">
            <Clock3 size={23} />
            <p>
              {events.length
                ? "No matching requests"
                : "Your next request appears here."}
            </p>
            <small>Follow each decision from caller to model.</small>
          </div>
        )}
      </div>
      <div className="rail-footer">
        <span className="status-dot available" />
        Live routing activity
      </div>
    </aside>
  );
}
