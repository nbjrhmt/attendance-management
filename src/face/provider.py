"""人脸识别提供方抽象。

统一封装"录入 / 更新 / 删除 / 搜索 / 检测"五类能力，使业务层与具体服务商解耦：

- :class:`BaiduFaceProvider`：调用百度 AI 人脸库（生产使用）；
- :class:`LocalFaceProvider`：**本地模式**，不调用任何外部服务、不做人脸比对，
  仅保存照片与录入状态，供未申请到百度密钥时前端联调与流程自测；
  生产环境（``ENV=production``）会拒绝启用，避免被误当成真实识别；
- :func:`get_face_provider`：FastAPI 依赖入口，按 ``FACE_PROVIDER`` 返回对应实现，
  并对百度实现做进程内缓存（复用 access_token 与 HTTP 连接池）。

百度错误码到业务响应的映射：

| 百度错误码 | 业务处理 |
| --- | --- |
| 222207 搜索无匹配 | 正常结果（返回空候选） |
| 222202 图片无人脸 | 400「照片中未检测到人脸」 |
| 222203 图片质量不合格 | 400「照片质量不合格，请重新拍摄」 |
| 其他非 0 | 503「人脸识别服务调用失败」并记录 log_id |

未实现人脸活体检测（liveness）：签到场景才需要"确认是活人本人"，
将在阶段五的签到流程中按需调用百度在线活体检测接口。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from src.common.exceptions import BusinessError
from src.common.response import ResponseCode
from src.config.settings import settings
from src.face.baidu_client import BaiduFaceClient, BaiduFaceError

logger = logging.getLogger(__name__)

__all__ = [
    "BaiduFaceProvider",
    "FaceCandidate",
    "FaceProvider",
    "LocalFaceProvider",
    "build_face_provider",
    "get_face_provider",
]

#: 图片质量不合格
_ERROR_BAD_IMAGE = 222203

#: 本地模式提示语（会随接口返回，避免被误认为已完成人脸比对）
LOCAL_MODE_NOTE = "当前为本地模式（FACE_PROVIDER=local），仅保存照片与录入状态，不做人脸比对"


@dataclass(slots=True)
class FaceCandidate:
    """人脸搜索候选。"""

    user_id: str
    score: float
    group_id: str


class FaceProvider(Protocol):
    """人脸识别提供方协议。"""

    name: str

    def detect(self, image: bytes) -> dict[str, Any] | None:
        """人脸检测，返回精简后的检测信息；不支持时返回 ``None``。"""

    def register(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        """录入人脸，返回人脸标识（face_token）。"""

    def update(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        """更新人脸，返回新的人脸标识。"""

    def delete(self, *, user_id: str, face_token: str | None = None) -> bool:
        """删除人脸，返回是否实际删除。"""

    def search(
        self, image: bytes, *, max_candidates: int, threshold: float
    ) -> list[FaceCandidate]:
        """人脸搜索，返回候选列表（按得分降序）。"""


# ----------------------------------------------------------------------
# 百度实现
# ----------------------------------------------------------------------
class BaiduFaceProvider:
    """百度 AI 人脸库实现。"""

    name = "baidu"

    def __init__(
        self, client: BaiduFaceClient | None = None, group_id: str | None = None
    ) -> None:
        self._client = client or BaiduFaceClient()
        self.group_id = group_id or settings.BAIDU_FACE_GROUP_ID

    # -- 内部工具 ------------------------------------------------------
    @staticmethod
    def _to_business_error(exc: BaiduFaceError) -> BusinessError:
        """百度异常 -> 业务异常。"""
        if exc.is_no_face:
            return BusinessError(
                "照片中未检测到人脸，请重新拍摄", code=ResponseCode.PARAM_ERROR
            )
        if exc.code == _ERROR_BAD_IMAGE:
            return BusinessError(
                "照片质量不合格（模糊、光照不足或遮挡），请重新拍摄",
                code=ResponseCode.PARAM_ERROR,
            )
        logger.error(
            "百度人脸识别服务返回错误：code=%s message=%s log_id=%s",
            exc.code,
            exc.message,
            exc.log_id,
        )
        return BusinessError(
            f"人脸识别服务调用失败：{exc.message}",
            code=ResponseCode.SERVICE_UNAVAILABLE,
        )

    @staticmethod
    def _simplify_detect(result: dict[str, Any] | None) -> dict[str, Any] | None:
        """把百度 detect 结果精简为业务需要的字段。"""
        if not result:
            return None

        face_list = result.get("face_list") or []
        first = face_list[0] if face_list else {}
        quality = first.get("quality") or {}
        return {
            "face_num": int(result.get("face_num") or 0),
            "face_probability": first.get("face_probability"),
            "blur": quality.get("blur"),
            "illumination": quality.get("illumination"),
            "completeness": quality.get("completeness"),
        }

    # -- 能力实现 ------------------------------------------------------
    def detect(self, image: bytes) -> dict[str, Any] | None:
        try:
            return self._simplify_detect(self._client.detect(image))
        except BaiduFaceError as exc:
            raise self._to_business_error(exc) from exc

    def register(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        try:
            return self._client.register(
                user_id=user_id, group_id=self.group_id, image=image, user_info=user_info
            )
        except BaiduFaceError as exc:
            raise self._to_business_error(exc) from exc

    def update(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        try:
            return self._client.update(
                user_id=user_id, group_id=self.group_id, image=image, user_info=user_info
            )
        except BaiduFaceError as exc:
            raise self._to_business_error(exc) from exc

    def delete(self, *, user_id: str, face_token: str | None = None) -> bool:
        try:
            return self._client.delete(
                user_id=user_id, group_id=self.group_id, face_token=face_token
            )
        except BaiduFaceError as exc:
            raise self._to_business_error(exc) from exc

    def search(
        self, image: bytes, *, max_candidates: int, threshold: float
    ) -> list[FaceCandidate]:
        try:
            user_list = self._client.search(
                image=image,
                group_id_list=self.group_id,
                max_user_num=max_candidates,
                match_threshold=threshold,
            )
        except BaiduFaceError as exc:
            raise self._to_business_error(exc) from exc

        candidates = [
            FaceCandidate(
                user_id=str(item.get("user_id", "")),
                score=float(item.get("score") or 0.0),
                group_id=str(item.get("group_id") or self.group_id),
            )
            for item in user_list
            if item.get("user_id")
        ]
        return sorted(candidates, key=lambda item: item.score, reverse=True)


# ----------------------------------------------------------------------
# 本地模式实现
# ----------------------------------------------------------------------
class LocalFaceProvider:
    """本地模式：不调用外部服务、不做人脸比对。

    仅用于未申请到百度密钥时的前端联调与流程自测：

    - 录入/更新：返回本地生成的假 face_token，照片与录入状态照常落库；
    - 搜索：**始终返回空候选**（不做任何比对），并在结果中附带提示语；
    - 生产环境禁止使用（由 :func:`build_face_provider` 拦截）。
    """

    name = "local"

    def __init__(self, group_id: str | None = None) -> None:
        self.group_id = group_id or settings.BAIDU_FACE_GROUP_ID

    def detect(self, image: bytes) -> dict[str, Any] | None:
        return None

    def register(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        token = f"local-{uuid.uuid4().hex}"
        logger.warning(
            "本地模式录入人脸（未接入百度）：user_id=%s image=%d 字节", user_id, len(image)
        )
        return token

    def update(self, *, user_id: str, user_info: str | None, image: bytes) -> str:
        return self.register(user_id=user_id, user_info=user_info, image=image)

    def delete(self, *, user_id: str, face_token: str | None = None) -> bool:
        logger.warning("本地模式删除人脸（未接入百度）：user_id=%s", user_id)
        return False

    def search(
        self, image: bytes, *, max_candidates: int, threshold: float
    ) -> list[FaceCandidate]:
        logger.warning("本地模式不进行人脸比对，返回空候选")
        return []


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------
#: 进程内提供方缓存（复用百度 access_token 与 HTTP 连接池）
_provider_cache: dict[str, FaceProvider] = {}


def build_face_provider() -> FaceProvider:
    """按配置构造人脸识别提供方（不做缓存）。"""
    provider_name = settings.FACE_PROVIDER

    if provider_name == LocalFaceProvider.name:
        if settings.is_production:
            raise BusinessError(
                "生产环境不允许使用本地人脸模式（FACE_PROVIDER=local），请配置百度人脸识别密钥",
                code=ResponseCode.SERVICE_UNAVAILABLE,
            )
        logger.warning("人脸识别运行在本地模式：仅保存照片，不做人脸比对")
        return LocalFaceProvider()

    if not settings.baidu_face_configured:
        raise BusinessError(
            "未配置百度人脸识别密钥（BAIDU_FACE_API_KEY / BAIDU_FACE_SECRET_KEY）。"
            "请在 .env 中填写；若仅需前端联调，可设置 FACE_PROVIDER=local",
            code=ResponseCode.SERVICE_UNAVAILABLE,
        )
    return BaiduFaceProvider()


def get_face_provider() -> FaceProvider:
    """FastAPI 依赖：获取人脸识别提供方（进程内缓存）。"""
    provider_name = settings.FACE_PROVIDER
    provider = _provider_cache.get(provider_name)
    if provider is None:
        provider = build_face_provider()
        _provider_cache[provider_name] = provider
    return provider
