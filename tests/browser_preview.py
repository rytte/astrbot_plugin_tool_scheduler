"""Isolated UI fixture. Never imports a running AstrBot or writes production data."""

from __future__ import annotations

import copy
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
NOW = time.time()
SETTINGS = {
    "smart": True,
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
OWNERS = [
    {
        "id": "plugin:search",
        "label": "网络搜索",
        "source": "plugin",
        "rule": {},
        "tools": 2,
        "calls": 35,
        "requests": 24,
    },
    {
        "id": "plugin:visual",
        "label": "聊天可视化",
        "source": "plugin",
        "rule": {},
        "tools": 2,
        "calls": 3,
        "requests": 2,
    },
    {
        "id": "mcp:files",
        "label": "本地文件 MCP",
        "source": "mcp",
        "rule": {},
        "tools": 1,
        "calls": 0,
        "requests": 0,
    },
    {
        "id": "builtin:core",
        "label": "AstrBot 内置",
        "source": "builtin",
        "rule": {},
        "tools": 1,
        "calls": 0,
        "requests": 0,
    },
]
TOOLS = []
for index, (name, description, mode, owner_index) in enumerate(
    [
        ("web_search", "搜索互联网内容，获取近期信息与参考来源。", "full", 0),
        ("open_page", "打开指定网页并提取主要文本。", "skills_like", 0),
        ("word_cloud", "根据聊天记录生成词云。", "skills_like", 1),
        ("weekly_report", "汇总本周聊天内容，生成可视化报告。", "dormant", 1),
        ("read_file", "读取当前允许目录下的本地文件。", "off", 2),
        ("handoff", "将任务交给其他 Agent 处理。", "unavailable", 3),
    ]
):
    owner = OWNERS[owner_index]
    TOOLS.append(
        {
            "id": owner["id"] + "::" + name,
            "name": name,
            "description": description,
            "owner": owner["id"],
            "owner_label": owner["label"],
            "source": owner["source"],
            "available": mode != "unavailable",
            "upstream_active": mode != "unavailable",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "查询内容"}},
                "required": ["query"],
            },
            "rule": {"enabled": False} if mode == "off" else {},
            "owner_rule": {},
            "stats": {
                "calls": 22 - index * 3 if index < 3 else 0,
                "requests": 12 - index * 3 if index < 3 else 0,
                "last_used": NOW - index * 3600 if index < 3 else None,
                "duration": 0.53,
            },
            "effective": {"mode": mode, "reason": "测试策略预览"},
        }
    )
STATE = {
    "revision": 0,
    "csrf": "local-fixture-only",
    "settings": SETTINGS,
    "profiles": [
        {"id": "default", "name": "默认配置"},
        {"id": "work", "name": "工作助手"},
    ],
    "tools": TOOLS,
    "owners": OWNERS,
    "coverage": {
        "last_connection": NOW,
        "active_runs": 0,
        "unsupported_runs": 0,
        "scope": "默认本地 ToolLoopAgentRunner；测试数据",
    },
    "overview": {
        "requests": 35,
        "metrics": {
            "extra_llm_calls": 8,
            "discovery_searches": 3,
            "discovery_hits": 2,
            "schemas_loaded": 11,
            "schema_tokens_avoided_estimate": 3640,
        },
        "trend": [
            {"day": NOW - (6 - day) * 86400, "calls": count}
            for day, count in enumerate([3, 8, 5, 12, 6, 16, 11])
        ],
    },
}
AUDIT = []
BRIDGE = """<script>
window.AstrBotPluginPage={ready:async()=>{},
apiGet:async(action,params={})=>{const r=await fetch('/fixture/'+action+'?'+new URLSearchParams(params));if(!r.ok)throw new Error('读取失败');return r.json()},
apiPost:async(action,body)=>{const r=await fetch('/fixture/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const result=await r.json();if(!r.ok)throw new Error(result.message);return result}
};</script>"""


class Handler(BaseHTTPRequestHandler):
    def reply(self, data, kind="application/json; charset=utf-8", status=200):
        content = (
            data.encode()
            if isinstance(data, str)
            else json.dumps(data, ensure_ascii=False).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/fixture/state":
            result = copy.deepcopy(STATE)
            profile = parse_qs(url.query, keep_blank_values=True).get(
                "profile", ["default"]
            )[0]
            for tool in result["tools"]:
                owner = next(
                    item for item in result["owners"] if item["id"] == tool["owner"]
                )
                if tool["effective"]["mode"] == "unavailable":
                    continue
                if (
                    SETTINGS["block_all"]
                    or tool["rule"].get("enabled") is False
                    or owner["rule"].get("enabled") is False
                ):
                    tool["effective"] = {"mode": "off", "reason": "管理员已关闭"}
                else:
                    fixed = tool["rule"].get("strategy", "inherit")
                    if fixed == "inherit":
                        fixed = owner["rule"].get("strategy", "inherit")
                    if fixed in {"full", "skills_like"}:
                        tool["effective"] = {
                            "mode": fixed,
                            "reason": "管理员固定提供策略",
                        }
                    elif not profile:
                        tool["effective"] = {"mode": "mixed", "reason": "请按配置查看"}
            return self.reply(result)
        if url.path == "/fixture/audit":
            return self.reply({"items": AUDIT})
        if url.path == "/narrow.html":
            return self.reply(
                '<!doctype html><meta charset="utf-8"><title>390px 窄屏测试</title><iframe src="/" style="width:390px;height:1000px;border:1px solid #aaa"></iframe>',
                "text/html; charset=utf-8",
            )
        filename = {
            "/": "index.html",
            "/index.html": "index.html",
            "/style.css": "style.css",
            "/app.js": "app.js",
        }.get(url.path)
        if not filename:
            return self.reply({"message": "not found"}, status=404)
        content = (ROOT / "pages" / "scheduler" / filename).read_text(encoding="utf-8")
        if filename.endswith("html"):
            content = content.replace(
                '<script src="app.js" defer></script>',
                BRIDGE + '<script src="app.js" defer></script>',
            )
        kind = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[
            filename.rsplit(".", 1)[1]
        ]
        self.reply(content, kind + "; charset=utf-8")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["revision"] != STATE["revision"]:
            return self.reply({"message": "版本冲突"}, status=409)
        if self.path == "/fixture/settings":
            SETTINGS.update(body["settings"])
        elif self.path == "/fixture/policy":
            collection = TOOLS if body["scope"] == "tool" else OWNERS
            next(item for item in collection if item["id"] == body["subject"])[
                "rule"
            ].update(body["policy"])
        else:
            return self.reply({"message": "not found"}, status=404)
        STATE["revision"] += 1
        AUDIT.insert(
            0,
            {
                "created": time.time(),
                "actor": "测试管理员",
                "message": "保存测试策略",
                "subject": body.get("subject", "global"),
            },
        )
        self.reply({"saved": True, "revision": STATE["revision"]})


if __name__ == "__main__":
    print("Isolated preview: http://127.0.0.1:8767", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8767), Handler).serve_forever()
