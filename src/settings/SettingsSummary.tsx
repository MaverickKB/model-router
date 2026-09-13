import type { Config } from "../types";

function scopeLabel(config: Config) {
  const { targets, include_loopback } = config.discovery;
  return [...targets, ...(include_loopback ? ["router loopback"] : [])].join(
    ", ",
  );
}

function RegistrationSummary({ config }: { config: Config }) {
  const scope = scopeLabel(config);
  return (
    <>
      {scope ? (
        <>
          {config.security.operator_auth_enabled
            ? "Operator authentication is required."
            : "Callers can submit model endpoints without signing in."}{" "}
          Caller and endpoint must both be in scope: {scope}, even when sweeps
          are off.{" "}
        </>
      ) : (
        <>No discovery addresses are allowed for registration. </>
      )}
      {config.discovery.auto_register
        ? "Automatic registration is on: recognized endpoints can become engines and receive requests allowed by caller policies."
        : "Automatic registration is off: new endpoints wait for operator connection. Existing engines keep their settings."}
    </>
  );
}

export function SavedAccess({
  config,
  operatorUrl,
}: {
  config: Config;
  operatorUrl: string | null;
}) {
  const security = config.security;
  const shared = config.clients.find(
    (client) => client.id === security.anonymous_client_id && client.enabled,
  );
  return (
    <section className="settings-summary" aria-label="Saved access state">
      <p className="settings-eyebrow">Saved settings · In effect now</p>
      <h2 id="access-heading">
        {security.operator_auth_enabled
          ? "Operator sign-in required"
          : "Open administration"}
      </h2>
      <p>
        {security.operator_auth_enabled
          ? "An operator key is required to sign in, including over loopback or an SSH tunnel. Existing signed-in sessions remain valid."
          : security.operator_networks.length
            ? `Anyone from ${security.operator_networks.join(", ")} can manage settings, credentials and callers without a key through ${operatorUrl ? "the canonical address or loopback / SSH tunnels" : "loopback / SSH tunnels"}. This is shared authority, not user isolation.`
            : "No trusted source networks are configured. An operator key or existing session is needed to manage the router."}
      </p>
      <p className="settings-address">
        {operatorUrl
          ? `Canonical address: ${operatorUrl}`
          : "Canonical address is not configured. Loopback and SSH tunnels are supported."}
      </p>
      <dl>
        <div>
          <dt>Caller keys</dt>
          <dd>
            {`Each route chooses whether a key is required. All connections are observed. ${shared ? `Unkeyed callers without a source override use “${shared.name}” where the selected route permits them.` : "Without a selected default policy, unkeyed callers still appear in the inventory and can use only routes that permit them."} Supplied keys select their named policy and may be reused across callers.`}
          </dd>
        </div>
        <div>
          <dt>Registration</dt>
          <dd>
            <RegistrationSummary config={config} />
          </dd>
        </div>
        <div>
          <dt>HTTP inspection</dt>
          <dd>
            {config.discovery.inspect_all_open_ports
              ? "Every open port, when a sweep runs."
              : config.discovery.http_ports.length
                ? `Approved ports only: ${config.discovery.http_ports.join(", ")}.`
                : "No sweep ports are approved for HTTP inspection."}{" "}
            Manage sweep scope and inspection in Discovery.
          </dd>
        </div>
      </dl>
    </section>
  );
}

export function SavedDiscovery({ config }: { config: Config }) {
  const policy = config.discovery;
  return (
    <section className="settings-summary" aria-label="Saved discovery state">
      <p className="settings-eyebrow">Saved settings · In effect now</p>
      <h2>Automatic sweeps {policy.enabled ? "on" : "off"}</h2>
      <p>
        {policy.enabled
          ? `The program repeats the configured sweep every ${policy.network_interval_seconds} seconds.`
          : "Manual Discover still uses the saved scope and inspection choices below. Registered model catalogs continue refreshing."}
      </p>
      <dl>
        <div>
          <dt>TCP inventory</dt>
          <dd>
            {scopeLabel(config) || "No targets selected"} · Ports{" "}
            {policy.port_range} ·{" "}
            {policy.scanner === "nmap" ? "Nmap" : "TCP connect"}. Every port in
            this range is checked when a sweep runs.
          </dd>
        </div>
        <div>
          <dt>HTTP inspection</dt>
          <dd>
            {policy.inspect_all_open_ports
              ? "Every open port receives bounded HTTP / HTTPS GET requests for published catalogs and API descriptions."
              : policy.http_ports.length
                ? `Only approved ports (${policy.http_ports.join(", ")}) receive bounded HTTP / HTTPS GET requests for published catalogs and API descriptions.`
                : "No sweep ports receive HTTP requests. Open ports remain visible as unverified services."}{" "}
            Non-HTTP devices can still react badly to unexpected requests.
          </dd>
        </div>
        <div>
          <dt>Registration</dt>
          <dd>
            <RegistrationSummary config={config} />
          </dd>
        </div>
        <div>
          <dt>Announcements</dt>
          <dd>
            Model-service announcements {policy.mdns ? "on" : "off"}.
            Router-local listener inventory{" "}
            {policy.include_loopback ? "on" : "off"}.
          </dd>
        </div>
      </dl>
    </section>
  );
}
