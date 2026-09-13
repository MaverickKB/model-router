import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";
import { ActivationDialog } from "./ActivationDialog";

const link = {
  token: "mra_secret",
  url: "http://router.test/portal/activate#token=mra_secret",
  expires: 1800259200,
};

function renderDialog() {
  return render(
    <ActivationDialog
      link={link}
      purpose="activate"
      accountName="Dana"
      accountsEnabled
      onClose={() => {}}
    />,
  );
}

describe("Activation dialog", () => {
  const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  afterEach(() => {
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
    else delete (navigator as { clipboard?: unknown }).clipboard;
  });

  it("copy reports Copied only after the clipboard write succeeds", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Copy link" }));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeVisible();
    expect(await navigator.clipboard.readText()).toBe(link.url);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("copy without a clipboard keeps the label and tells the operator to copy manually", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Copy is unavailable here — select the link and copy it manually before pressing Done.",
    );
    expect(screen.getByRole("button", { name: "Copy link" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Copied" }),
    ).not.toBeInTheDocument();
  });

  it("copy keeps the label when the clipboard write is rejected", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("denied")) },
      configurable: true,
    });
    renderDialog();
    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.getByRole("button", { name: "Copy link" })).toBeVisible();
  });
});
