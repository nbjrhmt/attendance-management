"""人脸记录数据访问层（CRUD）。

本层只负责数据库读写；照片文件的操作在 :mod:`src.face.storage`，
外部服务调用在 :mod:`src.face.provider`，业务与权限规则在 :mod:`src.face.service`。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.common.utils import like_pattern
from src.face.models import FaceProviderName, FaceRecord, FaceRecordStatus
from src.member.models import FamilyMember

__all__ = [
    "delete_by_member",
    "get_by_member",
    "get_registered_by_member",
    "list_records",
    "mark_matched",
    "upsert",
    "deactivate",
]


def get_by_member(db: Session, member_id: int) -> FaceRecord | None:
    """按成员ID查询人脸记录（一成员一条）。"""
    return db.scalar(select(FaceRecord).where(FaceRecord.member_id == member_id))


def get_registered_by_member(db: Session, member_id: int) -> FaceRecord | None:
    """按成员ID查询"已录入"状态的人脸记录。"""
    return db.scalar(
        select(FaceRecord).where(
            FaceRecord.member_id == member_id,
            FaceRecord.status == FaceRecordStatus.REGISTERED,
        )
    )


def upsert(
    db: Session,
    *,
    member_id: int,
    provider: FaceProviderName,
    group_id: str,
    face_token: str | None,
    image_path: str,
    image_md5: str,
    image_size: int,
    remark: str | None = None,
    commit: bool = True,
) -> FaceRecord:
    """写入或覆盖某成员的人脸记录（重新录入时覆盖旧记录）。"""
    record = get_by_member(db, member_id)
    if record is None:
        record = FaceRecord(
            member_id=member_id,
            provider=provider,
            group_id=group_id,
            face_token=face_token,
            image_path=image_path,
            image_md5=image_md5,
            image_size=image_size,
            status=FaceRecordStatus.REGISTERED,
            registered_at=datetime.now(),
            remark=remark,
        )
        db.add(record)
    else:
        record.provider = provider
        record.group_id = group_id
        record.face_token = face_token
        record.image_path = image_path
        record.image_md5 = image_md5
        record.image_size = image_size
        record.status = FaceRecordStatus.REGISTERED
        record.registered_at = datetime.now()
        if remark is not None:
            record.remark = remark

    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def deactivate(db: Session, record: FaceRecord, *, commit: bool = True) -> FaceRecord:
    """停用记录（删除人脸时调用，保留照片便于追溯与重新录入）。"""
    record.status = FaceRecordStatus.INACTIVE
    record.face_token = None
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def mark_matched(
    db: Session, record: FaceRecord, score: float, *, commit: bool = True
) -> FaceRecord:
    """记录最近一次识别成功的时间与得分。"""
    record.last_matched_at = datetime.now()
    record.last_match_score = round(float(score), 2)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def list_records(
    db: Session,
    *,
    family_id: int | None = None,
    status: FaceRecordStatus | None = None,
    provider: FaceProviderName | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[tuple[FaceRecord, FamilyMember]], int]:
    """分页查询人脸录入记录（联表带出成员信息）。

    :param keyword: 模糊匹配成员姓名
    :return: ``([(人脸记录, 成员), ...], 总记录数)``
    """
    conditions = []
    if family_id is not None:
        conditions.append(FamilyMember.family_id == family_id)
    if status is not None:
        conditions.append(FaceRecord.status == status)
    if provider is not None:
        conditions.append(FaceRecord.provider == provider)
    if keyword:
        conditions.append(
            FamilyMember.name.like(like_pattern(keyword), escape="\\")
        )

    base = (
        select(FaceRecord)
        .join(FamilyMember, FamilyMember.id == FaceRecord.member_id)
        .where(*conditions)
    )

    total = (
        db.scalar(
            select(func.count())
            .select_from(FaceRecord)
            .join(FamilyMember, FamilyMember.id == FaceRecord.member_id)
            .where(*conditions)
        )
        or 0
    )
    rows = db.execute(
        base.add_columns(FamilyMember)
        .order_by(FaceRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return [(row[0], row[1]) for row in rows], int(total)


def delete_by_member(db: Session, member_id: int, *, commit: bool = True) -> int:
    """删除某成员的人脸记录（成员被删除时联动调用）。

    :return: 删除的记录数
    """
    result = db.execute(delete(FaceRecord).where(FaceRecord.member_id == member_id))
    if commit:
        db.commit()
    else:
        db.flush()
    return int(result.rowcount or 0)
