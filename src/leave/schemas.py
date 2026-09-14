"""请假管理模块的请求/响应数据结构（Pydantic 模型）。

说明：

- 请假申请由**户主**为本户成员提交（管理员可为任意成员提交）；
- 审批结果只允许 ``approved`` / ``rejected``；撤销由户主本人或管理员操作；
- 响应中附带 ``event_name`` / ``member_name`` / ``family_id``（由服务层关联查询填充），
  避免前端为展示列表再发起多次请求。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.leave.models import LeaveStatus

__all__ = [
    "LeaveCreate",
    "LeaveOut",
    "LeaveReviewRequest",
]

#: 请假事由最大长度
REASON_MAX_LENGTH = 255
#: 审批说明最大长度
REMARK_MAX_LENGTH = 255


class LeaveCreate(BaseModel):
    """提交请假申请（户主为本户成员 / 管理员为任意成员）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: int = Field(ge=1, description="请假对应的签到活动ID")
    member_id: int = Field(ge=1, description="请假的家庭成员ID")
    reason: str = Field(
        min_length=1, max_length=REASON_MAX_LENGTH, description="请假事由"
    )


class LeaveReviewRequest(BaseModel):
    """审批请假请求（仅管理员，且仅待审批的申请可审批）。"""

    model_config = ConfigDict(extra="forbid")

    status: LeaveStatus = Field(description="审批结果：approved 通过 / rejected 驳回")
    remark: str | None = Field(
        default=None, max_length=REMARK_MAX_LENGTH, description="审批说明（可选）"
    )


class LeaveOut(BaseModel):
    """请假申请响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="请假申请ID")
    event_id: int = Field(description="签到活动ID")
    event_name: str = Field(description="活动名称")
    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    family_id: int | None = Field(default=None, description="成员所属家庭ID")
    household_no: str | None = Field(default=None, description="成员所属家庭户号")
    reason: str = Field(description="请假事由")
    status: LeaveStatus = Field(description="状态：pending / approved / rejected / cancelled")
    reviewed_by_id: int | None = Field(default=None, description="审批人ID")
    reviewed_at: datetime | None = Field(default=None, description="审批时间")
    review_remark: str | None = Field(default=None, description="审批说明")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
