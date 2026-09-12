import {
  ArrowDown,
  Plus,
  Save,
  SlidersHorizontal,
  Trash2,
  X,
} from "lucide-react";
import { useState } from "react";
import { post } from "../api";
import {
  Choice,
  Field,
  ListInput,
  Select,
  Switch,
  toggle,
} from "../components";
import { requireUnchanged, sameRecord } from "../editor-state";
import type { Config, Decision, EngineView, Route, Selector } from "../types";

import { DecisionView } from "./DecisionView";
type SaveConfig = (config: Config) => Promise<Config>;
function Selection({
  value,
  onChange,
  engines,
  label,
}: {
  value: Selector;
  onChange: (s: Selector) => void;
  engines: EngineView[];
  label: string;
}) {
  const eligible = engines.filter(
    (e) => value.kind === "any" || e.kind === value.kind,
  );
  return (
    <div className="policy-stage">
      <div className="stage-title">
        <h3>{label}</h3>
        <Select
          label={`${label} location`}
          value={value.kind}
          onChange={(kind) =>
            onChange({
              ...value,
              kind: kind as Selector["kind"],
              engine_ids: [],
            })
          }
        >
          <option value="local">Local engines</option>
          <option value="cloud">Cloud providers</option>
          <option value="any">Local and cloud</option>
        </Select>
      </div>
      <Choice
        checked={!value.engine_ids.length}
        onClick={() => onChange({ ...value, engine_ids: [] })}
      >
        Any matching engine, including newly discovered ones
      </Choice>
      <div className="engine-choices">
        {eligible.map((e) => (
          <Choice
            key={e.id}
            checked={value.engine_ids.includes(e.id)}
            onClick={() =>
              onChange({ ...value, engine_ids: toggle(value.engine_ids, e.id) })
            }
          >
            <span className={`status-dot ${e.status}`} />
            {e.name}
          </Choice>
        ))}
      </div>
      {!eligible.length && (
        <p className="hint">
          Connect a{" "}
          {value.kind === "cloud" ? "cloud provider" : "matching engine"} in
          Overview. This policy will wait for an eligible model.
        </p>
      )}
      <Field
        label="Model selection"
        hint="* follows the live catalog as models change. Use model names or patterns to narrow this purpose."
      >
        <ListInput
          aria-label={`${label} model selection`}
          values={value.model_patterns}
          onValues={(values) => onChange({ ...value, model_patterns: values })}
        />
      </Field>
      <details className="advanced">
        <summary>Match engine tags</summary>
        <ListInput
          aria-label={`${label} tags`}
          placeholder="Optional tags, separated by commas"
          values={value.tags}
          onValues={(values) => onChange({ ...value, tags: values })}
        />
      </details>
    </div>
  );
}

export function RouteEditor({
  initial,
  config,
  engines,
  save,
  onDelete,
  onClose,
}: {
  initial: Route;
  config: Config;
  engines: EngineView[];
  save: SaveConfig;
  onDelete: () => Promise<void>;
  onClose: () => void;
}) {
  const [draft, set] = useState(initial),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [client, setClient] = useState(config.clients[0]?.id || ""),
    [decision, setDecision] = useState<Decision | null>(null),
    [trial, setTrial] = useState(""),
    [trying, setTrying] = useState(false);
  const [baseline, setBaseline] = useState(
    config.routes.find((r) => r.id === initial.id),
  );
  const changed = !sameRecord(baseline, draft);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      requireUnchanged(
        baseline,
        config.routes.find((r) => r.id === draft.id),
        "route",
      );
      const saved = await save({
        ...config,
        routes: [...config.routes.filter((r) => r.id !== draft.id), draft],
      });
      const savedRoute = saved.routes.find((r) => r.id === draft.id)!;
      setBaseline(savedRoute);
      set(savedRoute);
      setError("");
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="detail-editor" onSubmit={submit}>
      <div className="detail-heading">
        <div>
          <p className="eyebrow">Route policy</p>
          <h2>{initial.name || "New route"}</h2>
        </div>
        <div className="actions">
          <Switch
            checked={draft.enabled}
            onChange={() => set({ ...draft, enabled: !draft.enabled })}
            label="Enable route"
          />
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Back to routes"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      <div className="field-pair">
        <Field label="Route name" hint="The stable name your caller uses.">
          <input
            required
            value={draft.name}
            placeholder="e.g. research"
            onChange={(e) => set({ ...draft, name: e.target.value })}
          />
        </Field>
        <Field label="Purpose">
          <input
            placeholder="What this route is for"
            value={draft.purpose}
            onChange={(e) => set({ ...draft, purpose: e.target.value })}
          />
        </Field>
      </div>
      <div className="setting-row">
        <div>
          <strong>Caller key for this route</strong>
          <p>
            {draft.require_caller_key
              ? "Only callers presenting a valid key can request this route."
              : "Callers without a key may request this route when their other permissions allow it."}
          </p>
        </div>
        <Switch
          label="Require a caller key for this route"
          checked={draft.require_caller_key}
          onChange={() =>
            set({
              ...draft,
              require_caller_key: !draft.require_caller_key,
            })
          }
        />
      </div>
      <Selection
        label="Primary"
        value={draft.primary}
        onChange={(primary) => set({ ...draft, primary })}
        engines={engines}
      />
      <div className="stage-connection">
        <ArrowDown size={17} />
        <span>If the primary cannot serve the request</span>
      </div>
      {draft.fallback ? (
        <>
          <Selection
            label="Backup"
            value={draft.fallback}
            onChange={(fallback) => set({ ...draft, fallback })}
            engines={engines}
          />
          <button
            type="button"
            className="text-button"
            onClick={() => set({ ...draft, fallback: null })}
          >
            Remove backup
          </button>
        </>
      ) : (
        <button
          type="button"
          className="add-backup"
          onClick={() =>
            set({
              ...draft,
              fallback: {
                kind: "cloud",
                engine_ids: [],
                model_patterns: ["*"],
                tags: [],
              },
            })
          }
        >
          <Plus size={17} />
          <span>
            Add a backup
            <small>
              Used only when primary candidates fail or are unavailable.
            </small>
          </span>
        </button>
      )}
      <Field label="When several models qualify">
        <Select
          value={draft.strategy}
          onChange={(strategy) =>
            set({ ...draft, strategy: strategy as Route["strategy"] })
          }
        >
          <option value="least_busy">Choose the least busy engine</option>
          <option value="ordered">Follow the selected engine order</option>
        </Select>
      </Field>
      <details className="advanced">
        <summary>
          <SlidersHorizontal size={15} />
          Request defaults
        </summary>
        <Field
          label="Thinking"
          hint="Applied only when the caller leaves the option unset."
        >
          <Select
            value={String(
              (
                draft.defaults.chat_template_kwargs as
                  { thinking?: boolean } | undefined
              )?.thinking ?? "default",
            )}
            onChange={(v) =>
              set({
                ...draft,
                defaults: {
                  ...draft.defaults,
                  chat_template_kwargs: {
                    ...((draft.defaults.chat_template_kwargs as object) || {}),
                    thinking: v === "default" ? undefined : v === "true",
                  },
                },
              })
            }
          >
            <option value="default">Engine default</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </Select>
        </Field>
      </details>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <footer className="editor-footer">
        <button type="button" className="danger text-button" onClick={onDelete}>
          <Trash2 size={15} />
          Remove route
        </button>
        <button
          className="primary"
          disabled={
            busy || (!changed && config.routes.some((r) => r.id === draft.id))
          }
        >
          <Save size={15} />
          {busy ? "Saving…" : "Save policy"}
        </button>
      </footer>
      <section className="explain">
        <h3>Where would a request go?</h3>
        <p className="hint">
          Check the saved policy against a caller's permissions and the current
          catalog.
        </p>
        <div className="inline-control">
          <Select label="Preview caller" value={client} onChange={setClient}>
            <option value="">Choose a caller</option>
            {config.clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
          <button
            type="button"
            disabled={!client || changed}
            onClick={async () => {
              try {
                setDecision(
                  await post<Decision>("/api/v1/explain", {
                    client_id: client,
                    payload: { model: draft.name },
                  }),
                );
              } catch (err) {
                setError(String(err));
              }
            }}
          >
            Show decision
          </button>
        </div>
        {decision && <DecisionView decision={decision} />}
        <div className="trial-controls">
          <button
            type="button"
            disabled={!client || changed || trying}
            onClick={async () => {
              setTrying(true);
              setTrial("");
              try {
                const result = await post<{
                  choices: {
                    message: { content?: string; reasoning_content?: string };
                  }[];
                }>("/api/v1/try-route", {
                  client_id: client,
                  route: draft.name,
                });
                setTrial(
                  result.choices?.[0]?.message?.content ||
                    result.choices?.[0]?.message?.reasoning_content ||
                    "The engine returned a response.",
                );
              } catch (err) {
                setError(String(err instanceof Error ? err.message : err));
              } finally {
                setTrying(false);
              }
            }}
          >
            {trying ? "Waiting for a response…" : "Try this route"}
          </button>
          <p className="hint">
            Sends a short greeting through this caller's saved permissions.
          </p>
          {trial && <p className="trial-result">{trial}</p>}
        </div>
      </section>
    </form>
  );
}
