// Editors retain the record they opened. Polling other records must not silently
// grant permission to overwrite a newer version of this same record.
export function sameRecord(a: unknown, b: unknown): boolean {
  const canonical = (value: unknown) =>
    JSON.stringify(value, (_key, item) =>
      item && typeof item === "object" && !Array.isArray(item)
        ? Object.fromEntries(
            Object.entries(item).sort(([a], [b]) => a.localeCompare(b)),
          )
        : item,
    );
  return canonical(a) === canonical(b);
}

export function requireUnchanged(
  baseline: unknown,
  current: unknown,
  subject: string,
) {
  if (!sameRecord(baseline, current)) {
    throw new Error(
      `This ${subject} changed elsewhere. Your draft is preserved. Reopen the editor to load the saved version before making further changes.`,
    );
  }
}
