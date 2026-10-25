import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from fastapi import FastAPI, Request

from astrbot_plugin_tool_scheduler.catalog import identify
from astrbot_plugin_tool_scheduler.web import PLUGIN, Dashboard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
async def web(env):
    dashboard = Dashboard(env.context, env.service)
    app = FastAPI()

    @app.api_route("/{action}", methods=["GET", "POST"])
    async def dispatch(action: str, request: Request):
        username = request.headers.get("x-test-username", "admin")
        with bind_request_context(
            PluginRequest(request, username=username, plugin_name=PLUGIN)
        ):
            return await dashboard.dispatch(action)

    token = jwt.encode(
        {"username": "admin", "exp": time.time() + 3600},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + token},
    ) as client:
        yield client, dashboard
    dashboard.close()


async def test_dashboard_saves_policies_and_rejects_stale_revisions(env, web):
    client, _ = web
    tool = env.tool("search")
    state = (await client.get("/state", params={"profile": "default"})).json()
    assert state["settings"]["smart"] is False
    body = {
        "csrf": state["csrf"],
        "revision": state["revision"],
        "scope": "tool",
        "subject": identify(tool).id,
        "policy": {"strategy": "skills_like", "aliases": ["检索"]},
    }
    assert (await client.post("/policy", json=body)).status_code == 200
    assert (await client.post("/policy", json=body)).status_code == 409
    updated = (await client.get("/state", params={"profile": "default"})).json()
    assert updated["tools"][0]["effective"]["mode"] == "skills_like"
    assert (await client.get("/audit")).json()["items"][0]["actor"] == "admin"


async def test_dashboard_rejects_missing_jwt_api_keys_and_wrong_csrf(env, web):
    client, _ = web
    assert (
        await client.get("/state", headers={"Authorization": ""})
    ).status_code == 403
    assert (
        await client.get("/state", headers={"Authorization": "Bearer invalid"})
    ).status_code == 403
    state = (await client.get("/state")).json()
    assert (
        await client.post(
            "/settings",
            json={"csrf": "wrong", "revision": 0, "settings": {"smart": True}},
        )
    ).status_code == 403
    assert env.service.settings["smart"] is False
    from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

    assert (
        await client.get(
            "/state", headers={"x-test-username": API_KEY_USERNAME_PREFIX + "key"}
        )
    ).status_code == 403
    assert (
        await client.get("/state", headers={"x-test-username": "other"})
    ).status_code == 403
    assert (
        await client.post(
            "/settings",
            json={"csrf": state["csrf"], "revision": 0, "settings": {"smart": "yes"}},
        )
    ).status_code == 400


async def test_page_tokens_are_not_accepted_as_admin_credentials(env, web):
    client, _ = web
    token = jwt.encode(
        {"username": "admin", "exp": time.time() + 3600, "token_type": "plugin_page"},
        env.context.get_config()["dashboard"]["jwt_secret"],
        algorithm="HS256",
    )
    assert (
        await client.get("/state", headers={"Authorization": "Bearer " + token})
    ).status_code == 403


async def test_plugin_defaults_and_child_overrides_are_visible_in_ui(env, web):
    first, second = env.tool("first"), env.tool("second")
    client, _ = web
    await client.get("/state")
    owner = identify(first).owner
    await env.service.change("admin", 0, "owner", owner, {"strategy": "skills_like"})
    await env.service.change(
        "admin", 1, "tool", identify(second).id, {"strategy": "full"}
    )
    snapshot = (await client.get("/state", params={"profile": "default"})).json()
    assert {tool["name"]: tool["effective"]["mode"] for tool in snapshot["tools"]} == {
        "first": "skills_like",
        "second": "full",
    }
    await env.service.change("admin", 2, "owner", owner, {"enabled": False})
    snapshot = (await client.get("/state", params={"profile": "default"})).json()
    assert all(tool["effective"]["mode"] == "off" for tool in snapshot["tools"])


async def test_aggregate_status_respects_auto_override_of_fixed_owner(env, web):
    tool = env.tool("search")
    client, _ = web
    await client.get("/state")
    await env.service.change(
        "admin", 0, "owner", identify(tool).owner, {"strategy": "full"}
    )
    await env.service.change(
        "admin", 1, "tool", identify(tool).id, {"strategy": "auto"}
    )
    snapshot = (await client.get("/state")).json()
    assert snapshot["tools"][0]["effective"]["mode"] == "mixed"


async def test_page_and_route_metadata_are_loadable(env, web):
    import yaml
    from astrbot.core.star.star_manager import PluginManager

    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == PLUGIN
    loaded = PluginManager._load_plugin_metadata(str(ROOT))
    assert loaded.name == PLUGIN and loaded.display_name == "工具调度器"
    assert (ROOT / "pages/scheduler/index.html").is_file()
    assert json.loads((ROOT / "_conf_schema.json").read_text()) == {}
    _, dashboard = web
    dashboard.register()
    assert len(env.context.registered_web_apis) == 4
    dashboard.close()
    assert not env.context.registered_web_apis
