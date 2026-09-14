"""人脸模块的请求/响应数据结构。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.face.models import FaceProviderName, FaceRecordStatus
from src.member.schemas import MemberOut

__all__ = [
    "FaceDeleteRequest",
    "FaceDetectInfo",
    "FaceRecordOut",
    "FaceRegisterResultOut",
    "FaceSearchCandidateOut",
    "FaceSearchFamilyOut",
    "FaceSearchResultOut",
    "FaceStatusOut",
]


class FaceDeleteRequest(BaseModel):
    """删除人脸请求。"""

    model_config = ConfigDict(extra="forbid")

    member_id: int = Field(ge=1, description="家庭成员ID")


class FaceDetectInfo(BaseModel):
    """人脸检测结果摘要（百度 ``face/v3/detect`` 的精简字段）。"""

    face_num: int = Field(description="检测到的人脸数量")
    face_probability: float | None = Field(default=None, description="人脸置信度（0-1）")
    blur: float | None = Field(default=None, description="模糊度，越小越清晰")
    illumination: float | None = Field(default=None, description="光照程度")
    completeness: float | None = Field(default=None, description="人脸完整度（0-1）")


class FaceRegisterResultOut(BaseModel):
    """人脸录入（注册/更新）结果。"""

    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    family_id: int = Field(description="所属家庭ID")
    provider: FaceProviderName = Field(description="人脸识别提供方")
    group_id: str = Field(description="人脸库（用户组）标识")
    status: FaceRecordStatus = Field(description="记录状态")
    image_size: int = Field(description="照片字节数")
    registered_at: datetime = Field(description="录入时间")
    photo_url: str = Field(description="照片读取地址（需携带令牌访问）")
    detect: FaceDetectInfo | None = Field(
        default=None, description="录入前的人脸检测结果；未开启检测或本地模式时为 null"
    )
    note: str | None = Field(default=None, description="附加提示（如本地模式说明）")


class FaceStatusOut(BaseModel):
    """成员人脸录入状态。"""

    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    family_id: int = Field(description="所属家庭ID")
    has_face: bool = Field(description="是否已录入人脸")
    status: FaceRecordStatus | None = Field(default=None, description="记录状态，未录入时为 null")
    provider: FaceProviderName | None = Field(default=None, description="人脸识别提供方")
    group_id: str | None = Field(default=None, description="人脸库标识")
    registered_at: datetime | None = Field(default=None, description="录入时间")
    last_matched_at: datetime | None = Field(default=None, description="最近识别成功时间")
    last_match_score: float | None = Field(default=None, description="最近识别得分")
    image_size: int | None = Field(default=None, description="照片字节数")
    photo_url: str | None = Field(default=None, description="照片读取地址，未录入时为 null")


class FaceRecordOut(BaseModel):
    """人脸录入记录（列表用）。"""

    id: int = Field(description="记录ID")
    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    family_id: int = Field(description="所属家庭ID")
    provider: FaceProviderName = Field(description="人脸识别提供方")
    group_id: str = Field(description="人脸库标识")
    status: FaceRecordStatus = Field(description="记录状态")
    image_size: int = Field(description="照片字节数")
    registered_at: datetime = Field(description="最近录入时间")
    last_matched_at: datetime | None = Field(default=None, description="最近识别成功时间")
    last_match_score: float | None = Field(default=None, description="最近识别得分")
    photo_url: str = Field(description="照片读取地址")


class FaceSearchCandidateOut(BaseModel):
    """人脸搜索候选（按得分降序）。"""

    member_id: int = Field(description="家庭成员ID")
    member_name: str = Field(description="成员姓名")
    family_id: int = Field(description="所属家庭ID")
    household_no: str | None = Field(default=None, description="户号")
    score: float = Field(description="相似度得分（0-100）")


class FaceSearchFamilyOut(BaseModel):
    """识别结果中的家庭摘要。"""

    family_id: int = Field(description="家庭ID")
    household_no: str | None = Field(default=None, description="户号")
    village: str | None = Field(default=None, description="所属村/组")
    owner_name: str = Field(description="户主姓名")


class FaceSearchResultOut(BaseModel):
    """人脸搜索结果。

    ``matched=false`` 属于正常的查询结果（未识别到人员），接口仍返回 ``code=0``，
    由前端根据 ``matched`` 与 ``note`` 提示用户。
    """

    matched: bool = Field(description="是否识别到符合条件的家庭成员")
    score: float | None = Field(default=None, description="匹配得分（0-100）")
    threshold: float = Field(description="本次判定使用的阈值")
    provider: FaceProviderName = Field(description="人脸识别提供方")
    member: MemberOut | None = Field(default=None, description="匹配到的成员信息")
    family: FaceSearchFamilyOut | None = Field(default=None, description="匹配成员的家庭摘要")
    candidates: list[FaceSearchCandidateOut] = Field(
        default_factory=list, description="候选列表（含未达阈值的候选，按得分降序）"
    )
    note: str | None = Field(default=None, description="附加提示（如本地模式说明）")
