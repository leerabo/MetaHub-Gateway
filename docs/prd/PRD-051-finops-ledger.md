# PRD-051 FinOps Ledger 正式账本设计

> 状态：已完成。两轮编码前自审、实现、0041、分层回归、真实 Runtime/Browser、日志隐私与停服终态审计均已通过。权威 Browser 批次为 `artifacts/prd051/20260727T043500-browser-success-02`；本状态只代表 PRD-051 完成，不代表全部 108 条 PRD 已完成。

## 1. 权威需求与现状

### 1.1 需求来源

- Excel `01_PRD总表` 第 52 行：`PRD-051`，`M1-企业POC`，`P1`，核心层，名称为“FinOps Ledger正式账本”。
- 用户：财务 / 管理员。当前 RBAC 只有 `admin/developer/viewer`，独立财务角色由 PRD-059 引入，因此本期由 `admin` 承担财务职责，不提前扩充角色。
- 用户故事：财务需要可追溯的正式成本账本，而不是随当前价格变化的临时统计。
- 范围：每次 Usage 入账；支持人工调整、冲正、赠送额度和试用额度；账本不可直接删除，只能追加新记录。
- API：`GET /finops/ledger`、`POST /finops/adjustments`。
- 页面：`PAGE-074 账本页`、`PAGE-075 用量详情`。
- 数据对象：`FinOpsLedgerEntry`、`AdjustmentRecord`。
- Excel 事件：`ledger.entry_created`；架构文档旧事件表写作 `billing.ledger_created`。
- 验收：每笔 Usage 成本可追溯到来源 `request_id` 和价格快照；调整有原因和操作者审计。
- 测试：`TC-0101` 主流程；`TC-0102` 金额精度、不可变性、权限、异常和安全。
- 非目标：发票开具。

`05_API接口清单` 把两个完整接口按空格错误拆成六行。实现以 `01_PRD总表` 的完整路径为准，不创建 `/GET`、`/POST` 等错误路由。

### 1.2 现有代码与可复用能力

- Gateway `usage/ledger.py` 已在一次事务内写入 `usage_ledger`、`price_snapshots`、成功 Routing Attempt 关联、Request Log 成本摘要和预算结算。
- `usage_ledger` 已记录 tenant、request、Token、CNY/USD 成本、raw/normalized usage、价格版本与快照、模型、Provider、Virtual Key、Environment、Project、Application 和 Attempt。
- 0032/0034 已用数据库触发器禁止 `usage_ledger` UPDATE/DELETE；它是不可变的来源用量事实。
- Control API 已有 `/usage/summary`、`/usage/records` 和 CSV 导出，可复用筛选、租户限定、分页、金额展示和脱敏逻辑，但它们不是正式会计分录。
- `audit_logs` 支持“业务变更 + 审计同事务，Webhook 提交后投递”；调整操作必须复用该边界。
- Webhook 具有事件白名单、敏感字段拒绝、稳定事件 ID 和幂等投递能力。
- Console 已有 Usage 页面、API 错误映射、请求链路展示和 admin-only 导出交互，可复用视觉与状态模式。

### 1.3 本 PRD 要解决的缺口

现有 Usage 表只表达非负用量成本，不能表达人工借记、贷记、部分/全部冲正、赠送和试用额度，也没有 AdjustmentRecord、正式账本 API、财务操作审计或 PAGE-074/075。因此不能把现有 Usage 概览直接改名为正式账本。

## 2. 需求理解与边界冻结

### 2.1 核心语义

1. `usage_ledger` 继续作为不可变来源证据，不建立第二份可被独立改写的 Token/成本计算逻辑。
2. `finops_ledger_entries` 是正式、只追加的账务投影：每条 Usage 恰好对应一条 `usage_charge`；每个 AdjustmentRecord 恰好对应一条非 Usage 分录。
3. Usage 的唯一金额权威源是 `normalized_usage.cost_amount + currency`；内部以原币整数微元保存。CNY 归一值是独立可空字段，USD 在没有受信汇率快照时必须为 NULL，不能用零冒充已折算或免费。
4. Adjustment 本期只接受 CNY。API 金额使用最多六位小数的普通十进制字符串，Python 使用 `Decimal`；禁止 binary float 参与解析、比较、累计或序列化。历史 SQLite NUMERIC 仅按 4.3 的一次性规则兼容。
5. `usage_charge` 为 debit；`manual_debit` 为 debit；`manual_credit`、`gift_credit`、`trial_credit` 为 credit；`reversal` 的方向和原币必须与目标分录相反/相同。
6. 历史记录不能 UPDATE、DELETE 或 REPLACE。纠错、额度回收和冲正都创建新 AdjustmentRecord 和新账本分录。
7. 冲正可为部分或全部冲正，同一目标累计同币种冲正金额不得超过目标原始金额；不能冲正另一条 reversal，避免形成难以解释的反向链。
8. Usage 的账务来源链固定为 `finops entry -> usage_ledger -> request_logs + price_snapshot`。历史账单只读入账时快照，绝不按当前价格重算。
9. 管理 API 的 `operation_request_id/operation_trace_id` 与模型调用的 `source_request_id/source_trace_id` 分开。重试调整请求不能冒充模型调用来源。
10. PRD-050 的 `estimated_hit` 不影响金额，`cache_tokens` 只是上游报告的缓存相关 Token，不能被本 PRD 当作折扣或真实缓存命中重算成本。
11. 每次 Provider Usage 必须进入主库或本地持久 spool。数据库写失败不能只返回 None/记 warning；只有主库提交或 spool 原子 fsync 成功后，非流式响应才结束。稳定 event_id 仍不等于持久 Webhook Outbox，外部投递沿用现有 best-effort 语义。

### 2.2 零价和历史无快照 Usage

“每次 Usage 入账”与“每笔成本可追溯到价格快照”必须同时成立：

- 新 Usage 若匹配有效价格，引用现有不可变 PriceSnapshot；正式分录的版本和原币必须与实际引用快照一致。Usage 与快照的非空版本、tenant、request 或 currency 冲突时整笔事务失败。
- 新 Usage 若没有有效价格，由 Gateway 创建 `source=finops_unpriced`、单位价为零、版本为 `unpriced-v1` 的不可变快照，再以零金额入账；它明确表示“当时没有可用价格”，不能伪装成免费计划。
- 0041 同时覆盖 A. 无 snapshot；B. snapshot 存在但版本为空。两类都新建该 Usage 独享的 ledger-only `source=finops_legacy_backfill` 快照，版本为 `legacy-unversioned-v1`；B 复制可信原币/单价，损坏字段只作明确 legacy 标记，正式金额始终来自不可变 Usage，绝不由零单价重算。
- ledger-only snapshot 使用随机安全 ID 和唯一约束，不依赖 Usage ID 格式。已有可信 snapshot 直接引用；不修改任何历史 Usage 或旧 snapshot。
- spool replay 按原请求的 `pricing_observed_at` 和持久化价格证据重建同一快照；不能使用重放时的当前价格。

### 2.3 明确不做

- 不做发票、税率、抬头、开票状态或电子票据。
- 不做自动退款、支付渠道、应收结算、账单争议或 PRD-080 的争议工作流。
- 不做 PRD-052 的租户/项目/Key 三级预算、PRD-053 的异常检测或 PRD-054 的 Explain Center；只保留下游可消费字段。
- 不引入 FinanceAdmin 或跨租户超级管理员；角色目录仍为 admin/developer/viewer。
- 不新增汇率表或复杂 FX；正式账本保留原币金额。无受信汇率的 USD 行把 normalized CNY 保持 NULL，并在 API/页面/事件明确标记“未折算”。
- 不保存或展示 Prompt、Response、Provider Key、Virtual Key 明文、Token、认证头、Affinity digest、Binding/Scope 或 Provider Credential ID。

## 3. 涉及模块

| 层 | 模块 | 修改目的 |
|---|---|---|
| 数据库 | `infra/migrations/0041_finops_ledger.sql` | 创建正式账本、调整记录、索引、租户/金额/币种/不可变触发器，回填历史 Usage并保真重建 Audit |
| 数据库 | `infra/migrations/0041_finops_ledger.down.sql` | 仅在没有不可逆调整/订阅/投递事实时条件回滚 |
| Shared SDK | `metahub_shared/runtime/webhooks.py` | 注册并严格校验 `ledger.entry_created` 安全事件合同 |
| Gateway | `metahub_gateway/usage/ledger.py` | 新 Usage 缺价时补零价快照；事务提交后返回正式账本事件 |
| Gateway | `metahub_gateway/api/openai_compatible.py` | 消费主库/持久 spool 结果，提交后仅入队一次账本事件 |
| Gateway | 新增 `metahub_gateway/usage/spool.py` | 数据库失败时原子持久化安全 Usage，并在启动/周期任务中幂等重放 |
| Control API | 新增 `metahub_control/finops` | DTO、查询筛选、调整校验、幂等、冲正、审计和事件投递 |
| Control API | `auth/dependencies.py`、`app.py` | 注册 admin-only 后端权限依赖和 `/finops` Router |
| Shared Runtime | `runtime/errors.py` | 注册稳定 FinOps 错误码及安全建议 |
| Audit | `audit/service.py` | 注册 `finops.adjustment_created` 与脱敏摘要白名单 |
| Console API | 新增 `console-web/src/lib/finops.js` | tenant-free 查询、调整 POST、统一错误与链路 ID |
| Console UI | 新增 `features/finops-ledger` | PAGE-074 列表、PAGE-075 详情、调整对话框与状态 |
| Console Shell | `app/main.js`、导航和样式 | admin-only 入口、状态加载、选择、分页和提交联动 |
| 测试/文档 | Python、Node、README、docs/testing | 主流程、异常、权限、迁移、运行与浏览器证据 |

## 4. 数据表和字段设计

### 4.1 `finops_adjustments`

该表保存财务操作的原始、不可变业务事实；它不保存可变处理状态。

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `finops_adjustment_id` | TEXT PK，`fadj_` + 32 位小写 hex | 调整对象 ID |
| `tenant_id` | TEXT NOT NULL，FK tenant | 强制租户所有权 |
| `adjustment_type` | enum | `manual_debit/manual_credit/reversal/gift_credit/trial_credit` |
| `amount_micros` | INTEGER NOT NULL，`1..10^15` | 正数金额幅度；方向由类型决定 |
| `currency` | `CNY/USD` | 非 reversal 强制 `CNY`；reversal 必须从同租户目标分录继承原币，不接受客户端自报 |
| `reversal_of_entry_id` | TEXT nullable | reversal 必填且只能引用同租户非 reversal 分录；其他类型必须为空 |
| `related_request_id` | TEXT nullable | 可选关联同租户模型请求；reversal 从目标复制，不能由客户端覆盖 |
| `reason` | TEXT，UTF-8 1..500 字节 | 财务可见原因；去首尾空白、拒绝控制字符，不写日志/Webhook |
| `reason_sha256` | 64 位小写 hex | 审计和完整性核对使用，不代替 reason |
| `actor_user_id` | TEXT NOT NULL | 从认证上下文取得，客户端不能传 |
| `operation_request_id` | TEXT NOT NULL | 当前管理请求链路 ID |
| `operation_trace_id` | TEXT NOT NULL | 当前管理请求 Trace |
| `idempotency_key_sha256` | 64 位小写 hex | 不保存客户端幂等键原文 |
| `payload_sha256` | 64 位小写 hex | 同键不同载荷时返回冲突 |
| `status` | 固定 `posted` | 不提供状态修改入口 |
| `created_at/updated_at` | TEXT，必须相等 | 只追加对象没有更新时间 |

唯一约束：`UNIQUE(tenant_id, idempotency_key_sha256)`。所有外键或 owner trigger 同时比较 `tenant_id`，禁止引用其他租户的用户、请求和目标分录。

### 4.2 `finops_ledger_entries`

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `ledger_sequence` | INTEGER PK AUTOINCREMENT | 稳定分页/水位，不作为公开对象 ID |
| `finops_ledger_entry_id` | TEXT UNIQUE，`fle_` + 32 位小写 hex | SQLite `lower(hex(randomblob(16)))` 生成，不依赖来源 ID |
| `event_id` | TEXT UNIQUE，`flevt_` + 32 位小写 hex | 与分录一同生成并冻结，不等同 Webhook Outbox |
| `tenant_id` | TEXT NOT NULL | 所有查询首条件 |
| `entry_type` | enum | `usage_charge` 或五种 adjustment type |
| `direction` | `debit/credit` | 与 entry_type/目标分录方向受 CHECK/trigger 约束 |
| `amount_micros` | INTEGER，`0..10^15` | 原币金额；Usage 可为零，Adjustment 必须大于零 |
| `currency` | `CNY/USD` | Usage 取来源原币；非 reversal Adjustment 固定 CNY；reversal 与 AdjustmentRecord 一同继承目标原币 |
| `normalized_cny_micros` | INTEGER nullable | 仅有受信 CNY 值时填写；无 FX 的 USD 必须为 NULL |
| `cny_normalization_status` | `normalized/unavailable` | 禁止把未折算展示为零 CNY |
| `posting_source` | `direct/spool_replay/legacy_backfill` | 区分正常入账、持久重放和迁移来源 |
| `quota_type` | `paid/free/trial/gift` | Usage/人工调整为 paid；赠送/试用按类型；reversal 继承目标 |
| `usage_ledger_id` | nullable | usage_charge 必填，其他必须为空 |
| `finops_adjustment_id` | nullable | 非 Usage 必填，usage_charge 必须为空 |
| `source_request_id` | nullable | Usage 必填；关联调整可选；gift/trial 可为空 |
| `source_trace_id` | nullable | 从 Request Log 或目标分录冻结，不由前端伪造 |
| `operation_request_id` | TEXT NOT NULL | Usage 使用来源请求 ID；Adjustment 使用管理请求 ID |
| `operation_trace_id` | TEXT NOT NULL | 用于操作链路排障 |
| `price_snapshot_id` | nullable | usage_charge 必填；reversal 若目标有快照则复制；其他按关联来源复制或为空 |
| `price_schema_version` | nullable | 与 price snapshot 同时冻结 |
| `price_snapshot_source` | nullable | 冻结入账快照来源；不按当前价格表反查，人工调整可为空 |
| `price_evidence_status` | enum | `priced/unpriced/legacy_amount_only/not_applicable`；reversal 继承目标，禁止把历史金额证据伪装成可重算单价 |
| `reversal_of_entry_id` | nullable | reversal 必填；其他必须为空 |
| `actor_user_id` | nullable | Adjustment 必填；Usage 为空 |
| `input/output/total/cache_tokens` | INTEGER >= 0 | Usage 从来源复制；Adjustment 固定 0 |
| `model/provider/virtual_key/environment/project/application_id` | nullable | Usage 或关联目标的安全归属快照；不含明文密钥 |
| `estimated` | 0/1 | Usage 来源复制；Adjustment 为 0 |
| `status` | 固定 `posted` | 不允许撤回或删除 |
| `created_at/updated_at` | TEXT，必须相等 | 只追加时间事实 |

唯一性和引用规则：

- `UNIQUE(tenant_id, usage_ledger_id)` 保证一次 Usage 只入账一次。
- `UNIQUE(tenant_id, finops_adjustment_id)` 保证一次 Adjustment 只形成一条分录。
- `(tenant_id, finops_ledger_entry_id)` 可被同租户 reversal 引用。
- Usage 的 tenant/request/snapshot/维度由数据库 owner trigger 核验；Adjustment 的 actor、目标分录和 related request 也必须同租户。
- UPDATE/DELETE trigger 固定抛出 `finops_ledger_append_only`；Adjustment 表固定抛出 `finops_adjustment_append_only`。
- 禁止 `INSERT OR REPLACE` 绕过不可变性：REPLACE 会先触发 DELETE 并被拒绝；冲突只允许显式读取幂等结果。

### 4.3 Usage 金额转换、自动投影与持久恢复

唯一金额源为 `normalized_usage.cost_amount + currency`：

1. 新写入值必须是 ASCII 普通十进制 string，整数 1..10 位、小数 0..6 位；右补零到六位后分别把整数段/小数段转 INTEGER，再做整数运算。禁止 `CAST(cost*1000000 AS INTEGER)`。
2. CNY 行要求 `normalized_usage.cost_cny` 与 cost_amount 的六位规范值一致，写 normalized CNY；USD 行在没有受信 FX 时 normalized CNY 为 NULL。
3. 历史行优先使用合法 normalized string；只有缺字段时才用 `printf('%.6f', usage_ledger.cost_amount)` 形成一次性 legacy canonical text。迁移必须披露该 fallback，逐行把 micros 反格式化回六位文本并比较；非有限、负数、超界、币种/快照不一致在任何 DDL 前 abort。
4. `AFTER INSERT ON usage_ledger` trigger 补齐/核验快照，使用上述文本算法创建随机 `fle_/flevt_` 分录。任一步失败回滚 Usage、Request Log、Attempt 与预算结算。
5. `UNIQUE(tenant_id, usage_ledger_id)` 是 exactly-once 边界；不依赖 Usage ID 格式，也不使用 `INSERT OR IGNORE` 掩盖冲突。

数据库异常时，`safe_record_usage_ledger` 必须把同一预生成 Usage ID、tenant/request/trace、安全路由维度、normalized Usage、pricing observed time 和价格证据写入本地持久 spool：

- spool 根目录由受校验配置指定在本地 data root；拒绝 symlink/reparse point。单文件上限 16 KiB，不含 raw usage、Prompt、Response、Endpoint、Key、Token 或 reason。
- 文件名使用 tenant + Usage ID 的域隔离 HMAC 摘要；内容是版本化 canonical JSON，并携带以服务端密钥和 `metahub.finops.usage-spool.v1` 域分隔计算的 HMAC-SHA256。读取时必须以常量时间校验文件名、内容 HMAC、严格 schema、UTF-8、重复键和 16 KiB 上限；普通 checksum 只能检测损坏，不能替代真实性校验。目录必须由运行服务账号独占写入，文件创建权限固定为仅服务账号读写；权限或 owner 校验失败时禁用 spool 并安全失败，不调用系统 shell 修补 ACL。
- 写入先在同目录以 exclusive-create 创建临时文件，flush + fsync 后原子 rename 到 `pending`；启动及有界周期 worker 通过 `pending -> processing/<digest>.<worker_token>.claim` 的单次原子 rename 领取，只有领取者可以读取、重放或清理。崩溃遗留 claim 达到冻结租期后才可由一次原子 rename 重新入队；目标冲突必须先验证同一 HMAC 载荷，否则双边隔离，禁止覆盖。
- worker 以 tenant + Usage ID 幂等重放，HMAC 通过后仍须在同一数据库事务核验 tenant/request/Usage 所有权，必要时恢复同租户 Request Log，再按原 `pricing_observed_at` 入账。事务返回 `created/existing`：仅 `created` 在 commit 后入队一次稳定 event_id，`existing` 只清理 claim、不重复入队；commit 后崩溃留下的 claim 再处理时必然走 `existing`。只有 commit 或已存在分录确认成功后才删除 claim，失败则按有界退避原子归还 pending；无法通过真实性、schema、owner 或路径校验的文件原子移入 `quarantine`，绝不重放。
- 主库与 spool 同时失败时，非流式返回统一 `finops.usage_record_unavailable`；Stream 已发正文时记录 critical 结构化错误和指标。测试门禁必须证明“数据库失败但 spool 可写”最终仍恰好一条正式分录。

迁移/运行断言：Usage 与 usage_charge 一一对应；每条都有 source request + snapshot + version；原币金额可反算；无重复/跨租户引用。列表 totals 在同一只读事务中流式读取整数 micros，以 Python 任意精度 int 累计，禁止 SQLite TOTAL/float 或可能溢出的无界 SUM。

### 4.4 冲正余额计算

创建 reversal 前，在 `BEGIN IMMEDIATE` 事务中读取同租户目标分录并计算：

```text
remaining_micros = target.amount_micros
                   - SUM(existing reversal.amount_micros WHERE reversal_of=target)
```

请求金额必须 `0 < amount_micros <= remaining_micros`。事务写锁使两个并发冲正不能同时基于相同旧余额提交。服务端不接受客户端传 `direction/quota_type/snapshot/actor/tenant`，这些字段全部从目标或认证上下文推导。

## 5. API 设计

### 5.1 `GET /finops/ledger`

权限：仅 `admin`。匿名返回统一认证错误；developer/viewer 返回 `auth.permission_denied`。依赖在解析业务筛选前执行，避免无权限用户用校验差异探测账本。

允许查询参数：

| 参数 | 约束 |
|---|---|
| `start_date/end_date` | ISO 日期，闭区间，最长 366 天 |
| `entry_type` | 账本类型枚举 |
| `direction` | debit/credit |
| `quota_type` | paid/free/trial/gift |
| `request_id` | 精确匹配 `source_request_id` |
| `price_snapshot_id` | 精确匹配 |
| `related_entry_id` | 匹配自身 ID 或 reversal target |
| `limit` | 默认 20，1..100 |
| `cursor` | 可选 opaque HMAC 游标；首次为空，后续必须与当前 tenant 和规范筛选一致 |

未知参数一律 `finops.query_invalid`，特别禁止 `tenant_id`、`actor_user_id`、原始 SQL 排序和任意字段名。SQL 必须从 `context.user.tenant_id` 开始构造；页面永不传 tenant_id。

成功响应：

```json
{
  "ledger_entries": [],
  "totals": {
    "by_currency": {"CNY": {"debit": "0.000000", "credit": "0.000000", "net": "0.000000"}},
    "net_cost_cny": "0.000000",
    "unnormalized_entry_count": 0,
    "entry_count": 0
  },
  "total": 0,
  "limit": 20,
  "next_cursor": null,
  "has_more": false,
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

单条 `FinOpsLedgerEntry` 返回上述公开 ID、类型、方向、六位金额字符串、quota、来源/操作 request_id、价格快照/版本、`price_snapshot_source`、`price_evidence_status`、reversal 关系、安全维度、Token、estimated、操作者最小身份（ID 与当前租户内脱敏显示名/邮箱）、reason（仅 Adjustment 且仅 admin）、创建时间。`priced` 仅表示有可追溯定价快照，`unpriced` 表示当时无价格且金额为零，`legacy_amount_only` 表示只能证明历史金额而不能按零单价重算，`not_applicable` 用于无定价来源的人工调整；绝不返回 raw_usage、Prompt/Response、密钥字段、reason hash、幂等 hash、payload hash 或 Provider Endpoint URL。

首次请求在同一只读事务冻结当前租户最大 sequence；totals/count/page 共用该快照。后续 opaque cursor 含版本、snapshot、before sequence、规范筛选 hash 和当前 tenant 的 HMAC 摘要，并用 auth secret 的 FinOps 域分隔 HMAC 签名；服务端验证签名、tenant、筛选和范围，换筛选复用旧 cursor 必须 422。排序固定 sequence DESC，前端维护 cursor 栈。金额由服务层流式累计 Python int 后格式化。

### 5.2 `POST /finops/adjustments`

权限：仅 `admin`。Header `Idempotency-Key` 必填，8..128 个可见 ASCII 字符；服务端仅保存 SHA-256。

请求体：

```json
{
  "adjustment_type": "manual_credit",
  "amount": "12.345678",
  "reversal_of_entry_id": null,
  "related_request_id": "req_optional",
  "reason": "合同约定的月度服务补偿"
}
```

字段规则：

- `amount` 必须是 JSON string，正数，最多 6 位小数，最大 `1000000000.000000`；拒绝 JSON number、指数、正负号、逗号、NaN/Infinity 和多余小数位。非 reversal 固定 CNY；reversal 的 currency 从目标复制，body 禁止自报 currency。
- reversal 必须提供 `reversal_of_entry_id`，禁止传 `related_request_id`，来源链从目标分录复制。
- 非 reversal 禁止传 `reversal_of_entry_id`；`related_request_id` 可选，但存在时必须对应当前租户已入账 Usage。
- `reason` 必填，规范化后 1..500 UTF-8 字节，拒绝 C0/C1 控制字符；服务端日志和 Webhook 不记录正文。
- tenant、actor、direction、quota、snapshot、schema version、operation request/trace 均不接受客户端输入。

首次成功返回 HTTP 201；相同 tenant + Idempotency-Key + 完全相同规范化载荷返回同一 Adjustment/Entry，`idempotent_replay=true`，不重复审计或投递；同键不同载荷返回 409 `finops.idempotency_conflict`。

响应：

```json
{
  "adjustment": {},
  "ledger_entry": {},
  "idempotent_replay": false,
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

业务写入、分录写入和 `finops.adjustment_created` 审计在同一事务提交。`ledger.entry_created` 与 `audit.recorded` 只在首次 commit 成功后尽力投递；Webhook 故障不能回滚已确认的财务事实，固定记录安全告警和稳定 event_id 供投递幂等。

### 5.3 不提供的接口

- 不提供 PUT/PATCH/DELETE FinOps 路由。
- 对 `/finops/ledger/{id}` 的页面详情不新增未列入 Excel 的服务端接口；PAGE-075 使用列表返回的安全详情对象，并可通过 `related_entry_id` 精确刷新。
- FastAPI 对未注册方法使用统一 `infra.method_not_allowed`，响应继续携带 request_id/trace_id。

## 6. 前端页面和交互设计

### 6.1 PAGE-074 账本页

- Console 导航新增“FinOps 账本”，仅 admin 可见；后端 admin-only 仍是权威边界。
- 首屏是工作型账本表，不做营销 Hero 或多层嵌套卡片。
- 顶部显示 debit、credit、net cost 和条目数；金额完全使用 API 字符串，不在浏览器用 Number 汇总。
- 提供日期、类型、方向、quota、request_id 和 price snapshot 筛选；分页布局尺寸稳定。
- 列表列：时间、分录类型、方向、金额、quota、来源 request_id、价格快照、操作者、状态。
- admin 可通过明确的“新增调整”命令打开模态框；类型使用下拉菜单，金额用 decimal 文本输入，原因用多行输入，reversal 使用当前选中分录自动填充目标。
- 提交期间禁用重复操作；失败只显示统一 code/message/request_id，不输出请求 body 或异常对象。

### 6.2 PAGE-075 用量详情

- 点击账本行在同一工作区打开详情侧栏/详情区，移动端切为全宽详情视图；关闭后保留列表筛选和分页。
- Usage 详情显示 request_id、trace_id、Usage ID、价格快照、价格版本、快照来源、价格证据状态、Token、模型/Provider/Project/Application/Environment、estimated 和历史金额；`legacy_amount_only` 必须明确标注“仅历史金额证据，不可按单价重算”。
- Adjustment 详情显示类型、方向、金额、原因、操作者、operation request/trace、关联请求和冲正链。
- request_id 可跳转现有 Request Logs；price_snapshot_id 可关联现有价格证据展示，但不得按当前价格重算。
- DOM 不渲染 raw_usage、Prompt、Response、密钥、hash、Endpoint URL 或隐藏数据属性中的敏感值。

### 6.3 状态和响应式要求

- 覆盖 loading、empty、error、success、submitting、idempotent replay 和 stale selection 状态。
- 桌面 1280x720 不出现工具栏/表头遮挡；移动 390x844 采用标签化行布局，最长 ID 自动换行，不横向撑破视口。
- 键盘焦点进入模态框、Esc 关闭、关闭后回到触发按钮；错误区域使用 `role=alert`，异步成功使用 `aria-live`。

## 7. 事件设计

### 7.1 canonical 名称与兼容口径

- canonical 事件固定为 Excel 的 `ledger.entry_created`。
- 架构旧名 `billing.ledger_created` 仅作为配置输入兼容别名：Webhook Endpoint 创建/更新时归一为 canonical 存储；事件表、投递记录、签名正文、筛选返回和文档只输出 `ledger.entry_created`。
- 禁止双发，否则同一账务事实会造成下游重复记账。旧别名不会成为第二个 event_id 或 delivery。

### 7.2 安全 payload

固定字段：

```json
{
  "ledger_entry_id": "fle_...",
  "entry_type": "usage_charge",
  "direction": "debit",
  "amount": "0.123456",
  "currency": "CNY",
  "normalized_cny": "0.123456",
  "cny_normalization_status": "normalized",
  "quota_type": "paid",
  "source_request_id": "req_...",
  "price_snapshot_id": "prs_...",
  "price_schema_version": "...",
  "price_snapshot_source": "model_price",
  "price_evidence_status": "priced",
  "reversal_of_entry_id": null
}
```

Envelope 的 event_id 使用账本行内持久 `flevt_...`，object_id 为 ledger entry ID，actor_id 仅 Adjustment 有值，trace_id 使用来源或操作 Trace。payload 精确字段校验，禁止额外 reason、Prompt、Response、raw_usage、Credential、Key 或 Endpoint 字段。

## 8. 错误码设计

| 错误码 | HTTP | 场景 |
|---|---:|---|
| `finops.query_invalid` | 422 | 未知筛选、枚举、范围、limit/cursor 非法 |
| `finops.adjustment_invalid` | 422 | 类型/字段组合/reason/幂等 Header 非法 |
| `finops.amount_invalid` | 422 | 金额不是规范字符串、越界、非正或精度超过六位 |
| `finops.entry_not_found` | 404 | 当前租户找不到目标分录；跨租户同样返回 404 |
| `finops.request_not_found` | 404 | 当前租户找不到 related request/Usage；跨租户同样返回 404 |
| `finops.reversal_not_allowed` | 409 | 目标是 reversal、零金额或其他不可冲正类型 |
| `finops.reversal_exceeds_remaining` | 409 | 累计冲正超过剩余额度 |
| `finops.idempotency_conflict` | 409 | 相同幂等键用于不同规范载荷 |
| `finops.ledger_unavailable` | 503 | 查询或写入数据库暂不可用，可重试 |
| `finops.usage_record_unavailable` | 503 | 主库与持久 spool 均无法确认 Usage，非流式请求结果不予确认 |
| `finops.adjustment_failed` | 500 | 非可重试一致性/审计写入失败的安全收敛错误 |

认证与权限继续复用 `auth.*`，未知路径/方法和 Pydantic 外层错误继续复用 `infra.*`。所有错误都由统一中间件补齐 `request_id`、`trace_id`、suggestion 和 diagnosis；底层 SQL、路径、payload、reason、Key、Token 与异常正文不得外发。

## 9. 安全与权限约束

1. GET/POST 均使用独立 `require_finops_admin` 后端依赖，当前仅 admin；不能复用允许 developer 的 `require_usage_reader`。
2. 所有 SQL 显式包含认证上下文 tenant_id；API 不接受 tenant_id，跨租户 ID 返回当前租户视角的 404。
3. 调整 actor 只能来自已认证 Session；不能从 body/header 自报用户 ID。
4. 数据库 owner triggers 核验 Usage、Request、PriceSnapshot、User、Adjustment 和 reversal target 的 tenant 一致性，防止遗漏 WHERE 后形成横向引用。
5. 正式账本、调整、价格快照和审计均为 append-only；没有软删除、管理员后门或批量清理 API。
6. 事务边界：Adjustment + Entry + Audit 同事务；Usage + PriceSnapshot + Entry + 既有 Usage 事务同生共死。
7. Webhook 和结构化日志只记录固定安全摘要，不记录 reason 正文、请求 body、幂等键原文、Prompt/Response 或任何密钥。
8. Console 所有服务端内容先转义；ID 只进入固定同源 URL 或 DOM 文本，不拼接任意 HTML。
9. 报表查询只在控制面运行，不进入 Gateway 热路径；Gateway 正常路径做 O(1) append，失败路径同步原子写小型 spool，后台有界重放；Webhook 入队仍不反压模型调用。
10. 数据与测试制品保留在中国境内本地路径；本 PRD 不增加外部 SaaS 依赖。

## 10. 数据库迁移与回滚

### 10.1 Up 0041

1. 在任一 DDL 前断言 0040 applied、关键 Usage/Price/Request/Audit/Webhook 表与不可变 trigger 存在，并完成历史金额、版本、币种和所有权 preflight。
2. 保真重建 `audit_logs`，只追加 `finops.adjustment_created/finops_adjustment`。全列含 rowid/NULL 双向 EXCEPT 复制，逐字恢复 0040 全部索引、owner/immutable/conflict/rowid triggers；扫描 sqlite_schema/foreign_key_list，未知外键、视图或其他 trigger 引用 audit_logs 时在切表前 abort。
3. 创建 Adjustment 与 Ledger 表、索引、owner/金额/币种/语义/append-only triggers。
4. 为历史无快照或快照无版本 Usage 创建随机 ID 的 ledger-only legacy snapshot；可信快照必须 tenant/request/version/currency 一致。
5. 按 `created_at, usage_ledger_id` 回填 usage_charge，不修改 Usage、Request 或旧 PriceSnapshot，并逐行反算原币 micros。
6. 创建未来 Usage 自动投影 trigger。
7. 执行计数、缺链、重复、跨租户、对象集合、`foreign_key_check` 和 `integrity_check` 门禁后提交。

### 10.2 Down 0041

下列任一事实存在则在任何 DDL 前原子拒绝：

- 非 Usage Adjustment/ledger 事实；
- `ledger.entry_created` Endpoint 订阅；
- 对应 Webhook delivery/attempt；
- `finops.adjustment_created` Audit。

没有上述不可逆事实且持久 spool 为空时，可删除自动投影 trigger、由 Usage 可重建的账本行/表和仅供 legacy backfill 的未被其他对象引用快照，并按同样的外部依赖扫描与双向保真门禁把 Audit 恢复到 0040 枚举。回滚后验证 0040 对象、数据计数、索引、trigger、外键和 integrity。生产回滚不静默删除人工财务事实。

## 11. 测试用例

### 11.1 迁移与数据库合同

- 0040 -> 0041 Up：历史有价/无价、CNY/USD、零金额、不同租户 Usage 均恰好回填一条，正式分录都有 request + snapshot + version + source + evidence status；非零 legacy 金额必须为 `legacy_amount_only`，不得被零单价重算。
- 新 Usage INSERT 自动同事务生成零/非零正式分录；故意破坏 owner 或 Ledger insert 时整个 Usage 事务回滚。
- UPDATE/DELETE/REPLACE Adjustment/Ledger 均被 trigger 拒绝；非法类型、方向、金额、owner、source 组合拒绝。
- 同一 Usage/Adjustment 重复入账拒绝；跨租户 request/snapshot/target/actor 引用拒绝。
- Down 空事实成功并逐项还原；存在 Adjustment、Audit、订阅或投递时原子拒绝且 0041 保持 applied。
- `PRAGMA foreign_key_check`、`integrity_check`、迁移重复执行/未知基线保护通过。

### 11.2 Gateway 主流程与异常

- 非流式、Fallback 成功和 Stream completed/合法 estimated 终态各产生一条正式 Usage 分录。
- 有效价格引用原快照；缺价格创建 `unpriced-v1` 零价快照；PRD-050 estimated_hit/cache_tokens 不改变金额。
- PriceSnapshot、Usage、Formal Entry、Attempt 或预算结算任一步失败全部回滚；随后把安全 normalized Usage 原子写 spool，重放后仍恰好一条。主库/spool 双失败的非流式请求返回统一 503；Stream 记录 critical 指标。
- spool 文件权限、reparse 拒绝、16 KiB 上限、域隔离 HMAC、防重复键、fsync/rename、原子 claim、崩溃恢复、非法文件隔离、所有权复核、重放幂等、仅 created 入队、成功后清理与原价格时点测试通过。
- 事件只在主库 commit 后入队，队列缺失/满载不反压请求；event_id 稳定但明确不是持久投递保证，payload 覆盖原币和可空 CNY 且无敏感字段。

### 11.3 Control API 主流程 TC-0101

- admin 以签名 snapshot cursor 分页读取当前租户账本；并发追加不重复/漏掉快照内行，换 tenant/筛选复用 cursor 被拒绝，原币汇总与可空 CNY 一致。
- 创建 manual debit、manual credit、gift、trial；金额 `0.000001` 和六位上限精确保存/返回。
- 对 Usage/credit 做部分与全部 reversal，方向相反、quota/snapshot/source 链正确，累计余额准确。
- 调整 + Entry + Audit 同事务；reason 可由 admin 查看，Audit 只有 hash/安全字段。
- 相同幂等键/载荷返回同一对象，不重复行、Audit 或 Webhook；不同载荷稳定 409。
- `ledger.entry_created` 仅发 canonical 名称；旧订阅别名归一后只收到一次 canonical delivery。

### 11.4 异常、权限和安全 TC-0102

- 匿名、developer、viewer 调 GET/POST 分别返回统一 401/403，并携带 request_id/trace_id。
- body/query 夹带 tenant_id/actor/direction/snapshot、未知字段、跨租户 request/entry 均拒绝且不泄露资源存在性。
- 金额 JSON number、负数、零、7 位小数、指数、NaN、超上限、Unicode 数字、空白均拒绝。
- reversal 缺目标、目标不存在、目标为 reversal、超过剩余、并发超额均拒绝且不产生半账。
- reason 空值、超长、控制字符拒绝；模拟日志/Webhook/错误响应确认不出现 reason、Prompt、Response、Provider Key、Token。
- 数据库锁/关闭/审计失败映射统一错误；幂等查询故障不能假定首次成功。

### 11.5 Console 与浏览器

- API client 永不发送 tenant_id，金额保持 string，POST 携带独立 request/trace 和 Idempotency-Key。
- admin 看到导航/列表/调整；developer/viewer 不看到入口且直接 API 仍 403。
- loading/empty/error/success/submitting/replay、筛选、分页、详情、冲正联动和焦点恢复单测通过。
- DOM 注入用例验证 reason/ID 被转义，敏感字段不进入 DOM/属性。
- Playwright 在 1280x720、390x844 验收 PAGE-074/075：无重叠、无横向溢出、金额和长 ID 可读、模态框可键盘操作。

### 11.6 分层执行策略

为避免局部问题触发无关的重复全量测试，执行顺序固定为：

1. 修改过程中只运行 PRD-051 新增单测及直接受影响的 Usage/Audit/Webhook/Console 测试。
2. 一个层的合同稳定后运行该层相关套件，不因样式或文档小改重复运行 Python 全量。
3. 最终候选冻结后，在 `TEMP=TMP=D:\MetaHub\.tmp`、PRD-051 专用新数据库上只执行一次全量 Python/Node/Console/迁移回归。
4. 回归通过后启动本地三服务做一次真实 API 和浏览器验收；只复测验收中实际修正的链路，最后补一次受影响门禁。
5. 全程留在 `D:\MetaHub` 沙箱，不使用 `C:\tmp`，不提权，不做系统级 CIM/端口 owner 扫描。

## 12. 代码修改计划

1. 完成本设计的两轮独立自审，先修正文档中的边界、权限、租户、金额、迁移和兼容问题。
2. 实现 0041 Up/Down 与迁移测试，先把不可变性、所有权、历史回填和自动投影锁在数据库层。
3. 实现 Shared Webhook 合同、Gateway 缺价快照和提交后事件链，运行 Gateway/事件相关测试。
4. 实现 Control FinOps DTO、后端 admin 权限、查询、调整、冲正、幂等、审计和统一错误，运行定向 API 测试。
5. 实现 Console API client、PAGE-074/075、导航、样式与交互测试。
6. 更新 README、目录说明、错误码/API/事件文档和 `docs/testing/PRD-051-tests.md`。
7. 运行中文注释门禁、相关静态检查、最终一次全量回归、新数据库迁移、真实服务/API 与桌面/移动浏览器验收。
8. 只有全部证据落盘后才把本文和 README 状态改为“已完成”；否则明确保留未完成项。

## 13. 验收清单

- [x] 两轮编码前自审完成并修订本文。
- [x] 0041 Up/Down、历史回填、不可变性和租户引用门禁通过。
- [x] 每条现有/新增 Usage 恰好一条正式分录，且有 source request + price snapshot/version。
- [x] 五种调整类型、部分/全部冲正、金额精度和幂等通过。
- [x] admin-only 后端 RBAC、跨租户 404、统一错误和 request_id/trace_id 通过。
- [x] Adjustment reason + actor 审计同事务且敏感字段不进日志/Webhook。
- [x] `ledger.entry_created` canonical/旧名归一/单次投递通过。
- [x] PAGE-074/075 主流程、错误、权限和响应式验收通过。
- [x] 中文注释检查、定向测试、最终全量回归、迁移与真实运行验收通过。
- [x] README、目录说明和 `docs/testing` 证据更新完成。

## 14. 编码前自审记录

### 14.1 第一轮自审

- 审查发现：SQLite NUMERIC/micros 转换未定义；无版本 snapshot 未覆盖；0041 漏掉 Audit 枚举保真重建；ID 错误依赖 Usage 格式；offset 分页和事件可靠性表述不成立。
- 修订：以 normalized 十进制文本拆分生成微元，legacy fallback 逐行反算；补 ledger-only versioned snapshot；冻结 Audit Up/Down 全对象保真；随机 ID + source 唯一；签名快照游标；明确 Webhook best-effort。
- 第一轮结论：上述阻断已进入第二轮验证，未提前编码。

### 14.2 第二轮自审

- 审查发现：数据库失败仍可能永久丢 Usage；cost_cny 与原币金额权威源冲突；多币种未贯穿 API/事件；snapshot 未校验 currency；裸 cursor 未绑定筛选；Audit 未扫描外部依赖。
- 修订：增加本地持久 Usage spool 与幂等恢复；唯一金额源改为 cost_amount + currency，normalized CNY 可空；统一 POST/DTO/Event 多币种字段；强制 snapshot currency；使用 tenant/filter 绑定的 HMAC cursor；迁移扫描 audit_logs 的外键/视图/外部 trigger。
- 短复核发现并修订：AdjustmentRecord 原先固定 CNY 与 USD reversal 冲突，现改为非 reversal 固定 CNY、reversal 从目标继承；spool 增加域隔离 HMAC、服务账号独占权限、原子 claim/租期恢复、quarantine、数据库 owner 复核和仅新建分录入队协议。
- 第二轮结论：最终短复核未发现新的 P0/P1，设计通过，允许进入编码。
- 编码边界校对补充：为避免 ledger-only 历史快照被误解为可按零单价重算，统一在数据库、API、事件和页面冻结 `price_snapshot_source + price_evidence_status`；该澄清不改变既有金额、租户、权限或幂等边界。

## 15. 实现与最终验收

### 15.1 实现收口

- 0041、Gateway 持久 spool/正式分录、Control admin-only 查询与调整、Shared 事件合同、PAGE-074/075 及 Runtime/Browser 验收器均已实现。
- 所有新增业务查询显式携带认证上下文 `tenant_id`；跨租户资源保持同形 404，developer/viewer 由后端返回 403。
- 调整、分录与 Audit 同事务；Usage、快照、分录与既有 Usage 事务同生共死。错误继续使用统一错误码并携带 `request_id/trace_id`。
- 日志、Webhook、错误和 Browser 证据只保留安全投影，不包含 Provider Key、Virtual Key、Token、Prompt、Response、调整原因或幂等键原文。

### 15.2 验证事实

- 合并专项：Python `39/39`；Node 示例 `33/33`；Console 全量 `375/375`；lint `125 files`。
- Python 根级全量只执行一次，共 `958` 项，结果为 `890 passed / 68 failed / 0 errors / 0 skipped`。68 项均按原失败节点聚焦闭环：迁移 `46/46`、Control/旧合同 `13/13`、Gateway `10/10`；没有伪写为 `958/958 passed`，也没有因局部修复重复跑全量。
- 开发库已升级到 `0041`，共 41 条迁移；18 条 Usage 恰好对应 18 条 `usage_charge`，Adjustment 为 0，checksum、integrity 和外键检查通过。
- 真实 Runtime 完成账本读取、调整 `201`、幂等重放 `200`、冲突 `409`、冲正 `201`、超额 `409` 和登出 `200`；运行库终态为 18 Usage、20 Ledger、2 Adjustment、2 FinOps Audit。
- 权威 Browser 批次 `artifacts/prd051/20260727T043500-browser-success-02` 覆盖 `1280x720` 与 `390x844`，6 张 PNG 均非空且哈希/尺寸匹配；6 组布局无横向溢出、裁切或重叠，blocked request 和 browser error 均为 0。
- 首次真实页面失败和两次启动前配置失败均保留，未覆盖为成功证据；成功 Browser Session 与 Runtime Session 均已撤销。
- 停服终态审计见 `artifacts/prd051/20260727T043500-browser-success-02/final/final-audit.json`：19 项门禁全部通过，默认/运行 spool 为空，日志敏感项零命中，三个精确 PID/端口已停止，SQLite 侧车不存在。

### 15.3 结论

设计、自审、实现、测试、迁移、文档、真实页面和运行终态均已完成，PRD-051 可运行、可验收，现标记为已完成。该结论不表示 108 条 PRD 已全部完成；按 Excel 顺序核对后的下一条未完成项是 `PRD-052 租户/项目/Key三级预算`（M1-企业POC，P1）。
