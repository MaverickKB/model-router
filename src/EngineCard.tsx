import { ChevronDown, Cloud, Cpu, RefreshCw, Settings2 } from "lucide-react";
import { useState } from "react";
import { Switch, timeLabel } from "./components";
import type { Engine, EngineView } from "./types";
import { engineHost, engineUrls } from "./engine-addresses";

function inventoryStatusLabel(engine: EngineView): string {
  if (engine.model_inventory_source !== "declared") {
    return engine.status === "available" ? "Catalog available" : engine.status;
  }
  if (engine.status === "configured") {
    return "Declared identities pending first request";
  }
  if (engine.status === "available") {
    return "Declared identities verified by request";
  }
  return engine.status;
}

function modelInventoryLabel(engine: EngineView, enabled: boolean | undefined) {
  if (enabled === false) return "Excluded";
  return engine.model_inventory_source === "declared"
    ? "Declared"
    : "Discovered";
}

function inventoryCheckLabel(engine: EngineView): string {
  return engine.model_inventory_source === "declared"
    ? "Endpoint checked"
    : "Catalog checked";
}

export function EngineCard({
  engine: e,
  onEdit,
  onChange,
  onRefresh,
  onRemove,
  onMerge,
}: {
  engine: EngineView;
  onEdit: () => void;
  onChange: (c: Partial<Engine>) => Promise<void>;
  onRefresh: () => Promise<void>;
  onRemove: () => Promise<void>;
  onMerge: () => void;
}) {
  const [open, setOpen] = useState((e.aliases?.length ?? 0) > 0);
  const host = engineHost(e);
  return (
    <article
      className={
        "engine-card paper " + (e.status === "available" ? "" : "unavailable")
      }
      data-engine-id={e.id}
    >
      <div className="engine-top">
        <div className={"engine-orbit " + e.status}>
          {e.kind === "cloud" ? (
            <Cloud size={25} strokeWidth={1.5} />
          ) : (
            <Cpu size={25} strokeWidth={1.5} />
          )}
          <span>{e.inflight}</span>
        </div>
        <button
          className="engine-title"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <div>
            <h2>{e.name}</h2>
            <span className="tag">
              {e.kind === "local" ? "Local" : "Cloud"}
            </span>
          </div>
          <p>
            {host}
            {e.aliases?.length ? ` · ${engineUrls(e).length} addresses` : ""}
            {e.members.length > 1 ? ` · ${e.members.length} members` : ""}
          </p>
          <span className="engine-status">
            <i className={"status-dot " + e.status} />
            {inventoryStatusLabel(e)}
            {e.models.length
              ? ` · ${e.models.length} model${e.models.length === 1 ? "" : "s"}`
              : ""}
          </span>
        </button>
        <div className="engine-controls">
          <Switch
            checked={e.enabled}
            label={`Enable ${e.name}`}
            onChange={() => void onChange({ enabled: !e.enabled })}
          />
          <button
            className="icon-button"
            onClick={onEdit}
            aria-label={`Settings for ${e.name}`}
          >
            <Settings2 size={18} />
          </button>
          <button
            className="icon-button"
            aria-label={`${open ? "Collapse" : "Expand"} ${e.name}`}
            onClick={() => setOpen(!open)}
          >
            <ChevronDown size={19} className={open ? "rotated" : ""} />
          </button>
        </div>
      </div>
      {!open && e.models.length > 0 && (
        <div className="model-preview">
          {e.models.slice(0, 3).map((m) => (
            <span key={m.id}>{m.id}</span>
          ))}
          {e.models.length > 3 && <span>+{e.models.length - 3}</span>}
        </div>
      )}
      {open && (
        <div className="engine-detail">
          <div className="engine-addresses">
            <strong>Preferred address</strong>
            <p>{e.base_url}</p>
            {!!e.aliases?.length && (
              <>
                <strong>Also known as</strong>
                {e.aliases.map((url) => (
                  <p key={url}>{url}</p>
                ))}
              </>
            )}
            <button className="text-button" onClick={onMerge}>
              Merge duplicate engine
            </button>
          </div>
          <div className="model-list">
            {e.models.map((m) => (
              <div className="model-row" key={m.id}>
                <span className="model-mark">
                  <Cpu size={14} />
                </span>
                <div>
                  <strong>{m.id}</strong>
                  <small>
                    {m.capabilities.join(" · ")}
                    {m.context_length
                      ? ` · ${Math.round(m.context_length / 1024)}K context`
                      : ""}
                  </small>
                </div>
                <span className="tag">{modelInventoryLabel(e, m.enabled)}</span>
              </div>
            ))}
            {!e.models.length && (
              <p className="hint">
                {e.error ||
                  "No current models. The router will check this endpoint again."}
              </p>
            )}
          </div>
          <div className="engine-foot">
            <span>
              {inventoryCheckLabel(e)} {timeLabel(e.checked_at)}
              {e.last_success
                ? ` · Last response ${timeLabel(e.last_success)}`
                : ""}
            </span>
            <button className="text-button" onClick={() => void onRefresh()}>
              <RefreshCw size={13} />
              Refresh
            </button>
          </div>
          <div className="engine-foot">
            <label className="drain-label">
              <Switch
                checked={e.draining}
                label={`Drain ${e.name}`}
                onChange={() => void onChange({ draining: !e.draining })}
              />
              Pause new requests
            </label>
            <button
              className="text-button danger"
              onClick={() => void onRemove()}
            >
              Remove engine
            </button>
          </div>
        </div>
      )}
    </article>
  );
}
