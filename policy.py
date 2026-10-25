"""Validate administrator policies and evaluate frequency-based transitions."""

from __future__ import annotations

from dataclasses import dataclass

DAY = 86400
DEFAULTS = {
    "smart": False,
    "block_all": False,
    "allow_dormancy": True,
    "window_days": 7,
    "promote_requests": 10,
    "demote_requests": 5,
    "demote_hours": 48,
    "observe_days": 7,
    "min_opportunities": 50,
    "retention_days": 30,
    "discovery_limit": 5,
}
STRATEGIES = {"inherit", "auto", "full", "skills_like"}


def validate_settings(patch: dict, current: dict) -> dict:
    """Return validated settings without modifying the previous snapshot.

    Args:
        patch: Administrator-supplied settings to replace.
        current: Current complete settings.

    Returns:
        A complete settings snapshot.

    Raises:
        ValueError: An unknown key, invalid type or conflicting threshold exists.
    """
    if not isinstance(patch, dict) or set(patch) - DEFAULTS.keys():
        raise ValueError("智能策略包含未知配置项。")
    result = {**current, **patch}
    limits = {
        "window_days": (1, 30),
        "promote_requests": (2, 100000),
        "demote_requests": (1, 99999),
        "demote_hours": (1, 720),
        "observe_days": (1, 30),
        "min_opportunities": (1, 100000),
        "retention_days": (30, 365),
        "discovery_limit": (1, 10),
    }
    for key, value in result.items():
        if key in {"smart", "block_all", "allow_dormancy"}:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值。")
        elif type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError(f"{key} 超出允许范围。")
    if result["demote_requests"] >= result["promote_requests"]:
        raise ValueError("降级阈值必须小于升级阈值。")
    return result


def validate_rule(patch: dict) -> dict:
    """Validate a tool or owner policy override.

    Args:
        patch: Policy fields supplied by an administrator.

    Returns:
        A normalized policy override.

    Raises:
        ValueError: The override is malformed.
    """
    if not isinstance(patch, dict) or set(patch) - {"enabled", "strategy", "aliases"}:
        raise ValueError("工具策略包含未知字段。")
    enabled = patch.get("enabled")
    strategy = patch.get("strategy", "inherit")
    aliases = patch.get("aliases", [])
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("允许使用必须是布尔值或继承。")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ValueError("提供策略无效。")
    if (
        not isinstance(aliases, list)
        or len(aliases) > 20
        or any(
            not isinstance(item, str) or not 1 <= len(item.strip()) <= 80
            for item in aliases
        )
    ):
        raise ValueError("最多设置 20 个别名，每个别名长度为 1～80。")
    return {
        "enabled": enabled,
        "strategy": strategy,
        "aliases": list(dict.fromkeys(item.strip() for item in aliases)),
    }


@dataclass(frozen=True)
class Decision:
    mode: str
    reason: str
    low_since: float | None = None


def decide(
    settings: dict,
    tool_rule: dict,
    owner_rule: dict,
    stats: dict,
    previous: dict,
    now: float,
    native_mode: str = "full",
) -> Decision:
    """Choose a request's initial exposure with cold-start and hysteresis rules.

    Args:
        settings: Complete scheduler settings.
        tool_rule: Explicit policy for this tool.
        owner_rule: Policy inherited from the owning plugin or MCP server.
        stats: Windowed request and opportunity counts.
        previous: Persisted automatic mode and observation timestamps.
        now: Current Unix timestamp.
        native_mode: Host default when automatic scheduling is paused.

    Returns:
        Exposure mode, user-facing explanation and downgrade timer.
    """
    if settings["block_all"]:
        return Decision("off", "已禁止所有受管模型工具")
    if owner_rule.get("enabled") is False:
        return Decision("off", "所属插件或来源已在调度器中关闭")
    if tool_rule.get("enabled") is False:
        return Decision("off", "管理员已关闭此工具")
    strategy = tool_rule.get("strategy", "inherit")
    if strategy == "inherit":
        strategy = owner_rule.get("strategy", "inherit")
    if strategy in {"full", "skills_like"}:
        return Decision(strategy, "管理员固定提供策略")
    if not settings["smart"]:
        return Decision(native_mode, "智能调度未启用，沿用宿主默认提供方式")
    requests = stats.get("requests", 0)
    if requests >= settings["promote_requests"]:
        return Decision("full", f"统计窗口内被 {requests} 个独立请求使用，达到升级阈值")
    if previous.get("mode") == "full":
        if requests >= settings["demote_requests"]:
            return Decision("full", "使用量处于升级与降级门槛之间，保持 Full")
        low_since = previous.get("low_since")
        if low_since is None:
            low_since = now
        if now - low_since < settings["demote_hours"] * 3600:
            return Decision("full", "低频降级缓冲期中，暂时保持 Full", low_since)
    observed = now - previous.get("first_seen", now) >= settings["observe_days"] * DAY
    enough = stats.get("opportunities", 0) >= settings["min_opportunities"]
    if settings["allow_dormancy"] and requests == 0 and observed and enough:
        return Decision(
            "dormant", "观察期和请求机会充分，近期未使用；仍可通过工具发现找回"
        )
    if not observed or not enough:
        return Decision("skills_like", "观察数据不足，保留轻量定义供模型选择")
    return Decision(
        "skills_like", f"统计窗口内被 {requests} 个独立请求使用，按需展开参数"
    )
