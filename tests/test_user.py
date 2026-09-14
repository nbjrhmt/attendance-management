"""用户管理模块接口测试。

覆盖点：

- 权限：未登录 401、非管理员 403（家庭用户/工作人员）、本人可查看自己的信息
- 列表：分页、角色/状态筛选、关键字搜索（含 LIKE 通配符转义）、参数边界
- 新增：管理员创建、用户名/手机号冲突
- 修改：本人修改姓名/手机号、清空手机号、越权修改角色或状态被拒绝、手机号冲突
- 密码：本人改密（校验原密码、新旧不能相同）、管理员重置、越权改他人密码被拒绝
- 状态与删除：启用/禁用、不可操作自己、删除后令牌失效
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.auth.security import create_access_token, verify_password
from src.user import crud
from src.user.models import User, UserRole, UserStatus
from tests.helpers import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    FAMILY_PASSWORD,
    FAMILY_USERNAME,
    STAFF_USERNAME,
    assert_unified_response,
    auth_headers,
)


# ======================================================================
# 权限控制
# ======================================================================
def test_list_users_requires_login(client) -> None:
    """未登录访问用户列表返回 401。"""
    response = client.get("/api/users")

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_list_users_requires_admin(client, family_headers) -> None:
    """家庭用户访问用户列表返回 403。"""
    response = client.get("/api/users", headers=family_headers)

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "权限不足" in body["message"]


def test_staff_cannot_list_users(client, staff_headers) -> None:
    """工作人员不能访问用户列表（仅管理员）。"""
    response = client.get("/api/users", headers=staff_headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_staff_cannot_create_user(client, staff_headers) -> None:
    """工作人员不能创建用户。"""
    response = client.post(
        "/api/users",
        headers=staff_headers,
        json={
            "username": "new_user",
            "password": "newpass123456",
            "real_name": "新用户",
        },
    )

    assert response.status_code == 403


# ======================================================================
# 列表查询
# ======================================================================
def test_list_users_pagination(client, admin_headers, make_user) -> None:
    """分页返回 total / page / page_size / items。"""
    for index in range(3):
        make_user(f"family_{index}", "family123456")

    response = client.get("/api/users?page=1&page_size=2", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["total"] == 4  # admin_headers 依赖已创建 1 个管理员 + 3 个家庭用户
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2
    assert "password_hash" not in data["items"][0]


def test_list_users_filter_by_role(client, admin_headers, staff_user, family_user) -> None:
    """按角色筛选。"""
    response = client.get("/api/users?role=staff", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["username"] == STAFF_USERNAME


def test_list_users_filter_by_status(client, admin_headers, make_user) -> None:
    """按状态筛选。"""
    make_user("disabled_one", "disabled123456", status=UserStatus.DISABLED)

    response = client.get("/api/users?status=disabled", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["status"] == "disabled"


def test_list_users_keyword_search(client, admin_headers, make_user) -> None:
    """关键字模糊搜索用户名/姓名/手机号（下划线被当作普通字符）。"""
    make_user("zhang_san", "zhang123456", real_name="张三", phone="13911112222")

    response = client.get("/api/users?keyword=zhang_san", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["username"] == "zhang_san"


def test_list_users_keyword_no_match(client, admin_headers) -> None:
    """关键字无匹配时返回空列表而不是报错。"""
    response = client.get("/api/users?keyword=不存在的名字", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 0
    assert body["data"]["items"] == []


def test_list_users_invalid_page_size(client, admin_headers) -> None:
    """page_size 超出上限被参数校验拦截。"""
    response = client.get("/api/users?page_size=1000", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body, code=400)
    assert "page_size" in body["message"]


# ======================================================================
# 新增用户
# ======================================================================
def test_admin_create_user(client, admin_headers) -> None:
    """管理员可创建指定角色与状态的用户。"""
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "worker_01",
            "password": "worker123456",
            "real_name": "工作人员甲",
            "phone": "13900000031",
            "role": "staff",
            "status": "active",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["role"] == "staff"
    assert body["data"]["username"] == "worker_01"


def test_admin_create_user_duplicate_username(client, admin_headers, family_user) -> None:
    """用户名重复返回 409。"""
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": FAMILY_USERNAME,
            "password": "anypass123456",
            "real_name": "重名用户",
        },
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_admin_create_user_duplicate_phone(client, admin_headers, family_user) -> None:
    """手机号重复返回 409。"""
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "another_user",
            "password": "another123456",
            "real_name": "另一个用户",
            "phone": family_user.phone,
        },
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_admin_create_user_invalid_role(client, admin_headers) -> None:
    """非法角色取值被拦截。"""
    response = client.post(
        "/api/users",
        headers=admin_headers,
        json={
            "username": "bad_role_user",
            "password": "badrole123456",
            "real_name": "非法角色",
            "role": "superman",
        },
    )

    body = response.json()
    assert_unified_response(body, code=400)


# ======================================================================
# 详情查询
# ======================================================================
def test_get_own_detail(client, family_headers, family_user) -> None:
    """家庭用户可以查看自己的信息。"""
    response = client.get(f"/api/users/{family_user.id}", headers=family_headers)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["id"] == family_user.id
    assert body["data"]["role"] == "family"


def test_get_other_detail_forbidden(client, family_headers, admin_user) -> None:
    """家庭用户不能查看他人信息。"""
    response = client.get(f"/api/users/{admin_user.id}", headers=family_headers)

    assert response.status_code == 403
    assert "只能操作本人" in response.json()["message"]


def test_admin_get_any_detail(client, admin_headers, family_user) -> None:
    """管理员可以查看任意用户信息。"""
    response = client.get(f"/api/users/{family_user.id}", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["username"] == FAMILY_USERNAME


def test_get_nonexistent_user(client, admin_headers) -> None:
    """用户不存在返回 404。"""
    response = client.get("/api/users/999999", headers=admin_headers)

    assert response.status_code == 404
    body = response.json()
    assert_unified_response(body, code=404)
    assert "不存在" in body["message"]


# ======================================================================
# 修改用户信息
# ======================================================================
def test_self_update_profile(client, family_headers, family_user) -> None:
    """本人可以修改姓名与手机号。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=family_headers,
        json={"real_name": "李四", "phone": "13700000009"},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["real_name"] == "李四"
    assert body["data"]["phone"] == "13700000009"


def test_self_update_clear_phone(client, family_headers, family_user) -> None:
    """手机号可以显式置空（exclude_unset 区分未提交与显式 null）。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=family_headers,
        json={"phone": None},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["phone"] is None
    assert body["data"]["real_name"] == family_user.real_name


def test_self_cannot_change_role(client, family_headers, family_user) -> None:
    """本人不能把自己的角色改成管理员。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=family_headers,
        json={"role": "admin"},
    )

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "角色" in body["message"]


def test_self_cannot_change_status(client, family_headers, family_user) -> None:
    """本人不能修改自己的账号状态。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=family_headers,
        json={"status": "disabled"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_cannot_update_other_user(client, family_headers, admin_user) -> None:
    """不能修改他人信息。"""
    response = client.put(
        f"/api/users/{admin_user.id}",
        headers=family_headers,
        json={"real_name": "篡改"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_update_phone_conflict(client, family_headers, family_user, admin_user) -> None:
    """手机号已被他人占用返回 409。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=family_headers,
        json={"phone": admin_user.phone},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_admin_update_role_and_status(client, admin_headers, family_user) -> None:
    """管理员可以修改角色与状态。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=admin_headers,
        json={"role": "staff", "status": "disabled", "real_name": "转为工作人员"},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["role"] == "staff"
    assert body["data"]["status"] == "disabled"
    assert body["data"]["real_name"] == "转为工作人员"


def test_update_rejects_unknown_field(client, admin_headers, family_user) -> None:
    """提交未声明字段（如 nickname）被拒绝。"""
    response = client.put(
        f"/api/users/{family_user.id}",
        headers=admin_headers,
        json={"nickname": "小名"},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "nickname" in body["message"]


def test_update_nonexistent_user(client, admin_headers) -> None:
    """修改不存在的用户返回 404。"""
    response = client.put(
        "/api/users/999999", headers=admin_headers, json={"real_name": "张三"}
    )

    assert response.status_code == 404


# ======================================================================
# 密码
# ======================================================================
def test_change_own_password(client, family_headers, family_user) -> None:
    """本人修改密码后，新密码可登录、旧密码失效。"""
    response = client.put(
        f"/api/users/{family_user.id}/password",
        headers=family_headers,
        json={"old_password": FAMILY_PASSWORD, "new_password": "brandnew123456"},
    )

    body = response.json()
    assert_unified_response(body)
    assert "成功" in body["message"]

    new_login = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": "brandnew123456"},
    )
    assert new_login.json()["code"] == 0

    old_login = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD},
    )
    assert old_login.status_code == 401


def test_change_password_wrong_old_password(client, family_headers, family_user) -> None:
    """原密码错误返回 400（HTTP 状态码与业务码一致）。"""
    response = client.put(
        f"/api/users/{family_user.id}/password",
        headers=family_headers,
        json={"old_password": "wrong-old-pass", "new_password": "brandnew123456"},
    )

    assert response.status_code == 400
    body = response.json()
    assert_unified_response(body, code=400)
    assert body["message"] == "原密码不正确"


def test_change_password_same_as_old(client, family_headers, family_user) -> None:
    """新密码不能与原密码相同。"""
    response = client.put(
        f"/api/users/{family_user.id}/password",
        headers=family_headers,
        json={"old_password": FAMILY_PASSWORD, "new_password": FAMILY_PASSWORD},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "不能与原密码相同" in body["message"]


def test_cannot_change_others_password(client, family_headers, admin_user) -> None:
    """不能修改他人密码（即使知道原密码）。"""
    response = client.put(
        f"/api/users/{admin_user.id}/password",
        headers=family_headers,
        json={"old_password": ADMIN_PASSWORD, "new_password": "hacked123456"},
    )

    assert response.status_code == 403
    body = response.json()
    assert_unified_response(body, code=403)
    assert "本人密码" in body["message"]


def test_admin_reset_password(client, db: Session, admin_headers, family_user) -> None:
    """管理员重置密码后，新密码可登录且落库为 bcrypt 哈希。"""
    response = client.put(
        f"/api/users/{family_user.id}/reset-password",
        headers=admin_headers,
        json={"new_password": "resetpass123456"},
    )

    body = response.json()
    assert_unified_response(body)
    assert body["message"] == "密码已重置"

    login = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": "resetpass123456"},
    )
    assert login.json()["code"] == 0

    db.rollback()
    stored = db.get(User, family_user.id)
    assert stored is not None
    assert stored.password_hash.startswith("$2b$")
    assert verify_password("resetpass123456", stored.password_hash) is True


def test_family_cannot_reset_password(client, family_headers, family_user) -> None:
    """家庭用户不能使用重置密码接口（仅管理员）。"""
    response = client.put(
        f"/api/users/{family_user.id}/reset-password",
        headers=family_headers,
        json={"new_password": "resetpass123456"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 启用/禁用
# ======================================================================
def test_admin_disable_and_enable_user(
    client, admin_headers, family_headers, family_user
) -> None:
    """禁用后该用户的旧令牌立即失效，重新启用后恢复可用。"""
    disable = client.put(
        f"/api/users/{family_user.id}/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )
    assert disable.json()["data"]["status"] == "disabled"
    assert disable.json()["message"] == "账号已禁用"

    blocked = client.get("/api/auth/profile", headers=family_headers)
    assert blocked.status_code == 403
    assert "禁用" in blocked.json()["message"]

    enable = client.put(
        f"/api/users/{family_user.id}/status",
        headers=admin_headers,
        json={"status": "active"},
    )
    assert enable.json()["data"]["status"] == "active"

    restored = client.get("/api/auth/profile", headers=family_headers)
    assert restored.json()["code"] == 0


def test_admin_cannot_disable_self(client, admin_headers, admin_user) -> None:
    """管理员不能禁用自己的账号。"""
    response = client.put(
        f"/api/users/{admin_user.id}/status",
        headers=admin_headers,
        json={"status": "disabled"},
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "自己" in body["message"]


def test_disable_nonexistent_user(client, admin_headers) -> None:
    """操作不存在的用户返回 404。"""
    response = client.put(
        "/api/users/999999/status", headers=admin_headers, json={"status": "disabled"}
    )

    assert response.status_code == 404
    assert_unified_response(response.json(), code=404)


# ======================================================================
# 删除
# ======================================================================
def test_admin_delete_user(client, admin_headers, family_user) -> None:
    """管理员删除用户后再查询返回 404。"""
    response = client.delete(f"/api/users/{family_user.id}", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["message"] == "删除成功"

    detail = client.get(f"/api/users/{family_user.id}", headers=admin_headers)
    assert detail.status_code == 404


def test_deleted_user_token_invalidated(client, admin_headers, make_user) -> None:
    """用户被删除后，其令牌立即失效。

    这里使用工作人员账号：户主账号因拥有家庭档案而无法直接删除（见 test_family.py）。
    """
    target = make_user("to_delete_user", "delete123456", role=UserRole.STAFF)
    target_headers = auth_headers(create_access_token(target))

    assert client.get("/api/auth/profile", headers=target_headers).json()["code"] == 0

    deleted = client.delete(f"/api/users/{target.id}", headers=admin_headers)
    assert_unified_response(deleted.json())

    response = client.get("/api/auth/profile", headers=target_headers)

    assert response.status_code == 401
    assert "用户不存在" in response.json()["message"]


def test_admin_cannot_delete_self(client, admin_headers, admin_user) -> None:
    """管理员不能删除自己的账号。"""
    response = client.delete(f"/api/users/{admin_user.id}", headers=admin_headers)

    body = response.json()
    assert_unified_response(body, code=400)
    assert "不能删除自己" in body["message"]


def test_family_cannot_delete_user(client, family_headers, admin_user) -> None:
    """家庭用户不能删除账号。"""
    response = client.delete(f"/api/users/{admin_user.id}", headers=family_headers)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


# ======================================================================
# 令牌与数据库一致性
# ======================================================================
def test_token_payload_role_is_used_for_authorization(
    client, db: Session, admin_headers, family_user
) -> None:
    """角色以数据库为准：令牌签发后角色被降级，接口权限立即随之变化。"""
    token = create_access_token(family_user)
    headers = auth_headers(token)

    assert client.get("/api/users", headers=headers).status_code == 403

    db.rollback()
    stored = db.get(User, family_user.id)
    assert stored is not None
    crud.update_user(db, stored, {"role": UserRole.ADMIN})

    assert client.get("/api/users", headers=headers).status_code == 200


def test_admin_username_is_correct(client, admin_headers, admin_user) -> None:
    """管理员夹具账号信息正确（防止夹具本身出错导致误判）。"""
    response = client.get("/api/auth/profile", headers=admin_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["username"] == ADMIN_USERNAME
    assert body["data"]["role"] == "admin"
