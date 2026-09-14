"""统一响应格式工具。

按照项目接口规范，所有接口（无论成功或失败）均返回统一结构::

    {
        "code": 0,
        "message": "success",
        "data": {}
    }

约定：

- ``code == 0`` 表示业务成功，非 0 表示业务失败，取值参见 :class:`ResponseCode`；
- ``message`` 为面向用户的提示信息；
- ``data`` 为业务数据，无数据时统一为空字典 ``{}``（不使用 ``null``）。

用法示例::

    from src.common.response import error_response, success_response

    return success_response(data={"status": "ok"})
    return success_response(data=user, message="登录成功")
    return error_response(message="用户名或密码错误", code=ResponseCode.UNAUTHORIZED)
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

__all__ = [
    "ResponseCode",
    "SUCCESS_MESSAGE",
    "error_response",
    "success_response",
]

#: 成功时的默认提示信息
SUCCESS_MESSAGE: str = "success"

#: 失败时的默认提示信息
ERROR_MESSAGE: str = "error"


class ResponseCode(IntEnum):
    """业务状态码。

    成功固定为 0；失败场景中，与 HTTP 语义一致的场景复用 HTTP 状态码数值
    （如 401 未认证、403 无权限、404 资源不存在、500 服务器错误），
    便于前端统一拦截处理。
    """

    SUCCESS = 0
    PARAM_ERROR = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    METHOD_NOT_ALLOWED = 405
    CONFLICT = 409
    TOO_MANY_REQUESTS = 429
    SERVER_ERROR = 500
    SERVICE_UNAVAILABLE = 503


def success_response(
    data: Any = None,
    message: str = SUCCESS_MESSAGE,
    code: ResponseCode | int = ResponseCode.SUCCESS,
) -> dict[str, Any]:
    """构造成功响应体。

    :param data: 业务数据，为 ``None`` 时返回空字典 ``{}``
    :param message: 提示信息，默认 ``"success"``
    :param code: 业务状态码，默认 0
    :return: 统一格式响应字典
    """
    return {
        "code": int(code),
        "message": message,
        "data": {} if data is None else data,
    }


def error_response(
    message: str = ERROR_MESSAGE,
    code: ResponseCode | int = ResponseCode.SERVER_ERROR,
    data: Any = None,
) -> dict[str, Any]:
    """构造失败响应体。

    :param message: 错误提示信息
    :param code: 业务状态码，默认 500（服务器内部错误）
    :param data: 附加错误数据（如字段级校验详情），为 ``None`` 时返回空字典 ``{}``
    :return: 统一格式响应字典
    """
    return {
        "code": int(code),
        "message": message,
        "data": {} if data is None else data,
    }
