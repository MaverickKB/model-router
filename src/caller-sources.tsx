import { callerClientLabel, CallerIdentity } from "./caller-identity";
import { sourceTimeLabel } from "./source-time";
import type { Client, ObservedCaller } from "./types";
import { useEffect, useState } from "react";
import "./caller-sources.css";

export type CallerSource = {
  key: string;
  address: string;
  addresses: string[];
  displayName: string;
  labelSource:
    | "operator"
    | "reported_hostname"
    | "discovered_hostname"
    | "address";
  hostname: string;
  identityQuality: "network_hardware" | "reported_device" | "address";
  callers: ObservedCaller[];
  lastSeen: number;
};

function sourceKey(caller: ObservedCaller) {
  return caller.source_key || `addr:${caller.source_address}`;
}

function displaySource(source: CallerSource) {
  const latest = source.callers[0];
  source.address = latest.source_address || source.address;
  source.addresses = [
    ...new Set([
      ...source.callers
        .flatMap((caller) => [
          ...(caller.recent_source_addresses || []),
          caller.source_address,
        ])
        .filter(Boolean),
    ]),
  ].sort();
  source.displayName =
    latest.source_label || source.address || "Source address unavailable";
  source.labelSource = latest.source_label_source || "address";
  source.hostname = latest.source_hostname || "";
  source.identityQuality = latest.source_identity_quality || "address";
}

export function groupCallerSources(callers: ObservedCaller[]): CallerSource[] {
  const sources = new Map<string, CallerSource>();
  for (const caller of callers) {
    const key = sourceKey(caller);
    let source = sources.get(key);
    if (!source) {
      source = {
        key,
        address: caller.source_address,
        addresses: [],
        displayName: caller.source_label || caller.source_address,
        labelSource: caller.source_label_source || "address",
        hostname: caller.source_hostname || "",
        identityQuality: caller.source_identity_quality || "address",
        callers: [],
        lastSeen: 0,
      };
      sources.set(key, source);
    }
    source.callers.push(caller);
    source.lastSeen = Math.max(source.lastSeen, caller.last_seen);
  }
  for (const source of sources.values()) {
    source.callers.sort(
      (a, b) => b.last_seen - a.last_seen || a.id.localeCompare(b.id),
    );
    displaySource(source);
  }
  return [...sources.values()].sort(
    (a, b) =>
      a.displayName.localeCompare(b.displayName) || a.key.localeCompare(b.key),
  );
}

export function CallerSourceDetails({
  source,
  policies,
  onEditPolicy,
  onRename,
}: {
  source: CallerSource;
  policies: Client[];
  onEditPolicy?: (policy: Client) => void;
  onRename?: (source: CallerSource, name: string) => Promise<void>;
}) {
  const address =
    source.addresses[0] || source.address || "Source address unavailable";
  const [name, setName] = useState(source.displayName);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  useEffect(() => {
    setName(source.displayName);
  }, [source.key, source.displayName, source.labelSource]);
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
  async function saveName(value: string) {
    if (!onRename) return;
    setSaving(true);
    setSaveError("");
    try {
      await onRename(source, value);
    } catch (error) {
      setName(source.displayName);
      setSaveError(
        error instanceof Error ? error.message : "The friendly name was not saved",
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <section
      className="caller-source-details"
      aria-label={`Source details for ${source.displayName || address}`}
    >
      <header>
        <p className="source-eyebrow">
          {source.labelSource === "operator"
            ? "Named caller source"
            : source.labelSource === "reported_hostname"
              ? "Reported hostname"
              : source.labelSource === "discovered_hostname"
                ? "Discovered hostname"
              : "Direct source address"}
        </p>
        <h2>{source.displayName || address}</h2>
        <p>
          {requests} {requests === 1 ? "request" : "requests"} observed · Last
          seen {sourceTimeLabel(source.lastSeen)}
        </p>
      </header>
      {onRename && (
        <form
          className="source-name-form"
          onSubmit={async (event) => {
            event.preventDefault();
            await saveName(name);
          }}
        >
          <label className="field">
            <span>Friendly name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={source.hostname || address}
              maxLength={100}
            />
          </label>
          <button disabled={saving}>
            {saving ? "Saving..." : "Save name"}
          </button>
          {source.labelSource === "operator" && (
            <button
              type="button"
              className="subtle"
              disabled={saving}
              onClick={() => {
                void saveName("");
              }}
            >
              Clear
            </button>
          )}
        </form>
      )}
      {saveError && <p className="error" role="alert">{saveError}</p>}
      {(source.addresses.length > 0 ||
        source.hostname ||
        names.length > 0 ||
        systems.length > 0) && (
        <dl className="source-reported-metadata">
          {source.addresses.length > 0 && (
            <>
              <dt>Observed addresses</dt>
              <dd>{source.addresses.join(", ")}</dd>
            </>
          )}
          {source.hostname && (
            <>
              <dt>Hostname evidence</dt>
              <dd>{source.hostname}</dd>
            </>
          )}
          <dt>Association</dt>
          <dd>
            {source.identityQuality === "network_hardware"
              ? "Network-observed hardware identifier. The friendly name follows this recorded identifier when discovery later observes it at a new address. It does not grant access."
              : source.identityQuality === "reported_device"
              ? "Caller-reported device identifier. The friendly name follows only that identifier after an address change. It does not grant access."
              : source.labelSource === "reported_hostname" ||
                  source.labelSource === "discovered_hostname"
                ? "Address-bound. A hostname is a display hint, not identity proof. This name stays on this address. A current, unique network hardware identifier or caller-reported device identifier creates a separately nameable source."
                : "Address-bound. This name stays on this address. A current, unique network hardware identifier or caller-reported device identifier creates a separately nameable source."}
          </dd>
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
        Software and identification recorded from requests attributed to this
        endpoint. One address can represent several applications or devices.
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
