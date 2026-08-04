# PRD-003 - 数据库迁移与种子数据设计

状态：已实现。

实现说明：本文档先完成设计，随后按 PRD-003 范围实现 SQLite 可运行迁移框架、MVP 表、默认 seed、CLI、测试和文档。当前运行时无 PostgreSQL 驱动，生产 PostgreSQL 接入保留同一 CLI 和迁移语义。

## 需求理解

本 PRD 的目标是建立可版本化、可重复执行、可回滚验证的数据库迁移框架，避免研发和交付人员手工建表。

来自 Excel 的核心要求：

- 建立迁移框架。
- 创建 `Tenant`、`User`、`VirtualKey`、`Provider`、`Model`、`RequestLog`、`UsageLedger` 等 MVP 表。
- 提供种子租户、管理员、测试 Provider。
- 提供 CLI：`db:migrate`、`db:seed`。
- 空库执行迁移成功。
- 重复执行幂等。
- 测试种子数据可登录控制台。
- 不做分库分表，不做国产数据库兼容。

本 PRD 仍遵守项目边界：产品默认服务中国境内企业用户，OpenRouter 和硅基流动只是对标/迁移兼容对象，不是默认模型厂家；默认 Provider 是模型厂商或云端/本地模型 Endpoint。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 迁移框架 | `infra/migrations/` | 存放版本化 SQL 迁移文件，包含 up/down 或可回滚验证信息 |
| 种子数据 | `infra/seeds/` | 存放默认租户、管理员、测试 Provider、模型别名种子定义 |
| DB 脚本 | `scripts/db/` | 增加 `migrate.ps1`、`seed.ps1`、`rollback.ps1` 或统一 `db.ps1` |
| 共享 DB 工具 | `shared-sdk/python/metahub_shared/db/` | 连接、迁移记录、事务、锁、脱敏日志、密码哈希工具 |
| 控制面 API | `control-plane-api/` | PRD-006 登录会使用种子管理员，本 PRD 不新增业务 API |
| 文档 | `README.md`、`docs/development/`、`docs/testing/` | 更新数据库初始化流程、表边界和测试说明 |
| 测试 | `tests/integration` | 覆盖空库迁移、重复迁移、种子幂等、约束、异常路径 |

编码时要保留详细中文注释，重点解释：为什么迁移表是全局基础设施表、为什么业务表必须带 `tenant_id`、Provider Key 为什么不能明文落日志。

## 数据表/字段设计

目标数据库以 PostgreSQL 为主；M0 本地/CI 可使用轻量测试数据库模式验证迁移逻辑，但生产契约按 PostgreSQL 字段和约束设计。不做 MySQL、国产数据库或分库分表兼容。

### schema_migrations

用途：记录已执行的迁移版本。它是基础设施元数据，不是业务表，因此不强制 `tenant_id`。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `version` | varchar(64) | PK | 迁移版本，如 `202607070001` |
| `name` | varchar(200) | not null | 迁移名称 |
| `checksum` | varchar(128) | not null | 迁移文件校验和 |
| `status` | varchar(32) | not null | `applied`、`rolled_back`、`failed` |
| `applied_at` | timestamptz | not null | 执行时间 |
| `execution_time_ms` | integer | not null | 执行耗时 |
| `error_message` | text | nullable | 失败摘要，不能包含密钥 |

### seed_runs

用途：记录种子数据执行情况。种子可能关联默认租户，因此保留 `tenant_id`。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `seed_run_id` | uuid | PK | 种子运行 ID |
| `tenant_id` | uuid | nullable/index | 默认租户 ID，基础 seed 可为空 |
| `seed_name` | varchar(120) | not null | 种子名称 |
| `checksum` | varchar(128) | not null | 种子定义校验和 |
| `status` | varchar(32) | not null | `applied`、`skipped`、`failed` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### tenants

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `tenant_id` | uuid | PK | 租户 ID |
| `slug` | varchar(80) | unique/not null | 租户唯一标识 |
| `name` | varchar(160) | not null | 租户名称 |
| `service_mode` | varchar(32) | not null | `internal_only`、`public_service` |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### users

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `user_id` | uuid | PK | 用户 ID |
| `tenant_id` | uuid | FK/index/not null | 所属租户 |
| `email` | varchar(255) | unique per tenant/not null | 登录邮箱 |
| `display_name` | varchar(120) | not null | 显示名 |
| `password_hash` | varchar(255) | not null | 密码哈希，禁止明文 |
| `role` | varchar(64) | not null | M0 默认 `admin` |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

唯一约束：`(tenant_id, email)`。

### virtual_keys

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `virtual_key_id` | uuid | PK | Virtual Key ID |
| `tenant_id` | uuid | FK/index/not null | 所属租户 |
| `name` | varchar(120) | not null | Key 名称 |
| `key_prefix` | varchar(24) | not null | 前缀，用于展示和排障 |
| `key_hash` | varchar(128) | unique/not null | Key 哈希，禁止明文存储 |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### providers

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `provider_id` | uuid | PK | Provider ID |
| `tenant_id` | uuid | FK/index/not null | 所属租户 |
| `provider_code` | varchar(80) | not null | 如 `deepseek`、`zhipu`、`local_openai` |
| `name` | varchar(160) | not null | 展示名 |
| `base_url` | text | not null | 上游 Endpoint 基础 URL，不含 key |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

唯一约束：`(tenant_id, provider_code)`。

### provider_credentials

Provider Key 不能进入日志，也不应明文存储。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `provider_credential_id` | uuid | PK | 凭证 ID |
| `tenant_id` | uuid | FK/index/not null | 所属租户 |
| `provider_id` | uuid | FK/index/not null | 所属 Provider |
| `key_fingerprint` | varchar(64) | not null | Key 指纹 |
| `encrypted_key` | text | not null | 加密后的 Provider Key 或 M0 占位密文 |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

### models

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `model_id` | uuid | PK | 模型 ID |
| `tenant_id` | uuid | FK/index/not null | 所属租户 |
| `provider_id` | uuid | FK/index/not null | 所属 Provider |
| `model_alias` | varchar(120) | not null | 网关侧模型别名 |
| `upstream_model_name` | varchar(160) | not null | 上游真实模型名 |
| `capabilities` | jsonb | not null default `{}` | chat、stream、tools 等能力 |
| `status` | varchar(32) | not null | `active`、`disabled` |
| `created_at` | timestamptz | not null | 创建时间 |
| `updated_at` | timestamptz | not null | 更新时间 |

唯一约束：`(tenant_id, model_alias)`。

### request_logs

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `request_id` | varchar(80) | PK | 请求 ID |
| `tenant_id` | uuid | FK/index/not null | 租户 ID |
| `trace_id` | varchar(120) | index/not null | Trace ID |
| `virtual_key_id` | uuid | nullable/index | 调用 Key |
| `provider_id` | uuid | nullable/index | 实际 Provider |
| `model_id` | uuid | nullable/index | 实际模型 |
| `status` | varchar(32) | not null | `success`、`failed` |
| `error_code` | varchar(120) | nullable/index | 归一错误码 |
| `latency_ms` | integer | nullable | 延迟 |
| `created_at` | timestamptz | not null | 创建时间 |

失败请求必须写入该表或错误日志，满足验收红线。

### usage_ledger

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `usage_ledger_id` | uuid | PK | 账本 ID |
| `tenant_id` | uuid | FK/index/not null | 租户 ID |
| `request_id` | varchar(80) | FK/index/not null | 关联请求 |
| `input_tokens` | integer | not null default 0 | 输入 token |
| `output_tokens` | integer | not null default 0 | 输出 token |
| `raw_usage` | jsonb | not null default `{}` | 上游原始用量 |
| `normalized_usage` | jsonb | not null default `{}` | 归一化用量 |
| `cost_amount` | numeric(18, 8) | not null default 0 | 成本金额 |
| `currency` | varchar(16) | not null default `CNY` | 币种 |
| `price_schema_version` | varchar(80) | nullable | 价格口径 |
| `created_at` | timestamptz | not null | 创建时间 |

## API 设计

本 PRD 不新增 HTTP API，只提供 CLI。

### CLI: db:migrate

PowerShell 入口建议：

```powershell
.\scripts\db\migrate.ps1
```

行为：

- 读取 `METAHUB_DATABASE_URL`。
- 创建 `schema_migrations` 表。
- 按版本顺序执行未执行的迁移。
- 每个迁移在事务中执行。
- 执行成功记录 checksum、耗时、状态。
- 重复执行时跳过已执行且 checksum 一致的迁移。
- checksum 不一致时返回错误，要求人工确认。

### CLI: db:seed

PowerShell 入口建议：

```powershell
.\scripts\db\seed.ps1
```

行为：

- 依赖迁移已完成。
- 创建默认租户 `default`。
- 创建默认管理员账号，密码来自 `METAHUB_SEED_ADMIN_PASSWORD`，只存哈希。
- 创建测试 Provider 和模型别名。
- 创建测试 Virtual Key，只输出一次明文到控制台或本地开发文件，数据库只存哈希。
- 重复执行时按唯一键 upsert 或跳过，保证幂等。

### CLI: db:rollback

M0 至少支持测试环境回滚最近一个迁移，生产回滚需要显式 `--confirm-prod`，避免误操作。

## 前端页面/交互设计

本 PRD 页面/入口为“无”。种子管理员能否登录控制台由 PRD-006 管理员登录实现后验证。

当前文档和 CLI 需要提供：

- README 数据库初始化步骤。
- `.env.example` 中的数据库、Redis、种子管理员配置占位。
- 控制台不新增按钮。

## 错误码设计

| 错误码 | 场景 | 建议 |
| --- | --- | --- |
| `db.config_missing` | `METAHUB_DATABASE_URL` 缺失 | 配置数据库连接串 |
| `db.connection_failed` | 无法连接数据库 | 检查地址、账号、网络 |
| `db.migration_failed` | 迁移执行失败 | 使用版本号和 request_id 查看日志 |
| `db.migration_checksum_changed` | 已执行迁移 checksum 改变 | 禁止直接修改历史迁移，新增修复迁移 |
| `db.migration_locked` | 已有迁移进程执行中 | 等待或清理异常锁 |
| `db.seed_failed` | 种子数据失败 | 检查唯一约束和配置 |
| `db.constraint_violation` | 字段约束或外键约束失败 | 修复 seed 或迁移定义 |

CLI 输出必须包含 `request_id` 或 `run_id`，日志不能输出 Provider Key、Virtual Key 明文或管理员密码。

## 安全与权限约束

- 所有业务表默认包含 `tenant_id`，基础设施表 `schema_migrations` 例外。
- 管理员密码只存哈希，seed 日志不输出明文密码。
- Provider Key 只存加密值或 M0 占位密文，不明文落库、不明文落日志。
- Virtual Key 只存 hash 和 prefix；明文只在 seed 创建时一次性输出。
- 生产环境执行 seed 或 rollback 必须显式确认。
- 迁移 SQL 不允许包含真实客户数据。
- `db.migrated` 事件 payload 只包含版本、状态、耗时、tenant_id，不包含密钥。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0005-1 | 空库执行 `db:migrate` | 所有 MVP 表和 `schema_migrations` 创建成功 |
| TC-0005-2 | 重复执行 `db:migrate` | 已执行迁移被跳过，结果幂等 |
| TC-0005-3 | 执行 `db:seed` | 默认租户、管理员、测试 Provider、模型、Virtual Key 创建成功 |
| TC-0005-4 | 重复执行 `db:seed` | 不产生重复租户、用户、Provider、模型 |
| TC-0005-5 | 字段约束 | 业务表 `tenant_id`、状态、唯一键、外键符合设计 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0006-1 | 缺失 `METAHUB_DATABASE_URL` | 返回 `db.config_missing`，包含建议 |
| TC-0006-2 | 数据库不可达 | 返回 `db.connection_failed` |
| TC-0006-3 | 已执行迁移 checksum 改变 | 返回 `db.migration_checksum_changed` |
| TC-0006-4 | 回滚最近迁移 | 测试环境可回滚，生产要求显式确认 |
| TC-0006-5 | 敏感数据日志 | Provider Key、Virtual Key、管理员密码不出现在日志 |

## 代码修改计划

已按以下顺序编码：

1. 增加 `shared-sdk/python/metahub_shared/db/`，实现数据库 URL 解析、连接、迁移执行、回滚、seed、run_id。
2. 增加 `infra/migrations/0001_core_mvp_tables.sql` 和 `0001_core_mvp_tables.down.sql`。
3. 增加 `infra/seeds/default_seed.json`，包含默认租户、管理员、测试 Provider、模型和 Virtual Key。
4. 增加 `scripts/db/migrate.ps1`、`scripts/db/seed.ps1`、`scripts/db/rollback.ps1`。
5. 扩展错误码：`db.*`。
6. 扩展 `.env.example`：`METAHUB_DATABASE_URL`、`METAHUB_SEED_ADMIN_EMAIL`、`METAHUB_SEED_ADMIN_PASSWORD`、测试 Provider 占位。
7. 增加集成测试：空库迁移、重复迁移、seed 幂等、checksum 异常、回滚、敏感值不落库。
8. 更新 README、`docs/development/directory-map.md` 和 `docs/testing/PRD-003-tests.md`。

不进入本 PRD 的内容：

- 不做分库分表。
- 不做国产数据库兼容。
- 不做完整 RBAC。
- 不做控制台数据库管理页面。
- 不做生产级密钥管理系统；M0 只保留加密/占位和脱敏边界，后续安全 PRD 增强。

## 验证结果

已运行：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\ci\check.ps1
```

结果：

- 后端 pytest：`18 passed`
- 控制台 Node test：`4 passed`
- 控制台 lint：通过

已覆盖：

- 空库迁移成功。
- 重复迁移幂等。
- seed 重复执行不重复创建核心对象。
- 已执行迁移 checksum 改变会失败。
- Provider Key、Virtual Key、管理员密码不明文落库。
- 测试环境 rollback 可删除 MVP 表。

真实 CLI 验证：

```powershell
$env:METAHUB_DATABASE_URL='sqlite:///./.data/prd003_verify.db'
.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```

结果：

- `schema_migrations=1`
- `tenants=1`
- `users=1`
- `providers=1`
- `models=1`
- `virtual_keys=1`
- 管理员密码、Provider Key、Virtual Key 检查结果均为 `masked`
