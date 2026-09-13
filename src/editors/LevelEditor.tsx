import { Cloud, HardDrive, Save, Trash2 } from "lucide-react";
import { useState } from "react";
import {
  Choice,
  Dialog,
  Field,
  ListInput,
  Select,
  Switch,
  toggle,
} from "../components";
import { requireUnchanged } from "../editor-state";
import { DeleteLevelDialog } from "../settings/DeleteLevelDialog";
import type { AccountLevel, Config, TokenBudget } from "../types";

export const WINDOW_PRESETS = [
  { seconds: 3600, label: "1 hour" },
  { seconds: 86400, label: "24 hours" },
  { seconds: 604800, label: "7 days" },
  { seconds: 2592000, label: "30 days" },
] as const;
const DEFAULT_BUDGET = { max_tokens: 100000, window_seconds: 86400 };

export function LevelEditor({
  initial,
  config,
  save,
  inUse,
  onClose,
}: {
  initial: AccountLevel;
  config: Config;
  save: (config: Config) => Promise<Config>;
  inUse: number;
  onClose: () => void;
}) {
  const [draft, set] = useState<AccountLevel>(initial);
  const [baseline, setBaseline] = useState(
    config.account_levels.find((l) => l.id === initial.id),
  );
  const [customWindow, setCustomWindow] = useState(
    !!initial.token_budget &&
      !WINDOW_PRESETS.some(
        (p) => p.seconds === initial.token_budget?.window_seconds,
      ),
  );
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const saved = config.account_levels.some((l) => l.id === draft.id);
  const budget = draft.token_budget;
  const setBudget = (change: Partial<TokenBudget>) =>
    set({
      ...draft,
      token_budget: { ...(budget || DEFAULT_BUDGET), ...change },
    });
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      requireUnchanged(
        baseline,
        config.account_levels.find((l) => l.id === draft.id),
        "level",
      );
      const result = await save({
        ...config,
        account_levels: [
          ...config.account_levels.filter((l) => l.id !== draft.id),
          draft,
        ],
      });
      const savedLevel = result.account_levels.find((l) => l.id === draft.id)!;
      setBaseline(savedLevel);
      set(savedLevel);
      setError("");
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  async function remove() {
    if (inUse > 0) {
      setRemoving(true);
      return;
    }
    setBusy(true);
    try {
      await save({
        ...config,
        account_levels: config.account_levels.filter((l) => l.id !== draft.id),
      });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog title={initial.name || "New level"} onClose={onClose} wide>
      <form onSubmit={submit} aria-label="Level editor">
        <p className="hint">
          A level is the whole of what an account can use. User keys grant
          exactly this access and never more.
        </p>
        <Field label="Level name">
          <input
            required
            placeholder="Name this level"
            value={draft.name}
            onChange={(e) => set({ ...draft, name: e.target.value })}
          />
        </Field>
        <Field label="Description (optional)">
          <input
            value={draft.description}
            maxLength={500}
            onChange={(e) => set({ ...draft, description: e.target.value })}
          />
        </Field>
        <section className="permission-group">
          <h3>Allowed routes</h3>
          <p className="hint">
            Choose the purposes accounts at this level can request.
          </p>
          <div className="choice-line">
            {config.routes.map((r) => (
              <Choice
                key={r.id}
                checked={draft.route_names.includes(r.name)}
                onClick={() =>
                  set({
                    ...draft,
                    route_names: toggle(draft.route_names, r.name),
                  })
                }
              >
                {r.name}
              </Choice>
            ))}
          </div>
        </section>
        <section className="permission-group">
          <h3>Allowed engines</h3>
          <Choice
            checked={!draft.engine_ids.length}
            onClick={() => set({ ...draft, engine_ids: [] })}
          >
            All engines permitted by this level's cloud and model rules
          </Choice>
          <div className="engine-choices">
            {config.engines.map((e) => (
              <Choice
                key={e.id}
                checked={draft.engine_ids.includes(e.id)}
                onClick={() =>
                  set({ ...draft, engine_ids: toggle(draft.engine_ids, e.id) })
                }
              >
                {e.kind === "cloud" ? (
                  <Cloud size={15} />
                ) : (
                  <HardDrive size={15} />
                )}{" "}
                {e.name}
              </Choice>
            ))}
          </div>
        </section>
        <Field
          label="Allowed models"
          hint="Applies to direct model requests and every route, including backups. Use * to follow future model changes."
        >
          <ListInput
            aria-label="Allowed models"
            values={draft.model_patterns}
            onValues={(values) => set({ ...draft, model_patterns: values })}
          />
        </Field>
        <div className="setting-row">
          <div>
            <strong>Allow cloud models</strong>
            <p>
              Permit requests at this level to use configured cloud providers.
            </p>
          </div>
          <Switch
            label="Allow cloud models"
            checked={draft.allow_cloud}
            onChange={() => set({ ...draft, allow_cloud: !draft.allow_cloud })}
          />
        </div>
        <div className="setting-row">
          <div>
            <strong>Allow direct model IDs</strong>
            <p>
              Let accounts request specific permitted models without using a
              named route. Engine and model allowlists still apply.
            </p>
          </div>
          <Switch
            label="Allow direct model IDs"
            checked={draft.allow_direct_models}
            onChange={() =>
              set({ ...draft, allow_direct_models: !draft.allow_direct_models })
            }
          />
        </div>
        <section className="permission-group">
          <h3>Limits</h3>
          <p className="hint">
            Limits apply to every request from accounts at this level and are
            checked before anything is sent to an engine.
          </p>
          <Field
            label="Token budget"
            hint="Budgets are enforced most precisely when callers send max_tokens or the level has a concurrency limit."
          >
            <div className="choice-line">
              <Choice
                checked={!budget}
                onClick={() => set({ ...draft, token_budget: null })}
              >
                Unlimited
              </Choice>
              <Choice checked={!!budget} onClick={() => setBudget({})}>
                Budget per window
              </Choice>
            </div>
          </Field>
          {budget && (
            <div className="field-pair">
              <Field label="Tokens per window">
                <input
                  type="number"
                  min="1"
                  aria-label="Tokens per window"
                  value={budget.max_tokens}
                  onChange={(e) =>
                    setBudget({ max_tokens: Number(e.target.value) })
                  }
                />
              </Field>
              <Field label="Window">
                <Select
                  label="Window"
                  value={
                    customWindow ? "custom" : String(budget.window_seconds)
                  }
                  onChange={(value) => {
                    if (value === "custom") setCustomWindow(true);
                    else {
                      setCustomWindow(false);
                      setBudget({ window_seconds: Number(value) });
                    }
                  }}
                >
                  {WINDOW_PRESETS.map((p) => (
                    <option key={p.seconds} value={p.seconds}>
                      {p.label}
                    </option>
                  ))}
                  <option value="custom">Custom seconds</option>
                </Select>
              </Field>
              {customWindow && (
                <Field label="Window seconds">
                  <input
                    type="number"
                    min="60"
                    aria-label="Window seconds"
                    value={budget.window_seconds}
                    onChange={(e) =>
                      setBudget({ window_seconds: Number(e.target.value) })
                    }
                  />
                </Field>
              )}
            </div>
          )}
          <Field label="Max concurrent requests">
            <div className="choice-line">
              <Choice
                checked={draft.max_concurrency === null}
                onClick={() => set({ ...draft, max_concurrency: null })}
              >
                Unlimited
              </Choice>
              <Choice
                checked={draft.max_concurrency !== null}
                onClick={() => set({ ...draft, max_concurrency: 2 })}
              >
                Limit
              </Choice>
            </div>
          </Field>
          {draft.max_concurrency !== null && (
            <Field label="Concurrent requests">
              <input
                type="number"
                min="1"
                aria-label="Concurrent requests"
                value={draft.max_concurrency}
                onChange={(e) =>
                  set({ ...draft, max_concurrency: Number(e.target.value) })
                }
              />
            </Field>
          )}
        </section>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <footer>
          {saved ? (
            <button
              type="button"
              className="danger text-button"
              onClick={remove}
              disabled={busy}
            >
              <Trash2 size={15} />
              Remove level
            </button>
          ) : (
            <button type="button" onClick={onClose}>
              Cancel
            </button>
          )}
          <button className="primary" disabled={busy}>
            <Save size={15} />
            {busy ? "Saving…" : "Save level"}
          </button>
        </footer>
      </form>
      {removing && (
        <DeleteLevelDialog
          level={initial}
          config={config}
          save={save}
          onClose={() => setRemoving(false)}
          onDeleted={onClose}
        />
      )}
    </Dialog>
  );
}
