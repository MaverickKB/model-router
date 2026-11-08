"""Registration is a state-changing management POST: same-origin in every mode."""

import httpx
import pytest

from gateway.app import create_app
from gateway.schema import Security

REFUSED = "Settings require a same-origin operator request"


@pytest.mark.asyncio
async def test_register_requires_same_origin_when_operator_auth_is_off(tmp_path):
    app = create_app(str(tmp_path), background=False)
    store = app.state.store
    config = store.config()
    config.security = Security(operator_auth_enabled=False)
    store.save(config)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
        base_url="http://router.test",
    ) as http:
        blocked = await http.post(
            "/v1/gateway/register", json={"base_url": "http://127.0.0.1:9991/v1"}
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == REFUSED

        # Same origin passes the CSRF test and fails only on discovery scope,
        # which proves the origin check is not what refuses it.
        same_origin = await http.post(
            "/v1/gateway/register",
            json={"base_url": "http://127.0.0.1:9991/v1"},
            headers={"Origin": "http://router.test"},
        )
        assert same_origin.status_code == 403
        assert same_origin.json()["detail"] == (
            "Registration is outside the configured discovery scope"
        )


@pytest.mark.asyncio
async def test_same_origin_registration_still_succeeds_without_operator_auth(tmp_path):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        return httpx.Response(404)

    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(upstream)
    )
    store = app.state.store
    config = store.config()
    config.security = Security(operator_auth_enabled=False)
    config.discovery.targets = ["127.0.0.1"]
    store.save(config)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://router.test",
            headers={"Origin": "http://router.test"},
        ) as http,
    ):
        registered = await http.post(
            "/v1/gateway/register", json={"base_url": "http://127.0.0.1:9991/v1"}
        )

    # Accepted for the operator to confirm; the point is that the origin check
    # did not refuse a same-origin registration.
    assert registered.status_code in {200, 202}, registered.text
