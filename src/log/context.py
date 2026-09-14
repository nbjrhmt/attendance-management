"""操作日志的请求上下文：把"当前请求的来源 IP"传给业务层埋点。

背景：日志埋点写在 **service 层**（event / checkin / leave），而 service 拿不到
``Request`` 对象。这里用一个 :class:`contextvars.ContextVar` 承载当前请求的客户端 IP，
由纯 ASGI 中间件 :class:`ClientIpMiddleware` 在请求进入时写入，
:func:`src.log.service.record_operation` 在未显式传入 ``ip`` 时自动读取。

为什么用**纯 ASGI 中间件**而不是 ``BaseHTTPMiddleware``/``@app.middleware``：

- 纯 ASGI 中间件不包装响应对象，对 ``StreamingResponse``（如统计报表 CSV 导出）
  零影响，也不会引入额外的缓冲；
- ``ContextVar`` 在同一个任务（task）内传递，端点函数读到的就是本请求的 IP。

只使用 TCP 对端地址（``scope["client"]``），**不信任** ``X-Forwarded-For``：
该请求头可由客户端伪造，写入审计日志会造成误导；如后续部署在反向代理之后，
应在代理层改写并在配置中显式开启（届时可在此处扩展）。
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

__all__ = [
    "ClientIpMiddleware",
    "get_current_ip",
    "reset_current_ip",
    "set_current_ip",
]

#: 当前请求的客户端 IP（无请求上下文时为 None）
_current_ip: ContextVar[str | None] = ContextVar("current_request_ip", default=None)


def set_current_ip(ip: str | None) -> Token[str | None]:
    """设置当前请求的客户端 IP，返回可用于还原的 token。"""
    return _current_ip.set(ip)


def reset_current_ip(token: Token[str | None]) -> None:
    """还原 :func:`set_current_ip` 之前的取值（请求结束后调用，避免串请求）。"""
    _current_ip.reset(token)


def get_current_ip() -> str | None:
    """读取当前请求的客户端 IP（不在请求上下文中时返回 None）。"""
    return _current_ip.get()


class ClientIpMiddleware:
    """纯 ASGI 中间件：把 TCP 对端 IP 写入 :data:`_current_ip`。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        token = set_current_ip(client[0] if client else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_ip(token)
