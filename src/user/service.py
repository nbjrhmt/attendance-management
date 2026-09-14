"""用户业务逻辑层。

职责：

- 业务规则校验（用户名/手机号唯一、原密码校验、禁止自删自禁等）；
- 权限规则判断（本人或管理员）；
- 返回 DTO（:class:`~src.user.schemas.UserOut`），不向上层暴露 ORM 对象与密码字段。

数据访问统一通过 :mod:`src.user.crud` 完成。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.auth.security import verify_password
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.family import crud as family_crud
from src.user import crud
from src.user.models import User, UserRole, UserStatus
from src.user.schemas import (
    PasswordChange,
    PasswordReset,
    UserAdminUpdate,
    UserCreate,
    UserOut,
    UserRegister,
    UserStatusUpdate,
)

__all__ = [
    "change_password",
    "create_family_user",
    "create_user",
    "delete_user",
    "get_user",
    "list_users",
    "load_user",
    "reset_password",
    "update_status",
    "update_user",
]


# ----------------------------------------------------------------------
# 内部校验工具
# ----------------------------------------------------------------------
def load_user(db: Session, user_id: int) -> User:
    """获取用户 ORM 对象，不存在时抛出 404 业务异常。"""
    user = crud.get_user_by_id(db, user_id)
    if user is None:
        raise BusinessError(f"用户不存在：{user_id}", code=ResponseCode.NOT_FOUND)
    return user


def _ensure_username_available(
    db: Session, username: str, *, exclude_id: int | None = None
) -> None:
    """校验用户名未被占用。"""
    existing = crud.get_user_by_username(db, username)
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(
            f"用户名已被占用：{username}", code=ResponseCode.CONFLICT
        )


def _ensure_phone_available(
    db: Session, phone: str | None, *, exclude_id: int | None = None
) -> None:
    """校验手机号未被占用（为空时跳过）。"""
    if not phone:
        return
    existing = crud.get_user_by_phone(db, phone)
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(f"手机号已被注册：{phone}", code=ResponseCode.CONFLICT)


def _ensure_self_or_admin(current_user: User, target_user_id: int) -> None:
    """仅允许本人或管理员操作目标用户。"""
    if not current_user.is_admin and current_user.id != target_user_id:
        raise BusinessError(
            "权限不足：只能操作本人信息", code=ResponseCode.FORBIDDEN
        )


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_users(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 10,
    role: UserRole | None = None,
    status: UserStatus | None = None,
    keyword: str | None = None,
) -> tuple[list[UserOut], int]:
    """分页查询用户列表（仅管理员可调用，权限由接口层依赖保证）。"""
    users, total = crud.list_users(
        db, page=page, page_size=page_size, role=role, status=status, keyword=keyword
    )
    return [UserOut.model_validate(user) for user in users], total


def get_user(db: Session, current_user: User, user_id: int) -> UserOut:
    """查询用户详情：本人或管理员。"""
    _ensure_self_or_admin(current_user, user_id)
    return UserOut.model_validate(load_user(db, user_id))


# ----------------------------------------------------------------------
# 新增
# ----------------------------------------------------------------------
def create_user(db: Session, data: UserCreate) -> UserOut:
    """管理员新增用户（可指定角色与状态）。"""
    _ensure_username_available(db, data.username)
    _ensure_phone_available(db, data.phone)
    user = crud.create_user(
        db,
        username=data.username,
        password=data.password,
        real_name=data.real_name,
        phone=data.phone,
        role=data.role,
        status=data.status,
    )
    return UserOut.model_validate(user)


def create_family_user(
    db: Session, data: UserRegister, *, commit: bool = True
) -> User:
    """创建家庭用户（户主）账号，返回 ORM 对象。

    :param commit: 是否立即提交。注册流程传 ``False``，由 :mod:`src.auth.service`
        在同一事务内继续创建家庭档案与户主成员行，保证三者要么全部成功要么全部回滚
    """
    _ensure_username_available(db, data.username)
    _ensure_phone_available(db, data.phone)
    return crud.create_user(
        db,
        username=data.username,
        password=data.password,
        real_name=data.real_name,
        phone=data.phone,
        role=UserRole.FAMILY,
        status=UserStatus.ACTIVE,
        commit=commit,
    )


# ----------------------------------------------------------------------
# 修改
# ----------------------------------------------------------------------
def update_user(
    db: Session, current_user: User, user_id: int, data: UserAdminUpdate
) -> UserOut:
    """修改用户信息。

    - 管理员：可修改姓名、手机号、角色、状态；
    - 其他角色：只能修改本人的姓名与手机号，提交角色/状态会被拒绝。
    """
    _ensure_self_or_admin(current_user, user_id)
    target = load_user(db, user_id)

    values: dict[str, Any] = data.model_dump(exclude_unset=True)

    # 角色/状态为 null 视为未修改，避免写入 NOT NULL 列
    for field in ("role", "status"):
        if field in values and values[field] is None:
            values.pop(field)

    # 真实姓名不允许清空
    if "real_name" in values and values["real_name"] is None:
        raise BusinessError("真实姓名不能为空", code=ResponseCode.PARAM_ERROR)

    if not current_user.is_admin:
        for field, label in (("role", "角色"), ("status", "状态")):
            if field in values:
                raise BusinessError(
                    f"权限不足：不能修改用户{label}", code=ResponseCode.FORBIDDEN
                )

    if "phone" in values:
        _ensure_phone_available(db, values["phone"], exclude_id=target.id)

    updated = crud.update_user(db, target, values)
    return UserOut.model_validate(updated)


def change_password(
    db: Session, current_user: User, user_id: int, data: PasswordChange
) -> None:
    """本人修改密码：需校验原密码，且新密码不能与原密码相同。"""
    if current_user.id != user_id:
        raise BusinessError(
            "权限不足：只能修改本人密码，重置他人密码请使用重置接口",
            code=ResponseCode.FORBIDDEN,
        )

    target = load_user(db, user_id)
    if not verify_password(data.old_password, target.password_hash):
        raise BusinessError("原密码不正确", code=ResponseCode.PARAM_ERROR)
    if verify_password(data.new_password, target.password_hash):
        raise BusinessError(
            "新密码不能与原密码相同", code=ResponseCode.PARAM_ERROR
        )

    crud.set_password(db, target, data.new_password)


def reset_password(db: Session, user_id: int, data: PasswordReset) -> None:
    """管理员重置密码（无需原密码，权限由接口层依赖保证）。"""
    target = load_user(db, user_id)
    crud.set_password(db, target, data.new_password)


def update_status(
    db: Session, current_user: User, user_id: int, data: UserStatusUpdate
) -> UserOut:
    """启用/禁用用户（仅管理员，且不允许操作自己）。"""
    target = load_user(db, user_id)
    if target.id == current_user.id:
        raise BusinessError(
            "不能修改自己的账号状态", code=ResponseCode.PARAM_ERROR
        )
    updated = crud.update_user(db, target, {"status": data.status})
    return UserOut.model_validate(updated)


# ----------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------
def delete_user(db: Session, current_user: User, user_id: int) -> None:
    """删除用户（仅管理员，且不允许删除自己）。

    若该用户是家庭户主，需先处理其家庭档案（停用或删除家庭），
    否则会触发 ``family.owner_id`` 的外键约束。
    """
    target = load_user(db, user_id)
    if target.id == current_user.id:
        raise BusinessError("不能删除自己的账号", code=ResponseCode.PARAM_ERROR)

    family = family_crud.get_family_by_owner(db, target.id)
    if family is not None:
        raise BusinessError(
            f"该账号是家庭户主（户号 {family.household_no}），请先停用其家庭档案",
            code=ResponseCode.CONFLICT,
        )

    crud.delete_user(db, target)
