import { SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import { DiscoveryEditor } from "../editors/DiscoveryEditor";
import type { Config } from "../types";
import { AccessSettings } from "./AccessSettings";

export function SettingsPage({
  config,
  operatorUrl,
  save,
  onSignOut,
  initialSection = "Access",
  focusDiscoveryTargets = false,
}: {
  config: Config;
  operatorUrl: string | null;
  save: (config: Config) => Promise<Config>;
  onSignOut: () => void;
  initialSection?: "Access" | "Discovery";
  focusDiscoveryTargets?: boolean;
}) {
  const [section, setSection] = useState(initialSection);
  return (
    <section className="settings-page">
      <div className="section-heading">
        <div>
          <h1>Settings</h1>
          <p>Control access and how your router finds models.</p>
        </div>
        <SlidersHorizontal size={20} />
      </div>
      {config.upgraded_from_schema != null && (
        <p className="settings-inherited">
          <strong>Settings retained during upgrade.</strong> This installation
          kept its existing choices. The settings below are active; edits apply
          only when saved.
        </p>
      )}
      <div
        className="filter-tabs settings-tabs"
        role="tablist"
        aria-label="Settings section"
      >
        {(["Access", "Discovery"] as const).map((value) => (
          <button
            type="button"
            key={value}
            role="tab"
            id={"settings-tab-" + value}
            aria-controls={"settings-panel-" + value}
            aria-selected={section === value}
            className={section === value ? "selected" : ""}
            onClick={() => setSection(value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault();
                const next = section === "Access" ? "Discovery" : "Access";
                setSection(next);
                document.getElementById("settings-tab-" + next)?.focus();
              }
            }}
          >
            {value}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="settings-panel-Access"
        aria-labelledby="settings-tab-Access"
        hidden={section !== "Access"}
      >
        <AccessSettings
          config={config}
          operatorUrl={operatorUrl}
          save={save}
          onSignOut={onSignOut}
        />
      </div>
      <div
        role="tabpanel"
        id="settings-panel-Discovery"
        aria-labelledby="settings-tab-Discovery"
        hidden={section !== "Discovery"}
      >
        <DiscoveryEditor
          config={config}
          save={save}
          focusTargets={focusDiscoveryTargets}
        />
      </div>
    </section>
  );
}
