import { Plus } from "lucide-react";
import { useState } from "react";
import type { AccountSummary, Config } from "../types";
import { AccountDetail } from "./AccountDetail";
import { CreateAccountDialog } from "./CreateAccountDialog";
import { UsageMeter } from "./UsageMeter";

const dot = (status: AccountSummary["status"]) =>
  status === "active"
    ? "available"
    : status === "pending"
      ? "waiting"
      : "disabled";

export function AccountsTable({
  config,
  accounts,
  reload,
  error,
}: {
  config: Config;
  accounts: AccountSummary[];
  reload: () => Promise<void>;
  error: string;
}) {
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = accounts.find((a) => a.id === selectedId);
  const levelName = (id: string) =>
    config.account_levels.find((l) => l.id === id)?.name || "Level removed";
  const plural = (n: number, word: string) =>
    `${n} ${word}${n === 1 ? "" : "s"}`;
  return (
    <section aria-labelledby="accounts-list-heading">
      <div className="section-row">
        <div>
          <h3 id="accounts-list-heading">Accounts</h3>
          <p className="hint">
            Each account signs in at the portal, creates its own keys and
            receives exactly its level's access. Open a row to manage it.
          </p>
        </div>
        <button
          type="button"
          disabled={!config.account_levels.length}
          onClick={() => setCreating(true)}
        >
          <Plus size={15} />
          Add account
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {accounts.length ? (
        <div className="account-rows">
          {accounts.map((account) => (
            <button
              type="button"
              key={account.id}
              className="account-row"
              aria-label={`Open account ${account.name}`}
              onClick={() => setSelectedId(account.id)}
            >
              <span className={`status-dot ${dot(account.status)}`} />
              <span>
                <strong>{account.name}</strong>
                <small>
                  {account.username} · {levelName(account.level_id)} ·{" "}
                  {plural(account.key_count, "key")} ·{" "}
                  {plural(account.device_count, "device")}
                </small>
              </span>
              {account.activation_pending && (
                <span className="badge badge-pending">Activation pending</span>
              )}
              {account.status === "suspended" && (
                <span className="badge">Suspended</span>
              )}
              <UsageMeter usage={account.usage} />
            </button>
          ))}
        </div>
      ) : (
        <p className="hint">
          {config.account_levels.length
            ? "No accounts yet."
            : "Add a level before creating accounts."}
        </p>
      )}
      {creating && (
        <CreateAccountDialog
          config={config}
          onCreated={reload}
          onClose={() => setCreating(false)}
        />
      )}
      {selected && (
        <AccountDetail
          key={selected.id}
          account={selected}
          config={config}
          onChanged={reload}
          onClose={() => setSelectedId(null)}
        />
      )}
    </section>
  );
}
