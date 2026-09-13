"""Optional mDNS model-service announcements, scoped before catalog requests."""

import asyncio

try:
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
except ImportError:
    AsyncZeroconf = None


def endpoint(address: str, port: int) -> str:
    """Return the advertised transport origin without assuming an API path."""
    authority = f"[{address}]" if ":" in address else address
    return f"http://{authority}:{port}"


async def listen(candidates, trusted_host, capacity):
    if AsyncZeroconf is None:
        raise RuntimeError("mDNS discovery requires the optional discovery package")
    azc = AsyncZeroconf()
    pending = set()
    advertised = {}

    async def resolve(service_type, name):
        info = AsyncServiceInfo(service_type, name)
        if await info.async_request(azc.zeroconf, 2000):
            urls = set()
            for address in info.parsed_addresses():
                if await trusted_host(address) and len(candidates) < capacity:
                    urls.add(endpoint(address, info.port))
            candidates.difference_update(advertised.get(name, set()))
            advertised[name] = urls
            candidates.update(urls)

    def changed(zeroconf, service_type, name, state_change):
        if state_change == ServiceStateChange.Removed:
            candidates.difference_update(advertised.pop(name, set()))
        elif len(pending) < 32 and (name in advertised or len(advertised) < capacity):
            task = asyncio.create_task(resolve(service_type, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(
        azc.zeroconf, ["_model-serving._tcp.local."], handlers=[changed]
    )
    try:
        await asyncio.Future()
    finally:
        await browser.async_cancel()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await azc.async_close()
        candidates.clear()
