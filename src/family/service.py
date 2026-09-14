"""家庭业务逻辑层。

职责：

- 家庭档案的创建、查询、修改与停用；
- 权限规则：管理员可管理全部家庭；工作人员只读（协助签到时需要查看名单）；
  家庭用户只能读写**本人**的家庭；
- 户主注册时的自动建档入口 :func:`ensure_family_for_owner`（由
  :mod:`src.auth.service` 的注册流程调用），在同一事务内创建"家庭档案 + 户主成员行"。

数据访问通过 :mod:`src.family.crud` 与 :mod:`src.member.crud` 完成。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.family import crud
from src.family.models import Family, FamilyStatus
from src.family.schemas import (
    FamilyCreate,
    FamilyDetailOut,
    FamilyOut,
    FamilyRegisterInfo,
    FamilyUpdate,
)
from src.member import crud as member_crud
from src.member.models import FamilyMember, MemberRelation
from src.member.schemas import MemberOut
from src.user.models import User, UserRole

__all__ = [
    "create_family",
    "deactivate_family",
    "ensure_family_for_owner",
    "ensure_family_manageable",
    "ensure_family_readable",
    "get_family_detail",
    "get_my_family",
    "get_own_family",
    "list_families",
    "load_family",
    "update_family",
]

#: 可查看全部家庭档案的角色（管理员管理数据，工作人员协助签到时需要名单）
_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def load_family(db: Session, family_id: int) -> Family:
    """获取家庭 ORM 对象，不存在时抛出 404。"""
    family = crud.get_family_by_id(db, family_id)
    if family is None:
        raise BusinessError(f"家庭档案不存在：{family_id}", code=ResponseCode.NOT_FOUND)
    return family


def get_own_family(db: Session, current_user: User) -> Family:
    """获取当前登录用户（户主）的家庭档案。"""
    family = crud.get_family_by_owner(db, current_user.id)
    if family is None:
        raise BusinessError(
            "当前账号尚无家庭档案，请联系管理员创建", code=ResponseCode.NOT_FOUND
        )
    return family


def _ensure_household_no_available(
    db: Session, household_no: str | None, *, exclude_id: int | None = None
) -> None:
    """校验户号未被占用。"""
    if not household_no:
        return
    existing = crud.get_family_by_household_no(db, household_no)
    if existing is not None and existing.id != exclude_id:
        raise BusinessError(
            f"户号已被占用：{household_no}", code=ResponseCode.CONFLICT
        )


def _load_owner(db: Session, owner_id: int) -> User:
    """加载户主账号并校验角色。"""
    owner = db.get(User, owner_id)
    if owner is None:
        raise BusinessError(f"用户不存在：{owner_id}", code=ResponseCode.NOT_FOUND)
    if owner.role != UserRole.FAMILY:
        raise BusinessError(
            f"该用户角色为{owner.role.label}，只有家庭用户（户主）才能拥有家庭档案",
            code=ResponseCode.PARAM_ERROR,
        )
    return owner


def ensure_householder_member(
    db: Session,
    family: Family,
    owner: User,
    *,
    commit: bool = True,
) -> FamilyMember:
    """确保家庭中存在"户主本人"的成员记录（幂等）。

    户主本人也是家庭的一员，需要在签到中被统计，因此建档时自动生成该成员行，
    并把 ``user_id`` 关联到登录账号。
    """
    existing = member_crud.get_householder_member(db, family.id)
    if existing is not None:
        if existing.user_id is None:
            existing.user_id = owner.id
            if commit:
                db.commit()
            else:
                db.flush()
        return existing

    return member_crud.create_member(
        db,
        family_id=family.id,
        user_id=owner.id,
        name=owner.real_name,
        relation=MemberRelation.HOUSEHOLDER,
        phone=owner.phone,
        needs_checkin=True,
        commit=commit,
    )


def _to_out(family: Family, member_count: int, checkin_count: int) -> FamilyOut:
    """ORM 家庭对象 -> 响应 DTO。"""
    owner = family.owner
    return FamilyOut(
        id=family.id,
        household_no=family.household_no,
        owner_id=family.owner_id,
        owner_name=owner.real_name,
        owner_username=owner.username,
        owner_phone=owner.phone,
        address=family.address,
        village=family.village,
        contact_phone=family.contact_phone,
        remark=family.remark,
        status=family.status,
        member_count=member_count,
        checkin_required_count=checkin_count,
        created_at=family.created_at,
        updated_at=family.updated_at,
    )


def _to_detail(db: Session, family: Family) -> FamilyDetailOut:
    """ORM 家庭对象 -> 含成员列表的响应 DTO。"""
    member_count, checkin_count = crud.count_members(db, family.id)
    members = member_crud.list_all_members(db, family.id)
    base = _to_out(family, member_count, checkin_count)
    return FamilyDetailOut(
        **base.model_dump(),
        members=[MemberOut.model_validate(member) for member in members],
    )


# ----------------------------------------------------------------------
# 权限规则
# ----------------------------------------------------------------------
def ensure_family_readable(current_user: User, family: Family) -> None:
    """校验可读：管理员/工作人员可读全部，家庭用户仅可读本户。"""
    if current_user.role in _READ_ALL_ROLES:
        return
    if family.owner_id != current_user.id:
        raise BusinessError(
            "权限不足：只能查看本人家庭的信息", code=ResponseCode.FORBIDDEN
        )


def ensure_family_manageable(current_user: User, family: Family) -> None:
    """校验可写：管理员可写全部，家庭用户仅可写本户，工作人员不可写。"""
    if current_user.is_admin:
        return
    if current_user.role != UserRole.FAMILY or family.owner_id != current_user.id:
        raise BusinessError(
            "权限不足：只有户主本人或管理员可以维护家庭信息",
            code=ResponseCode.FORBIDDEN,
        )


# ----------------------------------------------------------------------
# 建档
# ----------------------------------------------------------------------
def ensure_family_for_owner(
    db: Session,
    owner: User,
    info: FamilyRegisterInfo | None = None,
    *,
    commit: bool = True,
) -> Family:
    """确保户主拥有家庭档案（幂等），并同步生成户主成员行。

    户主注册（:func:`src.auth.service.register`）与管理员建档共用此入口。

    :param info: 可选的初始家庭信息，全部留空时自动生成户号
    :param commit: 是否立即提交；注册流程传 ``False`` 以便与账号创建同事务提交
    """
    family = crud.get_family_by_owner(db, owner.id)
    if family is None:
        values = (info or FamilyRegisterInfo()).model_dump()
        _ensure_household_no_available(db, values.get("household_no"))
        family = crud.create_family(
            db, owner_id=owner.id, commit=commit, **values
        )

    ensure_householder_member(db, family, owner, commit=commit)
    return family


def create_family(db: Session, data: FamilyCreate) -> FamilyOut:
    """管理员为指定户主创建家庭档案。"""
    owner = _load_owner(db, data.owner_id)
    if crud.get_family_by_owner(db, owner.id) is not None:
        raise BusinessError("该户主已有家庭档案", code=ResponseCode.CONFLICT)
    _ensure_household_no_available(db, data.household_no)

    family = crud.create_family(
        db,
        owner_id=owner.id,
        household_no=data.household_no,
        address=data.address,
        village=data.village,
        contact_phone=data.contact_phone,
        remark=data.remark,
    )
    ensure_householder_member(db, family, owner)

    member_count, checkin_count = crud.count_members(db, family.id)
    return _to_out(family, member_count, checkin_count)


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_families(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 10,
    status: FamilyStatus | None = None,
    village: str | None = None,
    keyword: str | None = None,
) -> tuple[list[FamilyOut], int]:
    """分页查询家庭列表（权限由接口层依赖保证：管理员/工作人员）。"""
    rows, total = crud.list_families(
        db,
        page=page,
        page_size=page_size,
        status=status,
        village=village,
        keyword=keyword,
    )
    return [_to_out(family, member_count, checkin_count) for family, member_count, checkin_count in rows], total


def get_family_detail(db: Session, current_user: User, family_id: int) -> FamilyDetailOut:
    """查询家庭详情（含成员列表）。"""
    family = load_family(db, family_id)
    ensure_family_readable(current_user, family)
    return _to_detail(db, family)


def get_my_family(db: Session, current_user: User) -> FamilyDetailOut:
    """查询当前登录户主的家庭详情。"""
    return _to_detail(db, get_own_family(db, current_user))


# ----------------------------------------------------------------------
# 修改与停用
# ----------------------------------------------------------------------
def update_family(
    db: Session, current_user: User, family_id: int, data: FamilyUpdate
) -> FamilyOut:
    """修改家庭信息（户主本人或管理员）。"""
    family = load_family(db, family_id)
    ensure_family_manageable(current_user, family)

    values = data.model_dump(exclude_unset=True)

    if "household_no" in values:
        if values["household_no"] is None:
            raise BusinessError("户号不能为空", code=ResponseCode.PARAM_ERROR)
        _ensure_household_no_available(db, values["household_no"], exclude_id=family.id)

    # 状态为 null 视为未修改
    if "status" in values and values["status"] is None:
        values.pop("status")

    updated = crud.update_family(db, family, values)
    member_count, checkin_count = crud.count_members(db, updated.id)
    return _to_out(updated, member_count, checkin_count)


def deactivate_family(db: Session, current_user: User, family_id: int) -> int:
    """停用家庭档案（仅管理员），并级联停用其正常成员。

    采用停用而非物理删除，便于保留家庭与成员的历史签到数据。

    :return: 级联停用的成员数量
    """
    family = load_family(db, family_id)
    if not current_user.is_admin:
        raise BusinessError(
            "权限不足：停用家庭档案仅限管理员", code=ResponseCode.FORBIDDEN
        )
    if not family.is_active:
        raise BusinessError("该家庭已处于停用状态", code=ResponseCode.CONFLICT)

    crud.update_family(db, family, {"status": FamilyStatus.INACTIVE})
    return member_crud.deactivate_by_family(db, family.id)
