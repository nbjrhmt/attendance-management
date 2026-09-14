"""签到活动模型：``event`` 表与活动状态枚举。

设计要点：

- **活动由管理员创建**，面向全村所有"需要签到"的家庭成员使用，
  因此活动本身不与家庭/成员建立关联表，而是通过签到记录（``checkin_record``）反向关联；
- ``late_threshold_minutes``：迟到判定阈值（分钟），创建时未指定则取配置项
  ``settings.DEFAULT_LATE_THRESHOLD_MINUTES``（默认 15）；判定规则见
  :mod:`src.checkin.service`（``checked_at - start_time > 阈值`` 即为迟到）；
- ``status`` 为活动状态机：``pending`` 未开始 → ``active`` 进行中 → ``finished`` 已结束，
  或 ``pending`` → ``cancelled`` 已取消；迁移规则集中在 :mod:`src.event.service`；
- 时间字段与其他表一致，使用**服务器本地时间**（``datetime.now()``）。

索引 ``(status, start_time)`` 支持"按状态 + 时间范围"的列表查询（列表默认按开始时间倒序）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.common.database import BaseModel
from src.common.models import enum_column
from src.config.settings import settings

__all__ = ["Event", "EventStatus"]


class EventStatus(str, Enum):
    """签到活动状态。"""

    PENDING = "pending"
    ACTIVE = "active"
    FINISHED = "finished"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """中文名称，用于提示信息与日志。"""
        return {
            "pending": "未开始",
            "active": "进行中",
            "finished": "已结束",
            "cancelled": "已取消",
        }[self.value]


class Event(BaseModel):
    """签到活动表。"""

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_status_start_time", "status", "start_time"),
        {"comment": "签到活动表"},
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="活动名称",
    )
    description: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="活动说明",
    )
    location: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="活动地点",
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="开始时间（服务器本地时间）",
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="结束时间（必须晚于开始时间）",
    )
    late_threshold_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=settings.DEFAULT_LATE_THRESHOLD_MINUTES,
        comment=f"迟到阈值（分钟），默认 {settings.DEFAULT_LATE_THRESHOLD_MINUTES}",
    )
    status: Mapped[EventStatus] = mapped_column(
        enum_column(EventStatus, name="event_status"),
        nullable=False,
        default=EventStatus.PENDING,
        comment="状态：pending 未开始 / active 进行中 / finished 已结束 / cancelled 已取消",
    )

    @property
    def is_pending(self) -> bool:
        """活动是否未开始。"""
        return self.status == EventStatus.PENDING

    @property
    def is_active(self) -> bool:
        """活动是否进行中。"""
        return self.status == EventStatus.ACTIVE

    @property
    def is_finished(self) -> bool:
        """活动是否已结束。"""
        return self.status == EventStatus.FINISHED

    @property
    def is_cancelled(self) -> bool:
        """活动是否已取消。"""
        return self.status == EventStatus.CANCELLED

    @property
    def is_closed(self) -> bool:
        """活动是否已关闭（已结束或已取消，不能签到、不能修改）。"""
        return self.status in (EventStatus.FINISHED, EventStatus.CANCELLED)

    def __repr__(self) -> str:
        return (
            f"<Event id={self.id} name={self.name!r} status={self.status.value} "
            f"start={self.start_time} end={self.end_time}>"
        )
