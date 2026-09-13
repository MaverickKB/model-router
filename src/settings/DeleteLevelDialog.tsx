import { useEffect, useState } from "react";
import { api } from "../api";
import { Dialog, Field, Select } from "../components";
import type { AccountLevel, AccountSummary, Config } from "../types";

// The server refuses to drop a level that accounts still reference, so the
// accounts are moved first, one PUT each, and the configuration is saved last.
export function DeleteLevelDialog({
  level,
  config,
  save,
  onClose,
  onDeleted,
}: {
  level: AccountLevel;
  config: Config;
  save: (config: Config) => Promise<Config>;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const others = config.account_levels.filter((l) => l.id !== level.id);
  const [target, setTarget] = useState(others[0]?.id || "");
  const [referencing, setReferencing] = useState<AccountSummary[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<{ accounts: AccountSummary[] }>("/api/v1/accounts")
      .then((value) =>
        setReferencing(value.accounts.filter((a) => a.level_id === level.id)),
      )
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [level.id]);
  const count = referencing?.length ?? 0;
  return (
    <Dialog title={`Remove level ${level.name}`} onClose={onClose}>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            for (const account of referencing || []) {
              await api(`/api/v1/accounts/${account.id}`, {
                method: "PUT",
                body: JSON.stringify({ level_id: target }),
              });
            }
            await save({
              ...config,
              account_levels: others,
            });
            onDeleted();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <p className="hint">
          {referencing === null
            ? "Checking which accounts use this level…"
            : `${count} account${count === 1 ? "" : "s"} use${count === 1 ? "s" : ""} this level. Each is moved to the chosen level before the level is removed; their keys then grant that level's access.`}
        </p>
        {others.length ? (
          <Field label="Move accounts to…">
            <Select
              label="Move accounts to…"
              value={target}
              onChange={setTarget}
            >
              {others.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </Select>
          </Field>
        ) : (
          <p className="hint">
            Add another level first so these accounts keep access.
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="primary"
            disabled={busy || referencing === null || !target}
          >
            {busy ? "Moving…" : "Move accounts and remove level"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
