import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_ledger.db"
    monkeypatch.setenv("LEDGER_DB_PATH", str(db_path))

    # 延迟导入，确保读取到测试用的 LEDGER_DB_PATH
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def setup_month(client):
    """创建 2026-09 自然月期间。"""
    resp = client.post("/v1/periods", json={
        "period_code": "2026-09",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
    })
    assert resp.status_code == 201, resp.text


def make_account(client, code, name, currency="CNY"):
    resp = client.post("/v1/accounts", json={
        "code": code, "name": name, "currency": currency,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_voucher(client, request_id, entries, *, date="2026-09-10",
                 currency="CNY", description="测试凭证"):
    return client.post("/v1/vouchers", json={
        "request_id": request_id,
        "voucher_date": date,
        "currency": currency,
        "description": description,
        "entries": entries,
    })
