import {
  ChevronDown,
  ChevronRight,
  LockKeyhole,
  Radio,
  Search,
  Server,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { timeLabel } from "../components";
import "./network.css";
import type { NetworkReport, NetworkService } from "./types";

export function networkIsScanning(report: NetworkReport): boolean {
  return [
    "queued",
    "presence",
    "service_scan",
    "local_services",
    "full_scan",
  ].includes(report.phase);
}

export function scanStatus(report: NetworkReport): string {
  const labels: Record<string, string> = {
    queued: "Scan queued",
    presence: "Finding network addresses",
    service_scan: "Identifying network services",
    local_services: "Inspecting local listeners",
    full_scan: "Scanning every configured TCP port",
    complete: "Network sweep completed",
    failed: "Network discovery needs attention",
    interrupted: "Network sweep interrupted",
    paused: "Network discovery paused",
    not_started: "Network discovery has not started",
  };
  return labels[report.phase] || report.phase;
}

export function NetworkSummary({
  report,
  onOpen,
}: {
  report: NetworkReport;
  onOpen: () => void;
}) {
  const services = report.hosts.flatMap((h) => h.services);
  const models = services
    .filter((s) => s.status !== "gateway")
    .flatMap((s) => s.models);
  return (
    <button className="network-summary" onClick={onOpen}>
      <Radio size={17} />
      <span>
        <strong>{scanStatus(report)}</strong>
        <small>
          {report.counts?.responding ??
            report.hosts.filter(
              (h) => h.scope === "network" && h.status === "up",
            ).length}{" "}
          responding addresses · {report.counts?.services ?? services.length}{" "}
          observed services · {report.counts?.models ?? models.length} model
          listings
        </small>
      </span>
      <ChevronRight size={17} />
    </button>
  );
}

export function NetworkView({
  report: summary,
  onDiscover,
  onConnect,
}: {
  report: NetworkReport;
  onDiscover: () => void;
  onConnect: (service: NetworkService) => void;
}) {
  const [report, setReport] = useState(summary);
  const [page, setPage] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("Responding");
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const params = new URLSearchParams({
          offset: String(page * 25),
          limit: "25",
          query: search,
          category: filter,
        });
        setReport(
          await api<NetworkReport>("/api/v1/network?" + params, {
            signal: controller.signal,
          }),
        );
        setLoadError("");
      } catch (error) {
        if (!controller.signal.aborted)
          setLoadError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(load, 3000);
      }
    }
    timer = setTimeout(load, 150);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [page, search, filter]);
  const running = networkIsScanning(report);
  const responding = report.hosts.filter(
    (h) => h.scope === "network" && h.status === "up",
  );
  const covered =
    report.counts?.covered ?? responding.filter((h) => h.scan_complete).length;
  const hosts = report.hosts.filter(
    (h) =>
      `${h.name} ${h.address} ${h.services.flatMap((s) => s.models.map((m) => m.id)).join(" ")}`
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (filter === "All addresses" ||
        (filter === "Responding" && h.status === "up") ||
        (filter === "Model services" &&
          h.services.some(
            (s) =>
              s.status === "model_service" ||
              s.status === "model_surface" ||
              s.status === "gateway",
          ))),
  );
  return (
    <section className="network-view">
      <div className="section-heading">
        <div>
          <h1>Your network</h1>
          <p>Serving models and the services found around them.</p>
        </div>
        {running && (
          <button
            onClick={async () => {
              try {
                await api("/api/v1/discover", { method: "DELETE" });
              } catch (e) {
                setLoadError(e instanceof Error ? e.message : String(e));
              }
            }}
          >
            Cancel scan
          </button>
        )}
        <button onClick={onDiscover} disabled={running}>
          <Radio size={15} />
          {running ? "Scanning…" : "Discover"}
        </button>
      </div>
      <div className="network-progress">
        <div>
          <strong>{scanStatus(report)}</strong>
          <span>
            {report.ports ? `TCP ${report.ports}` : "Awaiting scan coverage"}
          </span>
        </div>
        {running && (
          <progress
            max="100"
            value={
              report.phase === "full_scan"
                ? (report.progress ??
                  (100 * covered) / Math.max(1, responding.length))
                : undefined
            }
          />
        )}
        <p>
          {report.targets?.join(", ") ||
            "Configure discovery scope in Settings"}
          {report.completed_at
            ? ` · Completed ${timeLabel(report.completed_at)}`
            : " · Sweep incomplete"}
        </p>
        <p>
          {covered} of {report.counts?.responding ?? responding.length}{" "}
          responding addresses have completed TCP coverage.
        </p>
        {report.error && <p className="error">{report.error}</p>}
        {loadError && (
          <p role="alert" className="error">
            {loadError}
          </p>
        )}
        {report.warnings?.map((w) => (
          <p className="hint" key={w}>
            {w}
          </p>
        ))}
      </div>
      <div className="network-toolbar">
        <div className="filter-tabs">
          {["Responding", "Model services", "All addresses"].map((value) => (
            <button
              key={value}
              className={filter === value ? "selected" : ""}
              onClick={() => {
                setFilter(value);
                setPage(0);
              }}
            >
              {value}
            </button>
          ))}
        </div>
        <label className="network-search">
          <Search size={14} />
          <input
            aria-label="Search network"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
            placeholder="Find an address or model"
          />
        </label>
      </div>
      <div className="network-hosts">
        {hosts.map((host) => {
          const open = expanded === host.address;
          const relays = host.services.filter((s) => s.status === "gateway");
          const models = host.services
            .filter((s) => s.status !== "gateway")
            .flatMap((s) => s.models);
          const visibleServices =
            filter === "Model services"
              ? host.services.filter(
                  (s) =>
                    ["model_service", "model_surface", "gateway"].includes(
                      s.status,
                    ) || s.catalog_tracked,
                )
              : host.services;
          return (
            <article className="network-host" key={host.address}>
              <button
                className="host-heading"
                onClick={() => setExpanded(open ? null : host.address)}
                aria-expanded={open}
              >
                <Server size={21} />
                <span>
                  <strong>{host.name || host.address}</strong>
                  <small>
                    {host.name ? host.address + " · " : ""}
                    {host.scope === "router_local"
                      ? "Router-local listeners"
                      : host.status === "up"
                        ? "Responded on network"
                        : "No response to discovery"}
                  </small>
                </span>
                <span className="host-count">
                  {models.length
                    ? `${models.length} model${models.length === 1 ? "" : "s"}`
                    : relays.length
                      ? `${relays.length} relay catalogs`
                      : `${host.ports.length} open ports`}
                  <small>
                    {relays.length && models.length
                      ? `${relays.length} relay catalogs · `
                      : ""}
                    {host.status !== "up"
                      ? "Address checked"
                      : host.scan_complete
                        ? "Port sweep complete"
                        : "Port sweep pending"}
                  </small>
                </span>
                {open ? <ChevronDown size={17} /> : <ChevronRight size={17} />}
              </button>
              {models.length > 0 && !open && (
                <div className="network-model-preview">
                  {models.map((m, i) => (
                    <span key={m.id + i}>{m.id}</span>
                  ))}
                </div>
              )}
              {open && (
                <div className="host-services">
                  {visibleServices.map((service) => (
                    <div className="network-service" key={service.origin}>
                      <div className="service-heading">
                        <span>
                          <strong>{service.origin}</strong>
                          <small>
                            {service.protocol || "Protocol unconfirmed"} ·{" "}
                            {service.status === "gateway"
                              ? "Relay catalog"
                              : service.status.replaceAll("_", " ")}
                          </small>
                          <small>Checked {timeLabel(service.checked_at)}</small>
                        </span>
                        {[
                          "authentication_required",
                          "inspection_required",
                        ].includes(service.status) && (
                          <button onClick={() => onConnect(service)}>
                            <LockKeyhole size={14} />
                            {service.status === "authentication_required"
                              ? "Configure access"
                              : "Connect engine"}
                          </button>
                        )}
                      </div>
                      {service.models.map((model) => (
                        <div className="network-model" key={model.id}>
                          <strong>{model.id}</strong>
                          <span>
                            {model.capabilities.join(" · ")}
                            {!model.capabilities.includes("text") &&
                              " · Inventory only, chat routing unavailable"}
                            {model.available === false
                              ? ` · Unavailable: ${model.availability_reason || "reported by service"}`
                              : ""}
                            {model.loaded === true
                              ? " · Loaded"
                              : model.loaded === false
                                ? " · Available on disk"
                                : ""}
                          </span>
                        </div>
                      ))}
                      {service.detail && <p>{service.detail}</p>}
                    </div>
                  ))}
                  {!host.services.length && (
                    <p className="hint">
                      {host.scan_complete
                        ? "No serving endpoint was identified on the inspected ports."
                        : "This address is responding. Service identification is still in progress."}
                    </p>
                  )}
                  {!!host.filtered_ports && (
                    <p className="hint">
                      The port sweep recorded{" "}
                      {host.filtered_ports.toLocaleString()} inconclusive
                      responses. Fresh service observations are shown above.
                    </p>
                  )}
                </div>
              )}
            </article>
          );
        })}
      </div>
      {(report.total ?? 0) > 25 && (
        <nav className="network-pagination" aria-label="Network pages">
          <button disabled={page === 0} onClick={() => setPage(page - 1)}>
            Previous
          </button>
          <span>
            Addresses {page * 25 + 1} to{" "}
            {Math.min((page + 1) * 25, report.total ?? 0)} of {report.total}
          </span>
          <button
            disabled={(page + 1) * 25 >= (report.total ?? 0)}
            onClick={() => setPage(page + 1)}
          >
            Next
          </button>
        </nav>
      )}
      {!hosts.length && (
        <p className="hint">
          {running
            ? "Addresses and serving endpoints appear as the sweep progresses."
            : "No addresses match this view."}
        </p>
      )}
    </section>
  );
}
