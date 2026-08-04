# PRD-043 - 用量 CSV 导出

状态：已完成设计、两轮自审、实现、专项与全量拆分回归、0032 运行库迁移、真实同步/异步 API 及桌面/中间宽度/移动浏览器验收。

## 需求理解

### 来源与顺序

- Excel `01_PRD总表` 和 `02_M0-M1核心开发` 明确：PRD-043 属于 `M0.5-W3/4`、`P0`、核心层、基础报表导出，名称为“用量CSV导出”。
- README、`docs/prd` 和 `docs/testing` 的已完成证据连续到 PRD-042，PRD-043 尚无设计或测试记录，因此它是当前下一条未完成 PRD。
- 指定页面为“用量报表页”，指定入口为 `GET /usage/export.csv`，数据对象为 `UsageExportJob`，稳定事件为 `usage.exported`。
- `TC-0085` 要求按日期、项目、Key、模型导出，字段完整，大于阈值时异步生成且导出操作有审计；`TC-0086` 要求无权限、非法输入、依赖失败和大数据边界返回统一错误，不泄露敏感信息且状态可追踪。

### 用户价值与范围

企业管理员需要把当前租户的 Usage Ledger 导出为可被国内常见表格软件直接打开的 CSV，用于财务对账和成本核验：

1. 导出筛选与当前用量页一致，至少支持日期、项目、Virtual Key 和模型，并继续支持页面已有的 Environment、应用、Provider、estimated 和 request_id 筛选。
2. 小结果同步下载；超过同步阈值后创建可查询、可恢复、可下载的异步任务，而不是只返回一个无法领取制品的 202。
3. 每行能回溯到 `request_id`、项目/Key/模型/Provider、Token、原币/CNY/USD 成本、价格版本和逐行价格快照。
4. 导出请求、实际下载、任务状态和完成事件均可按租户、操作者、request_id/trace_id 追踪。
5. CSV 不包含 Prompt、Response、Authorization、Cookie、Provider Key、Virtual Key 明文/哈希、Secret、DLP 命中文本或原始 JSON 正文。

### 当前角色映射

- Excel 角色写“财务/管理员”，但当前稳定角色只有 `admin/developer/viewer`；`FinanceAdmin` 要到 PRD-059 才加入权限矩阵。
- PRD-043 不提前发明 finance 角色。当前批量账本导出仅允许 `admin`，新增后端 `require_usage_exporter` 守卫；developer 继续使用现有 `/usage/summary` 和 `/usage/records` 只读能力，但不能批量导出；viewer 和匿名继续拒绝。
- 前端只对 admin 显示导出命令，但后端守卫、租户条件和资源归属才是最终权限边界。

### 架构约束

- Usage Ledger 是账单和报表事实源，每条记录必须保留 `tenant_id/project_id/virtual_key_id` 成本归属、原始/归一用量口径、`request_id` 和 `price_schema_version`。
- 导出属于低频控制面和 FinOps 旁路，不进入 Gateway 高频模型调用热路径，也不能阻塞流式首字延迟。
- 金额默认人民币，同时保留账本原币和 USD 字段；PRD-043 不做实时汇率换算、发票、PDF 账单、额度调整或账单争议处理。
- 数据、制品、审计和事件默认留在境内本地数据库/部署基础设施，不调用外部在线转换、邮件或第三方文件服务。

### 明确非目标

- 不做发票、PDF/Word/HTML 账单或客户价值报告。
- 不做 FinOps 调整/冲正、赠送额度、账单争议和额度类型报表，这些属于 PRD-051、PRD-079/080。
- 不做审计日志自身 CSV 导出，属于 PRD-058。
- 不做租户全量数据打包或注销导出，属于 PRD-089。
- 不接入 MinIO/S3、分布式任务队列或跨节点对象存储；表结构预留 `storage_backend/object_key`，MVP 使用有界 SQLite BLOB。

## 涉及模块

- `infra/migrations/0032_usage_csv_export.sql` 与 down migration：任务、事件、索引、租户归属触发器、审计动作约束。
- `shared-sdk/python/metahub_shared/runtime/config.py`：同步阈值、硬上限、字节上限、轮询、lease、重试和保留期配置。
- `shared-sdk/python/metahub_shared/runtime/errors.py`：导出 not-found/not-ready/expired/too-large/unavailable 固定错误。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：加入可订阅的 `usage.exported` 安全事件。
- `shared-sdk/python/metahub_shared/runtime/fastapi_app.py`：CORS 暴露标准 `Content-Disposition`，供控制台读取安全文件名。
- `control-plane-api/src/metahub_control/auth/dependencies.py`：admin-only 导出守卫。
- `control-plane-api/src/metahub_control/usage/models.py`：任务、接受、查询响应 DTO。
- `control-plane-api/src/metahub_control/usage/service.py`：继续提供共享筛选规范化与 tenant-qualified SQL 条件。
- `control-plane-api/src/metahub_control/usage/export_service.py`：计数、快照、CSV 生成、任务状态、下载和事件。
- `control-plane-api/src/metahub_control/usage/scheduler.py`：可恢复异步任务领取、lease、重试和过期清理。
- `control-plane-api/src/metahub_control/usage/routes.py`、`app.py`：同步/异步路由和调度生命周期组合。
- `control-plane-api/src/metahub_control/audit/service.py`：导出请求/下载的严格审计白名单。
- `console-web/src/lib/usage.js`、`src/app/main.js`：Bearer 下载、202 轮询、Blob 生命周期、操作状态和 request_id。
- `console-web/src/features/usage/renderUsagePanel.js`、`src/styles/main.css`：导出命令、进度/失败/完成反馈和移动端字段标签。
- `control-plane-api/tests/integration/test_usage_export.py`、`test_db_migrations.py`、`console-web/tests/unit/usage.test.mjs`：专项和回归。

## 数据表和字段设计

### usage_export_ledger_sequences

SQLite 隐式 `rowid` 在删除最大行后可能复用，不能作为异步财务导出的稳定快照。0032 新增只增映射表：

- `usage_export_sequence INTEGER PRIMARY KEY AUTOINCREMENT`：只增且不复用的内部账本序号。
- `tenant_id`、`usage_ledger_id UNIQUE`：每条 Usage 只分配一个序号，归属触发器校验两者租户一致。
- 升级时按 `created_at, usage_ledger_id` 回填历史账本；运行期由 `usage_ledger AFTER INSERT` 在同一事务分配。
- 映射行不可更新或删除；0032 生效后 `usage_ledger` 采用追加事实语义，全行 UPDATE/DELETE 均拒绝，更正必须写新调整事实。
- 导出计数和生成都以 `tenant_id + usage_ledger_id` INNER JOIN 映射表，并使用同一 `usage_export_sequence <= snapshot_ledger_sequence` 水位。

### usage_export_jobs

所有字段都属于当前租户，客户端不能提交 tenant_id、actor、状态、路径或阈值。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `usage_export_job_id` | TEXT PK | 服务端生成 `uex_` ID。 |
| `tenant_id` | TEXT NOT NULL FK | 认证租户；所有查询第一条件。 |
| `requested_by` | TEXT NOT NULL FK users | 当前 admin；触发器校验用户属于同租户。 |
| `request_id` | TEXT NOT NULL | 导出业务幂等键；`UNIQUE(tenant_id, request_id)`。 |
| `trace_id` | TEXT NOT NULL | 初始请求链路。 |
| `status` | pending/processing/completed/failed/expired | 稳定状态机。 |
| `generation_mode` | sync/async | 由行数阈值决定。 |
| `format` | 固定 csv | 防止任务表变成任意文件生成器。 |
| `filters_json` | JSON object | 仅规范化日期、ID、estimated、request/trace 筛选，不保存名称或密钥。 |
| `filter_fingerprint` | SHA-256 | 审计使用，不记录完整筛选。 |
| `snapshot_at` | UTC timestamp | 保留导出时间语义并兼容日期筛选，不单独承担稳定快照。 |
| `snapshot_ledger_sequence` | INTEGER >= 0 | 当前租户只增账本水位；不进入 API、审计、事件或日志。 |
| `row_count` | INTEGER | 过滤后固定行数。 |
| `total_tokens` | INTEGER | 对账汇总。 |
| `total_cost_cny/usd` | NUMERIC | 按行账本值求和，不做汇率换算。 |
| `file_name` | ASCII TEXT | 服务端固定生成，不反射租户、项目或模型名称。 |
| `content_type` | 固定 text/csv | 下载合同。 |
| `csv_content` | BLOB nullable | completed 且未过期时保存完整有界制品。 |
| `content_size_bytes` | INTEGER | 字节硬上限校验。 |
| `content_sha256` | TEXT | 下载前完整性核对，不进入日志正文。 |
| `storage_backend` | 固定 sqlite_blob | 为后续对象存储迁移预留稳定口径。 |
| `object_key` | nullable TEXT | 当前保持 NULL，客户端永远不能提交。 |
| `download_count` | INTEGER default 0 | 实际授权下载次数。 |
| `attempt_count` | INTEGER 0-3 | worker 领取次数。 |
| `lease_token/lease_until` | nullable | 多进程/崩溃恢复领取凭证。 |
| `error_code` | nullable 固定枚举 | 不保存异常正文、SQL 或路径。 |
| `requested_at/started_at/completed_at/expires_at` | UTC timestamp | 生命周期。 |
| `created_at/updated_at` | UTC timestamp | 通用追踪。 |

约束与索引：

- CHECK 保证状态、模式、格式、计数、大小和状态相关字段一致；completed 必须有 BLOB/hash/完成时间，expired 必须清空 BLOB。
- `idx_usage_export_jobs_pending_lease` 支撑 pending 和 lease 过期 processing 领取。
- `idx_usage_export_jobs_tenant_time` 支撑当前租户状态查询。
- INSERT/UPDATE 触发器保证 tenant/requested_by 不错配，tenant/job/actor 归属字段不可被后续更新。
- 任务状态只允许 `pending -> processing -> completed|pending|failed`、`completed|failed -> expired`；下载只更新 download_count/updated_at。

### usage_export_events

持久化稳定完成事实，避免 Webhook 是否成功决定任务事实是否存在：

- `usage_export_event_id`、`tenant_id`、`usage_export_job_id`、`event_type='usage.exported'`。
- `actor_user_id`、`request_id`、`trace_id`、`payload_json`、`created_at`。
- `UNIQUE(tenant_id, usage_export_job_id, event_type)`，租户/任务/actor 归属触发器。
- payload 只允许 job ID、row_count、generation_mode、content_size_bytes、status；禁止筛选、CSV、名称、路径、hash、Key 或价格正文。

### audit_logs 扩展

- 新动作：`usage_export.requested -> usage_export_job`、`usage_export.downloaded -> usage_export_job`。
- 允许摘要字段仅为 `status/generation_mode/format/row_count/total_tokens/total_cost_cny/total_cost_usd/content_size_bytes/download_count/filter_fingerprint`。
- requested audit 与任务创建同事务；downloaded audit 与 download_count 增量同事务。同步响应在一个事务内记录 requested 和 downloaded；审计失败时不释放 CSV。
- 0032 重建 audit CHECK 时保留全部历史行和 0031 动作。down 先删除 PRD-043 审计行，再恢复 0031 CHECK，删除 event/job；不触碰 Usage Ledger 和既有审计。

## API 设计

### 共享筛选

三个 Usage API 共用同一规范化函数和 tenant-qualified WHERE：

- `start_date/end_date`：沿用当前 UTC 日边界，不在 PRD-043 单独改成上海业务日。
- `project_id/virtual_key_id/model_id`：Excel 必选筛选。
- `environment_id/application_id/provider_id/estimated/request_id/trace_id`：复用当前页面已有筛选。
- export 不接受 `limit/offset/tenant_id/user_id/path/format` 或未知 query；非法参数返回统一 422。
- count、同步生成和 worker 生成使用完全相同的 JOIN、WHERE、`snapshot_at` 和稳定 `created_at ASC, usage_ledger_id ASC` 排序。

### GET /usage/export.csv

权限：`require_usage_exporter`，当前仅 admin。

行为：

1. 从请求上下文确定 tenant、actor、request_id/trace_id，规范化筛选并固定 `snapshot_at`。
2. 若同租户同 request_id 已有任务，按原 `generation_mode/status` 回放，不重复排队、生成、审计或出网。
3. 行数大于 hard max：413 `usage.export_too_large`，不创建任务，提示缩小日期/项目/Key/模型范围。
4. 行数 `<= sync_row_limit`：生成 CSV，原子写 completed sync job、完成事件、requested/downloaded 审计，然后返回 200 CSV；空结果仍返回只有表头的 CSV。
5. 行数 `> sync_row_limit`：原子写 pending async job和 requested 审计，返回 202 JSON，由持久 scheduler 生成。

200 响应头：

- `Content-Type: text/csv; charset=utf-8`
- `Content-Disposition: attachment; filename="metahub-usage-<start>-<end>-<job>.csv"`
- `Cache-Control: no-store, private`
- `X-Content-Type-Options: nosniff`
- 共享中间件继续注入 `X-Request-ID/X-Trace-ID`

202 响应：

```json
{
  "export_job": {"usage_export_job_id": "uex_...", "status": "pending"},
  "status_url": "/usage/exports/uex_...",
  "download_url": "/usage/exports/uex_.../download",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

GET 有创建副作用是 Excel 固定合同，因此必须 `no-store`、只由显式按钮以 Bearer fetch 触发，且用 `tenant_id + X-Request-ID` 幂等；不能用普通可预取 `<a href>`，Token 也不能放 URL。

### GET /usage/exports/{usage_export_job_id}

- admin-only，按 `tenant_id + job_id` 查询；跨租户与不存在统一 404 `usage.export_not_found`。
- 返回安全任务 DTO、规范化筛选 ID、当前状态、行数/汇总、attempt、文件名/大小/hash、时间、固定 error_code、status/download URL 和当前响应 request_id/trace_id；不返回 BLOB、对象路径或完整审计内容。
- 查询前执行有界过期清理；过期任务保留元数据但 BLOB 已清。

### GET /usage/exports/{usage_export_job_id}/download

- admin-only、tenant-qualified。
- pending/processing 返回 409 `usage.export_not_ready`；failed 返回 503 `usage.export_unavailable`；expired 返回 410 `usage.export_expired`；跨租户统一 404。
- completed 时核对 BLOB size/hash，原子增加 download_count 并写 downloaded audit，再返回与同步接口相同的安全 CSV 头。

## CSV 字段与格式设计

### 固定列顺序

CSV schema version 1 固定以下列，不随数据库新增字段自动漂移：

1. `usage_ledger_id`
2. `tenant_id`
3. `request_id`
4. `trace_id`
5. `request_status`
6. `request_error_code`
7. `created_at`
8. `updated_at`
9. `environment_id`
10. `environment`
11. `environment_name`
12. `project_id`
13. `project_name`
14. `application_id`
15. `application_name`
16. `virtual_key_id`
17. `virtual_key_name`
18. `virtual_key_prefix`
19. `provider_id`
20. `provider_code`
21. `model_id`
22. `request_model`
23. `resolved_model`
24. `endpoint`
25. `routing_attempt_id`
26. `input_tokens`
27. `output_tokens`
28. `total_tokens`
29. `cache_tokens`
30. `cost_amount`
31. `cost_cny`
32. `cost_usd`
33. `currency`
34. `price_schema_version`
35. `price_snapshot_id`
36. `snapshot_input_price_per_1k`
37. `snapshot_output_price_per_1k`
38. `snapshot_currency`
39. `price_plan_id`
40. `price_effective_from`
41. `estimated`
42. `usage_source`

所有 JOIN 都同时带 tenant_id。单个导出可以引用多个价格快照，快照字段必须逐行输出，不能错误地写成 job 级单一 `price_snapshot_id`。

### 编码与安全

- 使用标准库 `csv.writer`、UTF-8 BOM 和 RFC 4180 CRLF，国内 Windows Excel/LibreOffice 可直接识别中文。
- 金额和价格从数据库值转为 Decimal 字符串，不经 float DTO 二次计算；CNY/USD 不换算。
- 所有文本去除 NUL 和危险控制字符，单元格有明确长度上限。
- CSV 双引号不能防公式注入。文本 `lstrip()` 后以 `= + - @`、TAB、CR 或 LF 开头时前置单引号，再交给 `csv.writer` 转义。
- `estimated` 输出稳定 `true/false`；数字字段由服务端类型化格式化，不把任意数据库文本当公式或数字。
- 不导出 `raw_usage/normalized_usage` JSON。其财务有效字段已经展开为 Token、成本、来源和价格快照；排障继续使用受权 Usage API。
- 生成超过 max bytes 时丢弃完整内存结果，不保存或下载半截 CSV，并按固定错误码重试/失败。

## 异步任务与事务设计

### 配置默认值

- `METAHUB_USAGE_EXPORT_ENABLED=true`
- `METAHUB_USAGE_EXPORT_POLL_INTERVAL_SECONDS=2`
- `METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT=5000`
- `METAHUB_USAGE_EXPORT_MAX_ROWS=100000`
- `METAHUB_USAGE_EXPORT_MAX_BYTES=52428800`（50 MiB）
- `METAHUB_USAGE_EXPORT_LEASE_SECONDS=60`
- `METAHUB_USAGE_EXPORT_MAX_ATTEMPTS=3`
- `METAHUB_USAGE_EXPORT_RETENTION_HOURS=24`

所有字段在 `AppConfig` 有安全默认值和上下界；测试通过配置降低阈值，不插入 5001 条记录。

### worker 状态机

1. scheduler 只安装在 Control API lifespan，和 Provider Health scheduler 组合，不进入 Gateway。
2. 每轮先清理到期 completed/failed：清 BLOB/object_key，标 expired，保留安全元数据。
3. `BEGIN IMMEDIATE` 领取最早 pending 或 lease 已过期的 processing；写随机 lease token、lease_until、started_at 和 attempt_count 后立即提交。
4. 在事务外按任务 `snapshot_ledger_sequence` 读取稳定范围、生成完整 CSV、校验 row_count/bytes/hash；不持有 SQLite 写锁包住生成过程。
5. 完成事务用 `job_id + tenant_id + lease_token + status=processing` 条件更新，写 BLOB/hash/汇总/completed/expiry 和唯一 event；lease 已被接管则丢弃本次结果。
6. 可重试失败在 attempt < 3 时回 pending，只保存固定 error_code；最终标 failed、清 BLOB。原始异常、SQL、路径、筛选和 CSV 不写日志。
7. 任务/事件提交后尽力分发 `usage.exported` Webhook；外部失败不能回滚已完成任务。

## 前端页面与交互设计

- 复用“用量与成本”页面，不新增导航和落地页。
- admin 在筛选区看到“导出 CSV”；developer 继续只读页面但不显示导出；viewer 无用量入口。按钮隐藏只是体验优化。
- 点击时快照当前筛选，排除 limit/offset/tenant_id；按钮禁用并设置 `aria-busy`，独立 `usageExportState` 不把整个用量页替换成错误页。
- 200：读取 Blob、校验 `text/csv`、安全解析 ASCII `.csv` 文件名、触发下载并立即 revoke object URL，显示 request_id。
- 202：显示 job ID/行数/pending，使用 Bearer Token 有限轮询 status URL；completed 后再以 Bearer fetch 下载。离开页面、登出或身份变化必须 Abort，不把 Token/job 写 URL、localStorage 或 sessionStorage。
- 401 清登录态；403/410/413/422/503 显示统一错误 code、message 和 request_id，长 ID/文件名允许换行。
- 给现有 9 个 Usage 明细单元格补 `data-label`；小于 1100px 时以标签/值两列显示，720px 以下单列，修复表头隐藏后字段不可辨识的问题。
- 1280x720、900x800 和 390x844 下导出命令、反馈、筛选、job 状态、分页和明细不得重叠或造成根节点横向溢出。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `usage.export_not_found` | 404 | job 不存在或不属于当前租户。 |
| `usage.export_not_ready` | 409 | pending/processing 尚不可下载。 |
| `usage.export_expired` | 410 | 制品已过期并清除。 |
| `usage.export_too_large` | 413 | 行数超过 hard max，需缩小范围。 |
| `usage.export_filter_invalid` | 422 | 未知/禁止 query 或筛选结构非法。 |
| `usage.export_unavailable` | 503 | 数据库、生成、hash、worker 或制品完整性失败。 |
| `auth.token_missing` | 401 | 匿名访问。 |
| `auth.permission_denied` | 403 | developer/viewer 或其他非 admin。 |

日期和已有筛选继续复用 `usage.date_invalid`、`usage.query_invalid`。所有错误保持 `code/message/request_id/trace_id/suggestion/diagnosis` 和响应头链路 ID，不回显 SQL、路径、筛选值或内部异常。

## 安全与权限约束

1. API 不接受 tenant_id；任务、事件、审计、Usage/price/request JOIN 都显式带认证 tenant_id。
2. job ID 猜测和跨租户 ID 与不存在资源使用同一 404，禁止资源枚举。
3. requested_by、job、event、项目/Key/模型筛选均做租户归属验证；无匹配筛选结果可导出空表头，但其他租户资源不能改变查询范围。
4. CSV 只包含固定财务字段；日志、审计、事件和任务 DTO 均不包含 CSV BLOB、filters_json 全文、对象路径、名称或密钥。
5. BLOB 有行数、字节、保留期和状态约束；过期先清制品再标状态，不能通过 status/download 复原。
6. worker 只记录 job/tenant/row_count/status/error_code/request_id/trace_id，不记录 CSV、hash 以外的内容、筛选、项目名或 Key。
7. 文件名和路由路径完全由服务端构造，不能发生路径穿越、响应头注入或用户名称反射。
8. GET 副作用以 no-store、显式 fetch 和 request_id 幂等收敛；浏览器不能用可预取链接直接创建任务。
9. `usage.exported` 允许订阅，但事件 payload 最小化；它不会包含逐行数据，也不会触发新的 Usage 记录。
10. 0032 up/down、任务创建、下载计数、审计、event、worker 完成都使用明确事务边界；外部 Webhook 只能在提交后执行。

## 测试用例

### 迁移

1. 空库 0001-0032、重复 migrate、0031 历史升级均成功。
2. job/event/索引/触发器和审计新动作存在；旧 Usage/audit/Webhook 数据保留。
3. tenant/actor/job/event 错配、未知状态、非法 JSON、completed 无 BLOB、expired 有 BLOB、重复 tenant+request_id 被数据库拒绝。
4. 0032 down 删除 PRD-043 job/event/audit，恢复 0031 audit CHECK，保留旧 audit/Usage；down-up 再执行成功。
5. `integrity_check=ok`、`foreign_key_check` 为空。

### 后端主流程

1. admin 小结果返回 200；BOM、CRLF、Content-Type/Disposition/no-store/nosniff、固定列顺序、升序和 X-Request/Trace 正确。
2. 日期/项目/Key/模型单项与组合筛选准确；空结果只有表头；结果总 Token、CNY/USD 与 summary 同口径。
3. 金额/价格输出 Decimal 字符串；每行 price snapshot/version 与账本一致，estimated/source 正确。
4. 阈值配置为 1 时，2 行返回 202；单轮 worker 后 status completed、download 200，hash/size/row_count/汇总正确。
5. 相同 X-Request-ID 串行/并发只创建一条 job、一条 requested audit 和一个 event；两个 worker 竞争只一个完成，lease 超时可恢复。
6. 同步 job 写 requested+downloaded audit；异步首次下载增加 download_count 并写 downloaded audit。
7. `usage.exported` 可订阅，HMAC 正确且 payload 无筛选/名称/CSV/Key；Webhook 失败不回滚 completed。

### 异常、权限与安全

1. developer/viewer 403、匿名 401；跨租户 status/download 404且不改变 download_count。
2. tenant_id/limit/offset/path/未知 query、非法日期/estimated/超长 ID 返回固定 422。
3. success 前下载 409、expired 410、hard max 413、失败/损坏 BLOB 503。
4. 数据库/审计/worker/生成/hash 失败不返回部分 CSV，状态重试或 failed；错误无原始异常正文。
5. 公式前缀、前导空白、TAB/CRLF、NUL、逗号、双引号和中文 round-trip；任何文本单元格不能成为 `= + - @` 公式。
6. CSV、响应、job、审计、事件、日志不含 `sk-`、whsec、Token、Prompt、Response、messages、Authorization、raw_usage 或 normalized_usage。
7. 过期清理清 BLOB；进程重启后 lease 过期任务能恢复；最终失败不无限重试。

### 前端

1. 客户端发送当前筛选且不含 tenant_id/limit/offset；Content-Disposition 只接受安全 ASCII `.csv`。
2. 200 Blob 下载、URL revoke、错误 Content-Type、401/403/422/5xx 和 request_id 显示正确。
3. 202 状态轮询、completed 下载、failed/expired、Abort 和防重复点击正确；Token 不进入 URL/存储。
4. admin 有导出命令；developer/viewer 无命令；后端权限测试仍独立存在。
5. 9 列 `data-label`、反馈转义、长 job/request/file 换行和响应式 CSS 回归。

### 回归与运行验收

1. PRD-017/029/030/031/032 用量、项目、环境、价格和估算回归通过。
2. PRD-034 审计、PRD-025/042 Webhook 和 shared runtime 事件回归通过。
3. Python 全量、Node 示例、Console 全量/lint、中文注释门禁通过。
4. 运行库应用 0032，三服务健康 200。
5. 真实浏览器完成小结果下载和大结果 202 -> completed -> download；CSV 在 Windows Excel/LibreOffice 中中文正常、无公式执行，行数/Token/成本与 API 对齐。
6. 1280x720、900x800 与 390x844 页面无重叠、截断或根节点横向溢出；developer/viewer/匿名后端权限实测，验收截图遮罩敏感身份文本。

## 代码修改计划

1. 完成两轮设计自审并修正权限、异步领取、事务、CSV 安全、迁移回滚和前端生命周期风险。
2. 实现 0032 正反迁移、配置、错误码、事件目录和 admin-only 守卫。
3. 重构 Usage 共享筛选，新增任务/CSV 服务、状态/下载 API、scheduler 和审计。
4. 实现控制台 API、下载/轮询状态、页面命令、会话清理和移动端标签。
5. 增加迁移、主流程、数据量、并发、异常、权限、跨租户、公式注入、精确金额、事件和前端测试。
6. 执行专项、全量、运行库迁移、真实 API/CSV 和桌面/移动浏览器验收。
7. 更新 README、目录边界和 `docs/testing/PRD-043-tests.md`；全部证据通过后才把本文件状态改为已完成。

## 设计自审 1

第一轮检查需求解释、权限、接口闭环、查询一致性和 CSV 安全，修订如下：

- **阻断：财务角色尚不存在。** 若直接复用 `require_usage_reader`，developer 会获得全租户批量账本。已改为 PRD-043 admin-only，FinanceAdmin 留给 PRD-059，不扩展角色枚举。
- **阻断：只有一个 202 接口无法领取异步结果。** Excel 虽只点名 `/usage/export.csv`，但“大于阈值异步生成”要求可用闭环。已增加 tenant-qualified status/download 从属接口。
- **重要：GET 创建任务可能被预取或重试。** 已固定为显式 Bearer fetch、no-store、按钮触发和 `tenant_id + X-Request-ID` 幂等，禁止普通链接和 Token query。
- **重要：count、页面和 worker 可能各用一套筛选。** 已要求三个 Usage API 共用规范化/WHERE；导出额外保存只增账本序号水位，count 与生成使用同一 cutoff、水位和排序。
- **重要：字段完整不能靠数据库整行自动导出。** 已固定 42 列 schema，逐行保留价格快照和归属，同时排除 raw/normalized JSON 与秘密字段。
- **安全：csv.writer 的引号不能阻止公式执行。** 已增加控制字符清理、危险首字符单引号和类型化数值输出，测试覆盖前导空白、TAB、CRLF、逗号和引号。
- **对账：float DTO 会制造额外精度漂移。** CSV 路径直接把数据库 NUMERIC 转 Decimal 字符串，不复用前端 float DTO 做二次求和。

## 设计自审 2

第二轮检查任务恢复、存储、审计约束、事件、迁移回滚和浏览器生命周期，修订如下：

- **阻断：一次性 BackgroundTask 无法在进程崩溃后恢复。** 已改为持久 pending/processing + lease scheduler，领取短事务、生成事务外、完成条件更新，过期 lease 可接管。
- **阻断：本地任意文件路径会引入穿越、跨进程和遗留文件风险。** 当前 local-sqlite 使用有界、短保留 SQLite BLOB，并预留 storage backend；100000 行/50 MiB/24h 构成明确上限。
- **阻断：审计 action 和数据库 CHECK 是严格白名单。** 0032 必须同时重建 CHECK、扩展服务白名单并做 up/down 测试；任务创建/下载与审计同事务。
- **重要：worker 无 AuthContext，不能伪造用户完成审计。** 用户动作只审计 requested/downloaded；系统完成由 job 状态和唯一 `usage.exported` event 追踪。
- **重要：同步响应也必须有任务事实。** 所有导出都创建 UsageExportJob；sync 在返回前原子写 completed job、event、requested/downloaded audit，重复 request_id 回放同一制品。
- **重要：Webhook 失败不能让财务制品消失。** event 与 completed 同事务，提交后才尽力分发；外部失败只形成 Webhook delivery，不回滚任务。
- **阻断：SQLite rowid 删除后可复用，时间戳也可能只有秒级。** 已改用独立 AUTOINCREMENT 映射序列；历史回填、新账本同事务分配，账本及映射均禁止原地更新/删除。专项测试覆盖任务后同秒写入、晚到序号高于水位、删除拒绝且 worker 行数不漂移。
- **租户：只在 API WHERE 加 tenant_id 不足以防内部错写。** 已加入 job actor、event job/actor 的数据库归属触发器和不可变租户字段。
- **回滚：audit_logs 无法直接删除且 CHECK 需恢复。** down 迁移必须在临时允许迁移的重建流程中排除 PRD-043 行并恢复 0031 结构，保留其他历史审计与 Usage。
- **前端：CORS 默认读不到 Content-Disposition。** 共享 CORS 只新增暴露标准文件名头；不暴露内部 lease/storage 信息。
- **体验：现有移动表头隐藏后没有字段名。** 本 PRD 同时补齐 Usage 行 `data-label`，并要求轮询在离页/登出/身份变化时 Abort，旧 job/request_id 不跨会话残留。

两轮自审结论：上述阻断和重要问题均已反映到最终实现；专项与全量拆分回归、运行库完整性、真实同步/异步 API 和三档浏览器验收全部通过，PRD-043 可以正式标记完成。
