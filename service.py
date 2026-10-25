"""Coordinate tool inventories, policies and private usage statistics."""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import asdict

from astrbot.core.tools.registry import get_builtin_tool_config_statuses

from .catalog import identify
from .policy import DAY, DEFAULTS, decide, validate_rule, validate_settings
from .sessions import SessionSnapshot, conversation_key, request_session
from .storage import Store

logger = logging.getLogger(__name__)


class ConflictError(ValueError):
    pass


class Scheduler:
    def __init__(self, context, store: Store) -> None:
        self.context = context
        self.store = store
        self.settings = dict(DEFAULTS)
        self.rules = {}
        self.catalog = {}
        self.revision = 0
        self.mutations = asyncio.Lock()
        self.closed = False
        self.bridges = weakref.WeakSet()
        self.last_connection = None
        self.unsupported_runs = 0
        self.last_cleanup = 0.0
        self.sessions = {}
        self.policy_epochs = {}

    async def initialize(self) -> None:
        await self.store.initialize()
        values, self.rules, entries = await self.store.load()
        self.settings = validate_settings(values.get("settings", {}), DEFAULTS)
        self.revision = values.get("revision", 0)
        self.policy_epochs = values.get("policy_epochs", {})
        self.catalog = {item["id"]: {**item, "available": False} for item in entries}
        await self.refresh()
        await self.cleanup()

    async def cleanup(self) -> None:
        async with self.mutations:
            if time.time() - self.last_cleanup >= 3600:
                await self.store.cleanup(self.settings["retention_days"])
                cutoff = time.time() - self.settings["retention_days"] * DAY
                for key, session in list(self.sessions.items()):
                    if session.updated < cutoff and not any(
                        bridge.session is session for bridge in self.bridges
                    ):
                        self.sessions.pop(key)
                        session.invalidated = True
                self.last_cleanup = time.time()

    async def refresh(self, request_tools=()) -> None:
        """Refresh known registrations without changing any native activation state.

        Args:
            request_tools: Additional tools supplied by the current request.
        """
        manager = self.context.get_llm_tool_manager()
        tools = [*manager.func_list, *manager.iter_builtin_tools(), *request_tools]
        for bridge in list(self.bridges):
            tools.extend(bridge.tools.values())
        entries = {}
        for tool in tools:
            info = identify(tool).as_dict()
            entries[info["id"]] = info
        for tool_id, entry in self.catalog.items():
            if tool_id not in entries:
                entries[tool_id] = {
                    **entry,
                    "available": False,
                    "upstream_active": False,
                }
        changed = [
            item for key, item in entries.items() if item != self.catalog.get(key)
        ]
        self.catalog = entries
        if changed:
            await self.store.save_catalog(changed)

    def rule(self, scope: str, subject: str) -> dict:
        return self.rules.get((scope, subject), {})

    def allowed(self, info: dict, tool=None) -> bool:
        """Check current administrator switches immediately before execution.

        Args:
            info: Stable tool identity metadata.
            tool: Original execution tool for upstream live-state checks.

        Returns:
            Whether scheduler and original activation switches permit this call.
        """
        if self.closed or self.settings["block_all"]:
            return False
        if (
            self.rule("tool", info["id"]).get("enabled") is False
            or self.rule("owner", info["owner"]).get("enabled") is False
        ):
            return False
        return tool is None or identify(tool).upstream_active

    def profile(self, event) -> tuple[str, str]:
        manager = getattr(self.context, "astrbot_config_mgr", None)
        if manager is not None:
            info = manager.get_conf_info(event.unified_msg_origin)
            if isinstance(info, dict):
                return info["id"], info.get("name", info["id"])
            return info.id, getattr(info, "name", info.id)
        return "default", "默认配置"

    async def begin(self, bridge, tools: list) -> dict:
        """Record demand, but keep automatic exposure fixed until a conversation boundary.

        Args:
            bridge: The request-local runtime adapter.
            tools: Tools eligible after the host's request filtering.

        Returns:
            Decisions indexed by stable tool identity.
        """
        async with self.mutations:
            now = time.time()
            await self.refresh(tools)
            infos = {identify(tool).id: identify(tool).as_dict() for tool in tools}
            ids = [
                identify(tool).id
                for tool in tools
                if self.allowed(identify(tool).as_dict(), tool)
            ]
            await self.store.begin_request(
                bridge.request_id, bridge.profile_id, ids, now
            )
            key, has_history = request_session(bridge)
            session = self.sessions.get(key) if key else None
            if key and session is None:
                data = await self.store.load_session(key)
                if data is not None:
                    session = SessionSnapshot.restore(key, bridge.profile_id, data)
            if session is not None and session.has_history and not has_history:
                session.invalidated = True
                session = None
            evaluation_infos = {**(session.entries if session else {}), **infos}
            candidates = await self.evaluate(
                bridge.profile_id, evaluation_infos, bridge.native_mode
            )
            if session is None:
                session = SessionSnapshot(key, bridge.profile_id, self.revision)
                for tool_id, info in sorted(infos.items()):
                    session.include(info, candidates[tool_id])
            else:
                self.apply_admin_changes(session, evaluation_infos, candidates)
            session.has_history |= has_history
            session.updated = now
            bridge.session = session
            bridge.loaded = session.loaded
            if key:
                self.sessions[key] = session
                # Bound idle metadata in memory; complete snapshots remain in SQLite.
                for cached_key in list(self.sessions):
                    if len(self.sessions) <= 128:
                        break
                    cached = self.sessions[cached_key]
                    if cached_key != key and not any(
                        item.session is cached for item in self.bridges
                    ):
                        self.sessions.pop(cached_key)
            await self.store.save_session(session)
            self.last_connection = now
            return session.decisions

    async def evaluate(self, profile: str, infos: dict, native_mode: str) -> dict:
        now = time.time()
        stats = await self.store.window(
            profile, now - self.settings["window_days"] * DAY
        )
        previous = await self.store.states(profile)
        decisions, automatic = {}, {}
        for tool_id, info in infos.items():
            tool_rule, owner_rule = (
                self.rule("tool", tool_id),
                self.rule("owner", info["owner"]),
            )
            decision = decide(
                self.settings,
                tool_rule,
                owner_rule,
                stats.get(tool_id, {}),
                previous.get(tool_id, {}),
                now,
                native_mode,
            )
            decisions[tool_id] = decision
            strategy = tool_rule.get("strategy", "inherit")
            if strategy == "inherit":
                strategy = owner_rule.get("strategy", "inherit")
            if self.settings["smart"] and strategy in {"inherit", "auto"}:
                automatic[tool_id] = decision
        await self.store.save_decisions(profile, automatic, previous, now)
        return decisions

    def changed_after(self, info: dict, revision: int) -> bool:
        return any(
            self.policy_epochs.get(key, 0) > revision
            for key in (
                "settings:global",
                "tool:" + info["id"],
                "owner:" + info["owner"],
            )
        )

    def apply_admin_changes(self, session, infos: dict, candidates: dict) -> None:
        for tool_id, info in infos.items():
            if self.changed_after(
                info, session.entry_revisions.get(tool_id, session.created_revision)
            ):
                session.include(info, candidates[tool_id], self.revision)
                session.loaded.discard(tool_id)
        session.revision = self.revision

    async def expand(self, bridge, tool_ids) -> None:
        async with self.mutations:
            session = bridge.session
            if session is None or session.invalidated:
                return
            session.loaded.update(
                tool_id for tool_id in tool_ids if tool_id in session.entries
            )
            await self.persist_session(session)

    async def persist_session(self, session) -> None:
        try:
            await self.store.save_session(session)
        except Exception:
            logger.exception("Could not persist a conversation tool snapshot.")

    async def complete(self, bridge) -> None:
        async with self.mutations:
            if bridge.session is not None and not self.closed:
                bridge.session.has_history |= any(
                    message.role == "assistant"
                    for message in bridge.runner.run_context.messages
                )
                await self.persist_session(bridge.session)

    async def reset_conversation(self, event, cid: str) -> None:
        profile, _ = self.profile(event)
        key = conversation_key(profile, event.unified_msg_origin, cid)
        async with self.mutations:
            session = self.sessions.pop(key, None)
            if session is not None:
                session.invalidated = True
            for bridge in list(self.bridges):
                if bridge.session is not None and bridge.session.key == key:
                    bridge.session.invalidated = True
            await self.store.delete_session(key)

    async def change(
        self, actor: str, revision: int, kind: str, subject: str, patch: dict
    ) -> None:
        """Commit one validated administrator change with optimistic concurrency.

        Args:
            actor: Authenticated dashboard identity.
            revision: Revision observed by the client.
            kind: Settings, tool policy or owner policy.
            subject: Stable tool or owner identity.
            patch: Explicit updated fields.

        Raises:
            ConflictError: Another administrator already changed these settings.
            ValueError: The requested change is invalid.
        """
        async with self.mutations:
            if type(revision) is not int or revision != self.revision:
                raise ConflictError("配置已被其他页面修改，请刷新后重试。")
            if kind == "settings":
                value = validate_settings(patch, self.settings)
            elif kind in {"tool", "owner"}:
                known = (
                    subject in self.catalog
                    if kind == "tool"
                    else any(item["owner"] == subject for item in self.catalog.values())
                )
                if not known:
                    raise ValueError("指定工具或来源不存在，请刷新目录。")
                value = validate_rule({**self.rule(kind, subject), **patch})
            else:
                raise ValueError("不支持的配置操作。")
            revision = self.revision + 1
            exposure_change = (
                kind == "settings"
                and bool(set(patch) - {"retention_days", "discovery_limit"})
            ) or (
                kind in {"tool", "owner"} and bool(set(patch) & {"enabled", "strategy"})
            )
            epochs = dict(self.policy_epochs)
            if exposure_change:
                epochs[kind + ":" + subject] = revision
            await self.store.change(revision, actor, kind, subject, value, epochs)
            if kind == "settings":
                self.settings = value
            else:
                self.rules = {**self.rules, (kind, subject): value}
            self.revision = revision
            self.policy_epochs = epochs
            if exposure_change:
                # Update already-running requests now; idle conversations update on their next request.
                handled = set()
                for bridge in list(self.bridges):
                    session = bridge.session
                    if session is None or session.invalidated or id(session) in handled:
                        continue
                    infos = dict(session.entries)
                    infos.update(
                        {
                            identify(tool).id: identify(tool).as_dict()
                            for tool in bridge.tools.values()
                        }
                    )
                    candidates = await self.evaluate(
                        bridge.profile_id, infos, bridge.native_mode
                    )
                    self.apply_admin_changes(session, infos, candidates)
                    await self.persist_session(session)
                    handled.add(id(session))
                for bridge in list(self.bridges):
                    bridge.sync_prompt()

    async def snapshot(self, profile: str | None = None, days: int = 7) -> dict:
        """Build administrator-facing inventory and statistics for the dashboard.

        Args:
            profile: Optional host configuration ID; None aggregates profiles.
            days: Display window, independent of the automatic policy window.

        Returns:
            Safe metadata, policies, aggregate statistics and adapter coverage.
        """
        if days not in {1, 7, 30}:
            raise ValueError("统计窗口仅支持 1、7、30 天。")
        await self.refresh()
        now = time.time()
        overview = await self.store.overview(profile, now - days * DAY)
        stats = await self.store.window(profile, now - days * DAY)
        policy_stats = await self.store.window(
            profile or "default", now - self.settings["window_days"] * DAY
        )
        previous = await self.store.states(profile or "default")
        stored_sessions = await self.store.session_summaries(profile)
        manager = getattr(self.context, "astrbot_config_mgr", None)
        configs = getattr(manager, "confs", {"default": self.context.get_config()})
        config = configs.get(profile or "default", self.context.get_config())
        native = config.get("provider_settings", {}).get("tool_schema_mode", "full")
        tools = []
        owners = {}
        for entry in self.catalog.values():
            tool_rule = self.rule("tool", entry["id"])
            owner_rule = self.rule("owner", entry["owner"])
            strategy = tool_rule.get("strategy", "inherit")
            if strategy == "inherit":
                strategy = owner_rule.get("strategy", "inherit")
            decision = decide(
                self.settings,
                tool_rule,
                owner_rule,
                policy_stats.get(entry["id"], {}),
                previous.get(entry["id"], {}),
                now,
                native,
            )
            effective = asdict(decision)
            session_modes = {}
            pending_sessions = 0
            for saved in stored_sessions:
                frozen = saved["decisions"].get(entry["id"])
                if frozen is None:
                    continue
                manually_changed = self.changed_after(
                    entry,
                    saved.get("entry_revisions", {}).get(
                        entry["id"], saved["revision"]
                    ),
                )
                mode = effective["mode"] if manually_changed else frozen["mode"]
                shown_mode = (
                    "full"
                    if not manually_changed
                    and entry["id"] in saved["loaded"]
                    and mode != "off"
                    else mode
                )
                session_modes[shown_mode] = session_modes.get(shown_mode, 0) + 1
                if (
                    profile is not None
                    and not manually_changed
                    and mode != effective["mode"]
                ):
                    pending_sessions += 1
            native_statuses = (
                get_builtin_tool_config_statuses(
                    entry["name"], [{"conf_id": profile or "default", "config": config}]
                )
                if entry["source"] == "builtin" and profile is not None
                else []
            )
            if (
                not entry["available"]
                or not entry["upstream_active"]
                or (native_statuses and not native_statuses[0]["enabled"])
            ):
                effective = {
                    "mode": "unavailable",
                    "reason": "宿主未启用、来源离线或工具已卸载",
                }
            elif (
                profile is None
                and effective["mode"] != "off"
                and strategy not in {"full", "skills_like"}
            ):
                effective = {
                    "mode": "mixed",
                    "reason": "当前显示跨配置统计，请选择具体配置查看自动提供状态",
                }
            tool = {
                **entry,
                "rule": tool_rule,
                "owner_rule": owner_rule,
                "stats": stats.get(entry["id"], {}),
                "effective": effective,
                "session_modes": session_modes,
                "pending_sessions": pending_sessions,
            }
            tools.append(tool)
            owner = owners.setdefault(
                entry["owner"],
                {
                    "id": entry["owner"],
                    "label": entry["owner_label"],
                    "source": entry["source"],
                    "rule": owner_rule,
                    "tools": 0,
                    "calls": 0,
                },
            )
            owner["tools"] += 1
            owner["calls"] += stats.get(entry["id"], {}).get("calls", 0)
        for owner in owners.values():
            owner.update(
                overview["owners"].get(owner["id"], {"requests": 0, "last_used": None})
            )
        profiles = [{"id": "default", "name": "默认配置"}]
        if manager is not None:
            profiles = [
                {"id": info["id"], "name": info["name"]}
                if isinstance(info, dict)
                else {"id": info.id, "name": info.name}
                for info in manager.get_conf_list()
            ]
        return {
            "revision": self.revision,
            "settings": self.settings,
            "tools": sorted(
                tools, key=lambda item: (item["owner_label"], item["name"])
            ),
            "owners": sorted(owners.values(), key=lambda item: item["label"]),
            "profiles": profiles,
            "overview": overview,
            "coverage": {
                "last_connection": self.last_connection,
                "active_runs": len(self.bridges),
                "unsupported_runs": self.unsupported_runs,
                "scope": "默认本地 ToolLoopAgentRunner；不接管第三方 Agent 或插件自行直连模型",
            },
        }

    async def close(self) -> None:
        self.closed = True
        for bridge in list(self.bridges):
            bridge.detach()
        await self.store.close()
