import { expect, it } from "vitest";
import { sourceTimeLabel } from "./source-time";

it("distinguishes observations on yesterday and the previous day at the same clock time", () => {
  const yesterday = new Date(2026, 8, 12, 9, 14);
  const previousDay = new Date(2026, 8, 11, 9, 14);
  const yesterdayLabel = sourceTimeLabel(yesterday.getTime() / 1000);
  const previousLabel = sourceTimeLabel(previousDay.getTime() / 1000);
  const month = yesterday.toLocaleString(undefined, { month: "short" });

  expect(yesterdayLabel).toContain(month);
  expect(yesterdayLabel).toContain("12");
  expect(previousLabel).toContain(month);
  expect(previousLabel).toContain("11");
  expect(yesterdayLabel).not.toBe(previousLabel);
});
