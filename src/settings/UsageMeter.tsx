import type { AccountUsage } from "../types";

const clock = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });

// Current-window token usage against the level's budget; counts only.
export function UsageMeter({ usage }: { usage: AccountUsage | null }) {
  if (!usage)
    return <span className="usage-meter">Level unavailable · no usage</span>;
  const max = usage.max_tokens;
  const share = max ? Math.min(usage.used / max, 1) : 0;
  return (
    <span className="usage-meter">
      {max
        ? `${usage.used.toLocaleString()} of ${max.toLocaleString()} tokens · resets ${clock(usage.resets_at)}`
        : `${usage.used.toLocaleString()} tokens this window · no budget`}
      {max ? (
        <i>
          <b
            className={usage.used >= max ? "over" : ""}
            style={{ width: `${share * 100}%` }}
          />
        </i>
      ) : null}
    </span>
  );
}

export function concurrencyLabel(usage: AccountUsage | null) {
  if (!usage) return "";
  return usage.max_concurrency === null
    ? `${usage.active_requests} running · unlimited concurrency`
    : `${usage.active_requests} of ${usage.max_concurrency} running`;
}
