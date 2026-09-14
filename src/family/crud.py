"""家庭数据访问层（CRUD）。

本层只负责数据库读写：不做权限判断与业务校验（见 :mod:`src.family.service`）。
写操作默认内部完成 ``commit`` 与 ``refresh``；传入 ``commit=False`` 时仅 ``flush``，
由调用方统一提交（用于户主注册时"账号 + 家庭 + 户主成员"的原子写入）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.common.utils import like_pattern
from src.family.models import Family, FamilyStatus
from src.member.models import FamilyMember, MemberStatus
from src.user.models import User

__all__ = [
    "count_members",
    "create_family",
    "get_family_by_household_no",
    "get_family_by_id",
    "get_family_by_owner",
    "list_families",
    "update_family",
]


def _member_count_subquery(*, needs_checkin: bool | None = None):
    """构造"有效成员数"相关子查询（按家庭聚合，避免 N+1）。"""
    conditions = [
        FamilyMember.family_id == Family.id,
        FamilyMember.status == MemberStatus.ACTIVE,
    ]
    if needs_checkin is not None:
        conditions.append(FamilyMember.needs_checkin.is_(needs_checkin))

    return (
        select(func.count(FamilyMember.id))
        .where(*conditions)
        .correlate(Family)
        .scalar_subquery()
    )


def get_family_by_id(db: Session, family_id: int) -> Family | None:
    """按主键查询家庭。"""
    return db.get(Family, family_id)


def get_family_by_owner(db: Session, owner_id: int) -> Family | None:
    """按户主用户ID查询家庭（一户主一家庭）。"""
    return db.scalar(select(Family).where(Family.owner_id == owner_id))


def get_family_by_household_no(db: Session, household_no: str) -> Family | None:
    """按户号查询家庭（户号唯一）。"""
    return db.scalar(select(Family).where(Family.household_no == household_no))


def create_family(
    db: Session,
    *,
    owner_id: int,
    household_no: str | None = None,
    address: str | None = None,
    village: str | None = None,
    contact_phone: str | None = None,
    remark: str | None = None,
    status: FamilyStatus = FamilyStatus.ACTIVE,
    commit: bool = True,
) -> Family:
    """创建家庭档案。

    未指定 ``household_no`` 时：先 ``flush`` 取得自增主键，再回填 ``F`` + 6 位序号
    （形如 ``F000123``），因此提交到数据库的家庭一定有户号。

    :param commit: 是否立即提交；为 ``False`` 时仅 ``flush``，由调用方统一提交
    """
    family = Family(
        owner_id=owner_id,
        household_no=household_no,
        address=address,
        village=village,
        contact_phone=contact_phone,
        remark=remark,
        status=status,
    )
    db.add(family)
    try:
        if household_no is None:
            db.flush()
            family.household_no = f"F{family.id:06d}"

        if not commit:
            db.flush()
            return family

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "该户主已有家庭档案，或户号已被占用", code=ResponseCode.CONFLICT
        ) from exc

    db.refresh(family)
    return family


def update_family(db: Session, family: Family, values: dict[str, Any]) -> Family:
    """按字段字典更新家庭（只更新传入的字段）。"""
    for field, value in values.items():
        setattr(family, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError("户号已被占用", code=ResponseCode.CONFLICT) from exc
    db.refresh(family)
    return family


def list_families(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 10,
    status: FamilyStatus | None = None,
    village: str | None = None,
    keyword: str | None = None,
) -> tuple[list[tuple[Family, int, int]], int]:
    """分页查询家庭列表。

    :param keyword: 模糊匹配户号 / 住址 / 村组 / 户主姓名 / 户主用户名
    :return: ``([(家庭, 有效成员数, 需签到成员数), ...], 总记录数)``
    """
    conditions = []
    if status is not None:
        conditions.append(Family.status == status)
    if village:
        conditions.append(Family.village == village)
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                Family.household_no.like(pattern, escape="\\"),
                Family.address.like(pattern, escape="\\"),
                Family.village.like(pattern, escape="\\"),
                User.real_name.like(pattern, escape="\\"),
                User.username.like(pattern, escape="\\"),
            )
        )

    total = (
        db.scalar(
            select(func.count())
            .select_from(Family)
            .join(User, User.id == Family.owner_id)
            .where(*conditions)
        )
        or 0
    )

    rows = db.execute(
        select(
            Family,
            _member_count_subquery().label("member_count"),
            _member_count_subquery(needs_checkin=True).label("checkin_required_count"),
        )
        .join(User, User.id == Family.owner_id)
        .where(*conditions)
        .options(selectinload(Family.owner))
        .order_by(Family.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items = [(row[0], int(row[1] or 0), int(row[2] or 0)) for row in rows]
    return items, int(total)


def count_members(db: Session, family_id: int) -> tuple[int, int]:
    """统计家庭的有效成员数与需签到成员数。

    :return: ``(有效成员数, 需签到成员数)``
    """
    member_count = (
        db.scalar(
            select(func.count(FamilyMember.id)).where(
                FamilyMember.family_id == family_id,
                FamilyMember.status == MemberStatus.ACTIVE,
            )
        )
        or 0
    )
    checkin_count = (
        db.scalar(
            select(func.count(FamilyMember.id)).where(
                FamilyMember.family_id == family_id,
                FamilyMember.status == MemberStatus.ACTIVE,
                FamilyMember.needs_checkin.is_(True),
            )
        )
        or 0
    )
    return int(member_count), int(checkin_count)
