import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool
from astrbot.core.star.star import StarMetadata, star_map
from mcp.types import CallToolResult, TextContent, Tool

from astrbot_plugin_tool_scheduler.catalog import identify
from astrbot_plugin_tool_scheduler.main import SESSION_KEY, Main

from .conftest import call, final


async def test_real_default_agent_hook_attaches_and_restores_instance(env, monkeypatch):
    tool = env.tool("search")
    await env.service.refresh()
    await env.service.change(
        "admin", 0, "tool", identify(tool).id, {"strategy": "skills_like"}
    )
    run = await env.run(
        [tool],
        [
            call(("search", {}, "selection")),
            call(("search", {"query": "real"}, "real")),
            final(),
        ],
        install=False,
    )
    plugin = Main.__new__(Main)
    plugin.service, plugin.ready, plugin.logger = (
        env.service,
        True,
        logging.getLogger("test"),
    )
    monkeypatch.setitem(
        __import__(
            Main.__module__, fromlist=["_ACTIVE_AGENT_RUNNERS"]
        )._ACTIVE_AGENT_RUNNERS,
        run.event.unified_msg_origin,
        run.runner,
    )

    class Hooks(BaseAgentRunHooks):
        async def on_agent_begin(self, context):
            await plugin.on_agent_begin(run.event, context)

        async def on_agent_done(self, context, response):
            await plugin.on_agent_done(run.event, context, response)

    run.runner.agent_hooks = Hooks()
    async for _ in run.runner.step_until_done(10):
        pass
    assert env.executed == [("search", {"query": "real"})]
    assert run.event.get_extra(SESSION_KEY) is None
    assert run.runner.tool_schema_mode == "full"
    assert not env.service.bridges


async def test_observed_frequency_promotes_and_zero_use_can_be_rediscovered(env):
    hot, cold = env.tool("hot"), env.tool("cold")
    await env.service.change(
        "admin",
        0,
        "settings",
        "global",
        {
            "smart": True,
            "promote_requests": 2,
            "demote_requests": 1,
            "min_opportunities": 2,
            "observe_days": 1,
        },
    )
    for index in range(2):
        run = await env.run(
            [hot, cold],
            [
                call(("hot", {}, "s")),
                call(("hot", {"query": str(index)}, "r")),
                final(),
            ],
        )
        async for _ in run.runner.step_until_done(10):
            pass
        await run.bridge.finish()
    await env.service.store.run(
        lambda connection: connection.execute(
            "UPDATE states SET first_seen=? WHERE tool_id=?",
            (time.time() - 2 * 86400, identify(cold).id),
        )
    )
    run = await env.run([hot, cold], [final()])
    assert run.bridge.decisions[identify(hot).id].mode == "full"
    assert run.bridge.decisions[identify(cold).id].mode == "dormant"
    assert run.bridge.provider_tools().get_tool("cold") is None
    await run.bridge.discover("cold")
    assert run.bridge.provider_tools().get_tool("cold") is not None
    await run.bridge.finish()


async def test_zero_parameter_full_tool_does_not_enter_parameter_generation(env):
    tool = env.tool("no_arguments")
    tool.parameters = {"type": "object", "properties": {}}
    run = await env.run([tool], [call((tool.name, {}, "direct")), final()])
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()
    assert len(run.provider.payloads) == 2
    assert env.executed == [(tool.name, {})]


@pytest.mark.parametrize("partial", [False, True])
async def test_failed_attach_disables_request_and_restores_partial_changes(
    env, monkeypatch, partial
):
    tool = env.tool("search")
    await env.service.refresh()
    await env.service.change(
        "admin", 0, "tool", identify(tool).id, {"strategy": "skills_like"}
    )
    run = await env.run([tool], [final()], install=False)
    plugin = Main.__new__(Main)
    plugin.service, plugin.ready, plugin.logger = (
        env.service,
        True,
        logging.getLogger("test"),
    )
    module = __import__(Main.__module__, fromlist=["_ACTIVE_AGENT_RUNNERS"])
    monkeypatch.setitem(
        module._ACTIVE_AGENT_RUNNERS, run.event.unified_msg_origin, run.runner
    )
    if partial:
        monkeypatch.setattr(
            "astrbot_plugin_tool_scheduler.runtime.TextPart",
            lambda **_: (_ for _ in ()).throw(RuntimeError("prompt failure")),
        )
    else:
        monkeypatch.setattr(
            env.service, "begin", AsyncMock(side_effect=OSError("disk failure"))
        )
    await plugin.on_agent_begin(run.event, run.wrapper)
    assert run.request.func_tool is None
    assert run.runner._skill_like_raw_tool_set is None
    assert "_resolve_tool_exec" not in run.runner.__dict__
    assert not env.service.bridges and not env.executed
    assert env.service.unsupported_runs == 1


async def test_host_stop_during_parameter_query_cleans_up_without_calling_tool(env):
    tool = env.tool("search")
    await env.service.refresh()
    await env.service.change(
        "admin", 0, "tool", identify(tool).id, {"strategy": "skills_like"}
    )

    async def stop(_):
        run.runner.request_stop()
        return call(("search", {"query": "must not execute"}, "real"))

    run = await env.run([tool], [call(("search", {}, "select")), stop])

    class Hooks(BaseAgentRunHooks):
        async def on_agent_done(self, context, response):
            await run.bridge.finish()

    run.runner.agent_hooks = Hooks()
    async for _ in run.runner.step_until_done(10):
        pass
    assert run.runner.was_aborted() and not env.executed
    assert not env.service.bridges and "_resolve_tool_exec" not in run.runner.__dict__


async def test_native_skills_like_mode_can_mix_a_fixed_full_tool(env):
    full, deferred = env.tool("full"), env.tool("deferred")
    await env.service.refresh()
    await env.service.change(
        "admin", 0, "tool", identify(full).id, {"strategy": "full"}
    )
    run = await env.run(
        [full, deferred],
        [
            call(("full", {"query": "f"}, "f"), ("deferred", {}, "select")),
            call(("deferred", {"query": "d"}, "d")),
            final(),
        ],
        native_mode="skills_like",
    )
    assert run.bridge.provider_tools().get_tool("full").parameters["required"] == [
        "query"
    ]
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()
    assert env.executed == [("full", {"query": "f"}), ("deferred", {"query": "d"})]
    assert run.runner.tool_schema_mode == "skills_like"


async def test_real_permission_proxy_preserves_plugin_identity_and_checks(
    env, monkeypatch
):
    tool = env.tool("guarded")
    tool.handler_module_path = "data.plugins.demo.main"
    metadata = StarMetadata(name="demo", display_name="示例插件")
    monkeypatch.setitem(star_map, tool.handler_module_path, metadata)
    manager = SimpleNamespace(_check_tool_permission=AsyncMock(return_value=None))
    guarded = _PermissionGuardedTool(tool, manager)
    assert guarded.handler is None and identify(guarded).owner == "plugin:demo"
    run = await env.run([guarded], [call(("guarded", {"query": "x"}, "x")), final()])
    assert run.bridge.tools["guarded"] is guarded
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()
    assert env.executed == [("guarded", {"query": "x"})]
    assert manager._check_tool_permission.await_count >= 3


async def test_mcp_uses_original_server_name_arguments_and_result(env):
    client = SimpleNamespace(
        call_tool_with_reconnect=AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text="MCP response")]
            )
        )
    )
    tool = MCPTool(
        Tool(
            name="files.read",
            description="读取文件",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        client,
        "file-server",
    )
    env.manager.func_list.append(tool)
    await env.service.refresh()
    await env.service.change(
        "admin", 0, "tool", identify(tool).id, {"strategy": "skills_like"}
    )
    run = await env.run(
        [tool],
        [
            call(("files_read", {}, "s")),
            call(("files_read", {"path": "example.txt"}, "r")),
            final(),
        ],
    )
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()
    arguments = client.call_tool_with_reconnect.call_args.kwargs
    assert arguments["tool_name"] == "files.read" and arguments["arguments"] == {
        "path": "example.txt"
    }
    assert identify(tool).owner == "mcp:file-server"
    assert (await env.service.store.window("default", 0))[identify(tool).id][
        "calls"
    ] == 1


async def test_owner_usage_deduplicates_two_tools_in_one_request(env):
    first, second = env.tool("first"), env.tool("second")
    run = await env.run(
        [first, second],
        [
            call(("first", {"query": "a"}, "a"), ("second", {"query": "b"}, "b")),
            final(),
        ],
    )
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()
    snapshot = await env.service.snapshot("default")
    assert snapshot["owners"][0]["requests"] == 1
    assert snapshot["owners"][0]["calls"] == 2
