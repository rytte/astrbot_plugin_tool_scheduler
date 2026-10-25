import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.message import dump_messages_with_checkpoints

from astrbot_plugin_tool_scheduler.catalog import identify
from astrbot_plugin_tool_scheduler.main import Main
from astrbot_plugin_tool_scheduler.service import Scheduler
from astrbot_plugin_tool_scheduler.storage import Store

from .conftest import call, final
from .test_runtime import drain, set_strategy

HISTORY = [
    {"role": "user", "content": "上一轮"},
    {"role": "assistant", "content": "已收到"},
]


async def test_automatic_promotion_waits_for_new_conversation_and_reset(env):
    tool = env.tool("search")
    await env.service.change(
        "admin",
        0,
        "settings",
        "global",
        {"smart": True, "promote_requests": 2, "demote_requests": 1},
    )
    original = await env.run([tool], [final()], conversation_id="original")
    await drain(original)
    for index in range(2):
        run = await env.run(
            [tool],
            [
                call(("search", {}, "select")),
                call(("search", {"query": "q"}, "real")),
                final(),
            ],
            conversation_id="other-" + str(index),
        )
        await drain(run)
    same = await env.run([tool], [final()], conversation_id="original", history=HISTORY)
    assert same.bridge.decisions[identify(tool).id].mode == "skills_like"
    assert (
        same.bridge.provider_tools().get_tool("search").parameters["properties"] == {}
    )
    preview = await env.service.snapshot("default")
    assert preview["tools"][0]["effective"]["mode"] == "full"
    assert preview["tools"][0]["pending_sessions"] >= 1
    await drain(same)
    fresh = await env.run([tool], [final()], conversation_id="new")
    assert fresh.bridge.decisions[identify(tool).id].mode == "full"
    await drain(fresh)
    reset = await env.run([tool], [final()], conversation_id="original", history=[])
    assert reset.bridge.session.epoch != original.bridge.session.epoch
    assert reset.bridge.decisions[identify(tool).id].mode == "full"
    await drain(reset)


async def test_expansion_survives_rounds_switching_conversations_and_restart(env):
    tool = env.tool("search")
    await set_strategy(env, tool, "skills_like")
    first = await env.run(
        [tool],
        [call(("search", {}, "s")), call(("search", {"query": "x"}, "r")), final()],
        conversation_id="a",
    )
    await drain(first)
    other = await env.run([tool], [final()], conversation_id="b")
    assert (
        other.bridge.provider_tools().get_tool("search").parameters["properties"] == {}
    )
    await drain(other)
    await env.service.close()
    reopened = Scheduler(env.context, Store(env.path / "scheduler.sqlite3"))
    await reopened.initialize()
    # The fixture's closure keeps the existing service object; transfer its reopened state.
    env.service.__dict__.update(reopened.__dict__)
    same = await env.run(
        [tool],
        [call(("search", {"query": "direct"}, "direct")), final()],
        conversation_id="a",
        history=HISTORY,
    )
    assert same.bridge.provider_tools().get_tool("search").parameters["required"] == [
        "query"
    ]
    await drain(same)
    assert len(same.provider.payloads) == 2
    recorded = dump_messages_with_checkpoints(same.wrapper.messages)
    assert "SCHEMA_ONLY_SENTINEL" not in json.dumps(recorded)
    reset = await env.run([tool], [final()], conversation_id="a", history=[])
    assert (
        reset.bridge.provider_tools().get_tool("search").parameters["properties"] == {}
    )
    await drain(reset)


async def test_admin_changes_apply_to_active_and_idle_conversations_immediately(env):
    tool, unaffected = env.tool("search"), env.tool("other")
    await set_strategy(env, tool, "skills_like")
    active = await env.run([tool, unaffected], [final()], conversation_id="a")
    idle = await env.run([tool, unaffected], [final()], conversation_id="b")
    await drain(idle)
    await set_strategy(env, tool, "full")
    assert active.bridge.provider_tools().get_tool("search").parameters["required"] == [
        "query"
    ]
    await drain(active)
    next_idle = await env.run(
        [tool, unaffected], [final()], conversation_id="b", history=HISTORY
    )
    assert next_idle.bridge.provider_tools().get_tool("search").parameters[
        "required"
    ] == ["query"]
    await set_strategy(env, tool, "skills_like")
    assert (
        next_idle.bridge.provider_tools().get_tool("search").parameters["properties"]
        == {}
    )
    await env.service.change(
        "admin", env.service.revision, "tool", identify(tool).id, {"enabled": False}
    )
    assert next_idle.bridge.provider_tools().get_tool("search") is None
    denied = [
        item
        async for item in next_idle.runner.tool_executor.execute(
            tool, next_idle.wrapper, query="blocked"
        )
    ]
    assert denied[0].isError and not env.executed
    await env.service.change(
        "admin", env.service.revision, "tool", identify(tool).id, {"enabled": True}
    )
    assert next_idle.bridge.provider_tools().get_tool("search") is not None
    await drain(next_idle)


async def test_admin_can_force_expanded_tool_back_to_skills_like(env):
    tool = env.tool("search")
    await set_strategy(env, tool, "skills_like")
    run = await env.run([tool], [final()], conversation_id="a")
    await env.service.expand(run.bridge, [identify(tool).id])
    assert run.bridge.provider_tools().get_tool("search").parameters["required"] == [
        "query"
    ]
    await set_strategy(env, tool, "skills_like")
    assert run.bridge.provider_tools().get_tool("search").parameters["properties"] == {}
    await drain(run)


@pytest.mark.parametrize(
    "initial,target", [("skills_like", "full"), ("full", "skills_like")]
)
async def test_policy_change_while_model_is_generating_respects_schema_it_received(
    env, initial, target
):
    tool = env.tool("search")
    await set_strategy(env, tool, initial)

    async def change(_):
        await set_strategy(env, tool, target)
        return call(
            (
                "search",
                {} if initial == "skills_like" else {"query": "from-full"},
                "first",
            )
        )

    responses = [change]
    if initial == "skills_like":
        responses.append(call(("search", {"query": "from-deferred"}, "resolved")))
    responses.append(final())
    run = await env.run([tool], responses, conversation_id="a")
    await drain(run)
    assert env.executed == [
        (
            "search",
            {"query": "from-deferred" if initial == "skills_like" else "from-full"},
        )
    ]
    assert len(run.provider.payloads) == (3 if initial == "skills_like" else 2)


async def test_reset_hook_only_invalidates_after_success_and_stale_finish_cannot_restore(
    env,
):
    tool = env.tool("search")
    run = await env.run([tool], [final()], conversation_id="a")
    plugin = Main.__new__(Main)
    plugin.service, plugin.ready, plugin.logger = (
        env.service,
        True,
        logging.getLogger("test"),
    )
    env.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="a")
    )
    await plugin.after_message_sent(run.event)
    assert not run.bridge.session.invalidated
    run.event.set_extra("_clean_group_context_session", True)
    await plugin.after_message_sent(run.event)
    assert run.bridge.session.invalidated and run.bridge.provider_tools() is None
    results = [
        item
        async for item in run.runner.tool_executor.execute(
            tool, run.wrapper, query="blocked"
        )
    ]
    assert results[0].isError and not env.executed
    await run.bridge.finish()
    assert await env.service.store.load_session(run.bridge.session.key) is None


async def test_schema_order_and_metadata_stay_stable_until_boundary(env):
    a, b = env.tool("a"), env.tool("b")
    original = await env.run([b, a], [final()], conversation_id="a")
    before = original.bridge.provider_tools().openai_schema()
    await drain(original)
    a.description = "宿主中更新的描述"
    same = await env.run([a, b], [final()], conversation_id="a", history=HISTORY)
    assert same.bridge.provider_tools().openai_schema() == before
    await drain(same)
    fresh = await env.run([a, b], [final()], conversation_id="new")
    assert fresh.bridge.provider_tools().get_tool("a").description == a.description
    await drain(fresh)


async def test_alias_edits_do_not_reset_expansion_or_apply_automatic_changes(env):
    tool = env.tool("search")
    await set_strategy(env, tool, "skills_like")
    run = await env.run([tool], [final()], conversation_id="a")
    await env.service.expand(run.bridge, [identify(tool).id])
    await env.service.change(
        "admin", env.service.revision, "tool", identify(tool).id, {"aliases": ["检索"]}
    )
    assert run.bridge.provider_tools().get_tool("search").parameters["required"] == [
        "query"
    ]
    await drain(run)


async def test_profiles_are_separate_and_new_tools_wait_for_boundary(env):
    first = env.tool("first")
    run = await env.run([first], [final()], conversation_id="a")
    await drain(run)
    new = env.tool("new")
    same = await env.run([first, new], [final()], conversation_id="a", history=HISTORY)
    assert same.bridge.provider_tools().get_tool("new") is None
    profile_b = await env.run(
        [first, new], [final()], conversation_id="a", history=HISTORY, profile="b"
    )
    assert profile_b.bridge.provider_tools().get_tool("new") is not None
    await drain(profile_b)
    await set_strategy(env, new, "full")
    assert same.bridge.provider_tools().get_tool("new") is not None
    await drain(same)


async def test_persisted_session_contains_only_metadata_and_hashed_identity(env):
    tool = env.tool("search")
    run = await env.run(
        [tool],
        [call(("search", {"query": "PRIVATE_ARGUMENT"}, "r")), final()],
        conversation_id="PRIVATE_CONVERSATION",
    )
    await drain(run)
    rows = await env.service.store.run(
        lambda db: [dict(row) for row in db.execute("SELECT * FROM sessions")]
    )
    serialized = json.dumps(rows)
    assert "PRIVATE_ARGUMENT" not in serialized
    assert "PRIVATE_CONVERSATION" not in serialized
    assert run.event.unified_msg_origin not in serialized
    assert len(rows[0]["key"]) == 64


async def test_automatic_demotion_and_dormancy_wait_for_reset(env):
    tool = env.tool("search")
    await env.service.change(
        "admin",
        0,
        "settings",
        "global",
        {
            "smart": True,
            "promote_requests": 2,
            "demote_requests": 1,
            "demote_hours": 1,
            "observe_days": 1,
            "min_opportunities": 2,
        },
    )
    for index in range(2):
        run = await env.run(
            [tool],
            [call(("search", {}, "s")), call(("search", {"query": "x"}, "r")), final()],
        )
        await drain(run)
    original = await env.run([tool], [final()], conversation_id="a")
    assert original.bridge.decisions[identify(tool).id].mode == "full"
    await drain(original)

    def simulate_aged_usage(db):
        db.execute("DELETE FROM calls")
        db.execute(
            "UPDATE states SET first_seen=?,low_since=?",
            (time.time() - 2 * 86400, time.time() - 7200),
        )

    await env.service.store.run(simulate_aged_usage)
    same = await env.run([tool], [final()], conversation_id="a", history=HISTORY)
    assert same.bridge.decisions[identify(tool).id].mode == "full"
    assert same.bridge.provider_tools().get_tool("search") is not None
    await drain(same)
    reset = await env.run([tool], [final()], conversation_id="a")
    assert reset.bridge.decisions[identify(tool).id].mode == "dormant"
    assert reset.bridge.provider_tools().get_tool("search") is None
    await reset.bridge.discover("search")
    await drain(reset)
    resumed = await env.run([tool], [final()], conversation_id="a", history=HISTORY)
    assert resumed.bridge.provider_tools().get_tool("search").parameters[
        "required"
    ] == ["query"]
    await drain(resumed)


async def test_admin_change_to_absent_tool_is_applied_when_host_allows_it_again(env):
    first = env.tool("first")
    original = await env.run([first], [final()], conversation_id="a")
    new = env.tool("new")
    await set_strategy(env, new, "skills_like")
    assert original.bridge.provider_tools().get_tool("new") is None
    await drain(original)
    same = await env.run([first, new], [final()], conversation_id="a", history=HISTORY)
    assert same.bridge.provider_tools().get_tool("new").parameters["properties"] == {}
    await drain(same)


async def test_owner_and_global_admin_switches_apply_without_conversation_reset(env):
    first, second = env.tool("first"), env.tool("second")
    run = await env.run([first, second], [final()], conversation_id="a")
    owner = identify(first).owner
    await env.service.change(
        "admin", env.service.revision, "owner", owner, {"strategy": "skills_like"}
    )
    assert all(
        tool.parameters["properties"] == {}
        for tool in run.bridge.provider_tools().tools
    )
    await env.service.change(
        "admin", env.service.revision, "settings", "global", {"smart": True}
    )
    assert run.bridge.provider_tools().get_tool("tool_scheduler_discover") is not None
    await env.service.change(
        "admin", env.service.revision, "settings", "global", {"block_all": True}
    )
    assert run.bridge.provider_tools() is None
    await env.service.change(
        "admin", env.service.revision, "settings", "global", {"block_all": False}
    )
    assert run.bridge.provider_tools().get_tool("first") is not None
    await env.service.change(
        "admin", env.service.revision, "owner", owner, {"enabled": False}
    )
    assert run.bridge.provider_tools().get_tool("first") is None
    assert run.bridge.provider_tools().get_tool("second") is None
    await drain(run)


async def test_session_snapshot_never_restores_tools_removed_by_host_permissions(env):
    allowed, removed = env.tool("allowed"), env.tool("removed")
    run = await env.run([allowed, removed], [final()], conversation_id="a")
    await drain(run)
    restricted = await env.run(
        [allowed], [final()], conversation_id="a", history=HISTORY
    )
    assert restricted.bridge.provider_tools().get_tool("removed") is None
    rejected = [
        item
        async for item in restricted.runner.tool_executor.execute(
            removed, restricted.wrapper, query="blocked"
        )
    ]
    assert rejected[0].isError and not env.executed
    await drain(restricted)
