"""请假记录模型：``leave_request`` 表与请假状态枚举。

设计要点：

- **成员按活动请假**：一次请假针对"某成员 + 某活动"，因此与 ``event``、``family_member``
  建立外键；同一成员同一活动可存在多条历史记录（如先被驳回、再重新提交），
  但**同时只能存在一条待审批/已通过**的申请（由服务层校验，见 :mod:`src.leave.service`）；
- 状态机：``pending`` 待审批 → ``approved`` 已通过 / ``rejected`` 已驳回 / ``cancelled`` 已撤销；
  已审批或已撤销的申请不能再次变更；
- 请假与签到联动：**已通过的请假只影响活动结束时的缺勤生成**——
  该成员若实际到场签到，仍以实际签到记录（``signed`` / ``late``）为准；
- ``reviewed_by_id`` / ``reviewed_at`` / ``review_remark`` 记录审批人与审批说明。

外键一律显式声明 ``ON DELETE``：活动/成员使用 ``RESTRICT``（保留历史请假记录，
有请假记录的活动不允许删除），审批人使用 ``SET NULL``。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column
from src.event.models import Event  # noqa: F401  注册 event 模型（外键目标）
from src.member.models import FamilyMember  # noqa: F401  注册 family_member 模型（外键目标）
from src.user.models import User  # noqa: F401  注册 sys_user 模型（外键目标）

__all__ = ["LeaveRequest", "LeaveStatus"]


class LeaveStatus(str, Enum):
    """请假申请状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {
            "pending": "待审批",
            "approved": "已通过",
            "rejected": "已驳回",
            "cancelled": "已撤销",
        }[self.value]


class LeaveRequest(BaseModel):
    """请假申请表。"""

    __tablename__ = "leave_request"
    __table_args__ = (
        Index("ix_leave_request_event_status", "event_id", "status"),
        Index("ix_leave_request_member", "member_id"),
        {"comment": "请假申请表"},
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="签到活动ID（event.id）",
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("family_member.id", ondelete="RESTRICT"),
        nullable=False,
        comment="请假的家庭成员ID（family_member.id）",
    )
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="请假事由",
    )
    status: Mapped[LeaveStatus] = mapped_column(
        enum_column(LeaveStatus, name="leave_status"),
        nullable=False,
        default=LeaveStatus.PENDING,
        comment="状态：pending 待审批 / approved 已通过 / rejected 已驳回 / cancelled 已撤销",
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="审批人（sys_user.id）",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="审批时间",
    )
    review_remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="审批说明",
    )

    @property
    def is_pending(self) -> bool:
        """是否为待审批状态。"""
        return self.status == LeaveStatus.PENDING

    @property
    def is_approved(self) -> bool:
        """是否为已通过状态。"""
        return self.status == LeaveStatus.APPROVED

    def __repr__(self) -> str:
        return (
            f"<LeaveRequest id={self.id} event_id={self.event_id} "
            f"member_id={self.member_id} status={self.status.value}>"
        )
