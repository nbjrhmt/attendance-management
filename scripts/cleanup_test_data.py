"""测试数据清理脚本（一次性残留清理 + 测试新增数据清理）。

两种清理模式
------------
1. **历史残留（默认，幂等）**：早期版本的 ``test_dsh_face_full.py`` 使用固定数据
   （owner_id=2 + 户号 F20260913001），且家庭/成员停用采用"软删除"
   （只改 status，不释放唯一约束），因此数据库里残留了：

   - ``family`` id=1（owner_id=2，户号 "string"，inactive）
   - ``family`` id=3（owner_id=6，户号 F20260913001，inactive）
   - ``family_member`` id=1/2/6（属于上述两个家庭，inactive）

   这些残留会占用 ``family.owner_id`` 与 ``family.household_no`` 的唯一约束，
   导致"用 owner_id=2 + 户号 F20260913001 建家"返回 409。
   脚本按外键依赖顺序物理删除：``face_record`` → ``family_member`` → ``family``。
   **不会**触碰 ``sys_user``（id=1/2/6 等）与其它活跃业务数据。

2. **测试新增数据（``--purge-test-generated``）**：新的测试每次运行都会新建
   带时间戳的户主账号（``tst_owner_*`` / ``tst_smoke*_*``）、工作人员账号（``tst_staff_*``）、
   家庭档案、户主成员行、签到活动、签到记录、请假申请与相关操作日志。
   这些数据不影响重复运行（用户名/户号/手机号每次都不同），但会逐轮累积——
   因为后端的家庭档案按设计"停用而非删除"，户主账号随之无法删除
   （``DELETE /api/users/{id}`` 返回 409）。
   该模式按序删除这些测试自建数据（以及指向它们的 ``operation_log`` 行，
   用户名以 ``tst`` 开头是唯一识别依据），照片文件（``uploads/face/<family_id>/``）
   不做删除。

用法::

    .venv\\Scripts\\python.exe scripts\\cleanup_test_data.py                       # 预览历史残留清理
    .venv\\Scripts\\python.exe scripts\\cleanup_test_data.py --yes                 # 执行历史残留清理
    .venv\\Scripts\\python.exe scripts\\cleanup_test_data.py --purge-test-generated # 预览测试数据清理
    .venv\\Scripts\\python.exe scripts\\cleanup_test_data.py --purge-test-generated --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 与 create_admin.py / seed_ai_knowledge.py 保持一致：
# 直接以 `python scripts/cleanup_test_data.py` 运行时，把项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from src.common.database import engine  # noqa: E402

#: 历史残留：待清理的家庭ID（均为 inactive 的测试残留）
TARGET_FAMILY_IDS: tuple[int, ...] = (1, 3)

#: 历史残留：待清理的成员ID（属于上述家庭，均为 inactive）
TARGET_MEMBER_IDS: tuple[int, ...] = (1, 2, 6)

#: 测试自建用户名前缀（test_dsh_face_full.py 生成的账号）
TEST_USERNAME_PREFIX = "tst"


def _int_list(values: tuple[int, ...]) -> str:
    """把整数序列拼成 SQL IN 列表（值均为本文件内定义的整数常量）。"""
    return ", ".join(str(int(value)) for value in values)


FIDS = _int_list(TARGET_FAMILY_IDS)
MIDS = _int_list(TARGET_MEMBER_IDS)

#: 历史残留清理：顺序不可颠倒（face_record 引用 member，member 引用 family）
LEGACY_STEPS: tuple[tuple[str, str], ...] = (
    ("face_record", f"DELETE FROM face_record WHERE member_id IN ({MIDS})"),
    (
        "family_member",
        "DELETE FROM family_member "
        f"WHERE id IN ({MIDS}) AND family_id IN ({FIDS}) AND status = 'inactive'",
    ),
    ("family", f"DELETE FROM family WHERE id IN ({FIDS}) AND status = 'inactive'"),
)

#: 历史残留预览
LEGACY_PREVIEW: tuple[tuple[str, str], ...] = (
    ("face_record", f"SELECT id, member_id, status FROM face_record WHERE member_id IN ({MIDS})"),
    (
        "family_member",
        "SELECT id, family_id, name, status FROM family_member "
        f"WHERE id IN ({MIDS}) AND family_id IN ({FIDS})",
    ),
    ("family", f"SELECT id, household_no, owner_id, status FROM family WHERE id IN ({FIDS})"),
)


def _rows(conn, sql: str) -> list[tuple]:
    return [tuple(row) for row in conn.execute(text(sql))]


def _print_state(conn, title: str) -> None:
    print(f"\n===== {title} =====")
    for label, sql in (
        ("family", "SELECT id, household_no, owner_id, status FROM family ORDER BY id"),
        ("family_member", "SELECT id, family_id, name, status FROM family_member ORDER BY id"),
        ("face_record", "SELECT id, member_id, status FROM face_record ORDER BY id"),
        ("sys_user", "SELECT id, username, role, status FROM sys_user ORDER BY id"),
    ):
        print(f"{label:14s}:", _rows(conn, sql))


def _cleanup_legacy(conn, *, execute: bool) -> dict[str, int]:
    """清理历史残留（family 1/3 + member 1/2/6）。"""
    print("\n---------- [1] 历史残留清理（固定ID，幂等） ----------")
    print("将被删除的行：")
    for table, sql in LEGACY_PREVIEW:
        print(f"  {table:14s}:", _rows(conn, sql))

    if not execute:
        return {}

    deleted: dict[str, int] = {}
    for table, sql in LEGACY_STEPS:
        deleted[table] = int(conn.execute(text(sql)).rowcount or 0)
        print(f"已删除 {table:14s}: {deleted[table]} 行")
    return deleted


def _cleanup_test_generated(conn, *, execute: bool) -> dict[str, int]:
    """清理测试自建数据（用户名以 ``tst`` 开头的账号及其家庭/成员/人脸/签到/请假数据）。

    删除顺序（按外键依赖）：``checkin_record`` → ``leave_request`` → ``family_member``
    → ``face_record`` → ``family`` → 无残留记录的 ``event`` → ``sys_user``。
    """
    print("\n---------- [2] 测试自建数据清理（用户名以 tst 开头） ----------")

    users = _rows(
        conn,
        "SELECT id, username, role FROM sys_user "
        f"WHERE username LIKE '{TEST_USERNAME_PREFIX}%' ORDER BY id",
    )
    user_ids = [row[0] for row in users]
    families = (
        _rows(
            conn,
            "SELECT id, household_no, owner_id, status FROM family "
            f"WHERE owner_id IN ({', '.join(str(i) for i in user_ids)}) ORDER BY id",
        )
        if user_ids
        else []
    )
    family_ids = [row[0] for row in families]
    members = (
        _rows(
            conn,
            "SELECT id, family_id, name, status FROM family_member "
            f"WHERE family_id IN ({', '.join(str(i) for i in family_ids)}) ORDER BY id",
        )
        if family_ids
        else []
    )
    member_ids = [row[0] for row in members]

    record_events: list[int] = []
    leave_events: list[int] = []
    record_ids: list[int] = []
    leave_ids: list[int] = []
    if member_ids:
        mids = ", ".join(str(i) for i in member_ids)
        record_ids = [
            row[0]
            for row in _rows(
                conn, f"SELECT id FROM checkin_record WHERE member_id IN ({mids})"
            )
        ]
        leave_ids = [
            row[0]
            for row in _rows(
                conn, f"SELECT id FROM leave_request WHERE member_id IN ({mids})"
            )
        ]
        record_events = [row[0] for row in _rows(
            conn, f"SELECT DISTINCT event_id FROM checkin_record WHERE member_id IN ({mids})"
        )]
        leave_events = [row[0] for row in _rows(
            conn, f"SELECT DISTINCT event_id FROM leave_request WHERE member_id IN ({mids})"
        )]
    event_ids = sorted(set(record_events) | set(leave_events))
    events = (
        _rows(
            conn,
            "SELECT id, name, status FROM event "
            f"WHERE id IN ({', '.join(str(i) for i in event_ids)}) ORDER BY id",
        )
        if event_ids
        else []
    )

    print(f"测试账号 {len(users)} 个:", users)
    print(f"其家庭档案 {len(families)} 条:", families)
    print(f"家庭成员 {len(members)} 条:", members)
    print(f"涉及其签到/请假的签到活动 {len(events)} 条:", events)
    print(f"相关签到记录 {len(record_ids)} 条、请假申请 {len(leave_ids)} 条")

    if not execute:
        return {}

    deleted: dict[str, int] = {}
    steps: list[tuple[str, str]] = []
    if member_ids:
        mids = ", ".join(str(i) for i in member_ids)
        steps.append(
            ("checkin_record", f"DELETE FROM checkin_record WHERE member_id IN ({mids})")
        )
        steps.append(
            ("leave_request", f"DELETE FROM leave_request WHERE member_id IN ({mids})")
        )
    if family_ids:
        fids = ", ".join(str(i) for i in family_ids)
        steps.append(
            (
                "face_record",
                "DELETE FROM face_record WHERE member_id IN "
                f"(SELECT id FROM family_member WHERE family_id IN ({fids}))",
            )
        )
        steps.append(("family_member", f"DELETE FROM family_member WHERE family_id IN ({fids})"))
        steps.append(("family", f"DELETE FROM family WHERE id IN ({fids})"))
    if event_ids:
        eids = ", ".join(str(i) for i in event_ids)
        # 仅删除"已无任何签到/请假记录"的活动，避免误删真实业务数据
        steps.append(
            (
                "event",
                f"DELETE FROM event WHERE id IN ({eids}) "
                "AND id NOT IN (SELECT event_id FROM checkin_record) "
                "AND id NOT IN (SELECT event_id FROM leave_request)",
            )
        )
    if user_ids:
        uids = ", ".join(str(i) for i in user_ids)
        # 兜底：只删仍无家庭档案的测试账号，避免误删
        steps.append(
            (
                "sys_user",
                "DELETE FROM sys_user WHERE id IN "
                f"({uids}) AND id NOT IN (SELECT owner_id FROM family)",
            )
        )

    # 最后清理与上述数据相关的操作日志（target 指向被删对象，或操作人是测试账号）
    log_conditions = []
    if event_ids:
        log_conditions.append(
            f"(target_type = 'event' AND target_id IN ({', '.join(str(i) for i in event_ids)}))"
        )
    if record_ids:
        log_conditions.append(
            f"(target_type = 'checkin' AND target_id IN ({', '.join(str(i) for i in record_ids)}))"
        )
    if leave_ids:
        log_conditions.append(
            f"(target_type = 'leave' AND target_id IN ({', '.join(str(i) for i in leave_ids)}))"
        )
    if user_ids:
        log_conditions.append(f"user_id IN ({', '.join(str(i) for i in user_ids)})")
    if log_conditions:
        steps.append(
            ("operation_log", f"DELETE FROM operation_log WHERE {' OR '.join(log_conditions)}")
        )

    for table, sql in steps:
        deleted[table] = int(conn.execute(text(sql)).rowcount or 0)
        print(f"已删除 {table:14s}: {deleted[table]} 行")

    if member_ids:
        print("提示：照片文件（uploads/face/<family_id>/）未删除，如需彻底清理请手动处理。")
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="测试数据清理脚本")
    parser.add_argument(
        "--purge-test-generated",
        action="store_true",
        help="额外清理测试自建数据（用户名以 tst 开头的账号及其家庭/成员/人脸记录）",
    )
    parser.add_argument("--yes", action="store_true", help="确认执行删除（默认仅预览）")
    args = parser.parse_args()

    with engine.begin() as conn:
        _print_state(conn, "清理前")

        legacy = _cleanup_legacy(conn, execute=args.yes)
        generated = (
            _cleanup_test_generated(conn, execute=args.yes)
            if args.purge_test_generated
            else {}
        )

        if not args.yes:
            print("\n[预览模式] 未执行删除；如需实际执行请加 --yes 参数。")
            if not args.purge_test_generated:
                print("提示：如需一并清理测试自建数据，请加 --purge-test-generated 参数。")
            return

        _print_state(conn, "清理后")

    print("\n清理完成：历史残留", legacy, "| 测试自建数据", generated)


if __name__ == "__main__":
    main()
