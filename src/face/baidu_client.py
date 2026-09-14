"""百度 AI 人脸识别（V3）HTTP 客户端。

接口约定（``https://aip.baidubce.com``）：

| 用途 | 路径 | 关键参数 |
| --- | --- | --- |
| 获取 access_token | ``/oauth/2.0/token`` | ``grant_type=client_credentials&client_id=AK&client_secret=SK`` |
| 人脸检测 | ``/rest/2.0/face/v3/detect`` | ``image``, ``image_type=BASE64``, ``face_field=quality`` |
| 人脸注册 | ``/rest/2.0/face/v3/faceset/user/add`` | ``image``, ``group_id``, ``user_id``, ``quality_control`` |
| 人脸更新 | ``/rest/2.0/face/v3/faceset/user/update`` | 同上 |
| 人脸删除 | ``/rest/2.0/face/v3/faceset/user/delete`` | ``user_id``, ``group_id``, ``face_token``（可选） |
| 人脸搜索 | ``/rest/2.0/face/v3/search`` | ``image``, ``group_id_list``, ``max_user_num``, ``match_threshold`` |

要点：

- V3 接口为 ``POST`` + ``application/json`` 请求体，``access_token`` 作为 URL 查询参数；
  ``image`` 传图片的 base64 字符串（放在 JSON 中无需再做 urlencode）；
- 响应统一为 ``{"error_code": 0, "error_msg": "SUCCESS", "result": {...}}``，
  ``error_code != 0`` 表示失败，本模块统一转换为 :class:`BaiduFaceError`；
- access_token 有效期 30 天，客户端缓存并在**过期前 1 小时**自动刷新；
  若服务端返回 token 失效（110/111），会自动强制刷新并重试一次；
- 搜索无匹配返回 ``222207``，属于正常业务结果而非异常，由调用方判定。

参考：百度智能云官方文档《人脸识别 API 接口文档》《错误码》
https://cloud.baidu.com/doc/FACE/s/5k37c1ij0
https://cloud.baidu.com/doc/FACE/s/ik37c1j82

多进程部署提示：token 缓存在进程内存中，多 worker 时各自持有 token（互不影响）；
若后续引入 Redis，可把 token 缓存迁移到 Redis 以实现共享。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from src.config.settings import settings

logger = logging.getLogger(__name__)

__all__ = [
    "BAIDU_API_BASE",
    "BaiduFaceClient",
    "BaiduFaceError",
    "ERROR_INVALID_TOKEN",
    "ERROR_NO_FACE",
    "ERROR_NO_MATCH",
    "ERROR_SUCCESS",
]

#: 百度 AI 开放平台地址
BAIDU_API_BASE = "https://aip.baidubce.com"

#: 获取 access_token
_TOKEN_PATH = "/oauth/2.0/token"
#: 人脸检测
_DETECT_PATH = "/rest/2.0/face/v3/detect"
#: 人脸注册 / 更新 / 删除
_REGISTER_PATH = "/rest/2.0/face/v3/faceset/user/add"
_UPDATE_PATH = "/rest/2.0/face/v3/faceset/user/update"
_DELETE_PATH = "/rest/2.0/face/v3/faceset/user/delete"
#: 人脸搜索
_SEARCH_PATH = "/rest/2.0/face/v3/search"

#: 成功
ERROR_SUCCESS = 0
#: 搜索无匹配结果（正常业务结果）
ERROR_NO_MATCH = 222207
#: 图片中未检测到人脸
ERROR_NO_FACE = 222202
#: access_token 无效或已过期，需要强制刷新
ERROR_INVALID_TOKEN = frozenset({110, 111})

#: 默认请求超时（秒）
_DEFAULT_TIMEOUT = 15.0


class BaiduFaceError(Exception):
    """百度人脸接口调用失败。

    :param code: 百度 ``error_code``；本地网络/解析异常时为 ``-1``
    :param message: 错误描述（优先使用百度返回的 ``error_msg``）
    :param log_id: 百度返回的 ``log_id``，便于向百度提工单时定位
    """

    def __init__(self, code: int, message: str, log_id: Any = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = int(code)
        self.message = message
        self.log_id = log_id

    @property
    def is_no_match(self) -> bool:
        """是否为"搜索无匹配"（正常结果）。"""
        return self.code == ERROR_NO_MATCH

    @property
    def is_no_face(self) -> bool:
        """是否为"图片中没有人脸"。"""
        return self.code == ERROR_NO_FACE


class BaiduFaceClient:
    """百度人脸识别 V3 客户端。

    :param api_key: 百度应用 API Key，默认取配置 ``BAIDU_FACE_API_KEY``
    :param secret_key: 百度应用 Secret Key，默认取配置 ``BAIDU_FACE_SECRET_KEY``
    :param http_client: 可注入的 httpx 客户端（单元测试用 ``httpx.MockTransport`` 替代网络）
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.BAIDU_FACE_API_KEY
        self.secret_key = (
            secret_key if secret_key is not None else settings.BAIDU_FACE_SECRET_KEY
        )
        self._client = http_client or httpx.Client(timeout=timeout)
        self._access_token: str | None = None
        self._token_expire_at: float = 0.0

    # ------------------------------------------------------------------
    # access_token
    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """是否已配置 AK/SK。"""
        return bool(self.api_key and self.secret_key)

    def _get_access_token(self, *, force_refresh: bool = False) -> str:
        """获取 access_token（带缓存与提前刷新）。"""
        now = time.time()
        if not force_refresh and self._access_token and now < self._token_expire_at:
            return self._access_token

        if not self.configured:
            raise BaiduFaceError(-1, "未配置百度人脸识别 API Key / Secret Key")

        try:
            response = self._client.post(
                f"{BAIDU_API_BASE}{_TOKEN_PATH}",
                params={
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.secret_key,
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BaiduFaceError(-1, f"获取百度 access_token 失败：{exc}") from exc

        token = data.get("access_token")
        if not token:
            raise BaiduFaceError(
                int(data.get("error_code") or -1),
                str(data.get("error_description") or "获取百度 access_token 失败"),
            )

        expires_in = int(data.get("expires_in") or 2592000)
        self._access_token = str(token)
        self._token_expire_at = now + max(
            expires_in - settings.BAIDU_FACE_TOKEN_REFRESH_MARGIN, 60
        )
        logger.info("已获取百度 access_token（%d 秒后过期）", expires_in)
        return self._access_token

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------
    def _post(
        self, path: str, payload: dict[str, Any], *, retry_on_token_error: bool = True
    ) -> dict[str, Any]:
        """发起 POST 请求并返回 ``result`` 字段。

        :raises BaiduFaceError: 网络异常、响应非 JSON 或 ``error_code != 0``
        """
        token = self._get_access_token()
        try:
            response = self._client.post(
                f"{BAIDU_API_BASE}{path}",
                params={"access_token": token},
                json=payload,
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BaiduFaceError(-1, f"调用百度人脸识别服务失败：{exc}") from exc

        error_code = int(data.get("error_code", 0))
        if error_code in ERROR_INVALID_TOKEN and retry_on_token_error:
            logger.warning("百度 access_token 已失效，强制刷新后重试：%s", path)
            self._get_access_token(force_refresh=True)
            return self._post(path, payload, retry_on_token_error=False)

        if error_code != ERROR_SUCCESS:
            raise BaiduFaceError(
                error_code,
                str(data.get("error_msg") or "百度人脸识别服务返回错误"),
                data.get("log_id"),
            )

        result = data.get("result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _encode_image(image: bytes) -> str:
        """图片转 base64 字符串。"""
        return base64.b64encode(image).decode("ascii")

    # ------------------------------------------------------------------
    # 业务能力
    # ------------------------------------------------------------------
    def detect(self, image: bytes, *, face_field: str = "quality") -> dict[str, Any]:
        """人脸检测。

        :return: 原始 ``result``，包含 ``face_num`` 与 ``face_list``
        """
        return self._post(
            _DETECT_PATH,
            {
                "image": self._encode_image(image),
                "image_type": "BASE64",
                "face_field": face_field,
                "max_face_num": 5,
                "face_type": "LIVE",
            },
        )

    def register(
        self,
        *,
        user_id: str,
        group_id: str,
        image: bytes,
        user_info: str | None = None,
        quality_control: str = "NORMAL",
        liveness_control: str = "NONE",
    ) -> str:
        """人脸注册。

        :return: 百度返回的 ``face_token``
        """
        payload: dict[str, Any] = {
            "image": self._encode_image(image),
            "image_type": "BASE64",
            "group_id": group_id,
            "user_id": user_id,
            "quality_control": quality_control,
            "liveness_control": liveness_control,
            "action_type": "REPLACE",
        }
        if user_info:
            payload["user_info"] = user_info

        result = self._post(_REGISTER_PATH, payload)
        return str(result.get("face_token", ""))

    def update(
        self,
        *,
        user_id: str,
        group_id: str,
        image: bytes,
        user_info: str | None = None,
        quality_control: str = "NORMAL",
        liveness_control: str = "NONE",
    ) -> str:
        """人脸更新（替换指定用户在人脸库中的照片）。

        :return: 百度返回的 ``face_token``
        """
        payload: dict[str, Any] = {
            "image": self._encode_image(image),
            "image_type": "BASE64",
            "group_id": group_id,
            "user_id": user_id,
            "quality_control": quality_control,
            "liveness_control": liveness_control,
        }
        if user_info:
            payload["user_info"] = user_info

        result = self._post(_UPDATE_PATH, payload)
        return str(result.get("face_token", ""))

    def delete(
        self, *, user_id: str, group_id: str, face_token: str | None = None
    ) -> bool:
        """删除人脸。

        :return: 是否实际删除（用户在人脸库中不存在时返回 ``False`` 而不抛异常）
        """
        payload: dict[str, Any] = {"user_id": user_id, "group_id": group_id}
        if face_token:
            payload["face_token"] = face_token

        try:
            self._post(_DELETE_PATH, payload)
        except BaiduFaceError as exc:
            if exc.is_no_match:
                logger.info("百度人脸库中不存在该用户，跳过删除：user_id=%s", user_id)
                return False
            raise
        return True

    def search(
        self,
        *,
        image: bytes,
        group_id_list: str,
        max_user_num: int = 5,
        match_threshold: float = 80.0,
        quality_control: str = "NORMAL",
        liveness_control: str = "NONE",
    ) -> list[dict[str, Any]]:
        """人脸搜索（1:N）。

        :return: ``user_list``；无匹配时返回空列表（百度返回 222207）
        """
        try:
            result = self._post(
                _SEARCH_PATH,
                {
                    "image": self._encode_image(image),
                    "image_type": "BASE64",
                    "group_id_list": group_id_list,
                    "max_user_num": max_user_num,
                    "match_threshold": int(match_threshold),
                    "quality_control": quality_control,
                    "liveness_control": liveness_control,
                },
            )
        except BaiduFaceError as exc:
            if exc.is_no_match:
                return []
            raise

        user_list = result.get("user_list")
        return list(user_list) if isinstance(user_list, list) else []

    def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        self._client.close()
