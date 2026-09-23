"""科研经费双重记账 API 端到端测试。"""
from __future__ import annotations

from conftest import make_account, make_voucher


# 一组典型科研经费科目
def _seed_accounts(client):
    make_account(client, "1002", "银行存款", "CNY")
    make_account(client, "5301", "科研事业支出-材料费", "CNY")
    make_account(client, "5302", "科研事业支出-差旅费", "CNY")
    make_account(client, "4001", "科研拨款收入", "CNY")
    make_account(client, "1002-USD", "美元存款", "USD")
    make_account(client, "5301-USD", "美元材料费", "USD")


PURCHASE = [
    {"account": "1002", "credit": "12000.00", "summary": "支付材料款"},
    {"account": "5301", "debit": "12000.00", "summary": "采购实验材料"},
]


# ---------------------------------------------------------------- 基础流程
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_full_flow_balance_and_ledger(client, setup_month):
    _seed_accounts(client)

    resp = make_voucher(client, "req-0001-flow-1", PURCHASE)
    assert resp.status_code == 201, resp.text
    v = resp.json()
    assert v["voucher_no"] == "RV-202609-0001"
    assert v["debit_total"] == "12000.00"
    assert v["credit_total"] == "12000.00"
    assert v["reverses"] is None and v["reversed_by"] is None

    # 余额：银行存款贷方 12000（净贷方），材料支出借方 12000（净借方）
    bal_bank = client.get("/v1/accounts/1002/balance").json()
    assert bal_bank["direction"] == "credit"
    assert bal_bank["net_credit"] == "12000.00"

    bal_exp = client.get("/v1/accounts/5301/balance").json()
    assert bal_exp["direction"] == "debit"
    assert bal_exp["net_debit"] == "12000.00"

    # 分户账：running 余额逐笔推进
    ledger = client.get("/v1/accounts/1002/ledger").json()
    assert len(ledger["lines"]) == 1
    assert ledger["lines"][0]["running_credit_total"] == "12000.00"
    assert ledger["closing_net_debit"] == "-12000.00"


def test_multi_line_voucher_with_same_account(client, setup_month):
    _seed_accounts(client)
    # 同一科目出现两次，应在余额侧聚合
    resp = make_voucher(client, "req-multiline-1", [
        {"account": "1002", "credit": "1000.00"},
        {"account": "1002", "credit": "500.00"},
        {"account": "5301", "debit": "1000.00"},
        {"account": "5302", "debit": "500.00"},
    ], description="材料+差旅")
    assert resp.status_code == 201, resp.text
    assert client.get("/v1/accounts/1002/balance").json()[
        "credit_total"] == "1500.00"


# ---------------------------------------------------------------- 校验
def test_unbalanced_voucher_rejected(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-unbal-0001", [
        {"account": "1002", "credit": "100.00"},
        {"account": "5301", "debit": "99.99"},
    ])
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "unbalanced_voucher"
    assert err["details"]["debit_total"] == "99.99"
    assert err["details"]["credit_total"] == "100.00"
    # 失败后余额无变化
    assert client.get("/v1/accounts/1002/balance").json()[
        "credit_total"] == "0.00"


def test_currency_mismatch_rejected(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-ccy-0001", [
        {"account": "1002", "credit": "100.00"},      # CNY
        {"account": "5301-USD", "debit": "100.00"},   # USD
    ])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "currency_mismatch"


def test_foreign_currency_voucher_ok(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-usd-0001", [
        {"account": "1002-USD", "credit": "300.00"},
        {"account": "5301-USD", "debit": "300.00"},
    ], currency="USD")
    assert resp.status_code == 201, resp.text
    bal = client.get("/v1/accounts/1002-USD/balance").json()
    assert bal["currency"] == "USD"
    assert bal["net_credit"] == "300.00"


def test_unknown_account_rejected(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-acc-0001", [
        {"account": "1002", "credit": "10.00"},
        {"account": "9999", "debit": "10.00"},
    ])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "account_not_found"


def test_entry_both_sides_or_zero_rejected(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-entry-001", [
        {"account": "1002", "debit": "5.00", "credit": "5.00"},
        {"account": "5301", "debit": "5.00", "credit": "5.00"},
    ])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_entry"


def test_amount_precision_rejected(client, setup_month):
    _seed_accounts(client)
    resp = make_voucher(client, "req-prec-0001", [
        {"account": "1002", "credit": "10.005"},
        {"account": "5301", "debit": "10.005"},
    ])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_amount"


# ---------------------------------------------------------------- 期间
def test_voucher_without_period_rejected(client):
    _seed_accounts(client)
    # 只建 2026-08，凭证日期落在 2026-09
    client.post("/v1/periods", json={
        "period_code": "2026-08",
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
    })
    resp = make_voucher(client, "req-noper-001", PURCHASE)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "period_not_found"


def test_closed_period_blocks_posting(client, setup_month):
    _seed_accounts(client)
    # 先正常入账
    assert make_voucher(client, "req-close-01", PURCHASE).status_code == 201

    # 结账后不能再入账
    closed = client.post("/v1/periods/2026-09/close").json()
    assert closed["is_closed"] is True

    resp = make_voucher(client, "req-close-02", [
        {"account": "1002", "credit": "1.00"},
        {"account": "5301", "debit": "1.00"},
    ])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "period_closed"


# ---------------------------------------------------------------- 幂等
def test_idempotent_replay_same_result(client, setup_month):
    _seed_accounts(client)
    payload = {
        "request_id": "req-idem-0001",
        "voucher_date": "2026-09-10",
        "currency": "CNY",
        "description": "幂等测试",
        "entries": PURCHASE,
    }
    first = client.post("/v1/vouchers", json=payload)
    assert first.status_code == 201
    second = client.post("/v1/vouchers", json=payload)
    # 重放：200 + 同一张凭证，不新增余额
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert client.get("/v1/accounts/5301/balance").json()[
        "debit_total"] == "12000.00"
    assert len(client.get("/v1/accounts/5301/ledger").json()["lines"]) == 1


def test_same_request_id_different_body_conflict(client, setup_month):
    _seed_accounts(client)
    r1 = make_voucher(client, "req-idem-0002", PURCHASE)
    assert r1.status_code == 201
    r2 = make_voucher(client, "req-idem-0002", [
        {"account": "1002", "credit": "1.00"},
        {"account": "5301", "debit": "1.00"},
    ])
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_conflict"


# ---------------------------------------------------------------- 冲销
def test_reverse_creates_counter_voucher_and_chain(client, setup_month):
    _seed_accounts(client)
    original = make_voucher(client, "req-rev-0001", PURCHASE).json()

    resp = client.post(f"/v1/vouchers/{original['voucher_no']}/reverse", json={
        "request_id": "req-rev-0001-r",
        "reversal_date": "2026-09-15",
    })
    assert resp.status_code == 201, resp.text
    rev = resp.json()

    # 借贷方向对调
    assert rev["entries"][0]["debit"] == "12000.00"
    assert rev["entries"][0]["account"] == "1002"
    assert rev["reverses"] is not None
    assert rev["reverses"]["voucher_no"] == original["voucher_no"]

    # 原凭证出现 reversed_by 指针
    got = client.get(f"/v1/vouchers/{original['voucher_no']}").json()
    assert got["reversed_by"]["voucher_no"] == rev["voucher_no"]

    # 余额回零（累计发生额仍保留：借贷各 24000，净额 0）
    bal = client.get("/v1/accounts/1002/balance").json()
    assert bal["debit_total"] == "12000.00"
    assert bal["credit_total"] == "12000.00"
    assert bal["net_debit"] == "0.00"

    # 凭证链：原凭证 -> 反向凭证
    chain = client.get(
        f"/v1/vouchers/{rev['voucher_no']}/chain").json()
    assert [x["voucher_no"] for x in chain] == [
        original["voucher_no"], rev["voucher_no"]]

    # 明细分录中反向分录带有关联标注
    ledger = client.get("/v1/accounts/1002/ledger").json()
    flags = [(ln["is_reversal"], ln["linked_relation"]) for ln in ledger["lines"]]
    assert flags == [(False, None), (True, "reverses")]


def test_double_reverse_rejected(client, setup_month):
    _seed_accounts(client)
    original = make_voucher(client, "req-rev-0002", PURCHASE).json()
    body = {"request_id": "req-rev-0002-r1", "reversal_date": "2026-09-15"}
    assert client.post(
        f"/v1/vouchers/{original['voucher_no']}/reverse", json=body
    ).status_code == 201
    body2 = {"request_id": "req-rev-0002-r2", "reversal_date": "2026-09-16"}
    resp = client.post(
        f"/v1/vouchers/{original['voucher_no']}/reverse", json=body2)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "voucher_already_reversed"


def test_reverse_idempotent_replay(client, setup_month):
    _seed_accounts(client)
    original = make_voucher(client, "req-rev-0003", PURCHASE).json()
    body = {"request_id": "req-rev-0003-r", "reversal_date": "2026-09-15"}
    r1 = client.post(f"/v1/vouchers/{original['voucher_no']}/reverse", json=body)
    r2 = client.post(f"/v1/vouchers/{original['voucher_no']}/reverse", json=body)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_reverse_into_closed_period_blocked(client):
    # 原凭证在 9 月；9 月结账后，冲销日仍落在 9 月 -> 拒绝
    client.post("/v1/periods", json={
        "period_code": "2026-09",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
    })
    client.post("/v1/periods", json={
        "period_code": "2026-10",
        "start_date": "2026-10-01",
        "end_date": "2026-10-31",
    })
    _seed_accounts(client)
    original = make_voucher(client, "req-rev-0004", PURCHASE).json()
    client.post("/v1/periods/2026-09/close")
    resp = client.post(f"/v1/vouchers/{original['voucher_no']}/reverse", json={
        "request_id": "req-rev-0004-r",
        "reversal_date": "2026-09-20",
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "period_closed"

    # 落入新的开放期间则允许
    resp2 = client.post(f"/v1/vouchers/{original['voucher_no']}/reverse", json={
        "request_id": "req-rev-0004-r2",
        "reversal_date": "2026-10-05",
    })
    assert resp2.status_code == 201, resp2.text


# ---------------------------------------------------------------- 不可变性
def test_voucher_is_immutable_in_database(client, setup_month):
    """直接在数据库层验证 UPDATE/DELETE 被触发器拒绝。"""
    import os
    import sqlite3

    _seed_accounts(client)
    v = make_voucher(client, "req-imm-0001", PURCHASE).json()

    conn = sqlite3.connect(os.environ["LEDGER_DB_PATH"])
    try:
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE vouchers SET description = 'x' WHERE id = ?",
                         (v["id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM entries WHERE voucher_id = ?", (v["id"],))
    finally:
        conn.close()
