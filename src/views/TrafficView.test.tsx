import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { Job } from "../types";
import { TrafficView } from "./TrafficView";

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ active: false, entries: 0 }),
      }),
    ),
  );
}

const job: Job = {
  id: "traffic-tokens",
  ts: 1700000000,
  client_id: "client-a",
  client: "Shared access",
  requested: "auto",
  status: "completed",
  attempts: [],
  decision: { candidates: [], rejections: [] },
  stream: true,
  performance: {
    stream: true,
    upstream_ms: 1200,
    first_chunk_ms: 400,
    generation_ms: 800,
    completion_tokens: 321,
    tokens_estimated: false,
    tokens_per_second: 40.1,
  },
};

it("shows completion tokens from performance when no usage block exists", () => {
  stubFetch();
  render(<TrafficView events={[job]} engines={[]} />);
  expect(screen.getByText("321 out")).toBeInTheDocument();
});

it("shows prompt plus completion when a usage block exists", () => {
  stubFetch();
  render(
    <TrafficView
      events={[
        {
          ...job,
          usage: {
            prompt_tokens: 1200,
            completion_tokens: 340,
            estimated: false,
          },
        },
      ]}
      engines={[]}
    />,
  );
  expect(screen.getByText("1,540")).toBeInTheDocument();
});

it("marks an estimated usage total", () => {
  stubFetch();
  render(
    <TrafficView
      events={[
        {
          ...job,
          usage: {
            prompt_tokens: 1200,
            completion_tokens: 340,
            estimated: true,
          },
        },
      ]}
      engines={[]}
    />,
  );
  expect(screen.getByText("≈ 1,540")).toBeInTheDocument();
});

it("marks token counts that are estimates", () => {
  stubFetch();
  render(
    <TrafficView
      events={[
        {
          ...job,
          performance: {
            ...job.performance!,
            completion_tokens: 40,
            tokens_estimated: true,
          },
        },
      ]}
      engines={[]}
    />,
  );
  expect(screen.getByText("≈ 40 out")).toBeInTheDocument();
});
