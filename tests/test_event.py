"""签到活动模块测试（阶段五）。

覆盖点：

- 权限：创建/修改/删除/状态迁移仅管理员（工作人员与家庭用户 403），列表与详情所有登录用户可读；
- 参数校验：结束时间必须晚于开始时间、名称必填/超长、迟到阈值非负、未认证 401；
- 列表：分页、状态筛选、关键字（名称/地点）搜索；
- 修改：未开始/进行中可改，已结束/已取消返回 409，修改后时间区间仍需合法；
- 删除：仅"未开始且无签到/请假记录"可删，其余 409；
- 状态机：pending → active → finished、pending → cancelled，非法迁移 409；
- 结束联动：active → finished 时自动为应签到成员生成缺勤/请假记录。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.checkin.models import CheckinRecord, CheckinStatus
from src.event.models import Event, EventStatus
from src.family.models import Family, FamilyStatus
from src.leave.models import LeaveRequest, LeaveStatus
from src.member.models import FamilyMember, MemberStatus
from tests.helpers import assert_unified_response

#: 活动接口前缀
EVENTS = "/api/events"


def _payload(
    *,
    name: str = "村民大会",
    start_offset_minutes: int = 30,
    end_offset_minutes: int = 120,
    **extra,
) -> dict:
    """构造活动请求体（相对当前时间偏移，返回 ISO 字符串）。"""
    now = datetime.now()
    body = {
        "name": name,
        "start_time": (now + timedelta(minutes=start_offset_minutes)).isoformat(),
        "end_time": (now + timedelta(minutes=end_offset_minutes)).isoformat(),
        "location": "村委会大院",
        "description": "季度村民大会签到",
    }
    body.update(extra)
    return body


# ======================================================================
# 一、创建与权限
# ======================================================================
def test_admin_creates_event(client: TestClient, admin_headers: dict[str, str]) -> None:
    """管理员创建活动：状态为 pending，迟到阈值默认取配置值 15 分钟。"""
    response = client.post(EVENTS, headers=admin_headers, json=_payload())

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["name"] == "村民大会"
    assert data["location"] == "村委会大院"
    assert data["status"] == EventStatus.PENDING.value
    assert data["late_threshold_minutes"] == 15


def test_create_event_with_custom_late_threshold(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """可自定义迟到阈值。"""
    response = client.post(
        EVENTS, headers=admin_headers, json=_payload(late_threshold_minutes=0)
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["late_threshold_minutes"] == 0


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_create_event(
    client: TestClient, request: pytest.FixtureRequest, role_fixture: str
) -> None:
    """工作人员与家庭用户不能创建活动（403）。"""
    headers = request.getfixturevalue(role_fixture)

    response = client.post(EVENTS, headers=headers, json=_payload())

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_create_event_requires_login(client: TestClient) -> None:
    """未携带令牌访问返回 401。"""
    response = client.post(EVENTS, json=_payload())

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_create_event_rejects_invalid_time_range(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """结束时间必须晚于开始时间（400）。"""
    body = _payload(start_offset_minutes=60, end_offset_minutes=30)

    response = client.post(EVENTS, headers=admin_headers, json=body)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "结束时间必须晚于开始时间"


def test_create_event_rejects_equal_time_range(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """开始时间与结束时间相同同样报错。"""
    response = client.post(
        EVENTS, headers=admin_headers, json=_payload(start_offset_minutes=30, end_offset_minutes=30)
    )

    assert response.status_code == 400
    assert response.json()["message"] == "结束时间必须晚于开始时间"


def test_create_event_validation_errors(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """参数校验：缺少名称、名称为空、迟到阈值为负、存在未声明字段。"""
    missing_name = _payload()
    missing_name.pop("name")
    negative_threshold = _payload(late_threshold_minutes=-1)
    blank_name = _payload(name="")
    extra_field = _payload(unknown_field="x")

    for body in (missing_name, negative_threshold, blank_name, extra_field):
        response = client.post(EVENTS, headers=admin_headers, json=body)
        assert response.status_code == 200, f"参数校验错误应返回 HTTP 200：{body}"
        assert_unified_response(response.json(), code=400)


# ======================================================================
# 二、列表与详情
# ======================================================================
def test_list_events_with_filters(
    client: TestClient, admin_headers: dict[str, str], make_event
) -> None:
    """列表支持状态筛选与名称/地点关键字搜索，并按开始时间倒序。"""
    make_event("上午活动", start_offset_minutes=-240, end_offset_minutes=-180)
    make_event("下午活动", start_offset_minutes=-120, end_offset_minutes=-60)
    make_event("已取消活动", status=EventStatus.CANCELLED, location="广场")

    all_events = client.get(EVENTS, headers=admin_headers, params={"page_size": 100}).json()
    assert_unified_response(all_events)
    assert all_events["data"]["total"] == 3
    names = [item["name"] for item in all_events["data"]["items"]]
    assert names == ["已取消活动", "下午活动", "上午活动"], "应按开始时间倒序"

    cancelled = client.get(
        EVENTS, headers=admin_headers, params={"status": "cancelled"}
    ).json()
    assert cancelled["data"]["total"] == 1
    assert cancelled["data"]["items"][0]["name"] == "已取消活动"

    by_name = client.get(EVENTS, headers=admin_headers, params={"keyword": "下午"}).json()
    assert [item["name"] for item in by_name["data"]["items"]] == ["下午活动"]

    by_location = client.get(EVENTS, headers=admin_headers, params={"keyword": "广场"}).json()
    assert [item["name"] for item in by_location["data"]["items"]] == ["已取消活动"]


def test_list_events_pagination(
    client: TestClient, admin_headers: dict[str, str], make_event
) -> None:
    """分页参数生效。"""
    for index in range(3):
        make_event(f"活动{index}")

    first = client.get(
        EVENTS, headers=admin_headers, params={"page": 1, "page_size": 2}
    ).json()
    second = client.get(
        EVENTS, headers=admin_headers, params={"page": 2, "page_size": 2}
    ).json()

    assert first["data"]["total"] == 3
    assert len(first["data"]["items"]) == 2
    assert len(second["data"]["items"]) == 1


@pytest.mark.parametrize("role_fixture", ["admin_headers", "staff_headers", "family_headers"])
def test_list_and_detail_readable_by_all_roles(
    client: TestClient, request: pytest.FixtureRequest, role_fixture: str, event: Event
) -> None:
    """列表与详情对所有登录角色开放。"""
    headers = request.getfixturevalue(role_fixture)

    listed = client.get(EVENTS, headers=headers)
    assert listed.status_code == 200
    assert_unified_response(listed.json())

    detail = client.get(f"{EVENTS}/{event.id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert_unified_response(body)
    assert body["data"]["id"] == event.id
    assert body["data"]["status"] == EventStatus.PENDING.value


def test_event_detail_not_found(client: TestClient, admin_headers: dict[str, str]) -> None:
    """活动不存在返回 404。"""
    response = client.get(f"{EVENTS}/999999", headers=admin_headers)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到活动不存在")


# ======================================================================
# 三、修改
# ======================================================================
def test_admin_updates_event(
    client: TestClient, admin_headers: dict[str, str], event: Event, db: Session
) -> None:
    """管理员修改活动：未提交字段保持原值。"""
    response = client.put(
        f"{EVENTS}/{event.id}",
        headers=admin_headers,
        json={"name": "秋季村民大会", "late_threshold_minutes": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["name"] == "秋季村民大会"
    assert body["data"]["late_threshold_minutes"] == 30
    assert body["data"]["location"] == "村委会", "未提交的字段应保持原值"

    db.rollback()
    assert db.get(Event, event.id).name == "秋季村民大会"


def test_update_event_rejects_invalid_time_range(
    client: TestClient, admin_headers: dict[str, str], event: Event
) -> None:
    """修改后的结束时间早于开始时间返回 400。"""
    response = client.put(
        f"{EVENTS}/{event.id}",
        headers=admin_headers,
        json={"end_time": (event.start_time - timedelta(minutes=1)).isoformat()},
    )

    assert response.status_code == 400
    assert response.json()["message"] == "结束时间必须晚于开始时间"


@pytest.mark.parametrize("status", [EventStatus.FINISHED, EventStatus.CANCELLED])
def test_cannot_update_closed_event(
    client: TestClient,
    admin_headers: dict[str, str],
    make_event,
    status: EventStatus,
) -> None:
    """已结束 / 已取消的活动不能修改（409）。"""
    closed = make_event("已关闭活动", status=status)

    response = client.put(
        f"{EVENTS}/{closed.id}", headers=admin_headers, json={"name": "改名"}
    )

    assert response.status_code == 409
    payload = response.json()
    assert_unified_response(payload, code=409)
    assert payload["message"] == f"活动已{status.label}，无法修改"


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_update_event(
    client: TestClient, request: pytest.FixtureRequest, role_fixture: str, event: Event
) -> None:
    """工作人员与家庭用户不能修改活动（403）。"""
    headers = request.getfixturevalue(role_fixture)

    response = client.put(f"{EVENTS}/{event.id}", headers=headers, json={"name": "改名"})

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 四、删除
# ======================================================================
def test_admin_deletes_pending_event(
    client: TestClient, admin_headers: dict[str, str], event: Event, db: Session
) -> None:
    """未开始且无记录的活动可以删除。"""
    response = client.delete(f"{EVENTS}/{event.id}", headers=admin_headers)

    assert response.status_code == 200
    assert_unified_response(response.json())

    db.rollback()
    assert db.get(Event, event.id) is None


@pytest.mark.parametrize(
    "status", [EventStatus.ACTIVE, EventStatus.FINISHED, EventStatus.CANCELLED]
)
def test_cannot_delete_non_pending_event(
    client: TestClient,
    admin_headers: dict[str, str],
    make_event,
    status: EventStatus,
) -> None:
    """只有未开始的活动可以删除，其余状态 409。"""
    target = make_event("非未开始活动", status=status)

    response = client.delete(f"{EVENTS}/{target.id}", headers=admin_headers)

    assert response.status_code == 409
    payload = response.json()
    assert_unified_response(payload, code=409)
    assert payload["message"].startswith("只有未开始的活动可以删除")


def test_cannot_delete_event_with_checkin_records(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """已有签到记录的活动不能删除（409）。"""
    session = session_factory()
    try:
        session.add(
            CheckinRecord(
                event_id=event.id,
                family_id=member.family_id,
                member_id=member.id,
                method=None,
                status=CheckinStatus.ABSENT,
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.delete(f"{EVENTS}/{event.id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["message"] == "该活动已有签到记录，无法删除"


def test_cannot_delete_event_with_leave_records(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """已有请假记录的活动不能删除（409），避免外键冲突与历史数据丢失。"""
    session = session_factory()
    try:
        session.add(
            LeaveRequest(
                event_id=event.id,
                member_id=member.id,
                reason="家中有事",
                status=LeaveStatus.PENDING,
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.delete(f"{EVENTS}/{event.id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["message"] == "该活动已有请假记录，无法删除"


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_delete_event(
    client: TestClient, request: pytest.FixtureRequest, role_fixture: str, event: Event
) -> None:
    """工作人员与家庭用户不能删除活动（403）。"""
    headers = request.getfixturevalue(role_fixture)

    response = client.delete(f"{EVENTS}/{event.id}", headers=headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 五、状态迁移
# ======================================================================
def test_start_and_finish_event(
    client: TestClient, admin_headers: dict[str, str], event: Event, db: Session
) -> None:
    """pending → active → finished 正常迁移。"""
    started = client.put(
        f"{EVENTS}/{event.id}/status", headers=admin_headers, json={"status": "active"}
    )
    assert started.status_code == 200
    assert started.json()["data"]["status"] == EventStatus.ACTIVE.value

    finished = client.put(
        f"{EVENTS}/{event.id}/status", headers=admin_headers, json={"status": "finished"}
    )
    assert finished.status_code == 200
    body = finished.json()
    assert_unified_response(body)
    assert body["data"]["status"] == EventStatus.FINISHED.value

    db.rollback()
    assert db.get(Event, event.id).status == EventStatus.FINISHED


def test_cancel_pending_event(
    client: TestClient, admin_headers: dict[str, str], event: Event
) -> None:
    """pending → cancelled 正常迁移。"""
    response = client.put(
        f"{EVENTS}/{event.id}/status", headers=admin_headers, json={"status": "cancelled"}
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["status"] == EventStatus.CANCELLED.value
    assert body["message"] == "活动已取消"


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (EventStatus.PENDING, EventStatus.PENDING),
        (EventStatus.PENDING, EventStatus.FINISHED),
        (EventStatus.ACTIVE, EventStatus.ACTIVE),
        (EventStatus.ACTIVE, EventStatus.CANCELLED),
        (EventStatus.ACTIVE, EventStatus.PENDING),
        (EventStatus.FINISHED, EventStatus.ACTIVE),
        (EventStatus.CANCELLED, EventStatus.ACTIVE),
    ],
)
def test_illegal_status_transitions_return_conflict(
    client: TestClient,
    admin_headers: dict[str, str],
    make_event,
    initial: EventStatus,
    target: EventStatus,
) -> None:
    """非法状态迁移一律 409。"""
    target_event = make_event("状态迁移测试", status=initial)

    response = client.put(
        f"{EVENTS}/{target_event.id}/status",
        headers=admin_headers,
        json={"status": target.value},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_status_update_validates_enum(
    client: TestClient, admin_headers: dict[str, str], event: Event
) -> None:
    """非法状态取值在参数校验阶段被拒绝（业务码 400）。"""
    response = client.put(
        f"{EVENTS}/{event.id}/status", headers=admin_headers, json={"status": "running"}
    )

    assert response.status_code == 200
    assert_unified_response(response.json(), code=400)


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_transition_status(
    client: TestClient, request: pytest.FixtureRequest, role_fixture: str, event: Event
) -> None:
    """工作人员与家庭用户不能迁移活动状态（403）。"""
    headers = request.getfixturevalue(role_fixture)

    response = client.put(
        f"{EVENTS}/{event.id}/status", headers=headers, json={"status": "active"}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 六、结束联动：生成缺勤 / 请假记录
# ======================================================================
def test_finishing_event_generates_absent_records(
    client: TestClient,
    admin_headers: dict[str, str],
    make_event,
    family: Family,
    member: FamilyMember,
    make_member,
    session_factory: sessionmaker[Session],
) -> None:
    """活动结束：已签到成员保留签到记录，未签到成员生成缺勤记录。"""
    # 活动刚开始（5 分钟前开始、阈值 15 分钟）：现在签到属于"已签到"而不是迟到
    ongoing = make_event(
        "刚开始的活动", start_offset_minutes=-5, end_offset_minutes=120,
        status=EventStatus.ACTIVE,
    )

    # 成员签到（走真实接口）
    checked = client.post(
        "/api/checkins/manual",
        headers=admin_headers,
        json={"event_id": ongoing.id, "member_id": member.id},
    )
    assert checked.status_code == 200
    assert checked.json()["data"]["status"] == CheckinStatus.SIGNED.value

    # 新增一名未签到成员
    absent_member = make_member(family.id, name="未到场成员", phone="13900000011")

    # 新增一名"不需要签到"的成员：不参与缺勤生成
    make_member(family.id, name="不需签到成员", needs_checkin=False)

    response = client.put(
        f"{EVENTS}/{ongoing.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert "自动生成" in body["message"]

    session = session_factory()
    try:
        records = {
            record.member_id: record
            for record in session.query(CheckinRecord)
            .filter(CheckinRecord.event_id == ongoing.id)
            .all()
        }
        # 未签到成员生成缺勤；已签到成员保留签到记录
        assert absent_member.id in records
        assert records[absent_member.id].status == CheckinStatus.ABSENT
        assert records[absent_member.id].method is None
        assert records[absent_member.id].checked_at is None
        assert records[member.id].status == CheckinStatus.SIGNED
        assert records[member.id].checked_at is not None

        # 不需要签到的成员不会被生成记录
        no_checkin = (
            session.query(FamilyMember)
            .filter(FamilyMember.name == "不需签到成员")
            .one()
        )
        assert no_checkin.id not in records
    finally:
        session.close()


def test_finishing_event_marks_approved_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    active_event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """活动结束：已通过请假的成员记为 leave 而不是 absent。"""
    applied = client.post(
        "/api/leaves",
        headers=family_headers,
        json={"event_id": active_event.id, "member_id": member.id, "reason": "外出务工"},
    )
    assert applied.status_code == 200
    leave_id = applied.json()["data"]["id"]

    approved = client.put(
        f"/api/leaves/{leave_id}/approve", headers=admin_headers, json={"status": "approved"}
    )
    assert approved.status_code == 200

    finished = client.put(
        f"{EVENTS}/{active_event.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )
    assert finished.status_code == 200

    session = session_factory()
    try:
        record = (
            session.query(CheckinRecord)
            .filter(
                CheckinRecord.event_id == active_event.id,
                CheckinRecord.member_id == member.id,
            )
            .one()
        )
        assert record.status == CheckinStatus.LEAVE
        assert record.method is None
        assert record.checked_at is None
    finally:
        session.close()


def test_finished_event_rejects_checkin(
    client: TestClient,
    admin_headers: dict[str, str],
    active_event: Event,
    member: FamilyMember,
) -> None:
    """活动结束后不能签到（400「活动已结束」）。"""
    client.put(
        f"{EVENTS}/{active_event.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )

    response = client.post(
        "/api/checkins/manual",
        headers=admin_headers,
        json={"event_id": active_event.id, "member_id": member.id},
    )

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "活动已结束"


def test_inactive_member_is_not_expected(
    client: TestClient,
    admin_headers: dict[str, str],
    active_event: Event,
    family: Family,
    make_member,
    session_factory: sessionmaker[Session],
) -> None:
    """已停用成员不计入应签到人数，也不生成缺勤记录。"""
    stopped = make_member(family.id, name="已停用成员", status=MemberStatus.INACTIVE)

    client.put(
        f"{EVENTS}/{active_event.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )

    session = session_factory()
    try:
        assert (
            session.query(CheckinRecord)
            .filter(
                CheckinRecord.event_id == active_event.id,
                CheckinRecord.member_id == stopped.id,
            )
            .count()
            == 0
        )
    finally:
        session.close()


def test_inactive_family_is_not_expected(
    client: TestClient,
    admin_headers: dict[str, str],
    active_event: Event,
    family: Family,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """已停用家庭的成员不计入应签到人数。"""
    session = session_factory()
    try:
        # 夹具返回的是脱离会话的对象，必须重新加载后再改状态
        stored = session.get(Family, family.id)
        stored.status = FamilyStatus.INACTIVE
        session.commit()
    finally:
        session.close()

    client.put(
        f"{EVENTS}/{active_event.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )

    session = session_factory()
    try:
        assert (
            session.query(CheckinRecord)
            .filter(
                CheckinRecord.event_id == active_event.id,
                CheckinRecord.member_id == member.id,
            )
            .count()
            == 0
        )
    finally:
        session.close()
