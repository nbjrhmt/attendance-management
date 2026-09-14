"""请假管理业务逻辑层。

职责：

- **提交请假**：户主为本户成员提交（管理员可为任意成员提交）；
- **审批**：管理员对"待审批"的申请做通过 / 驳回，记录审批人与审批时间；
- **撤销**：申请人（户主本人）或管理员撤销"待审批"的申请；
- **与签到联动**：已通过的请假只在**活动结束时**影响缺勤生成——
  该成员既未签到又已通过请假时记为 ``leave``；
  若其实际到场签到，仍以实际签到记录（``signed`` / ``late``）为准。

业务规则（错误码与 HTTP 状态码一致）：

| 场景 | 状态码 | 提示 |
| --- | --- | --- |
| 活动 / 成员不存在 | 404 | 签到活动不存在：``{id}`` / 家庭成员不存在：``{id}`` |
| 非本户成员 | 403 | 权限不足：只有户主本人或管理员可以维护家庭信息 |
| 为已结束 / 已取消的活动请假 | 400 | 活动已结束 / 活动已取消 |
| 成员或家庭已停用、成员无需签到 | 400 | 该成员已停用，无法请假 / 该家庭档案已停用，无法请假 / 该成员无需签到 |
| 已有待审批或已通过的请假 | 409 | 该成员已有待审批或已通过的请假 |
| 审批结果非 approved/rejected | 400 | 审批结果只能是 approved（通过）或 rejected（驳回） |
| 重复审批 / 审批已撤销、已撤回的申请 | 409 | 该请假申请已审批，无法重复审批 / 该请假申请已撤销，无法审批 |
| 撤销非待审批的申请 | 409 | 只有待审批的请假可以撤销 |
| 查看 / 撤销他人家庭的请假 | 403 | 权限不足：只能查看本家庭的请假申请 |

数据范围：管理员 / 工作人员可见全部请假；家庭用户仅可见本户。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.event import crud as event_crud
from src.event.models import Event
from src.family import service as family_service
from src.family.models import Family
from src.leave import crud
from src.leave.models import LeaveRequest, LeaveStatus
from src.leave.schemas import LeaveCreate, LeaveOut, LeaveReviewRequest
from src.log import service as log_service
from src.member.models import FamilyMember
from src.user.models import User, UserRole

__all__ = [
    "cancel_leave",
    "create_leave",
    "get_leave",
    "list_leaves",
    "load_leave",
    "review_leave",
]

#: 操作日志中的模块名
_LOG_MODULE = "leave"

#: 可查看全部请假记录的角色（管理员审批，工作人员协助核对名单）
_READ_ALL_ROLES = frozenset({UserRole.ADMIN, UserRole.STAFF})

#: 允许的审批结果
_REVIEW_TARGETS: tuple[LeaveStatus, ...] = (
    LeaveStatus.APPROVED,
    LeaveStatus.REJECTED,
)


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def load_leave(db: Session, leave_id: int) -> LeaveRequest:
    """获取请假申请 ORM 对象，不存在时抛出 404。"""
    leave = crud.get_leave_by_id(db, leave_id)
    if leave is None:
        raise BusinessError(
            f"请假申请不存在：{leave_id}", code=ResponseCode.NOT_FOUND
        )
    return leave


def _load_event(db: Session, event_id: int) -> Event:
    """加载活动，不存在时抛出 404。"""
    event = event_crud.get_event_by_id(db, event_id)
    if event is None:
        raise BusinessError(
            f"签到活动不存在：{event_id}", code=ResponseCode.NOT_FOUND
        )
    return event


def _load_member_and_family(db: Session, member_id: int) -> tuple[FamilyMember, Family]:
    """加载成员及其家庭，不存在时抛出 404。"""
    member = db.get(FamilyMember, member_id)
    if member is None:
        raise BusinessError(
            f"家庭成员不存在：{member_id}", code=ResponseCode.NOT_FOUND
        )
    family = family_service.load_family(db, member.family_id)
    return member, family


def _ensure_member_leaveable(member: FamilyMember, family: Family) -> None:
    """校验成员是否可请假：家庭正常、成员正常且需要签到。"""
    if not family.is_active:
        raise BusinessError(
            "该家庭档案已停用，无法请假", code=ResponseCode.PARAM_ERROR
        )
    if not member.is_active:
        raise BusinessError("该成员已停用，无法请假", code=ResponseCode.PARAM_ERROR)
    if not member.needs_checkin:
        raise BusinessError("该成员无需签到", code=ResponseCode.PARAM_ERROR)


def _to_out(
    leave: LeaveRequest,
    member: FamilyMember | None,
    event_name: str,
    household_no: str | None,
) -> LeaveOut:
    """请假申请 + 关联信息 -> 响应 DTO。"""
    return LeaveOut(
        id=leave.id,
        event_id=leave.event_id,
        event_name=event_name,
        member_id=leave.member_id,
        member_name=member.name if member is not None else "",
        family_id=member.family_id if member is not None else None,
        household_no=household_no,
        reason=leave.reason,
        status=leave.status,
        reviewed_by_id=leave.reviewed_by_id,
        reviewed_at=leave.reviewed_at,
        review_remark=leave.review_remark,
        created_at=leave.created_at,
        updated_at=leave.updated_at,
    )


def _detail_row(db: Session, leave_id: int):
    """查询单条请假的展示信息（不存在时抛出 404）。"""
    row = crud.get_leave_with_context(db, leave_id)
    if row is None:
        raise BusinessError(
            f"请假申请不存在：{leave_id}", code=ResponseCode.NOT_FOUND
        )
    return row


def _resolve_family_scope(db: Session, current_user: User, requested: int | None) -> int | None:
    """确定请假数据的家庭范围（家庭用户强制本户，跨户返回 403）。"""
    if current_user.role in _READ_ALL_ROLES:
        return requested

    own_family = family_service.get_own_family(db, current_user)
    if requested is not None and requested != own_family.id:
        raise BusinessError(
            "权限不足：只能查看本家庭的请假申请", code=ResponseCode.FORBIDDEN
        )
    return own_family.id


def _ensure_leave_readable(db: Session, current_user: User, leave: LeaveRequest) -> None:
    """校验可查看该请假申请：管理员/工作人员可看全部，家庭用户仅限本户。"""
    if current_user.role in _READ_ALL_ROLES:
        return

    member = db.get(FamilyMember, leave.member_id)
    family = db.get(Family, member.family_id) if member is not None else None
    if family is None or family.owner_id != current_user.id:
        raise BusinessError(
            "权限不足：只能查看本家庭的请假申请", code=ResponseCode.FORBIDDEN
        )


def _ensure_leave_cancelable(db: Session, current_user: User, leave: LeaveRequest) -> None:
    """校验可撤销该请假申请：管理员或该成员所属家庭的户主本人（工作人员不可撤销）。"""
    if current_user.is_admin:
        return

    member = db.get(FamilyMember, leave.member_id)
    family = db.get(Family, member.family_id) if member is not None else None
    if (
        family is None
        or current_user.role != UserRole.FAMILY
        or family.owner_id != current_user.id
    ):
        raise BusinessError(
            "权限不足：只有户主本人或管理员可以撤销请假申请",
            code=ResponseCode.FORBIDDEN,
        )


# ----------------------------------------------------------------------
# 提交
# ----------------------------------------------------------------------
def create_leave(db: Session, current_user: User, data: LeaveCreate) -> LeaveOut:
    """提交请假申请（户主为本户成员 / 管理员为任意成员）。"""
    event = _load_event(db, data.event_id)
    member, family = _load_member_and_family(db, data.member_id)

    # 权限：管理员可代任意成员，户主只能为本户成员（工作人员无权限）
    family_service.ensure_family_manageable(current_user, family)

    if event.is_finished:
        raise BusinessError("活动已结束", code=ResponseCode.PARAM_ERROR)
    if event.is_cancelled:
        raise BusinessError("活动已取消", code=ResponseCode.PARAM_ERROR)
    _ensure_member_leaveable(member, family)

    if crud.has_active_leave(db, event.id, member.id):
        raise BusinessError(
            "该成员已有待审批或已通过的请假", code=ResponseCode.CONFLICT
        )

    leave = crud.create_leave(
        db, event_id=event.id, member_id=member.id, reason=data.reason
    )
    return _to_out(leave, member, event.name, family.household_no)


# ----------------------------------------------------------------------
# 查询
# ----------------------------------------------------------------------
def list_leaves(
    db: Session,
    current_user: User,
    *,
    status: LeaveStatus | None = None,
    event_id: int | None = None,
    family_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[LeaveOut], int]:
    """分页查询请假申请（家庭用户仅本户）。"""
    target_family_id = _resolve_family_scope(db, current_user, family_id)
    rows, total = crud.list_leaves(
        db,
        status=status,
        event_id=event_id,
        family_id=target_family_id,
        page=page,
        page_size=page_size,
    )
    return [
        _to_out(leave, member, event_name, household_no)
        for leave, member, event_name, household_no in rows
    ], total


def get_leave(db: Session, current_user: User, leave_id: int) -> LeaveOut:
    """查询请假申请详情（本人所属家庭 / 管理员 / 工作人员）。"""
    leave, member, event_name, household_no = _detail_row(db, leave_id)
    _ensure_leave_readable(db, current_user, leave)
    return _to_out(leave, member, event_name, household_no)


# ----------------------------------------------------------------------
# 审批 / 撤销
# ----------------------------------------------------------------------
def review_leave(
    db: Session, current_user: User, leave_id: int, data: LeaveReviewRequest
) -> LeaveOut:
    """审批请假申请（仅管理员，且仅"待审批"的申请可审批）。"""
    if data.status not in _REVIEW_TARGETS:
        raise BusinessError(
            "审批结果只能是 approved（通过）或 rejected（驳回）",
            code=ResponseCode.PARAM_ERROR,
        )

    leave = load_leave(db, leave_id)
    if leave.status == LeaveStatus.APPROVED or leave.status == LeaveStatus.REJECTED:
        raise BusinessError(
            "该请假申请已审批，无法重复审批", code=ResponseCode.CONFLICT
        )
    if leave.status == LeaveStatus.CANCELLED:
        raise BusinessError(
            "该请假申请已撤销，无法审批", code=ResponseCode.CONFLICT
        )

    updated = crud.update_leave(
        db,
        leave,
        {
            "status": data.status,
            "reviewed_by_id": current_user.id,
            "reviewed_at": datetime.now(),
            "review_remark": data.remark,
        },
        commit=False,
    )

    member = db.get(FamilyMember, updated.member_id)
    event = event_crud.get_event_by_id(db, updated.event_id)
    family = db.get(Family, member.family_id) if member is not None else None

    log_service.record_operation(
        db,
        current_user,
        module=_LOG_MODULE,
        action=(
            "approve"
            if updated.status == LeaveStatus.APPROVED
            else "reject"
        ),
        target_type="leave",
        target_id=updated.id,
        detail=(
            f"审批请假：{member.name if member is not None else updated.member_id}"
            f"（活动{updated.event_id}）→ {updated.status.label}"
        ),
    )
    db.commit()
    db.refresh(updated)

    return _to_out(
        updated,
        member,
        event.name if event is not None else "",
        family.household_no if family is not None else None,
    )


def cancel_leave(db: Session, current_user: User, leave_id: int) -> LeaveOut:
    """撤销请假申请（申请人本人即户主，或管理员；仅"待审批"可撤销）。"""
    leave = load_leave(db, leave_id)
    _ensure_leave_cancelable(db, current_user, leave)

    if not leave.is_pending:
        raise BusinessError(
            "只有待审批的请假可以撤销", code=ResponseCode.CONFLICT
        )

    updated = crud.update_leave(db, leave, {"status": LeaveStatus.CANCELLED})

    member = db.get(FamilyMember, updated.member_id)
    event = event_crud.get_event_by_id(db, updated.event_id)
    family = db.get(Family, member.family_id) if member is not None else None
    return _to_out(
        updated,
        member,
        event.name if event is not None else "",
        family.household_no if family is not None else None,
    )
