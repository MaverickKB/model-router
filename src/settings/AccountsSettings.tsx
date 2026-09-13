import { Check, Plus, Save } from "lucide-react";
import { useState } from "react";
import { Field, Switch } from "../components";
import { requireUnchanged, sameRecord } from "../editor-state";
import { LevelEditor } from "../editors/LevelEditor";
import { newLevel } from "../editors/defaults";
import type { AccountLevel, Config } from "../types";
import { useAccounts } from "../useAccounts";
import { AccountsTable } from "./AccountsTable";
import { DevicesTable } from "./DevicesTable";
import { SavedAccounts } from "./SettingsSummary";

type Props = {
  config: Config;
  save: (config: Config) => Promise<Config>;
  baseUrl: string;
  levelsInUse?: Record<string, number>;
};

function budgetLabel(level: AccountLevel) {
  const parts = [
    level.token_budget
      ? `${level.token_budget.max_tokens.toLocaleString()} tokens / ${level.token_budget.window_seconds} s`
      : "Unlimited tokens",
    level.max_concurrency === null
      ? "unlimited concurrency"
      : `${level.max_concurrency} concurrent`,
  ];
  return parts.join(" · ");
}

function LevelsList({
  config,
  inUse,
  onEdit,
}: {
  config: Config;
  inUse: (level: AccountLevel) => number;
  onEdit: (level: AccountLevel) => void;
}) {
  return (
    <section aria-labelledby="levels-heading">
      <div className="section-row">
        <div>
          <h3 id="levels-heading">Levels</h3>
          <p className="hint">
            A level names the routes, engines, models and limits its accounts
            receive. User keys never grant more than their level.
          </p>
        </div>
        <button type="button" onClick={() => onEdit(newLevel())}>
          <Plus size={15} />
          Add level
        </button>
      </div>
      {config.account_levels.length ? (
        <div className="account-rows">
          {config.account_levels.map((level) => (
            <button
              type="button"
              key={level.id}
              className="account-row"
              aria-label={`Edit level ${level.name}`}
              onClick={() => onEdit(level)}
            >
              <span>
                <strong>{level.name}</strong>
                <small>
                  {level.description ? `${level.description} · ` : ""}
                  Routes: {level.route_names.join(", ") || "none"} ·{" "}
                  {budgetLabel(level)}
                </small>
              </span>
              <small>
                {inUse(level)} {inUse(level) === 1 ? "account" : "accounts"}
              </small>
            </button>
          ))}
        </div>
      ) : (
        <p className="hint">No levels yet. Add one before creating accounts.</p>
      )}
    </section>
  );
}

export function AccountsSettings({
  config,
  save,
  baseUrl,
  levelsInUse = {},
}: Props) {
  const [draft, setDraft] = useState(config.accounts);
  const [baseline, setBaseline] = useState(config.accounts);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [level, setLevel] = useState<AccountLevel | null>(null);
  const { accounts, reload, error: accountsError } = useAccounts();
  const changed = !sameRecord(baseline, draft);
  const inUse = (value: AccountLevel) =>
    levelsInUse[value.id] ??
    accounts.filter((a) => a.level_id === value.id).length;
  return (
    <section aria-labelledby="accounts-heading" className="accounts-settings">
      <SavedAccounts config={config} baseUrl={baseUrl} accounts={accounts} />
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
            requireUnchanged(baseline, config.accounts, "account settings");
            const result = await save({ ...config, accounts: { ...draft } });
            setDraft(result.accounts);
            setBaseline(result.accounts);
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
            <strong>User accounts</strong>
            <p>
              People sign in at /portal, create their own API keys and see their
              usage. Keys grant only their level's access.
            </p>
          </div>
          <Switch
            label="User accounts"
            checked={draft.enabled}
            onChange={() => setDraft({ ...draft, enabled: !draft.enabled })}
          />
        </div>
        {draft.enabled && (
          <div className="setting-row">
            <div>
              <strong>Allow device pre-registration (dev mode)</strong>
              <p>
                Less secure. A registered source address can use key-gated
                routes without an API key, limited to that account's level.
                Direct connection address only — forwarding headers are ignored.
                Leave off unless you need it.
              </p>
            </div>
            <Switch
              label="Allow device pre-registration (dev mode)"
              checked={draft.device_registration_enabled}
              onChange={() =>
                setDraft({
                  ...draft,
                  device_registration_enabled:
                    !draft.device_registration_enabled,
                })
              }
            />
          </div>
        )}
        <details className="advanced">
          <summary>Portal sessions</summary>
          <Field label="Keep portal browsers signed in (hours)">
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
              Account settings saved
            </span>
          )}
          <button className="primary" disabled={busy}>
            <Save size={15} />
            {busy ? "Saving…" : "Save account settings"}
          </button>
        </footer>
      </form>
      <LevelsList config={config} inUse={inUse} onEdit={setLevel} />
      <AccountsTable
        config={config}
        accounts={accounts}
        reload={reload}
        error={accountsError}
      />
      <DevicesTable config={config} accounts={accounts} reload={reload} />
      {level && (
        <LevelEditor
          key={level.id}
          initial={level}
          config={config}
          save={save}
          inUse={inUse(level)}
          onClose={() => {
            setLevel(null);
            void reload();
          }}
        />
      )}
    </section>
  );
}
