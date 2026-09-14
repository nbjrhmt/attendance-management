"""统计报表模块测试（阶段六）。

测试策略：用**固定种子数据**（两个家庭、五名成员、五个不同状态的活动、七条签到记录）
逐项核对每个报表的计数与出勤率，避免"接口返回 200 就算通过"的空洞断言。

种子数据（见 :func:`stats_seed`）：

| 对象 | 内容 |
| --- | --- |
| 家庭A | 测试户主（户主成员）、李小明、王小明 —— 户号 F000001、村组 测试村 |
| 家庭B | 户主 + 陈家成员 —— 村组 幸福村 |
| 活动 | e1/e2 已结束、e3 进行中、e4 未开始、e5 已取消 |
| 签到 | e1：A(签到/迟到/缺勤) + B(签到/缺勤)；e2：A(签到)；e3：A(签到) |

应签到人数（家庭正常 + 成员正常 + 需要签到）= 5。

覆盖点：总览各计数与平均出勤率（含"无已结束活动"空值分支）、单活动统计的家庭聚合与分页、
家庭排行三种排序与村组/关键字筛选（含 LIKE 转义）、无参与家庭不入榜、
趋势日期过滤与顺序、CSV 导出的列头/BOM/中文内容/文件名编码/404，
以及权限矩阵与"统计接口纯读"（调用后数据不变）。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.checkin import crud as checkin_crud
from src.checkin.models import CheckinMethod, CheckinRecord, CheckinStatus
from src.event.models import Event, EventStatus
from src.family import crud as family_crud
from src.family.models import Family, FamilyStatus
from src.log.models import OperationLog
from src.member import crud as member_crud
from src.member.models import FamilyMember, MemberStatus
from tests.helpers import assert_unified_response

#: 接口前缀
STATISTICS = "/api/statistics"
OVERVIEW = f"{STATISTICS}/overview"
FAMILIES = f"{STATISTICS}/families"
TREND = f"{STATISTICS}/trend"
EXPORT = f"{STATISTICS}/export"

#: 种子数据的应签到人数（家庭A 3 人 + 家庭B 2 人）
EXPECTED_TOTAL = 5


def _event_url(event_id: int) -> str:
    """单活动统计地址。"""
    return f"{STATISTICS}/events/{event_id}"


@pytest.fixture()
def stats_seed(
    client: TestClient,
    session_factory: sessionmaker[Session],
    family,
    member: FamilyMember,
    make_member,
    make_event,
    register_householder,
) -> dict:
    """构造统计模块的固定种子数据（家庭A / 家庭B + 五个活动 + 七条签到记录）。"""
    session = session_factory()
    try:
        householder_a = member_crud.get_householder_member(session, family.id)
        householder_a_id = householder_a.id
    finally:
        session.close()

    member_a2 = make_member(family.id, name="王小明", phone="13900000061")

    # 家庭B：注册新户主（自动建档 + 户主成员行）并添加一名成员
    other = register_householder(
        "hushu_stat_b",
        phone="13700000301",
        real_name="陈户主",
        family={"village": "幸福村"},
    )
    session = session_factory()
    try:
        family_b = family_crud.get_family_by_owner(session, other["user"]["id"])
        family_b_id = family_b.id
        householder_b_id = member_crud.get_householder_member(session, family_b_id).id
    finally:
        session.close()
    member_b2_id = client.post(
        "/api/members",
        headers=other["headers"],
        json={"name": "陈家成员", "phone": "13700000302"},
    ).json()["data"]["id"]

    e1 = make_event(
        "已结束活动一",
        start_offset_minutes=-3000,
        end_offset_minutes=-2900,
        status=EventStatus.FINISHED,
    )
    e2 = make_event(
        "已结束活动二",
        start_offset_minutes=-1500,
        end_offset_minutes=-1400,
        status=EventStatus.FINISHED,
    )
    e3 = make_event(
        "进行中活动",
        start_offset_minutes=-60,
        end_offset_minutes=60,
        status=EventStatus.ACTIVE,
    )
    e4 = make_event(
        "未开始活动",
        start_offset_minutes=120,
        end_offset_minutes=180,
        status=EventStatus.PENDING,
    )
    e5 = make_event(
        "已取消活动",
        start_offset_minutes=-45,
        end_offset_minutes=30,
        status=EventStatus.CANCELLED,
    )

    session = session_factory()
    try:

        def record(
            event: Event,
            member_id: int,
            family_id: int,
            status: CheckinStatus,
            method: CheckinMethod | None = CheckinMethod.MANUAL,
        ) -> None:
            checkin_crud.create_record(
                session,
                event_id=event.id,
                family_id=family_id,
                member_id=member_id,
                status=status,
                method=method,
                checked_at=datetime.now() if method else None,
            )

        # e1：A 户 签到 / 迟到 / 缺勤 + B 户 签到 / 缺勤
        record(e1, member.id, family.id, CheckinStatus.SIGNED)
        record(e1, member_a2.id, family.id, CheckinStatus.LATE)
        record(e1, householder_a_id, family.id, CheckinStatus.ABSENT, method=None)
        record(e1, householder_b_id, family_b_id, CheckinStatus.SIGNED)
        record(e1, member_b2_id, family_b_id, CheckinStatus.ABSENT, method=None)
        # e2：A 户 签到；e3：A 户 签到
        record(e2, member.id, family.id, CheckinStatus.SIGNED)
        record(e3, householder_a_id, family.id, CheckinStatus.SIGNED)
    finally:
        session.close()

    return {
        "family_a": family,
        "family_b_id": family_b_id,
        "householder_a_id": householder_a_id,
        "member_a2_id": member_a2.id,
        "householder_b_id": householder_b_id,
        "member_b2_id": member_b2_id,
        "e1": e1,
        "e2": e2,
        "e3": e3,
        "e4": e4,
        "e5": e5,
    }


# ======================================================================
# 一、总览
# ======================================================================
def test_overview_counts(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """总览各计数与平均出勤率与种子数据一致。"""
    response = client.get(OVERVIEW, headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["total_families"] == 2
    assert data["total_members"] == EXPECTED_TOTAL
    assert data["total_events"] == 5
    assert data["active_events"] == 1
    assert data["finished_events"] == 2
    # 签到次数（signed + late）：e1 3 次 + e2 1 次 + e3 1 次 = 5
    assert data["total_checkins"] == 5
    # 已结束活动出勤率：e1 = 3/5 = 0.6，e2 = 1/5 = 0.2 → 平均 0.4
    assert data["avg_attendance_rate"] == 0.4


def test_overview_without_finished_events(
    client: TestClient, admin_headers: dict[str, str], make_event
) -> None:
    """没有已结束活动时平均出勤率为 null（空值分支）。"""
    make_event("仅未开始的活动", start_offset_minutes=60, end_offset_minutes=120)

    data = client.get(OVERVIEW, headers=admin_headers).json()["data"]

    assert data["total_events"] == 1
    assert data["finished_events"] == 0
    assert data["total_checkins"] == 0
    assert data["avg_attendance_rate"] is None


def test_overview_counts_follow_status(
    client: TestClient,
    admin_headers: dict[str, str],
    stats_seed: dict,
    session_factory: sessionmaker[Session],
    family,
) -> None:
    """停用成员与停用家庭对计数的影响（含家庭停用级联停用成员）。"""
    # ① 单独停用王小明：成员数减 1（total_members 只看成员自身状态）
    session = session_factory()
    try:
        member_a2 = session.get(FamilyMember, stats_seed["member_a2_id"])
        member_a2.status = MemberStatus.INACTIVE
        session.commit()
    finally:
        session.close()

    data = client.get(OVERVIEW, headers=admin_headers).json()["data"]
    assert data["total_families"] == 2
    assert data["total_members"] == EXPECTED_TOTAL - 1

    # ② 通过接口停用家庭A（级联停用其成员）→ 家庭数与成员数同步减少
    deactivated = client.delete(
        f"/api/families/{stats_seed['family_a'].id}", headers=admin_headers
    )
    assert deactivated.status_code == 200
    assert_unified_response(deactivated.json())

    data = client.get(OVERVIEW, headers=admin_headers).json()["data"]
    assert data["total_families"] == 1, "家庭A 已停用"
    assert data["total_members"] == 2, "家庭A 的成员被级联停用，只剩家庭B 的 2 名成员"


# ======================================================================
# 二、单活动统计
# ======================================================================
def test_event_statistics_summary_and_families(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """单活动统计：总汇总（全村口径）+ 家庭维度列表。"""
    event = stats_seed["e1"]

    response = client.get(_event_url(event.id), headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["event"]["id"] == event.id
    assert data["event"]["status"] == EventStatus.FINISHED.value

    summary = data["summary"]
    assert summary["total_expected"] == EXPECTED_TOTAL
    assert summary["signed"] == 2
    assert summary["late"] == 1
    assert summary["absent"] == 2
    assert summary["leave"] == 0
    assert summary["abnormal"] == 0
    assert summary["attendance_rate"] == 0.6

    families = data["families"]
    assert families["total"] == 2
    assert families["page"] == 1
    assert families["page_size"] == 10

    first, second = families["items"]
    # 按应签到人数降序：家庭A（3 人）在前
    assert first["family_id"] == stats_seed["family_a"].id
    assert first["household_no"] == "F000001"
    assert first["owner_name"] == "测试户主"
    assert first["village"] == "测试村"
    assert (first["expected"], first["signed"], first["late"], first["absent"]) == (
        3,
        1,
        1,
        1,
    )
    assert first["rate"] == pytest.approx(0.6667)

    assert second["family_id"] == stats_seed["family_b_id"]
    assert second["village"] == "幸福村"
    assert (second["expected"], second["signed"], second["late"], second["absent"]) == (
        2,
        1,
        0,
        1,
    )
    assert second["rate"] == 0.5


def test_event_statistics_family_pagination(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """家庭维度列表分页：家庭A 在第 1 页、家庭B 在第 2 页。"""
    event = stats_seed["e1"]

    first = client.get(
        _event_url(event.id), headers=admin_headers, params={"page": 1, "page_size": 1}
    ).json()["data"]["families"]
    second = client.get(
        _event_url(event.id), headers=admin_headers, params={"page": 2, "page_size": 1}
    ).json()["data"]["families"]

    assert first["total"] == 2 and second["total"] == 2
    assert [item["family_id"] for item in first["items"]] == [
        stats_seed["family_a"].id
    ]
    assert [item["family_id"] for item in second["items"]] == [
        stats_seed["family_b_id"]
    ]


def test_event_statistics_includes_family_with_records_only(
    client: TestClient,
    admin_headers: dict[str, str],
    stats_seed: dict,
    session_factory: sessionmaker[Session],
    family,
) -> None:
    """活动结束后被停用的家庭仍保留历史统计（有记录即入榜）。"""
    session = session_factory()
    try:
        family_a = session.get(type(family), stats_seed["family_a"].id)
        family_a.status = family_a.status.INACTIVE
        session.commit()
    finally:
        session.close()

    families = client.get(
        _event_url(stats_seed["e1"].id), headers=admin_headers
    ).json()["data"]["families"]

    ids = [item["family_id"] for item in families["items"]]
    assert stats_seed["family_a"].id in ids, "已停用但历史有记录的家庭仍应出现在报表中"
    assert families["items"][-1]["family_id"] == stats_seed["family_a"].id


def test_event_statistics_not_found(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """活动不存在返回 404。"""
    response = client.get(_event_url(999999), headers=admin_headers)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到活动不存在")


# ======================================================================
# 三、家庭参与度排行
# ======================================================================
def test_family_rankings_values(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """排行行各字段与种子数据一致（默认按出勤率降序）。"""
    body = client.get(FAMILIES, headers=admin_headers).json()

    assert_unified_response(body)
    assert body["data"]["total"] == 2
    first, second = body["data"]["items"]

    assert first["family_id"] == stats_seed["family_a"].id
    assert first["household_no"] == "F000001"
    assert first["owner_name"] == "测试户主"
    assert first["village"] == "测试村"
    assert first["active_member_count"] == 3
    assert first["checkin_required_count"] == 3
    assert first["event_participated_count"] == 2
    assert first["total_checkins"] == 4, "e1 两次 + e2 一次 + e3 一次"
    assert first["attendance_rate"] == 0.75, "已结束活动 3/4"

    assert second["family_id"] == stats_seed["family_b_id"]
    assert second["owner_name"] == "陈户主"
    assert second["active_member_count"] == 2
    assert second["checkin_required_count"] == 2
    assert second["event_participated_count"] == 1
    assert second["total_checkins"] == 1
    assert second["attendance_rate"] == 0.5


@pytest.mark.parametrize(
    ("order_by", "order", "expected_first"),
    [
        ("rate", "desc", "family_a"),
        ("rate", "asc", "family_b"),
        ("checkins", "desc", "family_a"),
        ("checkins", "asc", "family_b"),
        ("members", "desc", "family_a"),
        ("members", "asc", "family_b"),
    ],
)
def test_family_rankings_order_by(
    client: TestClient,
    admin_headers: dict[str, str],
    stats_seed: dict,
    order_by: str,
    order: str,
    expected_first: str,
) -> None:
    """三种排序字段与两个方向均生效。"""
    body = client.get(
        FAMILIES, headers=admin_headers, params={"order_by": order_by, "order": order}
    ).json()

    assert_unified_response(body)
    items = body["data"]["items"]
    expected_id = (
        stats_seed["family_a"].id
        if expected_first == "family_a"
        else stats_seed["family_b_id"]
    )
    assert items[0]["family_id"] == expected_id


def test_family_rankings_filters(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """村组精确筛选与户号/户主姓名模糊搜索。"""
    by_village = client.get(
        FAMILIES, headers=admin_headers, params={"village": "测试村"}
    ).json()
    assert by_village["data"]["total"] == 1
    assert by_village["data"]["items"][0]["family_id"] == stats_seed["family_a"].id

    empty_village = client.get(
        FAMILIES, headers=admin_headers, params={"village": "不存在的村"}
    ).json()
    assert empty_village["data"]["total"] == 0

    by_no = client.get(FAMILIES, headers=admin_headers, params={"keyword": "F000001"}).json()
    assert by_no["data"]["total"] == 1
    assert by_no["data"]["items"][0]["owner_name"] == "测试户主"

    by_name = client.get(
        FAMILIES, headers=admin_headers, params={"keyword": "测试户主"}
    ).json()
    assert by_name["data"]["total"] == 1
    assert by_name["data"]["items"][0]["family_id"] == stats_seed["family_a"].id

    by_other_name = client.get(
        FAMILIES, headers=admin_headers, params={"keyword": "陈户主"}
    ).json()
    assert by_other_name["data"]["total"] == 1
    assert by_other_name["data"]["items"][0]["family_id"] == stats_seed["family_b_id"]

    # LIKE 通配符已转义：`%` 不再匹配任意字符串
    escaped = client.get(FAMILIES, headers=admin_headers, params={"keyword": "%"}).json()
    assert escaped["data"]["total"] == 0


def test_family_rankings_excludes_families_without_participation(
    client: TestClient,
    admin_headers: dict[str, str],
    stats_seed: dict,
    register_householder,
) -> None:
    """未参与过任何已结束活动的家庭不入榜。"""
    new_family = register_householder("hushu_stat_c", phone="13700000303")
    client.post(
        "/api/members",
        headers=new_family["headers"],
        json={"name": "未参与成员", "phone": "13700000304"},
    )

    body = client.get(FAMILIES, headers=admin_headers, params={"page_size": 100}).json()

    assert body["data"]["total"] == 2, "只有家庭A、家庭B 参与过已结束活动"
    assert {item["family_id"] for item in body["data"]["items"]} == {
        stats_seed["family_a"].id,
        stats_seed["family_b_id"],
    }


def test_family_rankings_pagination(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """排行分页生效。"""
    first = client.get(FAMILIES, headers=admin_headers, params={"page_size": 1}).json()
    second = client.get(
        FAMILIES, headers=admin_headers, params={"page": 2, "page_size": 1}
    ).json()

    assert first["data"]["total"] == 2
    assert len(first["data"]["items"]) == 1
    assert len(second["data"]["items"]) == 1
    assert first["data"]["items"][0]["family_id"] != second["data"]["items"][0]["family_id"]


# ======================================================================
# 四、签到趋势
# ======================================================================
def _trend_full_range(stats_seed: dict) -> dict[str, str]:
    """覆盖全部 5 个活动的显式日期范围：最早活动 start_time 前一天 ~ 最晚活动 start_time 后一天。

    **时间边界说明**：夹具 ``make_event`` 用"当前时刻 + offset 分钟"生成活动时间，
    ``stats_seed`` 中的 e4（"未开始活动"）为 ``+120`` 分钟；在深夜运行（例如 22:30 之后）
    时 e4 的日期会跨到次日，而 ``list_trend`` 的默认窗口是
    ``date.today() - 30 天`` ~ ``date.today()``（当天 23:59:59 截止），
    e4 会被默认窗口过滤，导致"5 个活动全量"的断言间歇性失败。

    因此凡是断言"全部活动"的用例都显式传入日期范围，**不再依赖运行时刻**；
    默认窗口本身由 :func:`test_trend_default_range_includes_recent_events` 单独覆盖。
    """
    events = [
        stats_seed["e1"],
        stats_seed["e2"],
        stats_seed["e3"],
        stats_seed["e5"],
        stats_seed["e4"],
    ]
    start = min(event.start_time for event in events).date() - timedelta(days=1)
    end = max(event.start_time for event in events).date() + timedelta(days=1)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


def test_trend_full_range_and_order(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """显式日期范围内：5 个活动全量返回并按开始时间升序。"""
    body = client.get(
        TREND,
        headers=admin_headers,
        params={**_trend_full_range(stats_seed), "page_size": 100},
    ).json()

    assert_unified_response(body)
    events = [
        stats_seed["e1"],
        stats_seed["e2"],
        stats_seed["e3"],
        stats_seed["e5"],
        stats_seed["e4"],
    ]
    assert body["data"]["total"] == len(events)
    assert [item["event_id"] for item in body["data"]["items"]] == [
        event.id for event in events
    ]

    first = body["data"]["items"][0]
    assert first["name"] == "已结束活动一"
    assert first["start_date"] == stats_seed["e1"].start_time.date().isoformat()
    assert first["status"] == EventStatus.FINISHED.value
    assert first["total_expected"] == EXPECTED_TOTAL
    assert (first["signed"], first["late"], first["absent"]) == (2, 1, 2)
    assert (first["leave"], first["abnormal"]) == (0, 0)
    assert first["attendance_rate"] == 0.6


def test_trend_default_range_includes_recent_events(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """默认窗口（最近 30 天）：e1 / e2 / e3 / e5 必然在窗口内。

    这里**不硬编码 total == 5**：e4（+120 分钟）在深夜运行时会跨到次日而被默认窗口过滤，
    属于默认窗口的正常语义，只断言"至少 4 条且这 4 条必然在内"。
    """
    body = client.get(TREND, headers=admin_headers, params={"page_size": 100}).json()

    assert_unified_response(body)
    returned = [item["event_id"] for item in body["data"]["items"]]
    assert body["data"]["total"] >= 4
    # 默认窗口同样按开始时间升序，前 4 条与显式范围内的顺序一致
    assert returned[:4] == [
        stats_seed["e1"].id,
        stats_seed["e2"].id,
        stats_seed["e3"].id,
        stats_seed["e5"].id,
    ]


def test_trend_date_filter(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """按 event.start_time 的日期范围过滤（含边界当天）。"""
    target_date = stats_seed["e2"].start_time.date().isoformat()

    body = client.get(
        TREND,
        headers=admin_headers,
        params={"start_date": target_date, "end_date": target_date},
    ).json()

    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["event_id"] == stats_seed["e2"].id
    assert body["data"]["items"][0]["signed"] == 1
    assert body["data"]["items"][0]["attendance_rate"] == 0.2


def test_trend_invalid_date_range(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """开始日期晚于结束日期返回 400。"""
    response = client.get(
        TREND,
        headers=admin_headers,
        params={"start_date": "2026-03-31", "end_date": "2026-03-01"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "开始日期不能晚于结束日期"


def test_trend_pagination(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """趋势分页（升序、分页稳定）。"""
    body = client.get(
        TREND,
        headers=admin_headers,
        params={**_trend_full_range(stats_seed), "page": 2, "page_size": 2},
    ).json()

    assert body["data"]["total"] == 5
    assert body["data"]["page"] == 2
    assert len(body["data"]["items"]) == 2


# ======================================================================
# 五、CSV 导出
# ======================================================================
def test_export_events_csv(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """活动统计 CSV：文件名编码、BOM、列头与数据行。"""
    response = client.get(EXPORT, headers=admin_headers, params={"type": "events"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    expected_disposition = (
        f"attachment; filename*=UTF-8''{quote('活动签到统计.csv')}"
    )
    assert response.headers["content-disposition"] == expected_disposition

    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff"), "首字符必须是 UTF-8 BOM，保证 Excel 不乱码"

    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0] == [
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
    ]
    assert len(rows) == 6, "1 行列头 + 5 个活动"
    # 按开始时间升序：e1 → e2 → e3 → e5 → e4
    assert rows[1][0] == str(stats_seed["e1"].id)
    assert rows[1][1] == "已结束活动一"
    assert rows[1][4] == "已结束"
    assert rows[1][5:11] == ["5", "2", "1", "2", "0", "0"]
    assert rows[1][11] == "0.6000"
    assert rows[2][1] == "已结束活动二"
    assert rows[2][6] == "1"
    assert rows[3][1] == "进行中活动"
    assert rows[4][1] == "已取消活动"
    # 未开始活动：应签到 5、各状态均为 0
    assert rows[5][1] == "未开始活动"
    assert rows[5][5:12] == ["5", "0", "0", "0", "0", "0", "0.0000"]


def test_export_events_by_event_id(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """按活动导出：只包含指定活动；活动不存在返回 404。"""
    event = stats_seed["e2"]

    response = client.get(
        EXPORT, headers=admin_headers, params={"type": "events", "event_id": event.id}
    )
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8").lstrip("\ufeff"))))
    assert len(rows) == 2
    assert rows[1][1] == "已结束活动二"
    assert rows[1][6] == "1", "仅 1 人已签到"

    missing = client.get(
        EXPORT, headers=admin_headers, params={"type": "events", "event_id": 999999}
    )
    assert missing.status_code == 404
    payload = missing.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到活动不存在")


def test_export_families_csv(
    client: TestClient, admin_headers: dict[str, str], stats_seed: dict
) -> None:
    """家庭统计 CSV：列头、中文内容与出勤率，且受筛选参数影响。"""
    response = client.get(EXPORT, headers=admin_headers, params={"type": "families"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f"attachment; filename*=UTF-8''{quote('家庭参与度统计.csv')}"
    )

    text = response.content.decode("utf-8")
    assert text.startswith("\ufeff")
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0] == ["户号", "户主", "村组", "在册成员", "应签到", "参与活动数", "签到次数", "出勤率"]
    assert len(rows) == 3, "1 行列头 + 2 个家庭"
    assert rows[1] == ["F000001", "测试户主", "测试村", "3", "3", "2", "4", "0.7500"]
    assert rows[2][2] == "幸福村"
    assert rows[2][7] == "0.5000"

    filtered = client.get(
        EXPORT,
        headers=admin_headers,
        params={"type": "families", "village": "幸福村"},
    )
    filtered_rows = list(
        csv.reader(io.StringIO(filtered.content.decode("utf-8").lstrip("\ufeff")))
    )
    assert len(filtered_rows) == 2
    assert filtered_rows[1][2] == "幸福村"


def test_export_requires_valid_type(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """type 必填且只能是 events / families。"""
    missing = client.get(EXPORT, headers=admin_headers)
    assert missing.status_code == 200
    assert_unified_response(missing.json(), code=400)

    invalid = client.get(EXPORT, headers=admin_headers, params={"type": "unknown"})
    assert invalid.status_code == 400
    payload = invalid.json()
    assert_unified_response(payload, code=400)
    assert payload["message"].startswith("导出类型只能是")


# ======================================================================
# 六、权限与"纯读"
# ======================================================================
def test_statistics_permissions(
    client: TestClient,
    admin_headers: dict[str, str],
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    stats_seed: dict,
) -> None:
    """统计接口：管理员与工作人员可访问，家庭用户 403，未登录 401。"""
    endpoints = [
        (OVERVIEW, {}),
        (_event_url(stats_seed["e1"].id), {}),
        (FAMILIES, {}),
        (TREND, {}),
        (EXPORT, {"params": {"type": "events"}}),
    ]

    for path, kwargs in endpoints:
        for headers in (admin_headers, staff_headers):
            allowed = client.get(path, headers=headers, **kwargs)
            assert allowed.status_code == 200, f"{path} 应允许管理员/工作人员"

        forbidden = client.get(path, headers=family_headers, **kwargs)
        assert forbidden.status_code == 403, f"{path} 应拒绝家庭用户"
        assert_unified_response(forbidden.json(), code=403)

        anonymous = client.get(path, **kwargs)
        assert anonymous.status_code == 401
        assert_unified_response(anonymous.json(), code=401)


def test_statistics_endpoints_are_read_only(
    client: TestClient,
    admin_headers: dict[str, str],
    stats_seed: dict,
    session_factory: sessionmaker[Session],
) -> None:
    """统计接口不写库：调用前后各表行数完全一致（含操作日志）。"""
    def snapshot() -> dict[str, int]:
        session = session_factory()
        try:
            return {
                "families": session.query(Family).count(),
                "members": session.query(FamilyMember).count(),
                "events": session.query(Event).count(),
                "checkins": session.query(CheckinRecord).count(),
                "logs": session.query(OperationLog).count(),
            }
        finally:
            session.close()

    before = snapshot()

    client.get(OVERVIEW, headers=admin_headers)
    client.get(_event_url(stats_seed["e1"].id), headers=admin_headers)
    client.get(FAMILIES, headers=admin_headers)
    client.get(TREND, headers=admin_headers)
    client.get(EXPORT, headers=admin_headers, params={"type": "events"})
    client.get(EXPORT, headers=admin_headers, params={"type": "families"})

    assert snapshot() == before
