# Access and discovery trust

The operator controls destinations which receive prompts and credentials. A client controls request content within its assigned policy. A discovered service and its catalog are untrusted input until validated and admitted under operator policy.

## Access modes

Fresh installs default to operator and client keys, but a pristine state exposes the first-use console only to a source address that is not publicly routed. Same-origin writes still require a matching Origin, and the first saved configuration, valid bootstrap-key login or key rotation ends that setup session and applies the saved access policy. Operator authentication includes loopback and SSH-tunnel connections. Cookie-based writes require a matching Origin, including when the header is absent. Bearer clients can use the API without browser ambient credentials. Forwarded source headers have no authority; the documented server command disables proxy-header trust.

The owner can disable either authentication requirement in Settings. With operator sign-in disabled, configured trusted source networks and the canonical/loopback host boundary grant administrative access. Every process or tunnel sharing that trusted origin has that authority. This is explicit trusted-network mode, not user isolation. With client keys optional, configured shared client permissions still constrain routing. Enabling key requirements can intentionally exclude shared callers; the UI explains that before saving.

An upgrade preserves installed access and discovery choices. Settings records that the installation was upgraded and shows the saved mode separately from draft edits. A retained full-port sweep, broad HTTP inspection or automatic registration stays enabled until the operator saves a change. An open management mode is an intentional operator choice.

Login and registration attempts are bounded. Successful operator bearer authentication is cached by digest so ordinary API polling does not consume a login-attempt budget. Sessions expire, survive process restart, and can be revoked by logout or operator-key replacement. Replacing an operator key leaves client keys unchanged.

## Discovery and admission

A fresh installation sends no discovery traffic. Enabling scanning authorizes the selected targets, range and budget. TCP connections themselves can affect connection-sensitive services. HTTP inspection additionally sends catalog requests and may send metadata POSTs for native runtime descriptions. HTTP requests can have side effects on printers and other non-HTTP services. Broad inspection must therefore be an explicit decision for a suitable scope. Open-port labels do not establish HTTP safety.

Automatic registration is separate from observation. Enabling it trusts recognized endpoints in that scope to receive permitted client traffic. On a shared or hostile network, keep it disabled and connect reviewed engines explicitly. No unauthenticated health response or mDNS announcement proves a service's operator identity.

Registration requires operator authentication in authenticated mode, plus a scoped caller and destination. In trusted-network mode it accepts scoped callers under the operator's explicit access and registration settings. Redirects are disabled. Discovery report consumers recheck destinations and obtain a fresh catalog. Protect discovery policy/report files from unrelated local users.

DNS names in discovery targets delegate trust to their DNS resolution. This implementation checks resolved addresses but does not pin DNS through the transport connection. DNS rebinding resistance remains open work; literal IP/CIDR scopes avoid that ambiguity. A relay which omits the explicit router extension cannot be reliably identified from an owner string. Keep admission explicit where endpoint ownership is uncertain.

## Credentials and transport

Provider credentials use Fernet authenticated encryption. The generated owner-only key file is beside, rather than inside, the state directory, or supplied via `MODEL_ROUTER_SECRET_KEY`/`MODEL_ROUTER_KEY_FILE`. Database-only backups do not contain that key. A full host compromise can read process credentials and is outside this protection. Back up the encryption key separately; restoring an encrypted database requires its matching key. Encryption-key rotation remains open work.

### Back up and restore credentials

Keep a consistent SQLite backup of `router.db` and its matching encryption key. Use SQLite's backup facility for a running database; copying only a live `router.db` can miss changes in its WAL file. Take a stopped-service copy only during an operator-chosen maintenance window because a restart interrupts generations.

The key is the value in `MODEL_ROUTER_SECRET_KEY` when set, otherwise the file selected by `MODEL_ROUTER_KEY_FILE`, otherwise the state directory path with `.key` appended (the default `state` directory uses `state.key`). Keep the key in a separate, access-controlled backup and record which database backup it belongs to. Do not include it in source archives or expose it in the console.

Restore the matching key before starting the router against a restored database, retaining owner-only file permissions. A newly generated key cannot decrypt existing provider credentials, and it also changes the lookup index for modern client keys. Losing the original key can therefore break both provider access and agent authentication. Verify a restore in an isolated environment before relying on the backup. Key rotation is a separate, unimplemented operation; replacing this file is not rotation.

Operator and new client keys use Argon2id verifiers. Client lookup uses a keyed digest. Existing high-entropy SHA-256 client verifiers migrate when their unchanged keys are next used. Old external backups may still contain legacy plaintext credentials; this migration cannot erase independent backups. Bootstrap operator keys are local owner-only files, and are removed when the operator rotates the key through Settings.

Upstream TLS verification stays enabled. Use endpoints with certificates trusted by the server. HTTP and SSH-tunnel access are suitable only within their intended trusted transport boundary. Provider credentials are sent only to their configured endpoint, never to fallback engines. Client credentials are never forwarded upstream. Request history stores route metadata, not prompts, responses or credentials.

Metadata and non-stream responses have size bounds. The service enforces per-engine admission within one process and stops upstream work when a caller disconnects. It does not provide a multi-process or multi-host admission coordinator. Streaming failover stops once output has reached the client.

## Evidence boundary

Controlled tests cover tunnel-shaped authentication, migration, session persistence, invalid keys, registration refusal, bounded parsing, policy exclusion, response limits, concurrency, actual socket cancellation, scanner XML errors and UI interactions. They do not establish exhaustive hostile-input resistance, privileged platform behavior, LAN completeness, production load capacity, or cloud-account compatibility.
