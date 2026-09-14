"""家庭成员模块接口测试。

覆盖点：

- 添加成员：户主添加、身份证号自动识别性别与出生日期、身份证号校验位/出生日期校验、
  身份证号重复 409、跨家庭 403、工作人员 403、管理员必须指定 family_id、已停用家庭 409
- 成员列表：户主仅看本户、跨家庭 403、管理员/工作人员可查全部、是否需签到筛选、关键字搜索
- 成员详情：户主本人可读、跨家庭 403、不存在 404
- 修改成员：普通字段更新、清空身份证号、身份证号冲突 409、姓名不可清空、越权 403
- 启用/停用：停用后不参与签到统计（家庭 member_count 随之减少）
- 删除成员：删除后 404、跨家庭 403
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.family.models import Family
from src.member.models import FamilyMember, MemberRelation, MemberStatus
from tests.helpers import assert_unified_response

#: 合法身份证号：1990-03-15 出生，性别男（末位为校验位 9）
VALID_ID_CARD = "110105199003151239"

#: 合法身份证号：1990-03-15 出生，性别女（末位为校验位 7）
FEMALE_ID_CARD = "110105199003151247"

#: 校验位错误的身份证号
INVALID_CHECKSUM_ID_CARD = "110105199003151238"

#: 出生日期非法的身份证号（1990-13-15）
INVALID_BIRTH_ID_CARD = "110105199013151239"


# ======================================================================
# 添加成员
# ======================================================================
def test_owner_adds_member(client, family_headers) -> None:
    """户主为自家添加成员，无需传 family_id。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={
            "name": "李小花",
            "relation": "daughter",
            "gender": "female",
            "phone": "13700000077",
            "needs_checkin": False,
            "remark": "在外读书",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["name"] == "李小花"
    assert data["relation"] == "daughter"
    assert data["gender"] == "female"
    assert data["needs_checkin"] is False
    assert data["status"] == "active"
    assert data["user_id"] is None


def test_add_member_derives_gender_and_birth_date_from_id_card(
    client, family_headers
) -> None:
    """填写身份证号后自动补齐性别与出生日期。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李大明", "relation": "father", "id_card": VALID_ID_CARD},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["id_card"] == VALID_ID_CARD
    assert body["data"]["gender"] == "male"
    assert body["data"]["birth_date"] == "1990-03-15"


def test_add_member_derives_female_gender(client, family_headers) -> None:
    """身份证号第 17 位为偶数时识别为女性。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李二妹", "relation": "mother", "id_card": FEMALE_ID_CARD},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["gender"] == "female"
    assert body["data"]["birth_date"] == "1990-03-15"


def test_add_member_rejects_invalid_id_card_checksum(client, family_headers) -> None:
    """身份证号校验位错误被拦截。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李错误", "id_card": INVALID_CHECKSUM_ID_CARD},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "校验位" in body["message"]


def test_add_member_rejects_invalid_birth_date(client, family_headers) -> None:
    """身份证号中的出生日期非法被拦截。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李错误", "id_card": INVALID_BIRTH_ID_CARD},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "出生日期" in body["message"]


def test_add_member_rejects_short_id_card(client, family_headers) -> None:
    """身份证号长度不足被拦截。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李错误", "id_card": "11010519900315"},
    )

    assert_unified_response(response.json(), code=400)


def test_add_member_duplicate_id_card(client, family_headers) -> None:
    """同一身份证号不能重复添加（全局唯一）。"""
    first = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李大明", "id_card": VALID_ID_CARD},
    )
    assert_unified_response(first.json())

    second = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李大明重复", "id_card": VALID_ID_CARD},
    )

    assert second.status_code == 409
    body = second.json()
    assert_unified_response(body, code=409)
    assert "身份证号已被其他成员使用" in body["message"]


def test_family_user_cannot_add_member_to_other_family(
    client, family_headers, register_householder
) -> None:
    """家庭用户提交他人 family_id 被拒绝。"""
    other = register_householder("hushu_m_other", phone="13700000071")
    other_family_id = client.get(
        "/api/families/me", headers=other["headers"]
    ).json()["data"]["id"]

    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "越权成员", "family_id": other_family_id},
    )

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "不能为其他家庭添加成员" in body["message"]


def test_admin_add_member_requires_family_id(client, admin_headers) -> None:
    """管理员未指定 family_id 时给出明确提示。"""
    response = client.post(
        "/api/members", headers=admin_headers, json={"name": "王五"}
    )

    assert response.status_code == 400
    body = response.json()
    assert_unified_response(body, code=400)
    assert "必须指定 family_id" in body["message"]


def test_admin_adds_member_to_specified_family(
    client, admin_headers, family: Family
) -> None:
    """管理员可为指定家庭添加成员。"""
    response = client.post(
        "/api/members",
        headers=admin_headers,
        json={"name": "王五", "relation": "other", "family_id": family.id},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["family_id"] == family.id


def test_staff_cannot_add_member(client, staff_headers) -> None:
    """工作人员无权添加成员。"""
    response = client.post(
        "/api/members", headers=staff_headers, json={"name": "越权成员"}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_cannot_add_member_to_inactive_family(
    client, admin_headers, family_headers, family: Family
) -> None:
    """家庭档案停用后不能继续添加成员。"""
    client.delete(f"/api/families/{family.id}", headers=admin_headers)

    response = client.post(
        "/api/members", headers=family_headers, json={"name": "新成员"}
    )

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "已停用" in body["message"]


def test_add_member_rejects_unknown_field(client, family_headers) -> None:
    """提交未声明字段被拒绝。"""
    response = client.post(
        "/api/members", headers=family_headers, json={"name": "李四", "age": 30}
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "age" in body["message"]


def test_add_member_rejects_invalid_relation(client, family_headers) -> None:
    """非法关系取值被拦截。"""
    response = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "李四", "relation": "uncle"},
    )

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 成员列表
# ======================================================================
def test_owner_lists_own_members(client, family_headers, family: Family, member) -> None:
    """户主查询本户成员（含自动生成的户主成员行）。"""
    response = client.get("/api/members", headers=family_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["total"] == 2
    assert {item["relation"] for item in data["items"]} == {"householder", "son"}
    assert all(item["family_id"] == family.id for item in data["items"])


def test_owner_cannot_list_other_family_members(
    client, family_headers, register_householder
) -> None:
    """户主传入他人 family_id 查询被拒绝。"""
    other = register_householder("hushu_m_other2", phone="13700000072")
    other_family_id = client.get(
        "/api/families/me", headers=other["headers"]
    ).json()["data"]["id"]

    response = client.get(
        f"/api/members?family_id={other_family_id}", headers=family_headers
    )

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "只能查看本家庭" in body["message"]


def test_admin_lists_members_by_family(
    client, admin_headers, family: Family, member
) -> None:
    """管理员可按家庭筛选成员。"""
    response = client.get(
        f"/api/members?family_id={family.id}", headers=admin_headers
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 2


def test_staff_can_list_all_members(client, staff_headers, family: Family, member) -> None:
    """工作人员可查看全部成员。"""
    response = client.get("/api/members", headers=staff_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 2


def test_list_members_filter_by_needs_checkin(client, family_headers) -> None:
    """按"是否需要签到"筛选。"""
    client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "需要签到", "needs_checkin": True},
    )
    client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "不需要签到", "needs_checkin": False},
    )

    response = client.get("/api/members?needs_checkin=false", headers=family_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["name"] == "不需要签到"


def test_list_members_filter_by_status(client, family_headers) -> None:
    """按状态筛选。"""
    created = client.post(
        "/api/members", headers=family_headers, json={"name": "待停用成员"}
    ).json()["data"]
    client.put(
        f"/api/members/{created['id']}/status",
        headers=family_headers,
        json={"status": "inactive"},
    )

    response = client.get("/api/members?status=inactive", headers=family_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1


def test_list_members_keyword(client, family_headers) -> None:
    """关键字模糊匹配姓名。"""
    client.post("/api/members", headers=family_headers, json={"name": "欧阳锋"})

    response = client.get("/api/members?keyword=欧阳", headers=family_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["name"] == "欧阳锋"


def test_list_members_invalid_page_size(client, family_headers) -> None:
    """page_size 超限被拦截。"""
    response = client.get("/api/members?page_size=999", headers=family_headers)

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 成员详情
# ======================================================================
def test_owner_gets_member_detail(client, family_headers, member: FamilyMember) -> None:
    """户主查询本户成员详情。"""
    response = client.get(f"/api/members/{member.id}", headers=family_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["id"] == member.id
    assert body["data"]["name"] == "李小明"


def test_owner_cannot_get_other_family_member(
    client, family_headers, register_householder
) -> None:
    """户主不能查看其他家庭的成员。"""
    other = register_householder("hushu_m_other3", phone="13700000073")
    other_member_id = client.post(
        "/api/members",
        headers=other["headers"],
        json={"name": "别家成员"},
    ).json()["data"]["id"]

    response = client.get(f"/api/members/{other_member_id}", headers=family_headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_member_detail_not_found(client, family_headers) -> None:
    """成员不存在返回 404。"""
    response = client.get("/api/members/999999", headers=family_headers)

    assert response.status_code == 404
    body = response.json()
    assert_unified_response(body, code=404)
    assert "家庭成员不存在" in body["message"]


# ======================================================================
# 修改成员
# ======================================================================
def test_update_member_fields(client, family_headers, member: FamilyMember) -> None:
    """修改姓名、关系、是否需签到与备注。"""
    response = client.put(
        f"/api/members/{member.id}",
        headers=family_headers,
        json={
            "name": "李小明（改名）",
            "relation": "son",
            "needs_checkin": False,
            "remark": "外出务工",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["name"] == "李小明（改名）"
    assert body["data"]["needs_checkin"] is False
    assert body["data"]["remark"] == "外出务工"


def test_update_member_clear_id_card(client, family_headers) -> None:
    """身份证号可显式清空，且不影响既有性别/出生日期。"""
    created = client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "清空身份证", "id_card": VALID_ID_CARD},
    ).json()["data"]
    assert created["gender"] == "male"

    response = client.put(
        f"/api/members/{created['id']}", headers=family_headers, json={"id_card": None}
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["id_card"] is None
    assert body["data"]["gender"] == "male"
    assert body["data"]["birth_date"] == "1990-03-15"


def test_update_member_duplicate_id_card(client, family_headers) -> None:
    """改为已被占用的身份证号返回 409。"""
    client.post(
        "/api/members",
        headers=family_headers,
        json={"name": "已有身份证", "id_card": VALID_ID_CARD},
    )
    other = client.post(
        "/api/members", headers=family_headers, json={"name": "待修改成员"}
    ).json()["data"]

    response = client.put(
        f"/api/members/{other['id']}",
        headers=family_headers,
        json={"id_card": VALID_ID_CARD},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_update_member_null_name_rejected(client, family_headers, member: FamilyMember) -> None:
    """姓名不能清空。"""
    response = client.put(
        f"/api/members/{member.id}", headers=family_headers, json={"name": None}
    )

    assert response.status_code == 400
    body = response.json()
    assert_unified_response(body, code=400)
    assert "成员姓名不能为空" in body["message"]


def test_owner_cannot_update_other_family_member(
    client, family_headers, register_householder
) -> None:
    """不能修改其他家庭的成员。"""
    other = register_householder("hushu_m_other4", phone="13700000074")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    response = client.put(
        f"/api/members/{other_member_id}",
        headers=family_headers,
        json={"name": "越权改名"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_update_member_rejects_unknown_field(client, family_headers, member: FamilyMember) -> None:
    """提交未声明字段被拒绝。"""
    response = client.put(
        f"/api/members/{member.id}", headers=family_headers, json={"face_id": "abc"}
    )

    assert_unified_response(response.json(), code=400)


# ======================================================================
# 启用/停用
# ======================================================================
def test_stop_member_excludes_from_family_count(
    client, db: Session, family_headers, family: Family, member: FamilyMember
) -> None:
    """停用成员后，家庭的有效成员数与需签到人数同步减少。"""
    before = client.get("/api/families/me", headers=family_headers).json()["data"]
    assert before["member_count"] == 2
    assert before["checkin_required_count"] == 2

    response = client.put(
        f"/api/members/{member.id}/status",
        headers=family_headers,
        json={"status": "inactive"},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["status"] == "inactive"
    assert body["message"] == "成员已停用"

    after = client.get("/api/families/me", headers=family_headers).json()["data"]
    assert after["member_count"] == 1
    assert after["checkin_required_count"] == 1

    db.rollback()
    stored = db.get(FamilyMember, member.id)
    assert stored is not None and stored.status == MemberStatus.INACTIVE


def test_restart_member(client, family_headers, member: FamilyMember) -> None:
    """停用后可重新启用。"""
    client.put(
        f"/api/members/{member.id}/status",
        headers=family_headers,
        json={"status": "inactive"},
    )

    response = client.put(
        f"/api/members/{member.id}/status",
        headers=family_headers,
        json={"status": "active"},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["status"] == "active"
    assert body["message"] == "成员已启用"


def test_staff_cannot_change_member_status(
    client, staff_headers, member: FamilyMember
) -> None:
    """工作人员不能修改成员状态。"""
    response = client.put(
        f"/api/members/{member.id}/status",
        headers=staff_headers,
        json={"status": "inactive"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 删除成员
# ======================================================================
def test_owner_deletes_member(client, family_headers, member: FamilyMember) -> None:
    """户主删除成员 = 停用成员：接口返回 200，成员状态变为 inactive 且仍可查询。"""
    response = client.delete(f"/api/members/{member.id}", headers=family_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["message"] == "删除成功"

    detail = client.get(f"/api/members/{member.id}", headers=family_headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert_unified_response(detail_body)
    assert detail_body["data"]["id"] == member.id
    assert detail_body["data"]["status"] == "inactive"

    # 列表中仍可见（默认不按状态筛选），便于保留历史签到数据
    listed = client.get(
        "/api/members", headers=family_headers, params={"page_size": 100}
    ).json()
    assert listed["code"] == 0
    assert member.id in [item["id"] for item in listed["data"]["items"]]


def test_delete_member_twice_returns_conflict(
    client, family_headers, member: FamilyMember
) -> None:
    """重复删除已停用的成员返回 409。"""
    first = client.delete(f"/api/members/{member.id}", headers=family_headers)
    assert first.status_code == 200

    second = client.delete(f"/api/members/{member.id}", headers=family_headers)
    assert second.status_code == 409
    assert_unified_response(second.json(), code=409)


def test_owner_cannot_delete_other_family_member(
    client, family_headers, register_householder
) -> None:
    """不能删除其他家庭的成员。"""
    other = register_householder("hushu_m_other5", phone="13700000075")
    other_member_id = client.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    response = client.delete(
        f"/api/members/{other_member_id}", headers=family_headers
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_member_relations_are_available(client, family_headers) -> None:
    """各关系取值均可正常写入（覆盖枚举取值）。"""
    relations = [
        "spouse",
        "son",
        "daughter",
        "father",
        "mother",
        "other",
    ]
    for index, relation in enumerate(relations):
        response = client.post(
            "/api/members",
            headers=family_headers,
            json={"name": f"成员{index}", "relation": relation},
        )
        body = response.json()
        assert_unified_response(body), f"{relation} 写入失败：{body}"
        assert body["data"]["relation"] == relation

    listed = client.get("/api/members", headers=family_headers).json()["data"]
    assert listed["total"] == len(relations) + 1  # 加上户主成员行
    assert {item["relation"] for item in listed["items"]} == set(relations) | {
        MemberRelation.HOUSEHOLDER.value
    }
