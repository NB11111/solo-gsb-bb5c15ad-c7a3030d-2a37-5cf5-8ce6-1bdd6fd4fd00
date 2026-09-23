"""端到端 API 测试。"""
from __future__ import annotations

from app.database import SessionLocal
from app.models import Voucher


def _create_account(client, code, name, currency="CNY", atype="asset"):
    r = client.post("/accounts", json={
        "code": code, "name": name, "currency": currency, "type": atype,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _post_voucher(client, rid, lines, currency="CNY", vdate="2026-09-20", memo=None):
    return client.post("/vouchers", json={
        "request_id": rid,
        "date": vdate,
        "currency": currency,
        "memo": memo,
        "lines": lines,
    })


def test_health_and_root(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/").json()["version"]


def test_create_account_and_duplicate_code(client):
    a = _create_account(client, "1001", "银行存款", "CNY")
    assert a["currency"] == "CNY" and a["type"] == "asset"

    r = client.post("/accounts", json={
        "code": "1001", "name": "重复", "currency": "CNY", "type": "asset",
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"

    # 小写币种自动转大写；非法币种拒绝
    r = client.post("/accounts", json={
        "code": "1002", "name": "美元户", "currency": "usd", "type": "asset",
    })
    assert r.status_code == 201 and r.json()["currency"] == "USD"


def test_post_balanced_voucher_and_balance(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "req-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "1000.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1000.00"},
    ])
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["number"] == "V-2026-09-0001"
    assert v["status"] == "posted"
    assert len(v["lines"]) == 2

    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert len(bal) == 1
    assert bal[0]["total_debit"] == "1000.00"
    assert bal[0]["total_credit"] == "0.00"
    assert bal[0]["net_debit"] == "1000.00"

    # 分类账：期初 0，一笔后累计借 1000
    ledger = client.get(f"/accounts/{bank['id']}/ledger").json()
    assert ledger["starting_debit"] == "0.00"
    assert ledger["entries"][0]["balance_after_debit"] == "1000.00"
    assert ledger["entries"][0]["voucher_number"] == v["number"]


def test_unbalanced_voucher_rejected(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "req-bad-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "1000.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "999.00"},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unprocessable_entity"

    # 只有借方也应拒绝
    r = _post_voucher(client, "req-bad-2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "debit", "amount": "100.00"},
    ])
    assert r.status_code == 422

    # 失败凭证不得产生余额
    assert client.get(f"/accounts/{bank['id']}/balances").json() == []


def test_currency_mismatch_rejected(client):
    cny = _create_account(client, "1002", "人民币户", "CNY")
    usd = _create_account(client, "1003", "美元户", "USD")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 同一张凭证混用 CNY/USD 科目
    r = _post_voucher(client, "req-cur-1", [
        {"account_id": cny["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], currency="USD")
    assert r.status_code == 422

    # 分录科目之间币种不一致
    r = _post_voucher(client, "req-cur-2", [
        {"account_id": cny["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": usd["id"], "direction": "credit", "amount": "100.00"},
    ], currency="CNY")
    assert r.status_code == 422

    # 同币种外币凭证应成功
    r = _post_voucher(client, "req-cur-3", [
        {"account_id": usd["id"], "direction": "debit", "amount": "50.00"},
        {"account_id": usd["id"], "direction": "credit", "amount": "50.00"},
    ], currency="USD")
    assert r.status_code == 201, r.text


def test_idempotent_request_id(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    body = {
        "request_id": "idem-1",
        "date": "2026-09-20",
        "currency": "CNY",
        "lines": [
            {"account_id": bank["id"], "direction": "debit", "amount": "250.50"},
            {"account_id": income["id"], "direction": "credit", "amount": "250.50"},
        ],
    }
    r1 = client.post("/vouchers", json=body)
    r2 = client.post("/vouchers", json=body)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.headers["X-Idempotent-Replay"] == "false"
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["number"] == r2.json()["number"]

    # 余额只记一次
    bal = client.get(f"/accounts/{bank['id']}/balances").json()
    assert bal[0]["total_debit"] == "250.50"

    # request_id 反查
    by_req = client.get("/vouchers/by-request/idem-1")
    assert by_req.status_code == 200
    assert by_req.json()["id"] == r1.json()["id"]


def test_closed_period_blocks_posting_and_reopen(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    # 先在 2026-08 入账
    r = _post_voucher(client, "req-p1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-08-15")
    assert r.status_code == 201

    # 结账 2026-08
    r = client.post("/periods/2026-08/close")
    assert r.status_code == 200 and r.json()["status"] == "closed"

    # 再向 8 月入账 -> 409
    r = _post_voucher(client, "req-p2", [
        {"account_id": bank["id"], "direction": "debit", "amount": "100.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "100.00"},
    ], vdate="2026-08-20")
    assert r.status_code == 409

    # 结账是幂等的
    r = client.post("/periods/2026-08/close")
    assert r.json()["status"] == "closed"

    # 9 月仍可入账
    r = _post_voucher(client, "req-p3", [
        {"account_id": bank["id"], "direction": "debit", "amount": "20.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "20.00"},
    ], vdate="2026-09-01")
    assert r.status_code == 201

    # 8 月余额冻结
    bal = client.get(f"/accounts/{bank['id']}/balances", params={"period": "2026-08"}).json()
    assert bal[0]["total_debit"] == "100.00"


def test_reversal_voucher_and_chain(client):
    bank = _create_account(client, "1002", "银行存款")
    expense = _create_account(client, "5001", "科研设备费", atype="expense")

    r = _post_voucher(client, "rev-base", [
        {"account_id": expense["id"], "direction": "debit", "amount": "800.00", "memo": "错误采购"},
        {"account_id": bank["id"], "direction": "credit", "amount": "800.00"},
    ], memo="待冲销")
    original = r.json()
    assert r.status_code == 201

    # 反向凭证
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-1", "date": "2026-09-25", "memo": "撤销错误采购",
    })
    assert r.status_code == 201, r.text
    reversal = r.json()
    assert reversal["reverses_id"] == original["id"]
    assert reversal["reverses_number"] == original["number"]
    # 方向全部对调，金额一致
    by_dir = {ln["direction"]: ln["amount"] for ln in reversal["lines"]}
    assert by_dir == {"credit": "800.00", "debit": "800.00"}

    # 原凭证状态变为 reversed 并回填
    fetched = client.get(f"/vouchers/{original['id']}").json()
    assert fetched["status"] == "reversed"
    assert fetched["reversed_by_id"] == reversal["id"]

    # 链：从原凭证或反向凭证查都得到同一链
    chain = client.get(f"/vouchers/{original['id']}/chain").json()
    assert chain["root_id"] == original["id"]
    assert [n["direction_in_chain"] for n in chain["nodes"]] == ["original", "reversal"]
    chain2 = client.get(f"/vouchers/{reversal['id']}/chain").json()
    assert chain2["root_id"] == original["id"]

    # 净余额归零 (800 借 + 800 贷)
    bal = client.get(f"/accounts/{expense['id']}/balances").json()
    assert bal[0]["total_debit"] == "800.00"
    assert bal[0]["total_credit"] == "800.00"
    assert bal[0]["net_debit"] == "0.00"

    # 不可重复冲销
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-2",
    })
    assert r.status_code == 409

    # 冲销的 request_id 也幂等
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-1",
    })
    assert r.status_code == 200 and r.json()["id"] == reversal["id"]


def test_reversal_allowed_into_open_period_when_original_period_closed(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")

    r = _post_voucher(client, "rev-cross", [
        {"account_id": bank["id"], "direction": "debit", "amount": "50.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "50.00"},
    ], vdate="2026-08-10")
    original = r.json()

    client.post("/periods/2026-08/close")
    # 冲销到新的开放期间 9 月
    r = client.post(f"/vouchers/{original['id']}/reverse", json={
        "request_id": "rev-cross-1", "date": "2026-09-02",
    })
    assert r.status_code == 201
    assert r.json()["period_key"] == "2026-09"


def test_voucher_immutable_and_no_delete(client):
    from sqlalchemy import event, text
    from app.database import engine

    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    v = _post_voucher(client, "imm-1", [
        {"account_id": bank["id"], "direction": "debit", "amount": "10.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "10.00"},
    ]).json()

    db = SessionLocal()
    # UPDATE 凭证关键字段被触发器拒绝
    db_obj = db.get(Voucher, v["id"])
    db_obj.memo = "试图篡改"
    from sqlalchemy.exc import IntegrityError as SAIntegrityError, OperationalError
    immutable_errors = (SAIntegrityError, OperationalError)
    try:
        db.flush()
        db.commit()
        raise AssertionError("应当触发不可变性触发器")
    except immutable_errors:
        db.rollback()

    # DELETE 凭证被拒绝 (RAISE ABORT 在 execute 时抛出)
    try:
        db.execute(text("DELETE FROM vouchers WHERE id = :i"), {"i": v["id"]})
        db.commit()
        raise AssertionError("DELETE 应当被拒绝")
    except immutable_errors:
        db.rollback()
    finally:
        db.close()
        # RAISE ABORT 会使底层连接停留在被中止的事务里，丢弃该连接
        from app.database import engine as _engine
        _engine.dispose()

    # REST 层没有删除端点 -> 405
    assert client.delete(f"/vouchers/{v['id']}").status_code == 405


def test_voucher_numbering_sequential_within_period(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    lines = [
        {"account_id": bank["id"], "direction": "debit", "amount": "1.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1.00"},
    ]
    n1 = _post_voucher(client, "seq-1", lines, vdate="2026-09-05").json()["number"]
    n2 = _post_voucher(client, "seq-2", lines, vdate="2026-09-06").json()["number"]
    other_month = _post_voucher(client, "seq-3", lines, vdate="2026-07-06").json()["number"]
    n3 = _post_voucher(client, "seq-4", lines, vdate="2026-09-07").json()["number"]
    assert (n1, n2, n3) == ("V-2026-09-0001", "V-2026-09-0002", "V-2026-09-0003")
    assert other_month == "V-2026-07-0001"


def test_list_and_filter_vouchers(client):
    bank = _create_account(client, "1002", "银行存款")
    income = _create_account(client, "4001", "科研拨款", atype="income")
    lines = [
        {"account_id": bank["id"], "direction": "debit", "amount": "1.00"},
        {"account_id": income["id"], "direction": "credit", "amount": "1.00"},
    ]
    _post_voucher(client, "lst-1", lines, vdate="2026-09-01")
    _post_voucher(client, "lst-2", lines, vdate="2026-09-02")

    listing = client.get("/vouchers").json()
    assert len(listing["items"]) == 2

    only_income = client.get("/vouchers", params={"account_id": income["id"]}).json()
    assert {v["request_id"] for v in only_income["items"]} == {"lst-1", "lst-2"}

    other = _create_account(client, "9999", "无关科目")
    assert client.get("/vouchers", params={"account_id": other["id"]}).json()["items"] == []

    period_filtered = client.get("/vouchers", params={"period": "2026-09"}).json()
    assert len(period_filtered["items"]) == 2
