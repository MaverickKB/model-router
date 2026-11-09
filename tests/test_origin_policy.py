import json

import httpx
import pytest

from gateway.app import create_app
from gateway.origin_policy import PRIVATE_HEADER, Origin, OriginPolicy
from gateway.schema import Client, Configuration, Engine

PRIVATE_IP = "192.0.2.66"   # TEST-NET-1, documentation range
OTHER_IP = "192.0.2.10"
TAGGED_IP = "198.51.100.7"


def write_policy(tmp_path, monkeypatch, **extra):
    flag = tmp_path / "private.maintenance"
    policy = tmp_path / "origin_policy.json"
    origin = {"name": "private-advisor", "source_networks": [f"{PRIVATE_IP}/32"],
              "source_tags": ["tag:test-private"], "private": True,
              "allowed_routes": ["auto"], "maintenance_flag": str(flag),
              "allowed_paths": ["/v1/chat/completions", "/v1/completions",
                                "/chat/completions", "/completions", "/v1/models"],
              "strip_prefixes": ["dream_"], "strip_keys": ["capture", "eval"], **extra}
    policy.write_text(json.dumps({"origins": [origin], "tag_resolver": None}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(policy))
    monkeypatch.setenv("MODEL_ROUTER_MAINTENANCE_FLAG", str(tmp_path / "global.flag"))
    return policy, flag


def make_app(tmp_path, seen):
    async def upstream(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}, {"id": "other"}]})
        seen.append({"headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    app = create_app(str(tmp_path / "state"), background=False,
                     transport=httpx.MockTransport(upstream))
    caller = Client(
        name="Origin-policy fixture",
        route_names=["auto"],
        allow_direct_models=True,
        allow_network_auth=True,
        source_networks=[f"{PRIVATE_IP}/32", f"{OTHER_IP}/32"],
    )
    app.state.store.save(Configuration(
        engines=[Engine(name="API", base_url="http://provider.test/v1")],
        clients=[caller],
    ))
    return app


async def post(app, ip, body, path="/v1/chat/completions"):
    return (await posts(app, [(ip, body, path)]))[0]


async def posts(app, calls):
    await app.state.discovery.refresh()
    out = []
    async with app.router.lifespan_context(app):
        for ip, b, path in calls:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=(ip, 1)),
                base_url="http://router.test",
            ) as http:
                out.append(await http.post(path, json=b))
    return out


def body(model="auto", **kw):
    return {"model": model, "messages": [{"role": "user", "content": "x"}], **kw}


@pytest.mark.asyncio
async def test_private_origin_strips_capture_metadata_and_marks_header(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    r = await post(app, PRIVATE_IP, body(
        metadata={"dream_capture": {"tag": "glm-terminal-v1", "request_id": "r1"},
                  "eval_run": "e", "capture": 1, "keep": "yes"},
        dream_mode=True, capture=True, eval=True))
    assert r.status_code == 200
    sent = seen[0]
    assert sent["headers"][PRIVATE_HEADER.lower()] == "1"
    assert sent["body"]["metadata"] == {"keep": "yes"}
    for k in ("dream_mode", "capture", "eval"):
        assert k not in sent["body"]


@pytest.mark.asyncio
async def test_other_origins_unchanged(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    md = {"dream_capture": {"tag": "glm-terminal-v1", "request_id": "r1"}}
    r = await post(app, OTHER_IP, body(metadata=md))
    assert r.status_code == 200
    assert PRIVATE_HEADER.lower() not in seen[0]["headers"]
    assert seen[0]["body"]["metadata"] == md


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["model", "other", "nemotron", "spark"])
async def test_private_origin_auto_only(tmp_path, monkeypatch, model):
    write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    r = await post(app, PRIVATE_IP, body(model=model))
    assert r.status_code == 403
    assert "may only use these routes" in r.text
    assert seen == []


@pytest.mark.asyncio
async def test_other_origin_may_pick_model(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    r = await post(app, OTHER_IP, body(model="model"))
    assert "may only use these routes" not in r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["origin_flag", "global_flag", "config"])
async def test_maintenance_gate_returns_503_and_never_forwards(tmp_path, monkeypatch, how):
    _policy, flag = write_policy(tmp_path, monkeypatch, maintenance=(how == "config"))
    if how == "origin_flag":
        flag.write_text("")
    if how == "global_flag":
        (tmp_path / "global.flag").write_text("")
    seen = []
    app = make_app(tmp_path, seen)
    r = await post(app, PRIVATE_IP, body())
    assert r.status_code == 503
    detail = r.json()["detail"]["error"]
    assert detail["code"] == "down_for_maintenance"
    assert "down for maintenance" in detail["message"]
    assert seen == []


@pytest.mark.asyncio
async def test_origin_flag_does_not_gate_others(tmp_path, monkeypatch):
    _, flag = write_policy(tmp_path, monkeypatch)
    flag.write_text("")
    seen = []
    app = make_app(tmp_path, seen)
    assert (await post(app, OTHER_IP, body())).status_code == 200


@pytest.mark.asyncio
async def test_gate_toggles_live_without_restart(tmp_path, monkeypatch):
    _, flag = write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    await app.state.discovery.refresh()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(PRIVATE_IP, 1)),
        base_url="http://router.test",
    ) as http:
        flag.write_text("")
        assert (await http.post("/v1/chat/completions", json=body())).status_code == 503
        flag.unlink()
        assert (await http.post("/v1/chat/completions", json=body())).status_code == 200


@pytest.mark.asyncio
async def test_private_events_excluded_from_exports(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    seen = []
    app = make_app(tmp_path, seen)
    await posts(app, [(PRIVATE_IP, body(), "/v1/chat/completions"),
                      (OTHER_IP, body(), "/v1/chat/completions")])
    events = app.state.store.events()
    assert len(events) == 1
    assert events[0]["caller"]["source_address"] == OTHER_IP
    # untagged legacy rows from the private source are excluded too
    app.state.store.event({"id": "legacy", "ts": 1.0, "caller": {"source_address": PRIVATE_IP}})
    assert all(e["id"] != "legacy" for e in app.state.store.events())


@pytest.mark.asyncio
async def test_private_origin_blocked_from_other_endpoints(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    app = make_app(tmp_path, [])
    r = await post(app, PRIVATE_IP, {"model": "auto"}, path="/v1/systemone")
    assert r.status_code == 403


def test_tag_classification_via_lookup(tmp_path, monkeypatch):
    write_policy(tmp_path, monkeypatch)
    p = OriginPolicy(str(tmp_path), tag_lookup=lambda a: ["tag:test-private"] if a == TAGGED_IP else [])
    assert p.classify(TAGGED_IP).name == "private-advisor"
    assert p.classify(OTHER_IP) is None
    assert p.classify(PRIVATE_IP).name == "private-advisor"


def test_no_policy_file_means_no_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(tmp_path / "missing.json"))
    assert OriginPolicy(str(tmp_path)).classify(PRIVATE_IP) is None


# --- neutral defaults / genericity ---------------------------------------

def test_origin_defaults_are_neutral():
    o = Origin.parse({"name": "x", "source_networks": ["192.0.2.0/24"]})
    assert o.private is False and o.allowed_routes is None and o.allowed_paths is None
    assert o.strip_prefixes == [] and o.strip_keys == [] and o.maintenance is False
    payload = {"model": "m", "metadata": {"dream_capture": 1, "eval": 2}}
    assert o.sanitize(payload) == 0 and payload["metadata"] == {"dream_capture": 1, "eval": 2}
    assert o.route_allowed("anything") and o.path_allowed("/any")


@pytest.mark.asyncio
async def test_no_policy_file_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(tmp_path / "missing.json"))
    monkeypatch.setenv("MODEL_ROUTER_MAINTENANCE_FLAG", str(tmp_path / "global.flag"))
    (tmp_path / "global.flag").write_text("")  # gate applies only to configured origins
    seen = []
    app = make_app(tmp_path, seen)
    md = {"dream_capture": {"tag": "t"}}
    r = await post(app, PRIVATE_IP, body(metadata=md))
    assert r.status_code == 200 and seen[0]["body"]["metadata"] == md
    assert PRIVATE_HEADER.lower() not in seen[0]["headers"]


def test_tag_resolver_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_TAILSCALE_TAGS", raising=False)
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"origins": [{"name": "t", "source_tags": ["tag:x"]}]}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(p))
    pol = OriginPolicy(str(tmp_path))
    assert pol.resolver is None and pol.classify(TAGGED_IP) is None


def test_pluggable_resolver_by_module_path(tmp_path, monkeypatch):
    import sys
    import types
    mod = types.ModuleType("fake_resolver_mod")
    mod.resolve = lambda a: ["tag:x"] if a == TAGGED_IP else []
    monkeypatch.setitem(sys.modules, "fake_resolver_mod", mod)
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"tag_resolver": "fake_resolver_mod:resolve",
                             "origins": [{"name": "t", "source_tags": ["tag:x"]}]}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(p))
    pol = OriginPolicy(str(tmp_path))
    assert pol.classify(TAGGED_IP).name == "t" and pol.classify(OTHER_IP) is None


def test_legacy_tailscale_env_alias_selects_tailscale_resolver(tmp_path, monkeypatch):
    from gateway import origin_resolvers
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"origins": [{"name": "t", "source_tags": ["tag:x"]}]}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(p))
    monkeypatch.setenv("MODEL_ROUTER_TAILSCALE_TAGS", "1")
    assert OriginPolicy(str(tmp_path)).resolver is origin_resolvers.tailscale_tags


def test_tailscale_resolver_absent_cli_returns_empty(monkeypatch):
    from gateway import origin_resolvers
    monkeypatch.setattr(origin_resolvers, "TAILSCALE_BIN", "definitely-not-installed-xyz")
    origin_resolvers._CACHE.clear()
    assert origin_resolvers.tailscale_tags("192.0.2.1") == []


def test_origin_name_header_optional(tmp_path, monkeypatch):
    p = tmp_path / "p.json"
    o = {"name": "n", "source_networks": [f"{PRIVATE_IP}/32"], "private": True}
    p.write_text(json.dumps({"origins": [o]}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(p))
    pol = OriginPolicy(str(tmp_path))
    assert pol.upstream_headers(pol.classify(PRIVATE_IP)) == {PRIVATE_HEADER: "1"}
    p.write_text(json.dumps({"origins": [o], "origin_name_header": True,
                             "private_header": "X-Private"}))
    import os

    os.utime(p, (1, 1))
    pol.reload()
    assert pol.upstream_headers(pol.classify(PRIVATE_IP)) == {"X-Private": "1", "X-Router-Origin": "n"}


def test_example_config_parses():
    import pathlib
    ex = pathlib.Path(__file__).resolve().parents[1] / "examples" / "origin_policy.example.json"
    raw = json.loads(ex.read_text())
    assert [Origin.parse(o).name for o in raw["origins"]]


def test_static_cidr_resolver_default_when_static_tags_given(tmp_path, monkeypatch):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"static_tags": {"tag:x": ["198.51.100.0/24"]},
                             "origins": [{"name": "t", "source_tags": ["tag:x"]}]}))
    monkeypatch.setenv("MODEL_ROUTER_ORIGIN_POLICY", str(p))
    monkeypatch.delenv("MODEL_ROUTER_TAILSCALE_TAGS", raising=False)
    pol = OriginPolicy(str(tmp_path))
    assert pol.classify(TAGGED_IP).name == "t" and pol.classify(OTHER_IP) is None


def test_local_site_config_is_behavior_identical():
    import pathlib
    local = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "origin_policy.local.json"
    if not local.exists():
        pytest.skip("site config is local-only")
    raw = json.loads(local.read_text())
    o = Origin.parse(raw["origins"][0])
    assert raw["tag_resolver"] == "tailscale" and raw["origin_name_header"] is True
    assert o.private and o.allowed_routes == ["auto"] and o.allowed_paths
    assert o.strip_prefixes == ["dream_"] and set(o.strip_keys) == {"capture", "eval"}


def test_readme_links_optional_extras():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    for rel in ("docs/ORIGIN_POLICY.md", "examples/origin_policy.example.json",
                "gateway/origin_resolvers.py", "scripts/origin-gate.sh"):
        assert f"]({rel})" in readme and (root / rel).exists()
