"""Own discovery job lifecycle; a Sweep owns TCP and protocol observations.

Runs inside the gateway by default. The optional external worker reads only the
credential-free discovery directory. Nmap raw-packet capability is opt-in.
"""

import argparse
import asyncio
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import httpx

from .report import Phase, read_json, read_policy
from .sweep import Sweep


class Collector(Sweep):
    async def run(self, once=False):
        catalog_task = None if once else asyncio.create_task(self.watch_catalogs())
        try:
            await self.run_scans(once)
        finally:
            if catalog_task:
                catalog_task.cancel()
                await asyncio.gather(catalog_task, return_exceptions=True)

    async def run_scans(self, once=False):
        last_policy = None
        request_path = self.root / "request.json"
        cancel_path = self.root / "cancel.json"
        while True:
            policy = await asyncio.to_thread(read_policy, self.root)
            request = read_json(request_path, {})
            requested = request and request.get("id") != self.report.get("job_id")
            changed = last_policy is not None and policy != last_policy
            due = time.time() >= self.report.get("completed_at", 0) + policy.get(
                "network_interval_seconds", 1800
            )
            if requested or (policy.get("enabled") and (changed or due)):
                self.report["job_id"] = request["id"] if requested else uuid4().hex
                if read_json(cancel_path, {}).get("id") == self.report["job_id"]:
                    self.report.update(
                        phase=Phase.INTERRUPTED,
                        error="Cancelled before starting",
                        completed_at=time.time(),
                    )
                    await self.save(True)
                    if once:
                        return
                    continue
                operation = asyncio.create_task(self.collect(policy))
                try:
                    while not operation.done():
                        await asyncio.wait({operation}, timeout=1)
                        await self.save()
                        cancelled = (
                            read_json(cancel_path, {}).get("id")
                            == self.report["job_id"]
                        )
                        updated = (
                            await asyncio.to_thread(read_policy, self.root) != policy
                        )
                        if cancelled or updated:
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                            self.report.update(
                                phase=Phase.INTERRUPTED,
                                error="Cancelled by operator"
                                if cancelled
                                else "Discovery policy changed; a new job will use the saved policy",
                                completed_at=time.time(),
                            )
                            break
                    if not operation.cancelled():
                        await operation
                except asyncio.CancelledError:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    self.report.update(
                        phase=Phase.INTERRUPTED, error="Worker stopped during this job"
                    )
                    raise
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    ET.ParseError,
                    httpx.HTTPError,
                ) as error:
                    # The worker owns the failure state. The next explicit request
                    # or scheduled interval can retry without losing prior evidence.
                    self.report.update(
                        phase=Phase.FAILED, error=str(error), completed_at=time.time()
                    )
                finally:
                    await self.save(True)
            last_policy = policy
            if once:
                return
            await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, required=True, help="Credential-free discovery directory"
    )
    parser.add_argument("--privileged", action="store_true")
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    asyncio.run(Collector(options.state, options.privileged).run(options.once))


if __name__ == "__main__":
    main()
