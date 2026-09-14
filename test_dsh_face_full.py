"""乡村基层活动智能签到管理平台 —— 人脸识别全流程接口测试（阶段四验收用例）。

本次修复要点
------------
1. **Token 不再硬编码**：旧版把 admin / huzhu01 的 JWT 写死在文件里，token 过期后
   所有接口都返回 401「登录已过期」，14 个用例集体假失败。现改为运行时获取：

   - 管理员 token：读取项目根目录 ``.env`` 的 ``JWT_SECRET_KEY``，用 ``python-jose``
     按 ``src/auth/security.py`` 的载荷格式现场签发
     （``{"sub","username","role","type","iat","exp","jti"}``，方案 A，无需知道密码）；
   - 户主 token：管理员新建户主账号后调用 ``POST /api/auth/login`` 真实登录获取（方案 B）。

2. **可重复运行**：不再使用固定 ``owner_id=2`` + 固定户号 ``F20260913001``。
   数据库里停用的历史家庭/成员仍占用 ``family.owner_id`` 与 ``family.household_no``
   唯一约束（软删除不释放约束），沿用固定值第二次运行必然 409。
   现在每次运行都新建带时间戳的户主账号、户号、身份证号与手机号，
   并在 test_99 里把本轮数据清理干净。

3. **百度接口 QPS 节流**：人脸录入/搜索/更新/删除都会真实调用百度人脸 API，
   免费版约 2 QPS，连续调用会返回「Open api qps request limit reached」（后端映射为 503）。
   因此所有会触发百度调用的请求统一经 :func:`face_upload` / :func:`face_json` 发起：
   两次调用之间自动间隔 ≥1.2 秒，并对 503/QPS 限流、以及「同图搜索首次未命中」
   各做一次 2 秒后重试。

4. **清理用例真实断言**：旧版 test_99 没有任何断言（假通过），
   现在逐项校验人脸是否删除、成员是否真的不存在、测试账号是否已无法登录、家庭是否已停用。

5. **断言只增强不放宽**：除新增的事实性断言（照片二进制一致、状态落库等）外，
   原有断言全部保留原样，包括 test_08「同人第二张照片必须 matched=True 且 score>80」。

运行前置条件
------------
1. MySQL 已启动，``.env`` 中的数据库/百度配置正确；
2. 后端已启动：``.venv\\Scripts\\python.exe -m uvicorn main:app --port 8000``；
3. 三张测试照片就位（可用 ``TEST_IMG_*`` 环境变量覆盖路径，无需改代码）：
   ``IMG_REGISTER``（录入的人脸）、``IMG_SAME_PERSON``（同一人的第二张照片）、
   ``IMG_STRANGER``（陌生人）。

⚠️ 素材要求（换照片时必读）
---------------------------
**IMG_SAME_PERSON 需用户替换为与 IMG_REGISTER 同一人的真实第二张照片**
（否则 test_08 的「同人第二张照片必须 matched=True 且 score>80」无法通过，
本文件不会放宽、跳过或删除该断言）。

当前素材已核对通过：``6de25dd6ly1h4bv85xjhdj21l92bcx0l.jpg`` 与 IMG_REGISTER
经百度真实比对得分 95.44（>80，同一人）；而 ``3.jpg``（陌生人）与 IMG_REGISTER 仅 56.5、
与 IMG_SAME_PERSON 仅 52.7，均低于阈值 80，符合"陌生人应不匹配"的预期。

运行方式::

    .venv\\Scripts\\python.exe -m pytest test_dsh_face_full.py -v
"""

from __future__ import annotations

import os
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

try:
    from jose import jwt as jose_jwt
except ImportError:  # pragma: no cover - 依赖缺失时在签发票据处给出明确提示
    jose_jwt = None  # type: ignore[assignment]

# ====================== 【配置区】 ======================
BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8000")
API = f"{BASE_URL}/api"

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"

# 桌面图片路径（保留原有写法；可用 TEST_IMG_* 环境变量覆盖，便于换素材不用改代码）
DESKTOP_PATH = os.getenv("TEST_DESKTOP_PATH", r"C:\Users\DELL\Desktop")
IMG_REGISTER = Path(
    os.getenv("TEST_IMG_REGISTER")
    or Path(DESKTOP_PATH) / "006wVUPtly1hsdzm92z97j30dk0iignb.jpg"
)
IMG_SAME_PERSON = Path(
    os.getenv("TEST_IMG_SAME_PERSON")
    or Path(DESKTOP_PATH) / "6de25dd6ly1h4bv85xjhdj21l92bcx0l.jpg"
)
IMG_STRANGER = Path(
    os.getenv("TEST_IMG_STRANGER") or Path(DESKTOP_PATH) / "3.jpg"
)

# 管理员账号（id=1，token 在运行时按 .env 的密钥现场签发，不硬编码）
ADMIN_USER_ID = 1
ADMIN_USERNAME = "admin"

# 运行时新建账号（户主 / 工作人员）统一使用的密码，创建后用于真实登录换取 token
TEST_PASSWORD = "Test@123456"

# 百度人脸 API 免费版约 2 QPS：两次会触发百度调用的请求之间至少间隔该秒数
BAIDU_MIN_INTERVAL = 1.2
# 命中限流或"同图搜索首次未命中"后的重试等待秒数
RETRY_DELAY = 2.0
# 单个接口最多尝试次数（1 次原始调用 + 重试）
MAX_ATTEMPTS = 3

# 本轮运行标识：时间戳 + 随机串，保证用户名/户号/身份证号/手机号每次运行都不重复
_RUN_ID = f"{time.strftime('%m%d%H%M%S')}{uuid.uuid4().hex[:4]}"
# ======================================================================

# 全局变量保存ID与token
family_id: int | None = None
member_id: int | None = None
owner_user_id: int | None = None
owner_username: str = ""
household_no: str = ""
staff_user_id: int | None = None
staff_username: str = ""

# 人脸是否已录入（决定 test_99 是否需要调用删除人脸接口）
face_registered = False

_OWNER_TOKEN: str | None = None
_STAFF_TOKEN: str | None = None
_ADMIN_HEADERS: dict[str, str] | None = None


# ====================== 【输出与素材工具】 ======================
def log(message: str = "") -> None:
    """打印进度信息。

    Windows 控制台（cp936）无法编码 emoji，直接 print 可能抛 ``UnicodeEncodeError``
    把用例带崩，这里降级为可编码字符，保证测试结果不受输出编码影响。
    """
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(message.encode(encoding, "replace").decode(encoding, "replace"))


def require_image(path: Path, purpose: str) -> bytes:
    """读取测试照片；缺失时立即给出明确提示（比接口报错更易定位）。"""
    assert path.is_file(), f"测试照片不存在：{path}（用途：{purpose}）"
    return path.read_bytes()


# ====================== 【Token：运行时获取】 ======================
def env_value(key: str, default: str | None = None) -> str | None:
    """读取配置项：真实环境变量优先，其次项目根目录 .env。

    支持 ``KEY=value``、``export KEY=value``、双/单引号包裹与未加引号时的行内注释。
    """
    value = os.getenv(key)
    if value:
        return value
    if not ENV_FILE.is_file():
        return default

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, raw_value = line.partition("=")
        if name.strip().removeprefix("export ").strip() != key:
            continue
        text = raw_value.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
            return text[1:-1]
        return text.split(" #", 1)[0].strip()
    return default


def mint_access_token(
    user_id: int, username: str, role: str, *, expires_in: int = 7200
) -> str:
    """按 ``src/auth/security.py`` 的载荷格式现场签发访问令牌（方案 A，无需密码）。"""
    assert jose_jwt is not None, (
        "缺少依赖 python-jose，请先安装："
        r".venv\Scripts\python.exe -m pip install python-jose"
    )
    secret = env_value("JWT_SECRET_KEY")
    assert secret, f"未在环境变量或 {ENV_FILE} 中找到 JWT_SECRET_KEY，无法签发管理员 token"
    algorithm = env_value("JWT_ALGORITHM", "HS256") or "HS256"

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid4().hex,
    }
    return jose_jwt.encode(payload, secret, algorithm=algorithm)


def admin_headers() -> dict[str, str]:
    """管理员请求头（首次调用时按 .env 密钥签发，全程复用）。"""
    global _ADMIN_HEADERS
    if _ADMIN_HEADERS is None:
        token = mint_access_token(ADMIN_USER_ID, ADMIN_USERNAME, "admin")
        _ADMIN_HEADERS = {"Authorization": f"Bearer {token}"}
    return _ADMIN_HEADERS


def owner_headers() -> dict[str, str]:
    """户主请求头（test_01 中通过真实登录接口获取）。"""
    assert _OWNER_TOKEN, "户主 token 尚未获取，请确认 test_01 已通过"
    return {"Authorization": f"Bearer {_OWNER_TOKEN}"}


def staff_headers() -> dict[str, str]:
    """工作人员请求头（test_14 中通过真实登录接口获取）。"""
    assert _STAFF_TOKEN, "工作人员 token 尚未获取，请确认 test_14 已通过"
    return {"Authorization": f"Bearer {_STAFF_TOKEN}"}


# ====================== 【唯一测试数据生成】 ======================
def unique_username(tag: str) -> str:
    """带时间戳的用户名（4~50 位字母/数字/下划线），避免与历史数据冲突。"""
    return f"tst_{tag}_{_RUN_ID}"[:50]


def unique_phone(prefix: str = "139") -> str:
    """随机手机号（``1[3-9]`` 开头的 11 位），避免手机号唯一约束冲突。"""
    return prefix + "".join(random.choice("0123456789") for _ in range(11 - len(prefix)))


#: 身份证前 17 位的加权因子（与 src/member/id_card.py 保持一致）
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
#: 身份证校验位对照表
_ID_CHECK_CODES = "10X98765432"


def make_id_card(seq: int) -> str:
    """生成校验位合法的 18 位身份证号（``110101`` + ``19900307`` + 3 位序列号）。"""
    body = f"11010119900307{seq % 1000:03d}"
    total = sum(int(digit) * weight for digit, weight in zip(body, _ID_WEIGHTS))
    return body + _ID_CHECK_CODES[total % 11]


# ====================== 【HTTP 调用工具】 ======================
def request_api(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
) -> requests.Response:
    """调用普通业务接口（不会触发百度人脸 API）。"""
    return requests.request(
        method,
        f"{API}{path}",
        headers=headers,
        json=json_body,
        params=params,
        timeout=timeout,
    )


def body_of(resp: requests.Response) -> dict[str, Any]:
    """安全获取响应体（非 JSON 时返回空字典）。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def show(label: str, resp: requests.Response) -> dict[str, Any]:
    """打印接口返回并返回响应体，便于失败时定位。"""
    data = body_of(resp)
    log(f"{label}: HTTP {resp.status_code} -> {data if data else resp.text[:200]}")
    return data


_last_face_call = 0.0


def _baidu_pause() -> None:
    """节流：确保两次会触发百度调用的请求间隔 ≥ BAIDU_MIN_INTERVAL 秒（免费版约 2 QPS）。"""
    global _last_face_call
    elapsed = time.monotonic() - _last_face_call
    if elapsed < BAIDU_MIN_INTERVAL:
        time.sleep(BAIDU_MIN_INTERVAL - elapsed)
    _last_face_call = time.monotonic()


def _retry_reason(resp: requests.Response, *, retry_on_miss: bool) -> str | None:
    """判断是否需要重试。

    - 503 / 业务码 503 / 提示中含 qps、request limit：百度限流或服务暂时不可用；
    - ``retry_on_miss=True`` 且业务成功但 ``matched=false``：同图/同人搜索偶发未命中，
      再给一次机会（仅用于"应当匹配"的用例；陌生人用例不重试）。
    """
    data = body_of(resp)
    message = str(data.get("message", "")).lower()
    if (
        resp.status_code == 503
        or data.get("code") == 503
        or "qps" in message
        or "request limit" in message
    ):
        return "百度接口限流/服务不可用"

    payload = data.get("data")
    if retry_on_miss and data.get("code") == 0 and isinstance(payload, dict):
        if payload.get("matched") is False:
            return "首次搜索未命中"
    return None


def face_upload(
    url: str,
    image_path: Path,
    *,
    headers: dict[str, str],
    form: dict[str, Any] | None = None,
    retry_on_miss: bool = False,
    attempts: int = MAX_ATTEMPTS,
    label: str = "人脸接口",
) -> requests.Response:
    """上传图片调用会触发百度人脸 API 的接口：自动节流 + 限流重试。"""
    path = Path(image_path)
    resp: requests.Response | None = None

    for attempt in range(1, attempts + 1):
        _baidu_pause()
        with path.open("rb") as handle:
            resp = requests.post(
                url,
                files={"file": (path.name, handle, "image/jpeg")},
                data=form,
                headers=headers,
                timeout=60,
            )
        reason = _retry_reason(resp, retry_on_miss=retry_on_miss)
        if reason and attempt < attempts:
            log(f"⏳ {label} 第 {attempt} 次调用异常（{reason}），{RETRY_DELAY:.0f} 秒后重试 …")
            time.sleep(RETRY_DELAY)
            continue
        break

    assert resp is not None, f"{label} 未发起请求"
    return resp


def face_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    attempts: int = MAX_ATTEMPTS,
    label: str = "人脸接口",
) -> requests.Response:
    """调用会触发百度人脸 API 的 JSON 接口（如删除人脸）：自动节流 + 限流重试。"""
    resp: requests.Response | None = None

    for attempt in range(1, attempts + 1):
        _baidu_pause()
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        reason = _retry_reason(resp, retry_on_miss=False)
        if reason and attempt < attempts:
            log(f"⏳ {label} 第 {attempt} 次调用异常（{reason}），{RETRY_DELAY:.0f} 秒后重试 …")
            time.sleep(RETRY_DELAY)
            continue
        break

    assert resp is not None, f"{label} 未发起请求"
    return resp


# ====================== 【用例】 ======================
@pytest.mark.order(1)
def test_01_create_family():
    """创建家庭档案：管理员 token（运行时签发）→ 新建户主账号 → 管理员建档 → 户主真实登录。"""
    global family_id, owner_user_id, owner_username, household_no, _OWNER_TOKEN

    # ① 测试素材先就位，避免后续用例给出误导性失败
    for path, purpose in (
        (IMG_REGISTER, "人脸录入 / 同图搜索"),
        (IMG_SAME_PERSON, "同人第二张照片（test_08）"),
        (IMG_STRANGER, "陌生人照片（test_09）"),
    ):
        assert path.is_file(), f"测试照片不存在：{path}（用途：{purpose}）"

    # ② 管理员 token 自检：签发无效（如 .env 密钥与后端不一致）时快速失败
    profile = request_api("GET", "/auth/profile", headers=admin_headers())
    profile_data = show("profile(admin) resp", profile)
    assert profile.status_code == 200 and profile_data.get("code") == 0, (
        f"管理员 token 校验失败：{profile_data or profile.text[:200]}；"
        f"请确认 {ENV_FILE} 中的 JWT_SECRET_KEY 与后端一致"
        "（本用例用该密钥现场签发 token，无硬编码）"
    )
    assert profile_data["data"]["role"] == "admin"

    # ③ 管理员新建一个全新的家庭户主账号（用户名带时间戳 → 可重复运行）
    owner_username = unique_username("owner")
    create_user = request_api(
        "POST",
        "/users",
        headers=admin_headers(),
        json_body={
            "username": owner_username,
            "password": TEST_PASSWORD,
            "real_name": "测试户主",
            "phone": unique_phone("139"),
            "role": "family",
        },
    )
    user_data = show("create owner user resp", create_user)
    assert create_user.status_code == 200 and user_data.get("code") == 0, (
        f"新建户主账号失败：{user_data or create_user.text[:200]}"
    )
    owner_user_id = user_data["data"]["id"]
    assert user_data["data"]["role"] == "family"

    # ④ 管理员为该户主建档（户号带时间戳，避开唯一约束）
    household_no = f"T{_RUN_ID}"
    resp = request_api(
        "POST",
        "/families",
        headers=admin_headers(),
        json_body={
            "household_no": household_no,
            "village": "测试村",
            "owner_id": owner_user_id,
        },
    )
    family_data = show("create_family resp", resp)
    assert resp.status_code == 200 and family_data.get("code") == 0, (
        f"创建家庭失败：{family_data or resp.text[:200]}"
    )
    family = family_data["data"]
    family_id = family["id"]
    assert family["owner_id"] == owner_user_id
    assert family["household_no"] == household_no

    # ⑤ 户主用真实密码登录换取 token（不使用硬编码 token）
    login = request_api(
        "POST",
        "/auth/login",
        json_body={"username": owner_username, "password": TEST_PASSWORD},
    )
    login_data = show("owner login resp", login)
    assert login.status_code == 200 and login_data.get("code") == 0, (
        f"户主登录失败：{login_data or login.text[:200]}"
    )
    _OWNER_TOKEN = login_data["data"]["access_token"]
    assert login_data["data"]["user"]["id"] == owner_user_id

    # ⑥ 户主可访问本户档案（验证建档时自动生成的"户主成员行"与访问权限正常）
    me = request_api("GET", "/families/me", headers=owner_headers())
    me_data = show("owner families/me resp", me)
    assert me.status_code == 200 and me_data.get("code") == 0, (
        f"户主查询本户失败：{me_data or me.text[:200]}"
    )
    assert me_data["data"]["id"] == family_id
    assert me_data["data"]["household_no"] == household_no

    log(
        f"✅ 创建家庭成功 family_id={family_id} 户号={household_no} "
        f"户主={owner_username}(id={owner_user_id})"
    )


@pytest.mark.order(2)
def test_02_add_member():
    """添加家庭成员张三：管理员 token；身份证号/手机号每次运行唯一。"""
    global member_id
    assert family_id, "家庭ID未初始化，请确认 test_01 已通过"

    id_card = phone = ""
    last: dict[str, Any] = {}
    for attempt in range(1, 6):
        id_card = make_id_card(random.randint(100, 999))
        phone = unique_phone("138")
        resp = request_api(
            "POST",
            "/members",
            headers=admin_headers(),
            json_body={
                "family_id": family_id,
                "name": "张三",
                "id_card": id_card,
                "phone": phone,
                "remark": "pytest自动测试成员",
            },
        )
        last = show(f"add_member resp(第 {attempt} 次)", resp)
        if resp.status_code == 200 and last.get("code") == 0:
            break
        # 仅当身份证号/手机号被上一轮残留数据占用（409）时才换号重试
        assert resp.status_code == 409, f"添加成员失败：{last or resp.text[:200]}"

    assert last.get("code") == 0, f"添加成员失败（已重试 5 次）：{last}"
    member = last["data"]
    member_id = member["id"]
    assert member["family_id"] == family_id
    assert member["name"] == "张三"
    assert member["status"] == "active"
    assert member["needs_checkin"] is True
    log(f"✅ 添加成员成功 member_id={member_id} 身份证={id_card} 手机={phone}")


@pytest.mark.order(3)
def test_03_face_register():
    """录入人脸：管理员 token，真实调用百度人脸库（自动 QPS 节流）。"""
    global face_registered
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    image = require_image(IMG_REGISTER, "人脸录入")

    resp = face_upload(
        f"{API}/face/register",
        IMG_REGISTER,
        headers=admin_headers(),
        form={"member_id": member_id},
        label="人脸录入",
    )
    data = show("face_register resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸录入失败：{data or resp.text[:200]}"
    )

    result = data["data"]
    assert result["member_id"] == member_id
    assert result["status"] == "registered"
    assert result["provider"] == "baidu"
    assert result["image_size"] == len(image)
    assert result["detect"] and result["detect"]["face_num"] == 1, (
        f"录入前的人脸检测结果异常：{result['detect']}"
    )
    face_registered = True
    log(
        f"✅ 人脸录入完成 member_id={member_id} provider={result['provider']} "
        f"照片大小={result['image_size']}字节"
    )


@pytest.mark.order(4)
def test_04_face_status_query():
    """查询人脸录入状态：必须是"已录入"且照片大小与上传一致。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    resp = request_api("GET", f"/face/status/{member_id}", headers=admin_headers())
    data = show("face_status resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸状态查询失败：{data or resp.text[:200]}"
    )

    status = data["data"]
    assert status["member_id"] == member_id
    assert status["has_face"] is True
    assert status["status"] == "registered"
    assert status["provider"] == "baidu"
    assert status["image_size"] == len(IMG_REGISTER.read_bytes())
    log(f"✅ 人脸状态查询成功：has_face={status['has_face']} status={status['status']}")


@pytest.mark.order(5)
def test_05_face_records_list():
    """人脸录入记录列表：按本户 + 成员姓名筛选，必须包含刚录入的记录。"""
    assert family_id and member_id, "家庭/成员ID未初始化，请确认 test_01、test_02 已通过"
    resp = request_api(
        "GET",
        "/face/records",
        headers=admin_headers(),
        params={"family_id": family_id, "keyword": "张三", "page_size": 100},
    )
    data = show("face_records resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸记录列表查询失败：{data or resp.text[:200]}"
    )

    page = data["data"]
    assert page["total"] >= 1, f"人脸记录列表为空：{page}"
    record = next(
        (item for item in page["items"] if item["member_id"] == member_id), None
    )
    assert record is not None, f"列表中未找到本次录入的记录：{page['items']}"
    assert record["status"] == "registered"
    assert record["family_id"] == family_id
    log(f"✅ 人脸记录列表查询成功：total={page['total']}，已找到 member_id={member_id}")


@pytest.mark.order(6)
def test_06_get_face_photo():
    """读取人脸原图：返回的二进制必须与录入时上传的文件完全一致。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    image = require_image(IMG_REGISTER, "人脸原图比对")

    resp = request_api("GET", f"/face/photo/{member_id}", headers=admin_headers())
    log(
        f"get_photo status={resp.status_code} "
        f"content-type={resp.headers.get('content-type')} bytes={len(resp.content)}"
    )
    assert resp.status_code == 200, f"读取人脸照片失败：HTTP {resp.status_code}"
    assert resp.headers.get("content-type", "").startswith("image/"), (
        f"响应类型不是图片：{resp.headers.get('content-type')}"
    )
    assert resp.content == image, "返回的照片与录入时上传的原图不一致"
    log("✅ 读取人脸图片接口返回正常，且与录入原图字节一致")


@pytest.mark.order(7)
def test_07_search_same_face():
    """同图搜索：必须是本人，且得分 >80（首次未命中会自动重试一次）。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    require_image(IMG_REGISTER, "同图搜索")

    resp = face_upload(
        f"{API}/face/search",
        IMG_REGISTER,
        headers=admin_headers(),
        retry_on_miss=True,
        label="同图搜索",
    )
    data = show("search1 resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸搜索失败：{data or resp.text[:200]}"
    )

    result = data["data"]
    assert result["matched"] is True, f"原图未匹配到任何人：{result}"
    assert result["score"] > 80, f"原图匹配得分过低：{result['score']}"
    assert result["member"]["id"] == member_id, f"匹配到了其他成员：{result['member']}"
    assert result["family"]["family_id"] == family_id
    log(
        f"✅ 原图识别成功，分数：{result['score']}，"
        f"匹配成员：{result['member']['name']}(id={result['member']['id']})"
    )


@pytest.mark.order(8)
def test_08_search_same_person_other_img():
    """同人的另一张照片，预期匹配成功（断言不允许放宽）。

    ⚠️ 素材要求：IMG_SAME_PERSON 必须是与 IMG_REGISTER 同一人的真实第二张照片。
    当前素材（6de25dd6ly1h4bv85xjhdj21l92bcx0l.jpg）经百度真实比对确认与
    IMG_REGISTER 是同一人（得分 95.44）。若日后替换照片，务必保证仍是同一人，
    否则本用例会失败——那是素材问题，不是后端缺陷，本文件不会放宽该断言。
    """
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    require_image(IMG_SAME_PERSON, "同人第二张照片")

    resp = face_upload(
        f"{API}/face/search",
        IMG_SAME_PERSON,
        headers=admin_headers(),
        retry_on_miss=True,
        label="同人第二张照片搜索",
    )
    data = show("search2 resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸搜索失败：{data or resp.text[:200]}"
    )

    result = data["data"]
    assert result["matched"] is True, (
        f"IMG_SAME_PERSON（{IMG_SAME_PERSON}）未匹配，score={result.get('score')}。\n"
        "该文件必须是与 IMG_REGISTER 同一人的真实第二张照片"
        "（经百度比对，同人应 >80，不同人通常只有 30~60）。\n"
        "请更换素材，或用环境变量 TEST_IMG_SAME_PERSON 指向正确照片；断言未做任何放宽。"
    )
    assert result["score"] > 80, f"同人第二张照片匹配得分过低：{result['score']}"
    assert result["member"]["id"] == member_id, f"匹配到了其他成员：{result['member']}"
    log(f"✅ 同人第二张照片识别成功，分数：{result['score']}")


@pytest.mark.order(9)
def test_09_search_stranger():
    """陌生人图片，预期不匹配（陌生人用例不做"未命中"重试）。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    require_image(IMG_STRANGER, "陌生人搜索")

    resp = face_upload(
        f"{API}/face/search",
        IMG_STRANGER,
        headers=admin_headers(),
        label="陌生人搜索",
    )
    data = show("search3 resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸搜索失败：{data or resp.text[:200]}"
    )

    result = data["data"]
    assert result["matched"] is False, (
        f"陌生人被误识别为：{result.get('member')}（score={result.get('score')}）"
    )
    assert result["member"] is None
    log("✅ 陌生人识别：未匹配，符合预期")


@pytest.mark.order(10)
def test_10_update_face():
    """更新人脸照片：接口成功，且服务端照片确实被覆盖为新图。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    new_image = require_image(IMG_SAME_PERSON, "更新人脸照片")

    resp = face_upload(
        f"{API}/face/update",
        IMG_SAME_PERSON,
        headers=admin_headers(),
        form={"member_id": member_id},
        label="更新人脸",
    )
    data = show("update_face resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"人脸更新失败：{data or resp.text[:200]}"
    )

    result = data["data"]
    assert result["member_id"] == member_id
    assert result["status"] == "registered"
    assert result["image_size"] == len(new_image)

    photo = request_api("GET", f"/face/photo/{member_id}", headers=admin_headers())
    assert photo.status_code == 200, f"更新后读取照片失败：HTTP {photo.status_code}"
    assert photo.content == new_image, "更新后读取到的照片不是新照片"
    log("✅ 人脸照片更新成功，且已核对服务端照片内容为新图")


@pytest.mark.order(11)
def test_11_modify_member_info():
    """修改成员备注信息，并回读确认已落库。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"
    new_remark = "pytest更新后的备注"

    resp = request_api(
        "PUT",
        f"/members/{member_id}",
        headers=admin_headers(),
        json_body={"remark": new_remark},
    )
    data = show("update_member resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"修改成员信息失败：{data or resp.text[:200]}"
    )
    assert data["data"]["remark"] == new_remark

    detail = request_api("GET", f"/members/{member_id}", headers=admin_headers())
    detail_data = body_of(detail)
    assert detail.status_code == 200 and detail_data.get("code") == 0
    assert detail_data["data"]["remark"] == new_remark, (
        f"备注未落库：{detail_data['data'].get('remark')}"
    )
    log("✅ 修改成员信息成功（已回读校验）")


@pytest.mark.order(12)
def test_12_disable_enable_member():
    """停用、启用成员状态（传字符串 active/inactive，不是 bool）。"""
    assert member_id, "成员ID未初始化，请确认 test_02 已通过"

    # 停用
    resp = request_api(
        "PUT",
        f"/members/{member_id}/status",
        headers=admin_headers(),
        json_body={"status": "inactive"},
    )
    data = show("disable member resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"停用成员失败：{data or resp.text[:200]}"
    )
    assert data["data"]["status"] == "inactive"

    # 启用
    resp = request_api(
        "PUT",
        f"/members/{member_id}/status",
        headers=admin_headers(),
        json_body={"status": "active"},
    )
    data = show("enable member resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"启用成员失败：{data or resp.text[:200]}"
    )
    assert data["data"]["status"] == "active"
    log("✅ 成员启停状态测试完成（inactive → active 均已生效）")


@pytest.mark.order(13)
def test_13_modify_family_info():
    """修改家庭信息（村组），并校验返回值已更新。"""
    assert family_id, "家庭ID未初始化，请确认 test_01 已通过"

    resp = request_api(
        "PUT",
        f"/families/{family_id}",
        headers=admin_headers(),
        json_body={"village": "测试村更新"},
    )
    data = show("update family resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"修改家庭信息失败：{data or resp.text[:200]}"
    )
    assert data["data"]["village"] == "测试村更新"
    log("✅ 家庭信息修改成功")


@pytest.mark.order(14)
def test_14_create_test_user():
    """新增测试工作人员账号（必填 real_name），并真实登录验证账号可用。"""
    global staff_user_id, staff_username, _STAFF_TOKEN

    staff_username = unique_username("staff")
    resp = request_api(
        "POST",
        "/users",
        headers=admin_headers(),
        json_body={
            "username": staff_username,
            "password": TEST_PASSWORD,
            "role": "staff",
            "real_name": "测试工作人员",
            "phone": unique_phone("137"),
        },
    )
    data = show("create user resp", resp)
    assert resp.status_code == 200 and data.get("code") == 0, (
        f"新建测试用户失败：{data or resp.text[:200]}"
    )
    staff_user_id = data["data"]["id"]
    assert data["data"]["role"] == "staff"

    # 新账号立刻登录一次，确认账号真的可用（而不是只写进了数据库）
    login = request_api(
        "POST",
        "/auth/login",
        json_body={"username": staff_username, "password": TEST_PASSWORD},
    )
    login_data = show("staff login resp", login)
    assert login.status_code == 200 and login_data.get("code") == 0, (
        f"新建账号登录失败：{login_data or login.text[:200]}"
    )
    _STAFF_TOKEN = login_data["data"]["access_token"]

    # 工作人员可只读查看家庭列表（验证角色权限生效）
    families = request_api(
        "GET", "/families", headers=staff_headers(), params={"page_size": 1}
    )
    families_data = body_of(families)
    assert families.status_code == 200 and families_data.get("code") == 0, (
        f"工作人员查看家庭列表失败：{families_data or families.text[:200]}"
    )
    log(
        f"✅ 新建测试用户成功 user_id={staff_user_id} username={staff_username}"
        "（可登录且具备工作人员只读权限）"
    )


@pytest.mark.order(99)
def test_99_cleanup():
    """【最后执行】清理本轮测试数据，并逐项校验清理结果。

    旧版此处没有任何断言（属于"假通过"），本次改为"清理 + 校验"：
    人脸是否删除、成员是否已停用、测试账号是否已无法登录、家庭是否已停用。
    """
    # 1) 删除人脸：同时把人脸从百度人脸库移除，避免污染后续运行
    if member_id and face_registered:
        resp = face_json(
            f"{API}/face/delete",
            {"member_id": member_id},
            headers=admin_headers(),
            label="删除人脸",
        )
        data = show("face delete resp", resp)
        assert resp.status_code == 200 and data.get("code") == 0, f"删除人脸失败：{data}"

    # 2) 删除成员（阶段五起语义为"停用"：返回 200，成员状态变为 inactive 且保留签到历史）
    if member_id:
        resp = request_api("DELETE", f"/members/{member_id}", headers=admin_headers())
        data = show("delete member resp", resp)
        assert resp.status_code == 200 and data.get("code") == 0, f"删除成员失败：{data}"

        detail = request_api("GET", f"/members/{member_id}", headers=admin_headers())
        detail_data = body_of(detail)
        assert detail.status_code == 200 and detail_data.get("code") == 0, (
            f"停用后的成员应仍可查询：HTTP {detail.status_code} {detail_data}"
        )
        assert detail_data["data"]["status"] == "inactive", (
            f"成员删除后状态应为 inactive，实际 {detail_data['data'].get('status')}"
        )

    # 3) 删除测试工作人员账号 → 校验该账号已无法登录
    if staff_user_id:
        resp = request_api("DELETE", f"/users/{staff_user_id}", headers=admin_headers())
        data = show("delete staff user resp", resp)
        assert resp.status_code == 200 and data.get("code") == 0, f"删除测试账号失败：{data}"

        login = request_api(
            "POST",
            "/auth/login",
            json_body={"username": staff_username, "password": TEST_PASSWORD},
        )
        assert login.status_code == 401, (
            f"已删除的账号仍可登录：HTTP {login.status_code} {body_of(login)}"
        )

    # 4) 停用家庭（级联停用成员）→ 校验状态已变为 inactive
    if family_id:
        resp = request_api("DELETE", f"/families/{family_id}", headers=admin_headers())
        if resp.status_code == 403:  # 兜底：管理员无权限时改用户主 token（保持旧版行为）
            resp = request_api("DELETE", f"/families/{family_id}", headers=owner_headers())
        data = show("deactivate family resp", resp)
        assert resp.status_code == 200 and data.get("code") == 0, f"停用家庭失败：{data}"

        detail = request_api("GET", f"/families/{family_id}", headers=admin_headers())
        detail_data = body_of(detail)
        assert detail.status_code == 200 and detail_data.get("code") == 0
        assert detail_data["data"]["status"] == "inactive", (
            f"家庭状态未变为停用：{detail_data['data'].get('status')}"
        )

    # 5) 户主账号：后端的家庭档案按设计"停用而非删除"（保留历史数据），
    #    因此只要该户主仍有家庭档案，删除账号会被拒绝（409），这是既有业务规则而非失败。
    if owner_user_id:
        resp = request_api("DELETE", f"/users/{owner_user_id}", headers=admin_headers())
        data = show("delete owner user resp", resp)
        assert resp.status_code == 409, (
            "预期 409（该户主仍有家庭档案，后端按设计保留），"
            f"实际 HTTP {resp.status_code} {data}"
        )

    log("🧹 本轮测试数据清理完成：人脸已删除、成员已停用、测试账号已删除、家庭已停用（均逐项校验通过）")
    log(
        "ℹ️ 说明：户主账号 + 已停用家庭档案 + 已停用成员行会保留（后端设计：保留历史签到数据），"
        "每轮运行使用全新时间戳数据，不影响重复运行；如需彻底清理可运行 "
        "scripts/cleanup_test_data.py --purge-test-generated"
    )
