"""初始化数据库表并创建管理员账号。

用法（在项目根目录执行）::

    # 1) 先确认 .env 中的数据库配置正确
    python -c "from src.common.database import ping_database; print(ping_database())"

    # 2) 创建数据表 + 交互式输入密码创建管理员（推荐，密码不会进入命令行历史）
    python scripts/create_admin.py --username admin

    # 3) 或直接指定参数（注意命令行历史/进程列表会暴露密码）
    python scripts/create_admin.py --username admin --password 你的密码 --real-name 村管理员

    # 4) 重置已存在管理员密码
    python scripts/create_admin.py --username admin --force

说明：

- 家庭用户可通过 ``POST /api/auth/register`` 自行注册；
  管理员账号只能由此脚本创建（后续可由已有管理员通过 ``POST /api/users`` 创建）；
- 脚本是幂等的：用户名已存在时默认跳过，加 ``--force`` 才会重置该账号的密码与角色；
- 脚本会自动执行 ``init_db()``，即按 ORM 模型创建尚不存在的数据表（不会修改已有表结构）。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from src.auth.security import hash_password  # noqa: E402
from src.common.database import SessionLocal, init_db, ping_database  # noqa: E402
from src.user.models import User, UserRole, UserStatus  # noqa: E402
from src.user.schemas import (  # noqa: E402
    BCRYPT_MAX_BYTES,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="初始化数据库表并创建/重置管理员账号",
    )
    parser.add_argument(
        "--username", "-u", default="admin", help="管理员用户名，默认 admin"
    )
    parser.add_argument(
        "--password",
        "-p",
        default=None,
        help="管理员密码；不传则在命令行交互式输入（推荐）",
    )
    parser.add_argument(
        "--real-name", "-n", default="系统管理员", help="管理员真实姓名"
    )
    parser.add_argument("--phone", default=None, help="管理员手机号（可选，需唯一）")
    parser.add_argument(
        "--force",
        action="store_true",
        help="用户名已存在时重置其密码并确保角色为管理员",
    )
    return parser.parse_args()


def read_password(raw_password: str | None) -> str | None:
    """读取并校验密码，非法时返回 None。"""
    password = raw_password
    if password is None:
        password = getpass.getpass("请输入管理员密码（输入时不显示）：")
        confirm = getpass.getpass("请再次输入以确认：")
        if password != confirm:
            print("[错误] 两次输入的密码不一致")
            return None

    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        print(
            f"[错误] 密码长度需为 {PASSWORD_MIN_LENGTH}~{PASSWORD_MAX_LENGTH} 个字符"
        )
        return None
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        print(f"[错误] 密码 UTF-8 编码后不能超过 {BCRYPT_MAX_BYTES} 字节")
        return None
    return password


def main() -> int:
    """脚本入口。

    :return: 进程退出码，0 表示成功
    """
    args = parse_args()

    if not ping_database():
        print("[错误] 无法连接数据库，请检查 .env 中的 DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME")
        return 1

    print("[1/3] 数据库连接正常，正在创建数据表 ...")
    init_db()

    password = read_password(args.password)
    if password is None:
        return 1

    print(f"[2/3] 正在处理管理员账号：{args.username} ...")
    with SessionLocal() as session:
        existing = session.scalar(select(User).where(User.username == args.username))

        if existing is not None and not args.force:
            print(
                f"[跳过] 账号已存在：{args.username}（角色：{existing.role.value}）。"
                f"如需重置密码请加 --force"
            )
            return 0

        if existing is not None:
            existing.password_hash = hash_password(password)
            existing.role = UserRole.ADMIN
            existing.status = UserStatus.ACTIVE
            existing.real_name = args.real_name
            if args.phone:
                existing.phone = args.phone
            session.commit()
            print(f"[3/3] 已重置管理员账号：{existing.username}（id={existing.id}）")
            return 0

        admin = User(
            username=args.username,
            password_hash=hash_password(password),
            real_name=args.real_name,
            phone=args.phone,
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        session.commit()
        session.refresh(admin)
        print(f"[3/3] 管理员创建成功：{admin.username}（id={admin.id}）")

    print("\n现在可以启动服务并登录：")
    print("    POST http://127.0.0.1:8000/api/auth/login")
    print(f'    请求体：{{"username": "{args.username}", "password": "<你设置的密码>"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
