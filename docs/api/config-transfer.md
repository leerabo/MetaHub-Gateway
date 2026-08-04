# 配置导入导出 API

本文冻结 PRD-062 当前实现中的配置迁移 HTTP 合同。接口面向 `gatewayctl` 和受控交付工具，迁移
Provider、Model、Alias 与 Virtual Key 非凭据元数据；它不解析客户代码库，不接受客户端指定目标租户，
也不导入或导出任何旧 Key、Token、Prompt、Response 或 Provider Credential。

PRD-062 仍处于实现与验收阶段。本文只描述已经落盘的合同，不代表专项测试、迁移验收或实际运行验收完成。

## 1. 认证、权限与租户

三个接口都必须携带有效 Control Plane Bearer Session。后端在每次请求从当前数据库权限快照授权：

| 接口 | 后端权限 |
| --- | --- |
| `POST /config/export` | `config.transfer.read` |
| `POST /config/import/validate` | `config.transfer.write` |
| `POST /config/import` | `config.transfer.write` |

权限依赖先于正文读取。匿名请求返回 401，已登录但缺少对应权限返回 403，即使正文畸形也不能利用校验差异
探测迁移合同。专用迁移权限不隐式开放 Provider、Model、Alias 或 Virtual Key 的普通 CRUD 权限。

目标 `tenant_id` 和操作者只来自认证上下文。清单中的 `source_tenant_id` 只是来源元数据，不能选择目标租户，
也不能进入目标资源的 owner 条件。所有查询、冲突判断、引用解析、写入、导入/导出事实和审计都以认证租户
为边界；另一租户即使使用相同业务 code 或名称也不会参与匹配。

0054 将 Permission 目录提升为 revision 2。Admin、仍保持旧默认矩阵的 Developer 与 OpsAdmin 会获得两项
默认授权；已经自定义的非 Admin 矩阵只新增可授予目录项，不会被迁移静默扩权。运行时始终以真实数据库关系
为准，不能根据角色名称推导权限。

## 2. 通用 wire 合同

### 2.1 请求约束

- 只接受 `application/json`，正文必须是严格 UTF-8 的单个 JSON object。
- 正文最多 2 MiB，解析深度最多 8，解析后最多 200000 个节点，任一字符串最多 4096 个字符。
- 拒绝重复 key、NaN、Infinity、指数溢出、C0/C1 控制字符、未配对 surrogate 和未知顶层字段。
- 四类集合上限分别为 Provider 500、Model 2000、Alias 2000、Virtual Key 2000，总行数最多 5000。
- 布尔值不是整数；浮点数、指数、数字字符串或其他类型不能被静默转换成整数字段。
- 三个接口都不接受查询参数。任何查询参数统一返回 `config_transfer.query_invalid`。

正文、未知字段名和解析器异常不会进入日志、错误 message、AuditLog 或 Webhook。容量、节点、字符串、
集合或总行数超限返回 413；内容或编码非法返回 400。

### 2.2 响应约束

所有成功与错误响应都设置 `Cache-Control: no-store`。成功响应在 Header、外层响应和内部报告之间维持同一
`request_id/trace_id`；错误使用平台统一 JSON envelope，也必须携带可追踪的 `request_id` 和 `trace_id`。
任何响应都不得返回 SQL、数据库路径、原始正文、文件名、URL 凭据或底层异常文本。

## 3. ConfigExport v1

导入正文与导出响应中的 `config_export` 使用以下顶层结构：

```json
{
  "schema_version": "metahub.config/v1",
  "source_tenant_id": "default",
  "exported_at": "2026-08-01T00:00:00Z",
  "providers": [],
  "models": [],
  "aliases": [],
  "virtual_keys": []
}
```

顶层字段必须精确匹配，不能提交目标 tenant、数据库 UUID、actor、request ID 或自定义扩展。
`exported_at` 必须是带时区时间，规范化为 UTC 秒格式；它只用于可读溯源，不参与 manifest hash。

manifest hash 基于完整归一化后的 `schema_version`、`source_tenant_id` 和四个有序集合计算。对象 key 排序，
使用 UTF-8 compact JSON 且禁止非有限数；数组顺序保留。因此 JSON 空白、对象 key 顺序和重新导出的时间变化
不会改变 hash，数组重排、来源租户或任一业务语义变化会改变 hash。

### 3.1 Provider 行

字段必须精确为：

- `provider_code`、`name`、`provider_type`、`base_url`、`auth_type`；
- `description`、`maintenance_note`、`status`。

禁止 Credential、KeyPool、健康错误和探测租约。`base_url` 继续执行现有 Provider URL 安全约束，不能包含
userinfo、query 或 fragment，回环外 HTTP 与非 HTTP(S) scheme 失败关闭。

### 3.2 Model 行

字段必须精确为：

- `internal_model_id`、`provider_code`、`provider_model_id`、`display_name`；
- `capabilities`、`context_window`；
- `input_price_per_1k`、`output_price_per_1k`、`currency`、`price_schema_version`；
- `description`、`status`。

Model 通过来源清单内的 `provider_code` 引用 Provider，不使用源数据库 ID。能力只接受现有布尔字段白名单；
价格必须是非负有限数。

### 3.3 Alias 行

字段必须精确为：

- `alias`、`environment`、`default_internal_model_id`、`fallback_internal_model_id`；
- `fallback_conditions`、`routing_mode`、`quality_cost_config`；
- `description`、`status`。

模型引用使用 `internal_model_id`。路由模式只接受 `fixed`、`cost_first`、`availability_first`、
`quality_cost`；质量成本配置必须包含现有五个整数基点/样本字段。数据库候选 ID、运行告警、Cache Affinity
和完整高级策略 DSL 不属于 v1。

### 3.4 Virtual Key 行

字段必须精确为：

- `name`、`environment`、`description`、`status`；
- `project_code`、`application_code`；
- `rpm_limit`、`tpm_limit`、`concurrency_limit`、`rate_limit_status`。

禁止 `virtual_key_id`、`key_prefix`、`key_hash`、`plain_value` 及所有 Key/Token/Secret/Credential 变体，
包括 camelCase 变体。服务端按规范化
`(environment, name, project_code|null, application_code|null)` 匹配当前租户：不存在则创建，字段完全相同
则记 `unchanged`，字段不同则冲突，多条匹配则 ambiguous。

新建 active Virtual Key 时生成全新安全随机 Key，数据库只保存哈希与短前缀；明文只在首次 apply 响应的
`generated_virtual_keys` 中出现一次。新建 disabled Key 仍保存不可恢复的随机凭据哈希，但不返回无效明文，
并计入 `disabled_virtual_keys`。

## 4. POST /config/export

请求体必须是严格空对象：

```json
{}
```

成功返回 HTTP 200：

```json
{
  "config_export": {
    "schema_version": "metahub.config/v1",
    "source_tenant_id": "default",
    "exported_at": "2026-08-01T00:00:00Z",
    "providers": [],
    "models": [],
    "aliases": [],
    "virtual_keys": []
  },
  "config_export_id": "<server-generated-id>",
  "manifest_sha256": "<64-lowercase-hex>",
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

服务先在一个只读事务和同一个 UTC `exported_at` 下构建一致快照，再用当前租户重新执行导入计划，要求每行
都可无损解释为 `unchanged`。形成 DTO 后还会复用导入端 2 MiB、深度、200000 节点和字符串门禁。历史脏值、
v1 无法表达的状态或超出可回导 wire 上限均返回 `409 config_transfer.export_state_invalid`，不输出部分正文。

成功清单对应的 `config_exports` 事实与 `config.exported` AuditLog 在短写事务内提交，数据库不保存导出正文。
因此同一 tenant、actor 和 `request_id` 无法重放旧文件，再次调用会返回
`409 config_transfer.export_request_conflict`；客户端必须使用新的 request ID。

导出只包含白名单字段。Provider Credential、Virtual Key 明文/哈希/前缀、Session Token、Prompt、Response、
运行统计和数据库 UUID 不会进入响应、事实或审计摘要。

## 5. POST /config/import/validate

请求体直接发送 ConfigExport v1，不使用额外 envelope。后端在当前认证租户的只读快照中完成：

- 精确结构、字段类型、领域约束与敏感字段扫描；
- 清单内重复业务键、跨集合引用和状态约束；
- 与当前租户存量配置的 create-only、unchanged、conflict 或 ambiguous 判断。

validate 不生成 Virtual Key、不写业务表、不写导入事实、不写 AuditLog，也不投递 Webhook。成功或业务无效
都返回 HTTP 200：

```json
{
  "report": {
    "status": "validated",
    "valid": true,
    "applied": false,
    "manifest_sha256": "<64-lowercase-hex>",
    "schema_version": "metahub.config/v1",
    "counts": {
      "providers_created": 0,
      "providers_unchanged": 0,
      "models_created": 0,
      "models_unchanged": 0,
      "aliases_created": 0,
      "aliases_unchanged": 0,
      "virtual_keys_created": 0,
      "virtual_keys_unchanged": 0,
      "generated_virtual_keys": 0,
      "disabled_virtual_keys": 0
    },
    "error_count": 0,
    "errors_truncated": false,
    "errors": [],
    "config_import_id": null,
    "request_id": "req_example_validate",
    "trace_id": "trc_example_validate"
  },
  "generated_virtual_keys": [],
  "request_id": "req_example_validate",
  "trace_id": "trc_example_validate"
}
```

业务校验失败时 `status=invalid`、`valid=false`、`applied=false`，仍使用 HTTP 200。行错误按对象类型、行号、
字段和错误码稳定排序；`error_count` 是完整数量，`errors` 恰好返回前 `min(error_count, 200)` 条，且仅当
`error_count > 200` 时 `errors_truncated=true`。未完成安全归一化的清单使用 64 个 `0` 作为固定 manifest
占位，避免脏值通过 hash 形成可比对侧信道。

单条错误只包含：

```json
{
  "object_type": "provider|model|alias|virtual_key|document",
  "row": 1,
  "field": "provider_code",
  "code": "provider.code_invalid"
}
```

数组行号从 1 开始，文档级错误使用行号 0。未知 key 不回显原名称；凭据语义统一折叠为
`field=credential` 与 `config_transfer.import_sensitive_field`。

## 6. POST /config/import

请求合同与 validate 相同。服务端不信任旧预检结果，会在单个 `BEGIN IMMEDIATE` 写事务内重新执行完整校验。

有行错误时返回 HTTP 200、`status=invalid`，四类业务表和导入事实均不变化。成功时返回 HTTP 201：

```json
{
  "report": {
    "status": "applied",
    "valid": true,
    "applied": true,
    "manifest_sha256": "<64-lowercase-hex>",
    "schema_version": "metahub.config/v1",
    "counts": {
      "providers_created": 1,
      "providers_unchanged": 0,
      "models_created": 0,
      "models_unchanged": 0,
      "aliases_created": 0,
      "aliases_unchanged": 0,
      "virtual_keys_created": 1,
      "virtual_keys_unchanged": 0,
      "generated_virtual_keys": 1,
      "disabled_virtual_keys": 0
    },
    "error_count": 0,
    "errors_truncated": false,
    "errors": [],
    "config_import_id": "<server-generated-id>",
    "request_id": "req_example_apply",
    "trace_id": "trc_example_apply"
  },
  "generated_virtual_keys": [
    {
      "virtual_key_id": "<server-generated-id>",
      "name": "delivery-key",
      "environment": "prod",
      "key_prefix": "<short-prefix>",
      "plain_value": "<returned-once>"
    }
  ],
  "request_id": "req_example_apply",
  "trace_id": "trc_example_apply"
}
```

一次事务依次写 Provider、Model、Alias 及其基础路由、Virtual Key、`config_imports`、逐行安全事实和
`config.imported` AuditLog。任一业务、owner、唯一性、审计或数据库错误都会回滚整个批次。事务提交后才投递
路由配置事件和 `config.imported` Webhook；外部投递失败不得撤销已经提交的配置，也不得阻断唯一一次 Key 响应。

### 6.1 精确重试

幂等身份为当前 tenant、actor 和 `request_id`：

- 相同 request ID 与相同规范 manifest 返回 HTTP 200、`status=replayed`；不再次返回任何 Key 明文。
- 相同 request ID 与不同 manifest 返回 `409 config_transfer.import_request_conflict`。
- replay 的 HTTP envelope 与 ImportReport 使用本次调用的 `trace_id`，表示本次重试链路。
- Webhook 补投继续使用首次提交事实中的 event ID、发生时间和原始 `trace_id`，不能用本次 HTTP trace 改写
  不可变业务事件。

## 7. 审计、事实与 Webhook

`config_exports` 只保存 tenant、actor、schema、manifest hash、四类计数、request/trace ID 和时间；不保存正文。
`config_imports/config_import_rows` 只保存批次身份、计数、行序、动作、对象 ID 和不可逆业务键 hash；不保存配置
名称、URL、description、原始行或凭据。

`config.imported` Webhook 使用持久 `event_id=evt_<config_import_id>`，payload 只包含 schema、manifest hash、
四类 created/unchanged 计数及 generated/disabled Key 计数。统一信封承载 tenant、actor、object、trace 和时间。
对象名称、Endpoint、项目/环境名称、Key、Token、文件名和输入正文都禁止进入事件。

## 8. 统一错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 400 | `config_transfer.body_invalid` | Content-Type、UTF-8、JSON、重复 key、控制字符或结构无效 |
| 401 | `auth.token_missing` / `auth.token_invalid` | 缺少、失效或已撤销 Session |
| 403 | `auth.permission_denied` | 当前数据库权限快照缺少专用导入/导出权限 |
| 409 | `config_transfer.import_request_conflict` | 导入 request ID 被同一 actor 用于不同 manifest |
| 409 | `config_transfer.export_state_invalid` | 当前配置不能由 v1 安全、完整且可回导地表达 |
| 409 | `config_transfer.export_request_conflict` | 导出 request ID 已使用，旧正文不可安全重放 |
| 413 | `config_transfer.body_too_large` | 字节、深度、节点、字符串、集合或总行数超限 |
| 422 | `config_transfer.query_invalid` | 任一迁移接口出现查询参数 |
| 422 | `config_transfer.schema_unsupported` | schema version 不支持；可作为行级报告码 |
| 422 | `config_transfer.import_sensitive_field` | 清单含凭据语义 key；作为行级报告码 |
| 422 | `config_transfer.import_invalid` | 文档或业务字段无效；主要作为行级报告码 |
| 503 | `config_transfer.import_unavailable` | 导入数据库、事务、审计或持久 schema 不可用 |
| 503 | `config_transfer.export_unavailable` | 导出快照、事实、审计或持久 schema 不可用 |

Provider、Model、Alias 与 Virtual Key 的字段级失败优先复用既有稳定领域码。`invalid` ImportReport 是正常业务
报告而非框架错误；调用方必须同时检查 HTTP 状态和 `report.status`。

## 9. 安全边界

- API 不接受目标 tenant、actor、源数据库 ID、旧 Key 或客户代码目录；CLI 也不能替代后端授权。
- 敏感 key 扫描先于 exact-shape 校验，覆盖 snake_case、kebab-case 和 camelCase 常见凭据变体。
- 成功导出必须通过与导入相同的 wire 资源门禁，防止生成无法回导的“成功”文件。
- validate 与 apply 是不同快照；apply 在写锁内重检，预检结果不构成并发安全凭据。
- 一次性 Key 只存在于首次 apply 响应内存；数据库、日志、AuditLog、Webhook 和重放响应均无明文。
- 配置迁移 AuditLog 不持久化 User-Agent，避免攻击者把凭据经可控 Header 写入审计。
- 普通日志只允许 tenant、actor、批次/事实 ID、计数、固定错误码和 request/trace ID。
