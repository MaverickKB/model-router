import { Cloud, HardDrive, Plus, SlidersHorizontal, X } from "lucide-react";
import { useState } from "react";
import { api, post } from "../api";
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
import type { Config, Engine, Model, State } from "../types";
import { engineUrls } from "../engine-addresses";

type SaveConfig = (config: Config) => Promise<Config>;
export function EngineEditor({
  initial,
  models,
  config,
  save,
  onClose,
}: {
  initial: Engine;
  models: Model[];
  config: Config;
  save: SaveConfig;
  onClose: () => void;
}) {
  const [draft, set] = useState(initial),
    [key, setKey] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const exists = config.engines.some((e) => e.id === draft.id);
  const [baseline, setBaseline] = useState(
    config.engines.find((e) => e.id === initial.id),
  );
  const [selectingModels, setSelectingModels] = useState(
    initial.kind === "cloud" &&
      initial.model_patterns.length === 0 &&
      models.length > 0,
  );
  const [catalog, setCatalog] = useState(models);
  const [modelSearch, setModelSearch] = useState("");
  const [mappings, setMappings] = useState(
    Object.entries(initial.value_mappings).flatMap(([field, values]) =>
      Object.entries(values).map(([from, to]) => ({ field, from, to })),
    ),
  );
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const value_mappings: Engine["value_mappings"] = {};
      for (const row of mappings) {
        if (row.field && row.from)
          (value_mappings[row.field] ??= {})[row.from] = row.to;
      }
      requireUnchanged(
        baseline,
        config.engines.find((e) => e.id === draft.id),
        "engine",
      );
      const saved = await save({
        ...config,
        engines: [
          ...config.engines.filter((e) => e.id !== draft.id),
          {
            ...draft,
            value_mappings,
            name_source:
              baseline && draft.name !== baseline.name
                ? "operator"
                : draft.name_source,
          },
        ],
      });
      const savedEngine = saved.engines.find((e) => e.id === draft.id)!;
      setBaseline(savedEngine);
      set(savedEngine);
      if (key)
        await api(`/api/v1/engines/${draft.id}/credential`, {
          method: "PUT",
          body: JSON.stringify({ key, type: draft.credential_type }),
        });
      await post(`/api/v1/engines/${draft.id}/refresh`);
      if (draft.kind === "cloud" && draft.model_patterns.length === 0) {
        const latest = await api<State>("/api/v1/state");
        setCatalog(latest.engines.find((e) => e.id === draft.id)?.models || []);
        setSelectingModels(true);
        setKey("");
      } else {
        onClose();
      }
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setBusy(false);
    }
  }
  if (selectingModels)
    return (
      <Dialog title="Choose provider models" onClose={onClose}>
        <form onSubmit={submit}>
          <p className="hint">
            Choose the models this provider may serve. The catalog refreshes
            automatically; only matching models become routing candidates.
          </p>
          <Field label="Search the live catalog">
            <input
              value={modelSearch}
              onChange={(e) => setModelSearch(e.target.value)}
              placeholder="Find a model"
            />
          </Field>
          <div className="provider-models">
            {catalog
              .filter((m) =>
                m.id.toLowerCase().includes(modelSearch.toLowerCase()),
              )
              .map((m) => (
                <Choice
                  key={m.id}
                  checked={draft.model_patterns.includes(m.id)}
                  onClick={() =>
                    set({
                      ...draft,
                      model_patterns: toggle(draft.model_patterns, m.id),
                    })
                  }
                >
                  {m.id}
                </Choice>
              ))}
          </div>
          {!catalog.length && (
            <p className="hint">
              The endpoint has not returned a usable catalog. Check its address
              and provider key in engine settings.
            </p>
          )}
          <Field
            label="Model names or patterns"
            hint="A pattern such as family-* can follow future versions. Use * only when every advertised model is suitable for chat."
          >
            <ListInput
              values={draft.model_patterns}
              onValues={(model_patterns) => set({ ...draft, model_patterns })}
            />
          </Field>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <footer>
            <button
              type="button"
              className="subtle"
              onClick={() => setSelectingModels(false)}
            >
              Engine settings
            </button>
            <button
              className="primary"
              disabled={busy || !draft.model_patterns.length}
            >
              {busy ? "Saving…" : "Use selected models"}
            </button>
          </footer>
        </form>
      </Dialog>
    );
  return (
    <Dialog
      title={exists ? "Engine settings" : "Connect an engine"}
      onClose={onClose}
    >
      <form onSubmit={submit}>
        <div className="segmented">
          <button
            type="button"
            className={draft.kind === "local" ? "active" : ""}
            onClick={() =>
              draft.kind !== "local" &&
              set({
                ...draft,
                kind: "local",
                model_patterns: ["*"],
                unsupported_parameters: [],
              })
            }
          >
            <HardDrive size={17} />
            Local engine
          </button>
          <button
            type="button"
            className={draft.kind === "cloud" ? "active" : ""}
            onClick={() =>
              draft.kind !== "cloud" &&
              set({
                ...draft,
                kind: "cloud",
                model_patterns: [],
                unsupported_parameters: ["chat_template_kwargs"],
              })
            }
          >
            <Cloud size={17} />
            Cloud provider
          </button>
        </div>
        <Field label="Name">
          <input
            autoFocus
            required
            placeholder={
              draft.kind === "local" ? "Name this engine" : "Name this provider"
            }
            value={draft.name}
            onChange={(e) => set({ ...draft, name: e.target.value })}
          />
        </Field>
        <Field
          label="Preferred URL"
          hint="Use the API's OpenAI-compatible base URL. Changing it keeps the previous saved address as an alias."
        >
          <input
            type="url"
            required
            placeholder="https://provider.example/v1"
            value={draft.base_url}
            onChange={(e) =>
              set({
                ...draft,
                base_url: e.target.value,
                aliases: [
                  ...new Set([
                    ...(draft.aliases || []),
                    ...(baseline && draft.base_url === baseline.base_url
                      ? [baseline.base_url]
                      : []),
                  ]),
                ].filter((url) => url !== e.target.value),
              })
            }
          />
        </Field>
        {!!draft.aliases?.length && (
          <Field
            label="Choose a saved address"
            hint="The preferred address receives requests. Switching keeps the other addresses as aliases."
          >
            <Select
              value={draft.base_url}
              onChange={(base_url) =>
                set({
                  ...draft,
                  base_url,
                  aliases: engineUrls(draft).filter((url) => url !== base_url),
                })
              }
            >
              {engineUrls(draft).map((url) => (
                <option key={url}>{url}</option>
              ))}
            </Select>
          </Field>
        )}
        <Field
          label="Other addresses for this API"
          hint="Saved aliases prevent duplicate engines on rediscovery. Include only URLs serving this same complete API."
        >
          <ListInput
            values={draft.aliases || []}
            onValues={(aliases) => set({ ...draft, aliases })}
            placeholder="Additional LAN, VPN or DNS URLs"
          />
        </Field>
        <Field
          label={
            exists
              ? "Replace provider credential (optional)"
              : "Provider credential (if required)"
          }
          hint="Stored on the router. Never shared with callers."
        >
          <Select
            value={draft.credential_type}
            onChange={(value) =>
              set({ ...draft, credential_type: value as Engine["credential_type"] })
            }
          >
            <option value="static">API key</option>
            <option value="xai_oauth">xAI / Grok OAuth</option>
            <option value="codex_oauth">Codex OAuth</option>
          </Select>
          <input
            type="password"
            autoComplete="new-password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={
              draft.credential_type === "static"
                ? exists
                  ? "Leave blank to keep the saved key"
                  : "Provider API key"
                : "OAuth refresh token"
            }
          />
        </Field>
        <details className="advanced">
          <summary>
            <SlidersHorizontal size={15} />
            Engine capabilities and limits
          </summary>
          <p className="hint">
            Declare capabilities the engine supports when its catalog does not
            report them.
          </p>
          <Field label="Catalog protocol">
            <select
              value={draft.catalog_protocol}
              onChange={(e) =>
                set({
                  ...draft,
                  catalog_protocol: e.target
                    .value as Engine["catalog_protocol"],
                })
              }
            >
              <option value="openai">OpenAI compatible</option>
              <option value="ollama">Ollama native catalog</option>
            </select>
          </Field>
          <div className="choice-line">
            {["text", "tools", "vision", "streaming"].map((cap) => (
              <Choice
                key={cap}
                checked={draft.capabilities.includes(cap)}
                onClick={() =>
                  set({
                    ...draft,
                    capabilities: toggle(draft.capabilities, cap),
                  })
                }
              >
                {cap}
              </Choice>
            ))}
          </div>
          <div className="field-pair">
            <Field label="Concurrent requests">
              <input
                type="number"
                min="1"
                value={draft.max_inflight}
                onChange={(e) =>
                  set({ ...draft, max_inflight: Number(e.target.value) })
                }
              />
            </Field>
            <Field label="Response timeout (seconds)">
              <input
                type="number"
                min="1"
                value={draft.timeout_seconds}
                onChange={(e) =>
                  set({ ...draft, timeout_seconds: Number(e.target.value) })
                }
              />
            </Field>
          </div>
          <Field label="Maximum response size (MiB)">
            <input
              type="number"
              min="1"
              max="512"
              value={draft.max_response_bytes / 1048576}
              onChange={(e) =>
                set({
                  ...draft,
                  max_response_bytes: Number(e.target.value) * 1048576,
                })
              }
            />
          </Field>
          <div className="field-pair">
            <Field label="Failure cooldown (seconds)">
              <input
                type="number"
                min="0"
                max="300"
                value={draft.failure_cooldown_seconds}
                onChange={(e) =>
                  set({
                    ...draft,
                    failure_cooldown_seconds: Number(e.target.value),
                  })
                }
              />
            </Field>
            <Field label="Rate limit cooldown (seconds)">
              <input
                type="number"
                min="0"
                max="300"
                value={draft.rate_limit_cooldown_seconds}
                onChange={(e) =>
                  set({
                    ...draft,
                    rate_limit_cooldown_seconds: Number(e.target.value),
                  })
                }
              />
            </Field>
          </div>

          <Field
            label="Tags"
            hint="Use tags to select changing groups of engines in a route."
          >
            <ListInput
              values={draft.tags}
              onValues={(values) => set({ ...draft, tags: values })}
            />
          </Field>
          <Field
            label="Member hosts"
            hint="Optional. A distributed engine remains one serving endpoint."
          >
            <ListInput
              values={draft.members}
              onValues={(values) => set({ ...draft, members: values })}
            />
          </Field>
        </details>
        {models.length > 0 && (
          <details className="advanced">
            <summary>Individual models</summary>
            <p className="hint">
              Override a model's capabilities or exclude it from routing.
            </p>
            {models.map((m) => {
              const settings = draft.model_settings[m.id] || {
                enabled: true,
                capabilities: m.capabilities,
                context_length: m.context_length,
              };
              return (
                <div className="model-override" key={m.id}>
                  <div>
                    <strong>{m.id}</strong>
                    <Switch
                      label={`Allow ${m.id}`}
                      checked={settings.enabled}
                      onChange={() =>
                        set({
                          ...draft,
                          model_settings: {
                            ...draft.model_settings,
                            [m.id]: { ...settings, enabled: !settings.enabled },
                          },
                        })
                      }
                    />
                  </div>
                  <div className="choice-line">
                    {["text", "tools", "vision", "streaming"].map((cap) => (
                      <Choice
                        key={cap}
                        checked={settings.capabilities.includes(cap)}
                        onClick={() =>
                          set({
                            ...draft,
                            model_settings: {
                              ...draft.model_settings,
                              [m.id]: {
                                ...settings,
                                capabilities: toggle(
                                  settings.capabilities,
                                  cap,
                                ),
                              },
                            },
                          })
                        }
                      >
                        {cap}
                      </Choice>
                    ))}
                  </div>
                </div>
              );
            })}
          </details>
        )}
        {draft.kind === "cloud" && (
          <section className="permission-group">
            <h3>Exposed models</h3>
            <p className="hint">
              {draft.model_patterns.length
                ? draft.model_patterns.join(", ")
                : "Choose models after connecting this provider."}
            </p>
            {models.length > 0 && (
              <button
                type="button"
                onClick={() => {
                  setCatalog(models);
                  setSelectingModels(true);
                }}
              >
                Choose models
              </button>
            )}
          </section>
        )}
        <details className="advanced">
          <summary>Unsupported request options</summary>
          <Field
            label="Options this engine does not accept"
            hint="Explicit caller options on this list exclude this engine. Optional route defaults on this list are skipped and recorded in the decision."
          >
            <ListInput
              values={draft.unsupported_parameters}
              onValues={(unsupported_parameters) =>
                set({ ...draft, unsupported_parameters })
              }
            />
          </Field>
        </details>
        <details className="advanced">
          <summary>Request value translations</summary>
          <p className="hint">
            Translate a caller's option value to the value this engine accepts.
          </p>
          {mappings.map((row, i) => (
            <div className="mapping-row" key={i}>
              {(["field", "from", "to"] as const).map((k) => (
                <input
                  aria-label={`Translation ${i + 1} ${k}`}
                  key={k}
                  placeholder={
                    k === "field"
                      ? "Option"
                      : k === "from"
                        ? "Caller value"
                        : "Engine value"
                  }
                  value={row[k]}
                  onChange={(e) =>
                    setMappings(
                      mappings.map((r, n) =>
                        n === i ? { ...r, [k]: e.target.value } : r,
                      ),
                    )
                  }
                />
              ))}
              <button
                type="button"
                className="icon-button"
                aria-label={`Remove translation ${i + 1}`}
                onClick={() => setMappings(mappings.filter((_, n) => n !== i))}
              >
                <X size={14} />
              </button>
            </div>
          ))}
          <button
            className="text-button"
            type="button"
            onClick={() =>
              setMappings([...mappings, { field: "", from: "", to: "" }])
            }
          >
            <Plus size={14} />
            Add translation
          </button>
        </details>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <footer>
          <button type="button" className="subtle" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy}>
            {busy ? "Connecting…" : exists ? "Save changes" : "Connect engine"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
