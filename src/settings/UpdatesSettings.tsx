import { useEffect, useState } from "react";
import { api, post } from "../api";
import { Switch } from "../components";
import type { Config, ReleaseStatus } from "../types";

export function UpdatesSettings({
  config,
  save,
}: {
  config: Config;
  save: (config: Config) => Promise<Config>;
}) {
  const [draft, setDraft] = useState(config.updates);
  const [status, setStatus] = useState<ReleaseStatus | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  async function load(refresh = false) {
    setError("");
    try {
      setStatus(
        await api<ReleaseStatus>(
          "/api/v1/updates" + (refresh ? "?refresh=true" : ""),
        ),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  useEffect(() => {
    void load();
  }, []);
  return (
    <section aria-labelledby="updates-heading" className="access-settings">
      <h2 id="updates-heading">Updates</h2>
      <p>
        Checking GitHub does not install anything. Applying a release is one
        explicit click, then the router restarts.
      </p>
      <div className="setting-row">
        <div>
          <strong>Check GitHub for releases</strong>
          <p>
            Repository {draft.repository}. Turn this off if this install should
            not contact GitHub.
          </p>
        </div>
        <Switch
          label="Check GitHub for releases"
          checked={draft.check_enabled}
          onChange={async () => {
            const updates = {
              ...draft,
              check_enabled: !draft.check_enabled,
            };
            setBusy("save");
            try {
              const saved = await save({ ...config, updates });
              setDraft(saved.updates);
              await load(true);
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy("");
            }
          }}
        />
      </div>
      {status && (
        <p>
          Running {status.current}
          {status.latest ? `. Latest published ${status.latest}.` : "."}{" "}
          {status.error}
        </p>
      )}
      {status?.notes && <pre className="update-notes">{status.notes}</pre>}
      {status?.html_url && (
        <p>
          <a href={status.html_url} target="_blank" rel="noreferrer">
            Release notes on GitHub
          </a>
        </p>
      )}
      <div className="setting-row">
        <button
          type="button"
          disabled={busy !== ""}
          onClick={() => void load(true)}
        >
          Check now
        </button>
        <button
          type="button"
          className="primary"
          disabled={!status?.available || busy !== ""}
          onClick={async () => {
            if (!status?.latest) return;
            setBusy("apply");
            setError("");
            try {
              await post("/api/v1/updates/apply", { tag: status.latest });
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
              setBusy("");
            }
          }}
        >
          {busy === "apply" ? "Updating…" : "Install update"}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </section>
  );
}
