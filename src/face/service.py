"""人脸识别业务逻辑层。

职责：

- 人脸录入 / 更新：照片校验 → 人脸检测（可选）→ 调用提供方 → 保存照片 → 落库；
- 人脸删除：调用提供方删除云侧人脸，本地记录置为"已删除"（保留照片用于追溯）；
- 人脸搜索：调用提供方做 1:N 比对，把 ``user_id`` 还原为家庭成员，
  并过滤掉**已停用成员 / 已停用家庭**，避免已迁出人员被识别为在场；
- 照片读取：带鉴权返回照片文件（``uploads/`` 不作为静态目录暴露）。

权限约定：

- 录入 / 更新 / 删除：户主本人（本户成员）或管理员；
- 搜索：仅管理员 / 工作人员（全村人脸库 1:N 检索，避免家庭用户探测他人信息）；
- 状态 / 照片 / 记录列表：户主本人（本户）、管理员、工作人员可读。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import settings
from src.family import service as family_service
from src.family.models import Family
from src.face import crud, storage
from src.face.models import FaceProviderName, FaceRecord, FaceRecordStatus
from src.face.provider import LOCAL_MODE_NOTE, FaceProvider, get_face_provider
from src.face.schemas import (
    FaceDetectInfo,
    FaceRecordOut,
    FaceRegisterResultOut,
    FaceSearchCandidateOut,
    FaceSearchFamilyOut,
    FaceSearchResultOut,
    FaceStatusOut,
)
from src.member.models import FamilyMember
from src.member.schemas import MemberOut
from src.user.models import User, UserRole

logger = logging.getLogger(__name__)

__all__ = [
    "delete_face",
    "get_face_provider",
    "get_face_status",
    "list_face_records",
    "load_face_photo",
    "remove_record_for_member",
    "save_face",
    "search_face",
]

#: 可查看全部人脸记录的角色
_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})

#: 照片读取地址模板
_PHOTO_URL_TEMPLATE = "{prefix}/face/photo/{member_id}"


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _photo_url(member_id: int) -> str:
    """成员人脸照片的读取地址（需携带令牌访问）。"""
    return _PHOTO_URL_TEMPLATE.format(prefix=settings.API_PREFIX, member_id=member_id)


def _load_member_and_family(db: Session, member_id: int) -> tuple[FamilyMember, Family]:
    """加载成员及其所属家庭，成员不存在时抛出 404。"""
    member = db.get(FamilyMember, member_id)
    if member is None:
        raise BusinessError(f"家庭成员不存在：{member_id}", code=ResponseCode.NOT_FOUND)
    family = db.get(Family, member.family_id)
    if family is None:
        raise BusinessError(
            f"家庭档案不存在：{member.family_id}", code=ResponseCode.NOT_FOUND
        )
    return member, family


def _ensure_face_allowed(member: FamilyMember, family: Family, *, action: str) -> None:
    """校验成员与家庭状态是否允许人脸相关操作。"""
    if not family.is_active:
        raise BusinessError(
            f"家庭档案已停用，无法{action}人脸", code=ResponseCode.CONFLICT
        )
    if not member.is_active:
        raise BusinessError(
            f"成员已停用，无法{action}人脸（可先在成员管理中启用）",
            code=ResponseCode.CONFLICT,
        )


def _detect_face(provider: FaceProvider, image: bytes) -> FaceDetectInfo | None:
    """录入前的人脸检测：拒绝无人脸 / 多张人脸的照片。"""
    raw = provider.detect(image)
    if raw is None:
        return None

    face_num = int(raw.get("face_num") or 0)
    if face_num < 1:
        raise BusinessError(
            "照片中未检测到人脸，请正对镜头重新拍摄", code=ResponseCode.PARAM_ERROR
        )
    if face_num > 1:
        raise BusinessError(
            "照片中检测到多张人脸，请上传仅含本人的正面照片",
            code=ResponseCode.PARAM_ERROR,
        )
    return FaceDetectInfo(
        face_num=face_num,
        face_probability=raw.get("face_probability"),
        blur=raw.get("blur"),
        illumination=raw.get("illumination"),
        completeness=raw.get("completeness"),
    )


def _to_record_out(record: FaceRecord, member: FamilyMember) -> FaceRecordOut:
    """人脸记录 + 成员 -> 列表响应 DTO。"""
    return FaceRecordOut(
        id=record.id,
        member_id=record.member_id,
        member_name=member.name,
        family_id=member.family_id,
        provider=record.provider,
        group_id=record.group_id,
        status=record.status,
        image_size=record.image_size,
        registered_at=record.registered_at,
        last_matched_at=record.last_matched_at,
        last_match_score=record.last_match_score,
        photo_url=_photo_url(record.member_id),
    )


def _parse_member_id(user_id: str) -> int | None:
    """把人脸库中的 user_id 还原为成员ID（本系统使用成员ID作为 user_id）。"""
    try:
        return int(user_id)
    except (TypeError, ValueError):
        logger.warning("人脸库中存在非本系统用户，已忽略：user_id=%s", user_id)
        return None


# ----------------------------------------------------------------------
# 录入 / 更新
# ----------------------------------------------------------------------
def save_face(
    db: Session,
    current_user: User,
    *,
    member_id: int,
    content: bytes,
    filename: str | None,
    provider: FaceProvider,
    is_update: bool = False,
) -> FaceRegisterResultOut:
    """录入或更新成员人脸。

    :param is_update: ``False`` 表示首次录入（已录入时报错）；``True`` 表示重新录入
    :raises BusinessError: 照片不合法、无权限、成员/家庭状态异常、提供方调用失败
    """
    member, family = _load_member_and_family(db, member_id)
    family_service.ensure_family_manageable(current_user, family)

    existing = crud.get_by_member(db, member_id)
    if is_update:
        if existing is None or not existing.is_registered:
            raise BusinessError(
                "该成员尚未录入人脸，请先调用录入接口",
                code=ResponseCode.NOT_FOUND,
            )
    elif existing is not None and existing.is_registered:
        raise BusinessError(
            "该成员已录入人脸，如需更换请调用更新接口",
            code=ResponseCode.CONFLICT,
        )

    _ensure_face_allowed(member, family, action="更新" if is_update else "录入")

    # 1) 照片校验（非空 / 大小 / 文件头魔数）
    suffix = storage.validate_image(content, filename)

    # 2) 人脸检测（可配置关闭）
    detect_info = _detect_face(provider, content) if settings.FACE_DETECT_ON_REGISTER else None

    # 3) 调用提供方录入（百度：首次用 add，已录入用 update）
    user_info = f"{member.name}"
    if existing is not None and existing.is_registered:
        face_token = provider.update(
            user_id=str(member.id), user_info=user_info, image=content
        )
    else:
        face_token = provider.register(
            user_id=str(member.id), user_info=user_info, image=content
        )

    # 4) 保存照片 + 落库
    image_path, image_md5 = storage.save_image(
        content, family_id=family.id, member_id=member.id, suffix=suffix
    )
    record = crud.upsert(
        db,
        member_id=member.id,
        provider=FaceProviderName(provider.name),
        group_id=getattr(provider, "group_id", settings.BAIDU_FACE_GROUP_ID),
        face_token=face_token,
        image_path=image_path,
        image_md5=image_md5,
        image_size=len(content),
    )

    return FaceRegisterResultOut(
        member_id=member.id,
        member_name=member.name,
        family_id=family.id,
        provider=record.provider,
        group_id=record.group_id,
        status=record.status,
        image_size=record.image_size,
        registered_at=record.registered_at,
        photo_url=_photo_url(member.id),
        detect=detect_info,
        note=LOCAL_MODE_NOTE if provider.name == FaceProviderName.LOCAL.value else None,
    )


# ----------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------
def delete_face(
    db: Session, current_user: User, *, member_id: int, provider: FaceProvider
) -> None:
    """删除成员人脸：先删云侧人脸，再把本地记录置为"已删除"。"""
    member, family = _load_member_and_family(db, member_id)
    family_service.ensure_family_manageable(current_user, family)

    record = crud.get_registered_by_member(db, member_id)
    if record is None:
        raise BusinessError(
            f"成员「{member.name}」尚未录入人脸", code=ResponseCode.NOT_FOUND
        )

    provider.delete(user_id=str(member.id), face_token=record.face_token)
    crud.deactivate(db, record)
    logger.info("已删除人脸：member_id=%s member=%s", member.id, member.name)


def remove_record_for_member(db: Session, member_id: int, *, commit: bool = True) -> int:
    """删除某成员的人脸记录（成员被删除时联动调用，不调用外部服务）。

    说明：百度人脸库中该用户的人脸可能残留，但其 ``user_id`` 已无法对应到任何成员，
    识别时会被过滤掉（视为未识别）；如需彻底清理，请在删除成员前先调用删除人脸接口。
    """
    return crud.delete_by_member(db, member_id, commit=commit)


# ----------------------------------------------------------------------
# 搜索
# ----------------------------------------------------------------------
def search_face(
    db: Session,
    current_user: User,
    *,
    content: bytes,
    filename: str | None,
    provider: FaceProvider,
) -> FaceSearchResultOut:
    """人脸搜索（1:N 识别）。

    未识别到人员属于正常查询结果（``matched=false``），接口仍返回 ``code=0``。
    """
    storage.validate_image(content, filename)

    threshold = float(settings.FACE_MATCH_THRESHOLD)
    candidates = provider.search(
        content,
        max_candidates=settings.FACE_SEARCH_MAX_CANDIDATES,
        threshold=threshold,
    )

    results: list[FaceSearchCandidateOut] = []
    matched_member: FamilyMember | None = None
    matched_family: Family | None = None
    matched_score: float | None = None

    for candidate in candidates:
        member_id = _parse_member_id(candidate.user_id)
        if member_id is None:
            continue

        member = db.get(FamilyMember, member_id)
        if member is None or not member.is_active:
            continue
        family = db.get(Family, member.family_id)
        if family is None or not family.is_active:
            continue

        results.append(
            FaceSearchCandidateOut(
                member_id=member.id,
                member_name=member.name,
                family_id=family.id,
                household_no=family.household_no,
                score=round(candidate.score, 2),
            )
        )
        if matched_member is None and candidate.score >= threshold:
            matched_member = member
            matched_family = family
            matched_score = candidate.score

    if matched_member is not None and matched_score is not None:
        record = crud.get_by_member(db, matched_member.id)
        if record is not None:
            crud.mark_matched(db, record, matched_score)

    note = LOCAL_MODE_NOTE if provider.name == FaceProviderName.LOCAL.value else None
    return FaceSearchResultOut(
        matched=matched_member is not None,
        score=round(matched_score, 2) if matched_score is not None else None,
        threshold=threshold,
        provider=FaceProviderName(provider.name),
        member=MemberOut.model_validate(matched_member) if matched_member else None,
        family=(
            FaceSearchFamilyOut(
                family_id=matched_family.id,
                household_no=matched_family.household_no,
                village=matched_family.village,
                owner_name=matched_family.owner.real_name,
            )
            if matched_family is not None
            else None
        ),
        candidates=results,
        note=note,
    )


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def get_face_status(db: Session, current_user: User, member_id: int) -> FaceStatusOut:
    """查询成员人脸录入状态。"""
    member, family = _load_member_and_family(db, member_id)
    family_service.ensure_family_readable(current_user, family)

    record = crud.get_registered_by_member(db, member_id)
    if record is None:
        return FaceStatusOut(
            member_id=member.id,
            member_name=member.name,
            family_id=family.id,
            has_face=False,
        )

    return FaceStatusOut(
        member_id=member.id,
        member_name=member.name,
        family_id=family.id,
        has_face=True,
        status=record.status,
        provider=record.provider,
        group_id=record.group_id,
        registered_at=record.registered_at,
        last_matched_at=record.last_matched_at,
        last_match_score=record.last_match_score,
        image_size=record.image_size,
        photo_url=_photo_url(member.id),
    )


def list_face_records(
    db: Session,
    current_user: User,
    *,
    family_id: int | None = None,
    status: FaceRecordStatus | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[FaceRecordOut], int]:
    """分页查询人脸录入记录。

    - 管理员 / 工作人员：可查全部，可用 ``family_id`` 筛选；
    - 家庭用户：仅可查本户。
    """
    if current_user.role in _READ_ALL_ROLES:
        target_family_id = family_id
    else:
        own_family = family_service.get_own_family(db, current_user)
        if family_id is not None and family_id != own_family.id:
            raise BusinessError(
                "权限不足：只能查看本家庭的人脸记录", code=ResponseCode.FORBIDDEN
            )
        target_family_id = own_family.id

    rows, total = crud.list_records(
        db,
        family_id=target_family_id,
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return [_to_record_out(record, member) for record, member in rows], total


def load_face_photo(
    db: Session, current_user: User, member_id: int
) -> tuple[Path, str]:
    """读取成员人脸照片。

    :return: ``(照片绝对路径, MIME 类型)``
    """
    member, family = _load_member_and_family(db, member_id)
    family_service.ensure_family_readable(current_user, family)

    record = crud.get_registered_by_member(db, member_id) or crud.get_by_member(
        db, member_id
    )
    if record is None:
        raise BusinessError(
            f"成员「{member.name}」尚未录入人脸", code=ResponseCode.NOT_FOUND
        )

    path = storage.resolve_image_path(record.image_path)
    if not path.is_file():
        logger.error("人脸照片文件缺失：%s（member_id=%s）", record.image_path, member_id)
        raise BusinessError(
            "人脸照片文件不存在或已被清理，请重新录入", code=ResponseCode.NOT_FOUND
        )
    return path, storage.content_type_of(path.suffix)
