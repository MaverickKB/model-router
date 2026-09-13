import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { groupCallerSources } from "../caller-sources";
import { fixtureConfig } from "../test-fixtures";
import type { Client, Config, ObservedCaller } from "../types";
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

const policy: Client = {
  id: "household",
  name: "Household account",
  kind: "person",
  enabled: true,
  route_names: ["writing"],
  engine_ids: [],
  model_patterns: ["*"],
  allow_cloud: false,
  allow_direct_models: false,
  allow_network_auth: false,
  source_networks: [],
  has_key: true,
};

function props(callers: ObservedCaller[], clients: Client[] = []) {
  return {
    config: { ...fixtureConfig(), clients },
    callers,
    clients,
    engines: [],
    activeClient: null,
    onSelect: vi.fn(),
    onDelete: vi.fn(),
    save: vi.fn(async (config: Config) => config),
  };
}

it("selects one source across software and policies, with metadata collapsed inside its details", async () => {
  const input = props(
    [
      observation("requests", { source_port: 51001 }),
      observation("openai", {
        software: "OpenAI/Python/2.24.0",
        source_port: 51002,
        policy_id: policy.id,
        identity_basis: "api_key",
        identity_quality: "policy_key",
        name: policy.name,
      }),
      observation("other-source", { source_address: "192.0.2.31" }),
    ],
    [policy],
  );
  const { rerender } = render(<ClientsView {...input} />);
  const user = userEvent.setup();
  const source = screen.getByRole("button", {
    name: "Inspect source 192.0.2.30",
  });
  expect(
    screen.getAllByRole("button", { name: /^Inspect source/ }),
  ).toHaveLength(2);
  expect(source).toHaveAttribute("aria-pressed", "false");
  expect(
    screen.queryByText("Software: Python requests 2.33.0"),
  ).not.toBeInTheDocument();

  await user.tab();
  expect(source).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(source).toHaveAttribute("aria-pressed", "true");
  const details = screen.getByRole("region", {
    name: "Source details for 192.0.2.30",
  });
  expect(
    within(details).getAllByRole("heading", { name: "192.0.2.30" }),
  ).toHaveLength(1);
  const software = within(details).getByText("Software: OpenAI Python 2.24.0");
  expect(software).toBeVisible();
  expect(
    within(details).getByText("Latest policy: Household account"),
  ).toBeVisible();
  expect(within(details).getByText("192.0.2.30:51002")).not.toBeVisible();
  await user.click(software);
  expect(within(details).getByText("192.0.2.30:51002")).toBeVisible();
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
    screen.getAllByRole("button", { name: "Inspect source 192.0.2.30" }),
  ).toHaveLength(1);
  expect(within(details).getByText("192.0.2.30:52000")).toBeVisible();

  await user.click(
    within(details).getByRole("button", {
      name: "Edit permission policy: Household account",
    }),
  );
  expect(input.onSelect).toHaveBeenLastCalledWith(policy);
  expect(input.save).not.toHaveBeenCalled();
});

it("shows declared names as metadata and keeps the same key on separate source addresses", async () => {
  const input = props(
    [
      observation("named-app", {
        name: "Writing assistant",
        reported_name: "Writing assistant",
        identity_quality: "self_reported",
        client_os: "macOS",
      }),
      observation("named-policy", {
        name: policy.name,
        identity_quality: "policy_key",
        identity_basis: "api_key",
        policy_id: policy.id,
        software: "OpenAI/Python/2.24.0",
      }),
      observation("same-key-another-source", {
        name: policy.name,
        identity_quality: "policy_key",
        identity_basis: "api_key",
        policy_id: policy.id,
        source_address: "192.0.2.31",
      }),
    ],
    [policy],
  );
  render(<ClientsView {...input} />);
  const user = userEvent.setup();
  expect(
    screen.getAllByRole("button", { name: /^Inspect source/ }),
  ).toHaveLength(2);
  await user.click(
    screen.getByRole("button", { name: "Inspect source 192.0.2.30" }),
  );
  const details = screen.getByRole("region", {
    name: "Source details for 192.0.2.30",
  });
  expect(within(details).getByText("Reported names")).toBeVisible();
  expect(
    within(details).getByText("Reported names").nextElementSibling,
  ).toHaveTextContent("Writing assistant");
  expect(within(details).getByText("Reported operating systems")).toBeVisible();
  expect(
    within(details).getByText("Latest policy: Household account"),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /Inspect Writing assistant/ }),
  ).not.toBeInTheDocument();

  await user.click(
    screen.getByRole("button", { name: "Inspect source 192.0.2.31" }),
  );
  const other = screen.getByRole("region", {
    name: "Source details for 192.0.2.31",
  });
  expect(
    within(other).getByText("Latest policy: Household account"),
  ).toBeVisible();
});

it("retains the unknown-credential explanation inside the source request metadata", async () => {
  render(<ClientsView {...props([observation("requests")])} />);
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "Inspect source 192.0.2.30" }),
  );
  await user.click(screen.getByText("Software: Python requests 2.33.0"));
  expect(
    screen.getByText(
      /supplied credentials did not identify a named permission policy/,
    ),
  ).toBeVisible();
  expect(screen.getByText(/Route settings determine access/)).toBeVisible();
});

it("orders sources by address and their metadata by recency without changing observation records", () => {
  const callers = [
    observation("later-address", { source_address: "192.0.2.31" }),
    observation("older", { last_seen: 20 }),
    observation("latest-b", { last_seen: 30 }),
    observation("latest-a", { last_seen: 30 }),
  ];
  const original = structuredClone(callers);
  const grouped = groupCallerSources(callers);
  expect(grouped.map((source) => source.address)).toEqual([
    "192.0.2.30",
    "192.0.2.31",
  ]);
  expect(grouped[0].callers.map((caller) => caller.id)).toEqual([
    "latest-a",
    "latest-b",
    "older",
  ]);
  expect(grouped[0].lastSeen).toBe(30);
  expect(callers).toEqual(original);
});

it("has an explicit empty state without inventing connected clients", () => {
  render(<ClientsView {...props([])} />);
  expect(screen.getByText("No caller traffic yet.")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /^Inspect source/ }),
  ).not.toBeInTheDocument();
});
