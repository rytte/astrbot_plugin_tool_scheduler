"""Expose dashboard-only APIs with JWT authentication and user-bound CSRF tokens."""

from __future__ import annotations

import functools
import hmac
import secrets
import time

import jwt
from astrbot.api.web import error_response, json_response, request
from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

from .service import ConflictError

PLUGIN = "astrbot_plugin_tool_scheduler"


class Dashboard:
    def __init__(self, context, service) -> None:
        self.context = context
        self.service = service
        self.nonces = {}
        self.handlers = []
        self.closed = False

    def register(self) -> None:
        for action, method in {
            "state": "GET",
            "audit": "GET",
            "settings": "POST",
            "policy": "POST",
        }.items():
            handler = functools.partial(self.dispatch, action)
            self.handlers.append(handler)
            self.context.register_web_api(
                f"/{PLUGIN}/{action}", handler, [method], "工具调度器（WebUI 管理员）"
            )

    def authorize(self) -> str:
        """Validate the full dashboard JWT rather than trusting iframe or API-key identity.

        Returns:
            The authenticated dashboard administrator name.

        Raises:
            PermissionError: Authentication is missing or not a dashboard session.
        """
        username = request.username
        if (
            self.closed
            or not isinstance(username, str)
            or not username
            or username.startswith(API_KEY_USERNAME_PREFIX)
        ):
            raise PermissionError("仅限已登录的 WebUI 管理员访问。")
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise PermissionError("请从已登录的 WebUI 插件页面访问。")
        secret = self.context.get_config().get("dashboard", {}).get("jwt_secret")
        if not isinstance(secret, str) or not secret:
            raise PermissionError("WebUI JWT 未正确配置。")
        try:
            payload = jwt.decode(
                authorization[7:].strip(),
                secret,
                algorithms=["HS256"],
                options={"require": ["exp", "username"]},
            )
        except jwt.InvalidTokenError as exc:
            raise PermissionError("WebUI 登录凭据无效或过期。") from exc
        if payload["username"] != username or payload.get("token_type") is not None:
            raise PermissionError("此凭据不能管理工具调度。")
        return username

    def nonce(self, owner: str) -> str:
        self.nonces = {
            name: value for name, value in self.nonces.items() if value[1] > time.time()
        }
        current = self.nonces.get(owner)
        if current is None:
            current = (secrets.token_urlsafe(32), time.time() + 3600)
            self.nonces[owner] = current
        return current[0]

    async def dispatch(self, action: str):
        try:
            owner = self.authorize()
            if action == "state":
                profile = request.query.get("profile") or None
                raw_days = request.query.get("days", "7")
                if raw_days not in {"1", "7", "30"}:
                    raise ValueError("统计窗口仅支持 1、7、30 天。")
                state = await self.service.snapshot(profile, int(raw_days))
                state["csrf"] = self.nonce(owner)
                return json_response(state)
            if action == "audit":
                return json_response({"items": await self.service.store.audit()})
            if (
                request.headers.get("Content-Length")
                and int(request.headers["Content-Length"]) > 32768
            ):
                raise ValueError("提交内容过大。")
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("提交内容必须为 JSON 对象。")
            nonce = self.nonces.get(owner)
            token = body.get("csrf")
            if (
                nonce is None
                or nonce[1] <= time.time()
                or not isinstance(token, str)
                or not hmac.compare_digest(nonce[0], token)
            ):
                raise PermissionError("操作凭据已过期，请刷新页面后重试。")
            if action == "settings":
                if set(body) != {"csrf", "revision", "settings"}:
                    raise ValueError("配置请求字段无效。")
                await self.service.change(
                    owner, body["revision"], "settings", "global", body["settings"]
                )
            elif action == "policy":
                if set(body) != {
                    "csrf",
                    "revision",
                    "scope",
                    "subject",
                    "policy",
                } or body["scope"] not in {"tool", "owner"}:
                    raise ValueError("工具策略请求字段无效。")
                if not isinstance(body["subject"], str) or not isinstance(
                    body["policy"], dict
                ):
                    raise ValueError("工具标识或策略格式无效。")
                await self.service.change(
                    owner,
                    body["revision"],
                    body["scope"],
                    body["subject"],
                    body["policy"],
                )
            else:
                raise ValueError("操作不存在。")
            return json_response({"saved": True, "revision": self.service.revision})
        except PermissionError as exc:
            return error_response(str(exc), status_code=403)
        except ConflictError as exc:
            return error_response(str(exc), status_code=409)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc), status_code=400)

    def close(self) -> None:
        self.closed = True
        self.context.registered_web_apis[:] = [
            entry
            for entry in self.context.registered_web_apis
            if entry[1] not in self.handlers
        ]
        self.nonces.clear()
