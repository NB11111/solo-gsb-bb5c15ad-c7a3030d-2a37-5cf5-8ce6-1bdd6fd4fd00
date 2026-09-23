# 科研经费双重记账 API

基于 **FastAPI + SQLite** 的多币种复式记账（Double-Entry Bookkeeping）服务。
客户端先创建**带币种的科目**，再提交包含**多条借贷分录的凭证**；服务端在单个数据库事务内完成全部校验与记账，
成功后生成**不可变凭证**并推进**科目余额**。凭证不能修改或删除，撤销只能通过提交**反向凭证**完成，
全链路保留冲销关联；重复 `request_id` 请求按**幂等**处理。

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

- API 地址：<http://localhost:8000>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- SQLite 数据持久化在命名卷 `ledger-data`（容器内 `/app/data/ledger.db`）

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷（清空账套）
```

### 本地（不使用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

测试：

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## 核心记账规则

1. **借贷必相等**：一张凭证全部借方金额合计必须等于贷方金额合计，否则整笔拒绝（`unbalanced_voucher`，422）。
2. **币种一致**：凭证声明一个币种，所有分录科目必须为同币种科目（`currency_mismatch`，422）。
3. **期间管控**：凭证日期必须落在已存在的会计期间内；已结账（closed）期间不可再入账（`period_closed`，422）。
4. **事务原子性**：校验、凭证号生成、写入凭证与分录、推进余额、登记幂等键在同一个 `BEGIN IMMEDIATE` 事务中完成，失败整体回滚。
5. **凭证不可变**：`vouchers` / `entries` 表由 SQLite 触发器禁止 `UPDATE` / `DELETE`。
6. **撤销只能冲销**：不提供删除/修改接口；`POST /v1/vouchers/{id}/reverse` 生成借贷对调的反向凭证，并与原凭证双向关联；已冲销凭证不可再次冲销。
7. **request_id 幂等**：相同 `request_id` + 相同请求体重放，返回原凭证（HTTP 200），不重复记账；相同 `request_id` 但请求体不同，返回 `idempotency_conflict`（409）。
8. **金额安全**：对外金额为字符串（如 `"12000.00"`），内部以整数分度（分）存储，杜绝浮点误差；超过两位小数拒绝。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/v1/accounts` | 创建带币种的科目 |
| GET | `/v1/accounts` | 科目列表 |
| POST | `/v1/periods` | 创建会计期间（日期范围不可重叠） |
| GET | `/v1/periods` | 期间列表 |
| POST | `/v1/periods/{period_code}/close` | 期间结账 |
| POST | `/v1/periods/{period_code}/reopen` | 重新打开期间 |
| POST | `/v1/vouchers` | 提交凭证（多条借贷分录，request_id 幂等） |
| GET | `/v1/vouchers` | 凭证列表（可按期间过滤） |
| GET | `/v1/vouchers/{id或凭证号}` | 凭证详情（含冲销关联） |
| GET | `/v1/vouchers/{id或凭证号}/chain` | 凭证链：原凭证 ↔ 反向凭证 |
| POST | `/v1/vouchers/{id或凭证号}/reverse` | 提交反向凭证（撤销） |
| GET | `/v1/accounts/{code}/balance` | 科目余额（借/贷累计、净额、方向） |
| GET | `/v1/accounts/{code}/ledger?from=&to=` | 科目明细分录账 + 逐笔 running 余额 |

错误响应统一格式：

```json
{"error": {"code": "unbalanced_voucher", "message": "借贷不平衡：...", "details": {...}}}
```

---

## 完整操作示例（curl）

### 1. 创建会计期间

```bash
curl -X POST http://localhost:8000/v1/periods \
  -H 'Content-Type: application/json' \
  -d '{"period_code":"2026-09","start_date":"2026-09-01","end_date":"2026-09-30"}'
```

### 2. 创建带币种的科目

```bash
curl -X POST http://localhost:8000/v1/accounts -H 'Content-Type: application/json' \
  -d '{"code":"1002","name":"银行存款","currency":"CNY"}'
curl -X POST http://localhost:8000/v1/accounts -H 'Content-Type: application/json' \
  -d '{"code":"5301","name":"科研事业支出-材料费","currency":"CNY"}'
curl -X POST http://localhost:8000/v1/accounts -H 'Content-Type: application/json' \
  -d '{"code":"4001","name":"科研拨款收入","currency":"CNY"}'
```

### 3. 提交凭证（财政拨款到账 100,000 元）

```bash
curl -X POST http://localhost:8000/v1/vouchers \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "grant-2026-09-0001",
    "voucher_date": "2026-09-05",
    "currency": "CNY",
    "description": "2026 年度科研拨款到账",
    "entries": [
      {"account": "1002", "debit":  "100000.00", "summary": "银行收款"},
      {"account": "4001", "credit": "100000.00", "summary": "财政拨款收入"}
    ]
  }'
```

成功返回 `201` 与不可变凭证号（如 `RV-202609-0001`）。
**用同样的 request_id 再提交一次**：返回 `200` 和同一张凭证，余额不会重复增加。

### 4. 提交凭证（采购实验材料 12,000 元）

```bash
curl -X POST http://localhost:8000/v1/vouchers \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "grant-2026-09-0002",
    "voucher_date": "2026-09-10",
    "currency": "CNY",
    "description": "采购实验试剂",
    "entries": [
      {"account": "5301", "debit":  "12000.00", "summary": "试剂一批"},
      {"account": "1002", "credit": "12000.00", "summary": "转账支付"}
    ]
  }'
```

### 5. 查询余额与明细分录账

```bash
curl http://localhost:8000/v1/accounts/1002/balance
curl "http://localhost:8000/v1/accounts/1002/ledger?from=2026-09-01&to=2026-09-30"
```

余额响应示例：

```json
{
  "account": "1002", "account_name": "银行存款", "currency": "CNY",
  "debit_total": "100000.00", "credit_total": "12000.00",
  "net_debit": "88000.00", "net_credit": "0.00",
  "direction": "debit", "updated_at": "2026-09-10T..."
}
```

### 6. 撤销：提交反向凭证（发现采购有误，冲销第 4 步凭证）

```bash
curl -X POST http://localhost:8000/v1/vouchers/RV-202609-0002/reverse \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"grant-2026-09-0002-R","reversal_date":"2026-09-20"}'
```

- 系统自动生成借贷对调的分录（`5301` 贷 12,000 / `1002` 借 12,000）；
- 原凭证上出现 `reversed_by`，反向凭证上出现 `reverses`，双向可追溯；
- 冲销后净余额回到 88,000 元，但借/贷**累计发生额保留**（体现审计轨迹）；
- 再次对原凭证冲销 → `409 voucher_already_reversed`；
- 反向凭证可落在新的开放期间（原期间结账后也能在当期冲销）。

### 7. 凭证链查询

```bash
curl http://localhost:8000/v1/vouchers/RV-202609-0002/chain
# -> [RV-202609-0002(原始), RV-202609-0003(冲销)]，按链顺序返回
```

### 8. 结账锁定期间

```bash
curl -X POST http://localhost:8000/v1/periods/2026-09/close
# 此后任何凭证日期落在 2026-09 的提交（含冲销）都返回 422 period_closed
```

---

## 数据模型

```
accounts  科目(id, code 唯一, name, currency)
periods   期间(id, period_code YYYY-MM, start_date, end_date, is_closed)
vouchers  凭证(id, voucher_no 唯一, period_id, date, currency, description,
              reverses_id → vouchers.id   -- 仅反向凭证非空)
entries   分录(id, voucher_id, line_no, account_id, debit, credit, summary)
balances  余额(account_id 唯一, currency, debit_total, credit_total, net_debit)
idempotency_keys  幂等(request_id 主键, endpoint, fingerprint, voucher_id)
```

- 金额列：`INTEGER`，单位为百分之一（分），`CHECK (debit >= 0 AND credit >= 0)`；
  一条分录只能有借或有贷，不能同时为 0。
- 触发器：`vouchers`、`entries` 禁止 `UPDATE/DELETE`；科目代码/名称/币种禁止修改。
- 并发行：WAL 模式 + `BEGIN IMMEDIATE` + `busy_timeout=30s`，写事务在 SQLite 层串行化。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与错误处理
│   ├── service.py     # 记账领域服务（事务、校验、冲销、查询）
│   ├── schemas.py     # Pydantic 模型
│   ├── money.py       # 金额字符串 ↔ 整数分度
│   ├── db.py          # SQLite 连接 / 事务 / 初始化
│   ├── errors.py      # 业务错误类型
│   └── schema.sql     # 表结构与不可变触发器
├── tests/             # pytest 端到端测试（18 个用例）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
