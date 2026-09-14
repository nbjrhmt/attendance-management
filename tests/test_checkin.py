"""签到业务模块测试（阶段五）。

覆盖点：

- 权限：人脸/手动签到仅管理员与工作人员（家庭用户 403、未登录 401），
  列表与活动明细所有登录用户可读，家庭用户仅本户（跨户 403），修正记录仅管理员；
- 手动签到：签到窗口（未开始/已结束/已取消）、成员资格（停用成员、停用家庭、无需签到、不存在）、
  迟到判定、幂等（重复签到 409）；
- 人脸签到：用 ``client_face`` + ``baidu_provider`` 离线替身验证真实调用链，
  未识别到成员 400、得分写入 ``face_score``、重复签到 409；
- 列表与明细：筛选、分页、活动签到汇总 summary（应签到人数/各状态计数/出勤率）；
- 修正记录：状态修正、审计字段（reviewed_by_id / reviewed_at）、状态与字段一致性修补；
- 结束联动：缺勤 / 请假记录生成规则。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.checkin.models import CheckinMethod, CheckinRecord, CheckinStatus
from src.event.models import Event, EventStatus
from src.family.models import Family, FamilyStatus
from src.member.models import FamilyMember, MemberStatus
from tests.baidu_mock import MockBaiduFaceApi
from tests.helpers import assert_unified_response

#: 接口前缀
CHECKINS = "/api/checkins"
EVENTS = "/api/events"
FACES = "/api/face"

#: 合法的 PNG 头部字节（内容本身不需要真实可解码，魔数校验即可）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128

#: 另一张"照片"（人脸库中不存在 → 模拟陌生人）
STRANGER_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x22" * 128


def _files(content: bytes, filename: str = "face.png") -> dict:
    """构造 multipart 文件参数。"""
    return {"file": (filename, content, "image/png")}


def _manual(client: TestClient, headers: dict[str, str], event_id: int, member_id: int):
    """调用手动签到接口。"""
    return client.post(
        f"{CHECKINS}/manual",
        headers=headers,
        json={"event_id": event_id, "member_id": member_id},
    )


def _face_checkin(
    client: TestClient, headers: dict[str, str], event_id: int, content: bytes = PNG_BYTES
):
    """调用人脸签到接口。"""
    return client.post(
        f"{CHECKINS}/face",
        headers=headers,
        data={"event_id": str(event_id)},
        files=_files(content),
    )


def _register_face(client: TestClient, headers: dict[str, str], member_id: int, content=PNG_BYTES):
    """通过人脸录入接口把照片写入（离线）人脸库。"""
    return client.post(
        f"{FACES}/register",
        headers=headers,
        data={"member_id": str(member_id)},
        files=_files(content),
    )


def _get_record(
    session_factory: sessionmaker[Session], event_id: int, member_id: int
) -> CheckinRecord | None:
    """直接查库读取签到记录。"""
    session = session_factory()
    try:
        return (
            session.query(CheckinRecord)
            .filter(
                CheckinRecord.event_id == event_id,
                CheckinRecord.member_id == member_id,
            )
            .one_or_none()
        )
    finally:
        session.close()


# ======================================================================
# 一、手动签到：权限
# ======================================================================
@pytest.mark.parametrize("role_fixture", ["admin_headers", "staff_headers"])
def test_manual_checkin_by_admin_and_staff(
    client: TestClient,
    request: pytest.FixtureRequest,
    role_fixture: str,
    event: Event,
    member: FamilyMember,
) -> None:
    """管理员与工作人员均可手动签到。"""
    headers = request.getfixturevalue(role_fixture)

    response = _manual(client, headers, event.id, member.id)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["member_id"] == member.id
    assert data["event_id"] == event.id
    assert data["family_id"] == member.family_id
    assert data["method"] == CheckinMethod.MANUAL.value
    assert data["member_name"] == "李小明"
    assert data["event_name"] == "村民大会"
    assert data["checked_at"] is not None
    assert data["face_score"] is None


def test_family_user_cannot_checkin(
    client: TestClient, family_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """家庭用户不能签到（403）。"""
    response = _manual(client, family_headers, event.id, member.id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_checkin_requires_login(client: TestClient, event: Event, member: FamilyMember) -> None:
    """未登录返回 401。"""
    response = _manual(client, {}, event.id, member.id)

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


# ======================================================================
# 二、签到窗口与迟到判定
# ======================================================================
def test_checkin_before_start_returns_400(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """活动尚未开始（now < start_time）返回 400。"""
    future = make_event("未来活动", start_offset_minutes=30, end_offset_minutes=90)

    response = _manual(client, admin_headers, future.id, member.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "活动尚未开始"


def test_checkin_after_end_returns_400(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """活动已过结束时间（now > end_time）返回 400。"""
    past = make_event("过去活动", start_offset_minutes=-120, end_offset_minutes=-60)

    response = _manual(client, admin_headers, past.id, member.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "活动已结束"


def test_checkin_on_finished_event_returns_400(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """活动状态为 finished 时不能签到（即使仍在时间窗内）。"""
    finished = make_event("已结束活动", status=EventStatus.FINISHED)

    response = _manual(client, admin_headers, finished.id, member.id)

    assert response.status_code == 400
    assert response.json()["message"] == "活动已结束"


def test_checkin_on_cancelled_event_returns_400(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """活动已取消时不能签到。"""
    cancelled = make_event("已取消活动", status=EventStatus.CANCELLED)

    response = _manual(client, admin_headers, cancelled.id, member.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "活动已取消"


def test_checkin_within_threshold_is_signed(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """开始 5 分钟、阈值 15 分钟 → 已签到。"""
    ongoing = make_event("刚开始", start_offset_minutes=-5, end_offset_minutes=60)

    response = _manual(client, admin_headers, ongoing.id, member.id)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == CheckinStatus.SIGNED.value


def test_checkin_beyond_threshold_is_late(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """开始 30 分钟、阈值 15 分钟 → 迟到。"""
    ongoing = make_event("已开始半小时", start_offset_minutes=-30, end_offset_minutes=60)

    response = _manual(client, admin_headers, ongoing.id, member.id)

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["status"] == CheckinStatus.LATE.value
    assert "迟到" in payload["message"]


def test_long_threshold_makes_late_checkin_signed(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """阈值放大到 60 分钟后，同样时间签到属于已签到（阈值确实生效）。"""
    ongoing = make_event(
        "长阈值活动",
        start_offset_minutes=-30,
        end_offset_minutes=60,
        late_threshold_minutes=60,
    )

    response = _manual(client, admin_headers, ongoing.id, member.id)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == CheckinStatus.SIGNED.value


# ======================================================================
# 三、成员校验与幂等
# ======================================================================
def test_checkin_event_not_found(
    client: TestClient, admin_headers: dict[str, str], member: FamilyMember
) -> None:
    """活动不存在返回 404。"""
    response = _manual(client, admin_headers, 999999, member.id)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到活动不存在")


def test_checkin_member_not_found(
    client: TestClient, admin_headers: dict[str, str], event: Event
) -> None:
    """成员不存在返回 404。"""
    response = _manual(client, admin_headers, event.id, 999999)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("家庭成员不存在")


def test_checkin_inactive_member_returns_400(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    family: Family,
    make_member,
) -> None:
    """已停用成员不能签到。"""
    stopped = make_member(family.id, name="停用成员", status=MemberStatus.INACTIVE)

    response = _manual(client, admin_headers, event.id, stopped.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "该成员已停用，无法签到"


def test_checkin_member_not_needing_checkin_returns_400(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    family: Family,
    make_member,
) -> None:
    """不需要签到的成员返回 400「该成员无需签到」。"""
    no_checkin = make_member(family.id, name="无需签到成员", needs_checkin=False)

    response = _manual(client, admin_headers, event.id, no_checkin.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "该成员无需签到"


def test_checkin_member_of_inactive_family_returns_400(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    family: Family,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """家庭已停用时成员不能签到。"""
    session = session_factory()
    try:
        session.get(Family, family.id).status = FamilyStatus.INACTIVE
        session.commit()
    finally:
        session.close()

    response = _manual(client, admin_headers, event.id, member.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "该家庭档案已停用，无法签到"


def test_duplicate_manual_checkin_returns_conflict(
    client: TestClient, admin_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """同一成员同一活动重复签到返回 409。"""
    first = _manual(client, admin_headers, event.id, member.id)
    assert first.status_code == 200

    second = _manual(client, admin_headers, event.id, member.id)

    assert second.status_code == 409
    payload = second.json()
    assert_unified_response(payload, code=409)
    assert payload["message"] == "该成员已在本次活动中签到"


def test_same_member_can_checkin_other_event(
    client: TestClient, admin_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """同一成员可以参加其他活动（幂等约束只针对同一活动）。"""
    first_event = make_event("活动A")
    second_event = make_event("活动B")

    assert _manual(client, admin_headers, first_event.id, member.id).status_code == 200
    assert _manual(client, admin_headers, second_event.id, member.id).status_code == 200


# ======================================================================
# 四、人脸签到（离线替身，真实调用链）
# ======================================================================
def test_face_checkin_success(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """人脸签到：识别到成员后落库，签到方式与得分为人脸识别的结果。"""
    # 活动刚开始（5 分钟前、阈值 15 分钟）：此时签到应为"已签到"
    ongoing = make_event("人脸签到活动", start_offset_minutes=-5, end_offset_minutes=60)
    assert _register_face(client_face, family_headers, member.id).status_code == 200

    response = _face_checkin(client_face, admin_headers, ongoing.id)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["member_id"] == member.id
    assert data["method"] == CheckinMethod.FACE.value
    assert data["face_score"] == baidu_api.search_score
    assert data["status"] == CheckinStatus.SIGNED.value
    assert "签到成功" in body["message"]


def test_face_checkin_stranger_returns_400(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """未识别人脸库中的成员（陌生人）返回 400，且不产生签到记录。"""
    assert _register_face(client_face, family_headers, member.id).status_code == 200

    response = _face_checkin(client_face, admin_headers, event.id, content=STRANGER_BYTES)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "未识别到人脸库中的成员"


def test_face_checkin_duplicate_returns_conflict(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """人脸签到同样受幂等约束。"""
    assert _register_face(client_face, family_headers, member.id).status_code == 200
    assert _face_checkin(client_face, admin_headers, event.id).status_code == 200

    response = _face_checkin(client_face, admin_headers, event.id)

    assert response.status_code == 409
    assert response.json()["message"] == "该成员已在本次活动中签到"


def test_face_checkin_late_status(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
) -> None:
    """人脸签到同样参与迟到判定。"""
    ongoing = make_event("迟到活动", start_offset_minutes=-45, end_offset_minutes=60)
    assert _register_face(client_face, family_headers, member.id).status_code == 200

    response = _face_checkin(client_face, admin_headers, ongoing.id)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == CheckinStatus.LATE.value


def test_face_checkin_after_event_end_returns_400(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
) -> None:
    """活动尚未开始时的窗口校验同样对人脸签到生效（识别成功但窗口不满足）。"""
    future = make_event("未来活动", start_offset_minutes=30, end_offset_minutes=90)
    assert _register_face(client_face, family_headers, member.id).status_code == 200

    response = _face_checkin(client_face, admin_headers, future.id)

    assert response.status_code == 400
    assert response.json()["message"] == "活动尚未开始"


def test_face_checkin_family_user_forbidden(
    client_face: TestClient,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """家庭用户不能调用人脸签到（403）。"""
    assert _register_face(client_face, family_headers, member.id).status_code == 200

    response = _face_checkin(client_face, family_headers, event.id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 五、列表与明细
# ======================================================================
def test_list_checkins_filters_and_permissions(
    client: TestClient,
    admin_headers: dict[str, str],
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    register_householder,
    event: Event,
    member: FamilyMember,
) -> None:
    """列表：管理员/工作人员可见全部，家庭用户仅本户，跨户 403，筛选与分页生效。"""
    other = register_householder("hushu_ck_other", phone="13700000101")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    assert _manual(client, admin_headers, event.id, member.id).status_code == 200
    assert _manual(client, admin_headers, event.id, other_member_id).status_code == 200

    # 管理员 / 工作人员看到全部
    for headers in (admin_headers, staff_headers):
        listed = client.get(CHECKINS, headers=headers, params={"event_id": event.id}).json()
        assert_unified_response(listed)
        assert listed["data"]["total"] == 2

    # 家庭用户只看到本户
    own = client.get(CHECKINS, headers=family_headers, params={"event_id": event.id}).json()
    assert own["code"] == 0
    assert own["data"]["total"] == 1
    assert own["data"]["items"][0]["member_id"] == member.id

    # 家庭用户指定其他家庭 → 403
    cross = client.get(
        CHECKINS, headers=family_headers, params={"family_id": other["user"]["id"] + 1000}
    )
    assert cross.status_code == 403
    assert_unified_response(cross.json(), code=403)

    # 关键字与状态筛选
    by_name = client.get(
        CHECKINS, headers=admin_headers, params={"event_id": event.id, "keyword": "李小明"}
    ).json()
    assert [item["member_name"] for item in by_name["data"]["items"]] == ["李小明"]

    by_status = client.get(
        CHECKINS, headers=admin_headers, params={"event_id": event.id, "status": "absent"}
    ).json()
    assert by_status["data"]["total"] == 0

    # 分页
    paged = client.get(
        CHECKINS, headers=admin_headers, params={"page": 1, "page_size": 1}
    ).json()
    assert paged["data"]["total"] == 2
    assert len(paged["data"]["items"]) == 1


def test_event_checkin_detail_summary(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    family: Family,
    member: FamilyMember,
    make_member,
    make_event,
) -> None:
    """活动签到明细：summary 统计各状态计数与出勤率。"""
    ongoing = make_event("明细活动", start_offset_minutes=-5, end_offset_minutes=120)
    # 应签到成员：户主（测试户主）、李小明、王小明
    make_member(family.id, name="王小明", phone="13900000021")

    assert _manual(client, admin_headers, ongoing.id, member.id).status_code == 200

    detail = client.get(f"{CHECKINS}/events/{ongoing.id}", headers=admin_headers)

    assert detail.status_code == 200
    body = detail.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["event"]["id"] == ongoing.id
    summary = data["summary"]
    assert summary["total_expected"] == 3
    assert summary["signed"] == 1
    assert summary["late"] == 0
    assert summary["absent"] == 0
    assert summary["leave"] == 0
    assert summary["abnormal"] == 0
    assert summary["attendance_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert data["checkins"]["total"] == 1
    assert data["checkins"]["items"][0]["member_id"] == member.id


def test_event_checkin_detail_after_finish(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    family: Family,
    member: FamilyMember,
    make_member,
    make_event,
) -> None:
    """活动结束后的明细：签到 + 请假 + 缺勤齐全，出勤率 = (signed+late)/应签到人数。"""
    ongoing = make_event(
        "结束明细活动",
        start_offset_minutes=-5,
        end_offset_minutes=120,
        status=EventStatus.ACTIVE,
    )
    leave_member = make_member(family.id, name="请假成员", phone="13900000022")
    make_member(family.id, name="缺勤成员", phone="13900000023")

    assert _manual(client, admin_headers, ongoing.id, member.id).status_code == 200

    applied = client.post(
        "/api/leaves",
        headers=family_headers,
        json={"event_id": ongoing.id, "member_id": leave_member.id, "reason": "生病"},
    ).json()
    assert applied["code"] == 0, applied
    approved = client.put(
        f"/api/leaves/{applied['data']['id']}/approve",
        headers=admin_headers,
        json={"status": "approved"},
    )
    assert approved.status_code == 200, approved.text

    finished = client.put(
        f"{EVENTS}/{ongoing.id}/status", headers=admin_headers, json={"status": "finished"}
    )
    assert finished.status_code == 200, finished.text

    summary = client.get(
        f"{CHECKINS}/events/{ongoing.id}", headers=admin_headers
    ).json()["data"]["summary"]

    assert summary["total_expected"] == 4, "户主 + 李小明 + 请假成员 + 缺勤成员"
    assert summary["signed"] == 1
    assert summary["leave"] == 1
    assert summary["absent"] == 2, "户主与缺勤成员均未签到且未请假"
    assert summary["attendance_rate"] == pytest.approx(0.25)


def test_family_user_sees_only_own_family_detail(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    register_householder,
    event: Event,
    member: FamilyMember,
) -> None:
    """家庭用户查看活动明细时只返回本户数据与本户视角汇总。"""
    other = register_householder("hushu_ck_other2", phone="13700000102")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    assert _manual(client, admin_headers, event.id, member.id).status_code == 200
    assert _manual(client, admin_headers, event.id, other_member_id).status_code == 200

    own_view = client.get(f"{CHECKINS}/events/{event.id}", headers=family_headers).json()
    assert own_view["code"] == 0
    assert own_view["data"]["checkins"]["total"] == 1
    assert own_view["data"]["summary"]["total_expected"] == 2, "本户应签到成员：户主 + 李小明"

    admin_view = client.get(f"{CHECKINS}/events/{event.id}", headers=admin_headers).json()
    assert admin_view["data"]["checkins"]["total"] == 2


def test_event_checkin_detail_not_found(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """活动不存在时明细返回 404。"""
    response = client.get(f"{CHECKINS}/events/999999", headers=admin_headers)

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


# ======================================================================
# 六、修正签到记录
# ======================================================================
def test_admin_corrects_absent_to_signed(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """管理员把缺勤修正为已签到：补齐签到时间与签到方式，并记录修正人。"""
    session = session_factory()
    try:
        record = CheckinRecord(
            event_id=event.id,
            family_id=member.family_id,
            member_id=member.id,
            method=None,
            status=CheckinStatus.ABSENT,
            checked_at=None,
        )
        session.add(record)
        session.commit()
        record_id = record.id
    finally:
        session.close()

    response = client.put(
        f"{CHECKINS}/{record_id}",
        headers=admin_headers,
        json={"status": "signed", "remark": "现场补签", "review_remark": "已核实到场"},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["status"] == CheckinStatus.SIGNED.value
    assert data["method"] == CheckinMethod.MANUAL.value
    assert data["checked_at"] is not None
    assert data["remark"] == "现场补签"
    assert data["review_remark"] == "已核实到场"
    assert data["reviewed_by_id"] == admin_user.id
    assert data["reviewed_at"] is not None


def test_admin_corrects_signed_to_absent(
    client: TestClient,
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """管理员把已签到修正为缺勤：清空签到时间与签到方式。"""
    checked = _manual(client, admin_headers, event.id, member.id).json()["data"]

    response = client.put(
        f"{CHECKINS}/{checked['id']}",
        headers=admin_headers,
        json={"status": "absent", "review_remark": "代签，实际未到场"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == CheckinStatus.ABSENT.value
    assert data["checked_at"] is None
    assert data["method"] is None


def test_admin_corrects_to_abnormal(
    client: TestClient, admin_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """可修正为异常状态（异常由人工产生）。"""
    checked = _manual(client, admin_headers, event.id, member.id).json()["data"]

    response = client.put(
        f"{CHECKINS}/{checked['id']}", headers=admin_headers, json={"status": "abnormal"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == CheckinStatus.ABNORMAL.value


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_correct_record(
    client: TestClient,
    request: pytest.FixtureRequest,
    role_fixture: str,
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """工作人员与家庭用户不能修正签到记录（403）。"""
    checked = _manual(client, admin_headers, event.id, member.id).json()["data"]
    headers = request.getfixturevalue(role_fixture)

    response = client.put(
        f"{CHECKINS}/{checked['id']}", headers=headers, json={"status": "absent"}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_correct_missing_record_returns_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """记录不存在返回 404。"""
    response = client.put(
        f"{CHECKINS}/999999", headers=admin_headers, json={"status": "signed"}
    )

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到记录不存在")


def test_correct_invalid_status_rejected(
    client: TestClient, admin_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """非法状态取值被参数校验拦截（业务码 400）。"""
    checked = _manual(client, admin_headers, event.id, member.id).json()["data"]

    response = client.put(
        f"{CHECKINS}/{checked['id']}", headers=admin_headers, json={"status": "unknown"}
    )

    assert response.status_code == 200
    assert_unified_response(response.json(), code=400)


# ======================================================================
# 七、结束联动：缺勤生成幂等性
# ======================================================================
def test_absent_generation_is_idempotent(
    client: TestClient,
    admin_headers: dict[str, str],
    active_event: Event,
    family: Family,
    make_member,
    session_factory: sessionmaker[Session],
) -> None:
    """缺勤生成按"应签到成员 - 已有记录"计算，重复调用不会产生重复记录。"""
    make_member(family.id, name="成员甲", phone="13900000031")
    make_member(family.id, name="成员乙", phone="13900000032")

    client.put(
        f"{EVENTS}/{active_event.id}/status", headers=admin_headers, json={"status": "finished"}
    )

    session = session_factory()
    try:
        first_round = (
            session.query(CheckinRecord)
            .filter(CheckinRecord.event_id == active_event.id)
            .count()
        )
    finally:
        session.close()
    assert first_round == 3, "户主 + 成员甲 + 成员乙均为应签到成员"

    # 再次调用生成逻辑（模拟重复触发）：不会新增记录
    from src.checkin import service as checkin_service

    session = session_factory()
    try:
        event = session.get(Event, active_event.id)
        generated = checkin_service.generate_absent_records(session, event)
        total = (
            session.query(CheckinRecord)
            .filter(CheckinRecord.event_id == active_event.id)
            .count()
        )
    finally:
        session.close()

    assert generated == 0
    assert total == first_round
