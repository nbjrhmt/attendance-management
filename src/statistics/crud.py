"""统计报表数据访问层（只读聚合查询）。

本模块**不建表、不写库**，所有查询都是聚合/分页读取，
因此没有 :mod:`src.statistics.models`：

- 计数类：:func:`count_families` / :func:`count_members` / :func:`event_status_counts`
- 签到聚合：:func:`checkin_status_counts`（可按活动/家庭/活动状态过滤）、
  :func:`event_status_counts_map`（一次取多个活动的状态分布）
- 应签到人数：:func:`expected_counts_by_family`（家庭维度，全局口径为
  :func:`src.checkin.crud.count_expected_members`）
- 报表行：:func:`list_event_family_stats`（单活动家庭维度）、
  :func:`list_family_rankings`（家庭参与度排行）、:func:`list_trend_events`（按时间的活动）

SQL 同时兼容 MySQL 8 与 SQLite（单元测试）：条件聚合统一使用 ``CASE WHEN`` + ``COALESCE``，
不使用窗口函数或数据库特有语法。
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.orm import Session

from src.checkin.models import CheckinRecord, CheckinStatus
from src.common.utils import like_pattern
from src.event.models import Event, EventStatus
from src.family.models import Family, FamilyStatus
from src.member.models import FamilyMember, MemberStatus
from src.user.models import User

__all__ = [
    "checkin_status_counts",
    "count_events",
    "count_families",
    "count_members",
    "event_status_counts",
    "event_status_counts_map",
    "expected_counts_by_family",
    "list_event_family_stats",
    "list_event_ids",
    "list_events_for_export",
    "list_family_rankings",
    "list_trend_events",
]

#: 视为"实际到场"的签到状态（计入出勤率与签到次数）
_PRESENT_STATUSES: tuple[CheckinStatus, ...] = (
    CheckinStatus.SIGNED,
    CheckinStatus.LATE,
)


def _present_sum(column) -> ColumnElement[int]:
    """构造"signed + late 计数"的条件聚合表达式。"""
    return func.sum(
        case((column.in_(_PRESENT_STATUSES), 1), else_=0)
    )


def _status_sum(column, status: CheckinStatus) -> ColumnElement[int]:
    """构造"某状态计数"的条件聚合表达式。"""
    return func.sum(case((column == status, 1), else_=0))


# ----------------------------------------------------------------------
# 基础计数
# ----------------------------------------------------------------------
def count_families(db: Session, *, status: FamilyStatus | None = None) -> int:
    """统计家庭数（可指定状态）。"""
    conditions = [] if status is None else [Family.status == status]
    return int(
        db.scalar(select(func.count()).select_from(Family).where(*conditions)) or 0
    )


def count_members(db: Session, *, status: MemberStatus | None = None) -> int:
    """统计成员数（可指定状态）。"""
    conditions = [] if status is None else [FamilyMember.status == status]
    return int(
        db.scalar(
            select(func.count()).select_from(FamilyMember).where(*conditions)
        )
        or 0
    )


def count_events(db: Session, *, status: EventStatus | None = None) -> int:
    """统计活动数（可指定状态）。"""
    conditions = [] if status is None else [Event.status == status]
    return int(
        db.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    )


def event_status_counts(db: Session) -> dict[EventStatus, int]:
    """按状态统计活动数。"""
    rows = db.execute(
        select(Event.status, func.count()).group_by(Event.status)
    ).all()
    return {status: int(count) for status, count in rows}


def checkin_status_counts(
    db: Session,
    *,
    event_id: int | None = None,
    family_id: int | None = None,
    event_ids: list[int] | None = None,
    event_status: EventStatus | None = None,
) -> dict[CheckinStatus, int]:
    """按状态统计签到记录数。

    :param event_status: 仅统计指定状态的活动（需联表 ``event``）
    :return: ``{CheckinStatus.SIGNED: 3, ...}``（无记录的状态不出现）
    """
    conditions = []
    if event_id is not None:
        conditions.append(CheckinRecord.event_id == event_id)
    if family_id is not None:
        conditions.append(CheckinRecord.family_id == family_id)
    if event_ids is not None:
        conditions.append(CheckinRecord.event_id.in_(event_ids))

    query = select(CheckinRecord.status, func.count()).select_from(CheckinRecord)
    if event_status is not None:
        query = query.join(Event, Event.id == CheckinRecord.event_id)
        conditions.append(Event.status == event_status)

    rows = db.execute(query.where(*conditions).group_by(CheckinRecord.status)).all()
    return {status: int(count) for status, count in rows}


def event_status_counts_map(
    db: Session, event_ids: list[int]
) -> dict[int, dict[CheckinStatus, int]]:
    """一次取回多个活动的签到状态分布。

    :return: ``{event_id: {CheckinStatus.SIGNED: 2, ...}}``
    """
    if not event_ids:
        return {}

    rows = db.execute(
        select(CheckinRecord.event_id, CheckinRecord.status, func.count())
        .where(CheckinRecord.event_id.in_(event_ids))
        .group_by(CheckinRecord.event_id, CheckinRecord.status)
    ).all()

    result: dict[int, dict[CheckinStatus, int]] = {}
    for event_id, status, count in rows:
        result.setdefault(int(event_id), {})[status] = int(count)
    return result


# ----------------------------------------------------------------------
# 应签到人数（按家庭）
# ----------------------------------------------------------------------
def expected_counts_by_family(db: Session) -> dict[int, int]:
    """统计每个家庭的应签到人数：家庭正常 + 成员正常 + 需要签到。

    :return: ``{family_id: 应签到人数}``（只包含应签到人数 > 0 的家庭）
    """
    rows = db.execute(
        select(FamilyMember.family_id, func.count())
        .join(Family, Family.id == FamilyMember.family_id)
        .where(
            FamilyMember.status == MemberStatus.ACTIVE,
            FamilyMember.needs_checkin.is_(True),
            Family.status == FamilyStatus.ACTIVE,
        )
        .group_by(FamilyMember.family_id)
    ).all()
    return {int(family_id): int(count) for family_id, count in rows}


# ----------------------------------------------------------------------
# 单活动 · 家庭维度
# ----------------------------------------------------------------------
def list_event_family_stats(
    db: Session, event_id: int, *, page: int = 1, page_size: int = 10
) -> tuple[list[tuple[Family, int, int, int, int, int]], int]:
    """单活动的家庭维度统计（分页，按应签到人数降序）。

    入榜条件：该户在本次活动中"有应签到成员"或"有签到记录"——
    这样既能展示尚未签到的家庭，也能保留活动结束后被停用家庭的历史统计。

    :return: ``([(家庭, expected, signed, late, absent, leave), ...], 总家庭数)``
    """
    expected_sub = (
        select(
            FamilyMember.family_id.label("family_id"),
            func.count().label("expected"),
        )
        .join(Family, Family.id == FamilyMember.family_id)
        .where(
            FamilyMember.status == MemberStatus.ACTIVE,
            FamilyMember.needs_checkin.is_(True),
            Family.status == FamilyStatus.ACTIVE,
        )
        .group_by(FamilyMember.family_id)
        .subquery()
    )
    records_sub = (
        select(
            CheckinRecord.family_id.label("family_id"),
            _status_sum(CheckinRecord.status, CheckinStatus.SIGNED).label("signed"),
            _status_sum(CheckinRecord.status, CheckinStatus.LATE).label("late"),
            _status_sum(CheckinRecord.status, CheckinStatus.ABSENT).label("absent"),
            _status_sum(CheckinRecord.status, CheckinStatus.LEAVE).label("leave"),
            func.count().label("recorded"),
        )
        .where(CheckinRecord.event_id == event_id)
        .group_by(CheckinRecord.family_id)
        .subquery()
    )

    expected_col = func.coalesce(expected_sub.c.expected, 0)
    recorded_col = func.coalesce(records_sub.c.recorded, 0)
    condition = or_(expected_col > 0, recorded_col > 0)

    joins = (
        select(Family)
        .join(expected_sub, expected_sub.c.family_id == Family.id, isouter=True)
        .join(records_sub, records_sub.c.family_id == Family.id, isouter=True)
    )

    total = int(
        db.scalar(
            select(func.count()).select_from(joins.where(condition).subquery())
        )
        or 0
    )
    rows = db.execute(
        select(
            Family,
            expected_col.label("expected"),
            func.coalesce(records_sub.c.signed, 0).label("signed"),
            func.coalesce(records_sub.c.late, 0).label("late"),
            func.coalesce(records_sub.c.absent, 0).label("absent"),
            func.coalesce(records_sub.c.leave, 0).label("leave"),
        )
        .join(expected_sub, expected_sub.c.family_id == Family.id, isouter=True)
        .join(records_sub, records_sub.c.family_id == Family.id, isouter=True)
        .where(condition)
        .order_by(expected_col.desc(), Family.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [
        (
            row[0],
            int(row[1] or 0),
            int(row[2] or 0),
            int(row[3] or 0),
            int(row[4] or 0),
            int(row[5] or 0),
        )
        for row in rows
    ]
    return items, total


# ----------------------------------------------------------------------
# 家庭参与度排行
# ----------------------------------------------------------------------
def list_family_rankings(
    db: Session,
    *,
    village: str | None = None,
    keyword: str | None = None,
    order_by: str = "rate",
    order: str = "desc",
    page: int = 1,
    page_size: int = 10,
) -> tuple[
    list[tuple[Family, int, int, int, int, int, int]],
    int,
]:
    """家庭参与度排行（分页）。

    入榜条件：**在已结束活动中产生过签到记录**（即至少参与过一场已结束活动）。

    :param order_by: ``rate`` 出勤率 / ``checkins`` 签到次数 / ``members`` 在册成员数
    :param order: ``asc`` / ``desc``
    :return: ``([(家庭, 在册成员数, 应签到成员数, 参与场次, 签到次数,
        已结束活动记录数, 已结束活动到场数), ...], 总家庭数)``
    """
    finished_records = (
        select(
            CheckinRecord.family_id.label("family_id"),
            func.count(func.distinct(CheckinRecord.event_id)).label("participated"),
            func.count().label("recorded"),
            _present_sum(CheckinRecord.status).label("present"),
        )
        .join(Event, Event.id == CheckinRecord.event_id)
        .where(Event.status == EventStatus.FINISHED)
        .group_by(CheckinRecord.family_id)
        .subquery()
    )
    all_records = (
        select(
            CheckinRecord.family_id.label("family_id"),
            _present_sum(CheckinRecord.status).label("checkins"),
        )
        .group_by(CheckinRecord.family_id)
        .subquery()
    )
    members_sub = (
        select(
            FamilyMember.family_id.label("family_id"),
            func.sum(
                case((FamilyMember.status == MemberStatus.ACTIVE, 1), else_=0)
            ).label("active_members"),
            func.sum(
                case(
                    (
                        and_(
                            FamilyMember.status == MemberStatus.ACTIVE,
                            FamilyMember.needs_checkin.is_(True),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("checkin_required"),
        )
        .group_by(FamilyMember.family_id)
        .subquery()
    )

    conditions = []
    if village:
        conditions.append(Family.village == village)
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                Family.household_no.like(pattern, escape="\\"),
                User.real_name.like(pattern, escape="\\"),
            )
        )

    rate_expr = (
        func.coalesce(finished_records.c.present, 0) * 1.0
        / func.coalesce(finished_records.c.recorded, 1)
    )
    checkins_expr = func.coalesce(all_records.c.checkins, 0)
    members_expr = func.coalesce(members_sub.c.active_members, 0)

    order_column = {
        "rate": rate_expr,
        "checkins": checkins_expr,
        "members": members_expr,
    }[order_by]
    ordering = order_column.asc() if order == "asc" else order_column.desc()

    base = (
        select(Family)
        .join(User, User.id == Family.owner_id)
        .join(finished_records, finished_records.c.family_id == Family.id)
        .where(*conditions)
    )
    total = int(
        db.scalar(select(func.count()).select_from(base.subquery())) or 0
    )

    rows = db.execute(
        select(
            Family,
            members_expr.label("active_members"),
            func.coalesce(members_sub.c.checkin_required, 0).label("checkin_required"),
            finished_records.c.participated.label("participated"),
            checkins_expr.label("checkins"),
            finished_records.c.recorded.label("recorded"),
            finished_records.c.present.label("present"),
        )
        .join(User, User.id == Family.owner_id)
        .join(finished_records, finished_records.c.family_id == Family.id)
        .join(all_records, all_records.c.family_id == Family.id, isouter=True)
        .join(members_sub, members_sub.c.family_id == Family.id, isouter=True)
        .where(*conditions)
        .order_by(ordering, Family.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [
        (
            row[0],
            int(row[1] or 0),
            int(row[2] or 0),
            int(row[3] or 0),
            int(row[4] or 0),
            int(row[5] or 0),
            int(row[6] or 0),
        )
        for row in rows
    ]
    return items, total


# ----------------------------------------------------------------------
# 按时间的活动趋势
# ----------------------------------------------------------------------
def list_trend_events(
    db: Session,
    *,
    start_date: date,
    end_date: date,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[Event], int]:
    """按 ``event.start_time`` 日期范围查询活动（升序，分页）。

    :return: ``(当前页活动列表, 总活动数)``
    """
    start_time = datetime.combine(start_date, time.min)
    end_time = datetime.combine(end_date, time.max)
    conditions = [Event.start_time >= start_time, Event.start_time <= end_time]

    total = int(
        db.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    )
    items = list(
        db.scalars(
            select(Event)
            .where(*conditions)
            .order_by(Event.start_time.asc(), Event.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, total


def list_event_ids(db: Session, *, status: EventStatus | None = None) -> list[int]:
    """查询活动ID列表（按开始时间升序），用于批量聚合统计。"""
    conditions = [] if status is None else [Event.status == status]
    return [
        int(event_id)
        for event_id in db.scalars(
            select(Event.id)
            .where(*conditions)
            .order_by(Event.start_time.asc(), Event.id.asc())
        ).all()
    ]


def list_events_for_export(
    db: Session,
    *,
    event_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 5000,
) -> list[Event]:
    """导出用活动查询：可按活动ID或开始时间范围过滤（按开始时间升序）。

    :param limit: 单次导出上限（防御性保护，村级数据量远小于该值）
    """
    conditions = []
    if event_id is not None:
        conditions.append(Event.id == event_id)
    if start_date is not None:
        conditions.append(Event.start_time >= datetime.combine(start_date, time.min))
    if end_date is not None:
        conditions.append(Event.start_time <= datetime.combine(end_date, time.max))

    return list(
        db.scalars(
            select(Event)
            .where(*conditions)
            .order_by(Event.start_time.asc(), Event.id.asc())
            .limit(limit)
        ).all()
    )
