import {
  Cloud,
  Cpu,
  Monitor,
  Pencil,
  Route as RouteIcon,
  Users,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Dialog } from "../components";
import { sourceTimeLabel } from "../source-time";
import {
  callerClientLabel,
  callerDisplayName,
  PermissionPolicyIdentity,
} from "../caller-identity";
import { CallerSourceDetails, groupCallerSources } from "../caller-sources";
import { engineHost } from "../engine-addresses";
import type {
  Client,
  ObservedCaller,
  Config,
  Engine,
  EngineView,
  Job,
  Route,
  RouteMap,
} from "../types";
import { linkPolicy, type MapLink } from "./links";
import { activeJobsForSource, sourceRouteAccess } from "./source-topology";
import "./map.css";

type Selection = {
  kind: "source" | "policy" | "route" | "engine" | "account";
  id: string;
};
type AccessState = "ready" | "mixed" | "blocked" | "unknown";
const accessLabel = (state: AccessState) =>
  ({
    ready: "Available",
    mixed: "Mixed access",
    blocked: "Unavailable",
    unknown: "Access not yet known",
  })[state];

type Props = {
  config: Config;
  callers?: ObservedCaller[];
  engines: EngineView[];
  topology?: RouteMap;
  jobs: Job[];
  save: (config: Config) => Promise<Config>;
  editCaller: (caller: Client) => void;
  editRoute: (route: Route) => void;
  editEngine: (engine: Engine) => void;
  inspectJob: (job: Job) => void;
};

export function RoutesMap({
  config,
  callers = [],
  engines,
  topology,
  jobs,
  save,
  editCaller,
  editRoute,
  editEngine,
  inspectJob,
}: Props) {
  const [selected, select] = useState<Selection | null>(null);
  const [inspectedRoute, inspectRoute] = useState<string | null>(null);
  const [pending, setPending] = useState<{
    config: Config;
    link: MapLink;
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const graphRef = useRef<HTMLDivElement>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const inspectedSourceId = selected?.kind === "source" ? selected.id : null;
  // Live polling refreshes evidence without moving the operator's focus.
  useEffect(() => {
    if (!inspectedSourceId) return;
    inspectorRef.current?.focus({ preventScroll: true });
    inspectorRef.current?.scrollIntoView?.({ block: "start" });
  }, [inspectedSourceId, inspectedRoute]);
  const backToMap = () => {
    select(null);
    inspectRoute(null);
    graphRef.current?.focus({ preventScroll: true });
    graphRef.current?.scrollIntoView?.({ block: "start" });
  };
  const active = [
    ...new Map(
      jobs
        .filter((job) => ["running", "routing", "waiting"].includes(job.status))
        .map((job) => [job.id, job]),
    ).values(),
  ];
  const sources = groupCallerSources(callers);
  const sourceId = (source: { key: string }) => `source:${source.key}`;
  const selectedSource =
    selected?.kind === "source"
      ? sources.find((source) => sourceId(source) === selected.id)
      : undefined;
  const selectedPolicy =
    selected?.kind === "policy"
      ? config.clients.find((policy) => policy.id === selected.id)
      : undefined;
  const observationsFor = (policyId: string) =>
    callers.filter((caller) => caller.policy_id === policyId);
  // Account callers carry the account id as their policy id and are never
  // looked up in config.clients; the router groups them for the map.
  const accountGroups = topology?.account_callers || [];
  const accountId = (id: string) => `account:${id}`;
  const selectedAccount =
    selected?.kind === "account"
      ? accountGroups.find(
          (group) => accountId(group.account_id) === selected.id,
        )
      : undefined;
  const y = (index: number) => 50 + index * 104 + 42;
  const height =
    50 +
    Math.max(sources.length, config.routes.length, engines.length, 1) * 104;
  const pick = (next: Selection) => {
    setError("");
    if (selectedPolicy && next.kind === "route") {
      setPending({
        config,
        link: { kind: "caller", callerId: selectedPolicy.id, routeId: next.id },
      });
    } else if (selected?.kind === "source" && next.kind === "route") {
      inspectRoute(next.id);
    } else if (selected?.kind === "route" && next.kind === "engine") {
      setPending({
        config,
        link: {
          kind: "engine",
          routeId: selected.id,
          engineId: next.id,
          tier: "primary",
        },
      });
    } else {
      select(
        selected?.id === next.id && selected.kind === next.kind ? null : next,
      );
      inspectRoute(null);
    }
  };
  const edges: {
    id: string;
    from: number;
    to: number;
    x: number;
    xx: number;
    state: AccessState;
    backup: boolean;
    title: string;
    jobs: Job[];
    dim: boolean;
  }[] = [];
  sources.forEach((source, sourceIndex) =>
    config.routes.forEach((route, routeIndex) => {
      const access = sourceRouteAccess(
        source,
        route.id,
        topology,
        selected?.kind === "engine" ? selected.id : undefined,
      );
      if (!access.edges.length) return;
      edges.push({
        id: `${sourceId(source)}-${route.id}`,
        from: y(sourceIndex),
        to: y(routeIndex),
        x: 280,
        xx: 360,
        state: access.state,
        backup: false,
        title: `${source.displayName || "Source address unavailable"} → ${route.name}: ${accessLabel(access.state)} · ${access.allowed} of ${access.total} client records have an eligible destination`,
        jobs: activeJobsForSource(source, route.name, active),
        dim:
          !!selected &&
          (selected.kind === "source"
            ? selected.id !== sourceId(source) ||
              (!!inspectedRoute && inspectedRoute !== route.id)
            : selected.kind === "route"
              ? selected.id !== route.id
              : selected.kind === "engine"
                ? !access.allowed
                : false),
      });
    }),
  );
  for (const edge of topology?.route_engines || []) {
    const routeIndex = config.routes.findIndex(
      (route) => route.id === edge.route_id,
    );
    const engineIndex = engines.findIndex(
      (engine) => engine.id === edge.engine_id,
    );
    if (routeIndex < 0 || engineIndex < 0) continue;
    const access = selectedSource
      ? sourceRouteAccess(
          selectedSource,
          edge.route_id,
          topology,
          edge.engine_id,
          edge.tier,
        )
      : undefined;
    const routeJobs = selectedSource
      ? activeJobsForSource(
          selectedSource,
          config.routes[routeIndex].name,
          active,
        )
      : active;
    const state = edge.ready ? access?.state || "ready" : "blocked";
    edges.push({
      id: `${edge.route_id}-${edge.engine_id}-${edge.tier}`,
      from: y(routeIndex),
      to: y(engineIndex),
      x: 640,
      xx: 720,
      state,
      backup: edge.tier === "fallback",
      title: `${edge.tier === "fallback" ? "Backup" : "Primary"} · ${access ? accessLabel(state) + " · " : ""}${edge.models.length ? edge.models.join(", ") : "Waiting for matching models"}`,
      jobs: routeJobs.filter(
        (job) =>
          job.engine_id === edge.engine_id &&
          (job.decision.route || job.requested) ===
            config.routes[routeIndex].name &&
          job.tier === edge.tier,
      ),
      dim:
        !!selected &&
        (selected.kind === "engine"
          ? selected.id !== edge.engine_id
          : selected.kind === "route"
            ? selected.id !== edge.route_id
            : selected.kind === "source"
              ? !access?.edges.length ||
                (!!inspectedRoute && inspectedRoute !== edge.route_id)
              : false),
    });
  }
  const node = (
    value: Selection,
    name: string,
    detail: string,
    icon: React.ReactNode,
    edit?: () => void,
    disabled = false,
  ) => (
    <div
      key={value.id}
      className={`map-node ${selected?.id === value.id && selected.kind === value.kind ? "selected" : ""} ${disabled ? "disabled" : ""}`}
    >
      <button
        className="map-node-select"
        onClick={() => pick(value)}
        aria-pressed={selected?.id === value.id && selected.kind === value.kind}
        aria-label={`Select ${value.kind === "policy" ? "permission policy" : value.kind} ${name}`}
        title={`${name} · ${detail}`}
      >
        {icon}
        <span>
          <strong>{name}</strong>
          <small>{detail}</small>
        </span>
      </button>
      {edit && (
        <button
          className="icon-button"
          aria-label={`Edit ${value.kind === "policy" ? "caller" : value.kind} ${name}`}
          onClick={edit}
        >
          <Pencil size={13} />
        </button>
      )}
    </div>
  );
  const pendingRoute =
    pending &&
    pending.config.routes.find((route) => route.id === pending.link.routeId);
  const pendingCallerId =
    pending?.link.kind === "caller" ? pending.link.callerId : null;
  const pendingEngineId =
    pending?.link.kind === "engine" ? pending.link.engineId : null;
  return (
    <section className="routes-map" aria-label="Source route engine map">
      <div className="map-intro">
        <div>
          <h2>Live routes</h2>
          <p>Select a source to inspect its clients and route access.</p>
        </div>
        {selected && (
          <button
            className="text-button"
            onClick={() => {
              select(null);
              inspectRoute(null);
            }}
          >
            <X size={14} />
            Clear selection
          </button>
        )}
      </div>
      <div className="map-legend">
        <span>
          <i />
          Available
        </span>
        <span className="mixed">
          <i />
          Mixed access
        </span>
        <span className="backup">
          <i />
          Backup
        </span>
        <span className="waiting">
          <i />
          Unavailable or unknown
        </span>
      </div>
      {!topology && <p role="status">Waiting for the current routing map.</p>}
      <div className="map-scroll">
        <div
          className="route-map-board"
          style={{ height }}
          ref={graphRef}
          tabIndex={-1}
          role="group"
          aria-label="Route map"
        >
          <svg
            viewBox={`0 0 1000 ${height}`}
            preserveAspectRatio="none"
            aria-label="Routing connections"
          >
            {edges.map((edge) => (
              <g
                key={edge.id}
                data-edge-id={edge.id}
                className={`map-edge ${edge.state} ${edge.backup ? "backup" : ""} ${edge.jobs.length ? "active" : ""} ${edge.dim ? "dim" : ""}`}
              >
                <title>{edge.title}</title>
                <path
                  d={`M${edge.x},${edge.from} C${edge.x + 40},${edge.from} ${edge.xx - 40},${edge.to} ${edge.xx},${edge.to}`}
                />
                {edge.jobs.length > 0 && (
                  <text
                    x={(edge.x + edge.xx) / 2}
                    y={(edge.from + edge.to) / 2 - 8}
                    textAnchor="middle"
                  >
                    {edge.jobs.length} active
                  </text>
                )}
              </g>
            ))}
          </svg>
          <div className="map-column" aria-label="Observed sources">
            <h3>
              Sources <small>{sources.length}</small>
            </h3>
            {sources.map((source) =>
              node(
                { kind: "source", id: sourceId(source) },
                source.displayName || "Source address unavailable",
                `Last seen ${sourceTimeLabel(source.lastSeen)}`,
                <Monitor size={18} />,
              ),
            )}
            {!sources.length && (
              <p className="map-empty">
                Sources appear here when they make a request.
              </p>
            )}
          </div>
          <div className="map-column">
            <h3>Routes</h3>
            {config.routes.map((route) =>
              node(
                { kind: "route", id: route.id },
                route.name,
                route.require_caller_key
                  ? "Caller key required"
                  : "Caller key optional",
                <RouteIcon size={18} />,
                () => editRoute(route),
                !route.enabled,
              ),
            )}
            {!config.routes.length && (
              <p className="map-empty">
                Add a route to connect callers to engines.
              </p>
            )}
          </div>
          <div className="map-column">
            <h3>Engines</h3>
            {engines.map((engine) =>
              node(
                { kind: "engine", id: engine.id },
                engine.name,
                `${engineHost(engine)} · ${engine.status}`,
                engine.kind === "cloud" ? (
                  <Cloud size={18} />
                ) : (
                  <Cpu size={18} />
                ),
                () => editEngine(engine),
                !engine.enabled,
              ),
            )}
            {!engines.length && (
              <p className="map-empty">Connect an engine to see its routes.</p>
            )}
          </div>
        </div>
      </div>
      <p className="hint">
        Paths show current text availability. Tools, images and capacity are
        checked per request.
      </p>
      {!!active.length && (
        <div className="map-jobs" aria-label="Active requests">
          {active.map((job) => (
            <button key={job.id} onClick={() => inspectJob(job)}>
              {job.caller
                ? callerDisplayName(job.caller)
                : "Source not recorded"}{" "}
              → {job.decision.route || job.requested} →{" "}
              {job.engine || "Selecting"}
            </button>
          ))}
        </div>
      )}
      {selectedSource && (
        <section
          className="map-source-details"
          ref={inspectorRef}
          tabIndex={-1}
          aria-label={`Routing details for ${selectedSource.address || "unknown source"}`}
        >
          <button className="text-button map-back" onClick={backToMap}>
            Back to map
          </button>
          <div className="map-access-details">
            <h3>Route access</h3>
            {config.routes
              .filter((route) => !inspectedRoute || route.id === inspectedRoute)
              .map((route) => {
                const access = sourceRouteAccess(
                  selectedSource,
                  route.id,
                  topology,
                );
                return (
                  <div
                    className={`map-access-row ${access.state}`}
                    key={route.id}
                  >
                    <h4>
                      {route.name}
                      <span>{accessLabel(access.state)}</span>
                    </h4>
                    <p>
                      {access.allowed} of {access.total} client records have an
                      eligible destination.
                    </p>
                    <ul className="map-access-contexts">
                      {selectedSource.callers.map((caller) => {
                        const edge = access.edges.find(
                          (entry) => entry.caller_id === caller.id,
                        );
                        const label =
                          caller.reported_name || callerClientLabel(caller);
                        const state = edge
                          ? edge.ready_engines.length
                            ? "ready"
                            : "blocked"
                          : "unknown";
                        // An account caller's policy id is its account id,
                        // never a configured client.
                        const policy = caller.account_id
                          ? undefined
                          : config.clients.find(
                              (entry) => entry.id === edge?.policy_id,
                            );
                        return (
                          <li
                            key={caller.id}
                            className={`map-access-context ${state}`}
                            aria-label={`Route decision for ${label}`}
                          >
                            <h5>
                              {label}
                              <span>{accessLabel(state)}</span>
                            </h5>
                            <p>
                              {edge?.reason ||
                                "Current route access has not been reported for this client."}
                            </p>
                            <small>
                              Permission policy:{" "}
                              {caller.account_id
                                ? "User account (Settings › Accounts)"
                                : edge
                                  ? policy?.name ||
                                    (edge.policy_id
                                      ? "Policy no longer configured"
                                      : "No named policy identified")
                                  : "Not yet reported"}
                            </small>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                );
              })}
          </div>
          <CallerSourceDetails
            source={selectedSource}
            policies={config.clients}
            onEditPolicy={editCaller}
          />
        </section>
      )}
      <details className="map-policy-tray">
        <summary>
          Permission policies <span>{config.clients.length}</span>
        </summary>
        <p className="hint">
          Select a policy, then a route to configure access for every client
          using that policy.
        </p>
        <div className="map-policy-list">
          {config.clients.map((policy) =>
            node(
              { kind: "policy", id: policy.id },
              policy.name,
              `${policy.kind || "Permission policy"} · ${observationsFor(policy.id).length} client records`,
              <Users size={18} />,
              () => editCaller(policy),
              !policy.enabled,
            ),
          )}
        </div>
        {!config.clients.length && (
          <p className="map-empty">No permission policies configured.</p>
        )}
        {selectedPolicy && (
          <>
            <PermissionPolicyIdentity
              policy={selectedPolicy}
              callers={observationsFor(selectedPolicy.id)}
            />
            <div className="map-reasons">
              {topology?.policy_routes
                ?.filter((edge) => edge.policy_id === selectedPolicy.id)
                .map((edge) => (
                  <p key={edge.route_id}>
                    <strong>
                      {
                        config.routes.find(
                          (route) => route.id === edge.route_id,
                        )?.name
                      }
                    </strong>
                    {edge.reason}
                  </p>
                ))}
            </div>
          </>
        )}
      </details>
      {accountGroups.length > 0 && (
        <details className="map-policy-tray map-account-tray">
          <summary>
            Accounts <span>{accountGroups.length}</span>
          </summary>
          <p className="hint">
            Connections identified by an account holder's own key or registered
            device. Their access is the account's level (Settings › Accounts),
            never a permission policy.
          </p>
          <div className="map-policy-list">
            {accountGroups.map((group) =>
              node(
                { kind: "account", id: accountId(group.account_id) },
                group.name,
                `Account · ${group.observed_callers.length} ${group.observed_callers.length === 1 ? "connection" : "connections"}`,
                <Users size={18} />,
              ),
            )}
          </div>
          {selectedAccount && (
            <div className="map-reasons">
              <p>
                <strong>Level</strong>
                {config.account_levels.find(
                  (level) => level.id === selectedAccount.level_id,
                )?.name || "Level unavailable"}
              </p>
              {selectedAccount.observed_callers.map((caller) => (
                <p key={caller.id}>
                  <strong>
                    {caller.source_address || "Source address unavailable"} ·{" "}
                    {callerClientLabel(caller)}
                  </strong>
                  {topology?.caller_routes
                    .filter((edge) => edge.caller_id === caller.id)
                    .map(
                      (edge) =>
                        `${config.routes.find((route) => route.id === edge.route_id)?.name || edge.route_id}: ${edge.reason}`,
                    )
                    .join(" · ") ||
                    "Current route access has not been reported."}
                </p>
              ))}
            </div>
          )}
        </details>
      )}
      {pending && (
        <Dialog title="Link policy" onClose={() => setPending(null)}>
          <form
            onSubmit={async (event) => {
              event.preventDefault();
              setBusy(true);
              try {
                await save(linkPolicy(pending.config, pending.link));
                setPending(null);
                select(null);
              } catch (e) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {pending.link.kind === "caller" ? (
              <p>
                Update permission policy{" "}
                <strong>
                  {
                    pending.config.clients.find((c) => c.id === pendingCallerId)
                      ?.name
                  }
                </strong>{" "}
                to allow <strong>{pendingRoute?.name}</strong>. This affects
                every connection using that policy. Its engine, model and cloud
                restrictions remain in effect.
              </p>
            ) : (
              <>
                <p>
                  Link <strong>{pendingRoute?.name}</strong> to{" "}
                  <strong>
                    {
                      pending.config.engines.find(
                        (e) => e.id === pendingEngineId,
                      )?.name
                    }
                  </strong>
                  .
                </p>
                <div className="segmented">
                  {(["primary", "fallback"] as const).map((tier) => (
                    <button
                      type="button"
                      key={tier}
                      className={
                        pending.link.kind === "engine" &&
                        pending.link.tier === tier
                          ? "active"
                          : ""
                      }
                      onClick={() =>
                        pending.link.kind === "engine" &&
                        setPending({
                          ...pending,
                          link: { ...pending.link, tier },
                        })
                      }
                    >
                      {tier === "primary" ? "Primary" : "Backup"}
                    </button>
                  ))}
                </div>
                <p className="hint">
                  {!pendingRoute?.[pending.link.tier]?.engine_ids.length
                    ? "This replaces automatic selection for this tier with the selected engine."
                    : "This adds the engine to this tier's pinned destinations."}{" "}
                  Existing model and tag filters still apply. Caller permissions
                  stay unchanged. Use Details for precise filters or to return
                  to automatic selection.
                </p>
              </>
            )}
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <footer>
              <button type="button" onClick={() => setPending(null)}>
                Cancel
              </button>
              <button className="primary" disabled={busy}>
                {busy ? "Saving…" : "Save link"}
              </button>
            </footer>
          </form>
        </Dialog>
      )}
    </section>
  );
}
