import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.message import dump_messages_with_checkpoints

from astrbot_plugin_tool_scheduler.catalog import identify
from astrbot_plugin_tool_scheduler.policy import Decision
from astrbot_plugin_tool_scheduler.runtime import DISCOVERY_NAME

from .conftest import call, final


async def drain(run):
    async for _ in run.runner.step_until_done(10):
        pass
    await run.bridge.finish()


async def set_strategy(env, tool, strategy):
    await env.service.refresh()
    await env.service.change(
        "admin", env.service.revision, "tool", identify(tool).id, {"strategy": strategy}
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_mixed_full_and_two_deferred_tools_execute_once_with_real_history(
    env, streaming
):
    weather, search, report = (
        env.tool("weather", "city"),
        env.tool("search"),
        env.tool("report", "title"),
    )
    await set_strategy(env, search, "skills_like")
    await set_strategy(env, report, "skills_like")
    run = await env.run(
        [weather, search, report],
        [
            call(
                ("weather", {"city": "北京"}, "full-1"),
                ("search", {}, "select-1"),
                ("report", {}, "select-2"),
            ),
            call(
                ("search", {"query": "关键词"}, "real-1"),
                ("report", {"title": "报告"}, "real-2"),
            ),
            final(),
        ],
        streaming=streaming,
    )
    schemas = run.bridge.provider_tools()
    assert schemas.get_tool("weather").parameters["required"] == ["city"]
    assert schemas.get_tool("search").parameters["properties"] == {}
    await drain(run)
    assert env.executed == [
        ("weather", {"city": "北京"}),
        ("search", {"query": "关键词"}),
        ("report", {"title": "报告"}),
    ]
    assert run.provider.payloads[1]["func_tool"].names() == ["search", "report"]
    messages = dump_messages_with_checkpoints(run.wrapper.messages)
    text = json.dumps(messages, ensure_ascii=False)
    assert (
        "SCHEMA_ONLY_SENTINEL" not in text and "Tool scheduling is active" not in text
    )
    recorded = [message for message in messages if message.get("tool_calls")][0][
        "tool_calls"
    ]
    assert [item["id"] for item in recorded] == ["full-1", "real-1", "real-2"]
    assert json.loads(recorded[1]["function"]["arguments"]) == {"query": "关键词"}
    assert run.runner.tool_schema_mode == "full"
    assert "_resolve_tool_exec" not in run.runner.__dict__


async def test_second_call_in_same_task_uses_loaded_schema_without_extra_query(env):
    search = env.tool("search")
    await set_strategy(env, search, "skills_like")
    run = await env.run(
        [search],
        [
            call(("search", {}, "s")),
            call(("search", {"query": "first"}, "a")),
            call(("search", {"query": "second"}, "b")),
            final(),
        ],
    )
    await drain(run)
    assert len(run.provider.payloads) == 4
    assert run.provider.payloads[2]["func_tool"].get_tool("search").parameters[
        "required"
    ] == ["query"]
    stats = await env.service.store.window("default", 0)
    assert stats[identify(search).id]["calls"] == 2
    assert stats[identify(search).id]["requests"] == 1
    new_run = await env.run([search], [final()])
    assert (
        new_run.bridge.provider_tools().get_tool("search").parameters["properties"]
        == {}
    )
    await new_run.bridge.finish()


async def test_dormant_discovery_loads_schema_without_persisting_it(env):
    cloud = env.tool("word_cloud", description="根据聊天记录生成词云")
    await env.service.change("admin", 0, "settings", "global", {"smart": True})
    run = await env.run(
        [cloud],
        [
            call((DISCOVERY_NAME, {"query": "生成词云"}, "find")),
            call(("word_cloud", {"query": "聊天"}, "cloud")),
            final(),
        ],
    )
    run.bridge.decisions[identify(cloud).id] = Decision("dormant", "test")
    assert run.bridge.provider_tools().get_tool("word_cloud") is None
    await drain(run)
    assert env.executed == [("word_cloud", {"query": "聊天"})]
    assert run.provider.payloads[1]["func_tool"].get_tool("word_cloud").parameters[
        "required"
    ] == ["query"]
    text = json.dumps(
        dump_messages_with_checkpoints(run.wrapper.messages), ensure_ascii=False
    )
    assert "SCHEMA_ONLY_SENTINEL" not in text
    stats = await env.service.store.window("default", 0)
    assert stats[identify(cloud).id]["calls"] == 1
    assert all(DISCOVERY_NAME not in key for key in stats)


async def test_discovery_never_adds_tools_outside_original_request_or_manual_switches(
    env,
):
    permitted, absent = env.tool("permitted"), env.tool("absent")
    await env.service.change("admin", 0, "settings", "global", {"smart": True})
    run = await env.run([permitted], [final()])
    assert [
        item["name"] for item in json.loads(await run.bridge.discover(""))["loaded"]
    ] == ["permitted"]
    await env.service.change(
        "admin",
        env.service.revision,
        "tool",
        identify(permitted).id,
        {"enabled": False},
    )
    assert json.loads(await run.bridge.discover(""))["loaded"] == []
    assert absent.name not in run.bridge.tools
    assert run.bridge.provider_tools().get_tool("permitted") is None
    results = [
        result
        async for result in run.runner.tool_executor.execute(
            permitted, run.wrapper, query="x"
        )
    ]
    assert results[0].isError and not env.executed
    await run.bridge.finish()


async def test_global_block_disables_discovery_and_rechecks_an_inflight_call(env):
    tool = env.tool("search")
    run = await env.run([tool], [final()])
    await env.service.change(
        "admin", env.service.revision, "settings", "global", {"block_all": True}
    )
    assert run.bridge.provider_tools() is None
    results = [
        result
        async for result in run.runner.tool_executor.execute(
            tool, run.wrapper, query="x"
        )
    ]
    assert results[0].isError and not env.executed
    await run.bridge.finish()


async def test_permission_guard_is_preserved_and_denied_calls_do_not_raise_frequency(
    env,
):
    tool = env.tool("admin_only")
    tool._mgr = SimpleNamespace(_check_tool_permission=AsyncMock(return_value=None))
    run = await env.run([tool], [final()])
    tool._mgr._check_tool_permission.return_value = "error: Permission denied"
    assert json.loads(await run.bridge.discover(""))["loaded"] == []
    results = [
        result
        async for result in run.runner.tool_executor.execute(
            tool, run.wrapper, query="x"
        )
    ]
    assert results[0].isError and not env.executed
    stats = await env.service.store.window("default", 0)
    assert stats[identify(tool).id]["requests"] == 0
    assert stats[identify(tool).id]["denied"] == 1
    await run.bridge.finish()


async def test_parameter_generation_failure_preserves_full_call_and_never_executes_empty_deferred(
    env,
):
    full, deferred = env.tool("full"), env.tool("deferred")
    await set_strategy(env, deferred, "skills_like")
    run = await env.run(
        [full, deferred],
        [
            call(("full", {"query": "valid"}, "f"), ("deferred", {}, "d")),
            RuntimeError("provider unavailable"),
            final(),
        ],
    )
    await drain(run)
    assert env.executed == [("full", {"query": "valid"})]


async def test_empty_parameter_reply_retries_once_and_invalid_schema_cannot_execute(
    env,
):
    tool = env.tool("search")
    await set_strategy(env, tool, "skills_like")
    run = await env.run(
        [tool],
        [
            call(("search", {}, "s")),
            final(""),
            call(("search", {"wrong": "value"}, "bad")),
            final(),
        ],
    )
    await drain(run)
    assert not env.executed
    stats = await env.service.store.window("default", 0)
    assert (
        stats[identify(tool).id]["invalid"] == 1
        and stats[identify(tool).id]["requests"] == 0
    )


async def test_profiles_are_isolated_and_state_survives_reopen(env):
    tool = env.tool("search")
    run = await env.run(
        [tool], [call(("search", {"query": "x"}, "x")), final()], profile="profile-a"
    )
    await drain(run)
    assert (await env.service.store.window("profile-a", 0))[identify(tool).id][
        "requests"
    ] == 1
    assert await env.service.store.window("profile-b", 0) == {}
    await env.service.change(
        "admin",
        env.service.revision,
        "tool",
        identify(tool).id,
        {"strategy": "skills_like", "aliases": ["检索"]},
    )
    await env.service.store.close()
    await env.service.store.initialize()
    values, rules, _ = await env.service.store.load()
    assert rules[("tool", identify(tool).id)]["aliases"] == ["检索"]
    assert values["revision"] == env.service.revision


async def test_max_step_guard_cannot_be_overridden_by_schema_callback(env):
    tool = env.tool("search")
    run = await env.run([tool], [final()])
    run.request.func_tool = None
    assert run.bridge.provider_tools() is None
    await run.bridge.finish()
    assert run.request.func_tool is None


async def test_scheduler_never_mutates_global_activation_or_shared_schema(env):
    tool = env.tool("search")
    before = json.dumps(tool.parameters, sort_keys=True)
    await set_strategy(env, tool, "skills_like")
    run = await env.run([tool], [final()])
    run.bridge.provider_tools()
    assert tool.active is True and json.dumps(tool.parameters, sort_keys=True) == before
    await run.bridge.finish()


async def test_colliding_call_ids_keep_both_calls_and_their_provider_signatures(env):
    full, deferred = env.tool("full"), env.tool("deferred")
    await set_strategy(env, deferred, "skills_like")
    first = call(("full", {"query": "f"}, "same"), ("deferred", {}, "select"))
    first.tools_call_extra_content = {"same": {"signature": "full-signature"}}
    second = call(("deferred", {"query": "d"}, "same"))
    second.tools_call_extra_content = {"same": {"signature": "deferred-signature"}}
    run = await env.run([full, deferred], [first, second, final()])
    await drain(run)
    recorded = next(
        item["tool_calls"]
        for item in dump_messages_with_checkpoints(run.wrapper.messages)
        if item.get("tool_calls")
    )
    assert len({item["id"] for item in recorded}) == 2
    assert recorded[0]["extra_content"]["signature"] == "full-signature"
    assert recorded[1]["extra_content"]["signature"] == "deferred-signature"
    assert len(env.executed) == 2


async def test_telemetry_failure_does_not_repeat_side_effect_or_skip_detach(
    env, monkeypatch
):
    tool = env.tool("search")
    run = await env.run([tool], [call(("search", {"query": "once"}, "x")), final()])
    monkeypatch.setattr(
        env.service.store, "record_call", AsyncMock(side_effect=OSError("disk full"))
    )
    monkeypatch.setattr(
        env.service.store, "metric", AsyncMock(side_effect=OSError("disk full"))
    )
    await drain(run)
    assert env.executed == [("search", {"query": "once"})]
    assert "_resolve_tool_exec" not in run.runner.__dict__
    assert not env.service.bridges
    text = json.dumps(dump_messages_with_checkpoints(run.wrapper.messages))
    assert "result:search" in text and "Tool scheduling is active" not in text


async def test_switch_changed_while_permission_check_waits_blocks_execution(env):
    tool = env.tool("search")
    tool._mgr = SimpleNamespace(_check_tool_permission=AsyncMock(return_value=None))
    run = await env.run([tool], [final()])

    async def close_during_permission_check(*_):
        await env.service.change(
            "admin", env.service.revision, "tool", identify(tool).id, {"enabled": False}
        )
        return None

    tool._mgr._check_tool_permission.side_effect = close_during_permission_check
    results = [
        result
        async for result in run.runner.tool_executor.execute(
            tool, run.wrapper, query="x"
        )
    ]
    assert results[0].isError and not env.executed
    await run.bridge.finish()
