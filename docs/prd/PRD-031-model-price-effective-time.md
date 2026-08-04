# PRD-031 - 价格表与生效时间

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：实现与聚焦验收完成，全量回归结果见测试记录
- 依赖：PRD-012 模型目录、PRD-017 Usage 与成本估算、PRD-030 Environment 隔离
- 明确边界：不自动抓取厂商价格，不做汇率换算，不重算已入账 Usage

## 实现结论

- 已新增不可变 `model_price_plans`、`model_price_events` 和价格快照计划关联字段。
- Gateway 已按服务器 UTC 时间选择当前 PricePlan，并为每条 Usage 复制实际价格快照；存量模型保留兼容回退。
- 控制台已新增模型价格导航、时间线、状态筛选和 admin 新增价格交互，developer 只读、viewer 无入口。
- `price.updated` 已进入安全 Webhook 白名单，主事务提交后 best-effort 分发。
- 聚焦后端 14 个、控制面全量 72 个、数据面全量 55 个、前端 108 个测试全部通过；中文注释门禁通过。

## 需求理解

当前模型目录保存一组“当前价格”，PRD-017 在每次调用入账时复制出 `price_snapshots`，已经保证历史账本可解释，但无法预约未来价格，也无法按请求时间选择当时生效的价格。PRD-031 要补齐有效期价格计划，并继续让每条 Usage 引用不可变价格快照。

核心行为：

1. 管理员可为当前租户内模型登记输入/输出 Token 单价、币种和 `effective_from`。
2. 同一模型允许存在历史、当前和未来价格计划；同一生效时刻只能有一条。
3. Gateway 按成本入账时刻选择 `effective_from <= now` 的最新计划，不允许调用方通过请求体或 Header 指定价格。
4. 每次 Usage 仍创建独立 `PriceSnapshot`，复制实际采用的单价、币种、版本和计划 ID。
5. 新价格只影响生效后的新账本；历史 `usage_ledger.cost_*` 与 `price_snapshot_id` 不更新、不回放。
6. 创建价格产生 `price.updated` 事件和 Webhook 安全事件。

角色说明：Excel 将主要用户写为“管理员/财务”。当前基础 RBAC 只有 admin/developer/viewer，独立 finance 角色不在本 PRD 的 API 和数据对象范围内。M0.5 由 admin 承担价格写入职责，admin/developer 可读以支持接入排障；未来引入 finance 角色时只需加入价格读写依赖，不改变数据模型。

## 涉及模块

- `infra/migrations/0022_model_price_effective_time.sql`：价格计划、事件及快照关联字段。
- `control-plane-api/src/metahub_control/model_prices/`：DTO、服务、路由和有效状态计算。
- `control-plane-api/src/metahub_control/auth/`：价格读写后端权限依赖。
- `control-plane-api/src/metahub_control/console/`：模型价格导航入口。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`：按时间选择价格计划并创建 Usage 快照。
- `shared-sdk/python/metahub_shared/runtime/`：错误码和 Webhook 事件白名单。
- `console-web/src/lib/modelPrices.js`、`features/model-prices/`：价格页客户端和交互。
- `control-plane-api/tests/integration/test_model_prices.py`：主流程、历史不变、权限和异常测试。

## 数据表/字段设计

### `model_price_plans`

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `price_plan_id` | TEXT | PK | `prp_` 前缀稳定 ID |
| `tenant_id` | TEXT | NOT NULL, FK | 租户隔离边界 |
| `model_id` | TEXT | NOT NULL, FK | 当前租户模型 |
| `input_price_per_1k` | NUMERIC | >= 0 | 输入 Token 每千个原币单价 |
| `output_price_per_1k` | NUMERIC | >= 0 | 输出 Token 每千个原币单价 |
| `currency` | TEXT | CNY/USD | 原始账单币种，不做换汇 |
| `price_schema_version` | TEXT | NOT NULL | 人工版本；未填时由服务生成 |
| `effective_from` | TEXT | NOT NULL | UTC ISO 生效时刻 |
| `source` | TEXT | `manual` | 明确不自动抓价 |
| `created_by` | TEXT | FK users | 操作人 |
| `created_at` | TEXT | NOT NULL | 创建时刻 |

约束与索引：

- `UNIQUE(tenant_id, model_id, effective_from)`，消除同一生效时刻的歧义。
- `UNIQUE(tenant_id, model_id, price_schema_version)`，版本在模型内唯一。
- `(tenant_id, model_id, effective_from DESC)` 支撑 Gateway 选择当前价格。
- 记录创建后不可修改或删除；纠错通过新建更晚计划完成。

`effective_to` 和 `effective_status` 不落库：列表查询使用下一条计划的 `effective_from` 推导结束时间，并以查询时刻计算 `historical/current/scheduled`，避免后台任务维护时间状态。

### `model_price_events`

保存 `tenant_id`、`price_plan_id`、`model_id`、`event_type=price.updated`、`actor_id`、`request_id`、`trace_id`、安全 `payload_json` 和 `created_at`。Payload 只包含模型 ID、币种、版本、生效时间和单价，不含 Provider Key、请求内容或响应内容。

### `price_snapshots` 扩展

- `price_plan_id TEXT NULL`：本次 Usage 采用的有效期计划；历史/兼容路径可为空。
- `effective_from TEXT NULL`：冗余采用计划的生效时刻，便于单条账本解释。

已有快照不回填虚构计划，继续保持 `source=model_catalog`。新计划路径写入 `source=price_plan`。`usage_ledger.price_snapshot_id` 和已入账成本保持不变。

### 模型目录兼容字段

`models.input_price_per_1k/output_price_per_1k/currency/price_schema_version` 暂不删除，用于旧数据和未建立 PricePlan 的兼容回退。Gateway 选择顺序为“已生效 PricePlan -> 模型目录兼容价格”。模型目录页面把价格编辑入口迁移到模型价格页；原 API 字段暂保留，避免破坏 PRD-012 客户端。

## API 设计

### `POST /model-prices`

仅 admin。请求体：

```json
{
  "model_id": "model_xxx",
  "input_price_per_1k": "0.001",
  "output_price_per_1k": "0.002",
  "currency": "CNY",
  "effective_from": "2026-07-15T00:00:00Z",
  "price_schema_version": "manual-20260715"
}
```

响应 `201`：`price_plan`、`request_id`、`trace_id`。金额使用 JSON 字符串返回，避免二进制浮点尾差。允许登记过去、当前或未来时刻；过去时刻不会触发历史 Usage 重算，只影响创建后按当前时间执行的新请求。

### `GET /model-prices`

admin/developer 可读。查询参数：

- `model_id`：可选，按当前租户模型筛选。
- `effective_status`：可选，`all/current/historical/scheduled`，默认 `all`。
- `limit/offset`：默认 100/0，最大 200。

响应：`price_plans`、`total`、`as_of`、`request_id`、`trace_id`。每项包含模型摘要、单价、币种、版本、`effective_from`、推导的 `effective_to/effective_status`、创建人和创建时间。

不提供 PATCH/DELETE：有效价格是财务审计记录，覆盖更新会破坏可追溯性。

## 前端页面/交互设计

- 侧边栏“调用与成本”新增“模型价格”。
- 顶部使用模型筛选和状态分段控件；列表按模型分组并显示历史、当前、待生效状态。
- admin 可打开“新增价格”表单，选择模型、输入两类单价、币种、版本和 UTC 生效时间。
- developer 只读；viewer 无入口。前端隐藏仅改善体验，后端仍执行 RBAC。
- 页面明确显示“已入账用量不会重算”，但不加入自动抓价、汇率、批量导入或审批流。
- 金额字段使用文本输入和后端 Decimal 校验；页面不自行做财务舍入。

## 错误码设计

| 错误码 | HTTP | 场景 |
|---|---:|---|
| `model_price.model_required` | 422 | 缺少模型 ID |
| `model_price.model_not_found` | 404 | 当前租户无此模型 |
| `model_price.price_invalid` | 422 | 单价非法、负数、非有限值或精度超限 |
| `model_price.currency_invalid` | 422 | 非 CNY/USD |
| `model_price.effective_from_invalid` | 422 | 缺失或非带时区 ISO 时间 |
| `model_price.version_invalid` | 422 | 版本为空、超长或格式非法 |
| `model_price.effective_conflict` | 409 | 模型同一生效时刻已有计划 |
| `model_price.version_conflict` | 409 | 模型内版本重复 |
| `model_price.query_invalid` | 422 | 状态或分页非法 |
| `auth.permission_denied` | 403 | developer 写入或 viewer 访问 |

所有错误由统一中间件补 `request_id/trace_id`，数据库异常文本不进入响应。

## 安全与权限约束

1. `tenant_id` 只从 Session 恢复；API 不接受客户端租户字段。
2. 模型必须通过 `tenant_id + model_id` 查询，跨租户 ID 对外表现为不存在。
3. Gateway 不接受价格 ID、币种或生效时间覆盖参数，只按可信模型和服务器 UTC 时间选价。
4. 创建事件、Webhook 和结构化日志不记录 Provider Key、Virtual Key、Authorization、Prompt、Response 或上游原始账单正文。
5. 金额全部使用 `Decimal`；CNY/USD 分栏记录，不做隐式汇率换算。
6. 已入账 `usage_ledger`、`price_snapshots` 不更新；创建历史生效计划也不能回放修改旧账。
7. Webhook 在主事务提交后 best-effort 分发，失败不回滚已创建价格计划。

## 测试用例

### 主流程

1. admin 创建当前生效价格，GET 返回 `current`，事件记录 `price.updated`。
2. 创建未来价格后，当前请求仍使用旧计划；时间推进到生效点后新请求使用新计划。
3. 两次请求分别引用不同 `price_snapshot_id/price_plan_id`，成本按各自单价计算。
4. 价格变更后查询历史 Usage，旧成本、币种、版本和快照不变。
5. 没有计划的旧模型继续使用模型目录兼容价格。

### 异常/权限/安全

1. developer POST、viewer GET 均返回后端 `auth.permission_denied`。
2. 跨租户 `model_id` 返回 `model_price.model_not_found`。
3. 重复生效时间、重复版本分别返回 409 统一错误。
4. 负数、NaN、Infinity、非法币种、无时区时间和非法分页返回 422。
5. 事件、日志、API 响应和数据库价格事件中不包含任何 Key、Token、Prompt 或 Response。
6. migration/down migration、外键检查和注释门禁通过。

## 代码修改计划

1. 新增 0022 正反迁移，保留旧价格快照和 Usage 关联。
2. 新增 `model_prices` 控制面模块、RBAC 依赖、路由注册、错误码和 Webhook 白名单。
3. 修改 Gateway 成本估算，按 `effective_from` 选择计划并写入不可变快照。
4. 新增控制台 API 客户端、价格页面、导航和样式。
5. 新增后端集成与前端单测，更新迁移、导航和既有 Usage 测试。
6. 更新 README、目录图和 PRD 测试记录，执行聚焦与全量门禁。

## 第一轮设计自审

结论：需修订后通过。

发现并修订：

1. 不能把 `price_snapshots` 直接改成价格计划表，否则会破坏 PRD-017 每请求快照语义；采用独立 `model_price_plans`，快照仅新增关联字段。
2. 不能用持久化 `current/scheduled` 状态，否则时间推进后需要后台任务；改为由 `effective_from` 与下一条计划动态推导。
3. 未来价格不能提前覆盖 `models` 当前字段；Gateway 直接按有效时间查询计划，模型字段只做旧数据回退。
4. Webhook 不能与数据库事务混在一起；主记录和事件先提交，再 best-effort 推送。

## 第二轮设计自审

结论：通过，可以编码。

确认项：

1. 价格选择使用服务器 UTC 入账时刻，而不是客户端时间，避免绕过生效边界。
2. 同时保留“模型+生效时刻唯一”和“模型+版本唯一”，错误映射不暴露 SQLite 文本。
3. 允许回填过去生效时间，但明确绝不更新旧 Usage；这覆盖历史计划录入而不破坏账本不可变性。
4. 当前 RBAC 未定义 finance，按现有架构由 admin 写、developer 读，并在文档中保留未来 finance 接入点，不在本 PRD 扩大基础角色模型。
5. 不加入自动抓价、汇率、审批、批量导入或复杂计费维度，符合 Excel 和架构 MVP 边界。
