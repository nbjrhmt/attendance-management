"""安全工具：密码哈希（bcrypt）与 JWT 令牌签发/解析。

密码：

- 使用 ``passlib`` 的 bcrypt 方案，数据库中只保存 60 字符的哈希串；
- bcrypt 只处理前 72 字节，超长密码在 schema 层已被拦截（见 ``src/user/schemas.py``）。

令牌：

- 采用 JWT（HS256），载荷包含 ``sub``（用户ID）、``username``、``role``、
  ``type``（access / refresh）、``iat``、``exp``、``jti``；
- ``access_token`` 用于接口鉴权，``refresh_token`` 仅用于换取新的访问令牌；
  鉴权依赖会校验 ``type``，防止用刷新令牌直接访问业务接口；
- JWT 中的时间遵循 JWT 规范使用 UTC 时间戳（数据库时间使用服务器本地时间，两者不混用）。

用法示例::

    from src.auth.security import create_access_token, decode_token, hash_password

    hashed = hash_password("123456")
    token = create_access_token(user)
    payload = decode_token(token)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
from passlib.context import CryptContext

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import settings
from src.user.models import User

__all__ = [
    "TokenType",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "get_access_token_expire_seconds",
    "get_subject_id",
    "hash_password",
    "pwd_context",
    "verify_password",
]

#: 密码哈希上下文（bcrypt；``deprecated="auto"`` 便于后续平滑升级算法）
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenType(str, Enum):
    """令牌类型。"""

    ACCESS = "access"
    REFRESH = "refresh"


def hash_password(password: str) -> str:
    """生成 bcrypt 密码哈希。"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配。

    哈希串被人工改坏等异常情况一律返回 ``False``，不向上抛出异常。
    """
    try:
        return pwd_context.verify(plain_password, password_hash)
    except ValueError:
        return False


def get_access_token_expire_seconds() -> int:
    """访问令牌有效期（秒），用于响应中的 ``expires_in`` 字段。"""
    return settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _role_value(role: Any) -> str:
    """取角色字符串值（兼容枚举与普通字符串）。"""
    return role.value if isinstance(role, Enum) else str(role)


def _create_token(user: User, token_type: TokenType, expires_delta: timedelta) -> str:
    """签发令牌。

    :param user: 用户 ORM 对象
    :param token_type: 令牌类型
    :param expires_delta: 有效期
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "username": user.username,
        "role": _role_value(user.role),
        "type": token_type.value,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid4().hex,
    }
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_access_token(user: User) -> str:
    """签发访问令牌。"""
    return _create_token(
        user,
        TokenType.ACCESS,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(user: User) -> str:
    """签发刷新令牌。"""
    return _create_token(
        user,
        TokenType.REFRESH,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str) -> dict[str, Any]:
    """解析并校验令牌。

    :raises BusinessError: 令牌过期（401）或无效（401）
    :return: 令牌载荷
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except ExpiredSignatureError as exc:
        raise BusinessError(
            "登录已过期，请重新登录", code=ResponseCode.UNAUTHORIZED
        ) from exc
    except JWTError as exc:
        raise BusinessError(
            "无效的认证令牌", code=ResponseCode.UNAUTHORIZED
        ) from exc

    if not payload.get("sub"):
        raise BusinessError("无效的认证令牌", code=ResponseCode.UNAUTHORIZED)
    return payload


def get_subject_id(payload: dict[str, Any]) -> int:
    """从令牌载荷中取出用户ID。"""
    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(
            "无效的认证令牌", code=ResponseCode.UNAUTHORIZED
        ) from exc
