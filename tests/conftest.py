"""Use the adjacent real AstrBot source with isolated temporary runtime data."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, os.environ.get("ASTRBOT_SOURCE", str(ROOT.parent / "AstrBot")))
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="tool-scheduler-tests-")

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.db.po import Conversation
from astrbot.core.provider.entities import LLMResponse, ProviderRequest, TokenUsage
from astrbot.core.provider.provider import Provider

from astrbot_plugin_tool_scheduler.runtime import RunBridge
from astrbot_plugin_tool_scheduler.service import Scheduler
from astrbot_plugin_tool_scheduler.storage import Store


class Event:
    unified_msg_origin = "test:FriendMessage:session"
    role = "member"

    def __init__(self):
        self.extra = {}
        self.message_obj = SimpleNamespace(message=[], message_id="message")

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def is_admin(self):
        return self.role == "admin"

    def is_stopped(self):
        return False

    def get_sender_id(self):
        return "test-user"

    def get_result(self):
        return None

    def clear_result(self):
        pass


class ScriptedProvider(Provider):
    def __init__(self, responses):
        super().__init__({"id": "test-provider", "max_context_tokens": 0}, {})
        self.responses = list(responses)
        self.payloads = []

    def get_current_key(self):
        return "test"

    def set_key(self, key):
        pass

    async def get_models(self):
        return ["test"]

    async def text_chat(self, **kwargs):
        self.payloads.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = await response(kwargs)
        return response

    async def text_chat_stream(self, **kwargs):
        response = await self.text_chat(**kwargs)
        yield response


def call(*items, text="调用工具"):
    return LLMResponse(
        role="assistant",
        completion_text=text,
        tools_call_name=[item[0] for item in items],
        tools_call_args=[item[1] for item in items],
        tools_call_ids=[item[2] for item in items],
        usage=TokenUsage(input_other=10, output=5),
    )


def final(text="完成"):
    return LLMResponse(
        role="assistant",
        completion_text=text,
        usage=TokenUsage(input_other=10, output=5),
    )


@pytest.fixture
async def env(tmp_path):
    registrations = []
    config = {
        "provider_settings": {"tool_schema_mode": "full"},
        "dashboard": {"jwt_secret": "test-secret-with-at-least-32-characters"},
    }
    manager = SimpleNamespace(func_list=registrations, iter_builtin_tools=lambda: [])
    context = SimpleNamespace(
        get_llm_tool_manager=lambda: manager,
        get_config=lambda umo=None: config,
        registered_web_apis=[],
        astrbot_config_mgr=SimpleNamespace(
            get_conf_info=lambda umo: {"id": "default", "name": "默认配置"},
            get_conf_list=lambda: [{"id": "default", "name": "默认配置"}],
        ),
    )
    context.register_web_api = lambda *entry: context.registered_web_apis.append(entry)
    service = Scheduler(context, Store(tmp_path / "scheduler.sqlite3"))
    await service.initialize()
    executed = []

    def tool(name, field="query", description=None):
        async def handler(event, **arguments):
            executed.append((name, arguments))
            return "result:" + name

        result = FunctionTool(
            name=name,
            description=description or "测试 " + name,
            parameters={
                "type": "object",
                "properties": {
                    field: {"type": "string", "description": "SCHEMA_ONLY_SENTINEL"}
                },
                "required": [field],
                "additionalProperties": False,
            },
            handler=handler,
        )
        registrations.append(result)
        return result

    async def run(
        tools,
        responses,
        *,
        streaming=False,
        profile="default",
        install=True,
        native_mode="full",
        conversation_id=None,
        history=None,
    ):
        provider = ScriptedProvider(responses)
        event = Event()
        wrapper = ContextWrapper(context=SimpleNamespace(event=event, context=context))
        runner = ToolLoopAgentRunner()
        request = ProviderRequest(
            prompt="执行用户任务",
            system_prompt="原始角色设定",
            func_tool=ToolSet(tools),
            conversation=Conversation(
                "test", "test-user", conversation_id, history=json.dumps(history or [])
            )
            if conversation_id
            else None,
            contexts=history or [],
        )
        await runner.reset(
            provider=provider,
            request=request,
            run_context=wrapper,
            tool_executor=FunctionToolExecutor(),
            agent_hooks=BaseAgentRunHooks(),
            streaming=streaming,
            tool_schema_mode=native_mode,
        )
        bridge = RunBridge(service, runner)
        bridge.profile_id = profile
        if install:
            await bridge.install()
        return SimpleNamespace(
            provider=provider,
            event=event,
            runner=runner,
            bridge=bridge,
            wrapper=wrapper,
            request=request,
        )

    yield SimpleNamespace(
        service=service,
        context=context,
        tool=tool,
        run=run,
        executed=executed,
        manager=manager,
        path=tmp_path,
    )
    await service.close()
