# PRD-022 - Virtual Key级RPM/TPM限流

状态：已实现，已通过注释门禁、后端测试、前端测试和控制台 lint 验证。

## 需求理解

PRD-022 属于 M0-W2/P0 的基础限流能力，目标是在数据面按 Virtual Key 控制每分钟请求数、每分钟 Token 数和并发数，防止单个 Key 打爆上游或产生不可控成本。管理员或开发者可以在控制台为某个 Virtual Key 设置限制；数据面鉴权成功后读取该 Key 的限流规则，在调用上游前完成准入判断，超限返回统一 429，并记录 `rate_limit.exceeded` 事件和失败 request log。

本 PRD 只做 Virtual Key 级限制，不做租户级复杂配额、多层限流协同、模型级限流、预算扣减和跨区域一致性。M0 使用 Redis 维护计数器；数据库只保存规则配置，不保存每分钟计数器历史。未配置限流规则时不拦截请求；已配置限流规则但 Redis 不可用时采用 fail closed，返回 503，避免限流保护静默失效后继续打上游。

本次实现同时覆盖 Chat Completions 和 Embeddings 两个已上线 OpenAI-compatible 入口。流式请求也必须在上游调用前进入准入判断，并在流结束或异常时释放并发计数。

## 涉及模块

- `infra/migrations/`
  - 新增 `0014_virtual_key_rate_limit_rules.sql` 和 down 迁移。
  - 保存 Virtual Key 限流规则，所有业务字段带 `tenant_id`。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加限流配置校验、超限、Redis 不可用等统一错误码。
- `control-plane-api/src/metahub_control/virtual_keys/`
  - 扩展 Virtual Key DTO，列表返回当前规则。
  - 新增 `PATCH /virtual-keys/{id}/limits`。
  - 继续使用后端 `require_virtual_key_writer` 做权限校验。
- `gateway-data-plane/src/metahub_gateway/rate_limits/`
  - 新增限流规则加载、Token 估算、Redis 计数器、并发 acquire/release 逻辑。
  - 测试环境提供内存计数器实现，便于覆盖窗口边界和并发。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`
  - 在鉴权、读请求体和路由解析后，上游调用前执行限流。
  - 成功、异常和流式结束都要释放并发占用。
  - 超限或 Redis 故障时写失败 `request_logs`。
- `console-web/src/lib/virtualKeys.js`
  - 增加 `updateVirtualKeyLimits()` 客户端函数。
- `console-web/src/features/virtual-keys/renderVirtualKeyPanel.js`
  - 在 Key 行内展示 RPM/TPM/并发限制和保存按钮。
  - 429 错误通过已有错误诊断/错误卡片能力展示。
- `console-web/src/app/main.js`
  - 绑定限制设置表单。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-022-tests.md`
  - 更新功能说明、模块边界和测试记录。

## 数据表/字段设计

### 新表：virtual_key_rate_limit_rules

用途：保存当前租户下每把 Virtual Key 的限流规则。M0 每把 Key 最多一条 active 规则。

字段：

- `rate_limit_rule_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `virtual_key_id TEXT NOT NULL`
- `rpm_limit INTEGER`
  - 每 60 秒最多请求数。
  - `NULL` 表示不限制。
  - 非空时必须大于 0。
- `tpm_limit INTEGER`
  - 每 60 秒最多估算 Token 数。
  - `NULL` 表示不限制。
  - 非空时必须大于 0。
- `concurrency_limit INTEGER`
  - 同一 Virtual Key 同时在途请求数。
  - `NULL` 表示不限制。
  - 非空时必须大于 0。
- `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))`
- `created_by TEXT`
- `updated_by TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

约束和索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id)`
- `UNIQUE (tenant_id, virtual_key_id)`
- `idx_virtual_key_rate_limit_rules_tenant_id`
- `idx_virtual_key_rate_limit_rules_virtual_key_id`

设计理由：

- 不把限制字段直接塞入 `virtual_keys`，避免 Key 元数据表随治理能力膨胀。
- 后续可扩展 `scope_type/scope_id/window_seconds` 做租户级、模型级或多窗口限制。
- 计数器不落库，避免热路径写 SQLite；Redis 是运行时状态源。

### Redis Key 设计

Redis Key 不包含明文 Virtual Key，只使用 `tenant_id` 与 `virtual_key_id`：

- RPM：`metahub:rl:{tenant_id}:{virtual_key_id}:rpm:{window_start_epoch}`
- TPM：`metahub:rl:{tenant_id}:{virtual_key_id}:tpm:{window_start_epoch}`
- 并发：`metahub:rl:{tenant_id}:{virtual_key_id}:concurrency`

TTL：

- RPM/TPM 固定窗口计数器 TTL 为 90 秒，覆盖 60 秒窗口和少量时钟偏移。
- 并发计数器 TTL 为 120 秒，避免进程异常退出导致永久占用。

窗口算法：

- M0 使用固定 60 秒窗口：`window_start = now_epoch - now_epoch % 60`。
- 先估算本次请求 token，再用 Redis 原子递增检查是否超过阈值。
- RPM 和 TPM 任一超过限制都拒绝。

TPM 估算：

- Chat：累加 `messages[].content` 的字符长度，按 `ceil(chars / 4)` 估算输入 token；`max_tokens` 若为正整数则加入预留输出 token，否则不预留。
- Embeddings：按 `input` 字符串或列表内容长度估算输入 token。
- 最小估算值为 1，避免空但通过基础校验的请求绕过 TPM。
- 上游返回真实 usage 后仍由 `usage_ledger` 追踪成本；PRD-022 的 TPM 只是准入估算，不改账本口径。

## API设计

### PATCH /virtual-keys/{id}/limits

权限：`admin`、`developer`。后端使用 `require_virtual_key_writer`，并按登录态 `tenant_id` 过滤，不接受请求体里的 `tenant_id`。

请求体：

```json
{
  "rpm_limit": 60,
  "tpm_limit": 120000,
  "concurrency_limit": 5,
  "status": "active"
}
```

字段规则：

- 三个限制字段可传整数或 `null`。
- `null` 表示清除该项限制。
- 非空整数范围：`1..100000000`。
- `status` 可选，只允许 `active` 或 `disabled`。
- 至少提供一个可编辑字段。
- 请求体没有任何可编辑字段时，返回 `virtual_key.limit_update_empty`。
- 三个限制字段都显式传 `null` 是合法操作，表示清除该 Key 的全部基础限制。

响应：

```json
{
  "virtual_key": {
    "virtual_key_id": "vk_x",
    "tenant_id": "tenant_default",
    "name": "生产调用 Key",
    "key_prefix": "mhv_prod_ab",
    "environment": "prod",
    "description": "order-service",
    "status": "active",
    "last_used_at": "2026-07-09T10:00:00Z",
    "created_at": "2026-07-09T09:00:00Z",
    "updated_at": "2026-07-09T10:00:00Z",
    "disabled_at": null,
    "rate_limit_rule": {
      "rate_limit_rule_id": "rlr_x",
      "rpm_limit": 60,
      "tpm_limit": 120000,
      "concurrency_limit": 5,
      "status": "active",
      "updated_at": "2026-07-09T10:01:00Z"
    }
  },
  "request_id": "req_x",
  "trace_id": "tr_x"
}
```

### GET /virtual-keys

现有接口保持路径不变，扩展每条 `virtual_key.rate_limit_rule` 字段；没有规则时为 `null`。列表仍不得返回 `key_hash` 或明文 Key。

### 数据面 /v1/chat/completions 与 /v1/embeddings

数据面不新增对外路径。请求流程调整为：

1. 校验 Bearer Virtual Key，恢复 `tenant_id/virtual_key_id/environment`。
2. 读取请求体并完成基础校验。
3. 解析模型路由，构造 `RequestLogContext`。
4. 加载 Virtual Key 限流规则。
5. 若规则存在且启用，估算本次 tokens，执行 Redis 准入。
6. 准入成功后调用上游；失败返回统一错误并写 request log。
7. 请求完成、上游异常或流式结束时释放并发占用。

超限响应示例：

```json
{
  "code": "gateway.rate_limit_exceeded",
  "message": "Rate limit exceeded.",
  "request_id": "req_x",
  "trace_id": "tr_x",
  "suggestion": "Reduce request rate or increase the Virtual Key limits.",
  "diagnosis": {
    "rule_id": "gateway_rate_limit_exceeded",
    "category": "gateway_rate_limit",
    "reason": "Virtual Key reached its configured rate limit.",
    "suggestion": "Wait for the current window to reset or ask an administrator to adjust limits.",
    "action": "retry_after_window",
    "severity": "warning",
    "retryable": true,
    "matched_by": "error_code",
    "safe_summary": "gateway.rate_limit_exceeded"
  }
}
```

## 前端页面/交互设计

入口：控制台 `Virtual Key` 面板。

交互：

- Key 列表新增三列或一个紧凑限制区：
  - RPM
  - TPM
  - 并发
- 每行展示当前限制，空值显示“不限制”。
- admin/developer 可编辑三个数字输入并点击“保存限制”。
- viewer 角色当前不会进入 Virtual Key 写面板；即使绕过前端调用 API，后端仍返回 403。
- 保存成功后刷新列表并显示“限制已保存”。
- 保存失败展示统一错误消息、`request_id` 和建议。
- 网关调试台或调用方遇到 429 时复用 PRD-021 的诊断卡片展示原因和建议。

可用性约束：

- 数字输入使用 `min=1`，并允许清空表示不限制。
- 不展示 Virtual Key 明文。
- 不把请求 Prompt、Response、Authorization 写入前端状态或本地存储。
- 行内表单维持稳定宽度，避免长 Key 名称或错误文案撑破布局。

## 错误码设计

新增错误码：

- `virtual_key.limit_invalid`
  - HTTP 422。
  - 限制字段不是正整数、超过上限，或 `status` 非法。
- `virtual_key.limit_update_empty`
  - HTTP 422。
  - 限制更新请求没有任何可编辑字段。
- `gateway.rate_limit_exceeded`
  - HTTP 429。
  - Virtual Key 已达到 RPM、TPM 或并发限制。
  - `retryable=true`。
- `gateway.rate_limit_unavailable`
  - HTTP 503。
  - 限流规则启用但 Redis 不可用或命令失败。
  - `retryable=true`。

错误响应必须继续包含：

- `code`
- `message`
- `request_id`
- `trace_id`
- `suggestion`
- `diagnosis`

`gateway.rate_limit_exceeded` 和 `gateway.rate_limit_unavailable` 需要加入错误诊断规则库，诊断内容只包含安全摘要，不包含明文 Virtual Key、Provider Key、Prompt、Response、Redis URL 或上游返回体。

## 安全与权限约束

- 所有规则表记录必须带 `tenant_id`，查询和更新必须使用认证上下文中的 `tenant_id`。
- `PATCH /virtual-keys/{id}/limits` 必须验证目标 Key 属于当前租户。
- 前端隐藏控件不是权限边界；viewer 直接调用后端必须被拒绝。
- Virtual Key 明文只允许创建响应返回一次；限流逻辑只使用 `virtual_key_id/key_prefix` 等安全元数据。
- Redis Key 不包含明文 Key、Prompt、Response、Authorization 或 Provider Key。
- 结构化日志 `rate_limit.exceeded` 只记录：
  - `tenant_id`
  - `actor_id=virtual_key_id`
  - `object_id=request_id`
  - `trace_id`
  - `payload.limit_type`
  - `payload.limit`
  - `payload.current`
  - `payload.window_seconds`
  - `payload.endpoint`
- 超限请求不得调用上游 Provider，避免无意义成本。
- 超限请求必须写入 `request_logs`，`status=failed`，`error_code=gateway.rate_limit_exceeded`。
- Redis 不可用时：
  - 如果目标 Key 没有启用限流规则，允许继续请求。
  - 如果目标 Key 启用了任一限制，返回 `gateway.rate_limit_unavailable`，并写失败 request log。

## 测试用例

### 后端控制面

1. 主流程：admin 创建 Virtual Key 后调用 `PATCH /virtual-keys/{id}/limits`，设置 RPM/TPM/并发，`GET /virtual-keys` 能返回 `rate_limit_rule`。
2. 权限：viewer 调用 `PATCH /virtual-keys/{id}/limits` 返回 403 `auth.permission_denied`。
3. 租户隔离：当前租户不能更新其他租户的 Virtual Key 限制，返回 `virtual_key.not_found`。
4. 异常：负数、0、非整数、超大整数、非法 status 返回 `virtual_key.limit_invalid`。
5. 异常：空请求体返回 `virtual_key.limit_update_empty`。
6. 安全：响应和日志不包含 Virtual Key 明文、`key_hash` 或 Provider Key。

### 后端数据面

1. 主流程：设置 `rpm_limit=2` 后，同一 Virtual Key 第 1、2 次请求成功，第 3 次返回 429 `gateway.rate_limit_exceeded`。
2. 隔离：Key A 超过 RPM 不影响 Key B。
3. TPM：设置较小 `tpm_limit` 后，大输入请求返回 429，且不调用 fake upstream。
4. 并发：设置 `concurrency_limit=1`，并发两个请求时一个成功一个 429；完成后下一次请求可成功。
5. 窗口边界：跨 60 秒窗口后 RPM/TPM 计数重置。
6. Redis 不可用：启用限流规则时返回 503 `gateway.rate_limit_unavailable`，并写失败 request log。
7. 无规则：Redis URL 缺失或测试计数器不可用但 Key 没有限流规则时，请求不受影响。
8. 失败日志：超限和 Redis 不可用请求均写入 `request_logs`，带 `tenant_id`、`virtual_key_id`、`error_code`。
9. 安全：Redis Key、request log、结构化日志不包含明文 Virtual Key、Provider Key、Prompt、Response。

### 前端

1. `listVirtualKeys()` 能接收并保留 `rate_limit_rule`。
2. `updateVirtualKeyLimits()` 调用正确路径、方法、请求体和错误包装。
3. Virtual Key 面板渲染 RPM/TPM/并发表单，保存按钮存在。
4. 限制保存错误展示 `request_id`，并对服务端消息做 HTML 转义。
5. viewer 视角不渲染写入控件。

## 代码修改计划

1. 新增数据库迁移：
   - `infra/migrations/0014_virtual_key_rate_limit_rules.sql`
   - `infra/migrations/0014_virtual_key_rate_limit_rules.down.sql`
2. 扩展错误码和错误诊断：
   - `shared-sdk/python/metahub_shared/runtime/errors.py`
   - `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
3. 扩展 Virtual Key 控制面：
   - `models.py` 增加 `RateLimitRuleInfo`、`UpdateVirtualKeyLimitsRequest`。
   - `service.py` 增加规则加载、校验、upsert、列表 join。
   - `routes.py` 增加 `PATCH /virtual-keys/{id}/limits`。
4. 新增数据面限流模块：
   - `gateway-data-plane/src/metahub_gateway/rate_limits/__init__.py`
   - `gateway-data-plane/src/metahub_gateway/rate_limits/models.py`
   - `gateway-data-plane/src/metahub_gateway/rate_limits/store.py`
   - `gateway-data-plane/src/metahub_gateway/rate_limits/service.py`
5. 在 `openai_compatible.py` 接入限流：
   - 鉴权和路由之后、上游请求之前 acquire。
   - 所有退出路径 release 并发。
   - 超限/Redis 故障写失败 request log。
6. Redis 客户端依赖：
   - 增加 `redis` Python 依赖使用计划。
   - 测试中用内存 store 注入，不依赖本机 Redis 服务。
7. 扩展前端：
   - `console-web/src/lib/virtualKeys.js`
   - `console-web/src/features/virtual-keys/renderVirtualKeyPanel.js`
   - `console-web/src/app/main.js`
   - `console-web/src/styles/main.css`
8. 新增/更新测试：
   - `control-plane-api/tests/integration/test_virtual_key_rate_limits.py`
   - `gateway-data-plane/tests/unit/test_rate_limits.py`
   - `gateway-data-plane/tests/integration/test_virtual_key_rate_limits.py`
   - `console-web/tests/unit/virtualKeys.test.mjs`
   - `control-plane-api/tests/integration/test_db_migrations.py`
9. 更新文档：
   - `README.md`
   - `docs/development/directory-map.md`
   - `docs/testing/PRD-022-tests.md`
10. 验证：
    - `python -m pytest control-plane-api/tests/integration/test_virtual_key_rate_limits.py gateway-data-plane/tests/unit/test_rate_limits.py gateway-data-plane/tests/integration/test_virtual_key_rate_limits.py -q`
    - `python -m pytest control-plane-api/tests/integration/test_db_migrations.py -q`
    - `node --test console-web/tests/unit/*.test.mjs`
    - `node console-web/scripts/lint.mjs`
    - `python scripts/ci/check-comments.py`
    - `python -m pytest -q`

## 自审 1

- 与 PRD 边界一致：只做 Virtual Key 级 RPM/TPM/并发，不做租户级、多层协同或预算。
- 与验收规则一致：超限返回 429，不同 Key 独立，限制修改后由数据库规则生效。
- 成本风险处理：启用限流后 Redis 不可用 fail closed，不让保护静默失效。
- 可追踪性：超限和 Redis 不可用都会写 request log，并记录安全事件。
- 敏感信息：Redis Key、日志和响应都不包含明文 Virtual Key、Provider Key、Prompt、Response。
- 权限：控制面后端继续使用 writer 权限，不依赖前端隐藏按钮。

## 自审 2

- 最大复杂点是并发释放：必须把 acquire/release 放在统一上下文或 `try/finally`，流式响应要在迭代器 `finally` 中释放。
- 最大一致性风险是测试环境没有 Redis：设计通过 store 抽象和内存实现覆盖算法，同时生产路径走 Redis。
- 最大体验风险是前端行内表单过宽：实现时需要稳定列宽和响应式布局，不让长名称、错误文案或数字输入撑破面板。
- 最大审计风险是超限早于 request log context：设计要求限流在路由后执行，确保失败日志有 `tenant_id/virtual_key_id/model/provider` 等可追踪元数据。

结论：设计可以进入审核；审核通过后再编码。
