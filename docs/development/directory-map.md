# 目录边界说明

本文档对应 `PRD-001 - 仓库工程骨架与模块边界`。

## 顶层目录

| 目录 | 当前状态 | 职责边界 |
| --- | --- | --- |
| `gateway-data-plane/` | 已接入 OpenAI-compatible 基础入口、模型别名最小路由、Provider Adapter/KeyPool、Project/Application 归属、调用日志/用量、限流、预算、DLP、Fallback/SSE、策略快照缓存与请求期执行，以及已验收的 PRD-047 动态初始路由 | 高频模型调用热路径。PRD-045 的 per-tenant singleflight/轮询、不可变 lease、LKG、请求 claim 与受控 `route_to` 继续生效；PRD-047 为 Alias 冻结 RoutingPolicy/候选/Health/KeyPool/按需价格/Fallback，原子保存 Decision/Candidate，并以不可改写总预留加未结算分项保留主备未知账单。动态解释事实或 active Budget 的可审计依赖不可用时停在 Provider 前。 |
| `control-plane-api/` | 已接入基础认证、当前租户、RBAC、Project/Application、Virtual Key、Provider/KeyPool、模型、日志/用量/CSV、预算、DLP、Policy、策略快照发布/回滚、策略 Dry-Run、Webhook、Dashboard、i18n、导航、示例下载、调试台、错误诊断和已验收的 PRD-047 RoutingPolicy API | 低频管理 API。PRD-046 的 admin-only query-only Dry-Run 与三事实事务保持原合同；PRD-047 的 `GET /model-aliases` 返回当前 RoutingPolicy/配置候选，admin-only `PATCH /model-aliases/{id}/routing-mode` 执行严格 wire、CAS、持久幂等、租户事务和审计/事件，调用日志详情返回强类型请求候选。 |
| `console-web/` | 已接入登录态、统一控制台骨架、Dashboard、语言、租户/用户、项目/应用、Virtual Key、Provider/KeyPool、模型、日志/用量/CSV、预算、DLP、Policy、策略发布/历史、策略模拟器、路由策略、Webhook、快速开始、SDK 示例、调试台和错误诊断 | 企业管理员和开发者控制台。`policy-simulator` 的 Prompt 生命周期与后端 RBAC 合同继续保持；已验收的 PRD-047 `routing-policies` 页面由 admin 编辑、developer 只读，viewer 无 DOM/预取，草稿和 PATCH ticket 绑定身份、导航、Alias、Policy 版本与 operation generation 并拒绝迟到响应。 |
| `examples/python/` | PRD-038 新增官方客户端示例项目 | 提供 Chat、Embeddings、base_url、Virtual Key、超时、重试与 request_id 错误处理；不封装完整 SDK，不保存真实密钥。 |
| `examples/js/` | PRD-039 新增 Node.js 服务端示例项目 | 提供非流式/流式 Chat、baseURL、Virtual Key、超时、重试与 request_id 安全处理；拒绝浏览器长期 Key，不封装完整 SDK。 |
| `shared-sdk/` | 已建契约目录 | OpenAPI、JSON Schema、Python/TypeScript SDK，以及当前两个服务共享的运行时配置、错误码、错误诊断、i18n、日志、request_id、受限 Policy DSL、严格快照合同、`policy_preview`，以及 PRD-047 `routing_policy` 两阶段资格/排序、Token 估算、15 位 Decimal 成本和稳定原因码纯计算合同。 |
| `scripts/` | 已建脚本目录，已新增单机部署 CLI 和 PRD-047 验收程序 | 本地启动、CI、数据库、部署、发布、预检脚本；PRD-028 的 `scripts/deploy/install.ps1`、`start.ps1`、`stop.ps1`、`status.ps1`、`smoke.ps1` 保持不变。PRD-047 Runtime/Browser 程序已通过真实三服务、精确 origin、DOM/URL/storage 隐私、PNG 像素、owner/命名图和 finally cleanup 验收；旧失败制品不作为通过证据。 |
| `infra/` | 已建迁移、seed、Docker 与 Compose 目录 | Docker、Compose、数据库迁移、种子数据。0034 快照/claim 与 0035 explanation/Dry-Run 合同保持不变；已验收的 PRD-047 0036 新增 RoutingPolicy/Event/Candidate 和 `budget_reservations.outstanding_cost_cny`，扩展 Decision/Audit。它保留历史行与链路字段，但把删除旧 Provider Audit 可逆 `base_url` 明文作为唯一安全例外；down 不恢复该 URL。 |
| `docs/` | 已建文档目录 | PRD 设计、架构、API、ADR、开发说明、Runbook、安全与测试文档；`docs/development/code-commenting-standard.md` 固定中文注释规范。 |
| `docs/planning/` | SaaS 增量路线图 | 保存 PRD-090 起的权威规划说明；详细字段、废弃映射和门禁以根目录 `企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 为准。 |

PRD-024 后，M0 热路径已接入基础 DLP：控制面管理租户级关键词/正则规则，数据面只扫描 Chat Completions 的 `role=user` 文本，调用日志详情只展示命中摘要哈希。PRD-025 后，控制面已接入 Webhook Endpoint 注册、事件类型订阅、HMAC 测试发送、3 次重试和发送记录；生产治理事件触发点按后续 PRD 复用同一事件信封接入。PRD-026 后，控制台概览页改为当前租户 Dashboard，从真实 `request_logs` 与 `usage_ledger` 聚合今日请求、成功率、P95、成本、Top 模型、Top Virtual Key 和最近错误。PRD-027 后，控制台建立 i18n 基础框架，当前用户可通过 `/user/preferences` 保存 `zh-CN` 或 `en-US`，错误码目录可按 locale 返回展示层 label，但内部 code、日志和事件类型继续保持英文稳定值。PRD-028 后，仓库新增单机 Docker Compose 部署包，默认以 `local-sqlite` profile 启动 Gateway、Control API 和 Console；Redis、PostgreSQL、MinIO 通过 `compose-full` 作为可选基础设施服务预留，应用默认数据库仍为 sqlite。PRD-029 后，Project/Application 成为 Virtual Key、调用日志、用量账本和预算规则的业务归属维度；历史日志和 usage 保存请求发生时的归属快照，不随 Key 后续改绑漂移。PRD-042 后，Webhook 支持最近 100 条详情筛选、failed delivery 幂等重放、Secret 原子轮换、版本快照和脱敏管理审计；持久崩溃恢复与 dead-letter 仍留给 PRD-057。PRD-043 后，admin 可在用量页同步下载或异步生成固定 42 列 CSV；只增账本序列固定异步快照，任务、下载审计和 `usage.exported` 事件按租户追踪，FinanceAdmin、对象存储和分布式任务仍留给后续 PRD。PRD-044 已完成受限 DSL、Policy 管理 API、admin-only 编辑器和全部运行验收。PRD-045 已完成不可变快照发布/单调回滚、Gateway 按租户消费、请求 claim、调用日志版本追踪、运行库迁移、真实 API 与三档浏览器验收。PRD-046 已完成策略预演实现、专项/全量、0035、真实 API、结果未知补偿、隐私扫描和三档浏览器运行验收。PRD-047 已完成设计、自审、实现、安全复审、专项/全量、0036、真实 API/Webhook/Usage、隐私清理和三档浏览器验收。

### PRD-089 数据生命周期边界

`control-plane-api/src/metahub_control/tenant_data/` owns the tenant export and deletion
workflow. `routes.py` is the strict HTTP boundary, `service.py` contains tenant-qualified
projections, encrypted artifact generation, schema/trigger manifest verification and the
exclusive deletion transaction, while `scheduler.py` composes the bounded cancellable worker
into the FastAPI lifespan. The shared `tenant_data/crypto.py` module is an independent AES-GCM
key domain for export ZIPs and retention archives. `console-web/src/lib/tenantDataLifecycle.js`
and `console-web/src/features/tenant-data-lifecycle/` own PAGE-121 presentation state and
clear-before-abort lifecycle cleanup; they never choose a tenant or infer backend permissions.

## 后端服务边界

`gateway-data-plane` 和 `control-plane-api` 都通过 `metahub_shared.runtime.fastapi_app.create_service_app` 创建 FastAPI 应用。这样可以统一：

- `X-Request-ID` 生成与透传。
- `/health` 和 `/version` 响应结构。
- `code/message/request_id/suggestion` 错误结构。
- `diagnosis` 错误诊断结构和 `GET /error-codes` 只读错误码目录。
- JSON 结构化日志。
- Provider Key、Authorization、Token 等敏感字段脱敏。
- `X-Trace-ID` 解析、生成和响应头透传。
- 错误响应 `trace_id` 字段。
- `defaults < .env < METAHUB_CONFIG_FILE < 环境变量` 的配置优先级。
- 配置摘要 `ConfigSummary` 的安全脱敏。

新增业务 API 时不能绕过该应用工厂，否则会破坏 request_id、错误码和日志契约。

## 配置边界

PRD-002 建立了运行配置与环境隔离基础：

- 只允许 `dev`、`test`、`prod` 三类运行环境。
- M0 配置在进程启动时加载，修改后需要重启服务。
- 默认配置可以被 `.env` 覆盖，`.env` 可以被 `METAHUB_CONFIG_FILE` 指向的配置文件覆盖，最终由环境变量覆盖。
- Provider 连接信息从 `METAHUB_PROVIDER_<NAME>_BASE_URL` 读取。
- Provider Key、数据库 URL、Redis URL、内部 token 等敏感项只允许在摘要中显示 `***MASKED***` 或 `missing`。
- `prod` 环境访问 `/internal/config/summary` 必须经过 `X-Internal-Token` 后端校验。
- `METAHUB_POLICY_EVALUATION_TIMEOUT_MS` 默认 5，限定为 1-100 ms；该时间预算由服务端配置，Policy API 不能由客户端覆盖。
- `METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED` 控制 Gateway 快照同步；生产环境或 `public_service` 模式必须启用。轮询、HTTP 超时、普通/应急 LKG 有效期和 payload 上限使用 `.env.example` 中六个 `METAHUB_POLICY_SNAPSHOT_*` 变量，且普通有效期不得短于轮询间隔、应急有效期不得短于普通有效期。
- 快照同步复用 `METAHUB_CONTROL_API_BASE_URL` 和 `METAHUB_INTERNAL_CONFIG_TOKEN`；Token 必须在 Gateway/Control 两端一致且满足 32-512 UTF-8 bytes、无空白/控制字符的约束。生产 URL 只允许 HTTPS，dev/test HTTP 只允许回环地址，禁止 URL credentials/query/fragment。

配置摘要接口只用于运维和开发排障，不是动态配置中心；PRD-002 不支持在线修改、热更新、审批或灰度发布。

## 数据库迁移边界

PRD-003 建立了可运行的迁移与种子数据基础：

- 迁移文件放在 `infra/migrations/`，命名为 `<version>_<name>.sql`。
- 回滚脚本放在同目录，命名为 `<version>_<name>.down.sql`。
- `schema_migrations` 是基础设施表，不带 `tenant_id`。
- 业务表必须带 `tenant_id`，包括 `tenants`、`seed_runs`、`roles`、`users`、`sessions`、`virtual_keys`、`providers`、`provider_credentials`、`models`、`model_aliases`、`model_alias_routing_policies`、`routing_decisions`、`routing_candidates`、`routing_policy_events`、`request_logs`、`usage_ledger`、`price_snapshots`、`budget_rules`、`budget_alert_events`、`budget_evaluations`、`user_preferences`、`console_page_events`、`developer_portal_events`、`debug_console_events`、`webhook_endpoints`、`webhook_deliveries`、`webhook_delivery_attempts`、`policies`、`policy_rules`、`policy_actions`、`policy_events`、`policy_explanations`、`policy_snapshots`、`policy_snapshot_items`、`policy_snapshot_heads`、`policy_snapshot_events` 和 `gateway_request_claims`。
- seed 数据放在 `infra/seeds/default_seed.json`。
- CLI 位于 `scripts/db/`，包括 `migrate.ps1`、`seed.ps1`、`rollback.ps1`。
- PRD-022 新增 `virtual_key_rate_limit_rules`，用于保存 Virtual Key 级限流规则，默认带 `tenant_id`，运行时计数器不落业务库。
- PRD-023 新增 `budget_rules`、`budget_alert_events` 和 `budget_evaluations`，用于保存预算配置、阈值事件和请求级预算评估，均默认带 `tenant_id`。
- PRD-024 新增 `dlp_rules` 和 `dlp_matches`，用于保存租户级 DLP 规则和请求级命中摘要，均默认带 `tenant_id`，命中记录不保存敏感明文。
- PRD-025 新增 `webhook_endpoints`、`webhook_deliveries` 和 `webhook_delivery_attempts`，用于保存租户级 Webhook Endpoint、最终投递状态和逐次重试记录，均默认带 `tenant_id`。
- PRD-042 为 Endpoint 增加 Secret 版本/轮换元数据，为 delivery 增加 original/replay 类型、来源、操作者、幂等键与 Secret 版本快照；两组部分唯一索引分别保护原始事件和人工重放，归属触发器阻止 Endpoint、来源 delivery、actor 与 tenant 错配。
- PRD-043 新增 `usage_export_ledger_sequences`、`usage_export_jobs`、`usage_export_events`，扩展审计动作；AUTOINCREMENT 序列固定任务水位，账本与映射采用追加事实语义，任务状态、actor 和事件继续由 tenant-qualified 外键与触发器约束。
- PRD-044 新增 `policies`、`policy_rules`、`policy_actions`、`policy_events`，并为 users/models 增加租户复合唯一索引；复合外键、owner/identity/version/immutable 触发器和 created/validation_failed 部分唯一索引共同约束租户归属、精确版本与事件幂等。
- PRD-045 新增 `policy_snapshots`、`policy_snapshot_items`、`policy_snapshot_heads`、`policy_snapshot_events` 和 `gateway_request_claims`；0034 把 `request_logs` 主键改为 `(tenant_id, request_id)`，同步升级 `usage_ledger`、`price_snapshots` 的同租户复合请求外键，并增加实际快照版本/状态与安全 Policy 摘要。down migration 检测到跨租户重复 request_id 时必须先安全中止，不能丢行、改 ID 或错绑 Usage。
- PRD-046 新增 0035 `policy_explanations`，并扩展 `policy_events`、`audit_logs` 的 dry-run 合同和 Webhook 事件白名单。三类事实以 `tenant_id + actor_user_id + request_id` 部分唯一索引约束精确幂等，explanation 不可更新/删除；down migration 过滤 PRD-046 事实后恢复 0034 表、索引和触发器，非 PRD-046 历史必须逐字段保真。
- PRD-047 已验收的 0036 新增一对一 `model_alias_routing_policies`、不可变 `routing_policy_events`、请求级不可变 `routing_candidates` 和 nullable `budget_reservations.outstanding_cost_cny`；重建 `routing_decisions` 加入 Policy/version/mode、成本时点、Token 估算和事件关联，重建 `audit_logs` 接受 RoutingPolicy 审计。复合外键与 owner/identity/version/graph/immutable 触发器约束同租户 Alias/Environment/Model/Provider/Decision/Candidate 图。迁移保留历史行和链路字段，但不可逆删除旧 Provider Audit 的 `base_url` 明文；down 删除 PRD-047 事实和 outstanding 列，不恢复已清理 URL。
- PRD-055 的 0045 新增 `saved_filters` 和 `request_log_query_events`，两者均带
  `tenant_id`；前者还以 `owner_user_id` 约束个人筛选并只允许软删除，后者是不可变
  `log.query_executed` source outbox。迁移同时增加 tenant-first RequestLog 复合索引、
  owner/identity/状态机/REPLACE/rowid 触发器及业务事实 down guard。
- PRD-064 的 0056 新增 tenant-scoped、不可变的 `adapter_contract_runs`、
  `adapter_contract_cases` 和 `adapter_contract_event_outbox`；复合 owner、计数、状态、唯一性、
  REPLACE/更新/删除触发器及业务事实 down guard 共同约束报告、case 和失败事件恢复。实现已落地并完成
  开发库实际 `0055 -> 0056`、专项验证、真实 Control/Gateway/Browser 和最终分层回归。
- PRD-026 不新增 Dashboard 缓存表；迁移扩展 `console_page_events.event_type` 支持 `dashboard.viewed`，并为 `request_logs` 增加 Dashboard 今日聚合索引。
- PRD-027 扩展 `user_preferences.locale` 并新增 `user_preference_events`，用于保存当前用户语言偏好和 `user.locale_changed` 事件；二者都按认证上下文中的 `tenant_id/user_id` 写入。
- PRD-029 新增 `projects`、`applications`、`project_events`，并扩展 `virtual_keys`、`request_logs`、`usage_ledger` 与预算表 scope，项目和应用数据都按认证上下文中的 `tenant_id` 写入。
- Provider Key、Virtual Key、管理员密码不能明文落库或落日志。
- 访问 Token 只允许保存 `token_hash`，不能明文落库或落日志。

当前运行时已有有界 PostgreSQL 连接池适配器，但历史迁移仍是 SQLite-native SQL。本地/CI 使用
`sqlite://` 验证完整迁移行为；标准 migrate/rollback CLI 对 `postgresql://` 在连接前固定返回
`db.migration_backend_unsupported`，直到独立 PostgreSQL-native migration bundle 覆盖全部历史版本。

## 可观测边界

PRD-004 建立了统一日志、TraceID 和错误码基础：

- 所有 API 响应头包含 `X-Request-ID` 和 `X-Trace-ID`。
- 错误响应包含 `code/message/request_id/trace_id/suggestion/diagnosis`。
- 结构化 JSON 日志包含 `request_id`、`trace_id`、`path`、`method`、`status_code`、`latency_ms`。
- 未捕获异常统一返回 `infra.unhandled`，客户端不看到堆栈或密钥。
- PRD-004 阶段控制台只提供错误页预览和调试台预留；PRD-016 与 PRD-020 后，调用日志和内置 API 调试台已接入真实低频治理链路。
- PRD-021 后，`error.diagnosed` 结构化日志只记录安全诊断元数据，调用日志详情和调试台错误卡片展示同一套诊断 DTO。
- M0 不接入 OpenTelemetry 后端，也不做完整 Trace 可视化。

## 健康检查边界

PRD-005 建立了服务健康检查与版本信息基础：

- `/health/live` 是 liveness probe，只检查进程响应，不访问外部依赖。
- `/health/ready` 是 readiness probe，数据库配置存在时执行 ping，失败则 `ready=false`。
- Redis 和 Provider 在 M0 是可选检查，不阻塞 ready。
- `/version` 输出版本、commit、build_time、started_at、uptime、request_id、trace_id。
- 控制台系统状态小组件只读展示 live/ready/version，不提供重启或修复按钮。
- health/version 响应不能包含 DB URL、Redis 密码、Provider Key。

## 控制台边界

PRD-006 后控制台先进入登录页；登录成功后仍是受控的本地启动/状态控制台。它只能展示：

- Gateway Data Plane 状态。
- Control Plane API 状态。
- Console Web 自身状态。
- README、PRD 和健康检查链接。
- 当前租户信息和允许的 M0 租户基础字段编辑。
- admin 可见的当前租户用户管理和角色选择。
- admin 可见的 Provider 列表、创建、连接测试、启停入口和 KeyPool 管理入口。
- admin/developer 可见的模型目录；admin 可创建和启停模型，developer 只读。
- admin/developer 可见的模型别名；admin 可创建和启停别名，developer 只读。
- admin/developer 可见的项目与应用列表、Virtual Key 列表、创建、项目/应用绑定和禁用入口。
- admin 可见的策略管理与编辑器；developer/viewer 的导航、DOM 和后台请求都不得恢复该入口，后端四个 Policy 路由仍统一执行 admin 守卫。
- admin 可见的 `policy-simulator` 策略模拟器；developer/viewer 直达 hash 必须回退 overview 且不发送 Dry-Run 请求，`POST /policies/dry-run` 仍由后端 `require_policy_admin` 校验。
- admin/developer 可见的内置 API 调试台入口；调试请求由后端按登录态、当前租户和 active Virtual Key 元数据重新校验。

租户、用户、Provider、模型目录、模型别名、Virtual Key、调试台等入口只有在对应后端权限和 API 完成后才展示；账单等后续管理按钮仍不展示。PRD-020 已用内置 API 调试台替换 PRD-009 的手输 Key 调试入口，成功和失败请求会进入 request_logs、usage_ledger 或 debug_console_events 可追踪链路。

已实现的认证入口：

- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`

已实现的租户入口：

- `GET /tenant/current`
- `PATCH /tenant/current`

已实现的用户/RBAC 入口：

- `GET /users`
- `POST /users`
- `PATCH /users/{id}/role`
- 后端角色依赖：`require_current_admin`、`require_virtual_key_writer`、`require_model_catalog_reader`

已实现的 Virtual Key 入口：

- `GET /virtual-keys`
- `POST /virtual-keys`
- `PATCH /virtual-keys/{id}`
- `PATCH /virtual-keys/{id}/limits`
- `DELETE /virtual-keys/{id}`，执行软禁用，不物理删除。

已实现的 Provider 入口：

- `GET /providers`
- `POST /providers`
- `PATCH /providers/{id}`
- `POST /providers/{id}/test`
- `GET /providers/{id}/keys`
- `POST /providers/{id}/keys`
- `PATCH /provider-keys/{id}`

已实现的模型目录入口：

- `GET /models`
- `POST /models`
- `PATCH /models/{id}`

已实现的模型别名入口：

- `GET /model-aliases`
- `POST /model-aliases`
- `PATCH /model-aliases/{id}`

已实现的调用日志入口：
- `GET /request-logs`
- `GET /request-logs/{request_id}`
- 详情响应按 `request_logs.error_code` 或路由失败错误码派生 `error_diagnosis`，不新增持久化规则表。
- `GET /usage/summary`
- `GET /usage/records`
- `GET /usage/export.csv`
- `GET /usage/exports/{job_id}`
- `GET /usage/exports/{job_id}/download`

已实现的预算告警入口：

- `GET /budgets`
- `POST /budgets`
- `PATCH /budgets`
- `PATCH /budgets/{id}/mode`
- `GET /budget-status`

已实现的基础 DLP 入口：

- `GET /dlp/rules`
- `POST /dlp/rules`
- `PATCH /dlp/rules/{rule_id}`

已实现的 Dashboard 入口：

- `GET /dashboard/overview`

已实现的用户偏好入口：

- `GET /user/preferences`
- `PATCH /user/preferences`

已实现的控制台骨架入口：

- `GET /console/navigation`
- `GET /console/preferences`
- `PATCH /console/preferences`
- `POST /console/events`

已实现的开发者门户入口：

- `GET /developer/quickstart-data`

已实现的内置 API 调试入口：

- `POST /debug/chat-completions`

后续管理 API 必须复用 `require_current_admin` 或更细的后端权限依赖，不能依赖前端隐藏按钮。
租户接口只能从认证上下文读取 `tenant_id`，不能信任请求体或查询参数传入的租户 ID。
用户管理接口只能读写当前租户，developer/viewer 直接调用会被后端拒绝；Virtual Key 接口只能读写当前租户，viewer 直接调用会被后端拒绝；Provider 和 Provider KeyPool 接口只能读写当前租户，developer/viewer 直接调用会被后端拒绝；模型目录和模型别名接口只能读写当前租户，developer 只读，viewer 直接调用会被后端拒绝。

## OpenAI-Compatible 数据面边界

PRD-009 建立了最小 OpenAI-compatible API 兼容层：

- `POST /v1/chat/completions`：支持基础 Chat Completions JSON 请求和 `stream=true` SSE 透传。
- `POST /v1/embeddings`：支持基础 Embeddings JSON 请求。
- Virtual Key 鉴权在 `gateway-data-plane/src/metahub_gateway/auth/`，只通过 Bearer Token 哈希恢复 `tenant_id`。
- PRD-010 后，数据面成功鉴权会更新 `virtual_keys.last_used_at`；禁用 Key 统一返回 `gateway.auth_invalid`。
- 模型路由在 `gateway-data-plane/src/metahub_gateway/routing/`，只查当前租户数据；请求体 `model` 优先按 `model_aliases.alias + Virtual Key environment` 解析，未命中时兼容内部标准模型 ID，数据面最终转发 `provider_model_id`。
- OpenAI-compatible Provider Adapter 和 KeyPool 在 `gateway-data-plane/src/metahub_gateway/providers/`，Provider Key 优先从当前租户 Provider 的数据库 KeyPool 解密选择。环境 Key 只是非生产本地兼容路径：active 默认租户、active Provider、code 和数据库/冻结 Route Endpoint 均与运维基准精确匹配时才能使用。生产或其他租户必须使用自己的托管 Key；Key 只进入上游请求头。
- PRD-013 后 Provider 调用统一走 Adapter 合同：`build_request`、`send_non_stream`、`send_stream`、`parse_usage`、`normalize_error`。新增 OpenAI-compatible Endpoint 或通用 HTTP 兼容 Endpoint 时应扩展 Adapter/工厂，不直接修改 `/v1` 主流程。
- PRD-022 后，Virtual Key 级限流在 `gateway-data-plane/src/metahub_gateway/rate_limits/`，数据面在路由解析后、上游调用前执行 RPM/TPM/并发准入判断。
- PRD-023 后，日预算检查在 `gateway-data-plane/src/metahub_gateway/budgets/`，数据面在限流通过后、上游调用前按租户和 Virtual Key 规则执行预算告警或阻断。
- PRD-024 后，基础 DLP 在 `gateway-data-plane/src/metahub_gateway/dlp/`，数据面在路由解析后、限流和预算前扫描 `role=user` 文本；阻断时返回 `gateway.dlp_blocked`，放行命中只写摘要。
- request/usage 写入在 `gateway-data-plane/src/metahub_gateway/usage/`，M0 已记录 token usage 和零成本占位。
- 路由解释写入 `routing_decisions`，记录 `request_id`、请求别名、真实模型、Provider、选择原因和失败错误码；不记录 prompt/input/messages 或密钥。

当前数据面不做 Responses API、Assistants API、MCP 主链路、多 Provider 动态路由、流式中途 fallback、语义/多轮/文件级复杂 DLP 或价格归一化。这些能力必须按后续 PRD 分阶段接入，不能拖慢 M0/M1 基础可用性。

## Virtual Key 管理边界

PRD-010 建立了控制面 Virtual Key 管理能力：

- 明文 Key 只在创建响应展示一次。
- 数据库只保存 `key_hash`、`key_prefix`、环境、备注、状态、最近使用时间和禁用审计字段。
- `DELETE /virtual-keys/{id}` 是软禁用，保留历史追踪关系。
- admin/developer 可创建、列表、更新和禁用；viewer 后端拒绝。
- 控制台不保存明文 Key 到 sessionStorage/localStorage，刷新后不再展示。

PRD-022 已接入 Virtual Key 级 RPM、TPM 和并发限制；PRD-023 已接入 Virtual Key 级日预算规则。M0 仍不做 Key 审批、自动轮换、复杂额度、多层限流协同或项目绑定外键。项目/环境正式模型和 Key 轮换按后续 PRD 接入。

## Provider 管理边界

PRD-011 建立了控制面 Provider 管理能力：

- Provider 是当前租户的模型 Endpoint 元数据，所有查询和更新都必须带认证上下文中的 `tenant_id`。
- 支持国内模型厂商或云端 Endpoint 类型：智谱、通义、文心、火山、混元、DeepSeek，以及客户本地 OpenAI-compatible Endpoint。
- OpenRouter 和硅基流动只作为对标/竞品/迁移兼容对象，不作为默认模型厂商类别。
- `/providers` 全部使用 `require_current_admin`，developer/viewer 后端拒绝。
- Provider 注册 API 不接收 Provider Key 明文；Provider Key 由租户 KeyPool/Credential 管理，只保留上述受控本地环境兼容路径。
- 连接测试只记录 HTTP 状态、耗时和脱敏失败摘要；Provider 创建/更新运行日志不记录 URL 或 hash，持久审计只保存规范化完整 URL 的 `base_url_hash`。host/path/query/fragment、上游密钥和认证头不得落日志或审计明文。
- 控制台 Provider 面板仅 admin 可见，提供创建、测试连接、启用/禁用和 KeyPool 管理入口。

M0 不做供应商账户余额同步、复杂 KeyPool 轮换、动态路由策略、完整价格模型或供应商 SLA 管理。这些能力按后续 PRD 分阶段接入。

## Provider KeyPool 管理边界

PRD-014 建立了 Provider KeyPool 基础能力：
- KeyPool 隶属于当前租户的单个 Provider，所有查询、创建和更新都必须带认证上下文中的 `tenant_id`。
- `GET /providers/{id}/keys`、`POST /providers/{id}/keys` 和 `PATCH /provider-keys/{id}` 全部使用 `require_current_admin`，developer/viewer 后端拒绝。
- 控制面只返回 `provider_key_id`、`key_prefix`、`key_fingerprint`、`weight`、`status`、最近使用和最近错误元数据，不返回 `plain_value` 或 `encrypted_key`。
- 新增 Key 使用 shared-sdk 的 AES-GCM 工具加密落库；生产环境必须显式配置 `METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET`。
- 数据面按当前租户和 Provider 选择可用 Key，跳过 `disabled` 和未过期的 `rate_limited` Key，并在上游 429 后写入临时摘除状态。
- seed 阶段的 `local-placeholder` 旧 Key 不可解密，不参与真实 KeyPool 选择。没有托管 Key 时，环境 fallback 仍必须满足“非生产 + active 默认租户 + active Provider + code/数据库/冻结 Route Endpoint 全部匹配运维基准”；任一条件不满足都使用 `provider_key.unavailable` 失败关闭。
- 控制台 Provider 面板内嵌 KeyPool 区域，仅 admin 可见。

M0 不做余额同步、智能权重、分布式限流令牌桶、KMS 托管或跨 Provider 动态路由；这些能力按后续 PRD 接入。

## 模型目录管理边界

PRD-012 建立了控制面模型目录能力：

- ModelCatalog 是当前租户的标准模型目录，所有查询和更新都必须带认证上下文中的 `tenant_id`。
- `models.model_id` 是技术主键，用于日志和外键关联；`model_alias` 在 M0 作为调用方可见的内部标准模型 ID，对外响应字段为 `internal_model_id`。
- `provider_model_id` 是 Provider 上游真实模型 ID，数据面优先使用它替换 OpenAI-compatible 请求体中的 `model`。
- `GET /models` 使用 `require_model_catalog_reader`，admin/developer 可读，viewer 后端拒绝。
- `POST /models` 和 `PATCH /models/{id}` 使用 `require_current_admin`，developer/viewer 后端拒绝。
- 模型目录响应只包含模型元数据、能力、上下文、价格和状态，不返回 Provider Key、Virtual Key 或上游认证头。
- 控制台模型目录面板 admin/developer 可见；admin 可创建和启停模型，developer 只读。

M0 不做模型质量评分、备案标签、价格历史、多维计费模型、灰度路由、A/B 或成本优先路由。这些能力按后续 PRD 分阶段接入。

## 模型别名与最小路由边界

PRD-015 建立了稳定调用别名和最小路由能力：

- ModelAlias 是当前租户的路由配置对象，所有查询、创建和更新都必须带认证上下文中的 `tenant_id`。
- `GET /model-aliases` 使用 `require_model_catalog_reader`，admin/developer 可读，viewer 后端拒绝。
- `POST /model-aliases` 和 `PATCH /model-aliases/{id}` 使用 `require_current_admin`，developer/viewer 后端拒绝。
- 数据面用 Virtual Key 恢复 `tenant_id` 和 `environment`，按 `tenant_id + alias + environment` 精确匹配别名，不接受请求体覆盖租户或环境。
- 别名命中后优先选择默认模型；默认模型禁用或 Provider 不可用时，仅尝试管理员显式配置的备用模型。
- 未配置同名 ModelAlias 时，数据面保留 PRD-012 的内部模型 ID 直连兼容路径。
- 每次别名路由成功或失败都尽量写入 `routing_decisions`，记录请求别名、真实模型、Provider、选择原因和错误码；失败请求继续写入 `request_logs`。
- 控制台模型别名面板 admin/developer 可见；admin 可创建和启停别名，developer 只读。

M0 不做复杂 DSL、质量/成本打分、灰度路由、A/B 测试、跨 Provider 动态调度或流式中途 fallback。这些能力按后续 PRD 分阶段接入。

## Request Log 最小闭环边界
PRD-016 建立了控制台可查询的调用日志闭环：

- `request_logs` 是当前租户调用记录的查询主表，扩展保存 endpoint、请求模型、解析模型、Provider Code、token、成本估算、币种、价格版本和更新时间。
- 数据面成功请求写入 `request_logs` 与 `usage_ledger`；鉴权成功后的失败请求也尽量写入 `request_logs`，并保存统一 `error_code`。
- `usage_ledger` 写入后会把 token 与成本摘要回写到 `request_logs`，方便控制台分页列表直接展示。
- 控制面 `GET /request-logs` 和 `GET /request-logs/{request_id}` 只按认证上下文中的 `tenant_id` 查询，不接受前端传入租户 ID。
- 调用日志读取使用 `require_request_log_reader`，admin/developer 可读，viewer 后端拒绝。
- 控制台调用日志面板只展示 request_id、状态、模型、Provider、延迟、token、成本、错误码、路由摘要和 `key_prefix`；不展示 Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。

M0 不做全文检索、不保存完整 Prompt/Response 明文、不做日志导出、不做复杂 Trace 可视化；这些能力按后续 PRD 分阶段接入。

PRD-017 建立了用量与成本估算闭环：

- `usage_ledger` 扩展为请求级 UsageRecord，记录 `input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`、`cost_cny`、`cost_usd`、`estimated`、`usage_source` 和 `price_snapshot_id`。
- `price_snapshots` 保存入账时的模型价格、币种和 `price_schema_version`，用于解释历史成本，不做 M0 汇率回溯。
- Adapter 缺少上游 usage 时只按文本长度做估算，不保存 prompt/response 原文；控制台用“估算”标记避免财务误读。
- 控制面 `GET /usage/summary` 和 `GET /usage/records` 只按认证上下文中的 `tenant_id` 查询，不接受前端传入租户 ID。
- 用量读取使用 `require_usage_reader`，admin/developer 可读，viewer 后端拒绝。
- CSV 导出使用独立 `require_usage_exporter` 后端守卫，当前仅 admin 可创建、查询和下载，developer 的 Usage 只读权限不能替代批量导出授权。
- `usage_export_ledger_sequences.usage_export_sequence` 以 AUTOINCREMENT 为每条账本事实分配不复用序号；任务保存内部 `snapshot_ledger_sequence`，计数和 worker 生成复用同一租户水位。
- `usage_export_jobs` 保存有界同步/异步任务和 SQLite BLOB，`usage_export_events` 保存最小 `usage.exported` 完成事实；审计只记录安全汇总，不记录 CSV 或完整筛选。
- 0032 后 `usage_ledger` 与序列映射采用追加事实语义，已入账记录不能 UPDATE/DELETE；更正写新事实，不原地改写历史账本。

## 三级预算边界

PRD-023 建立租户和 Virtual Key 日预算；PRD-052 保留 Application 历史兼容能力，并将正式预算扩展为 `tenant/project/virtual_key` 三级累计 AND 门禁：

- `budget_rules` 保存当前租户、Environment 与可信 scope 的 CNY 日预算规则；`budget_alert_events` 按规则、业务日和阈值去重保存 `budget.threshold_reached` 与 `budget.exhausted`。
- `budget_evaluations` 保存请求级判断摘要；`budget_reservations` 保存 Provider 前原子预留；`budget_blocks` 与 `budget_block_events` 保存粘性阻断和恢复生命周期；0042 新增 append-only `budget_scope_events` 保存首次 `budget.scope_exceeded` 事实。
- 正式已用金额只读取 `finops_ledger_entries` 中 `status=posted + entry_kind=usage_charge + direction=debit` 的 CNY 整数微元，再叠加 outstanding reservation；`usage_ledger.cost_cny` 不再是预算聚合来源。
- 数据面按 `tenant -> project -> application（兼容） -> virtual_key` 可信归属链累计执行全部适用规则；任一 `block` 规则阻断时在 Provider 前返回带 `request_id` 的 402 `gateway.budget_exhausted`，且不产生 Provider attempt。
- 控制面 `GET /budgets`、create-only `POST /budgets`、兼容 `PATCH /budgets`、`PATCH /budgets/{id}/mode` 和 `GET /budget-status` 只使用认证 Session 的 `tenant_id`，并在后端校验 Environment/Project/Application/Key 归属。
- 预算读取使用 `require_budget_reader`，admin/developer 可读、viewer 后端拒绝；预算写入使用 `require_budget_writer`，仅 admin 可写，前端隐藏入口不能代替权限校验。
- 调用日志和所有预算事实只保存 scope、成本、阈值、动作与链路 ID，不保存 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key。
- 0042 为 Rule、Alert、Evaluation、Block、Reservation 与 ScopeEvent 增加租户、Environment、scope 和父规则归属触发器，作为服务层 tenant-qualified 查询之后的数据库防线。

当前不做部门分账、发票、充值余额、预算审批、自动降级到低价模型、跨币种实时汇率折算或 Redis 预算快照；这些能力按后续 PRD 接入。

## 基础 DLP 边界

PRD-024 建立了关键词/正则敏感词拦截能力：

- `dlp_rules` 保存当前租户的关键词或正则规则，M0 只支持租户级规则，不做项目、部门或环境维度规则。
- `dlp_matches` 保存请求级命中摘要，只记录规则 ID、规则名称、类型、动作、消息索引、哈希和长度，不保存命中文本明文。
- 控制面 `GET /dlp/rules`、`POST /dlp/rules` 和 `PATCH /dlp/rules/{rule_id}` 只按认证上下文中的 `tenant_id` 查询和写入，不接受前端传入租户 ID。
- DLP 规则管理使用 `require_dlp_admin`，M0 仅 admin 可写可读；developer/viewer 直接调用会被后端拒绝。
- 数据面只扫描 Chat Completions 请求中的 `role=user` 文本，不扫描 system/assistant/tool 消息、Embeddings input、文件、图片、工具结果或模型响应。
- 正则规则保存前和运行时都通过 `shared-sdk/python/metahub_shared/runtime/safe_regex.py` 校验；规则读取或正则评估失败时 fail closed，返回 `gateway.dlp_unavailable`。
- 调用日志保存 DLP 摘要字段，日志详情补充 `dlp_matches`，但不保存 Prompt、Response、Authorization、Virtual Key 明文、Provider Key 或命中文本。

M0 不做语义风险识别、多轮上下文风险累计、文件内容扫描、响应侧 DLP、复杂数据出境判断或安全审批流；这些能力按后续 PRD 接入。

## 控制台骨架与导航边界

PRD-018 建立了登录后的统一控制台主框架：

- `NavigationItem` 由后端按当前用户角色生成，前端不自行决定可见权限范围。
- `user_preferences` 只保存当前 `tenant_id + user_id` 的 active navigation 和侧边栏状态，不保存业务查询条件、密钥或 Prompt/Response。
- `console_page_events` 只写入 `console.page_viewed` 最小页面访问事件，payload 使用白名单并拒绝 Token、Authorization、Provider Key、Virtual Key、Prompt、Response、messages 和 input。
- Admin 可见 Provider、用户、模型、Key、日志、用量和系统入口；Developer 可见 Key、日志、用量、API 文档和调试台；Viewer 只保留低风险只读入口。
- 控制台隐藏入口只是体验层收敛，业务 API 仍必须由各自后端依赖校验 `tenant_id` 和角色权限。

## 开发者快速开始边界

PRD-019 建立了 5 分钟快速接入页：

- `GET /developer/quickstart-data` 只开放给 admin/developer，viewer 由后端返回 `auth.permission_denied`。
- SDKExample 和 QuickstartStep 由后端按当前 `tenant_id` 生成，不接受前端传入租户或角色。
- 示例覆盖 Python、JavaScript 和 curl，统一使用 Gateway `/v1/chat/completions` 的非流式调用。
- Virtual Key 明文不进入 API 响应、页面事件或复制属性；示例只使用 `METAHUB_VIRTUAL_KEY` 环境变量，后端最多返回脱敏 `key_prefix`。
- `developer_portal_events` 只记录 `developer.quickstart_viewed` 的安全元数据，不记录示例代码全文、Prompt、Response、messages、input、Authorization 或 Provider Key。

## 内置 API 调试台边界

PRD-020 建立了控制台内低频 API 调试能力：

- `POST /debug/chat-completions` 只开放给 admin/developer，viewer 由后端返回 `auth.permission_denied`。
- 调试请求只接收 `virtual_key_id`、`model_alias`、`messages`、`temperature` 和 `max_tokens` 等安全字段，不接收前端传入 `tenant_id`、Virtual Key 明文、Provider Key、Authorization 或密钥类字段。
- 后端只按认证上下文和当前租户 active Virtual Key 元数据恢复 `environment/key_prefix`，不读取或返回 `key_hash`。
- 调试编排复用数据面 `resolve_model_route`、Provider Adapter、`request_logs` 与 `usage_ledger` 写入能力，但该入口仍是低频控制面调试，不替代 Gateway `/v1` 热路径。
- `debug_console_events` 只保存 `debug.request_sent` 的安全元数据和状态，不保存 Prompt、messages content、完整 response、Virtual Key 明文、Provider Key 或 Authorization。
- PRD-021 后，调试台失败卡片展示 `diagnosis.reason/suggestion/action/severity/retryable`，但不展示上游原始错误正文。
- 控制台成功后可按 `request_id` 跳转调用日志详情；失败时展示统一错误码、request_id、trace_id 和 suggestion。

## 智能错误提示边界

PRD-021 建立了基础错误归因能力：

- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py` 保存静态 `ErrorDiagnosisRule`，M0 不新增规则表、不做租户自定义规则。
- `shared-sdk/python/metahub_shared/runtime/errors.py` 的 `ErrorResponse` 和 `ErrorCodeInfo` 都携带 `diagnosis`。
- `create_service_app` 暴露 `GET /error-codes`，该接口只返回全局只读元数据，不接收 `tenant_id`。
- Provider Adapter 只使用 HTTP 状态、Provider error type 和受控关键词归一错误，不把上游 body、headers、Prompt、Response 或密钥传入诊断输出。
- 调用日志详情按已有错误码派生诊断；M0 不扩展 `request_logs` 表。
- 前端 `AuthApiError`、`GatewayApiError` 保留 `diagnosis`，调试台和日志详情卡片统一 HTML 转义。

PRD-021 不做自动修复、RCA 模型、Key 自动轮换、工单创建或租户可编辑规则；这些能力只能按后续 PRD 独立接入。

## Provider Adapter 边界

PRD-013 建立了数据面 Provider Adapter 抽象：

- `ProviderAdapter` 是数据面内部接口，不暴露新的公开 HTTP API。
- 默认实现包括 `OpenAICompatibleAdapter` 和 `GenericHttpConfigAdapter`。
- `/v1/chat/completions` 和 `/v1/embeddings` 主流程只依赖 Adapter 工厂，不直接硬编码 Provider HTTP 细节。
- Adapter 负责构造上游 URL/headers/payload、发送非流式请求、打开流式请求、归一化 usage 和归一化上游错误；PRD-021 后会把 401/403、429、402/quota、404/model、timeout、400/422 归一到细分 `gateway.upstream_*` 错误码。
- `provider_adapter.error` 事件只记录 provider_code、adapter、HTTP 状态、错误码和 retryable 信息，不记录 Authorization、Provider Key、prompt、input 或 message。
- Provider Key 由 KeyPool 选择后进入上游请求头，不能进入响应、日志、request_logs 或 usage_ledger；上游 429 会把本次数据库 Key 临时标记为 `rate_limited`。

M0 不做全部国内厂商深度适配，也不做外部插件动态加载。后续新增厂商优先新增 Adapter，再由工厂按 `providers.provider_type` 路由。

## 数据边界

PRD-001 不创建业务表。`AppConfig` 和 `ServiceInfo` 是运行时 DTO，不落库。

从 PRD-003 开始，业务表必须默认包含 `tenant_id`，或能通过强外键追溯到 `tenant_id`。任何跨租户查询都必须有后端权限校验和审计记录。

## 单机部署边界

PRD-028 建立了单机 Docker Compose 部署边界：

- 默认 profile 为 `local-sqlite`，用于保证全新服务器能用一条命令启动 MVP。
- `infra/compose/.env.compose.example` 只能保存占位符；真实 `.env.compose` 由 `scripts/deploy/install.ps1` 生成并被 `.gitignore` 排除。
- `deployment.completed` 事件只写入 `.logs/deploy/deployment-events.jsonl`，只记录 profile、服务 URL 和 `request_id`，不记录管理员密码、Provider Key、Virtual Key、Token Secret、数据库密码或 MinIO Secret。
- `stop.ps1` 默认保留 volume；删除 volume 必须同时传入 `-RemoveVolumes` 和 `-ConfirmRemoveVolumes`。
- `compose-full` 会启动无持久化 Redis、PostgreSQL 16、MinIO、bucket 初始化和独立 Backup Worker；运行时可
  显式选择 SQLite 本地/测试或 PostgreSQL 连接池适配器，数据库失败不得跨后端回退；PostgreSQL 服务当前
  只用于适配器集成，native migration bundle 完成前不构成生产发布能力。
- 该 PRD 不包含 Kubernetes/Helm、高可用、TLS、域名、反向代理和生产密钥托管。
## PRD-030 Environment 隔离目录

- `control-plane-api/src/metahub_control/environments/`：Environment DTO、管理接口、租户绑定解析、审计事件与 Webhook 调度。
- `gateway-data-plane/src/metahub_gateway/auth/virtual_key.py`：从 Virtual Key 恢复可信环境上下文，调用方不能覆盖。
- `gateway-data-plane/src/metahub_gateway/routing/`、`budgets/`、`usage/`：按环境执行别名解析、预算准入、日志与用量归因。
- `console-web/src/features/environments/`、`console-web/src/lib/environments.js`：环境设置页和 API 客户端；admin 可写、developer 只读。
- `infra/migrations/0021_environment_isolation.sql`：环境主数据、事件、资源外键、历史回填和租户默认 dev 触发器。
- `control-plane-api/tests/integration/test_environments.py`、`console-web/tests/unit/environments.test.mjs`：环境解析、生产 Key 误用阻断、权限和前端展示测试。

## PRD-031 模型价格目录

- `control-plane-api/src/metahub_control/model_prices/`：有效期价格 DTO、查询/创建 API、Decimal 校验、审计事件和 Webhook。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`：按服务器 UTC 时间选择当前 PricePlan，并创建不可变 PriceSnapshot。
- `console-web/src/features/model-prices/`、`console-web/src/lib/modelPrices.js`：价格时间线、状态筛选和管理员新增价格交互。
- `infra/migrations/0022_model_price_effective_time.sql`：价格计划、价格事件及快照计划关联字段。
- `control-plane-api/tests/integration/test_model_prices.py`：价格生效切换、历史 Usage 不变、冲突、金额校验和 RBAC 测试。

## PRD-038 Python 示例目录

- `examples/python/`：独立可安装示例项目，README 与 `.env.example` 说明 base URL、Virtual Key、模型、超时和重试配置。
- `examples/python/src/metahub_gateway_examples/`：官方 OpenAI 客户端创建、Chat、Embeddings、成功 request_id 和安全错误格式化入口。
- `control-plane-api/src/metahub_control/developer_portal/`：按认证租户返回示例元数据，并从固定文件白名单构建 ZIP。
- `infra/migrations/0028_python_sdk_examples.sql`：扩展开发者门户事件约束，支持 `sdk.example_downloaded`。
- `console-web/src/features/python-sdk-examples/`、`console-web/src/lib/pythonSdkExamples.js`：受保护下载页、Blob 下载和统一错误反馈。
- `examples/python/tests/`、`control-plane-api/tests/integration/test_python_sdk_examples.py`、`console-web/tests/unit/pythonSdkExamples.test.mjs`：示例运行、归档安全、租户/RBAC 和前端交互测试。

## PRD-039 JavaScript/Node 示例目录

- `examples/js/`：独立 Node.js 22+ ESM 示例，lockfile 固定官方 OpenAI 客户端版本，README 与 `.env.example` 说明服务端 Key 配置。
- `examples/js/src/`：客户端创建、非流式 Chat、SSE 异步迭代、总流式时限、安全 request_id 和错误格式化入口。
- `control-plane-api/src/metahub_control/developer_portal/`：按认证租户返回 JavaScript 元数据，并以双语言共用的严格白名单归档器构建 ZIP。
- `console-web/src/features/python-sdk-examples/`、`console-web/src/lib/pythonSdkExamples.js`：Python/JavaScript 分段视图、Node 安全提示、受保护 Blob 下载和统一错误反馈。
- `examples/js/tests/`、`control-plane-api/tests/integration/test_javascript_sdk_examples.py`、`console-web/tests/unit/pythonSdkExamples.test.mjs`：真实 SDK/SSE、流式异常、归档安全、租户/RBAC 和双语言交互测试。

## PRD-040 从直连模型厂商迁移指南目录

- `console-web/src/features/migration-guide/`：迁移指南展示模型、显式 Environment/alias 选择、Python/Node 前后代码、错误差异、治理安全回滚和独立复制反馈。
- `control-plane-api/src/metahub_control/console/`：角色化导航、可信 Gateway `/v1` 地址和 `migration.guide_viewed` 事件的后端配对、路径、枚举与敏感值校验。
- `control-plane-api/src/metahub_control/model_aliases/`：为迁移页提供默认模型和 Provider 的只读可用状态，继续按认证租户隔离。
- `infra/migrations/0029_migration_guide_event.sql`、`infra/migrations/0029_migration_guide_event.down.sql`：扩展控制台访问事件约束，并支持保留历史记录的可逆迁移。
- `shared-sdk/python/metahub_shared/db/migrate.py`：保证显式事务迁移失败时先回滚，不提交半完成结构。
- `control-plane-api/tests/integration/test_migration_guide.py`、`console-web/tests/unit/migrationGuide.test.mjs`：可信配置、事件/RBAC/租户隔离、内容、交互、去重与安全测试。

## PRD-041 竞品定位与能力映射目录

- `console-web/src/features/competitor-comparison/`：版本化平台定位、当前能力矩阵、售前规范、官方来源和后端可见导航过滤。
- `console-web/src/lib/consoleVisitTracker.js`：迁移指南与竞品帮助页共用的渲染后唯一访问状态机。
- `control-plane-api/src/metahub_control/console/`：帮助中心导航、`help.competitor_viewed` 枚举与 event/nav/path/hash/source/viewport 严格配对。
- `control-plane-api/src/metahub_control/providers/`：继续执行 Provider 类型白名单，拒绝把竞品平台名称登记为模型厂商。
- `infra/migrations/0030_competitor_help_event.sql`、`infra/migrations/0030_competitor_help_event.down.sql`：扩展控制台事件约束并支持保留历史的可逆迁移。
- `control-plane-api/tests/integration/test_competitor_positioning.py`、`console-web/tests/unit/competitorComparison.test.mjs`：角色、租户、事件、定位内容、权限过滤、ARIA 和 Provider 分类测试。

## PRD-042 Webhook 事件过滤与重放基础目录

- `control-plane-api/src/metahub_control/webhooks/`：详情 DTO/路由、100 条有界筛选、failed 重放、Secret 轮换、统一错误和租户/RBAC 校验。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`、`webhook_delivery.py`：共享安全信封、递归敏感键拒绝、当前 Secret/版本重读、HMAC 和原始投递兼容逻辑。
- `control-plane-api/src/metahub_control/audit/service.py`：`webhook_delivery.replayed` 与 `webhook_endpoint.secret_rotated` 安全摘要白名单。
- `infra/migrations/0031_webhook_replay_secret_rotation.sql`、对应 down migration：Secret 版本、replay 关联、部分唯一索引、租户所有权触发器和可逆回滚。
- `console-web/src/features/webhooks/`、`console-web/src/lib/webhooks.js`、`console-web/src/app/main.js`：详情筛选、刷新、重放、轮换确认、一次性 Secret/反馈会话清理和定时刷新防重入。
- `control-plane-api/tests/integration/test_webhooks.py`、`test_db_migrations.py`、`console-web/tests/unit/webhooks.test.mjs`：主流程、异常/权限、并发幂等、签名竞态、迁移往返和响应式前端回归。

## PRD-043 用量 CSV 导出目录

- `control-plane-api/src/metahub_control/usage/export_service.py`：tenant-qualified 计数、稳定序列快照、固定 42 列 CSV、任务状态、lease/retry、完整性校验、下载审计和完成事件。
- `control-plane-api/src/metahub_control/usage/scheduler.py`、`routes.py`、`models.py`：Control API 生命周期调度、admin-only 创建/状态/下载路由和稳定 DTO；Gateway 热路径不运行导出 worker。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`audit/service.py`：独立导出权限守卫及 requested/downloaded 安全审计白名单。
- `shared-sdk/python/metahub_shared/runtime/config.py`、`errors.py`、`webhooks.py`、`fastapi_app.py`：有界导出配置、统一错误、可订阅 `usage.exported` 和 `Content-Disposition` CORS 暴露。
- `infra/migrations/0032_usage_csv_export.sql`、对应 down migration：只增账本序列、任务/BLOB、完成事件、状态/租户/不可变触发器、审计动作和可逆回滚。
- `console-web/src/lib/usage.js`、`src/features/usage/renderUsagePanel.js`、`src/app/main.js`、`src/styles/main.css`：Bearer 文件下载、幂等 request_id、202 轮询/继续查询、Abort/会话清理、admin-only 命令和 900/390 窄宽度 9 字段标签。
- `console-web/src/lib/consoleShell.js`、`src/lib/environments.js` 及对应单元测试：裁剪畸形/超长/越权 hash 并固定回退 overview；viewer 不预取 Environment，避免隐藏 Usage/Environment 页面仍产生权限请求。
- `control-plane-api/tests/integration/test_usage_export.py`、`test_usage.py`、`test_audit_logs.py`、`test_webhooks.py`、`test_db_migrations.py`、`console-web/tests/unit/usage.test.mjs`：同步/异步、异常/权限、租户、审计/事件、安全、迁移和响应式交互回归。
- `scripts/acceptance/prd043-browser-acceptance.mjs`、`artifacts/prd043/`：真实 Chrome 验证 8677 bytes、15 行、42 列、BOM/CRLF、公式防护和 SHA-256 CSV 合同；900x800 表格/记录宽 562、390x844 宽 318 且均无局部横向滚动；角色/hash 边界通过，三张截图以灰色 mask 移除邮箱、租户和 Session 文本。

## PRD-044 策略引擎 DSL 最小版目录

- `shared-sdk/python/metahub_shared/runtime/policy_dsl.py`：白名单词法、解析、canonical AST/hash、单表达式与策略集纯求值、可解释结果和超时 fail-closed；禁止脚本、函数、属性访问与外部副作用。
- `shared-sdk/python/metahub_shared/runtime/config.py`、`errors.py`、`error_diagnosis.py`、`webhooks.py`、`fastapi_app.py`：1-100 ms 有界时间预算、固定策略错误/诊断、两类事件白名单、`If-Match` CORS 和策略响应 no-store。
- `control-plane-api/src/metahub_control/policies/`：Policy DTO、admin-only 列表/校验/创建/PATCH、tenant-qualified 查询、事务内版本/route target 复核、审计、失败事件和提交后 Webhook 调度。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`audit/service.py`、`console/service.py`、`app.py`：独立策略管理员守卫、安全审计摘要、admin-only 导航和路由注册。
- `infra/migrations/0033_policy_engine_minimal_dsl.sql`、对应 down migration：Policy/Rule/Action/Event 四表、租户复合外键、身份/版本/不可变触发器、事件部分唯一索引和保留历史的 audit 往返重建。
- `console-web/src/lib/policies.js`、`src/features/policies/renderPoliciesPanel.js`、`src/app/main.js`、`src/styles/main.css`：无 tenant_id 客户端、active-only 加载、内存 dirty draft、revision/AbortController 防旧响应、If-Match 保存、转义渲染和响应式策略列表。
- `control-plane-api/tests/unit/test_policy_dsl.py`、`tests/integration/test_policies.py`、`test_db_migrations.py`、`gateway-data-plane/tests/unit/test_runtime_config.py`、`console-web/tests/unit/policies.test.mjs`：DSL/配置、API 主流程与故障注入、租户/RBAC、迁移往返和前端状态安全专项。
- `scripts/acceptance/prd044-browser-acceptance.mjs`、`artifacts/prd044/`：真实 Chrome 的 admin 创建/更新/非法草稿、developer/viewer 越权回退、三档布局、隐私遮罩截图和运行库 0033 前备份证据。
- `docs/prd/PRD-044-policy-engine-minimal-dsl.md`、`docs/testing/PRD-044-tests.md`：两轮设计自审、Excel/架构验收映射、专项/全量回归、运行库、真实 API、敏感扫描和浏览器完整验收证据。

## PRD-045 策略快照版本化与发布目录

本节登记已完成并通过运行验收的实现边界；最终批次和可复核统计以测试记录为准。

- `shared-sdk/python/metahub_shared/runtime/policy_snapshot.py`：严格 wire schema、tenant-bound content hash、完整 artifact hash、反序列化重编译和 frozen 快照对象；未知字段、重复 JSON key、降版/篡改和资源越界均失败关闭。
- `shared-sdk/python/metahub_shared/runtime/config.py`、`errors.py`、`error_diagnosis.py`、`webhooks.py`：快照轮询/LKG 有界配置、生产启动门禁、统一错误/诊断和 `policy_snapshot.published` 事件白名单；`.env.example` 保存可运行的非敏感配置示例。
- `control-plane-api/src/metahub_control/policy_snapshots/`：admin-only current/history/publish/rollback DTO、路由与事务服务，以及独立内部 Token 的 tenant current wire/ETag 入口。公共 DTO 不返回 DSL/AST，发布锁内重编 active Policy 并执行 head/candidate 双 CAS。
- `control-plane-api/src/metahub_control/app.py`、`auth/dependencies.py`、`audit/service.py`、`console/service.py`：快照路由注册、后端管理员守卫、安全审计动作、admin-only `policy-snapshots` 导航与强 ETag CORS 合同。
- `gateway-data-plane/src/metahub_gateway/policies/cache.py`、`runtime.py`、`app.py`：按 tenant singleflight/后台轮询、per-tenant ETag/304、不可变 lease、LKG/emergency LKG/硬过期，以及 lifespan 安装和纯 Policy 求值。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`、`routing/models.py`、`budgets/service.py`、`usage/ledger.py`：读取 JSON 前冻结 lease/claim，构建可信上下文，执行 `deny/route_to`，重新验证受控路由，计算只读预算余额，并把版本/状态/安全决策贯穿日志与 Usage。
- `control-plane-api/src/metahub_control/request_logs/`、`debug_console/service.py`：日志列表/详情增加 `snapshot_version` 筛选和安全摘要；低频调试入口也先 claim，但固定 `snapshot_version=NULL/policy_snapshot_status=disabled`。
- `infra/migrations/0034_policy_snapshot_versioning.sql`、对应 down migration：快照 header/item/head/event、请求 claim、同租户复合 request log/Usage/price 关联、不可变触发器、审计/Webhook 合同和遇重复 request_id 时安全中止的可逆回滚。
- `console-web/src/lib/policySnapshots.js`、`src/features/policy-snapshots/renderPolicySnapshotsPanel.js`、`src/app/main.js`、`src/styles/main.css`：强 CAS API 客户端、发布/上一版回滚、候选漂移、稳定 request_id 重试、admin-only 导航、响应式历史和内存短生命周期状态。
- `console-web/src/lib/requestLogs.js`、`src/features/request-logs/renderRequestLogPanel.js`：实际快照版本筛选、状态和 Policy 安全摘要展示，不展示表达式、上下文实际值、Prompt 或 Response。
- `gateway-data-plane/tests/unit/test_policy_snapshot.py`、`test_policy_snapshot_cache.py`、`tests/integration/test_openai_compatible_api.py`、`control-plane-api/tests/integration/test_policy_snapshots.py`、`test_request_logs.py`、`test_debug_console.py`、`test_db_migrations.py`、`console-web/tests/unit/policySnapshots.test.mjs`、`requestLogs.test.mjs`：shared、缓存/LKG、热路径、API/RBAC/租户、claim、迁移和前端状态专项。
- `scripts/acceptance/prd045-runtime-acceptance.py`、`prd045-browser-acceptance.mjs`：真实 API/Gateway/内部身份/Webhook/Usage/敏感扫描，以及真实 Console 发布/回滚、RBAC、调用日志、三档布局和精确资源清理。
- `artifacts/prd045/`：运行库迁移前备份、成功证据库、批次化三服务日志和遮罩截图；失败批次保留用于复盘，最终结论只引用测试记录指定的成功批次。
- `docs/prd/PRD-045-policy-snapshot-versioning-publishing.md`、`docs/testing/PRD-045-tests.md`：两轮设计自审、实现边界、专项/全量、运行库、真实 API、敏感扫描和浏览器完整验收证据。

## PRD-046 策略模拟与单请求预演目录

本节登记已完成并通过运行验收的实现边界；最终批次、制品 hash 和可复核统计以测试记录为准。

- `shared-sdk/python/metahub_shared/runtime/policy_preview.py`：无数据库、网络或 Provider 副作用的路由、DLP、token/成本和预算预演合同；所有集合、树节点和比较结果有明确上限，不保存 Prompt 或实际比较值。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`、`webhooks.py`、`fastapi_app.py`：Dry-Run 固定错误/诊断、`policy.dry_run_executed` 精确事件信封和 no-store/request_id/trace_id 公共响应合同。
- `control-plane-api/src/metahub_control/policies/dry_run_service.py`、`models.py`、`routes.py`：严格 DTO、256 KiB 有界 JSON、actor/tenant limiter、query-only tenant 快照、Policy/route/DLP/budget 短路、三事实原子写入、重复 request_id 恢复和提交后 Webhook。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`audit/service.py`、`console/service.py`、`app.py`：`require_policy_admin` 后端守卫、Dry-Run 审计白名单、admin-only `policy-simulator` 导航和路由注册；请求体不能选择 tenant。
- `console-web/src/lib/policyDryRun.js`、`src/features/policy-dry-run/renderPolicyDryRunPanel.js`、`src/app/main.js`、`src/styles/main.css`：正式 DTO client、兼容性/决策树/规则/路由/DLP/预算视图、revision/Abort 状态机、Prompt 生命周期清理、焦点恢复和 1280/900/390 响应式约束。
- `infra/migrations/0035_policy_dry_run.sql`、对应 down migration：tenant-bound `policy_explanations`、Policy event/audit 扩展、三事实部分唯一索引、不可变触发器、Webhook 订阅合同和 0034 历史保真回滚。
- `control-plane-api/tests/unit/test_policy_preview.py`、`test_policy_dry_run_contracts.py`、`tests/integration/test_policy_dry_run.py`、`test_policies.py`、`test_webhooks.py`、`test_db_migrations.py`、`console-web/tests/unit/policyDryRun.test.mjs`：纯计算、API 主/异常/RBAC/租户、三事实/Webhook、零成本、迁移和前端状态专项。
- `scripts/acceptance/prd046-runtime-acceptance.py`、`prd046-browser-acceptance.mjs`：真实三服务/API/零成本/隐私/清理与真实 Console/RBAC/三档布局验收。runtime 对所有 mutation 预登记唯一 intent，以有限超时、登录 Session 围栏、前后 settle、ID coalesce、最终数据库收敛和活动残留 0 处理结果未知路径。
- `artifacts/prd046/`：运行库迁移前备份、最终 0035 证据库、批次化服务/验收日志和遮罩截图；诊断批次保留用于复盘，最终结论只引用测试记录指定的成功批次和完整 hash。
- `docs/prd/PRD-046-policy-dry-run-single-request-preview.md`、`docs/testing/PRD-046-tests.md`：需求设计、两轮自审、实现期回写、专项/全量、0035、真实 API、结果未知补偿、敏感扫描和浏览器完整验收证据。

## PRD-047 成本优先与可用性优先路由目录（已完成）

本节登记已通过的设计、主体实现、自动化、0036 运行库、三服务 API/Webhook/Usage、隐私清理和三档浏览器证据。最终成功批次为 `artifacts/prd047/20260719T145100`，完整 hash 与作废批次见测试记录。

- `shared-sdk/python/metahub_shared/runtime/routing_policy.py`：最多两个 default/fallback 候选的冻结数据结构、mode-aware 资格过滤、确定性 `fixed/cost_first/availability_first` 两阶段排序、不可逆 Token 长度估算、15 位 Decimal 成本和稳定原因码；不接收 DB、网络、日志或凭据。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`、`webhooks.py`：ModelAlias/Gateway 路由统一错误与诊断，`route.policy_selected` 的 `configuration_changed`、`request_selected` 两种精确安全载荷和稳定 `rpe_` 信封约束。
- `control-plane-api/src/metahub_control/model_aliases/models.py`、`routes.py`、`service.py`：GET RoutingPolicy/配置候选摘要，认证后 8 KiB 严格 JSON reader，admin-only routing-mode PATCH，锁内 CAS/no-op、tenant+actor+request_id 恢复、审计/配置事件同事务和提交后 Webhook。
- `control-plane-api/src/metahub_control/policies/dry_run_service.py`：按 Dry-Run canonical input 读取安全 Health/KeyPool/价格快照并复用 shared 排序器；不解密 Key、不调用 Provider。
- `control-plane-api/src/metahub_control/request_logs/models.py`、`service.py`：调用日志详情增加冻结 Policy/version/mode、价格时点/Token 估算和最多两条强类型 RoutingCandidate；展示 JOIN 继续按 tenant 限定。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`audit/service.py`、`console/service.py`、`app.py`：GET 的 admin/developer reader、PATCH 的 admin 后端守卫、RoutingPolicy 审计白名单、`routing-policies` 角色导航和路由注册。
- `gateway-data-plane/src/metahub_gateway/routing/models.py`、`providers/keypool.py`：同一短事务冻结 Alias/Policy/候选和 mode 实际需要的 Health/KeyPool/价格/Fallback，原子写 Decision/Candidate；动态模式解释事实不可写时在 Provider 前失败关闭。环境 Key 的快照与实际选择共用非生产/default tenant/code/基准 Endpoint 边界，其他租户不得借同名 Provider 复用全局 Secret。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`、`events/dispatcher.py`：初始选路成功提交后把稳定 `request_selected` 事件放入有界异步队列，不让 Webhook I/O 反压 Provider 热路径。
- `console-web/src/lib/modelAliases.js`、`src/features/routing-policies/renderRoutingPolicyPanel.js`、`src/app/main.js`、`src/styles/main.css`：PAGE-067 API client、admin 草稿/CAS/稳定 request ID/迟到响应门禁、developer 只读、viewer 无 DOM/预取，以及桌面/窄桌面/移动响应式布局。
- `infra/migrations/0036_cost_availability_routing.sql`、对应 down migration：`model_alias_routing_policies`、`routing_policy_events`、`routing_candidates`、`budget_reservations.outstanding_cost_cny`，Decision/Audit 重建、复合 owner 关系、精确版本与不可变事实约束，以及 0035 历史恢复。Audit 保留行与链路字段，但不可逆清理旧 Provider `base_url`；down 不伪造或恢复 URL。
- `shared-sdk/python/tests/test_routing_policy.py`、`test_routing_policy_contracts.py`、`control-plane-api/tests/integration/test_model_aliases.py`、`test_policy_dry_run.py`、`test_request_logs.py`、`test_providers.py`、`test_db_migrations.py`、`gateway-data-plane/tests/unit/test_provider_keypool.py`、`test_provider_adapters.py`、`tests/integration/test_model_alias_routing.py`、`test_openai_compatible_api.py`、`test_budget_guard.py`、`test_request_log_minimal_loop.py`、`console-web/tests/unit/routingPolicies.test.mjs`、`prd047BrowserAcceptance.test.mjs`：纯算法、统一错误、wire/CAS/幂等/RBAC/租户、动态路由、Dry-Run parity、强类型日志、环境 Key 所有权/Endpoint、Provider URL 日志/审计、CNY Budget/未结算分项、迁移、前端异步状态与 Browser 安全收口专项。
- `scripts/acceptance/prd047-runtime-acceptance.py`、`prd047-browser-acceptance.mjs`：已通过真实三服务/API、第二租户、无 KeyPool、Webhook HMAC、RequestLog/Usage/隐私/清理和三档 Chrome 验收。Browser 使用精确回环 origin、DOM/URL/storage 扫描、PNG 非空像素、owner/命名图、tenant-qualified 精确影响行和 finally cleanup。
- `docs/prd/PRD-047-cost-first-availability-first-routing.md`、`docs/api/routing-policies.md`、`docs/testing/PRD-047-tests.md`：需求理解、两轮设计自审、实现期第三轮安全复审、ModelAlias wire/CAS/幂等/RBAC/租户、Gateway 模式、预算分项、Provider Key/URL 安全、专项/全量和最终运行验收证据。

## PRD-048 效果-成本路由基础目录（已完成）

本节登记 PRD-048 最终模块边界。设计、两轮设计自审、实现后安全复审、专项/全量回归、0037 迁移、真实三服务/API/Webhook、停止后数据库与日志复核，以及 1280/900/390 三档浏览器验收均已通过；权威证据批次为 `artifacts/prd048/20260719T200349`。

- `shared-sdk/python/metahub_shared/runtime/routing_policy.py`：在 PRD-047 候选资格与最小依赖模型上增加 `QualityCostConfig`、租户内冻结 Score 事实、`quality_cost_v1` 两阶段门槛/冷启动/依赖矩阵，以及整数 bps、Decimal 成本和延迟分量的确定性综合分；纯函数不读取数据库、网络或凭据。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`、`webhooks.py`、`i18n.py`：模型评分、质量快照和无可用路由的统一错误/诊断标签；`model.feedback_recorded` 精确安全 payload；RoutingPolicy v2 配置事件允许完整质量配置，但运行时 `request_selected` 不扩散权重、Score 或分量。
- `control-plane-api/src/metahub_control/model_quality_scores/models.py`、`routes.py`、`service.py`：`GET/PATCH /model-quality-scores` 的无缓存分页读取、认证后有界严格 JSON、admin/developer 读与 admin 写、独立 `feedback_version` CAS、tenant+actor+request_id 幂等恢复，以及评分、审计、事件同事务和提交后 Webhook。
- `control-plane-api/src/metahub_control/model_aliases/`：RoutingPolicy DTO 始终返回五字段 `quality_cost_config`；旧三模式继续使用两字段 PATCH，`quality_cost` 必须提交完整配置。mode 或配置真实变化共用一次版本推进、审计和 v2 配置事件，切回旧模式保留休眠配置。
- `control-plane-api/src/metahub_control/policies/dry_run_service.py`、`policies/models.py`：Dry-Run 从同一只读事务读取当前 Policy、Score 和按需价格，复用 shared 公式并返回冻结公式、证据与分量；不调用 Provider、不累计 Score、不写 Decision/Usage，也不新增运行时或反馈 Webhook。
- `control-plane-api/src/metahub_control/request_logs/models.py`、`service.py`：详情只读取不可变 Decision/Candidate 行，返回质量时点、公式、权重、门槛、Score ID、统计/反馈版本、证据、计数、人工评分、延迟、三分量和总分；禁止 JOIN 当前 Score 回算历史。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`audit/service.py`、`console/service.py`、`app.py`：模型评分 reader/writer 后端守卫、评分审计白名单、`model-quality-scores` admin/developer 导航和路由注册；viewer 前端隐藏不替代后端拒绝。
- `gateway-data-plane/src/metahub_gateway/quality/observations.py`、`routing/attempts.py`、`streaming/sessions.py`：调用方显式决定真实上游样本资格；attempt/session 第一次终态和 Score 单样本增量共用事务，重复终态、本地 DLP/预算/KeyPool/Adapter/审计故障和客户端断开不重复或错误累计。
- `gateway-data-plane/src/metahub_gateway/routing/models.py`：同一租户短事务按需读取 Score，先执行质量门槛和冷启动，再决定是否读取延迟/Token/价格；Decision/Candidate 原子冻结所有解释字段。低质或 cold fallback 不得绕过门禁重新进入 PRD-036 运行时 Fallback。
- `console-web/src/lib/modelQualityScores.js`、`src/features/model-quality-scores/renderModelQualityScoresPanel.js`：PAGE-069 无 tenant_id API client、admin 行级人工评分草稿/CAS/稳定 request ID/迟到响应门禁、developer 只读、viewer 无 DOM/预取、筛选分页和 XSS 转义。
- `console-web/src/lib/modelAliases.js`、`src/features/routing-policies/renderRoutingPolicyPanel.js`、`src/features/request-logs/renderRequestLogPanel.js`、`src/features/developer-start/renderDeveloperStart.js`、`src/app/main.js`、`src/styles/main.css`：PAGE-068 四模式与精确 bps 权重合计、PAGE-069 active-only 生命周期、RequestLog 冻结质量解释，以及 1280/900/390 响应式布局。
- `infra/migrations/0037_quality_cost_routing.sql`、对应 down migration：Score/Event 新表、存量 Model 与可计数终态回填、RoutingPolicy/Decision/Candidate/Event/Audit 重建、复合 owner/版本/不可变与字段级更新约束。down 在任何正式修改前校验自动统计全字段 manifest 和业务事实；缺源/坏源/不一致统计固定报 `prd048_down_automatic_statistics_unrebuildable`，quality mode/Decision/Event/Audit/Webhook、fixed 非默认休眠配置、v2 created 非默认历史或清除后 feedback 元数据固定报 `prd048_down_business_facts_present`。
- `shared-sdk/python/tests/test_routing_policy.py`、`test_routing_policy_contracts.py`、`control-plane-api/tests/integration/test_model_quality_scores.py`、`test_model_aliases.py`、`test_policy_dry_run.py`、`test_request_logs.py`、`test_db_migrations.py`、`gateway-data-plane/tests/integration/test_model_alias_routing.py`、`test_openai_compatible_api.py`、`console-web/tests/unit/modelQualityScores.test.mjs`、`routingPolicies.test.mjs`、`requestLogs.test.mjs`、`prd048BrowserAcceptance.test.mjs`、`dashboard.test.mjs`：算法、事件、评分 API、CAS/RBAC/租户、迁移、Gateway 路由矩阵、Console 异步状态、浏览器隐私和 900px 响应式专项，均已纳入最终全量或独立门禁。
- `scripts/acceptance/prd048-runtime-acceptance.py`、`prd048-browser-acceptance.mjs`：真实三服务、双本地上游、三角色、第二租户、Webhook HMAC、冻结 RequestLog、敏感扫描、精确幂等清理，以及三视口 DOM/storage/network/PNG 验收。Runtime 状态只保存脱敏 ID 和布尔/计数证据，密码、Token、Key、Prompt 与 Response 仅驻留进程内存。
- `docs/prd/PRD-048-quality-cost-routing-foundation.md`、`docs/api/routing-policies.md`、`docs/testing/PRD-048-tests.md`：需求、两轮设计自审、实现后第三轮安全复审、公开 API/NULL 合同，以及专项、全量、迁移、Runtime、Browser、日志、清理和停止后最终证据。

## PRD-049 调用反馈与采纳率记录目录（已完成）

本节登记 PRD-049 最终模块边界。设计与多轮自审、实现和不可变加固、专项/全量回归、0038/0039 正反迁移、真实三服务、Runtime、Browser、隐私与停止后数据库验收均已完成；权威最终批次为 `artifacts/prd049/20260720T181908`。该状态只代表 PRD-049 完成，不代表后续 PRD 或全部 108 条均已完成。

- `shared-sdk/python/metahub_shared/feedback/{contracts.py,eligibility.py,storage.py,validation.py,webhook.py,wire.py,__init__.py}`：双服务共享 actor、严格 POST DTO/标签规范化、意图指纹、可反馈资格、tenant-qualified SQLite 事务、Summary 查询、公开 wire 与 `feedback.created` 安全 payload；共享包不反向依赖 Control 或 Gateway。
- `shared-sdk/python/metahub_shared/runtime/{errors.py,fastapi_app.py,error_diagnosis.py,i18n.py,webhooks.py}`：Feedback 统一错误码/诊断/中英文目录与事件白名单；FastAPI 工厂统一 405 `infra.method_not_allowed`、`Allow`、request_id/trace_id、禁缓存和 CORS 行为，不把路由默认异常体泄露给客户端。
- `shared-sdk/python/metahub_shared/db/migrate.py`：正向迁移、回滚、空迁移和失败路径都由外层 `finally` 关闭 SQLite 连接，避免 Windows 验收库被遗留句柄占用。
- `control-plane-api/src/metahub_control/feedback/{models.py,routes.py,service.py,summary_runtime.py,__init__.py}`：Session 双角色创建、Control-only Summary、认证前后严格 body/query 边界、250ms 绝对 deadline、单进程聚合门禁，以及 commit 后 Webhook 投递。
- `control-plane-api/src/metahub_control/request_logs/{models.py,service.py}`、`model_quality_scores/{models.py,service.py}`：RequestLog 列表/详情投影 `has_feedback`、不可变反馈与可反馈资格；ModelQuality 只读增加逐请求反馈计数、评分、采纳率和五档分布，不改写 PRD-048 自动样本、人工评分或历史路由解释。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`console/service.py`、`app.py`：Feedback reader/writer 后端 RBAC、PAGE-071 admin/developer 导航、viewer 裁剪和 Feedback 路由注册；前端隐藏入口不替代后端权限校验。
- `gateway-data-plane/src/metahub_gateway/feedback/{models.py,routes.py,service.py,__init__.py}`、`gateway-data-plane/src/metahub_gateway/app.py`：仅注册 Virtual Key `POST /feedback`，恢复同租户同 Key actor、复用共享领域事务并在提交后异步入队事件；不注册 `GET /feedback/summary`。
- `console-web/src/lib/requestFeedback.js`、`src/features/request-feedback/renderRequestFeedback.js`、`src/features/request-logs/renderRequestLogPanel.js`、`src/features/developer-start/renderDeveloperStart.js`、`src/app/main.js`、`src/styles/main.css`：PAGE-070 提交/已记录只读态和 PAGE-071 报表；API client 不发送 tenant/actor，页面按 identity/tenant/navigation/generation/Abort owner 隔离异步状态，筛选后等待四项指标、唯一模型行和全部标签 DOM 收敛再截图。
- `infra/migrations/0038_request_feedback.sql`、`0038_request_feedback.down.sql`：创建 `request_feedbacks`、`request_feedback_events`、`model_feedback_statistics`，增加复合租户/actor/源 RequestLog/final Attempt 图、不可变事实、聚合、事件、索引与六类业务事实回滚门禁。
- `infra/migrations/0039_request_feedback_immutability_hardening.sql`、`0039_request_feedback_immutability_hardening.down.sql`：不修改已应用 0038 checksum，新增 Feedback/Event 冲突 INSERT、已反馈 RequestLog 与最终 RoutingAttempt 冲突 INSERT/UPDATE 共六个守卫，阻断 `INSERT/UPDATE OR REPLACE` 改写；down 先确认六类事实不存在再原子移除加固。
- `shared-sdk/python/tests/test_feedback.py`、`test_feedback_concurrency_side_effects.py`、`control-plane-api/tests/integration/test_request_feedback.py`、`test_feedback_summary_resources.py`、`test_prd049_migration.py`、`test_db_migrations.py`、`test_request_logs.py`、`test_model_quality_scores.py`、`gateway-data-plane/tests/integration/test_gateway_request_feedback.py`、`test_observability_errors.py`、`gateway-data-plane/tests/unit/test_error_codes.py`：双入口、租户/RBAC/防枚举、幂等/并发副作用、Summary 资源门禁、RequestLog/ModelQuality、统一 405、0038/0039 十三个冲突分支和正反迁移专项。
- `console-web/tests/unit/requestFeedback.test.mjs`、`prd049BrowserAcceptance.test.mjs`、`scripts/acceptance/tests/test_prd049_runtime_acceptance.py`：PAGE-070/071 渲染、owner/权限/隐私、筛选 DOM 收敛、0039 六守卫、双服务 405、HMAC 与凭据清理轻量门禁。
- `scripts/acceptance/prd049-runtime-acceptance.py`、`prd049-browser-acceptance.mjs`：真实 Control/Gateway Feedback、Summary、三角色、第二租户、Webhook、RequestLog/ModelQuality、数据库/日志敏感扫描和清理；Browser 覆盖 1280x720、900x800、390x844 三视口、15 张 PNG、DOM/URL/storage/network 与非空像素。
- `artifacts/prd049/`：保留最终成功批次、脱敏状态、测试/迁移/服务日志、Runtime/Browser/15 图和停止后数据库证据；`diagnostics/` 中失败尝试只作诊断，不能替代最终通过证据。
- `docs/prd/PRD-049-request-feedback-adoption-records.md`、`docs/api/request-feedback.md`、`docs/testing/PRD-049-tests.md`：需求与自审、双入口公开 API/错误/权限合同，以及专项、Python 783、Console 287、39/0039、Runtime、15 图 Browser 和最终批次 `20260720T181908` 的权威记录。

## PRD-050 Prompt Cache 感知粘性路由目录（已完成）

本节登记 PRD-050 的最终模块边界。两轮编码前自审、实现、0040、当前源码相关回归、真实 Runtime/Browser、日志隐私、清理和停止后 SQLite 终态审计均已完成；权威批次为 `artifacts/prd050/20260725T005725`。该状态只代表 PRD-050 完成，不代表全部 108 条 PRD 已完成。

- `shared-sdk/python/metahub_shared/runtime/cache_affinity.py`：有界 system/developer 前缀规范化、独立 HMAC 指纹、tenant/Virtual Key/data_class/endpoint revision 隔离摘要及 repr-safe 请求上下文；不保存 Prompt/Response。
- `shared-sdk/python/metahub_shared/runtime/{errors.py,error_diagnosis.py,i18n.py,webhooks.py}`：CacheAffinity 统一错误/诊断/i18n 和 `cache.affinity_selected` 两种精确安全 payload。
- `control-plane-api/src/metahub_control/model_aliases/{models.py,routes.py,service.py}`：Alias 配置投影、认证后严格 PATCH、admin 写与 admin/developer 读、viewer 后端拒绝、tenant-qualified CAS/no-op 幂等、Audit/Event/Webhook。
- `control-plane-api/src/metahub_control/request_logs/{models.py,service.py}`：只按当前租户返回不可变 Selection 安全摘要；不返回 Credential/Binding/Scope、data_class、digest、Prompt 或 Endpoint URL。
- `control-plane-api/src/metahub_control/auth/dependencies.py`、`console/service.py`、`app.py`：CacheAffinity reader/writer 后端守卫、角色导航和路由注册；前端零 DOM 不替代权限。
- `gateway-data-plane/src/metahub_gateway/cache_affinity/`：Scope/Binding pending reservation、Key target generation、Selection 创建、成功 warm/失败释放 CAS、容量清理和启动恢复。
- `gateway-data-plane/src/metahub_gateway/routing/models.py`、`providers/keypool.py`：在当前合格动态候选中应用 pending/warm Endpoint 偏好，并在已选 Endpoint 内复用或重绑 Provider Credential；fixed 和硬门禁不被覆盖。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`、`routing/attempts.py`、`streaming/sessions.py`、`app.py`：Provider 前 Selection/事件、非流式与流式真实终态、迟到 callback 防护、启动恢复和失败关闭。
- `console-web/src/lib/modelAliases.js`、`features/routing-policies/renderRoutingPolicyPanel.js`、`features/request-logs/renderRequestLogPanel.js`、`app/main.js`、`styles/main.css`：PAGE-072 配置/只读/冲突状态、PAGE-073 estimated hit 与 cache tokens 分离、身份/导航 owner 和三档响应式布局。
- `infra/migrations/0040_prompt_cache_affinity_routing.sql` 及 down migration：五张业务表、Candidate reason/Audit 保真重建、tenant owner/graph/immutable/REPLACE/rowid/down guard；当前 loader checksum 为 `dc4289d44091e231f2c288398c58ab8eccc075fd7bcbbc869f2b7b7c8257f090`。
- `shared-sdk/python/tests/test_cache_affinity.py`、Control/Gateway CacheAffinity/路由/迁移测试、`console-web/tests/unit/cacheAffinity.test.mjs`、`dashboard.test.mjs`、`prd050BrowserAcceptance.test.mjs`：纯函数、租户/RBAC/幂等、pending/warm/重绑/终态、迁移、Console 状态和验收器合同。
- `scripts/acceptance/prd050-runtime-acceptance.py`、`prd050-browser-acceptance.mjs`：真实本地上游、三服务、第二租户、三角色、三视口、Webhook/RequestLog、隐私、PNG 像素、布局和清理验收；Browser attempt 目录隔离且旧失败证据不覆盖。
- `docs/prd/PRD-050-prompt-cache-aware-sticky-routing.md`、`docs/api/cache-affinity.md`、`docs/testing/PRD-050-tests.md`：需求与两轮自审、公开 API/RBAC/错误/事件合同，以及风险分层测试、Runtime/Browser/日志/数据库终态证据。
- `artifacts/prd050/20260725T005725/`：权威 Runtime、最终 Browser 45 PNG、当前定向测试、日志/视觉/数据库审计和 final manifest；同目录旧失败与诊断 exitcode 只作复盘，不作为通过证据。

## PRD-051 FinOps Ledger 正式账本目录（已完成）

本节登记 PRD-051 最终模块边界。设计与两轮编码前自审、实现、0041、分层回归、开发库升级、真实 Runtime/Browser、日志隐私及停服终态审计均已通过；该状态只代表 PRD-051 完成，不代表全部 108 条 PRD 已完成。

- `shared-sdk/python/metahub_shared/runtime/{config.py,errors.py,i18n.py,webhooks.py}`：本机 Usage spool 的有界配置与安全摘要、`finops.usage_record_unavailable` 统一 503、中英文标签、canonical `ledger.entry_created` 及旧事件名归一；spool 内容与文件名使用独立业务域 HMAC，复用进程根密钥但不新增独立 Secret。
- `gateway-data-plane/src/metahub_gateway/usage/{ledger.py,spool.py}`：Usage、价格快照、正式分录投影、RequestLog 与预算结算同事务；数据库失败后同步写本机 HMAC/fsync spool，按 tenant + Usage ID 幂等重放、篡改隔离、claim 租期恢复，并只为本次新建分录入队事件。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`、`app.py`：非流式在主库与 spool 双失败时返回统一可重试 503；流式正文发出后只记录安全 critical，不伪造 SSE；lifespan 在事件队列就绪后恢复并周期处理有限批次。
- `control-plane-api/src/metahub_control/finops/{models.py,routes.py,service.py}`：admin-only 账本读取、HMAC 快照游标、五类调整、部分/全部冲正、幂等恢复、跨租户同形 404、同事务 Audit 及提交后 Webhook；客户端不能提交 tenant、actor、币种或派生归因字段。
- `control-plane-api/src/metahub_control/{auth,console,audit,webhooks}` 与 `app.py`：FinOps 后端 reader/writer 权限、admin 导航、审计对象白名单、事件投递和路由注册；developer/viewer 无法依赖前端隐藏绕过后端。
- `console-web/src/lib/finops.js`、`src/features/finops-ledger/`、`src/app/main.js`、`src/styles/main.css`：PAGE-074/075 的筛选、分页、汇总、详情、调整/冲正、身份与导航 owner、迟到响应门禁和 1280/390 响应式布局。
- `infra/migrations/0041_finops_ledger.sql` 及 down migration：调整与正式分录表、历史回填、运行时自动投影、复合 tenant owner、append-only/REPLACE/rowid 防护、Audit/Event 保真和业务事实回滚门禁。
- `control-plane-api/tests/integration/{test_prd051_db_migrations.py,test_finops.py,test_console_navigation.py}`、`gateway-data-plane/tests/integration/test_prd051_finops_gateway.py`、`shared-sdk/python/tests/test_prd051_finops_runtime_contracts.py`、Console 三个 FinOps 测试文件：合并专项 Python `39/39`；Python 根级全量首次 `958 = 890 passed / 68 failed`，随后按原失败节点以迁移 `46/46`、Control `13/13`、Gateway `10/10` 聚焦闭环；Node `33/33`、Console `375/375`、lint `125 files`。不得把首次全量记录改写为 `958/958 passed`。
- `scripts/acceptance/prd051-runtime-acceptance.py`、`prd051-browser-acceptance.mjs` 及 Runtime 合同测试：真实登录、账本、201/200/409、全额冲正/超额、request_id/no-store、双视口 6 张遮罩截图、草稿销毁、登出和安全 write-once 证据。
- `docs/prd/PRD-051-finops-ledger.md`、`docs/api/finops-ledger.md`、`docs/testing/PRD-051-tests.md`：需求与两轮自审、公开 API/RBAC/错误/事件合同，以及分层测试和最终验收记录。
- `artifacts/prd051/20260727T015822/`：保留专项、一次根级全量、聚焦 delta、开发库迁移、已通过 Runtime 与首次真实 Browser 失败；`20260727T043500-browser-success/` 保留两次启动前失败；`20260727T043500-browser-success-02/` 是权威 Browser 成功批次，包含 6 张 PNG、服务日志、19 项停服终态审计及 final manifest。失败证据不得覆盖或作为通过证据。

## PRD-052 租户/项目/Key三级预算目录（已完成）

本节登记 PRD-052 最终模块边界。设计与两轮编码前自审、实现、0042、聚焦专项与相邻回归、开发库迁移、真实 Runtime/Browser 验收均已有可复核记录；该状态只代表 PRD-052 完成，不代表全部 108 条 PRD 已完成。

- `shared-sdk/python/metahub_shared/runtime/{budget_scopes.py,budget_spend.py,budget_reservations.py,budget_blocks.py}`：稳定 BudgetScope ID/优先级、正式消费和未结算预留的整数微元口径、reservation 生命周期与粘性 block 共享合同；正式消费只读取 `finops_ledger_entries` 的 `posted usage_charge/debit`。
- `gateway-data-plane/src/metahub_gateway/budgets/{models.py,service.py}`：按 `tenant -> project -> application（兼容） -> virtual_key` 可信链逐层执行累计 AND 准入，在同一事务中读取正式账本、叠加 outstanding reservation、写预留与 `budget_scope_events`；任一层阻断即回滚本请求先前预留并在 Provider 前返回统一 402。
- `control-plane-api/src/metahub_control/budgets/{models.py,routes.py,service.py}`：三级规则/状态投影、可信 Scope resolver、create-only `POST /budgets`、兼容 PATCH、重复创建 409、统一错误和 `request_id`；admin 可写、admin/developer 可读、viewer 后端拒绝，tenant_id 只从 Session 恢复，Environment/Project/Application/Key 均执行租户与归属校验。
- `console-web/src/lib/budgets.js`、`src/features/budgets/{renderBudgetPanel.js,budgetPageLifecycle.js}`、`src/app/main.js`、`src/styles/main.css`：PAGE-076 三级摘要、正式账本/预留拆分、可信作用链、POST/PATCH 表单、409 request_id 保留、异步 owner 门禁及 1280/390 响应式布局；Application 只作为既有兼容项展示，不计入三级摘要。
- `infra/migrations/0042_three_level_budgets.sql` 及 down migration：新增 append-only `budget_scope_events`、正式 Usage Charge 的分层索引，并补强 Rule、Alert、Evaluation、Block、Reservation、ScopeEvent 的租户、Environment、scope 与父规则归属触发器。
- `shared-sdk/python/tests/{test_prd052_budget_scopes.py,test_prd052_budget_webhook_contracts.py,test_budget_spend.py,test_budget_reservations.py}`、Control PRD-052/迁移预算测试、Gateway Budget Guard/Service 测试和 Console 三个 PRD-052 测试文件：覆盖主流程、异常、后端权限、跨租户/跨 Environment 归属、create-only/409、正式账本与预留拆分、逐层阻断、事件、页面 owner 和响应式验收器合同；实际执行节点与结果以 `docs/testing/PRD-052-tests.md` 为准。
- `scripts/acceptance/prd052-runtime-acceptance.py`、`prd052-browser-acceptance.mjs` 及 Runtime 合同测试：真实三服务验证三级规则、developer/viewer 403、正式 Usage/FinOps 投影、Project 预算 402 且 Provider attempt 为 0、Session 注销；Browser 覆盖 1280x720 与 390x844、PAGE-076 三种页面状态、布局/网络/隐私扫描及 6 张非空 PNG。
- `docs/prd/PRD-052-tenant-project-key-three-level-budget.md`、`docs/api/three-level-budgets.md`、`docs/testing/PRD-052-tests.md`：需求与两轮自审、公开 API/RBAC/错误/事件合同，以及聚焦测试和最终验收记录。
- `artifacts/prd052/20260727T112603/{runtime-evidence.json,browser-evidence.json,final-audit.json,screenshots/}`：权威 Runtime、Browser 与停服终态证据；三份 evidence 状态均为 `passed`，最终截图目录含桌面/移动端 6 张 PNG，`screenshots-pre-fix-overflow/` 仅保留修复前诊断图，不能作为通过证据。
- PRD-053 至 PRD-061 均已完成。按 Excel 两张核心表和完成记录重新识别，当前下一项是
  `PRD-062 | M1-企业POC | P1 | 配置导入导出CLI`；
  `all_108_prds_complete=false`。

## PRD-053 成本异常检测基础目录（已完成）

本节登记 PRD-053 最终模块边界。设计与两轮编码前自审、实现、0043、专项测试、一次根级全量及原失败定向闭环、真实 Runtime/Browser 和静止终态均有可复核证据。

- `shared-sdk/python/metahub_shared/runtime/{config.py,errors.py,i18n.py,webhooks.py}`：成本异常六项有界部署配置、14 个统一错误码与中英文标签、`cost.anomaly_detected` 精确 20 字段 payload/信封/任意精度金额合同，以及稳定持久事件 ID。
- `control-plane-api/src/metahub_control/cost_anomalies/{models.py,detector.py,service.py,routes.py,outbox.py,scheduler.py}`：公开 DTO、正式 FinOps 账本检测、run lease/CAS、冷却去重、admin-only GET/PATCH、严格 wire、动作/幂等/Audit 事务、source outbox 恢复和生命周期调度。
- `control-plane-api/src/metahub_control/{auth/dependencies.py,app.py,audit/service.py,console/service.py,webhooks/service.py}`：后端成本异常 reader/writer、路由与 scheduler 注册、Audit 白名单、admin 导航，以及公共 Webhook pending resume；tenant/actor 只从 Session 恢复。
- `gateway-data-plane/src/metahub_gateway/rate_limits/service.py`：在既有 Virtual Key 限流执行器前加载 tenant-qualified 永久规则和有效 `temporary_rate_limit_overrides`，逐字段取最小值；到期或 release 自然恢复。
- `console-web/src/lib/costAnomalies.js`、`src/features/cost-anomalies/{costAnomalyPageState.js,renderCostAnomalies.js,bindCostAnomalies.js}`、`src/app/main.js`、`src/styles/main.css`：PAGE-077/078 列表、告警卡片、详情、筛选/游标、严格动作、幂等生命周期、角色 owner 和桌面/移动端布局。
- `infra/migrations/0043_cost_anomaly_detection.sql` 及 down migration：检测运行、CostAnomaly、append-only action、临时限流覆盖、tenant owner、不可变事实、状态转换、幂等和回滚合同。
- `shared-sdk/python/tests/test_prd053_cost_anomaly_shared_contracts.py`、Control 的 PRD-053 迁移/API/detector/Webhook 测试、Gateway overlay 测试、`console-web/tests/unit/costAnomalies.test.mjs` 及 Runtime/Browser 合同：覆盖主流程、异常、后端权限、跨租户 owner、严格 wire、状态机、幂等、页面 owner、双视口和静止清理；一次 Python 全量为 `1178 passed / 30 failed`，原 30 个环境或旧合同失败随后以受影响范围 `34/34` 全部闭环。
- `scripts/acceptance/prd053-runtime-acceptance.py`、`prd053-browser-acceptance.mjs` 及对应合同测试：真实三服务、Webhook、detector、GET/PATCH、Gateway 429/release、三角色、双视口五状态、截图/hash、网络/Storage/Session 隐私和异常清理。
- `.env.example`、`docs/prd/PRD-053-cost-anomaly-detection-foundation.md`、`docs/api/cost-anomalies.md`、`docs/testing/PRD-053-tests.md`：部署边界、冻结设计、公开 API/Webhook 合同和最终分级测试证据。
- `artifacts/prd053/20260727T183515/{runtime-acceptance.json,browser-evidence.json,database-final.json,logs-final.json,pytest-link-cleanup.json,final-audit.json,screenshots/}`：权威 Runtime、Browser 与停服静止终态；正式截图为 10 张，`screenshots-diagnostic-attempt-*` 仅是失败诊断历史并明确排除。

最终结果：Runtime 43/0043，原开发库保持 42/0042；Python 原全量失败已定向闭环，Node `33/33`、Console `401/401`、lint `134 files`、中文注释门禁、Session/端口/日志隐私/SQLite checkpoint 与 sidecar 均通过。PRD-053 可标记完成，但不代表全部 108 条 PRD 已完成。

## PRD-054 请求解释中心目录（已完成）

本节登记最终模块边界。PRD-054 的专项、0044 实迁移、真实 Runtime/Browser、隐私和静止
终态均已有可复核直接证据；该状态只表示 PRD-054 完成，不代表全部 108 条 PRD 已完成。

- shared-sdk/python/metahub_shared/runtime/{config.py,errors.py,error_diagnosis.py,i18n.py,webhooks.py}：
  RequestExplanation outbox 部署开关、七个统一错误码与中英文标签，以及
  request.explanation_viewed 精确 payload、信封绑定和敏感键递归门禁。
- control-plane-api/src/metahub_control/request_explanations/{contracts.py,models.py,wire.py,
  repository.py,service.py,routes.py,outbox.py,scheduler.py}：严格 GET wire、十阶段/十一区块
  exact-key DTO、tenant-qualified 单快照聚合、不可变访问事实、source outbox lease/CAS
  和生命周期 worker；不读取当前配置回算历史，不复制 Prompt/Response 或原始错误正文。
- control-plane-api/src/metahub_control/{auth/dependencies.py,console/service.py,app.py}：
  admin/developer 后端 reader、viewer 拒绝、request-explanations 导航、路由及 scheduler
  注册；认证和 RBAC 先于业务输入校验执行。
- infra/migrations/0044_request_explainability_center.sql 及 down migration：新增全部带
  tenant_id 的 request_explanations、request_webhook_delivery_links，补齐请求维度索引，
  并以 owner、append-only、REPLACE/rowid、outbox 状态机和业务事实 down guard 保护历史。
- console-web/src/lib/requestExplanations.js、src/features/request-explanations/、
  src/features/request-logs/renderRequestLogPanel.js、src/app/main.js、src/styles/main.css：
  PAGE-079、Request Logs 行内解释动作、一次性身份绑定 handoff、Abort/迟到响应门禁、
  折叠/焦点恢复、精确 fact 白名单和 1280/390 响应式布局；源请求 ID 不进入 URL/Storage。
- shared-sdk/python/tests/test_prd054_request_explanation_contracts.py、Control 的
  test_prd054_db_migrations.py、test_prd054_request_explanations.py、
  test_prd054_request_explanation_edges.py，以及 Console requestExplanations 和
  prd054BrowserAcceptance 测试：覆盖错误/事件、0044、RBAC/租户、完整/partial、截断、
  精度、故障关闭、outbox 和前端 owner/布局；实际执行结果只以最终测试记录为准。
- scripts/acceptance/prd054-runtime-acceptance.py 及后续 Browser 验收器：
  只允许回环三服务和批次独立数据库，验证真实登录、API、事件、迁移、双视口、隐私与
  Session 清理；诊断批次不得替代最终 passed 证据。
- docs/prd/PRD-054-request-explainability-center.md、docs/api/request-explanations.md：
  分别冻结需求/两轮编码前自审和公开 API/RBAC/错误/事件合同。
- docs/testing/PRD-054-tests.md、artifacts/prd054/20260728T040841/：记录聚焦测试、一次根级
  全量及原失败定向闭环、权威 Runtime/Browser、六张正式截图、数据库/日志、链接节点清理和
  停服静止终态；Runtime 与 Browser 均为 `status=passed`，最终审计哈希已稳定登记。

## PRD-055 Trace查询与过滤增强目录（已完成）

本节登记最终模块边界。PRD-055 的两轮编码前/实现后自审、0045、专项与全量回归、真实
Runtime/Browser、中文注释门禁和停服静止终态均已有可复核证据。该状态只代表 PRD-055
完成，不代表后续 PRD 或全部 108 条 PRD 已完成。

- `shared-sdk/python/metahub_shared/runtime/{config.py,errors.py,i18n.py,webhooks.py}`：
  QueryEvent outbox 有界配置、完整统一错误码/中英文标签、`log.query_executed` 精确
  payload/信封和敏感键递归门禁。
- `control-plane-api/src/metahub_control/request_logs/{models.py,query.py,saved_filters.py,
  routes.py,outbox.py,scheduler.py}`：inline/SavedFilter/legacy 三种互斥来源、严格规范化、
  materialized CTE + window count、白名单排序、有界分页、个人 SavedFilter CRUD、
  QueryEvent source outbox 和生命周期 worker。
- `control-plane-api/src/metahub_control/{app.py,console/service.py}`：RequestLog router、
  outbox scheduler 和 PAGE-080 导航注册；后端只允许 admin/developer，viewer 拒绝。
- `console-web/src/lib/requestLogs.js`、`src/features/request-logs/renderRequestLogPanel.js`、
  `src/app/main.js`、`src/styles/main.css`：PAGE-080 高级筛选、SavedFilter 显式应用、
  稳定分页、身份 owner/abort、解释 handoff、周期刷新竞态保护及 1280/390 响应式布局。
- `infra/migrations/0045_trace_query_enhancement.sql/.down.sql`：SavedFilter、不可变
  QueryEvent/source outbox、tenant-first RequestLog 索引、owner/状态机触发器和 down guard。
- Shared PRD-055 合同测试、Control core/API/outbox/迁移/隐私测试、Console
  `requestLogs.test.mjs` 与 `prd055BrowserAcceptance.test.mjs`：覆盖主流程、异常、
  后端权限、跨租户/owner、兼容、性能、页面生命周期和双视口验收器合同。
- `scripts/acceptance/prd055-runtime-acceptance.py`、`prd055-browser-acceptance.mjs`：
  批次独立数据库和真实三服务验收；Runtime 覆盖十万行双租户与 P95，Browser 覆盖三角色、
  双视口八状态、SavedFilter、Explain、布局、网络、隐私和 Session 清理。
- `docs/prd/PRD-055-trace-query-enhancement.md`、`docs/api/request-log-query.md`、
  `docs/testing/PRD-055-tests.md`：技术设计与四轮自审、公开 API/RBAC/错误/事件合同，以及
  最终测试、运行证据和完成结论。
- `artifacts/prd055/20260728T134456/{runtime-acceptance.json,browser-acceptance.json,
  database-final.json,logs-final.json,pytest-link-cleanup.json,pytest-link-cleanup-details.json,
  final-audit.json,screenshots/}`：权威 Runtime、Browser、数据库、日志、链接清理、最终审计和
  八张正式 PNG；所有正式 JSON 的 `status` 均为 `passed`。

最终结果：Python 根级全量只执行一次，真实分布为 `1363 passed / 1 failed`；唯一失败是
测试误用生产 `5 ms` 策略超时，测试级改为 `100 ms` 后原 node `1/1 passed`，生产逻辑
未改且没有重跑整套全量。Node `33/33`、Console `433/433`、lint `140 files` 和中文注释
门禁均通过；数据库为 `45/0045`，checkpoint、完整性、外键、端口、sidecar、spool、
Session 和日志隐私终态均通过。Runtime、Browser、最终审计哈希分别为
`8cc8f626...14143`、`967db05a...08754`、`55f5eaa6...097de`；仅删除 `502` 个链接节点并
保留 `502` 个目标。该状态只代表 PRD-055 完成，`all_108_prds_complete=false`。

## PRD-056 Provider错误归一化规则库目录（已完成）

本节登记 PRD-056 最终模块边界。两轮编码前设计自审、实现后两轮复核、0046、专项与完整
分段回归、真实 Runtime/Browser、中文注释和停服静止终态均有可复核证据。该状态只代表
PRD-056 完成，不代表后续 PRD 或全部 108 条 PRD 已完成。

- `shared-sdk/python/metahub_shared/runtime/provider_error_map.py`：不可变 builtin catalog、tenant
  overlay 规则编译、强 ETag/wire、64 KiB 有界事实提取、不可覆盖安全 guard、确定性 matcher、
  统一错误推导和 repr-safe 脱敏证据；原始 code/type/message/body 只存在于短生命周期内存。
- `shared-sdk/python/metahub_shared/runtime/{config.py,errors.py,error_diagnosis.py,i18n.py,webhooks.py}`：
  缓存/轮询配置、统一错误与双语标签、PAGE-082 诊断以及 `error_map.updated` 精确信封和敏感键门禁。
- `control-plane-api/src/metahub_control/error_maps/{models.py,routes.py,service.py,outbox.py,scheduler.py}`：
  严格 wire、admin 写/admin+developer 读、viewer 后端拒绝、tenant-qualified CRUD/CAS/幂等、
  immutable version/head、operation ledger、Source Outbox lease/CAS 和生命周期 worker。
- `control-plane-api/src/metahub_control/{auth/dependencies.py,app.py,audit/service.py,console/service.py,
  webhooks/service.py}`：后端 reader/writer 与内部 service-token 门禁、路由/scheduler、Audit 白名单、
  PAGE-081 导航和公共 Webhook pending resume；tenant/actor 只从可信认证上下文恢复。
- `gateway-data-plane/src/metahub_gateway/error_maps/{cache.py,facts.py}`：per-tenant singleflight、
  200/304、LKG/builtin 降级、取消传播、有界 close，以及只接收 repr-safe evidence 的 Fact helper。
- `gateway-data-plane/src/metahub_gateway/{providers,routing,streaming,api/openai_compatible.py,app.py}`：
  Adapter 非 2xx 有界读取、请求期单次 lease、主备同 revision、attempt/session Fact 同事务、
  失败 RequestLog/成本/用量终态和 cache 生命周期安装；热路径不逐请求查询 Control 或数据库规则。
- `infra/migrations/0046_provider_error_normalization_rules.sql/.down.sql`：规则、不可变版本/head、
  operation、Source Outbox、builtin catalog/rules、attempt/session Fact，以及 tenant owner、
  append-only、REPLACE、状态机、反向 owner、stream 列和未知后续依赖 down guard。
- `console-web/src/lib/errorMaps.js`、`src/features/error-maps/{errorMapPageState.js,
  errorMapDialogFocus.js,renderErrorMapPanel.js}`、`src/app/main.js`、RequestLog 详情与 CSS：PAGE-081、
  PAGE-082 冻结 Fact、admin/developer/viewer、三 request-id 409、owner/Abort、焦点/注销清理及
  1280x720/390x844 稳定布局；tenant_id、筛选和敏感正文不进入 URL/Storage。
- Shared PRD-056 合同测试、Control 0046/API/DTO 测试、Gateway cache/Fact/归一化测试、Console
  `errorMaps`/focus/Browser 合同和 Runtime 合同：覆盖主流程、异常、后端权限、跨租户、事务、
  兼容、并发、隐私、迁移和页面生命周期。106 个 PRD-056 Python node 均包含在最终 1472 个
  唯一 node 的无重叠分段覆盖中。
- `scripts/acceptance/prd056-runtime-acceptance.py`、`prd056-browser-acceptance.mjs`：批次独立 0046
  运行库和真实三服务验收；Runtime 覆盖热更新、归一矩阵、stream/nonstream Fact、Source
  Outbox/Webhook/隐私，Browser 覆盖三角色、409、PAGE-082、焦点、注销、双视口和六张非空 PNG。
- `docs/prd/PRD-056-provider-error-normalization-rule-library.md`、`docs/api/provider-error-maps.md`、
  `docs/testing/PRD-056-tests.md`：冻结设计与四轮复核、公开 API/RBAC/错误/事件合同，以及最终
  分段测试、局部失败闭环、迁移、Runtime/Browser 和静止终态记录。
- `artifacts/prd056/20260730T022600/{runtime-acceptance.json,browser-acceptance.json,runtime.db,
  pytest-link-cleanup.json,pytest-link-cleanup-details.json,final-audit.json,screenshots/,
  final-regression/logs/}`：权威 Runtime、Browser、46/0046 运行库、六张正式截图、116 份回归日志、
  584 个链接节点清理明细和最终审计；artifact 复制前后关键文件 hash 完全一致且无链接/sidecar。

最终结果：Python 收集 `1472` 个唯一 node；根级进程在前 433 个通过后中止，随后以无重叠切片
覆盖全部余下 node，并对 4、1、52、21 个局部失败集合定向闭环。Node `33/33`；Console 一次
全量 `451 = 450 passed / 1 failed`，旧测试切片修正后对应文件 `10/10 passed`；lint `147 files`，
中文注释门禁通过。Runtime、Browser、最终审计 SHA-256 分别为 `72fc7b86...fb245`、
`4cc280d0...bb380`、`1bac341f...b224b`。运行库 `46/0046`；Session、端口、sidecar、spool、
ReparsePoint 均为 0，仅删除 584 个链接节点并保留 584 个目标。该阶段后续 PRD-057、PRD-058
也已完成；`all_108_prds_complete=false`。

## PRD-057 Webhook死信与持久重放目录（已完成）

本节登记 PRD-057 最终模块边界。技术设计、两轮编码前自审、0047/0048、后端
DeadLetter/replay worker、公开 API、PAGE-083、专项与回归、实际运行库、真实 Runtime、Browser、
停服静止终态和正式 artifact 均已通过。该阶段后续 PRD-058 也已完成，
`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/runtime/{errors.py,i18n.py,webhooks.py}`：新增死信查询、状态、
  归档、持久 replay 的统一错误与中英文标签，登记内部 `webhook.dead_lettered` 事实，并把它从
  租户 Endpoint 可订阅集合排除，避免接收端故障形成递归死信。
- `infra/migrations/0047_webhook_dead_letter_persistent_replay.sql/.down.sql`：新增
  `webhook_dead_letter_events`、tenant-first 查询/到期/replay due 索引、复合 owner FK、
  identity/状态机/DELETE/REPLACE/rowid trigger，回填 0046 历史 failed original 与最新 replay，
  扩展 Audit action；down 在 PRD-057 事实存在时原子拒绝，并重建 0046 Audit 合同。
- `infra/migrations/0048_webhook_dead_letter_state_consistency.sql/.down.sql`：不改写 0047 checksum
  或既有死信事实，约束根死信与当前 replay delivery 的稳定终态关系；保留
  `replay_pending + terminal delivery` 崩溃瞬时状态供 worker 原子收敛，down 只恢复 0047 owner
  trigger，不删除业务数据。
- `control-plane-api/src/metahub_control/webhooks/{models.py,routes.py,wire.py,service.py,outbox.py,
  scheduler.py,__init__.py}`：无正文 DeadLetter DTO、seek 列表、RBAC 后 4 KiB 严格 replay wire、
  tenant-qualified intent/Audit 事务、同键纯读取、archive/Audit 事务、30..120 秒 lease、五元组
  CAS、1/5/30/300 秒退避、过期和 Control lifespan scheduler。网络只允许发生在 intent 提交后。
- `control-plane-api/src/metahub_control/{app.py,auth/dependencies.py,audit/service.py,
  console/service.py}`：注册路由/scheduler、admin/developer 后端 Webhook manager 守卫、死信归档
  Audit 白名单和 PAGE-083 角色导航；tenant/actor 只从 Session 恢复，viewer 后端拒绝。
- `console-web/src/lib/webhooks.js`、`src/features/webhook-dead-letters/{webhookDeadLetterPageState.js,
  renderWebhookDeadLetters.js,bindWebhookDeadLetters.js}`、`src/app/main.js`、`src/styles/main.css`：
  PAGE-083 安全列表、规范化筛选/cursor、内存幂等键、重放/归档、轮询、identity/navigation/
  generation/Abort owner、归档确认、注销/降权/Session revision 清理，以及桌面/移动响应式布局；
  不解析 payload，不把筛选、资源 ID 或幂等键写入 URL/Storage。
- `control-plane-api/tests/integration/test_prd057_webhook_dead_letters.py`：三个组合 node 覆盖真实 500
  根死信、失败/成功 replay、归档、跨租户/RBAC、远端成功后崩溃接管、seek 和固定过期。
- `control-plane-api/tests/integration/test_prd057_replay_idempotency_regression.py`：六项独立回归冻结
  旧 key 不领取另一 pending、viewer/匿名畸形 JSON 先 403/401、损坏 payload 动态 Prompt 标记
  零泄漏、授权正文超限/重复 key/NaN/非法 UTF-8 的严格 wire、97 字符 delivery ID 同形 404、
  列表 Header/Body request_id 一致。
- `control-plane-api/tests/integration/test_prd057_db_migrations.py`：三个 node 覆盖 fresh up/down/up、
  0046 历史映射、根 ID、Audit rowid/NULL/全列双向 EXCEPT 保真、含事实 down guard，以及 0048
  稳定终态错配拒绝与可恢复瞬时状态。
- `control-plane-api/tests/integration/test_prd057_hardening_regressions.py`：七个 node 覆盖发送跨 lease
  后重新读取时钟、过期 owner 零出网/finish/reschedule、列表/归档/replay 数据库连接失败的专用
  503 错误码、OpenAPI 必填 `idempotency_key`，以及 terminal replay 崩溃后只收敛根状态而不重发。
- `console-web/tests/unit/webhookDeadLetters.test.mjs` 及相邻 Webhook 测试：冻结后端模型字段、
  `dead_letters/filters/next_cursor`、动作 `dead_letter`、attempt `attempt_no/created_at`、状态 owner、
  viewer 零预取和旧 Webhook 兼容。
- `docs/prd/PRD-057-webhook-dead-letter-persistent-replay.md`、
  `docs/api/webhook-dead-letters.md`、`docs/testing/PRD-057-tests.md`：技术设计与两轮自审、公开
  API/RBAC/错误/状态机/可靠性合同，以及最终测试与运行验收证据。

最终结果：代码冻结后的专项为 `24 passed`。唯一一次根级 Python 全量为
`1496 = 1407 passed / 89 failed`，pytest `5960.85s`、wrapper `5970.009s`；没有重跑根级全量，
原失败节点按迁移 `14+21+40` 和非迁移相关六文件 `53 passed` 验证，最终 `89/89` 精确定向闭环。
Node `33/33`、Console `462/462`、lint `151 files`、Python 内存语法 `26 files`、Ruff 致命规则与
中文注释门禁均通过。隔离运行库为 `48/0048`，0048 checksum 为
`5d3a6b1a76e41e6586e75d42946164c3d7de4cd4c2fcf4268994176f7282ac20`，integrity/FK 与 9 个
PRD-057 trigger 通过。真实 Runtime、三角色/匿名/第二租户、HMAC/DTO 去敏、1280x720/390x844
Browser 五图和停服静止终态均通过。

链接清理严格限定在 `D:/MetaHub/.tmp`：根级全量阶段仅删除 539 个 SymbolicLink 和 1 个 Junction，
保留 540 个目标；增量阶段仅删除 159 个 SymbolicLink，保留 159 个目标。目标缺失和授权可见范围
剩余均为 0。22 个历史目录因既有 ACL 无法遍历，未提权、未绕过，所以结论为
`passed_with_authorized_visible_scope`，不宣称无条件全局 0；未使用 `C:/tmp`。

- `artifacts/prd057/20260731T005000/`：权威 Runtime、Browser、数据库、回归、链接清理、停服静止
  终态、5 张正式截图、运行库和 final audit。目录共 38 个文件，manifest 登记 36/36 且 hash/字节
  复核一致，artifact 内 ReparsePoint 与 SQLite sidecar 为 0。
- 最终审计、manifest、Runtime、Browser、Browser 数据库审计和运行库 SHA-256 分别为
  `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b`、
  `a3327905175b997016c49d01f50d5d1937b52a3c793c67ab33d6b1657a2cfd33`、
  `a99a7138492e3b272938b30116b0f5a4efdcedc005b21a6a6904725bd4469070`、
  `6e2d9bf3560d6df2099b856abd7a863517803793ffd75ba6014b5de82b274c0c`、
  `51feaf2c68a9cd6b395da95d5cd139306ea0a7460e5595de86ae5dd1efdc2c64` 和
  `b8cb28d630dd2b52ed4f36999a493dc89b8f7e00f552e2554c7d984541534723`。

PRD-057 已完成；该阶段后续 PRD-058 也已完成，`all_108_prds_complete=false`。

## PRD-058 审计日志不可删除与导出目录（已完成）

本节登记 PRD-058 最终模块边界。两轮编码前自审、实现、实现后两轮复核、0049 实迁移、专项与
相邻回归、真实 API/Webhook、Browser、停服验收和正式 artifact 均已通过；
`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/runtime/{errors.py,i18n.py,webhooks.py}`：登记审计导出筛选、
  幂等冲突、大小、完整性和不可用错误；新增 `audit.exported` 固定八字段 payload/envelope，禁止
  筛选原文、CSV、邮箱、IP、User-Agent、Endpoint URL、Secret 或 lease 进入事件。
- `infra/migrations/0049_audit_log_immutable_export.sql/.down.sql`：新增 AUTOINCREMENT
  `audit_log_export_sequences` 稳定水位、不可变 `audit_exports` 与可恢复 `audit_export_events`，
  用 tenant 复合 FK、owner/状态机/DELETE/REPLACE/rowid trigger 约束关系；down 在已有导出、
  source、`audit.exported` 或相关 Webhook 证据时原子拒绝。
- `control-plane-api/src/metahub_control/audit/filters.py`：列表与导出共用七字段严格规范化、带时区
  时间和 tenant-first 参数化 SQL；客户端不能传 tenant、分页、任意列或 SQL 片段。
- `control-plane-api/src/metahub_control/audit/{export_service.py,models.py,routes.py,service.py}`：
  admin-only `GET /audit-logs/export`、稳定快照、cursor 逐行 UTF-8 BOM/CRLF 18 列 CSV、
  10,000 行/16 MiB 门禁、Unicode C* 归一与公式防护、严格存量 AuditLog decoder、幂等重放、
  安全下载头，以及 AuditExport/source/Audit 同事务提交。
- `control-plane-api/src/metahub_control/audit/{export_outbox.py,export_scheduler.py,__init__.py}`：
  `audit.exported` source 的启动/周期恢复、30 秒 lease、claim token/owner、CAS 终结、
  `5/30/300` 秒退避和最多 10 次尝试；只有有效 source lease owner 可以恢复 pending Webhook intent。
- `control-plane-api/src/metahub_control/{auth/dependencies.py,app.py,projects/service.py}`：独立
  `require_audit_exporter` 后端 admin 守卫、Control-only CORS 响应头和 scheduler 注册；Application
  跨项目迁移时为每个被间接修改归属的 Virtual Key 写同事务审计，关闭批量 Key 更新旁路。
- `console-web/src/lib/auditLogs.js`、`src/features/audit-logs/renderAuditLogPanel.js`、
  `src/app/main.js`、`src/styles/main.css`：PAGE-084 显式导出、固定响应头/Blob 二次校验、一次性
  object URL、完整身份/navigation/generation/Abort owner、安全反馈和响应式布局；自动刷新只读
  列表，离页、注销、筛选或身份变化会销毁导出状态，tenant/文件/筛选不进入 URL 或 Storage。
- `control-plane-api/tests`、`shared-sdk/python/tests`、`console-web/tests`：覆盖 CSV 主流程、上限、
  公式注入、幂等、不可变/owner trigger、RBAC、跨租户、source lease/CAS、Webhook 和前端生命周期。
  当前代码直接回归为受影响 Python `39 passed`、Console PRD-058 `15 passed`、Runtime 合同
  `9 passed`，中文注释和 Ruff 致命规则通过。
- `docs/prd/PRD-058-audit-log-immutable-export.md`、`docs/api/audit-exports.md`、
  `docs/testing/PRD-058-tests.md`：冻结技术设计、公开 API/CSV/RBAC/租户/幂等/事件/错误合同，以及
  如实分级的专项、根级历史、迁移、Runtime、Browser、链接清理和最终验收记录。
- `.tmp/prd058/artifacts/20260731T124500-r14/`：权威 Runtime、Browser、迁移、回归、8 张真实 PNG、链接清理、
  两轮实现后复核和 final audit。目录共 58 个文件，manifest 登记 56/56；ReparsePoint、SQLite
  sidecar、数据库文件和已知敏感明文命中均为 0。

最终结果：PRD-058 冻结专项 `10/10`、Shared `443/443`、受影响 Control `17/17`、0049 `6/6`、
历史迁移 `5/5`、Application/Project `4/4`、Node `33/33`、Console `470/470`、lint `151 files`。
根级收集 1536 个节点，后段 `372/1 failure` 与失败窗口 `15/1 failure` 的两条旧迁移头断言分别
定向 `1/1` 闭环，没有改写成一次全绿。开发库为 `49/0049`，Runtime、Browser 和 final audit 均为
`status=passed`；final audit SHA-256 为
`a555fa14edefc7540de4c9af9b4f4f126e932af5b83c70ea021995ff1024fef4`。仅在四个获准 `.tmp`
目录删除 128 个链接节点并保留 128 个目标。其后 PRD-059 也已完成，
`all_108_prds_complete=false`。

## PRD-059 细粒度角色权限矩阵目录（已完成）

本节登记 PRD-059 最终模块边界。设计与两轮编码前自审、两轮范围纠偏复审、实现、0050 开发库
迁移、专项、直接受影响回归、真实三服务 API、Microsoft Edge 双视口 Browser 和停服静止终态均已
通过。该状态只代表 PRD-059 完成，`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/rbac.py`：冻结六角色、48 Permission、产品授权上限、规范排序和
  matrix SHA-256；运行时不能用这些静态上限回退数据库授权。
- `infra/migrations/0050_fine_grained_role_permissions.sql/.down.sql`：新增租户 Permission、
  RolePermission、角色 revision/hash、权限 source event、新租户初始化及 owner/不可变 trigger；
  重建角色/AuditLog 时逐 rowid 保留 0049 Audit/Export 事实和冲突优先级。
- `control-plane-api/src/metahub_control/permissions/`：提供完整矩阵 GET、严格 JSON PATCH、
  BEGIN IMMEDIATE/revision CAS、no-op、职责上限、同事务 Audit/source 和提交后 Webhook。
- `control-plane-api/src/metahub_control/auth/` 与直接受影响业务路由：每次 Bearer 请求从数据库加载
  当前 role/revision/permissions，按 permission code 执行后端权限，不再按角色名补权。
- `control-plane-api/src/metahub_control/console/`：导航 DTO 携带 required permissions、permission
  revision 和 authorization fingerprint；PAGE-085 只对 `rbac.matrix.manage` 可见。
- `console-web/src/lib/permissions.js`、`src/features/role-permissions/`、`src/app/main.js` 及受影响
  页面：精确权限快照、owner 销毁、六角色矩阵、移动 Tab、locked/grantable、dirty/no-op/CAS 和
  无角色名补权的预取/控件门禁。
- `control-plane-api/tests/integration/test_prd059_{db_migrations,auth_permissions}.py` 与
  `tests/unit/test_prd059_route_permission_mapping.py`：覆盖迁移、两向租户关系、Admin 不可变、
  down guard、六角色、即时降权、职责分离、strict wire、跨租户、CAS、事务和路由映射。
- `console-web/tests/unit/permissions.test.mjs`、`auth.test.mjs`、`permissionTestContext.mjs` 及受影响
  页面测试：显式 permission snapshot、PAGE-085 生命周期和兼容页面失败关闭。
- `scripts/acceptance/prd059-browser-acceptance.mjs`：正式 loopback-only Browser 验收器，浏览器
  profile、截图和 JSON 只允许写 `D:/MetaHub/.tmp`；已使用本机 Microsoft Edge 完成真实页面验收。
- `docs/prd/PRD-059-fine-grained-role-permission-matrix.md`、`docs/api/role-permissions.md`、
  `docs/testing/PRD-059-tests.md`：分别记录设计与收口复核、公开 API 和最终分级测试/运行证据。

当前直接结果为迁移 `6/6`、权限事件 guard `1/1`、认证/权限/路由 `13/13`、Console 受影响
`116/116`、lint `156 files`；开发库栈顶为 `50/0050`，完整性 `ok`、外键违规 0、默认租户矩阵
`6/48/125`，三服务健康检查均为 HTTP 200。Edge Browser 真实登录、Admin 锁定、草稿放弃、隐私、
注销和 1280x720/390x844 两张非空 PNG 通过；停服后三端口、WAL/SHM 和 pending 权限事件均为 0。
权威 Browser 证据位于 `.tmp/prd059-edge-acceptance-20260731b`。按 Excel 两张核心表第 61 行及仓库
完成记录交叉识别，其后 PRD-060、PRD-061 也已完成，下一项是
`PRD-062 | M1-企业POC | P1 | 配置导入导出CLI`；
`all_108_prds_complete=false`。

## PRD-060 用户邀请与加入流程目录（已完成）

本节登记 PRD-060 最终模块边界。设计、两轮编码前自审、两轮实现期复审、0051/0052、后端与
Console 实现、专项和直接受影响回归、开发库迁移、真实 Runtime、Edge 双视口 Browser、隐私与静止
终态均有可复核记录。该状态只代表 PRD-060 完成，`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/runtime/{errors.py,i18n.py,webhooks.py,fastapi_app.py}`：Invitation
  统一错误和中文标签、两个严格事件 payload/envelope、敏感 path/Header 链路 ID 重建及路由模板日志；
  Token、邮箱和密码不进入普通日志或事件。
- `control-plane-api/src/metahub_control/invitations/{models.py,routes.py,service.py}`：严格 4 KiB wire、
  admin `user.manage` 后端守卫、一次性高熵 Token、tenant-qualified 查询、锁内实时权限/角色复核、
  同事务 Invitation/User/Audit/source、固定接受审计脱敏和提交后 Webhook。
- `control-plane-api/src/metahub_control/{app.py,audit/service.py,auth,console,users}`：注册受保护创建和
  公开接受路由，扩展可信 AuditActor 与 PAGE-086/087 导航；前端按钮隐藏不替代后端权限。
- `infra/migrations/0051_user_invitation_join_flow.sql/.down.sql`：新增 Invitation、不可变 source event、
  tenant owner/状态机/冲突/rowid trigger，并保真扩展 0050 AuditLog。已应用 checksum 固定为
  `4bc2793f8d825591195562415761375ba2881343e8b30b3099cfab01712a1c11`。
- `infra/migrations/0052_invitation_audit_privacy.sql/.down.sql`：历史和后续接受审计只保存固定脱敏邮箱；
  up 在持久变更前验证 8 张核心表、15 个核心 trigger 和历史接受图，down 在事实、后续版本或未知
  schema 依赖存在时原子拒绝。
- `console-web/src/lib/invitations.js`、`src/features/invitations/`、`src/features/users/`、
  `src/app/main.js`、`src/styles/main.css`：PAGE-087 邀请 dialog、24/72/168 小时、闭包私有链接/邮件
  文案、Clipboard 降级、fragment 公开接受、无 Session accept、刷新/离页/注销/降权 owner 清理和
  1280x720/390x844 响应式布局。
- `control-plane-api/tests/integration/test_prd060_{db_migrations,invitations}.py`、
  `control-plane-api/tests/unit/test_prd060_invitation_path_privacy.py`、
  `shared-sdk/python/tests/test_prd060_invitation_contracts.py`：覆盖 0051/0052、主流程、异常、权限、
  跨租户、两连接并发、Audit/source/commit 回滚、路径/Header/日志隐私和 Shared 合同。
- `scripts/acceptance/prd060-{runtime-acceptance.py,browser-acceptance.mjs}`：三服务真实登录、创建、公开
  接受、目标角色登录、越权与重放，以及 Edge 双视口、截图像素、网络、Storage、Clipboard 和 Token
  隐私门禁。
- `docs/prd/PRD-060-user-invitation-join-flow.md`、`docs/api/invitations.md`、
  `docs/testing/PRD-060-tests.md`：冻结设计和四轮复审、公开 API/RBAC/错误/事务合同，以及最终分级
  测试、迁移、Runtime、Browser 和静止终态证据。
- `.tmp/prd060-edge-acceptance-20260731b/`：权威 Browser 批次，含 5 张非空 PNG 和
  `browser-acceptance.json`；外部/阻断请求、页面/Console 错误、横向/控件文字溢出和 Token 在
  URL/DOM/Storage 命中均为 0。

最终结果：迁移 `23/23`、Invitation/路径/Shared `26/26`、Console `491/491`、lint `160 files`、
中文注释门禁通过。开发库为 `52/0052`、完整性 `ok`、外键违规 0；Runtime 与 Browser 均为
`status=passed`。其后 PRD-061 也已完成，下一项为
`PRD-062 | M1-企业POC | P1 | 配置导入导出CLI`；
`all_108_prds_complete=false`。

## PRD-061 内部 Provider 状态页目录（已完成）

本节登记 PRD-061 最终模块边界。两轮编码前自审、实现期收口复核、0053、后端与 Console、专项与
直接受影响回归、真实调用/探测、最新源码 HTTP、Edge 双视口和运行清理均已通过。该状态只代表
PRD-061 完成，`all_108_prds_complete=false`。

- `control-plane-api/src/metahub_control/provider_status/{models.py,routes.py,service.py}`：严格筛选和分页、
  `provider.health.read + request_log.read` 双权限、tenant 首条件、同一只读快照、调用/流/探测独立指标、
  P95、万分比、最近故障和统一 422/503。探测按
  `(provider_id, request_id, health_generation, created_at)` 区分可复用链路标识的独立事务。
- `control-plane-api/src/metahub_control/{auth,providers,console,app.py}`：注册状态路由和 PAGE-088 导航，
  逐请求权限快照；维护备注复用 `provider.write`，Audit 仅保存不可逆 hash；`status.viewed` 使用最小事件。
- `infra/migrations/0053_provider_status_page.sql/.down.sql`：新增 `maintenance_note`、调用/流/探测四个聚合
  索引，并保真扩展 `console_page_events`；up/down/re-up、未知依赖和非空备注 down guard 均有专项。
- `console-web/src/lib/providerStatus.js`、`src/features/provider-health/renderProviderStatusPanel.js`、
  `src/app/main.js`、`src/styles/main.css`：PAGE-088 loading/空态/统一错误、摘要、筛选/重置、三类 P95、
  六项计数、故障详情、立即探测、15 秒刷新、身份/权限/filter owner 和双视口布局。
- `control-plane-api/tests/integration/test_provider_status.py` 与
  `test_prd061_db_migrations.py`：覆盖指标、跨租户、双权限即时撤销、严格 wire、维护备注、坏事实、
  跨/同健康代次复用 request ID、迁移保真和 query plan。
- `scripts/acceptance/prd061-browser-acceptance.mjs` 与
  `console-web/tests/unit/prd061BrowserAcceptance.test.mjs`：固定工作区证据根、浏览器环境白名单、外部网络
  阻断、精确 query/POST/DTO/事件、DOM 替换、Storage 隐私、截图像素和 503 分类。
- `docs/prd/PRD-061-internal-provider-status-page.md`、`docs/api/provider-status.md`、
  `docs/testing/PRD-061-tests.md`：分别记录冻结设计及两轮自审、公开 HTTP/RBAC/错误合同和最终测试/运行证据。
- `.artifacts/prd061-browser/run-20260801T040500-final/`：权威 Edge 批次，3 张非空 PNG；loading、真实 200、
  筛选、探测、503、空态、恢复、事件、隐私与注销通过，外部请求和 page/console error 为 0。

最终结果：Provider 状态 `8 passed`、迁移 `8 passed`、直接回归 `12 passed`、Console 直接影响
`44 passed`、验收器合同 `10 passed`、lint `164 files`、中文注释门禁通过。开发库为 `53/0053`，
checkpoint `0,0,0`、完整性 `ok`、外键违规 0；Gateway/Control/Console 与既有状态接口均为 HTTP 200。
按 Excel `01_PRD总表!A63:R63`、`02_M0-M1核心开发!A63:R63` 及仓库完成记录交叉识别，下一项为
`PRD-062 | M1-企业POC | P1 | 核心层 | 迁移工具CLI | 配置导入导出CLI`。其范围是导入 Provider、Model、
Alias、Virtual Key 名称并导出配置 JSON；不得导入/导出明文旧 Key，不自动解析客户代码库；导入前须校验，
失败须给行级错误。`all_108_prds_complete=false`。

## PRD-062 配置导入导出 CLI 目录（已完成）

本节登记 PRD-062 最终模块边界。设计、两轮编码前自审、实现、专项、直接回归、0054 实迁移、真实
source/target CLI、权限、租户和 Gateway 调用均已通过；`all_108_prds_complete=false`。

- `control-plane-api/src/metahub_control/config_transfer/{models.py,parser.py,service.py,routes.py,__init__.py}`：
  ConfigExport v1 白名单 DTO、2 MiB/深度 8/200000 节点严格 JSON reader、当前租户一致导出、只读预检、
  create-only 单事务导入、行级安全报告、一次性 Virtual Key 分离响应和 replay 合同。导出在写事实前复用回导
  wire 门禁；manifest hash 排除 `exported_at`。
- `control-plane-api/src/metahub_control/{app.py,auth/dependencies.py,audit/service.py}`：注册
  `POST /config/export`、`POST /config/import/validate`、`POST /config/import`，分别执行后端
  `config.transfer.read/write` 权限，并登记 `config.imported/config.exported` 审计白名单。tenant/actor 只从
  Session 恢复，配置迁移审计不保存 User-Agent。
- `shared-sdk/python/metahub_shared/gatewayctl/{cli.py,__init__.py}`：标准库两级 CLI、严格本地清单和服务端响应
  验证、冻结 origin、同源 307/308 POST、原子导出、固定退出码和脱敏 stdout/stderr。active Virtual Key 即使
  validate 为 unchanged，也会在 apply 前独占预留私有密钥文件，防止两快照间并发变化造成明文无处交付。
- `scripts/gatewayctl.ps1`：按脚本位置加载 shared SDK 源码的 Windows 包装；Token 只由 CLI 从
  `METAHUB_CONTROL_TOKEN` 或显式 `--token-stdin` 读取，脚本不接受 tenant，也不扫描客户目录。
- `shared-sdk/python/metahub_shared/{rbac.py,runtime/errors.py,runtime/webhooks.py}`：权限目录 revision 2、
  `config.transfer.read/write`、配置迁移统一错误码与严格 `config.imported` 事件合同。HTTP replay 使用本次 trace，
  Webhook 补投继续使用首次提交事实中的原 trace、event ID 和发生时间。
- `infra/migrations/0054_config_import_export_cli.sql` 与 `.down.sql`：建立不可变
  `config_imports/config_import_rows/config_exports`、owner/状态/不可变 trigger、索引与 RBAC/AuditLog 扩展；
  导出事实不保存正文，因此 request ID 复用返回 `config_transfer.export_request_conflict` 而不伪造旧正文重放。
- `control-plane-api/tests/integration/test_prd062_{config_transfer,db_migrations}.py`、
  `control-plane-api/tests/unit/test_prd062_config_transfer_audit.py` 与
  `shared-sdk/python/tests/test_prd062_gatewayctl.py`：覆盖主流程、异常、权限、租户、迁移、审计、replay、
  严格协议、密钥文件边界及无 runpy warning 的真实模块启动。
- `docs/prd/PRD-062-config-import-export-cli.md`、`docs/api/config-transfer.md` 与
  `docs/development/gatewayctl-config-transfer.md`：分别保存设计及两轮自审、公开 HTTP/RBAC/错误合同和交付 CLI
  操作说明；`docs/testing/PRD-062-tests.md` 保存最终自动化、迁移、CLI/Control/Gateway 和隐私证据。

最终结果：Config Transfer API `6 passed`、CLI `33 passed`、Audit `13 passed`、0054 `3 passed`、直接
受影响回归 `21 passed`，中文注释门禁与关键模块语法编译通过。source/target 两库均为 `54/0054`、checksum
一致、完整性 `ok`、外键违规 0；真实 CLI、Viewer 403、跨租户 404、Gateway 失败追踪和 5 Token 成功用量通过。
按 Excel 两张核心表及完成记录交叉识别，下一项为
`PRD-063 | M1-企业POC | P1 | 核心层 | 国产Provider适配1 | 国内模型Endpoint适配规范`。

## PRD-063 国内模型 Endpoint 适配规范目录（已完成）

本节登记 PRD-063 最终模块边界。设计、两轮编码前自审、实现、两轮实现后复审、0055、专项和直接
受影响回归、开发库实迁移、真实 Control/Gateway/本地 Stub、Edge 双视口、隐私和服务日志均已通过。
该状态只代表 PRD-063 完成，`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/runtime/provider_adapter_spec.py`：不可变九类 Adapter spec、分类、鉴权、
  模型映射、Usage、错误、流式、价格、探测、canonical bytes/hash/version 与加载期失败关闭。
- `shared-sdk/python/metahub_shared/{rbac.py,runtime/errors.py,runtime/webhooks.py}`：复用
  `provider.health.read`，注册 Adapter 统一错误和严格 `adapter.spec_updated`；补投保持首次 source request/trace。
- `gateway-data-plane/src/metahub_gateway/providers/{base.py,factory.py,generic_http.py,openai_compatible.py,keypool.py}`：
  工厂与直接实例化双门禁、Adapter version/catalog hash 请求绑定、九类选择、auth、stream option、Usage 和
  安全错误日志；未知或漂移配置在 Key 选择与网络 I/O 前失败关闭。
- `control-plane-api/src/metahub_control/adapter_specs/{models.py,service.py,routes.py,__init__.py}`：
  `GET /adapter-specs` 的 Session tenant/实时后端权限/严格 query，以及内部激活的服务 Token、1024-byte
  分块正文门禁、幂等/冲突、唯一 active、提交后 Webhook 与统一 no-store 错误。
- `control-plane-api/src/metahub_control/{providers,config_transfer,provider_status,console,app.py}`：Provider
  create/update/import/test/probe 统一读取 spec/auth；健康联网固定已校验 DNS IP；注册 PAGE-090 导航 owner 和路由。
- `infra/migrations/0055_provider_adapter_specs.sql` 与 `.down.sql`：tenant-scoped activation、唯一 active、
  owner/immutable trigger、索引、现有/新租户 bootstrap、RBAC/导航/事件扩展及后续 catalog 事实 down guard。
- `console-web/src/lib/adapterSpecs.js`、`src/features/adapter-specs/{adapterSpecPageState.js,renderAdapterSpecs.js}`、
  `src/app/main.js`、`src/styles/main.css`：严格公开 DTO、三分类九类、竞品边界、筛选、独立 owner/generation、
  XSS 过滤、403/503/request_id 和 1280/390 响应式合同表。
- `shared-sdk/python/tests/test_prd063_{provider_adapter_specs,adapter_spec_webhook}.py`、
  `gateway-data-plane/tests/unit/test_prd063_provider_adapter_contracts.py`、
  `control-plane-api/tests/integration/test_prd063_{adapter_specs,db_migrations}.py`：覆盖冻结身份、九类最小合同、
  Gateway 失败关闭、权限/租户/内部身份/幂等、事件与 0055 保真。
- `console-web/tests/unit/{adapterSpecs,consoleShell,mainRefreshOwnership,providerStatus,prd063BrowserAcceptance}.test.mjs`：
  覆盖白名单 DTO、owner/迟到响应、XSS、相邻页面不回归、浏览器网络/布局/隐私合同。
- `scripts/acceptance/prd063-browser-acceptance.mjs`：固定三个本机 Origin 和
  `.artifacts/prd063-browser/<run-id>` 普通目录，完成真实登录、三次目录读取、九类 DOM、双视口 PNG、
  page/console error、Storage 隐私和注销清理。
- `docs/prd/PRD-063-domestic-model-endpoint-adapter-specification.md`、
  `docs/api/provider-adapter-specs.md`、`docs/testing/PRD-063-tests.md`：分别保存设计与四轮复审、
  用户/内部/Gateway 发布合同和最终自动化、迁移、Runtime、Browser 证据。
- `.artifacts/prd063-runtime-final/`：开发库升级前备份、迁移 metadata 和真实 API/Gateway/Stub 脱敏结论。
- `.artifacts/prd063-browser/run-20260801T100000-final/`：权威 Edge 批次；桌面默认六类与移动端迁移边界
  两张非空 PNG，外部请求、非法目录请求、page/console error 和敏感 DOM/Storage 命中均为 0。

最终结果：Shared/Gateway `86 passed`，Shared Webhook `16 passed`，Control `7 passed`，0055 `3 passed`，
Provider/Health `14 passed`，Webhook `8 passed`，Console `32 passed`，浏览器验收器 `8 passed`，中文注释
门禁通过。开发库为 `55/0055`、完整性 `ok`、外键违规 0；Runtime 与 Browser 均为 `status=passed`。
按 Excel `01_PRD总表` 与 `02_M0-M1核心开发` 交叉识别，其后实现项为
`PRD-064 | M1-企业POC | P1 | 核心层 | 契约测试 | Provider Adapter契约测试框架`。

## PRD-064 Provider Adapter 契约测试框架目录（已完成）

本节登记已落地的模块边界和最终证据。0056 开发库实迁移、Gateway/Control 自动化专项、两类 Adapter
受控真实 Gateway probe、Control RBAC/双租户/Outbox、PAGE-091、双视口 Edge、全链路隐私、三服务
HTTP 200、中文注释门禁和必要分层回归均已通过；`all_108_prds_complete=false`。

- `gateway-data-plane/src/metahub_gateway/provider_contracts/`：显式夹具矩阵、loopback mock、报告模型与
  canonical hash、离线 merge gate、受控 Gateway probe、Control 上报和 CLI。Gateway/Control 目标必须与
  `METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN`、`METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN` 完全同源；
  `METAHUB_CONTRACT_TRUSTED_TENANT_ID` 绑定报告 owner，凭据只从命名环境变量读取。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`：契约 probe 专用 tenant 证明只在
  Virtual Key 鉴权后校验并回送；缺失证明头保持普通 OpenAI-compatible 行为，重复、合并值或错租户失败关闭。
- `control-plane-api/src/metahub_control/adapter_contracts/`：内部
  `POST /internal/tenants/{tenant_id}/adapter-contract-runs` 和当前租户只读
  `GET /adapter-contract-runs[/{run_id}]`；内部 Token、严格 JSON、实时 `provider.health.read`、tenant-first
  查询、幂等/冲突、统一错误、request/trace ID 和 no-store 均在后端执行。
- `infra/migrations/0056_provider_adapter_contract_runs.sql/.down.sql`：新增 tenant-scoped 不可变 run/case、
  owner/计数/状态约束、失败事件 source/outbox 恢复及 down guard。升级前备份为 `55/0055`，当前开发库为
  `56/0056`，两者 `integrity_check=ok`、`foreign_key_check=[]`；fresh、幂等、down/re-up 和 down guard
  专项均已通过。
- `console-web/src/lib/adapterContractRuns.js`、
  `console-web/src/features/adapter-contract-runs/`、`console-web/src/app/main.js` 和
  `console-web/src/styles/main.css`：PAGE-091 合并门禁/真实探测分段、摘要、筛选、分页、详情、权限裁剪、
  stale response 淘汰、XSS 过滤和移动端列化；页面不提供发起真实 probe 的控制。最终单文件专项
  `15/15`；403 销毁旧受保护状态，503 只保留已净化历史页。
- `scripts/acceptance/prd064-browser-acceptance.mjs` 与
  `console-web/tests/unit/prd064BrowserAcceptance.test.mjs`：固定三个 localhost Origin 和
  `.artifacts/prd064-browser/<run-id>` 普通目录，覆盖真实登录、后端导航 owner、摘要、suite/status/mode、
  Control cursor 分页、详情、空态、一次性 403/503、注销、1280x720/390x844、PNG 非空及浏览器隐私。
  纯合同专项 `10/10`；真实 Edge 证据 `status=passed`，两张 PNG 均非空，浏览器 profile/download 已清理。
- `scripts/ci/provider-adapter-contract-test.ps1`：从任意 cwd 启动同一 Python CLI，报告只能原子写入
  `.artifacts`，合同失败直接传播非零退出码；Control 上报是显式配置的提交后副作用。
- `gateway-data-plane/tests/unit/test_prd064_{provider_adapter_contracts,gateway_probe_upload}.py`、
  `control-plane-api/tests/integration/test_prd064_{adapter_contracts,db_migrations}.py` 和
  `console-web/tests/unit/adapterContractRuns.test.mjs`：覆盖 merge gate、probe/upload、Control API/Outbox、
  0056 与 PAGE-091。当前已登记 Gateway `41/41`、直接受影响 ASGI `4/4`、Control/Outbox `5/5`、迁移
  `2/2` 和 Console `15/15`；最终回归边界以 `docs/testing/PRD-064-tests.md` 为唯一测试记录。
- `.artifacts/prd064-wrapper-secure-final/report.json`：当前安全版阶段性 mock 报告为
  `92 passed / 0 failed`，catalog 为 `adapter-spec-v1`；该单项证据不能替代其余运行验收。
- `.artifacts/prd064-runtime/20260801T125516/gateway-probe-acceptance-r4/evidence.json`：两类 Adapter 的
  非流式、SSE、429/503、Usage、request/trace 追踪、敏感 canary 零命中和隔离资源清理均已通过。
- `.artifacts/prd064-runtime/20260801T125516/control-plane-acceptance-r1/evidence.json`：真实 HTTP 的
  admin/developer、viewer 403、匿名 401、跨租户 404、幂等冲突和失败事件恢复均已通过。
- `.artifacts/prd064-browser-runtime/run-20260801T081846Z/runtime-evidence.json`：真实 Edge 双视口、
  分页/详情/空态、403/503/恢复、注销、隐私扫描及资源清理的最终封装证据。
- `docs/prd/PRD-064-provider-adapter-contract-test-framework.md` 与
  `docs/api/provider-adapter-contract-runs.md`、`docs/testing/PRD-064-tests.md`：分别保存设计/自审、稳定接口和
  最终测试/运行验收。Python 根级超时、分层失败闭环和 5 个目录边界排除项均如实保留。

按 Excel `01_PRD总表` 与 `03_M2-M3扩展交付` 交叉识别，PRD-064 之后的实现项是
`PRD-065 | M2-企业增强 | P2 | 扩展层 | UCD角色首页 | 多角色首页与导航`；该项现已完成，见下节。

## PRD-065 多角色首页与导航目录（已完成）

本节登记 PRD-065 的稳定模块边界和最终证据。两轮编码前自审、tenant-scoped 实现、0057、后端权限、
统一错误、PAGE-092、主流程/异常/权限/租户专项、真实 HTTP、六角色双视口 Edge、数据库终态、隐私扫描、
三服务 HTTP 200 和中文注释门禁均已通过；`all_108_prds_complete=false`。

- `control-plane-api/src/metahub_control/dashboard/catalog.py`、`models.py`、`role_service.py`、`routes.py`：
  固定五视图/六卡片目录、role-view DTO、tenant-first 配置读取、实时 permission 交集、有界聚合、严格 query
  及 `GET /dashboard/role-view`。身份、角色、tenant 和权限只从认证上下文恢复；显式越权失败关闭。
- `control-plane-api/src/metahub_control/auth/dependencies.py`：继续以 `dashboard.read` 作为后端首层权限门禁，
  且认证/权限先于原始 query 解析，避免匿名或缺权调用方探测视图目录。
- `control-plane-api/src/metahub_control/console/models.py`、`service.py`：新增
  `dashboard.role_viewed` exact payload，写事件时重新检查当前租户配置、config revision、实际视图和实时权限。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`：新增并登记角色首页配置损坏和
  query 错误的统一诊断，不把 SQLite、SQL、配置 JSON 或底层异常正文回显给调用方。
- `infra/migrations/0057_role_dashboard_views.sql/.down.sql`：新增 tenant-scoped
  `role_dashboard_configs`、现有/未来租户 canonical 六角色配置、延迟复合角色 FK、owner/revision/replace/delete
  门禁和事件表保真重建；down 只接受 canonical 终态并保留兼容访问轨迹。
- `console-web/src/lib/dashboard.js`、`console-web/src/features/dashboard/renderRoleDashboardPanel.js`、
  `roleDashboardPageState.js`：role-view client、响应白名单投影、authorization owner 状态机、视图切换先清卡、
  401/403 销毁、同 owner/同 view 503 partial、事件 claim 和单层响应式卡片。
- `console-web/src/app/main.js`、`console-web/src/features/developer-start/renderDeveloperStart.js`、
  `console-web/src/styles/main.css`：把现有 overview 导航绑定到 PAGE-092，保持旧 Dashboard 合同但不做失败回退；
  实现稳定桌面/移动布局、分段控制、命令权限求交和注销/换租户/授权 revision 漂移清理。
- `control-plane-api/tests/integration/test_prd065_role_dashboard.py`、
  `test_prd065_db_migrations.py`：六角色、第二租户、权限撤销、事件防伪、统一错误和 0057 上下迁移专项；最终
  分别为 `3/3` 和 `19/19`。
- `console-web/tests/unit/roleDashboard.test.mjs`、`dashboard.test.mjs`：PAGE-092 状态/渲染/安全合同 `14/14`，
  旧 Dashboard 接线兼容 `6/6`；Provider Health 直接受影响组合 `14/14`。
- `scripts/acceptance/prd065-browser-acceptance.mjs` 与
  `console-web/tests/unit/prd065BrowserAcceptance.test.mjs`：固定三个 loopback Origin 和
  `.artifacts/prd065-browser/<run-id>`，覆盖六角色、admin 切换、同 Session 后端 CAS 撤权/回落/恢复、注销、
  双视口、PNG 像素/布局、外网阻断和 URL/DOM/Storage/Console/Network 隐私；合同专项 `6/6`。
- `docs/prd/PRD-065-multi-role-home-navigation.md`、`docs/api/role-dashboard.md`、
  `docs/testing/PRD-065-tests.md`：分别保存需求设计及两轮自审、稳定 API/事件/迁移合同和最终测试运行记录。
- `.artifacts/prd065-runtime/run-20260801T2102CST/`：六角色/第二租户与异常/权限/事件两份真实 HTTP 证据；
  request/trace ID、401/403/422、撤权回落/恢复和隐私均通过。
- `.artifacts/prd065-browser/run-20260801T225656-CST/`：权威 Microsoft Edge 批次；六角色均验证
  `1280x720/390x844`，role-view/event 均 9 次 HTTP 200，两张 PNG 非空且溢出/重叠/隐私命中为 0。
- `.artifacts/prd065-browser-runtime/run-20260801T222423-CST/`：隔离数据库 closeout-v2 与正常开发库只读
  preflight；两者均为 `57/0057`、`integrity_check=ok`、外键违规 0，隔离会话清至 0 且 WAL/SHM 已清理。

最终结果：后端 `3 passed`、迁移 `19 passed`、直接受影响 Python `17 passed`、PAGE-092 `14 passed`、
旧 Dashboard `6 passed`、Provider Health 受影响组合 `14 passed`、浏览器合同 `6 passed`、Console lint
`178 files passed`、中文注释门禁通过；Gateway/Control/Console 为 `200/200/200`。按 Excel 两张核心表继续
识别的下一项为 `PRD-066 | M2-企业增强 | P2 | 扩展层 | 企业Chat | 企业Chat演示入口`。

## PRD-066 企业 Chat 演示入口目录（已完成）

本节登记已经稳定且完成验收的模块边界；专项、必要分层回归、开发库迁移、真实 Runtime、应用内
Browser 双视口、两次受治理 Chat、同会话撤权/恢复、注销和运行清理均已通过。

- `shared-sdk/python/metahub_shared/rbac.py`、`runtime/config.py`、`runtime/errors.py`、
  `runtime/webhooks.py`：提供 RBAC revision 3、`chat.use`、独立内部 Token、统一错误及
  `chat.message_sent` exact-set 安全事件。
- `infra/migrations/0058_enterprise_chat.sql/.down.sql`：新增 tenant-scoped `chat_sessions`、
  `chat_messages`、`chat_execution_grants`，以及 owner、状态单调、单在途和 grant 单次消费门禁。
- `control-plane-api/src/metahub_control/enterprise_chat/`：实现 options、创建 Session、发送、清空、
  实时后端 RBAC、tenant/owner/CAS 和 Control -> Gateway server-side 调用；正文不落库。
- `gateway-data-plane/src/metahub_gateway/api/internal_chat.py` 与 `api/openai_compatible.py`：消费独立
  Token 和一次性 grant，实时复核资源后进入公开入口共用治理流水线；内部 RequestLog 固定为
  `/internal/chat/completions`，Provider 调用仍使用 `chat/completions`。
- `console-web/src/features/enterprise-chat/` 与 `console-web/src/lib/enterpriseChat.js`：实现 PAGE-093、
  独立内存 owner/generation 状态机、后端权限导航、401/403 清理和响应式页面。
- `scripts/acceptance/prd066-runtime-acceptance.py`：在 `.artifacts/prd066-runtime/<run-id>` 使用动态
  回环端口完成 0058、真实 Gateway/Control/Console/Provider、治理事实、权限/租户/异常、隐私和关闭终态。
- `scripts/acceptance/prd066-browser-acceptance.mjs`：冻结双视口、真实 Chat、撤权恢复、截图像素和
  URL/DOM/Storage/Console/Network 隐私合同；应用内 Browser 已直接加载 localhost 并完成双视口、
  两次真实 Chat、治理摘要、清空、撤权安全回落、恢复后 options 加载和注销回到登录页。
- `scripts/acceptance/prd066-browser-runtime.py` 与 `scripts/acceptance/tests/test_prd066_browser_runtime.py`：
  提供真实浏览器资源准备/收口编排和纯沙箱安全合同；合同 `4 passed`，本轮实际页面由应用内 Browser
  验收，不需要启动外部 Edge。
- `.artifacts/prd066-browser-iab-20260802b/iab_stack_runner.py` 与
  `scripts/acceptance/tests/test_prd066_iab_stack_runner.py`：应用内 Browser 验收栈先独占监听再创建临时 Key，
  创建/停用同事务强审计，异常和 SQLite 锁有界清理；专项 `7 passed`，真实第二实例未改动首实例 Key。
- `docs/prd/PRD-066-enterprise-chat-demo-entry.md`、`docs/api/enterprise-chat.md`、
  `docs/testing/PRD-066-tests.md`：分别保存冻结设计与两轮自审、稳定 API 合同和直接测试/最终验收记录。
- `.artifacts/prd066-runtime/run-20260802T0955-CST/runtime-evidence.json`：当前权威运行证据，58 条迁移、
  三服务 200、成功及异常 request/trace、Provider `1` 次、Usage `11/7/18`、成本 `0.002500 CNY`、
  Budget reservation `1/1` 被正式 Usage 覆盖、数据库/日志明文命中 0，
  SHA-256 `97DCE88A2CBA1E1D8FADE66465C8FB06C6BC1D234DCCD77E748CB6F8E4FB0C45`。
- 应用内 Browser 固定证据：Provider `2/0`，桌面截图 SHA-256
  `75B9978BD6DC8546D433597DBE7346133697E831A3BFC5D13F4347212E7F7ACA`，移动截图 SHA-256
  `594B892398DF5F04FAF97DC64DF540B201B306EC48A495EE8FEC4CB0F950394F`，无溢出、重叠或敏感信息命中。
- `.artifacts/prd066-browser-iab-20260802b/browser-closeout-evidence.json`：保存撤权安全回落、权限恢复、
  `/chat/options 200`、`/auth/logout 200`、登录页恢复、Chat/Grant/外键零终态、端口释放和临时 Key
  created/updated 强审计配对；不保存任何账号、口令、Key、Token、正文或租户/用户标识。
- 兼容修复前全量基线：Python `1907 collected / 1886 passed / 21 excluded`（只排除用户禁用的 PRD-050 文件）、
  Node `33 passed`、Console `594 passed`、lint `184 files`。本轮定向 Python `69 passed`、Console `20 passed`、
  IAB 启动器 `7 passed`，窄范围 Ruff、`py_compile` 和中文注释门禁通过；开发库幂等确认至 `0058`，
  Gateway/Control/Console 运行期为 `200/200/200`。最终 `8000/8001/11434` 已释放且 `5173` 保持运行，
  PRD-066 已完成；`all_108_prds_complete=false`。

## PRD-067 企业 Prompt 模板库目录（已完成）

本节登记 PRD-067 的稳定模块边界和最终证据。设计、两轮主审、后端交叉终审、0059、后端实时权限、
tenant 复合 owner、统一错误与 request/trace ID、真实 Runtime、应用内 Browser 双视口、资源清理和中文注释
门禁均已通过；`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/prompt_templates/`：提供受限 `${lower_snake_case}` 解析、严格变量
  schema、有界内存渲染、独立 owner-bound AES-GCM/HMAC 和安全 Webhook 投影。
- `shared-sdk/python/metahub_shared/rbac.py`：RBAC catalog revision 4，新增
  `prompt_template.read/write/use`；实际授权仍逐请求读取当前 tenant 的数据库矩阵。
- `infra/migrations/0059_enterprise_prompt_template_library.sql/.down.sql`：新增 tenant-scoped Template、
  不可变 Version、Project/Model binding、Invocation、Operation、Audit/source event 和精确 owner/immutable
  trigger；down 在业务事实、自定义授权或未知依赖存在时失败关闭。
- `control-plane-api/src/metahub_control/prompt_templates/`：实现列表、最小 options、创建、详情、定义读取、
  metadata CAS、启停和发布接口；tenant/actor 只从 Session 恢复，正文只在授权 no-store 响应中解密。
- `control-plane-api/src/metahub_control/enterprise_chat/` 与
  `gateway-data-plane/src/metahub_gateway/api/internal_chat.py`：把模板版本/变量接入 PRD-066 单次 grant；
  Control/Gateway 双重复核 tenant、权限、current head、状态和 Project 后，在内存渲染最终 user message，
  继续走 DLP、Policy、预算、路由、Provider、Usage、FinOps 和 RequestLog 完整治理链。
- `console-web/src/features/prompt-templates/`、`console-web/src/features/enterprise-chat/`、
  `console-web/src/lib/promptTemplates.js`：PAGE-094 模板管理和 PAGE-093 模板选择/变量输入，包含独立 owner、
  generation、401/403 清理、定义 hydration、长文本省略和移动端稳定布局。
- `scripts/acceptance/prd067-runtime-acceptance.py`：使用真实 Control/Gateway/Console/本地
  OpenAI-compatible Provider 完成创建、发布、启停、异常、撤权恢复、跨租户、18 tokens、成本、审计、
  事件、Invocation、用量和敏感明文零落库验收。
- `.artifacts/prd067-browser/`：只保留应用内 Browser 生成的桌面详情、移动详情和移动列表三张最终截图；
  外部浏览器诊断脚本、合同测试和 11 个 `run-*` 批次均未纳入权威证据并已清理。
- `docs/prd/PRD-067-enterprise-prompt-template-library.md`、`docs/api/prompt-templates.md`、
  `docs/testing/PRD-067-tests.md`：分别保存冻结设计及复审、稳定 API/RBAC/错误合同和最终测试运行记录。
- `.artifacts/prd067-runtime/run-20260802T110138Z-c00b97cac3/runtime-evidence.json`：Runtime 权威证据，
  SHA-256 `AFE1FDF4710AB35DB7E82F0C967E94E482E2A4FC21B3B4270038C3BFE4F1D49A`。
- 应用内 Browser 截图 SHA-256：桌面详情
  `D593B5EE648FF6B28710079B3410F64FE34E8B718961D9A3B0E3294772EC427A`、移动详情
  `3514737BEC7051800800C832CB14F699C058577B5424FE2FC3696EF63025EDA1`、移动列表
  `1AC9ABA7FE5B74B36CE93632BF1C3B42AC01B5CD09F94BA4434A276CE8E64E18`；双视口无横向溢出，
  移动工具栏按钮同行且 25 个列表项裁切数为 0。

最终结果：Python 受影响组合 `73 passed`、Console 相关 `38 passed`、移动布局定向 `10 passed`、
lint `188 files`、中文注释门禁通过；开发库为 `59/0059` 且幂等，完整性/外键/测试 Session/Chat/Grant/
临时 Key 终态均通过，三服务为 `200/200/200`。
Excel 总表、M2/M3 交付表和验收清单确认的下一项为
`PRD-068 | M2-企业增强 | P2 | 扩展层 | 策略模板库 | 行业/场景策略模板库`。

## PRD-068 行业/场景策略模板库目录（已完成）

本节登记已经落地并通过自动化与运行验收的模块边界；正式 API 真实 apply、应用内 Browser
`1280x720` 与 `390x844` 双视口和注销清理均已完成。PRD-068 已完成，但总体目标仍在进行中：
`all_108_prds_complete=false`。

- `shared-sdk/python/metahub_shared/policy/`：扩展受限策略 wire 与纯求值器，使 `mask/log_only` 作为非终止
  modifier 继续扫描最终 `allow/deny/route_to`，保持确定顺序和旧动作兼容。
- `gateway-data-plane/src/metahub_gateway/`：在 Provider 调用前对 Chat、Stream、Embeddings 的安全文本副本
  执行有界脱敏，未知结构、遍历超限或 capability 损坏均失败关闭且不调用 Provider。
- `control-plane-api/src/metahub_control/policy_templates/`：固定五模板目录、同租户绑定、精确影响、双强 CAS、
  原子 apply、幂等回放、分类复合权限、持续发布不变量和模板感知补偿 rollback。
- `control-plane-api/src/metahub_control/policies/`、`policy_snapshots/`：抽取 connection-level 编译/发布 helper，
  由旧接口和模板事务共用；普通 PRD-045 rollback 合同保持不变。
- `infra/migrations/0060_industry_scenario_policy_template_library.sql/.down.sql`：新增模板、application、依赖、
  item、event、RBAC revision 5、mask trace 及 tenant 复合 owner/不可变/降级保护。
- `console-web/src/features/policy-templates/`、`console-web/src/lib/policyTemplates.js`、
  `console-web/src/app/main.js`：PAGE-095 目录、绑定、精确预览、incompatible 修复入口、完整确认摘要、
  apply 结果与 Snapshot 入口；owner 变化、403、注销、迟到响应和损坏 ID/版本均失败关闭。最终 Browser
  验收补正 Candidate CAS 的 `before_candidate_hash` 校验，并为移动确认框增加固定遮罩和长 ID 断行。
- `docs/prd/PRD-068-industry-scenario-policy-template-library.md`、`docs/api/policy-templates.md`、
  `docs/testing/PRD-068-tests.md`：分别保存两轮自审设计、稳定 API/RBAC/错误合同和分层测试证据。
- `.artifacts/prd068/provider-adapter-contract-report.json`：独立离线合并门禁 `92/92 passed`、
  `failed_cases=0`，SHA-256
  `A04F5E56F4DFA48DBAF526A32E2B1FA97D837773E3C794FEC84143BD14B468A1`。

当前自动化结果：Control `13 passed`、迁移 `12 passed`、Shared `26 passed`、Gateway `12 passed`、
安全回放 `11 passed`、真实 Provider 脱敏 `2 passed`、PAGE-095 `16 passed`、Node `33 passed`、
Console 全量 `619 passed`、lint `192 files`、中文注释门禁通过。唯一一次根级 Python 全量为
`2025 passed / 29 failed`，原失败节点定向 `29/29 passed`，未重跑全量。开发库为 `60/0060`，
Gateway/Control/Console 均为 HTTP 200。真实 apply 已把已发布基线推进到 Snapshot v12，candidate 与
current 一致；数据库中 application 1 条、item/dependency 各 2 条，`policy.created`、
`policy_snapshot.published`、`policy_template.applied` 三类 source event 与同 action AuditLog
均各 1 条，生成 Policy 为 active，`integrity_check=ok`、外键违规 0，apply request ID 为
`req_prd068_runtime_apply_redaction_v12`。应用内 Browser 最终验证五模板、已应用后的 incompatible 防重复、
预算模板 `ready_with_warnings`、完整确认摘要、Snapshot v12、权限状态、无页面级横向溢出和零页面错误；
注销后模板页、绑定、预览、确认框和原因 DOM 均为 0。Excel 确认的下一项为
`PRD-069 | M2-企业增强 | P2 | 扩展层 | 语义安全 | 轻量敏感意图分类`。

## PRD-069 轻量敏感意图分类目录（已完成）

- `shared-sdk/python/metahub_shared/intent_security/`：纯本地、确定性的强规则与轻量分类器；输出固定
  taxonomy、整数分数和版本，不返回正文、命中位置或 hash。20k 性能闭环使用 NFKC 已规范化和纯字母
  数字快路径，不改变全角、大小写或有限分隔符语义。
- `infra/migrations/0061_lightweight_sensitive_intent_classification.sql/.down.sql`：新增 result/event、
  RequestLog 9 列、RBAC revision 6、tenant-qualified owner/immutable trigger 和完整降级门禁。
- `control-plane-api/src/metahub_control/intent_security/`：权限先行、严格 JSON 的单次分类入口；结果/event
  同事务，detected 提交后复用持久 Webhook。`request_logs/` 增加三项 owner 关联的七状态安全摘要。
- `gateway-data-plane/src/metahub_gateway/intent_security/`：DLP 后、Rate Limit 前同步强闸门和有界异步
  worker；分类与存储错误分离，异步故障不反压主请求。
- `gateway-data-plane/src/metahub_gateway/usage/{ledger.py,spool.py}`：RequestLog intent 冻结字段和 crash
  spool v3；writer 只写 v3，reader 对 v1/v2/v3 分别执行 exact-set 校验。
- `gateway-data-plane/src/metahub_gateway/{api/openai_compatible.py,app.py,dlp/service.py}`：分别固定
  DLP 后/限流前的一次性语义闸门、Intent/Webhook worker 启停顺序，以及 DLP 文本抽取的只读复用边界。
- `console-web/src/features/intent-security/`、`src/lib/intentSecurity.js`：PAGE-096 owner-bound 一次性分类；
  `features/request-logs/` 在 PAGE-097 展示七状态并提供显式有界刷新。
- `docs/prd/PRD-069-lightweight-sensitive-intent-classification.md`、`docs/api/intent-security.md`、
  `docs/testing/PRD-069-tests.md`：分别保存两轮自审设计、公开 API/RBAC/错误合同和分层验收证据。

最终专项、Node/Console 全量、Console lint、中文注释、开发库 `61/0061`、三服务真实 API 和应用内
Browser 双视口均已通过。唯一一次根级 Python 全量为 `2146 passed / 4 failed`；四项均为历史测试对
`0061` 的合同漂移，原 node ID 定向 `4/4 passed`，未重跑全量。新增 20k 性能、Webhook queue 和
shutdown timeout 门禁 `4/4 passed`；中文 strong/light P95 为 `6.7792/8.1162ms`，均低于 10ms。
Gateway/Control/Console 最终为 HTTP `200/200/200`，开发库完整性和外键通过，39 个运行日志文件敏感命中
为 0。PRD-069 已完成；总体目标仍为 `all_108_prds_complete=false`。

## PRD-070 多轮会话风险引擎目录（已完成）

- `shared-sdk/python/metahub_shared/session_risk/`：提供 current-turn 有界信号、固定整数计分/阈值、
  versioned 分域 HMAC 和不可变模型；不返回正文、命中位置或完整 session digest。
- `infra/migrations/0062_multi_turn_session_risk_engine.sql/.down.sql`：新增 tenant-scoped score、
  observation、freeze overlay、action 和 source event，升级 RBAC revision 7，并以 owner、状态机、
  append-only、REPLACE 和 down guard 约束跨租户及降级安全。
- `gateway-data-plane/src/metahub_gateway/session_risk/`：实现公开 Header/企业 Chat 可信 scope、Redis Lua
  原子累计、pending evidence、数据库持久化、冻结预检、current/previous cycle 选择和 HMAC 移除 verifier。
  OpenAI、内部 Chat、DLP/Intent/Usage 接入点保持 Provider 前顺序，spool payload v4 对 v1-v3 兼容。
- `control-plane-api/src/metahub_control/session_risks/`：实现 tenant-first 列表、签名 cursor、read/manage
  双权限、事务内复核、CAS/持久幂等、独立 session/Key overlay 处置、Audit/Event 和提交后 Redis 同步。
- `console-web/src/lib/sessionRisks.js`、`src/features/session-risks/` 与 `src/styles/main.css`：实现
  PAGE-098 exact wire、owner/generation 状态、权限裁剪、筛选、详情、动作时间线和容器自适应布局。
- `scripts/acceptance/prd070-resp-server.py`：仓库内回环 RESP/EVAL 验收服务，只验证生产 Redis Store
  协议，不冒充外部 Redis 产品；覆盖三轮冻结、pending/ACK、reset、restore 和轮换扫描。
- `scripts/preflight/prd070-hmac-rotation-verifier.py`：previous 配置移除前的正式只读门禁。退出码
  `0/3/2` 分别表示 ready/blocked/unavailable，输出只含聚合计数且不接收命令行 URL 或 Secret。
- `docs/prd/PRD-070-multi-turn-session-risk-engine.md`、`docs/api/session-risks.md` 与
  `docs/testing/PRD-070-tests.md`：分别保存冻结设计及两轮自审、稳定 API/轮换运维合同和分层验收记录。
- `.artifacts/prd070-runtime/run-20260804T091013/runtime-acceptance.json`：权威运行证据，SHA-256
  `B5C9CE673231F8BE84CC0E2A3208C5A27A1EF35F02AEF9288C13996D3ABE9B99`。

当前结果：聚焦 Python `86 passed`、HMAC 相关回归 `35 passed`、数据库文件系统故障节点另行 `1 passed`、
PAGE-098 `11 passed`、Console lint `201 files`、定向 Ruff 和中文注释门禁通过；开发库为 `62/0062`，
三服务 HTTP `200/200/200`。应用内 Browser 已完成 `1280x720` 与 `390x844` 双视口：默认/已处置筛选、
详情、2 个冻结 overlay、5 条动作时间线及零横向溢出均通过，桌面筛选栏溢出已修复并复验。文本证据为
`.artifacts/prd070-browser/in-app-browser-acceptance.txt`；
`.artifacts/prd070-browser/browser-control-failure.txt` 仅保留并行 Edge extension 超时诊断，不是权威验收。
PRD-070 已完成。Excel 交叉确认的下一项为
`PRD-071 | M2-企业增强 | P2 | 扩展层 | 输出安全 | 输出侧安全检测与流式滑窗`；该项现已完成。
总体目标仍为 `all_108_prds_complete=false`。

## PRD-071 输出侧安全检测与流式滑窗目录（已完成）

- `shared-sdk/python/metahub_shared/output_safety/`：冻结规则/决策模型、非流式已知字段投影、Token 估算、
  有限滑窗扫描和严格 `BoundedSseEventFramer`；同 lane 可跨 event 续接，不同 choice/字段/tool call 不拼接。
- `gateway-data-plane/src/metahub_gateway/output_safety/`：负责 tenant-first output/both 规则快照、Provider 前
  pending owner、单次终态、append-only source event 和无正文 Webhook 投影。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`：在非流式成功响应返回前执行全文扫描，
  在流式完整 SSE event 放行前执行检查点；阻断关闭上游、禁止 Fallback，并按 observed/scanned/emitted
  三类计数收口 Usage、暴露和统一错误。
- `gateway-data-plane/src/metahub_gateway/streaming/sessions.py`、`cache_affinity/lifecycle.py` 与
  `usage/ledger.py`：增加 `output_blocked/output_safety_unavailable` 原子终态、Provider 成功质量语义、
  Usage durability 和有 scan 才写入的 spool v5/dispute 幂等恢复。
- `infra/migrations/0063_output_safety_streaming_window.sql/.down.sql`：增加 `dlp_rules.scan_scope`、
  `output_safety_scans`、`output_safety_events`、`finops_ledger_disputes`、StreamSession/RequestLog 终态与
  tenant-qualified owner/不可变/down guard；RBAC catalog 保持 revision 7。
- `control-plane-api/src/metahub_control/{dlp,request_logs,finops}/`：DLP API 支持
  `input/output/both`，PAGE-099 固定复用 `terminal_stage=output_safety`，PAGE-100 返回严格扫描详情，
  FinOps 只投影 scan ID、待复核状态和人工冲正资格。
- `console-web/src/features/output-safety/`：PAGE-099 输出安全事件列表、四类筛选、分页和详情；
  `features/request-logs/` 在 PAGE-100 展示输出安全证据，`features/dlp/` 与 `features/finops-ledger/`
  分别展示检测方向和最小争议徽标。
- `shared-sdk/python/tests/test_prd071_output_safety.py`、
  `gateway-data-plane/tests/{unit,integration}/test_prd071_*.py`、
  `control-plane-api/tests/{unit,integration}/test_prd071_*.py` 和
  `console-web/tests/unit/outputSafety.test.mjs`：覆盖扫描/framer、热路径、持久化、原子终结、spool、
  迁移、Control 读模型、权限、租户、owner 完整性与前端 owner/DOM。
- `scripts/acceptance/prd071-runtime-acceptance.py`：只连接工作区开发库和既有回环 Gateway/Control，临时提供
  本机 OpenAI-compatible Provider，完成 clean、非流式阻断、跨 chunk 流式阻断、统一错误、持久 owner、
  账单/事件、权限/租户与敏感明文零命中的真实 HTTP 验收，并将安全证据写入 `.artifacts/prd071-runtime/`。
- `scripts/acceptance/tests/test_prd071_runtime_acceptance.py`：冻结验收器的工作区路径、回环 Origin、失败关闭、
  一次性 Provider Key、流式构造、敏感字段禁入证据和原子写入合同；最终专项为 `7 passed`、退出码 `0`。
- `docs/prd/PRD-071-output-safety-streaming-window.md`、`docs/api/output-safety.md` 与
  `docs/testing/PRD-071-tests.md`：分别保存冻结设计及两轮自审、公开合同和完整最终验收记录。
- `.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/`：保存脱敏的正式 Runtime、应用内 Browser
  证据与 `SHA256SUMS.txt`；数据库、日志、Prompt/Response、DLP pattern、Provider Key 和 Token 不进入制品。

PRD-071 的 0063 开发库升级、相关分层复跑、中文注释、真实三服务/API、账单事件 owner、隐私和应用内
Browser 双视口均已通过。Excel 多表交叉确认的下一条未完成项为
`PRD-072 | M2-企业增强 | P2 | 文件解析 | 文件上传与文本解析基础`；该项现已完成。

## PRD-072 文件上传与文本解析基础目录（已完成）

- `shared-sdk/python/metahub_shared/files/`：冻结 PDF/DOCX/UTF-8 TXT 格式合同、有界解析、确定性 Token
  估算、tenant DLP 全文扫描、分域 HMAC/AES-GCM 和只保存认证密文的本地原子 Blob Store。
- `control-plane-api/src/metahub_control/files/`：提供鉴权后有界 multipart 读取、同步容量门禁、
  `POST /files`、`GET /files`、`GET /files/{id}`、两阶段状态事务、租户签名 cursor 和安全事件派发。
- `shared-sdk/python/metahub_shared/runtime/{config,errors,error_diagnosis,i18n,logging,webhooks}.py`：增加
  文件密钥/存储/资源配置、统一文件错误、`pypdf` 自由日志拦截以及 `file.uploaded/file.scanned` exact-key
  Webhook 合同；Provider、Prompt、Auth 与文件根密钥保持独立。
- `infra/migrations/0064_file_upload_text_extraction.sql/.down.sql`：增加 UploadedFile、FileExtract、
  FileDlpMatch、FileEvent 的 tenant owner、单向状态机、不可变和 down guard；RBAC 升至 revision 8。
- `console-web/src/features/files/` 与 `src/lib/files.js`：实现 PAGE-101/102 上传栏、状态筛选、私有 cursor、
  文件处理记录、DLP/病毒/Token 摘要、只读权限投影和 owner/generation 清理。
- `shared-sdk/python/tests/test_prd072_*.py`、`control-plane-api/tests/**/*prd072*.py` 和
  `console-web/tests/unit/{files,prd072BrowserAcceptance}.test.mjs`：覆盖主流程、格式/解析/存储、DLP、权限、
  租户、迁移、DOM 隐私和双视口布局合同。
- `scripts/acceptance/prd072-runtime-acceptance.py`：仅访问工作区隔离数据库、对象目录和固定回环三服务，
  验证 TXT/DOCX/PDF、DLP block、SecurityAdmin 只读、跨租户 404、密文、事件、数据库完整性和日志 canary；
  UTF-8、UTF-16LE、UTF-16BE 均按等价敏感值扫描。
- `docs/prd/PRD-072-file-upload-text-extraction.md`、`docs/api/files.md` 与
  `docs/testing/PRD-072-tests.md`：分别保存冻结设计及两轮自审、公开 API 合同和完整验收记录。
- `.artifacts/prd072-runtime/prd072_20260804T212908Z_922f8ab2ff200912/`：只保存脱敏 Runtime JSON；
  文件正文、文件名、规则 pattern、密码、Session Token、数据库和日志均不进入提交制品。
- `.artifacts/prd072-browser/prd072_20260804T212908Z_922f8ab2ff200912/`：保存脱敏 Browser JSON、
  `SHA256SUMS.txt` 及桌面/移动截图；抽取正文 canary、对象键、规则内容和凭据均未进入 DOM 或证据。

PRD-072 的 0064、相关分层回归、中文注释、真实三服务/API、密文/隐私和 Browser 双视口均已通过。
PRD-072 完成时，Excel 总表、M2/M3 交付、页面、API、数据对象、Webhook 和验收用例共同确认其
下一项为 `PRD-073 | M2-企业增强 | P2 | 文件型注入防护 | 隐藏 Prompt Injection 检测`；对应
PAGE-103、`POST /files/{id}/security-scan`、FileSecurityFinding、
`file.prompt_injection_detected` 和 TC-0145/0146。PRD-073 主体实现、追加 0066 source-outbox 崩溃恢复、
定向回归、`66/0066` 真实 Runtime 和实际页面复验均已通过；全部 108 条总体目标仍为
`all_108_prds_complete=false`。

## PRD-073 隐藏 Prompt Injection 检测目录

- `shared-sdk/python/metahub_shared/file_security/`：冻结安全报告/发现合同、可见 Prompt 规则、DOCX
  `w:vanish`/颜色/字号/嵌入对象检测、PDF 文本渲染模式/OCG/附件检测、候选图片提取、有界离线 OCR、
  PRD-072 认证持久抽取正文的模型语义扫描、canonical report SHA-256、HMAC evidence 和动态模型
  上下文边界；原文件结构操作数不能替代 `trusted_extracted_text`。
- `control-plane-api/src/metahub_control/file_security/`：提供
  `POST /files/{id}/security-scan`、`GET /files/{id}/security-report` 和 CSV 下载；扫描前与事务内执行
  `file.read + file.security.scan` 双权限和 tenant owner 复核；同一个短 `BEGIN IMMEDIATE` 权限快照
  读取并验真原件与抽取 Blob，报告读取只要求 `file.read`；`outbox.py` 以 90 秒 lease、每次真实
  网络 attempt 前完整 CAS 续租和稳定 `fsev_` event ID 恢复 Webhook intent。worker 先在独立短事务真实
  claim 并提交 `processing` lease；随后当前有效 owner 在另一个独立事务以 tenant/event/token/owner/
  attempt/当前且未过期 lease 完整 CAS，补齐 legacy 部分 delivery、创建/恢复 intent 并持久冻结包括空集合
  在内的 Endpoint 快照，冻结后不追溯新 Endpoint；坏 source 先完成真实 claim，再由 owner 交叉验证并以
  完整 CAS 隔离为 `webhook.payload_invalid`，不能在 claim 前验证或伪造 attempt；
  `scheduler.py` 组合进 Control lifespan，在启动、wake 或轮询时 drain，以 `threading.Event` 协作停服
  并等待同步 worker 完成。坏 source 固定隔离后继续领取；source 终态与剩余 pending delivery/死信
  同事务收敛。路由提交后只发送 wake 信号，不直接联网投递。
- `control-plane-api/src/metahub_control/webhooks/service.py`：文件安全恢复复用公共快照和失败关闭 helper；
  attempt 摘要以 pending/前序 attempt/Secret 版本 CAS，阻断 reaper 后迟到 worker 覆盖终态。
- `shared-sdk/python/metahub_shared/runtime/{config,errors,error_diagnosis,i18n,webhooks}.py`：增加扫描/OCR
  部署上限、统一错误与中文/英文展示，并冻结 `file.prompt_injection_detected` 七字段 exact-key payload。
- `infra/migrations/0065_file_prompt_injection_detection.sql/.down.sql`：增加 FileSecurityScan、
  FileSecurityFinding、FileSecurityEvent 三张 tenant-qualified append-only 表、失败 attempt/权威结果索引、
  owner/计数/不可变/down guard；新增 `file.security.scan` 并把 RBAC catalog 升至 revision 9。
- `infra/migrations/0066_file_security_event_outbox_recovery.sql/.down.sql`：不改写已发布 0065 checksum；
  新增 tenant-qualified `file_security_event_outbox`、存量 source 回填、`AFTER INSERT` 同事务 trigger、
  持久 `delivery_snapshot_frozen_at`、到期索引、90 秒 claim/续租与不可变状态机。
  pending/processing/dispatched/failed 任一业务行存在时拒绝 down。
- `console-web/src/features/file-security/` 与 `src/lib/fileSecurity.js`：实现 PAGE-103 文件选择、扫描、
  报告、风险指标、发现表和 CSV；私有 cursor、owner/generation/Abort、列表重排和权限漂移共同阻止
  迟到响应或旧身份数据写回。
- `shared-sdk/python/tests/test_prd073_*.py`、`control-plane-api/tests/**/*prd073*.py` 和
  `console-web/tests/unit/{fileSecurity,prd073BrowserAcceptance}.test.mjs`：覆盖检测器、配置、迁移、API、
  权限/租户、Webhook、模型准入、PDF `ToUnicode/CMap` 语义绕过、FindingCollector 饱和优先级、
  0066 trigger/backfill、启动恢复、90 秒 lease/逐网络 attempt 续租、legacy 部分快照补齐、空快照冻结、
  冻结后不追溯新 Endpoint、协作停服等待同步 worker、失败退避、过期接管、迟到 worker CAS、
  第十次终结、strict DTO、DOM 隐私和双视口布局。
- `scripts/acceptance/prd073-runtime-acceptance.py`：只访问工作区 `.tmp` 隔离数据库/对象目录和固定回环
  三服务，真实验证 clean、可见/隐藏/OCR 注入、字体映射 PDF、只读 403、跨租户 404、CSV、模型
  准入、完整性、隐私、`66/0066`、Control 真停服、停服期间 source 提交、新 lifespan scheduler 恢复、
  冻结快照、outbox 终态与协作停服；管理员密码只从环境读取一次，临时用户、Session 和同步 worker
  在失败路径也通过 finally/lifespan 收敛。日志 canary 同时覆盖 UTF-8/UTF-16LE/UTF-16BE。
- `scripts/acceptance/tests/test_prd073_runtime_acceptance.py`：冻结工作区路径、回环 URL、密码移除、
  真实 DOCX/PDF/PNG/CMap 样本、失败清理与证据脱敏，不连接任何服务；最终计数见测试记录。
- `docs/prd/PRD-073-hidden-prompt-injection-detection.md`、`docs/api/file-security.md` 与
  `docs/testing/PRD-073-tests.md`：分别保存冻结设计及两轮自审、公开 API/Webhook 合同和最终验收记录。
- `.artifacts/prd073-runtime/prd073_20260806T062221Z_b2632ac89f57fc3a/`：保存唯一权威 0066 Runtime
  JSON 与 `SHA256SUMS.txt`；JSON SHA-256 为
  `E2EFF29227A4246AE8FBB86B5B3C973C6E5212A1568AA32D9ABFB05B0DAE34FD`。旧 `65/0065` Runtime
  仅是修复前检查点，不再作为 PRD-073 终态证据。
- `.artifacts/prd073-browser/prd073_20260805T045800Z_4f3ee3d6aa3f233b/`：保存最终 Browser JSON、
  `SHA256SUMS.txt` 和桌面/移动截图；Prompt、对象键、report SHA、evidence fingerprint 与文件名
  URL/dataset 的 DOM 命中均为 0。0066 不改变 PAGE-103 或公开 API，因此该 Browser 证据仍有效。

PRD-073 的主体实现和 0066 终审修复均已落地：Shared 强制扫描 PRD-072 持久抽取正文以关闭 PDF
字体 CMap 语义绕过，FindingCollector 饱和后仍保留 Prompt 与 coverage；0066 以同事务 trigger、
source outbox、启动恢复、90 秒 lease、逐网络 attempt 完整 CAS 续租、持久 Endpoint 快照和协作停服
提供稳定 event ID 的 at-least-once。最终测试计数、`66/0066` Runtime 路径、SHA-256、开发库
checksum、三服务和双视口页面复验均已写入测试记录；旧 `65/0065` 结果未用于宣称终态。Excel 多表
交叉确认其后的下一项为
`PRD-074 | M2-企业增强 | P2 | 批处理调度 | 异步批处理 Scheduler 与 Worker`，对应 PAGE-104、
BatchJob/BatchTask、三个 BatchJob API、`batch_job.created/completed` 和 TC-0147/0148。
全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## PRD-074 批量任务 Scheduler 与 Worker 目录（已完成）

- `shared-sdk/python/metahub_shared/batch/`：冻结 Batch operation、owner-bound payload/result 加密与 HMAC，
  以及 Control/Gateway 共用的 12 位 canonical 单价 wire；Provider Key、Token、Prompt 和 Response 不进入
  repr、日志或公开证据。
- `control-plane-api/src/metahub_control/batch_jobs/`：提供 tenant-qualified BatchJob/BatchTask API、
  后端 RBAC、文件安全准入、路由/价格冻结、预算 hold、同秒公平 Scheduler、短事务 lease/heartbeat、
  独立 Worker、崩溃 Reaper、结果读取、取消和人工对账。
- `gateway-data-plane/src/metahub_gateway/api/internal_batch.py` 与 `batch/`：只接受内部 Batch Token 和
  一次性 grant，实时优先 gate 在读取 grant/正文前可直接返回 busy；消费事务复核 actor/RBAC、owner、
  lease、route、allocation 和 deadline，再复用完整 Gateway 治理链并先持久化加密结果。
- `control-plane-api/src/metahub_control/webhooks/{scheduler.py,service.py}`：Control lifespan 恢复
  `batch_job.created/completed` source/outbox；pending resume 从不可变 Batch event 严格恢复
  `source_request_id` 和完整身份，Worker 不持有 Webhook Secret。
- `infra/migrations/0067_batch_scheduler_worker.sql/.down.sql`：新增 Batch Job/Task/Attempt、预算
  hold/allocation、grant、tenant scheduler cursor、event/outbox 及 owner/状态/租约/金额/不可变/down guard；
  RBAC catalog revision 10 为 64 个 permission templates、173 条角色模板关系。
- `console-web/src/features/batch-jobs/` 与 `src/lib/batchJobs.js`：实现 PAGE-104 列表、状态/进度、
  创建、取消、失败原因和显式结果读取；使用 owner/generation/Abort、先清理后中止和后端权限导航裁剪。
- `docs/api/batch-jobs.md`、`docs/prd/PRD-074-batch-scheduler-worker.md` 与
  `docs/testing/PRD-074-tests.md`：分别保存公开 API、两轮编码前自审与实现后复核、最终测试记录。
- `scripts/acceptance/prd074-runtime-acceptance.py`：在仓库内空 SQLite、固定回环 Gateway/Control、
  本地 OpenAI-compatible Provider/Webhook stub 和独立 Worker 上验证实时优先、恢复、取消、撤权、
  RequestLog/Usage/FinOps、成本、outbox、隐私与进程清理；工作目录独占且失败运行不复用。
- `.artifacts/prd074-runtime/prd074_20260808T105000Z_runtime_final2/`：只保留脱敏 Runtime JSON 与
  SHA-256；`status=passed`、`67/0067`、完整性 ok、外键违规 0、23 个文件明文命中 0。
- `.artifacts/prd074-browser/prd074_20260808T094300Z_page104/`：只保留脱敏 Browser JSON、三张
  被 manifest 引用的 Chromium 截图和 SHA-256；桌面/移动/移动创建均无横向溢出、重叠或文本溢出。

Excel 总表、M2/M3 交付、PAGE-105、API、数据对象、Webhook 和验收用例共同确认，下一条权威未完成项为
`PRD-075 | M2-企业增强 | P2 | 模型评测 | 模型 A/B 测试与质量评估`；对应
`POST /experiments`、`GET /experiments/{id}/results`、ModelExperiment/ExperimentResult、
`experiment.completed` 和 TC-0149/0150。`all_108_prds_complete=false`。

## PRD-075 模型 A/B 测试与质量评估目录（已完成）

- `shared-sdk/python/metahub_shared/experiments/`：冻结采样与 variant 独立 HMAC 域、exact A/B 配额、
  actor-scoped 幂等、route-config fingerprint、整数统计和 `experiment.completed` 安全 payload；RBAC
  revision 11 与统一错误目录仍由 Shared runtime 提供。
- `control-plane-api/src/metahub_control/experiments/`：提供 tenant-qualified 创建/列表/详情/结果 API、
  五权限事务内复核、稳定私有 cursor、Scheduler、finalizer、execution/settlement Reaper、250ms 统计快照
  和可恢复 source/outbox；Reaper 仅在 tenant/request/model/provider owner 完整匹配时降级残留 success
  RequestLog，owner 缺失或冲突会在 Outcome 前失败并回滚整个事务。跨 tenant 与不存在同形，前端入口不构成授权。
- `gateway-data-plane/src/metahub_gateway/experiments/` 与 `api/openai_compatible.py`：仅让符合条件的公开
  Virtual Key Chat 进入实验，原子创建 Assignment/Execution/Settlement，冻结 exact Model/Provider route，
  以 generation/lease/send fence 保护流/非流终态，并继续记录 RequestLog、Usage、FinOps 与失败事实；OpenAI
  raw passthrough 使用有界、无正文留存且不重分块的 `[DONE]` observer，Generic/custom NULL marker 保持 clean EOF
  兼容，active 审计失败由 shielded cleanup 关闭响应、Session 和 Cache Affinity owner。实验 timeout/通用异常
  不在持久化前抢先发送，Session/RequestLog/Usage/Outcome 收口后只交付一个最终 SSE error；普通流保留即时
  error 时序并以已发送标记阻止 finalizer 重复交付。
- `infra/migrations/0068_model_ab_testing_quality_evaluation.sql/.down.sql`：新增 request scope claim、
  Experiment、Assignment、Execution、Outcome、Settlement、Feedback Attribution、Result、Event/Outbox 和
  scheduler cursor；复合 tenant owner、deferred closure、状态/不可变 trigger 与原子 down guard 完整，
  RBAC catalog 提升到 revision 11、66 个 permissions、179 条 canonical 关系。所有认证请求保留普通 claim；
  只有同时具备 Virtual Key、Project、Environment ID 和规范 environment code 时才写实验 scope claim。
- `console-web/src/features/experiments/` 与 `src/lib/experiments.js`：实现 PAGE-105 列表、不可变配置详情、
  A/B 权威结果和创建 Dialog，使用 permission owner、generation 与独立 Abort lanes 拒绝迟到响应；
  desktop/mobile Browser 已验证详情、最终结果、焦点、移动滚动、后台刷新和撤权清理。
- `shared-sdk/python/tests/test_prd075_experiment_contracts.py`、
  `gateway-data-plane/tests/**/*prd075*.py`、`control-plane-api/tests/**/*prd075*.py` 和
  `console-web/tests/unit/prd075Experiments.test.mjs`：覆盖 Shared、Gateway、Control、迁移、Console 的
  主流程、异常、权限、跨租户、并发、恢复、回退和隐私合同；最终受影响回归为 Shared/Provider `180 passed`、
  Gateway `204 passed`、Control/0068 `120 passed`、Runtime 验收器 `21 passed`，Console `723 passed`、
  lint `225 files passed`，分组不相加冒充一次根级全量。
- `docs/prd/PRD-075-model-ab-testing-quality-evaluation.md`、`docs/api/experiments.md` 与
  `docs/testing/PRD-075-tests.md`：分别保存冻结设计及两轮自审、公开 API/错误/Webhook 合同和最终测试记录。
- `scripts/acceptance/prd075-runtime-acceptance.py`：在仓库内隔离 SQLite、固定回环 Control/Gateway/Console、
  两个 OpenAI-compatible Provider 和 Webhook 上验证 A/B 配额、Provider 失败、成本/用量、撤权/跨租户、
  Control 停机恢复、outbox、隐私和 owned-process 清理；还在空库执行 0068 fresh/repeat/down/re-up，
  不保存 Prompt/Response 或真实凭据。
- `.artifacts/prd075-runtime/prd075-final-20260810T011951Z/`：最终脱敏 Runtime JSON 与 SHA-256；四阶段迁移
  完整性均 ok、外键违规 0，服务、失败/成本追踪和停机恢复通过，outbox pending 0，145 个受控文件随机明文
  canary 命中 0，SQLite sidecar 0。
- `.artifacts/prd075-browser/prd075-final-20260809T234829Z/`：最终 Browser 主 JSON、独立 API helper JSON、
  11 张非空 PAGE-105 截图和 SHA-256；13 项 manifest 全匹配，双视口无横向溢出/重叠/文本溢出，UI 创建、
  幂等/冲突、新 owner、撤权清理与后端 403 均通过。

PRD-075 已通过设计、自审、实现、专项、必要回归、迁移、Runtime、Browser、中文注释和提交前安全门禁；
该结论只关闭本条，`all_108_prds_complete=false`。

## PRD-076 Response Cache 可选能力目录（已完成）

- `shared-sdk/python/metahub_shared/response_cache/`：冻结 tenant-qualified cache identity、策略/失效合同、
  HMAC key 与 AES-GCM envelope；敏感正文和密钥不进入 repr、日志或公开证据。
- `gateway-data-plane/src/metahub_gateway/response_cache/`：实现资格判定、generation fence、单写者 fill lease、
  加密 Entry、TTL/标签校验、hit settlement 和 crash-safe failure spool；`api/openai_compatible.py` 在完整安全
  闸门后接入，实验、stream、direct model、Policy route 与不可信 data class 明确旁路。
- `control-plane-api/src/metahub_control/response_cache/`：提供当前租户 Policy GET/PATCH、失效 API、
  后端独立 read/write/invalidate 权限、CAS/幂等、Audit、Webhook source/outbox 和 lifespan 恢复；跨租户与
  不存在保持同形，统一错误保留 request_id。
- `infra/migrations/0069_response_cache_optional_capability.sql/.down.sql`：新增 Policy、Key Generation、Entry、
  Fill、Tag、Request Fact、Hit、Invalidation、Event/Outbox 及 owner/不可变/down guard；保真扩展 Usage、
  Output Safety 与 Audit，RBAC catalog 提升到 revision 12。
- `console-web/src/features/cache-policies/`、`src/lib/responseCache.js` 与 RequestLog renderer：实现 PAGE-106
  配置、版本冲突显式 rebase、标签/全部失效、独立权限、owner/generation/Abort 清理及命中成本投影；
  desktop/mobile 均使用稳定网格和可达 Dialog actions。
- `scripts/maintenance/`：提供停机后 Response Cache key rotation 与 guarded down；先复核配置、spool、
  schema/trigger 和禁用状态，再以事务清理并执行迁移，不接受隐式在线破坏。
- `shared-sdk/python/tests/test_prd076_response_cache_contracts.py`、
  `gateway-data-plane/tests/**/*prd076*.py`、`control-plane-api/tests/**/*prd076*.py`、
  `scripts/maintenance/tests/test_response_cache_maintenance.py` 与
  `console-web/tests/unit/prd076ResponseCache.test.mjs`：覆盖合同、资格/隔离、并发、计费、权限、恢复、迁移、
  维护和 Console 生命周期；最终计数见测试记录。
- `docs/prd/PRD-076-response-cache-optional-capability.md`、`docs/api/response-cache.md` 与
  `docs/testing/PRD-076-tests.md`：分别保存冻结设计及两轮自审、公开 API/错误/Webhook 合同和最终测试记录。
- `scripts/acceptance/prd076-runtime-acceptance.py` 与
  `.artifacts/prd076-runtime/prd076-final-20260811-06/`：空 SQLite 往返迁移和真实回环服务验收；最终 JSON
  SHA-256 为 `2fcfe8579b40a6d13ed9addc09193074ac036bb9d8f80ed3b12ee3dd98ffe19d`，148 个文件明文命中 0。
- `scripts/acceptance/prd076-browser-acceptance.mjs` 与
  `.artifacts/prd076-browser/prd076-browser-final-20260811-24/`：保存脱敏 Browser JSON、9 张 PAGE-106
  截图和 manifest；双视口布局、UI mutation、动态撤权、RequestLog hit、隐私和 owned-process 清理通过。

Excel 总表、M2/M3 交付、PAGE-107、API、AssistantQuery/AssistantAudit、`assistant.queried` 和
TC-0153/0154 共同确认，下一条权威未完成项为
`PRD-077 | M2-企业增强 | P2 | 扩展层 | Admin AI助手 | 只读管理员AI助手`。本条不实现 PRD-077，
`all_108_prds_complete=false`。

## PRD-077 只读管理员 AI 助手目录（已完成）

- `shared-sdk/python/metahub_shared/admin_assistant/`：冻结四类有界自然语言意图、只读动作闸门、
  tenant-safe typed claim/source 图、内部 href allowlist 和确定性回答 renderer；Shared runtime 同时提供
  RBAC revision 13、统一错误目录、`assistant.queried` exact payload 与安全配置。
- `control-plane-api/src/metahub_control/admin_assistant/`：提供 `POST /admin-assistant/query`、严格 wire、
  tenant-first 来源 repository、原子 Query/Audit/AuditLog/event 持久化、source outbox 与 lifespan
  scheduler；`auth/`、`audit/`、`console/` 和 `webhooks/` 接入仅认证依赖、事务权限复核、安全摘要、
  PAGE-107 导航和提交后恢复。
- Control execution runtime 按 app/event loop 持有认证/来源 read-stage 与最终/终态 write-stage 两条长期
  SQLite WAL/FULL 连接，并以独立有限 admission、入口绝对 deadline、progress handler、shielded worker
  和 teardown rollback/close 关闭排队、取消、迟到提交与文件句柄泄漏；`last_seen` 与治理图同事务提交。
- `infra/migrations/0070_read_only_admin_ai_assistant.sql/.down.sql`：新增 tenant-qualified
  AssistantQuery、AssistantAudit、event source/outbox、AuditLog owner/immutable/down guard，并把 RBAC
  catalog 提升到 revision 13；默认角色扩展与自定义矩阵、0069 AuditLog rowid/sequence/schema 保真由
  迁移专项约束。
- `console-web/src/features/admin-assistant/`、`src/lib/adminAssistant.js`、`src/lib/healthRequest.js` 及
  Console shell/i18n/styles：实现 PAGE-107 问题输入、claim/source/redaction、四个可执行示例、
  owner/generation/Abort 清理、动态撤权、来源权限交集和 desktop/mobile 稳定布局；问题与回答不进入
  HTML 模板、Storage、日志或持久证据。
- `shared-sdk/python/tests/test_prd077_admin_assistant.py`、
  `control-plane-api/tests/integration/test_prd077_*.py`、
  `console-web/tests/unit/prd077AdminAssistant.test.mjs` 与 `scripts/acceptance/tests/test_prd077_*`：覆盖
  Shared/0070/outbox、Control、Runtime/Browser harness、PAGE-107 Console、权限、租户、deadline、并发、
  恢复、隐私和 owner cleanup；已知通过结果及重叠边界见测试记录，不能相加冒充根级全量。
- `docs/prd/PRD-077-read-only-admin-ai-assistant.md`、`docs/api/admin-assistant.md` 与
  `docs/testing/PRD-077-tests.md`：分别保存冻结设计及两轮自审、公开 API/错误/Webhook 合同和最终测试记录。
- `scripts/acceptance/prd077-runtime-acceptance.py`、`prd077-browser-harness.py` 与
  `prd077-browser-acceptance.mjs`：固定仓库内临时目录、loopback、source provenance、隐私扫描、原子证据
  发布和 owner-safe cleanup。
- `.artifacts/prd077-runtime/prd077-final-20260812-04/`：从空 SQLite 完成
  `70/0070 -> 70/0070 -> 69/0069 -> 70/0070`；四阶段完整性均为 ok、外键违规 0。真实 Control 覆盖
  四类查询、租户/RBAC、只读阻断、20 并发、来源故障及停服恢复，最终 outbox、owner process、sidecar
  与敏感明文残留为 0；主 JSON SHA-256 为
  `c4903b6c542051164d8ce9298b61581620423ac9782141e9d9a1340be9993f4b`。
- `.artifacts/prd077-browser/prd077-browser-final-20260812-05/`：保存脱敏 Browser/Runtime summary、桌面与
  移动清空态截图及 SHA-256 manifest；PAGE-107 的四类查询、来源权限、动态撤权、后端 403、来源防篡改、
  取消/迟到/登出和双视口布局通过。manifest 精确库存和全部摘要经独立重算匹配，截图已经人工复核。

PRD-077 的设计、实现、专项、必要回归、迁移、Runtime、Browser、中文注释和提交前安全门禁均已通过。
Excel 总表第 79 行、M2/M3 交付第 15 行、PAGE-108/109、审批 API、ApprovalTask、
`approval.created`/`approval.timeout` 与 TC-0155/0156 共同确认，PRD-077 之后的下一条权威未完成项为
`PRD-078 | M2-企业增强 | P2 | 扩展层 | 移动审批 | 移动端/企微钉钉审批入口`。本次不启动或混入
PRD-078；`all_108_prds_complete=false`。

## PRD-078 移动端/企微钉钉审批入口目录（已完成）

- `shared-sdk/python/metahub_shared/approvals/`：独立 versioned HMAC keyring、canonical bytes 与分域 digest；
  Shared runtime 同时登记 RBAC revision 14、审批统一错误、配置隔离和
  `approval.created`/`approval.timeout` exact payload。
- `infra/migrations/0071_mobile_enterprise_approval_entry.sql/.down.sql`：新增 tenant-qualified Task、Decision、
  step-up challenge/guard、writeback、event source/outbox 和 scheduler lease；迁移以 canonical schema、
  catalog、业务事实、订阅/delivery 与自定义 grant guard 保护原子 up/down。
- `control-plane-api/src/metahub_control/approvals/`：有界内部创建、严格 wire、列表/详情、密码 step-up、
  approve/reject、预算与会话风险固定 target adapter、数据库时钟 timeout、公平 tenant lease、Webhook source
  outbox 和 Redis writeback 恢复；认证依赖、Console 导航、Audit/Webhook owner 与 lifespan scheduler 完成接入。
- `console-web/src/lib/approvals.js` 与 `src/features/approvals/`：严格响应 schema、私有缓存 Header、PAGE-108/109
  列表/详情/移动卡片、倒计时、原生 Dialog、后端 step-up、owner/generation/Abort、动态撤权和敏感内存清理；
  主 app、shell、health、permissions、developer start 与样式完成有界装配。
- `shared-sdk/python/tests/test_prd078_approvals.py`、`control-plane-api/tests/**/*prd078*.py`、
  `console-web/tests/unit/prd078Approvals.test.mjs` 及登录/健康 owner 回归、
  `scripts/acceptance/tests/test_prd078_*`：覆盖合同、迁移、API、权限/租户、幂等/并发、step-up、timeout、
  outbox/writeback 恢复、Console 生命周期和证据安全；最终分组结果见测试记录，不能相加冒充根级全量。
- `docs/prd/PRD-078-mobile-enterprise-approval-entry.md`、`docs/api/approvals.md` 与
  `docs/testing/PRD-078-tests.md`：保存冻结设计、两轮编码前和两轮实现后自审、公开 API/错误/Webhook
  合同及最终门禁记录。
- `scripts/acceptance/prd078-runtime-acceptance.py` 与
  `.artifacts/prd078-runtime/prd078-runtime-20260814-08/`：从空 SQLite 完成
  `71/0071 -> 71/0071 -> 70/0070 -> 71/0071`，四阶段完整性 ok、外键违规 0；两类真实目标、三角色、
  step-up、幂等、timeout、Control 停服恢复、事件和 Redis 回写均通过，主 JSON SHA-256 为
  `5a3930e775654a3503241a464e37b206d3850db50c45c7b58b5f7fcf5cb43411`。
- `scripts/acceptance/prd078-browser-harness.py`、`prd078-browser-acceptance.mjs` 与
  `.artifacts/prd078-browser/prd078-browser-final-20260814-36/`：保存脱敏 Browser JSON、三张桌面/移动截图和
  SHA-256 manifest；标准/高风险审批、忙碌态、撤权、owner 漂移、迟到响应、登出、网络/Console、像素和
  布局门禁通过，截图已经人工复核。

PRD-078 的设计、实现、专项、必要回归、迁移、Runtime、Browser、中文注释和提交前安全门禁均已通过。
Excel 总表第 80 行、M2/M3 交付第 16 行、PAGE-110/111、套餐与额度 API、Plan/QuotaBucket、
`quota.exhausted`/`trial.expired` 与 TC-0157/0158 共同确认，下一条权威未完成项为
`PRD-079 | M2-企业增强 | P2 | 扩展层 | 套餐计费 | 套餐、试用与额度类型`。本次不启动 PRD-079，
`all_108_prds_complete=false`。

## PRD-079 套餐、试用与额度类型目录（已完成）

- `shared-sdk/python/metahub_shared/quota/`：冻结 CNY 微元、四类额度、ID、事件 payload、FinOps 分摊和
  Usage spool v7 合同；Shared runtime 同时登记 RBAC revision 15、套餐/额度统一错误和两类 Webhook。
- `infra/migrations/0072_plan_trial_quota_types.sql/.down.sql`：新增 tenant-qualified Plan、Bucket、
  Adjustment、Reservation/Item、Settlement/Allocation 与 Event/Outbox，全量复合 owner、守恒、CAS、
  append-only 和 down fail-closed guard 均在数据库层冻结。
- `control-plane-api/src/metahub_control/plan_billing/`：提供当前租户 Plan CAS、固定四桶查询、幂等
  credit/debit 调整、试用到期 scheduler、公平 outbox lease 和 Webhook 恢复；认证依赖、Audit、FinOps、
  Console 导航、Webhook 与 lifespan 已完成接入。
- `gateway-data-plane/src/metahub_gateway/quota/`：在 Provider 前按最坏 CNY 成本原子预留，按
  `trial/free/gift/paid` 顺序消费，并在 canonical Usage/FinOps 事务内结算；失败、outcome unknown、
  spool、Batch、A/B 和 Response Cache 都复用同一 quota owner 与恢复状态机。
- `console-web/src/lib/planBilling.js` 与 `src/features/plan-billing/`：PAGE-110/111 严格 wire、独立
  owner/generation/Abort、套餐草稿、四类报表、额度调整、原生 Dialog、busy Escape 捕获、动态撤权、
  迟到响应和登出清理；主 app、导航、i18n 和响应式样式完成有界装配。
- `shared-sdk/python/tests/test_prd079_quota_contracts.py`、`control-plane-api/tests/**/*prd079*.py`、
  `gateway-data-plane/tests/**/*prd079*.py`、`console-web/tests/unit/prd079PlanBilling.test.mjs` 与
  `scripts/acceptance/tests/test_prd079_runtime_acceptance.py`：覆盖合同、迁移、API、租户/RBAC、顺序、
  并发、账务、恢复、Console 生命周期和证据安全；最终分组见测试记录，不能相加冒充根级全量。
- `docs/prd/PRD-079-plan-trial-quota-types.md`、`docs/api/plan-billing.md`、`docs/api/finops-ledger.md` 与
  `docs/testing/PRD-079-tests.md`：保存冻结设计、四轮自审、公开 API/错误/Webhook/FinOps 合同及最终门禁。
- `scripts/acceptance/prd079-runtime-acceptance.py` 与
  `.artifacts/prd079-runtime/run-agent4-b72e1d/`：从空 SQLite 完成
  `72/0072 -> 72/0072 -> 71/0071 -> 72/0072`，四阶段完整性 ok、外键违规 0；真实 Control/Gateway/
  Provider、402 阻断、Usage/FinOps/Settlement、scheduler/outbox 恢复和证据隐私均通过，主 JSON
  SHA-256 为 `c09609cb3e03941721a4ea4e8c023136b543cec57b8d5efd9aa3ea649ec5ae5d`。
- `scripts/acceptance/prd079-browser-harness.py`、`prd079-browser-acceptance.mjs` 与
  `.artifacts/prd079-browser/main-browser-final-20260814-i/`：保存脱敏 Browser JSON、四张桌面/移动截图和
  SHA-256 manifest；Plan/Quota、Dialog、撤权、迟到响应、登出、网络/Console、像素、布局和进程清理
  门禁均通过，截图已经人工复核。

PRD-079 的设计、实现、专项、必要回归、迁移、Runtime、Browser、中文注释和提交前安全门禁均已通过。
Excel 总表第 81 行、M2/M3 交付第 17 行、PAGE-112、账单争议 API、BillingDispute/AdjustmentRecord、
`billing_dispute.created` 与 TC-0159/0160 共同确认，下一条权威未完成项为
`PRD-080 | M2-企业增强 | P2 | 扩展层 | 账单争议 | 账单争议与调整流程`。本次不启动 PRD-080，
`all_108_prds_complete=false`。

## PRD-080 账单争议与调整流程目录（已完成）

- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,i18n,webhooks}.py`：发布 RBAC revision 16、
  三项独立账单争议权限、统一错误和 `billing_dispute.created` exact payload/envelope 合同。
- `infra/migrations/0073_billing_dispute_adjustment_workflow.sql/.down.sql`：新增 tenant-qualified dispute、item、
  settlement hold、transition、Adjustment link、event/outbox 和 scheduler tenant 水位；复合 owner、append-only、
  revision CAS、金额守恒、公平序列与 down fail-closed guard 均由数据库约束。
- `control-plane-api/src/metahub_control/billing_disputes/`：提供严格列表/详情、request/time 创建、start review、
  部分 approve、reject/cancel、动态后端 RBAC、自审阻断、Audit、FinOps reversal、稳定 delivery ID outbox 与
  lifespan scheduler；`auth/`、`audit/`、`finops/`、`console/`、`webhooks/` 和应用 no-store 完成有界接入。
  专用路由还固定未知异常 503，整条路径生成服务端链路 ID，自由文本无法借 `User-Agent` 进入 Audit。
- `console-web/src/lib/billingDisputes.js` 与 `src/features/billing-disputes/`：PAGE-112 exact wire、完整身份 owner、
  generation/Abort、私有 reason vault、列表/详情、原生 Dialog、焦点环、busy Escape、动态撤权、迟到响应和
  登出清理；主 app、导航、i18n 与双视口样式完成装配。
- `shared-sdk/python/tests/test_prd080_billing_dispute_contracts.py`、`control-plane-api/tests/**/*prd080*.py`、
  `console-web/tests/unit/prd080*.test.mjs` 与 `scripts/acceptance/tests/test_prd080_*`：覆盖合同、迁移、API、
  tenant/RBAC、金额/并发、Audit/FinOps、outbox/scheduler、Console 生命周期、进程清理和证据隐私。
- `docs/prd/PRD-080-billing-dispute-adjustment-workflow.md`、`docs/api/billing-disputes.md` 与
  `docs/testing/PRD-080-tests.md`：保存冻结设计、四轮自审、公开 exact API、最终门禁和下一项交叉确认。
- `scripts/acceptance/prd080-runtime-acceptance.py` 与
  `.artifacts/prd080-runtime/prd080-runtime-final-20260815-05/`：从空 SQLite 完成
  `73/0073 -> 73/0073 -> 72/0072 -> 73/0073`，四阶段完整性 ok、外键违规 0；真实 Gateway/Control、
  request/time scope、部分冲正、失败追踪、Control 崩溃和稳定 delivery ID 恢复通过，主 JSON SHA-256 为
  `74174e1a88553644e08a9584e54e1e866206b943873630c86ecf09fe7619bfd7`。
- `scripts/acceptance/prd080-browser-harness.py`、`prd080-browser-acceptance.mjs` 与
  `.artifacts/prd080-browser/prd080-browser-final-20260815-05/`：保存脱敏 Browser JSON、四张 PAGE-112
  桌面/移动截图和 manifest；完整工作流、RBAC、自审、焦点、Escape、撤权、迟到、登出、网络、像素、布局、
  provenance 和 owned-process 清理通过，截图已经人工复核；Browser JSON 与 manifest SHA-256 分别为
  `a119250da461f04e966c3867f017084d6048eaf53d56877a0fcfef2a653e5193` 和
  `c0d53161ee0c1463c1cd98cd07d0fa58cd18f8a92b62faf42e71d2935bb12a99`。

PRD-080 的设计、实现、专项、必要回归、迁移、Runtime、Browser 和提交前安全门禁均已通过。Excel 总表
第 82 行、M2/M3 交付第 18 行、PAGE-113、Webhook format API、SecurityEventPayload、
`security.event_pushed` 与 TC-0161/0162 共同确认 PRD-081；PRD-081 现已按以下目录完成，
`all_108_prds_complete=false`。

## PRD-081 SIEM/安全事件格式目录（已完成）

- `shared-sdk/python/metahub_shared/runtime/security_events.py`：冻结 exact `SecurityEventPayload`、三类 source
  映射、5 秒窗口、JSON/CEF deterministic wire、CEF escaping/16 KiB 和 HMAC 实际字节合同。
- `shared-sdk/python/metahub_shared/runtime/{errors,i18n,fastapi_app,webhooks,webhook_delivery}.py`：登记
  统一错误、可订阅 `security.event_pushed`、Webhook 私有缓存边界及旧 JSON delivery 的 0074 兼容写入。
- `infra/migrations/0074_webhook_siem_security_event_format.sql/.down.sql`：扩展 Endpoint/delivery format、wire、
  rate，新增 tenant-qualified source、aggregate/member、outbox、rate claim 和 Provider decrypt anomaly；
  owner、append-only、CAS、generation、wire freeze 与 down guard 均由数据库冻结。
- `control-plane-api/src/metahub_control/security_events/`：持久 aggregate outbox、tenant 公平 lease scheduler、
  坏 source 隔离、过期 owner 接管与 lifespan 恢复。
- `control-plane-api/src/metahub_control/webhooks/{models,routes,wire,service,outbox}.py`：认证后 16 KiB exact
  创建 wire、JSON/CEF Endpoint、冻结 delivery/replay、分钟 rate gate、当前 Secret 刷新和安全响应 DTO。
- `gateway-data-plane/src/metahub_gateway/{providers,cache_affinity}/keypool.py` 与
  `api/openai_compatible.py`：在可信 request/trace 和调用方事务内记录不含 Credential 标识的 decrypt anomaly；
  Provider auth fact、RequestLog、Usage/FinOps 与失败追踪保持兼容。
- `console-web/src/lib/webhooks.js`、`src/features/webhooks/`、`src/app/main.js` 与 `src/styles/main.css`：
  PAGE-113 JSON/CEF segmented control、1..600 输入、即时订阅收敛、冻结元数据、权限/owner 清理和双视口
  安全换行。
- `shared-sdk/python/tests/test_prd081_security_event_contracts.py`、
  `control-plane-api/tests/integration/test_prd081_*.py`、受影响 Webhook/RequestLog/Gateway 测试、
  `console-web/tests/unit/{webhooks,prd081BrowserAcceptance}.test.mjs`：覆盖合同、迁移、API、tenant/RBAC、
  source、聚合、限流、replay、Gateway anomaly、Console 生命周期与证据清理。
- `scripts/acceptance/prd081-runtime-acceptance.py` 与
  `.artifacts/prd081-runtime/final-20260816t/`：空库完成
  `74/0074 -> 74/0074 -> 73/0073 -> 74/0074`，四阶段 integrity=ok、FK=0；三类真实 source、
  JSON/CEF、HMAC、聚合、限流、失败/费用追踪和 Control 重启恢复通过，Runtime JSON SHA-256 为
  `a67a3d330056a14e92aec86a752964d5228fcc28d72ddbb0e83a81c116cb124d`。
- `scripts/acceptance/prd081-browser-harness.py`、`prd081-browser-acceptance.mjs` 与
  `.artifacts/prd081-browser/final-20260816q/`：PAGE-113 在 `1280x720`/`390x844` 完成两个 Endpoint 与
  两次真实投递，RBAC、网络、Storage、SQLite、像素、溢出、重叠、provenance 和 owned-process 清理通过；
  Browser JSON/manifest SHA-256 分别为
  `55f131ad4f79d438fdc81720189e77a99f4da5cbf8480204cea38eb9a7e320dc`、
  `ea72a95cfd92e0bbc79720001d771027ea394d476585a13a09ee023e72c93740`。
- `docs/prd/PRD-081-siem-security-event-format.md`、`docs/api/webhook-siem.md` 与
  `docs/testing/PRD-081-tests.md`：保存冻结设计、四轮自审、公开 wire/API、最终门禁与下一项依据。

PRD-081 的实现、专项、必要回归、迁移、Runtime、Browser、文档和提交前安全门禁均已通过。Excel 总表
第 83 行、M2/M3 交付第 19 行、PAGE-114、API-372..377、McpServer/McpTool/McpInvocation、
`mcp.tool_called` 与 TC-0163/0164 共同确认 PRD-082；PRD-082 现已按以下目录完成，
`all_108_prds_complete=false`。

## PRD-082 MCP 工具注册与权限目录（已完成）

- `shared-sdk/python/metahub_shared/mcp/`：提供 MCP exact contracts、Draft 2020-12 Schema profile、
  canonical/effective Schema、分域 HMAC/AES-GCM、参数 DLP 投影、结果脱敏和 `mcp.tool_called` exact payload。
- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,i18n,webhooks}.py`：发布 RBAC revision 17、
  五项固定 MCP 权限、统一错误和事件订阅合同；动态 Tool 名不进入全局权限目录。
- `infra/migrations/0075_mcp_tool_registration_permissions.sql/.down.sql`：创建 tenant-qualified Server、
  Credential、Operation、SyncJob、Tool、role/user Grant、Invocation、Attempt、Approval 绑定、event/outbox 与
  scheduler 事实；owner、复合外键、append-only、CAS、lease、Audit 保真和 down guard 均由数据库冻结。
- `control-plane-api/src/metahub_control/mcp_governance/`：实现认证后 exact wire、Server 注册/刷新/停用/启用、
  Tool Schema/policy、异步 invoke/result、官方 SDK loopback executor、公平 scheduler、迟到 owner、事件 outbox
  和 lifespan 恢复。production executor 固定 `disabled`，不从 Control 或普通 worker 连接 tenant Endpoint。
- `control-plane-api/src/metahub_control/{approvals,auth,audit,console}/` 与应用装配：增加
  `mcp_tool_invocation` target、非自审高危审批、逐请求 RBAC、MCP Audit 白名单、PAGE-114 后端导航和
  `/mcp` 全路径 no-store/request_id；前端可见性不代替后端授权。
- `console-web/src/lib/mcpTools.js` 与 `src/features/mcp-tools/`：PAGE-114 Servers、Tools、Invocations 三 tab、
  原生注册 Dialog、按需 Schema Dialog、Tool policy、安全 Invocation/审批摘要和 owner/generation/Abort 生命周期；
  页面不保存或展示 Token、Endpoint、原始参数或结果正文。
- `shared-sdk/python/tests/test_prd082_mcp_shared.py`、`control-plane-api/tests/**/*prd082*.py`、
  `console-web/tests/unit/{mcpTools,prd082McpLifecycle,prd082BrowserAcceptance}.test.mjs` 与
  `scripts/acceptance/tests/test_prd082_*`：覆盖 Shared、0075、API、tenant/RBAC、审批、DLP/脱敏、executor、
  scheduler/outbox、Console 生命周期和 Runtime/Browser 证据合同；最终 Python 专项 `146/146`、Scheduler
  `30/30`、Console 全量 `846/846`、lint `261 files`。
- `scripts/acceptance/prd082-runtime-acceptance.py` 与
  `.artifacts/prd082-runtime/prd082-final-runtime-7/`：从空 SQLite 完成
  `75/0075 -> 75/0075 -> 74/0074 -> 75/0075`，四阶段 integrity=ok、FK=0；官方 SDK loopback、
  DLP block、返回脱敏、高危审批、已知失败、结果未知、费用/失败追踪和恢复通过，Runtime JSON SHA-256 为
  `15457991d5e1fb5c1b1acc55530d53acd68e74f9fb53bc5a7c3f3c0633b21c21`。
- `scripts/acceptance/prd082-browser-harness.py`、`prd082-browser-acceptance.mjs` 与
  `.artifacts/prd082-browser/prd082-page114-20260816-r8/`：PAGE-114 在 `1280x720`/`390x844` 完成三 tab、
  Server disable/enable、Schema Dialog 和移动注册 Dialog；四图已目视复核，外部请求、禁止 query、
  Console/Page error、全局横向 overflow、敏感明文和 owned-process 残留均为 0。Browser JSON/manifest
  SHA-256 分别为 `7c9a965af27968769f8294c14a569bb152e27998d4b083f300f51625511360eb`、
  `e387ef1cf76b53b2a886751899ab919b9ea5fd90ca44beb9ec74e953f4eb411c`。
- `docs/prd/PRD-082-mcp-tool-registration-permissions.md`、`docs/api/mcp-tools.md` 与
  `docs/testing/PRD-082-tests.md`：保存冻结设计、两轮编码前与两轮实现后自审、最终独立深审、公开
  API/错误/事件合同、最终门禁和下一项依据。

PRD-082 的实现、专项、迁移、Runtime、Browser 和文档均已形成正式证据；生产第三方 MCP 出网仍等待
PRD-083 的沙箱和网络 ACL。Excel 总表第 84 行、M2/M3 交付第 20 行、PAGE-115、API-378..382、
McpNetworkPolicy/OBJ-114、`mcp.network_blocked` 与 TC-0165/0166 共同确认，下一条权威未完成项为
`PRD-083 | M2-企业增强 | P2 | 扩展层 | MCP沙箱 | 第三方MCP沙箱网络ACL`。本次只登记、不实现 PRD-083，
`all_108_prds_complete=false`。

## PRD-083 第三方 MCP 沙箱网络 ACL 目录（已完成）

- `shared-sdk/python/metahub_shared/mcp/network.py` 与 `metahub_shared/mcp/__init__.py`：冻结 Network Policy
  caps/canonical fingerprint、Endpoint-only ACL、DNS 全地址判定、短时 envelope、boot-bound exact permit、
  加密 response、replay cache 和 EVT-095 安全 payload；permit 精确绑定 Tool、Credential、Schema、Tool Policy、
  network decision、durable call intent 与 pinned destination。
- `shared-sdk/python/metahub_shared/runtime/{config,mcp_sandbox_config,errors,i18n,webhooks}.py` 与 `rbac.py`：
  发布三角色闭集配置、独立最小 SandboxConfig、固定错误/可用性枚举、可订阅 `mcp.network_blocked` 和
  RBAC revision 18；未知或跨角色 `METAHUB_*` 失败关闭。
- `infra/migrations/0076_mcp_sandbox_network_acl.sql/.down.sql`：在 0075 drain preflight 后保真扩展 MCP
  operation/job/invocation/attempt，新增八张 tenant-qualified network policy/revision/decision/circuit/intent/
  event/outbox 表及 owner、append-only、CAS、事件正交和 down guard；空库与 seeded 往返均验证 rowid、值、
  integrity 和复合外键。
- `control-plane-api/src/metahub_control/mcp_governance/{models,routes,service,scheduler,sandbox_client,
  scheduler_main}.py`：实现后端双权限 Policy GET/PATCH、revision CAS、waiting-policy 恢复、独立 Scheduler、
  Worker 加密协议、实时 RBAC/Grant/Schema/Policy 重检、call intent、circuit、EVT-095 source/outbox、迟到 fence
  和固定失败/成本追踪。
- `mcp-sandbox-worker/` 与 `infra/docker/mcp-sandbox.Dockerfile`：独立无数据库 Worker、单次 DNS 全量校验、
  HTTPS pinned transport、Host/SNI 保留、proxy/redirect 禁止、阶段/overall deadline、加密 permit/response、
  session registry 和有界清理；不会把 Endpoint、Token、参数、结果或第三方异常正文写入日志/证据。
- `console-web/src/{lib,features/mcp-tools,app,styles}`：PAGE-115 Network policy 摘要与原生编辑 Dialog、后端
  capability/RBAC owner、409 手工重基、权限漂移清理、迟到响应拒绝和双视口安全布局；read-only 用户无编辑
  命令，前端隐藏不替代后端授权。
- `infra/compose/`、`infra/docker/requirements.mcp-sandbox.txt`、`.dockerignore` 与部署脚本/文档：增加独立
  Scheduler/Sandbox 服务、control/egress 网络、只读非 root 容器、cap drop、no-new-privileges、资源上限和
  专用 secret/config 边界；Sandbox 不挂载数据库、日志、Control 配置或宿主业务目录。
- `shared-sdk/python/tests/test_prd083_mcp_network.py`、`mcp-sandbox-worker/tests/`、
  `control-plane-api/tests/integration/test_prd083_*.py`、相邻 PRD-082 MCP 回归与
  `console-web/tests/unit/*prd083*.test.mjs`：覆盖 exact 合同、ACL/transport、配置隔离、permit/response、
  0076、API、Scheduler 竞态、熔断、事件、Console owner 和隐私边界。
- `scripts/acceptance/prd083-{runtime-acceptance.py,browser-harness.py,browser-acceptance.mjs}` 及合同测试：
  从空 SQLite 运行真实 Control、Scheduler、Sandbox 与隔离 MCP 测试服务，并对 PAGE-115 双视口、迁移、
  RBAC、409、动态撤权、隐私、证据 provenance、进程/端口/sidecar 清理执行失败关闭验收。
- `docs/prd/PRD-083-third-party-mcp-sandbox-network-acl.md`、`docs/api/mcp-tools.md`、
  `docs/deployment/single-node-compose.md` 与 `docs/testing/PRD-083-tests.md`：保存冻结设计、四轮自审、公开
  API/部署边界、最终门禁、证据 hash 和下一项交叉确认。

PRD-083 的实现、专项、迁移、Runtime、Browser 和文档已形成正式证据。权威 Runtime 为
`.artifacts/prd083-runtime/prd083-final-20260816-ac`，JSON SHA-256 为
`C5FFB99B15FF2156D26408B08C4F8ACD2C4EC07610A06799DDDFE471B23D0047`；权威 Browser 为
`.artifacts/prd083-browser/prd083-final-20260816-f`，JSON/manifest SHA-256 分别为
`30685F52892AE64838CCA4C32C1CAA044B974D311D8E0425016BFFDC8C61AE94`、
`0C59FEE846B45B54461DC00D7C86C2591F47EBF9B87248CB3850128D80830044`。Excel `01_PRD总表` 第 85 行、
`03_M2-M3扩展交付` 第 21 行、PAGE-116、API-383..388、OBJ-115、EVT-096 与 TC-0167/0168 共同确认，
交接后的实现项为 `PRD-084 | M2-企业增强 | P2 | 扩展层 | 本地模型 | 本地模型Endpoint接入`；该项现已按
以下目录完成，`all_108_prds_complete=false`。

## PRD-084 本地模型 Endpoint 接入目录（已完成）

- `shared-sdk/python/metahub_shared/runtime/{provider_adapter_spec,local_model_events,config,errors,
  error_diagnosis,i18n,webhooks,webhook_delivery}.py` 与 `rbac.py`：发布 Adapter v2 `local_endpoint`、
  EVT-096 安全事件、部署级私网 ACL/永久 metadata deny、有界超时、统一错误和 RBAC revision 19；默认上游
  仍为国内厂商官方/云服务及客户本地 OpenAI-compatible Endpoint，不把 OpenRouter/硅基流动列为默认上游。
- `infra/migrations/0077_local_model_endpoint_access.sql/.down.sql`：保真扩展 Provider、routing attempt call
  fence、LocalModelEndpoint/catalog/create receipt/call event/source outbox，安装 tenant owner、父子不可漂移、
  append-only、状态 CAS、事件正交和 down guard；fresh/repeat/down/re-up 均验证 integrity 与复合外键。
- `control-plane-api/src/metahub_control/local_endpoints/` 及 Provider/Health/Config Transfer 入口：实现
  `POST/GET /local-endpoints` 和 `POST /local-endpoints/{id}/test`，Provider -> initial Model -> Endpoint ->
  optional encrypted Credential -> receipt/Audit 原子创建；后端实时校验 tenant、read/manage/test 权限、DNS 全地址
  私网闭集、lease/generation 与目录上限，通用 Provider/Import 旁路失败关闭。
- `gateway-data-plane/src/metahub_gateway/providers/local_network.py`、`local_model_events.py`、
  `api/openai_compatible.py` 与 routing/provider adapter：在 DLP/预算/权限之后执行 pinning、Host/SNI、proxy/
  redirect/metadata deny 与 provider-call fence；vLLM/Ollama/OpenAI-compatible 的非流式、流式、失败、超时、
  fallback 和取消路径保持 request/attempt/usage/cost/event 一致，只有实际 `called` 才发布 EVT-096。
- `console-web/src/features/local-models/`、`src/lib/localEndpoints.js`、`src/app/main.js`、i18n 与样式：交付
  PAGE-116 独立导航、筛选、原生创建 Dialog、none/bearer、测试状态、owner/Abort、动态撤权与租户切换清理；
  URL/Token 不进入 Storage、query string、Console error 或正式证据，前端 capability 不替代后端授权。
- `scripts/acceptance/prd084-{runtime-acceptance.py,browser-harness.py,browser-acceptance.mjs}` 及对应合同测试：
  从空 SQLite 启动真实 Control/Gateway/Console、两个 OpenAI-compatible stub、Webhook receiver 和 Chromium，
  验证 0077、DLP/预算零上游、成功/失败/usage/cost/EVT-096、三角色、动态撤权、租户隔离、双视口、隐私与
  owner-safe 进程/端口/SQLite sidecar 清理。
- `shared-sdk/python/tests/test_prd084_shared_contracts.py`、`control-plane-api/tests/integration/test_prd084_*.py`、
  `gateway-data-plane/tests/unit/test_prd084_*.py`、`console-web/tests/unit/prd084*.test.mjs` 及 PRD-063/064
  相邻回归：覆盖 exact wire、迁移、API、tenant/RBAC、网络、取消、durable delivery、Adapter v2 和 PAGE-116。
- `docs/prd/PRD-084-local-model-endpoint-access.md`、`docs/api/local-model-endpoints.md`、
  `docs/api/provider-adapter-specs.md`、`docs/deployment/single-node-compose.md` 与
  `docs/testing/PRD-084-tests.md`：保存冻结设计、四轮自审、公开 API/部署边界、最终门禁、证据 hash 和下一项依据。

PRD-084 的实现、专项、必要回归、迁移、Runtime、Browser 和文档已形成正式证据。权威 Runtime JSON 为
`.artifacts/prd084-runtime/20260816T183036Z-c60eb988.json`，SHA-256 为
`F226D7665714C6AF335C72AE64E33392976337CE7A1E8850319384C8FFDBFDDC`；权威 Browser 为
`.artifacts/prd084-browser/final-20260817-01/`，JSON/manifest SHA-256 分别为
`F1C6EAF61B496956326CD56974058A9250FAF8AF20C23816F1F9EFF9A949507E`、
`9A20078068521D2C3E1AC33C838E7EDBBA5060F112617C5BABBA28FC66290F13`。Excel `01_PRD总表` 第 86 行、
`03_M2-M3扩展交付` 第 22 行、PAGE-117、API-389..391、OBJ-116、EVT-097 与 TC-0169/0170 共同确认，
下一条权威未完成项为
`PRD-085 | M2-企业增强 | P2 | 扩展层 | 用户行为埋点 | 产品使用埋点与激活漏斗`。本次只登记、不实现
PRD-085，`all_108_prds_complete=false`。

## PRD-085 产品使用埋点与激活漏斗目录（已完成）

- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,webhooks}.py`：发布 RBAC revision 20、
  `product_analytics.read`、固定产品分析错误及 EVT-097 可订阅事件合同。
- `infra/migrations/0078_product_analytics_activation_funnel.sql/.down.sql`：创建 tenant-qualified
  ProductEvent、source outbox 和来源投影，安装 owner、append-only、唯一来源、状态 CAS、订阅快照与 down
  guard；fresh/repeat/down/re-up 均验证 integrity 与复合外键。
- `control-plane-api/src/metahub_control/product_analytics/` 及 auth/app/console/webhook 装配：实现
  `GET /product-analytics/funnel`、七阶段整数基点聚合、实时后端 RBAC、tenant 隔离和可恢复 source outbox。
- `console-web/src/lib/productAnalytics.js`、`src/features/product-analytics/`、`src/app/main.js`、i18n 与样式：
  交付 PAGE-117 七阶段漏斗、初始态、刷新、owner/Abort、动态撤权、迟到响应丢弃、租户切换清理和双视口布局。
- `scripts/acceptance/prd085-{runtime-acceptance.py,browser-harness.py,browser-acceptance.mjs}` 与合同测试：
  从空 SQLite 启动真实 Control/Gateway/Console、本地上游、Webhook receiver 和 Chromium，验证 0078、七来源、
  Usage/成本/失败、outbox 恢复、双租户 `2/7` 对 `1/7`、动态撤权、当前 401/403、迟到 200/401、24 文件
  来源证明、隐私及 owner-safe 进程/端口/SQLite sidecar/隔离 Chrome 运行树分项清理。
- `shared-sdk/python/tests/test_prd085_product_analytics_contracts.py`、
  `control-plane-api/tests/integration/test_prd085_*.py`、相邻 Shared/Control/Gateway 回归和
  `console-web/tests/unit/{productAnalytics,prd085BrowserAcceptance}.test.mjs`：覆盖 exact wire、迁移、来源原子性、
  API、tenant/RBAC、乱序时间、终态失败、Console 生命周期和 Browser 证据合同。
- `docs/prd/PRD-085-product-analytics-activation-funnel.md`、`docs/api/product-analytics.md` 与
  `docs/testing/PRD-085-tests.md`：保存冻结设计、四轮自审、公开 API、最终门禁、证据 hash 和下一项依据。

PRD-085 的实现、专项、必要回归、迁移、Runtime、Browser 和文档已形成正式证据。权威 Runtime JSON 为
`.artifacts/prd085-runtime/prd085-runtime-20260817T055917Z-f41d66e4.json`，SHA-256 为
`9BC26BCD9DC04A18B9628BE5A689FE9492EE2E4ED99F6A623DFFE36397EE61D6`；权威 Browser 为
`.artifacts/prd085-browser/prd085-browser-20260817T073524Z-final-r2/`，JSON/manifest SHA-256 分别为
`2BA160C3B7351FF1B9771AC1D2BD1BBA9BCFB06B90E94CAA3463484B92A881F9`、
`BC51513B7ED3B20AF628121C1D49ADCC7A5FEDB6D4A04156875D113591BE49A1`。Excel `01_PRD总表` 第 87 行、
`03_M2-M3扩展交付` 第 23 行、PAGE-118、API-392/393、OBJ-117、EVT-098 与 TC-0171/0172 共同确认，
下一条权威未完成项为 `PRD-086 | M2-企业增强 | P2 | 扩展层 | 客户健康 | 客户健康评分`。本次只登记、
不实现 PRD-086，`all_108_prds_complete=false`。

## PRD-086 客户健康评分目录（已完成）

- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,webhooks}.py`：发布 RBAC revision 21、
  `customer_health.read`、客户健康固定错误和 EVT-098 exact 可订阅事件合同。
- `infra/migrations/0079_customer_health_score.sql/.down.sql`：创建 tenant-qualified append-only 评分、source
  outbox 和 Scheduler tenant 游标，安装 owner、safe-count、公式/hash、CAS/lease、changed-only 事件及 down guard。
- `control-plane-api/src/metahub_control/customer_health/` 及 auth/app/console/webhook 装配：实现纯读
  `GET /customer-health`、五类权威信号、工单 unavailable、整数最大余数、风险下限、每小时 Scheduler 和可恢复 outbox；
  idle 轮询先只读探测，不占用 SQLite writer。
- `console-web/src/lib/customerHealth.js`、`src/features/customer-health/`、主壳、i18n 与样式：交付 PAGE-118
  健康工作台、六信号贡献、风险标签、建议动作、激活上下文、strict owner/ticket/generation、动态撤权、迟到响应丢弃和
  390/1280/1440 响应布局。
- `shared-sdk/python/tests/test_prd086_customer_health_contracts.py`、
  `control-plane-api/tests/integration/test_prd086_{customer_health,db_migrations}.py` 与
  `console-web/tests/unit/{customerHealth,prd086BrowserAcceptance}.test.mjs`：覆盖 exact wire、0079、公式、租户/RBAC、
  纯读 GET、Scheduler/outbox 恢复、idle writer 保护、Console 生命周期和视觉合同。
- `.artifacts/prd086-runtime/prd086-live-20260817-{05,06}/`：空 SQLite 往返、真实 lifespan Scheduler、
  API 200/401/403/422/503、三租户纯读隔离及 healthy/high-risk/no-samples/tenant-isolation 四张截图。
- `docs/prd/PRD-086-customer-health-score.md`、`docs/api/customer-health.md` 与
  `docs/testing/PRD-086-tests.md`：保存冻结设计、两轮编码前自审、公开 API、最终门禁、证据 hash 和下一项依据。

PRD-086 的实现、专项、迁移、真实 Runtime/API、Browser 和文档已形成证据。Excel `01_PRD总表` 第 88 行、
`03_M2-M3扩展交付` 第 24 行、PAGE-119、API-394..399、OBJ-118..120、EVT-099 与 TC-0173/0174 共同确认，
下一条权威未完成项为
`PRD-087 | M2-企业增强 | P2 | 扩展层 | 伙伴门户 | 渠道伙伴与白标门户`。本次只登记、不实现 PRD-087，
`all_108_prds_complete=false`。

## PRD-087 渠道伙伴与白标门户目录（已完成）

- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,webhooks}.py`：发布 RBAC revision 22、六组伙伴
  权限、固定伙伴错误和 EVT-099 exact 可订阅事件合同。
- `infra/migrations/0080_channel_partner_white_label_portal.sql/.down.sql`：创建 tenant-qualified Partner、
  PartnerCustomer、CommissionRecord、分润游标、source outbox 与安全操作链，安装 owner、append-only、
  idempotency/payload hash、CAS/lease/cutoff、exact event、权限矩阵和 down guard。
- `control-plane-api/src/metahub_control/partner_portal/` 及 auth/app/console/webhook 装配：实现 Partner 配置、
  白标、Project 客户映射、历史用量、映射后分润、CSV、交付包、两阶段 Scheduler 和可恢复 EVT-099 outbox；
  所有 API 从 Session 恢复 tenant/actor 并由后端实时校验 RBAC。
- `console-web/src/lib/partnerPortal.js`、`src/features/partner-portal/`、主壳、i18n 与样式：交付 PAGE-119
  初始化/就绪工作台、客户映射、历史用量、分润、CSV/交付、根节点品牌变量、owner/ticket/generation、动态撤权、
  迟到响应丢弃、租户切换清理和 390/1280/1440 响应布局。
- `scripts/acceptance/prd087-{browser-harness.py,browser-acceptance.mjs}` 与合同测试：从空 SQLite 启动真实
  Control/Console/Chromium，验证 0080 往返、API、双租户、动态撤权、未授权零预取、附件、隐私、截图 hash 以及
  owner-safe 进程、端口和 SQLite sidecar 清理。
- `shared-sdk/python/tests/test_prd087_partner_portal_contracts.py`、
  `control-plane-api/tests/integration/test_prd087_{partner_portal,db_migrations}.py` 与
  `console-web/tests/unit/{partnerPortal,prd087BrowserAcceptance,prd087BrowserAcceptanceDriver}.test.mjs`：覆盖 exact
  wire、0080、租户/RBAC、幂等/CAS、分润并发/接管/cutoff、EVT-099 恢复、附件、生命周期和视觉合同。
- `.artifacts/prd087-browser/page119-final-20260817-r17/`：保存最终 `browser-acceptance.json`、SHA-256 manifest
  与 setup、ready、commission、tenant-isolation 四张已逐图复核的 PNG。
- `docs/prd/PRD-087-channel-partner-white-label-portal.md`、`docs/api/partner-portal.md` 与
  `docs/testing/PRD-087-tests.md`：保存冻结设计、两轮编码前自审、公开 API、最终门禁、证据 hash 和下一项依据。

PRD-087 的实现、迁移、专项、必要回归和真实 Browser 已形成正式证据。其完成时，Excel `01_PRD总表` 第 89 行、
`03_M2-M3扩展交付` 第 25 行、PAGE-120、API-400/401、OBJ-121、EVT-100 与 TC-0175/0176 共同确认后续
PRD-088；该历史登记随后已由下方 PRD-088 目录完成项关闭，`all_108_prds_complete=false`。

## PRD-088 客户/渠道白标配置目录（已完成）

- `shared-sdk/python/metahub_shared/rbac.py` 与 `runtime/{errors,webhooks}.py`：发布 RBAC revision 23、
  `branding.read/manage`、固定 Branding 错误和 EVT-100 exact 事件合同。
- `infra/migrations/0081_tenant_white_label_branding.sql/.down.sql`：创建 tenant-qualified BrandingConfig、
  immutable source event/outbox，安装 owner、PNG/域名、revision CAS、lease/generation、RBAC 和 down guard。
- `control-plane-api/src/metahub_control/branding/` 及 audit/auth/app/console 装配：实现受保护/公共投影、
  安全 PNG、域名预留、后端实时权限、原子审计/event/outbox 和可停止恢复 worker。
- `console-web/src/lib/branding.js`、`src/features/branding/`、登录页、主壳、developer-start 和样式：交付
  PAGE-120、公共登录品牌、认证后 shell、管理员 CAS 保存、viewer 只读、owner/generation、动态撤权和
  Blob URL/CSS/title/tenant 切换清理。
- `control-plane-api/tests/integration/test_prd088_{branding,db_migrations}.py` 与
  `console-web/tests/unit/branding.test.mjs`：覆盖 exact wire、0081 往返、租户/RBAC/导航、PNG、审计、
  EVT-100 恢复、公共投影、迟到响应和页面生命周期。
- `docs/prd/PRD-088-tenant-white-label-branding.md`、`docs/api/branding.md` 与
  `docs/testing/PRD-088-tests.md`：保存冻结设计、两轮编码前自审、公开 API、最终门禁和下一项依据。

PRD-088 的实现、迁移、专项、必要回归、真实 HTTP 和三视口 Browser 已形成正式记录。Excel `01_PRD总表`
第 90 行、`03_M2-M3扩展交付` 第 26 行、PAGE-121、API-402..406、OBJ-122/123、EVT-101/102 与
TC-0177/0178 共同确认过 PRD-089；该历史登记已由上方 PRD-089 数据生命周期边界关闭。当前下一条
新 SaaS 路线图的 PRD-090..096 已完成，权威未完成项已切换到 PRD-097，`saas_incremental_roadmap_complete=false`；
兼容旧口径继续保持 `all_108_prds_complete=false`。

## PRD-090 起 SaaS 增量规划目录

- `企业级AI_Gateway_PRD开发总清单_给CodexClaude.xlsx`：保留 PRD-001..089 完成事实和旧 PRD-090..108
  历史内容；旧 090..108 已废弃、禁止开发。
- `企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx`：PRD-090 起的唯一详细计划，当前基线至
  PRD-148；这是当前已细化范围，不设置需求总数或最终编号上限。角色页面矩阵明确平台员工与企业职能用户的页面、动作、禁止范围和分配主体；`11_现有导航处置`把源码64项tenant Console导航逐项绑定到API/表/Scheduler、目标域、角色、承接PRD和切换门禁。
- `docs/planning/MetaHub-SaaS-PRD-090-onward.md`：保存权威性、产品/云边/权限边界、阶段路线、当前详细总表和
  执行纪律。
- `scripts/planning/generate_saas_prd_plan.py`：可重复生成并校验工作簿，检查从 PRD-090 起的开放式连续编号、
  完成状态前缀、旧计划废弃映射、12个Sheet、64项导航处置和两个总体未完成状态标记。
- `scripts/planning/validate_saas_roadmap.py`：失败关闭验证开放式编号、依赖DAG、精确角色、对象域、旧计划退役、
  源码64项导航一一处置、两轮自审、安全合同、Excel安全性、0085边界和下一未完成项。
- `scripts/planning/retire_legacy_prd090_onward.py`：仅修改旧总清单的两个活动状态指针，保留旧090..108明细供审计。
- `docs/architecture/saas-target-architecture.md`、`docs/security/saas-threat-model.md`、
  `docs/prd/PRD-090-saas-architecture-migration-baseline.md`、`docs/testing/PRD-090-tests.md`：PRD-090冻结架构、
  威胁、迁移、自审和实际验收记录。

新 `PRD-090 - SaaS总体架构与兼容迁移基线` 至
`PRD-122 - 私有网关遥测、用量与失败事实同步` 已完成；当前下一项是
`PRD-123 - 私有网关本地Secret托管与轮换`。
`saas_incremental_roadmap_complete=false`；兼容旧口径继续保持 `all_108_prds_complete=false`。

## PRD-091 全局账号目录

- `control-plane-api/src/metahub_control/identity/`：全局账号、Opaque Cookie Session、CSRF、本人设备撤销和密码轮换；不恢复 tenant/role/permission。
- `console-web/identity.html` 与 `console-web/src/features/identity/`：独立账号安全入口，不写浏览器 Storage，不取代旧企业 Console 登录。
- `infra/migrations/0083_global_account_identity.sql`：expand 回填、冲突隔离、兼容触发器、事件/Outbox 和安全事实回滚门禁。
- `docs/prd/PRD-091-global-account-and-credentials.md`、`docs/api/global-identity.md` 与 `docs/testing/PRD-091-tests.md`：冻结设计、公开合同和最终验收记录。

## PRD-092 自助注册验证目录

- `control-plane-api/src/metahub_control/identity/{self_service.py,delivery.py}`：tenant-free 注册、邮箱验证、
  generation 重发、密码找回、持久滥用门禁、Invitation verified binding 和固定投递失败边界。
- `shared-sdk/python/metahub_shared/identity/crypto.py`：RegistrationIntent owner-bound AES-256-GCM
  邮箱信封；AAD 绑定 purpose、intent、字段类型和 key version，失败统一为 `identity.unavailable`。
- `console-web/src/features/identity/` 与 `console-web/src/lib/identity.js`：注册、验证和找回视图；
  intent/code/password 只在内存与当前输入生命周期存在，不写 URL 或 Storage。
- `infra/migrations/0084_identity_self_service.sql`：全局 intent/challenge/verification/consent/abuse/
  binding/event/outbox 表，以及任何安全事实存在时的 down guard。
- `docs/prd/PRD-092-self-service-registration-and-verification.md`、`docs/api/global-identity.md` 与
  `docs/testing/PRD-092-tests.md`：冻结设计、公开合同、测试与独立敏感审计记录。

## PRD-093 Workspace 创建目录

- `control-plane-api/src/metahub_control/workspaces/`：严格 GlobalSession/CSRF/Origin/幂等边界，原子创建、
  owner foundation 列表/详情和 profile CAS；不签发 WorkspaceSession，不接受客户端 owner 标识。
- `console-web/src/lib/workspaces.js` 与 `console-web/src/features/identity/`：个人/企业 Workspace 创建和
  安全摘要列表；草稿与原始幂等 key 只在内存生命周期存在，内部 owner/tenant/session 标识不进 DOM 或 Storage。
- `infra/migrations/0085_workspace_creation.sql`：Workspace、不可变 Tenant binding、owner foundation、
  Region/profile/provisioning/environment origin、event/outbox、一次性 legacy User 投影证明和确定性 Tenant 回填。
- `docs/prd/PRD-093-workspace-creation.md`、`docs/api/global-identity.md` 与
  `docs/testing/PRD-093-tests.md`：冻结设计、公开合同、迁移/性能/浏览器门禁和独立敏感审计记录。

## PRD-094 Workspace 授权目录（已完成）

- `control-plane-api/src/metahub_control/workspace_access/`：七个 Global/Workspace 路由、独立 Cookie
  解释、数据库权威 revision 复核、成员委派、邀请升级和 Owner 转移 CAS；WorkspaceSession 与
  Legacy Bearer 混用失败关闭。
- `control-plane-api/src/metahub_control/{auth,console,identity,invitations,workspaces}/`：装配
  WorkspaceSession tenant auth、Workspace 导航投影、GlobalSession 联动撤权、PRD-092 verified binding
  消费和 PRD-060 N/N-1 邀请桥。
- `console-web/src/lib/workspaces.js` 与 `console-web/src/features/identity/`：紧凑可搜索 Workspace
  选择器、成员角色/状态、邀请和 Owner 转移；BroadcastChannel 仅发匿名 generation，Abort/fence
  丢弃迟到响应，邀请 token 不进 DOM，浏览器 Storage 零调用。
- `infra/migrations/0086_membership_workspace_sessions.sql/.down.sql`：完整 Membership、WorkspaceSession、
  MembershipInvitation、OwnerTransfer、MembershipEvent/Outbox、legacy link 回填、动态撤权 trigger 和
  授权事实 down guard；新 legacy User 回填不能替换 immutable Workspace creator。
- `shared-sdk/python/tests/test_prd094_workspace_session_config.py`、
  `control-plane-api/tests/integration/test_prd094_{db_migrations,workspace_access}.py` 与
  `console-web/tests/unit/prd094WorkspaceAccess.test.mjs`：覆盖独立根密钥、0086 往返、九角色委派、
  七路由、100 并发、warm p95、敏感 canary、跨标签页和 Storage/DOM 隐私。
- `docs/prd/PRD-094-membership-and-workspace-switching.md`、`docs/api/workspace-access.md` 与
  `docs/testing/PRD-094-tests.md`：保存冻结设计、两轮自审、公开合同、自动化与真实 Browser 证据。
  390/1280/1440 三视口、键盘流程、Owner 转移显式确认、Console 零错误和切换 p75 `805.8 ms` 均通过；
  开发启动器 access-log canary 证明邀请 token 不进入原始请求日志。下一权威项为 PRD-095。

## PRD-095 Platform IAM 与受控支持目录（已完成）

- `shared-sdk/python/metahub_shared/identity/` 与 `control-plane-api/src/metahub_control/identity/`：
  owner-bound AES-256-GCM TOTP、counter 防重放、持久尝试门禁、enrollment/activation 和 active factor
  权威投影；不修改旧 `mfa_state` CHECK，也不提供客服 MFA 绕过。
- `control-plane-api/src/metahub_control/platform_access/`：一次性 owner bootstrap、13 角色目录、
  PlatformSession 动态鉴权、Operator CAS、双域 SupportGrant、短 SupportSession、诊断白名单、
  break-glass 双复核，以及会话/角色绑定的 HMAC keyset 审计游标。
- `console-web/platform.html`、`console-web/src/features/platform/`、`src/lib/platform.js` 与
  `src/styles/platform.css`：独立平台工作台、后端权限导航、Operator/Support/Audit 流程、页面内确认、
  敏感输入清理和 390/1280/1440 响应布局；不复用企业 Console DOM 或 Storage。
- `console-web/src/features/identity/` 与 `src/lib/identity.js`：账号安全页 TOTP enrollment/activation，
  以及只由 `GET /identity/platform-access` 控制的“进入平台工作台”入口。
- `infra/migrations/0087_platform_iam_controlled_support.sql/.down.sql`：MFA、bootstrap、Platform IAM、
  Platform/Support Session、grant/approval、break-glass review、不可变审计/outbox、固定目录和业务事实
  down guard。
- `shared-sdk/python/tests/test_prd095_platform_security.py`、
  `control-plane-api/tests/integration/test_prd095_{db_migrations,platform_iam}.py` 与
  `console-web/tests/unit/prd095PlatformAccess.test.mjs`：覆盖密钥域、0087 往返、MFA/bootstrap、凭据
  负矩阵、双审批、100 并发、warm p95、签名游标、Support 裁剪、敏感审计和前端生命周期。
- `docs/prd/PRD-095-platform-iam-controlled-support.md`、`docs/api/platform-access.md` 与
  `docs/testing/PRD-095-tests.md`：保存两轮设计自审、公开合同、自动化、真实 Browser 和独立敏感
  审计证据。该历史下一项已由下方 PRD-096 完成项关闭。

## PRD-096 Platform Workspace 目录与运营（已完成）

- `control-plane-api/src/metahub_control/platform_access/workspace_{models,service}.py` 与 `routes.py`：
  提供角色裁剪目录、exact ID/slug 搜索、Session/角色/Operator revision/筛选绑定的 HMAC 游标、详情、
  health 和事件时间线，以及 suspend/restore、风险标签和 Owner 通知的 CAS/幂等写路径。
- `control-plane-api/src/metahub_control/{workspace_access,workspaces}/service.py` 与共享 seed：在 0088
  schema 存在时叠加 operational state；冻结、删除或冲突状态拒绝 WorkspaceSession 和身份目录恢复，
  新 Workspace 显式初始化 state，兼容 N/N-1 schema。
- `infra/migrations/0088_platform_workspace_operations.sql/.down.sql`：建立 operational state、risk tag、
  health projection、action intent、Owner notification outbox、operational event/outbox 和 31 个 PRD-096
  对象；数据库 guard 使旧应用在 inactive Workspace 上失败关闭，非 pristine down 原子阻断。
- `console-web/src/lib/platform.js`、`src/features/platform/` 与 `src/styles/platform.css`：交付紧凑目录、
  状态/Region/风险/健康筛选、游标分页、全宽详情、健康/动作/事件时间线、页面内生命周期确认、风险与
  通知操作，以及写后 generation + abort 重载；390px 使用字段标签堆叠行，宽屏保持高密度表格。
- `control-plane-api/tests/integration/test_prd096_{platform_workspaces,db_migrations}.py` 与
  `console-web/tests/unit/prd096PlatformWorkspaces.test.mjs`：覆盖 0087->0088、pristine/non-pristine down、
  五角色字段裁剪、游标篡改/跨角色、health fresh/stale/坏源、风险职责隔离、生命周期并发、通知无邮箱、
  迟到响应和移动布局；PRD-093/094/095 六文件兼容组和 Console 全量同步通过。
- `docs/prd/PRD-096-platform-workspace-directory-operations.md`、`docs/api/platform-access.md` 与
  `docs/testing/PRD-096-tests.md`：保存冻结设计、两轮自审、公开合同、完整门禁和敏感审计。下一权威项
  为 PRD-097。

## PRD-114 企业域名与 SSO 目录（已完成）

- `control-plane-api/src/metahub_control/enterprise_identity/`：企业域名、OIDC/SAML connection、证书、
  登录发现与回调、协议测试、强制策略和 break-glass recovery；逐请求恢复 WorkspaceSession 与权限。
- `infra/migrations/0106_enterprise_domain_sso.sql/.down.sql`：十张企业身份表、复合 owner、全局域名唯一、
  state/replay、事件/outbox、不变式触发器和业务事实 down guard。
- `shared-sdk/python/metahub_shared/enterprise_identity.py` 与 `metahub_shared/rbac.py`：企业身份严格合同、
  权限矩阵和配置错误边界。
- `console-web/src/lib/enterpriseIdentity.js`、`src/features/identity/` 与 `src/styles/identity.css`：Owner 管理、
  auditor 只读、developer 隐藏、OIDC/SAML 表单、一次性 DNS challenge 和提交后敏感输入清理。
- `docs/prd/PRD-114-enterprise-domain-sso-login-discovery.md` 与 `docs/testing/PRD-114-tests.md`：冻结设计、
  自动化、本地认证、三角色 Browser、九组响应式与敏感扫描记录。其历史下一项已由下方 PRD-115 完成项关闭；
  S4 G4 保留到 PRD-119 独立提交后统一执行。

## PRD-115 SCIM 目录与成员生命周期（已完成）

- `control-plane-api/src/metahub_control/enterprise_identity/scim_*.py`：Workspace 管理面、Bearer 恢复 owner 的
  SCIM Users/Groups、受限 filter/PATCH、token 轮换、role mapping、Membership 投影和冲突 CAS。
- `infra/migrations/0107_scim_directory_membership_lifecycle.sql/.down.sql`：SCIM token/link/group/mapping/run/
  conflict/command/outbox 表、复合 owner、状态机、目录角色约束、权限矩阵和业务事实 down guard。
- `shared-sdk/python/metahub_shared/scim_directory.py`、`rbac.py` 与 `runtime/errors.py`：SCIM 规范化、filter/
  version parser、域分离 HMAC、保守角色合并、权限和固定错误合同。
- `console-web/src/lib/scimDirectory.js`、`src/features/identity/` 与 `src/styles/identity.css`：Owner 管理、auditor
  只读、developer 隐藏、一次性 Bearer 清理、目录摘要、角色映射和三动作冲突恢复。
- `docs/prd/PRD-115-scim-directory-membership-lifecycle.md` 与 `docs/testing/PRD-115-tests.md`：冻结设计、
  自动化、本地认证、冲突恢复、三视口 Browser 与敏感扫描记录。下一权威项为 PRD-116；S4 G4 保留到
  PRD-119 独立提交后统一执行。

## PRD-116 MFA、条件访问、设备与会话安全（已完成）

- `infra/migrations/0108_mfa_conditional_access_device_session_security.sql/.down.sql`：多因子、WebAuthn、
  恢复挑战与恢复码、受信设备、条件访问、step-up、Session assurance/revoke generation 和 down guard。
- `control-plane-api/src/metahub_control/identity/` 与 `workspace_access/conditional_access.py`：统一 enrollment、
  WebAuthn、受控延迟恢复、设备/Session 撤销、条件访问 simulation/CAS 和一次性 grant 消费。
- `shared-sdk/python/metahub_shared/`：PRD-116 权限、稳定错误码和身份安全公共合同。
- `console-web/src/lib/{identity,conditionalAccess}.js`、`src/features/identity/` 与 `src/styles/identity.css`：
  身份安全、条件访问、一次性证明清理、恢复后 Workspace 显式重入和三档响应式布局。
- `docs/prd/PRD-116-mfa-conditional-access-device-session-security.md` 与 `docs/testing/PRD-116-tests.md`：
  冻结设计、两轮自审、专项/必要回归、本地认证、受控恢复、三视口 Browser 与敏感扫描记录。其历史下一项
  已由 PRD-117..119 关闭；S4 G4 在 PRD-119 独立提交后统一执行。

## PRD-117 数据驻留、保留与法律保全目录（已完成）

- `control-plane-api/src/metahub_control/data_governance/` 与消费者适配：版本化驻留/保留策略、Legal Hold、
  删除调度、低敏证据和路由/备份/DR/job/tenant deletion 失败关闭。
- `infra/migrations/0109_data_residency_retention_legal_hold.sql/.down.sql`：分类、策略、hold、删除计划、
  evidence、事件/outbox、权限与业务事实 down guard。
- Workspace/Platform Console：策略 simulation/confirmed apply、Hold 生命周期和驻留证据筛选；移动端单列布局。
- `docs/prd/PRD-117-data-residency-retention-legal-hold.md` 与 `docs/testing/PRD-117-tests.md`：保存专项、
  必要回归、性能、本地认证、Browser 和敏感审计事实。

## PRD-118 机器身份目录（已完成）

- `control-plane-api/src/metahub_control/machine_identity/`：Service Account、OAuth client、短期 opaque token、
  machine-safe scope、IP/mTLS 约束、轮换/撤销、幂等命令和机器审计。
- `infra/migrations/0110_service_account_oauth_machine_identity.sql/.down.sql`：机器主体、credential/token grant、
  route scope、事件/outbox、复合 owner 和业务事实 down guard。
- Identity Console：机器身份列表、一次性 client secret、关闭/导航/错误清理和响应式表单。
- `docs/prd/PRD-118-service-account-oauth-machine-identity.md` 与 `docs/testing/PRD-118-tests.md`：保存迁移、
  API/性能、必要回归、本地 Owner、Browser 和敏感审计事实。

## PRD-119 官方 Provider 目录（已完成）

- `control-plane-api/src/metahub_control/provider_catalog/`：签名目录读取、Workspace 实例化/兼容说明/preflight、
  Platform canonical manifest 发布、动态权限和企业/平台凭据域隔离。
- `infra/migrations/0111_official_provider_catalog_regional_compliance.sql/.down.sql`：可信公钥、不可变版本、
  entry/capability/region/compatibility、实例/preflight、事件/outbox、权限扩展和 down guard。
- `console-web/src/lib/providerCatalog.js`、Provider Catalog 视图与 Platform 发布视图：四维筛选、凭据后续动作、
  preflight、签名输入清理、发布/安全投影分权和移动端布局。
- `docs/prd/PRD-119-official-provider-catalog-regional-compliance.md` 与 `docs/testing/PRD-119-tests.md`：
  保存两轮自审、专项/必要回归、真实 API/Browser、性能和敏感审计；下一权威项为 PRD-120，S4 G4 在
  PRD-119 独立提交后统一执行。

## PRD-120 Edge Agent 设备身份与 mTLS（已完成）

- `control-plane-api/src/metahub_control/edge_management/`：Workspace Deployment 注册意图、Agent CSR
  注册、设备证书轮换、短时 EdgeSession、吊销和 Platform Fleet 脱敏投影；三个凭据域失败关闭。
- `shared-sdk/python/metahub_shared/private_gateway/`：严格 Ed25519 CSR、registration proof、nonce、Deployment
  owner 和证书摘要合同，Secret-aware 对象禁止 `repr`。
- `infra/migrations/0112_edge_agent_device_identity_mtls.sql/.down.sql`：Edge Deployment、注册意图、设备
  identity/certificate/session、事件/outbox、不可变权限扩展和业务事实 down guard。
- `console-web/src/features/private-gateways/`、`console-web/src/lib/edgeManagement.js` 与 Platform Fleet：
  Workspace 一次性注册材料只驻留内存，Platform 只读取安全 Fleet 字段，直达 hash 与离页清理已验证。
- `docs/prd/PRD-120-edge-agent-device-identity-mtls.md` 与 `docs/testing/PRD-120-tests.md`：保存迁移、API、
  mTLS、Browser、SLO 和敏感审计事实；下一权威项为 PRD-121，G5 在 PRD-130 后统一执行。

## PRD-121 云边期望状态、签名快照与离线 LKG（已完成）

- `control-plane-api/src/metahub_control/edge_management/desired_state_service.py` 与同域 models/routes/service：
  Desired State CAS、服务端风险派生、不同管理员审批、RFC 8785 + Ed25519 制品、trust manifest、Agent 拉取、
  单调 ACK/LKG、递增 generation 回滚和 Platform Fleet 最小投影；Cookie/Agent/Platform 三凭据域失败关闭。
- `shared-sdk/python/metahub_shared/private_gateway/desired_state.py`：严格 envelope/artifact/trust schema、
  duplicate key/non-finite/超限数字拒绝、用途域签名、owner/audience/TTL 校验和固定失败码。
- `gateway-data-plane/src/metahub_gateway/config/edge_desired_state.py`：候选 slot、高水位、active pointer 与
  父目录固定 fsync 顺序，跨重启单调恢复、有界 LKG 和完整性故障隔离。
- `infra/migrations/0113_edge_desired_state_signed_snapshot_lkg.sql/.down.sql`：不可变 Desired State、head、
  approval、artifact/snapshot、apply/ack/LKG、事件/outbox、复合 owner 约束和业务事实 down guard。
- `console-web/src/features/private-gateways/`、`console-web/src/lib/edgeManagement.js` 与 Platform Fleet：
  Workspace 期望/实际/LKG、审批/发布/回滚和固定失败摘要；Platform 只获得 generation/lag/stale/failure code。
- `docs/prd/PRD-121-edge-desired-state-signed-snapshot-offline-lkg.md`、
  `docs/development/edge-desired-state-sync.md` 与 `docs/testing/PRD-121-tests.md`：保存协议、原子恢复、
  两轮自审、专项/必要回归、真实非自批审批与 Agent ACK、双视口 Browser 和敏感审计；下一权威项为
  PRD-122，G5 在 PRD-130 后统一执行。

## PRD-122 私有网关遥测、用量与失败事实同步（已完成）

- `shared-sdk/python/metahub_shared/private_gateway/telemetry.py`：严格四流判别联合、三档 policy、RFC 8785
  canonical payload、递归禁止字段扫描、连续 cursor/receipt 和固定失败合同。
- `gateway-data-plane/src/metahub_gateway/telemetry/edge_spool.py`：本地 SQLite append-only batch/receipt、
  原子 sequence、受控 epoch 旋转、gzip EdgeSession 上传、禁止重定向和 no-store receipt 校验。
- `control-plane-api/src/metahub_control/edge_management/telemetry_service.py` 与同域 models/routes/service：
  policy CAS、设备能力恢复、批次去重/冲突、无缺口 ACK、Usage/失败事务归集、事件/outbox 和 Fleet 安全投影。
- `infra/migrations/0114_edge_telemetry_usage_failure_sync.sql/.down.sql`：policy/head、batch/receipt、cursor、
  Usage/失败、Fleet 投影、命令、事件/outbox、复合 owner、不可变事实和业务事实 down guard。
- Workspace/Platform Console：遥测策略、水位、积压、Usage/成本、固定失败和资源桶；两个 Session/DTO
  独立，移动网格、空投影和敏感字段裁剪已验证。
- `docs/prd/PRD-122-private-gateway-telemetry-usage-failure-sync.md` 与
  `docs/testing/PRD-122-tests.md`：保存两轮自审、专项/必要回归、真实上传链路、双视口 Browser 和独立
  敏感审计；下一权威项为 PRD-123，G5 在 PRD-130 后统一执行。

## MVP 非目标

以下能力不得拖慢 M0/M1：

- 硬件盒子。
- MCP 主链路。
- 信创适配。
- 复杂数据出境。
- Kubernetes/Helm 生产部署。
- 完整 Trace 可视化或 OpenTelemetry 后端。
