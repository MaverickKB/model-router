import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fixtureConfig, mockJsonFetch } from "../test-fixtures";
import type { Config } from "../types";
import { AccountsSettings } from "./AccountsSettings";

describe("Accounts settings", () => {
  beforeEach(() => {
    mockJsonFetch(() => ({ accounts: [], devices: [] }));
  });

  it("accounts switch is off by default and the device switch is hidden until accounts are on", async () => {
    const config = fixtureConfig();
    const save = vi.fn(async (value: Config) => value);
    render(
      <AccountsSettings
        config={config}
        save={save}
        baseUrl="http://router.test/v1"
      />,
    );
    const master = screen.getByRole("switch", { name: "User accounts" });
    expect(master).toHaveAttribute("aria-checked", "false");
    expect(
      screen.queryByRole("switch", {
        name: "Allow device pre-registration (dev mode)",
      }),
    ).not.toBeInTheDocument();
    await userEvent.setup().click(master);
    const devices = screen.getByRole("switch", {
      name: "Allow device pre-registration (dev mode)",
    });
    expect(devices).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/Less secure\./)).toBeVisible();
    expect(
      screen.getByText("Changes below are not active until saved."),
    ).toBeVisible();
    expect(save).not.toHaveBeenCalled();
  });

  it("saved summary says user keys are refused while off and shows the portal address when on", () => {
    const config = fixtureConfig();
    const save = vi.fn(async (value: Config) => value);
    const props = { config, save, baseUrl: "http://router.test/v1" };
    const view = render(<AccountsSettings {...props} />);
    const summary = () =>
      screen.getByRole("region", { name: "Saved accounts state" });
    expect(summary()).toHaveTextContent(
      "User accounts off — user keys are refused. Levels and accounts can be prepared now.",
    );
    expect(summary()).not.toHaveTextContent("/portal");
    const on = {
      ...config,
      accounts: { ...config.accounts, enabled: true },
      account_levels: [
        {
          id: "level-1",
          name: "Basic",
          description: "",
          route_names: ["auto"],
          engine_ids: [],
          model_patterns: ["*"],
          allow_cloud: false,
          allow_direct_models: false,
          token_budget: null,
          max_concurrency: null,
        },
      ],
    };
    view.rerender(<AccountsSettings {...props} config={on} />);
    expect(summary()).toHaveTextContent("User accounts on · 1 level");
    expect(summary()).toHaveTextContent("Portal: http://router.test/portal");
  });

  it("stale account settings draft is refused", async () => {
    const config = fixtureConfig();
    const save = vi.fn(async (value: Config) => value);
    const props = { config, save, baseUrl: "http://router.test/v1" };
    const view = render(<AccountsSettings {...props} />);
    const newer = {
      ...config,
      accounts: { ...config.accounts, session_hours: 24 },
    };
    view.rerender(<AccountsSettings {...props} config={newer} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Save account settings" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "account settings changed elsewhere",
    );
    expect(save).not.toHaveBeenCalled();
  });
});
