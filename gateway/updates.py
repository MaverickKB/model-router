"""Optional GitHub release check and one-click apply. Never runs without the operator."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .schema import Configuration

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def installed_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def parse_version(value: str) -> tuple:
    match = VERSION_RE.match(value.strip())
    if not match:
        raise ValueError("Release tags must look like 1.2.3 or v1.2.3")
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), pre or "")


def newer(candidate: str, current: str) -> bool:
    left, right = parse_version(candidate), parse_version(current)
    # A plain release is newer than the same numbers with a pre-release suffix.
    left_key = (*left[:3], left[3] == "", left[3])
    right_key = (*right[:3], right[3] == "", right[3])
    return left_key > right_key


def _run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True
    )
    return (result.stdout or result.stderr).strip()


class Updates:
    def __init__(self, http: httpx.AsyncClient, root: Path = ROOT):
        self.http = http
        self.root = root
        self._cached: dict | None = None
        self._cached_at = 0.0

    def repository(self, config: Configuration) -> str:
        return os.environ.get("MODEL_ROUTER_GITHUB_REPO") or config.updates.repository

    async def status(self, config: Configuration, *, refresh=False) -> dict:
        current = installed_version()
        body = {
            "current": current,
            "check_enabled": config.updates.check_enabled,
            "repository": self.repository(config),
            "available": False,
            "latest": None,
            "notes": "",
            "html_url": "",
            "error": "",
        }
        if not config.updates.check_enabled:
            return body
        now = time.time()
        if not refresh and self._cached and now - self._cached_at < 3600:
            return self._cached
        try:
            release = await self._latest(self.repository(config))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                body["error"] = "No GitHub release has been published yet."
                self._cached, self._cached_at = body, now
                return body
            body["error"] = "GitHub could not be reached for releases."
            return body
        except (httpx.HTTPError, ValueError, KeyError):
            body["error"] = "GitHub could not be reached for releases."
            return body
        tag = str(release.get("tag_name") or "")
        body.update(
            latest=tag.lstrip("v"),
            notes=str(release.get("body") or ""),
            html_url=str(release.get("html_url") or ""),
            available=newer(tag, current),
        )
        self._cached, self._cached_at = body, now
        return body

    async def apply(self, config: Configuration, tag: str) -> dict:
        status = await self.status(config, refresh=True)
        latest = status.get("latest") or ""
        wanted = tag.lstrip("v")
        if not status["available"] or wanted != latest:
            raise ValueError("That release is not the published update")
        if self._dirty():
            raise ValueError("The install has local changes; commit or stash them first")
        ref = tag if tag.startswith("v") else f"v{tag}"
        await asyncio.to_thread(self._checkout, ref)
        self._cached = None
        return {"ok": True, "installed": wanted, "restarting": True}

    async def _latest(self, repository: str) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "model-router",
        }
        # Public releases need no GitHub account. A token is only for private forks
        # and must be set explicitly; never inherit a developer GH_TOKEN.
        token = os.environ.get("MODEL_ROUTER_GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await self.http.get(
            f"https://api.github.com/repos/{repository}/releases",
            headers=headers,
            params={"per_page": 5},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Release metadata was unreadable")  # noqa: TRY004
        for item in payload:
            if (
                isinstance(item, dict)
                and item.get("tag_name")
                and not item.get("draft")
            ):
                return item
        missing = httpx.Response(404, request=response.request)
        raise httpx.HTTPStatusError(
            "No published release", request=response.request, response=missing
        )

    def _dirty(self) -> bool:
        try:
            return bool(_run(["git", "status", "--porcelain"], self.root))
        except (subprocess.CalledProcessError, FileNotFoundError):
            return True

    def _checkout(self, ref: str) -> None:
        _run(["git", "fetch", "--tags", "origin", ref], self.root)
        _run(["git", "checkout", "--detach", "FETCH_HEAD"], self.root)
        _run(["uv", "sync", "--frozen"], self.root)
        _run(["npm", "ci"], self.root)
        _run(["npm", "run", "build"], self.root)

    def restart(self) -> None:
        os.execv(sys.executable, [sys.executable, *sys.argv])
