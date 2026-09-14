"""签到业务接口路由（挂载前缀 ``/api/checkins``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/checkins/face`` | 人脸签到（multipart：event_id + file） | 管理员 / 工作人员 |
| POST | ``/api/checkins/manual`` | 手动签到（event_id + member_id） | 管理员 / 工作人员 |
| GET | ``/api/checkins`` | 签到记录列表（分页、活动/家庭/状态筛选） | 登录用户（家庭用户仅本户） |
| GET | ``/api/checkins/events/{event_id}`` | 活动签到明细 + 汇总统计 | 登录用户（家庭用户仅本户） |
| PUT | ``/api/checkins/{checkin_id}`` | 人工修正签到记录 | 管理员 |

说明：

- 人脸签到复用阶段四的 1:N 人脸识别（``face_service.search_face``），
  未识别到成员返回 400「未识别到人脸库中的成员」；识别得分写入 ``face_score``；
- 签到窗口、迟到判定、幂等（一成员一活动一条记录）等规则见 :mod:`src.checkin.service`；
- ``GET /api/checkins/events/{event_id}`` 返回 ``event`` + ``summary`` + 分页 ``checkins``；
  家庭用户仅能看到本户明细与本户视角的汇总，避免泄露全村成员出勤情况。

所有响应均为统一格式 ``{"code": 0, "message": "success", "data": {}}``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from sqlalchemy.orm import Session

from src.auth.dependencies import AdminUser, CurrentUser, StaffOrAdminUser
from src.checkin import service as checkin_service
from src.checkin.models import CheckinStatus
from src.checkin.schemas import (
    CheckinManualRequest,
    CheckinRecordOut,
    CheckinUpdate,
    EventCheckinDetailOut,
)
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.face.provider import FaceProvider, get_face_provider

router = APIRouter()

#: 人脸提供方依赖（按 FACE_PROVIDER 配置返回百度或本地实现）
ProviderDep = Annotated[FaceProvider, Depends(get_face_provider)]


@router.post(
    "/face",
    response_model=ApiResponse[CheckinRecordOut],
    summary="人脸签到",
    description=(
        "上传现场照片进行 1:N 人脸识别并签到：识别到成员后校验签到窗口、成员是否"
        "需要签到以及是否重复签到，成功后记录签到时间、状态（signed/late）与识别得分。"
        "未识别到人脸库中的成员返回 400。"
    ),
)
def checkin_by_face(
    event_id: Annotated[int, Form(ge=1, description="签到活动ID")],
    file: Annotated[UploadFile, File(description="现场人脸照片")],
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    provider: ProviderDep,
) -> dict[str, Any]:
    """人脸签到。"""
    record = checkin_service.checkin_by_face(
        db,
        current_user,
        event_id=event_id,
        content=file.file.read(),
        filename=file.filename,
        provider=provider,
    )
    return success_response(
        data=record,
        message=f"{record.member_name} 签到成功（{record.status.label}）",
    )


@router.post(
    "/manual",
    response_model=ApiResponse[CheckinRecordOut],
    summary="手动签到",
    description=(
        "管理员或工作人员为指定成员手动签到（人脸识别不可用时的备用方式），"
        "窗口校验、迟到判定与幂等规则与人脸签到一致。"
    ),
)
def checkin_manual(
    data: CheckinManualRequest,
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """手动签到。"""
    record = checkin_service.checkin_manual(db, current_user, data)
    return success_response(
        data=record,
        message=f"{record.member_name} 签到成功（{record.status.label}）",
    )


@router.get(
    "",
    response_model=ApiResponse[PageData[CheckinRecordOut]],
    summary="签到记录列表",
    description=(
        "分页查询签到记录，支持按活动、家庭与状态筛选；"
        "家庭用户只能查看本家庭的记录（指定其他家庭返回 403）。"
    ),
)
def list_checkins(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    event_id: Annotated[int | None, Query(ge=1, description="按活动筛选")] = None,
    family_id: Annotated[
        int | None, Query(ge=1, description="按家庭筛选（管理员/工作人员可用）")
    ] = None,
    status: Annotated[CheckinStatus | None, Query(description="状态筛选")] = None,
    keyword: Annotated[
        str | None, Query(max_length=50, description="成员姓名模糊搜索")
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询签到记录。"""
    records, total = checkin_service.list_checkins(
        db,
        current_user,
        event_id=event_id,
        family_id=family_id,
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data=PageData[CheckinRecordOut](
            total=total, page=page, page_size=page_size, items=records
        )
    )


@router.get(
    "/events/{event_id}",
    response_model=ApiResponse[EventCheckinDetailOut],
    summary="活动签到明细",
    description=(
        "查询某活动的签到明细与汇总统计：``summary`` 包含应签到人数、"
        "各状态计数与出勤率 ``(signed+late)/total_expected``；"
        "``checkins`` 为分页签到记录。家庭用户仅返回本户数据。"
    ),
)
def get_event_checkins(
    event_id: Annotated[int, Path(ge=1, description="活动ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """查询活动签到明细。"""
    detail = checkin_service.get_event_checkins(
        db, current_user, event_id, page=page, page_size=page_size
    )
    return success_response(data=detail)


@router.put(
    "/{checkin_id}",
    response_model=ApiResponse[CheckinRecordOut],
    summary="修正签到记录",
    description=(
        "管理员人工修正签到状态（如把缺勤改为已签到、把异常改为正常），"
        "同时记录修正人与修正时间；非到场状态会自动清空签到时间与签到方式，"
        "修正为已签到/迟到时若缺少签到时间则以修正时间补齐。"
    ),
)
def update_checkin(
    checkin_id: Annotated[int, Path(ge=1, description="签到记录ID")],
    data: CheckinUpdate,
    current_user: AdminUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """修正签到记录。"""
    record = checkin_service.update_checkin(db, current_user, checkin_id, data)
    return success_response(data=record, message="签到记录已修正")
