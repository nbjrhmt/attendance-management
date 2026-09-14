"""用户数据访问层（CRUD）。

本层只负责与数据库交互，不做权限判断与业务校验（由 :mod:`src.user.service` 负责），
所有写操作内部完成 ``commit`` 与 ``refresh``，调用方拿到的是可用状态的对象。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth.security import hash_password
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.user.models import User, UserRole, UserStatus

__all__ = [
    "create_user",
    "delete_user",
    "get_user_by_id",
    "get_user_by_phone",
    "get_user_by_username",
    "list_users",
    "set_password",
    "touch_last_login",
    "update_user",
]


def _escape_like(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入的 ``%`` / ``_`` 被当作通配符。"""
    return (
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def get_user_by_id(db: Session, user_id: int) -> User | None:
    """按主键查询用户。"""
    return db.get(User, user_id)


def get_user_by_username(db: Session, username: str) -> User | None:
    """按用户名查询用户（用户名唯一）。"""
    return db.scalar(select(User).where(User.username == username))


def get_user_by_phone(db: Session, phone: str) -> User | None:
    """按手机号查询用户（手机号唯一）。"""
    return db.scalar(select(User).where(User.phone == phone))


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    real_name: str,
    phone: str | None = None,
    role: UserRole = UserRole.FAMILY,
    status: UserStatus = UserStatus.ACTIVE,
    commit: bool = True,
) -> User:
    """新增用户，密码以 bcrypt 哈希形式存储。

    :param commit: 是否立即提交。为 ``False`` 时仅 ``flush``（取得自增主键），
        由调用方统一提交，用于"注册时同时创建家庭档案"这类需要跨模块原子写入的场景
    用户名/手机号的友好提示由服务层提前检查，这里捕获唯一约束冲突作为兜底
    （防止并发注册时的竞态）。
    """
    user = User(
        username=username,
        password_hash=hash_password(password),
        real_name=real_name,
        phone=phone,
        role=role,
        status=status,
    )
    db.add(user)
    if not commit:
        db.flush()
        return user

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "用户名或手机号已被占用", code=ResponseCode.CONFLICT
        ) from exc
    db.refresh(user)
    return user


def list_users(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 10,
    role: UserRole | None = None,
    status: UserStatus | None = None,
    keyword: str | None = None,
) -> tuple[list[User], int]:
    """分页查询用户列表。

    :param keyword: 模糊匹配用户名 / 真实姓名 / 手机号
    :return: ``(当前页用户列表, 总记录数)``
    """
    conditions = []
    if role is not None:
        conditions.append(User.role == role)
    if status is not None:
        conditions.append(User.status == status)
    if keyword:
        pattern = f"%{_escape_like(keyword.strip())}%"
        conditions.append(
            or_(
                User.username.like(pattern, escape="\\"),
                User.real_name.like(pattern, escape="\\"),
                User.phone.like(pattern, escape="\\"),
            )
        )

    total = db.scalar(select(func.count()).select_from(User).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(User)
            .where(*conditions)
            .order_by(User.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, int(total)


def update_user(db: Session, user: User, values: dict[str, Any]) -> User:
    """按字段字典更新用户（只更新传入的字段）。"""
    for field, value in values.items():
        setattr(user, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "用户名或手机号已被占用", code=ResponseCode.CONFLICT
        ) from exc
    db.refresh(user)
    return user


def set_password(db: Session, user: User, new_password: str) -> User:
    """设置新密码（bcrypt 哈希后存储）。"""
    user.password_hash = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user


def touch_last_login(db: Session, user: User) -> User:
    """记录最后登录时间。"""
    user.last_login_at = datetime.now()
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user: User) -> None:
    """物理删除用户。"""
    db.delete(user)
    db.commit()
