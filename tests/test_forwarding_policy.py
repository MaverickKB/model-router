import gzip

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Client, Configuration, Engine, Route, Security, Selector


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["default", "source", "key"])
async def test_retry_rechecks_current_request_authority(tmp_path, identity):
    calls = []

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            if identity == "key":
                app.state.store.revoke_keys(policy.id)
            else:
                cfg = app.state.store.config()
                replacement = Client(
                    name="Replacement policy",
                    route_names=["work"],
                    allow_cloud=False,
                    allow_network_auth=identity == "source",
                    source_networks=["192.0.2.10/32"] if identity == "source" else [],
                )
                cfg.clients = [replacement]
                cfg.security.anonymous_client_id = (
                    replacement.id if identity == "default" else None
                )
                app.state.store.save(cfg)
            return httpx.Response(503)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "backup"}}]}
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    policy = Client(
        name="Initial policy",
        route_names=["work"],
        allow_cloud=True,
        allow_network_auth=identity == "source",
        source_networks=["192.0.2.10/32"] if identity == "source" else [],
    )
    app.state.store.save(
        Configuration(
            engines=[
                Engine(name="Primary", base_url="http://primary.test/v1"),
                Engine(
                    name="Backup",
                    base_url="https://backup.test/v1",
                    kind="cloud",
                    model_patterns=["*"],
                ),
            ],
            clients=[policy],
            routes=[
                Route(
                    name="work",
                    fallback=Selector(kind="cloud"),
                    require_caller_key=identity == "key",
                )
            ],
            security=Security(
                anonymous_client_id=policy.id if identity == "default" else None
            ),
        )
    )
    key = app.state.store.issue_key(policy.id) if identity == "key" else "placeholder"
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.10", 1)),
            base_url="http://router.test",
            headers={"Authorization": f"Bearer {key}"},
        ) as http,
    ):
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "work", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == (401 if identity == "key" else 503)
    assert calls == ["primary.test"]
    assert all(e["inflight"] == 0 for e in app.state.discovery.views())


@pytest.mark.asyncio
async def test_compressed_upstream_stream_is_decoded(tmp_path):
    events = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

    class CompressedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = gzip.compress(events)
            yield body[:12]
            yield body[12:]

    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        return httpx.Response(
            200,
            stream=CompressedStream(),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/event-stream"},
        )

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    app.state.store.save(
        Configuration(engines=[Engine(name="API", base_url="http://provider.test/v1")])
    )
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://router.test"
        ) as http,
    ):
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    assert response.content == events
    assert "content-encoding" not in response.headers
    assert all(e["inflight"] == 0 for e in app.state.discovery.views())


@pytest.mark.asyncio
async def test_console_try_route_keeps_selected_policy(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    policy = Client(name="Selected test policy", route_names=["private"])
    app.state.store.save(
        Configuration(
            engines=[Engine(name="API", base_url="http://provider.test/v1")],
            clients=[policy],
            routes=[Route(name="private", require_caller_key=True)],
            security=Security(operator_auth_enabled=False),
        )
    )
    await app.state.discovery.refresh()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"Origin": "http://localhost"},
        ) as http,
    ):
        response = await http.post(
            "/api/v1/try-route", json={"route": "private", "client_id": policy.id}
        )
    assert response.status_code == 200
    assert app.state.store.events()[0]["client_id"] == policy.id
