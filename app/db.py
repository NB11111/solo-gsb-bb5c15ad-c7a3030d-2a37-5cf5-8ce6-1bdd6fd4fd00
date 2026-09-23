"""SQLite 连接管理。

每个请求一条连接；写操作使用 BEGIN IMMEDIATE 立即取写锁，
配合 WAL 模式，让并发的多凭证提交在 SQLite 层面串行化，
余额、凭证编号、幂等键在同一事务内保持一致。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _resolve_path() -> Path:
    import os

    return Path(os.environ.get("LEDGER_DB_PATH", str(DB_PATH)))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(
        _resolve_path(),
        detect_types=sqlite3.PARSE_DECLTYPES,
        timeout=30.0,
        isolation_level=None,  # 手工管理事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖。"""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """串行写事务：BEGIN IMMEDIATE ... COMMIT/ROLLBACK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(path: Path | str | None = None) -> Path:
    """应用启动 / 测试时初始化数据库。"""
    db_path = Path(path) if path else _resolve_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return db_path
