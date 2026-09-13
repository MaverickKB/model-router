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

Open `http://localhost:8690`. A fresh installation does not require an operator key. Trusted local/private sources can manage the console until the owner turns **Operator sign-in** on in Settings. That switch is off by default; enabling it is how you protect access. If sign-in is on and no key exists yet, the first save or **Generate replacement operator key** creates one (`state/operator-bootstrap.key` only when that path is used). Browser sessions persist across service restarts. Start with an independent state directory by setting `MODEL_ROUTER_STATE`.

1. Connect a serving engine by its OpenAI-compatible base URL. Under **Engine capabilities and limits**, declare the features it supports when its catalog does not report them. Tool-using agents need **tools**; streaming agents also need **streaming**. A plain model catalog does not establish either feature. Provider credentials stay on the server. New cloud connections require explicit model selection.
2. Configure `auto` or add a purpose route. The Routes map shows one node per observed source address, connected to routes and engines. Select a source to inspect its software and access decisions. Open Permission policies to configure access, including before callers connect; select a policy and a route to review a link. Select a route and an engine to review a destination link. Use Details for model patterns, tags, ordering, and optional defaults.
3. Add caller permission policies only when you want named, keyed access. Each has permitted routes, engines, models, cloud access and an optional shared/agent/machine/person label. Labels do not change permissions. Connections are observed even before a policy exists, and a single key can be reused across callers.
4. Point the client at this router's `/v1` endpoint and use its route name. The gateway chooses from fresh catalogs and records its decision.

## Identify actual callers

The Callers page groups observed connections by direct source address, separately from permission policies. Expand a source to inspect its application and transport observations. A source can represent several processes or devices behind a proxy; it does not establish a person's identity. Observations retain their identity across source-port, authentication and recognized software-version changes. Their latest request supplies the displayed permission evidence. Existing duplicate observations are consolidated on upgrade, while historical request events retain their original evidence.

A policy's existence does not claim an agent is connected. Optional-key connections show the direct peer, client library and version, runtime hints, request count and recent source ports. An optional `X-Router-Caller` or `X-Client-Name` label is self-reported evidence. Keys select permissions and may be shared by several callers. Forwarding headers and application labels never grant access. Console route tests identify the console explicitly.

When every eligible destination lacks a required capability, the router returns HTTP 400 with `unsupported_capability` and names the missing request features. Check the engine's capability settings or select a compatible route. Temporary engine or capacity failures remain HTTP 503. Request details in the console retain each destination's rejection reason.

An unrecognized route or model name returns HTTP 404 with `model_not_found`. Use `GET /v1/models` to find the names available to the caller. Existing route and model permissions still apply. In request history, authentication and permission refusals are **denied**; an unknown name or unsupported capability is **failed**. Both remain visible under Errors.

## One engine, many addresses

Name each API once. Engine settings holds the editable name, preferred request URL and aliases. The engine row shows a configured hostname when available. To combine duplicate LAN, overlay, loopback or DNS rows, expand a row and choose **Merge duplicate engine**. Select the surviving engine, preferred URL and credential. Explicit route and caller references follow the surviving engine. Rediscovery of any saved alias returns that same engine.

A merge preserves the surviving engine's model policy and limits. It requires both engines to have finished active requests. Requests use the preferred URL; aliases do not count as extra capacity or independent backups.

## Access is optional

**Settings > Access** controls operator sign-in and the optional default policy for unkeyed callers. Fresh installs start with sign-in off. The owner turns it on when they want to protect management access. Caller-key requirements belong to each route, so local and cloud routes can be mixed in one installation without a global access switch.

When operator sign-in is off, configured trusted source networks can manage the console through its canonical URL or loopback tunnel. Same-origin checks still protect browser writes. `MODEL_ROUTER_PUBLIC_URL` declares the canonical external URL. Keep sign-in enabled when exposing an administrative listener beyond trusted operators.

For each route, the operator can require a caller key or leave it open to unkeyed traffic. A matching source-specific policy takes priority, then the selected default policy. Without either policy, the connection is still observed and can use only routes whose gate is open. A supplied valid key selects its own policy and can be reused across callers. An unusable key header is observed as an unassigned connection but follows the same source, default, or transient routing identity as a request without that header. It can use open routes and is rejected by routes that require a key. Route and model permissions still apply. One exception: when user accounts and device pre-registration are both on, a registered direct source address is identified as its account before the source and default policies are consulted and is treated as key-present, whether or not the request carries an unusable key header; see the dev-mode paragraph under "User accounts (optional)".

Enabling sign-in keeps the current browser signed in. Generating a replacement operator key preserves this browser, revokes other operator sessions, and leaves all agent keys unchanged. Upgrades materialize the former global caller-key setting onto each existing route, so an installed access policy is not silently changed.

Settings identifies installations upgraded from the earlier schema and shows the saved access and discovery mode separately from unsaved edits. Inherited broad inspection and automatic registration stay enabled until you choose otherwise. With sign-in off, everyone using the trusted management boundary shares administrative authority. Scoped endpoint registration also remains open, independently of scheduled sweeps; automatic registration determines whether new endpoints become engines.

### User accounts (optional)

User accounts are off by default. When an administrator enables them, a person's API key (prefixed `mru_`) authenticates as that person's account and receives exactly the access of the account's level: allowed routes, engines, models and cloud access. Such a key never widens access, is refused while accounts are disabled, and stops working between attempts as soon as the account is suspended or its level changes. The Callers page shows account connections named after the account and key, with permissions attributed to the account rather than to a caller policy.

Operators manage accounts through `/api/v1/accounts`. The walkthrough is: save a level in the configuration (`account_levels`), create an account with `POST /api/v1/accounts {"username", "name", "level_id"}`, hand the person the activation link from the 201 response, then enable user accounts. The link is shown once, expires after 72 hours, and carries its token in the URL fragment; `POST /api/v1/accounts/{id}/activation` issues a replacement (a password-reset link for an active account) and invalidates the previous one. Every step works while accounts are still disabled, so an installation can be prepared before the switch is turned on; the link only activates once accounts are enabled. `PUT /api/v1/accounts/{id}` renames an account, moves it to another level, or suspends and re-enables it (suspension signs the person out of the portal and refuses their keys between attempts); `DELETE` removes the account with its keys, sessions, devices and usage. `GET /api/v1/accounts` lists each account with its key and device counts, activation state and current-window usage; `GET /api/v1/accounts/{id}` adds the key list and seven days of usage windows. Re-enabling an account that was suspended before it ever activated returns it to pending and voids the link issued before the suspension, so a fresh activation link must be issued. A request still in flight when its account is deleted records no usage afterwards. Responses carry ids and counts only, never password verifiers, key material or activation tokens after issue. The portal API that consumes the link is described below; the Settings › Accounts page arrives in a later change.

**Device pre-registration (dev mode)** is a second switch, off by default and inert unless user accounts are also on. Less secure: a registered source address can use key-gated routes without an API key, limited to that account's level. Direct connection address only — forwarding headers are ignored. Leave off unless you need it. When both switches are on, an account holder registers a device in the portal by its single IPv4 or IPv6 address (no CIDRs; loopback means every process on the router host; at most 10 per account, each address registered once across the whole router). A request from that address is then identified as that account with the key present, receives exactly the level's routes, engines, models and cloud access, and counts against the level's token budget and concurrency limit, so the path can never exceed what the same person's key already gets. A registered address takes precedence over an administrator source policy covering the same host; `GET /api/v1/state` warns with the policy and network each device overrides, `GET /api/v1/devices` lists every device with its account and the policies it shadows, `PUT /api/v1/devices/{id} {"enabled": false}` disables one without deleting it, and `DELETE /api/v1/devices/{id}` removes it. An unusable key header from the registered address (the placeholder key many OpenAI-compatible clients always send) does not change this: the request is still identified as the device, while a valid `mru_` account key is resolved, or refused, before the device is consulted. A disabled device, a suspended account or a missing level falls through to the ordinary source and default policies rather than refusing the host, and turning either switch off makes every registration inert without deleting it. Keep the documented `--no-proxy-headers` server command: only the direct TCP peer address can match a device.

Back up the database and its matching external encryption key. Restore that key before starting a restored database. [Credential backup and restore](docs/THREAT_MODEL.md#back-up-and-restore-credentials) explains the key location, consistent SQLite backups and the effect of losing the key. Back up before upgrading: the schema moves forward on first start, and an older release does not read a newer database.

**Console walkthrough.** Settings › Accounts is the operator's page for all of this. The saved-state summary at the top says whether user accounts are on, how many levels and accounts exist (and how many are still pending activation), and the portal address people should open. Below it, the "User accounts" switch enables the feature; the "Allow device pre-registration (dev mode)" switch only appears once the draft has accounts on, and both are saved together with the portal session length. Levels are added and edited in a dialog that mirrors the caller-policy editor — allowed routes, engines, models, cloud and direct-model access — plus a token budget (a number of tokens per 1-hour, 24-hour, 7-day, 30-day or custom window) and a maximum number of concurrent requests, each of which can be left unlimited. Removing a level that accounts still use opens a dialog that moves those accounts to another level first and then saves the configuration without it, because the router refuses to drop a referenced level. "Add account" asks for a username, an optional display name and a level, and shows the activation link exactly once with a Copy button; while accounts are still off the dialog says so and the link starts working once they are enabled. Opening an account row shows its usage for the current window and the last seven days, lets the operator rename it, move it to another level, suspend or re-enable it (a pending account must activate through its link first), issue an activation or password-reset link, revoke its keys, disable or remove its registered devices, and remove the account with everything it owns. A "Registered devices" list appears whenever device pre-registration is saved on or rows exist, naming the source policy each device takes precedence over and offering Disable and Remove; while the switch is off the list says the rows are inert. On the Routes map, account connections are grouped in an "Accounts" tray under their account name rather than resolved against permission policies.

### User accounts and limits

Each level can carry a token budget (`max_tokens` per `window_seconds`) and a maximum number of concurrent requests; either or both may be left unlimited. Limits apply to account principals only. Configured caller policies and unkeyed connections are never metered or limited. Limits are checked after routing has chosen candidates and before anything is sent to an engine. A refused request receives HTTP 429 with an OpenAI-style `rate_limit_exceeded` error whose `code` is `token_budget_exceeded` or `concurrency_limit_exceeded`, a `request_id` that matches the request in the Activity rail (status `limited`), and `Retry-After` when a budget caused the refusal.

Budgets use fixed windows aligned to the clock: an hourly window starts on the hour. A request is refused when the tokens already used in the current window plus the tokens reserved by requests still in flight have reached the budget. The request that crosses the line is allowed once, so a small request near the limit is not refused for a large reservation it does not need. Each admitted request reserves its estimated prompt size plus the caller's `max_tokens` (else `max_completion_tokens`, else the route's default, else 1024) until it finishes. That reservation bounds how far concurrent requests can overshoot: per in-flight request, at most the difference between its actual completion and its reservation. Callers that send `max_tokens`, routes with a `max_tokens` default, and a concurrency limit on the level make budgets precise. A window boundary permits one burst of up to twice the budget across the two windows.

Counts come from the engine's `usage` report. For streamed requests from account principals the router sets `stream_options: {"include_usage": true}` on the upstream request, and a caller cannot turn that off. The engine then ends the stream with one extra chunk that carries `usage` and an empty `choices` list, the standard OpenAI shape, which the router forwards unchanged. Clients built on OpenAI SDKs handle it; raw SSE consumers should expect it. If an engine rejects `stream_options`, add `stream_options` to that engine's unsupported parameters and the router falls back to an estimate from prompt bytes and stream chunks, shown with a `≈` prefix. Cancelled and interrupted streams are charged from the usage report when it already arrived, otherwise from the estimate. Usage rows hold token and request counts only, never prompts or responses.

Limits are enforced per router process, exactly like engine capacity. A multi-process deployment enforces each process's share independently.

### Self-service portal

The portal is the account holder's side of user accounts, served from the same build as the console at `/portal` and backed by `/api/v1/portal/…`. A person opens the activation link the administrator handed over, chooses a password (12 to 256 characters, anything except their own username) and is signed in; that one use consumes the link, and a refused password leaves it usable. Afterwards they sign in with username and password. Signed in, `GET /api/v1/portal/me` shows their account and level: the routes the level allows and whether each is ready right now (the same answer `/v1/models` gives their keys), model and cloud permissions, the token budget and concurrency limit with current-window usage, their keys, and the connections recently seen using their keys. They can create named API keys (`POST /api/v1/portal/keys`, up to 20 per account; the key is shown once and works on `/v1` exactly as the level allows), revoke a key, change their password (`PUT /api/v1/portal/password`, which signs out every other browser and leaves API keys untouched) and sign out.

Portal sessions are separate from operator sessions: a different cookie (`router_portal`), a separate table and no shared authority in either direction, so a portal session cannot reach the management API and an operator session cannot reach the portal. Every portal write requires a same-origin request. Sign-in, and the current-password check of a password change, is bounded per source address and per username; an unknown username, a wrong password and a not-yet-activated account all receive the same answer, and a suspended account is reported only after its correct password. Suspending an account or issuing it a password-reset link signs it out everywhere. While user accounts are disabled every portal endpoint answers 403 except `GET /api/v1/portal/status`, which reports only the switches; existing sessions resume when accounts are enabled again. `me.devices.observed` always lists the connections recently seen using the person's keys or registered devices, scoped to their own account; `me.devices.registered` is a list only while device pre-registration is on (null otherwise), and `POST /api/v1/portal/devices {"address", "name"}` / `DELETE /api/v1/portal/devices/{id}` register and remove the person's own devices, answering 404 while the switch is off. Each registered row says whether it shadows an administrator source policy.

**Using the portal page.** Open the activation link the administrator sent you; it lands on `/portal` with the token in the address fragment, asks for a password and its confirmation, then signs you in and drops the token from the address bar. From then on `/portal` is a username and password sign-in ("Stay signed in on this browser" keeps the session for the configured session length, otherwise one hour). Overview shows your level, the routes you may use with a ready dot for each, cloud and direct-model permissions, a usage meter ("42,100 of 200,000 tokens · resets 14:00", "1 of 2 running", or Unlimited) and the base URL to point clients at. API keys creates a named key and shows it once with a ready-to-paste `OPENAI_BASE_URL` / `OPENAI_API_KEY` snippet; Revoke asks once before removing a key. Devices lists the machines recently seen using your keys; the registration form, its dev-mode warning and the list of registered addresses appear only while the administrator has device pre-registration on, prefilled with your own address as the router sees it, and each registered row says when it takes precedence over an administrator network policy. Account changes your password (other browsers are signed out, keys are unchanged) and signs out. The page only ever calls `/api/v1/portal/…`; while user accounts are disabled it shows a single "not enabled" notice with no sign-in form.

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
