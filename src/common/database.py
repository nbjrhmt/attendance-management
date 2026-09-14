"""数据库模块：SQLAlchemy 引擎、Session 会话工厂与 ORM 基础模型。

主要对象：

- ``engine``：全局数据库引擎（连接 MySQL，使用 PyMySQL 驱动）
- ``SessionLocal``：Session 会话工厂
- ``Base``：所有 ORM 模型的声明式基类
- ``BaseModel``：带 ``id`` / ``created_at`` / ``updated_at`` 的抽象基础模型
- ``get_db``：FastAPI 依赖注入使用的会话生成器
- ``session_scope``：脚本、定时任务使用的会话上下文管理器
- ``init_db``：按模型元数据创建数据表
- ``ping_database``：数据库连通性检测

用法示例::

    # 定义业务模型
    from sqlalchemy import String
    from sqlalchemy.orm import Mapped, mapped_column
    from src.common.database import BaseModel

    class Family(BaseModel):
        __tablename__ = "family"

        household_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # 在接口中使用会话
    from fastapi import Depends
    from sqlalchemy.orm import Session
    from src.common.database import get_db

    @router.get("/families")
    def list_families(db: Session = Depends(get_db)):
        return db.query(Family).all()
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, create_engine, func, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from src.config.settings import settings

logger = logging.getLogger(__name__)

#: 需要在建表前导入的 ORM 模型模块（新增业务模型时在此登记）
_MODEL_MODULES: tuple[str, ...] = (
    "src.user.models",
    "src.family.models",
    "src.member.models",
    "src.face.models",
    "src.event.models",
    "src.checkin.models",
    "src.leave.models",
    "src.log.models",
    "src.assistant.models",
)

__all__ = [
    "Base",
    "BaseModel",
    "SessionLocal",
    "dispose_engine",
    "engine",
    "get_db",
    "init_db",
    "ping_database",
    "session_scope",
]


def _build_engine_kwargs() -> dict[str, Any]:
    """根据数据库类型构造引擎参数。

    SQLite（单元测试常用）不支持连接池容量参数，需要单独处理。
    """
    kwargs: dict[str, Any] = {
        "echo": settings.DB_ECHO,
        "pool_pre_ping": True,
    }

    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_recycle=settings.DB_POOL_RECYCLE,
        )

    return kwargs


#: 全局数据库引擎（惰性连接，导入时不会真正建立数据库连接）
engine: Engine = create_engine(settings.database_url, **_build_engine_kwargs())

#: Session 会话工厂
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。

    仅承载元数据（``Base.metadata``），业务模型请继承 :class:`BaseModel`。
    """


class BaseModel(Base):
    """ORM 基础模型，统一提供主键与时间戳字段。

    :cvar id: 主键，自增整数
    :cvar created_at: 创建时间，插入时由数据库自动填充
    :cvar updated_at: 更新时间，插入时填充，更新时自动刷新
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        comment="主键ID",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={getattr(self, 'id', None)}>"


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入：为每个请求提供独立的数据库会话，请求结束后自动关闭。

    用法::

        @router.get("/items")
        def list_items(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """会话上下文管理器：正常结束时提交事务，异常时回滚。

    适用于脚本、定时任务等非请求场景::

        with session_scope() as db:
            db.add(Family(household_name="张三"))
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """按 ORM 模型元数据创建数据表（已存在的表不会被修改）。

    开发环境可直接调用；生产环境建议使用 Alembic 迁移工具管理表结构变更。

    注意：必须先导入各 ORM 模型模块，模型类才会注册到 ``Base.metadata``；
    仅导入包（``src.user``）只会执行 ``__init__.py``，不会加载 ``models.py``。
    """
    for module_name in _MODEL_MODULES:
        importlib.import_module(module_name)

    Base.metadata.create_all(bind=engine)
    logger.info(
        "数据库表结构初始化完成：%s（共 %d 张表）",
        settings.DB_NAME,
        len(Base.metadata.tables),
    )


def ping_database() -> bool:
    """检测数据库连通性，供启动自检或健康检查使用。

    :return: 连接可用返回 ``True``，否则返回 ``False``
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.error("数据库连接失败：%s", exc)
        return False
    return True


def dispose_engine() -> None:
    """释放连接池中的所有连接，应用关闭时调用。"""
    engine.dispose()
    logger.info("数据库连接池已释放")
