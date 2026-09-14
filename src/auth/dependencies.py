"""接口鉴权依赖。

提供三个可直接用于接口签名的依赖：

- :data:`CurrentUser`：要求已登录，返回当前用户 ORM 对象（可用于判断本人身份）
- :data:`AdminUser`：要求管理员角色
- :func:`require_roles`：按角色集合自定义限制，如 ``require_roles(UserRole.ADMIN, UserRole.STAFF)``

鉴权流程：

    读取 Authorization: Bearer <token>  →  解析并校验 JWT  →  校验令牌类型为 access
    →  查询用户  →  校验账号未被禁用  →  返回用户对象

任何一步失败都会抛出 :class:`~src.common.exceptions.BusinessError`，
由全局异常处理器转换为统一响应格式（401 / 403）。

用法示例::

    from src.auth.dependencies import AdminUser, CurrentUser

    @router.get("/profile")
    def profile(current_user: CurrentUser): ...

    @router.get("/users")
    def list_users(current_user: AdminUser): ...
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.auth.security import TokenType, decode_token, get_subject_id
from src.common.database import get_db
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.user import crud
from src.user.models import User, UserRole

__all__ = [
    "AdminUser",
    "CurrentUser",
    "StaffOrAdminUser",
    "bearer_scheme",
    "get_current_user",
    "get_token_payload",
    "require_roles",
]

#: Bearer 令牌安全方案（``auto_error=False``：缺少令牌时由本模块给出中文提示）
bearer_scheme = HTTPBearer(
    auto_error=False,
    bearerFormat="JWT",
    description="填写登录接口返回的 access_token 即可（Swagger 会自动加 Bearer 前缀）",
)


def get_token_payload(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> dict[str, Any]:
    """从请求头中解析并校验访问令牌。

    :raises BusinessError: 未提供令牌（401）或令牌无效/过期（401）
    """
    if credentials is None or not credentials.credentials:
        raise BusinessError(
            "未提供认证令牌，请先登录", code=ResponseCode.UNAUTHORIZED
        )
    return decode_token(credentials.credentials)


def get_current_user(
    payload: Annotated[dict[str, Any], Depends(get_token_payload)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """获取当前登录用户。

    :raises BusinessError: 令牌类型错误（401）、用户不存在（401）、账号被禁用（403）
    """
    if payload.get("type") != TokenType.ACCESS.value:
        raise BusinessError(
            "令牌类型错误，请使用访问令牌", code=ResponseCode.UNAUTHORIZED
        )

    user = crud.get_user_by_id(db, get_subject_id(payload))
    if user is None:
        raise BusinessError(
            "用户不存在或已被删除", code=ResponseCode.UNAUTHORIZED
        )
    if not user.is_active:
        raise BusinessError(
            "账号已被禁用，请联系管理员", code=ResponseCode.FORBIDDEN
        )
    return user


#: 要求登录，返回当前用户对象
CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    """构造角色校验依赖。

    :param roles: 允许访问的角色集合
    :return: FastAPI 依赖函数，返回当前用户对象
    """

    allowed = set(roles)
    allowed_label = " / ".join(role.label for role in roles)

    def checker(current_user: CurrentUser) -> User:
        if current_user.role not in allowed:
            raise BusinessError(
                f"权限不足：该操作仅限{allowed_label}",
                code=ResponseCode.FORBIDDEN,
            )
        return current_user

    checker.__doc__ = f"要求当前用户角色为：{allowed_label}"
    return checker


#: 要求管理员角色
AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]

#: 要求管理员或工作人员角色（查看家庭/成员名单等只读接口）
StaffOrAdminUser = Annotated[
    User, Depends(require_roles(UserRole.ADMIN, UserRole.STAFF))
]
