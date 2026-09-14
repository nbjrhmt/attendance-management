"""操作日志模块的请求/响应数据结构（Pydantic 模型）。

日志为**只读审计数据**：没有任何"创建/修改"请求模型，
写入只通过 :func:`src.log.service.record_operation` 由业务代码完成。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["OperationLogOut"]

#: 关键字搜索最大长度
KEYWORD_MAX_LENGTH = 50


class OperationLogOut(BaseModel):
    """操作日志响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="日志ID")
    user_id: int | None = Field(
        default=None, description="操作人ID；账号删除后为 null（保留用户名快照）"
    )
    username: str = Field(description="操作人用户名（写入时快照）")
    module: str = Field(description="业务模块：event / checkin / leave 等")
    action: str = Field(
        description="操作类型：create / update / delete / change_status / "
        "face_checkin / manual_checkin / correct / approve / reject 等"
    )
    target_type: str | None = Field(default=None, description="操作对象类型")
    target_id: int | None = Field(default=None, description="操作对象ID")
    target: str | None = Field(
        default=None, description="操作对象展示形式，如 event/12；无对象时为 null"
    )
    detail: str | None = Field(default=None, description="操作摘要")
    ip: str | None = Field(default=None, description="请求来源IP")
    created_at: datetime = Field(description="操作时间")
