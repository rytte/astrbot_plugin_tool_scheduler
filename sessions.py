"""Freeze automatic exposure and tool definitions for a conversation epoch."""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from .policy import Decision


def conversation_key(profile: str, origin: str, conversation_id: str) -> str:
    value = json.dumps([profile, origin, conversation_id], ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def request_session(bridge) -> tuple[str | None, bool]:
    conversation = bridge.runner.req.conversation
    cid = getattr(conversation, "cid", None)
    if not isinstance(cid, str) or not cid:
        return None, False
    history = getattr(conversation, "history", "[]")
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except (ValueError, TypeError):
            history = []
    event = bridge.runner.run_context.context.event
    return conversation_key(bridge.profile_id, event.unified_msg_origin, cid), bool(
        history
    )


@dataclass
class SessionSnapshot:
    key: str | None
    profile: str
    revision: int
    entries: dict = field(default_factory=dict)
    decisions: dict = field(default_factory=dict)
    loaded: set = field(default_factory=set)
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    has_history: bool = False
    updated: float = field(default_factory=time.time)
    invalidated: bool = False
    created_revision: int | None = None
    entry_revisions: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.created_revision is None:
            self.created_revision = self.revision

    def dump(self) -> dict:
        return {
            "revision": self.revision,
            "entries": self.entries,
            "decisions": {key: asdict(value) for key, value in self.decisions.items()},
            "loaded": sorted(self.loaded),
            "epoch": self.epoch,
            "has_history": self.has_history,
            "created_revision": self.created_revision,
            "entry_revisions": self.entry_revisions,
        }

    @classmethod
    def restore(cls, key: str, profile: str, data: dict) -> SessionSnapshot:
        return cls(
            key=key,
            profile=profile,
            revision=data["revision"],
            entries=data["entries"],
            decisions={
                key: Decision(**value) for key, value in data["decisions"].items()
            },
            loaded=set(data["loaded"]),
            epoch=data["epoch"],
            has_history=data["has_history"],
            created_revision=data.get("created_revision", data["revision"]),
            entry_revisions=data.get(
                "entry_revisions",
                {tool_id: data["revision"] for tool_id in data["entries"]},
            ),
        )

    def include(
        self, info: dict, decision: Decision, revision: int | None = None
    ) -> None:
        self.entries[info["id"]] = copy.deepcopy(info)
        self.decisions[info["id"]] = decision
        self.entry_revisions[info["id"]] = (
            self.revision if revision is None else revision
        )

    def ordered_entries(self):
        return sorted(self.entries.items())
