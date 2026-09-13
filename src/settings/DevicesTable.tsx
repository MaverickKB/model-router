import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AccountSummary, Config, RegisteredDevice } from "../types";
import { when } from "./AccountDetail";

// Registered devices across every account. Rows stay manageable while either
// switch is off: they are inert then, not gone.
export function DevicesTable({
  config,
  accounts,
}: {
  config: Config;
  accounts: AccountSummary[];
}) {
  const [devices, setDevices] = useState<RegisteredDevice[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      const value = await api<{ devices?: RegisteredDevice[] }>(
        "/api/v1/devices",
      );
      setDevices(Array.isArray(value.devices) ? value.devices : []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  // Account actions (delete, device removal in the detail view) change rows.
  useEffect(() => {
    void load();
  }, [load, accounts]);
  const saved = config.accounts.device_registration_enabled;
  if (!saved && !devices.length) return null;
  const live = config.accounts.enabled && saved;
  async function change(device: RegisteredDevice, init: RequestInit) {
    setBusy(true);
    try {
      await api(`/api/v1/devices/${device.id}`, init);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section aria-labelledby="devices-heading">
      <div className="section-row">
        <div>
          <h3 id="devices-heading">Registered devices</h3>
          <p className="hint">
            {live
              ? "Dev mode is on: each address below is identified as its account without an API key."
              : "Registered devices are inert while device pre-registration is off. They grant nothing until both switches are on."}
          </p>
        </div>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {devices.length ? (
        <div className="account-rows">
          {devices.map((device) => (
            <div className="account-row" key={device.id}>
              <span
                className={`status-dot ${device.enabled ? "available" : "disabled"}`}
              />
              <span>
                <strong>
                  {device.name} · {device.address}
                </strong>
                <small>
                  {device.account_name || device.account_id} ·{" "}
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
                    void change(device, {
                      method: "PUT",
                      body: JSON.stringify({ enabled: !device.enabled }),
                    })
                  }
                >
                  {device.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  aria-label={`Remove device ${device.name}`}
                  onClick={() => void change(device, { method: "DELETE" })}
                >
                  Remove
                </button>
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="hint">
          No registered devices. Account holders register their own in the
          portal.
        </p>
      )}
    </section>
  );
}
