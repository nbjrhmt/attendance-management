"""业务异常定义。

业务代码通过抛出 :class:`BusinessError` 中断流程，由 ``main.py`` 中注册的全局异常
处理器转换为统一响应格式::

    {"code": 401, "message": "用户名或密码错误", "data": {}}

设计说明：

- ``code`` 为业务状态码（取值见 :class:`~src.common.response.ResponseCode`），
  成功固定为 0，失败为非 0；
- ``http_status`` 默认与 ``code`` 保持一致（仅当 ``code`` 落在 400~599 之间时），
  这样前端的 HTTP 拦截器可以继续依赖状态码（如 401 跳转登录页），
  响应体同时保留统一格式；
- 若业务码不是 HTTP 状态码（如 1001），则 HTTP 状态码退化为 200，
  由前端根据 ``code`` 判断。

用法示例::

    from src.common.exceptions import BusinessError
    from src.common.response import ResponseCode

    raise BusinessError("用户名或密码错误", code=ResponseCode.UNAUTHORIZED)
    raise BusinessError("用户名已存在", code=ResponseCode.CONFLICT)
"""

from __future__ import annotations

from typing import Any

from src.common.response import ResponseCode

__all__ = ["BusinessError"]


class BusinessError(Exception):
    """业务异常。

    :param message: 面向用户的错误提示信息
    :param code: 业务状态码，默认 400（参数错误）
    :param data: 附加数据（如冲突的字段名），可选
    :param http_status: 自定义 HTTP 状态码，默认根据 ``code`` 推断
    """

    def __init__(
        self,
        message: str,
        code: ResponseCode | int = ResponseCode.PARAM_ERROR,
        data: Any = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = int(code)
        self.data = data
        if http_status is not None:
            self.http_status = http_status
        elif 400 <= self.code <= 599:
            self.http_status = self.code
        else:
            self.http_status = 200

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        return f"<BusinessError code={self.code} message={self.message!r}>"
