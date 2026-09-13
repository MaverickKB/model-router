import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { newClient } from "../editors/defaults";
import type { Config } from "../types";
import { AccessSettings } from "./AccessSettings";

import { fixtureConfig } from "../test-fixtures";

describe("Access settings", () => {
  it("changes operator access without inventing a caller policy", async () => {
    const user = userEvent.setup();
    const config = fixtureConfig();
    config.security.operator_auth_enabled = true;
    config.clients = [
      {
        ...newClient(),
        name: "Installed agent",
        allow_cloud: true,
        has_key: true,
      },
    ];
    const save = vi.fn(async (value: Config) => ({ ...value, revision: 2 }));
    render(
      <AccessSettings
        config={config}
        operatorUrl="http://localhost:18790"
        save={save}
        onSignOut={() => {}}
      />,
    );
    await user.click(screen.getByRole("switch", { name: "Operator sign-in" }));
    await user.click(
      screen.getByRole("button", { name: "Save access settings" }),
    );
    await screen.findByText("Access settings saved");
    const saved = save.mock.calls[0][0];
    expect(saved.security.operator_auth_enabled).toBe(false);
    expect(saved.security.client_auth_enabled).toBe(true);
    expect(saved.clients[0]).toEqual(config.clients[0]);
    expect(saved.clients).toHaveLength(1);
  });

  it("reuses the selected shared policy without creating duplicate clients", async () => {
    const config = fixtureConfig();
    const policy = {
      ...newClient(),
      name: "Existing shared permissions",
      route_names: ["writing"],
    };
    config.clients = [policy];
    config.security.client_auth_enabled = false;
    config.security.anonymous_client_id = policy.id;
    const save = vi.fn(async (c: Config) => c);
    render(
      <AccessSettings
        config={config}
        operatorUrl="http://localhost:18790"
        save={save}
        onSignOut={() => {}}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Save access settings" }),
    );
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save.mock.calls[0][0].clients).toEqual([policy]);
  });

  it("prevents a stale access draft from overwriting a newer policy", async () => {
    const config = fixtureConfig();
    const save = vi.fn(async (c: Config) => c);
    const props = {
      config,
      operatorUrl: "http://localhost:18790",
      save,
      onSignOut: () => {},
    };
    const view = render(<AccessSettings {...props} />);
    const newer = {
      ...config,
      security: { ...config.security, client_auth_enabled: false },
    };
    view.rerender(<AccessSettings {...props} config={newer} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Save access settings" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("changed");
    expect(save).not.toHaveBeenCalled();
  });
});
