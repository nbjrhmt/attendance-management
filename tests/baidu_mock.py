"""百度 AI 人脸识别服务的离线测试替身。

用法：用 ``httpx.MockTransport`` 替换网络层，让 **真实的** :class:`BaiduFaceClient`
与 :class:`BaiduFaceProvider` 代码路径被完整执行（URL、请求体、错误码映射都参与断言），
只有 HTTP 传输被拦截，因此不需要联网、也不需要百度密钥。

模拟能力：

- ``/oauth/2.0/token``：校验 AK/SK，返回带有效期的 access_token，并统计调用次数；
- ``faceset/user/add`` / ``update`` / ``delete``：维护内存人脸库；
- ``face/v3/search``：按**照片内容 MD5** 命中已录入的人脸（同图即同人），
  未命中返回百度真实错误码 ``222207``；
- ``face/v3/detect``：按 ``detect_face_num`` 返回人脸数量，用于测试"无人脸/多张人脸"分支；
- ``force_invalid_token_once``：模拟 access_token 过期（返回 110），用于测试自动刷新重试；
- ``next_error``：模拟任意百度错误码，用于测试错误码到业务响应的映射。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx

__all__ = ["MockBaiduFaceApi"]


class MockBaiduFaceApi:
    """内存版百度人脸识别服务。"""

    def __init__(
        self,
        *,
        api_key: str = "test-ak",
        secret_key: str = "test-sk",
        access_token: str = "mock-access-token",
        group_id: str = "test_group",
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = access_token
        self.group_id = group_id

        #: user_id -> {"face_token": str, "image_md5": str, "image": bytes}
        self.faces: dict[str, dict[str, Any]] = {}
        #: 照片 MD5 -> user_id（搜索用）
        self.image_index: dict[str, str] = {}

        self.token_requests = 0
        self.request_log: list[tuple[str, dict[str, Any]]] = []

        self.detect_face_num = 1
        self.search_score = 95.5
        #: 是否忽略 match_threshold（模拟服务端未过滤阈值的情况）
        self.ignore_threshold = True
        self.force_invalid_token_once = False
        self.next_error: tuple[str, int, str] | None = None

    # ------------------------------------------------------------------
    # httpx 集成
    # ------------------------------------------------------------------
    def client(self) -> httpx.Client:
        """构造带 MockTransport 的 httpx 客户端。"""
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        """统一的请求处理入口。"""
        path = request.url.path

        if path == "/oauth/2.0/token":
            return self._handle_token(request)

        payload = self._json_body(request)
        self.request_log.append((path, payload))

        params = request.url.params
        if params.get("access_token") != self.access_token:
            return self._result(110, "Access token invalid or no longer valid")

        if self.force_invalid_token_once:
            self.force_invalid_token_once = False
            return self._result(110, "Access token invalid or no longer valid")

        if self.next_error is not None:
            error_path, code, message = self.next_error
            if path.endswith(error_path):
                self.next_error = None
                return self._result(code, message)

        if path.endswith("/face/v3/detect"):
            return self._handle_detect()
        if path.endswith("/faceset/user/add"):
            return self._handle_register(payload)
        if path.endswith("/faceset/user/update"):
            return self._handle_update(payload)
        if path.endswith("/faceset/user/delete"):
            return self._handle_delete(payload)
        if path.endswith("/face/v3/search"):
            return self._handle_search(payload)

        return self._result(216101, "not enough param")

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    @staticmethod
    def _json_body(request: httpx.Request) -> dict[str, Any]:
        if not request.content:
            return {}
        try:
            data = json.loads(request.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _result(error_code: int, error_msg: str, result: dict | None = None) -> httpx.Response:
        body: dict[str, Any] = {
            "error_code": error_code,
            "error_msg": error_msg,
            "log_id": 1234567890,
        }
        if result is not None:
            body["result"] = result
        return httpx.Response(200, json=body)

    @staticmethod
    def _decode_image(payload: dict[str, Any]) -> bytes:
        return base64.b64decode(payload.get("image", ""))

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        params = request.url.params
        if (
            params.get("client_id") != self.api_key
            or params.get("client_secret") != self.secret_key
        ):
            return httpx.Response(
                200,
                json={
                    "error": "invalid_client",
                    "error_description": "unknown client id",
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": self.access_token,
                "expires_in": 2592000,
                "scope": "brain_all_scope",
            },
        )

    def _handle_detect(self) -> httpx.Response:
        face_num = self.detect_face_num
        face_list = [
            {
                "face_token": f"detect-{index}",
                "face_probability": 0.99,
                "location": {"left": 100, "top": 100, "width": 200, "height": 200},
                "quality": {"blur": 0.12, "illumination": 120.0, "completeness": 0.98},
            }
            for index in range(face_num)
        ]
        return self._result(0, "SUCCESS", {"face_num": face_num, "face_list": face_list})

    def _handle_register(self, payload: dict[str, Any]) -> httpx.Response:
        image = self._decode_image(payload)
        if not image:
            return self._result(222202, "pic not has face")
        user_id = str(payload.get("user_id", ""))
        image_md5 = hashlib.md5(image).hexdigest()
        face_token = f"ft-{user_id}-{image_md5[:6]}"
        self.faces[user_id] = {
            "face_token": face_token,
            "image_md5": image_md5,
            "image": image,
        }
        self.image_index[image_md5] = user_id
        return self._result(
            0,
            "SUCCESS",
            {
                "face_token": face_token,
                "location": {"left": 100, "top": 100, "width": 200, "height": 200},
            },
        )

    def _handle_update(self, payload: dict[str, Any]) -> httpx.Response:
        user_id = str(payload.get("user_id", ""))
        if user_id not in self.faces:
            return self._result(222207, "match user is not found")
        return self._handle_register(payload)

    def _handle_delete(self, payload: dict[str, Any]) -> httpx.Response:
        user_id = str(payload.get("user_id", ""))
        face = self.faces.pop(user_id, None)
        if face is None:
            return self._result(222207, "match user is not found")
        self.image_index.pop(face["image_md5"], None)
        return self._result(0, "SUCCESS", {})

    def _handle_search(self, payload: dict[str, Any]) -> httpx.Response:
        image = self._decode_image(payload)
        image_md5 = hashlib.md5(image).hexdigest()
        user_id = self.image_index.get(image_md5)
        if user_id is None:
            return self._result(222207, "match user is not found")

        threshold = float(payload.get("match_threshold") or 0)
        score = self.search_score
        if not self.ignore_threshold and score < threshold:
            return self._result(222207, "match user is not found")

        return self._result(
            0,
            "SUCCESS",
            {
                "face_token": hashlib.md5(image).hexdigest(),
                "user_list": [
                    {
                        "group_id": payload.get("group_id_list", self.group_id),
                        "user_id": user_id,
                        "user_info": "",
                        "score": score,
                    }
                ],
            },
        )
