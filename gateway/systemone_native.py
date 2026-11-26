"""One uncancellable native job; positive finish or supervised exit owns release."""
import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import Response
from anyio import CancelScope

from .adapters.jevos_native import (
    CONTRACT, MAX_BODY, MAX_REPLY, NativeFault, PROTOCOL,
    read_health, request_body, response_body, server_timing,
)
from .proxy import InflightRequest
from .routing import decide

TERMINAL = {"completed", "rejected", "recovered"}


def starttoken(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def unit_properties(unit):
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "--property=MainPID,ControlGroup,ActiveState"],
        capture_output=True, text=True, timeout=2, check=False,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise NativeFault("backend_owner_unknown")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def generation(engine):
    """Read trusted unit/proc identity only; no model or service-control calls."""
    settings = engine.native_owner
    try:
        props = unit_properties(settings.owner_unit)
        pid = int(props["MainPID"])
        cgroup = props["ControlGroup"]
        if (props["ActiveState"] != "active" or pid <= 0
                or not cgroup.startswith(f"/user.slice/user-{os.getuid()}.slice/")
                or ".." in Path(cgroup).parts or len(cgroup) > 256):
            raise NativeFault("backend_owner_unknown")
        token = starttoken(pid)
        executable = Path(f"/proc/{pid}/exe")
        if executable.stat().st_size > 64 * 1024 * 1024:
            raise NativeFault("backend_binary_limit")
        binary = hashlib.sha256(executable.read_bytes()).hexdigest()
        with Path(f"/proc/{pid}/cmdline").open("rb") as stream:
            command = stream.read(8193)
        if len(command) > 8192:
            raise NativeFault("backend_owner_unknown")
        args = command.split(b"\0")
        def flag(name):
            return args[args.index(name.encode()) + 1].decode("ascii")
        port = urlsplit(engine.base_url).port
        if (binary != settings.expected_binary_sha256
                or flag("--ctx") != str(settings.max_input_tokens)
                or any(flag(n) != "0" for n in ("--state-cache", "--state-cache-tokens", "--warmup", "--batch-tokens"))
                or int(flag("--port")) != port
                or flag("--host") != urlsplit(engine.base_url).hostname
                or f"0::{cgroup}" not in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                or starttoken(pid) != token):
            raise NativeFault("backend_generation_mismatch")
        return {"unit": settings.owner_unit, "pid": pid, "starttoken": token,
                "cgroup": cgroup, "binary_sha256": binary, "endpoint": engine.base_url,
                "fingerprint": settings.expected_fingerprint}
    except NativeFault:
        raise
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        raise NativeFault("backend_owner_unknown") from None


def exited(record):
    """No healthy label, PID reuse or HTTP close substitutes for these OS proofs."""
    try:
        if Path(f"/proc/{record['pid']}").exists():
            return False
        props = unit_properties(record["unit"])
        if int(props["MainPID"]) != 0 or props["ActiveState"] not in {"inactive", "failed"}:
            return False
        group = Path("/sys/fs/cgroup") / record["cgroup"].lstrip("/")
        if ".." in group.parts or not str(group).startswith("/sys/fs/cgroup/user.slice/"):
            return False
        if group.is_symlink():
            return False
        if group.exists():
            for count, path in enumerate(group.glob("**/cgroup.procs"), 1):
                if count > 32 or path.is_symlink():
                    return False
                with path.open() as stream:
                    if stream.read(4097).strip():
                        return False
        port = urlsplit(record["endpoint"]).port
        for name in ("tcp", "tcp6"):
            raw = Path("/proc/net") / name
            if raw.stat().st_size > 131072:
                return False
            with raw.open() as stream:
                text = stream.read(131073)
            if len(text) > 131072:
                return False
            for line in text.splitlines()[1:]:
                parts = line.split()
                if parts[3] == "0A" and int(parts[1].rsplit(":", 1)[1], 16) == port:
                    return False
        return True
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError, NativeFault):
        return False


def producer_headers(path):
    """Optional future private producer credential; never caller-supplied."""
    if path is None:
        return {}
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o777 != 0o600 or not 1 <= info.st_size <= 4096):
                raise NativeFault("producer_credential_unavailable")
            value = os.read(fd, 4097).decode("ascii").strip()
            if not value or any(c.isspace() or ord(c) < 33 for c in value):
                raise NativeFault("producer_credential_unavailable")
            return {"Authorization": "Bearer " + value}
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        raise NativeFault("producer_credential_unavailable") from None


async def durable(function, *args):
    """A cancelled caller cannot strand a SQLite commit half-observed."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    interrupted = False
    with CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
    value = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return value


class NativeOwner:
    def __init__(self, store, discovery=None, http=None, identity=None, *, probe=None, exit_probe=None):
        self.store, self.discovery, self.http, self.identity = store, discovery, http, identity
        self.probe, self.exit_probe = probe or generation, exit_probe or exited
        self.task = self.slot = None
        self.held = None
        self.restored = False

    def _sql(self, action, value=None, patch=None):
        if not self.store.lock.acquire(timeout=.02):
            raise NativeFault("ownership_store_busy")
        db = None
        try:
            db = sqlite3.connect(self.store.directory / "router.db", timeout=.02)
            db.execute("CREATE TABLE IF NOT EXISTS native_job_owner (id INTEGER PRIMARY KEY CHECK(id=1), "
                       "body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=2048))")
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT body FROM native_job_owner WHERE id=1").fetchone()
            row = json.loads(item[0]) if item else None
            if action == "read":
                return row
            if action == "claim":
                if row and row["phase"] not in TERMINAL:
                    return False
                row = value
            else:
                if not row or row["job_id"] != value:
                    raise NativeFault("ownership_mismatch")
                row.update(patch)
            raw = json.dumps(row, separators=(",", ":"), allow_nan=False)
            if len(raw.encode()) > 2048:
                raise NativeFault("ownership_metadata_limit")
            db.execute("INSERT OR REPLACE INTO native_job_owner VALUES (1,?)", (raw,))
            db.commit()
            return row if action != "claim" else True
        except (sqlite3.Error, ValueError, KeyError):
            raise NativeFault("ownership_write_failed") from None
        finally:
            if db is not None:
                db.close()
            self.store.lock.release()

    async def start(self):
        if not any(e.protocol == PROTOCOL for e in self.store.config().engines):
            return
        row = await durable(self._sql, "read")
        if (self.held and row and row["job_id"] == self.held[0]
                and row["phase"] in TERMINAL and (not self.task or self.task.done())):
            await self._release()
        if not self.restored and row and row["phase"] not in TERMINAL:
            self.discovery.observation(row["engine_id"]).inflight += 1
            self.held = row["job_id"], row["engine_id"]
        self.restored = True

    async def _release(self):
        if self.slot is not None:
            await self.slot.release()
        elif self.held and self.discovery:
            obs = self.discovery.observation(self.held[1])
            obs.inflight = max(0, obs.inflight - 1)
        self.slot = self.held = None

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def dispatch(self, request, payload, client):
        job = None
        try:
            try:
                local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
            except ValueError:
                local = False
            if not local:
                raise HTTPException(403, "Native decision ingress is local only")
            client = await self.identity.identify(request)
            if getattr(request.state, "account_id", None):
                raise HTTPException(403, "Native account metering is not granted")
            await self.start()
            config = self.store.config()
            route = next((r for r in config.routes if r.name == payload.get("model")), None)
            if (not route or route.fallback or route.defaults or route.primary.kind != "local"
                    or len(route.primary.engine_ids) != 1
                    or sum(e.protocol == PROTOCOL for e in config.engines) != 1):
                raise HTTPException(400, "One named local native route without fallback is required")
            choice = decide(config, self.discovery.views(), client, payload,
                            caller_key_present=request.state.caller_key_present)
            if not choice["candidates"]:
                raise HTTPException(choice.get("status", 503), "Native route unavailable or denied")
            candidate = choice["candidates"][0]
            engine = next(e for e in config.engines if e.id == candidate["engine_id"])
            if engine.protocol != PROTOCOL:
                raise HTTPException(400, "Named questions require native JevOS protocol")
            try:
                qid = request_body(payload)
            except NativeFault as exc:
                raise HTTPException(400, str(exc)) from None
            if self.task and not self.task.done():
                raise HTTPException(503, "Native owner busy")
            actual = await asyncio.to_thread(self.probe, engine)
            job = uuid4().hex
            record = {"job_id": job, "engine_id": engine.id, "generation": actual,
                      "phase": "prepared", "protocol": PROTOCOL, "request_id": job}
            if not await durable(self._sql, "claim", record):
                raise HTTPException(503, "Native owner uncertain or busy")
            self.held = job, engine.id
            slot = InflightRequest.claim(self.discovery.observation(engine.id), 1, engine.failure_cooldown_seconds)
            if slot is None:
                await durable(self._sql, "update", job, {"phase": "rejected", "code": "admission_refused"})
                self.held = None
                raise HTTPException(503, "Native owner busy")
            self.slot = slot
            body = {**payload, "model": candidate["model"]}
            self.task = asyncio.create_task(self._run(engine, body, qid, job, actual, request, client.id, route.name))
            self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        except asyncio.CancelledError:
            if job is not None:  # No native POST exists before the tracked task is assigned.
                try:
                    row = await durable(self._sql, "read")
                    if row and row["job_id"] == job and row["phase"] == "prepared":
                        await durable(self._sql, "update", job, {"phase": "rejected", "code": "cancelled_before_submission"})
                        if self.held and self.held[0] == job:
                            await self._release()
                except (NativeFault, asyncio.CancelledError):
                    pass  # Failed persistence remains quarantined, never silently released.
            raise
        except NativeFault as exc:
            raise HTTPException(503, str(exc)) from None
        return await self._connected(request, self.task)

    async def _connected(self, request, operation):
        async def disconnected():
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                raise HTTPException(499, "Native caller disconnected; job remains owned")
            return operation.result()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def _run(self, engine, body, qid, job, actual, request, client_id, route_name):
        response = None
        submitted = False
        started = time.monotonic()
        phase, code, usage = "unknown", "native_job_uncertain", None
        try:
            async with asyncio.timeout(engine.timeout_seconds):
                headers = await asyncio.to_thread(producer_headers, engine.native_owner.producer_credential_path)
                health = await read_health(self.http, engine.base_url, headers)
                if health["model"] != body["model"] or health["fingerprint"] != actual["fingerprint"]:
                    raise NativeFault("native_identity_mismatch")
                current = await self.identity.identify(request)
                config = self.store.config()
                route = next((r for r in config.routes if r.name == route_name), None)
                fresh = decide(config, self.discovery.views(), current, {**body, "model": route_name}, consider_capacity=False,
                               caller_key_present=request.state.caller_key_present)
                if (not route or route.fallback or route.defaults or route.primary.kind != "local"
                        or route.primary.engine_ids != [engine.id]
                        or current.id != client_id or getattr(request.state, "account_id", None)
                        or next((e for e in config.engines if e.id == engine.id), None) != engine
                        or not any(c["engine_id"] == engine.id for c in fresh["candidates"])):
                    raise NativeFault("native_authorization_changed")
                await durable(self._sql, "update", job, {"phase": "submitted"})
                submitted = True  # Durable uncertainty precedes any network submission.
                req = self.http.build_request("POST", engine.base_url + "/v1/systemone", json=body,
                                              headers={"Accept-Encoding": "identity", **headers},
                                              timeout=engine.timeout_seconds)
                response = await self.http.send(req, stream=True)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > min(MAX_REPLY, engine.max_response_bytes):
                        raise NativeFault("native_reply_limit")
                if await asyncio.to_thread(self.probe, engine) != actual:
                    raise NativeFault("native_generation_changed")
                if response.status_code == 200:
                    parsed = response_body(raw, qid, body["model"], engine.native_owner.max_input_tokens)
                    timing = server_timing(response.headers.get("Server-Timing"))
                    usage, phase, code = parsed["usage"], "completed", "positive_native_finish"
                    await durable(self._sql, "update", job, {"phase": phase, "code": code,
                                                            "native_usage": usage, "timing": timing})
                elif response.status_code == 422:
                    rejected = json.loads(raw)
                    if not isinstance(rejected, dict) or not isinstance(rejected.get("detail"), list) or not rejected["detail"]:
                        raise NativeFault("invalid_native_rejection")
                    phase, code = "rejected", "native_prequeue_rejection"
                    await durable(self._sql, "update", job, {"phase": phase, "code": code})
                else:
                    raise NativeFault("native_status_uncertain")
                await self._release()
                return Response(bytes(raw), status_code=response.status_code, media_type="application/json",
                                headers={"Cache-Control": "no-store", "x-router-engine": engine.id,
                                         "x-router-model": body["model"], "x-router-request": job,
                                         "x-router-protocol": PROTOCOL, "x-router-contract": CONTRACT,
                                         "x-router-backend-fingerprint": actual["fingerprint"],
                                         **({"Server-Timing": response.headers["Server-Timing"]} if phase == "completed" else {})})
        except BaseException as exc:
            code = str(exc) if isinstance(exc, NativeFault) else "native_job_uncertain"
            try:
                row = await durable(self._sql, "read")
                phase = "rejected" if not submitted and row["phase"] == "prepared" else "unknown"
                await durable(self._sql, "update", job, {"phase": phase, "code": code})
                if phase == "rejected":
                    await self._release()
            except (NativeFault, asyncio.CancelledError):
                phase = "unknown"  # The existing nonterminal reservation is never erased.
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(503, code) from None
        finally:
            interrupted = False
            if response is not None:
                with CancelScope(shield=True):
                    try:
                        await asyncio.wait_for(response.aclose(), .5)
                    except asyncio.CancelledError:
                        interrupted = True
                    except Exception:
                        pass  # Socket close proves nothing about scheduler ownership.
            event = {"id": job, "ts": time.time(), "engine_id": engine.id,
                     "model": body["model"], "protocol": PROTOCOL, "native_job_state": phase,
                     "status": "completed" if phase == "completed" else "failed",
                     "elapsed_ms": round((time.monotonic() - started) * 1000), "code": code}
            if usage:
                event["native_usage"] = usage
            try:
                await durable(self.store.event, event)
            except asyncio.CancelledError:
                interrupted = True
            except Exception:
                pass  # Singleton owns correctness; history is bounded secondary metadata.
            if interrupted:
                raise asyncio.CancelledError

    async def reconcile(self, expected_job):
        if self.task and not self.task.done():
            raise NativeFault("native_job_still_tracked")
        row = await durable(self._sql, "read")
        if not row or row["job_id"] != expected_job or row["phase"] in TERMINAL:
            raise NativeFault("ownership_mismatch")
        if not await asyncio.to_thread(self.exit_probe, row["generation"]):
            raise NativeFault("backend_exit_unproven")
        await durable(self._sql, "update", expected_job, {"phase": "recovered", "code": "positive_backend_exit"})
        if self.held and self.held[0] == expected_job:
            await self._release()
        return {"job_id": expected_job, "phase": "recovered", "proof": "positive_backend_exit"}


if __name__ == "__main__":
    import argparse
    from .store import Store
    parser = argparse.ArgumentParser(description="Reconcile a private native reservation after verified owner exit")
    parser.add_argument("--state", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    try:
        path = Path(args.state)
        info = (path / "router.db").lstat()
        if not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
            raise NativeFault("private_state_required")
        print(json.dumps(asyncio.run(NativeOwner(Store(str(path))).reconcile(args.job))))
    except (NativeFault, OSError):
        print('{"status":"unavailable","code":"backend_exit_unproven"}')
        raise SystemExit(1)
