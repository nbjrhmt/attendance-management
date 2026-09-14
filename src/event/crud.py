"""签到活动数据访问层（CRUD）。

本层只负责数据库读写，不做权限判断与状态机校验（见 :mod:`src.event.service`）。
写操作默认内部完成 ``commit`` 与 ``refresh``；传入 ``commit=False`` 时仅 ``flush``，
由调用方统一提交（用于"活动结束 + 生成缺勤记录"的原子写入）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.common.utils import like_pattern
from src.event.models import Event, EventStatus

__all__ = [
    "create_event",
    "delete_event",
    "get_event_by_id",
    "list_events",
    "update_event",
]


def get_event_by_id(db: Session, event_id: int) -> Event | None:
    """按主键查询活动。"""
    return db.get(Event, event_id)


def create_event(
    db: Session,
    *,
    name: str,
    start_time: datetime,
    end_time: datetime,
    description: str | None = None,
    location: str | None = None,
    late_threshold_minutes: int,
    status: EventStatus = EventStatus.PENDING,
    commit: bool = True,
) -> Event:
    """创建签到活动。

    :param commit: 是否立即提交；为 ``False`` 时仅 ``flush``，由调用方统一提交
    """
    event = Event(
        name=name,
        description=description,
        location=location,
        start_time=start_time,
        end_time=end_time,
        late_threshold_minutes=late_threshold_minutes,
        status=status,
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


def update_event(
    db: Session, event: Event, values: dict[str, Any], *, commit: bool = True
) -> Event:
    """按字段字典更新活动（只更新传入的字段）。

    :param commit: 为 ``False`` 时仅 ``flush``，便于与操作日志同事务提交
    """
    for field, value in values.items():
        setattr(event, field, value)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


def delete_event(db: Session, event: Event, *, commit: bool = True) -> None:
    """物理删除活动（仅允许"未开始且无签到/请假记录"的活动，见服务层）。

    :param commit: 为 ``False`` 时仅 ``flush``，便于与操作日志同事务提交
    """
    db.delete(event)
    if commit:
        db.commit()
    else:
        db.flush()


def list_events(
    db: Session,
    *,
    status: EventStatus | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[Event], int]:
    """分页查询活动列表（按开始时间倒序、同时间按ID倒序）。

    :param keyword: 模糊匹配活动名称 / 活动地点
    :return: ``(当前页活动列表, 总记录数)``
    """
    conditions = []
    if status is not None:
        conditions.append(Event.status == status)
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                Event.name.like(pattern, escape="\\"),
                Event.location.like(pattern, escape="\\"),
            )
        )

    total = db.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Event)
            .where(*conditions)
            .order_by(Event.start_time.desc(), Event.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, int(total)
