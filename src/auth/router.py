"""认证接口路由（挂载前缀 ``/api/auth``）。

| 方法 | 路径 | 说明 | 是否需要登录 |
| --- | --- | --- | --- |
| POST | ``/api/auth/register`` | 家庭用户（户主）注册 | 否 |
| POST | ``/api/auth/login`` | 账号密码登录，返回 access_token 与 refresh_token | 否 |
| POST | ``/api/auth/refresh`` | 使用 refresh_token 换取新的访问令牌 | 否 |
| GET  | ``/api/auth/profile`` | 获取当前登录用户信息 | 是 |

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.auth import service as auth_service
from src.auth.dependencies import CurrentUser
from src.auth.schemas import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse
from src.user.schemas import UserOut

router = APIRouter()


@router.post(
    "/register",
    response_model=ApiResponse[UserOut],
    summary="家庭用户（户主）注册",
    description=(
        "家庭以户主为单位注册。注册成功后角色固定为 family（家庭用户），"
        "系统会在同一事务内自动创建家庭档案（户号形如 F000123）与户主成员记录；"
        "可在请求体的 family 字段中携带住址、村组等初始信息（可选）。"
        "注册完成后请调用登录接口获取令牌。"
    ),
)
def register(
    data: RegisterRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """注册家庭用户账号并自动建立家庭档案。"""
    user = auth_service.register(db, data)
    return success_response(data=user, message="注册成功，已自动创建家庭档案")


@router.post(
    "/login",
    response_model=ApiResponse[TokenResponse],
    summary="登录",
    description=(
        "使用用户名与密码登录。成功后返回 access_token（默认 120 分钟）与 "
        "refresh_token（默认 7 天）。后续请求在请求头中携带 "
        "``Authorization: Bearer <access_token>``。"
    ),
)
def login(
    data: LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """登录并签发令牌。"""
    tokens = auth_service.login(db, data)
    return success_response(data=tokens, message="登录成功")


@router.post(
    "/refresh",
    response_model=ApiResponse[TokenResponse],
    summary="刷新访问令牌",
    description=(
        "使用 refresh_token 换取新的访问令牌（滑动续期）。"
        "返回值中同时包含新的 refresh_token，客户端应覆盖保存。"
    ),
)
def refresh(
    data: RefreshRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """刷新令牌。"""
    tokens = auth_service.refresh_token(db, data.refresh_token)
    return success_response(data=tokens, message="刷新成功")


@router.get(
    "/profile",
    response_model=ApiResponse[UserOut],
    summary="获取当前登录用户信息",
    description="根据请求头中的访问令牌返回当前登录用户信息（不含密码字段）。",
)
def get_profile(current_user: CurrentUser) -> dict[str, Any]:
    """获取当前登录用户信息。"""
    return success_response(data=UserOut.model_validate(current_user))
