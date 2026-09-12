import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { post } from "../api";
import { fixtureConfig } from "../test-fixtures";
import type { Config } from "../types";
import { newEngine } from "./defaults";
import { EngineEditor } from "./EngineEditor";

vi.mock("../api", () => ({ api: vi.fn(), post: vi.fn() }));

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
