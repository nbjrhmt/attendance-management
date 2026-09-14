"""测试公共常量与工具函数。

被 ``conftest.py`` 与各测试模块共同引用。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ADMIN_PASSWORD",
    "ADMIN_USERNAME",
    "FAMILY_PASSWORD",
    "FAMILY_USERNAME",
    "STAFF_PASSWORD",
    "STAFF_USERNAME",
    "assert_unified_response",
    "auth_headers",
]

#: 测试管理员账号
ADMIN_USERNAME = "admin_test"
ADMIN_PASSWORD = "admin123456"

#: 测试家庭用户账号
FAMILY_USERNAME = "family_test"
FAMILY_PASSWORD = "family123456"

#: 测试工作人员账号
STAFF_USERNAME = "staff_test"
STAFF_PASSWORD = "staff123456"


def auth_headers(token: str) -> dict[str, str]:
    """构造携带访问令牌的请求头。"""
    return {"Authorization": f"Bearer {token}"}


def assert_unified_response(body: Any, code: int = 0) -> None:
    """断言响应体符合统一格式 ``{"code", "message", "data"}``。

    :param body: 响应 JSON
    :param code: 期望的业务状态码，默认 0（成功）
    """
    assert isinstance(body, dict), f"响应体应为字典，实际为 {type(body)}"
    assert set(body.keys()) == {"code", "message", "data"}, f"响应字段不符合统一格式：{body.keys()}"
    assert body["code"] == code, f"期望 code={code}，实际 {body['code']}：{body['message']}"
    assert isinstance(body["message"], str) and body["message"], "message 不能为空"
