"""统计报表业务逻辑层（纯读）。

五个报表：

| 接口 | 说明 | 口径要点 |
| --- | --- | --- |
| ``GET /api/statistics/overview`` | 全村总览 | 平均出勤率 = 各已结束活动出勤率的算术平均（2 位小数） |
| ``GET /api/statistics/events/{id}`` | 单活动统计 | 总汇总复用阶段五 :func:`src.checkin.service.build_summary`，另给家庭维度分页 |
| ``GET /api/statistics/families`` | 家庭参与度排行 | 仅"参与过已结束活动"的家庭入榜 |
| ``GET /api/statistics/trend`` | 按活动时间的签到趋势 | 默认最近 30 天，按开始时间升序 |
| ``GET /api/statistics/export`` | CSV 导出 | UTF-8 BOM + ``filename*=UTF-8''``，Excel 直接打开不乱码 |

**出勤率口径**（与阶段五保持一致的"当前应签到人数"口径）：

- 活动维度：``(signed + late) / 应签到人数``，应签到人数 = 当前"家庭正常 + 成员正常 +
  ``needs_checkin=True``"的成员总数（活动结束时系统正是按这个集合生成缺勤记录，
  因此活动结束后它与记录总数一致）；
- 家庭排行维度：分母取该户在**已结束活动中的签到记录总数**（即活动结束时的应签到总数，
  属于历史口径，不会因日后新增成员而回溯变化）；
- 分母为 0 时：总览与排行返回 ``None``，单活动家庭行返回 ``None``，
  趋势行返回 ``0.0``（与阶段五 summary 的取值保持一致）。

CSV 导出说明：使用标准库 ``csv`` + ``io.StringIO``（**不引入任何新依赖**），
首字符写入 ``\\ufeff``（UTF-8 BOM）保证 Excel 打开中文不乱码；
出勤率列输出 0~1 的比例并固定保留 4 位小数。

错误码约定（与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 活动不存在（单活动统计 / 按活动导出） | 404 | 签到活动不存在：``{event_id}`` |
| 趋势查询的开始日期晚于结束日期 | 400 | 开始日期不能晚于结束日期 |
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from src.checkin import crud as checkin_crud
from src.checkin import service as checkin_service
from src.checkin.models import CheckinStatus
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.common.schemas import PageData
from src.event import service as event_service
from src.event.models import EventStatus
from src.event.schemas import EventOut
from src.family.models import Family, FamilyStatus
from src.member.models import MemberStatus
from src.statistics import crud
from src.statistics.schemas import (
    EventFamilyStatOut,
    EventStatisticsOut,
    FamilyRankingOut,
    StatisticsOverviewOut,
    TrendPointOut,
)

__all__ = [
    "build_events_csv",
    "build_families_csv",
    "build_overview",
    "get_event_statistics",
    "list_family_rankings",
    "list_trend",
]

#: 趋势查询默认回溯天数
DEFAULT_TREND_DAYS = 30

#: CSV 导出单次最大行数（村级数据量远小于该上限，仅作防御性保护）
EXPORT_ROW_LIMIT = 5000

#: 排行可选排序字段与方向
OrderByField = Literal["rate", "checkins", "members"]
OrderDirection = Literal["asc", "desc"]


def _present(counts: dict[CheckinStatus, int]) -> int:
    """从状态计数中取出"实际到场"次数（signed + late）。"""
    return counts.get(CheckinStatus.SIGNED, 0) + counts.get(CheckinStatus.LATE, 0)


def _rate(numerator: int, denominator: int, *, empty: float | None = None) -> float | None:
    """计算比例（4 位小数）；分母为 0 时返回 ``empty``。"""
    if not denominator:
        return empty
    return round(numerator / denominator, 4)


# ----------------------------------------------------------------------
# 1. 总览
# ----------------------------------------------------------------------
def build_overview(db: Session) -> StatisticsOverviewOut:
    """全村总览：家庭/成员/活动/签到计数与已结束活动的平均出勤率。"""
    event_counts = crud.event_status_counts(db)
    checkin_counts = crud.checkin_status_counts(db)
    finished_events = event_counts.get(EventStatus.FINISHED, 0)

    avg_rate: float | None = None
    if finished_events:
        expected = checkin_crud.count_expected_members(db)
        finished_ids = crud.list_event_ids(db, status=EventStatus.FINISHED)
        status_map = crud.event_status_counts_map(db, finished_ids)
        rates = [
            _rate(_present(status_map.get(event_id, {})), expected, empty=0.0) or 0.0
            for event_id in finished_ids
        ]
        avg_rate = round(sum(rates) / len(rates), 2)

    return StatisticsOverviewOut(
        total_families=crud.count_families(db, status=FamilyStatus.ACTIVE),
        total_members=crud.count_members(db, status=MemberStatus.ACTIVE),
        total_events=crud.count_events(db),
        active_events=event_counts.get(EventStatus.ACTIVE, 0),
        finished_events=finished_events,
        total_checkins=_present(checkin_counts),
        avg_attendance_rate=avg_rate,
    )


# ----------------------------------------------------------------------
# 2. 单活动统计
# ----------------------------------------------------------------------
def _to_family_stat(
    family: Family, expected: int, signed: int, late: int, absent: int, leave: int
) -> EventFamilyStatOut:
    """家庭 + 计数 -> 家庭维度统计行。"""
    return EventFamilyStatOut(
        family_id=family.id,
        household_no=family.household_no,
        owner_name=family.owner.real_name if family.owner is not None else "",
        village=family.village,
        expected=expected,
        signed=signed,
        late=late,
        absent=absent,
        leave=leave,
        rate=_rate(signed + late, expected),
    )


def get_event_statistics(
    db: Session, event_id: int, *, page: int = 1, page_size: int = 10
) -> EventStatisticsOut:
    """单活动统计：活动信息 + 全村口径汇总 + 家庭维度分页。

    :raises BusinessError: 活动不存在（404）
    """
    event = event_service.load_event(db, event_id)
    summary = checkin_service.build_summary(db, event)
    rows, total = crud.list_event_family_stats(
        db, event_id, page=page, page_size=page_size
    )

    return EventStatisticsOut(
        event=EventOut.model_validate(event),
        summary=summary,
        families=PageData[EventFamilyStatOut](
            total=total,
            page=page,
            page_size=page_size,
            items=[_to_family_stat(*row) for row in rows],
        ),
    )


# ----------------------------------------------------------------------
# 3. 家庭参与度排行
# ----------------------------------------------------------------------
def list_family_rankings(
    db: Session,
    *,
    village: str | None = None,
    keyword: str | None = None,
    order_by: OrderByField = "rate",
    order: OrderDirection = "desc",
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[FamilyRankingOut], int]:
    """家庭参与度排行（仅参与过已结束活动的家庭入榜）。"""
    rows, total = crud.list_family_rankings(
        db,
        village=village,
        keyword=keyword,
        order_by=order_by,
        order=order,
        page=page,
        page_size=page_size,
    )

    items = [
        FamilyRankingOut(
            family_id=family.id,
            household_no=family.household_no,
            owner_name=family.owner.real_name if family.owner is not None else "",
            village=family.village,
            active_member_count=active_members,
            checkin_required_count=checkin_required,
            event_participated_count=participated,
            total_checkins=checkins,
            attendance_rate=_rate(present, recorded),
        )
        for (
            family,
            active_members,
            checkin_required,
            participated,
            checkins,
            recorded,
            present,
        ) in rows
    ]
    return items, total


# ----------------------------------------------------------------------
# 4. 签到趋势
# ----------------------------------------------------------------------
def list_trend(
    db: Session,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[TrendPointOut], int]:
    """按活动开始时间的签到趋势（升序，分页）。

    :param start_date: 起始日期，缺省为"今天 - 30 天"
    :param end_date: 结束日期，缺省为今天
    :raises BusinessError: 开始日期晚于结束日期（400）
    """
    today = date.today()
    start = start_date or today - timedelta(days=DEFAULT_TREND_DAYS)
    end = end_date or today
    if start > end:
        raise BusinessError("开始日期不能晚于结束日期", code=ResponseCode.PARAM_ERROR)

    events, total = crud.list_trend_events(
        db, start_date=start, end_date=end, page=page, page_size=page_size
    )
    status_map = crud.event_status_counts_map(db, [event.id for event in events])
    expected = checkin_crud.count_expected_members(db)

    items = []
    for event in events:
        counts = status_map.get(event.id, {})
        items.append(
            TrendPointOut(
                event_id=event.id,
                name=event.name,
                start_date=event.start_time.date(),
                status=event.status,
                total_expected=expected,
                signed=counts.get(CheckinStatus.SIGNED, 0),
                late=counts.get(CheckinStatus.LATE, 0),
                absent=counts.get(CheckinStatus.ABSENT, 0),
                leave=counts.get(CheckinStatus.LEAVE, 0),
                abnormal=counts.get(CheckinStatus.ABNORMAL, 0),
                attendance_rate=_rate(_present(counts), expected, empty=0.0) or 0.0,
            )
        )
    return items, total


# ----------------------------------------------------------------------
# 5. CSV 导出
# ----------------------------------------------------------------------
def _csv_text(header: list[str], rows: list[list[str]]) -> str:
    """构造 CSV 文本：UTF-8 BOM + 标准库 csv 生成（Excel 直接打开不乱码）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()


def _format_time(value) -> str:
    """时间列格式：``YYYY-MM-DD HH:MM:SS``（Excel 可直接识别为日期时间）。"""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def build_events_csv(
    db: Session,
    *,
    event_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> str:
    """导出活动签到统计 CSV。

    :param event_id: 指定活动则只导出该活动；否则导出日期范围内的全部活动
    :raises BusinessError: 指定活动不存在（404）
    """
    events = crud.list_events_for_export(
        db,
        event_id=event_id,
        start_date=start_date,
        end_date=end_date,
        limit=EXPORT_ROW_LIMIT,
    )
    if event_id is not None and not events:
        raise BusinessError(
            f"签到活动不存在：{event_id}", code=ResponseCode.NOT_FOUND
        )

    status_map = crud.event_status_counts_map(db, [event.id for event in events])
    expected = checkin_crud.count_expected_members(db)

    rows: list[list[str]] = []
    for event in events:
        counts = status_map.get(event.id, {})
        rate = _rate(_present(counts), expected, empty=0.0) or 0.0
        rows.append(
            [
                str(event.id),
                event.name,
                _format_time(event.start_time),
                _format_time(event.end_time),
                event.status.label,
                str(expected),
                str(counts.get(CheckinStatus.SIGNED, 0)),
                str(counts.get(CheckinStatus.LATE, 0)),
                str(counts.get(CheckinStatus.ABSENT, 0)),
                str(counts.get(CheckinStatus.LEAVE, 0)),
                str(counts.get(CheckinStatus.ABNORMAL, 0)),
                f"{rate:.4f}",
            ]
        )

    return _csv_text(
        [
            "活动ID",
            "活动名称",
            "开始时间",
            "结束时间",
            "状态",
            "应签到",
            "已签到",
            "迟到",
            "缺勤",
            "请假",
            "异常",
            "出勤率",
        ],
        rows,
    )


def build_families_csv(
    db: Session,
    *,
    village: str | None = None,
    keyword: str | None = None,
    order_by: OrderByField = "rate",
    order: OrderDirection = "desc",
) -> str:
    """导出家庭参与度统计 CSV（全部入榜家庭，不受分页限制）。"""
    items, _ = list_family_rankings(
        db,
        village=village,
        keyword=keyword,
        order_by=order_by,
        order=order,
        page=1,
        page_size=EXPORT_ROW_LIMIT,
    )

    rows = [
        [
            item.household_no or "",
            item.owner_name,
            item.village or "",
            str(item.active_member_count),
            str(item.checkin_required_count),
            str(item.event_participated_count),
            str(item.total_checkins),
            "" if item.attendance_rate is None else f"{item.attendance_rate:.4f}",
        ]
        for item in items
    ]

    return _csv_text(
        ["户号", "户主", "村组", "在册成员", "应签到", "参与活动数", "签到次数", "出勤率"],
        rows,
    )
