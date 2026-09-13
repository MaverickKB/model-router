import { useId, useState } from "react";
import {
  callerClientLabel,
  callerDisplayName,
  CallerIdentity,
} from "../caller-identity";
import { ArrowLeft, ChevronDown, Plus, Users } from "lucide-react";
import { ClientEditor, newClient } from "../editors";
import type { Client, Config, EngineView, ObservedCaller } from "../types";

function CallerSource({
  address,
  callers,
  selected,
  onSelect,
}: {
  address: string;
  callers: ObservedCaller[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const latest = Math.max(...callers.map((caller) => caller.last_seen));
  return (
    <div className="caller-source">
      <button
        className="caller-source-toggle"
        aria-expanded={expanded}
        aria-controls={detailsId}
        onClick={() => setExpanded(!expanded)}
      >
        <span>
          <strong>{address || "Source address unavailable"}</strong>{" "}
          <small>
            {callers.length}{" "}
            {callers.length === 1 ? "observation" : "observations"}
            {" · "}Last seen{" "}
            {latest
              ? new Date(latest * 1000).toLocaleString([], {
                  month: "short",
                  day: "numeric",
                  hour: "numeric",
                  minute: "2-digit",
                })
              : "time unavailable"}
          </small>
        </span>
        <ChevronDown size={15} aria-hidden="true" />
      </button>
      <div
        id={detailsId}
        className="caller-source-observations"
        hidden={!expanded}
      >
        {callers.map((caller) => {
          const name = callerDisplayName(caller);
          const client = callerClientLabel(caller);
          const unnamed = !name || name === "Unidentified caller";
          const label = unnamed ? client : name;
          return (
            <button
              key={caller.id}
              className={selected === caller.id ? "selected" : ""}
              aria-pressed={selected === caller.id}
              aria-label={`Inspect ${label} from ${address || "unknown source"}`}
              onClick={() => onSelect(caller.id)}
            >
              <span>
                <strong>{label}</strong>
                <small>
                  {unnamed ? "Application name not supplied" : client}
                </small>
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

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
  const sources = new Map<string, ObservedCaller[]>();
  for (const caller of callers) {
    const observations = sources.get(caller.source_address) || [];
    observations.push(caller);
    sources.set(caller.source_address, observations);
  }
  return (
    <div
      className={
        "management " + (activeClient || connection ? "has-selection" : "")
      }
    >
      <div className="record-list">
        <h2>
          Caller sources <span>{sources.size}</span>
        </h2>
        <p>
          Activity from the last seven days, grouped by direct source address.
          Expand a source to inspect its applications and client libraries. One
          address can represent several applications or devices.
        </p>
        {[...sources].map(([address, observations]) => (
          <CallerSource
            key={address}
            address={address}
            callers={observations}
            selected={selectedCaller}
            onSelect={(id) => {
              selectCaller(id);
              onSelect(null);
            }}
          />
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
              selectCaller(null);
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
      ) : connection ? (
        <div>
          <button className="subtle" onClick={() => selectCaller(null)}>
            <ArrowLeft size={16} />
            Back to caller sources
          </button>
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
          <h2>Inspect a caller source.</h2>
          <p>
            Expand a source and select an observation to inspect its
            <br />
            application details and latest identity evidence.
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
