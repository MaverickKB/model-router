/** Source history spans several days, so a clock time alone is ambiguous. */
export function sourceTimeLabel(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Relative age is display-only; the router retains its existing history. */
export function sourceAgeLabel(
  timestamp: number,
  now = Date.now() / 1000,
): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) return "time unknown";
  const seconds = Math.max(0, now - timestamp);
  if (seconds < 60) return "less than a minute ago";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60)
    return `${minutes} ${minutes === 1 ? "minute" : "minutes"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}
