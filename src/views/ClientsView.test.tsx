import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { fixtureConfig } from "../test-fixtures";
import type { Config, ObservedCaller } from "../types";
import { ClientsView } from "./ClientsView";

function observation(
  id: string,
  fields: Partial<ObservedCaller> = {},
): ObservedCaller {
  return {
    id,
    name: "Unidentified caller",
    policy_id: null,
    source_address: "192.0.2.30",
    source_port: 50000,
    software: "python-requests/2.33.0",
    reported_name: "",
    identity_basis: "unassigned",
    last_seen: 1800000000,
    last_path: "/v1/chat/completions",
    ...fields,
  };
}

function props(callers: ObservedCaller[]) {
  return {
    config: fixtureConfig(),
    callers,
    clients: [],
    engines: [],
    activeClient: null,
    onSelect: vi.fn(),
    onDelete: vi.fn(),
    save: vi.fn(async (config: Config) => config),
  };
}

it("groups a source once and keeps each transport observation inspectable", async () => {
  const input = props([
    observation("requests", { source_port: 51001 }),
    observation("openai", {
      software: "OpenAI/Python/2.24.0",
      source_port: 51002,
    }),
    observation("other-source", { source_address: "192.0.2.31" }),
  ]);
  const { rerender } = render(<ClientsView {...input} />);
  const user = userEvent.setup();
  const source = screen.getByRole("button", {
    name: /192\.0\.2\.30 2 observations/,
  });
  expect(source).toHaveAttribute("aria-expanded", "false");
  expect(
    screen.getByRole("button", { name: /192\.0\.2\.31 1 observation/ }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /Inspect Python requests/ }),
  ).not.toBeInTheDocument();

  await user.tab();
  expect(source).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(source).toHaveAttribute("aria-expanded", "true");
  await user.click(
    screen.getByRole("button", {
      name: "Inspect OpenAI Python 2.24.0 from 192.0.2.30",
    }),
  );
  const details = screen.getByRole("region", {
    name: "Connection details for Unidentified caller",
  });
  expect(within(details).getByText("192.0.2.30:51002")).toBeVisible();
  expect(within(details).getByText("OpenAI Python 2.24.0")).toBeVisible();
  expect(input.save).not.toHaveBeenCalled();

  rerender(
    <ClientsView
      {...input}
      callers={input.callers.map((caller) =>
        caller.id === "openai" ? { ...caller, source_port: 52000 } : caller,
      )}
    />,
  );
  expect(
    screen.getAllByRole("button", { name: /192\.0\.2\.30 2 observations/ }),
  ).toHaveLength(1);
  expect(within(details).getByText("192.0.2.30:52000")).toBeVisible();

  await user.click(
    screen.getByRole("button", { name: "Back to caller sources" }),
  );
  await user.click(
    screen.getByRole("button", {
      name: "Inspect Python requests 2.33.0 from 192.0.2.30",
    }),
  );
  expect(screen.getByText("192.0.2.30:51001")).toBeVisible();
});

it("retains reported application and key policy names inside their source", async () => {
  render(
    <ClientsView
      {...props([
        observation("named-app", {
          name: "Writing assistant",
          reported_name: "Writing assistant",
          identity_quality: "self_reported",
        }),
        observation("named-policy", {
          name: "Household account",
          identity_quality: "policy_key",
          identity_basis: "api_key",
          policy_id: "household",
        }),
      ])}
    />,
  );
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: /192\.0\.2\.30 2 observations/ }),
  );
  expect(
    screen.getByRole("button", {
      name: "Inspect Writing assistant from 192.0.2.30",
    }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", {
      name: "Inspect Household account from 192.0.2.30",
    }),
  ).toBeVisible();
});

it("has an explicit empty state without inventing connected clients", () => {
  render(<ClientsView {...props([])} />);
  expect(screen.getByText("No caller traffic yet.")).toBeVisible();
  expect(
    screen.queryByRole("button", { expanded: false }),
  ).not.toBeInTheDocument();
});
