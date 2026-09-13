/** Source history spans several days, so a clock time alone is ambiguous. */
export function sourceTimeLabel(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
