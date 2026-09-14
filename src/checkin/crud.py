"""签到业务数据访问层（CRUD）。

除常规增删改查外，还提供统计与"应签到成员"查询：

- :func:`list_records`：签到记录分页（联表带出成员姓名与活动名称）；
- :func:`count_by_status`：按状态分组计数，供活动签到汇总使用；
- :func:`list_expected_members`：**应签到成员**集合
  （家庭正常 + 成员正常 + ``needs_checkin=True``），活动结束时据此生成缺勤/请假记录；
- :func:`bulk_create_records`：批量写入自动生成的缺勤/请假记录。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.checkin.models import CheckinMethod, CheckinRecord, CheckinStatus
from src.common.utils import like_pattern
from src.event.models import Event
from src.family.models import Family, FamilyStatus
from src.member.models import FamilyMember, MemberStatus

__all__ = [
    "bulk_create_records",
    "count_by_event",
    "count_by_status",
    "count_expected_members",
    "create_record",
    "get_record_by_id",
    "get_record_by_member",
    "list_expected_members",
    "list_existing_member_ids",
    "list_records",
    "update_record",
]


def get_record_by_id(db: Session, checkin_id: int) -> CheckinRecord | None:
    """按主键查询签到记录。"""
    return db.get(CheckinRecord, checkin_id)


def get_record_by_member(
    db: Session, event_id: int, member_id: int
) -> CheckinRecord | None:
    """按"活动 + 成员"查询签到记录（唯一约束保证最多一条）。"""
    return db.scalar(
        select(CheckinRecord).where(
            CheckinRecord.event_id == event_id,
            CheckinRecord.member_id == member_id,
        )
    )


def create_record(
    db: Session,
    *,
    event_id: int,
    family_id: int,
    member_id: int,
    status: CheckinStatus,
    method: CheckinMethod | None = None,
    checked_at: datetime | None = None,
    face_score: float | None = None,
    reviewed_by_id: int | None = None,
    reviewed_at: datetime | None = None,
    review_remark: str | None = None,
    remark: str | None = None,
    commit: bool = True,
) -> CheckinRecord:
    """新增签到记录。

    :param commit: 是否立即提交；为 ``False`` 时仅 ``flush``，由调用方统一提交
    """
    record = CheckinRecord(
        event_id=event_id,
        family_id=family_id,
        member_id=member_id,
        method=method,
        status=status,
        checked_at=checked_at,
        face_score=face_score,
        reviewed_by_id=reviewed_by_id,
        reviewed_at=reviewed_at,
        review_remark=review_remark,
        remark=remark,
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def update_record(
    db: Session, record: CheckinRecord, values: dict[str, Any], *, commit: bool = True
) -> CheckinRecord:
    """按字段字典更新签到记录。"""
    for field, value in values.items():
        setattr(record, field, value)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def bulk_create_records(
    db: Session, rows: Sequence[dict[str, Any]], *, commit: bool = True
) -> int:
    """批量写入签到记录（活动结束时生成缺勤/请假记录）。

    :return: 实际写入的记录条数
    """
    if not rows:
        return 0
    db.add_all([CheckinRecord(**row) for row in rows])
    if commit:
        db.commit()
    else:
        db.flush()
    return len(rows)


def list_existing_member_ids(db: Session, event_id: int) -> set[int]:
    """查询某活动已有签到记录的成员ID集合（用于去重与幂等判断）。"""
    return set(
        db.scalars(
            select(CheckinRecord.member_id).where(CheckinRecord.event_id == event_id)
        ).all()
    )


def count_by_event(db: Session, event_id: int) -> int:
    """统计某活动的签到记录数（用于判断活动能否删除）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(CheckinRecord)
            .where(CheckinRecord.event_id == event_id)
        )
        or 0
    )


def count_by_status(
    db: Session, event_id: int, *, family_id: int | None = None
) -> dict[CheckinStatus, int]:
    """按状态分组统计某活动的签到记录数。

    :param family_id: 仅统计指定家庭（家庭用户的本户视角）
    :return: ``{CheckinStatus.SIGNED: 3, CheckinStatus.ABSENT: 1, ...}``（无记录的状态不出现）
    """
    conditions = [CheckinRecord.event_id == event_id]
    if family_id is not None:
        conditions.append(CheckinRecord.family_id == family_id)

    rows = db.execute(
        select(CheckinRecord.status, func.count())
        .where(*conditions)
        .group_by(CheckinRecord.status)
    ).all()
    return {status: int(count) for status, count in rows}


def list_records(
    db: Session,
    *,
    event_id: int | None = None,
    family_id: int | None = None,
    status: CheckinStatus | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[tuple[CheckinRecord, FamilyMember, str]], int]:
    """分页查询签到记录（联表带出成员姓名与活动名称）。

    :param keyword: 模糊匹配成员姓名
    :return: ``([(签到记录, 成员, 活动名称), ...], 总记录数)``
    """
    conditions = []
    if event_id is not None:
        conditions.append(CheckinRecord.event_id == event_id)
    if family_id is not None:
        conditions.append(CheckinRecord.family_id == family_id)
    if status is not None:
        conditions.append(CheckinRecord.status == status)
    if keyword:
        conditions.append(
            FamilyMember.name.like(like_pattern(keyword), escape="\\")
        )

    total = (
        db.scalar(
            select(func.count())
            .select_from(CheckinRecord)
            .join(FamilyMember, FamilyMember.id == CheckinRecord.member_id)
            .where(*conditions)
        )
        or 0
    )

    rows = db.execute(
        select(CheckinRecord, FamilyMember, Event.name)
        .join(FamilyMember, FamilyMember.id == CheckinRecord.member_id)
        .join(Event, Event.id == CheckinRecord.event_id)
        .where(*conditions)
        .order_by(CheckinRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [(row[0], row[1], str(row[2])) for row in rows]
    return items, int(total)


def count_expected_members(db: Session, *, family_id: int | None = None) -> int:
    """统计应签到人数：家庭正常 + 成员正常 + 需要签到。

    :param family_id: 仅统计指定家庭（家庭用户查看本户视角的汇总时使用）
    """
    conditions = [
        FamilyMember.status == MemberStatus.ACTIVE,
        FamilyMember.needs_checkin.is_(True),
        Family.status == FamilyStatus.ACTIVE,
    ]
    if family_id is not None:
        conditions.append(FamilyMember.family_id == family_id)

    return int(
        db.scalar(
            select(func.count())
            .select_from(FamilyMember)
            .join(Family, Family.id == FamilyMember.family_id)
            .where(*conditions)
        )
        or 0
    )


def list_expected_members(db: Session, *, exclude_member_ids: Iterable[int] = ()) -> list[FamilyMember]:
    """查询**应签到成员**：家庭正常 + 成员正常 + 需要签到。

    :param exclude_member_ids: 需要排除的成员ID（已有签到记录者）
    :return: 需要生成缺勤/请假记录的成员列表
    """
    conditions = [
        FamilyMember.status == MemberStatus.ACTIVE,
        FamilyMember.needs_checkin.is_(True),
        Family.status == FamilyStatus.ACTIVE,
    ]
    excluded = {int(value) for value in exclude_member_ids}
    if excluded:
        conditions.append(FamilyMember.id.not_in(excluded))

    return list(
        db.scalars(
            select(FamilyMember)
            .join(Family, Family.id == FamilyMember.family_id)
            .where(*conditions)
            .order_by(FamilyMember.id.asc())
        ).all()
    )
