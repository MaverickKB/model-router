import {
  Check,
  Cloud,
  Copy,
  HardDrive,
  KeyRound,
  Save,
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
import { requireUnchanged } from "../editor-state";
import type { Client, Config, EngineView } from "../types";

type SaveConfig = (config: Config) => Promise<Config>;
export function ClientEditor({
  initial,
  config,
  engines,
  save,
  onDelete,
  onClose,
}: {
  initial: Client;
  config: Config;
  engines: EngineView[];
  save: SaveConfig;
  onDelete: () => Promise<void>;
  onClose: () => void;
}) {
  const { has_key: _, ...original } = initial;
  const [draft, set] = useState<Client>(original),
    [key, setKey] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [copied, setCopied] = useState(false);
  const [baseline, setBaseline] = useState(
    config.clients.find((c) => c.id === initial.id),
  );
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      requireUnchanged(
        baseline,
        config.clients.find((c) => c.id === draft.id),
        "caller",
      );
      const saved = await save({
        ...config,
        clients: [...config.clients.filter((c) => c.id !== draft.id), draft],
      });
      const savedClient = saved.clients.find((c) => c.id === draft.id)!;
      setBaseline(savedClient);
      set(savedClient);
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
          <p className="eyebrow">Caller permissions</p>
          <h2>{initial.name || "New caller"}</h2>
        </div>
        <div className="actions">
          <Switch
            checked={draft.enabled}
            onChange={() => set({ ...draft, enabled: !draft.enabled })}
            label="Enable caller"
          />
          <button
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Back to callers"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      <Field label="Caller name">
        <input
          required
          placeholder="Name this caller"
          value={draft.name}
          onChange={(e) => set({ ...draft, name: e.target.value })}
        />
      </Field>
      <Field
        label="Kind (optional)"
        hint="A label only. Shared access, agents, machines and people use this same policy."
      >
        <Select
          value={draft.kind || ""}
          onChange={(kind) =>
            set({ ...draft, kind: kind ? (kind as Client["kind"]) : null })
          }
        >
          <option value="">No label</option>
          <option value="shared">Shared</option>
          <option value="agent">Agent</option>
          <option value="machine">Machine</option>
          <option value="person">Person</option>
        </Select>
      </Field>
      <section className="permission-group">
        <h3>Allowed routes</h3>
        <p className="hint">Choose the purposes this caller can request.</p>
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
          All engines permitted by this caller's cloud and model rules
        </Choice>
        <div className="engine-choices">
          {engines.map((e) => (
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
            Permit this caller's requests to use configured cloud providers.
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
            Let this caller request specific permitted models without using a
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
        <h3>
          <KeyRound size={16} />
          Caller key
        </h3>
        <p className="hint">
          Add a key when this caller needs its own identity. Shared access stays
          available according to Settings. Replacing this caller's key revokes
          its previous key only.
        </p>
        {config.clients.some((c) => c.id === draft.id) ? (
          <button
            type="button"
            onClick={async () => {
              try {
                const result = await post<{ key: string }>(
                  `/api/v1/clients/${draft.id}/key`,
                );
                setKey(result.key);
                setCopied(false);
              } catch (err) {
                setError(String(err));
              }
            }}
          >
            {initial.has_key ? "Replace caller key" : "Create caller key"}
          </button>
        ) : (
          <p className="hint">Save this caller to create its key.</p>
        )}
        {key && (
          <div className="key-result">
            <p>Copy this key now. It is shown once.</p>
            <code>{key}</code>
            <button
              type="button"
              onClick={async () => {
                await navigator.clipboard.writeText(key);
                setCopied(true);
              }}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}{" "}
              {copied ? "Copied" : "Copy key"}
            </button>
          </div>
        )}
      </section>
      <details className="advanced">
        <summary>Shared network access</summary>
        <p className="hint">
          Requests without a key can share this policy by source address. A key
          assigned to this policy is also restricted to these addresses. Use
          separate caller keys for individual control.
        </p>
        <div className="setting-row">
          <div>
            <strong>Allow shared network access</strong>
            <p>
              Use this policy for matching source addresses without a key. Each
              route's caller-key requirement still applies.
            </p>
          </div>
          <Switch
            label="Allow shared network access"
            checked={draft.allow_network_auth}
            onChange={() =>
              set({ ...draft, allow_network_auth: !draft.allow_network_auth })
            }
          />
        </div>
        <Field label="Source addresses or networks">
          <ListInput
            placeholder="Optional source restriction for this caller"
            values={draft.source_networks}
            onValues={(values) => set({ ...draft, source_networks: values })}
          />
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
          Remove caller
        </button>
        <button className="primary" disabled={busy}>
          <Save size={15} />
          {busy ? "Saving…" : "Save permissions"}
        </button>
      </footer>
    </form>
  );
}
