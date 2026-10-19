# Origin policy

Optional per-origin rules applied before routing. **Off by default**: with no
policy file there are no origins, so requests are untouched and no gate applies.

## Configuration
- File: `MODEL_ROUTER_ORIGIN_POLICY`, default `<state>/origin_policy.json`. It is re-read
  when it changes; no restart is needed.
- Global gate flag: `MODEL_ROUTER_MAINTENANCE_FLAG`, default `<state>/maintenance.flag`.
  While the file exists, every *configured* origin gets 503. Unconfigured traffic is unaffected.
- Example: `examples/origin_policy.example.json`.

Top-level keys:
| key | default | meaning |
|---|---|---|
| `origins` | `[]` | list of origins, first match wins (networks first, then tags) |
| `private_header` | `X-Router-Origin-Private` | header added upstream for private origins |
| `origin_name_header` | `false` | `true` (sends `X-Router-Origin`) or a header name; off, so origin names never leave the router |
| `static_tags` | `{}` | `{tag: [cidr, ...]}` for the built-in static CIDR resolver (used automatically when present) |
| `tag_resolver` | `null` | `null` (off, or static when `static_tags` is set), `"static"`, `"tailscale"` (optional plugin; needs the `tailscale` CLI, returns no tags without it), or `"module:callable"` |

Origin keys (all optional except `name`):
| key | default | effect |
|---|---|---|
| `source_networks` | `[]` | CIDRs matched against the TCP peer address |
| `source_tags` | `[]` | matched only when a `tag_resolver` is set |
| `private` | `false` | adds the private header upstream, tags the event, hides events/callers from `/state` exports |
| `allowed_routes` | `null` (any) | model/route names allowed; anything else gets 403 before routing |
| `allowed_paths` | `null` (any) | HTTP paths allowed; others (and websockets) get 403 |
| `strip_prefixes` / `strip_keys` | `[]` | keys removed from the body, `metadata` and `extra_body(.metadata)` |
| `maintenance` | `false` | gate on from config |
| `maintenance_flag` | `null` | per-origin flag file; gate on while it exists |
| `maintenance_message` | generic | text of the 503 `down_for_maintenance` error |

The classification uses the direct peer address. Behind a reverse proxy, run with
proxy headers enabled only if the proxy is trusted.

## Gate helper
`scripts/origin-gate.sh <flag-file> on|off|status` creates or removes a flag file.

## Tag resolvers (optional)
Interface: any callable `resolve(address) -> list[str]` (see `TagResolver` in
[`gateway/origin_resolvers.py`](../gateway/origin_resolvers.py)).
- Built-in default: `StaticCIDRResolver`, from `static_tags`. No external tools, any OS.
- Optional plugin: `tailscale`, which uses `tailscale whois --json`. Binary: `MODEL_ROUTER_TAILSCALE_BIN`; cache TTL: `MODEL_ROUTER_TAG_RESOLVER_TTL`.
- Custom: `"tag_resolver": "package.module:callable"`.
