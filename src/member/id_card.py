"""身份证号校验与信息解析工具。

支持 18 位二代居民身份证：

- 格式校验：17 位数字 + 1 位数字或 ``X``
- 校验位校验：按 GB 11643-1999 加权因子计算，防止手工录入时写错
- 信息解析：出生日期（第 7~14 位）与性别（第 17 位，奇数为男、偶数为女）

这样在录入成员时只需填写身份证号，性别与出生日期可自动补齐，减少基层录入工作量。
"""

from __future__ import annotations

import re
from datetime import date

from src.member.models import Gender

__all__ = ["normalize_id_card", "parse_birth_date_and_gender", "validate_id_card"]

#: 18 位身份证号格式（末位可为 X/x）
ID_CARD_PATTERN = re.compile(r"^\d{17}[\dXx]$")

#: 前 17 位的加权因子
_WEIGHTS: tuple[int, ...] = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)

#: 校验位对照表（索引为加权和 % 11）
_CHECK_CODES: str = "10X98765432"

#: 出生日期的合理范围
_MIN_BIRTH_YEAR = 1900


def normalize_id_card(value: str) -> str:
    """去除首尾空白并把末位 ``x`` 统一转为大写 ``X``。"""
    return value.strip().upper()


def _is_valid_birth_date(birth: str) -> bool:
    """校验身份证中的出生日期段是否为合法日期且在合理范围内。"""
    try:
        parsed = date(int(birth[0:4]), int(birth[4:6]), int(birth[6:8]))
    except ValueError:
        return False
    return _MIN_BIRTH_YEAR <= parsed.year <= date.today().year


def validate_id_card(value: str) -> str:
    """校验身份证号并返回规范化结果。

    :raises ValueError: 格式错误、出生日期非法或校验位不正确
    """
    id_card = normalize_id_card(value)
    if not ID_CARD_PATTERN.match(id_card):
        raise ValueError("身份证号格式不正确（应为 18 位，最后一位可以是 X）")
    if not _is_valid_birth_date(id_card[6:14]):
        raise ValueError("身份证号中的出生日期不合法")

    total = sum(int(digit) * weight for digit, weight in zip(id_card[:17], _WEIGHTS))
    if id_card[17] != _CHECK_CODES[total % 11]:
        raise ValueError("身份证号校验位不正确，请核对")
    return id_card


def parse_birth_date_and_gender(id_card: str) -> tuple[date, Gender]:
    """从合法身份证号中解析出生日期与性别。

    :param id_card: 已通过 :func:`validate_id_card` 校验的身份证号
    :return: ``(出生日期, 性别)``
    """
    birth = id_card[6:14]
    birth_date = date(int(birth[0:4]), int(birth[4:6]), int(birth[6:8]))
    gender = Gender.MALE if int(id_card[16]) % 2 == 1 else Gender.FEMALE
    return birth_date, gender
