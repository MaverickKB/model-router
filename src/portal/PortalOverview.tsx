import { Check, Copy, KeyRound } from "lucide-react";
import { useState } from "react";
import type { PortalMe } from "./types";
import { UsageMeter } from "./UsageMeter";

export function PortalOverview({
  me,
  onKeys,
}: {
  me: PortalMe;
  onKeys: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const level = me.level;
  return (
    <section className="portal-page">
      <div className="section-heading">
        <div>
          <h1>{me.account.name || me.account.username}</h1>
          <p>
            {level
              ? `${level.name}${level.description ? " · " + level.description : ""}`
              : "No level is assigned to this account. Ask the administrator."}
          </p>
        </div>
      </div>
      {level && (
        <div className="permission-group">
          <h2>What you can use</h2>
          {level.routes.length ? (
            <ul className="portal-routes">
              {level.routes.map((route) => (
                <li key={route.name}>
                  <span
                    className={`status-dot ${route.ready ? "available" : "unavailable"}`}
                    title={route.ready ? "Ready" : "No engine ready"}
                  />
                  <strong>{route.name}</strong>
                  <span>
                    {route.purpose ||
                      (route.ready ? "Ready" : "No engine is ready right now")}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="hint">This level allows no routes yet.</p>
          )}
          <dl className="portal-facts">
            <dt>Cloud engines</dt>
            <dd>{level.allow_cloud ? "Yes" : "No"}</dd>
            <dt>Direct model IDs</dt>
            <dd>{level.allow_direct_models ? "Yes" : "No"}</dd>
            {level.model_patterns.length > 0 && (
              <>
                <dt>Model patterns</dt>
                <dd>{level.model_patterns.join(", ")}</dd>
              </>
            )}
          </dl>
        </div>
      )}
      {me.usage && (
        <div className="permission-group">
          <h2>Limits</h2>
          <UsageMeter usage={me.usage} />
        </div>
      )}
      <div className="permission-group">
        <h2>Connect</h2>
        <p className="hint">
          Point any OpenAI-compatible client at this address with one of your
          keys.
        </p>
        <div className="copy-field">
          <code>{me.base_url}</code>
          <button
            type="button"
            className="subtle"
            onClick={async () => {
              await navigator.clipboard.writeText(me.base_url);
              setCopied(true);
            }}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <button className="primary portal-cta" onClick={onKeys}>
          <KeyRound size={16} />
          {me.keys.length ? "Manage API keys" : "Create your first API key"}
        </button>
      </div>
    </section>
  );
}
