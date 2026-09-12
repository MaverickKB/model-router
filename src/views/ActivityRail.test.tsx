import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ActivityRail } from "./ActivityRail";

it("uses the same unidentified label for legacy transport-only requests", () => {
  render(
    <ActivityRail
      events={[
        {
          id: "job",
          ts: 0,
          client_id: "shared",
          client: "Shared access",
          requested: "auto",
          status: "denied",
          attempts: [],
          decision: { candidates: [], rejections: [] },
          stream: false,
          caller: {
            id: "caller",
            policy_id: "shared",
            name: "hermes-cli/0.21.0",
            source_address: "192.0.2.40",
            software: "hermes-cli/0.21.0",
            reported_name: "",
            identity_basis: "shared_access",
            last_seen: 0,
            last_path: "/v1/models",
          },
        },
      ]}
      hover={null}
      onHover={vi.fn()}
      onSelect={vi.fn()}
    />,
  );

  expect(
    screen.getByRole("button", { name: /Unidentified caller/ }),
  ).toBeVisible();
  expect(
    screen.queryByText("hermes-cli/0.21.0", { exact: true }),
  ).not.toBeInTheDocument();
});
