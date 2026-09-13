"""GitHub release checks never apply themselves; the operator chooses."""

import httpx
import pytest

from gateway.app import create_app
from gateway.updates import newer


def test_newer_orders_releases():
    assert newer("0.3.1", "0.3.0")
    assert newer("v0.4.0", "0.3.9")
    assert newer("0.3.0", "0.3.0-beta.1")
    assert not newer("0.3.0-beta.1", "0.3.0")
    assert not newer("0.3.0", "0.3.0")


@pytest.mark.asyncio
async def test_update_check_reports_a_newer_release_without_applying(tmp_path, monkeypatch):
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200,
                json=[
                    {
                        "tag_name": "v0.3.1",
                        "draft": False,
                        "prerelease": True,
                        "body": "Fix discovery prefixes.",
                        "html_url": "https://github.com/example/model-router/releases/tag/v0.3.1",
                    }
                ],
            )
        return httpx.Response(404)

    monkeypatch.setattr("gateway.updates.installed_version", lambda: "0.3.0")
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    ) as client:
        status = await client.get("/api/v1/updates")
        assert status.status_code == 200
        body = status.json()
        assert body["available"] is True
        assert body["latest"] == "0.3.1"
        assert "Fix discovery prefixes." in body["notes"]
        assert calls
        disabled = app.state.store.config()
        disabled.updates.check_enabled = False
        app.state.store.save(disabled)
        quiet = await client.get("/api/v1/updates")
        assert quiet.json()["available"] is False
        assert quiet.json()["check_enabled"] is False


@pytest.mark.asyncio
async def test_apply_refuses_when_the_tree_is_dirty(tmp_path, monkeypatch):
    async def handler(request):
        if request.url.path.endswith("/releases"):
            return httpx.Response(
                200, json=[{"tag_name": "v0.3.1", "draft": False, "body": "notes"}]
            )
        return httpx.Response(404)

    monkeypatch.setattr("gateway.updates.installed_version", lambda: "0.3.0")
    app = create_app(
        str(tmp_path), background=False, transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(app.state.updates, "_dirty", lambda: True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://localhost",
        headers={"Origin": "http://localhost"},
    ) as client:
        refused = await client.post("/api/v1/updates/apply", json={"tag": "v0.3.1"})
        assert refused.status_code == 409
        assert "local changes" in refused.json()["detail"]
