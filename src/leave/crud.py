"""请假管理数据访问层（CRUD）。

除常规增删改查外，提供两项业务查询：

- :func:`has_active_leave`：判断某成员在某活动下是否已有"待审批/已通过"的请假
  （用于提交请假的重复校验）；
- :func:`approved_member_ids`：查询某活动下所有**已通过**请假的成员ID
  （活动结束时为这些成员生成 ``leave`` 记录，其余应签到成员生成 ``absent`` 记录）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.common.utils import like_pattern
from src.event.models import Event
from src.family.models import Family
from src.leave.models import LeaveRequest, LeaveStatus
from src.member.models import FamilyMember

__all__ = [
    "approved_member_ids",
    "count_by_event",
    "create_leave",
    "get_leave_by_id",
    "get_leave_with_context",
    "has_active_leave",
    "list_leaves",
    "update_leave",
]

#: 占用中（不可重复提交）的请假状态
ACTIVE_LEAVE_STATUSES: tuple[LeaveStatus, ...] = (
    LeaveStatus.PENDING,
    LeaveStatus.APPROVED,
)


def get_leave_by_id(db: Session, leave_id: int) -> LeaveRequest | None:
    """按主键查询请假申请。"""
    return db.get(LeaveRequest, leave_id)


def create_leave(
    db: Session,
    *,
    event_id: int,
    member_id: int,
    reason: str,
    status: LeaveStatus = LeaveStatus.PENDING,
    commit: bool = True,
) -> LeaveRequest:
    """新增请假申请。"""
    leave = LeaveRequest(
        event_id=event_id,
        member_id=member_id,
        reason=reason,
        status=status,
    )
    db.add(leave)
    if commit:
        db.commit()
        db.refresh(leave)
    else:
        db.flush()
    return leave


def update_leave(
    db: Session, leave: LeaveRequest, values: dict[str, Any], *, commit: bool = True
) -> LeaveRequest:
    """按字段字典更新请假申请（审批 / 撤销）。

    :param commit: 为 ``False`` 时仅 ``flush``，便于与操作日志同事务提交
    """
    for field, value in values.items():
        setattr(leave, field, value)
    if commit:
        db.commit()
        db.refresh(leave)
    else:
        db.flush()
    return leave


def has_active_leave(db: Session, event_id: int, member_id: int) -> bool:
    """某成员在某活动下是否已有待审批或已通过的请假。"""
    return (
        db.scalar(
            select(func.count())
            .select_from(LeaveRequest)
            .where(
                LeaveRequest.event_id == event_id,
                LeaveRequest.member_id == member_id,
                LeaveRequest.status.in_(ACTIVE_LEAVE_STATUSES),
            )
        )
        or 0
    ) > 0


def approved_member_ids(db: Session, event_id: int) -> set[int]:
    """某活动下所有"已通过请假"的成员ID集合。"""
    return set(
        db.scalars(
            select(LeaveRequest.member_id).where(
                LeaveRequest.event_id == event_id,
                LeaveRequest.status == LeaveStatus.APPROVED,
            )
        ).all()
    )


def count_by_event(db: Session, event_id: int) -> int:
    """统计某活动的请假申请数（用于判断活动能否删除）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(LeaveRequest)
            .where(LeaveRequest.event_id == event_id)
        )
        or 0
    )


def get_leave_with_context(
    db: Session, leave_id: int
) -> tuple[LeaveRequest, FamilyMember | None, str, str | None] | None:
    """查询单条请假申请及其展示信息（成员、活动名称、户号）。

    :return: ``(请假申请, 成员或None, 活动名称, 户号)``；不存在时返回 ``None``
    """
    row = db.execute(
        select(LeaveRequest, FamilyMember, Event.name, Family.household_no)
        .join(FamilyMember, FamilyMember.id == LeaveRequest.member_id, isouter=True)
        .join(Family, Family.id == FamilyMember.family_id, isouter=True)
        .join(Event, Event.id == LeaveRequest.event_id, isouter=True)
        .where(LeaveRequest.id == leave_id)
    ).first()
    if row is None:
        return None
    return row[0], row[1], str(row[2] or ""), row[3]


def list_leaves(
    db: Session,
    *,
    status: LeaveStatus | None = None,
    event_id: int | None = None,
    family_id: int | None = None,
    member_id: int | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[tuple[LeaveRequest, FamilyMember | None, str, str | None]], int]:
    """分页查询请假申请（联表带出成员、家庭与活动信息）。

    使用 ``LEFT JOIN`` 关联成员与家庭：即使成员数据异常也不会漏掉请假记录。

    :param keyword: 模糊匹配成员姓名 / 请假事由
    :return: ``([(请假申请, 成员或None, 活动名称, 户号), ...], 总记录数)``
    """
    conditions = []
    if status is not None:
        conditions.append(LeaveRequest.status == status)
    if event_id is not None:
        conditions.append(LeaveRequest.event_id == event_id)
    if member_id is not None:
        conditions.append(LeaveRequest.member_id == member_id)
    if family_id is not None:
        conditions.append(FamilyMember.family_id == family_id)
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                FamilyMember.name.like(pattern, escape="\\"),
                LeaveRequest.reason.like(pattern, escape="\\"),
            )
        )

    total = int(
        db.scalar(
            select(func.count())
            .select_from(LeaveRequest)
            .join(FamilyMember, FamilyMember.id == LeaveRequest.member_id, isouter=True)
            .where(*conditions)
        )
        or 0
    )

    rows = db.execute(
        select(LeaveRequest, FamilyMember, Event.name, Family.household_no)
        .join(FamilyMember, FamilyMember.id == LeaveRequest.member_id, isouter=True)
        .join(Family, Family.id == FamilyMember.family_id, isouter=True)
        .join(Event, Event.id == LeaveRequest.event_id, isouter=True)
        .where(*conditions)
        .order_by(LeaveRequest.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [
        (row[0], row[1] if row[1] is not None else None, str(row[2] or ""), row[3])
        for row in rows
    ]
    return items, total
