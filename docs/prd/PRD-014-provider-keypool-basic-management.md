# PRD-014 - Provider KeyPool基础管理设计

状态：已完成设计、自审、编码、测试与文档更新。

设计来源：Excel `PRD-014 - Provider KeyPool基础管理`。阶段 `M0-W1`，优先级 `P0`，模块 `KeyPool`。范围是实现 `ProviderCredential/KeyPoolState`：同一 Provider 多个上游 API Key、权重、状态、最近错误、限流摘除、手动禁用；接口为 `POST /providers/{id}/keys`、`GET /providers/{id}/keys`、`PATCH /provider-keys/{id}`。验收要求：同一 Provider 可配置多个 Key；禁用 Key 不参与路由；Key 明文不回显；限流错误可临时摘除。

设计自审 1（需求完整性）：本 PRD 必须同时覆盖控制面 KeyPool 管理和数据面 Key 选择。仅能创建/列出/禁用 Key 还不够，Gateway 调用上游时必须从当前 Provider 的健康 Key 中选择一个，并在限流类错误后临时摘除该 Key。

设计自审 2（安全与兼容）：现有 `provider_credentials.encrypted_key` 是不可解的占位密文，无法支撑真实 KeyPool 调用。M0 将引入 AES-GCM 本地信封加密工具，数据库仍不存明文；seed 的旧占位 Key 继续兼容，但不可解密的历史占位 Key 不参与真实 KeyPool 选择。Provider Key 仍不能写入日志、响应、request_logs 或 usage_ledger。

参考边界：用户允许参考 New API 与 Sub2Api 源码。PRD-014 可参考它们“多渠道/多 Key、状态摘除、用量与鉴权分层”的产品思路，但本实现不复制外部源码，仍按本项目 `ProviderCredential`、`ProviderAdapter` 和统一错误码体系落地。

## 需求理解

管理员需要为一个 Provider 配置多个上游 API Key，用于分摊限流、手动禁用和临时摘除故障 Key。数据面调用时不应再只从 `METAHUB_PROVIDER_<CODE>_API_KEY` 读取单一 Key，而应优先选择当前租户、当前 Provider 下可用的 KeyPool Key；如果没有可解密/可用 Key，则回退到环境变量，保持本地开发和旧 seed 可运行。

限流摘除 M0 只做基础能力：当 Adapter 归一化出上游 429 或明确限流错误时，把本次使用的 Provider Key 标记为 `rate_limited` 并设置短期 `rate_limited_until`。后续请求选择 Key 时跳过 disabled/rate_limited 未过期 Key。

明确不做：

- M0 不做供应商余额同步。
- M0 不做智能权重、动态权重学习或成本优先 Key 选择。
- M0 不做复杂并发令牌桶或分布式限流。
- M0 不做 KMS/信封加密托管；仅提供本地 AES-GCM 加密边界，后续安全 PRD 替换。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `0007` 扩展 `provider_credentials` 为 KeyPool |
| 安全工具 | `shared-sdk/python/metahub_shared/db/security.py` | 新增加密/解密 Provider Key 的 AES-GCM 工具 |
| 控制面 API | `control-plane-api/src/metahub_control/providers/` | 增加 Provider KeyPool DTO、service、routes |
| 数据面 KeyPool | `gateway-data-plane/src/metahub_gateway/providers/keypool.py` | 新增 Key 选择、状态更新、环境变量 fallback |
| Provider Adapter | `gateway-data-plane/src/metahub_gateway/providers/` | Adapter 请求构造使用 KeyPool 选择出的 Authorization |
| 控制台 | `console-web/src/features/providers/` | 在 Provider 管理面板中展示 KeyPool 和创建/禁用入口 |
| 测试 | `control-plane-api/tests/`、`gateway-data-plane/tests/`、`console-web/tests/` | 覆盖加密、Key 选择、禁用和限流摘除 |

## 数据表/字段设计

扩展现有 `provider_credentials`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `name` | TEXT | NULL | 管理台展示名 |
| `key_prefix` | TEXT | NULL | 安全前缀，如 `sk-xxx` 前 12 位 |
| `weight` | INTEGER | NOT NULL DEFAULT 100 | M0 只用于稳定排序和后续扩展 |
| `last_used_at` | TEXT | NULL | 最近被数据面选择时间 |
| `last_error_at` | TEXT | NULL | 最近上游错误时间 |
| `last_error_code` | TEXT | NULL | 最近归一化错误码 |
| `last_error_message` | TEXT | NULL | 脱敏错误摘要 |
| `rate_limited_until` | TEXT | NULL | 临时摘除截止时间 |
| `created_by` | TEXT | NULL | 创建人 user_id |
| `disabled_at` | TEXT | NULL | 手动禁用时间 |
| `disabled_by` | TEXT | NULL | 手动禁用人 |

状态约束扩展：

- `active`
- `disabled`
- `rate_limited`

索引：

- `idx_provider_credentials_status`：`(tenant_id, provider_id, status)`
- `idx_provider_credentials_rate_limit`：`(tenant_id, provider_id, rate_limited_until)`

加密：

- `encrypted_key` 保存 AES-GCM 加密串：`aesgcm-v1:<nonce_b64>:<ciphertext_b64>`。
- 加密密钥来自 `METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET`，本地 dev/test 可回退 `METAHUB_AUTH_TOKEN_SECRET`；prod 必须显式配置。
- `key_fingerprint` 继续保存不可逆短指纹，用于重复校验和排障。

## API设计

所有接口位于 Control Plane API，必须走统一 request_id/trace_id 和错误结构。

### GET /providers/{id}/keys

权限：admin。

返回当前租户指定 Provider 的 Key 元数据，不返回明文或可解密密文。

```json
{
  "provider_keys": [
    {
      "provider_key_id": "provider_key_...",
      "tenant_id": "tenant_...",
      "provider_id": "provider_...",
      "name": "prod key 1",
      "key_prefix": "sk-prod-abc",
      "key_fingerprint": "6ac1...",
      "weight": 100,
      "status": "active",
      "last_used_at": null,
      "last_error_at": null,
      "last_error_code": null,
      "last_error_message": null,
      "rate_limited_until": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /providers/{id}/keys

权限：admin。

请求：

```json
{
  "name": "prod key 1",
  "plain_value": "sk-...",
  "weight": 100
}
```

规则：

- 明文只在请求体中出现，不在响应中回显。
- 落库前立即生成 `key_prefix`、`key_fingerprint` 和 `encrypted_key`。
- 同一租户同一 Provider 下重复 `key_fingerprint` 返回冲突。

### PATCH /provider-keys/{id}

权限：admin。

可更新字段：

- `name`
- `weight`
- `status`

`status=disabled` 写 `disabled_at/disabled_by`；`status=active` 清理手动禁用和限流字段；M0 不允许管理员手动设置 `rate_limited`，该状态由数据面限流摘除逻辑写入。

## 前端页面/交互设计

在现有 Provider 管理面板中新增 KeyPool 折叠/行内区域：

- admin 可见 KeyPool 列表和创建表单。
- developer/viewer 不显示 Provider 面板，后端也拒绝 KeyPool API。
- 创建表单字段：Key 名称、明文 Key、权重。
- 创建成功后只显示元数据：名称、前缀、指纹、权重、状态、最近使用、最近错误、限流截止。
- 明文 Key 不在 DOM、sessionStorage、localStorage 中保存。
- 操作：启用/禁用 Key。

M0 不新增独立路由页，先挂在 Provider 管理区域；后续页面系统成熟后再拆成 Provider KeyPool 配置页。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `provider_key.not_found` | 404 | 当前租户找不到 Key |
| `provider_key.create_invalid` | 422 | 创建 payload 非法 |
| `provider_key.name_invalid` | 422 | 名称为空或过长 |
| `provider_key.plain_value_invalid` | 422 | 明文 Key 为空或格式明显非法 |
| `provider_key.weight_invalid` | 422 | 权重非正数或过大 |
| `provider_key.status_invalid` | 422 | 状态非法 |
| `provider_key.duplicate` | 409 | 同一 Provider 下 Key 指纹重复 |
| `provider_key.update_empty` | 422 | PATCH 无有效字段 |
| `provider_key.encryption_failed` | 500 | 加密配置缺失或加解密失败 |
| `provider_key.unavailable` | 503 | 数据面无可用 Provider Key 且无环境变量 fallback |

事件：

- `provider_key.created`
- `provider_key.disabled`
- `provider_key.rate_limited`

事件 payload 不包含明文 Key 或 encrypted_key。

## 安全与权限约束

- 所有 KeyPool API 仅 admin 可调用，后端使用 `require_current_admin`。
- 所有 SQL 必须带认证上下文中的 `tenant_id`。
- 响应不返回 `plain_value`、`encrypted_key` 或 Authorization。
- 日志只允许记录 `provider_key_id`/`provider_credential_id`、`key_prefix`、`key_fingerprint`、状态和错误码。
- 数据面选择 Key 后只把明文放进上游请求头，不写入日志或账本。
- 禁用 Key、rate_limited 未过期 Key 不参与路由。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD014-1 | admin 创建 Provider Key | 返回元数据，不回显明文，数据库无明文 |
| TC-PRD014-2 | admin 列表读取 KeyPool | 返回当前 Provider 多个 Key 元数据 |
| TC-PRD014-3 | admin 禁用 Key | 禁用后数据面不选择该 Key |
| TC-PRD014-4 | 数据面选择健康 Key | 上游收到选中 Key 的 Authorization |
| TC-PRD014-5 | 限流摘除 | 上游 429 后 Key 进入 rate_limited，并在截止时间前不参与选择 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD014-6 | developer/viewer 调用 KeyPool API | 返回 `auth.permission_denied` |
| TC-PRD014-7 | 重复 Key | 返回 `provider_key.duplicate` |
| TC-PRD014-8 | 非法明文/权重/状态 | 返回对应 `provider_key.*` 错误码 |
| TC-PRD014-9 | 无可用 Key | 数据面返回 `provider_key.unavailable` 或回退环境变量 |
| TC-PRD014-10 | 日志/数据库安全 | 明文 Key 不出现在响应、日志、request_logs、usage_ledger 或数据库 dump |

## 代码修改计划

1. 新增迁移 `0007_provider_keypool_metadata.sql` 和 down 脚本。
2. 扩展 `security.py`，增加 AES-GCM `encrypt_provider_key/decrypt_provider_key`。
3. 新增 `provider_key.*` 错误码。
4. 扩展控制面 Provider models/service/routes，增加 KeyPool API。
5. 新增数据面 `providers/keypool.py`，实现健康 Key 选择、last_used_at 更新、rate_limited 摘除和环境变量 fallback。
6. 修改 Adapter 构造请求时使用 KeyPool 选择出的 Authorization。
7. 控制台 Provider 面板增加 KeyPool 元数据展示和创建/启停表单。
8. 增加后端集成测试、Gateway 单元/集成测试、前端 Node tests。
9. 更新 README、目录边界和测试说明。
10. 运行 `scripts/ci/check.ps1` 并做真实 HTTP/browser 验收。

## 完成记录

- 数据库：新增 `0007_provider_keypool_metadata.sql` 与 down 脚本，扩展 `provider_credentials` 的权重、状态、最近错误、限流摘除和审计字段。
- 控制面：新增 `GET /providers/{id}/keys`、`POST /providers/{id}/keys`、`PATCH /provider-keys/{id}`，全部由 `require_current_admin` 守卫。
- 数据面：新增 KeyPool 选择与错误回写逻辑，Adapter 构造请求时使用当前租户和 Provider 的健康 Key；上游 429 后写入 `rate_limited`。
- 前端：Provider 管理面板内嵌 KeyPool 创建、列表和启停操作。
- 测试：已覆盖迁移、Key 加密落库、控制面权限/异常、数据库 Key 选择、禁用 Key 跳过、429 摘除、OpenAI-compatible API 回归和前端 KeyPool 渲染/API client。
