"""请假管理模块测试（阶段五）。

覆盖点：

- 提交：户主为本户成员、管理员为任意成员；工作人员与跨户家庭用户 403；未登录 401；
- 校验：活动/成员不存在 404，已结束或已取消的活动 400，停用成员/家庭与无需签到 400；
- 重复：已有待审批或已通过的请假 409，被驳回/已撤销后可重新提交；
- 列表与详情：管理员/工作人员可见全部，家庭用户仅本户（跨户 403），筛选与分页；
- 审批：通过/驳回、审批人审计字段、重复审批 409、非法审批结果 400、非管理员 403；
- 撤销：户主本人与管理员可撤销，非待审批状态 409，他人家庭 403；
- 与签到联动：已通过请假的成员若实际到场签到，以实际签到为准。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from src.checkin.models import CheckinRecord, CheckinStatus
from src.event.models import Event, EventStatus
from src.family.models import Family, FamilyStatus
from src.leave.models import LeaveRequest, LeaveStatus
from src.member.models import FamilyMember, MemberStatus
from tests.helpers import assert_unified_response

#: 接口前缀
LEAVES = "/api/leaves"
EVENTS = "/api/events"
CHECKINS = "/api/checkins"


def _apply(
    client: TestClient,
    headers: dict[str, str],
    event_id: int,
    member_id: int,
    reason: str = "家中有事",
):
    """提交请假申请。"""
    return client.post(
        f"{LEAVES}",
        headers=headers,
        json={"event_id": event_id, "member_id": member_id, "reason": reason},
    )


def _approve(
    client: TestClient, headers: dict[str, str], leave_id: int, status: str = "approved", **extra
):
    """审批请假申请。"""
    return client.put(
        f"{LEAVES}/{leave_id}/approve", headers=headers, json={"status": status, **extra}
    )


def _cancel(client: TestClient, headers: dict[str, str], leave_id: int):
    """撤销请假申请。"""
    return client.put(f"{LEAVES}/{leave_id}/cancel", headers=headers)


# ======================================================================
# 一、提交请假
# ======================================================================
def test_owner_applies_leave(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """户主为本户成员提交请假：默认待审批，并带出成员与活动信息。"""
    response = _apply(client, family_headers, event.id, member.id, "外出务工")

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["status"] == LeaveStatus.PENDING.value
    assert data["member_id"] == member.id
    assert data["member_name"] == "李小明"
    assert data["event_id"] == event.id
    assert data["event_name"] == "村民大会"
    assert data["family_id"] == member.family_id
    assert data["reason"] == "外出务工"
    assert data["reviewed_by_id"] is None
    assert data["reviewed_at"] is None


def test_admin_applies_leave_for_any_member(
    client: TestClient,
    admin_headers: dict[str, str],
    register_householder,
    event: Event,
) -> None:
    """管理员可为任意家庭的成员提交请假。"""
    other = register_householder("hushu_lv_admin", phone="13700000201")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    response = _apply(client, admin_headers, event.id, other_member_id, "住院")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == LeaveStatus.PENDING.value


def test_staff_cannot_apply_leave(
    client: TestClient,
    staff_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """工作人员不能提交请假（403）。"""
    response = _apply(client, staff_headers, event.id, member.id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_cannot_apply_leave_for_other_family_member(
    client: TestClient,
    family_headers: dict[str, str],
    register_householder,
    event: Event,
) -> None:
    """户主不能为其他家庭的成员请假（403）。"""
    other = register_householder("hushu_lv_other", phone="13700000202")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    response = _apply(client, family_headers, event.id, other_member_id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_apply_leave_requires_login(
    client: TestClient, event: Event, member: FamilyMember
) -> None:
    """未登录提交请假返回 401。"""
    response = _apply(client, {}, event.id, member.id)

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_apply_leave_event_not_found(
    client: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """活动不存在返回 404。"""
    response = _apply(client, family_headers, 999999, member.id)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("签到活动不存在")


def test_apply_leave_member_not_found(
    client: TestClient, family_headers: dict[str, str], event: Event
) -> None:
    """成员不存在返回 404。"""
    response = _apply(client, family_headers, event.id, 999999)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("家庭成员不存在")


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (EventStatus.FINISHED, "活动已结束"),
        (EventStatus.CANCELLED, "活动已取消"),
    ],
)
def test_cannot_apply_leave_for_closed_event(
    client: TestClient,
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    status: EventStatus,
    message: str,
) -> None:
    """已结束 / 已取消的活动不接受请假申请（400）。"""
    closed = make_event("已关闭活动", status=status)

    response = _apply(client, family_headers, closed.id, member.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == message


def test_cannot_apply_leave_for_member_without_checkin(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    family: Family,
    make_member,
) -> None:
    """不需要签到的成员不能请假（400「该成员无需签到」）。"""
    no_checkin = make_member(family.id, name="无需签到成员", needs_checkin=False)

    response = _apply(client, family_headers, event.id, no_checkin.id)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "该成员无需签到"


def test_cannot_apply_leave_for_inactive_member(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    family: Family,
    make_member,
) -> None:
    """已停用成员不能请假（400）。"""
    stopped = make_member(family.id, name="停用成员", status=MemberStatus.INACTIVE)

    response = _apply(client, family_headers, event.id, stopped.id)

    assert response.status_code == 400
    assert response.json()["message"] == "该成员已停用，无法请假"


def test_cannot_apply_leave_for_inactive_family(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    family: Family,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """家庭已停用时不能请假（400）。"""
    session = session_factory()
    try:
        session.get(Family, family.id).status = FamilyStatus.INACTIVE
        session.commit()
    finally:
        session.close()

    response = _apply(client, family_headers, event.id, member.id)

    assert response.status_code == 400
    assert response.json()["message"] == "该家庭档案已停用，无法请假"


def test_apply_leave_validation_errors(
    client: TestClient, family_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """参数校验：缺少事由、事由为空、存在未声明字段。"""
    bodies = [
        {"event_id": event.id, "member_id": member.id},
        {"event_id": event.id, "member_id": member.id, "reason": ""},
        {"event_id": event.id, "member_id": member.id, "reason": "有事", "extra": 1},
    ]

    for body in bodies:
        response = client.post(LEAVES, headers=family_headers, json=body)
        assert response.status_code == 200
        assert_unified_response(response.json(), code=400)


# ======================================================================
# 二、重复请假
# ======================================================================
def test_duplicate_pending_leave_returns_conflict(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """已有待审批的请假时重复提交返回 409。"""
    assert _apply(client, family_headers, event.id, member.id).status_code == 200

    response = _apply(client, family_headers, event.id, member.id, "再次请假")

    assert response.status_code == 409
    payload = response.json()
    assert_unified_response(payload, code=409)
    assert payload["message"] == "该成员已有待审批或已通过的请假"


def test_duplicate_approved_leave_returns_conflict(
    client: TestClient,
    family_headers: dict[str, str],
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """已有已通过的请假时重复提交返回 409。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _approve(client, admin_headers, leave_id).status_code == 200

    response = _apply(client, family_headers, event.id, member.id, "再次请假")

    assert response.status_code == 409
    assert response.json()["message"] == "该成员已有待审批或已通过的请假"


def test_can_reapply_after_rejection(
    client: TestClient,
    family_headers: dict[str, str],
    admin_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """被驳回后可以重新提交，旧记录保留为历史。"""
    first_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _approve(client, admin_headers, first_id, "rejected").status_code == 200

    response = _apply(client, family_headers, event.id, member.id, "重新申请")

    assert response.status_code == 200
    assert response.json()["data"]["id"] != first_id

    session = session_factory()
    try:
        statuses = [
            leave.status
            for leave in session.query(LeaveRequest)
            .filter(LeaveRequest.event_id == event.id, LeaveRequest.member_id == member.id)
            .order_by(LeaveRequest.id)
            .all()
        ]
    finally:
        session.close()
    assert statuses == [LeaveStatus.REJECTED, LeaveStatus.PENDING]


def test_can_reapply_after_cancel(
    client: TestClient,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """撤销后可以重新提交。"""
    first_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _cancel(client, family_headers, first_id).status_code == 200

    response = _apply(client, family_headers, event.id, member.id, "重新申请")

    assert response.status_code == 200


def test_same_member_can_apply_leave_for_other_event(
    client: TestClient, family_headers: dict[str, str], make_event, member: FamilyMember
) -> None:
    """同一成员可为不同活动分别请假。"""
    first_event = make_event("活动A")
    second_event = make_event("活动B")

    assert _apply(client, family_headers, first_event.id, member.id).status_code == 200
    assert _apply(client, family_headers, second_event.id, member.id).status_code == 200


# ======================================================================
# 三、列表与详情
# ======================================================================
def test_list_leaves_scoped_by_role(
    client: TestClient,
    admin_headers: dict[str, str],
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    register_householder,
    event: Event,
    member: FamilyMember,
) -> None:
    """列表：管理员/工作人员可见全部，家庭用户仅本户，跨户 403。"""
    other = register_householder("hushu_lv_other2", phone="13700000203")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]
    assert _apply(client, other["headers"], event.id, other_member_id).status_code == 200
    assert _apply(client, family_headers, event.id, member.id).status_code == 200

    for headers in (admin_headers, staff_headers):
        listed = client.get(LEAVES, headers=headers, params={"event_id": event.id}).json()
        assert_unified_response(listed)
        assert listed["data"]["total"] == 2

    own = client.get(LEAVES, headers=family_headers, params={"event_id": event.id}).json()
    assert own["code"] == 0
    assert own["data"]["total"] == 1
    assert own["data"]["items"][0]["member_id"] == member.id
    assert own["data"]["items"][0]["household_no"] == "F000001"

    cross = client.get(LEAVES, headers=family_headers, params={"family_id": 999999})
    assert cross.status_code == 403
    assert_unified_response(cross.json(), code=403)


def test_list_leaves_filters_and_pagination(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    family: Family,
    make_member,
    make_event,
) -> None:
    """状态、活动与家庭筛选以及分页参数生效。"""
    member_a = make_member(family.id, name="成员甲", phone="13900000041")
    member_b = make_member(family.id, name="成员乙", phone="13900000042")

    leave_a = _apply(client, family_headers, event.id, member_a.id).json()["data"]["id"]
    _apply(client, family_headers, event.id, member_b.id, "有事")
    other_event = make_event("另一个活动")
    _apply(client, family_headers, other_event.id, member_a.id, "另一个活动的请假")

    by_event = client.get(LEAVES, headers=admin_headers, params={"event_id": event.id}).json()
    assert by_event["data"]["total"] == 2

    assert _approve(client, admin_headers, leave_a).status_code == 200
    by_status = client.get(
        LEAVES, headers=admin_headers, params={"status": "approved"}
    ).json()
    assert by_status["data"]["total"] == 1
    assert by_status["data"]["items"][0]["id"] == leave_a

    by_family = client.get(
        LEAVES, headers=admin_headers, params={"family_id": family.id}
    ).json()
    assert by_family["data"]["total"] == 3, "本户三个活动请假记录（含另一活动）"

    by_event_and_family = client.get(
        LEAVES,
        headers=admin_headers,
        params={"event_id": event.id, "family_id": family.id},
    ).json()
    assert by_event_and_family["data"]["total"] == 2

    paged = client.get(
        LEAVES, headers=admin_headers, params={"page": 2, "page_size": 2}
    ).json()
    assert paged["data"]["total"] == 3
    assert len(paged["data"]["items"]) == 1


def test_get_leave_detail_permissions(
    client: TestClient,
    admin_headers: dict[str, str],
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    register_householder,
    event: Event,
    member: FamilyMember,
) -> None:
    """详情：本户户主、管理员与工作人员可看，其他家庭 403。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    other = register_householder("hushu_lv_other3", phone="13700000204")

    for headers in (family_headers, admin_headers, staff_headers):
        detail = client.get(f"{LEAVES}/{leave_id}", headers=headers)
        assert detail.status_code == 200, headers
        body = detail.json()
        assert_unified_response(body)
        assert body["data"]["id"] == leave_id
        assert body["data"]["member_name"] == "李小明"

    forbidden = client.get(f"{LEAVES}/{leave_id}", headers=other["headers"])
    assert forbidden.status_code == 403
    assert_unified_response(forbidden.json(), code=403)


def test_get_leave_detail_not_found(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """详情不存在返回 404。"""
    response = client.get(f"{LEAVES}/999999", headers=admin_headers)

    assert response.status_code == 404
    payload = response.json()
    assert_unified_response(payload, code=404)
    assert payload["message"].startswith("请假申请不存在")


# ======================================================================
# 四、审批
# ======================================================================
@pytest.mark.parametrize("result", ["approved", "rejected"])
def test_admin_reviews_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    admin_user,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    result: str,
) -> None:
    """管理员通过 / 驳回请假，并记录审批人与审批时间。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]

    response = _approve(client, admin_headers, leave_id, result, remark="已核实")

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["status"] == result
    assert data["reviewed_by_id"] == admin_user.id
    assert data["reviewed_at"] is not None
    assert data["review_remark"] == "已核实"


def test_review_twice_returns_conflict(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """已审批的请假不能重复审批（409）。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _approve(client, admin_headers, leave_id).status_code == 200

    response = _approve(client, admin_headers, leave_id, "rejected")

    assert response.status_code == 409
    payload = response.json()
    assert_unified_response(payload, code=409)
    assert payload["message"] == "该请假申请已审批，无法重复审批"


def test_cannot_review_cancelled_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """已撤销的请假不能审批（409）。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _cancel(client, family_headers, leave_id).status_code == 200

    response = _approve(client, admin_headers, leave_id)

    assert response.status_code == 409
    assert response.json()["message"] == "该请假申请已撤销，无法审批"


@pytest.mark.parametrize("bad_status", ["cancelled", "pending"])
def test_review_rejects_invalid_result(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    bad_status: str,
) -> None:
    """审批结果只能是 approved / rejected，其他取值返回 400。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]

    response = _approve(client, admin_headers, leave_id, bad_status)

    assert response.status_code == 400
    payload = response.json()
    assert_unified_response(payload, code=400)
    assert payload["message"] == "审批结果只能是 approved（通过）或 rejected（驳回）"


@pytest.mark.parametrize("role_fixture", ["staff_headers", "family_headers"])
def test_non_admin_cannot_review_leave(
    client: TestClient,
    request: pytest.FixtureRequest,
    role_fixture: str,
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """工作人员与家庭用户不能审批请假（403）。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    headers = request.getfixturevalue(role_fixture)

    response = _approve(client, headers, leave_id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_review_missing_leave_returns_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """审批不存在的请假返回 404。"""
    response = _approve(client, admin_headers, 999999)

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


# ======================================================================
# 五、撤销
# ======================================================================
def test_owner_cancels_own_leave(
    client: TestClient, family_headers: dict[str, str], event: Event, member: FamilyMember
) -> None:
    """户主本人可撤销本户的待审批请假。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]

    response = _cancel(client, family_headers, leave_id)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["status"] == LeaveStatus.CANCELLED.value


def test_admin_cancels_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """管理员可撤销任意请假。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]

    response = _cancel(client, admin_headers, leave_id)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == LeaveStatus.CANCELLED.value


def test_staff_cannot_cancel_leave(
    client: TestClient,
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
) -> None:
    """工作人员不能撤销请假（403）。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]

    response = _cancel(client, staff_headers, leave_id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_cannot_cancel_other_family_leave(
    client: TestClient,
    family_headers: dict[str, str],
    register_householder,
    event: Event,
) -> None:
    """户主不能撤销其他家庭的请假（403）。"""
    other = register_householder("hushu_lv_other4", phone="13700000205")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]
    leave_id = _apply(client, other["headers"], event.id, other_member_id).json()["data"]["id"]

    response = _cancel(client, family_headers, leave_id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


@pytest.mark.parametrize("result", ["approved", "rejected"])
def test_cannot_cancel_reviewed_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    event: Event,
    member: FamilyMember,
    result: str,
) -> None:
    """已审批的请假不能撤销（409）。"""
    leave_id = _apply(client, family_headers, event.id, member.id).json()["data"]["id"]
    assert _approve(client, admin_headers, leave_id, result).status_code == 200

    response = _cancel(client, family_headers, leave_id)

    assert response.status_code == 409
    payload = response.json()
    assert_unified_response(payload, code=409)
    assert payload["message"] == "只有待审批的请假可以撤销"


def test_cancel_missing_leave_returns_404(
    client: TestClient, family_headers: dict[str, str]
) -> None:
    """撤销不存在的请假返回 404。"""
    response = _cancel(client, family_headers, 999999)

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


# ======================================================================
# 六、与签到联动
# ======================================================================
def test_actual_checkin_wins_over_approved_leave(
    client: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    make_event,
    member: FamilyMember,
    session_factory: sessionmaker[Session],
) -> None:
    """已通过请假的成员实际到场签到：以实际签到为准，结束时不覆盖为请假。"""
    ongoing = make_event(
        "联动活动",
        start_offset_minutes=-5,
        end_offset_minutes=120,
        status=EventStatus.ACTIVE,
    )

    leave_id = _apply(client, family_headers, ongoing.id, member.id, "临时有事").json()[
        "data"
    ]["id"]
    assert _approve(client, admin_headers, leave_id).status_code == 200

    # 该成员实际到场签到
    checked = client.post(
        f"{CHECKINS}/manual",
        headers=admin_headers,
        json={"event_id": ongoing.id, "member_id": member.id},
    )
    assert checked.status_code == 200
    assert checked.json()["data"]["status"] == CheckinStatus.SIGNED.value

    # 结束活动：已有签到记录的成员不会被生成为 leave
    finished = client.put(
        f"{EVENTS}/{ongoing.id}/status", headers=admin_headers, json={"status": "finished"}
    )
    assert finished.status_code == 200

    session = session_factory()
    try:
        record = (
            session.query(CheckinRecord)
            .filter(
                CheckinRecord.event_id == ongoing.id,
                CheckinRecord.member_id == member.id,
            )
            .one()
        )
        assert record.status == CheckinStatus.SIGNED
        assert record.method is not None
    finally:
        session.close()

    summary = client.get(f"{CHECKINS}/events/{ongoing.id}", headers=admin_headers).json()[
        "data"
    ]["summary"]
    assert summary["signed"] == 1
    assert summary["leave"] == 0
