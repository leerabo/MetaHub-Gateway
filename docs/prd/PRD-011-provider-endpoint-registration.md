# PRD-011 - 模型 Endpoint Provider 注册设计

状态：已完成编码与验证。

设计来源：Excel `PRD-011 - 模型Endpoint Provider注册`、页面清单 `PAGE-017/PAGE-018`、API 清单 `POST /providers`、`GET /providers`、`PATCH /providers/{id}`、`POST /providers/{id}/test`、数据对象 `Provider`、事件 `provider.created` / `provider.health_checked`、验收用例 `TC-0021/TC-0022`。架构约束要求默认上游是模型厂家或云端模型 Endpoint，OpenRouter/硅基流动仅作为对标/迁移兼容对象；控制面低频管理 Provider，数据面通过 Provider Adapter 调用 Endpoint。

## 需求理解

本 PRD 的目标是让管理员在控制面注册模型 Endpoint Provider，例如智谱、通义、文心、火山、混元、DeepSeek 官方或云上服务，以及客户本地 OpenAI-compatible Endpoint。Provider 配置后，后续模型目录、路由、KeyPool 和 Adapter 可以引用这些上游 Endpoint。

核心范围：

- 控制面新增 Provider 列表。
- 控制面新增 Provider 注册能力：名称、类型、base_url、认证方式、状态、备注。
- Provider 类型面向模型厂家、云端 Endpoint、本地 OpenAI-compatible Endpoint。
- OpenRouter/硅基流动只能作为 `migration_compatible` 或备注类选项，不作为默认模型厂家分类。
- 支持 Provider 更新和禁用。
- 支持测试连接，返回成功/失败原因。
- 写结构化事件：`provider.created`、`provider.health_checked`。

明确不做：

- M0 不做供应商账户余额同步。
- M0 不做复杂 KeyPool 选择和健康摘除。
- M0 不做 Provider Credential 明文管理；Provider Key 仍按现有运行时环境变量或后续 KeyPool PRD 接入。
- M0 不做完整 Provider 价格、区域、SLA、配额管理。
- M0 不做 OpenRouter/硅基流动作为默认上游分类。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `0005` 扩展 `providers` 元数据字段 |
| 控制面 API | `control-plane-api/src/metahub_control/providers/` | 新增 Provider models、service、routes |
| 控制面权限 | `control-plane-api/src/metahub_control/auth/` | 复用 `require_current_admin`，Provider 管理仅 admin 可写 |
| Provider 测试 | `control-plane-api/src/metahub_control/providers/service.py` | 最小连接测试：URL 格式、HTTP 可达、OpenAI-compatible `/models` 或 `/chat/completions` 探测 |
| 数据面路由 | `gateway-data-plane/src/metahub_gateway/routing/` | PRD-011 不改路由行为，只让 Provider 元数据更完整 |
| 错误码 | `shared-sdk/python/metahub_shared/runtime/errors.py` | 新增 `provider.*` 管理错误码 |
| 控制台 | `console-web/src/features/providers/`、`console-web/src/lib/providers.js` | 新增 Provider 列表、注册表单、禁用和测试连接入口 |
| 测试与文档 | `control-plane-api/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖注册 2 个 Provider、连接测试成功/失败、禁用、权限和 base_url 校验 |

## 数据表/字段设计

当前 `providers` 表已有：

| 字段 | 现状 | 使用方式 |
| --- | --- | --- |
| `provider_id` | 主键 | 对外返回 id，模型表引用 |
| `tenant_id` | 已有 | 所有管理查询必须限制当前租户 |
| `provider_code` | 已有 | 租户内唯一 code，环境变量 Key/URL 读取也使用它 |
| `name` | 已有 | 展示名 |
| `base_url` | 已有 | 上游 OpenAI-compatible base URL |
| `status` | `active/disabled` | 数据面只路由 active Provider |
| `created_at` / `updated_at` | 已有 | 元数据展示 |

新增迁移 `0005_provider_metadata.sql`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `provider_type` | TEXT | NOT NULL DEFAULT `'openai_compatible'` | 厂商/Endpoint 类型 |
| `auth_type` | TEXT | NOT NULL DEFAULT `'bearer'` | 认证方式，M0 支持 `none`、`bearer`、`custom_header` |
| `description` | TEXT | NULL | 备注 |
| `health_status` | TEXT | NOT NULL DEFAULT `'unknown'` | `unknown/healthy/unhealthy` |
| `last_tested_at` | TEXT | NULL | 最近测试连接时间 |
| `last_test_error` | TEXT | NULL | 最近测试失败原因摘要，不含 Key |
| `created_by` | TEXT | NULL | 创建人 user_id |
| `disabled_at` | TEXT | NULL | 禁用时间 |
| `disabled_by` | TEXT | NULL | 禁用操作人 |

Provider 类型建议：

- `openai_compatible`：客户本地 OpenAI-compatible Endpoint 或兼容云端 Endpoint。
- `zhipu`、`dashscope`、`wenxin`、`volcengine`、`hunyuan`、`deepseek`：国内模型厂家或云服务类型。
- `custom`：无法归类但由客户自管的 Endpoint。
- `migration_compatible`：OpenRouter/硅基流动等迁移兼容或对标平台，不能作为默认推荐分类。

认证方式：

- `none`：本地无认证测试 Endpoint。
- `bearer`：使用 Bearer Token，M0 不在 Provider 表保存明文 Key。
- `custom_header`：预留自定义 Header，M0 仅保存类型，不保存密钥明文。

## API 设计

所有接口位于 Control Plane API，返回统一 `request_id` / `trace_id`，错误走统一错误结构。

### 权限

- Provider 管理是租户级高风险配置，M0 仅 `admin` 可创建、更新、禁用和测试。
- `developer` / `viewer` 直接调用返回 `auth.permission_denied`。
- 所有 SQL 必须带 `tenant_id = context.user.tenant_id`。

### POST /providers

请求：

```json
{
  "provider_code": "deepseek_official",
  "name": "DeepSeek 官方 Endpoint",
  "provider_type": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "auth_type": "bearer",
  "description": "生产候选 Provider"
}
```

响应：

```json
{
  "provider": {
    "provider_id": "provider_...",
    "tenant_id": "tenant_...",
    "provider_code": "deepseek_official",
    "name": "DeepSeek 官方 Endpoint",
    "provider_type": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "auth_type": "bearer",
    "status": "active",
    "description": "生产候选 Provider",
    "health_status": "unknown",
    "last_tested_at": null,
    "last_test_error": null,
    "created_at": "2026-07-08T10:00:00Z",
    "updated_at": "2026-07-08T10:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

规则：

- `provider_code` 租户内唯一，建议小写字母、数字、下划线。
- `base_url` 必须是 `http://` 或 `https://`，不能为空。
- `status` 创建时默认 `active`。
- 不接收 Provider Key 明文字段。
- 创建后写 `provider.created` 结构化日志，不含密钥。

### GET /providers

查询参数：

- `status=active|disabled` 可选。
- `provider_type=...` 可选。

响应为当前租户 Provider 列表，不返回任何 Provider Key。

### PATCH /providers/{id}

可更新：

- `name`
- `provider_type`
- `base_url`
- `auth_type`
- `description`
- `status`

规则：

- 目标 Provider 必须属于当前租户，否则返回 `provider.not_found`。
- `status=disabled` 后，数据面模型路由已在 PRD-009 中按 Provider active 状态拒绝。
- 已绑定模型的 Provider 可禁用，但后续模型调用会返回 `gateway.provider_not_found`，便于管理员显式下线。

### POST /providers/{id}/test

执行最小连接测试。

请求：

```json
{
  "timeout_seconds": 5
}
```

响应：

```json
{
  "provider": { "...": "..." },
  "test_result": {
    "status": "success",
    "http_status": 200,
    "latency_ms": 42,
    "message": "Endpoint is reachable.",
    "checked_at": "2026-07-08T10:05:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

测试策略：

- M0 优先请求 `${base_url}/models`，因为 OpenAI-compatible Endpoint 通常支持该接口。
- 如果 `/models` 返回 404，可退化为 base URL HEAD/GET 探测，只判断可达。
- 2xx/3xx 视为可达；401/403 视为认证失败但 Endpoint 可达，返回失败原因 `provider.auth_failed` 或测试结果失败，不泄露密钥。
- 连接失败、超时、非法 URL 返回失败结果，并更新 `health_status='unhealthy'`、`last_test_error`。
- 成功更新 `health_status='healthy'`、`last_tested_at`，并写 `provider.health_checked` 结构化日志。

## 前端页面/交互设计

### Provider 列表

控制台新增 “Provider 管理” 面板，仅 admin 可见：

- 展示名称、类型、base_url、认证方式、状态、健康状态、最近测试时间。
- 支持创建 Provider。
- 支持禁用/启用。
- 支持测试连接，展示成功/失败原因。

### Provider 配置页 / 表单

M0 以面板内表单实现：

- `provider_code`
- 名称
- Provider 类型下拉
- base_url
- auth_type 下拉
- 备注

前端文案明确：

- OpenRouter/硅基流动是对标/迁移兼容，不是默认模型厂家。
- Provider Key 不在此表单输入，后续 KeyPool/Credential PRD 管理。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `provider.not_found` | 404 | 当前租户找不到 Provider | 检查 Provider id 是否属于当前租户 |
| `provider.create_invalid` | 422 | 创建字段缺失或非法 | 检查 code/name/type/base_url/auth_type |
| `provider.code_invalid` | 422 | provider_code 格式非法 | 使用小写字母、数字、下划线 |
| `provider.code_exists` | 409 | 当前租户 code 已存在 | 换一个 provider_code |
| `provider.name_invalid` | 422 | 名称为空或过长 | 使用 1-120 字符名称 |
| `provider.type_invalid` | 422 | provider_type 非允许值 | 选择支持的 Provider 类型 |
| `provider.auth_type_invalid` | 422 | auth_type 非允许值 | 选择 none、bearer 或 custom_header |
| `provider.base_url_invalid` | 422 | base_url 非 http/https | 填写合法 Endpoint base URL |
| `provider.status_invalid` | 422 | status 非 active/disabled | 选择 active 或 disabled |
| `provider.update_empty` | 422 | PATCH 无可更新字段 | 至少传入一个可更新字段 |
| `provider.test_failed` | 200 或 502 | 测试连接失败 | 查看 test_result.message |
| `auth.permission_denied` | 403 | 非 admin 访问 Provider 管理 | 联系管理员 |

## 安全与权限约束

- Provider 管理接口仅 admin 可用。
- 请求体不接受 Provider Key 明文。
- Provider Key、Authorization、Token 不允许写入日志或响应。
- 所有 Provider 查询/更新限制当前 `tenant_id`。
- 跨租户 Provider id 返回 `provider.not_found`。
- 测试连接只记录 URL、HTTP 状态、耗时和错误摘要，不记录请求头密钥。
- OpenRouter/硅基流动不能作为默认 Provider 类型推荐，避免违背项目边界。
- `base_url` 校验必须阻止空字符串和非 http/https scheme；M0 暂不做 SSRF 完整防护，后续安全 PRD 增强内网地址策略。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0021-1 | admin 创建 DeepSeek Provider | 返回 active Provider，字段完整 |
| TC-0021-2 | admin 创建本地 OpenAI-compatible Provider | 可配置至少 2 个 Provider |
| TC-0021-3 | 列表读取 | 只返回当前租户 Provider |
| TC-0021-4 | 测试连接成功 | 返回 success、latency、checked_at，health_status=healthy |
| TC-0021-5 | 测试连接失败 | 返回失败原因，health_status=unhealthy |
| TC-0021-6 | 禁用 Provider | Provider status=disabled，后续绑定模型调用被拒 |
| TC-0021-7 | 控制台 Provider 管理 | admin 可创建、查看、测试、禁用 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0022-1 | developer/viewer 调用 Provider API | 返回 `auth.permission_denied` |
| TC-0022-2 | 非法 base_url | 返回 `provider.base_url_invalid` |
| TC-0022-3 | 重复 provider_code | 返回 `provider.code_exists` |
| TC-0022-4 | 非法 auth_type | 返回 `provider.auth_type_invalid` |
| TC-0022-5 | 非法 provider_type | 返回 `provider.type_invalid` |
| TC-0022-6 | 空 PATCH | 返回 `provider.update_empty` |
| TC-0022-7 | 跨租户 provider_id | 返回 `provider.not_found` |
| TC-0022-8 | 日志脱敏 | 测试连接和创建日志不包含 Provider Key |

## 代码修改计划

审核通过后按以下顺序编码：

1. 新增迁移 `0005_provider_metadata.sql` 和 down 脚本，扩展 `providers` 元数据字段。
2. 更新 seed，使默认 Provider 写入 `provider_type='openai_compatible'`、`auth_type='bearer'`。
3. 扩展错误码注册表，新增 `provider.*` 管理错误码。
4. 新增控制面 `providers` 包：Pydantic models、service、routes。
5. 在 `control-plane-api/src/metahub_control/app.py` 注册 `/providers` router。
6. Provider 管理接口使用 `require_current_admin`，确保 developer/viewer 后端拒绝。
7. 实现 Provider 连接测试：HTTP 可达、状态码、耗时、失败原因、health_status 更新。
8. 新增后端集成测试：创建 2 个 Provider、列表、更新/禁用、测试成功/失败、权限拒绝、base_url 校验、重复 code。
9. 控制台新增 `providers.js`、Provider 面板、创建表单、测试连接和禁用交互。
10. 新增前端 Node test：Provider API 客户端、列表渲染、admin 可见、developer/viewer 不渲染、错误展示。
11. 更新 README、目录边界和 `docs/testing/PRD-011-tests.md`。
12. 运行 `scripts/ci/check.ps1`，重启本地服务，真实 HTTP + 浏览器验证：创建 2 个 Provider、测试连接成功/失败、禁用 Provider。

## 实现结果

- 新增数据库迁移 `0005_provider_metadata.sql`，扩展 `providers` 的 `provider_type`、`auth_type`、`description`、`health_status`、`last_tested_at`、`last_test_error`、`created_by`、`disabled_at`、`disabled_by` 字段，并提供回滚脚本。
- seed 默认 Provider 写入 `provider_type='openai_compatible'`、`auth_type='bearer'`、`health_status='unknown'`，继续保证 Provider Key 不明文落库。
- 控制面新增 `/providers` API：
  - `GET /providers`
  - `POST /providers`
  - `PATCH /providers/{provider_id}`
  - `POST /providers/{provider_id}/test`
- Provider API 统一复用 `require_current_admin`，developer/viewer 直接调用会返回 `auth.permission_denied`。
- Provider 连接测试优先访问 `${base_url}/models`，404 时退化为 base URL 可达性探测；成功写入 `healthy`，失败写入 `unhealthy` 与脱敏错误摘要。
- 控制台新增 admin-only Provider 管理面板，支持创建 Provider、测试连接、启用/禁用，并展示健康状态与最近测试结果。
- 新增前后端测试覆盖主流程、连接失败、权限拒绝、字段校验、重复 code、空 PATCH、迁移回滚和前端 HTML 转义。

## 验证记录

- `python -m pytest control-plane-api/tests/integration/test_providers.py control-plane-api/tests/integration/test_db_migrations.py -q`：7 passed。
- `npm --prefix console-web test -- providers.test.mjs`：36 passed。
- `$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; .\scripts\ci\check.ps1`：后端 49 passed，前端 36 passed，console lint 32 files ok。
- 真实 HTTP 验收：管理员创建 2 个 Provider；`/providers/{id}/test` 成功分支返回 `success/healthy`，失败分支返回 `failed/unhealthy`；禁用 Provider 后返回 `status='disabled'` 且 `disabled_at` 非空。
- 真实权限验收：developer 直接调用 `GET /providers` 返回 403 `auth.permission_denied`。
- 浏览器验收：控制台 admin 登录后显示 Provider 管理面板；页面创建 `prd011_browser_*` Provider 成功，连接测试后显示 `healthy` 和 `Endpoint is reachable.`，禁用后行状态变为 `disabled` 且按钮变为“启用”；浏览器 warn/error 日志为空。
