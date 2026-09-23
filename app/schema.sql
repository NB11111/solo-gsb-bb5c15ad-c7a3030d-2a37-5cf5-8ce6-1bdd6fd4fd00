-- 科研经费双重记账系统：数据库结构
-- 金额一律以「整数分度」存储（如 CNY/EUR 的分），避免浮点误差；
-- 对外通过字符串形式的十进制金额交互。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 会计科目（带币种）
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL UNIQUE,
    name         TEXT    NOT NULL,
    currency     TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    created_at   TEXT    NOT NULL
);

-- 会计期间（按月），关闭后不可再入凭证
CREATE TABLE IF NOT EXISTS periods (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_code  TEXT    NOT NULL UNIQUE,           -- YYYY-MM
    start_date   TEXT    NOT NULL,                  -- ISO 日期
    end_date     TEXT    NOT NULL,
    is_closed    INTEGER NOT NULL DEFAULT 0,
    closed_at    TEXT,
    CHECK (start_date <= end_date)
);

-- 凭证（记账凭证）。一经提交即不可变：更新/删除由触发器拒绝。
CREATE TABLE IF NOT EXISTS vouchers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no      TEXT    NOT NULL UNIQUE,        -- 形如 RV-202609-0001
    period_id       INTEGER NOT NULL REFERENCES periods(id),
    voucher_date    TEXT    NOT NULL,               -- ISO 日期
    currency        TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    description     TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    -- 反向凭证与被冲销凭证之间的关联（仅反向凭证非空）
    reverses_id     INTEGER REFERENCES vouchers(id),
    reversal_type   TEXT CHECK (reversal_type IS NULL OR reversal_type = 'reversal')
);

-- 凭证分录（行）。同样不可变。
CREATE TABLE IF NOT EXISTS entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id   INTEGER NOT NULL REFERENCES vouchers(id),
    line_no      INTEGER NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    debit        INTEGER NOT NULL DEFAULT 0 CHECK (debit  >= 0),
    credit       INTEGER NOT NULL DEFAULT 0 CHECK (credit >= 0),
    summary      TEXT    NOT NULL DEFAULT '',
    CHECK (debit > 0 OR credit > 0),                 -- 不允许空分录
    CHECK (NOT (debit > 0 AND credit > 0)),          -- 一行只记借或贷
    UNIQUE (voucher_id, line_no)
);

-- 科目累计余额（按币种）。每张凭证在同一事务内推进。
CREATE TABLE IF NOT EXISTS balances (
    account_id   INTEGER PRIMARY KEY REFERENCES accounts(id),
    currency     TEXT    NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
    debit_total  INTEGER NOT NULL DEFAULT 0 CHECK (debit_total  >= 0),
    credit_total INTEGER NOT NULL DEFAULT 0 CHECK (credit_total >= 0),
    net_debit    INTEGER NOT NULL DEFAULT 0,         -- 借方正数 / 贷方负数
    updated_at   TEXT    NOT NULL
);

-- request_id 幂等表：记录已处理的请求与其结果
CREATE TABLE IF NOT EXISTS idempotency_keys (
    request_id     TEXT PRIMARY KEY,
    endpoint       TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,                    -- 请求体指纹
    voucher_id     INTEGER NOT NULL REFERENCES vouchers(id),
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_account     ON entries(account_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_period     ON vouchers(period_id);
CREATE INDEX IF NOT EXISTS idx_vouchers_reverses    ON vouchers(reverses_id);

-- ---------------------------------------------------------------------------
-- 不可变性约束：已生成的凭证与分录禁止 UPDATE/DELETE
-- （余额/期间状态等运维数据不在此限）
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_vouchers_immutable_update
BEFORE UPDATE ON vouchers
BEGIN
    SELECT RAISE(ABORT, 'vouchers are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_vouchers_immutable_delete
BEFORE DELETE ON vouchers
BEGIN
    SELECT RAISE(ABORT, 'vouchers are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_entries_immutable_update
BEFORE UPDATE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_entries_immutable_delete
BEFORE DELETE ON entries
BEGIN
    SELECT RAISE(ABORT, 'entries are immutable');
END;

-- 科目创建后币种即不可更改（防止余额串币）
CREATE TRIGGER IF NOT EXISTS trg_accounts_immutable
BEFORE UPDATE OF code, name, currency ON accounts
BEGIN
    SELECT RAISE(ABORT, 'accounts are immutable after creation');
END;
