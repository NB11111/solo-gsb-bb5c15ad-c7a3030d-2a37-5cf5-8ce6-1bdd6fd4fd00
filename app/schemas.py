"""Pydantic 请求 / 响应模型。

所有金额字段使用字符串传输，服务端以整数分度记账。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------- 请求模型
class AccountCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64,
                      description="科目代码，全局唯一，如 1002、5301")
    name: str = Field(..., min_length=1, max_length=128, description="科目名称")
    currency: str = Field(..., description="ISO 4217 三字母币种，如 CNY/USD/EUR")

    @field_validator("currency")
    @classmethod
    def _upper_ccy(cls, v: str) -> str:
        v = v.strip().upper()
        if not CURRENCY_RE.match(v):
            raise ValueError("currency 必须为 3 位大写字母（ISO 4217），如 CNY")
        return v

    @field_validator("code")
    @classmethod
    def _strip_code(cls, v: str) -> str:
        return v.strip()


class PeriodCreate(BaseModel):
    period_code: str = Field(..., description="会计期间，格式 YYYY-MM，如 2026-09")
    start_date: date
    end_date: date

    @field_validator("period_code")
    @classmethod
    def _check_code(cls, v: str) -> str:
        if not PERIOD_RE.match(v):
            raise ValueError("period_code 格式必须为 YYYY-MM")
        return v


class EntryIn(BaseModel):
    account: str = Field(..., description="科目代码")
    debit: str = Field("0", description="借方金额（字符串，两位小数内）")
    credit: str = Field("0", description="贷方金额（字符串，两位小数内）")
    summary: str = Field("", max_length=256)


class VoucherCreate(BaseModel):
    request_id: str = Field(
        ..., description="客户端幂等键，8-128 位字母数字及 ._:-",
    )
    voucher_date: date = Field(..., description="凭证记账日期（决定所属期间）")
    currency: str = Field(..., description="凭证币种，所有分录科目必须同币种")
    description: str = Field("", max_length=512)
    entries: list[EntryIn] = Field(..., min_length=2)

    @field_validator("currency")
    @classmethod
    def _upper_ccy(cls, v: str) -> str:
        v = v.strip().upper()
        if not CURRENCY_RE.match(v):
            raise ValueError("currency 必须为 3 位大写字母（ISO 4217），如 CNY")
        return v

    @field_validator("request_id")
    @classmethod
    def _check_rid(cls, v: str) -> str:
        v = v.strip()
        if not REQUEST_ID_RE.match(v):
            raise ValueError("request_id 需为 8-128 位字母数字及 ._:- 字符")
        return v


class VoucherReverse(BaseModel):
    request_id: str = Field(..., description="幂等键（与原凭证不同）")
    reversal_date: date = Field(..., description="反向凭证记账日期")
    description: str = Field("", max_length=512,
                             description="留空则自动生成冲销说明")

    @field_validator("request_id")
    @classmethod
    def _check_rid(cls, v: str) -> str:
        v = v.strip()
        if not REQUEST_ID_RE.match(v):
            raise ValueError("request_id 需为 8-128 位字母数字及 ._:- 字符")
        return v


# ---------------------------------------------------------------- 响应模型
class AccountOut(BaseModel):
    id: int
    code: str
    name: str
    currency: str
    created_at: str


class PeriodOut(BaseModel):
    id: int
    period_code: str
    start_date: str
    end_date: str
    is_closed: bool
    closed_at: str | None = None


class EntryOut(BaseModel):
    line_no: int
    account: str
    debit: str
    credit: str
    summary: str


class ReversalRef(BaseModel):
    voucher_id: int
    voucher_no: str
    voucher_date: str
    relation: Literal["reverses", "reversed_by"]


class VoucherOut(BaseModel):
    id: int
    voucher_no: str
    voucher_date: str
    period: str
    currency: str
    description: str
    created_at: str
    debit_total: str
    credit_total: str
    entries: list[EntryOut]
    reverses: ReversalRef | None = None
    reversed_by: ReversalRef | None = None


class BalanceOut(BaseModel):
    account: str
    account_name: str
    currency: str
    debit_total: str
    credit_total: str
    net_debit: str
    net_credit: str
    direction: Literal["debit", "credit"]
    updated_at: str | None = None


class LedgerEntryOut(BaseModel):
    line_no: int
    voucher_id: int
    voucher_no: str
    voucher_date: str
    description: str
    debit: str
    credit: str
    running_debit_total: str
    running_credit_total: str
    running_net_debit: str
    is_reversal: bool
    linked_voucher_no: str | None = None
    linked_relation: Literal["reverses", "reversed_by"] | None = None


class LedgerOut(BaseModel):
    account: str
    account_name: str
    currency: str
    period_from: str | None = None
    period_to: str | None = None
    opening_net_debit: str
    opening_debit_total: str
    opening_credit_total: str
    lines: list[LedgerEntryOut]
    closing_debit_total: str
    closing_credit_total: str
    closing_net_debit: str
