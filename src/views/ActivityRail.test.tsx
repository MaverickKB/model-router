import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { Job } from "../types";
import { ActivityRail } from "./ActivityRail";

const error =
  "No permitted destination is configured for the required capabilities: tools. Check the engine capability settings.";
const failed: Job = {
  id: "failed-request",
  ts: 1800000000,
  client_id: "default-policy",
  client: "Default policy",
  requested: "writing",
  status: "failed",
  attempts: [],
  decision: { candidates: [], rejections: [], error },
  elapsed_ms: 5,
  http_status: 400,
  stream: true,
  caller: {
    id: "observed-source",
    policy_id: null,
    name: "python-requests/2.33.0",
    source_address: "192.0.2.30",
    source_port: 51002,
    software: "python-requests/2.33.0",
    reported_name: "",
    identity_basis: "unassigned",
    last_seen: 1800000000,
    last_path: "/v1/chat/completions",
  },
};
const completed: Job = {
  ...failed,
  id: "completed-request",
  requested: "conversation",
  model: "chat-model",
  status: "completed",
  decision: { candidates: [], rejections: [] },
  caller: {
    ...failed.caller!,
    id: "other-software",
    name: "Notebook assistant",
    reported_name: "Notebook assistant",
    software: "OpenAI/Python/2.24.0",
    policy_id: "paid-policy",
  },
};
const props = (events: Job[]) => ({
  events,
  hover: null,
  onHover: vi.fn(),
  onSelect: vi.fn(),
});
const group = (address = "192.0.2.30") =>
  screen.getByRole("region", { name: `Request history for ${address}` });
const toggle = (address = "192.0.2.30") =>
  within(group(address)).getByRole("button", { expanded: false });

it("collapses 99 requests from one source into one summary while preserving all 99 inspectable requests", async () => {
  const events = Array.from({ length: 99 }, (_, index) => ({
    ...completed,
    id: `request-${index}`,
    ts: completed.ts + index,
    caller:
      index % 2
        ? completed.caller
        : { ...failed.caller!, policy_id: "another-policy" },
  }));
  const view = props(events);
  const { container } = render(<ActivityRail {...view} />);
  expect(
    screen.getByRole("heading", { name: "Request history" }),
  ).toBeVisible();
  expect(screen.getByText("99 recent requests · 1 source")).toBeVisible();
  expect(screen.getAllByRole("region")).toHaveLength(1);
  expect(screen.getAllByText("192.0.2.30")).toHaveLength(1);
  expect(within(group()).getByText("99 completed")).toBeVisible();
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(0);
  const controlledHistory = document.getElementById(
    toggle().getAttribute("aria-controls")!,
  );
  expect(controlledHistory).toBeInTheDocument();
  expect(controlledHistory).not.toBeVisible();
  await userEvent.setup().click(toggle());
  const rows = [
    ...container.querySelectorAll<HTMLButtonElement>("[data-job-id]"),
  ];
  expect(rows).toHaveLength(99);
  expect(new Set(rows.map((row) => row.dataset.jobId))).toEqual(
    new Set(events.map((job) => job.id)),
  );
  expect(screen.getAllByText("192.0.2.30")).toHaveLength(1);
  for (const row of rows) {
    row.click();
    expect(view.onSelect).toHaveBeenLastCalledWith(
      events.find((job) => job.id === row.dataset.jobId),
    );
  }
  expect(view.onSelect).toHaveBeenCalledTimes(99);
});

it("keeps source status totals and the latest failure visible before expansion", () => {
  const running = { ...completed, id: "running", status: "running" };
  const cancelled = { ...completed, id: "cancelled", status: "cancelled" };
  const other = {
    ...completed,
    id: "other-source",
    caller: { ...completed.caller!, source_address: "192.0.2.31" },
  };
  const { container } = render(
    <ActivityRail {...props([failed, completed, running, cancelled, other])} />,
  );
  expect(screen.getByText("5 recent requests · 2 sources")).toBeVisible();
  const source = group();
  for (const value of [
    "4 requests",
    "1 active",
    "1 error",
    "1 completed",
    "1 cancelled",
  ])
    expect(within(source).getByText(value)).toBeVisible();
  expect(within(source).getByText(error)).toBeVisible();
  expect(within(source).queryByText("Default policy")).not.toBeInTheDocument();
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(0);
  expect(group("192.0.2.31")).toBeVisible();
});

it("keeps a named caller endpoint together when its reported device moves addresses", () => {
  const first = {
    ...completed,
    id: "before-dhcp",
    caller: {
      ...completed.caller!,
      source_key: "device:abc123",
      source_label: "Studio Mac",
      source_label_source: "operator" as const,
      source_identity_quality: "reported_device" as const,
      source_address: "192.0.2.30",
    },
  };
  const second = {
    ...completed,
    id: "after-dhcp",
    ts: completed.ts + 1,
    caller: { ...first.caller, source_address: "192.0.2.31" },
  };
  render(<ActivityRail {...props([first, second])} />);

  expect(screen.getByText("2 recent requests · 1 source")).toBeVisible();
  expect(group("Studio Mac")).toBeVisible();
  expect(within(group("Studio Mac")).getByText("192.0.2.31")).toBeVisible();
});

it("shows preserved legacy failure reasons without treating a completed request's rejected alternative as an error", async () => {
  const decision = {
    candidates: [],
    rejections: [
      {
        engine: "Engine",
        engine_id: "engine",
        model: "model",
        tier: "primary",
        reason: "Missing capability: tools",
      },
    ],
  };
  const other = {
    ...completed,
    caller: { ...completed.caller!, source_address: "192.0.2.31" },
  };
  render(
    <ActivityRail
      {...props([
        { ...failed, decision },
        { ...other, decision },
      ])}
    />,
  );
  expect(screen.getAllByText("Missing capability: tools")).toHaveLength(1);
  expect(within(group("192.0.2.31")).getByText("0 errors")).toBeVisible();
  await userEvent.setup().click(toggle("192.0.2.31"));
  expect(screen.getByText("chat-model")).toBeVisible();
  expect(
    within(group("192.0.2.31")).queryByText("Missing capability: tools"),
  ).not.toBeInTheDocument();
});

it("reveals matched request rows for Errors and Active, preserving whole-source totals", async () => {
  const running = {
    ...completed,
    id: "running-request",
    status: "running",
    requested: "voice",
  };
  const view = props([failed, completed, running]);
  const { container } = render(<ActivityRail {...view} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Errors" }));
  expect(screen.getByText("1 matching request")).toBeVisible();
  expect(within(group()).getByText("1 matching of 3 requests")).toBeVisible();
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(1);
  const failedRow = container.querySelector<HTMLButtonElement>(
    '[data-job-id="failed-request"]',
  )!;
  expect(within(failedRow).getByText(error)).toBeVisible();
  await user.click(failedRow);
  expect(view.onSelect).toHaveBeenCalledWith(failed);
  await user.click(screen.getByRole("button", { name: "Active" }));
  expect(container.querySelector('[data-job-id="failed-request"]')).toBeNull();
  expect(
    container.querySelector('[data-job-id="running-request"]'),
  ).toBeVisible();
  // The earlier failure stays visible in the source summary while active work is inspected.
  expect(within(group()).getByText(error)).toBeVisible();
  await user.click(screen.getByRole("button", { name: "All" }));
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(0);
});

it.each([
  ["192.0.2.30", 2],
  ["OpenAI/Python", 1],
  ["conversation", 1],
  ["chat-model", 1],
  ["TOOLS", 1],
])(
  "searches request evidence and exposes exact matches: %s",
  async (query, count) => {
    const { container } = render(
      <ActivityRail {...props([failed, completed])} />,
    );
    await userEvent
      .setup()
      .type(screen.getByRole("textbox", { name: "Search requests" }), query);
    expect(container.querySelectorAll("[data-job-id]")).toHaveLength(count);
    expect(
      screen.getByText(
        `${count} matching ${count === 1 ? "request" : "requests"}`,
      ),
    ).toBeVisible();
    if (query === "TOOLS")
      expect(
        container.querySelector('[data-job-id="failed-request"]'),
      ).toBeVisible();
  },
);

it("retains disclosure and row focus across refreshed objects and additional requests", async () => {
  const view = props([completed]);
  const { container, rerender } = render(<ActivityRail {...view} />);
  const user = userEvent.setup();
  await user.click(toggle());
  const row = container.querySelector<HTMLButtonElement>(
    '[data-job-id="completed-request"]',
  )!;
  row.focus();
  expect(view.onHover).toHaveBeenLastCalledWith(completed);
  expect(row).toHaveFocus();
  rerender(
    <ActivityRail
      {...view}
      events={[
        { ...completed, id: "new-request", ts: completed.ts + 1 },
        { ...completed },
      ]}
    />,
  );
  expect(within(group()).getByRole("button", { expanded: true })).toBeVisible();
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(2);
  expect(row).toHaveFocus();
  await user.unhover(row);
  await user.click(within(group()).getByRole("button", { expanded: true }));
  expect(container.querySelectorAll("[data-job-id]")).toHaveLength(0);
});

it("does not infer a source or person from saved policy names when connection evidence is missing", async () => {
  const view = props([{ ...failed, caller: null }]);
  const { container } = render(<ActivityRail {...view} />);
  expect(screen.getByText("1 recent request · 0 sources")).toBeVisible();
  expect(screen.getByText("1 without a recorded source")).toBeVisible();
  expect(screen.getByText("Source not recorded")).toBeVisible();
  expect(screen.queryByText("Default policy")).not.toBeInTheDocument();
  expect(screen.getByText(error)).toBeVisible();
  await userEvent.setup().click(toggle("Source not recorded"));
  const row = container.querySelector<HTMLButtonElement>("[data-job-id]")!;
  await userEvent.setup().click(row);
  expect(view.onSelect).toHaveBeenCalledWith(view.events[0]);
});

it("preserves console-test identity as request context without substituting it for a source", async () => {
  const test = {
    ...completed,
    caller: {
      ...completed.caller!,
      name: "Operator test",
      identity_basis: "operator_test" as const,
      source_address: "127.0.0.1",
    },
  };
  render(<ActivityRail {...props([test])} />);
  expect(screen.getByText("127.0.0.1")).toBeVisible();
  expect(
    screen.queryByText("Operator test · Console test"),
  ).not.toBeInTheDocument();
  await userEvent.setup().click(toggle("127.0.0.1"));
  expect(screen.getByText("Operator test · Console test")).toBeVisible();
  expect(screen.getAllByText("127.0.0.1")).toHaveLength(1);
});
