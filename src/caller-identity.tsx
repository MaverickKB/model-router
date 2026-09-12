import { Fragment } from "react";
import type { ObservedCaller, Client } from "./types";
import { timeLabel } from "./components";

const QUALITY_LABELS = {
  policy_key: "Policy key",
  self_reported: "Self-reported label",
  runtime_hints: "Client runtime metadata",
  transport_only: "Transport evidence only",
} as const;

const HINT_LABELS: Record<string, string> = {
  client_version: "Client version",
  openai_client_user_agent: "OpenAI client user-agent",
  stainless_lang: "SDK language",
  stainless_package_version: "SDK package version",
  stainless_runtime: "Runtime",
  stainless_runtime_version: "Runtime version",
  stainless_os: "Operating system",
  stainless_arch: "Architecture",
};

function peer(caller: ObservedCaller) {
  return caller.source_port == null
    ? caller.source_address
    : `${caller.source_address}:${caller.source_port}`;
}

function parsedSoftware(software: string) {
  const match = software.match(
    /(?:^|\s)(python-requests|Python-urllib|openai-python|OpenAI\/Python|curl|httpx|axios)(?:\/(\S+))?/i,
  );
  if (!match) return null;
  const raw = match[1].toLowerCase();
  if (raw === "openai/python" || raw === "openai-python") {
    return { family: "OpenAI Python", version: match[2] || "" };
  }
  const family =
    raw === "python-requests"
      ? "Python requests"
      : raw === "python-urllib"
        ? "Python urllib"
        : raw === "httpx"
          ? "HTTPX"
          : raw === "axios"
            ? "Axios"
            : "curl";
  return { family, version: match[2] || "" };
}

function clientLabel(caller: ObservedCaller) {
  if (caller.client_family && caller.client_version) {
    return `${caller.client_family} ${caller.client_version}`;
  }
  if (caller.client_family) return caller.client_family;
  const parsed = parsedSoftware(caller.software);
  if (parsed?.version) return `${parsed.family} ${parsed.version}`;
  return parsed?.family || caller.software || "No client metadata";
}

export function callerDisplayName(caller: ObservedCaller) {
  if (caller.reported_name || caller.identity_quality === "policy_key") {
    return caller.name;
  }
  const rawName = `${caller.name} ${caller.software}`;
  return parsedSoftware(rawName) ? "Unidentified caller" : caller.name;
}

export function callerSummary(caller: ObservedCaller) {
  return `${peer(caller)} · ${clientLabel(caller)}`;
}

export function identityExplanation(caller: ObservedCaller) {
  switch (caller.identity_basis) {
    case "api_key":
      return "A configured caller key selected this permission policy. The policy name is operator-assigned; the client metadata below is observed evidence.";
    case "source_network":
      return "The direct source address matched a configured policy. Any application name below is self-reported and does not grant access.";
    case "shared_access":
      return "No caller key was supplied. The direct connection and client metadata are recorded, while any application label is self-reported.";
    case "unassigned":
      return "No configured permission policy accepted this request. The direct connection evidence is retained so the request can be reviewed.";
    default:
      return "This connection was created by a route test in the operator console.";
  }
}

export function CallerIdentity({
  caller,
  policy,
}: {
  caller: ObservedCaller;
  policy?: Client;
}) {
  const quality = caller.identity_quality || "transport_only";
  const hints = Object.entries(caller.identity_hints || {}).filter(
    ([key, value]) =>
      Boolean(value) && key !== "router_caller" && key !== "client_name",
  );
  return (
    <section
      className="caller-identity"
      aria-label={`Connection details for ${callerDisplayName(caller)}`}
    >
      <h3>{callerDisplayName(caller)}</h3>
      <p>{identityExplanation(caller)}</p>
      <dl>
        <dt>Identity evidence</dt>
        <dd>
          {QUALITY_LABELS[quality]} · {caller.identity_basis}
        </dd>
        <dt>Direct peer</dt>
        <dd>{peer(caller)}</dd>
        <dt>Client library</dt>
        <dd>{clientLabel(caller)}</dd>
        {caller.client_runtime && (
          <>
            <dt>Runtime</dt>
            <dd>{caller.client_runtime}</dd>
          </>
        )}
        {caller.client_os && (
          <>
            <dt>Operating system</dt>
            <dd>{caller.client_os}</dd>
          </>
        )}
        {caller.client_arch && (
          <>
            <dt>Architecture</dt>
            <dd>{caller.client_arch}</dd>
          </>
        )}
        {caller.reported_name && (
          <>
            <dt>Reported name</dt>
            <dd>
              {caller.reported_name}
              {caller.reported_name_source
                ? ` · ${caller.reported_name_source}`
                : ""}
            </dd>
          </>
        )}
        <dt>Permissions</dt>
        <dd>{policy?.name || "No permission policy assigned"}</dd>
        <dt>Requests observed</dt>
        <dd>{caller.request_count ?? 1}</dd>
        <dt>First seen</dt>
        <dd>{timeLabel(caller.first_seen ?? caller.last_seen)}</dd>
        <dt>Last request</dt>
        <dd>
          {caller.last_method || "Unknown method"} {caller.last_path}
          <br />
          {timeLabel(caller.last_seen)}
        </dd>
        <dt>User-agent</dt>
        <dd>{caller.software || "Not supplied"}</dd>
        {caller.recent_source_ports &&
          caller.recent_source_ports.length > 1 && (
            <>
              <dt>Recent source ports</dt>
              <dd>{caller.recent_source_ports.join(", ")}</dd>
            </>
          )}
      </dl>
      {hints.length > 0 && (
        <div className="identity-hints">
          <h4>Client-reported hints</h4>
          <dl>
            {hints.map(([key, value]) => (
              <Fragment key={key}>
                <dt>{HINT_LABELS[key] || key}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
          </dl>
        </div>
      )}
      <p className="hint">
        A Python or HTTP library identifies the transport, not the person or
        agent using it. A self-reported label helps an operator recognize a
        connection, while a caller key is what selects named permissions.
      </p>
    </section>
  );
}

export function PermissionPolicyIdentity({
  policy,
  callers,
}: {
  policy: Client;
  callers: ObservedCaller[];
}) {
  return (
    <section
      className="caller-identity"
      aria-label={`Permission policy details for ${policy.name}`}
    >
      <h3>{policy.name}</h3>
      <p>
        Permission policy ·{" "}
        {policy.kind ? `kind: ${policy.kind}` : "kind not set"}. This is the
        identity used for routing decisions, not a claim that an individual
        agent is connected.
      </p>
      <dl>
        <dt>Allowed routes</dt>
        <dd>
          {policy.route_names.length
            ? policy.route_names.join(", ")
            : "No routes"}
        </dd>
        <dt>Observed connections</dt>
        <dd>{callers.length}</dd>
      </dl>
      {callers.map((caller) => (
        <CallerIdentity key={caller.id} caller={caller} policy={policy} />
      ))}
      {!callers.length && (
        <p className="hint">
          No client connection has been observed for this policy yet.
        </p>
      )}
    </section>
  );
}
