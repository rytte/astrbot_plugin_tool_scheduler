"""Register default-Agent scheduling hooks and the administrator dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.pipeline.process_stage.follow_up import _ACTIVE_AGENT_RUNNERS
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .runtime import RunBridge
from .service import Scheduler
from .storage import Store
from .web import Dashboard

SESSION_KEY = "_tool_scheduler_run"


class Main(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        path = (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_tool_scheduler"
            / "scheduler.sqlite3"
        )
        self.service = Scheduler(context, Store(path))
        self.dashboard = Dashboard(context, self.service)
        self.ready = False

    async def initialize(self) -> None:
        await self.service.initialize()
        self.dashboard.register()
        self.ready = True

    @filter.on_agent_begin()
    async def on_agent_begin(self, event, run_context) -> None:
        """Attach to the already-registered default runner, without replacing the core class.

        Args:
            event: The request event used by AstrBot's active-runner registry.
            run_context: Context belonging to that exact Agent run.
        """
        if not self.ready:
            return
        runner = _ACTIVE_AGENT_RUNNERS.get(event.unified_msg_origin)
        required = (
            "_func_tool_for_provider",
            "_resolve_tool_exec",
            "_sanitize_contexts_for_provider",
            "_sanitize_malformed_tool_calls",
            "_await_or_stop",
            "_skill_like_raw_tool_set",
        )
        if (
            not isinstance(runner, ToolLoopAgentRunner)
            or runner.run_context is not run_context
            or any(not hasattr(runner, name) for name in required)
        ):
            self.service.unsupported_runs += 1
            self.logger.warning(
                "Tool scheduler did not attach: unsupported or unregistered Agent runner."
            )
            if (
                isinstance(runner, ToolLoopAgentRunner)
                and runner.run_context is run_context
            ):
                self.block_request(runner)
            return
        if event.get_extra(SESSION_KEY) is not None:
            return
        bridge = None
        try:
            bridge = RunBridge(self.service, runner)
            await bridge.install()
        except asyncio.CancelledError:
            if bridge is not None:
                bridge.detach()
            self.block_request(runner)
            raise
        except Exception:
            if bridge is not None:
                bridge.detach()
            self.service.unsupported_runs += 1
            self.logger.exception(
                "Tool scheduler could not attach; tools are disabled for this request."
            )
            self.block_request(runner)
            return
        event.set_extra(SESSION_KEY, bridge)

    @staticmethod
    def block_request(runner) -> None:
        """Fail closed when a default-runner request cannot be managed safely."""
        runner.req.func_tool = None
        runner._skill_like_raw_tool_set = None
        runner._tool_schema_param_set = None

    @filter.on_agent_done()
    async def on_agent_done(self, event, run_context, llm_response) -> None:
        bridge = event.get_extra(SESSION_KEY)
        if isinstance(bridge, RunBridge) and bridge.runner.run_context is run_context:
            try:
                await bridge.finish()
            finally:
                event.set_extra(SESSION_KEY, None)
        if not self.service.closed:
            await self.service.cleanup()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata) -> None:
        if self.ready:
            await self.service.refresh()

    @filter.after_message_sent()
    async def after_message_sent(self, event) -> None:
        # AstrBot sets this marker only after a successful /reset or /new, including renamed commands.
        if not self.ready or not event.get_extra("_clean_group_context_session", False):
            return
        manager = getattr(self.service.context, "conversation_manager", None)
        if manager is not None:
            cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                await self.service.reset_conversation(event, cid)

    async def terminate(self) -> None:
        self.ready = False
        self.dashboard.close()
        await self.service.close()
