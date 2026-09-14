"""人脸识别接口路由（挂载前缀 ``/api/face``）。

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | ``/api/face/register`` | 录入人脸（multipart 上传照片） | 户主本人 / 管理员 |
| POST | ``/api/face/update`` | 重新录入（更新）人脸 | 户主本人 / 管理员 |
| POST | ``/api/face/delete`` | 删除人脸 | 户主本人 / 管理员 |
| POST | ``/api/face/search`` | 人脸搜索（1:N 识别） | 管理员 / 工作人员 |
| GET | ``/api/face/status/{member_id}`` | 成员人脸录入状态 | 户主本人 / 管理员 / 工作人员 |
| GET | ``/api/face/records`` | 人脸录入记录列表 | 户主本人 / 管理员 / 工作人员 |
| GET | ``/api/face/photo/{member_id}`` | 读取人脸照片（图片流） | 户主本人 / 管理员 / 工作人员 |

说明：

- 上传方式为 ``multipart/form-data``，字段为 ``member_id``（表单）与 ``file``（文件）；
- ``GET /api/face/photo/{member_id}`` 返回图片二进制流，**不使用统一响应格式**
  （图片类接口的规范例外），需携带 ``Authorization`` 头访问；
  人脸照片属于敏感个人信息，因此 ``uploads/`` 不对外暴露为静态目录；
- 人脸搜索仅限管理员与工作人员：全村人脸库的 1:N 检索若对家庭用户开放，
  等于允许探测他人身份。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from src.auth.dependencies import CurrentUser, StaffOrAdminUser
from src.common.database import get_db
from src.common.response import success_response
from src.common.schemas import ApiResponse, PageData
from src.config.settings import settings
from src.face import service as face_service
from src.face.models import FaceRecordStatus
from src.face.provider import FaceProvider, get_face_provider
from src.face.schemas import (
    FaceDeleteRequest,
    FaceRecordOut,
    FaceRegisterResultOut,
    FaceSearchResultOut,
    FaceStatusOut,
)

router = APIRouter()

#: 提供方依赖（按 FACE_PROVIDER 配置返回百度或本地实现）
ProviderDep = Annotated[FaceProvider, Depends(get_face_provider)]


@router.post(
    "/register",
    response_model=ApiResponse[FaceRegisterResultOut],
    summary="录入人脸",
    description=(
        "上传家庭成员的人脸照片（JPG/PNG/BMP，默认不超过 2MB）。"
        "录入前会先做一次人脸检测，拒绝无人脸或含多张人脸的照片；"
        "照片保存在服务端（uploads/face），可随时通过照片接口查看。"
    ),
)
def register_face(
    member_id: Annotated[int, Form(ge=1, description="家庭成员ID")],
    file: Annotated[UploadFile, File(description="人脸照片文件")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    provider: ProviderDep,
) -> dict[str, Any]:
    """录入成员人脸。"""
    result = face_service.save_face(
        db,
        current_user,
        member_id=member_id,
        content=file.file.read(),
        filename=file.filename,
        provider=provider,
        is_update=False,
    )
    return success_response(data=result, message=f"成员「{result.member_name}」人脸录入成功")


@router.post(
    "/update",
    response_model=ApiResponse[FaceRegisterResultOut],
    summary="更新人脸",
    description="为已录入人脸的家庭成员更换照片（覆盖人脸库中的旧照片）。",
)
def update_face(
    member_id: Annotated[int, Form(ge=1, description="家庭成员ID")],
    file: Annotated[UploadFile, File(description="新的人脸照片文件")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    provider: ProviderDep,
) -> dict[str, Any]:
    """更新成员人脸。"""
    result = face_service.save_face(
        db,
        current_user,
        member_id=member_id,
        content=file.file.read(),
        filename=file.filename,
        provider=provider,
        is_update=True,
    )
    return success_response(data=result, message=f"成员「{result.member_name}」人脸更新成功")


@router.post(
    "/delete",
    response_model=ApiResponse[dict],
    summary="删除人脸",
    description=(
        "删除人脸库中该成员的人脸，本地记录置为「已删除」并保留照片以便追溯；"
        "如需彻底清理照片文件，请联系管理员在服务器上处理。"
    ),
)
def delete_face(
    data: FaceDeleteRequest,
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    provider: ProviderDep,
) -> dict[str, Any]:
    """删除成员人脸。"""
    face_service.delete_face(
        db, current_user, member_id=data.member_id, provider=provider
    )
    return success_response(message="人脸已删除")


@router.post(
    "/search",
    response_model=ApiResponse[FaceSearchResultOut],
    summary="人脸搜索（识别）",
    description=(
        f"上传现场照片进行 1:N 识别，返回匹配到的家庭成员（默认阈值 {settings.FACE_MATCH_THRESHOLD} 分）。"
        "未识别到人员时返回 matched=false，接口仍为成功响应（code=0）。"
        "仅管理员与工作人员可调用。"
    ),
)
def search_face(
    file: Annotated[UploadFile, File(description="现场人脸照片文件")],
    current_user: StaffOrAdminUser,
    db: Annotated[Session, Depends(get_db)],
    provider: ProviderDep,
) -> dict[str, Any]:
    """人脸搜索。"""
    result = face_service.search_face(
        db,
        current_user,
        content=file.file.read(),
        filename=file.filename,
        provider=provider,
    )
    message = (
        f"识别成功：{result.member.name}" if result.matched and result.member else "未识别到匹配的家庭成员"
    )
    return success_response(data=result, message=message)


@router.get(
    "/status/{member_id}",
    response_model=ApiResponse[FaceStatusOut],
    summary="人脸录入状态",
    description="查询指定家庭成员是否已录入人脸，以及最近一次识别时间与得分。",
)
def get_face_status(
    member_id: Annotated[int, Path(ge=1, description="家庭成员ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """查询人脸录入状态。"""
    return success_response(data=face_service.get_face_status(db, current_user, member_id))


@router.get(
    "/records",
    response_model=ApiResponse[PageData[FaceRecordOut]],
    summary="人脸录入记录列表",
    description="分页查询人脸录入记录，支持按家庭、状态与成员姓名筛选；家庭用户仅可查看本户。",
)
def list_face_records(
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    family_id: Annotated[
        int | None, Query(ge=1, description="按家庭筛选（管理员/工作人员可用）")
    ] = None,
    status: Annotated[FaceRecordStatus | None, Query(description="记录状态筛选")] = None,
    keyword: Annotated[str | None, Query(max_length=50, description="成员姓名模糊搜索")] = None,
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数，1~100")] = 10,
) -> dict[str, Any]:
    """分页查询人脸录入记录。"""
    records, total = face_service.list_face_records(
        db,
        current_user,
        family_id=family_id,
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return success_response(
        data=PageData[FaceRecordOut](
            total=total, page=page, page_size=page_size, items=records
        )
    )


@router.get(
    "/photo/{member_id}",
    summary="读取人脸照片",
    description=(
        "返回该成员的人脸照片图片流（Content-Type 为 image/*），"
        "需携带 Authorization 头；图片类接口不套用统一响应格式。"
    ),
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}, "description": "人脸照片图片流"}},
)
def get_face_photo(
    member_id: Annotated[int, Path(ge=1, description="家庭成员ID")],
    current_user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> FileResponse:
    """读取成员人脸照片。"""
    path, media_type = face_service.load_face_photo(db, current_user, member_id)
    return FileResponse(path, media_type=media_type)
