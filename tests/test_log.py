"""操作日志模块测试（阶段六）。

覆盖点：

- **埋点**：``record_operation`` 随调用方事务提交（不主动 commit）；
  event / checkin / leave 的关键操作会写入日志，家庭用户自助行为不写日志；
- **接口**：列表筛选（操作人/模块/操作类型/时间范围/关键字）、分页与排序、
  详情、按日期清理、权限矩阵（admin 200 / staff 403 / family 403 / 未登录 401）；
- **回归**：埋点不改变任何接口的响应结构与业务行为（响应断言与阶段五一致）；
- **来源 IP**：接口触发的操作会记录来源 IP。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.checkin.models import CheckinMethod, CheckinStatus
from src.checkin import crud as checkin_crud
from src.event import crud as event_crud
from src.event.models import Event, EventStatus
from src.log import service as log_service
from src.log.models import OperationLog
from src.member.models import FamilyMember
from tests.helpers import assert_unified_response

#: 接口前缀
LOGS = "/api/logs"
EVENTS = "/api/events"
CHECKINS = "/api/checkins"
LEAVES = "/api/leaves"

#: 测试用照片（仅魔数校验通过即可）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128


def _rows(
    session_factory: sessionmaker[Session],
    *,
    module: str | None = None,
    action: str | None = None,
) -> list[dict]:
    """直接查库读取日志（返回普通字典，避免脱离会话的 ORM 对象）。"""
    session = session_factory()
    try:
        query = session.query(OperationLog)
        if module:
            query = query.filter(OperationLog.module == module)
        if action:
            query = query.filter(OperationLog.action == action)
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "username": row.username,
                "module": row.module,
                "action": row.action,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "target": row.target,
                "detail": row.detail,
                "ip": row.ip,
                "created_at": row.created_at,
            }
            for row in query.order_by(OperationLog.id).all()
        ]
    finally:
        session.close()


def _count(session_factory: sessionmaker[Session]) -> int:
    """日志总条数。"""
    session = session_factory()
    try:
        return int(session.query(OperationLog).count())
    finally:
        session.close()


# ======================================================================
# 一、record_operation：事务语义
# ======================================================================
def test_record_operation_persists_with_caller_commit(
    session_factory: sessionmaker[Session], admin_user
) -> None:
    """调用方 commit 后日志落库，字段与参数一致。"""
    session = session_factory()
    try:
        log_service.record_operation(
            session,
            admin_user,
            module="event",
            action="create",
            target_type="event",
            target_id=7,
            detail="创建活动：村晚联欢",
            ip="10.0.0.1",
        )
        session.commit()
    finally:
        session.close()

    rows = _rows(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == admin_user.id
    assert row["username"] == admin_user.username
    assert row["module"] == "event"
    assert row["action"] == "create"
    assert row["target_type"] == "event"
    assert row["target_id"] == 7
    assert row["target"] == "event/7"
    assert row["detail"] == "创建活动：村晚联欢"
    assert row["ip"] == "10.0.0.1"


def test_record_operation_does_not_commit(
    session_factory: sessionmaker[Session], admin_user
) -> None:
    """record_operation 不单独提交：调用方回滚后日志不落库。"""
    session = session_factory()
    try:
        log_service.record_operation(
            session, admin_user, module="event", action="create", detail="临时日志"
        )
        session.rollback()
    finally:
        session.close()

    assert _count(session_factory) == 0


def test_record_operation_allows_missing_user(
    session_factory: sessionmaker[Session]
) -> None:
    """操作人可为空（系统操作），此时用户名记为 system。"""
    session = session_factory()
    try:
        log_service.record_operation(
            session, None, module="system", action="clean", detail="系统清理"
        )
        session.commit()
    finally:
        session.close()

    row = _rows(session_factory)[0]
    assert row["user_id"] is None
    assert row["username"] == "system"


def test_record_operation_truncates_long_detail(
    session_factory: sessionmaker[Session], admin_user
) -> None:
    """超长摘要按列长度截断（500 字符），不会写入失败。"""
    session = session_factory()
    try:
        log_service.record_operation(
            session, admin_user, module="event", action="create", detail="活" * 900
        )
        session.commit()
    finally:
        session.close()

    assert len(_rows(session_factory)[0]["detail"]) == 500


# ======================================================================
# 二、接口权限矩阵
# ======================================================================
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", LOGS, {}),
        ("GET", f"{LOGS}/1", {}),
        ("DELETE", f"{LOGS}/clean", {"params": {"before": "2026-01-01"}}),
    ],
)
def test_logs_require_admin(
    client: TestClient,
    admin_headers: dict[str, str],
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    method: str,
    path: str,
    kwargs: dict,
) -> None:
    """日志接口：管理员可访问，工作人员与家庭用户 403，未登录 401。"""
    allowed = client.request(method, path, headers=admin_headers, **kwargs)
    assert allowed.status_code in (200, 404), allowed.text
    assert_unified_response(
        allowed.json(), code=0 if allowed.status_code == 200 else 404
    )

    for headers in (staff_headers, family_headers):
        forbidden = client.request(method, path, headers=headers, **kwargs)
        assert forbidden.status_code == 403, forbidden.text
        assert_unified_response(forbidden.json(), code=403)

    anonymous = client.request(method, path, **kwargs)
    assert anonymous.status_code == 401
    assert_unified_response(anonymous.json(), code=401)


# ======================================================================
# 三、列表筛选与分页
# ======================================================================
def test_list_logs_filters(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    staff_user,
    session_factory: sessionmaker[Session],
    event: Event,
    member: FamilyMember,
) -> None:
    """列表支持按模块、操作类型、操作人与关键字筛选。"""
    # 通过真实接口产生日志：创建活动（admin）+ 手动签到（staff）
    created = client.post(
        EVENTS,
        headers=admin_headers,
        json={
            "name": "筛选测试活动",
            "start_time": datetime.now().isoformat(),
            "end_time": (datetime.now() + timedelta(hours=1)).isoformat(),
        },
    )
    assert created.status_code == 200
    checked = client.post(
        f"{CHECKINS}/manual",
        headers=admin_headers,
        json={"event_id": event.id, "member_id": member.id},
    )
    assert checked.status_code == 200

    all_rows = client.get(LOGS, headers=admin_headers).json()
    assert_unified_response(all_rows)
    assert all_rows["data"]["total"] >= 2

    by_module = client.get(LOGS, headers=admin_headers, params={"module": "event"}).json()
    assert by_module["data"]["total"] == 1
    assert by_module["data"]["items"][0]["action"] == "create"

    by_action = client.get(
        LOGS, headers=admin_headers, params={"action": "manual_checkin"}
    ).json()
    assert by_action["data"]["total"] == 1
    assert by_action["data"]["items"][0]["module"] == "checkin"
    assert by_action["data"]["items"][0]["detail"].startswith("手动签到：李小明")

    by_user = client.get(
        LOGS, headers=admin_headers, params={"user_id": admin_user.id}
    ).json()
    assert by_user["data"]["total"] == 2
    assert {item["username"] for item in by_user["data"]["items"]} == {
        admin_user.username
    }

    by_keyword = client.get(
        LOGS, headers=admin_headers, params={"keyword": "筛选测试活动"}
    ).json()
    assert by_keyword["data"]["total"] == 1
    assert by_keyword["data"]["items"][0]["target"] == f"event/{created.json()['data']['id']}"

    # 关键字 LIKE 通配符已转义：`%` 只作为普通字符匹配，不会命中全部日志
    escaped = client.get(LOGS, headers=admin_headers, params={"keyword": "%"}).json()
    assert escaped["data"]["total"] == 0

    no_match = client.get(
        LOGS, headers=admin_headers, params={"module": "leave", "action": "approve"}
    ).json()
    assert no_match["data"]["total"] == 0


def test_list_logs_time_range_and_pagination(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    session_factory: sessionmaker[Session],
) -> None:
    """时间范围筛选与分页（按时间倒序）生效。"""
    session = session_factory()
    try:
        for index in range(3):
            log_service.record_operation(
                session,
                admin_user,
                module="event",
                action="create",
                target_type="event",
                target_id=index + 1,
                detail=f"第 {index + 1} 条",
                ip="127.0.0.1",
            )
        session.commit()

        # 造一条"一年前"的历史日志用于时间范围筛选
        session.query(OperationLog).filter(OperationLog.target_id == 3).update(
            {OperationLog.created_at: datetime.now() - timedelta(days=365)}
        )
        session.commit()
    finally:
        session.close()

    start = (datetime.now() - timedelta(days=1)).isoformat()
    end = (datetime.now() + timedelta(minutes=1)).isoformat()
    recent = client.get(
        LOGS, headers=admin_headers, params={"start_time": start, "end_time": end}
    ).json()
    assert recent["data"]["total"] == 2

    old = client.get(
        LOGS,
        headers=admin_headers,
        params={"end_time": (datetime.now() - timedelta(days=30)).isoformat()},
    ).json()
    assert old["data"]["total"] == 1
    assert old["data"]["items"][0]["detail"] == "第 3 条"

    first_page = client.get(
        LOGS, headers=admin_headers, params={"page": 1, "page_size": 2}
    ).json()
    second_page = client.get(
        LOGS, headers=admin_headers, params={"page": 2, "page_size": 2}
    ).json()
    assert first_page["data"]["total"] == 3
    assert len(first_page["data"]["items"]) == 2
    assert len(second_page["data"]["items"]) == 1
    # 倒序：最新的（target_id=2）在前
    assert first_page["data"]["items"][0]["target"] == "event/2"


def test_get_log_detail(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    session_factory: sessionmaker[Session],
) -> None:
    """详情返回完整字段；不存在返回 404。"""
    session = session_factory()
    try:
        log = log_service.record_operation(
            session,
            admin_user,
            module="leave",
            action="approve",
            target_type="leave",
            target_id=9,
            detail="审批请假：李小明 → 已通过",
            ip="192.168.1.9",
        )
        session.commit()
        log_id = log.id
    finally:
        session.close()

    response = client.get(f"{LOGS}/{log_id}", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["id"] == log_id
    assert data["module"] == "leave"
    assert data["action"] == "approve"
    assert data["target"] == "leave/9"
    assert data["ip"] == "192.168.1.9"
    assert data["username"] == admin_user.username

    missing = client.get(f"{LOGS}/999999", headers=admin_headers)
    assert missing.status_code == 404
    payload = missing.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("操作日志不存在")


# ======================================================================
# 四、按日期清理
# ======================================================================
def test_clean_logs_by_date(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    session_factory: sessionmaker[Session],
) -> None:
    """DELETE /clean 精确删除 before 之前的日志并返回条数。"""
    today = date.today()
    session = session_factory()
    try:
        fresh = log_service.record_operation(
            session, admin_user, module="event", action="create", detail="今天的日志"
        )
        session.commit()
        stale_ids = []
        for days in (1, 10, 100):
            stale = log_service.record_operation(
                session,
                admin_user,
                module="event",
                action="create",
                detail=f"{days} 天前的日志",
            )
            session.commit()
            session.query(OperationLog).filter(OperationLog.id == stale.id).update(
                {OperationLog.created_at: datetime.now() - timedelta(days=days)}
            )
            session.commit()
            stale_ids.append(stale.id)
        fresh_id = fresh.id
    finally:
        session.close()

    # 清理 today-30 之前的日志：只应删除"100 天前"那一条
    response = client.request(
        "DELETE",
        f"{LOGS}/clean",
        headers=admin_headers,
        params={"before": (today - timedelta(days=30)).isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["deleted"] == 1
    assert body["data"]["before"] == (today - timedelta(days=30)).isoformat()

    remaining = {row["id"] for row in _rows(session_factory)}
    assert remaining == {fresh_id, stale_ids[0], stale_ids[1]}

    # 清理今天之前的日志：1 天前与 10 天前的两条被删除，今天的保留
    second = client.request(
        "DELETE",
        f"{LOGS}/clean",
        headers=admin_headers,
        params={"before": today.isoformat()},
    ).json()
    assert second["data"]["deleted"] == 2
    assert {row["id"] for row in _rows(session_factory)} == {fresh_id}


def test_clean_logs_requires_before(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """before 为必填参数，缺失时返回参数校验错误（业务码 400）。"""
    response = client.delete(f"{LOGS}/clean", headers=admin_headers)

    assert response.status_code == 200
    assert_unified_response(response.json(), code=400)


# ======================================================================
# 五、埋点：event / checkin / leave
# ======================================================================
def test_event_operations_are_logged(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    session_factory: sessionmaker[Session],
) -> None:
    """活动创建/修改/删除/状态迁移均写入模块 event 的日志。"""
    payload = {
        "name": "埋点测试活动",
        "location": "文化广场",
        "start_time": datetime.now().isoformat(),
        "end_time": (datetime.now() + timedelta(hours=2)).isoformat(),
        "late_threshold_minutes": 10,
    }
    created = client.post(EVENTS, headers=admin_headers, json=payload)
    assert created.status_code == 200
    assert_unified_response(created.json())
    event_id = created.json()["data"]["id"]

    updated = client.put(
        f"{EVENTS}/{event_id}", headers=admin_headers, json={"name": "埋点测试活动（改）"}
    )
    assert updated.status_code == 200
    assert_unified_response(updated.json())

    started = client.put(
        f"{EVENTS}/{event_id}/status", headers=admin_headers, json={"status": "active"}
    )
    assert started.status_code == 200
    assert_unified_response(started.json())

    finished = client.put(
        f"{EVENTS}/{event_id}/status", headers=admin_headers, json={"status": "finished"}
    )
    assert finished.status_code == 200
    finished_body = finished.json()
    assert_unified_response(finished_body)
    assert finished_body["data"]["status"] == EventStatus.FINISHED.value

    rows = _rows(session_factory, module="event")
    assert [row["action"] for row in rows] == [
        "create",
        "update",
        "change_status",
        "change_status",
    ]
    assert rows[0]["username"] == admin_user.username
    assert rows[0]["target"] == f"event/{event_id}"
    assert "埋点测试活动" in rows[0]["detail"]
    assert rows[1]["detail"].startswith("修改活动：埋点测试活动（改）")
    assert rows[2]["detail"] == "状态：pending→active"
    assert rows[3]["detail"].startswith("状态：active→finished")
    assert all(row["ip"] for row in rows), "接口触发的操作应记录来源 IP"


def test_event_delete_is_logged(
    client: TestClient,
    admin_headers: dict[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    """删除活动写入 action=delete 的日志（对象ID在被删后仍可追溯）。"""
    created = client.post(
        EVENTS,
        headers=admin_headers,
        json={
            "name": "待删除活动",
            "start_time": datetime.now().isoformat(),
            "end_time": (datetime.now() + timedelta(hours=1)).isoformat(),
        },
    ).json()
    event_id = created["data"]["id"]

    deleted = client.delete(f"{EVENTS}/{event_id}", headers=admin_headers)
    assert deleted.status_code == 200
    assert_unified_response(deleted.json())

    rows = _rows(session_factory, module="event", action="delete")
    assert len(rows) == 1
    assert rows[0]["target"] == f"event/{event_id}"
    assert "待删除活动" in rows[0]["detail"]


def test_checkin_operations_are_logged(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """手动签到、人脸签到、人工修正均写入模块 checkin 的日志。"""
    # 活动刚开始（5 分钟前、阈值 15 分钟）→ 签到状态确定为"已签到"
    ongoing = make_event(
        "签到埋点活动", start_offset_minutes=-5, end_offset_minutes=60
    )
    manual = client_face.post(
        f"{CHECKINS}/manual",
        headers=admin_headers,
        json={"event_id": ongoing.id, "member_id": member.id},
    )
    assert manual.status_code == 200
    manual_data = manual.json()["data"]
    assert manual_data["method"] == CheckinMethod.MANUAL.value
    assert manual_data["status"] == CheckinStatus.SIGNED.value

    corrected = client_face.put(
        f"{CHECKINS}/{manual_data['id']}",
        headers=admin_headers,
        json={"status": "abnormal", "review_remark": "照片无法辨认"},
    )
    assert corrected.status_code == 200
    assert_unified_response(corrected.json())

    rows = _rows(session_factory, module="checkin")
    assert [row["action"] for row in rows] == ["manual_checkin", "correct"]
    assert rows[0]["target"] == f"checkin/{manual_data['id']}"
    assert rows[0]["detail"] == f"手动签到：{member.name}（活动{ongoing.id}，已签到）"
    assert rows[1]["detail"].endswith("→ 异常")


def test_face_checkin_is_logged(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """人脸签到写入 action=face_checkin 的日志（含成员名与活动ID）。"""
    ongoing = make_event("人脸签到活动", start_offset_minutes=-5, end_offset_minutes=60)
    assert client_face.post(
        "/api/face/register",
        headers=family_headers,
        data={"member_id": str(member.id)},
        files={"file": ("face.png", PNG_BYTES, "image/png")},
    ).status_code == 200

    response = client_face.post(
        f"{CHECKINS}/face",
        headers=admin_headers,
        data={"event_id": str(ongoing.id)},
        files={"file": ("face.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 200, response.text
    record_id = response.json()["data"]["id"]

    rows = _rows(session_factory, module="checkin", action="face_checkin")
    assert len(rows) == 1
    assert rows[0]["target"] == f"checkin/{record_id}"
    assert rows[0]["detail"] == f"人脸签到：{member.name}（活动{ongoing.id}，已签到）"


def test_leave_review_is_logged_but_self_service_is_not(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """请假审批写日志（approve/reject），户主提交与撤销不写日志。"""
    applied = client.post(
        f"{LEAVES}",
        headers=family_headers,
        json={"event_id": event.id, "member_id": member.id, "reason": "外出务工"},
    ).json()
    assert applied["code"] == 0
    leave_id = applied["data"]["id"]

    rejected = client.put(
        f"{LEAVES}/{leave_id}/approve",
        headers=admin_headers,
        json={"status": "rejected", "remark": "材料不全"},
    )
    assert rejected.status_code == 200
    assert_unified_response(rejected.json())

    # 重新提交后撤销：仍不应产生日志
    reapplied = client.post(
        f"{LEAVES}",
        headers=family_headers,
        json={"event_id": event.id, "member_id": member.id, "reason": "重新申请"},
    ).json()
    assert reapplied["code"] == 0
    cancelled = client.put(
        f"{LEAVES}/{reapplied['data']['id']}/cancel", headers=family_headers
    )
    assert cancelled.status_code == 200

    rows = _rows(session_factory, module="leave")
    assert len(rows) == 1, "只有审批动作应被审计"
    assert rows[0]["action"] == "reject"
    assert rows[0]["target"] == f"leave/{leave_id}"
    assert rows[0]["detail"].endswith("→ 已驳回")

    # 通过审批（重新提交后再审批）记录 approve
    third = client.post(
        f"{LEAVES}",
        headers=family_headers,
        json={"event_id": event.id, "member_id": member.id, "reason": "再次申请"},
    ).json()
    approved = client.put(
        f"{LEAVES}/{third['data']['id']}/approve",
        headers=admin_headers,
        json={"status": "approved"},
    )
    assert approved.status_code == 200

    rows = _rows(session_factory, module="leave")
    assert [row["action"] for row in rows] == ["reject", "approve"]


def test_absent_generation_is_recorded_in_status_detail(
    client: TestClient,
    admin_headers: dict[str, str],
    active_event: Event,
    family,
    make_member,
    session_factory: sessionmaker[Session],
) -> None:
    """活动结束时的缺勤生成体现在状态迁移日志的摘要中。"""
    make_member(family.id, name="缺勤成员甲", phone="13900000051")

    finished = client.put(
        f"{EVENTS}/{active_event.id}/status",
        headers=admin_headers,
        json={"status": "finished"},
    )
    assert finished.status_code == 200

    rows = _rows(session_factory, module="event", action="change_status")
    assert len(rows) == 1
    assert "状态：active→finished" in rows[0]["detail"]
    assert "自动生成" in rows[0]["detail"]


# ======================================================================
# 六、埋点不改变业务响应
# ======================================================================
def test_instrumentation_keeps_response_shape(
    client: TestClient,
    admin_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """埋点只追加日志行：响应结构、字段与业务语义保持阶段五的表现。"""
    ongoing = make_event(
        "结构校验签到活动", start_offset_minutes=-5, end_offset_minutes=60
    )
    response = client.post(
        f"{CHECKINS}/manual",
        headers=admin_headers,
        json={"event_id": ongoing.id, "member_id": member.id},
    )
    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["message"] == f"{member.name} 签到成功（已签到）"
    data = body["data"]
    assert set(data) == {
        "id",
        "event_id",
        "event_name",
        "family_id",
        "member_id",
        "member_name",
        "method",
        "status",
        "checked_at",
        "face_score",
        "reviewed_by_id",
        "reviewed_at",
        "review_remark",
        "remark",
        "created_at",
        "updated_at",
    }
    assert data["method"] == "manual"
    assert data["face_score"] is None

    # 签到记录确实落库，且与响应一致
    session = session_factory()
    try:
        record = checkin_crud.get_record_by_member(session, ongoing.id, member.id)
        assert record is not None
        assert record.id == data["id"]
        assert record.status == CheckinStatus.SIGNED
    finally:
        session.close()

    # 活动创建/状态迁移的响应结构同样不变
    created = client.post(
        EVENTS,
        headers=admin_headers,
        json={
            "name": "结构校验活动",
            "start_time": datetime.now().isoformat(),
            "end_time": (datetime.now() + timedelta(hours=1)).isoformat(),
        },
    )
    assert created.status_code == 200
    created_body = created.json()
    assert_unified_response(created_body)
    assert set(created_body["data"]) == {
        "id",
        "name",
        "description",
        "location",
        "start_time",
        "end_time",
        "late_threshold_minutes",
        "status",
        "created_at",
        "updated_at",
    }
    session = session_factory()
    try:
        assert event_crud.get_event_by_id(session, created_body["data"]["id"]) is not None
    finally:
        session.close()
