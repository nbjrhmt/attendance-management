"""签到业务逻辑层。

职责：

- **签到**：人脸签到（复用阶段四 :func:`src.face.service.search_face` 的 1:N 识别）
  与手动签到；
- **窗口与迟到判定**：签到必须在活动时间窗内，并按 ``late_threshold_minutes`` 判定迟到；
- **幂等**：一成员一活动一条记录（数据库唯一约束 + 服务层 409 前置校验）；
- **活动结束联动**：为所有"应签到但无记录"的成员生成缺勤 / 请假记录
  （:func:`generate_absent_records`，由 :mod:`src.event.service` 在结束时调用）；
- **统计**：活动签到汇总（:func:`build_summary`）与明细查询。

业务规则（错误码与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 活动/成员/家庭不存在 | 404 | 签到活动不存在：``{id}`` / 家庭成员不存在：``{id}`` |
| 活动未开始（``now < start_time``） | 400 | 活动尚未开始 |
| 活动已结束（``now > end_time`` 或状态 finished） | 400 | 活动已结束 |
| 活动已取消 | 400 | 活动已取消 |
| 成员或家庭已停用 | 400 | 该成员已停用，无法签到 / 该家庭档案已停用，无法签到 |
| 成员不需要签到 | 400 | 该成员无需签到 |
| 人脸未识别到成员 | 400 | 未识别到人脸库中的成员 |
| 该成员已有记录 | 409 | 该成员已在本次活动中签到 |
| 跨家庭查看签到记录 | 403 | 权限不足：只能查看本家庭的签到记录 |

签到状态取值：``signed`` 已签到 / ``late`` 迟到 / ``absent`` 缺勤 /
``leave`` 请假 / ``abnormal`` 异常。其中 ``absent`` / ``leave`` 由活动结束时的
批量生成产生（``method`` 与 ``checked_at`` 均为 ``NULL``），``abnormal`` 由管理员修正产生。

**家庭用户的数据范围**：签到列表与活动明细对家庭用户自动收敛到本户
（避免向任意家庭用户暴露全村成员姓名与出勤情况），管理员/工作人员可见全部。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.checkin import crud
from src.checkin.models import CheckinMethod, CheckinRecord, CheckinStatus
from src.checkin.schemas import (
    CheckinManualRequest,
    CheckinRecordOut,
    CheckinSummaryOut,
    CheckinUpdate,
    EventCheckinDetailOut,
)
from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.common.schemas import PageData
from src.event import crud as event_crud
from src.event.models import Event, EventStatus
from src.event.schemas import EventOut
from src.face.provider import FaceProvider
from src.face import service as face_service
from src.family import service as family_service
from src.family.models import Family
from src.leave import crud as leave_crud
from src.log import service as log_service
from src.member.models import FamilyMember
from src.user.models import User, UserRole

__all__ = [
    "build_summary",
    "checkin_by_face",
    "checkin_manual",
    "generate_absent_records",
    "get_event_checkins",
    "list_checkins",
    "load_event",
    "update_checkin",
]

#: 操作日志中的模块名
_LOG_MODULE = "checkin"

#: 可查看全部签到记录的角色（管理员管理数据，工作人员协助现场签到）
_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})

#: 视为"到场"的状态（计入出勤率）
_PRESENT_STATUSES: tuple[CheckinStatus, ...] = (
    CheckinStatus.SIGNED,
    CheckinStatus.LATE,
)

#: 系统自动生成的记录备注
_AUTO_REMARK = "活动结束时自动生成"


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def load_event(db: Session, event_id: int) -> Event:
    """获取活动 ORM 对象，不存在时抛出 404。"""
    event = event_crud.get_event_by_id(db, event_id)
    if event is None:
        raise BusinessError(
            f"签到活动不存在：{event_id}", code=ResponseCode.NOT_FOUND
        )
    return event


def _load_member_and_family(db: Session, member_id: int) -> tuple[FamilyMember, Family]:
    """加载成员及其所属家庭（不存在时抛出 404）。"""
    member = db.get(FamilyMember, member_id)
    if member is None:
        raise BusinessError(
            f"家庭成员不存在：{member_id}", code=ResponseCode.NOT_FOUND
        )
    family = db.get(Family, member.family_id)
    if family is None:
        raise BusinessError(
            f"家庭档案不存在：{member.family_id}", code=ResponseCode.NOT_FOUND
        )
    return member, family


def _ensure_member_checkinable(member: FamilyMember, family: Family) -> None:
    """校验成员是否具备签到资格：家庭正常、成员正常且需要签到。"""
    if not family.is_active:
        raise BusinessError(
            "该家庭档案已停用，无法签到", code=ResponseCode.PARAM_ERROR
        )
    if not member.is_active:
        raise BusinessError("该成员已停用，无法签到", code=ResponseCode.PARAM_ERROR)
    if not member.needs_checkin:
        raise BusinessError("该成员无需签到", code=ResponseCode.PARAM_ERROR)


def _ensure_within_window(event: Event, *, now: datetime | None = None) -> datetime:
    """校验签到时间窗口，返回本次签到时间。

    校验顺序：先看活动状态（已取消 / 已结束优先于时间判断），再判断时间窗口。
    """
    moment = now or datetime.now()

    if event.is_cancelled:
        raise BusinessError("活动已取消", code=ResponseCode.PARAM_ERROR)
    if event.is_finished:
        raise BusinessError("活动已结束", code=ResponseCode.PARAM_ERROR)
    if moment < event.start_time:
        raise BusinessError("活动尚未开始", code=ResponseCode.PARAM_ERROR)
    if moment > event.end_time:
        raise BusinessError("活动已结束", code=ResponseCode.PARAM_ERROR)
    return moment


def _determine_status(event: Event, checked_at: datetime) -> CheckinStatus:
    """按迟到阈值判定签到状态：超过 ``late_threshold_minutes`` 分钟即为迟到。"""
    elapsed_seconds = (checked_at - event.start_time).total_seconds()
    if elapsed_seconds > event.late_threshold_minutes * 60:
        return CheckinStatus.LATE
    return CheckinStatus.SIGNED


def _ensure_not_checked_in(db: Session, event_id: int, member_id: int) -> None:
    """幂等校验：同一成员同一活动只能有一条签到记录。"""
    if crud.get_record_by_member(db, event_id, member_id) is not None:
        raise BusinessError(
            "该成员已在本次活动中签到", code=ResponseCode.CONFLICT
        )


def _to_out(record: CheckinRecord, member: FamilyMember, event_name: str) -> CheckinRecordOut:
    """签到记录 + 成员 + 活动名称 -> 列表/详情响应 DTO。"""
    return CheckinRecordOut(
        id=record.id,
        event_id=record.event_id,
        event_name=event_name,
        family_id=record.family_id,
        member_id=record.member_id,
        member_name=member.name if member is not None else "",
        method=record.method,
        status=record.status,
        checked_at=record.checked_at,
        face_score=record.face_score,
        reviewed_by_id=record.reviewed_by_id,
        reviewed_at=record.reviewed_at,
        review_remark=record.review_remark,
        remark=record.remark,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _resolve_family_scope(db: Session, current_user: User, requested: int | None) -> int | None:
    """确定签到数据的家庭范围。

    - 管理员 / 工作人员：不限制（可用 ``family_id`` 筛选）；
    - 家庭用户：强制为本户，指定其他家庭返回 403。
    """
    if current_user.role in _READ_ALL_ROLES:
        return requested

    own_family = family_service.get_own_family(db, current_user)
    if requested is not None and requested != own_family.id:
        raise BusinessError(
            "权限不足：只能查看本家庭的签到记录", code=ResponseCode.FORBIDDEN
        )
    return own_family.id


# ----------------------------------------------------------------------
# 签到
# ----------------------------------------------------------------------
def _create_checkin(
    db: Session,
    *,
    current_user: User,
    event: Event,
    member: FamilyMember,
    family: Family,
    checked_at: datetime,
    method: CheckinMethod,
    face_score: float | None = None,
    remark: str | None = None,
) -> CheckinRecordOut:
    """落库签到记录（人脸 / 手动共用），并写入操作日志。"""
    _ensure_not_checked_in(db, event.id, member.id)

    record = crud.create_record(
        db,
        event_id=event.id,
        family_id=family.id,
        member_id=member.id,
        method=method,
        status=_determine_status(event, checked_at),
        checked_at=checked_at,
        face_score=face_score,
        remark=remark,
        commit=False,
    )
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action=(
            "face_checkin" if method == CheckinMethod.FACE else "manual_checkin"
        ),
        target_type="checkin",
        target_id=record.id,
        detail=(
            f"{'人脸' if method == CheckinMethod.FACE else '手动'}签到：{member.name}"
            f"（活动{event.id}，{record.status.label}）"
        ),
    )
    db.commit()
    db.refresh(record)
    return _to_out(record, member, event.name)


def checkin_by_face(
    db: Session,
    current_user: User,
    *,
    event_id: int,
    content: bytes,
    filename: str | None,
    provider: FaceProvider,
) -> CheckinRecordOut:
    """人脸签到（管理员 / 工作人员）。

    流程：加载活动（不存在直接 404，避免无意义的百度调用）→ 1:N 人脸识别 →
    未识别到成员返回 400 → 校验成员资格 / 签到窗口 / 是否重复 → 落库签到记录
    （``method=face``，``face_score`` 为识别得分）。
    """
    event = load_event(db, event_id)

    result = face_service.search_face(
        db,
        current_user,
        content=content,
        filename=filename,
        provider=provider,
    )
    if not result.matched or result.member is None:
        raise BusinessError(
            "未识别到人脸库中的成员", code=ResponseCode.PARAM_ERROR
        )

    member, family = _load_member_and_family(db, result.member.id)
    _ensure_member_checkinable(member, family)
    checked_at = _ensure_within_window(event)

    return _create_checkin(
        db,
        current_user=current_user,
        event=event,
        member=member,
        family=family,
        checked_at=checked_at,
        method=CheckinMethod.FACE,
        face_score=result.score,
    )


def checkin_manual(
    db: Session, current_user: User, data: CheckinManualRequest
) -> CheckinRecordOut:
    """手动签到（管理员 / 工作人员代成员签到）。"""
    event = load_event(db, data.event_id)

    member, family = _load_member_and_family(db, data.member_id)
    _ensure_member_checkinable(member, family)
    checked_at = _ensure_within_window(event)

    return _create_checkin(
        db,
        current_user=current_user,
        event=event,
        member=member,
        family=family,
        checked_at=checked_at,
        method=CheckinMethod.MANUAL,
        remark=data.remark,
    )


# ----------------------------------------------------------------------
# 活动结束联动：生成缺勤 / 请假记录
# ----------------------------------------------------------------------
def generate_absent_records(db: Session, event: Event, *, commit: bool = True) -> int:
    """为"应签到但无签到记录"的成员生成缺勤 / 请假记录。

    规则（活动结束时调用）：

    - 面向**家庭正常 + 成员正常 + ``needs_checkin=True``** 的全部成员；
    - 已有签到记录的成员跳过（已到场者以实际签到为准，包括已通过请假的成员）；
    - 其中**存在已通过请假**的成员记为 ``leave``，其余记为 ``absent``；
    - 自动生成的记录 ``method`` 与 ``checked_at`` 均为 ``NULL``。

    :param commit: 是否立即提交；``False`` 时仅 ``flush``，便于与活动状态同事务提交
    :return: 生成的记录条数
    """
    existing_ids = crud.list_existing_member_ids(db, event.id)
    expected_members = crud.list_expected_members(db, exclude_member_ids=existing_ids)
    if not expected_members:
        return 0

    approved_ids = leave_crud.approved_member_ids(db, event.id)
    rows = [
        {
            "event_id": event.id,
            "family_id": member.family_id,
            "member_id": member.id,
            "method": None,
            "status": (
                CheckinStatus.LEAVE
                if member.id in approved_ids
                else CheckinStatus.ABSENT
            ),
            "checked_at": None,
            "face_score": None,
            "remark": _AUTO_REMARK,
        }
        for member in expected_members
    ]
    return crud.bulk_create_records(db, rows, commit=commit)


# ----------------------------------------------------------------------
# 查询与统计
# ----------------------------------------------------------------------
def build_summary(
    db: Session, event: Event, *, family_id: int | None = None
) -> CheckinSummaryOut:
    """汇总活动签到情况。

    :param family_id: 仅统计指定家庭（家庭用户的本户视角）
    :return: 应签到人数、各状态计数与出勤率 ``(signed+late)/total_expected``
    """
    counts = crud.count_by_status(db, event.id, family_id=family_id)
    expected = crud.count_expected_members(db, family_id=family_id)
    signed = counts.get(CheckinStatus.SIGNED, 0)
    late = counts.get(CheckinStatus.LATE, 0)
    rate = round((signed + late) / expected, 4) if expected else 0.0

    return CheckinSummaryOut(
        total_expected=expected,
        signed=signed,
        late=late,
        absent=counts.get(CheckinStatus.ABSENT, 0),
        leave=counts.get(CheckinStatus.LEAVE, 0),
        abnormal=counts.get(CheckinStatus.ABNORMAL, 0),
        attendance_rate=rate,
    )


def list_checkins(
    db: Session,
    current_user: User,
    *,
    event_id: int | None = None,
    family_id: int | None = None,
    status: CheckinStatus | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[CheckinRecordOut], int]:
    """分页查询签到记录（家庭用户仅本户）。"""
    target_family_id = _resolve_family_scope(db, current_user, family_id)
    rows, total = crud.list_records(
        db,
        event_id=event_id,
        family_id=target_family_id,
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return [_to_out(record, member, name) for record, member, name in rows], total


def get_event_checkins(
    db: Session,
    current_user: User,
    event_id: int,
    *,
    page: int = 1,
    page_size: int = 10,
) -> EventCheckinDetailOut:
    """活动签到明细：活动信息 + 汇总统计 + 分页签到记录。

    家庭用户仅能看到本户的明细与本户视角的汇总（避免泄露全村出勤情况）。
    """
    event = load_event(db, event_id)
    scope_family_id = (
        None
        if current_user.role in _READ_ALL_ROLES
        else family_service.get_own_family(db, current_user).id
    )

    rows, total = crud.list_records(
        db, event_id=event_id, family_id=scope_family_id, page=page, page_size=page_size
    )
    items = [_to_out(record, member, name) for record, member, name in rows]

    return EventCheckinDetailOut(
        event=EventOut.model_validate(event),
        summary=build_summary(db, event, family_id=scope_family_id),
        checkins=PageData[CheckinRecordOut](
            total=total, page=page, page_size=page_size, items=items
        ),
    )


# ----------------------------------------------------------------------
# 人工修正
# ----------------------------------------------------------------------
def update_checkin(
    db: Session, current_user: User, checkin_id: int, data: CheckinUpdate
) -> CheckinRecordOut:
    """修正签到记录（仅管理员），并记录修正人与时间。

    为保证状态与字段自洽，服务层同时做一致性修补：

    - 修正为 ``signed`` / ``late``：``checked_at`` 为空时补为当前时间，
      ``method`` 为空时补为 ``manual``；
    - 修正为 ``absent`` / ``leave`` / ``abnormal``：清空 ``checked_at`` 与 ``method``
      （非到场状态没有签到方式与签到时间）。
    """
    record = crud.get_record_by_id(db, checkin_id)
    if record is None:
        raise BusinessError(
            f"签到记录不存在：{checkin_id}", code=ResponseCode.NOT_FOUND
        )

    member = db.get(FamilyMember, record.member_id)
    event = event_crud.get_event_by_id(db, record.event_id)
    event_name = event.name if event is not None else ""

    values: dict[str, object] = {
        "status": data.status,
        "reviewed_by_id": current_user.id,
        "reviewed_at": datetime.now(),
    }
    if data.remark is not None:
        values["remark"] = data.remark
    if data.review_remark is not None:
        values["review_remark"] = data.review_remark

    if data.status in _PRESENT_STATUSES:
        if record.checked_at is None:
            values["checked_at"] = values["reviewed_at"]
        if record.method is None:
            values["method"] = CheckinMethod.MANUAL
    else:
        values["checked_at"] = None
        values["method"] = None

    updated = crud.update_record(db, record, values, commit=False)
    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action="correct",
        target_type="checkin",
        target_id=updated.id,
        detail=(
            f"修正签到：{member.name if member is not None else updated.member_id}"
            f"（活动{updated.event_id}）→ {data.status.label}"
        ),
    )
    db.commit()
    db.refresh(updated)
    return _to_out(updated, member, event_name)
