import { LogOut } from "lucide-react";
import { useState } from "react";
import { api, post } from "../api";
import { dateLabel } from "./format";
import type { PortalMe } from "./types";

export function PortalAccount({
  me,
  onChange,
  onSignedOut,
}: {
  me: PortalMe;
  onChange: () => Promise<void>;
  onSignedOut: () => Promise<void>;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [message, setMessage] = useState("");
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <section className="portal-page">
      <div className="section-heading">
        <div>
          <h1>Account</h1>
          <p>Signed in as {me.account.username}</p>
        </div>
        <button
          className="subtle"
          onClick={async () => {
            try {
              await post("/api/v1/portal/logout");
            } finally {
              await onSignedOut();
            }
          }}
        >
          <LogOut size={15} />
          Sign out
        </button>
      </div>
      <dl className="portal-facts">
        <dt>Name</dt>
        <dd>{me.account.name || "—"}</dd>
        <dt>Username</dt>
        <dd>{me.account.username}</dd>
        <dt>Member since</dt>
        <dd>{dateLabel(me.account.created)}</dd>
        <dt>Last sign-in</dt>
        <dd>{dateLabel(me.account.last_login)}</dd>
      </dl>
      <form
        className="permission-group portal-password"
        onSubmit={(e) => {
          e.preventDefault();
          if (next !== confirm) {
            setFailure("Passwords do not match");
            return;
          }
          setBusy(true);
          api("/api/v1/portal/password", {
            method: "PUT",
            body: JSON.stringify({ current, new: next }),
          })
            .then(async () => {
              setCurrent("");
              setNext("");
              setConfirm("");
              setFailure("");
              setMessage(
                "Password changed. Other browsers were signed out; your API keys are unchanged.",
              );
              await onChange();
            })
            .catch((err) => {
              setMessage("");
              setFailure(err instanceof Error ? err.message : String(err));
            })
            .finally(() => setBusy(false));
        }}
      >
        <h2>Change password</h2>
        <label className="field">
          <span>Current password</span>
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <div className="field-pair">
          <label className="field">
            <span>New password</span>
            <input
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
              minLength={12}
              maxLength={256}
              required
            />
          </label>
          <label className="field">
            <span>Confirm new password</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
          </label>
        </div>
        <p className="hint">
          12 to 256 characters, anything except your username. Changing it signs
          out every other browser and leaves your API keys untouched.
        </p>
        {failure && (
          <p className="error" role="alert">
            {failure}
          </p>
        )}
        {message && (
          <p className="saved" role="status">
            {message}
          </p>
        )}
        <button className="primary" disabled={busy}>
          {busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </section>
  );
}
