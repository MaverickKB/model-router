# Handoff — deploy items 3, 4, 7

Written 2026-10-19 ~19:10 local (23:10 UTC) by Cairn on this Mac.
Target host for all three: **Jarvis** (`100.82.24.28`, `192.168.1.75`),
checkout `/home/master/services/model-router`.
One item (7) also touches **spark-48e7** (`192.168.1.91`, user `maverick`).

Ken asked for 1 and 2 (done, live, verified) and handed 3, 4, 7 to a
third-party Grok deployer. This document is what that deployer needs.

## Read this first

**Your own session runs through this router.** Bringing it down or shipping a
bad config disconnects Ken's assistant. Every step below is reversible, and each
one names its rollback. Do not skip the backup step.

**State, not code, is the risk.** Items 3, 4, 7 change code and one systemd
unit. None of them changes `router.db` config. The config was already changed
twice today (revision 40 -> 41 -> 42) by Ken's direct instruction; do not change
it again without asking.

**The working tree is 57 entries dirty and that is expected.** 23 modified
files, 34 untracked. Some untracked files are load-bearing (`gateway/speech.py`,
`gateway/origin_policy.py`, `gateway/runtime_metadata.py`,
`gateway/systemone_native.py`). Committing means committing all of it or none of
it; a partial commit produces a tree that does not run.

## Live facts to trust (verified 2026-10-19 19:00 local)

```
router pid       1804102 (systemd --user unit model-router.service)
health           {"ok":true,"service":"model-router"}
config revision  42
engines          4 (one Spark record, sys1, struct, cairn_voice)
auto route       hosts ['192.168.1.91']  max_concurrency 8
merge suggests   []   (the duplicate was merged; do not expect one)
probe            POST /v1/chat/completions model=auto -> 200, "ok"
```

Items 1 and 2 are already done — do not redo them:
- The duplicate engine (`7d0aba0f`, the `/v1`-less twin of `9ce42377`) was
  merged away. Survivor keeps `http://192.168.1.91:8888/v1` with
  `http://192.168.1.91:8888` as an alias.
- `auto`'s host selector no longer names `192.168.1.33` (the TP worker).

Backups from those two changes: `backups/premerge-20261019-181748/`
(`config.json`, `state.json`, `router.db`).

---

# Item 3 — commit the working tree

**Goal:** get 57 dirty entries into git so there is a rollback point.

**Why it matters:** right now a stray `git checkout` loses the duplicate fix,
the origin policy, speech, runtime metadata and the systemone lane. There is no
commit to return to.

## Preflight (read-only)

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git rev-parse --abbrev-ref HEAD && git rev-parse --short HEAD && \
  git status --porcelain | wc -l && git diff --stat | tail -3'
```

Expect: `feat/oauth-engine-credentials`, `b8a42af`, 57, ~1563 insertions.

## Do

1. **Tag the current HEAD first**, so rollback is a name and not a hash:

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git tag pre-commit-snapshot-20261019 && git tag --list | tail -3'
```

2. **Delete the macOS resource-fork junk before committing.** These are `._*`
   files that came from a Mac checkout; they are not source and should never be
   in the tree:

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git status --porcelain | grep "^??" | sed "s/^?? //" | grep "/\._\|^\._" \
    | tee /tmp/junk.txt; \
  xargs -r -a /tmp/junk.txt rm -v'
```

Expect it to list and remove exactly eight paths:
`gateway/._app.py`, `gateway/._discovery.py`, `gateway/._proxy.py`,
`gateway/._routing.py`, `gateway/._schema.py`,
`gateway/accounts/._limits.py`, `gateway/adapters/.___init__.py`,
`gateway/adapters/._systemone.py`.

3. **Decide what is local-only and must NOT be committed.** Add to
   `.git/info/exclude` (not `.gitignore` — these are this host's business):

```
.aethermind/
.cairn/
backups/
origin-gate-watch/
```

`backups/` holds copies of `router.db`, which contains encrypted provider
secrets. Never commit it.

4. **Stage and inspect before committing:**

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git add -A && git status --porcelain | head -40 && \
  git diff --cached --stat | tail -5'
```

Read that list. Every remaining path should be source, tests, docs, or config
(`pyproject.toml`, `uv.lock`). If `backups/`, `state/`, or `.aethermind/`
appears, stop and fix the exclude first.

5. **Commit** with a message that says what the tree is:

```
feat: origin policy, speech lane, systemone native, runtime metadata

Working tree accumulated since b8a42af. Committed as one unit because the
pieces are interdependent: app.py wires origin policy, speech and the native
owner together, and proxy.py carries the usage meter that runtime_metadata
feeds.

- gateway/origin_policy.py + origin_resolvers.py: per-origin request policy,
  tailnet tag resolver, maintenance gate. Off unless state/origin_policy.json
  exists.
- gateway/speech.py: cairn_voice websocket lane.
- gateway/systemone_native.py + adapters/: system one native lane.
- gateway/runtime_metadata.py: reads a backend's /health for serving limits.
- gateway/engine_suggestions.py + discovery.py: the duplicate-engine fix Ken
  corrected on 2026-10-19 (address is identity, hostname outranks subnet).
- tests: origin policy, speech, auto recovery, foreground reservation,
  systemone native, engine identity.

Live config is unchanged by this commit; it is code catch-up only.
```

## Verify

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git status --porcelain | wc -l && git log --oneline -2'
```

Expect `0` dirty entries and the new commit on top of `b8a42af`.

**Then prove the tree still runs.** The commit does not restart anything, but
run the suite before declaring done:

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  timeout 600 uv run python -m pytest -q \
    --ignore=tests/test_speech_close.py --ignore=tests/test_speech_frames.py 2>&1 | tail -5'
```

Expect: all pass. **Two files are excluded and that is correct** — they import
`cairn_sentinel`, which is not installed on Jarvis (it lives at
`/home/master/Programming/cairn-sentinel-lab/`). This is pre-existing, not
caused by the commit. Do not "fix" it by adding a path hack; it is a separate
piece of work.

## Rollback

```sh
ssh master@100.82.24.28 'cd /home/master/services/model-router && \
  git reset --soft pre-commit-snapshot-20261019'
```

That returns the tree to its exact pre-commit dirty state. Nothing is lost.

---

# Item 4 — pilot telemetry: rotate it or stop it

**Goal:** stop a 492 MB file growing on a 92%-full disk with no rotation.

## The facts

- `spark_pilot.py` runs on `spark-48e7` as `spark-pilot-dspark.service`
  (`/etc/systemd/system/`, root-owned, `User=maverick`, `Restart=on-failure`).
- It POSTs telemetry to `http://192.168.1.75:8680/v1/cairn/pilot`.
- **That route does not exist.** `POST /v1/cairn/pilot` returns `404`. Verified
  again today; there is no `cairn` route in `gateway/app.py` outside
  `cairn_voice`.
- It writes `/var/lib/spark-pilot/telemetry.jsonl`: **492,231,458 bytes**,
  growing ~664 bytes / 10 s (~5.7 MB/day).
- No logrotate entry, no rotation in the code.
- Disk on the Spark: `3.7T total, 3.2T used, 291G free, 92%`.
- It also listens on `:8456` (`PILOT_HANDOFF_PORT`) for its own handoff API.

So: the pilot is writing into a void and will do so forever. Two honest options.

## Option A (recommended) — stop it, keep the file

The lane it posts to does not exist and is not planned in this handoff. A
resident Python process holding 20 MB RSS to POST into a 404 is not worth its
headroom, which is the exact thing Ken objected to.

```sh
# 1. stop and disable
ssh maverick@192.168.1.91 'sudo systemctl stop spark-pilot-dspark.service && \
  sudo systemctl disable spark-pilot-dspark.service && \
  sudo systemctl is-active spark-pilot-dspark.service; \
  pgrep -af spark_pilot || echo "pilot stopped"'

# 2. keep the evidence, do not delete it
ssh maverick@192.168.1.91 'sudo mkdir -p /var/lib/spark-pilot/archive && \
  sudo mv /var/lib/spark-pilot/telemetry.jsonl \
          /var/lib/spark-pilot/archive/telemetry-20261019.jsonl && \
  sudo gzip -9 /var/lib/spark-pilot/archive/telemetry-20261019.jsonl && \
  ls -lh /var/lib/spark-pilot/archive/'
```

Expect: service `inactive`, no `spark_pilot` process, one `.jsonl.gz` of roughly
40–60 MB.

**Why gzip, not delete:** the file may hold the only record of what the pilot
observed before it was stopped. Compressing keeps it and reclaims ~430 MB.

## Option B — keep it running, add rotation

Only if Ken says the pilot lane is going to be built. Then:

```sh
ssh maverick@192.168.1.91 'sudo tee /etc/logrotate.d/spark-pilot >/dev/null <<"EOF"
/var/lib/spark-pilot/telemetry.jsonl {
  daily
  rotate 7
  maxsize 50M
  compress
  delaycompress
  missingok
  notifempty
  copytruncate
}
EOF
sudo logrotate -d /etc/logrotate.d/spark-pilot 2>&1 | tail -12'
```

`copytruncate` is required: the pilot holds the file open and does not reopen
on signal. `-d` is a dry run — read the output before dropping `-d`.

## Do not do

Do not point the pilot at a new router route in this deploy. That is a design
change (build the lane, decide the schema, decide retention) and it is not item
4. Item 4 is: stop the bleeding.

## Verify

```sh
ssh maverick@192.168.1.91 'df -h / | tail -1; ls -lh /var/lib/spark-pilot/'
```

Option A: free space should jump by ~430 MB (291G -> ~292G). Option B: logrotate
dry run should report the file as a rotation candidate.

## Rollback

Option A: `sudo systemctl enable --now spark-pilot-dspark.service`. The archived
file is untouched; the pilot starts a fresh `telemetry.jsonl`.

---

# Item 7 — install spark-probe as the optional advanced worker

**Goal:** make Ken's "small lab version of hosting capabilities" real on the
Spark, at ~1 MB instead of a resident Python process.

## What exists

- Source: `/tmp/cba-ls-c17ac1/probe.c` (on this Mac) — a one-shot C program.
- Built binary on the Spark: `/tmp/spark-probe-src/spark-probe`, 71,496 bytes,
  aarch64, built there with the box's own gcc 13.3.
- **Measured on the target, 5 runs each:**
  - C probe: **1044–1232 kB** peak RSS, ~0.00 s, nothing resident.
  - Python doing the same job once: 19,432–19,680 kB, 0.04–0.05 s.
  - Resident `spark_pilot.py`: 20,716 kB held permanently.
- It answers from the engine's own `/health` and `/metrics`:
  served model, aliases, `max_batch_size`, warming, connections, waiting,
  decode tps, memory active/peak, and Prometheus counters (prompt tokens,
  generation tokens, requests running/waiting, kv cache).
- Verified output today against the live engine:
  `{"ok":true,"source":"127.0.0.1:8888","served_model":"DeepSeek-V4.1-Flash-TF",
  "served_models":["DeepSeek-V4.1-Flash-TF","GLM-5.3-Flash-EXL3"],
  "max_batch_size":4,...}`

**Do not rebuild it on this Mac.** The Mac is arm64 Darwin; the Spark is arm64
Linux. A Mac build copied over fails with `Exec format error` and produces a
fake RSS number (that mistake was already made once today and corrected).

## Do — on the Spark

```sh
# 1. build from source on the box, so the binary matches its libc
ssh maverick@192.168.1.91 'cd /tmp/spark-probe-src && \
  gcc -O2 -Wall -Wextra -o spark-probe probe.c && \
  ./spark-probe --selftest | python3 -m json.tool | head -6'

# 2. install it where a caller can find it
ssh maverick@192.168.1.91 'sudo install -m 0755 -o root -g root \
  /tmp/spark-probe-src/spark-probe /usr/local/bin/spark-probe && \
  ls -l /usr/local/bin/spark-probe && \
  /usr/local/bin/spark-probe 127.0.0.1 8888 | python3 -m json.tool'

# 3. confirm it costs nothing when idle
ssh maverick@192.168.1.91 '/usr/bin/time -v /usr/local/bin/spark-probe \
  127.0.0.1 8888 2>&1 | grep -E "Maximum resident|Elapsed"'
```

Expect: JSON that parses, and `Maximum resident set size` around 1200 kB.

## Do — a tiny HTTP wrapper so the router can call it

The router speaks HTTP; the probe speaks stdout. Wrap it without keeping state
or loading Python. **This is the one piece you have to write.** Keep it this
small:

```sh
ssh maverick@192.168.1.91 'sudo tee /usr/local/bin/spark-probe-http >/dev/null <<"EOF"
#!/bin/sh
# One request in, one JSON document out. No daemon, no state, no Python.
# Bound to all interfaces so Jarvis can reach it; the probe itself only talks
# to loopback. Reads the port from argv[1], defaulting to 8888.
PORT="${1:-8888}"
while true; do
  { printf "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n"
    /usr/local/bin/spark-probe 127.0.0.1 "$PORT"
  } | nc -l -p 8099 -q 1
done
EOF
sudo chmod 0755 /usr/local/bin/spark-probe-http'
```

Then supervise it minimally:

```sh
ssh maverick@192.168.1.91 'sudo tee /etc/systemd/system/spark-probe-http.service >/dev/null <<"EOF"
[Unit]
Description=spark-probe HTTP wrapper (one-shot probe per request)
After=network-online.target

[Service]
ExecStart=/usr/local/bin/spark-probe-http 8888
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
MemoryMax=8M

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now spark-probe-http.service
sudo systemctl is-active spark-probe-http.service'
```

`MemoryMax=8M` is deliberate: it makes the memory budget a fact the kernel
enforces, not a hope. If the wrapper ever needs more than 8 MB, it dies and
restarts instead of quietly eating headroom.

## Verify

```sh
ssh master@100.82.24.28 'curl -sS -m 5 http://192.168.1.91:8099/ | python3 -m json.tool | head -10'
ssh maverick@192.168.1.91 'systemctl show spark-probe-http.service -p MemoryCurrent --value; \
  ss -lntp | grep 8099'
```

Expect: the same JSON the probe prints, `MemoryCurrent` in the low megabytes,
and a listener on `:8099`.

## Rollback

```sh
ssh maverick@192.168.1.91 'sudo systemctl disable --now spark-probe-http.service; \
  sudo rm /etc/systemd/system/spark-probe-http.service /usr/local/bin/spark-probe-http; \
  sudo systemctl daemon-reload'
```

The probe binary at `/usr/local/bin/spark-probe` can stay; it costs nothing
unless called.

---

# What NOT to do in this deploy

1. **Do not restart the router.** Items 3, 4, 7 need no restart. `model-router`
   is a plain uvicorn process under a systemd user unit; a restart drops Ken's
   session. If a restart ever becomes necessary, use
   `systemctl --user restart model-router.service` and expect ~15 s of downtime,
   and say so before doing it.
2. **Do not touch `state/router.db` config.** Revision 42 is correct and was set
   by Ken's instruction. The merge and the `.33` removal are done.
3. **Do not "fix" the two excluded speech tests** by adding a
   `cairn_sentinel` path. That is a separate project on a different shelf.
4. **Do not delete `backups/`.** It holds the only pre-change copies of
   `router.db` from today's two live changes.
5. **Do not run `git checkout` or `git clean`** before step 3's commit lands.
   That is the entire reason item 3 exists.
6. **Do not commit `backups/`, `state/`, `.aethermind/`, `.cairn/`.** They hold
   encrypted secrets and local continuity state.

---

# Definition of done

- [ ] Item 3: `git status --porcelain` on Jarvis is empty, HEAD is the new
      commit, tag `pre-commit-snapshot-20261019` exists, and the suite passes
      with the two known exclusions.
- [ ] Item 4: `spark-pilot-dspark.service` is `inactive` and `disabled`; no
      `spark_pilot` process; `telemetry.jsonl` moved to
      `/var/lib/spark-pilot/archive/` and gzipped; Spark free space increased.
- [ ] Item 7: `/usr/local/bin/spark-probe` exists and answers;
      `spark-probe-http.service` is active; `curl http://192.168.1.91:8099/`
      from Jarvis returns parseable JSON; `MemoryCurrent` is a few MB.
- [ ] Router never restarted; `/v1/health` still `{"ok":true}`;
      `POST /v1/chat/completions model=auto` still answers 200.

Report back with: the commit hash, the tag, `git status --porcelain | wc -l`
(zero), the Spark's `df -h /` before/after, and one captured JSON document from
`http://192.168.1.91:8099/`.
