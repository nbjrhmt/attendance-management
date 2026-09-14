"""签到活动业务逻辑层。

职责：

- 活动的创建、查询、修改与删除；
- **状态机**：``pending`` 未开始 → ``active`` 进行中 → ``finished`` 已结束，
  或 ``pending`` → ``cancelled`` 已取消；不在表中的迁移一律 409；
- **结束联动**：``active → finished`` 时委托 :mod:`src.checkin.service` 生成
  缺勤/请假记录，再落活动状态（同一次提交内完成）。

权限由接口层依赖保证：写操作（创建/修改/删除/状态迁移）仅管理员，
读操作对所有登录用户开放。

错误码约定（与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 活动不存在 | 404 | 签到活动不存在：``{event_id}`` |
| 结束时间早于开始时间 | 400 | 结束时间必须晚于开始时间 |
| 修改已结束/已取消的活动 | 409 | 活动已结束/已取消，无法修改 |
| 非法状态迁移 | 409 | 不允许的状态变更：未开始 → 已结束 |
| 删除非"未开始"的活动 | 409 | 只有未开始的活动可以删除 |
| 活动已有签到/请假记录 | 409 | 该活动已有签到记录，无法删除 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from src.checkin import crud as checkin_crud
from src.checkin import service as checkin_service
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.event import crud
from src.event.models import Event, EventStatus
from src.event.schemas import EventCreate, EventOut, EventStatusUpdate, EventUpdate
from src.leave import crud as leave_crud
from src.log import service as log_service
from src.user.models import User

__all__ = [
    "create_event",
    "delete_event",
    "get_event",
    "list_events",
    "load_event",
    "transition_status",
    "update_event",
]

#: 操作日志中的模块名
_LOG_MODULE = "event"

#: 合法的状态迁移表（键为当前状态，值为允许迁移到的目标状态）
_ALLOWED_TRANSITIONS: dict[EventStatus, tuple[EventStatus, ...]] = {
    EventStatus.PENDING: (EventStatus.ACTIVE, EventStatus.CANCELLED),
    EventStatus.ACTIVE: (EventStatus.FINISHED,),
    EventStatus.FINISHED: (),
    EventStatus.CANCELLED: (),
}


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def load_event(db: Session, event_id: int) -> Event:
    """获取活动 ORM 对象，不存在时抛出 404。"""
    event = crud.get_event_by_id(db, event_id)
    if event is None:
        raise BusinessError(
            f"签到活动不存在：{event_id}", code=ResponseCode.NOT_FOUND
        )
    return event


def _validate_time_range(start_time: datetime, end_time: datetime) -> None:
    """校验活动时间区间：结束时间必须晚于开始时间。"""
    if end_time <= start_time:
        raise BusinessError(
            "结束时间必须晚于开始时间", code=ResponseCode.PARAM_ERROR
        )


def _to_out(event: Event) -> EventOut:
    """ORM 活动对象 -> 响应 DTO。"""
    return EventOut.model_validate(event)


# ----------------------------------------------------------------------
# 创建 / 查询
# ----------------------------------------------------------------------
def create_event(db: Session, current_user: User, data: EventCreate) -> EventOut:
    """创建签到活动（状态固定为 pending），并写入操作日志。"""
    _validate_time_range(data.start_time, data.end_time)

    event = crud.create_event(
        db,
        name=data.name,
        description=data.description,
        location=data.location,
        start_time=data.start_time,
        end_time=data.end_time,
        late_threshold_minutes=data.late_threshold_minutes,
        commit=False,
    )
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action="create",
        target_type="event",
        target_id=event.id,
        detail=f"创建活动：{event.name}",
    )
    db.commit()
    db.refresh(event)
    return _to_out(event)


def list_events(
    db: Session,
    *,
    status: EventStatus | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[EventOut], int]:
    """分页查询活动列表。"""
    events, total = crud.list_events(
        db, status=status, keyword=keyword, page=page, page_size=page_size
    )
    return [_to_out(event) for event in events], total


def get_event(db: Session, event_id: int) -> EventOut:
    """查询活动详情。"""
    return _to_out(load_event(db, event_id))


# ----------------------------------------------------------------------
# 修改 / 删除
# ----------------------------------------------------------------------
def update_event(
    db: Session, current_user: User, event_id: int, data: EventUpdate
) -> EventOut:
    """修改活动（仅未开始 / 进行中的活动可改），并写入操作日志。"""
    event = load_event(db, event_id)
    if event.is_closed:
        raise BusinessError(
            f"活动已{event.status.label}，无法修改", code=ResponseCode.CONFLICT
        )

    values: dict[str, Any] = data.model_dump(exclude_unset=True)
    if "name" in values and values["name"] is None:
        raise BusinessError("活动名称不能为空", code=ResponseCode.PARAM_ERROR)

    start_time = values.get("start_time") or event.start_time
    end_time = values.get("end_time") or event.end_time
    _validate_time_range(start_time, end_time)

    updated = crud.update_event(db, event, values, commit=False)
    detail = f"修改活动：{updated.name}"
    if values:
        detail += f"；字段：{'、'.join(sorted(values))}"
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action="update",
        target_type="event",
        target_id=updated.id,
        detail=detail,
    )
    db.commit()
    db.refresh(updated)
    return _to_out(updated)


def delete_event(db: Session, current_user: User, event_id: int) -> None:
    """删除活动：仅"未开始且无签到/请假记录"的活动可物理删除，并写入操作日志。"""
    event = load_event(db, event_id)
    if not event.is_pending:
        raise BusinessError(
            f"只有未开始的活动可以删除（当前状态：{event.status.label}）",
            code=ResponseCode.CONFLICT,
        )
    if checkin_crud.count_by_event(db, event_id) > 0:
        raise BusinessError("该活动已有签到记录，无法删除", code=ResponseCode.CONFLICT)
    if leave_crud.count_by_event(db, event_id) > 0:
        raise BusinessError("该活动已有请假记录，无法删除", code=ResponseCode.CONFLICT)

    # 删除后 ORM 对象即失效，先取出日志需要的字段
    event_name = event.name
    crud.delete_event(db, event, commit=False)
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action="delete",
        target_type="event",
        target_id=event_id,
        detail=f"删除活动：{event_name}（ID {event_id}）",
    )
    db.commit()


# ----------------------------------------------------------------------
# 状态迁移
# ----------------------------------------------------------------------
def transition_status(
    db: Session, current_user: User, event_id: int, data: EventStatusUpdate
) -> tuple[EventOut, int]:
    """按状态机迁移活动状态，并写入操作日志。

    :return: ``(活动信息, 本次自动生成的缺勤/请假记录数)``；
        只有 ``active → finished`` 会产生记录，其余迁移返回 0
    """
    event = load_event(db, event_id)
    current, target = event.status, data.status

    if target == current:
        raise BusinessError(
            f"活动已处于「{current.label}」状态", code=ResponseCode.CONFLICT
        )
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise BusinessError(
            f"不允许的状态变更：{current.label} → {target.label}",
            code=ResponseCode.CONFLICT,
        )

    generated = 0
    if target == EventStatus.FINISHED:
        # 先生成缺勤/请假记录，再落活动状态，二者在同一次提交内完成
        generated = checkin_service.generate_absent_records(db, event, commit=False)

    updated = crud.update_event(db, event, {"status": target}, commit=False)
    detail = f"状态：{current.value}→{target.value}"
    if generated:
        detail += f"；自动生成 {generated} 条缺勤/请假记录"
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action="change_status",
        target_type="event",
        target_id=updated.id,
        detail=detail,
    )
    db.commit()
    db.refresh(updated)
    return _to_out(updated), generated
