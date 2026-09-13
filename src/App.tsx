import {
  ArrowUpRight,
  ChevronRight,
  Cpu,
  KeyRound,
  Network,
  Plus,
  Radio,
  Route as RouteIcon,
  X,
} from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { api, post } from "./api";
import { timeLabel } from "./components";
import { EngineEditor, newClient, newEngine, newRoute } from "./editors";
import {
  NetworkSummary,
  NetworkView,
  networkIsScanning,
} from "./network/NetworkView";
import "./styles.css";
import type { Client, Config, Engine, Job, Route } from "./types";
import { useRouterState } from "./useRouterState";
import { ActivityRail } from "./views/ActivityRail";
import { ClientsView } from "./views/ClientsView";
import { ConnectionDialog } from "./views/ConnectionDialog";
import { JobDialog } from "./views/JobDialog";
import { RoutesView } from "./views/RoutesView";

import { EngineCard } from "./EngineCard";
import { SettingsPage } from "./settings/SettingsPage";
import { MergeEnginesDialog } from "./editors/MergeEnginesDialog";
import { engineUrls } from "./engine-addresses";

type Tab = "Overview" | "Network" | "Routes" | "Callers" | "Settings";
export function App() {
  const { state, locked, connectionError, reload } = useRouterState();
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("Overview");
  const [settingsSection, setSettingsSection] = useState<
    "Access" | "Discovery"
  >("Access");
  const [focusDiscoveryTargets, setFocusDiscoveryTargets] = useState(false);
  const [engine, setEngine] = useState<Engine | null>(null),
    [route, setRoute] = useState<Route | null>(null),
    [client, setClient] = useState<Client | null>(null);
  const [merging, setMerging] = useState<Engine | null>(null);
  const [job, setJob] = useState<Job | null>(null),
    [hover, setHover] = useState<Job | null>(null),
    [endpoints, setEndpoints] = useState(false);
  const [token, setToken] = useState(""),
    [unlocking, setUnlocking] = useState(false);
  const workspace = useRef<HTMLDivElement>(null);
  const [curve, setCurve] = useState("");
  async function action(fn: () => Promise<unknown>) {
    try {
      await fn();
      setError("");
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  async function save(config: Config) {
    const saved = await api<Config>("/api/v1/config", {
      method: "PUT",
      body: JSON.stringify(config),
    });
    await reload();
    return saved;
  }
  useLayoutEffect(() => {
    function update() {
      const root = workspace.current;
      if (!root || !hover?.engine_id || tab !== "Overview") {
        setCurve("");
        return;
      }
      const from = root.querySelector<HTMLElement>(
          `[data-job-id="${CSS.escape(hover.id)}"]`,
        ),
        to = root.querySelector<HTMLElement>(
          `[data-engine-id="${CSS.escape(hover.engine_id)}"] .engine-orbit`,
        );
      if (!from || !to) {
        setCurve("");
        return;
      }
      const a = from.getBoundingClientRect(),
        b = to.getBoundingClientRect(),
        r = root.getBoundingClientRect();
      const x = a.right - r.left,
        y = a.top + a.height / 2 - r.top,
        xx = b.left - r.left + 5,
        yy = b.top + b.height / 2 - r.top;
      setCurve(`M ${x} ${y} C ${x + 60} ${y}, ${xx - 60} ${yy}, ${xx} ${yy}`);
    }
    update();
    const ro = new ResizeObserver(update);
    if (workspace.current) ro.observe(workspace.current);
    window.addEventListener("resize", update);
    workspace.current?.addEventListener("scroll", update, true);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
      workspace.current?.removeEventListener("scroll", update, true);
    };
  }, [hover, state, tab]);
  if (locked)
    return (
      <div className="unlock">
        <Network size={34} />
        <h1>Model Router</h1>
        <p>Unlock your routing workspace.</p>
        <form
          className="paper"
          onSubmit={async (e) => {
            e.preventDefault();
            setUnlocking(true);
            try {
              await post("/api/v1/login", { token });
              setToken("");
              setError("");
              await reload();
            } catch (e) {
              setError(String(e instanceof Error ? e.message : e));
            } finally {
              setUnlocking(false);
            }
          }}
        >
          <label className="field">
            <span>Operator key</span>
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoFocus
              autoComplete="current-password"
              required
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button className="primary" disabled={unlocking}>
            <KeyRound size={16} />
            {unlocking ? "Unlocking…" : "Unlock router"}
          </button>
          <small>
            Your operator key is stored privately on the router host.
          </small>
        </form>
      </div>
    );
  if (!state)
    return (
      <div className="unlock">
        <Network size={32} />
        <h1>Model Router</h1>
        <p>{connectionError || "Connecting to your router…"}</p>
      </div>
    );
  const config = state.config;
  const hasDiscoveryScope =
    config.discovery.targets.length > 0 || config.discovery.include_loopback;
  const activeRoute = config.routes.find((r) => r.id === route?.id) || route;
  const activeClient = state.clients.find((c) => c.id === client?.id) || client;
  function selectTab(
    t: Tab,
    section: "Access" | "Discovery" = "Access",
    focusTargets = false,
  ) {
    setSettingsSection(section);
    setFocusDiscoveryTargets(
      t === "Settings" && section === "Discovery" && focusTargets,
    );
    setTab(t);
    setError("");
    setHover(null);
    workspace.current?.querySelector("main")?.scrollTo({ top: 0 });
  }
  function configureDiscovery() {
    selectTab("Settings", "Discovery", true);
  }
  return (
    <div className="application">
      <header className="titlebar">
        <div className="brand">
          <Network size={25} strokeWidth={1.7} />
          <span>Model Router</span>
        </div>
        <span className="connection">
          <span
            className={`status-dot ${connectionError ? "offline" : "available"}`}
          />
          {state.environment_label || new URL(state.base_url).host}
        </span>
      </header>
      <nav className="topbar paper" aria-label="Main navigation">
        <div className="tabs">
          {(
            ["Overview", "Network", "Routes", "Callers", "Settings"] as Tab[]
          ).map((t) => (
            <button
              key={t}
              aria-current={tab === t ? "page" : undefined}
              className={tab === t ? "active" : ""}
              onClick={() => selectTab(t)}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="top-actions">
          <button className="subtle" onClick={() => setEndpoints(true)}>
            Connect a caller
            <ArrowUpRight size={15} />
          </button>
          {tab === "Overview" ? (
            <button className="primary" onClick={() => setEngine(newEngine())}>
              <Plus size={16} />
              Connect engine
            </button>
          ) : tab === "Routes" ? (
            <button className="primary" onClick={() => setRoute(newRoute())}>
              <Plus size={16} />
              Add route
            </button>
          ) : tab === "Callers" ? (
            <button className="primary" onClick={() => setClient(newClient())}>
              <Plus size={16} />
              Add caller
            </button>
          ) : null}
        </div>
      </nav>
      {(connectionError || error) && (
        <div className="global-error" role="alert">
          {connectionError || error}
          <button
            className="icon-button"
            aria-label="Dismiss error"
            onClick={() => setError("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {state.setup_required && (
        <div className="setup-banner" role="status">
          <div>
            <strong>First-use setup</strong>
            <span>
              This private setup session is ready. Connect an engine, review
              access, then save to finish setup.
            </span>
          </div>
          <button
            className="subtle"
            onClick={() => selectTab("Settings", "Access")}
          >
            Review access
            <ChevronRight size={14} />
          </button>
        </div>
      )}
      <div
        className={"workspace " + (tab === "Routes" ? "routes-workspace" : "")}
        ref={workspace}
      >
        <ActivityRail
          events={state.events}
          hover={hover}
          onHover={setHover}
          onSelect={setJob}
        />
        {curve && (
          <svg className="route-connection" aria-hidden="true">
            <path d={curve} />
          </svg>
        )}
        <main>
          {state.warnings?.map((warning) => (
            <p className="hint" role="status" key={warning}>
              {warning}
            </p>
          ))}
          {tab === "Overview" && (
            <section className="overview">
              <div className="section-heading">
                <div>
                  <h1>Your engines</h1>
                  <p>Live models, wherever they are served.</p>
                </div>
                <button
                  className="subtle"
                  onClick={() => {
                    if (!hasDiscoveryScope) {
                      configureDiscovery();
                      return;
                    }
                    void action(() => post("/api/v1/discover"));
                  }}
                  disabled={
                    networkIsScanning(state.network) || state.discovery.scanning
                  }
                >
                  {hasDiscoveryScope || networkIsScanning(state.network) ? (
                    <Radio
                      size={16}
                      className={networkIsScanning(state.network) ? "pulse" : ""}
                    />
                  ) : (
                    <ChevronRight size={16} />
                  )}
                  {networkIsScanning(state.network)
                    ? "Discovering…"
                    : hasDiscoveryScope
                      ? "Discover"
                      : "Set discovery scope"}
                </button>
              </div>
              <button
                className="auto-summary"
                onClick={() => {
                  setRoute(
                    config.routes.find((r) => r.name === "auto") || null,
                  );
                  selectTab("Routes");
                }}
              >
                <RouteIcon size={16} />
                <strong>auto</strong>
                <span>
                  {config.routes.find((r) => r.name === "auto")?.purpose ||
                    "Configure your default route"}
                </span>
                <ChevronRight size={15} />
              </button>
              <div className="engine-list">
                {state.engines.map((e) => (
                  <EngineCard
                    key={e.id}
                    engine={e}
                    onMerge={() => setMerging(e)}
                    onEdit={() =>
                      setEngine(config.engines.find((x) => x.id === e.id)!)
                    }
                    onChange={(change) =>
                      action(() =>
                        save({
                          ...config,
                          engines: config.engines.map((x) =>
                            x.id === e.id ? { ...x, ...change } : x,
                          ),
                        }),
                      )
                    }
                    onRefresh={() =>
                      action(() => post(`/api/v1/engines/${e.id}/refresh`))
                    }
                    onRemove={() =>
                      action(() =>
                        save({
                          ...config,
                          engines: config.engines.filter((x) => x.id !== e.id),
                          discovery: {
                            ...config.discovery,
                            ignored_urls: [
                              ...(config.discovery.ignored_urls || []),
                              ...engineUrls(e),
                            ],
                          },
                        }),
                      )
                    }
                  />
                ))}
              </div>
              {!!state.engine_merge_suggestions?.length && (
                <section
                  className="merge-suggestions"
                  aria-labelledby="merge-suggestions-heading"
                >
                  <div>
                    <h2 id="merge-suggestions-heading">
                      Possible duplicate addresses
                    </h2>
                    <p className="hint">
                      These engines share endpoint evidence. Review the complete
                      API before merging; nothing is merged automatically.
                    </p>
                  </div>
                  {state.engine_merge_suggestions.map((suggestion) => {
                    const source = config.engines.find(
                      (e) => e.id === suggestion.source_id,
                    );
                    if (!source) return null;
                    return (
                      <div
                        className="merge-suggestion"
                        key={`${suggestion.source_id}-${suggestion.target_id}`}
                      >
                        <div>
                          <strong>{suggestion.source_name}</strong>
                          <span>
                            may be the same API as {suggestion.target_name}
                          </span>
                          <small>{suggestion.reasons.join(" · ")}</small>
                        </div>
                        <button onClick={() => setMerging(source)}>
                          Review merge
                        </button>
                      </div>
                    );
                  })}
                </section>
              )}
              {!!state.discovery.pending?.length && (
                <section className="pending-engines">
                  <h2>Discovered nearby</h2>
                  <p className="hint">
                    Automatic registration is off. Choose which engines to
                    connect.
                  </p>
                  {state.discovery.pending.map((found) => (
                    <div className="pending-engine" key={found.base_url}>
                      <div>
                        <strong>{found.name}</strong>
                        <small>
                          {found.base_url} · {found.models.length} models
                        </small>
                      </div>
                      <button
                        onClick={() =>
                          setEngine({
                            ...newEngine(),
                            name: found.name,
                            base_url: found.base_url,
                            capabilities: found.capabilities,
                            catalog_protocol: found.catalog_protocol,
                            source: "discovery",
                          })
                        }
                      >
                        Connect
                      </button>
                    </div>
                  ))}
                </section>
              )}
              {!state.engines.length && (
                <div className="empty-engines">
                  <div className="empty-orbit">
                    <Cpu size={33} strokeWidth={1} />
                  </div>
                  <h2>Make room for your models.</h2>
                  <p>
                    Discover serving engines on your network,
                    <br />
                    or connect a cloud provider.
                  </p>
                  <div className="actions">
                    <button onClick={configureDiscovery}>
                      <Radio size={16} />
                      Set discovery scope
                    </button>
                    <button
                      className="primary"
                      onClick={() => setEngine(newEngine())}
                    >
                      <Plus size={16} />
                      Connect engine
                    </button>
                  </div>
                </div>
              )}
              <NetworkSummary
                report={state.network}
                onOpen={() => selectTab("Network")}
              />
              <div className="discovery-foot">
                <div>
                  <Radio size={14} />
                  <span>
                    {state.discovery.scanning
                      ? "Refreshing discovery registrations"
                      : config.discovery.enabled
                        ? "Automatic discovery is on"
                        : "Automatic discovery is paused"}
                    {state.discovery.last_scan
                      ? ` · Registrations checked ${timeLabel(state.discovery.last_scan)}`
                      : ""}
                  </span>
                </div>
                <button
                  className="text-button"
                  onClick={configureDiscovery}
                >
                  Configure discovery
                  <ChevronRight size={14} />
                </button>
              </div>
              {state.discovery.error && (
                <p className="error">{state.discovery.error}</p>
              )}
            </section>
          )}
          {tab === "Network" && (
            <NetworkView
              report={state.network}
              scopeReady={hasDiscoveryScope}
              onConfigureDiscovery={configureDiscovery}
              onDiscover={() => action(() => post("/api/v1/discover"))}
              onConnect={(service) =>
                setEngine({
                  ...newEngine(),
                  name: service.name || service.origin,
                  base_url: service.base_url || service.origin + "/v1",
                  source: "manual",
                })
              }
            />
          )}
          {tab === "Routes" && (
            <RoutesView
              callers={state.observed_callers}
              topology={state.route_map}
              jobs={state.events}
              onCaller={(value) => {
                setClient(value);
                selectTab("Callers");
              }}
              onEngine={setEngine}
              onJob={setJob}
              config={config}
              engines={state.engines}
              activeRoute={activeRoute}
              onSelect={setRoute}
              save={save}
              onDelete={() =>
                action(async () => {
                  if (!activeRoute) return;
                  await save({
                    ...config,
                    routes: config.routes.filter(
                      (item) => item.id !== activeRoute.id,
                    ),
                  });
                  setRoute(null);
                })
              }
            />
          )}
          {tab === "Callers" && (
            <ClientsView
              config={config}
              engines={state.engines}
              activeClient={activeClient}
              onSelect={setClient}
              save={save}
              clients={state.clients}
              callers={state.observed_callers}
              onDelete={() =>
                action(async () => {
                  if (!activeClient) return;
                  await save({
                    ...config,
                    clients: config.clients.filter(
                      (item) => item.id !== activeClient.id,
                    ),
                  });
                  setClient(null);
                })
              }
            />
          )}
          {tab === "Settings" && (
            <SettingsPage
              key={`${settingsSection}-${focusDiscoveryTargets ? "scope" : "default"}`}
              initialSection={settingsSection}
              focusDiscoveryTargets={focusDiscoveryTargets}
              config={config}
              operatorUrl={state.operator_url ?? null}
              save={save}
              onSignOut={() => void action(() => post("/api/v1/logout", {}))}
            />
          )}
        </main>
      </div>
      {engine && (
        <EngineEditor
          initial={engine}
          models={state.engines.find((e) => e.id === engine.id)?.models || []}
          config={config}
          save={save}
          onClose={() => {
            setEngine(null);
            void reload();
          }}
        />
      )}
      {merging && (
        <MergeEnginesDialog
          source={merging}
          config={config}
          targetId={
            state.engine_merge_suggestions?.find(
              (suggestion) => suggestion.source_id === merging.id,
            )?.target_id
          }
          onMerged={reload}
          onClose={() => setMerging(null)}
        />
      )}
      {endpoints && (
        <ConnectionDialog
          config={config}
          baseUrl={state.base_url}
          onClose={() => setEndpoints(false)}
          onManage={() => selectTab("Callers")}
        />
      )}
      {job && <JobDialog job={job} onClose={() => setJob(null)} />}
    </div>
  );
}
