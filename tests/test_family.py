"""家庭模块接口测试。

覆盖点：

- **户主注册自动建档**：家庭档案 + 户主成员行，同事务写入，失败整体回滚
- 我的家庭：户主查看自家详情（含成员）、未登录 401、无档案 404
- 家庭列表：管理员/工作人员可访问、家庭用户 403、分页与成员数统计、关键字与筛选
- 家庭详情：户主本人/管理员/工作人员可读，跨家庭 403，不存在 404
- 家庭修改：户主本人可改、工作人员 403、跨家庭 403、户号冲突 409、户号不可清空
- 管理员建档：为无家庭的 family 角色用户建档、重复建档 409、非家庭角色 400
- 停用家庭：管理员停用并级联停用成员、重复停用 409、家庭用户 403
- 与用户模块联动：删除仍有家庭档案的户主账号被拒绝（409），避免外键冲突
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.family.models import Family, FamilyStatus
from src.member.models import FamilyMember, MemberRelation, MemberStatus
from src.user.models import User, UserRole
from tests.helpers import FAMILY_USERNAME, assert_unified_response

REGISTER_BODY = {
    "username": "hushu_001",
    "password": "hushu123456",
    "real_name": "赵户主",
    "phone": "13700000001",
    "family": {
        "address": "幸福村 12 号",
        "village": "幸福村",
        "contact_phone": "13700000001",
    },
}


# ======================================================================
# 户主注册自动建档
# ======================================================================
def test_register_creates_family_and_householder_member(client, db: Session) -> None:
    """注册成功后自动创建家庭档案与户主成员记录。"""
    response = client.post("/api/auth/register", json=REGISTER_BODY)

    body = response.json()
    assert_unified_response(body)
    assert "家庭档案" in body["message"]
    user_id = body["data"]["id"]

    db.rollback()
    family = db.scalar(select(Family).where(Family.owner_id == user_id))
    assert family is not None
    assert family.household_no is not None
    assert re.fullmatch(r"F\d{6}", family.household_no), family.household_no
    assert family.address == "幸福村 12 号"
    assert family.village == "幸福村"
    assert family.contact_phone == "13700000001"
    assert family.status == FamilyStatus.ACTIVE

    member = db.scalar(select(FamilyMember).where(FamilyMember.family_id == family.id))
    assert member is not None
    assert member.relation == MemberRelation.HOUSEHOLDER
    assert member.user_id == user_id
    assert member.name == "赵户主"
    assert member.phone == "13700000001"
    assert member.needs_checkin is True
    assert member.status == MemberStatus.ACTIVE


def test_register_without_family_info_still_creates_family(client, db: Session) -> None:
    """family 字段可省略，系统仍会建档并生成户号。"""
    response = client.post(
        "/api/auth/register",
        json={
            "username": "hushu_003",
            "password": "hushu123456",
            "real_name": "钱户主",
        },
    )

    assert_unified_response(response.json())
    db.rollback()
    family = db.scalar(
        select(Family).where(Family.owner_id == response.json()["data"]["id"])
    )
    assert family is not None and family.household_no is not None


def test_register_with_custom_household_no(client, db: Session) -> None:
    """注册时可指定真实户籍号。"""
    response = client.post(
        "/api/auth/register",
        json={
            **REGISTER_BODY,
            "username": "hushu_004",
            "phone": "13700000004",
            "family": {"household_no": "HJ2025001"},
        },
    )

    assert_unified_response(response.json())
    db.rollback()
    family = db.scalar(select(Family).where(Family.household_no == "HJ2025001"))
    assert family is not None


def test_register_duplicate_household_no_rolls_back(
    client, db: Session, register_householder
) -> None:
    """户号重复时注册失败，且账号不会残留在数据库中（整体回滚）。"""
    register_householder("hushu_a", phone="13700000009", family={"household_no": "HJ-001"})

    response = client.post(
        "/api/auth/register",
        json={**REGISTER_BODY, "family": {"household_no": "HJ-001"}},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)

    db.rollback()
    assert db.scalar(select(User).where(User.username == "hushu_001")) is None
    assert db.scalar(select(Family).where(Family.household_no == "HJ-001")) is not None


def test_register_rejects_unknown_family_field(client) -> None:
    """family 字段中提交未声明字段被拒绝。"""
    response = client.post(
        "/api/auth/register",
        json={**REGISTER_BODY, "family": {"address": "某村", "owner_name": "张冠李戴"}},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "owner_name" in body["message"]


def test_register_rejects_invalid_contact_phone(client) -> None:
    """家庭联系电话格式错误被拦截。"""
    response = client.post(
        "/api/auth/register",
        json={**REGISTER_BODY, "family": {"contact_phone": "12345"}},
    )

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 我的家庭
# ======================================================================
def test_my_family_returns_family_with_members(
    client, family_headers, family: Family, member: FamilyMember
) -> None:
    """户主可查看自家详情，含成员列表与统计。"""
    response = client.get("/api/families/me", headers=family_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["id"] == family.id
    assert data["household_no"] == family.household_no
    assert data["owner_username"] == FAMILY_USERNAME
    assert data["owner_name"] == "测试户主"
    assert data["member_count"] == 2
    assert data["checkin_required_count"] == 2
    assert {item["relation"] for item in data["members"]} == {"householder", "son"}


def test_my_family_requires_login(client) -> None:
    """未登录访问我的家庭返回 401。"""
    response = client.get("/api/families/me")

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_my_family_without_family_returns_404(client, admin_headers) -> None:
    """管理员账号没有家庭档案时给出明确提示。"""
    response = client.get("/api/families/me", headers=admin_headers)

    assert response.status_code == 404
    body = response.json()
    assert_unified_response(body, code=404)
    assert "尚无家庭档案" in body["message"]


# ======================================================================
# 家庭列表
# ======================================================================
def test_family_list_forbidden_for_family_user(client, family_headers) -> None:
    """家庭用户不能查看家庭列表。"""
    response = client.get("/api/families", headers=family_headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_family_list_allowed_for_staff(
    client, staff_headers, register_householder
) -> None:
    """工作人员可查看家庭列表（协助签到需要名单）。"""
    register_householder("hushu_staff_view", phone="13700000011")

    response = client.get("/api/families", headers=staff_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1


def test_family_list_pagination_and_counts(
    client, admin_headers, register_householder
) -> None:
    """列表返回分页信息与成员数统计。"""
    first = register_householder("hushu_p1", phone="13700000021")
    second = register_householder("hushu_p2", phone="13700000022")
    # 给第二个家庭加一名成员
    client.post(
        "/api/members",
        headers=second["headers"],
        json={"name": "王二", "relation": "spouse", "needs_checkin": False},
    )

    response = client.get("/api/families?page=1&page_size=10", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["total"] == 2
    assert data["page"] == 1 and data["page_size"] == 10

    by_owner = {item["owner_id"]: item for item in data["items"]}
    assert by_owner[first["user"]["id"]]["member_count"] == 1
    assert by_owner[first["user"]["id"]]["checkin_required_count"] == 1
    assert by_owner[second["user"]["id"]]["member_count"] == 2
    assert by_owner[second["user"]["id"]]["checkin_required_count"] == 1


def test_family_list_keyword_by_owner_name(
    client, admin_headers, register_householder
) -> None:
    """关键字可按户主姓名搜索。"""
    register_householder("hushu_kw", phone="13700000031", real_name="孙悟空")

    response = client.get("/api/families?keyword=悟空", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["owner_name"] == "孙悟空"


def test_family_list_keyword_by_household_no(
    client, admin_headers, register_householder
) -> None:
    """关键字可按户号精确搜索。"""
    registered = register_householder(
        "hushu_kw2", phone="13700000032", family={"household_no": "HJ-8888"}
    )
    family_id = client.get(
        "/api/families/me", headers=registered["headers"]
    ).json()["data"]["id"]

    response = client.get("/api/families?keyword=HJ-8888", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["id"] == family_id


def test_family_list_filter_by_status_and_village(
    client, admin_headers, register_householder
) -> None:
    """按状态与村组筛选。"""
    register_householder(
        "hushu_f1", phone="13700000041", family={"village": "和平村"}
    )
    register_householder(
        "hushu_f2", phone="13700000042", family={"village": "幸福村"}
    )

    by_village = client.get(
        "/api/families?village=和平村", headers=admin_headers
    ).json()
    assert by_village["data"]["total"] == 1

    by_status = client.get(
        "/api/families?status=inactive", headers=admin_headers
    ).json()
    assert by_status["data"]["total"] == 0


def test_family_list_invalid_page_size(client, admin_headers) -> None:
    """page_size 超限被参数校验拦截。"""
    response = client.get("/api/families?page_size=999", headers=admin_headers)

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 家庭详情
# ======================================================================
def test_family_detail_visible_to_owner_staff_and_admin(
    client, family_headers, staff_headers, admin_headers, family: Family
) -> None:
    """户主本人、工作人员、管理员均可查看详情。"""
    for headers in (family_headers, staff_headers, admin_headers):
        response = client.get(f"/api/families/{family.id}", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert_unified_response(body)
        assert body["data"]["id"] == family.id

    assert len(body["data"]["members"]) == 1  # 户主成员行


def test_family_detail_forbidden_across_families(
    client, family_headers, register_householder
) -> None:
    """家庭用户不能查看他人家庭详情。"""
    other = register_householder("hushu_other", phone="13700000051")
    other_family_id = client.get(
        "/api/families/me", headers=other["headers"]
    ).json()["data"]["id"]

    response = client.get(f"/api/families/{other_family_id}", headers=family_headers)

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "只能查看本人家庭" in body["message"]


def test_family_detail_not_found(client, admin_headers) -> None:
    """家庭不存在返回 404。"""
    response = client.get("/api/families/999999", headers=admin_headers)

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


# ======================================================================
# 修改家庭信息
# ======================================================================
def test_owner_updates_family_info(client, family_headers, family: Family) -> None:
    """户主可修改住址、村组、联系电话与备注。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=family_headers,
        json={
            "address": "幸福村 88 号",
            "village": "幸福村二组",
            "contact_phone": "13700000088",
            "remark": "低保户",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["address"] == "幸福村 88 号"
    assert body["data"]["village"] == "幸福村二组"
    assert body["data"]["remark"] == "低保户"
    assert body["data"]["household_no"] == family.household_no


def test_owner_can_clear_optional_fields(client, family_headers, family: Family) -> None:
    """可选字段传 null 表示清空。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=family_headers,
        json={"address": None, "contact_phone": None},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["address"] is None
    assert body["data"]["contact_phone"] is None


def test_household_no_cannot_be_cleared(client, family_headers, family: Family) -> None:
    """户号不允许清空。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=family_headers,
        json={"household_no": None},
    )

    assert response.status_code == 400
    body = response.json()
    assert_unified_response(body, code=400)
    assert "户号不能为空" in body["message"]


def test_household_no_conflict(client, family_headers, family: Family, register_householder) -> None:
    """户号被其他家庭占用返回 409。"""
    register_householder(
        "hushu_conflict", phone="13700000061", family={"household_no": "HJ-9999"}
    )

    response = client.put(
        f"/api/families/{family.id}",
        headers=family_headers,
        json={"household_no": "HJ-9999"},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_staff_cannot_update_family(client, staff_headers, family: Family) -> None:
    """工作人员只读，不能修改家庭信息。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=staff_headers,
        json={"address": "越权修改"},
    )

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "只有户主本人或管理员" in body["message"]


def test_family_user_cannot_update_other_family(
    client, family_headers, register_householder
) -> None:
    """不能修改他人家庭信息。"""
    other = register_householder("hushu_other2", phone="13700000062")
    other_family_id = client.get(
        "/api/families/me", headers=other["headers"]
    ).json()["data"]["id"]

    response = client.put(
        f"/api/families/{other_family_id}",
        headers=family_headers,
        json={"address": "越权修改"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_admin_can_update_any_family(client, admin_headers, family: Family) -> None:
    """管理员可修改任意家庭（含停用标记）。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=admin_headers,
        json={"village": "幸福村三组", "status": "inactive"},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["village"] == "幸福村三组"
    assert body["data"]["status"] == "inactive"


def test_update_family_rejects_unknown_field(client, admin_headers, family: Family) -> None:
    """提交未声明字段被拒绝。"""
    response = client.put(
        f"/api/families/{family.id}",
        headers=admin_headers,
        json={"owner_name": "改名"},
    )

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 管理员建档
# ======================================================================
def test_admin_creates_family_for_user(client, db: Session, admin_headers, make_user) -> None:
    """管理员可为尚无家庭档案的户主补录家庭，并自动生成户主成员行。"""
    owner = make_user("hushu_bare", "hushu123456", role=UserRole.FAMILY, real_name="孙户主")

    response = client.post(
        "/api/families",
        headers=admin_headers,
        json={"owner_id": owner.id, "address": "和平村 3 号", "village": "和平村"},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert "户号" in body["message"]
    assert body["data"]["owner_id"] == owner.id
    assert body["data"]["member_count"] == 1

    db.rollback()
    member = db.scalar(
        select(FamilyMember).where(FamilyMember.family_id == body["data"]["id"])
    )
    assert member is not None
    assert member.relation == MemberRelation.HOUSEHOLDER
    assert member.user_id == owner.id


def test_admin_create_family_duplicate(client, admin_headers, family: Family) -> None:
    """该户主已有家庭档案时返回 409。"""
    response = client.post(
        "/api/families", headers=admin_headers, json={"owner_id": family.owner_id}
    )

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "已有家庭档案" in body["message"]


def test_admin_create_family_for_non_family_role(
    client, admin_headers, staff_user: User
) -> None:
    """工作人员账号不能拥有家庭档案。"""
    response = client.post(
        "/api/families", headers=admin_headers, json={"owner_id": staff_user.id}
    )

    assert response.status_code == 400
    body = response.json()
    assert_unified_response(body, code=400)
    assert "只有家庭用户" in body["message"]


def test_admin_create_family_unknown_owner(client, admin_headers) -> None:
    """户主不存在返回 404。"""
    response = client.post(
        "/api/families", headers=admin_headers, json={"owner_id": 999999}
    )

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


def test_family_user_cannot_create_family(client, family_headers, make_user) -> None:
    """家庭用户不能调用建档接口。"""
    owner = make_user("hushu_bare2", "hushu123456", role=UserRole.FAMILY)

    response = client.post(
        "/api/families", headers=family_headers, json={"owner_id": owner.id}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 停用家庭
# ======================================================================
def test_admin_deactivates_family_and_members(
    client, db: Session, admin_headers, family: Family, member: FamilyMember
) -> None:
    """停用家庭时级联停用成员，并返回影响数量。"""
    response = client.delete(f"/api/families/{family.id}", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["deactivated_members"] == 2
    assert "已停用" in body["message"]

    db.rollback()
    refreshed_family = db.get(Family, family.id)
    assert refreshed_family is not None
    assert refreshed_family.status == FamilyStatus.INACTIVE

    members = db.scalars(
        select(FamilyMember).where(FamilyMember.family_id == family.id)
    ).all()
    assert all(item.status == MemberStatus.INACTIVE for item in members)


def test_deactivate_family_twice(client, admin_headers, family: Family) -> None:
    """重复停用返回 409。"""
    client.delete(f"/api/families/{family.id}", headers=admin_headers)

    response = client.delete(f"/api/families/{family.id}", headers=admin_headers)

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "已处于停用状态" in body["message"]


def test_family_user_cannot_deactivate_family(client, family_headers, family: Family) -> None:
    """家庭用户不能停用家庭档案（仅管理员）。"""
    response = client.delete(f"/api/families/{family.id}", headers=family_headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 与用户模块联动
# ======================================================================
def test_delete_householder_user_is_blocked(
    client, admin_headers, family: Family
) -> None:
    """删除仍有家庭档案的户主账号被拒绝，避免外键冲突。"""
    response = client.delete(f"/api/users/{family.owner_id}", headers=admin_headers)

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "家庭户主" in body["message"]


def test_delete_user_without_family_still_works(client, admin_headers, make_user) -> None:
    """无家庭档案的用户仍可正常删除（不受阶段三改动影响）。"""
    target = make_user("no_family_user", "nofamily123456", role=UserRole.STAFF)

    response = client.delete(f"/api/users/{target.id}", headers=admin_headers)

    assert response.status_code == 200
    assert_unified_response(response.json())
