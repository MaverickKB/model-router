import { KeyRound, Link2, MonitorSmartphone, Save, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, post } from "../api";
import { Dialog, Field, Select, Switch, when } from "../components";
import type {
  AccountDetail as Detail,
  AccountSummary,
  ActivationLink,
  Config,
} from "../types";
import { ActivationDialog } from "./ActivationDialog";
import { concurrencyLabel, UsageMeter } from "./UsageMeter";

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

// One account's management surface. Every action is a request to the accounts
// API followed by a reload; nothing here touches the configuration revision.
export function AccountDetail({
  account: initial,
  config,
  onChanged,
  onClose,
}: {
  account: AccountSummary;
  config: Config;
  onChanged: () => Promise<void> | void;
  onClose: () => void;
}) {
  const base = `/api/v1/accounts/${initial.id}`;
  const [detail, setDetail] = useState<Detail | null>(null);
  const [name, setName] = useState(initial.name);
  const [levelId, setLevelId] = useState(initial.level_id);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState<ActivationLink | null>(null);
  const [confirming, setConfirming] = useState(false);
  const account = detail?.account ?? initial;
  const pending = account.status === "pending";
  const suspended = account.status === "suspended";
  const load = useCallback(async () => {
    try {
      setDetail(await api<Detail>(base));
    } catch (e) {
      setError(message(e));
    }
  }, [base]);
  useEffect(() => {
    void load();
  }, [load]);
  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
      await onChanged();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  const put = (body: unknown) =>
    api(base, { method: "PUT", body: JSON.stringify(body) });
  async function remove() {
    setBusy(true);
    try {
      await api(base, { method: "DELETE" });
      await onChanged();
      onClose();
    } catch (e) {
      setError(message(e));
      setBusy(false);
    }
  }
  const windows = detail?.usage_windows ?? [];
  return (
    <Dialog title={account.name} onClose={onClose} wide>
      <div className="dialog-body account-detail">
        <p>
          {account.username} · {account.status} · Created{" "}
          {when(account.created)} · Last sign-in {when(account.last_login)}
        </p>
        <form
          aria-label="Account details"
          onSubmit={(event) => {
            event.preventDefault();
            void run(() => put({ name: name.trim(), level_id: levelId }));
          }}
        >
          <div className="field-pair">
            <Field label="Name">
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="Level">
              <Select label="Level" value={levelId} onChange={setLevelId}>
                {!config.account_levels.some((l) => l.id === levelId) && (
                  <option value={levelId}>Level removed</option>
                )}
                {config.account_levels.map((level) => (
                  <option key={level.id} value={level.id}>
                    {level.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <footer>
            <button className="primary" disabled={busy}>
              <Save size={15} />
              Save account
            </button>
          </footer>
        </form>
        <div className="setting-row">
          <div>
            <strong>Account enabled</strong>
            <p>
              {pending
                ? "Activate the account with its link before enabling it."
                : suspended
                  ? "Suspended. Keys are refused and the portal refuses sign-in."
                  : "Suspending signs the person out of the portal and refuses their keys."}
            </p>
          </div>
          <Switch
            label="Account enabled"
            checked={account.status === "active"}
            disabled={pending || busy}
            onChange={() =>
              void run(() =>
                put({ status: suspended ? "active" : "suspended" }),
              )
            }
          />
        </div>
        <section className="permission-group">
          <h3>
            <Link2 size={16} />
            {pending ? "Activation link" : "Password reset"}
          </h3>
          <p className="hint">
            {pending
              ? account.activation_pending
                ? `A link is outstanding until ${when(account.activation_expires)}. Issuing another replaces it.`
                : "No usable link is outstanding. Issue one and hand it to the person."
              : "A reset link lets the person choose a new password and signs out their other portal sessions. API keys are untouched."}
          </p>
          <button
            type="button"
            disabled={busy || suspended}
            onClick={() =>
              void run(async () =>
                setLink(await post<ActivationLink>(`${base}/activation`)),
              )
            }
          >
            {pending ? "Issue activation link" : "Issue password-reset link"}
          </button>
          {suspended && (
            <p className="hint">Enable the account before issuing a link.</p>
          )}
        </section>
        <section className="permission-group">
          <h3>Usage</h3>
          <UsageMeter usage={account.usage} />
          {account.usage && (
            <p className="hint">{concurrencyLabel(account.usage)}</p>
          )}
          {windows.length ? (
            <div className="account-rows" aria-label="Usage windows">
              {windows.map((row) => {
                const total =
                  row.prompt_tokens +
                  row.completion_tokens +
                  row.estimated_tokens;
                return (
                  <div
                    className="account-row"
                    key={`${row.window_start}-${row.window_seconds}`}
                  >
                    <span>
                      <strong>{when(row.window_start)}</strong>
                      <small>{row.window_seconds / 3600} h window</small>
                    </span>
                    <small>
                      {row.estimated_tokens ? "≈ " : ""}
                      {total.toLocaleString()} tokens · {row.requests}{" "}
                      {row.requests === 1 ? "request" : "requests"}
                    </small>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="hint">No usage recorded in the last 7 days.</p>
          )}
        </section>
        <section className="permission-group">
          <h3>
            <KeyRound size={16} />
            API keys
          </h3>
          {detail?.keys.length ? (
            <div className="account-rows" aria-label="Account keys">
              {detail.keys.map((key) => (
                <div className="account-row" key={key.id}>
                  <span>
                    <strong>{key.name}</strong>
                    <small>
                      Created {when(key.created)} · Last used{" "}
                      {when(key.last_used)}
                    </small>
                  </span>
                  <span className="actions">
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Revoke key ${key.name}`}
                      onClick={() =>
                        void run(() =>
                          api(`${base}/keys/${key.id}`, { method: "DELETE" }),
                        )
                      }
                    >
                      Revoke
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="hint">
              No keys. The person creates keys in the portal.
            </p>
          )}
        </section>
        <section className="permission-group">
          <h3>
            <MonitorSmartphone size={16} />
            Registered devices
          </h3>
          {detail?.devices.length ? (
            <div className="account-rows" aria-label="Account devices">
              {detail.devices.map((device) => (
                <div className="account-row" key={device.id}>
                  <span
                    className={`status-dot ${device.enabled ? "available" : "disabled"}`}
                  />
                  <span>
                    <strong>
                      {device.name} · {device.address}
                    </strong>
                    <small>
                      {device.enabled ? "Enabled" : "Disabled"} · Last matched{" "}
                      {when(device.last_matched)}
                      {device.shadows?.map(
                        (s) =>
                          ` · Takes precedence over ${s.client_name} (${s.network})`,
                      )}
                    </small>
                  </span>
                  {!!device.shadows?.length && (
                    <span className="badge badge-shadow">Overrides policy</span>
                  )}
                  <span className="actions">
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`${device.enabled ? "Disable" : "Enable"} device ${device.name}`}
                      onClick={() =>
                        void run(() =>
                          api(`/api/v1/devices/${device.id}`, {
                            method: "PUT",
                            body: JSON.stringify({ enabled: !device.enabled }),
                          }),
                        )
                      }
                    >
                      {device.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Remove device ${device.name}`}
                      onClick={() =>
                        void run(() =>
                          api(`/api/v1/devices/${device.id}`, {
                            method: "DELETE",
                          }),
                        )
                      }
                    >
                      Remove
                    </button>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="hint">
              {config.accounts.device_registration_enabled
                ? "No registered devices."
                : "Device pre-registration is off; nothing to show."}
            </p>
          )}
        </section>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <footer>
          {confirming ? (
            <>
              <span className="hint">
                Remove {account.name} with its keys, sessions, devices and
                usage? This cannot be undone.
              </span>
              <button type="button" onClick={() => setConfirming(false)}>
                Keep
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => void remove()}
              >
                Remove permanently
              </button>
            </>
          ) : (
            <button
              type="button"
              className="danger text-button"
              disabled={busy}
              onClick={() => setConfirming(true)}
            >
              <Trash2 size={15} />
              Remove account
            </button>
          )}
        </footer>
      </div>
      {link && (
        <ActivationDialog
          link={link}
          purpose={pending ? "activate" : "reset"}
          accountName={account.name}
          accountsEnabled={config.accounts.enabled}
          onClose={() => setLink(null)}
        />
      )}
    </Dialog>
  );
}
