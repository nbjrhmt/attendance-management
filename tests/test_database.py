"""数据库层测试。

重点回归阶段二遗留缺陷：``init_db()`` 曾使用 ``from src import user`` 导入业务模块，
而包导入只执行 ``__init__.py``，导致 ``Base.metadata`` 为空、``create_all()`` 建不出任何表。
"""

from __future__ import annotations

import importlib

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import configure_mappers
from sqlalchemy.pool import StaticPool

import src.common.database as database
from src.common.database import Base
from src.family.models import Family
from src.member.models import FamilyMember

#: 阶段三结束后应存在的业务表
EXPECTED_TABLES = {"sys_user", "family", "family_member"}


def test_init_db_model_modules_are_importable() -> None:
    """init_db 登记的模型模块必须可导入，且模型已注册到 Base.metadata。"""
    for module_name in database._MODEL_MODULES:
        importlib.import_module(module_name)

    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_model_modules_point_to_models_submodule() -> None:
    """回归保护：登记项必须是 ``*.models``，而不是包本身。"""
    assert all(name.endswith(".models") for name in database._MODEL_MODULES)


def test_init_db_creates_all_tables(monkeypatch) -> None:
    """init_db() 应在目标库中创建全部业务表。"""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(database, "engine", test_engine)
    try:
        database.init_db()
        tables = set(inspect(test_engine).get_table_names())
        assert EXPECTED_TABLES <= tables, f"缺少数据表：{EXPECTED_TABLES - tables}"
    finally:
        test_engine.dispose()


def test_orm_relationships_resolve() -> None:
    """ORM 关系可正常配置（Family.owner 与成员外键目标均能解析）。"""
    configure_mappers()

    family_relationships = set(inspect(Family).relationships.keys())
    assert "owner" in family_relationships

    member_fks = {fk.target_fullname for fk in FamilyMember.__table__.foreign_keys}
    assert "family.id" in member_fks
    assert "sys_user.id" in member_fks
