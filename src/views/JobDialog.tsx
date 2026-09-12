import { ArrowRight } from "lucide-react";
import { Dialog, timeLabel } from "../components";
import { DecisionView } from "../editors";
import type { Job } from "../types";
export function JobDialog({ job, onClose }: { job: Job; onClose: () => void }) {
  return (
    <Dialog title="Routing decision" onClose={onClose} wide>
      <div className="dialog-body">
        <div className="request-path">
          <span>{job.caller?.name || "Caller not recorded"}</span>
          <ArrowRight size={16} />
          <strong>{job.requested}</strong>
          <ArrowRight size={16} />
          <span>{job.engine || "Unserved"}</span>
        </div>
        <div className="request-meta">
          <span className={"status-dot " + job.status} />
          {job.status}
          <span>{timeLabel(job.ts)}</span>
          <span>
            {job.elapsed_ms != null
              ? `${(job.elapsed_ms / 1000).toFixed(2)} seconds`
              : ""}
          </span>
        </div>
        <p className="hint">
          Source: {job.caller?.source_address || "Not recorded"} · Permission
          policy: {job.client}
        </p>
        {job.model && <p className="observed-model">{job.model}</p>}
        {job.attempts.length > 0 && (
          <>
            <h3>Attempts</h3>
            {job.attempts.map((a, i) => (
              <div className="attempt" key={i}>
                <span>{i + 1}</span>
                <div>
                  <strong>{a.engine}</strong>
                  <small>{a.model}</small>
                </div>
                <span>
                  {a.error ||
                    (a.http_status
                      ? `HTTP ${a.http_status}`
                      : a.status || "Waiting")}
                </span>
              </div>
            ))}
          </>
        )}
        <h3>Policy at request time</h3>
        <DecisionView decision={job.decision} />
        <p className="hint">
          Request history contains routing metadata. Prompts, responses, and
          credentials are never recorded.
        </p>
      </div>
    </Dialog>
  );
}
