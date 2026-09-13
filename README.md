# Model Router

An operator console and OpenAI-compatible chat gateway. Clients keep one endpoint and a stable route such as `auto` while serving models change. Operators configure engine selection, purpose routes, cloud backups, and each caller's permissions in the browser.

The interface takes visual and workflow cues from NVIDIA PAIR. Attribution and the applicable third-party license are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The project is licensed under [Apache License 2.0](LICENSE).

## Run a separate test drive

Requirements: Python 3.11+, uv, Node.js 22.12+ and npm.

```sh
uv sync --frozen
npm ci
npm run build
uv run uvicorn gateway.app:create_app --factory --host 127.0.0.1 --port 8690 --no-proxy-headers --no-access-log
```

Open `http://localhost:8690`. A fresh installation opens a clearly marked first-use setup session when the connection comes from a local or private source, so the owner can configure the router before signing in. The first saved configuration or valid bootstrap-key login finishes setup and applies the saved access policy. If operator sign-in remains enabled, the bootstrap key is in `state/operator-bootstrap.key`; enter it when the setup source is not available. Browser sessions persist across service restarts. Start with an independent state directory by setting `MODEL_ROUTER_STATE`.

1. Connect a serving engine by its OpenAI-compatible base URL. Under **Engine capabilities and limits**, declare the features it supports when its catalog does not report them. Tool-using agents need **tools**; streaming agents also need **streaming**. A plain model catalog does not establish either feature. Provider credentials stay on the server. New cloud connections require explicit model selection.
2. Configure `auto` or add a purpose route. The Routes map shows one node per observed source address, connected to routes and engines. Select a source to inspect its software and access decisions. Open Permission policies to configure access, including before callers connect; select a policy and a route to review a link. Select a route and an engine to review a destination link. Use Details for model patterns, tags, ordering, and optional defaults.
3. Add caller permission policies only when you want named, keyed access. Each has permitted routes, engines, models, cloud access and an optional shared/agent/machine/person label. Labels do not change permissions. Connections are observed even before a policy exists, and a single key can be reused across callers.
4. Point the client at this router's `/v1` endpoint and use its route name. The gateway chooses from fresh catalogs and records its decision.

## Identify actual callers

The Callers page groups observed connections by direct source address, separately from permission policies. Expand a source to inspect its application and transport observations. A source can represent several processes or devices behind a proxy; it does not establish a person's identity. Observations retain their identity across source-port, authentication and recognized software-version changes. Their latest request supplies the displayed permission evidence. Existing duplicate observations are consolidated on upgrade, while historical request events retain their original evidence.

A policy's existence does not claim an agent is connected. Optional-key connections show the direct peer, client library and version, runtime hints, request count and recent source ports. An optional `X-Router-Caller` or `X-Client-Name` label is self-reported evidence. Keys select permissions and may be shared by several callers. Forwarding headers and application labels never grant access. Console route tests identify the console explicitly.

When every eligible destination lacks a required capability, the router returns HTTP 400 with `unsupported_capability` and names the missing request features. Check the engine's capability settings or select a compatible route. Temporary engine or capacity failures remain HTTP 503. Request details in the console retain each destination's rejection reason.

## One engine, many addresses

Name each API once. Engine settings holds the editable name, preferred request URL and aliases. The engine row shows a configured hostname when available. To combine duplicate LAN, overlay, loopback or DNS rows, expand a row and choose **Merge duplicate engine**. Select the surviving engine, preferred URL and credential. Explicit route and caller references follow the surviving engine. Rediscovery of any saved alias returns that same engine.

A merge preserves the surviving engine's model policy and limits. It requires both engines to have finished active requests. Requests use the preferred URL; aliases do not count as extra capacity or independent backups.

## Access is optional

**Settings > Access** controls operator sign-in and the optional default policy for unkeyed callers. Caller-key requirements belong to each route, so local and cloud routes can be mixed in one installation without a global access switch.

When operator sign-in is off, configured trusted source networks can manage the console through its canonical URL or loopback tunnel. Same-origin checks still protect browser writes. `MODEL_ROUTER_PUBLIC_URL` declares the canonical external URL. Keep sign-in enabled when exposing an administrative listener beyond trusted operators.

For each route, the operator can require a caller key or leave it open to unkeyed traffic. A matching source-specific policy takes priority, then the selected default policy. Without either policy, the connection is still observed and can use only routes whose gate is open. A supplied valid key selects its own policy and can be reused across callers. An unusable key header is observed as an unassigned connection but follows the same source, default, or transient routing identity as a request without that header. It can use open routes and is rejected by routes that require a key. Route and model permissions still apply.

Enabling sign-in keeps the current browser signed in. Generating a replacement operator key preserves this browser, revokes other operator sessions, and leaves all agent keys unchanged. Upgrades materialize the former global caller-key setting onto each existing route, so an installed access policy is not silently changed.

Settings identifies installations upgraded from the earlier schema and shows the saved access and discovery mode separately from unsaved edits. Inherited broad inspection and automatic registration stay enabled until you choose otherwise. With sign-in off, everyone using the trusted management boundary shares administrative authority. Scoped endpoint registration also remains open, independently of scheduled sweeps; automatic registration determines whether new endpoints become engines.

Back up the database and its matching external encryption key. Restore that key before starting a restored database. [Credential backup and restore](docs/THREAT_MODEL.md#back-up-and-restore-credentials) explains the key location, consistent SQLite backups and the effect of losing the key.

## Discovery belongs to the application

Fresh installations have automatic discovery, automatic registration, and mDNS disabled. Settings selects targets, complete TCP ranges, attempt rate, address budget, and sweep timing. `8000-8100` is the initial editable range; `1-65535` enables full TCP coverage.

The portable TCP scanner needs no external binary or packet privileges. It inventories open ports without sending application payloads. HTTP catalog inspection runs on explicitly approved ports, or across every open port when **Inspect all open ports as HTTP** is enabled. Choose that broader mode only for services which can safely receive HTTP requests. Every observed open port remains visible, including unverified and credential-protected surfaces.

Discovery and admission are separate switches. Automatic registration trusts recognized endpoints in the selected scope to receive client requests. With it off, discovered catalogs remain available for operator connection. Every admitted engine still passes client permissions. The Discover action creates an identifiable job; Network shows progress, incomplete responses, failures, and cancellation.

Optional adapters:

```sh
uv sync --frozen --extra discovery
```

This enables loopback-listener inventory through psutil and scoped `_model-serving._tcp.local.` announcements through zeroconf. The Nmap scanner is separately installed and selected in Settings. Missing dependencies and denied inventory permissions appear as errors or warnings. [Architecture](docs/DESIGN.md) explains worker ownership and the external-worker option.

## Protocol and operating scope

Chat transport supports `/v1/chat/completions` and `/v1/completions`, including streaming, tool and image message payloads when the selected model declares those capabilities. Catalog adapters support OpenAI-compatible and native Ollama metadata. Observed speech, embedding, transcription and image-generation surfaces remain visible in Network, labelled as inventory when chat routing is unavailable. Transport for those operations is open work, not an advertised capability.

The current service is one process. Requests have admission limits and explicit connection ownership; restarts interrupt in-flight work. Multi-process admission and high availability require further work. A catalog proves advertisement, not inference success. Per-user memory injection also remains open work.

The versioned management API is `/api/v1`; `/api` remains an installed-client alias. OpenAPI is at `/openapi.json`.

## Verification and review

```sh
uv run python -m pytest -q
uv run ruff check gateway tests
npm run test:ui
npm run build
```

Tests use controlled HTTP services, loopback sockets and scanner-process fixtures. They do not initiate LAN scans, exercise a real cloud account, establish visual acceptance, or authorize deployment. [DESIGN.md](docs/DESIGN.md) records ownership and flow. [THREAT_MODEL.md](docs/THREAT_MODEL.md) records the access, discovery and credential assumptions.

## Contributing

Issues and pull requests are welcome. Start with the focused tests for the area you change, then run the complete verification commands above. Keep endpoint addresses, provider credentials and runtime state in local configuration; do not commit them.
