import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { newEngine } from "./editors/defaults";
import { EngineCard } from "./EngineCard";
import type { EngineView } from "./types";

function engineView(overrides: Partial<EngineView> = {}): EngineView {
  return {
    ...newEngine(),
    name: "Serving endpoint",
    base_url: "https://completion.example/v1",
    models: [
      {
        id: "configured-model",
        capabilities: ["text"],
        context_length: null,
      },
    ],
    has_credential: false,
    status: "available",
    checked_at: 1,
    observed_at: 1,
    latency_ms: null,
    error: "",
    inflight: 0,
    last_success: null,
    ...overrides,
  };
}

function renderCard(engine: EngineView) {
  return render(
    <EngineCard
      engine={engine}
      onEdit={vi.fn()}
      onChange={vi.fn(async () => undefined)}
      onRefresh={vi.fn(async () => undefined)}
      onRemove={vi.fn(async () => undefined)}
      onMerge={vi.fn()}
    />,
  );
}

it("labels a declared inventory as configured rather than discovered catalog data", async () => {
  renderCard(
    engineView({
      model_inventory_source: "declared",
      declared_models: ["configured-model"],
      model_patterns: ["configured-model"],
      status: "configured",
    }),
  );
  const user = userEvent.setup();

  expect(
    screen.getByText(/Declared identities pending first request/),
  ).toBeVisible();
  await user.click(
    screen.getByRole("button", {
      name: /Declared identities pending first request/,
    }),
  );
  expect(screen.getByText("Declared")).toBeVisible();
  expect(screen.getByText(/Endpoint checked/)).toBeVisible();
  expect(screen.queryByText("Discovered")).not.toBeInTheDocument();
  expect(screen.queryByText(/Catalog checked/)).not.toBeInTheDocument();
});

it("keeps catalog language for catalog-backed engines", async () => {
  renderCard(engineView());
  const user = userEvent.setup();

  expect(screen.getByText(/Catalog available/)).toBeVisible();
  await user.click(screen.getByRole("button", { name: /Catalog available/ }));
  expect(screen.getByText("Discovered")).toBeVisible();
});
