# PRD-017 - Token Usage 与成本估算

状态：已完成设计、编码、测试与文档更新。

## 需求理解

PRD-017 属于 M0-W1/P0 的 Usage 记录能力。目标是在每次模型调用后，把 Provider Adapter 解析出的 usage 归一化为可查询、可汇总、可追踪的企业账本记录，让管理员、财务和开发者能看到请求级 Token 消耗与大概成本。

本 PRD 必须满足：

- 从 Adapter 解析 usage，统一 `input_tokens`、`output_tokens`、`cache_tokens`、`cost_cny`、`cost_usd`。
- 当上游缺少 usage 字段时仍写入 UsageRecord，并标记 `estimated=true`。
- 至少支持按天、模型、Virtual Key 统计用量。
- 每条 UsageRecord 能回溯到 `tenant_id`、`request_id`、`price_schema_version` 和价格快照。
- 成本单位清晰，真实 usage 与估算 usage 在 API 和控制台中可区分。

边界：

- M0 不做复杂汇率回溯，不做账单冲正，不做完整 PricePlan 管理，不自动抓取厂商价格。
- PriceSnapshot 在本 PRD 只作为调用入账时的价格锚点，完整价格表与生效时间留给 PRD-031。
- OpenRouter、硅基流动、New API、Sub2Api 仅作为对标和迁移兼容参考，不作为默认上游模型厂商。

架构约束摘录：

- Usage 统计走旁路，不能阻塞流式首字延迟。
- Provider Adapter 必须有 Usage 字段测试。
- 需要同时保留 raw_usage 与 normalized_usage。
- Usage Ledger 应能回溯到 `request_id` 与 `price_schema_version`。
- Trace、Usage、Budget、Webhook 等数据均按租户隔离查询。

## 涉及模块

- `gateway-data-plane/src/metahub_gateway/providers/*`
  - 扩展 `NormalizedUsage`，解析 `cache_tokens`，缺失 usage 时生成估算 usage。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`
  - 写入 UsageRecord、PriceSnapshot，并回写 request_logs 的扁平摘要字段。
- `infra/migrations`
  - 新增 `0010_token_usage_cost_estimation.sql` 与 down 迁移。
- `control-plane-api/src/metahub_control/usage`
  - 新增 Usage 查询模型、服务和路由。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增 Usage 查询错误码。
- `console-web/src/lib/usage.js`
  - 新增控制台 Usage API 客户端。
- `console-web/src/features/usage/renderUsagePanel.js`
  - 新增用量概览、分组汇总、用量记录列表。
- `console-web/src/app/main.js` 与 `renderDeveloperStart.js`
  - 接入 Usage 面板和筛选交互。
- `tests`
  - 补迁移、Gateway usage、Control API、Console UI 单元/集成测试。

## 数据表/字段设计

### 迁移策略

沿用现有 `usage_ledger` 表作为 UsageRecord 物理表，避免 M0 引入重复账本表。新增 `price_snapshots` 作为入账时价格锚点。

### `usage_ledger` 新增字段

- `status TEXT NOT NULL DEFAULT 'recorded'`
  - 当前仅使用 `recorded`，后续冲正/撤销能力由 M1/M2 扩展。
- `total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0)`
- `cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_tokens >= 0)`
- `cost_cny NUMERIC NOT NULL DEFAULT 0 CHECK (cost_cny >= 0)`
- `cost_usd NUMERIC NOT NULL DEFAULT 0 CHECK (cost_usd >= 0)`
- `estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1))`
- `usage_source TEXT NOT NULL DEFAULT 'upstream' CHECK (usage_source IN ('upstream', 'estimated'))`
- `price_snapshot_id TEXT`
- `model_id TEXT`
- `provider_id TEXT`
- `virtual_key_id TEXT`
- `endpoint TEXT`
- `request_model TEXT`
- `resolved_model TEXT`
- `provider_code TEXT`
- `updated_at TEXT`

索引：

- `idx_usage_ledger_tenant_created_at(tenant_id, created_at DESC)`
- `idx_usage_ledger_tenant_model_time(tenant_id, model_id, created_at DESC)`
- `idx_usage_ledger_tenant_key_time(tenant_id, virtual_key_id, created_at DESC)`
- `idx_usage_ledger_tenant_provider_time(tenant_id, provider_id, created_at DESC)`
- `idx_usage_ledger_tenant_estimated_time(tenant_id, estimated, created_at DESC)`

### `price_snapshots` 新表

字段：

- `price_snapshot_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `model_id TEXT`
- `provider_id TEXT`
- `provider_code TEXT`
- `price_schema_version TEXT`
- `currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD'))`
- `input_price_per_1k NUMERIC NOT NULL DEFAULT 0`
- `output_price_per_1k NUMERIC NOT NULL DEFAULT 0`
- `cache_price_per_1k NUMERIC NOT NULL DEFAULT 0`
- `source TEXT NOT NULL DEFAULT 'model_catalog'`
- `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

索引：

- `idx_price_snapshots_tenant_model_version(tenant_id, model_id, price_schema_version)`
- `idx_price_snapshots_tenant_created_at(tenant_id, created_at DESC)`

### 成本口径

- `cost_amount` + `currency` 保留为模型目录原始币种成本，兼容 PRD-016。
- `cost_cny` 与 `cost_usd` 明确区分币种：
  - 模型目录币种为 `CNY`：`cost_cny=cost_amount`，`cost_usd=0`。
  - 模型目录币种为 `USD`：`cost_usd=cost_amount`，`cost_cny=0`。
- M0 不做 CNY/USD 互转，避免伪造汇率和历史账单口径。

## API 设计

### `GET /usage/summary`

用途：返回当前租户的用量总览和分组汇总。

权限：`admin`、`developer` 可读；`viewer` 拒绝。M0 还没有独立 `finance` 角色，财务视角先由 `admin` 承载。

Query：

- `start_date`: 可选，ISO 日期或时间，默认不限制。
- `end_date`: 可选，ISO 日期或时间，默认不限制。
- `model_id`: 可选。
- `virtual_key_id`: 可选。
- `provider_id`: 可选。
- `estimated`: 可选布尔值。

响应：

```json
{
  "totals": {
    "record_count": 12,
    "estimated_count": 2,
    "input_tokens": 1000,
    "output_tokens": 600,
    "cache_tokens": 120,
    "total_tokens": 1720,
    "cost_cny": 0.84,
    "cost_usd": 0
  },
  "by_day": [
    {"day": "2026-07-09", "record_count": 12, "total_tokens": 1720, "cost_cny": 0.84, "cost_usd": 0}
  ],
  "by_model": [
    {"model_id": "model_default", "resolved_model": "deepseek-chat", "total_tokens": 1720, "cost_cny": 0.84}
  ],
  "by_virtual_key": [
    {"virtual_key_id": "vk_x", "key_prefix": "mhv_dev_****", "total_tokens": 1720, "cost_cny": 0.84}
  ],
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

### `GET /usage/records`

用途：分页返回当前租户 UsageRecord 明细。

权限：`admin`、`developer` 可读；`viewer` 拒绝。

Query：

- `start_date`
- `end_date`
- `model_id`
- `virtual_key_id`
- `provider_id`
- `request_id`
- `estimated`
- `limit`，默认 20，最大 100。
- `offset`，默认 0。

响应：

```json
{
  "usage_records": [
    {
      "usage_ledger_id": "usg_x",
      "tenant_id": "tenant_default",
      "request_id": "req_x",
      "trace_id": "trc_x",
      "virtual_key_id": "vk_x",
      "virtual_key_prefix": "mhv_dev_****",
      "model_id": "model_default",
      "resolved_model": "deepseek-chat",
      "provider_id": "provider_x",
      "provider_code": "deepseek",
      "input_tokens": 100,
      "output_tokens": 60,
      "cache_tokens": 20,
      "total_tokens": 180,
      "cost_cny": 0.12,
      "cost_usd": 0,
      "currency": "CNY",
      "estimated": false,
      "usage_source": "upstream",
      "price_snapshot_id": "ps_x",
      "price_schema_version": "seed-v1",
      "created_at": "2026-07-09T09:00:00Z"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0,
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

## 前端页面/交互设计

新增“用量概览”面板，放在调用日志面板之前或附近，面向 admin/developer 展示，viewer 不渲染入口。

页面组成：

- 顶部 KPI：
  - 总请求数、估算请求数、总 Token、缓存 Token、CNY 成本、USD 成本。
- 筛选区：
  - 起始日期、结束日期、模型 ID、Virtual Key ID、Provider ID、是否估算。
- 汇总表：
  - 按天汇总。
  - 按模型汇总。
  - 按 Virtual Key 汇总。
- 明细表：
  - request_id、模型、Key 前缀、Provider、Token、成本、估算标识、时间。
  - request_id 可复制或与后续调用详情联动；M0 不新增复杂路由，只展示安全元数据。

交互边界：

- 前端不传 `tenant_id`。
- 前端不展示 raw_usage、Prompt、Response、Provider Key、Virtual Key 明文。
- 前端隐藏 viewer 入口只是体验优化，后端仍必须返回 `auth.permission_denied`。

## 错误码设计

新增：

- `usage.query_invalid`
  - HTTP 422。
  - 用于 `limit/offset`、过长过滤条件、非法布尔值等查询参数错误。
- `usage.date_invalid`
  - HTTP 422。
  - 用于日期格式错误或 `start_date > end_date`。

复用：

- `auth.token_missing`、`auth.token_invalid`、`auth.permission_denied`
- `infra.unhandled`
- 数据库异常通过 `DbError` 映射为统一错误结构。

所有错误响应继续由共享 FastAPI 中间件携带 `request_id` 和 `trace_id`。

## 安全与权限约束

- Usage 查询只使用登录态中的 `context.user.tenant_id`，不接受客户端传入 tenant_id。
- `usage_ledger`、`price_snapshots` 均包含 `tenant_id`。
- Provider Key、Virtual Key 明文、Authorization、Prompt、Response 不进入 Usage API 响应。
- `raw_usage` 可落库用于审计，但控制面 API 默认只返回 `normalized_usage` 的安全字段。
- 估算 usage 必须显式标记 `estimated=true` 和 `usage_source=estimated`，避免财务误认为上游真实返回。
- PriceSnapshot 只保存价格字段和模型/Provider 元数据，不保存厂商密钥或请求内容。

## 测试用例

### 主流程

- Gateway 非流式请求返回 upstream usage：
  - Adapter 解析 input/output/cache/total。
  - usage_ledger 写入真实 usage，`estimated=false`。
  - 成本按模型目录价格计算，`cost_cny/cost_usd` 与原始币种一致。
  - price_snapshot_id 写入，`usage.recorded` 事件日志不含密钥。
- `GET /usage/summary`：
  - admin/developer 可按天、模型、Virtual Key 汇总。
  - 响应含 request_id/trace_id。
- `GET /usage/records`：
  - 支持分页和 request_id/model/key/provider/estimated 过滤。

### 异常/权限

- 上游缺少 usage：
  - 仍写入 UsageRecord。
  - 使用轻量估算策略，`estimated=true`，`usage_source=estimated`。
- 价格缺失：
  - 成本回落到 0，仍保留 usage 记录。
- USD 模型：
  - `cost_usd` 有值，`cost_cny=0`，不做汇率换算。
- viewer 调用 `/usage/*`：
  - 后端返回 `auth.permission_denied`。
- 非法日期、非法 limit/offset：
  - 返回 `usage.date_invalid` 或 `usage.query_invalid`，响应含 request_id。
- 控制台渲染：
  - 不泄露 raw_usage、Provider Key、Virtual Key 明文。

## 代码修改计划

1. 新增 `0010_token_usage_cost_estimation.sql/down.sql`。
2. 扩展 `NormalizedUsage` 与 OpenAI-compatible Adapter usage 解析：
   - cache token。
   - 缺 usage 的估算标记。
3. 扩展 `usage/ledger.py`：
   - PriceSnapshot 写入/复用。
   - cost_cny/cost_usd、estimated、usage_source、metadata 写入。
   - `usage.recorded` 安全事件日志。
4. 新增控制面 Usage 模块：
   - `models.py`
   - `service.py`
   - `routes.py`
   - app 注册路由。
5. 新增前端 Usage 客户端与面板，并接入首页刷新和事件绑定。
6. 补测试：
   - 迁移测试。
   - Gateway Adapter/Ledger 测试。
   - Control API 集成测试。
   - Console Node 测试。
7. 更新 README 与测试文档。

## 自审 1：需求完整性

- 已覆盖 Excel 中 PRD-017 的 `input_tokens/output_tokens/cache_tokens/cost_cny/cost_usd/estimated=true`。
- 已覆盖验收要求的按天、模型、Key 汇总。
- 已保留与 PRD-031 的边界：本 PRD 只做入账价格快照，不做完整价格计划管理和历史汇率回放。
- 已覆盖架构文档对 raw_usage、normalized_usage、request_id、price_schema_version 的要求。

结论：设计完整，未把 M0.5/M1 能力提前塞进 M0。

## 自审 2：安全、性能与可运维

- Usage 写入仍在数据面旁路，失败走 `safe_record_usage_ledger` 降级，不影响网关响应。
- 控制面查询全部使用登录上下文 tenant_id，不接受客户端 tenant_id。
- Provider Key、Virtual Key 明文、Authorization、Prompt、Response 不进入响应和事件日志。
- 汇总查询依赖租户+时间、租户+模型、租户+Key 索引，满足 M0 控制台低频查询。
- 缺 usage 记录会显式标记估算，避免财务误读。

结论：已按设计完成编码与测试，后续价格计划管理、汇率回溯和账单冲正继续留给 PRD-031 及后续账单 PRD。

## 实现结果

- 数据层新增 `0010_token_usage_cost_estimation.sql/down.sql`，扩展 `usage_ledger` 并新增 `price_snapshots`。
- Gateway Adapter 支持 `cache_tokens` 解析；上游缺少 usage 时生成显式 `estimated=true` 的本地估算 usage。
- Usage Ledger 写入 `cost_cny`、`cost_usd`、`price_snapshot_id`、`usage_source` 和模型/Provider/Virtual Key 维度，并记录安全的 `usage.recorded` 事件。
- Control Plane 新增 `GET /usage/summary` 与 `GET /usage/records`，后端使用 `require_usage_reader` 校验 admin/developer 权限，viewer 直接拒绝。
- Console Web 新增 Token 用量与成本面板，展示总览、按天/模型/Key 聚合、明细分页、真实/估算标识和 CNY/USD 成本。
- 调用日志详情补充 cache token、双币种成本、价格快照和 usage 来源，仍不返回 raw usage、Prompt、Response 或任何 Key 明文。

## 测试结果

已覆盖主流程、异常、权限和前端展示：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m pytest control-plane-api\tests\integration\test_usage.py control-plane-api\tests\integration\test_request_logs.py gateway-data-plane\tests\unit\test_provider_adapters.py gateway-data-plane\tests\integration\test_request_log_minimal_loop.py -q
```

结果：`13 passed`。

```powershell
& $py -m pytest -q
```

结果：后端全量 pytest 通过。

```powershell
$node='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test console-web\tests\unit\*.test.mjs
& $node console-web\scripts\lint.mjs
```

结果：控制台 Node 测试 `55 pass`，`console lint ok (44 files)`。

```powershell
& $py scripts\ci\check-comments.py
```

结果：`comment coverage ok`，中文注释门禁覆盖 Python、JavaScript、SQL 与 CSS。
