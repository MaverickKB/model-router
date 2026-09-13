import { SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import { DiscoveryEditor } from "../editors/DiscoveryEditor";
import type { Config } from "../types";
import { AccessSettings } from "./AccessSettings";
import { AccountsSettings } from "./AccountsSettings";

export const SECTIONS = ["Access", "Accounts", "Discovery"] as const;
export type Section = (typeof SECTIONS)[number];

export function SettingsPage({
  config,
  operatorUrl,
  baseUrl,
  levelsInUse,
  save,
  onSignOut,
  initialSection = "Access",
  focusDiscoveryTargets = false,
}: {
  config: Config;
  operatorUrl: string | null;
  baseUrl: string;
  levelsInUse?: Record<string, number>;
  save: (config: Config) => Promise<Config>;
  onSignOut: () => void;
  initialSection?: Section;
  focusDiscoveryTargets?: boolean;
}) {
  const [section, setSection] = useState<Section>(initialSection);
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
        {SECTIONS.map((value) => (
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
                const step = event.key === "ArrowRight" ? 1 : -1;
                const next =
                  SECTIONS[
                    (SECTIONS.indexOf(section) + step + SECTIONS.length) %
                      SECTIONS.length
                  ];
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
        id="settings-panel-Accounts"
        aria-labelledby="settings-tab-Accounts"
        hidden={section !== "Accounts"}
      >
        <AccountsSettings
          config={config}
          save={save}
          baseUrl={baseUrl}
          levelsInUse={levelsInUse}
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
