"""FastAPI 应用：科研经费双重记账 API。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Response
from fastapi.responses import JSONResponse

from . import service
from .db import get_db, init_db
from .errors import LedgerError
from .money import format_amount
from .schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    LedgerOut,
    PeriodCreate,
    PeriodOut,
    VoucherCreate,
    VoucherOut,
    VoucherReverse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="科研经费双重记账 API",
    description=(
        "多币种复式记账：科目（带币种）、会计期间、借贷平衡凭证、"
        "反向凭证冲销、request_id 幂等、余额与凭证链查询。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

Db = Annotated[sqlite3.Connection, Depends(get_db)]


@app.exception_handler(LedgerError)
async def ledger_error_handler(_request: Request, exc: LedgerError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message,
                           "details": exc.details}},
    )


# ---------------------------------------------------------------- 健康检查
@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 科目
@app.post("/v1/accounts", response_model=AccountOut, status_code=201,
          tags=["accounts"])
def create_account(body: AccountCreate, conn: Db):
    row = service.create_account(conn, body)
    return _account_out(row)


@app.get("/v1/accounts", response_model=list[AccountOut], tags=["accounts"])
def list_accounts(conn: Db):
    return [_account_out(r) for r in service.list_accounts(conn)]


def _account_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "code": row["code"], "name": row["name"],
            "currency": row["currency"], "created_at": row["created_at"]}


# ---------------------------------------------------------------- 会计期间
@app.post("/v1/periods", response_model=PeriodOut, status_code=201,
          tags=["periods"])
def create_period(body: PeriodCreate, conn: Db):
    return _period_out(service.create_period(conn, body))


@app.get("/v1/periods", response_model=list[PeriodOut], tags=["periods"])
def list_periods(conn: Db):
    return [_period_out(r) for r in service.list_periods(conn)]


@app.post("/v1/periods/{period_code}/close", response_model=PeriodOut,
          tags=["periods"])
def close_period(period_code: str, conn: Db):
    return _period_out(service.close_period(conn, period_code))


@app.post("/v1/periods/{period_code}/reopen", response_model=PeriodOut,
          tags=["periods"])
def reopen_period(period_code: str, conn: Db):
    return _period_out(service.reopen_period(conn, period_code))


def _period_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "period_code": row["period_code"],
            "start_date": row["start_date"], "end_date": row["end_date"],
            "is_closed": bool(row["is_closed"]), "closed_at": row["closed_at"]}


# ---------------------------------------------------------------- 凭证
@app.post("/v1/vouchers", response_model=VoucherOut, status_code=201,
          tags=["vouchers"])
def create_voucher(body: VoucherCreate, conn: Db, response: Response):
    voucher, replayed = service.create_voucher(conn, body)
    # 幂等重放返回 200 而非 201，表示未新建资源
    if replayed:
        response.status_code = 200
    return voucher


@app.post("/v1/vouchers/{ref}/reverse", response_model=VoucherOut,
          status_code=201, tags=["vouchers"])
def reverse_voucher(ref: str, body: VoucherReverse, conn: Db,
                    response: Response):
    voucher, replayed = service.reverse_voucher(conn, ref, body)
    if replayed:
        response.status_code = 200
    return voucher


@app.get("/v1/vouchers", response_model=list[VoucherOut], tags=["vouchers"])
def list_vouchers(
    conn: Db,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    period: str | None = Query(None, description="按期间 YYYY-MM 过滤"),
):
    return service.list_vouchers(conn, limit=limit, period_code=period)


@app.get("/v1/vouchers/{ref}", response_model=VoucherOut, tags=["vouchers"])
def get_voucher(ref: str, conn: Db):
    return service.get_voucher(conn, ref)


@app.get("/v1/vouchers/{ref}/chain", response_model=list[VoucherOut],
         tags=["vouchers"])
def voucher_chain(ref: str, conn: Db):
    """凭证链：原始凭证 -> 冲销凭证 -> 再冲销（恢复）… 按链顺序返回。"""
    return service.voucher_chain(conn, ref)


# ---------------------------------------------------------------- 查询
@app.get("/v1/accounts/{account_code}/balance", response_model=BalanceOut,
         tags=["query"])
def get_balance(account_code: str, conn: Db):
    acc, bal = service.get_balance(conn, account_code)
    if bal is None:
        return {"account": acc["code"], "account_name": acc["name"],
                "currency": acc["currency"],
                "debit_total": "0.00", "credit_total": "0.00",
                "net_debit": "0.00", "net_credit": "0.00",
                "direction": "debit", "updated_at": None}
    net = bal["net_debit"]
    return {
        "account": acc["code"], "account_name": acc["name"],
        "currency": bal["currency"],
        "debit_total": format_amount(bal["debit_total"]),
        "credit_total": format_amount(bal["credit_total"]),
        "net_debit": format_amount(net) if net >= 0 else "0.00",
        "net_credit": format_amount(-net) if net < 0 else "0.00",
        "direction": "debit" if net >= 0 else "credit",
        "updated_at": bal["updated_at"],
    }


@app.get("/v1/accounts/{account_code}/ledger", response_model=LedgerOut,
         tags=["query"])
def get_ledger(
    account_code: str,
    conn: Db,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
):
    """按科目返回明细分录与逐笔累计余额，反向凭证标注冲销关联。"""
    return service.get_ledger(
        conn, account_code,
        date_from.isoformat() if date_from else None,
        date_to.isoformat() if date_to else None,
    )
