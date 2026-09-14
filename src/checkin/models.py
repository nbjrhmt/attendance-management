"""签到记录模型：``checkin_record`` 表与签到方式/状态枚举。

设计要点：

- **一成员一活动一条记录**：``UNIQUE(event_id, member_id)`` 保证签到幂等，
  重复签到由服务层转换为 409「该成员已在本次活动中签到」；
- ``family_id`` 为**冗余字段**：签到记录直接携带家庭ID，统计报表可按家庭聚合
  而无需再与 ``family_member`` 关联（成员后续被停用/迁出也不影响历史统计）；
- ``method`` 可空：``face`` 人脸签到 / ``manual`` 手动签到；
  活动结束时系统自动生成的 ``absent`` 缺勤与 ``leave`` 请假记录没有签到方式，为 ``NULL``；
- ``status`` 覆盖签到状态：``signed`` 已签到 / ``late`` 迟到 / ``absent`` 缺勤 /
  ``leave`` 请假 / ``abnormal`` 异常（异常由管理员人工修正产生）；
- ``checked_at`` 仅在实际签到（``signed`` / ``late``）时有值，
  ``absent`` / ``leave`` 为 ``NULL``；迟到判定即基于 ``checked_at`` 与活动 ``start_time``；
- ``reviewed_by_id`` / ``reviewed_at`` / ``review_remark`` 记录管理员人工修正的审计信息。

外键一律显式声明 ``ON DELETE``：活动/家庭/成员使用 ``RESTRICT``（有签到记录时禁止删除，
避免历史数据悬空），审核人使用 ``SET NULL``（管理员账号被删除后仍保留签到记录）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column
from src.event.models import Event  # noqa: F401  注册 event 模型（外键目标）
from src.family.models import Family  # noqa: F401  注册 family 模型（外键目标）
from src.member.models import FamilyMember  # noqa: F401  注册 family_member 模型（外键目标）
from src.user.models import User  # noqa: F401  注册 sys_user 模型（外键目标）

__all__ = ["CheckinMethod", "CheckinRecord", "CheckinStatus"]


class CheckinMethod(str, Enum):
    """签到方式。"""

    FACE = "face"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {"face": "人脸识别", "manual": "手动签到"}[self.value]


class CheckinStatus(str, Enum):
    """签到状态。"""

    SIGNED = "signed"
    LATE = "late"
    ABSENT = "absent"
    LEAVE = "leave"
    ABNORMAL = "abnormal"

    @property
    def label(self) -> str:
        """中文名称。"""
        return {
            "signed": "已签到",
            "late": "迟到",
            "absent": "缺勤",
            "leave": "请假",
            "abnormal": "异常",
        }[self.value]

    @property
    def is_present(self) -> bool:
        """是否为"到场"状态（计入出勤率）。"""
        return self in (CheckinStatus.SIGNED, CheckinStatus.LATE)


class CheckinRecord(BaseModel):
    """签到记录表（活动 × 成员，唯一）。"""

    __tablename__ = "checkin_record"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "member_id", name="uq_checkin_record_event_member"
        ),
        Index("ix_checkin_record_event_status", "event_id", "status"),
        Index("ix_checkin_record_family", "family_id"),
        {"comment": "签到记录表"},
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("event.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="签到活动ID（event.id）",
    )
    family_id: Mapped[int] = mapped_column(
        ForeignKey("family.id", ondelete="RESTRICT"),
        nullable=False,
        comment="家庭ID（family.id，冗余存储便于按家庭统计）",
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("family_member.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="家庭成员ID（family_member.id）",
    )
    method: Mapped[CheckinMethod | None] = mapped_column(
        enum_column(CheckinMethod, name="checkin_method", length=10),
        nullable=True,
        comment="签到方式：face 人脸 / manual 手动；系统生成的缺勤与请假记录为 NULL",
    )
    status: Mapped[CheckinStatus] = mapped_column(
        enum_column(CheckinStatus, name="checkin_status"),
        nullable=False,
        comment="签到状态：signed 已签到 / late 迟到 / absent 缺勤 / leave 请假 / abnormal 异常",
    )
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="签到时间（服务器本地时间）；缺勤/请假为 NULL",
    )
    face_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="人脸签到得分（0-100），手动签为空",
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="最近一次人工修正的操作人（sys_user.id）",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近一次人工修正时间",
    )
    review_remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="人工修正说明",
    )
    remark: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="备注",
    )

    @property
    def is_present(self) -> bool:
        """是否到场（已签到或迟到）。"""
        return self.status.is_present

    def __repr__(self) -> str:
        return (
            f"<CheckinRecord id={self.id} event_id={self.event_id} "
            f"member_id={self.member_id} status={self.status.value}>"
        )
