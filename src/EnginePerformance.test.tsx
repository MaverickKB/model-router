import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "./App";
import { newEngine } from "./editors/defaults";
import { requestTimingLabel } from "./performance-format";
import { fixtureConfig, mockJsonFetch } from "./test-fixtures";
import type { EngineView, PerformanceRow } from "./types";

afterEach(() => {
  vi.unstubAllGlobals();
});

function row(overrides: Partial<PerformanceRow>): PerformanceRow {
  return {
    engine_id: "engine-1",
    engine: "Serving API",
    model: "model",
    in_catalog: true,
    served: 0,
    failed: 0,
    failure_rate: null,
    first_seen: 1800000000,
    last_seen: 1800000000,
    stream: {
      samples: 0,
      first_chunk_ms_p50: null,
      first_chunk_ms_p95: null,
      tokens_per_second_p50: null,
      estimated_samples: 0,
    },
    non_stream: { samples: 0, upstream_ms_p50: null, upstream_ms_p95: null },
    ...overrides,
  };
}

function renderEngine(rowsForHours: (hours: string) => PerformanceRow[]) {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  const engine: EngineView = {
    ...newEngine(),
    id: "engine-1",
    name: "Serving API",
    base_url: "http://192.0.2.8:8000/v1",
    models: [{ id: "replacement", capabilities: ["chat"] }],
    has_credential: false,
    status: "available",
    checked_at: 1800000000,
    observed_at: 1800000000,
    latency_ms: 4,
    error: "",
    inflight: 0,
    last_success: null,
  } as EngineView;
  const config = fixtureConfig();
  config.engines = [engine];
  const network = {
    phase: "not_started",
    hosts: [],
    error: "",
    completed_at: 0,
  };
  return mockJsonFetch((call) => {
    if (call.url === "/api/v1/state") {
      return {
        setup_required: false,
        config,
        engines: [engine],
        clients: [],
        events: [],
        server_time: 0,
        base_url: "http://router.test/v1",
        environment_label: "Fixture",
        discovery: { scanning: false, last_scan: 0, error: "", pending: [] },
        network,
      };
    }
    if (call.url.startsWith("/api/v1/performance")) {
      const hours = new URL(call.url, "http://router.test").searchParams.get(
        "hours",
      )!;
      return {
        window_hours: Number(hours),
        since: 0,
        generated_at: 0,
        rows: rowsForHours(hours),
      };
    }
    if (call.url.startsWith("/api/v1/network")) return network;
    return {};
  });
}

it("compares a replacement model with the model it replaced in the engine card", async () => {
  const calls = renderEngine((hours) => {
    if (hours !== "24") return [];
    return [
      row({
        model: "replacement",
        in_catalog: true,
        served: 12,
        failed: 1,
        failure_rate: 0.0769,
        last_seen: 1800000900,
        stream: {
          samples: 12,
          first_chunk_ms_p50: 240,
          first_chunk_ms_p95: 1320,
          tokens_per_second_p50: 41.2,
          estimated_samples: 0,
        },
      }),
      row({
        model: "original",
        in_catalog: false,
        served: 30,
        non_stream: {
          samples: 30,
          upstream_ms_p50: 2100,
          upstream_ms_p95: 4800,
        },
      }),
    ];
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(
    await screen.findByRole("button", { name: "Expand Serving API" }),
  );
  const section = await screen.findByRole("region", { name: "Performance" });

  const replacement = await waitFor(() => {
    const found = section.querySelector('[data-model="replacement"]');
    expect(found).not.toBeNull();
    return found as HTMLElement;
  });
  expect(within(replacement).getByText("Listed now")).toBeTruthy();
  expect(
    within(replacement).getByText(/12 served · 1 failed \(8%\)/),
  ).toBeTruthy();
  expect(within(replacement).getByText("240 ms")).toBeTruthy();
  expect(within(replacement).getByText("p95 1.3 s")).toBeTruthy();
  expect(within(replacement).getByText("41 tok/s")).toBeTruthy();

  const original = section.querySelector(
    '[data-model="original"]',
  ) as HTMLElement;
  expect(within(original).getByText("No longer listed")).toBeTruthy();
  expect(within(original).getByText(/30 served · no failures/)).toBeTruthy();
  expect(within(original).getByText("No streamed requests")).toBeTruthy();
  expect(within(original).getByText("2.1 s")).toBeTruthy();

  await user.click(within(section).getByRole("button", { name: "7d" }));
  expect(
    await within(section).findByText(
      "No requests reached this engine in the last 7 days.",
    ),
  ).toBeTruthy();
  expect(
    within(section)
      .getByRole("button", { name: "7d" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  const windows: string[] = [];
  for (const call of calls) {
    if (call.url.startsWith("/api/v1/performance")) windows.push(call.url);
  }
  expect(windows).toContain("/api/v1/performance?hours=24");
  expect(windows).toContain("/api/v1/performance?hours=168");
});

it("marks generation rates measured from estimated token counts", async () => {
  renderEngine(() => [
    row({
      model: "replacement",
      served: 2,
      stream: {
        samples: 2,
        first_chunk_ms_p50: 90,
        first_chunk_ms_p95: 95,
        tokens_per_second_p50: 7.25,
        estimated_samples: 1,
      },
    }),
  ]);
  const user = userEvent.setup();
  render(<App />);
  await user.click(
    await screen.findByRole("button", { name: "Expand Serving API" }),
  );
  expect(await screen.findByText("≈ 7.3 tok/s")).toBeTruthy();
});

it("describes one request's served timing", () => {
  expect(
    requestTimingLabel({
      stream: true,
      upstream_ms: 1800,
      first_chunk_ms: 310,
      generation_ms: 1490,
      completion_tokens: 60,
      tokens_estimated: true,
      tokens_per_second: 40.3,
    }),
  ).toBe("First chunk 310 ms · ≈ 40 tok/s · ≈ 60 completion tokens");
  expect(
    requestTimingLabel({
      stream: false,
      upstream_ms: 2450,
      first_chunk_ms: null,
      generation_ms: null,
      completion_tokens: 12,
      tokens_estimated: false,
      tokens_per_second: null,
    }),
  ).toBe("Engine response 2.5 s · 12 completion tokens");
});
