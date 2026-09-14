"""pytest 公共夹具。

设计要点：

- 每个测试用例使用**独立的内存 SQLite 数据库**，不依赖本地 MySQL/Redis，可离线运行；
- 通过覆盖 ``get_db`` 依赖，让接口使用测试数据库会话（生产代码无需为测试做任何妥协）；
- ``StaticPool`` 保证内存数据库在同一连接池内共享，多个会话看到同一份数据；
- 提供 ``make_user`` 工厂与 ``*_headers`` 夹具，便于构造不同角色的登录态。

运行方式::

    pytest -v
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Generator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# 确保项目根目录在 sys.path 中，便于 pytest 直接从任意目录运行
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import app  # noqa: E402
from src.auth.security import create_access_token, hash_password  # noqa: E402
from src.common.database import Base, get_db  # noqa: E402
from src.config.settings import settings  # noqa: E402
from src.event import crud as event_crud  # noqa: E402
from src.event.models import Event, EventStatus  # noqa: E402
from src.family import crud as family_crud  # noqa: E402
from src.family import service as family_service  # noqa: E402
from src.family.models import Family  # noqa: E402
from src.face.baidu_client import BaiduFaceClient  # noqa: E402
from src.face.provider import BaiduFaceProvider, get_face_provider  # noqa: E402
from src.member import crud as member_crud  # noqa: E402
from src.member.models import (  # noqa: E402
    FamilyMember,
    Gender,
    MemberRelation,
    MemberStatus,
)
from src.user.models import User, UserRole, UserStatus  # noqa: E402
from tests.baidu_mock import MockBaiduFaceApi  # noqa: E402
from tests.helpers import (  # noqa: E402
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    FAMILY_PASSWORD,
    FAMILY_USERNAME,
    STAFF_PASSWORD,
    STAFF_USERNAME,
    auth_headers,
)


@pytest.fixture()
def db_engine() -> Generator[Engine, None, None]:
    """每个测试用例独立的 SQLite 内存数据库引擎。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    """与测试数据库绑定的会话工厂（参数与生产环境保持一致）。"""
    return sessionmaker(
        bind=db_engine,
        class_=Session,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )


@pytest.fixture()
def db(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """直接操作数据库的会话（用于断言落库结果）。"""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Generator[TestClient, None, None]:
    """覆盖数据库依赖后的测试客户端。"""

    def override_get_db() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def make_user(session_factory: sessionmaker[Session]) -> Callable[..., User]:
    """创建测试用户的工厂函数。

    返回的 ORM 对象已提交并脱离会话（``expire_on_commit=False``），可直接读取字段或签发令牌。
    """

    def _make_user(
        username: str,
        password: str,
        *,
        role: UserRole = UserRole.FAMILY,
        status: UserStatus = UserStatus.ACTIVE,
        real_name: str = "测试用户",
        phone: str | None = None,
    ) -> User:
        session = session_factory()
        try:
            user = User(
                username=username,
                password_hash=hash_password(password),
                real_name=real_name,
                phone=phone,
                role=role,
                status=status,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user
        finally:
            session.close()

    return _make_user


@pytest.fixture()
def admin_user(make_user: Callable[..., User]) -> User:
    """管理员账号。"""
    return make_user(
        ADMIN_USERNAME,
        ADMIN_PASSWORD,
        role=UserRole.ADMIN,
        real_name="测试管理员",
        phone="13800000001",
    )


@pytest.fixture()
def family_user(make_user: Callable[..., User]) -> User:
    """家庭用户（户主）账号。"""
    return make_user(
        FAMILY_USERNAME,
        FAMILY_PASSWORD,
        role=UserRole.FAMILY,
        real_name="测试户主",
        phone="13800000002",
    )


@pytest.fixture()
def staff_user(make_user: Callable[..., User]) -> User:
    """工作人员账号。"""
    return make_user(
        STAFF_USERNAME,
        STAFF_PASSWORD,
        role=UserRole.STAFF,
        real_name="测试工作人员",
        phone="13800000003",
    )


@pytest.fixture()
def admin_headers(admin_user: User) -> dict[str, str]:
    """管理员登录态请求头。"""
    return auth_headers(create_access_token(admin_user))


@pytest.fixture()
def family_headers(family: Family, family_user: User) -> dict[str, str]:
    """家庭用户（户主）登录态请求头。

    依赖 ``family`` 夹具：使用该请求头的测试默认"该户主已有家庭档案"，
    这与真实注册流程一致（户主注册即自动建档）。
    """
    return auth_headers(create_access_token(family_user))


@pytest.fixture()
def staff_headers(staff_user: User) -> dict[str, str]:
    """工作人员登录态请求头。"""
    return auth_headers(create_access_token(staff_user))


# ----------------------------------------------------------------------
# 家庭与成员（阶段三）
# ----------------------------------------------------------------------
@pytest.fixture()
def family(session_factory: sessionmaker[Session], family_user: User) -> Family:
    """为 ``family_user`` 创建家庭档案（含户主成员行），返回已提交的家庭对象。"""
    session = session_factory()
    try:
        created = family_crud.create_family(
            session,
            owner_id=family_user.id,
            address="测试村 1 号",
            village="测试村",
            contact_phone="13800000002",
        )
        family_service.ensure_householder_member(session, created, family_user)
        return created
    finally:
        session.close()


@pytest.fixture()
def member(session_factory: sessionmaker[Session], family: Family) -> FamilyMember:
    """为 ``family`` 添加一名普通成员（儿子，需要签到）。"""
    session = session_factory()
    try:
        return member_crud.create_member(
            session,
            family_id=family.id,
            name="李小明",
            relation=MemberRelation.SON,
            gender=Gender.MALE,
            needs_checkin=True,
        )
    finally:
        session.close()


@pytest.fixture()
def register_householder(client: TestClient) -> Callable[..., dict]:
    """通过注册接口创建"户主 + 家庭档案"，并返回用户信息与登录请求头。

    用于需要多个独立家庭的场景（例如校验"不能操作他人家庭"）。
    """

    def _register(
        username: str,
        password: str = "hushu123456",
        *,
        real_name: str = "测试户主",
        phone: str | None = None,
        family: dict | None = None,
    ) -> dict:
        body: dict = {
            "username": username,
            "password": password,
            "real_name": real_name,
        }
        if phone is not None:
            body["phone"] = phone
        if family is not None:
            body["family"] = family

        response = client.post("/api/auth/register", json=body)
        payload = response.json()
        assert payload["code"] == 0, f"注册失败：{payload}"

        login = client.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).json()
        assert login["code"] == 0, f"登录失败：{login}"

        return {
            "user": payload["data"],
            "headers": auth_headers(login["data"]["access_token"]),
            "password": password,
        }

    return _register


# ----------------------------------------------------------------------
# 活动与签到（阶段五）
# ----------------------------------------------------------------------
@pytest.fixture()
def make_event(session_factory: sessionmaker[Session]) -> Callable[..., Event]:
    """创建签到活动的工厂。

    默认时间窗口覆盖当前时间（开始 -30 分钟 ~ 结束 +30 分钟），便于直接测试签到；
    通过 ``start_offset_minutes`` / ``end_offset_minutes`` 可构造"未开始 / 已结束"场景。
    """

    def _make_event(
        name: str = "测试活动",
        *,
        start_offset_minutes: int = -30,
        end_offset_minutes: int = 30,
        late_threshold_minutes: int = 15,
        status: EventStatus = EventStatus.PENDING,
        description: str | None = None,
        location: str | None = "村委会",
    ) -> Event:
        now = datetime.now()
        session = session_factory()
        try:
            return event_crud.create_event(
                session,
                name=name,
                description=description,
                location=location,
                start_time=now + timedelta(minutes=start_offset_minutes),
                end_time=now + timedelta(minutes=end_offset_minutes),
                late_threshold_minutes=late_threshold_minutes,
                status=status,
            )
        finally:
            session.close()

    return _make_event


@pytest.fixture()
def event(make_event: Callable[..., Event]) -> Event:
    """待开始的签到活动（时间窗口覆盖当前时间）。"""
    return make_event("村民大会")


@pytest.fixture()
def active_event(make_event: Callable[..., Event]) -> Event:
    """进行中的签到活动（时间窗口覆盖当前时间）。"""
    return make_event("村民大会", status=EventStatus.ACTIVE)


@pytest.fixture()
def make_member(session_factory: sessionmaker[Session]) -> Callable[..., FamilyMember]:
    """为指定家庭创建成员的工厂（默认：正常状态、需要签到）。"""

    def _make_member(
        family_id: int,
        name: str = "测试成员",
        *,
        relation: MemberRelation = MemberRelation.OTHER,
        needs_checkin: bool = True,
        status: MemberStatus = MemberStatus.ACTIVE,
        phone: str | None = None,
        id_card: str | None = None,
    ) -> FamilyMember:
        session = session_factory()
        try:
            return member_crud.create_member(
                session,
                family_id=family_id,
                name=name,
                relation=relation,
                needs_checkin=needs_checkin,
                status=status,
                phone=phone,
                id_card=id_card,
            )
        finally:
            session.close()

    return _make_member


# ----------------------------------------------------------------------
# 人脸识别（阶段四）
# ----------------------------------------------------------------------
@pytest.fixture()
def face_upload_dir(monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    """把人脸照片目录指向项目内的临时目录（``uploads/`` 已在 .gitignore 中）。"""
    base = PROJECT_ROOT / "uploads" / "_pytest"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "FACE_UPLOAD_DIR", "uploads/_pytest")
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def baidu_api() -> MockBaiduFaceApi:
    """离线版百度人脸识别服务替身。"""
    return MockBaiduFaceApi()


@pytest.fixture()
def baidu_provider(baidu_api: MockBaiduFaceApi) -> BaiduFaceProvider:
    """接入替身传输层的**真实**百度人脸提供方。"""
    client = BaiduFaceClient(
        api_key=baidu_api.api_key,
        secret_key=baidu_api.secret_key,
        http_client=baidu_api.client(),
    )
    return BaiduFaceProvider(client=client, group_id=baidu_api.group_id)


@pytest.fixture()
def client_face(
    client: TestClient,
    baidu_provider: BaiduFaceProvider,
    face_upload_dir: Path,
) -> Generator[TestClient, None, None]:
    """把人脸提供方替换为离线替身的测试客户端（照片写入临时目录）。"""
    app.dependency_overrides[get_face_provider] = lambda: baidu_provider
    yield client
