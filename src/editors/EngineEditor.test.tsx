import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { post } from "../api";
import { fixtureConfig } from "../test-fixtures";
import { OPENAI_COMPLETION_PATHS, type Config } from "../types";
import { newEngine } from "./defaults";
import { EngineEditor } from "./EngineEditor";

vi.mock("../api", () => ({ api: vi.fn(), post: vi.fn() }));

it("defaults a manual engine to both supported OpenAI completion operations", () => {
  expect(newEngine().completion_paths).toEqual(OPENAI_COMPLETION_PATHS);
});

it("requires explicit operation selection for a discovery draft with no proven path", () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Unproven discovery surface",
    name_source: "discovered" as const,
    source: "manual",
    completion_paths: [],
  };
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={vi.fn(async (value: Config) => value)}
      onClose={vi.fn()}
    />,
  );

  expect(
    screen.getByRole("button", {
      name: "Chat completions (/chat/completions)",
    }),
  ).toHaveAttribute("aria-pressed", "false");
  expect(
    screen.getByRole("button", {
      name: "Text completions (/completions)",
    }),
  ).toHaveAttribute("aria-pressed", "false");
  expect(
    screen.getByText("Select at least one OpenAI completion operation."),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Connect engine" })).toBeDisabled();
});

it("retains known addresses when typing a new preferred URL and refreshes only that engine", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Serving API",
    base_url: "http://192.0.2.8:8000/v1",
    aliases: ["http://192.0.2.9:8000/v1"],
  };
  config.engines = [engine];
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();
  const url = screen.getByRole("textbox", { name: /^Preferred URL/ });
  await user.clear(url);
  await user.type(url, "http://model.test:8000/v1");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save.mock.calls[0][0].engines).toEqual([
    {
      ...engine,
      base_url: "http://model.test:8000/v1",
      aliases: [...engine.aliases, engine.base_url],
    },
  ]);
  expect(post).toHaveBeenCalledExactlyOnceWith(
    `/api/v1/engines/${engine.id}/refresh`,
  );
});

it("keeps an operator-edited name after a discovered host seeds the editor", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "discovered-host.example",
    name_source: "discovered" as const,
    base_url: "https://completion.example/v1",
  };
  config.engines = [engine];
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();
  const name = screen.getByRole("textbox", { name: "Name" });
  await user.clear(name);
  await user.type(name, "Living room inference");
  await user.click(screen.getByRole("button", { name: "Save changes" }));

  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save).toHaveBeenCalledWith({
    ...config,
    engines: [
      expect.objectContaining({
        name: "Living room inference",
        name_source: "operator",
      }),
    ],
  });
});

it("requires exact operator-declared model IDs for a completion endpoint without a catalog", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Completion endpoint",
    base_url: "https://completion.example/v1",
    source: "discovery",
    name_source: "discovered" as const,
    capabilities: ["text", "streaming"],
    model_inventory_source: "declared" as const,
    declared_models: [],
    model_patterns: [],
  };
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();

  expect(
    screen.getByText(/Enter the exact model IDs this endpoint accepts/),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Connect engine" })).toBeDisabled();
  expect(save).not.toHaveBeenCalled();

  const modelIds = screen.getByRole("textbox", {
    name: /^Declared model IDs/,
  });
  await user.type(modelIds, "*");
  expect(
    screen.getByText(/Wildcards and patterns are not allowed here/),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Connect engine" })).toBeDisabled();

  await user.clear(modelIds);
  await user.type(modelIds, "operator-selected-model");
  const connect = screen.getByRole("button", { name: "Connect engine" });
  expect(connect).toBeEnabled();
  await user.click(connect);

  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save).toHaveBeenCalledWith({
    ...config,
    engines: [
      expect.objectContaining({
        model_inventory_source: "declared",
        declared_models: ["operator-selected-model"],
        model_patterns: ["operator-selected-model"],
      }),
    ],
  });
});

it("lets a manual engine use operator-declared exact model IDs", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Manual completion endpoint",
    base_url: "https://completion.example/v1",
  };
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();

  await user.selectOptions(
    screen.getByRole("combobox", { name: "Model identities" }),
    "declared",
  );
  const modelIds = screen.getByRole("textbox", {
    name: /^Declared model IDs/,
  });
  await user.type(modelIds, "operator-selected-model");
  await user.click(screen.getByRole("button", { name: "Connect engine" }));

  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save).toHaveBeenCalledWith({
    ...config,
    engines: [
      expect.objectContaining({
        model_inventory_source: "declared",
        declared_models: ["operator-selected-model"],
        model_patterns: ["operator-selected-model"],
      }),
    ],
  });
});

it("clears declared model IDs when switching back to a published catalog", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Manual completion endpoint",
    base_url: "https://completion.example/v1",
    model_inventory_source: "declared" as const,
    declared_models: ["operator-selected-model"],
    model_patterns: ["operator-selected-model"],
  };
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();

  await user.selectOptions(
    screen.getByRole("combobox", { name: "Model identities" }),
    "catalog",
  );
  expect(screen.queryByRole("textbox", { name: /^Declared model IDs/ })).toBe(
    null,
  );
  await user.click(screen.getByRole("button", { name: "Connect engine" }));

  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save).toHaveBeenCalledWith({
    ...config,
    engines: [
      expect.objectContaining({
        model_inventory_source: "catalog",
        declared_models: [],
        model_patterns: ["*"],
      }),
    ],
  });
});

it("saves the selected OpenAI operations and refuses an empty operation set", async () => {
  const config = fixtureConfig();
  const engine = {
    ...newEngine(),
    name: "Completion API",
    base_url: "https://completion.example/v1",
  };
  config.engines = [engine];
  const save = vi.fn(async (value: Config) => value);
  const close = vi.fn();
  const first = render(
    <EngineEditor
      initial={engine}
      models={[]}
      config={config}
      save={save}
      onClose={close}
    />,
  );
  const user = userEvent.setup();
  const chat = screen.getByRole("button", {
    name: "Chat completions (/chat/completions)",
  });
  const text = screen.getByRole("button", {
    name: "Text completions (/completions)",
  });

  expect(chat).toHaveAttribute("aria-pressed", "true");
  expect(text).toHaveAttribute("aria-pressed", "true");
  await user.click(chat);
  await user.click(screen.getByRole("button", { name: "Save changes" }));

  await waitFor(() => expect(close).toHaveBeenCalled());
  expect(save).toHaveBeenCalledWith({
    ...config,
    engines: [expect.objectContaining({ completion_paths: ["/completions"] })],
  });
  first.unmount();

  const emptyConfig = fixtureConfig();
  const emptyEngine = {
    ...newEngine(),
    name: "Operation test API",
    base_url: "https://operations.example/v1",
  };
  emptyConfig.engines = [emptyEngine];
  const second = render(
    <EngineEditor
      initial={emptyEngine}
      models={[]}
      config={emptyConfig}
      save={vi.fn(async (value: Config) => value)}
      onClose={vi.fn()}
    />,
  );
  const emptyChat = screen.getByRole("button", {
    name: "Chat completions (/chat/completions)",
  });
  const emptyText = screen.getByRole("button", {
    name: "Text completions (/completions)",
  });
  await user.click(emptyChat);
  await user.click(emptyText);

  expect(
    screen.getByText("Select at least one OpenAI completion operation."),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  second.unmount();
});
