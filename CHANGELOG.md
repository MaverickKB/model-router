# Changes

## Unreleased

- Fresh installs leave operator sign-in off. An operator key is created only when the owner enables sign-in or sets `MODEL_ROUTER_ADMIN_TOKEN`.
- Schema v5: account levels and account settings (disabled by default); account storage.
- Account keys (`mru_`) authenticate as level-derived principals: exact level access, refused while accounts are disabled, re-derived before every attempt (a policy change between attempts is reported as a denial); account callers observed by id and grouped on the routing map.
- Account token budgets and concurrency limits enforced before upstream send: fixed windows with in-flight reservation, 429 `rate_limit_exceeded` bodies with `Retry-After`, the `limited` request status, forced `stream_options.include_usage` on streamed requests, and metadata-only usage rows that survive restarts.

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
