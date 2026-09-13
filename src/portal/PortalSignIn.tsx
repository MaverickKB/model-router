import { KeyRound, Network } from "lucide-react";
import { useState } from "react";
import { post } from "../api";

function hashToken() {
  const match = /(?:^#|[#&])token=([^&]+)/.exec(location.hash);
  return match ? decodeURIComponent(match[1]) : "";
}

export function PortalSignIn({
  error,
  onSignedIn,
}: {
  error: string;
  onSignedIn: () => Promise<void>;
}) {
  const [token, setToken] = useState(hashToken);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [remember, setRemember] = useState(true);
  const [message, setMessage] = useState(error);
  const [busy, setBusy] = useState(false);
  const activating = token !== "";

  async function submit(fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      setMessage("");
      setPassword("");
      setConfirm("");
      await onSignedIn();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="unlock">
      <Network size={34} />
      <h1>Model Router · Portal</h1>
      <p>
        {activating
          ? "Choose a password to activate your account."
          : "Sign in to manage your API keys."}
      </p>
      <form
        className="paper"
        onSubmit={(e) => {
          e.preventDefault();
          if (activating) {
            if (password !== confirm) {
              setMessage("Passwords do not match");
              return;
            }
            void submit(async () => {
              await post("/api/v1/portal/activate", { token, password });
              // The one-time token must not survive in the address bar.
              history.replaceState(null, "", "/portal");
              setToken("");
            });
          } else {
            void submit(() =>
              post("/api/v1/portal/login", { username, password, remember }),
            );
          }
        }}
      >
        {activating ? (
          <>
            <label className="field">
              <span>New password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoFocus
                autoComplete="new-password"
                minLength={12}
                maxLength={256}
                required
              />
              <small>
                12 to 256 characters. Anything except your username.
              </small>
            </label>
            <label className="field">
              <span>Confirm password</span>
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>
          </>
        ) : (
          <>
            <label className="field">
              <span>Username</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                autoCapitalize="none"
                required
              />
            </label>
            <label className="field">
              <span>Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>
            <label className="portal-remember">
              <input
                type="checkbox"
                checked={remember}
                onChange={() => setRemember(!remember)}
              />
              Stay signed in on this browser
            </label>
          </>
        )}
        {message && (
          <p className="error" role="alert">
            {message}
          </p>
        )}
        <button className="primary" disabled={busy}>
          <KeyRound size={16} />
          {busy
            ? activating
              ? "Activating…"
              : "Signing in…"
            : activating
              ? "Activate account"
              : "Sign in"}
        </button>
        <small>
          {activating
            ? "This link works once. A refused password leaves it usable."
            : "Ask the administrator for an activation link if you do not have a password yet."}
        </small>
      </form>
    </div>
  );
}
