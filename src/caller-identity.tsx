import type { ObservedCaller, Client } from "./types";
import { timeLabel } from "./components";

export function identityExplanation(caller: ObservedCaller) {
  switch (caller.identity_basis) {
    case "api_key":
      return "Identified by the key assigned to this permission policy.";
    case "source_network":
      return "Matched by connection address. The application name is self-reported.";
    case "shared_access":
      return "Using default permissions. The application name is self-reported; no individual key was supplied.";
    default:
      return "A route test started in this console, using the selected policy.";
  }
}

export function CallerIdentity({
  caller,
  policy,
}: {
  caller: ObservedCaller;
  policy?: Client;
}) {
  return (
    <section
      className="caller-identity"
      aria-label={`Connection details for ${caller.name}`}
    >
      <h3>{caller.name}</h3>
      <p>{identityExplanation(caller)}</p>
      <dl>
        <dt>Connected from</dt>
        <dd>{caller.source_address}</dd>
        <dt>Client software</dt>
        <dd>{caller.software || "Not supplied"}</dd>
        {caller.reported_name && (
          <>
            <dt>Reported identity</dt>
            <dd>{caller.reported_name}</dd>
          </>
        )}
        <dt>Permissions</dt>
        <dd>{policy?.name || "Policy removed"}</dd>
        <dt>Last seen</dt>
        <dd>
          {timeLabel(caller.last_seen)} · {caller.last_path}
        </dd>
      </dl>
      <p className="hint">
        The source is the direct connection peer. A shared key or shared address
        cannot identify an individual agent process. A dedicated key gives that
        agent its own named identity and permissions.
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
