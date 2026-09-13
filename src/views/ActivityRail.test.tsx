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
    id: "other-source",
    name: "Notebook assistant",
    reported_name: "Notebook assistant",
    source_address: "192.0.2.31",
  },
};

it("explains preserved legacy failures without labeling rejected alternatives as a failed request", async () => {
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
  render(
    <ActivityRail
      events={[
        { ...failed, decision },
        { ...completed, decision },
      ]}
      hover={null}
      onHover={vi.fn()}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getAllByText("Missing capability: tools")).toHaveLength(1);
  expect(screen.getByText("chat-model")).toBeVisible();
  await userEvent
    .setup()
    .type(screen.getByRole("textbox", { name: "Search requests" }), "tools");
  expect(screen.getByText("Missing capability: tools")).toBeVisible();
  expect(screen.queryByText("chat-model")).not.toBeInTheDocument();
});

it("shows source evidence and the failure reason before opening request details", async () => {
  const onSelect = vi.fn();
  render(
    <ActivityRail
      events={[failed]}
      hover={null}
      onHover={vi.fn()}
      onSelect={onSelect}
    />,
  );

  const card = screen.getByRole("button", {
    name: /192\.0\.2\.30.*writing/,
  });
  expect(within(card).getByText("192.0.2.30")).toBeVisible();
  expect(
    within(card).getByText("192.0.2.30:51002 · Python requests 2.33.0"),
  ).toBeVisible();
  expect(within(card).getByText(error)).toBeVisible();
  expect(within(card).queryByText("Default policy")).not.toBeInTheDocument();
  expect(within(card).queryByText("No model selected")).not.toBeInTheDocument();
  expect(onSelect).not.toHaveBeenCalled();

  await userEvent.setup().click(card);
  expect(onSelect).toHaveBeenCalledOnce();
  expect(onSelect).toHaveBeenCalledWith(failed);
});

it.each(["192.0.2.30", "TOOLS"])(
  "finds the failed request by source or capability: %s",
  async (query) => {
    render(
      <ActivityRail
        events={[failed, completed]}
        hover={null}
        onHover={vi.fn()}
        onSelect={vi.fn()}
      />,
    );
    const user = userEvent.setup();
    const search = screen.getByRole("textbox", { name: "Search requests" });
    await user.type(search, query);

    expect(
      screen.getByRole("button", { name: /192\.0\.2\.30.*writing/ }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", {
        name: /Notebook assistant.*conversation/,
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(error)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Errors" }));
    expect(screen.getByText(error)).toBeVisible();
    await user.clear(search);
    await user.click(screen.getByRole("button", { name: "All" }));
    expect(
      screen.getByRole("button", { name: /Notebook assistant.*conversation/ }),
    ).toBeVisible();
  },
);

it("does not turn a saved permission policy into a recorded caller when evidence is absent", () => {
  render(
    <ActivityRail
      events={[{ ...failed, caller: null }]}
      hover={null}
      onHover={vi.fn()}
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("Caller not recorded")).toBeVisible();
  expect(screen.queryByText("Default policy")).not.toBeInTheDocument();
  expect(screen.getByText(error)).toBeVisible();
});
