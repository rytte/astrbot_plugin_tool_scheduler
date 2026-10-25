"""Resolve stable tool identities without executing plugin code."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.star.star import star_map


def unwrap(tool):
    """Follow AstrBot's permission proxies while retaining the execution wrapper elsewhere.

    Args:
        tool: A registered or request-local tool object.

    Returns:
        The underlying tool used only for identification and live status checks.
    """
    seen = set()
    while hasattr(tool, "_wrapped") and id(tool) not in seen:
        seen.add(id(tool))
        tool = tool._wrapped
    return tool


@dataclass(frozen=True)
class ToolInfo:
    id: str
    name: str
    owner: str
    owner_label: str
    source: str
    description: str
    parameters: dict
    upstream_active: bool
    schema_estimate: int
    available: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def identify(tool) -> ToolInfo:
    """Identify plugins, MCP tools, builtins and request-local tools.

    Args:
        tool: The original request tool, possibly permission-wrapped.

    Returns:
        Stable identity and display metadata.
    """
    original = unwrap(tool)
    module = getattr(original, "handler_module_path", None)
    if not module:
        callback = getattr(original, "handler", None)
        while hasattr(callback, "func"):
            callback = callback.func
        module = getattr(callback, "__module__", None) or type(original).__module__
    metadata = next(
        (
            value
            for key, value in sorted(star_map.items(), key=lambda item: -len(item[0]))
            if module == key or module.startswith(key + ".")
        ),
        None,
    )
    if isinstance(original, MCPTool):
        source = "mcp"
        label = original.mcp_server_name
        owner = "mcp:" + label
    elif metadata is not None and not metadata.reserved:
        source = "plugin"
        owner = "plugin:" + (metadata.name or metadata.root_dir_name or module)
        label = (
            metadata.display_name or metadata.name or metadata.root_dir_name or module
        )
    elif module.startswith("astrbot.") or (metadata is not None and metadata.reserved):
        source, owner, label = "builtin", "builtin:astrbot", "AstrBot 内置"
    else:
        source, owner, label = "request", "request:" + module, module
    parameters = getattr(tool, "parameters", None) or {
        "type": "object",
        "properties": {},
    }
    schema_text = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
    active = bool(getattr(original, "active", True)) and (
        metadata is None or metadata.activated
    )
    return ToolInfo(
        id=owner + "::" + tool.name,
        name=tool.name,
        owner=owner,
        owner_label=label,
        source=source,
        description=tool.description or "",
        parameters=parameters,
        upstream_active=active,
        schema_estimate=max(1, (len(schema_text) + 3) // 4),
    )


def relevance(query: str, entry: dict, aliases: list[str]) -> float:
    """Rank local names, descriptions and administrator aliases with Chinese n-grams.

    Args:
        query: Model-supplied discovery query.
        entry: Public tool metadata.
        aliases: Optional searchable administrator aliases.

    Returns:
        A deterministic nonnegative search score.
    """
    query = query.casefold().strip()
    if not query:
        return 1
    text = " ".join(
        [entry["name"], entry["description"], entry["owner_label"], *aliases]
    ).casefold()
    tokens = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", query)
    score = 0.0
    for token in tokens:
        score += 5 if token in text else 0
        if re.fullmatch(r"[\u3400-\u9fff]+", token) and len(token) > 1:
            grams = {token[index : index + 2] for index in range(len(token) - 1)}
            score += sum(gram in text for gram in grams) / len(grams)
    return score
