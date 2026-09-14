"""人脸识别模块测试（阶段四）。

测试策略：**只替换 HTTP 传输层**——用 ``httpx.MockTransport`` 模拟百度人脸识别服务，
真实的 :class:`BaiduFaceClient`、:class:`BaiduFaceProvider` 与接口层代码路径全部参与执行，
因此无需联网、无需百度密钥也能验证 URL、请求体、token 缓存、错误码映射与业务流程。

覆盖点：

- 百度客户端：token 获取与缓存、token 失效自动刷新重试、请求体字段（base64/group_id/action_type）、
  搜索无匹配（222207）、无人脸（222202）、删除不存在用户、网络异常包装
- 提供方：本地模式不做比对、生产环境禁用本地模式、未配置密钥返回 503、错误码映射
- 接口：录入/更新/删除/搜索/状态/记录列表/照片读取
- 校验：非图片、空文件、超大小、无人脸、多张人脸、成员/家庭停用、成员不存在
- 权限：跨家庭 403、工作人员不能录入、家庭用户不能搜索、照片鉴权
- 联动：删除成员时清理人脸记录
"""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from main import app
from src.common.exceptions import BusinessError
from src.config.settings import BASE_DIR, settings
from src.face.baidu_client import BaiduFaceClient, BaiduFaceError
from src.face.crud import get_by_member
from src.face.models import FaceRecord, FaceRecordStatus
from src.face.provider import (
    BaiduFaceProvider,
    LocalFaceProvider,
    build_face_provider,
    get_face_provider,
)
from src.member.models import FamilyMember, MemberStatus
from tests.baidu_mock import MockBaiduFaceApi
from tests.helpers import assert_unified_response

#: 合法的 PNG 头部字节（照片内容本身不需要真实可解码，魔数校验即可）
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128

#: 合法 JPEG 头部字节（用于"更新人脸"换一张照片）
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x01" * 128

#: 另一张不同内容的"照片"，用于测试未识别到人员
OTHER_FACE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x22" * 128

#: 非图片内容
NOT_IMAGE_BYTES = b"this is definitely not an image"


def _files(content: bytes, filename: str = "face.png") -> dict:
    """构造 multipart 文件参数。"""
    return {"file": (filename, content, "image/png")}


def _register_request(
    client: TestClient, headers: dict[str, str], member_id: int, content: bytes = PNG_BYTES
):
    """调用录入接口。"""
    return client.post(
        "/api/face/register",
        headers=headers,
        data={"member_id": str(member_id)},
        files=_files(content),
    )


# ======================================================================
# 一、百度客户端（传输层打桩，代码路径真实）
# ======================================================================
def _make_client(api: MockBaiduFaceApi) -> BaiduFaceClient:
    return BaiduFaceClient(
        api_key=api.api_key, secret_key=api.secret_key, http_client=api.client()
    )


def test_client_caches_access_token(baidu_api: MockBaiduFaceApi) -> None:
    """access_token 只获取一次（进程内缓存）。"""
    client = _make_client(baidu_api)

    client.detect(PNG_BYTES)
    client.detect(PNG_BYTES)

    assert baidu_api.token_requests == 1


def test_client_refreshes_token_on_invalid_token(baidu_api: MockBaiduFaceApi) -> None:
    """服务端返回 token 失效（110）时自动强制刷新并重试一次。"""
    client = _make_client(baidu_api)
    baidu_api.force_invalid_token_once = True

    result = client.detect(PNG_BYTES)

    assert result["face_num"] == 1
    assert baidu_api.token_requests == 2


def test_client_maps_no_face_error(baidu_api: MockBaiduFaceApi) -> None:
    """222202 映射为 BaiduFaceError.is_no_face。"""
    client = _make_client(baidu_api)
    baidu_api.next_error = ("/face/v3/detect", 222202, "pic not has face")

    with pytest.raises(BaiduFaceError) as excinfo:
        client.detect(PNG_BYTES)

    assert excinfo.value.is_no_face is True
    assert excinfo.value.log_id == 1234567890


def test_client_register_payload_fields(baidu_api: MockBaiduFaceApi) -> None:
    """人脸注册请求体字段符合百度 V3 规范。"""
    client = _make_client(baidu_api)

    face_token = client.register(
        user_id="42", group_id="g1", image=PNG_BYTES, user_info="张三"
    )

    assert face_token.startswith("ft-42-")
    path, payload = baidu_api.request_log[-1]
    assert path.endswith("/rest/2.0/face/v3/faceset/user/add")
    assert payload["image_type"] == "BASE64"
    assert base64.b64decode(payload["image"]) == PNG_BYTES
    assert payload["group_id"] == "g1"
    assert payload["user_id"] == "42"
    assert payload["user_info"] == "张三"
    assert payload["action_type"] == "REPLACE"
    assert payload["quality_control"] == "NORMAL"


def test_client_search_returns_empty_when_no_match(baidu_api: MockBaiduFaceApi) -> None:
    """222207（无匹配）返回空列表而不是抛异常。"""
    client = _make_client(baidu_api)

    assert client.search(image=PNG_BYTES, group_id_list="g1") == []


def test_client_search_parses_user_list(baidu_api: MockBaiduFaceApi) -> None:
    """搜索命中时解析 user_list。"""
    client = _make_client(baidu_api)
    client.register(user_id="7", group_id="g1", image=PNG_BYTES)

    users = client.search(image=PNG_BYTES, group_id_list="g1")

    assert len(users) == 1
    assert users[0]["user_id"] == "7"
    assert users[0]["score"] == 95.5


def test_client_delete_tolerates_missing_user(baidu_api: MockBaiduFaceApi) -> None:
    """删除人脸库中不存在的用户返回 False，不抛异常。"""
    client = _make_client(baidu_api)

    assert client.delete(user_id="404", group_id="g1") is False


def test_client_wraps_transport_error(baidu_api: MockBaiduFaceApi) -> None:
    """网络异常被包装为 BaiduFaceError（code=-1），不向上抛 httpx 异常。"""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = BaiduFaceClient(
        api_key="a",
        secret_key="b",
        http_client=httpx.Client(transport=httpx.MockTransport(boom)),
    )

    with pytest.raises(BaiduFaceError) as excinfo:
        client.detect(PNG_BYTES)

    assert excinfo.value.code == -1


def test_client_without_keys_raises(baidu_api: MockBaiduFaceApi) -> None:
    """未配置 AK/SK 时调用直接报错。"""
    client = BaiduFaceClient(
        api_key="", secret_key="", http_client=baidu_api.client()
    )

    with pytest.raises(BaiduFaceError):
        client.detect(PNG_BYTES)


# ======================================================================
# 二、提供方（百度实现 / 本地模式）
# ======================================================================
def test_provider_maps_no_face_to_400(
    baidu_api: MockBaiduFaceApi, baidu_provider: BaiduFaceProvider
) -> None:
    """百度"无人脸"错误映射为 400 提示。"""
    baidu_api.next_error = ("/face/v3/detect", 222202, "pic not has face")

    with pytest.raises(BusinessError) as excinfo:
        baidu_provider.detect(PNG_BYTES)

    assert excinfo.value.code == 400
    assert "未检测到人脸" in excinfo.value.message


def test_provider_maps_service_error_to_503(
    baidu_api: MockBaiduFaceApi, baidu_provider: BaiduFaceProvider
) -> None:
    """其他百度错误映射为 503，避免把服务故障当成业务失败。"""
    baidu_api.next_error = ("/face/v3/search", 216101, "not enough param")

    with pytest.raises(BusinessError) as excinfo:
        baidu_provider.search(PNG_BYTES, max_candidates=1, threshold=80)

    assert excinfo.value.code == 503


def test_local_provider_never_matches() -> None:
    """本地模式：可生成 token、不做检测、搜索始终为空。"""
    provider = LocalFaceProvider()

    assert provider.detect(PNG_BYTES) is None
    token = provider.register(user_id="1", user_info="张三", image=PNG_BYTES)
    assert token.startswith("local-")
    assert provider.search(PNG_BYTES, max_candidates=5, threshold=80) == []
    assert provider.delete(user_id="1") is False


def test_build_provider_rejects_local_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产环境禁止使用本地人脸模式。"""
    monkeypatch.setattr(settings, "FACE_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENV", "production")

    with pytest.raises(BusinessError) as excinfo:
        build_face_provider()

    assert excinfo.value.code == 503
    assert "生产环境" in excinfo.value.message


def test_build_provider_requires_baidu_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """百度模式未配置密钥时抛出 503 并给出配置指引。"""
    monkeypatch.setattr(settings, "FACE_PROVIDER", "baidu")
    monkeypatch.setattr(settings, "BAIDU_FACE_API_KEY", "")
    monkeypatch.setattr(settings, "BAIDU_FACE_SECRET_KEY", "")

    with pytest.raises(BusinessError) as excinfo:
        build_face_provider()

    assert excinfo.value.code == 503
    assert "BAIDU_FACE_API_KEY" in excinfo.value.message


def test_build_provider_returns_local_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置 FACE_PROVIDER=local 时返回本地实现（开发环境允许）。"""
    monkeypatch.setattr(settings, "FACE_PROVIDER", "local")

    provider = build_face_provider()

    assert isinstance(provider, LocalFaceProvider)


# ======================================================================
# 三、人脸录入 / 更新
# ======================================================================
def test_register_face_success(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """录入成功：调用百度、照片落盘、记录入库、返回照片地址。"""
    response = _register_request(client_face, family_headers, member.id)

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["member_id"] == member.id
    assert data["member_name"] == member.name
    assert data["provider"] == "baidu"
    assert data["status"] == "registered"
    assert data["group_id"] == baidu_api.group_id
    assert data["image_size"] == len(PNG_BYTES)
    assert data["photo_url"] == f"/api/face/photo/{member.id}"
    assert data["detect"] == {
        "face_num": 1,
        "face_probability": 0.99,
        "blur": 0.12,
        "illumination": 120.0,
        "completeness": 0.98,
    }
    assert "成功" in body["message"]

    # 百度侧已注册
    assert str(member.id) in baidu_api.faces

    # 数据库记录
    db.rollback()
    record = get_by_member(db, member.id)
    assert record is not None
    assert record.status == FaceRecordStatus.REGISTERED
    assert record.face_token and record.face_token.startswith(f"ft-{member.id}-")
    assert record.image_size == len(PNG_BYTES)

    # 照片按家庭分目录保存
    photo = BASE_DIR / record.image_path
    assert photo.is_file()
    assert photo.read_bytes() == PNG_BYTES
    assert photo.parent.name == str(member.family_id)


def test_register_face_twice_conflict(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """同一成员重复录入返回 409，提示改用更新接口。"""
    assert_unified_response(_register_request(client_face, family_headers, member.id).json())

    response = _register_request(client_face, family_headers, member.id)

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "更新接口" in body["message"]


def test_update_face_requires_existing(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """未录入过的成员调用更新接口返回 404。"""
    response = client_face.post(
        "/api/face/update",
        headers=family_headers,
        data={"member_id": str(member.id)},
        files=_files(JPEG_BYTES, "new.jpg"),
    )

    assert response.status_code == 404
    assert "尚未录入人脸" in response.json()["message"]


def test_update_face_success(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """更新人脸：替换人脸库照片、生成新 token、仍只有一条记录。"""
    first = _register_request(client_face, family_headers, member.id).json()["data"]
    token_before = baidu_api.faces[str(member.id)]["face_token"]

    response = client_face.post(
        "/api/face/update",
        headers=family_headers,
        data={"member_id": str(member.id)},
        files=_files(JPEG_BYTES, "new.jpg"),
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["image_size"] == len(JPEG_BYTES)
    assert "更新成功" in body["message"]

    db.rollback()
    records = db.scalars(
        select(FaceRecord).where(FaceRecord.member_id == member.id)
    ).all()
    assert len(records) == 1
    assert records[0].image_size == len(JPEG_BYTES)
    assert records[0].image_md5 == hashlib.md5(JPEG_BYTES).hexdigest()
    # 百度侧同一 user_id 只有一条人脸，且 token 与照片内容已更新
    assert baidu_api.faces[str(member.id)]["image"] == JPEG_BYTES
    assert baidu_api.faces[str(member.id)]["face_token"] != token_before
    assert first["image_size"] == len(PNG_BYTES)


def test_register_face_rejects_non_image(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """非图片内容被拦截（按文件头判断，不信任扩展名）。"""
    response = _register_request(
        client_face, family_headers, member.id, content=NOT_IMAGE_BYTES
    )

    body = response.json()
    assert_unified_response(body, code=400)
    assert "格式不受支持" in body["message"]


def test_register_face_rejects_empty_file(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """空文件被拦截。"""
    response = _register_request(client_face, family_headers, member.id, content=b"")

    body = response.json()
    assert_unified_response(body, code=400)
    assert "为空" in body["message"]


def test_register_face_rejects_oversized_image(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超过大小上限的照片被拦截。"""
    monkeypatch.setattr(settings, "FACE_MAX_IMAGE_MB", 1)
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024 + 10)

    response = _register_request(client_face, family_headers, member.id, content=oversized)

    body = response.json()
    assert_unified_response(body, code=400)
    assert "不能超过" in body["message"]


def test_register_face_rejects_photo_without_face(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """检测到 0 张人脸时拒绝录入。"""
    baidu_api.detect_face_num = 0

    response = _register_request(client_face, family_headers, member.id)

    body = response.json()
    assert_unified_response(body, code=400)
    assert "未检测到人脸" in body["message"]


def test_register_face_rejects_multiple_faces(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """检测到多张人脸时拒绝录入。"""
    baidu_api.detect_face_num = 2

    response = _register_request(client_face, family_headers, member.id)

    body = response.json()
    assert_unified_response(body, code=400)
    assert "多张人脸" in body["message"]


def test_register_face_baidu_reports_no_face(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """百度直接返回 222202 时给出中文提示。"""
    baidu_api.next_error = ("/face/v3/detect", 222202, "pic not has face")

    response = _register_request(client_face, family_headers, member.id)

    body = response.json()
    assert_unified_response(body, code=400)
    assert "未检测到人脸" in body["message"]


def test_register_face_member_not_found(
    client_face: TestClient, family_headers: dict[str, str]
) -> None:
    """成员不存在返回 404。"""
    response = _register_request(client_face, family_headers, 999999)

    assert response.status_code == 404
    assert "家庭成员不存在" in response.json()["message"]


def test_register_face_other_family_forbidden(
    client_face: TestClient,
    family_headers: dict[str, str],
    register_householder,
) -> None:
    """不能给其他家庭的成员录入人脸。"""
    other = register_householder("face_other_1", phone="13600000021")
    other_member_id = client_face.post(
        "/api/members",
        headers=other["headers"],
        json={"name": "别家成员"},
    ).json()["data"]["id"]

    response = _register_request(client_face, family_headers, other_member_id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_register_face_staff_forbidden(
    client_face: TestClient, staff_headers: dict[str, str], member: FamilyMember
) -> None:
    """工作人员无权录入人脸。"""
    response = _register_request(client_face, staff_headers, member.id)

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_register_face_inactive_member_conflict(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
) -> None:
    """已停用成员不能录入人脸。"""
    db.rollback()
    stored = db.get(FamilyMember, member.id)
    assert stored is not None
    stored.status = MemberStatus.INACTIVE
    db.commit()

    response = _register_request(client_face, family_headers, member.id)

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "成员已停用" in body["message"]


def test_register_face_inactive_family_conflict(
    client_face: TestClient,
    family_headers: dict[str, str],
    admin_headers: dict[str, str],
    member: FamilyMember,
    family,
) -> None:
    """已停用家庭不能录入人脸。"""
    client_face.delete(f"/api/families/{family.id}", headers=admin_headers)

    response = _register_request(client_face, family_headers, member.id)

    assert response.status_code == 409
    body = response.json()
    assert_unified_response(body, code=409)
    assert "家庭档案已停用" in body["message"]


# ======================================================================
# 四、人脸搜索
# ======================================================================
def test_search_requires_staff_or_admin(
    client_face: TestClient, family_headers: dict[str, str]
) -> None:
    """家庭用户不能调用全村人脸库搜索。"""
    response = client_face.post(
        "/api/face/search", headers=family_headers, files=_files(PNG_BYTES)
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_search_matches_registered_member(
    client_face: TestClient,
    family_headers: dict[str, str],
    staff_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
) -> None:
    """同一张照片可以被识别到对应成员，并记录最近识别时间。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.post(
        "/api/face/search", headers=staff_headers, files=_files(PNG_BYTES)
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    data = body["data"]
    assert data["matched"] is True
    assert data["member"]["id"] == member.id
    assert data["member"]["name"] == member.name
    assert data["score"] == 95.5
    assert data["threshold"] == settings.FACE_MATCH_THRESHOLD
    assert data["provider"] == "baidu"
    assert data["family"]["family_id"] == member.family_id
    assert data["family"]["household_no"]
    assert len(data["candidates"]) == 1
    assert member.name in body["message"] or "识别成功" in body["message"]

    db.rollback()
    record = get_by_member(db, member.id)
    assert record is not None
    assert record.last_matched_at is not None
    assert record.last_match_score == 95.5


def test_search_no_match_returns_success(
    client_face: TestClient,
    staff_headers: dict[str, str],
    family_headers: dict[str, str],
    member: FamilyMember,
) -> None:
    """未识别到人员属于正常查询结果（code=0、matched=false）。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.post(
        "/api/face/search", headers=staff_headers, files=_files(OTHER_FACE_BYTES)
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["data"]["matched"] is False
    assert body["data"]["member"] is None
    assert body["data"]["score"] is None
    assert body["data"]["candidates"] == []
    assert "未识别到" in body["message"]


def test_search_below_threshold_is_not_matched(
    client_face: TestClient,
    family_headers: dict[str, str],
    staff_headers: dict[str, str],
    member: FamilyMember,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """得分低于阈值时不判定为匹配（服务端返回候选但未达阈值）。"""
    _register_request(client_face, family_headers, member.id)
    baidu_api.search_score = 60.0

    response = client_face.post(
        "/api/face/search", headers=staff_headers, files=_files(PNG_BYTES)
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["matched"] is False
    assert body["data"]["candidates"][0]["score"] == 60.0


def test_search_filters_inactive_member(
    client_face: TestClient,
    family_headers: dict[str, str],
    staff_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
) -> None:
    """已停用成员不参与识别（避免已迁出人员被识别为在场）。"""
    _register_request(client_face, family_headers, member.id)

    db.rollback()
    stored = db.get(FamilyMember, member.id)
    assert stored is not None
    stored.status = MemberStatus.INACTIVE
    db.commit()

    response = client_face.post(
        "/api/face/search", headers=staff_headers, files=_files(PNG_BYTES)
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["matched"] is False
    assert body["data"]["candidates"] == []


def test_search_ignores_unknown_library_user(
    client_face: TestClient,
    staff_headers: dict[str, str],
    baidu_api: MockBaiduFaceApi,
) -> None:
    """人脸库中存在非本系统用户时安全忽略，不误判为家庭成员。"""
    # 直接向替身人脸库写入一个非本系统的 user_id
    baidu_api.image_index[hashlib.md5(PNG_BYTES).hexdigest()] = "not-a-member-id"

    response = client_face.post(
        "/api/face/search", headers=staff_headers, files=_files(PNG_BYTES)
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["matched"] is False


def test_local_mode_flow(
    client: TestClient,
    family_headers: dict[str, str],
    staff_headers: dict[str, str],
    member: FamilyMember,
    face_upload_dir,
) -> None:
    """本地模式：录入正常落库（provider=local、无检测信息），搜索恒为未识别并给出提示。"""
    app.dependency_overrides[get_face_provider] = lambda: LocalFaceProvider()

    register = _register_request(client, family_headers, member.id)
    body = register.json()
    assert_unified_response(body)
    assert body["data"]["provider"] == "local"
    assert body["data"]["detect"] is None
    assert body["data"]["note"] and "本地模式" in body["data"]["note"]

    search = client.post("/api/face/search", headers=staff_headers, files=_files(PNG_BYTES))
    search_body = search.json()
    assert_unified_response(search_body)
    assert search_body["data"]["matched"] is False
    assert search_body["data"]["note"] and "不做人脸比对" in search_body["data"]["note"]


# ======================================================================
# 五、删除人脸 / 状态 / 照片 / 列表
# ======================================================================
def test_delete_face_success(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
    baidu_api: MockBaiduFaceApi,
) -> None:
    """删除人脸：百度侧移除、本地记录置为已删除、状态接口显示未录入。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.post(
        "/api/face/delete", headers=family_headers, json={"member_id": member.id}
    )

    assert response.status_code == 200
    body = response.json()
    assert_unified_response(body)
    assert body["message"] == "人脸已删除"
    assert str(member.id) not in baidu_api.faces

    db.rollback()
    record = get_by_member(db, member.id)
    assert record is not None
    assert record.status == FaceRecordStatus.INACTIVE
    assert record.face_token is None

    status_body = client_face.get(
        f"/api/face/status/{member.id}", headers=family_headers
    ).json()
    assert status_body["data"]["has_face"] is False


def test_delete_face_not_registered(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """未录入人脸时删除返回 404。"""
    response = client_face.post(
        "/api/face/delete", headers=family_headers, json={"member_id": member.id}
    )

    assert response.status_code == 404
    assert "尚未录入人脸" in response.json()["message"]


def test_delete_face_other_family_forbidden(
    client_face: TestClient, family_headers: dict[str, str], register_householder
) -> None:
    """不能删除他人家庭的人脸。"""
    other = register_householder("face_other_2", phone="13600000022")
    other_member_id = client_face.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]

    response = client_face.post(
        "/api/face/delete", headers=family_headers, json={"member_id": other_member_id}
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_face_status_before_and_after_register(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """状态接口：录入前 has_face=false，录入后带出 provider 与照片地址。"""
    before = client_face.get(
        f"/api/face/status/{member.id}", headers=family_headers
    ).json()
    assert_unified_response(before)
    assert before["data"]["has_face"] is False
    assert before["data"]["status"] is None
    assert before["data"]["photo_url"] is None

    _register_request(client_face, family_headers, member.id)

    after = client_face.get(
        f"/api/face/status/{member.id}", headers=family_headers
    ).json()
    assert after["data"]["has_face"] is True
    assert after["data"]["provider"] == "baidu"
    assert after["data"]["photo_url"] == f"/api/face/photo/{member.id}"
    assert after["data"]["registered_at"] is not None


def test_face_photo_returns_image_bytes(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """照片接口返回原始图片流与正确的 Content-Type。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.get(f"/api/face/photo/{member.id}", headers=family_headers)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG_BYTES


def test_face_photo_requires_token(client_face: TestClient, member: FamilyMember) -> None:
    """未携带令牌读取照片返回 401。"""
    response = client_face.get(f"/api/face/photo/{member.id}")

    assert response.status_code == 401


def test_face_photo_other_family_forbidden(
    client_face: TestClient,
    family_headers: dict[str, str],
    register_householder,
) -> None:
    """不能读取他人家庭成员的照片。"""
    other = register_householder("face_other_3", phone="13600000023")
    other_member_id = client_face.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]
    _register_request(client_face, other["headers"], other_member_id)

    response = client_face.get(
        f"/api/face/photo/{other_member_id}", headers=family_headers
    )

    assert response.status_code == 403


def test_face_photo_not_registered(
    client_face: TestClient, family_headers: dict[str, str], member: FamilyMember
) -> None:
    """未录入人脸时读取照片返回 404。"""
    response = client_face.get(f"/api/face/photo/{member.id}", headers=family_headers)

    assert response.status_code == 404


def test_face_photo_missing_file(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
) -> None:
    """照片文件被清理后返回 404 而不是 500。"""
    _register_request(client_face, family_headers, member.id)
    db.rollback()
    record = get_by_member(db, member.id)
    assert record is not None
    (BASE_DIR / record.image_path).unlink()

    response = client_face.get(f"/api/face/photo/{member.id}", headers=family_headers)

    assert response.status_code == 404
    assert "文件不存在" in response.json()["message"]


def test_face_records_family_scoped(
    client_face: TestClient,
    family_headers: dict[str, str],
    member: FamilyMember,
    register_householder,
) -> None:
    """家庭用户只能看到本户的人脸记录。"""
    _register_request(client_face, family_headers, member.id)
    other = register_householder("face_other_4", phone="13600000024")
    other_member_id = client_face.post(
        "/api/members", headers=other["headers"], json={"name": "别家成员"}
    ).json()["data"]["id"]
    _register_request(client_face, other["headers"], other_member_id)

    response = client_face.get("/api/face/records", headers=family_headers)

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["member_id"] == member.id


def test_face_records_admin_can_filter(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    member: FamilyMember,
    family,
) -> None:
    """管理员可按家庭筛选人脸记录。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.get(
        f"/api/face/records?family_id={family.id}", headers=admin_headers
    )

    body = response.json()
    assert_unified_response(body)
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["photo_url"] == f"/api/face/photo/{member.id}"


def test_face_records_other_family_forbidden(
    client_face: TestClient, family_headers: dict[str, str], register_householder
) -> None:
    """家庭用户传他人 family_id 查询被拒绝。"""
    other = register_householder("face_other_5", phone="13600000025")
    other_family_id = client_face.get(
        "/api/families/me", headers=other["headers"]
    ).json()["data"]["id"]

    response = client_face.get(
        f"/api/face/records?family_id={other_family_id}", headers=family_headers
    )

    assert response.status_code == 403
    assert_unified_response(response.json(), code=403)


def test_face_records_status_filter(
    client_face: TestClient,
    admin_headers: dict[str, str],
    family_headers: dict[str, str],
    member: FamilyMember,
) -> None:
    """按状态筛选记录（删除后为 inactive）。"""
    _register_request(client_face, family_headers, member.id)
    client_face.post(
        "/api/face/delete", headers=family_headers, json={"member_id": member.id}
    )

    inactive = client_face.get(
        "/api/face/records?status=inactive", headers=admin_headers
    ).json()
    registered = client_face.get(
        "/api/face/records?status=registered", headers=admin_headers
    ).json()

    assert inactive["data"]["total"] == 1
    assert inactive["data"]["items"][0]["status"] == "inactive"
    assert registered["data"]["total"] == 0


# ======================================================================
# 六、与成员模块联动
# ======================================================================
def test_delete_member_keeps_face_record(
    client_face: TestClient,
    family_headers: dict[str, str],
    admin_headers: dict[str, str],
    member: FamilyMember,
    db: Session,
) -> None:
    """删除成员改为"停用"后，人脸记录保留，但该成员不再参与人脸识别。"""
    _register_request(client_face, family_headers, member.id)

    response = client_face.delete(f"/api/members/{member.id}", headers=family_headers)
    assert_unified_response(response.json())

    db.rollback()
    record = get_by_member(db, member.id)
    assert record is not None, "停用成员不应删除人脸记录（保留历史与照片）"

    # 成员仍可查询（状态为已停用），人脸状态接口也照常返回
    detail = client_face.get(f"/api/members/{member.id}", headers=family_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == MemberStatus.INACTIVE.value

    status = client_face.get(f"/api/face/status/{member.id}", headers=family_headers)
    assert status.status_code == 200
    assert status.json()["data"]["has_face"] is True

    # 停用成员不会被人脸搜索识别出来（search_face 会过滤已停用成员）
    search = client_face.post(
        "/api/face/search", headers=admin_headers, files=_files(PNG_BYTES)
    )
    assert search.status_code == 200
    assert search.json()["data"]["matched"] is False
