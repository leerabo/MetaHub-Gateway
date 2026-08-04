# PRD-062 - 配置导入导出 CLI

## 1. 需求理解

### 1.1 权威来源与开发顺序

Excel `01_PRD总表!A63:R63` 与 `02_M0-M1核心开发!A63:R63` 冻结本项为：

| 字段 | 冻结值 |
| --- | --- |
| PRD_ID | `PRD-062` |
| 阶段 / 优先级 / 能力层 | `M1-企业POC / P1 / 核心层` |
| 模块 / 名称 | `迁移工具CLI / 配置导入导出CLI` |
| 用户 | 开发者 / 交付 |
| 范围 | 导入 Provider、Model、Alias、Virtual Key 名称；导出当前范围内配置 JSON |
| 明确边界 | 不导入明文旧 Key；不自动解析客户代码库 |
| CLI | `gatewayctl config import`、`gatewayctl config export` |
| 核心对象 | `ConfigExport`、`ImportReport` |
| 事件 | `config.imported` |
| 验收 | 导入前校验；失败提供行级错误；导出不包含密钥明文 |
| 依赖 | Provider、Model、Alias |
| 测试重点 | 兼容、密钥安全 |

PRD-061 已完成，仓库中不存在 PRD-062 设计、测试记录、`gatewayctl` 或 0054 迁移，因此本项是按
`M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3` 顺序的下一条未完成需求。总体 108 条目标仍在进行，
本设计不得表述为全部完成。

### 1.2 架构约束

系统架构文档要求：

1. 控制面负责配置管理、版本与变更留痕，数据面只消费控制面发布的配置；CLI 不能绕过 Control API
   直接连接数据库。
2. 配置变更必须先校验、后保存、再编译/下发，并记录版本、变更人和原因；本 PRD 以不可变
   `config_imports` 事实和 `config.imported` 事件承载一次批量变更身份。
3. OpenRouter、硅基流动只作为迁移兼容对象，不作为默认模型厂家；Provider 类型继续使用现有国内厂家、
   云服务、本地 OpenAI-compatible Endpoint 及 `migration_compatible` 枚举。
4. 每一项持久业务事实都由认证上下文绑定 `tenant_id`。导入文件中的来源租户只作脱敏来源元数据，
   不能决定目标租户。
5. 后端按数据库权限快照执行授权；CLI 参数、前端可见性和角色名称都不能替代权限检查。
6. Provider Key、Session Token、Virtual Key、Prompt、Response 和 URL 凭据不得进入普通日志、审计、
   Webhook、导出文件或测试证据。

### 1.3 已有能力与真实缺口

现有仓库已经具备：

- Provider、Model、Model Alias、Virtual Key 的租户级 CRUD 与稳定领域错误码；
- Provider URL 安全校验、Virtual Key 安全随机生成与不可逆哈希；
- Alias 创建时的默认 RoutingPolicy、FallbackPolicy、审计及路由配置事件；
- 数据库权限快照、统一 `request_id/trace_id`、AuditLog 和 Webhook 投递；
- `metahub-db` 数据库 CLI，但没有面向租户配置迁移的 `gatewayctl`。

真实缺口是跨四个对象的可移植 JSON 合同、全量预校验、单事务导入、行级安全错误、一次性新 Key 交付、
确定性脱敏导出和命令行网络客户端。直接循环调用现有四组 CRUD 会形成逐行提交和半成功状态，不符合本 PRD。

### 1.4 实现范围

本 PRD 实现：

1. `ConfigExport v1`，导出当前租户 Provider、Model、Alias 基础映射和 Virtual Key 非密钥元数据。
2. Control API 的导出、预校验和原子导入接口。
3. `gatewayctl config export/import` 薄客户端；只通过 Bearer Session 调用 Control API。
4. 导入前完整结构、引用、冲突、权限和数据库快照校验；失败返回稳定行号、对象类型、字段和错误码。
5. Virtual Key 导入只接受名称与安全绑定，创建全新随机 Key；旧 Key 或任意凭据字段一律拒绝。
6. 0054 迁移、专用迁移权限、不可变 `config_imports/config_import_rows/config_exports` 事实、AuditLog
   摘要和 `config.imported` Webhook。
7. 专项、异常、权限、租户、迁移、CLI 和真实运行验收。

### 1.5 明确不做

- 不解析客户代码仓库、环境变量、`.env`、OpenRouter/LiteLLM 私有文件或云厂商控制台导出。
- 不导入 Provider Credential、旧 Virtual Key 明文/哈希/前缀、Session Token 或 Webhook Secret。
- 不迁移 Request、Trace、Usage、Cost、Audit 历史。
- 不导入 DLP、预算、完整策略 DSL、KeyPool 或高级 RoutingPolicy；Alias v1 只迁移别名、环境、默认/备用模型
  和备用触发条件。后续格式通过新 schema version 扩展，不能静默猜测。
- 不在 CLI 内执行数据库迁移、直连 SQLite/PostgreSQL 或自动重启 Gateway。
- 不以文件中的 UUID 建立目标引用，不接受文件指定目标 `tenant_id`。

## 2. 涉及模块

| 模块 | 修改 |
| --- | --- |
| `control-plane-api/.../config_transfer/` | 新增 models、严格 JSON 解析、导出/校验/导入服务与路由 |
| `control-plane-api/.../auth/` | 新增 `config.transfer.read/write` 后端依赖 |
| `control-plane-api/.../app.py` | 注册 `/config/export`、`/config/import/validate`、`/config/import` |
| `shared-sdk/python/metahub_shared/gatewayctl/` | 新增标准库 HTTP CLI、退出码、脱敏输出和安全文件写入 |
| `shared-sdk/python/metahub_shared/rbac.py` | 权限目录提升到 revision 2，并定义默认可授权角色集合 |
| `shared-sdk/.../runtime/errors.py` | 新增 `config_transfer.*` 统一错误码 |
| `shared-sdk/.../runtime/webhooks.py` | 注册并严格校验 `config.imported` 安全事件 |
| `control-plane-api/.../audit/service.py` | 注册 `config.imported -> config_import` 和摘要白名单 |
| `infra/migrations/0054_*` | 新增不可变导入事实、索引，并扩展 AuditLog action 约束 |
| `scripts/gatewayctl.ps1` | Windows 源码运行包装，不保存 Token |
| `control-plane-api/tests/`、`shared-sdk/python/tests/` | API、服务、权限、租户、密钥与 CLI 测试 |
| `docs/api/config-transfer.md`、`docs/development/gatewayctl-config-transfer.md` | 公开 HTTP 合同与交付 CLI 用法 |
| `docs/testing/`、README、目录说明 | 验收完成后登记测试证据；开发中只记录真实落盘状态，不提前标记完成 |

Console 不新增页面。Excel 的入口是“迁移工具说明”，本项通过开发文档和 CLI 提供交付工作流，避免制造与
现有 Provider/Model/Alias/Virtual Key 页面重复的配置界面。

## 3. 数据对象与 JSON 合同

### 3.1 ConfigExport v1

导出 API 响应为：

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
  "manifest_sha256": "64位小写十六进制",
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

`gatewayctl config export` 只把 `config_export` 写入目标 JSON；HTTP envelope 的导出事实 ID、manifest hash 和
追踪 ID 只进入命令结果摘要。数组按稳定业务键排序，数据库 UUID、创建人、时间、健康状态和运行统计均不进入
迁移文件。

### 3.2 ProviderExport

允许字段：

- `provider_code`：租户内稳定引用键；
- `name`、`provider_type`、`base_url`、`auth_type`、`description`、`maintenance_note`、`status`。

禁止 Provider Credential、KeyPool、健康错误、探测租约、URL userinfo/query/fragment。导出前再次执行 URL
安全投影；发现历史脏 URL 时整个导出失败关闭，不输出部分文件。

### 3.3 ModelExport

允许字段：

- `internal_model_id`：租户内稳定引用键；
- `provider_code`：引用 Provider，而不是源 `provider_id`；
- `provider_model_id`、`display_name`、`capabilities`、`context_window`；
- `input_price_per_1k`、`output_price_per_1k`、`currency`、`price_schema_version`、`description`、`status`。

价格使用现有非负数约束；JSON 禁止 NaN/Infinity。能力对象只接受现有布尔能力白名单。

### 3.4 AliasExport

允许字段：

- `alias`、`environment`、`default_internal_model_id`；
- 可选 `fallback_internal_model_id`；
- `fallback_conditions`、`routing_mode`、`quality_cost_config`、`description`、`status`。

环境使用 code，模型使用 `internal_model_id`。`routing_mode` 精确支持现有
`fixed/cost_first/availability_first/quality_cost`；质量成本配置使用现有五个整数基点/样本字段。版本号、
数据库候选 ID 和运行时 warning 不迁移；候选图由目标默认/备用模型重新编译。Cache Affinity 属于独立配置域，
不在本 PRD 范围，不能混入 Alias 行。

### 3.5 VirtualKeyExport 与新 Key 交付

允许字段：

- `name`、`environment`、`description`、`status`；
- 可选 `project_code`、`application_code`；
- 可选基础限流 `rpm_limit/tpm_limit/concurrency_limit/rate_limit_status`。

明确禁止 `virtual_key_id`、`key_prefix`、`key_hash` 和旧明文。幂等匹配键冻结为规范化
`(environment, name, project_code|null, application_code|null)`：零条创建，一条且全部规范字段相同记
`unchanged`，一条但字段不同记 conflict，多条记 ambiguous；不能新增全局 name 唯一索引破坏存量合法数据。

导入 active Virtual Key 时由 Control API 使用
系统安全随机源生成新 Key，只把哈希和短前缀落库。API 的一次性 `generated_virtual_keys` 与普通
`ImportReport` 分离；CLI 不把该字段写 stdout/stderr，而要求显式 `--generated-keys-output`，以独占创建、
仅授权主体可读的文件承接。CLI 必须在 apply 前完成路径、链接、已存在文件和可写性检查并预先打开独占文件
句柄，apply 成功后立即 write/flush/fsync；普通输出不得包含秘密。Windows 使用受保护 DACL，仅允许当前用户、
SYSTEM 和 Administrators 完全控制，并拒绝 UNC/链接路径；POSIX 额外验证当前用户所有且模式精确为 0600。
部署方仍必须把输出目录设为交付人员私有目录。

预检与 apply 使用不同数据库快照。只要输入中存在状态为 `active`（省略状态也按 `active`）的 Virtual Key，CLI
即使在 validate 时判定为 `unchanged`，也必须在 apply 前预留 `--generated-keys-output` 文件；否则并发删除目标
Key 后，apply 可能重新创建 Key 而没有安全句柄接收唯一一次明文。apply 最终没有生成 Key 时，CLI 删除未提交的
空预留文件；重放也不会留下空密钥文件。

disabled Virtual Key 仍生成不可恢复随机凭据并仅保存哈希，但不把无效明文写入交付文件；报告计入
`disabled_virtual_key_count`，后续由管理员显式创建/轮换可用 Key。

### 3.6 ImportReport

```json
{
  "status": "invalid|validated|applied|replayed",
  "valid": true,
  "applied": false,
  "manifest_sha256": "64位小写十六进制",
  "schema_version": "metahub.config/v1",
  "counts": {
    "providers_created": 1,
    "providers_unchanged": 0,
    "models_created": 2,
    "models_unchanged": 0,
    "aliases_created": 1,
    "aliases_unchanged": 0,
    "virtual_keys_created": 1,
    "virtual_keys_unchanged": 0,
    "generated_virtual_keys": 1,
    "disabled_virtual_keys": 0
  },
  "error_count": 0,
  "errors_truncated": false,
  "errors": [],
  "config_import_id": null,
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

`ImportRowError` 只含：

```json
{
  "object_type": "provider|model|alias|virtual_key|document",
  "row": 1,
  "field": "provider_code",
  "code": "provider.code_invalid"
}
```

行号按每个数组从 1 开始。错误按对象顺序、行号、字段、错误码确定性排序；最多返回 200 条，完整数量写
`error_count`，超出时 `errors_truncated=true`。未知或攻击者控制的 JSON 键不得原样回显；敏感字段统一报告
`field="credential"` 与 `config_transfer.import_sensitive_field`，不返回值、文件名、URL 或异常文本。

## 4. 数据表和迁移设计

规范 manifest hash 定义为：严格解析并完成默认值、大小写、空值和业务键归一化后的 ConfigExport 对象，明确
排除仅用于可读溯源的 `exported_at`，保留数组行顺序，对其余对象键排序，以 UTF-8、compact separators、
`allow_nan=false` 编码后计算 SHA-256。JSON 空白、对象键顺序和单纯重新导出的时间变化不改变 hash；数组重排、
来源租户或任何业务语义变化都会改变 hash。

### 4.1 0054_config_import_export_cli.sql

新增 `config_imports`：

| 字段 | 类型 / 约束 | 说明 |
| --- | --- | --- |
| `config_import_id` | TEXT PK | 服务端 UUID |
| `tenant_id` | TEXT NOT NULL FK | 目标租户，来自 AuthContext |
| `actor_user_id` | TEXT NOT NULL | 操作者，和 tenant 复合 owner trigger 校验 |
| `manifest_sha256` | TEXT NOT NULL CHECK | canonical manifest SHA-256，不保存正文 |
| `schema_version` | TEXT NOT NULL CHECK | 本期只允许 `metahub.config/v1` |
| 四类 `*_created_count/*_unchanged_count` | INTEGER NOT NULL CHECK >= 0 | 创建与相同配置复用数量 |
| `generated_virtual_key_count` | INTEGER NOT NULL CHECK >= 0 | 一次性交付数量 |
| `disabled_virtual_key_count` | INTEGER NOT NULL CHECK >= 0 | 无明文交付数量 |
| `request_id`、`trace_id` | TEXT NOT NULL | 调用链身份 |
| `event_id` | TEXT NOT NULL UNIQUE | `config.imported` 持久事件身份 |
| `created_at` | TEXT NOT NULL | UTC 时间 |

约束与索引：

- `UNIQUE(tenant_id, actor_user_id, request_id)` 支持精确重试；同 ID 不同 hash 返回 409。
- `INDEX(tenant_id, created_at DESC, config_import_id)` 支持审计定位。
- 复合 owner trigger 要求 actor 属于同一 tenant；禁止 UPDATE/DELETE，形成不可变事实。
- 所有计数总和设上限并禁止 bool/负数/溢出。

新增 `config_import_rows`，每个成功创建或 unchanged 行保存 `tenant_id/config_import_id/collection/row_number/
object_type/action/object_id/business_key_sha256`。它不保存业务名称、URL、description、输入 JSON 或凭据，
使用复合 owner FK、`UNIQUE(config_import_id, collection, row_number)` 和 UPDATE/DELETE 阻断 trigger。

新增 `config_exports`，只保存 tenant/actor/request/trace、schema、manifest hash、四类导出计数和时间；不保存
导出正文。导出与 `config.exported` AuditLog 在同一短写事务提交，随后才把已在一致性读快照构建的
ConfigExport 返回给客户端。

0054 把 RBAC `CATALOG_REVISION` 提升为 2，新增：

- `config.transfer.read`：读取并导出严格白名单配置；risk=`read`；
- `config.transfer.write`：create-only 批量导入及生成全新 Virtual Key；risk=`critical`。

两项默认可授权给 Admin、Developer、OpsAdmin。不可变 Admin 必须补齐两项；仍处于 revision 1 且 hash 精确
等于旧默认模板的 Developer/OpsAdmin 自动加入，租户已经自定义过的矩阵不得被迁移静默扩权，只增加可授权
目录项，由管理员显式授予。迁移逐角色推进 permission revision 和 matrix hash，认证不得回退硬编码默认值。

AuditLog action 白名单扩展为 `config.imported/config.exported`，对象类型统一 `config_transfer`，摘要只允许
schema、manifest hash、created/unchanged/导出计数。up/down 使用单一 `BEGIN IMMEDIATE/COMMIT`；up 在首项
持久 DDL 前复核 0053、RBAC revision、核心表/列/trigger，down 在存在任一 config transfer 或审计事实时失败
关闭，不能删除证据；空事实回滚时恢复 revision 1 模板、权限投影、角色关系和原矩阵 hash。

### 4.2 原子导入事务

apply 在单连接 `BEGIN IMMEDIATE` 中按以下顺序执行：

1. 复核精确重试和 manifest hash；
2. 对当前租户重新执行完整冲突、引用和状态校验；
3. 对四类业务键执行 create-only merge：缺失=`created`、规范字段全等=`unchanged`、不同=`conflict`；
4. Provider；
5. Model；
6. Alias、RoutingPolicy trigger、完整 mode/config、FallbackPolicy 和逐 Alias 路由配置事件；
7. Virtual Key、基础限流及新随机凭据哈希；
8. `config_imports/config_import_rows`；
9. 同事务 `config.imported` AuditLog；
10. 提交。

任何业务错误、唯一冲突、owner trigger、审计或数据库异常都回滚整个事务。Webhook 是提交后的外部副作用，
投递失败不能撤销已经提交的配置；持久 `event_id` 和 delivery 唯一约束避免精确重试重复发送。

## 5. API 设计

### 5.1 POST /config/export

- 权限：要求 `config.transfer.read`；该专用权限只开放白名单导出，不隐式开放四个普通 CRUD API。
- 租户：只从 Bearer Session 的 AuthContext 取得。
- 查询参数：无；请求体必须是严格空对象 `{}`，任何额外字段返回 `config_transfer.body_invalid`。
- 响应：`ConfigExportResponse`，HTTP 200，`Cache-Control: no-store`。
- 导出先使用单连接只读事务和一个 UTC `exported_at` 构建一致快照，再以短写事务保存 `config_exports` 和
  `config.exported` AuditLog；任一阶段失败均不返回部分正文。
- 导出正文在写事实前复用导入端 2 MiB、深度 8、200000 节点和单字符串 4096 字符门禁；超出门禁或当前状态
  无法由 v1 无损回导时统一返回 `409 config_transfer.export_state_invalid`，不能生成只可导出、不可回导的文件。
- `config_exports` 不保存正文，无法安全重放旧导出；同一 tenant、actor 和 `request_id` 再次导出一律返回
  `409 config_transfer.export_request_conflict`，调用方必须使用新的 request ID。

### 5.2 POST /config/import/validate

- 权限：要求 `config.transfer.write`；专用 create-only 能力不隐式放开普通 Provider/Model/Alias/VK API。
- 请求：严格 UTF-8 `application/json` 的 `ConfigExport`；最多 2 MiB、深度 8、200000 节点、单字符串 4096
  字符；拒绝重复键、控制字符、非有限数字和额外顶层字段。
- 处理：只读快照内完成结构、跨行、当前租户冲突与引用校验，不生成 Key、不写业务数据。
- 响应：HTTP 200 `ImportReport(status=validated|invalid)`；行级错误是业务报告，不用框架 422 取代。
- malformed body、错误 Content-Type、超限和无法安全解析使用统一错误 envelope。

### 5.3 POST /config/import

- 权限和请求合同与 validate 相同。
- 服务端始终重新执行完整校验；客户端先调用 validate 只是交付体验，不能作为信任凭据。
- 有行错误时 HTTP 200 返回 `status=invalid, applied=false`，数据库零业务变更。
- 成功时 HTTP 201 返回 `status=applied`、ImportReport 和一次性 `generated_virtual_keys`。
- 新 request 遇到业务键完全相同的行记 `unchanged`；任一字段不同或 VK 身份 ambiguous 均整批 invalid。
- 精确重试返回 HTTP 200 `status=replayed`，不再次返回 Virtual Key 明文；不同意图复用 request_id 返回 409。
  replay 的 HTTP envelope 与 ImportReport 使用本次调用的 `trace_id`，便于调用方排查本次重试；补投原
  `config.imported` Webhook 则继续使用首次提交事实中保存的原始 `trace_id`、event ID 和发生时间。
- 响应和错误均携带中间件生成/透传的 `request_id/trace_id`，并设置 `Cache-Control: no-store`。

### 5.4 config.imported Webhook

事件使用持久 `event_id=evt_<config_import_id>`，只包含：

- `config_import_id`、`schema_version`、`manifest_sha256`；
- 四类对象计数、generated/disabled Virtual Key 计数；
- actor/object/tenant/trace 使用统一信封字段。

禁止对象名称、URL、description、项目/环境名称、Key、Token、原始行和文件名。Shared SDK 对 exact keys、
整数范围、hash 格式、对象/actor/tenant/trace 一致性执行严格验证。首次提交使用当次 trace；精确重试触发
Webhook 补投时保留首次提交的原 trace，不把本次 HTTP 重试 trace 改写进不可变业务事件。

## 6. CLI 设计

### 6.1 命令

```text
gatewayctl config export [--output FILE|-] [--force] [--control-url URL] [--token-stdin]
gatewayctl config import --input FILE [--generated-keys-output FILE] [--dry-run]
                         [--control-url URL] [--token-stdin]
```

源码环境通过 `scripts/gatewayctl.ps1` 启动 `python -m metahub_shared.gatewayctl.cli`。Token 只从
`METAHUB_CONTROL_TOKEN` 或显式 `--token-stdin` 读取，不提供 `--token` 参数，避免进入 shell history、进程列表
或测试命令行。CLI 不接受 tenant 参数。

### 6.2 网络与文件安全

- 默认 Control URL 为 `http://localhost:8001`；非 loopback 只允许 HTTPS。
- 拒绝 URL userinfo、query、fragment、非 HTTP(S) scheme 和重定向到不同 origin；Authorization 只发给冻结 origin。
- 输入必须是普通非链接文件，最多 2 MiB；不会扫描目录、客户代码库或环境文件。
- export 输出以同目录临时文件、flush/fsync、原子 replace 写入；已存在目标默认拒绝，只有 `--force` 才覆盖；
  目标是 stdout 时只输出已脱敏 ConfigExport。
- generated keys 文件必须不存在且不是链接，以 exclusive create 写入；POSIX 模式 0600，Windows 验证 ACL/owner
  能力，无法证明安全时失败关闭。普通结果永不包含 `plain_value`。
- HTTP、JSON、文件和未知异常都折叠为固定 CLI 错误码，不打印 exception、响应正文、Token、输入内容或堆栈。

### 6.3 两阶段工作流与退出码

import 默认：本地结构门禁 -> `/validate` -> 对潜在 active Key 安全预留 secret 目标 -> `/import` -> 有新 Key 时
提交 secret 文件，否则删除空预留 -> 输出报告。`--dry-run` 只执行前两步且不能与
`--generated-keys-output` 同用。验证失败不请求 apply。

| 退出码 | 语义 |
| --- | --- |
| 0 | 导出、dry-run 或 apply 成功 |
| 2 | CLI 参数、本地文件或 JSON 无效 |
| 3 | 401/403 认证权限失败 |
| 4 | 服务器行级校验、冲突或 request_id 复用冲突 |
| 5 | 网络、超时、5xx 或协议响应无效 |
| 6 | generated keys 安全文件无法创建/提交 |

stdout 只输出单行安全 JSON；stderr 只输出单行固定错误 JSON。两者均含可用的 request_id，未知网络失败使用 CLI
本地生成的 request_id。CLI 为 validate/apply 使用不同 request_id，并在最终摘要同时保留 validation_request_id。

## 7. 错误码设计

| 错误码 | HTTP | 使用场景 |
| --- | ---: | --- |
| `config_transfer.query_invalid` | 422 | export 出现未知查询参数 |
| `config_transfer.body_invalid` | 400 | Content-Type、UTF-8、JSON、重复键或结构无效 |
| `config_transfer.body_too_large` | 413 | 正文、节点或行数超限 |
| `config_transfer.schema_unsupported` | 422 | schema version 不支持 |
| `config_transfer.import_sensitive_field` | 422 报告码 | 文件含 Key/Token/Secret/Credential 类字段 |
| `config_transfer.import_invalid` | 422/报告码 | 文档级或无法归属行的业务错误 |
| `config_transfer.import_request_conflict` | 409 | 同 actor/request_id 复用为不同 hash |
| `config_transfer.import_unavailable` | 503 | 数据库、事务、审计或持久 schema 不可用 |
| `config_transfer.export_state_invalid` | 409 | 当前范围内配置含脏值或无法安全投影的状态 |
| `config_transfer.export_request_conflict` | 409 | 导出 request_id 已存在且旧正文不可安全重放 |
| `config_transfer.export_unavailable` | 503 | 导出快照或持久 schema 不可用 |

行级错误优先复用 Provider/Model/Alias/Virtual Key 的稳定领域码。传输级错误使用统一错误 envelope；正常
ImportReport 的 `invalid` 状态仍携带 request_id/trace_id，CLI 据此返回退出码 4。

## 8. 安全与权限约束

1. export/import 后端分别要求 `config.transfer.read/write`；权限来自本次请求加载的数据库 revision，不能按
   role string、CLI 参数或普通领域权限推导。
2. 所有 SELECT、唯一校验、引用解析、INSERT、AuditLog 和 config_imports 都显式匹配 AuthContext `tenant_id`；
   源 tenant、UUID 或业务 code 不能越过目标租户边界。
3. 导入拒绝任何 `tenant_id` 目标选择字段；`source_tenant_id` 只参与 canonical manifest hash，不进入 SQL
   owner 条件。
4. Provider/Model/Alias/Virtual Key 使用业务键解析，跨租户同名记录必须完全独立。
5. URL 只允许现有安全 scheme/host/port 规则，禁止 userinfo/query/fragment、回环外 HTTP 和 DNS/文件 scheme。
6. Key/Token 绝不进入导出、ImportReport、row error、AuditLog、Webhook、logger 或普通 CLI 输出。
7. 一次性 Virtual Key 明文只存在于 apply 响应内存和预先打开的显式 secret 文件；精确重试不重放。
8. JSON 使用 2 MiB、深度 8、200000 节点、单字符串 4096 字符的双端严格资源上限，禁止重复键覆盖、
   深度/节点放大、控制字符和非有限数；成功导出必须能通过同一组回导门禁。
9. 行级错误不回显用户值、未知键或底层异常；manifest hash 只对严格解析、归一化成功的安全对象计算。
10. validate 和 apply 都执行相同校验；apply 在写锁内重检，消除校验后并发变化导致的 TOCTOU。
11. 配置导入、审计事实和 source event 同事务；Webhook 网络副作用提交后执行。
12. 日志只允许 tenant、actor、config_import_id、计数、request/trace 和固定错误码，不记录路径和正文。

## 9. 测试用例

### 9.1 0054 迁移

- 0053 -> 0054 up、repeat no-op、down/re-up；fresh 0001..0054。
- `config_imports/config_import_rows/config_exports` 全列、CHECK、unique、复合 owner、不可变 trigger 和 query plan。
- RBAC revision 1 -> 2：Admin 必得两项；旧默认 Developer/Ops 自动加入；自定义矩阵不静默扩权；down 精确恢复。
- 同租户/跨租户 actor、重复 request、错误 hash/计数/时间/event ID 均由数据库失败关闭。
- AuditLog action 扩展保留旧行 rowid、列、索引、trigger 和 sequence；down 有 config import 事实时阻断。
- up/down/re-up 后 `integrity_check=ok`、`foreign_key_check` 为空，后续未知迁移时前置阻断。

### 9.2 导出主流程

- 两租户使用相同 Provider code、Model internal ID、Alias 和 Virtual Key name；只导出当前租户。
- Provider/Model/Alias/VK 字段白名单、完整四种 Alias mode、业务引用和确定性排序正确；往返空目标保持语义。
- 输出/API/日志/Webhook 扫描 Provider Key、VK 明文/hash/prefix、Session Token、Prompt/Response canary 为 0。
- 历史脏 URL/JSON/数值、缺表或查询故障整体返回统一错误，不产生部分文件或孤立 export 事实。

### 9.3 导入主流程

- canonical v1 文件通过 validate 后一次事务创建四类对象、Alias/完整路由模式/Fallback/限流、AuditLog、
  config_imports 和逐行安全事实。
- 部分目标已有完全相同配置时记 unchanged；不同配置、同 manifest 重复行和 VK ambiguous 时整批零写入。
- 目标 ID 全部新生成；引用只按 provider_code/internal_model_id/environment/project/application code 解析。
- active Virtual Key 新密钥可真实鉴权一次，旧 canary Key 不可用；数据库只含新 hash，报告不含明文。
- apply 精确重试返回同一 config_import_id，不重复对象、审计、事件或 secret。
- config.imported 事件 exact payload、签名、订阅和 delivery 幂等通过。

### 9.4 异常、权限与原子性

- 每类对象覆盖非法字段、重复行、当前库冲突、缺失/disabled 引用、Alias 自引用和项目绑定不一致。
- 至少一个错误时四类业务表、策略、限流、审计和 config_imports 均无新增；第 1/中间/最后写故障都回滚。
- 默认 admin/developer/ops_admin 通过；撤销 `config.transfer.read/write` 后旧 Session 立即 403；viewer/finance/
  security_admin 和匿名分别 403/401；普通四域权限不能替代 transfer 权限。
- 文件伪造 tenant_id、跨租户 ID/code、同名跨租户事实均不能读写另一租户。
- Key/Token/secret/authorization/provider credential 等大小写与嵌套变体均被拒绝且错误不回显值。
- 2 MiB、最大行数、深度、节点、字符串、重复键、控制字符、NaN/Infinity 边界逐一覆盖。
- request_id 同 hash 重试、不同 hash 冲突、不同 actor/tenant 独立；数据库 busy/损坏统一 503。

### 9.5 CLI

- export 到 stdout/文件；import dry-run/apply；stdout/stderr JSON 与退出码合同。
- Token 只从 env/stdin；进程参数、错误、mock transport、日志和报告扫描无 Token。
- 非 loopback HTTP、userinfo/query/fragment、跨 origin redirect、恶意 Content-Type/响应体均失败关闭。
- 输入/输出链接、目标已存在、权限无法收紧、磁盘写失败不触发 apply 或不泄露 secret。
- validate 失败不调用 apply；apply 协议异常不把原始响应打印；精确重试不覆盖已有 secret 文件。
- `scripts/gatewayctl.ps1` 从任意工作目录解析源码根，且不读取或枚举客户目录。

### 9.6 实际运行验收

- 开发库真实迁移到 0054，并确认 54 条连续迁移、checksum、integrity、foreign key 与关键 trigger。
- Gateway、Control API、Console 健康检查 HTTP 200；CLI 使用真实 admin Session 与 8001 通信。
- 从受控源租户导出，在受控目标租户 dry-run、apply、精确重试；目标配置可由 Control API 读取。
- 使用新生成 Virtual Key 对 Gateway 执行一次真实 OpenAI-compatible 请求，确认用量/失败链路仍可追踪。
- 使用缺权限用户执行 export/import 得到真实 403；跨租户 source/业务键不泄漏。
- 验收后撤销受控 Session、停用/删除受控目标业务数据或使用独立验收数据库，不让测试 Key 留在开发基线。
- 测试临时目录只使用 `D:/MetaHub/.artifacts/prd062-*`，pytest 固定 `-p no:cacheprovider`；不提权、
  不使用 `C:/tmp`，不读取、枚举、写入或删除 `D:/MetaHub/.tmp`。

## 10. 代码修改计划

1. 完成本文两轮编码前自审，冻结格式、安全、权限、事务和回滚边界。
2. 新增 0054 up/down 和迁移专项测试，先证明不可变事实与 AuditLog 保真。
3. 新增 `config_transfer` 严格 DTO/解析器、字段投影、导出、validate 和原子 importer。
4. 注册专用 transfer 权限、router、统一错误码、AuditLog action 和 `config.imported` Webhook。
5. 新增 `gatewayctl` 客户端和 PowerShell 包装，完成安全文件与退出码合同。
6. 完成专项测试和直接受影响回归，运行中文注释门禁。
7. 更新 API/CLI 文档、README、目录说明和 `docs/testing/PRD-062-tests.md`。
8. 迁移独立验收数据库，执行真实 Control/Gateway/CLI 主流程、异常和权限验收并清理受控凭据。

测试只覆盖 PRD-062 专项与直接受影响回归；局部失败先运行最小相关测试，不重复无关根级全量。所有命令
限定明确源码/测试路径，不使用仓库根递归枚举。

## 11. 第一轮编码前自审

状态：**修正后通过，尚未进入编码；等待第二轮独立自审**。

| 检查项 | 初稿风险 | 修正 |
| --- | --- | --- |
| 控制面边界 | 直接改库 CLI 会绕过 Session、权限、审计和事务 | 冻结薄 CLI + Control API，CLI 永不取得数据库路径 |
| 原子性 | 循环调用现有 CRUD 会逐条 commit，错误时留下半配置 | 专用 importer 在 `BEGIN IMMEDIATE` 中重检并一次提交四类对象、审计与事实 |
| 引用迁移 | 复用源 UUID 会跨租户错误关联或碰撞 | 所有引用改用 provider_code、internal_model_id、environment/project/application code |
| 旧 Key | 把旧 Key 放入导入文件、报告或 argv 会形成长期泄漏 | schema 明确拒绝所有 credential 字段；Virtual Key 只生成全新随机凭据 |
| 新 Key 交付 | 把新 Key 放普通 stdout/ImportReport 会被 CI 和日志采集 | 一次性 API 字段与报告分离，CLI 只写显式独占安全文件；重试不重放 |
| Virtual Key disabled | 为 disabled 行返回无用明文扩大泄漏面 | 仍落随机不可逆 hash，但不交付 disabled 明文，报告单独计数 |
| 后端权限 | 只检查 role 或根据文件对象动态猜权限会产生绕过 | 第一轮先选四读/四写组合并要求数据库快照；第二轮继续复核 persona 与最小授权 |
| 租户隔离 | 信任导出文件 tenant_id 会允许选择目标租户 | source tenant 仅是来源元数据；目标 tenant 唯一来自 AuthContext |
| 行级错误 | 回显原值/未知键可能把 Key 或攻击载荷写入 CLI/测试 | error 仅含固定对象、行号、白名单字段和稳定 code，最多 200 条 |
| JSON 攻击面 | 框架默认解析接受重复键且缺少统一资源上限 | 严格 UTF-8 流式限长、重复键/深度/节点/控制字符/有限数门禁 |
| 导出泄漏 | 序列化数据库整行会暴露 hash、prefix、健康错误或 actor | 每个对象显式字段白名单，业务键引用，历史脏值整体失败关闭 |
| URL 安全 | 迁移文件可把凭据放 URL 或诱导 CLI 出网 | Provider URL 复用安全校验；CLI origin 冻结且非 loopback 强制 HTTPS |
| Alias 兼容 | v1 静默丢弃高级路由会改变生产语义 | v1 冻结基础 Alias/Fallback；高级模式导出明确 409，不做降级 |
| 校验竞态 | CLI validate 成功后数据库可能在 apply 前变化 | apply 在写锁内完整重检；validate 只提供预览，不作为授权/信任凭据 |
| 幂等 | 网络超时重试可能重复创建对象和 Key | tenant+actor+request 唯一事实与 manifest hash；精确重试回读、不返 secret |
| 变更留痕 | 只写对象而无批次身份，难以追踪一次迁移 | 新增不可变 config_imports、同事务 AuditLog、持久 event_id 和安全 Webhook |
| 回滚证据 | down 直接删表会销毁导入和审计事实 | 有任何导入事实时 down 前置阻断；空表才允许保真回滚 |
| 测试范围 | 局部失败反复跑根级全量会浪费时间并触及无关路径 | 冻结专项 + 直接回归，命令只作用于明确目录，临时产物只放 `.artifacts/prd062-*` |

第一轮已把架构边界、密钥安全、租户、组合权限、全事务、行级错误、幂等与 CLI 文件交付问题写成可测试
合同。第二轮必须独立复核：generated keys 文件在 apply 前后的失败窗口、Alias trigger/策略一致性、
AuditLog 0054 up/down 保真、Webhook exact payload、原始 JSON hash 定义以及 API 200 invalid 语义。

## 12. 第二轮编码前自审

状态：**修正后通过，可以进入编码**。

| 检查项 | 第二轮发现 | 修正 |
| --- | --- | --- |
| Excel persona | 四读/四写组合会拒绝默认 Developer，而 Excel 明确指定开发者/交付 | 新增 `config.transfer.read/write`，只授权严格白名单导出和 create-only 导入；不放开普通 CRUD |
| 权限迁移扩权 | 把新权限无条件加入所有 Developer/Ops 会覆盖租户自定义矩阵 | 仅旧 revision/hash 精确等于默认模板时自动补齐；Admin 不可变补齐；自定义角色只增加可授权目录项 |
| 权限目录一致性 | 只改 Python catalog 会让认证的数据库模板逐字段复核失败 | 0054 同步 template、tenant projection、role relation、revision 和 matrix hash；up/down 做精确保真测试 |
| Virtual Key 业务键 | 数据库没有 name 唯一约束，仅按名称会把合法重复项误认为幂等 | 使用环境、名称、项目、应用四元身份；0/1/>1 分别 create/compare/ambiguous，不新增破坏存量的唯一索引 |
| create-only 兼容 | 只允许空目标会让交付重试和部分预置环境难以使用 | 每行冻结 created/unchanged/conflict；规范字段完全相同可复用，任一不同整批零写入 |
| Alias 语义 | 第一轮排除高级模式会使“导出当前 Alias”在现有四种模式下不完整 | v1 纳入四种 routing_mode、五字段 quality config 和 fallback conditions；版本与候选 ID 在目标重新生成 |
| Alias 持久不变量 | 仅插 alias 依赖 trigger 虽会生成 fixed Policy，但缺少 mode 更新、fallback 与配置事件 | 同事务执行 trigger 后的完整 Policy/Fallback 写入，并为每个 created Alias 生成确定性 child request 事件 |
| manifest hash | “原始规范 JSON”未说明空白、键顺序、默认值和导出时间，重试可能误冲突 | 对严格解析并归一化后的业务对象做 canonical UTF-8 JSON SHA-256；明确排除 `exported_at`，保留数组顺序，忽略无语义空白/键序 |
| 导出审计 | GET 只读导出没有持久批次，敏感配置文件的产生不可追踪 | 改为 `POST /config/export`，正文 `{}`；一致快照后同事务记录 `config_exports + config.exported` 安全摘要 |
| 行级追踪 | 只有批次计数无法证明每一行是新建还是复用，也无法安全定位导入对象 | 增加不可变 `config_import_rows`，只存行号、action、object ID 和业务键 hash，不存业务值 |
| 新 Key 丢响应 | apply 已提交但 CLI 尚未保存明文时崩溃，精确重试不能安全重放；validate/apply 两快照间 active Key 也可能由 unchanged 变 created | 只要清单含潜在 active Key，CLI 就在 apply 前完成全部路径检查并保持 exclusive 文件句柄；响应后立即 write/flush/fsync，无新 Key 时删除空预留；提交后落盘失败返回 6 和 import ID，不把秘密写普通输出 |
| Windows 文件权限 | POSIX 0600 不能直接等同 Windows DACL，继承父目录 ACL 可能把一次性 Key 暴露给额外主体 | Windows 拒绝链接和 UNC 路径，并写入受保护 DACL，只允许当前用户、SYSTEM、Administrators 完全控制；无法证明收紧成功即在 apply 前失败关闭 |
| disabled Key | 生成后丢弃 disabled 明文会留下不能安全启用的配置 | disabled 行不交付秘密，并在报告中独立计数；文档要求后续删除并用普通创建流程重建，不允许直接启用未知 Key |
| API invalid 语义 | 行错误若抛统一 422，现有 ErrorResponse 无法承载结构化行列表 | 解析/协议错误继续统一 envelope；完整解析后的业务预检返回 HTTP 200 `status=invalid`，CLI 退出 4，数据库零变更 |
| 事件最小化 | config.imported 若携带名称、URL 或原始行会复制敏感配置 | exact payload 仅含 import ID、schema、manifest hash、created/unchanged 计数和时间，Shared SDK 逐字段失败关闭 |
| 迁移 down | 删除 transfer 表或新权限会破坏导入、导出、审计和自定义矩阵证据 | 有事实时 down 前置阻断；空事实时精确恢复 revision 1 目录/角色 hash，未知后续依赖在 DDL 前阻断 |
| 测试路径 | 根级递归即使带过滤也会在过滤前尝试进入禁区 | 所有实现和测试命令只使用明确文件/目录；禁止仓库根递归，整个 `.tmp` 不访问、不枚举、不修改 |

第二轮对照了 Provider/Model/Alias/Virtual Key 的真实唯一约束、各 create service 的独立事务、Alias owner
trigger、RoutingPolicy 事件幂等键、0050 RBAC 数据库快照和现有一次性 Virtual Key 响应合同。修订后没有
发现编码前 P0/P1 阻断项。实现阶段必须先完成 0054/RBAC 和迁移专项，再实现 API 与 CLI；在专项测试、
实际 CLI/Gateway 运行和文档证据全部通过前，PRD-062 仍不得标记完成。

## 13. 实现期合同同步

状态：**完成；专项、直接回归、0054 实迁移和真实 CLI/Gateway 验收均通过**。

编码期交叉复核在不扩大 PRD 范围的前提下冻结以下细节：

1. JSON 节点上限由初稿的较低值调整为 200000。2 MiB 正文仍是权威硬上限；新上限用于覆盖 5000 行最大合法
   清单的字段节点，同时继续阻断解析后对象放大。
2. `exported_at` 仅用于人类溯源，明确排除在 manifest hash 之外；相同业务配置在不同时间导出得到相同 hash。
3. `config_exports` 不保存正文，服务端无法证明可重放旧导出文件；导出 request ID 复用新增稳定
   `409 config_transfer.export_request_conflict`，不能把当前快照伪装成旧请求结果。
4. 导出 DTO 在写 `config_exports` 和 AuditLog 前复用导入端字节、深度、节点、字符串和 UTF-8 门禁；超限或
   无法由 v1 无损表示统一映射为 `409 config_transfer.export_state_invalid`。
5. 精确导入 replay 的 HTTP envelope/ImportReport 表示本次调用，使用本次中间件 trace；`config.imported`
   Webhook 补投属于首次提交事实，继续使用原 event ID、发生时间和原 trace。
6. 对潜在 active Virtual Key，CLI 不只依赖 validate 的 created 计数，而是在 apply 前无条件预留安全文件；
   apply 没有生成明文时删除空预留。Windows 使用受保护 DACL，POSIX 使用当前用户所有的精确 0600。

上述同步项已经写入本设计的 JSON、API、CLI、错误码和安全章节，并由
`docs/api/config-transfer.md`、`docs/development/gatewayctl-config-transfer.md` 对外展开。最终专项、0054、
实际 source/target CLI、权限、租户、Gateway 调用及隐私证据见 `docs/testing/PRD-062-tests.md`。

## 14. 完成结论

PRD-062 已按本文完成设计、两轮编码前自审、实现、中文注释、专项和直接受影响回归。两套独立数据库均实际
迁移到 0054 并通过 checksum、完整性、外键和关键 trigger 复核；真实 `gatewayctl` 完成 source import/export、
target dry-run/apply/replay，Viewer 由后端拒绝，跨租户业务键不泄漏。导入生成的 Virtual Key 对 Gateway
完成失败追踪和 5 Token 成功调用，明文未进入数据库或日志。因此本 PRD 可以标记完成，但
`all_108_prds_complete=false`。
