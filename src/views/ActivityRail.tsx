import { ChevronDown, ChevronRight, Clock3, Search } from "lucide-react";
import { useId, useState } from "react";
import type { Job } from "../types";
import { callerClientLabel, callerDisplayName } from "../caller-identity";
import { sourceTimeLabel } from "../source-time";
import { failureReason, groupRequestHistory } from "./request-history";
import "./activity-history.css";

type Filter = "All" | "Active" | "Errors";

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
  const [filter, setFilter] = useState<Filter>("All");
  const [search, setSearch] = useState("");
  const [expandedSources, setExpandedSources] = useState<Set<string>>(
    new Set(),
  );
  const [collapsedMatches, setCollapsedMatches] = useState<Set<string>>(
    new Set(),
  );
  const listPrefix = useId();
  const narrowed = filter !== "All" || Boolean(search.trim());
  const history = groupRequestHistory(events, filter, search);
  const toggle = (id: string) => {
    const update = narrowed ? setCollapsedMatches : setExpandedSources;
    update((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  return (
    <aside className="activity-rail" aria-label="Request history">
      <div className="rail-head">
        <h2>Request history</h2>
        <p
          className="history-count"
          title="Counts cover the request history loaded in this view."
        >
          {history.total} recent {history.total === 1 ? "request" : "requests"}{" "}
          · {history.sources} {history.sources === 1 ? "source" : "sources"}
          {history.missingSource > 0 && (
            <small>{history.missingSource} without a recorded source</small>
          )}
        </p>
        <div className="rail-filters">
          {(["All", "Active", "Errors"] as const).map((value) => (
            <button
              key={value}
              className={value === filter ? "active" : ""}
              aria-pressed={value === filter}
              onClick={() => {
                setFilter(value);
                setCollapsedMatches(new Set());
              }}
            >
              {value}
            </button>
          ))}
        </div>
        <label className="search">
          <Search size={14} />
          <input
            aria-label="Search requests"
            placeholder="Search requests"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setCollapsedMatches(new Set());
            }}
          />
        </label>
        {narrowed && (
          <p className="history-matches" role="status">
            {history.matched} matching{" "}
            {history.matched === 1 ? "request" : "requests"}
          </p>
        )}
      </div>
      <div className="jobs request-history-groups">
        {history.groups.map((group) => {
          const expanded = narrowed
            ? !collapsedMatches.has(group.id)
            : expandedSources.has(group.id);
          const listId = `${listPrefix}-${group.id}`;
          const source = group.address || "Source not recorded";
          return (
            <section
              className="request-source"
              key={group.id}
              aria-label={`Request history for ${source}`}
            >
              <button
                className="request-source-toggle"
                aria-expanded={expanded}
                aria-controls={listId}
                onClick={() => toggle(group.id)}
              >
                <span className="request-source-heading">
                  <strong>{source}</strong>
                  {expanded ? (
                    <ChevronDown size={15} />
                  ) : (
                    <ChevronRight size={15} />
                  )}
                </span>
                <span className="request-source-total">
                  {narrowed
                    ? `${group.jobs.length} matching of ${group.total}`
                    : group.total}{" "}
                  {group.total === 1 ? "request" : "requests"}
                </span>
                <span className="request-source-counts">
                  <span className={group.counts.active ? "has-active" : ""}>
                    {group.counts.active} active
                  </span>
                  <span className={group.counts.error ? "has-errors" : ""}>
                    {group.counts.error}{" "}
                    {group.counts.error === 1 ? "error" : "errors"}
                  </span>
                  <span>{group.counts.completed} completed</span>
                  <span>{group.counts.cancelled} cancelled</span>
                  {group.counts.other > 0 && (
                    <span>{group.counts.other} other</span>
                  )}
                </span>
                <span className="request-source-last">
                  Last request {sourceTimeLabel(group.latestSeen)}
                </span>
              </button>
              {group.latestFailure && (
                <p className="request-source-error">
                  <strong>Latest error</strong>
                  {group.latestFailure}
                </p>
              )}
              <div
                className="request-source-history"
                id={listId}
                hidden={!expanded}
              >
                {expanded &&
                  group.jobs.map((job) => {
                    const reason = failureReason(job);
                    const software = job.caller
                      ? callerClientLabel(job.caller)
                      : "Client metadata not recorded";
                    const context =
                      job.caller?.identity_basis === "operator_test"
                        ? `${callerDisplayName(job.caller)} · Console test`
                        : job.caller?.reported_name
                          ? `${job.caller.reported_name} · ${software}`
                          : software;
                    return (
                      <button
                        key={job.id}
                        data-job-id={job.id}
                        className={`request-history-row ${hover?.id === job.id ? "highlighted" : ""}`}
                        onMouseEnter={() => onHover(job)}
                        onFocus={() => onHover(job)}
                        onMouseLeave={() => onHover(null)}
                        onBlur={() => onHover(null)}
                        onClick={() => onSelect(job)}
                      >
                        <span className="request-row-heading">
                          <strong>{job.decision.route || job.requested}</strong>
                          <ChevronRight size={13} />
                        </span>
                        <span className="request-row-time">
                          {sourceTimeLabel(job.ts)}
                        </span>
                        <span className="request-row-state">
                          <span className={`status-dot ${job.status}`} />
                          {job.status}
                          {job.elapsed_ms != null && (
                            <span>{(job.elapsed_ms / 1000).toFixed(1)}s</span>
                          )}
                        </span>
                        <span className="request-row-context">{context}</span>
                        {reason ? (
                          <p className="request-row-error">{reason}</p>
                        ) : (
                          job.model && (
                            <p className="request-row-model">{job.model}</p>
                          )
                        )}
                      </button>
                    );
                  })}
              </div>
            </section>
          );
        })}
        {!history.groups.length && (
          <div className="rail-empty">
            <Clock3 size={23} />
            <p>
              {events.length
                ? "No matching requests"
                : "Your next request appears here."}
            </p>
            <small>Expand a source to inspect each request.</small>
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
