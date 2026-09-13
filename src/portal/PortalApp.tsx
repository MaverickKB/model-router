import { Network, X } from "lucide-react";
import { useState } from "react";
import "../styles.css";
import { PortalAccount } from "./PortalAccount";
import { PortalDevices } from "./PortalDevices";
import { PortalDisabled } from "./PortalDisabled";
import { PortalKeys } from "./PortalKeys";
import { PortalOverview } from "./PortalOverview";
import { hashToken, PortalSignIn } from "./PortalSignIn";
import "./portal.css";
import { usePortalState } from "./usePortalState";

const TABS = ["Overview", "API keys", "Devices", "Account"] as const;
type Tab = (typeof TABS)[number];

export function PortalApp() {
  const { status, me, signedOut, error, reload, signOut, setError } =
    usePortalState();
  const [tab, setTab] = useState<Tab>("Overview");
  // An activation link is honoured even while another session is signed in:
  // the form stays until the token is consumed, and /activate issues the new session.
  const [token, setToken] = useState(hashToken);
  const connecting = (
    <div className="unlock">
      <Network size={32} />
      <h1>Model Router · Portal</h1>
      <p>{error || "Connecting to your router…"}</p>
    </div>
  );
  if (!status) return connecting;
  if (!status.enabled) return <PortalDisabled />;
  if (signedOut || token)
    return (
      <PortalSignIn
        key={error}
        token={token}
        error={error}
        onSignedIn={async () => {
          setError("");
          setTab("Overview");
          await reload();
          setToken("");
        }}
      />
    );
  // Signed in per /status but /me has not answered yet (or failed transiently):
  // the hook keeps polling, so this is never the sign-in form.
  if (!me) return connecting;
  return (
    <div className="application portal">
      <header className="titlebar">
        <div className="brand">
          <Network size={25} strokeWidth={1.7} />
          <span>Model Router · Portal</span>
        </div>
        <span className="connection">
          <span className="status-dot available" />
          {me.account.name || me.account.username}
        </span>
      </header>
      <nav className="topbar paper" aria-label="Portal navigation">
        <div className="tabs">
          {TABS.map((t) => (
            <button
              key={t}
              aria-current={tab === t ? "page" : undefined}
              className={tab === t ? "active" : ""}
              onClick={() => {
                setTab(t);
                setError("");
              }}
            >
              {t}
            </button>
          ))}
        </div>
      </nav>
      {error && (
        <div className="global-error" role="alert">
          {error}
          <button
            className="icon-button"
            aria-label="Dismiss error"
            onClick={() => setError("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
      <div className="workspace">
        <main>
          {tab === "Overview" && (
            <PortalOverview me={me} onKeys={() => setTab("API keys")} />
          )}
          {tab === "API keys" && (
            <PortalKeys me={me} onChange={reload} onError={setError} />
          )}
          {tab === "Devices" && (
            <PortalDevices me={me} onChange={reload} onError={setError} />
          )}
          {tab === "Account" && (
            <PortalAccount
              me={me}
              onChange={reload}
              onError={setError}
              onSignedOut={signOut}
            />
          )}
        </main>
      </div>
    </div>
  );
}
