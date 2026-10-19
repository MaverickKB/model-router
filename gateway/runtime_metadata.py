"""Read optional serving limits without tying routing to a backend's identity."""

from urllib.parse import urlsplit, urlunsplit

from .adapters.http import json_document


def positive_integer(*values):
    for value in values:
        if type(value) is int and value > 0:
            return value
    return None


async def serving_limits(http, engine, headers):
    if engine.kind != "local" or engine.protocol != "openai":
        return {}
    parts = urlsplit(engine.base_url)
    origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    health = await json_document(http, origin + "/health", headers=headers) or {}
    streams = health.get("streams")
    streams = streams if isinstance(streams, dict) else {}
    context = positive_integer(health.get("context_length"), health.get("max_model_len"))
    capacity = positive_integer(health.get("max_concurrent_requests"), health.get("max_inflight"), streams.get("max"))
    if context is None and capacity is None:
        properties = await json_document(http, origin + "/props", headers=headers) or {}
        settings = properties.get("default_generation_settings")
        settings = settings if isinstance(settings, dict) else {}
        context = positive_integer(properties.get("n_ctx"), settings.get("n_ctx"))
        capacity = positive_integer(properties.get("total_slots"), properties.get("n_parallel"))
    return {"context_length": context, "max_inflight": capacity}
