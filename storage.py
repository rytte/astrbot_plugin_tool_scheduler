"""Persist policies and parameter-free usage records in a private SQLite database."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalog (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
 scope TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(scope, subject)
);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, profile TEXT NOT NULL, started REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
 tool_id TEXT NOT NULL, request_id TEXT NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(tool_id, request_id)
);
CREATE INDEX IF NOT EXISTS opportunities_time ON opportunities(created);
CREATE TABLE IF NOT EXISTS calls (
 id INTEGER PRIMARY KEY, tool_id TEXT NOT NULL, request_id TEXT NOT NULL,
 profile TEXT NOT NULL, started REAL NOT NULL, duration REAL NOT NULL,
 outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_window ON calls(profile, tool_id, started);
CREATE TABLE IF NOT EXISTS states (
 profile TEXT NOT NULL, tool_id TEXT NOT NULL, first_seen REAL NOT NULL,
 mode TEXT NOT NULL DEFAULT 'skills_like', low_since REAL,
 PRIMARY KEY(profile, tool_id)
);
CREATE TABLE IF NOT EXISTS metrics (
 request_id TEXT NOT NULL, name TEXT NOT NULL, amount REAL NOT NULL,
 PRIMARY KEY(request_id, name)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
 kind TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 key TEXT PRIMARY KEY, profile TEXT NOT NULL, data TEXT NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection = None

    async def run(self, operation):
        """Serialize database work outside the event loop.

        Args:
            operation: Callable receiving the open SQLite connection.

        Returns:
            The operation's result after committing the transaction.
        """
        async with self.lock:
            if self.connection is None:
                raise RuntimeError("Statistics database is not open")

            def execute():
                with self.connection:
                    return operation(self.connection)

            task = asyncio.create_task(asyncio.to_thread(execute))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled await must not release the lock while SQLite is still writing.
                await task
                raise

    async def initialize(self) -> None:
        def open_database():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(SCHEMA)
            return connection

        self.connection = await asyncio.to_thread(open_database)

    async def load(self) -> tuple[dict, dict, list]:
        def read(connection):
            values = {
                row["key"]: json.loads(row["value"])
                for row in connection.execute("SELECT * FROM kv")
            }
            rules = {
                (row["scope"], row["subject"]): json.loads(row["data"])
                for row in connection.execute("SELECT * FROM rules")
            }
            catalog = [
                json.loads(row["data"])
                for row in connection.execute("SELECT * FROM catalog")
            ]
            return values, rules, catalog

        return await self.run(read)

    async def save_catalog(self, entries: list[dict]) -> None:
        await self.run(
            lambda connection: connection.executemany(
                "INSERT INTO catalog VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,data=excluded.data,updated=excluded.updated",
                [
                    (
                        item["id"],
                        item["owner"],
                        json.dumps(item, ensure_ascii=False),
                        time.time(),
                    )
                    for item in entries
                ],
            )
        )

    async def begin_request(
        self, request_id: str, profile: str, tools: list[str], now: float
    ) -> None:
        def begin(connection):
            connection.execute(
                "INSERT OR IGNORE INTO requests VALUES(?,?,?)",
                (request_id, profile, now),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO opportunities VALUES(?,?,?)",
                [(tool_id, request_id, now) for tool_id in tools],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO states(profile,tool_id,first_seen) VALUES(?,?,?)",
                [(profile, tool_id, now) for tool_id in tools],
            )

        await self.run(begin)

    async def window(self, profile: str | None, since: float) -> dict:
        def query(connection):
            profile_filter = " AND profile=?" if profile is not None else ""
            params = (since, profile) if profile is not None else (since,)
            calls = connection.execute(
                "SELECT tool_id,SUM(outcome NOT IN ('denied','blocked','invalid')) AS calls,COUNT(*) AS attempts,COUNT(DISTINCT CASE WHEN outcome NOT IN ('denied','blocked','invalid') THEN request_id END) AS requests,"
                "SUM(outcome='error') AS errors,SUM(outcome='denied') AS denied,SUM(outcome='blocked') AS blocked,SUM(outcome='invalid') AS invalid,"
                "AVG(CASE WHEN outcome NOT IN ('denied','blocked','invalid') THEN duration END) AS duration,MAX(CASE WHEN outcome NOT IN ('denied','blocked','invalid') THEN started END) AS last_used FROM calls WHERE started>=?"
                + profile_filter
                + " GROUP BY tool_id",
                params,
            )
            result = {row["tool_id"]: dict(row) for row in calls}
            for row in connection.execute(
                "SELECT o.tool_id,COUNT(*) AS opportunities FROM opportunities o JOIN requests r ON o.request_id=r.id WHERE o.created>=?"
                + (" AND r.profile=?" if profile is not None else "")
                + " GROUP BY o.tool_id",
                params,
            ):
                result.setdefault(row["tool_id"], {}).update(dict(row))
            return result

        return await self.run(query)

    async def states(self, profile: str) -> dict:
        return await self.run(
            lambda connection: {
                row["tool_id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM states WHERE profile=?", (profile,)
                )
            }
        )

    async def save_decisions(
        self, profile: str, decisions: dict, previous: dict, now: float
    ) -> None:
        def save(connection):
            for tool_id, decision in decisions.items():
                if decision.mode not in {"full", "skills_like", "dormant"}:
                    continue
                connection.execute(
                    "UPDATE states SET mode=?,low_since=? WHERE profile=? AND tool_id=?",
                    (decision.mode, decision.low_since, profile, tool_id),
                )
                old = previous.get(tool_id, {}).get("mode")
                if old is not None and old != decision.mode:
                    connection.execute(
                        "INSERT INTO audit(created,actor,kind,subject,message) VALUES(?,?,?,?,?)",
                        (
                            now,
                            "自动策略",
                            "mode",
                            tool_id,
                            f"新会话策略 {old} → {decision.mode}：{decision.reason}；已有会话保留原策略，管理员修改除外。",
                        ),
                    )

        await self.run(save)

    async def record_call(
        self,
        tool_id: str,
        request_id: str,
        profile: str,
        started: float,
        duration: float,
        outcome: str,
    ) -> None:
        await self.run(
            lambda connection: connection.execute(
                "INSERT INTO calls(tool_id,request_id,profile,started,duration,outcome) VALUES(?,?,?,?,?,?)",
                (tool_id, request_id, profile, started, duration, outcome),
            )
        )

    async def metric(self, request_id: str, name: str, amount: float = 1) -> None:
        await self.run(
            lambda connection: connection.execute(
                "INSERT INTO metrics VALUES(?,?,?) ON CONFLICT(request_id,name) DO UPDATE SET amount=amount+excluded.amount",
                (request_id, name, amount),
            )
        )

    async def change(
        self,
        revision: int,
        actor: str,
        kind: str,
        subject: str,
        value: dict,
        policy_epochs: dict | None = None,
    ) -> None:
        def save(connection):
            if kind == "settings":
                connection.execute(
                    "INSERT OR REPLACE INTO kv VALUES('settings',?)",
                    (json.dumps(value, ensure_ascii=False),),
                )
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO rules VALUES(?,?,?)",
                    (kind, subject, json.dumps(value, ensure_ascii=False)),
                )
            connection.execute(
                "INSERT OR REPLACE INTO kv VALUES('revision',?)", (str(revision),)
            )
            if policy_epochs is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO kv VALUES('policy_epochs',?)",
                    (json.dumps(policy_epochs),),
                )
            connection.execute(
                "INSERT INTO audit(created,actor,kind,subject,message) VALUES(?,?,?,?,?)",
                (
                    time.time(),
                    actor,
                    kind,
                    subject,
                    json.dumps(value, ensure_ascii=False),
                ),
            )

        await self.run(save)

    async def load_session(self, key: str) -> dict | None:
        def read(connection):
            row = connection.execute(
                "SELECT data FROM sessions WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

        return await self.run(read)

    async def save_session(self, session) -> None:
        if session.key is None or session.invalidated:
            return
        data = json.dumps(session.dump(), ensure_ascii=False)
        await self.run(
            lambda connection: connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                (session.key, session.profile, data, time.time()),
            )
        )

    async def delete_session(self, key: str) -> None:
        await self.run(
            lambda connection: connection.execute(
                "DELETE FROM sessions WHERE key=?", (key,)
            )
        )

    async def session_summaries(self, profile: str | None) -> list:
        return await self.run(
            lambda connection: [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT data FROM sessions"
                    + (" WHERE profile=?" if profile is not None else ""),
                    (profile,) if profile is not None else (),
                )
            ]
        )

    async def overview(self, profile: str | None, since: float) -> dict:
        def read(connection):
            params = (since, profile) if profile is not None else (since,)
            condition = " WHERE started>=?" + (
                " AND profile=?" if profile is not None else ""
            )
            requests = connection.execute(
                "SELECT COUNT(*) FROM requests" + condition, params
            ).fetchone()[0]
            metrics = {
                row["name"]: row["total"]
                for row in connection.execute(
                    "SELECT name,SUM(amount) AS total FROM metrics m JOIN requests r ON r.id=m.request_id WHERE r.started>=?"
                    + (" AND r.profile=?" if profile is not None else "")
                    + " GROUP BY name",
                    params,
                )
            }
            trend = [
                dict(row)
                for row in connection.execute(
                    "SELECT CAST(started/86400 AS INTEGER)*86400 AS day,COUNT(*) AS calls,COUNT(DISTINCT request_id) AS requests FROM calls"
                    + condition
                    + " AND outcome NOT IN ('denied','blocked','invalid') GROUP BY day ORDER BY day",
                    params,
                )
            ]
            owners = {
                row["owner"]: dict(row)
                for row in connection.execute(
                    "SELECT t.owner,COUNT(DISTINCT c.request_id) AS requests,MAX(c.started) AS last_used FROM calls c JOIN catalog t ON t.id=c.tool_id WHERE c.started>=?"
                    + (" AND c.profile=?" if profile is not None else "")
                    + " AND c.outcome NOT IN ('denied','blocked','invalid') GROUP BY t.owner",
                    params,
                )
            }
            return {
                "requests": requests,
                "metrics": metrics,
                "trend": trend,
                "owners": owners,
            }

        return await self.run(read)

    async def audit(self, limit: int = 100) -> list:
        return await self.run(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
                )
            ]
        )

    async def cleanup(self, days: int) -> None:
        cutoff = time.time() - days * 86400

        def prune(connection):
            connection.execute("DELETE FROM calls WHERE started<?", (cutoff,))
            connection.execute("DELETE FROM opportunities WHERE created<?", (cutoff,))
            connection.execute(
                "DELETE FROM metrics WHERE request_id IN (SELECT id FROM requests WHERE started<?)",
                (cutoff,),
            )
            connection.execute("DELETE FROM requests WHERE started<?", (cutoff,))
            connection.execute("DELETE FROM audit WHERE created<?", (cutoff,))
            connection.execute("DELETE FROM sessions WHERE updated<?", (cutoff,))

        await self.run(prune)

    async def close(self) -> None:
        async with self.lock:
            if self.connection is not None:
                await asyncio.to_thread(self.connection.close)
                self.connection = None
