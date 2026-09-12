import { useState } from "react";
import { post } from "../api";
import { Dialog, Field, Select } from "../components";
import { engineUrls } from "../engine-addresses";
import type { Config, Engine } from "../types";

export function MergeEnginesDialog({
  source,
  config,
  targetId: suggestedTargetId,
  onMerged,
  onClose,
}: {
  source: Engine;
  config: Config;
  targetId?: string;
  onMerged: () => Promise<void>;
  onClose: () => void;
}) {
  const [revision] = useState(config.revision);
  const [targetId, setTargetId] = useState(
    suggestedTargetId && suggestedTargetId !== source.id
      ? suggestedTargetId
      : config.engines.find((e) => e.id !== source.id)?.id || "",
  );
  const target = config.engines.find((e) => e.id === targetId);
  const [preferred, setPreferred] = useState(
    target?.base_url || source.base_url,
  );
  const [credential, setCredential] = useState("target");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Dialog title="Merge engine addresses" onClose={onClose}>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            await post("/api/v1/engines/merge", {
              revision,
              source_id: source.id,
              target_id: targetId,
              preferred_url: preferred,
              credential_source: credential,
            });
            await onMerged();
            onClose();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <p className="hint">
          Use this when two rows reach the same complete API. All addresses will
          belong to one engine. Similar model names alone do not establish that
          identity.
        </p>
        <Field label={`Merge ${source.name} into`}>
          <Select
            value={targetId}
            onChange={(id) => {
              setTargetId(id);
              setPreferred(config.engines.find((e) => e.id === id)!.base_url);
            }}
          >
            {config.engines
              .filter((e) => e.id !== source.id)
              .map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                </option>
              ))}
          </Select>
        </Field>
        <Field
          label="Preferred URL"
          hint="Requests use this address. The others remain aliases, not independent backups."
        >
          <Select value={preferred} onChange={setPreferred}>
            {[
              ...new Set([
                ...(target ? engineUrls(target) : []),
                ...engineUrls(source),
              ]),
            ].map((url) => (
              <option key={url}>{url}</option>
            ))}
          </Select>
        </Field>
        <Field label="Credential to keep">
          <Select value={credential} onChange={setCredential}>
            <option value="target">
              Use {target?.name}'s saved credential
            </option>
            <option value="source">Use {source.name}'s saved credential</option>
            <option value="none">No credential</option>
          </Select>
        </Field>
        <p className="hint">
          {target?.name} keeps its name, local/cloud type, model policy and
          limits. Routes and caller allowlists that name {source.name} will
          reference {target?.name}. The separate {source.name} row is removed.
          Existing request history keeps its original identities.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="primary" disabled={busy || !target}>
            {busy ? "Merging…" : "Merge into one engine"}
          </button>
        </footer>
      </form>
    </Dialog>
  );
}
