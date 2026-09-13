import { Cloud, Cpu, Pencil, Route as RouteIcon, Users, X } from "lucide-react";
import { useState } from "react";
import { Dialog } from "../components";
import {
  callerDisplayName,
  callerSummary,
  PermissionPolicyIdentity,
} from "../caller-identity";
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
import "./map.css";

type Selection = { kind: "caller" | "route" | "engine"; id: string };
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
  const [pending, setPending] = useState<{
    config: Config;
    link: MapLink;
  } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = jobs.filter((job) =>
    ["running", "routing", "waiting"].includes(job.status),
  );
  const policies = config.clients;
  const observationsFor = (policyId: string) =>
    callers.filter((caller) => caller.policy_id === policyId);
  const unassigned =
    topology?.unassigned_callers || callers.filter((caller) => !caller.policy_id);
  const callerNodes = [
    ...policies.map((policy) => ({
      id: policy.id,
      name: policy.name,
      detail: `${policy.kind ? `Kind: ${policy.kind}` : "Kind not set"} · ${observationsFor(policy.id).length} connection${observationsFor(policy.id).length === 1 ? "" : "s"}`,
      policy,
      observation: null as ObservedCaller | null,
    })),
    ...unassigned.map((caller) => ({
      id: caller.id,
      name: callerDisplayName(caller),
      detail: `Observed connection · ${callerSummary(caller)} · No permission policy`,
      policy: null,
      observation: caller,
    })),
  ];
  const rows = Math.max(
    callerNodes.length,
    config.routes.length,
    engines.length,
    2,
  );
  const height = 50 + rows * 104;
  const y = (index: number) => 50 + index * 104 + 42;
  const pick = (next: Selection) => {
    if (
      selected?.kind === "caller" &&
      next.kind === "route" &&
      policies.some((policy) => policy.id === selected.id)
    ) {
      setPending({
        config,
        link: {
          kind: "caller",
          callerId: selected.id,
          routeId: next.id,
        },
      });
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
    } else
      select(
        selected?.id === next.id && selected.kind === next.kind ? null : next,
      );
    setError("");
  };
  const edges: {
    id: string;
    from: number;
    to: number;
    x: number;
    xx: number;
    ready: boolean;
    backup: boolean;
    title: string;
    jobs: Job[];
    dim: boolean;
  }[] = [];
  for (const edge of topology?.caller_routes || []) {
    const a = callerNodes.findIndex((caller) => caller.id === edge.caller_id);
    const b = config.routes.findIndex((route) => route.id === edge.route_id);
    if (a < 0 || b < 0) continue;
    edges.push({
      id: `${edge.caller_id}-${edge.route_id}`,
      from: y(a),
      to: y(b),
      x: 280,
      xx: 360,
      ready: !!edge.ready_engines.length,
      backup: false,
      title: edge.reason,
      jobs: active.filter(
        (job) =>
          (job.caller?.policy_id || job.client_id) === edge.caller_id &&
          (job.decision.route || job.requested) === config.routes[b].name,
      ),
      dim:
        !!selected &&
        (selected.kind === "caller"
          ? selected.id !== edge.caller_id
          : selected.kind === "route"
            ? selected.id !== edge.route_id
            : !edge.ready_engines.includes(selected.id)),
    });
  }
  for (const edge of topology?.route_engines || []) {
    const a = config.routes.findIndex((route) => route.id === edge.route_id);
    const b = engines.findIndex((engine) => engine.id === edge.engine_id);
    if (a < 0 || b < 0) continue;
    const callerPath =
      selected?.kind === "caller"
        ? topology?.caller_routes.find(
            (value) =>
              value.caller_id === selected.id &&
              value.route_id === edge.route_id,
          )
        : undefined;
    edges.push({
      id: `${edge.route_id}-${edge.engine_id}-${edge.tier}`,
      from: y(a),
      to: y(b),
      x: 640,
      xx: 720,
      ready:
        edge.ready &&
        (selected?.kind !== "caller" ||
          !!callerPath?.ready_engines.includes(edge.engine_id)),
      backup: edge.tier === "fallback",
      title: `${edge.tier === "fallback" ? "Backup" : "Primary"}${edge.dynamic ? " · Automatic selection" : " · Pinned engine"} · ${edge.models.length ? edge.models.join(", ") : "Waiting for matching models"}`,
      jobs: active.filter(
        (job) =>
          job.engine_id === edge.engine_id &&
          (job.decision.route || job.requested) === config.routes[a].name &&
          job.tier === edge.tier,
      ),
      dim:
        !!selected &&
        (selected.kind === "engine"
          ? selected.id !== edge.engine_id
          : selected.kind === "route"
            ? selected.id !== edge.route_id
            : !callerPath),
    });
  }
  const node = (
    value: Selection,
    name: string,
    detail: string,
    icon: React.ReactNode,
    edit?: () => void,
    disabled = false,
    observations: ObservedCaller[] = [],
  ) => (
    <div
      key={value.id}
      className={`map-node ${selected?.id === value.id ? "selected" : ""} ${disabled ? "disabled" : ""}`}
    >
      <button
        className="map-node-select"
        onClick={() => pick(value)}
        aria-pressed={selected?.id === value.id}
        aria-label={`Select ${value.kind === "caller" ? (edit ? "permission policy" : "observed caller") : value.kind} ${name}`}
        title={`${name} · ${detail}`}
      >
        {icon}
        <span>
          <strong>{name}</strong>
          <small>{detail}</small>
          {observations.length > 0 && (
            <small className="map-observations">
              {observations.slice(0, 2).map((observation) => (
                <span key={observation.id}>
                  {callerDisplayName(observation)} ·{" "}
                  {callerSummary(observation)}
                </span>
              ))}
              {observations.length > 2 && (
                <span>+{observations.length - 2} more connections</span>
              )}
            </small>
          )}
        </span>
      </button>
      {edit && (
        <button
          className="icon-button"
          aria-label={`Edit ${value.kind} ${name}`}
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
    <section
      className="routes-map"
      aria-label="Permission policy route engine map"
    >
      <div className="map-intro">
        <div>
          <h2>Follow the route</h2>
          <p>
            Follow observed callers and saved permission policies into each
            route, then into the engines that can serve it. Select a saved
            policy, then a route to link them; observed callers without a
            policy remain visible for review.
          </p>
        </div>
        {selected && (
          <button className="text-button" onClick={() => select(null)}>
            <X size={14} />
            Clear selection
          </button>
        )}
      </div>
      <div className="map-legend">
        <span>
          <i />
          Eligible text path
        </span>
        <span className="backup">
          <i />
          Backup
        </span>
        <span className="waiting">
          <i />
          Waiting or restricted
        </span>
      </div>
      {!topology && (
        <p role="status">
          The candidate has not supplied a current routing map.
        </p>
      )}
      <div className="map-scroll">
        <div className="route-map-board" style={{ height }}>
          <svg
            viewBox={`0 0 1000 ${height}`}
            preserveAspectRatio="none"
            aria-label="Routing connections"
          >
            {edges.map((edge) => (
              <g
                key={edge.id}
                className={`map-edge ${edge.ready ? "ready" : "waiting"} ${edge.backup ? "backup" : ""} ${edge.jobs.length ? "active" : ""} ${edge.dim ? "dim" : ""}`}
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
          <div className="map-column">
            <h3>Callers and permission policies</h3>
            {callerNodes.map((caller) =>
              node(
                { kind: "caller", id: caller.id },
                caller.name,
                caller.detail,
                <Users size={18} />,
                caller.policy ? () => editCaller(caller.policy!) : undefined,
                caller.policy ? !caller.policy.enabled : false,
                caller.policy
                  ? observationsFor(caller.policy.id)
                  : caller.observation
                    ? [caller.observation]
                    : [],
              ),
            )}
            {!callerNodes.length && (
              <p className="map-empty">
                No callers observed and no permission policies configured yet.
              </p>
            )}
          </div>
          <div className="map-column">
            <h3>Routes</h3>
            {config.routes.map((route) =>
              node(
                { kind: "route", id: route.id },
                route.name,
                `${route.purpose || "Routing policy"} · ${route.require_caller_key ? "Caller key required" : "Caller key optional"}`,
                <RouteIcon size={18} />,
                () => editRoute(route),
                !route.enabled,
              ),
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
        Paths follow saved policy and current catalogs. Request-specific tools,
        images and capacity can narrow selection. Dashed paths remain
        configured; they have no eligible text destination now.
      </p>
      {selected?.kind === "caller" &&
        config.clients.some((policy) => policy.id === selected.id) && (
          <PermissionPolicyIdentity
            policy={config.clients.find((policy) => policy.id === selected.id)!}
            callers={observationsFor(selected.id)}
          />
        )}
      {selected?.kind === "caller" && (
        <div className="map-reasons">
          {topology?.caller_routes
            .filter((edge) => edge.caller_id === selected.id)
            .map((edge) => (
              <p key={edge.route_id}>
                <strong>
                  {config.routes.find((r) => r.id === edge.route_id)?.name}
                </strong>{" "}
                {edge.reason}
              </p>
            ))}
        </div>
      )}
      {!!active.length && (
        <div className="map-jobs">
          {active.map((job) => (
            <button key={job.id} onClick={() => inspectJob(job)}>
              {job.caller ? callerDisplayName(job.caller) : "Caller not recorded"} →{" "}
              {job.decision.route || job.requested} →{" "}
              {job.engine || "Selecting"}
            </button>
          ))}
        </div>
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
