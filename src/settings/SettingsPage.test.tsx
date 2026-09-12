import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fixtureConfig } from "../test-fixtures";
import type { Config } from "../types";
import { SettingsPage } from "./SettingsPage";

describe("Saved settings and inherited choices", () => {
  it("makes an inherited open scope visible without changing it on visit", async () => {
    const config = fixtureConfig();
    config.upgraded_from_schema = 0;
    config.security.operator_auth_enabled = false;
    config.security.client_auth_enabled = false;
    config.security.operator_networks = ["192.0.2.0/24"];
    Object.assign(config.discovery, {
      enabled: true,
      targets: ["192.0.2.0/24"],
      port_range: "1-65535",
      inspect_all_open_ports: true,
      auto_register: true,
      scanner: "nmap",
      include_loopback: true,
    });
    const save = vi.fn(async (value: Config) => value);
    const user = userEvent.setup();
    render(
      <SettingsPage
        config={config}
        operatorUrl="https://console.test"
        save={save}
        onSignOut={() => {}}
      />,
    );
    expect(screen.getByText("Settings retained during upgrade.")).toBeVisible();
    const access = screen.getByRole("region", { name: "Saved access state" });
    expect(access).toHaveTextContent("Open administration");
    expect(access).toHaveTextContent("192.0.2.0/24");
    expect(access).toHaveTextContent("https://console.test");
    expect(access).toHaveTextContent("not user isolation");
    expect(access).toHaveTextContent(
      "Callers can submit model endpoints without signing in",
    );
    expect(access).toHaveTextContent("Automatic registration is on");
    await user.click(screen.getByRole("tab", { name: "Discovery" }));
    const discovery = screen.getByRole("region", {
      name: "Saved discovery state",
    });
    expect(discovery).toHaveTextContent("Automatic sweeps on");
    expect(discovery).toHaveTextContent("Ports 1-65535");
    expect(discovery).toHaveTextContent("Every open port receives HTTP");
    expect(discovery).toHaveTextContent("metadata POSTs");
    expect(discovery).toHaveTextContent("printers and other non-HTTP services");
    expect(discovery).toHaveTextContent(
      "receive requests allowed by caller policies",
    );
    expect(save).not.toHaveBeenCalled();
  });

  it("keeps saved effects distinct from drafts and updates both pages after save", async () => {
    const initial = fixtureConfig();
    initial.discovery.targets = ["192.0.2.0/24"];
    const saved = vi.fn();
    function Console() {
      const [config, setConfig] = useState(initial);
      return (
        <SettingsPage
          config={config}
          operatorUrl={null}
          onSignOut={() => {}}
          save={async (value) => {
            const next = { ...value, revision: value.revision + 1 };
            setConfig(next);
            saved(next);
            return next;
          }}
        />
      );
    }
    const user = userEvent.setup();
    render(<Console />);
    expect(
      screen.getByText(/Canonical address is not configured/),
    ).toBeVisible();
    expect(
      screen.queryByText("Settings retained during upgrade."),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Operator sign-in" }));
    const access = screen.getByRole("region", { name: "Saved access state" });
    expect(access).toHaveTextContent("Operator sign-in required");
    expect(
      screen.getByText("Changes below are not active until saved."),
    ).toBeVisible();
    expect(saved).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "Save access settings" }),
    );
    await within(access).findByText("Open administration");
    await user.click(screen.getByRole("tab", { name: "Discovery" }));
    const discovery = screen.getByRole("region", {
      name: "Saved discovery state",
    });
    expect(discovery).toHaveTextContent(
      "Callers can submit model endpoints without signing in",
    );
    expect(discovery).toHaveTextContent("Automatic registration is off");
    await user.click(
      screen.getByRole("switch", { name: "Register discovered engines" }),
    );
    expect(discovery).toHaveTextContent("Automatic registration is off");
    await user.click(screen.getByRole("button", { name: "Save discovery" }));
    await within(discovery).findByText(/Automatic registration is on/);
    expect(discovery).toHaveTextContent("Automatic sweeps off");
    expect(discovery).toHaveTextContent("No sweep ports receive HTTP requests");
    expect(saved.mock.calls[1][0].security.client_auth_enabled).toBe(true);
  });
});
