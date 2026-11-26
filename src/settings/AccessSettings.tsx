import { Check, KeyRound, Save } from "lucide-react";
import { useState } from "react";
import { post } from "../api";
import { Field, ListInput, Select, Switch } from "../components";
import { requireUnchanged, sameRecord } from "../editor-state";
import type { Config } from "../types";
import { SavedAccess } from "./SettingsSummary";

type Props = {
  config: Config;
  operatorUrl: string | null;
  save: (config: Config) => Promise<Config>;
  onSignOut: () => void;
};

export function AccessSettings({
  config,
  operatorUrl,
  save,
  onSignOut,
}: Props) {
  const [draft, setDraft] = useState(config.security);
  const [baseline, setBaseline] = useState(config.security);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [newKey, setNewKey] = useState("");
  const changed = !sameRecord(baseline, draft);
  return (
    <section aria-labelledby="access-heading" className="access-settings">
      <SavedAccess config={config} operatorUrl={operatorUrl} />
      {changed && (
        <p className="hint" role="status">
          Changes below are not active until saved.
        </p>
      )}
      <form
        className="settings-form"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            requireUnchanged(
              baseline,
              config.security,
              "access settings",
            );
            const security = { ...draft };
            const result = await save({
              ...config,
              security,
            });
            setDraft(result.security);
            setBaseline(result.security);
            setSaved(true);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="setting-row">
          <div>
            <strong>Operator sign-in</strong>
            <p>
              {draft.operator_auth_enabled
                ? "Sign in once to manage the router. This browser stays signed in across restarts."
                : "Trusted addresses can manage the router without signing in."}
            </p>
          </div>
          <Switch
            label="Operator sign-in"
            checked={draft.operator_auth_enabled}
            onChange={() =>
              setDraft({
                ...draft,
                operator_auth_enabled: !draft.operator_auth_enabled,
              })
            }
          />
        </div>
        {!draft.operator_auth_enabled && (
          <Field
            label="Trusted operator networks"
            hint="Only these source addresses can open Settings without a key. Loopback includes your SSH tunnel."
          >
            <ListInput
              values={draft.operator_networks}
              onValues={(operator_networks) =>
                setDraft({ ...draft, operator_networks })
              }
            />
          </Field>
        )}
        <Field
          label="Default unkeyed policy (optional)"
          hint="This policy is used when a caller has no key and no source-specific override. Route settings still decide whether each route accepts an unkeyed request."
        >
          <Select
            value={draft.anonymous_client_id || ""}
            onChange={(anonymous_client_id) =>
              setDraft({
                ...draft,
                anonymous_client_id: anonymous_client_id || null,
              })
            }
          >
            <option value="">No default policy</option>
            {config.clients
              .filter((c) => c.enabled)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
          </Select>
        </Field>
        <details className="advanced">
          <summary>Sessions</summary>
          <Field label="Keep this browser signed in (hours)">
            <input
              type="number"
              min="1"
              max="720"
              value={draft.session_hours}
              onChange={(e) =>
                setDraft({ ...draft, session_hours: Number(e.target.value) })
              }
            />
          </Field>
        </details>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <footer>
          {saved && !changed && (
            <span className="saved" role="status">
              <Check size={15} />
              Access settings saved
            </span>
          )}
          <button className="primary" disabled={busy}>
            <Save size={15} />
            {busy ? "Saving…" : "Save access settings"}
          </button>
        </footer>
      </form>
      <details className="advanced">
        <summary>Operator key</summary>
        <p className="hint">
          Replace the key used to sign into Settings. Agent keys are
          independent. Other operator sessions will need to sign in again; this
          browser stays connected.
        </p>
        <button
          type="button"
          onClick={async () => {
            try {
              const result = await post<{ key: string }>(
                "/api/v1/operator/key",
                {},
              );
              setNewKey(result.key);
              setError("");
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          <KeyRound size={15} />
          Generate replacement operator key
        </button>
        {newKey && (
          <Field
            label="New operator key"
            hint="Save this key now. It is displayed only here."
          >
            <input readOnly value={newKey} onFocus={(e) => e.target.select()} />
          </Field>
        )}
      </details>
      {config.security.operator_auth_enabled && (
        <button className="text-button" type="button" onClick={onSignOut}>
          Sign out of this browser
        </button>
      )}
    </section>
  );
}
