"""用户管理接口路由（挂载前缀 ``/api/users``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | ``/api/users`` | 用户列表（分页、角色/状态筛选、关键字搜索） | 管理员 |
| POST | ``/api/users`` | 新增用户（可指定角色与状态） | 管理员 |
| GET | ``/api/users/{user_id}`` | 用户详情 | 本人或管理员 |
| PUT | ``/api/users/{user_id}`` | 修改用户信息 | 本人（仅姓名/手机号）或管理员（全部） |
| PUT | ``/api/users/{user_id}/password`` | 修改本人密码（需原密码） | 本人 |
| PUT | ``/api/users/{user_id}/reset-password`` | 重置密码（无需原密码） | 管理员 |
| PUT | ``/api/users/{user_id}/status`` | 启用/禁用账号 | 管理员（不可操作自己） |
| DELETE | ``/api/users/{user_id}`` | 删除账号 | 管理员（不可删除自己） |

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser, CurrentUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.user import service as user_service
from src.user.models import UserRole, UserStatus
from src.user.schemas import (
    PasswordChange,
    PasswordReset,
    UserAdminUpdate,
    UserCreate,
    UserOut,
    UserStatusUpdate,
)

router = APIRouter()


@router.get(
    "",
    response_model=ApiResponse[PageData[UserOut]],
    summary="用户列表",
    description="分页查询用户，支持按角色、状态筛选与关键字（用户名/姓名/手机号）搜索。仅管理员可访问。",
)
def list_users(
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
    role: Annotated[UserRole | None, Query(description="角色筛选")] = None,
    status: Annotated[UserStatus | None, Query(description="状态筛选")] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="用户名/姓名/手机号模糊搜索")
    ] = None,
) -> dict[str, Any]:
    """分页查询用户列表。"""
    users, total = user_service.list_users(
        db,
        page=page,
        page_size=page_size,
        role=role,
        status=status,
        keyword=keyword,
    )
    return success_response(
        data=PageData[UserOut](
            total=total, page=page, page_size=page_size, items=users
        )
    )


@router.post(
    "",
    response_model=ApiResponse[UserOut],
    summary="新增用户",
    description="管理员新增用户，可指定角色（admin/staff/family）与状态。仅管理员可访问。",
)
def create_user(
    data: UserCreate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """新增用户。"""
    user = user_service.create_user(db, data)
    return success_response(data=user, message="创建成功")


@router.get(
    "/{user_id}",
    response_model=ApiResponse[UserOut],
    summary="用户详情",
    description="查询指定用户信息，本人或管理员可访问。",
)
def get_user(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询用户详情。"""
    return success_response(data=user_service.get_user(db, current_user, user_id))


@router.put(
    "/{user_id}",
    response_model=ApiResponse[UserOut],
    summary="修改用户信息",
    description=(
        "管理员可修改姓名、手机号、角色、状态；"
        "其他角色仅能修改本人的姓名与手机号，提交 role/status 会被拒绝（403）。"
    ),
)
def update_user(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    data: UserAdminUpdate,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修改用户信息。"""
    user = user_service.update_user(db, current_user, user_id, data)
    return success_response(data=user, message="更新成功")


@router.put(
    "/{user_id}/password",
    response_model=ApiResponse[dict],
    summary="修改本人密码",
    description="需提供原密码；仅能修改本人密码，管理员重置他人密码请使用重置接口。",
)
def change_password(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    data: PasswordChange,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修改本人密码。"""
    user_service.change_password(db, current_user, user_id, data)
    return success_response(message="密码修改成功")


@router.put(
    "/{user_id}/reset-password",
    response_model=ApiResponse[dict],
    summary="重置用户密码",
    description="管理员重置指定用户密码，无需原密码。仅管理员可访问。",
)
def reset_password(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    data: PasswordReset,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """管理员重置密码。"""
    user_service.reset_password(db, user_id, data)
    return success_response(message="密码已重置")


@router.put(
    "/{user_id}/status",
    response_model=ApiResponse[UserOut],
    summary="启用/禁用账号",
    description="管理员启用或禁用指定账号，不允许操作自己的账号。",
)
def update_status(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    data: UserStatusUpdate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """启用/禁用账号。"""
    user = user_service.update_status(db, current_user, user_id, data)
    message = "账号已启用" if data.status == UserStatus.ACTIVE else "账号已禁用"
    return success_response(data=user, message=message)


@router.delete(
    "/{user_id}",
    response_model=ApiResponse[dict],
    summary="删除账号",
    description="管理员删除指定账号，不允许删除自己的账号。",
)
def delete_user(
    user_id: Annotated[int, Path(ge=1, description="用户ID")],
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """删除账号。"""
    user_service.delete_user(db, current_user, user_id)
    return success_response(message="删除成功")
