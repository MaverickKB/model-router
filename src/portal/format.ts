export const count = (value: number) => value.toLocaleString();
export const clockLabel = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
export const dateLabel = (ts: number | null) =>
  ts
    ? new Date(ts * 1000).toLocaleDateString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
      })
    : "Never";
