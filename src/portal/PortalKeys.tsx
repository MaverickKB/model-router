import { Check, Copy, Plus } from "lucide-react";
import { useState } from "react";
import { api, post } from "../api";
import { dateLabel } from "./format";
import type { PortalKey, PortalMe } from "./types";

export function PortalKeys({
  me,
  onChange,
  onError,
}: {
  me: PortalMe;
  onChange: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<{ key: string; name: string } | null>(
    null,
  );
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState<PortalKey | null>(null);
  const snippet = issued
    ? `OPENAI_BASE_URL=${me.base_url}\nOPENAI_API_KEY=${issued.key}`
    : "";

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
          <h1>API keys</h1>
          <p>
            Each key grants exactly your level's access. Keys are shown once.
          </p>
        </div>
      </div>
      <form
        className="portal-inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const result = await post<{
              key: string;
              record: PortalKey;
            }>("/api/v1/portal/keys", { name: name.trim() });
            setIssued({ key: result.key, name: result.record.name });
            setCopied(false);
            setName("");
          });
        }}
      >
        <label className="field">
          <span>Key name</span>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="laptop, ci-runner, notebook…"
            maxLength={60}
            required
          />
        </label>
        <button className="primary" disabled={busy || !name.trim()}>
          <Plus size={16} />
          Create key
        </button>
      </form>
      {issued && (
        <div className="key-result" role="status">
          <p>
            Copy the key for <strong>{issued.name}</strong> now. It is shown
            once.
          </p>
          <code>{issued.key}</code>
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard.writeText(issued.key);
              setCopied(true);
            }}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            {copied ? "Copied" : "Copy key"}
          </button>
          <pre className="portal-snippet">{snippet}</pre>
        </div>
      )}
      <ul className="portal-list" aria-label="Your API keys">
        {me.keys.map((key) => (
          <li key={key.id}>
            <div>
              <strong>{key.name}</strong>
              <small>
                Created {dateLabel(key.created)} · Last used{" "}
                {dateLabel(key.last_used)}
              </small>
            </div>
            {confirming?.id === key.id ? (
              <span className="portal-confirm">
                Revoke {key.name}?
                <button
                  type="button"
                  className="danger"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      await api(`/api/v1/portal/keys/${key.id}`, {
                        method: "DELETE",
                      });
                      setConfirming(null);
                      if (issued?.name === key.name) setIssued(null);
                    })
                  }
                >
                  Revoke
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
                onClick={() => setConfirming(key)}
              >
                Revoke
              </button>
            )}
          </li>
        ))}
      </ul>
      {!me.keys.length && !issued && (
        <p className="hint">
          No keys yet. Create one to start sending requests.
        </p>
      )}
    </section>
  );
}
