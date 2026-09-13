import { Check, Save } from "lucide-react";
import { useState } from "react";
import { Field, ListInput, Select, Switch } from "../components";
import { requireUnchanged, sameRecord } from "../editor-state";
import type { Config, Discovery } from "../types";
import { SavedDiscovery } from "../settings/SettingsSummary";

type SaveConfig = (config: Config) => Promise<Config>;
export function DiscoveryEditor({
  config,
  save,
  focusTargets = false,
}: {
  config: Config;
  save: SaveConfig;
  focusTargets?: boolean;
}) {
  const [draft, set] = useState<Discovery>(config.discovery),
    [error, setError] = useState(""),
    [saved, setSaved] = useState(false);
  const [baseline, setBaseline] = useState(config.discovery);
  return (
    <form
      className="settings-form"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          requireUnchanged(baseline, config.discovery, "discovery policy");
          const result = await save({ ...config, discovery: draft });
          setBaseline(result.discovery);
          set(result.discovery);
          setError("");
          setSaved(true);
        } catch (err) {
          setError(String(err instanceof Error ? err.message : err));
        }
      }}
    >
      <SavedDiscovery config={config} />
      {!sameRecord(baseline, draft) && (
        <p className="hint" role="status">
          Changes below are not active until saved.
        </p>
      )}
      <div className="setting-row">
        <div>
          <strong>Automatic discovery</strong>
          <p>Continuously look for new engines inside the configured scope.</p>
        </div>
        <Switch
          label="Automatic discovery"
          checked={draft.enabled}
          onChange={() => set({ ...draft, enabled: !draft.enabled })}
        />
      </div>
      <Field
        label="Where to look"
        hint="Hostnames, IP addresses, or network ranges, separated by commas. IP/CIDR scopes avoid DNS ambiguity. Hostname addresses are checked but are not pinned through connection; DNS rebinding protection remains open work."
      >
        <ListInput
          autoFocus={focusTargets}
          aria-label="Where to look"
          placeholder="Add a host or a network range"
          values={draft.targets}
          onValues={(targets) => set({ ...draft, targets })}
        />
      </Field>
      <Field
        label="TCP port coverage"
        hint="The program checks every TCP port in this range. HTTP inspection is controlled separately below. Use 1-65535 for full coverage."
      >
        <input
          value={draft.port_range}
          onChange={(e) => set({ ...draft, port_range: e.target.value })}
          placeholder="8000-8100"
        />
      </Field>
      <Field label="Scanner">
        <Select
          value={draft.scanner}
          onChange={(scanner) =>
            set({ ...draft, scanner: scanner as Discovery["scanner"] })
          }
        >
          <option value="connect">Portable TCP connections</option>
          <option value="nmap">Nmap adapter (installed separately)</option>
        </Select>
      </Field>
      <div className="setting-row">
        <div>
          <strong>Inspect all open ports as HTTP</strong>
          <p>
            Send HTTP and HTTPS catalog requests to every open port. Enable only
            for a scope whose services can safely receive HTTP requests.
            Requests include metadata POSTs where supported and can have side
            effects on printers and other non-HTTP services.
          </p>
        </div>
        <Switch
          label="Inspect all open ports as HTTP"
          checked={draft.inspect_all_open_ports}
          onChange={() =>
            set({
              ...draft,
              inspect_all_open_ports: !draft.inspect_all_open_ports,
            })
          }
        />
      </div>
      {!draft.inspect_all_open_ports && (
        <Field
          label="Ports approved for HTTP inspection"
          hint="These ports are always included in the TCP scan, then HTTP-inspected. Defaults cover common serving ports such as Ollama 11434 and vLLM 8888, even when the range above is 8000-8100."
        >
          <ListInput
            values={draft.http_ports}
            onValues={(ports) =>
              set({ ...draft, http_ports: ports.map(Number) })
            }
          />
        </Field>
      )}
      <div className="setting-row">
        <div>
          <strong>Include router-local listeners</strong>
          <p>
            Inventory loopback listeners using the optional discovery package.
            HTTP inspection follows the same policy.
          </p>
        </div>
        <Switch
          label="Include router-local listeners"
          checked={draft.include_loopback}
          onChange={() =>
            set({ ...draft, include_loopback: !draft.include_loopback })
          }
        />
      </div>
      <Field
        label="Maximum addresses per sweep"
        hint="Increase this budget to cover a larger network. Exceeding it reports an error and performs no scan."
      >
        <input
          type="number"
          min="1"
          value={draft.max_addresses}
          onChange={(e) =>
            set({ ...draft, max_addresses: Number(e.target.value) })
          }
        />
      </Field>
      <div className="setting-row">
        <div>
          <strong>Register discovered engines</strong>
          <p>
            Trust discovered serving endpoints in this scope to receive
            requests. Caller allowlists still apply. Enable only for a network
            you trust.
          </p>
        </div>
        <Switch
          label="Register discovered engines"
          checked={draft.auto_register}
          onChange={() =>
            set({ ...draft, auto_register: !draft.auto_register })
          }
        />
      </div>
      <div className="setting-row">
        <div>
          <strong>Listen for network announcements</strong>
          <p>
            Listen only for model-serving announcements inside the same scope.
            Requires the optional discovery package.
          </p>
        </div>
        <Switch
          label="Listen for network announcements"
          checked={draft.mdns}
          onChange={() => set({ ...draft, mdns: !draft.mdns })}
        />
      </div>
      <details className="advanced">
        <summary>Excluded endpoints</summary>
        <p className="hint">
          Removed engines stay excluded from automatic discovery. Clear an
          endpoint to allow it to return.
        </p>
        <ListInput
          aria-label="Excluded endpoints"
          values={draft.ignored_urls}
          onValues={(ignored_urls) => set({ ...draft, ignored_urls })}
        />
      </details>
      <div className="field-pair">
        <Field label="Network sweep interval (seconds)">
          <input
            type="number"
            min="60"
            value={draft.network_interval_seconds}
            onChange={(e) =>
              set({
                ...draft,
                network_interval_seconds: Number(e.target.value),
              })
            }
          />
        </Field>
        <Field label="Maximum connection attempts or packets per second">
          <input
            type="number"
            min="10"
            max="10000"
            value={draft.packets_per_second}
            onChange={(e) =>
              set({ ...draft, packets_per_second: Number(e.target.value) })
            }
          />
        </Field>
      </div>
      <details className="advanced">
        <summary>Discovery timing</summary>
        <div className="field-pair">
          <Field label="Registration check interval (seconds)">
            <input
              type="number"
              min="5"
              value={draft.interval_seconds}
              onChange={(e) =>
                set({ ...draft, interval_seconds: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Catalog refresh (seconds)">
            <input
              type="number"
              min="2"
              value={draft.refresh_seconds}
              onChange={(e) =>
                set({ ...draft, refresh_seconds: Number(e.target.value) })
              }
            />
          </Field>
        </div>
        <Field label="Observation expiry (seconds)">
          <input
            type="number"
            min="5"
            value={draft.stale_seconds}
            onChange={(e) =>
              set({ ...draft, stale_seconds: Number(e.target.value) })
            }
          />
        </Field>
      </details>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <footer>
        {saved && sameRecord(baseline, draft) && (
          <span className="saved">
            <Check size={15} />
            Discovery settings saved
          </span>
        )}
        <button className="primary">
          <Save size={15} />
          Save discovery
        </button>
      </footer>
    </form>
  );
}
