"""家庭成员业务逻辑层。

职责：

- 成员的增删改查与启用/停用；
- 归属判定：家庭用户只能操作本户成员，管理员可为任意家庭添加成员（需显式指定
  ``family_id``），工作人员只读；
- 身份证号唯一性校验，并在未提供性别/出生日期时按身份证号自动补齐（减少基层录入量）。

数据访问通过 :mod:`src.member.crud` 完成，家庭与权限规则复用 :mod:`src.family.service`。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.family import service as family_service
from src.family.models import Family
from src.member import crud
from src.member.id_card import parse_birth_date_and_gender
from src.member.models import FamilyMember, MemberStatus
from src.member.schemas import (
    MemberCreate,
    MemberOut,
    MemberStatusUpdate,
    MemberUpdate,
)
from src.user.models import User, UserRole

__all__ = [
    "create_member",
    "delete_member",
    "get_member",
    "list_members",
    "load_member",
    "resolve_family_id",
    "update_member",
    "update_status",
]

#: 可查看全部成员的角色（管理员管理数据，工作人员协助签到时需要名单）
_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def load_member(db: Session, member_id: int) -> FamilyMember:
    """获取成员 ORM 对象，不存在时抛出 404。"""
    member = crud.get_member_by_id(db, member_id)
    if member is None:
        raise BusinessError(f"家庭成员不存在：{member_id}", code=ResponseCode.NOT_FOUND)
    return member


def _ensure_id_card_available(
    db: Session, id_card: str | None, *, exclude_id: int | None = None
) -> None:
    """校验身份证号未被其他成员使用。"""
    if not id_card:
        return
    existing = crud.get_member_by_id_card(db, id_card)
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(
            f"身份证号已被其他成员使用：{id_card}", code=ResponseCode.CONFLICT
        )


def _apply_id_card_derived_fields(values: dict[str, Any]) -> None:
    """填写身份证号时自动补齐性别与出生日期（未显式提供时）。"""
    id_card = values.get("id_card")
    if not id_card:
        return
    birth_date, gender = parse_birth_date_and_gender(id_card)
    if values.get("birth_date") is None:
        values["birth_date"] = birth_date
    if values.get("gender") is None:
        values["gender"] = gender


def _load_member_for_user(
    db: Session, current_user: User, member_id: int, *, manage: bool
) -> FamilyMember:
    """加载成员并校验权限（manage=True 表示写操作）。"""
    member = load_member(db, member_id)
    family: Family = family_service.load_family(db, member.family_id)
    if manage:
        family_service.ensure_family_manageable(current_user, family)
    else:
        family_service.ensure_family_readable(current_user, family)
    return member


def resolve_family_id(
    db: Session, current_user: User, requested_family_id: int | None
) -> int:
    """确定新增成员的归属家庭。

    - 管理员：必须显式指定 ``family_id``；
    - 家庭用户：只能添加到本户（提交 ``family_id`` 会被拒绝）；
    - 工作人员：无权添加。
    """
    if current_user.is_admin:
        if requested_family_id is None:
            raise BusinessError(
                "管理员添加成员时必须指定 family_id", code=ResponseCode.PARAM_ERROR
            )
        return family_service.load_family(db, requested_family_id).id

    if current_user.role != UserRole.FAMILY:
        raise BusinessError(
            "权限不足：只有户主本人或管理员可以添加家庭成员",
            code=ResponseCode.FORBIDDEN,
        )

    if requested_family_id is not None:
        raise BusinessError(
            "权限不足：不能为其他家庭添加成员", code=ResponseCode.FORBIDDEN
        )

    family = family_service.get_own_family(db, current_user)
    family_service.ensure_family_manageable(current_user, family)
    return family.id


# ----------------------------------------------------------------------
# 新增
# ----------------------------------------------------------------------
def create_member(db: Session, current_user: User, data: MemberCreate) -> MemberOut:
    """添加家庭成员。"""
    family_id = resolve_family_id(db, current_user, data.family_id)
    family = family_service.load_family(db, family_id)
    if not family.is_active:
        raise BusinessError(
            "该家庭档案已停用，无法添加成员", code=ResponseCode.CONFLICT
        )

    values: dict[str, Any] = data.model_dump(exclude={"family_id"})
    _ensure_id_card_available(db, values.get("id_card"))
    _apply_id_card_derived_fields(values)

    member = crud.create_member(db, family_id=family_id, **values)
    return MemberOut.model_validate(member)


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_members(
    db: Session,
    current_user: User,
    *,
    family_id: int | None = None,
    status: MemberStatus | None = None,
    needs_checkin: bool | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[MemberOut], int]:
    """分页查询成员。

    - 管理员/工作人员：可查全部，可用 ``family_id`` 筛选；
    - 家庭用户：仅可查本户（传入他人 ``family_id`` 会被拒绝）。
    """
    if current_user.role in _READ_ALL_ROLES:
        target_family_id = family_id
    else:
        own_family = family_service.get_own_family(db, current_user)
        if family_id is not None and family_id != own_family.id:
            raise BusinessError(
                "权限不足：只能查看本家庭的成员", code=ResponseCode.FORBIDDEN
            )
        target_family_id = own_family.id

    members, total = crud.list_members(
        db,
        family_id=target_family_id,
        status=status,
        needs_checkin=needs_checkin,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return [MemberOut.model_validate(member) for member in members], total


def get_member(db: Session, current_user: User, member_id: int) -> MemberOut:
    """查询成员详情。"""
    member = _load_member_for_user(db, current_user, member_id, manage=False)
    return MemberOut.model_validate(member)


# ----------------------------------------------------------------------
# 修改
# ----------------------------------------------------------------------
def update_member(
    db: Session, current_user: User, member_id: int, data: MemberUpdate
) -> MemberOut:
    """修改成员信息（户主本人或管理员）。"""
    member = _load_member_for_user(db, current_user, member_id, manage=True)
    values = data.model_dump(exclude_unset=True)

    if "name" in values and values["name"] is None:
        raise BusinessError("成员姓名不能为空", code=ResponseCode.PARAM_ERROR)

    # 以下字段为 null 视为未修改（NOT NULL 列）
    for field in ("relation", "needs_checkin"):
        if field in values and values[field] is None:
            values.pop(field)

    if "id_card" in values:
        _ensure_id_card_available(db, values["id_card"], exclude_id=member.id)
    _apply_id_card_derived_fields(values)

    updated = crud.update_member(db, member, values)
    return MemberOut.model_validate(updated)


def update_status(
    db: Session, current_user: User, member_id: int, data: MemberStatusUpdate
) -> MemberOut:
    """启用/停用成员（户主本人或管理员）。"""
    member = _load_member_for_user(db, current_user, member_id, manage=True)
    updated = crud.update_member(db, member, {"status": data.status})
    return MemberOut.model_validate(updated)


# ----------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------
def delete_member(db: Session, current_user: User, member_id: int) -> None:
    """删除成员 = **停用**成员（户主本人或管理员）。

    阶段五引入签到记录后改为"停用而非物理删除"：

    - 签到记录（``checkin_record``）与请假记录（``leave_request``）都以
      ``family_member.id`` 为外键（``ON DELETE RESTRICT``），物理删除会破坏历史数据；
    - 停用后该成员不再参与签到，也不会在活动结束时被生成缺勤记录；
    - 人脸记录（``face_record``）**保留**：人脸识别时会过滤掉已停用成员，
      不会造成误识别；如需彻底清理云侧人脸，可先调用 ``POST /api/face/delete``；
    - 接口仍返回 200「删除成功」，成员在列表/详情中仍可见（``status=inactive``）。
    """
    member = _load_member_for_user(db, current_user, member_id, manage=True)
    if not member.is_active:
        raise BusinessError(
            f"成员「{member.name}」已处于停用状态", code=ResponseCode.CONFLICT
        )
    crud.update_member(db, member, {"status": MemberStatus.INACTIVE})
