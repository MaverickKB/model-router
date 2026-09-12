import { useState } from "react";
import {
  callerDisplayName,
  callerSummary,
  CallerIdentity,
} from "../caller-identity";
import { Plus, Users } from "lucide-react";
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
}: {
  config: Config;
  engines: EngineView[];
  activeClient: Client | null;
  onSelect: (value: Client | null) => void;
  save: (config: Config) => Promise<Config>;
  onDelete: () => Promise<void>;
  clients: Client[];
  callers?: ObservedCaller[];
}) {
  const [selectedCaller, selectCaller] = useState<string | null>(null);
  const connection = callers.find((c) => c.id === selectedCaller);
  return (
    <div className={"management " + (activeClient ? "has-selection" : "")}>
      <div className="record-list">
        <h2>
          Connected callers<span>{callers.length}</span>
        </h2>
        <p>Connections observed by this candidate in the last seven days.</p>
        {callers.map((caller) => (
          <button
            key={caller.id}
            onClick={() => {
              selectCaller(caller.id);
              onSelect(null);
            }}
          >
            <Users size={17} />
            <span>
              <strong>{callerDisplayName(caller)}</strong>
              <small>{callerSummary(caller)}</small>
            </span>
          </button>
        ))}
        {!callers.length && <p>No caller traffic yet.</p>}
        <h2>
          Permission policies<span>{config.clients.length}</span>
        </h2>
        <p>
          These rules govern callers. A policy is not proof that an agent has
          connected.
        </p>
        {clients.map((c) => (
          <button
            key={c.id}
            className={activeClient?.id === c.id ? "selected" : ""}
            onClick={() => onSelect(c)}
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
      ) : connection ? (
        <div>
          <CallerIdentity
            caller={connection}
            policy={
              connection.policy_id
                ? config.clients.find((p) => p.id === connection.policy_id)
                : undefined
            }
          />
          {connection.policy_id && (
            <button
              onClick={() =>
                onSelect(
                  config.clients.find((p) => p.id === connection.policy_id) ||
                    null,
                )
              }
            >
              Edit assigned permissions
            </button>
          )}
        </div>
      ) : (
        <div className="detail-empty">
          <Users size={30} strokeWidth={1.3} />
          <h2>Know who is connected.</h2>
          <p>
            Select a connection to inspect its source,
            <br />
            software and assigned permission policy.
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
