"""Credential-free discovery policy, explicit jobs, and atomic evidence snapshots."""

import copy
import json
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from uuid import uuid4


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "full_scan"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value))
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def read_policy(root: Path) -> dict:
    return read_json(root / "policy.json", {"enabled": False, "targets": []})


_report_lock = threading.RLock()


def publish(root: Path, report: dict):
    """Atomically replace a report while serializing in-process writers."""
    with _report_lock:
        write_json(root / "network.json", report)


def mutate_report(root: Path, mutation: Callable[[dict], dict | None]) -> dict:
    """Read, change, and publish one discovery report as one critical section.

    A sweep and mDNS listener run as separate asynchronous tasks.  Atomic file
    replacement protects readers from partial JSON, but it does not protect a
    stale read-modify-write sequence from replacing newer evidence.  This
    helper is the single same-process mutation boundary for ``network.json``.
    The returned copy is a snapshot, so callers cannot mutate stored state
    after the critical section ends.
    """
    with _report_lock:
        report = read_report(root)
        replacement = mutation(report)
        if replacement is not None:
            if not isinstance(replacement, dict):
                raise TypeError("network report mutations must return a dictionary")
            report = replacement
        write_json(root / "network.json", report)
        return copy.deepcopy(report)


def read_report(root: Path) -> dict:
    report = read_json(
        root / "network.json",
        {
            "phase": Phase.NOT_STARTED,
            "hosts": [],
            "error": "",
            "completed_at": 0,
            "detail": "Network discovery has not reported yet",
        },
    )
    if (
        report.get("phase") == Phase.RUNNING
        and time.time() - report.get("updated_at", 0) > 30
    ):
        report.update(
            phase=Phase.FAILED,
            error="Discovery worker stopped reporting. Restart its owning process or request a new scan.",
        )
    return report


_request_lock = threading.Lock()


def request_scan(root: Path) -> dict:
    with _request_lock:
        return _request_scan(root)


def _request_scan(root: Path) -> dict:
    pending = read_json(root / "request.json", {})
    report = read_report(root)
    if pending and pending.get("id") != report.get("job_id"):
        return {**pending, "status": Phase.QUEUED, "accepted": False}
    if report.get("phase") == Phase.RUNNING:
        return {"id": report.get("job_id"), "status": Phase.RUNNING, "accepted": False}
    job = {"id": uuid4().hex, "requested_at": time.time()}
    write_json(root / "request.json", job)
    return {**job, "status": Phase.QUEUED, "accepted": True}
