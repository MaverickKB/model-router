"""OAuth engine credentials resolve to live access tokens at the chokepoint."""

import httpx
import pytest

from gateway.oauth import OAuthSession, Provider
from gateway.schema import Engine
from tests.test_gateway import setup  # noqa: F401  (pytest fixture)


@pytest.mark.asyncio
async def test_credential_set_accepts_oauth_type(setup):  # noqa: F811 (pytest fixture)
    (app, *_rest) = setup
    store = app.state.store
    engine = next(e for e in store.config().engines if e.kind == "cloud")
    op_token = app.state.identity.install_bootstrap_key()

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5123)),
            base_url="http://localhost",
            headers={
                "Authorization": f"Bearer {op_token}",
                "Origin": "http://localhost",
            },
        ) as client,
    ):
        response = await client.put(
            f"/api/v1/engines/{engine.id}/credential",
            json={"key": "encrypted-fresh", "type": "xai_oauth"},
        )
        assert response.status_code == 200, response.text

    saved = store.config().engines
    target = next(e for e in saved if e.id == engine.id)
    assert target.credential_type == "xai_oauth"
    # credential_type must round-trip through the discovery view
    view = next(v for v in app.state.discovery.views() if v["id"] == engine.id)
    assert view["credential_type"] == "xai_oauth"


@pytest.mark.asyncio
async def test_oauth_engine_uses_live_token_not_refresh_secret(setup, monkeypatch):  # noqa: F811 (pytest fixture)
    (app, *_rest) = setup
    store, discovery = app.state.store, app.state.discovery

    oauth = Engine(
        name="Grok OAuth",
        base_url="https://grok-oauth.test/v1",
        kind="cloud",
        model_patterns=["*"],
        credential_type="xai_oauth",
    )
    config = store.config()
    config.engines.append(oauth)
    store.save(config)
    store.set_secret(oauth.id, "encrypted-refresh-token")

    seen = {}

    async def fake_token(provider, refresh_token, http_client):
        seen["provider"] = provider
        seen["refresh"] = refresh_token
        return "live-access-token", None

    monkeypatch.setattr(discovery.oauth, "access_token", fake_token)

    headers = await discovery.headers(oauth)

    assert headers == {"Authorization": "Bearer live-access-token"}
    assert seen["provider"] == "xai_oauth"
    assert seen["refresh"] == "encrypted-refresh-token"


@pytest.mark.asyncio
async def test_oauth_rotated_refresh_is_persisted(setup, monkeypatch):  # noqa: F811 (pytest fixture)
    (app, *_rest) = setup
    store, discovery = app.state.store, app.state.discovery

    oauth = Engine(
        name="Codex OAuth",
        base_url="https://codex-oauth.test/v1",
        kind="cloud",
        model_patterns=["*"],
        credential_type="codex_oauth",
    )
    config = store.config()
    config.engines.append(oauth)
    store.save(config)
    store.set_secret(oauth.id, "old-refresh-token")

    async def fake_token(provider, refresh_token, http_client):
        return "live-access-token", "new-refresh-token"

    monkeypatch.setattr(discovery.oauth, "access_token", fake_token)

    await discovery.headers(oauth)

    assert store.secret(oauth.id) == "new-refresh-token"


@pytest.mark.asyncio
async def test_static_engine_uses_raw_secret(setup):  # noqa: F811 (pytest fixture)
    (app, *_rest) = setup
    discovery = app.state.discovery

    engine = next(e for e in app.state.store.config().engines if e.kind == "cloud")
    headers = await discovery.headers(engine)

    assert headers == {"Authorization": "Bearer demo-provider-key"}


@pytest.mark.asyncio
async def test_access_tokens_isolated_per_refresh_token():
    """Two engines sharing a provider must not share a cached access token."""
    issued = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        issued.append(data["refresh_token"])
        return httpx.Response(
            200, json={"access_token": f"at-{data['refresh_token']}", "expires_in": 3600}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OAuthSession()

    first = await session.access_token(Provider.CODEX.value, "token-a", http)
    second = await session.access_token(Provider.CODEX.value, "token-b", http)

    assert first[0] == "at-token-a"
    assert second[0] == "at-token-b"
    # A cached access token must not be reused cross-account: both POSTs fired
    assert issued == ["token-a", "token-b"]


@pytest.mark.asyncio
async def test_codex_refresh_posts_expected_grant():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["data"] = dict(
            pair.split("=", 1) for pair in request.content.decode().split("&")
        )
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OAuthSession()
    token = await session.access_token(
        Provider.CODEX.value, "refresh-token-value", http
    )

    assert token == ("fresh", None)
    assert captured["url"] == "https://auth.openai.com/oauth/token"
    assert captured["data"]["grant_type"] == "refresh_token"
    assert captured["data"]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert captured["data"]["refresh_token"] == "refresh-token-value"


@pytest.mark.asyncio
async def test_xai_discovers_token_endpoint_then_refreshes():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "well-known" in str(request.url):
            return httpx.Response(
                200,
                json={"token_endpoint": "https://auth.x.ai/oauth2/token"},
            )
        return httpx.Response(200, json={"access_token": "grok-fresh", "expires_in": 3600})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OAuthSession()
    token = await session.access_token(Provider.XAI.value, "grok-refresh", http)

    assert token == ("grok-fresh", None)
    assert calls == [
        "https://auth.x.ai/.well-known/openid-configuration",
        "https://auth.x.ai/oauth2/token",
    ]
