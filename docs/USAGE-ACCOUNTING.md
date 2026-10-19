# Token accounting per principal — built, proven, not yet deployed

Written 2026-10-19 by Cairn. Branch: `feat/usage-accounting` in
`/home/master/services/model-router-wt-usage` on the serving host.
Commit `111ba7f`, on top of `b8a42af`.

**Status: code complete, 528 tests pass, NOT live.** The live router still
counts nothing. Deploying this is a separate, deliberate act because it changes
the request path that Ken's own session runs through.

## What Ken asked for

> "I need token counts, i want a small lab version of hosting capabilities here.
> we map via origin point or API key. I can't block all free paths now without
> having to retool dr Moore, but still need to track by account/origin. We know
> his, he's on my tailscale and is exempt. I'm exempt. I want to share with
> another tailscale user that access point and port number via their tailscale
> email account, not mine. So we can potentially track by that"

Three constraints, all honored:

1. **Count tokens.** Done, unconditionally.
2. **Do not block free paths.** Enforcement is untouched. Account levels still
   decide limits, and accounts are still disabled live.
3. **Track by account or origin.** Done, with a fourth fallback: a bare address.

## The gap this closes, measured

Before this branch, on the live router: **0 of 4,000 recent events carried a
usage count.** The `usage_windows` table had 0 rows. `accounts.enabled: false`,
so the only code path that created a `UsageMeter` never ran.

The meter existed; nothing called it. That was the whole gap.

After: every request that actually spends tokens gets a `principal_usage` row
and an `event.usage` block, whether or not an account exists.

## The identity ladder

Strongest first. The first one that resolves wins.

| kind | principal | when | evidence |
|---|---|---|---|
| `account` | `account:<id>` | an account key was verified | cryptographic |
| `key` | `key:client:<id>` | a client key was verified | cryptographic |
| `origin` | `origin:<name>` | TCP peer matched a configured origin | address + tailnet login |
| `address` | `addr:<ip>` | nothing else resolved | the address itself |

A display label is never identity. This is the same rule as
`endpoint-identity-review`: an alias is a claim, a hostname is identity, a label
is prose.

**Why `key:client:<id>` and not `key:<keyid>`:** `identity.py` records *which
client* presented a key, not which key row. Naming the client is the honest
identity available at that point. If a client later needs per-key tracking, that
is a separate change in `identity.py`, not a guess here.

## What changed

**New file `gateway/usage_principals.py`** (91 lines). `resolve(request)` returns
the principal plus the evidence that produced it. `row(...)` shapes the ledger
tuple. Pure functions, no I/O.

**`gateway/store.py`** gains:
- table `principal_usage`, keyed by `(principal, window_start, window_seconds)`,
  with `label` and `login` columns for reporting
- `record_principal_usage(...)` — adds one request's tokens
- `principal_usage(days, kind)` — aggregates per principal, sorted by tokens

**`gateway/proxy.py`:**
- the `UsageMeter` is created per request, not inside the accounts branch
- `finish()` reads the meter **once** and feeds both ledgers from that reading
- a principal row is written only when `usage.total > 0`; a refusal before any
  upstream send spent nothing and is charged nothing
- `stream_options: {include_usage: true}` is injected **only for an admitted
  (account) request**. An unadmitted caller's bytes go upstream exactly as sent;
  its stream is counted from chunks. This was a real bug I introduced and caught:
  it changed what the engine saw for key-holders and unkeyed callers alike.

**`gateway/app.py`** gains `GET /api/usage?days=7&kind=` — operator-only,
returns per-principal totals.

**`gateway/origin_resolvers.py`** gains `tailscale_login(address)` — the tailnet
account email (`LoginName` from `tailscale whois --json`'s `UserProfile`). Tags
classify; a login names the account. Tagged devices have no login and return `""`.

**`gateway/origin_policy.py`** carries the login alongside the tag resolver, and
gains `set_login_resolver()` so an injected resolver survives a policy reload.

**`gateway/callers.py`** records `source_login` on every caller observation.

## Proven, not asserted

`tests/test_principal_usage.py`, 11 cases:

- usage recorded per address with accounts disabled (the live shape)
- two addresses are two principals, never one merged total
- a keyed client is tracked by its key, labelled by its client name
- an origin is tracked by name **and** login, and two addresses in one origin
  collapse to one principal
- exempt owner and guest coexist: exempt means not blocked, not invisible
- a refused request creates no principal row and no usage block
- estimates are marked apart from reported counts
- `/api/usage` is operator-only and reports totals
- the identity ladder is a contract (account > key > origin > address)
- the ledger row shape matches the store columns
- window start is stable within a day

One existing test was changed deliberately:
`test_client_and_unkeyed_principals_are_never_limited_or_metered`. It asserted
the old contract, "unkeyed means unmetered". It now asserts the new one while
keeping every no-admission / no-limit assertion intact — including that
`stream_options` is still not injected.

**Full suite: 528 passed, 1 skipped** with `tests/test_speech_close.py` and
`tests/test_speech_frames.py` excluded (pre-existing `cairn_sentinel` import
failure, present on live too).

## Deploying it (when Ken says so)

The branch is not merged and not on the live tree. To deploy:

1. **Back up state and config first** — same as today's merge:
   `backups/` with `config.json`, `state.json`, `router.db`.
2. **Bring the branch onto the live tree.** Either `git merge` into
   `feat/oauth-engine-credentials` (after item 3's commit lands), or copy the
   changed files. Do not copy the whole worktree — it is a snapshot, not a tree
   to run.
3. **Restart is required**, because `proxy.py` is on the request path. Use
   `systemctl --user restart model-router.service`. Expect ~15 s downtime. Do it
   when Ken is not mid-session, or accept that his session drops.
4. **Verify:**
   ```sh
   curl -sS http://127.0.0.1:8680/v1/health
   curl -sS -X POST http://127.0.0.1:8680/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"auto","messages":[{"role":"user","content":"ok"}],"max_tokens":8}'
   K=$(cat state/operator-bootstrap.key)
   curl -sS -H "Authorization: Bearer $K" 'http://127.0.0.1:8680/api/usage?days=1'
   ```
   Expect: health ok, probe 200, and a `principals` array with at least one
   entry carrying non-zero tokens.

## Sharing with a tailnet user by their email — the honest state

Ken wants to share an access point and port with another tailnet user, tracked
by *their* tailnet email, not his.

**This works in the code.** `tailscale_login()` returns the `LoginName` from
`whois`, and an origin can be keyed by it. An origin config like this tracks
that person's tokens separately:

```json
{
  "origins": [
    {
      "name": "shared-with-friend",
      "source_networks": ["100.64.0.0/10"],
      "private": true,
      "allowed_routes": ["auto"]
    }
  ],
  "tag_resolver": "tailscale"
}
```

...with the login resolved per request and stored on the principal row.

**But the tailnet does not have a third user yet.** Live check today: exactly
two logins exist — `operator@example.com` (22 devices, including router host itself)
and `tagged-devices` (41 devices, including every `moore-test-1` node, tagged
`tag:customer-oscar-advisor`).

So "share with another tailscale user by their email" has a prerequisite that is
**not** code: that person must be invited to the tailnet under their own login.
Until then there is no third email to key on, and a guest who joins under Ken's
login is indistinguishable from Ken at the tailnet layer.

That is the one thing to tell Ken plainly: the router can now track by tailnet
email, and will, the moment a second human login exists in the tailnet.

## Dr. Moore's exemption

`state/origin_policy.json` already has origin `customer-oscar-advisor` for
`100.98.242.66/32` and tag `tag:customer-oscar-advisor`, marked `private: true`,
routes restricted to `auto`. That origin is exempt from blocking by
configuration. With this branch, his traffic also starts being **counted**, under
`origin:customer-oscar-advisor`. Exempt and invisible are different things, and
the code now separates them.

One thing worth knowing: the tailnet has ~14 nodes named `moore-test-1`, most
tagged `tag:customer-oscar-advisor` and all offline today, plus one under Ken's
own login at `100.111.246.25`. The origin policy pins a single address
(`100.98.242.66`). If his node's address changes, the tag resolver catches it;
if it joins under a different tag, it does not. Worth confirming with Ken which
node he actually uses.
