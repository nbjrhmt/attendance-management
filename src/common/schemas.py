"""统一响应的 Pydantic 模型（用于 OpenAPI 文档与响应校验）。

接口层实际返回的是 :mod:`src.common.response` 中的字典，
这里的模型仅用于 ``response_model``，让 Swagger 文档能够展示准确的响应结构::

    @router.get("/users", response_model=ApiResponse[PageData[UserOut]])
    def list_users(...) -> dict[str, Any]:
        return success_response(data={"total": 0, "page": 1, "page_size": 10, "items": []})
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ApiResponse", "PageData"]

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应信封。

    :cvar code: 业务状态码，0 表示成功
    :cvar message: 提示信息
    :cvar data: 业务数据
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"code": 0, "message": "success", "data": {}}
        }
    )

    code: int = Field(default=0, description="业务状态码，0 表示成功")
    message: str = Field(default="success", description="提示信息")
    data: T | None = Field(default=None, description="业务数据")


class PageData(BaseModel, Generic[T]):
    """分页数据结构，作为 :class:`ApiResponse` 的 ``data`` 字段。"""

    total: int = Field(description="总记录数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")
    items: list[T] = Field(default_factory=list, description="当前页数据列表")
