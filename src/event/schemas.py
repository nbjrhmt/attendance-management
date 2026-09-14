"""签到活动模块的请求/响应数据结构（Pydantic 模型）。

约定与其他模块一致：输入模型 ``extra="forbid"`` 拒绝未声明字段；
输出模型开启 ``from_attributes`` 以便直接由 ORM 对象生成。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.config.settings import settings
from src.event.models import EventStatus

__all__ = ["EventCreate", "EventOut", "EventStatusUpdate", "EventUpdate"]

#: 活动名称最大长度
NAME_MAX_LENGTH = 100
#: 活动说明最大长度
DESCRIPTION_MAX_LENGTH = 500
#: 活动地点最大长度
LOCATION_MAX_LENGTH = 100
#: 迟到阈值上限（分钟）：24 小时
MAX_LATE_THRESHOLD_MINUTES = 1440


class EventCreate(BaseModel):
    """创建签到活动请求。

    活动创建后状态固定为 ``pending``（未开始），需要通过
    ``PUT /api/events/{event_id}/status`` 迁移到 ``active`` 才开始接受签到。
    ``end_time`` 必须晚于 ``start_time``，校验在服务层完成，
    以便返回与其他接口一致的中文错误消息（400「结束时间必须晚于开始时间」）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=NAME_MAX_LENGTH, description="活动名称"
    )
    description: str | None = Field(
        default=None, max_length=DESCRIPTION_MAX_LENGTH, description="活动说明"
    )
    location: str | None = Field(
        default=None, max_length=LOCATION_MAX_LENGTH, description="活动地点"
    )
    start_time: datetime = Field(description="开始时间（服务器本地时间）")
    end_time: datetime = Field(description="结束时间，必须晚于开始时间")
    late_threshold_minutes: int = Field(
        default=settings.DEFAULT_LATE_THRESHOLD_MINUTES,
        ge=0,
        le=MAX_LATE_THRESHOLD_MINUTES,
        description=f"迟到阈值（分钟），默认 {settings.DEFAULT_LATE_THRESHOLD_MINUTES}",
    )


class EventUpdate(BaseModel):
    """修改签到活动请求（未提交的字段保持原值）。

    已结束（``finished``）或已取消（``cancelled``）的活动不允许修改（409）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=1, max_length=NAME_MAX_LENGTH, description="活动名称"
    )
    description: str | None = Field(
        default=None, max_length=DESCRIPTION_MAX_LENGTH, description="活动说明"
    )
    location: str | None = Field(
        default=None, max_length=LOCATION_MAX_LENGTH, description="活动地点"
    )
    start_time: datetime | None = Field(default=None, description="开始时间")
    end_time: datetime | None = Field(default=None, description="结束时间")
    late_threshold_minutes: int | None = Field(
        default=None,
        ge=0,
        le=MAX_LATE_THRESHOLD_MINUTES,
        description="迟到阈值（分钟）",
    )


class EventStatusUpdate(BaseModel):
    """活动状态迁移请求。

    合法迁移：``pending → active``（开始）、``active → finished``（结束，
    同时生成缺勤/请假记录）、``pending → cancelled``（取消）；其余组合返回 409。
    """

    model_config = ConfigDict(extra="forbid")

    status: EventStatus = Field(description="目标状态：active / finished / cancelled")


class EventOut(BaseModel):
    """签到活动信息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="活动ID")
    name: str = Field(description="活动名称")
    description: str | None = Field(default=None, description="活动说明")
    location: str | None = Field(default=None, description="活动地点")
    start_time: datetime = Field(description="开始时间")
    end_time: datetime = Field(description="结束时间")
    late_threshold_minutes: int = Field(description="迟到阈值（分钟）")
    status: EventStatus = Field(description="状态：pending / active / finished / cancelled")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime = Field(description="更新时间")
