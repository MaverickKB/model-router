# Changes

## Unreleased

- Fresh installs leave operator sign-in off. An operator key is created only when the owner enables sign-in or sets `MODEL_ROUTER_ADMIN_TOKEN`.
- Schema v5: account levels and account settings (disabled by default); account storage.
- Account keys (`mru_`) authenticate as level-derived principals: exact level access, refused while accounts are disabled, re-derived before every attempt (a policy change between attempts is reported as a denial); account callers observed by id and grouped on the routing map.
- Account token budgets and concurrency limits enforced before upstream send: fixed windows with in-flight reservation, 429 `rate_limit_exceeded` bodies with `Retry-After`, the `limited` request status, forced `stream_options.include_usage` on streamed requests, and metadata-only usage rows that survive restarts.
- Operator endpoints for accounts (`/api/v1/accounts`): create with a one-time activation link, rename, reassign level, suspend and re-enable, delete with cascade, revoke a user key, and read per-account usage; `/api/v1/state` reports accounts per level.
- Self-service portal API (`/api/v1/portal`): activation links set the password and sign in, username/password sign-in with separate `router_portal` sessions, `me` with level, route readiness, usage, keys and observed connections, user-created API keys shown once, and password change that signs out other browsers; `/portal` serves the console build for the portal page.
- Optional device pre-registration (dev mode, off by default, inert unless accounts are on): a registered direct source address is identified as its account with the key present, receives exactly the level's access and limits, outranks an administrator source policy for that host with a warning naming the policy, and falls through to ordinary policy when disabled, suspended or switched off; portal `/api/v1/portal/devices` and `me.devices.registered`, operator `/api/v1/devices` list, disable and delete.
- Self-service portal page at `/portal` (same build as the console): activation from the link, sign-in, overview with route readiness and usage meter, one-time API key display with a paste-ready snippet, observed devices with registration shown only while pre-registration is on, password change and sign-out; never requests operator state.
- Settings › Accounts in the console: the accounts and dev-mode switches with session hours, level editor with token budget and concurrency limits, account creation with a one-time activation link, per-account detail (rename, level, suspend, links, keys, devices, usage) and the registered-device list; the Routes map groups account connections in an Accounts tray.

## 0.3.0

- Observed caller connections, source addresses and identification evidence, separate from permission policies; console tests keep their own identity.
- Cancellation-safe request history and upstream cleanup; text maps exclude speech-only catalogs.

- One engine identity with editable name, preferred URL, saved aliases and explicit transactional merge of references and selected credentials.
- One caller policy with optional shared, agent, machine or person labels; existing optional access and keys stay intact.
- Live caller/route/engine map with reviewed click-to-link edits, effective permission paths, precise forms and active request metadata.
- Targeted engine catalog refresh after add/edit, without refreshing unrelated providers.

- Optional operator and client authentication in Settings, with access-preserving migration and persistent sessions.
- First-use setup is available on local or private connections before the first completing owner action, then returns to the configured operator authentication boundary.
- Saved access/discovery summaries, retained-setting notices after upgrade, and matching database/encryption-key backup guidance.
- Encrypted provider credentials, salted key verification, bounded management requests and login attempts.
- Program-owned portable discovery, explicit HTTP inspection/admission, identifiable jobs, cancellation and paginated inventories.
- Catalog adapters, typed routing boundaries, response limits and explicit admission ownership.
- Focused UI modules, interaction coverage, versioned management API and operational documentation.
- Route-level caller-key gates for local or cloud routes, with observation before authorization and visible unassigned callers.
