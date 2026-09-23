"""应用错误类型与统一错误响应。

错误体格式：{"error": {"code": ..., "message": ..., "details": ...}}
"""
from __future__ import annotations

from typing import Any


class LedgerError(Exception):
    """业务规则错误。status_code 为对外 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status_code: int = 400,
                 details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class AccountNotFound(LedgerError):
    def __init__(self, key: str):
        super().__init__("account_not_found", f"科目不存在: {key}", 404)


class PeriodNotFound(LedgerError):
    def __init__(self, date: str):
        super().__init__("period_not_found",
                         f"日期 {date} 对应的会计期间不存在，请先创建期间", 422)


class PeriodClosed(LedgerError):
    def __init__(self, period_code: str):
        super().__init__("period_closed",
                         f"会计期间 {period_code} 已结账，不可再入账", 422,
                         {"period": period_code})


class CurrencyMismatch(LedgerError):
    def __init__(self, account_code: str, account_ccy: str, voucher_ccy: str):
        super().__init__(
            "currency_mismatch",
            f"科目 {account_code} 币种为 {account_ccy}，与凭证币种 {voucher_ccy} 不一致",
            422,
            {"account": account_code, "account_currency": account_ccy,
             "voucher_currency": voucher_ccy},
        )


class NotBalanced(LedgerError):
    def __init__(self, debit: str, credit: str):
        super().__init__(
            "unbalanced_voucher",
            f"借贷不平衡：借方合计 {debit}，贷方合计 {credit}", 422,
            {"debit_total": debit, "credit_total": credit},
        )


class VoucherNotFound(LedgerError):
    def __init__(self, key: str):
        super().__init__("voucher_not_found", f"凭证不存在: {key}", 404)


class VoucherAlreadyReversed(LedgerError):
    def __init__(self, voucher_no: str, reversal_no: str):
        super().__init__(
            "voucher_already_reversed",
            f"凭证 {voucher_no} 已被反向凭证 {reversal_no} 冲销", 409,
            {"voucher_no": voucher_no, "reversal_no": reversal_no},
        )


class IdempotencyConflict(LedgerError):
    def __init__(self, request_id: str):
        super().__init__(
            "idempotency_conflict",
            f"request_id={request_id} 已被用于不同的请求内容", 409,
            {"request_id": request_id},
        )
