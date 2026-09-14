"""家庭成员数据访问层（CRUD）。

写操作默认内部完成 ``commit`` 与 ``refresh``；传入 ``commit=False`` 时仅 ``flush``，
由调用方统一提交（户主注册时创建户主成员行即使用该方式）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.common.utils import like_pattern
from src.member.models import FamilyMember, Gender, MemberRelation, MemberStatus

__all__ = [
    "create_member",
    "deactivate_by_family",
    "get_member_by_id",
    "get_member_by_id_card",
    "get_householder_member",
    "list_all_members",
    "list_members",
    "update_member",
]


def get_member_by_id(db: Session, member_id: int) -> FamilyMember | None:
    """按主键查询成员。"""
    return db.get(FamilyMember, member_id)


def get_member_by_id_card(db: Session, id_card: str) -> FamilyMember | None:
    """按身份证号查询成员（身份证号唯一）。"""
    return db.scalar(select(FamilyMember).where(FamilyMember.id_card == id_card))


def get_householder_member(db: Session, family_id: int) -> FamilyMember | None:
    """查询某家庭的户主成员行。"""
    return db.scalar(
        select(FamilyMember).where(
            FamilyMember.family_id == family_id,
            FamilyMember.relation == MemberRelation.HOUSEHOLDER,
        )
    )


def list_members(
    db: Session,
    *,
    family_id: int | None = None,
    status: MemberStatus | None = None,
    needs_checkin: bool | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[FamilyMember], int]:
    """分页查询成员列表。

    :param keyword: 模糊匹配姓名 / 身份证号 / 联系电话
    :return: ``(当前页成员列表, 总记录数)``
    """
    conditions = []
    if family_id is not None:
        conditions.append(FamilyMember.family_id == family_id)
    if status is not None:
        conditions.append(FamilyMember.status == status)
    if needs_checkin is not None:
        conditions.append(FamilyMember.needs_checkin.is_(needs_checkin))
    if keyword:
        pattern = like_pattern(keyword)
        conditions.append(
            or_(
                FamilyMember.name.like(pattern, escape="\\"),
                FamilyMember.id_card.like(pattern, escape="\\"),
                FamilyMember.phone.like(pattern, escape="\\"),
            )
        )

    total = (
        db.scalar(
            select(func.count()).select_from(FamilyMember).where(*conditions)
        )
        or 0
    )
    items = list(
        db.scalars(
            select(FamilyMember)
            .where(*conditions)
            .order_by(FamilyMember.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return items, int(total)


def list_all_members(
    db: Session, family_id: int, *, include_inactive: bool = True
) -> list[FamilyMember]:
    """查询某家庭的全部成员（家庭详情页使用，不分页）。"""
    conditions = [FamilyMember.family_id == family_id]
    if not include_inactive:
        conditions.append(FamilyMember.status == MemberStatus.ACTIVE)

    return list(
        db.scalars(
            select(FamilyMember)
            .where(*conditions)
            .order_by(FamilyMember.id.asc())
        ).all()
    )


def create_member(
    db: Session,
    *,
    family_id: int,
    name: str,
    relation: MemberRelation = MemberRelation.OTHER,
    gender: Gender | None = None,
    id_card: str | None = None,
    birth_date: date | None = None,
    phone: str | None = None,
    needs_checkin: bool = True,
    remark: str | None = None,
    user_id: int | None = None,
    status: MemberStatus = MemberStatus.ACTIVE,
    commit: bool = True,
) -> FamilyMember:
    """新增家庭成员。

    :param commit: 是否立即提交；为 ``False`` 时仅 ``flush``，由调用方统一提交
    """
    member = FamilyMember(
        family_id=family_id,
        user_id=user_id,
        name=name,
        relation=relation,
        gender=gender,
        id_card=id_card,
        birth_date=birth_date,
        phone=phone,
        needs_checkin=needs_checkin,
        remark=remark,
        status=status,
    )
    db.add(member)
    try:
        if not commit:
            db.flush()
            return member

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "身份证号已被其他成员使用", code=ResponseCode.CONFLICT
        ) from exc

    db.refresh(member)
    return member


def update_member(
    db: Session, member: FamilyMember, values: dict[str, Any]
) -> FamilyMember:
    """按字段字典更新成员（只更新传入的字段）。"""
    for field, value in values.items():
        setattr(member, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise BusinessError(
            "身份证号已被其他成员使用", code=ResponseCode.CONFLICT
        ) from exc
    db.refresh(member)
    return member


def deactivate_by_family(db: Session, family_id: int, *, commit: bool = True) -> int:
    """停用某家庭的全部正常成员（家庭停用时级联调用）。

    :return: 受影响的成员行数
    """
    result = db.execute(
        update(FamilyMember)
        .where(
            FamilyMember.family_id == family_id,
            FamilyMember.status == MemberStatus.ACTIVE,
        )
        .values(status=MemberStatus.INACTIVE)
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return int(result.rowcount or 0)
