"""金额工具：字符串金额 <-> 整数分度（分）。

对外只暴露字符串，服务内部与数据库使用整数，彻底杜绝二进制浮点误差。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .errors import LedgerError

# 形如 12 / 12.3 / 12.34，允许前导 0、千分位逗号；不接受科学计数法与符号后缀
_AMOUNT_RE = re.compile(r"^-?\d+(?:,\d{3})*(?:\.\d+)?$")

SCALE = 2  # 内部统一保留两位小数（百分之一）


def parse_amount(value: str | int | float, *, field: str = "amount") -> int:
    """把外部金额字符串解析为整数分度。

    超过两位小数直接报错（不做静默四舍五入，避免金额语义歧义）。
    零金额允许出现在合计中，但分录行级别不允许（由 schema/服务层保证）。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，显式拒绝
        raise LedgerError("invalid_amount", f"{field} 金额非法: {value!r}", 422)
    if isinstance(value, int):
        return value * 10 ** SCALE
    if isinstance(value, float):
        # 只用于兜底；正常路径都是字符串
        text = format(value, ".10f")
    elif isinstance(value, str):
        text = value.strip().replace(",", "")
    else:
        raise LedgerError("invalid_amount", f"{field} 金额类型非法", 422)

    if not _AMOUNT_RE.match(text):
        raise LedgerError("invalid_amount", f"{field} 金额非法: {value!r}", 422)

    try:
        dec = Decimal(text)
    except InvalidOperation:
        raise LedgerError("invalid_amount", f"{field} 金额非法: {value!r}", 422)

    if not dec.is_finite():
        raise LedgerError("invalid_amount", f"{field} 金额非法: {value!r}", 422)

    quant = Decimal(1).scaleb(-SCALE)  # 0.01
    if dec != dec.quantize(quant):
        raise LedgerError(
            "invalid_amount",
            f"{field} 金额 {value} 超过 {SCALE} 位小数", 422,
        )
    return int(dec.scaleb(SCALE))  # 等价于 *100，但全程 Decimal


def format_amount(minor: int) -> str:
    """整数分度 -> 两位小数字符串。"""
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"
