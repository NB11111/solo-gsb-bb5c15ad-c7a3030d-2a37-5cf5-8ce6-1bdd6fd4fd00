"""领域服务层：科目 / 期间 / 凭证 / 冲销 / 查询。

所有写操作都在单个 BEGIN IMMEDIATE 事务中完成：
  校验 -> 生成凭证号 -> 写凭证与分录 -> 推进余额 -> 登记幂等键。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone

from .errors import (
    AccountNotFound,
    CurrencyMismatch,
    IdempotencyConflict,
    LedgerError,
    NotBalanced,
    PeriodClosed,
    PeriodNotFound,
    VoucherAlreadyReversed,
    VoucherNotFound,
)
from .db import write_tx
from .money import format_amount, parse_amount
from .schemas import (
    AccountCreate,
    EntryIn,
    LedgerOut,
    PeriodCreate,
    VoucherCreate,
    VoucherOut,
    VoucherReverse,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(payload: dict) -> str:
    """对规范化后的请求体取 SHA-256，用于检测同一 request_id 内容是否一致。"""
    canonical = json.dumps(payload, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ===================================================================
# 科目
# ===================================================================
def create_account(conn: sqlite3.Connection, data: AccountCreate) -> sqlite3.Row:
    now = _now()
    with conn:  # 自动事务
        try:
            cur = conn.execute(
                "INSERT INTO accounts(code, name, currency, created_at) "
                "VALUES (?, ?, ?, ?)",
                (data.code, data.name, data.currency, now),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(
                "account_conflict",
                f"科目代码 {data.code} 已存在", 409,
            ) from exc
        return conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (cur.lastrowid,)
        ).fetchone()


def list_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM accounts ORDER BY code"))


def _get_account_by_code(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM accounts WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise AccountNotFound(code)
    return row


# ===================================================================
# 会计期间
# ===================================================================
def create_period(conn: sqlite3.Connection, data: PeriodCreate) -> sqlite3.Row:
    if data.start_date > data.end_date:
        raise LedgerError(
            "invalid_period", "start_date 不能晚于 end_date", 422,
        )
    # 期间日期范围不得与既有期间重叠（持写锁检查，防止并发插入重叠期间）
    with write_tx(conn):
        overlap = conn.execute(
            "SELECT period_code FROM periods "
            "WHERE start_date <= ? AND end_date >= ?",
            (data.end_date.isoformat(), data.start_date.isoformat()),
        ).fetchone()
        if overlap is not None:
            raise LedgerError(
                "period_overlap",
                f"期间与既有期间 {overlap['period_code']} 日期范围重叠", 409,
            )
        try:
            cur = conn.execute(
                "INSERT INTO periods(period_code, start_date, end_date) "
                "VALUES (?, ?, ?)",
                (data.period_code, data.start_date.isoformat(),
                 data.end_date.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(
                "period_conflict", f"期间 {data.period_code} 已存在", 409,
            ) from exc
        period_id = cur.lastrowid
    return conn.execute(
        "SELECT * FROM periods WHERE id = ?", (period_id,)
    ).fetchone()


def list_periods(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM periods ORDER BY period_code"))


def close_period(conn: sqlite3.Connection, period_code: str) -> sqlite3.Row:
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM periods WHERE period_code = ?", (period_code,)
        ).fetchone()
        if row is None:
            raise LedgerError("period_not_found",
                              f"期间不存在: {period_code}", 404)
        if not row["is_closed"]:
            conn.execute(
                "UPDATE periods SET is_closed = 1, closed_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
        period_id = row["id"]
    return conn.execute(
        "SELECT * FROM periods WHERE id = ?", (period_id,)
    ).fetchone()


def reopen_period(conn: sqlite3.Connection, period_code: str) -> sqlite3.Row:
    """开放期间（运维/测试用）。凭证不可变，期间状态可调整。"""
    with write_tx(conn):
        row = conn.execute(
            "SELECT * FROM periods WHERE period_code = ?", (period_code,)
        ).fetchone()
        if row is None:
            raise LedgerError("period_not_found",
                              f"期间不存在: {period_code}", 404)
        conn.execute(
            "UPDATE periods SET is_closed = 0, closed_at = NULL WHERE id = ?",
            (row["id"],),
        )
        period_id = row["id"]
    return conn.execute(
        "SELECT * FROM periods WHERE id = ?", (period_id,)
    ).fetchone()


def _open_period_for_date(conn: sqlite3.Connection, iso_date: str) -> sqlite3.Row:
    period = conn.execute(
        "SELECT * FROM periods WHERE start_date <= ? AND end_date >= ?",
        (iso_date, iso_date),
    ).fetchone()
    if period is None:
        raise PeriodNotFound(iso_date)
    if period["is_closed"]:
        raise PeriodClosed(period["period_code"])
    return period


# ===================================================================
# 凭证
# ===================================================================
def _normalized_entries(entries: list[EntryIn], currency: str
                        ) -> list[tuple[str, int, int, str]]:
    """把输入分录解析成 (科目代码, 借方分度, 贷方分度, 摘要)，并做行级校验。"""
    if len(entries) < 2:
        raise LedgerError("invalid_voucher", "一张凭证至少需要两条分录", 422)

    out: list[tuple[str, int, int, str]] = []
    for idx, e in enumerate(entries, start=1):
        debit = parse_amount(e.debit, field=f"entries[{idx}].debit")
        credit = parse_amount(e.credit, field=f"entries[{idx}].credit")
        if debit < 0 or credit < 0:
            raise LedgerError(
                "invalid_amount", f"第 {idx} 条分录金额不能为负", 422,
            )
        if debit == 0 and credit == 0:
            raise LedgerError(
                "empty_entry", f"第 {idx} 条分录借贷均为 0", 422,
            )
        if debit > 0 and credit > 0:
            raise LedgerError(
                "invalid_entry",
                f"第 {idx} 条分录不能同时存在借方与贷方金额", 422,
            )
        out.append((e.account.strip(), debit, credit, e.summary))
    return out


def _next_voucher_no(conn: sqlite3.Connection, period_code: str) -> str:
    """期间内顺序编号，形如 RV-202609-0001。调用处须已持有写事务。"""
    prefix = f"RV-{period_code.replace('-', '')}-"
    row = conn.execute(
        "SELECT voucher_no FROM vouchers WHERE voucher_no LIKE ? "
        "ORDER BY voucher_no DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    seq = int(row["voucher_no"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


def _consume_idempotency(conn: sqlite3.Connection, request_id: str,
                         endpoint: str, fingerprint: str) -> sqlite3.Row | None:
    """命中幂等键时返回既有凭证行；内容不一致则冲突。返回 None 表示新请求。"""
    existing = conn.execute(
        "SELECT * FROM idempotency_keys WHERE request_id = ?", (request_id,),
    ).fetchone()
    if existing is None:
        return None
    if existing["endpoint"] != endpoint or existing["fingerprint"] != fingerprint:
        raise IdempotencyConflict(request_id)
    return conn.execute(
        "SELECT * FROM vouchers WHERE id = ?", (existing["voucher_id"],)
    ).fetchone()


def _apply_balances(conn: sqlite3.Connection,
                    deltas: "OrderedDict[int, tuple[int, int]]",
                    currency: str, now: str) -> None:
    """按科目聚合后的借/贷增量推进累计余额（UPSERT）。"""
    for account_id, (d_debit, d_credit) in deltas.items():
        conn.execute(
            """
            INSERT INTO balances(account_id, currency, debit_total,
                                 credit_total, net_debit, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                debit_total  = debit_total  + excluded.debit_total,
                credit_total = credit_total + excluded.credit_total,
                net_debit    = net_debit    + excluded.debit_total
                                             - excluded.credit_total,
                updated_at   = excluded.updated_at
            """,
            (account_id, currency, d_debit, d_credit, d_debit - d_credit, now),
        )


def _load_voucher_with_links(conn: sqlite3.Connection,
                             voucher_id: int) -> sqlite3.Row:
    return conn.execute(
        """
        SELECT v.*,
               rp.voucher_no   AS reverses_no,
               rp.voucher_date AS reverses_date,
               rb.id           AS reversed_by_id,
               rb.voucher_no   AS reversed_by_no,
               rb.voucher_date AS reversed_by_date
        FROM vouchers v
        LEFT JOIN vouchers rp ON rp.id = v.reverses_id
        LEFT JOIN vouchers rb ON rb.reverses_id = v.id
        WHERE v.id = ?
        """,
        (voucher_id,),
    ).fetchone()


def create_voucher(conn: sqlite3.Connection, data: VoucherCreate,
                   *, endpoint: str = "POST /v1/vouchers") -> tuple[VoucherOut, bool]:
    """提交凭证。返回 (凭证, 是否复用幂等结果)。整段在一个写事务中。"""
    iso_date = data.voucher_date.isoformat()
    norm = _normalized_entries(data.entries, data.currency)
    total_debit = sum(d for _, d, _, _ in norm)
    total_credit = sum(c for _, _, c, _ in norm)

    # 校验 1：借贷总额相等
    if total_debit != total_credit:
        raise NotBalanced(format_amount(total_debit), format_amount(total_credit))

    fingerprint = _fingerprint(data.model_dump(mode="json"))

    # 取写锁后执行所有读校验，避免并发下的 TOCTOU
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _consume_idempotency(conn, data.request_id,
                                        endpoint, fingerprint)
        replayed = existing is not None
        vrow = None
        if existing is not None:
            vrow = _load_voucher_with_links(conn, existing["id"])
        if vrow is None:
            # 校验 2：期间存在且未结账
            period = _open_period_for_date(conn, iso_date)

            # 校验 3：逐行科目存在且币种一致；同时按科目聚合
            account_rows: dict[str, sqlite3.Row] = {}
            deltas: OrderedDict[int, tuple[int, int]] = OrderedDict()
            for code, debit, credit, _summary in norm:
                acc = account_rows.get(code) or _get_account_by_code(conn, code)
                account_rows[code] = acc
                if acc["currency"] != data.currency:
                    raise CurrencyMismatch(code, acc["currency"], data.currency)
                d0, c0 = deltas.get(acc["id"], (0, 0))
                deltas[acc["id"]] = (d0 + debit, c0 + credit)

            now = _now()
            voucher_no = _next_voucher_no(conn, period["period_code"])
            cur = conn.execute(
                "INSERT INTO vouchers(voucher_no, period_id, voucher_date, "
                "currency, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (voucher_no, period["id"], iso_date, data.currency,
                 data.description, now),
            )
            voucher_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO entries(voucher_id, line_no, account_id, "
                "debit, credit, summary) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (voucher_id, i, account_rows[code]["id"],
                     debit, credit, summary)
                    for i, (code, debit, credit, summary) in enumerate(norm, 1)
                ],
            )
            _apply_balances(conn, deltas, data.currency, now)
            conn.execute(
                "INSERT INTO idempotency_keys(request_id, endpoint, fingerprint, "
                "voucher_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (data.request_id, endpoint, fingerprint, voucher_id, now),
            )
            vrow = _load_voucher_with_links(conn, voucher_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return serialize_voucher(conn, vrow), replayed


def reverse_voucher(conn: sqlite3.Connection, ref: str | int,
                    data: VoucherReverse,
                    *, endpoint_tpl: str = "POST /v1/vouchers/{}/reverse"
                    ) -> tuple[VoucherOut, bool]:
    """提交反向凭证：借贷方向对调，并与原凭证保留双向关联。"""
    fingerprint = _fingerprint({
        "action": "reverse",
        "target": str(ref),
        "reversal_date": data.reversal_date.isoformat(),
        "description": data.description,
    })
    endpoint = endpoint_tpl.format(ref)
    iso_date = data.reversal_date.isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _consume_idempotency(conn, data.request_id,
                                        endpoint, fingerprint)
        replayed = existing is not None
        vrow = None
        if existing is not None:
            vrow = _load_voucher_with_links(conn, existing["id"])
        if vrow is None:
            original = _get_voucher(conn, ref)
            # 已被冲销的凭证不能重复冲销
            existing_rev = conn.execute(
                "SELECT voucher_no FROM vouchers WHERE reverses_id = ?",
                (original["id"],),
            ).fetchone()
            if existing_rev is not None:
                raise VoucherAlreadyReversed(original["voucher_no"],
                                             existing_rev["voucher_no"])

            # 反向凭证落入冲销日所在的（开放）期间
            period = _open_period_for_date(conn, iso_date)

            orig_entries = list(conn.execute(
                "SELECT e.*, a.code AS account_code "
                "FROM entries e JOIN accounts a ON a.id = e.account_id "
                "WHERE e.voucher_id = ? ORDER BY e.line_no",
                (original["id"],),
            ))

            description = data.description or f"冲销凭证 {original['voucher_no']}"
            now = _now()
            voucher_no = _next_voucher_no(conn, period["period_code"])
            cur = conn.execute(
                "INSERT INTO vouchers(voucher_no, period_id, voucher_date, "
                "currency, description, created_at, reverses_id, reversal_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'reversal')",
                (voucher_no, period["id"], iso_date, original["currency"],
                 description, now, original["id"]),
            )
            rev_id = cur.lastrowid

            deltas: OrderedDict[int, tuple[int, int]] = OrderedDict()
            new_rows = []
            for i, e in enumerate(orig_entries, start=1):
                # 借贷对调
                new_rows.append((rev_id, i, e["account_id"],
                                 e["credit"], e["debit"],
                                 f"冲销: {e['summary']}".strip(": ")))
                d0, c0 = deltas.get(e["account_id"], (0, 0))
                deltas[e["account_id"]] = (d0 + e["credit"], c0 + e["debit"])
            conn.executemany(
                "INSERT INTO entries(voucher_id, line_no, account_id, "
                "debit, credit, summary) VALUES (?, ?, ?, ?, ?, ?)",
                new_rows,
            )
            _apply_balances(conn, deltas, original["currency"], now)
            conn.execute(
                "INSERT INTO idempotency_keys(request_id, endpoint, fingerprint, "
                "voucher_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (data.request_id, endpoint, fingerprint, rev_id, now),
            )
            vrow = _load_voucher_with_links(conn, rev_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return serialize_voucher(conn, vrow), replayed


# ===================================================================
# 查询
# ===================================================================
def _get_voucher(conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row:
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        row = conn.execute("SELECT * FROM vouchers WHERE id = ?", (int(ref),)
                           ).fetchone()
    else:
        row = conn.execute("SELECT * FROM vouchers WHERE voucher_no = ?",
                           (ref,)).fetchone()
    if row is None:
        raise VoucherNotFound(str(ref))
    return row


def get_voucher(conn: sqlite3.Connection, ref: str | int) -> VoucherOut:
    row = _get_voucher(conn, ref)
    full = _load_voucher_with_links(conn, row["id"])
    return serialize_voucher(conn, full)


def voucher_chain(conn: sqlite3.Connection, ref: str | int) -> list[VoucherOut]:
    """凭证链：沿冲销关系回溯到原始凭证，再正向展开整链（按时间排序）。"""
    row = _get_voucher(conn, ref)
    root_id = row["id"]
    cur = row
    # 向上回溯
    while cur["reverses_id"] is not None:
        cur = conn.execute("SELECT * FROM vouchers WHERE id = ?",
                           (cur["reverses_id"],)).fetchone()
    root_id = cur["id"]
    # 向下展开
    chain_ids = [root_id]
    while True:
        nxt = conn.execute(
            "SELECT id FROM vouchers WHERE reverses_id = ? ORDER BY id",
            (chain_ids[-1],),
        ).fetchall()
        # 每个凭证至多有一张反向凭证（服务层保证）
        if not nxt:
            break
        chain_ids.append(nxt[0]["id"])
    return [serialize_voucher(conn, _load_voucher_with_links(conn, vid))
            for vid in chain_ids]


def list_vouchers(conn: sqlite3.Connection, *, limit: int = 50,
                  period_code: str | None = None) -> list[VoucherOut]:
    sql = (
        "SELECT v.id FROM vouchers v "
        "JOIN periods p ON p.id = v.period_id "
    )
    params: list = []
    if period_code:
        sql += "WHERE p.period_code = ? "
        params.append(period_code)
    sql += "ORDER BY v.id DESC LIMIT ?"
    params.append(limit)
    ids = [r["id"] for r in conn.execute(sql, params)]
    return [serialize_voucher(conn, _load_voucher_with_links(conn, i))
            for i in ids]


def serialize_voucher(conn: sqlite3.Connection, v: sqlite3.Row) -> VoucherOut:
    rows = conn.execute(
        "SELECT e.*, a.code AS account_code "
        "FROM entries e JOIN accounts a ON a.id = e.account_id "
        "WHERE e.voucher_id = ? ORDER BY e.line_no",
        (v["id"],),
    ).fetchall()
    debit_total = sum(r["debit"] for r in rows)
    credit_total = sum(r["credit"] for r in rows)
    period_code = conn.execute(
        "SELECT period_code FROM periods WHERE id = ?", (v["period_id"],)
    ).fetchone()["period_code"]

    def _ref(rel: str):
        vid, no, d = {
            "reverses": (v["reverses_id"], v["reverses_no"], v["reverses_date"]),
            "reversed_by": (v["reversed_by_id"], v["reversed_by_no"],
                            v["reversed_by_date"]),
        }[rel]
        if vid is None:
            return None
        from .schemas import ReversalRef
        return ReversalRef(voucher_id=vid, voucher_no=no,
                           voucher_date=d, relation=rel)

    from .schemas import EntryOut
    return VoucherOut(
        id=v["id"], voucher_no=v["voucher_no"], voucher_date=v["voucher_date"],
        period=period_code, currency=v["currency"],
        description=v["description"], created_at=v["created_at"],
        debit_total=format_amount(debit_total),
        credit_total=format_amount(credit_total),
        entries=[
            EntryOut(line_no=r["line_no"], account=r["account_code"],
                     debit=format_amount(r["debit"]),
                     credit=format_amount(r["credit"]), summary=r["summary"])
            for r in rows
        ],
        reverses=_ref("reverses"), reversed_by=_ref("reversed_by"),
    )


def get_balance(conn: sqlite3.Connection, account_code: str):
    acc = _get_account_by_code(conn, account_code)
    bal = conn.execute(
        "SELECT * FROM balances WHERE account_id = ?", (acc["id"],)
    ).fetchone()
    return acc, bal


def get_ledger(conn: sqlite3.Connection, account_code: str,
               date_from: str | None = None, date_to: str | None = None
               ) -> LedgerOut:
    """按科目返回期间内明细分录链与累计（running）余额。

    期初 = 起始日之前所有分录的合计；行内 running_* 为逐笔累计。
    """
    acc = _get_account_by_code(conn, account_code)

    where = ["e.account_id = ?"]
    params: list = [acc["id"]]
    if date_from:
        where.append("v.voucher_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("v.voucher_date <= ?")
        params.append(date_to)
    where_sql = " AND ".join(where)

    # 期初（窗口起点之前）
    if date_from:
        op = conn.execute(
            "SELECT COALESCE(SUM(e.debit),0) AS d, COALESCE(SUM(e.credit),0) AS c "
            "FROM entries e JOIN vouchers v ON v.id = e.voucher_id "
            "WHERE e.account_id = ? AND v.voucher_date < ?",
            (acc["id"], date_from),
        ).fetchone()
    else:
        op = {"d": 0, "c": 0}
    opening_d, opening_c = op["d"], op["c"]

    lines_rows = conn.execute(
        f"""
        SELECT e.line_no, e.debit, e.credit, e.summary,
               v.id AS voucher_id, v.voucher_no, v.voucher_date,
               v.description, v.reverses_id, v.reversal_type,
               rv.voucher_no AS linked_no
        FROM entries e
        JOIN vouchers v ON v.id = e.voucher_id
        LEFT JOIN vouchers rv ON rv.id = v.reverses_id
        WHERE {where_sql}
        ORDER BY v.voucher_date, v.id, e.line_no
        """,
        params,
    ).fetchall()

    from .schemas import LedgerEntryOut

    run_d, run_c = opening_d, opening_c
    lines = []
    for r in lines_rows:
        run_d += r["debit"]
        run_c += r["credit"]
        linked_relation = "reverses" if r["reverses_id"] else None
        lines.append(LedgerEntryOut(
            line_no=r["line_no"], voucher_id=r["voucher_id"],
            voucher_no=r["voucher_no"], voucher_date=r["voucher_date"],
            description=r["description"],
            debit=format_amount(r["debit"]), credit=format_amount(r["credit"]),
            running_debit_total=format_amount(run_d),
            running_credit_total=format_amount(run_c),
            running_net_debit=format_amount(run_d - run_c),
            is_reversal=r["reversal_type"] == "reversal",
            linked_voucher_no=r["linked_no"],
            linked_relation=linked_relation,
        ))

    return LedgerOut(
        account=acc["code"], account_name=acc["name"], currency=acc["currency"],
        period_from=date_from, period_to=date_to,
        opening_net_debit=format_amount(opening_d - opening_c),
        opening_debit_total=format_amount(opening_d),
        opening_credit_total=format_amount(opening_c),
        lines=lines,
        closing_debit_total=format_amount(run_d),
        closing_credit_total=format_amount(run_c),
        closing_net_debit=format_amount(run_d - run_c),
    )
