import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { Job } from "../types";
import { JobDialog } from "./JobDialog";

const job: Job = {
  id: "job-1",
  ts: 1700000000,
  client_id: "a3f1c2d4e5f60718293a4b5c6d7e8f90",
  client: "Alice",
  requested: "private",
  status: "completed",
  attempts: [],
  decision: { candidates: [], rejections: [] },
  stream: false,
};

it("shows an estimated token count and the account limit that refused a job", () => {
  const { unmount } = render(
    <JobDialog
      job={{
        ...job,
        usage: { prompt_tokens: 12, completion_tokens: 30, estimated: true },
      }}
      onClose={vi.fn()}
    />,
  );
  expect(screen.getByText("≈ 42 tokens")).toBeInTheDocument();
  expect(screen.queryByText(/Refused by account limit/)).toBeNull();
  unmount();
  render(
    <JobDialog
      job={{
        ...job,
        status: "limited",
        limit: { code: "token_budget_exceeded", retry_after: 120 },
      }}
      onClose={vi.fn()}
    />,
  );
  expect(
    screen.getByText("Refused by account limit: token_budget_exceeded"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/tokens$/)).toBeNull();
});
