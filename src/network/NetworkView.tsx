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
import {
  isCompletionEndpoint,
  isNativeInventory,
  isProtectedCompletionEndpoint,
  isVerifiedCatalog,
  modelInventoryEvidenceLabel,
  needsManualEndpointReview,
  needsManualTransportEntry,
  serviceKindLabel,
} from "./service-classification";
import "./network.css";
import type { NetworkHost, NetworkReport, NetworkService } from "./types";

const networkFilters = [
  "Responding",
  "Verified catalogs",
  "Completion endpoints",
  "All addresses",
] as const;

function catalogAttemptLabel(
  attempt: NonNullable<NetworkService["catalog_attempts"]>[number],
): string {
  const target = attempt.path || attempt.url || "Catalog endpoint";
  if (attempt.status !== undefined && attempt.status !== null) {
    return `${target}: ${typeof attempt.status === "number" ? `HTTP ${attempt.status}` : attempt.status}`;
  }
  return attempt.detail ? `${target}: ${attempt.detail}` : target;
}

function documentedApiBase(service: NetworkService): string {
  return (
    service.observed_base_url ||
    service.base_url ||
    service.compatible_base_url ||
    ""
  );
}

function observedApiBase(service: NetworkService): string {
  return documentedApiBase(service) || service.origin;
}

function surfaceKey(service: NetworkService): string {
  return service.surface_id || `${service.origin}|${observedApiBase(service)}`;
}

function catalogCoverageLabel(count: number): string {
  return `${count} documented catalog path${count === 1 ? "" : "s"} not checked`;
}

function modelEvidence(
  service: NetworkService,
  model: NetworkService["models"][number],
): string {
  const evidence = [
    service.status === "model_surface"
      ? "Published identity, not a catalog"
      : needsManualTransportEntry(service)
        ? modelInventoryEvidenceLabel(service)
        : "",
    ...model.capabilities,
    !model.capabilities.includes("text")
      ? "Inventory only, chat routing unavailable"
      : "",
    model.available === false
      ? `Unavailable: ${model.availability_reason || "reported by service"}`
      : "",
    model.loaded === true
      ? "Loaded"
      : model.loaded === false
        ? "Available on disk"
        : "",
  ];
  return evidence.filter(Boolean).join(" · ");
}

function CatalogEvidence({
  service,
  onConfigureDiscovery,
}: {
  service: NetworkService;
  onConfigureDiscovery: () => void;
}) {
  const apiBase = documentedApiBase(service);
  const observedEndpoint = observedApiBase(service);
  return (
    <dl>
      <div>
        <dt>{apiBase ? "Observed API base" : "Service origin"}</dt>
        <dd>{observedEndpoint}</dd>
      </div>
      {apiBase && apiBase !== service.origin && (
        <div>
          <dt>Discovered service</dt>
          <dd>{service.origin}</dd>
        </div>
      )}
      {!service.compatible_base_url && (
        <div>
          <dt>Routing proof</dt>
          <dd>
            No compatible OpenAI API base was derived. Review a documented
            endpoint manually before connecting it.
          </dd>
        </div>
      )}
      {service.catalog_attempts?.length ? (
        <div>
          <dt>Catalog checks</dt>
          <dd>
            <ul>
              {service.catalog_attempts.map((attempt, index) => (
                <li
                  key={`${attempt.path || attempt.url || "catalog"}-${index}`}
                >
                  {catalogAttemptLabel(attempt)}
                </li>
              ))}
            </ul>
          </dd>
        </div>
      ) : null}
      {(service.catalog_paths_unprobed ?? 0) > 0 && (
        <div>
          <dt>Coverage limit</dt>
          <dd>
            <span>
              {catalogCoverageLabel(service.catalog_paths_unprobed ?? 0)}. The
              inspector did not conclude that those paths have no catalog.
            </span>{" "}
            <button
              className="text-button"
              onClick={() => onConfigureDiscovery()}
            >
              Review discovery coverage
            </button>
          </dd>
        </div>
      )}
    </dl>
  );
}

function ServiceAction({
  service,
  hostName,
  onConnect,
  onOpenEngine,
}: {
  service: NetworkService;
  hostName: NetworkHost["name"];
  onConnect: (service: NetworkService, hostName: NetworkHost["name"]) => void;
  onOpenEngine?: (engineId: string) => void;
}) {
  if (service.engine_id) {
    return onOpenEngine ? (
      <button onClick={() => onOpenEngine(service.engine_id!)}>
        Review connected engine
      </button>
    ) : null;
  }
  if (service.status === "model_service") {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        Review and connect
      </button>
    );
  }
  if (isProtectedCompletionEndpoint(service)) {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        <LockKeyhole size={14} />
        Configure access
      </button>
    );
  }
  if (service.status === "model_surface") {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        {service.compatible_base_url
          ? "Review and connect"
          : "Review endpoint manually"}
      </button>
    );
  }
  if (service.status === "authentication_required") {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        <LockKeyhole size={14} />
        Review access requirements
      </button>
    );
  }
  if (needsManualEndpointReview(service)) {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        Review endpoint manually
      </button>
    );
  }
  if (service.status === "inspection_required") {
    return (
      <button onClick={() => onConnect(service, hostName)}>
        Review endpoint
      </button>
    );
  }
  return null;
}

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
  const catalogModels = services
    .filter((s) => s.status === "model_service")
    .flatMap((s) => s.models);
  const completionEndpoints = services.filter(isCompletionEndpoint);
  const nativeInventoryModels = services
    .filter(isNativeInventory)
    .flatMap((s) => s.models);
  const publishedInventoryModels = services
    .filter(
      (service) =>
        needsManualTransportEntry(service) && !isNativeInventory(service),
    )
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
          observed services · {report.counts?.models ?? catalogModels.length}{" "}
          catalog model listings
          {completionEndpoints.length
            ? ` · ${completionEndpoints.length} completion endpoint${completionEndpoints.length === 1 ? "" : "s"}`
            : ""}
          {nativeInventoryModels.length
            ? ` · ${nativeInventoryModels.length} native model identit${nativeInventoryModels.length === 1 ? "y" : "ies"}`
            : ""}
          {publishedInventoryModels.length
            ? ` · ${publishedInventoryModels.length} published model identit${publishedInventoryModels.length === 1 ? "y" : "ies"}, transport unverified`
            : ""}
        </small>
      </span>
      <ChevronRight size={17} />
    </button>
  );
}

export function NetworkView({
  report: summary,
  scopeReady,
  onDiscover,
  onConfigureDiscovery,
  onConnect,
  onOpenEngine,
}: {
  report: NetworkReport;
  scopeReady: boolean;
  onDiscover: () => void;
  onConfigureDiscovery: () => void;
  onConnect: (service: NetworkService, hostName: NetworkHost["name"]) => void;
  onOpenEngine?: (engineId: string) => void;
}) {
  const [report, setReport] = useState(summary);
  const [page, setPage] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] =
    useState<(typeof networkFilters)[number]>("Responding");
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
  const needsScope = !scopeReady && !running;
  const responding = report.hosts.filter(
    (h) => h.scope === "network" && h.status === "up",
  );
  const covered =
    report.counts?.covered ?? responding.filter((h) => h.scan_complete).length;
  const hosts = report.hosts.filter(
    (h) =>
      `${h.name} ${h.address} ${h.services.flatMap((s) => s.models.map((m) => m.id)).join(" ")}`
        .concat(
          " ",
          h.services
            .map((service) => `${observedApiBase(service)} ${service.origin}`)
            .join(" "),
        )
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (filter === "All addresses" ||
        (filter === "Responding" && h.status === "up") ||
        (filter === "Verified catalogs" &&
          h.services.some(isVerifiedCatalog)) ||
        (filter === "Completion endpoints" &&
          h.services.some(isCompletionEndpoint))),
  );
  return (
    <section className="network-view">
      <div className="section-heading">
        <div>
          <h1>Your network</h1>
          <p>
            Serving endpoints and published model inventories found in your
            discovery scope.
          </p>
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
        {needsScope ? (
          <button className="primary" onClick={onConfigureDiscovery}>
            Set discovery scope
            <ChevronRight size={15} />
          </button>
        ) : (
          <button onClick={onDiscover} disabled={running}>
            <Radio size={15} />
            {running ? "Scanning…" : "Discover"}
          </button>
        )}
      </div>
      <div className="network-progress">
        {!scopeReady && (
          <div
            className="network-setup"
            aria-labelledby="discovery-scope-heading"
          >
            <Radio size={18} />
            <div>
              <strong id="discovery-scope-heading">
                Set a discovery scope before scanning
              </strong>
              <p>
                Choose hostnames, IP addresses, network ranges, or router-local
                listeners in Settings. Save the scope, then return here to
                discover.
              </p>
            </div>
          </div>
        )}
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
          {scopeReady
            ? report.targets?.join(", ") || "Discovery scope is saved"
            : "No discovery scope is saved"}
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
          {networkFilters.map((value) => (
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
          const catalogServices = host.services.filter(isVerifiedCatalog);
          const completionEndpoints =
            host.services.filter(isCompletionEndpoint);
          const catalogModels = host.services
            .filter((s) => s.status === "model_service")
            .flatMap((s) => s.models);
          const manualTransportInventoryModels = host.services
            .filter(needsManualTransportEntry)
            .flatMap((s) => s.models);
          const nativeInventoryModels = host.services
            .filter(isNativeInventory)
            .flatMap((s) => s.models);
          const publishedInventoryModels = host.services
            .filter(
              (service) =>
                needsManualTransportEntry(service) &&
                !isNativeInventory(service),
            )
            .flatMap((s) => s.models);
          const visibleServices =
            filter === "Verified catalogs"
              ? host.services.filter(isVerifiedCatalog)
              : filter === "Completion endpoints"
                ? host.services.filter(isCompletionEndpoint)
                : host.services;
          const hostInventory = [
            catalogModels.length
              ? `${catalogModels.length} catalog model${catalogModels.length === 1 ? "" : "s"}`
              : "",
            nativeInventoryModels.length
              ? `${nativeInventoryModels.length} native model identit${nativeInventoryModels.length === 1 ? "y" : "ies"}`
              : "",
            publishedInventoryModels.length
              ? `${publishedInventoryModels.length} published model identit${publishedInventoryModels.length === 1 ? "y" : "ies"}, transport unverified`
              : "",
            completionEndpoints.length
              ? `${completionEndpoints.length} completion endpoint${completionEndpoints.length === 1 ? "" : "s"}`
              : "",
            !catalogModels.length &&
            !nativeInventoryModels.length &&
            !completionEndpoints.length &&
            relays.length
              ? `${relays.length} routing service catalog${relays.length === 1 ? "" : "s"}`
              : "",
          ].filter(Boolean);
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
                  {hostInventory.join(" · ") ||
                    `${host.ports.length} open ports`}
                  <small>
                    {relays.length && catalogServices.length
                      ? `${relays.length} routing service catalog${relays.length === 1 ? "" : "s"} · `
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
              {[...catalogModels, ...manualTransportInventoryModels].length >
                0 &&
                !open && (
                  <div className="network-model-preview">
                    {[...catalogModels, ...manualTransportInventoryModels].map(
                      (m, i) => (
                        <span key={m.id + i}>{m.id}</span>
                      ),
                    )}
                  </div>
                )}
              {open && (
                <div className="host-services">
                  {visibleServices.map((service) => (
                    <div className="network-service" key={surfaceKey(service)}>
                      <div className="service-heading">
                        <span>
                          <strong>{observedApiBase(service)}</strong>
                          <small>
                            {service.protocol || "Protocol unconfirmed"} ·{" "}
                            {serviceKindLabel(service)}
                          </small>
                          {observedApiBase(service) !== service.origin && (
                            <small>Discovered service {service.origin}</small>
                          )}
                          {service.engine_id && (
                            <small>
                              Connected engine{" "}
                              {service.engine_name || service.engine_id}
                            </small>
                          )}
                          <small>Checked {timeLabel(service.checked_at)}</small>
                        </span>
                        <ServiceAction
                          service={service}
                          hostName={host.name}
                          onConnect={onConnect}
                          onOpenEngine={onOpenEngine}
                        />
                      </div>
                      {service.status === "model_surface" && (
                        <div className="discovery-evidence">
                          <p>
                            This API base documents a compatible JSON completion
                            request and OpenAI choices response, but did not
                            publish a readable model catalog. It is not
                            registered automatically.
                          </p>
                          <CatalogEvidence
                            service={service}
                            onConfigureDiscovery={onConfigureDiscovery}
                          />
                        </div>
                      )}
                      {service.status === "model_service" &&
                        (service.catalog_paths_unprobed ?? 0) > 0 && (
                          <div className="discovery-evidence">
                            <CatalogEvidence
                              service={service}
                              onConfigureDiscovery={onConfigureDiscovery}
                            />
                          </div>
                        )}
                      {isProtectedCompletionEndpoint(service) && (
                        <div className="discovery-evidence">
                          <p>
                            This API base documents a compatible JSON completion
                            request and OpenAI choices response, but credentials
                            are required before its model catalog can be read.
                            It is not registered automatically.
                          </p>
                          <CatalogEvidence
                            service={service}
                            onConfigureDiscovery={onConfigureDiscovery}
                          />
                        </div>
                      )}
                      {needsManualEndpointReview(service) && (
                        <div className="discovery-evidence">
                          <p>
                            {isNativeInventory(service)
                              ? "This API base published a native model inventory"
                              : service.models.length
                                ? "This API base published model identities"
                                : "This API base was observed"}
                            , but discovery did not prove a compatible JSON
                            completion request and OpenAI choices response. It
                            is not registered automatically.
                          </p>
                          <CatalogEvidence
                            service={service}
                            onConfigureDiscovery={onConfigureDiscovery}
                          />
                        </div>
                      )}
                      {service.models.map((model, modelIndex) => (
                        <div
                          className="network-model"
                          key={`${surfaceKey(service)}-${model.id}-${modelIndex}`}
                        >
                          <strong>{model.id}</strong>
                          <span>{modelEvidence(service, model)}</span>
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
