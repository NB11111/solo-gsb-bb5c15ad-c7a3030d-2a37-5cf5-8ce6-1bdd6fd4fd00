# 科研经费双重记账 API

基于 **FastAPI + SQLite + SQLAlchemy 2.x** 的复式记账（double-entry bookkeeping）服务，面向科研项目经费管理场景。提供 `Dockerfile` 与 `docker-compose.yml`，一条命令启动。

## 功能特性

- **带币种的会计科目**：每个科目绑定 ISO 4217 币种（如 `CNY` / `USD`），类型为资产/负债/净资产/收入/费用。
- **多借贷分录凭证**：一张凭证包含多条分录，金额以**整数分**存储（API 用十进制字符串传输），杜绝浮点误差。
- **事务内强校验**（`BEGIN IMMEDIATE` 单事务，失败整体回滚）：
  - 借方总额 = 贷方总额；
  - 至少一条借方与一条贷方；
  - 凭证币种与所有分录科目币种一致（禁止跨币种凭证）；
  - 入账期间必须存在且**未结账**。
- **不可变凭证**：过账后凭证头/分录不可 `UPDATE`/`DELETE`（服务层 + SQLite 触发器双重保证），并分配期间内顺序凭证号 `V-YYYY-MM-NNNN`。
- **只能反向冲销**：撤销通过提交「反向凭证」完成——借贷方向逐行对调、金额不变，双向保留 `reverses_id` / `reversed_by_id` 关联，可跨期间冲销（原期间已结账也可冲入新的开放期间），禁止重复冲销。
- **request_id 幂等**：客户端为每次提交提供唯一键，网络重试/重复提交返回同一凭证（`200` + `X-Idempotent-Replay: true`），余额不重复累计。
- **余额物化**：按（科目 × 期间）实时维护借贷累计额，过账/冲销在同一事务内 UPSERT。
- **查询**：科目余额（可按期间）、科目分类账（逐笔累计余额、游标分页）、凭证列表（按科目/期间过滤）、凭证链（原始凭证 ↔ 反向凭证）。
- **并发安全**：SQLite WAL + `BEGIN IMMEDIATE` + `busy_timeout`，写入在事务起点排队；已验证 16 线程并发无锁错误、凭证号不重号。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

- API：http://localhost:8000
- 交互式文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- SQLite 数据持久化在命名卷 `ledger-data`（容器内 `/data`）
- 首次启动自动写入一套科研经费演示账套（`SEED_DEMO=true`，幂等）

不想用演示数据时，把 `docker-compose.yml` 中的 `SEED_DEMO` 改为 `"false"`。

停止并保留数据：

```bash
docker compose down          # 保留 ledger-data 卷
docker compose down -v       # 同时删除数据卷
```

## 本地运行（不用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 可选：初始化演示账套
DATA_DIR=./data python3 -m app.seed

uvicorn app.main:app --reload
```

运行测试：

```bash
python3 -m pytest -q
```

## API 总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 创建带币种的科目 |
| GET | `/accounts` | 科目列表 |
| GET | `/accounts/{id}` | 科目详情 |
| GET | `/accounts/{id}/balances?period=YYYY-MM` | 科目各期间余额 |
| GET | `/accounts/{id}/ledger?period_from=&period_to=&cursor=&limit=` | 科目分类账（逐笔累计） |
| POST | `/vouchers` | 提交凭证（`request_id` 幂等） |
| GET | `/vouchers?account_id=&period=&cursor=` | 凭证列表/过滤 |
| GET | `/vouchers/by-request/{request_id}` | 按幂等键反查 |
| GET | `/vouchers/{id}` | 凭证详情 |
| GET | `/vouchers/{id}/chain` | 凭证链 |
| POST | `/vouchers/{id}/reverse` | 提交反向凭证冲销 |
| GET | `/periods` / `/periods/{key}` | 期间列表/详情 |
| POST | `/periods/{key}/close` | 结账（幂等，冻结入账） |
| POST | `/periods/{key}/reopen` | 重新打开期间（纠错用） |

错误统一返回：

```json
{ "error": { "code": "unprocessable_entity", "message": "借贷不平衡",
             "detail": { "reason": "debits_not_equal_credits",
                         "total_debit": 1200000, "total_credit": 999900 } } }
```

## 快速上手示例

### 1. 创建科目

```bash
curl -XPOST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "code": "1002", "name": "银行存款", "currency": "CNY", "type": "asset"
}'
curl -XPOST localhost:8000/accounts -H 'Content-Type: application/json' -d '{
  "code": "4001", "name": "科研拨款收入", "currency": "CNY", "type": "income"
}'
```

### 2. 提交拨款到账凭证（借银行 / 贷收入）

```bash
curl -XPOST localhost:8000/vouchers -H 'Content-Type: application/json' -d '{
  "request_id": "grant-2026-0001",
  "date": "2026-09-20",
  "currency": "CNY",
  "memo": "国家自然科学基金拨款到账",
  "lines": [
    {"account_id": 1, "direction": "debit",  "amount": "1000000.00"},
    {"account_id": 2, "direction": "credit", "amount": "1000000.00"}
  ]
}'
```

借贷不平、科目币种与凭证币种不一致、期间已结账等情况会得到 `422`/`409`，且不写入任何数据。用同一 `request_id` 重放得到同一凭证、余额不翻倍。

### 3. 结账与冲销

```bash
# 冻结 2026-09，之后不能再向该期间入账（409）
curl -XPOST localhost:8000/periods/2026-09/close

# 唯一允许的撤销方式：反向凭证（方向对调、金额不变、保留关联），可冲入 10 月
curl -XPOST localhost:8000/vouchers/1/reverse -H 'Content-Type: application/json' -d '{
  "request_id": "grant-2026-0001-rev",
  "date": "2026-10-08",
  "memo": "撤销错误入账"
}'
```

### 4. 查询余额与凭证链

```bash
curl localhost:8000/accounts/1/balances
curl localhost:8000/accounts/1/ledger
curl localhost:8000/vouchers/1/chain
```

## 关键设计说明

### 复式记账规则

- 分录金额为非负整数分，方向只取 `debit` / `credit`；
- 过账条件：`Σ借方 = Σ贷方`，且两侧都至少有一行；
- 余额字段：`total_debit`、`total_credit`、`net_debit = 借−贷`、`net_credit = 贷−借`（资产/费用正常为借方余额，负债/收入为贷方余额）；
- 反向凭证不是负数金额，而是**金额不变、方向全部对调**的新凭证，因此它本身仍是一张合法的、借贷平衡的凭证。

### 不可变性（两层防护）

1. API 层根本不提供凭证修改/删除端点（`DELETE` 返回 405）；
2. SQLite 触发器拦截凭证头关键字段更新、分录的更新/删除、凭证与余额的删除。过账后唯一允许的变化是登记冲销关系（`status: posted → reversed`、`reversed_by_id`），且状态不可回退。

### 事务与并发

所有写入（校验 → 编号 → 凭证/分录 → 余额 UPSERT → 冲销回填）在单个事务内完成。连接配置：

- pysqlite autocommit 模式由 SQLAlchemy 全权管理事务边界；
- 每个事务以 `BEGIN IMMEDIATE` 开始，进入即取保留锁，写冲突在事务起点排队（`busy_timeout=10s`），而非在提交时抛锁错误；
- WAL 模式允许读写并发。

### 金额处理

API 边界使用字符串（正则限定非负、最多两位小数），服务端用 `Decimal` 转为整数分存储（`app/money.py`），比较与汇总全部为整数运算。

## 项目结构

```
.
├── app/
│   ├── main.py              # FastAPI 入口、生命周期、异常处理
│   ├── config.py            # 配置 (DB URL、金额上限、busy timeout)
│   ├── database.py          # 引擎/会话、BEGIN IMMEDIATE、建表与触发器
│   ├── models.py            # ORM: Account/Period/Voucher/VoucherLine/Balance/...
│   ├── schemas.py           # Pydantic 请求/响应模型
│   ├── money.py             # 字符串金额 ↔ 整数分、期间键
│   ├── errors.py            # 统一业务异常
│   ├── serializers.py       # ORM -> 响应模型
│   ├── seed.py              # 演示账套 (幂等)
│   ├── routers/             # accounts / periods / vouchers
│   └── services/            # master_data（科目/期间）、voucher（过账/冲销/查询）
├── tests/test_api.py        # 12 组端到端测试
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
