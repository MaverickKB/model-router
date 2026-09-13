import { useState } from "react";
import { CallerSourceDetails, groupCallerSources } from "../caller-sources";
import { sourceTimeLabel } from "../source-time";
import { ArrowLeft, Plus, Users } from "lucide-react";
import { ClientEditor, newClient } from "../editors";
import type { Client, Config, EngineView, ObservedCaller } from "../types";

export function ClientsView({
  config,
  engines,
  activeClient,
  onSelect,
  save,
  onDelete,
  clients,
  callers = [],
  onRenameSource,
}: {
  config: Config;
  engines: EngineView[];
  activeClient: Client | null;
  onSelect: (value: Client | null) => void;
  save: (config: Config) => Promise<Config>;
  onDelete: () => Promise<void>;
  clients: Client[];
  callers?: ObservedCaller[];
  onRenameSource?: (sourceKey: string, name: string) => Promise<void>;
}) {
  const [selectedSource, selectSource] = useState<string | null>(null);
  const sources = groupCallerSources(callers);
  const source = sources.find((entry) => entry.key === selectedSource);
  return (
    <div
      className={
        "management " + (activeClient || source ? "has-selection" : "")
      }
    >
      <div className="record-list">
        <h2>
          Caller sources <span>{sources.length}</span>
        </h2>
        <p>
          Caller endpoints observed in the last seven days. Select a source to
          inspect its request metadata and permissions.
        </p>
        {sources.map((entry) => (
          <button
            key={entry.key}
            className={
              selectedSource === entry.key && !activeClient ? "selected" : ""
            }
            aria-pressed={selectedSource === entry.key && !activeClient}
            aria-label={`Inspect source ${
              entry.displayName || "address unavailable"
            }`}
            onClick={() => {
              selectSource(entry.key);
              onSelect(null);
            }}
          >
            <Users size={17} />
            <span>
              <strong>
                {entry.displayName || "Source address unavailable"}
              </strong>
              <small>
                {entry.labelSource === "operator"
                  ? "Named source"
                  : entry.labelSource === "reported_hostname"
                    ? "Reported hostname"
                    : entry.labelSource === "discovered_hostname"
                      ? "Discovered hostname"
                    : "Address-bound"}{" "}
                · Last seen {sourceTimeLabel(entry.lastSeen)}
              </small>
            </span>
          </button>
        ))}
        {!callers.length && <p>No caller traffic yet.</p>}
        <h2>
          Permission policies <span>{config.clients.length}</span>
        </h2>
        <p>
          These rules govern callers. A policy is not proof that an agent has
          connected.
        </p>
        {clients.map((c) => (
          <button
            key={c.id}
            className={activeClient?.id === c.id ? "selected" : ""}
            onClick={() => {
              selectSource(null);
              onSelect(c);
            }}
          >
            <Users size={17} />
            <span>
              <strong>{c.name}</strong>
              <small>
                {c.kind ? `${c.kind} · ` : ""}
                {c.id === config.security.anonymous_client_id
                  ? "Default unkeyed policy"
                  : c.allow_network_auth && c.source_networks.length
                    ? "Explicit source override"
                    : c.has_key
                      ? "Caller key configured"
                      : "Key not created"}
              </small>
            </span>
            <span
              className={"status-dot " + (c.enabled ? "available" : "disabled")}
            />
          </button>
        ))}
      </div>
      {activeClient ? (
        <ClientEditor
          key={activeClient.id}
          initial={activeClient}
          config={config}
          engines={engines}
          save={save}
          onClose={() => onSelect(null)}
          onDelete={onDelete}
        />
      ) : source ? (
        <div className="source-inspector">
          <button className="subtle" onClick={() => selectSource(null)}>
            <ArrowLeft size={16} />
            Back to caller sources
          </button>
          <CallerSourceDetails
            source={source}
            policies={config.clients}
            onEditPolicy={onSelect}
            onRename={
              onRenameSource
                ? (entry, name) => onRenameSource(entry.key, name)
                : undefined
            }
          />
        </div>
      ) : (
        <div className="detail-empty">
          <Users size={30} strokeWidth={1.3} />
          <h2>Inspect a caller source.</h2>
          <p>
            Select a source to see its activity, reported software,
            <br />
            and the permission policies used by its requests.
          </p>
          <button onClick={() => onSelect(newClient())}>
            <Plus size={16} />
            Add caller
          </button>
        </div>
      )}{" "}
    </div>
  );
}
