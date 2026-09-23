"""科目路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_session
from ..schemas import AccountCreate, AccountOut
from ..serializers import account_out, balance_out
from ..services import master_data
from ..services.voucher import account_balances, account_ledger
from ..schemas import BalanceOut, LedgerOut

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=AccountOut, status_code=201, summary="创建带币种的科目")
def create_account(payload: AccountCreate, db: Session = Depends(get_session)) -> AccountOut:
    account = master_data.create_account(db, payload)
    db.commit()
    db.refresh(account)
    return account_out(account)


@router.get("", response_model=list[AccountOut], summary="科目列表")
def list_accounts(db: Session = Depends(get_session)) -> list[AccountOut]:
    return [account_out(a) for a in master_data.list_accounts(db)]


@router.get("/{account_id}", response_model=AccountOut, summary="科目详情")
def get_account(account_id: int, db: Session = Depends(get_session)) -> AccountOut:
    return account_out(master_data.get_account(db, account_id))


@router.get(
    "/{account_id}/balances",
    response_model=list[BalanceOut],
    summary="按期间查询科目余额",
)
def get_balances(
    account_id: int,
    period: str | None = None,
    db: Session = Depends(get_session),
) -> list[BalanceOut]:
    rows = account_balances(db, account_id, period)
    return [balance_out(db, b) for b in rows]


@router.get(
    "/{account_id}/ledger",
    response_model=LedgerOut,
    summary="科目分类账：分录流水 + 逐笔累计余额",
)
def get_ledger(
    account_id: int,
    period_from: str | None = None,
    period_to: str | None = None,
    cursor: int | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
) -> LedgerOut:
    return account_ledger(
        db, account_id,
        period_from=period_from, period_to=period_to,
        cursor=cursor, limit=limit,
    )
