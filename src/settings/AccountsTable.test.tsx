import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fixtureAccount, fixtureConfig, mockJsonFetch } from "../test-fixtures";
import type { AccountLevel } from "../types";
import { AccountsTable } from "./AccountsTable";
import { DevicesTable } from "./DevicesTable";

const level: AccountLevel = {
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
};

describe("Accounts table", () => {
  it("create account shows the activation link once with copy and the disabled note when accounts are off", async () => {
    const config = fixtureConfig();
    config.account_levels = [level];
    const calls = mockJsonFetch(() => ({
      account: fixtureAccount({ status: "pending", activation_pending: true }),
      activation: {
        token: "mra_secret",
        url: "http://router.test/portal/activate#token=mra_secret",
        expires: 1800259200,
      },
    }));
    const reload = vi.fn(async () => {});
    const user = userEvent.setup();
    render(
      <AccountsTable config={config} accounts={[]} reload={reload} error="" />,
    );
    expect(screen.getByText("No accounts yet.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Add account" }));
    await user.type(screen.getByLabelText(/^Username/), "Dana");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Activation link",
    });
    expect(calls).toEqual([
      {
        url: "/api/v1/accounts",
        method: "POST",
        body: { username: "dana", name: null, level_id: "level-1" },
      },
    ]);
    expect(reload).toHaveBeenCalledOnce();
    expect(
      within(dialog).getByText(
        "http://router.test/portal/activate#token=mra_secret",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Shown once. Expires in 72 hours."),
    ).toBeVisible();
    expect(
      within(dialog).getByText(
        "Accounts are disabled — this link works once accounts are enabled.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Copy link" }),
    ).toBeVisible();
    expect(
      within(dialog).queryByLabelText(/^Username/),
    ).not.toBeInTheDocument();
  });

  it("suspend calls PUT with status suspended", async () => {
    const config = fixtureConfig();
    config.account_levels = [level];
    const account = fixtureAccount();
    const calls = mockJsonFetch((call) =>
      call.method === "PUT"
        ? { account: { ...account, status: "suspended" } }
        : {
            account: calls.some((c) => c.method === "PUT")
              ? { ...account, status: "suspended" }
              : account,
            keys: [
              {
                id: "key-1",
                name: "laptop",
                created: 1800000000,
                last_used: null,
              },
            ],
            devices: [],
            usage_windows: [],
          },
    );
    const reload = vi.fn(async () => {});
    const user = userEvent.setup();
    render(
      <AccountsTable
        config={config}
        accounts={[account]}
        reload={reload}
        error=""
      />,
    );
    const row = screen.getByRole("button", { name: "Open account Dana" });
    expect(row).toHaveTextContent("42,100 of 200,000 tokens");
    await user.click(row);
    expect(await screen.findByText("laptop")).toBeVisible();
    const toggle = screen.getByRole("switch", { name: "Account enabled" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      "GET /api/v1/accounts/acct-1",
      "PUT /api/v1/accounts/acct-1",
      "GET /api/v1/accounts/acct-1",
    ]);
    expect(calls[1].body).toEqual({ status: "suspended" });
    expect(
      screen.getByRole("switch", { name: "Account enabled" }),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("device row shows the precedence note and disable calls PUT", async () => {
    const config = fixtureConfig();
    config.accounts.enabled = true;
    config.accounts.device_registration_enabled = true;
    const device = {
      id: "dev-1",
      account_id: "acct-1",
      account_name: "Dana",
      address: "192.0.2.40",
      name: "bench",
      enabled: true,
      created: 1800000000,
      last_matched: null,
      shadows: [
        { client_id: "c1", client_name: "LAN policy", network: "192.0.2.0/24" },
      ],
    };
    const calls = mockJsonFetch((call) =>
      call.method === "PUT"
        ? { device: { ...device, enabled: false } }
        : {
            devices: [
              calls.some((c) => c.method === "PUT")
                ? { ...device, enabled: false }
                : device,
            ],
          },
    );
    const user = userEvent.setup();
    render(<DevicesTable config={config} accounts={[]} />);
    expect(
      await screen.findByText(
        /Takes precedence over LAN policy \(192\.0\.2\.0\/24\)/,
      ),
    ).toBeVisible();
    expect(screen.getByText("Overrides policy")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Disable device bench" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Enable device bench" }),
      ).toBeVisible(),
    );
    expect(calls[1]).toEqual({
      url: "/api/v1/devices/dev-1",
      method: "PUT",
      body: { enabled: false },
    });
  });
});
