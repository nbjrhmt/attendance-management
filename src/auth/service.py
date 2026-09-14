"""认证业务逻辑：注册、登录、令牌刷新。

本模块负责组合 :mod:`src.user.crud`（数据访问）、:mod:`src.auth.security`（密码与令牌）
和 :mod:`src.user.service`（用户业务校验），对外提供认证能力。
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.schemas import LoginRequest, RegisterRequest, TokenResponse
from src.auth.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_access_token_expire_seconds,
    get_subject_id,
    verify_password,
)
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.family import service as family_service
from src.user import crud
from src.user import service as user_service
from src.user.models import User
from src.user.schemas import UserOut

__all__ = ["build_token_response", "login", "refresh_token", "register"]


def _load_active_user(db: Session, user_id: int) -> User:
    """加载用户并校验可用状态（令牌刷新、登录鉴权共用）。"""
    user = crud.get_user_by_id(db, user_id)
    if user is None:
        raise BusinessError(
            "用户不存在或已被删除", code=ResponseCode.UNAUTHORIZED
        )
    if not user.is_active:
        raise BusinessError(
            "账号已被禁用，请联系管理员", code=ResponseCode.FORBIDDEN
        )
    return user


def build_token_response(user: User) -> TokenResponse:
    """由用户对象构造令牌响应。"""
    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
        token_type="bearer",
        expires_in=get_access_token_expire_seconds(),
        user=UserOut.model_validate(user),
    )


def register(db: Session, data: RegisterRequest) -> UserOut:
    """家庭用户（户主）注册：同一事务内创建账号 + 家庭档案 + 户主成员记录。

    三步写入共用一次提交：任一步失败整体回滚，避免出现"有账号却没有家庭档案"
    或"有家庭档案却没有户主"的脏数据。

    :param data: 账号信息，``family`` 字段可选携带初始家庭档案信息
    """
    try:
        user = user_service.create_family_user(db, data, commit=False)
        family_service.ensure_family_for_owner(db, user, data.family, commit=False)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "用户名、手机号或户号已被占用", code=ResponseCode.CONFLICT
        ) from exc

    db.refresh(user)
    return UserOut.model_validate(user)


def login(db: Session, data: LoginRequest) -> TokenResponse:
    """账号密码登录，成功后返回访问令牌与刷新令牌。

    用户名不存在与密码错误返回相同的提示，避免账号枚举。
    """
    user = crud.get_user_by_username(db, data.username)
    if user is None or not verify_password(data.password, user.password_hash):
        raise BusinessError(
            "用户名或密码错误", code=ResponseCode.UNAUTHORIZED
        )
    if not user.is_active:
        raise BusinessError(
            "账号已被禁用，请联系管理员", code=ResponseCode.FORBIDDEN
        )

    crud.touch_last_login(db, user)
    return build_token_response(user)


def refresh_token(db: Session, token: str) -> TokenResponse:
    """使用刷新令牌换取新的访问令牌（滑动续期，同时返回新的刷新令牌）。"""
    payload = decode_token(token)
    if payload.get("type") != TokenType.REFRESH.value:
        raise BusinessError(
            "令牌类型错误，请使用刷新令牌", code=ResponseCode.UNAUTHORIZED
        )

    user = _load_active_user(db, get_subject_id(payload))
    return build_token_response(user)
