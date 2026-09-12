import "@testing-library/jest-dom/vitest";
// jsdom has no native dialog top layer. Real-browser checks cover its focus
// and dismissal behavior; interaction tests need only the open state.
HTMLDialogElement.prototype.showModal = function () {
  this.setAttribute("open", "");
};
HTMLDialogElement.prototype.close = function () {
  this.removeAttribute("open");
};
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

Object.defineProperty(Element.prototype, "scrollTo", {
  configurable: true,
  value() {},
});
