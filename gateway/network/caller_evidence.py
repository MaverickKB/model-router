"""Read-only caller identity evidence from saved network discovery snapshots."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from ..caller_sources import (
    normalized_address,
    normalized_hardware_address,
    normalized_hostname,
)
from .report import read_report


class DiscoveryCallerEvidence:
    """Cache current discovery evidence without initiating discovery work."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._completed_at = 0.0
        self._complete = False
        self._addresses: dict[str, dict[str, str]] = {}

    def sources(self, maximum_age_seconds: int) -> dict[str, dict[str, str]]:
        path = self.root / "network.json"
        try:
            stat = path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        now = time.time()
        with self.lock:
            if signature != self._signature:
                self._refresh(signature)
            hardware_is_current = (
                self._complete
                and self._completed_at > 0
                and self._completed_at <= now
                and now - self._completed_at <= maximum_age_seconds
            )
            return {
                address: {
                    "hostname": evidence["hostname"],
                    "hardware_address": (
                        evidence["hardware_address"] if hardware_is_current else ""
                    ),
                }
                for address, evidence in self._addresses.items()
            }

    def _refresh(self, signature: tuple[int, int] | None):
        report = read_report(self.root)
        try:
            completed_at = float(report.get("completed_at", 0) or 0)
        except (TypeError, ValueError):
            completed_at = 0
        complete = report.get("phase") == "complete" and completed_at > 0
        addresses: dict[str, dict[str, str]] = {}
        hardware_candidates: list[tuple[str, str]] = []
        for host in report.get("hosts", []):
            if not isinstance(host, dict):
                continue
            address = normalized_address(str(host.get("address", "")))
            if not address or address == "Unknown source":
                continue
            hardware_address = normalized_hardware_address(
                str(host.get("hardware_address", ""))
            )
            addresses[address] = {
                "hostname": normalized_hostname(str(host.get("name", ""))),
                "hardware_address": "",
            }
            if (
                complete
                and host.get("scope") == "network"
                and host.get("status") == "up"
                and host.get("scan_complete") is True
                and hardware_address
            ):
                hardware_candidates.append((address, hardware_address))
        hardware_counts: dict[str, int] = {}
        for _address, hardware_address in hardware_candidates:
            hardware_counts[hardware_address] = (
                hardware_counts.get(hardware_address, 0) + 1
            )
        for address, hardware_address in hardware_candidates:
            if hardware_counts[hardware_address] == 1:
                addresses[address]["hardware_address"] = hardware_address
        self._signature = signature
        self._completed_at = completed_at
        self._complete = complete
        self._addresses = addresses
