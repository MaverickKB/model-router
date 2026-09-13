import { clockLabel, count } from "./format";
import type { PortalUsage } from "./types";

export function UsageMeter({ usage }: { usage: PortalUsage }) {
  const share =
    usage.max_tokens && usage.max_tokens > 0
      ? Math.min(1, usage.used / usage.max_tokens)
      : 0;
  return (
    <div className="usage-meter">
      <div className="usage-line">
        <strong>
          {usage.max_tokens === null
            ? `${count(usage.used)} tokens this window · Unlimited`
            : `${count(usage.used)} of ${count(usage.max_tokens)} tokens · resets ${clockLabel(usage.resets_at)}`}
        </strong>
        {usage.max_tokens !== null && (
          <div
            className="usage-bar"
            role="progressbar"
            aria-label="Tokens used in the current window"
            aria-valuemin={0}
            aria-valuemax={usage.max_tokens}
            aria-valuenow={usage.used}
          >
            <span style={{ width: `${share * 100}%` }} />
          </div>
        )}
      </div>
      <div className="usage-line">
        <strong>
          {usage.max_concurrency === null
            ? "Unlimited concurrent requests"
            : `${count(usage.active_requests)} of ${count(usage.max_concurrency)} running`}
        </strong>
      </div>
    </div>
  );
}
