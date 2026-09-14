"""认证模块接口测试：注册、登录、令牌刷新、鉴权依赖。

覆盖点：

- 注册：成功、密码 bcrypt 落库、用户名/手机号重复、用户名/密码/越权字段校验
- 登录：成功、密码错误、账号不存在（提示不泄露账号是否存在）、账号被禁用、记录最后登录时间
- 鉴权：缺少令牌、格式错误、令牌被篡改、令牌过期、刷新令牌不能当访问令牌用
- 刷新：正常刷新、拒绝访问令牌、拒绝非法令牌
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.security import (
    create_access_token,
    create_refresh_token,
    verify_password,
)
from src.config.settings import settings
from src.user import crud
from src.user.models import User, UserRole, UserStatus
from tests.helpers import (
    FAMILY_PASSWORD,
    FAMILY_USERNAME,
    assert_unified_response,
    auth_headers,
)

REGISTER_BODY = {
    "username": "zhangsan",
    "password": "zhangsan123456",
    "real_name": "张三",
    "phone": "13900000001",
}


# ======================================================================
# 注册
# ======================================================================
def test_register_family_user_success(client) -> None:
    """家庭户主注册成功，角色固定为 family，响应不含密码字段。"""
    response = client.post("/api/auth/register", json=REGISTER_BODY)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["username"] == "zhangsan"
    assert data["real_name"] == "张三"
    assert data["phone"] == "13900000001"
    assert data["role"] == "family"
    assert data["status"] == "active"
    assert data["last_login_at"] is None
    assert "password" not in data and "password_hash" not in data


def test_register_password_stored_as_bcrypt(client, db: Session) -> None:
    """密码必须以 bcrypt 哈希形式落库，不能是明文。"""
    client.post("/api/auth/register", json=REGISTER_BODY)

    db.rollback()
    user = db.scalar(select(User).where(User.username == "zhangsan"))
    assert user is not None
    assert user.password_hash != REGISTER_BODY["password"]
    assert user.password_hash.startswith("$2b$")
    assert len(user.password_hash) == 60
    assert verify_password(REGISTER_BODY["password"], user.password_hash) is True
    assert verify_password("wrong-password", user.password_hash) is False


def test_register_duplicate_username(client, family_user: User) -> None:
    """用户名重复返回 409。"""
    response = client.post(
        "/api/auth/register",
        json={**REGISTER_BODY, "username": FAMILY_USERNAME},
    )

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "已被占用" in body["message"]


def test_register_duplicate_phone(client, family_user: User) -> None:
    """手机号重复返回 409（family_user 的手机号为 13800000002）。"""
    response = client.post(
        "/api/auth/register",
        json={**REGISTER_BODY, "phone": family_user.phone},
    )

    assert response.status_code == 409
    assert_unified_response(response.json(), code=409)


def test_register_rejects_short_username(client) -> None:
    """用户名过短被参数校验拦截（HTTP 200 + code 400）。"""
    response = client.post("/api/auth/register", json={**REGISTER_BODY, "username": "ab"})

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body, code=400)
    assert "username" in body["message"]


def test_register_rejects_invalid_username_chars(client) -> None:
    """用户名含非法字符被拦截。"""
    response = client.post(
        "/api/auth/register", json={**REGISTER_BODY, "username": "张三 001"}
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "格式不正确" in body["message"]


def test_register_rejects_short_password(client) -> None:
    """密码少于 6 位被拦截，提示中包含长度要求。"""
    response = client.post("/api/auth/register", json={**REGISTER_BODY, "password": "123"})

    body = response.json()
    assert_unified_response(body, code=400)
    assert "最少 6 个字符" in body["message"]


def test_register_rejects_password_over_72_bytes(client) -> None:
    """密码超过 bcrypt 的 72 字节上限被拦截（30 个中文字符 = 90 字节）。"""
    response = client.post(
        "/api/auth/register", json={**REGISTER_BODY, "password": "密" * 30}
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "72 字节" in body["message"]


def test_register_rejects_unknown_field(client) -> None:
    """注册时提交未声明字段（如 role=admin）被拒绝，防止越权提权。"""
    response = client.post(
        "/api/auth/register", json={**REGISTER_BODY, "role": "admin"}
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "role" in body["message"]


# ======================================================================
# 登录
# ======================================================================
def test_login_success(client, family_user: User) -> None:
    """登录成功返回双令牌与用户信息。"""
    response = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert data["access_token"] and data["refresh_token"]
    assert data["access_token"] != data["refresh_token"]
    assert data["user"]["username"] == FAMILY_USERNAME
    assert "password_hash" not in data["user"]


def test_login_wrong_password(client, family_user: User) -> None:
    """密码错误返回 401。"""
    response = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": "wrong-password"},
    )

    assert response.status_code == 401
    body = response.json()
    assert_unified_response(body, code=401)
    assert body["message"] == "用户名或密码错误"


def test_login_unknown_username_returns_same_message(client, family_user: User) -> None:
    """账号不存在与密码错误的提示一致，避免账号枚举。"""
    response = client.post(
        "/api/auth/login",
        json={"username": "not_exist_user", "password": "whatever123"},
    )

    assert response.status_code == 401
    assert response.json()["message"] == "用户名或密码错误"


def test_login_disabled_user(client, make_user) -> None:
    """已禁用账号登录返回 403。"""
    make_user(
        "disabled_user",
        "disabled123456",
        role=UserRole.FAMILY,
        status=UserStatus.DISABLED,
    )

    response = client.post(
        "/api/auth/login",
        json={"username": "disabled_user", "password": "disabled123456"},
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)
    assert "禁用" in response.json()["message"]


def test_login_records_last_login_at(client, db: Session, family_user: User) -> None:
    """登录成功后应记录最后登录时间。"""
    client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD},
    )

    db.rollback()
    refreshed = db.get(User, family_user.id)
    assert refreshed is not None and refreshed.last_login_at is not None


def test_login_rejects_unknown_field(client, family_user: User) -> None:
    """登录提交多余字段被拒绝。"""
    response = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD, "sms_code": "1234"},
    )

    body = response.json()
    assert_unified_response(body, code=400)


# ======================================================================
# 鉴权依赖
# ======================================================================
def test_profile_requires_token(client) -> None:
    """未携带令牌访问受保护接口返回 401。"""
    response = client.get("/api/auth/profile")

    assert response.status_code == 401
    body = response.json()
    assert_unified_response(body, code=401)
    assert "未提供认证令牌" in body["message"]


def test_profile_rejects_non_bearer_scheme(client, family_user: User) -> None:
    """非 Bearer 认证方式返回 401。"""
    response = client.get(
        "/api/auth/profile",
        headers={"Authorization": "Basic YWRtaW46YWRtaW4="},
    )

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_profile_rejects_tampered_token(client, family_user: User) -> None:
    """被篡改的令牌返回 401。"""
    token = create_access_token(family_user)
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")

    response = client.get("/api/auth/profile", headers=auth_headers(tampered))

    assert response.status_code == 401
    assert response.json()["message"] == "无效的认证令牌"


def test_profile_rejects_expired_token(client, family_user: User, monkeypatch) -> None:
    """过期令牌返回 401 且提示重新登录。"""
    monkeypatch.setattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", -1)
    expired_token = create_access_token(family_user)

    response = client.get("/api/auth/profile", headers=auth_headers(expired_token))

    assert response.status_code == 401
    assert "过期" in response.json()["message"]


def test_profile_rejects_refresh_token(client, family_user: User) -> None:
    """刷新令牌不能用于业务接口鉴权。"""
    response = client.get(
        "/api/auth/profile", headers=auth_headers(create_refresh_token(family_user))
    )

    assert response.status_code == 401
    assert response.json()["message"] == "令牌类型错误，请使用访问令牌"


def test_profile_success(client, family_user: User) -> None:
    """携带有效令牌可获取当前登录用户信息。"""
    login = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD},
    )
    token = login.json()["data"]["access_token"]

    response = client.get("/api/auth/profile", headers=auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["id"] == family_user.id
    assert body["data"]["username"] == FAMILY_USERNAME
    assert "password_hash" not in body["data"]


def test_profile_rejects_token_of_deleted_user(client, db: Session, family_user: User) -> None:
    """用户被删除后，其旧令牌立即失效。"""
    token = create_access_token(family_user)

    db.rollback()
    stored = db.get(User, family_user.id)
    assert stored is not None
    crud.delete_user(db, stored)

    response = client.get("/api/auth/profile", headers=auth_headers(token))

    assert response.status_code == 401
    assert "用户不存在" in response.json()["message"]


# ======================================================================
# 刷新令牌
# ======================================================================
def test_refresh_success(client, family_user: User) -> None:
    """刷新令牌可换取新的访问令牌，且新令牌可正常访问接口。"""
    login = client.post(
        "/api/auth/login",
        json={"username": FAMILY_USERNAME, "password": FAMILY_PASSWORD},
    )
    refresh_token_value = login.json()["data"]["refresh_token"]

    response = client.post(
        "/api/auth/refresh", json={"refresh_token": refresh_token_value}
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    new_access_token = body["data"]["access_token"]
    assert new_access_token

    profile = client.get("/api/auth/profile", headers=auth_headers(new_access_token))
    assert profile.json()["data"]["username"] == FAMILY_USERNAME


def test_refresh_rejects_access_token(client, family_user: User) -> None:
    """访问令牌不能用于刷新接口。"""
    response = client.post(
        "/api/auth/refresh",
        json={"refresh_token": create_access_token(family_user)},
    )

    assert response.status_code == 401
    assert response.json()["message"] == "令牌类型错误，请使用刷新令牌"


def test_refresh_rejects_invalid_token(client) -> None:
    """非法刷新令牌返回 401。"""
    response = client.post("/api/auth/refresh", json={"refresh_token": "not-a-jwt"})

    assert response.status_code == 401
    assert_unified_response(response.json(), code=401)


def test_refresh_rejects_disabled_user(client, db: Session, make_user) -> None:
    """账号被禁用后，刷新令牌也不能再换取访问令牌。"""
    user = make_user("disabled_refresh", "disabled123456", role=UserRole.FAMILY)
    refresh_token_value = create_refresh_token(user)

    db.rollback()
    stored = db.get(User, user.id)
    assert stored is not None
    stored.status = UserStatus.DISABLED
    db.commit()

    response = client.post(
        "/api/auth/refresh", json={"refresh_token": refresh_token_value}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)
    assert "禁用" in response.json()["message"]
