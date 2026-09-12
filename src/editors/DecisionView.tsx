import { Check } from "lucide-react";
import type { Decision } from "../types";

export function DecisionView({ decision }: { decision: Decision }) {
  return (
    <div className="decision">
      <div className="decision-result">
        {decision.candidates.length ? (
          <>
            <Check size={17} />
            <span>
              {decision.candidates[0].engine}
              <small>{decision.candidates[0].model}</small>
            </span>
          </>
        ) : (
          <span>
            {decision.error || "No permitted engine is currently available"}
          </span>
        )}
      </div>
      {decision.candidates.map((c, i) => (
        <div className="reason-row" key={`${c.engine_id}-${c.model}-${i}`}>
          <span className="status-dot available" />
          <div>
            <strong>{c.engine}</strong>
            <span>{c.model}</span>
            <small>{c.reason}</small>
            {!!c.skipped_defaults?.length && (
              <small>
                Optional defaults not applied: {c.skipped_defaults.join(", ")}
              </small>
            )}
          </div>
          <span className="tag">{c.tier}</span>
        </div>
      ))}
      {decision.rejections.length > 0 && (
        <details className="advanced">
          <summary>
            {decision.rejections.length} excluded candidate
            {decision.rejections.length === 1 ? "" : "s"}
          </summary>
          {decision.rejections.map((r, i) => (
            <div className="reason-row" key={i}>
              <span className="status-dot offline" />
              <div>
                <strong>{r.engine}</strong>
                <small>{r.reason}</small>
              </div>
            </div>
          ))}
        </details>
      )}
    </div>
  );
}
