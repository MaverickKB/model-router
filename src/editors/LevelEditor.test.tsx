import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { fixtureAccount, fixtureConfig, mockJsonFetch } from "../test-fixtures";
import type { AccountLevel, Config } from "../types";
import { newLevel } from "./defaults";
import { LevelEditor } from "./LevelEditor";

const level = (fields: Partial<AccountLevel> = {}): AccountLevel => ({
  ...newLevel(),
  id: "level-1",
  name: "Basic",
  ...fields,
});

describe("Level editor", () => {
  it("new level presets auto and unlimited budget/concurrency save as null", async () => {
    const config = fixtureConfig();
    const save = vi.fn(async (value: Config) => value);
    const user = userEvent.setup();
    render(
      <LevelEditor
        initial={newLevel()}
        config={config}
        save={save}
        inUse={0}
        onClose={() => {}}
      />,
    );
    await user.type(screen.getByPlaceholderText("Name this level"), "Basic");
    await user.click(screen.getByRole("button", { name: "Save level" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const saved = save.mock.calls[0][0].account_levels[0];
    expect(saved.name).toBe("Basic");
    expect(saved.route_names).toEqual(["auto"]);
    expect(saved.token_budget).toBeNull();
    expect(saved.max_concurrency).toBeNull();
    expect(save.mock.calls[0][0].accounts).toEqual(config.accounts);
  });

  it("window preset writes seconds", async () => {
    const config = fixtureConfig();
    config.account_levels = [level()];
    const save = vi.fn(async (value: Config) => value);
    const user = userEvent.setup();
    render(
      <LevelEditor
        initial={config.account_levels[0]}
        config={config}
        save={save}
        inUse={0}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Budget per window" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Window" }),
      "604800",
    );
    await user.click(screen.getByRole("button", { name: "Limit" }));
    await user.click(screen.getByRole("button", { name: "Save level" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const saved = save.mock.calls[0][0].account_levels[0];
    expect(saved.token_budget).toEqual({
      max_tokens: 100000,
      window_seconds: 604800,
    });
    expect(saved.max_concurrency).toBe(2);
  });

  it("clicking Limit again keeps the configured concurrency", async () => {
    const config = fixtureConfig();
    config.account_levels = [level({ max_concurrency: 10 })];
    const save = vi.fn(async (value: Config) => value);
    const user = userEvent.setup();
    render(
      <LevelEditor
        initial={config.account_levels[0]}
        config={config}
        save={save}
        inUse={0}
        onClose={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Limit" }));
    expect(screen.getByLabelText("Concurrent requests")).toHaveValue(10);
    await user.click(screen.getByRole("button", { name: "Save level" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].account_levels[0].max_concurrency).toBe(10);
  });

  it("remove level with accounts opens the move dialog and calls PUT per account then saves without the level", async () => {
    const config = fixtureConfig();
    const other = level({ id: "level-2", name: "Other" });
    config.account_levels = [level(), other];
    const order: string[] = [];
    mockJsonFetch((call) => {
      order.push(`${call.method} ${call.url}`);
      if (call.method === "GET")
        return {
          accounts: [
            fixtureAccount({ id: "a1", level_id: "level-1" }),
            fixtureAccount({ id: "a2", level_id: "level-1", username: "eli" }),
            fixtureAccount({ id: "a3", level_id: "level-2", username: "kim" }),
          ],
        };
      return { account: fixtureAccount() };
    });
    const save = vi.fn(async (value: Config) => {
      order.push("save");
      return value;
    });
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <LevelEditor
        initial={config.account_levels[0]}
        config={config}
        save={save}
        inUse={2}
        onClose={onClose}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Remove level" }));
    expect(save).not.toHaveBeenCalled();
    expect(await screen.findByText(/2 accounts use this level/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Move accounts and remove level" }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(order).toEqual([
      "GET /api/v1/accounts",
      "PUT /api/v1/accounts/a1",
      "PUT /api/v1/accounts/a2",
      "save",
    ]);
    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls;
    expect(JSON.parse(calls[1][1].body)).toEqual({ level_id: "level-2" });
    expect(save.mock.calls[0][0].account_levels).toEqual([other]);
  });
});
