import { Plus, Route as RouteIcon } from "lucide-react";
import { newRoute, RouteEditor } from "../editors";
import { useState } from "react";
import { RoutesMap } from "../map/RoutesMap";
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
export function RoutesView({
  config,
  callers,
  engines,
  activeRoute,
  onSelect,
  save,
  onDelete,
  topology,
  jobs,
  onCaller,
  onEngine,
  onJob,
}: {
  config: Config;
  callers?: ObservedCaller[];
  engines: EngineView[];
  activeRoute: Route | null;
  onSelect: (value: Route | null) => void;
  save: (config: Config) => Promise<Config>;
  onDelete: () => Promise<void>;
  topology?: RouteMap;
  jobs: Job[];
  onCaller: (caller: Client) => void;
  onEngine: (engine: Engine) => void;
  onJob: (job: Job) => void;
}) {
  const [mode, setMode] = useState<"map" | "details">("map");
  const details = mode === "details" || !!activeRoute;
  return (
    <>
      <div className="filter-tabs routes-mode" aria-label="Routes view">
        <button
          className={!details ? "selected" : ""}
          onClick={() => {
            setMode("map");
            onSelect(null);
          }}
        >
          Map
        </button>
        <button
          className={details ? "selected" : ""}
          onClick={() => setMode("details")}
        >
          Details
        </button>
      </div>
      {!details ? (
        <RoutesMap
          config={config}
          callers={callers}
          engines={engines}
          topology={topology}
          jobs={jobs}
          save={save}
          editCaller={onCaller}
          editRoute={onSelect}
          editEngine={onEngine}
          inspectJob={onJob}
        />
      ) : (
        <div className={"management " + (activeRoute ? "has-selection" : "")}>
          <div className="record-list">
            <h2>
              Routes<span>{config.routes.length}</span>
            </h2>
            <p>Stable names. Flexible destinations.</p>
            {config.routes.map((r) => (
              <button
                key={r.id}
                className={activeRoute?.id === r.id ? "selected" : ""}
                onClick={() => onSelect(r)}
              >
                <RouteIcon size={17} />
                <span>
                  <strong>{r.name}</strong>
                  <small>
                    {r.purpose || "Routing policy"} ·{" "}
                    {r.require_caller_key ? "Caller key required" : "Caller key optional"}
                  </small>
                </span>
                <span
                  className={
                    "status-dot " + (r.enabled ? "available" : "disabled")
                  }
                />
              </button>
            ))}
          </div>
          {activeRoute ? (
            <RouteEditor
              key={activeRoute.id}
              initial={activeRoute}
              config={config}
              engines={engines}
              save={save}
              onClose={() => onSelect(null)}
              onDelete={onDelete}
            />
          ) : (
            <div className="detail-empty">
              <RouteIcon size={30} strokeWidth={1.3} />
              <h2>A purpose for every request.</h2>
              <p>
                Select a route to shape its primary selection,
                <br />
                backup, and model preferences.
              </p>
              <button onClick={() => onSelect(newRoute())}>
                <Plus size={16} />
                Add route
              </button>
            </div>
          )}{" "}
        </div>
      )}
    </>
  );
}
