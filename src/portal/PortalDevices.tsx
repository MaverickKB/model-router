import { Plus } from "lucide-react";
import { useState } from "react";
import { api, post } from "../api";
import { Field } from "../components";
import { dateLabel } from "./format";
import type { PortalMe, RegisteredDevice } from "./types";

export function PortalDevices({
  me,
  onChange,
  onError,
}: {
  me: PortalMe;
  onChange: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [address, setAddress] = useState(me.source_address);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<RegisteredDevice | null>(null);
  const registered = me.devices.registered;

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      onError("");
      await onChange();
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="portal-page">
      <div className="section-heading">
        <div>
          <h1>Devices</h1>
          <p>Machines and harnesses recently seen using your access.</p>
        </div>
      </div>
      <div className="permission-group">
        <h2>Seen recently</h2>
        {me.devices.observed.length ? (
          <ul className="portal-list" aria-label="Recently seen devices">
            {me.devices.observed.map((device) => (
              <li
                // One host can hold several observed connections on one key.
                key={[
                  device.via,
                  device.source_address,
                  device.credential_name,
                  device.reported_name,
                  device.software,
                ].join("|")}
              >
                <div>
                  <strong>
                    {device.reported_name || device.software || "Connection"}
                  </strong>
                  <small>
                    {device.source_address} ·{" "}
                    {device.via === "device"
                      ? "registered device"
                      : `key ${device.credential_name}`}{" "}
                    · {dateLabel(device.last_seen)}
                    {device.software && device.reported_name
                      ? ` · ${device.software}`
                      : ""}
                  </small>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">
            Nothing yet. Connections that use your keys appear here.
          </p>
        )}
      </div>
      {registered !== null && (
        <div className="permission-group">
          <h2>Registered devices</h2>
          <p className="hint">
            Dev mode: a registered address can use your allowed routes without a
            key. Anyone at that address gets your access and counts against your
            limits.
          </p>
          <form
            className="portal-inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              void run(async () => {
                await post("/api/v1/portal/devices", {
                  address: address.trim(),
                  name: name.trim(),
                });
                setName("");
              });
            }}
          >
            <Field
              label="Address"
              hint="One IPv4 or IPv6 address. Prefilled with this browser's address as the router sees it."
            >
              <input
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                maxLength={64}
                required
              />
            </Field>
            <label className="field">
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="workstation, edge box…"
                maxLength={60}
                required
              />
            </label>
            <button
              className="primary"
              disabled={busy || !name.trim() || !address.trim()}
            >
              <Plus size={16} />
              Register device
            </button>
          </form>
          {registered.length ? (
            <ul className="portal-list" aria-label="Registered devices">
              {registered.map((device) => (
                <li key={device.id}>
                  <div>
                    <strong>
                      {device.name}
                      {!device.enabled && (
                        <span className="tag">
                          Disabled by the administrator
                        </span>
                      )}
                      {device.shadowed && (
                        <span className="tag">
                          Takes precedence over an administrator network policy
                        </span>
                      )}
                    </strong>
                    <small>
                      {device.address} · Last matched{" "}
                      {dateLabel(device.last_matched)}
                    </small>
                  </div>
                  {confirming?.id === device.id ? (
                    <span className="portal-confirm">
                      Remove {device.name}?
                      <button
                        type="button"
                        className="danger"
                        disabled={busy}
                        onClick={() =>
                          void run(async () => {
                            await api(`/api/v1/portal/devices/${device.id}`, {
                              method: "DELETE",
                            });
                            setConfirming(null);
                          })
                        }
                      >
                        Remove
                      </button>
                      <button
                        type="button"
                        className="subtle"
                        onClick={() => setConfirming(null)}
                      >
                        Keep
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="danger text-button"
                      onClick={() => setConfirming(device)}
                    >
                      Remove
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">No registered devices.</p>
          )}
        </div>
      )}
    </section>
  );
}
