"""Attach request-local mixed schemas to AstrBot's existing Agent runner."""

from __future__ import annotations

import copy
import json
import logging
import time
import uuid

import jsonschema
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse
from mcp.types import CallToolResult, TextContent

from .catalog import identify, relevance

DISCOVERY_NAME = "tool_scheduler_discover"
logger = logging.getLogger(__name__)
RUNTIME_PROMPT = (
    "Tool scheduling is active. Some tool descriptions mark their parameter schemas as deferred. "
    "Call these tools with {} to select them; their real arguments will be requested in a second stage. "
    "Tools with complete schemas can be called normally. Never guess arguments for a deferred tool. "
    "If the available tools do not cover the user's requested capability, call tool_scheduler_discover "
    "to find and load an allowed tool; try alternate keywords or an empty query to browse if needed. "
    "Loading a tool does not execute its action. Once loaded, its complete schema remains available "
    "for the rest of this conversation, until reset or an administrator changes its policy. "
    "Always explain meaningful results to the user."
)


def failure(message: str) -> CallToolResult:
    return CallToolResult(
        isError=True, content=[TextContent(type="text", text=message)]
    )


class DiscoveryTool(FunctionTool):
    def __init__(self, bridge) -> None:
        super().__init__(
            name=DISCOVERY_NAME,
            description="查找并加载当前任务可用的工具。现有工具不足时按能力、名称或别名搜索；空 query 可浏览，支持 offset 翻页。仅加载定义，不执行操作。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "能力或工具关键词；空字符串浏览全部",
                        "maxLength": 200,
                    },
                    "owner": {
                        "type": "string",
                        "description": "可选，按来源名称或 ID 筛选",
                    },
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        self.bridge = bridge

    async def call(self, context, **kwargs):
        return await self.bridge.discover(**kwargs)


class MeteredExecutor:
    def __init__(self, bridge, delegate) -> None:
        self.bridge = bridge
        self.delegate = delegate

    async def execute(self, tool, run_context, **arguments):
        """Preserve the native executor while checking live switches and metering once.

        Args:
            tool: The original tool, including its original permission wrapper.
            run_context: AstrBot's existing Agent run context.
            **arguments: Arguments produced using the real parameter schema.

        Yields:
            Original executor responses, or a structured rejection.
        """
        bridge = self.bridge
        if tool is bridge.discovery:
            if not bridge.service.settings["block_all"] and not bridge.service.closed:
                async for result in self.delegate.execute(
                    tool, run_context, **arguments
                ):
                    yield result
            else:
                yield failure("管理员已禁止所有受管模型工具，工具发现不可用。")
            return
        info = identify(tool).as_dict()
        start = time.time()
        outcome = "ok"
        try:
            if (
                not bridge.service.allowed(info, tool)
                or tool.name not in bridge.tools
                or bridge.session is None
                or bridge.session.invalidated
                or info["id"] not in bridge.session.entries
            ):
                outcome = "blocked"
                yield failure("此工具已被关闭或当前任务无权使用，未执行操作。")
                return
            permission = getattr(tool, "_mgr", None)
            if permission is not None:
                error = await permission._check_tool_permission(tool.name, run_context)
                if error:
                    outcome = "denied"
                    yield failure(error)
                    return
            try:
                jsonschema.validate(arguments, tool.parameters or {"type": "object"})
            except jsonschema.ValidationError:
                outcome = "invalid"
                yield failure("工具参数不符合提供的 Schema，请修正参数后重试。")
                return
            if not bridge.service.allowed(info, tool):
                outcome = "blocked"
                yield failure("此工具在权限检查期间已被关闭，未执行操作。")
                return
            async for result in self.delegate.execute(tool, run_context, **arguments):
                if isinstance(result, CallToolResult) and result.isError:
                    outcome = "error"
                yield result
        except BaseException:
            outcome = "error"
            raise
        finally:
            if not bridge.service.closed:
                try:
                    await bridge.service.store.record_call(
                        info["id"],
                        bridge.request_id,
                        bridge.profile_id,
                        start,
                        time.time() - start,
                        outcome,
                    )
                except Exception:
                    # A telemetry failure must never turn a completed side effect into a retryable tool error.
                    logger.exception("Could not persist a tool usage record.")


class RunBridge:
    """Keep all scheduling changes confined to one default Agent runner instance."""

    def __init__(self, service, runner) -> None:
        self.service = service
        self.runner = runner
        self.request_id = uuid.uuid4().hex
        self.profile_id, self.profile_name = service.profile(
            runner.run_context.context.event
        )
        self.native_mode = runner.tool_schema_mode
        self.originals = {}
        self.original_request_tools = runner.req.func_tool
        raw = (
            runner._skill_like_raw_tool_set
            if runner.tool_schema_mode == "skills_like"
            else runner.req.func_tool
        )
        self.tools = {
            tool.name: tool
            for tool in (raw.tools if raw else [])
            if getattr(tool, "active", True)
        }
        self.decisions = {}
        self.session = None
        self.loaded = set()
        self.presented_deferred = None
        self.discovery = DiscoveryTool(self)
        self.schema_savings = 0
        self.finished = False
        self.prompt = None
        self.prompt_part = None
        self.prompt_original = None
        self.created_prompt = False
        self.temporary_messages = []

    async def metric(self, name: str, amount: float = 1) -> None:
        if not self.service.closed:
            try:
                await self.service.store.metric(self.request_id, name, amount)
            except Exception:
                logger.exception("Could not persist a scheduling metric.")

    async def install(self) -> None:
        """Attach instance callbacks after the host has assembled its request context."""
        if DISCOVERY_NAME in self.tools:
            raise RuntimeError("A tool conflicts with the scheduler discovery name")
        permitted = {}
        for name, tool in self.tools.items():
            manager = getattr(tool, "_mgr", None)
            if manager is None or not await manager._check_tool_permission(
                name, self.runner.run_context
            ):
                permitted[name] = tool
        self.tools = permitted
        self.decisions = await self.service.begin(self, list(self.tools.values()))
        names = (
            "tool_schema_mode",
            "_skill_like_raw_tool_set",
            "_tool_schema_param_set",
            "_func_tool_for_provider",
            "_resolve_tool_exec",
            "tool_executor",
        )
        self.originals = {
            name: (name in self.runner.__dict__, getattr(self.runner, name))
            for name in names
        }
        execution_tools = ToolSet(list(self.tools.values()))
        execution_tools.add_tool(self.discovery)
        self.runner.req.func_tool = execution_tools
        self.runner._skill_like_raw_tool_set = execution_tools
        self.runner._tool_schema_param_set = None
        self.runner.tool_schema_mode = "skills_like"
        self.runner._func_tool_for_provider = self.provider_tools
        self.runner._resolve_tool_exec = self.resolve
        self.runner.tool_executor = MeteredExecutor(self, self.runner.tool_executor)
        self.sync_prompt()
        self.service.bridges.add(self)

    def sync_prompt(self) -> None:
        if self.finished:
            return
        if self.prompt is None and (
            self.service.settings["smart"]
            or any(
                decision.mode == "skills_like" for decision in self.decisions.values()
            )
        ):
            self.attach_prompt()

    def attach_prompt(self) -> None:
        """Add temporary guidance only when discovery or deferred tools are offered."""
        prompt_text = RUNTIME_PROMPT
        prompt_text = prompt_text.replace(
            "If the available tools do not cover the user's requested capability, call tool_scheduler_discover ",
            "If tool_scheduler_discover is available and the current tools do not cover the user's requested capability, call it ",
        )
        self.prompt_part = TextPart(text=prompt_text).mark_as_temp()
        self.prompt = next(
            (
                message
                for message in self.runner.run_context.messages
                if message.role == "system"
            ),
            None,
        )
        if self.prompt is None:
            self.prompt = Message(role="system", content=[self.prompt_part])
            self.created_prompt = True
            self.runner.run_context.messages.insert(0, self.prompt)
        else:
            self.prompt_original = self.prompt.content
            original_parts = (
                [TextPart(text=self.prompt.content)]
                if isinstance(self.prompt.content, str)
                else list(self.prompt.content or [])
            )
            self.prompt.content = [*original_parts, self.prompt_part]

    def provider_tools(self) -> ToolSet | None:
        """Reuse the conversation's ordered definitions and live administrator policies."""
        if (
            not self.runner.req.func_tool
            or self.session is None
            or self.session.invalidated
        ):
            return None  # Respect the host's max-step guard, which removes all tools.
        modalities = self.runner.provider.provider_config.get("modalities")
        if modalities and "tool_use" not in modalities:
            return None
        result = ToolSet()
        self.presented_deferred = set()
        for tool_id, info in self.session.ordered_entries():
            name = info["name"]
            tool = self.tools.get(name)
            if tool is None or identify(tool).id != tool_id:
                continue
            mode = self.decisions[tool_id].mode
            if not self.service.allowed(info, tool) or mode == "off":
                continue
            if tool_id in self.loaded or mode == "full":
                result.add_tool(self.schema(tool_id))
            elif mode == "skills_like":
                result.add_tool(
                    FunctionTool(
                        name=name,
                        description=info["description"]
                        + "\nParameter schema deferred: call with {} to select this tool.",
                        parameters={"type": "object", "properties": {}},
                    )
                )
                self.presented_deferred.add(name)
                self.schema_savings += max(0, info["schema_estimate"] - 10)
            else:
                self.schema_savings += info["schema_estimate"]
        if (
            self.service.settings["smart"]
            and not self.service.settings["block_all"]
            and not self.service.closed
        ):
            result.add_tool(self.discovery)
        return result if result.tools else None

    def schema(self, tool_id: str) -> FunctionTool:
        info = self.session.entries[tool_id]
        return FunctionTool(
            name=info["name"],
            description=info["description"],
            parameters=copy.deepcopy(info["parameters"]),
        )

    async def discover(self, query: str, owner: str = "", offset: int = 0) -> str:
        """Load matching tools into the conversation snapshot without executing them.

        Args:
            query: Requested capability, name or alias; empty browses all allowed tools.
            owner: Optional source filter.
            offset: Search result offset.

        Returns:
            Short discovery metadata; schemas stay out of tool-result messages.
        """
        if (
            not self.service.settings["smart"]
            or self.service.settings["block_all"]
            or self.service.closed
            or self.session is None
            or self.session.invalidated
        ):
            return json.dumps(
                {"loaded": [], "message": "工具发现当前不可用。"}, ensure_ascii=False
            )
        if (
            not isinstance(query, str)
            or len(query) > 200
            or not isinstance(owner, str)
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            return json.dumps(
                {"loaded": [], "message": "工具发现参数无效。"}, ensure_ascii=False
            )
        candidates = []
        for name, tool in self.tools.items():
            tool_id = identify(tool).id
            if tool_id not in self.session.entries:
                continue
            info = self.session.entries[tool_id]
            if not self.service.allowed(info, tool):
                continue
            manager = getattr(tool, "_mgr", None)
            if manager is not None and await manager._check_tool_permission(
                name, self.runner.run_context
            ):
                continue
            if (
                owner
                and owner.casefold()
                not in (info["owner"] + " " + info["owner_label"]).casefold()
            ):
                continue
            score = relevance(
                query, info, self.service.rule("tool", info["id"]).get("aliases", [])
            )
            if score > 0:
                candidates.append((score, name, info))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        selected = candidates[
            offset : offset + self.service.settings["discovery_limit"]
        ]
        newly_loaded = {info["id"] for _, _, info in selected} - self.loaded
        await self.service.expand(self, newly_loaded)
        await self.metric("discovery_searches")
        if selected:
            await self.metric("discovery_hits")
        if newly_loaded:
            await self.metric("schemas_loaded", len(newly_loaded))
        return json.dumps(
            {
                "loaded": [
                    {
                        "name": name,
                        "owner": info["owner_label"],
                        "description": info["description"][:180],
                    }
                    for _, name, info in selected
                ],
                "total": len(candidates),
                "next_offset": offset + len(selected)
                if offset + len(selected) < len(candidates)
                else None,
                "message": "已加载这些工具的完整参数定义，下一轮可按定义调用；尚未执行操作。"
                if selected
                else "未找到匹配工具，请尝试其他关键词或空 query 浏览。",
            },
            ensure_ascii=False,
        )

    async def resolve(self, response: LLMResponse) -> tuple[LLMResponse, ToolSet]:
        """Resolve only deferred calls and preserve every already-complete call.

        Args:
            response: The first-stage response, possibly mixing full and deferred calls.

        Returns:
            Merged executable calls and the original execution tool set.
        """
        pending = []
        passthrough = []
        for name, arguments, call_id in zip(
            response.tools_call_name, response.tools_call_args, response.tools_call_ids
        ):
            tool = self.tools.get(name)
            if tool is not None:
                tool_id = identify(tool).id
                decision = self.decisions.get(tool_id)
                deferred = (
                    (name in self.presented_deferred)
                    if self.presented_deferred is not None
                    else (
                        decision is not None
                        and decision.mode == "skills_like"
                        and tool_id not in self.loaded
                    )
                )
                if deferred and self.service.allowed(identify(tool).as_dict(), tool):
                    pending.append(name)
                    continue
            passthrough.append((name, arguments, call_id))
        if not pending:
            return response, self.runner.req.func_tool
        names = list(dict.fromkeys(pending))
        ids = [identify(self.tools[name]).id for name in names]
        subset = ToolSet(
            [self.schema(tool_id) for tool_id in ids]
        ).get_param_only_tool_set()
        await self.service.expand(self, ids)
        await self.metric("schemas_loaded", len(names))
        reply = None
        for attempt in range(2):
            contexts = [
                message.model_dump() for message in self.runner.run_context.messages
            ]
            instruction = (
                "You selected these deferred tools: "
                + ", ".join(names)
                + ". Generate their real calls now using the provided parameter schemas. Do not call other tools or repeat already-complete calls. If a call is inappropriate, briefly explain why."
            )
            if attempt:
                instruction += " Do not return an empty response. Either call a selected tool or explain why it cannot be called."
            contexts.insert(0, {"role": "system", "content": instruction})
            await self.metric("extra_llm_calls")
            try:
                reply = await self.runner._await_or_stop(
                    self.runner.provider.text_chat(
                        contexts=self.runner._sanitize_contexts_for_provider(contexts),
                        func_tool=subset,
                        model=self.runner.req.model,
                        session_id=self.runner.req.session_id,
                        extra_user_content_parts=self.runner.req.extra_user_content_parts,
                        abort_signal=self.runner._abort_signal,
                        request_max_retries=self.runner.request_max_retries,
                    )
                )
            except Exception:
                await self.metric("parameter_errors")
                reply = LLMResponse(
                    role="assistant",
                    completion_text="工具参数生成失败，本次未执行这些按需工具。",
                )
            if reply is None:
                return response, self.runner.req.func_tool
            if reply.usage:
                self.runner.stats.token_usage += reply.usage
            self.runner._sanitize_malformed_tool_calls(reply)
            if reply.tools_call_name or (reply.completion_text or "").strip():
                break
        resolved = []
        resolved_extra = {}
        seen_ids = {call_id for _, _, call_id in passthrough}
        for name, arguments, call_id in zip(
            reply.tools_call_name, reply.tools_call_args, reply.tools_call_ids
        ):
            if name not in names:
                continue
            original_id = call_id
            if call_id in seen_ids:
                call_id = "sched_" + uuid.uuid4().hex
            seen_ids.add(call_id)
            resolved.append((name, arguments, call_id))
            if original_id in reply.tools_call_extra_content:
                resolved_extra[call_id] = reply.tools_call_extra_content[original_id]
        merged = copy.copy(response)
        calls = [*passthrough, *resolved]
        merged.tools_call_name = [name for name, _, _ in calls]
        merged.tools_call_args = [args for _, args, _ in calls]
        merged.tools_call_ids = [call_id for _, _, call_id in calls]
        response.tools_call_extra_content.update(resolved_extra)
        if not calls:
            merged = copy.copy(reply)
            merged.tools_call_name, merged.tools_call_args, merged.tools_call_ids = (
                [],
                [],
                [],
            )
            if not (merged.completion_text or "").strip():
                merged.completion_text = "未能生成有效工具参数，本次未执行操作。"
        elif not resolved:
            hint = Message(
                role="user",
                content=[
                    TextPart(
                        text="Deferred tool parameter generation did not produce valid calls. Only the already-complete calls were retained. Explain any remaining task limitations to the user."
                    ).mark_as_temp()
                ],
            )
            hint._no_save = True
            self.runner.run_context.messages.append(hint)
            self.temporary_messages.append(hint)
        return merged, self.runner.req.func_tool

    async def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            await self.service.complete(self)
            await self.metric("schema_tokens_avoided_estimate", self.schema_savings)
        finally:
            self.detach()

    def detach(self) -> None:
        """Restore instance state without changing the host class or other requests."""
        if not self.originals:
            return
        # A host max-step guard may already have cleared req.func_tool; preserve that guard.
        if self.runner.req.func_tool is not None:
            self.runner.req.func_tool = self.original_request_tools
        for name, (existed, value) in self.originals.items():
            if existed:
                setattr(self.runner, name, value)
            else:
                self.runner.__dict__.pop(name, None)
        self.originals.clear()
        if self.created_prompt:
            self.temporary_messages.append(self.prompt)
        elif self.prompt is not None and isinstance(self.prompt.content, list):
            remaining = [
                part for part in self.prompt.content if part is not self.prompt_part
            ]
            if (
                isinstance(self.prompt_original, str)
                and len(remaining) == 1
                and isinstance(remaining[0], TextPart)
                and remaining[0].text == self.prompt_original
            ):
                self.prompt.content = self.prompt_original
            else:
                self.prompt.content = remaining
        self.runner.run_context.messages[:] = [
            message
            for message in self.runner.run_context.messages
            if all(message is not temporary for temporary in self.temporary_messages)
        ]
        self.service.bridges.discard(self)
