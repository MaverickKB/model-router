import { useEffect, useState } from "react";
import { api, post } from "../api";
import type { DebugEntries, DebugEntryDetail, DebugStatus } from "../types";
import "./traffic.css";

const SCOPES = [
  { id: "headers", label: "Headers" },
  { id: "request", label: "Request body" },
  { id: "response", label: "Response body" },
  { id: "stream", label: "Stream chunks" },
];

export function DebugPanel() {
  const [status, setStatus] = useState<DebugStatus | null>(null);
  const [entries, setEntries] = useState<DebugEntries>({ items: [], total: 0 });
  const [detail, setDetail] = useState<DebugEntryDetail | null>(null);
  const [error, setError] = useState("");
  const [ttlMinutes, setTtlMinutes] = useState(15);
  const [maxMegabytes, setMaxMegabytes] = useState(100);
  const [scopes, setScopes] = useState<string[]>([
    "headers",
    "request",
    "response",
    "stream",
  ]);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const nextStatus = await api<DebugStatus>("/api/v1/debug/status");
      setStatus(nextStatus);
      if (nextStatus.entries > 0) {
        const nextEntries = await api<DebugEntries>(
          "/api/v1/debug/entries?limit=100",
        );
        setEntries(nextEntries);
      } else {
        setEntries({ items: [], total: 0 });
      }
      setError("");
    } catch (failure) {
      if (failure instanceof Error) setError(failure.message);
      else setError("Debug status could not be loaded");
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function startCapture() {
    setBusy(true);
    try {
      await post("/api/v1/debug/start", {
        ttl_seconds: ttlMinutes * 60,
        max_bytes: maxMegabytes * 1024 * 1024,
        scope: scopes,
      });
      await load();
    } catch (failure) {
      if (failure instanceof Error) setError(failure.message);
      else setError("Debug capture could not be started");
    } finally {
      setBusy(false);
    }
  }

  async function stopCapture() {
    setBusy(true);
    try {
      await post("/api/v1/debug/stop");
      await load();
    } catch (failure) {
      if (failure instanceof Error) setError(failure.message);
      else setError("Debug capture could not be stopped");
    } finally {
      setBusy(false);
    }
  }

  async function purgeCapture() {
    setBusy(true);
    try {
      await post("/api/v1/debug/purge");
      setDetail(null);
      await load();
    } catch (failure) {
      if (failure instanceof Error) setError(failure.message);
      else setError("Debug entries could not be purged");
    } finally {
      setBusy(false);
    }
  }

  async function viewEntry(entryId: number) {
    try {
      const next = await api<DebugEntryDetail>(
        `/api/v1/debug/entries/${entryId}`,
      );
      setDetail(next);
      setError("");
    } catch (failure) {
      if (failure instanceof Error) setError(failure.message);
      else setError("Debug entry could not be loaded");
    }
  }

  function toggleScope(scope: string) {
    const next: string[] = [];
    let found = false;
    for (const value of scopes) {
      if (value === scope) {
        found = true;
        continue;
      }
      next.push(value);
    }
    if (!found) next.push(scope);
    setScopes(next);
  }

  const active = status?.active === true;
  const session = status?.session ?? null;
  const lastSession = status?.last_session ?? null;
  let statusText = "Capture is off.";
  if (active && session && status) {
    statusText =
      "Capturing until " +
      new Date(session.expires * 1000).toLocaleTimeString() +
      ". " +
      Math.ceil(session.ttl_remaining / 60) +
      " min left. " +
      formatBytes(status.bytes_used) +
      " of " +
      formatBytes(session.max_bytes) +
      " used. " +
      status.entries +
      " entries.";
  } else if (status && status.entries > 0) {
    statusText =
      status.entries + " entries retained. Purge when you are finished.";
    if (lastSession?.stop_reason === "max_bytes") {
      statusText += " The last session stopped at the size limit.";
    } else if (lastSession?.status === "expired") {
      statusText += " The last session expired and its entries were purged.";
    } else if (lastSession?.stop_reason === "operator") {
      statusText += " The last session was stopped by the operator.";
    }
  }

  return (
    <section className="paper traffic-debug">
      <div className="debug-head">
        <div>
          <h2>Debug capture</h2>
          <p className="debug-status">{statusText}</p>
        </div>
        {status && (
          <span className={`traffic-status ${active ? "running" : "completed"}`}>
            {active ? "capturing" : "off"}
          </span>
        )}
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      <div className="debug-controls">
        <label>
          <span>Window (minutes)</span>
          <input
            type="number"
            min={1}
            max={60}
            value={ttlMinutes}
            onChange={(event) => setTtlMinutes(Number(event.target.value))}
            disabled={active || busy}
          />
        </label>
        <label>
          <span>Storage cap (MB)</span>
          <input
            type="number"
            min={1}
            max={500}
            value={maxMegabytes}
            onChange={(event) => setMaxMegabytes(Number(event.target.value))}
            disabled={active || busy}
          />
        </label>
        <button
          className="primary"
          onClick={() => void startCapture()}
          disabled={active || busy || scopes.length === 0}
        >
          Start capture
        </button>
        <button onClick={() => void stopCapture()} disabled={!active || busy}>
          Stop
        </button>
        <button
          onClick={() => void purgeCapture()}
          disabled={busy || (status?.entries ?? 0) === 0}
        >
          Purge
        </button>
      </div>

      <div className="debug-scopes">
        {SCOPES.map((scope) => (
          <label key={scope.id}>
            <input
              type="checkbox"
              checked={scopes.includes(scope.id)}
              onChange={() => toggleScope(scope.id)}
              disabled={active || busy}
            />
            <span>{scope.label}</span>
          </label>
        ))}
      </div>

      <p className="debug-warning">
        Captured payloads may contain sensitive prompt content. Capture is off
        by default, stops at the time or size limit, and redacts credential
        headers and known credential fields.
      </p>

      <div className="debug-entries">
        <h3>Captured entries</h3>
        {entries.items.length === 0 && (
          <p className="hint">No entries captured in this window.</p>
        )}
        {entries.items.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Direction</th>
                <th>Request</th>
                <th>Bytes</th>
                <th>Preview</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.items.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.ts * 1000).toLocaleTimeString()}</td>
                  <td>{entry.direction}</td>
                  <td>
                    {entry.request_id ? entry.request_id.slice(0, 8) : ""}
                  </td>
                  <td>{formatBytes(entry.body_bytes)}</td>
                  <td className="debug-preview">
                    {entry.body_preview}
                    {entry.truncated ? " (truncated)" : ""}
                  </td>
                  <td>
                    <button
                      className="subtle"
                      onClick={() => void viewEntry(entry.id)}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {detail && (
        <section className="debug-detail paper">
          <div className="debug-detail-head">
            <strong>
              Entry {detail.id} · {detail.direction}
            </strong>
            <button className="subtle" onClick={() => setDetail(null)}>
              Close
            </button>
          </div>
          <table className="debug-headers">
            <tbody>
              {Object.keys(detail.headers).length === 0 && (
                <tr>
                  <td>Headers</td>
                  <td>Not captured</td>
                </tr>
              )}
              {Object.keys(detail.headers).map((key) => (
                <tr key={key}>
                  <td>{key}</td>
                  <td>{detail.headers[key]}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <pre>{detail.body || "(empty body)"}</pre>
          {detail.truncated ? (
            <p className="hint">Body was truncated at the capture limit.</p>
          ) : null}
        </section>
      )}
    </section>
  );
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
