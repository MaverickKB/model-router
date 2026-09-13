import { useState } from "react";
import { post } from "../api";
import { Dialog, Field, Select } from "../components";
import type { AccountSummary, ActivationLink, Config } from "../types";
import { ActivationDialog } from "./ActivationDialog";

// Creating an account issues its activation link in the same response; the
// success view hands that link over exactly once.
export function CreateAccountDialog({
  config,
  onCreated,
  onClose,
}: {
  config: Config;
  onCreated: () => Promise<void> | void;
  onClose: () => void;
}) {
  const levels = config.account_levels;
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [levelId, setLevelId] = useState(levels[0]?.id || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{
    account: AccountSummary;
    activation: ActivationLink;
  } | null>(null);
  if (result)
    return (
      <ActivationDialog
        link={result.activation}
        purpose="activate"
        accountName={result.account.name}
        accountsEnabled={config.accounts.enabled}
        onClose={onClose}
      />
    );
  return (
    <Dialog title="Add account" onClose={onClose}>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            const value = await post<{
              account: AccountSummary;
              activation: ActivationLink;
            }>("/api/v1/accounts", {
              username: username.trim().toLowerCase(),
              name: name.trim() || null,
              level_id: levelId,
            });
            await onCreated();
            setResult(value);
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <p className="hint">
          The person receives a one-time activation link, sets their own
          password in the portal and then creates their own API keys.
        </p>
        <Field
          label="Username"
          hint="2–64 lowercase letters, digits, dots, dashes or underscores."
        >
          <input
            required
            autoFocus
            autoComplete="off"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field label="Display name (optional)">
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Level">
          <Select label="Level" value={levelId} onChange={setLevelId}>
            {levels.map((level) => (
              <option key={level.id} value={level.id}>
                {level.name}
              </option>
            ))}
          </Select>
        </Field>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy || !levelId}>
            {busy ? "Creating…" : "Create account"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
