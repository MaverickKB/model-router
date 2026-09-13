import { callerClientLabel, CallerIdentity } from "./caller-identity";
import { sourceTimeLabel } from "./source-time";
import type { Client, ObservedCaller } from "./types";
import "./caller-sources.css";

export type CallerSource = {
  address: string;
  callers: ObservedCaller[];
  lastSeen: number;
};

export function groupCallerSources(callers: ObservedCaller[]): CallerSource[] {
  const sources = new Map<string, CallerSource>();
  for (const caller of callers) {
    let source = sources.get(caller.source_address);
    if (!source) {
      source = { address: caller.source_address, callers: [], lastSeen: 0 };
      sources.set(caller.source_address, source);
    }
    source.callers.push(caller);
    source.lastSeen = Math.max(source.lastSeen, caller.last_seen);
  }
  for (const source of sources.values()) {
    source.callers.sort(
      (a, b) => b.last_seen - a.last_seen || a.id.localeCompare(b.id),
    );
  }
  return [...sources.values()].sort((a, b) =>
    a.address.localeCompare(b.address),
  );
}

export function CallerSourceDetails({
  source,
  policies,
  onEditPolicy,
}: {
  source: CallerSource;
  policies: Client[];
  onEditPolicy?: (policy: Client) => void;
}) {
  const address = source.address || "Source address unavailable";
  const names = [
    ...new Set(
      source.callers.map((caller) => caller.reported_name).filter(Boolean),
    ),
  ];
  const systems = [
    ...new Set(
      source.callers.map((caller) => caller.client_os).filter(Boolean),
    ),
  ];
  const requests = source.callers.reduce(
    (count, caller) => count + (caller.request_count ?? 1),
    0,
  );
  return (
    <section
      className="caller-source-details"
      aria-label={`Source details for ${address}`}
    >
      <header>
        <p className="source-eyebrow">Direct source address</p>
        <h2>{address}</h2>
        <p>
          {requests} {requests === 1 ? "request" : "requests"} observed · Last
          seen {sourceTimeLabel(source.lastSeen)}
        </p>
      </header>
      {(names.length > 0 || systems.length > 0) && (
        <dl className="source-reported-metadata">
          {names.length > 0 && (
            <>
              <dt>Reported names</dt>
              <dd>{names.join(", ")}</dd>
            </>
          )}
          {systems.length > 0 && (
            <>
              <dt>Reported operating systems</dt>
              <dd>{systems.join(", ")}</dd>
            </>
          )}
        </dl>
      )}
      <h3>Request metadata</h3>
      <p className="source-explanation">
        Software and identification recorded from requests at this address. One
        address can represent several applications or devices.
      </p>
      <div className="source-metadata-list">
        {source.callers.map((caller) => {
          const policy = policies.find(
            (entry) => entry.id === caller.policy_id,
          );
          return (
            <details key={caller.id} className="source-metadata-record">
              <summary>
                <span className="source-metadata-summary">
                  <strong>Software: {callerClientLabel(caller)}</strong>
                  <span>
                    {caller.reported_name && (
                      <>Reported name: {caller.reported_name} · </>
                    )}
                    {policy
                      ? `Policy identified by request: ${policy.name}`
                      : "No named policy identified by the request"}
                  </span>
                </span>
              </summary>
              <CallerIdentity
                caller={caller}
                policy={policy}
                showHeading={false}
              />
              {policy && onEditPolicy && (
                <button className="subtle" onClick={() => onEditPolicy(policy)}>
                  Edit permission policy: {policy.name}
                </button>
              )}
            </details>
          );
        })}
      </div>
    </section>
  );
}
