# PRD-058 - 审计日志不可删除与导出

状态：**已完成**。设计初稿、两轮编码前自审、实现、实现后两轮交叉复核、0049 实迁移、专项与
相邻回归、真实 Runtime、Browser、停服静止终态和正式 artifact 均已通过。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 第 59 行一致定义：
  `PRD-058 | M1-企业POC | P1 | 核心层 | 管理操作审计增强 | 审计日志不可删除与导出`。
- Excel 指定用户为安全负责人/管理员，用户故事是把关键管理操作导出留档。
- Excel 指定范围是：审计日志不可删除；支持按时间、操作者、对象导出 CSV；导出本身也生成
  审计事件。
- Excel 指定非目标为 WORM 桶；本 PRD 不承诺独立不可改介质、第三方时间戳或监管级不可抵赖。
- 页面、接口、对象和事件分别是 `PAGE-084 审计日志页`、`GET /audit-logs/export`、
  `AuditLog/AuditExport` 和 `audit.exported`。
- `TC-0115` 要求管理员修改 Provider、Key、预算、DLP、策略均有记录，且导出结果可复核。
- `TC-0116` 要求异常、权限和安全路径返回统一错误码与 `request_id`，不泄漏敏感信息并可追踪。
- 本 PRD 启动时 PRD-057 已完整完成；按 M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3 和 Excel
  行顺序，PRD-058 当时是下一条未完成项，不是仅凭编号推断。

### 1.2 架构约束映射

- 产品部署在中国境内并服务中国企业；OpenRouter、硅基流动只是竞品与迁移兼容对象，不是默认上游。
- 架构第 12.3 节要求支持租户级审计日志导出，账单和强审计数据不能被普通管理员随意删除。
- 架构第 15.3 节要求业务表默认携带 `tenant_id`，管理 API 必须在后端鉴权，不能依赖前端隐藏。
- 架构附录 E 要求强审计路径按合规留痕优先；审计写入失败时不能释放未留痕的导出结果。
- 所有错误继续使用稳定英文 code，中文只属于展示层；API 和错误响应保留 `request_id/trace_id`。
- Provider Key、Virtual Key、Token、Prompt、Response、DLP 原始 pattern、Authorization、Cookie、
  Secret 和原始请求/响应正文不得进入 CSV 之外的日志，也不得被意外新增到 CSV。

### 1.3 现有能力与真实缺口

现有 PRD-034 审计基础已经具备：

- `audit_logs` 业务表、tenant-first 索引、操作者归属触发器、不可更新/删除触发器、重复 ID/rowid
  冲突门禁和负 rowid 门禁。
- Provider、Provider Key、Virtual Key、预算、DLP、Policy 及后续治理对象均在业务事务内调用
  `record_audit_log`，审计插入失败会回滚业务修改。
- `GET /audit-logs` 已支持时间、操作者、动作、对象、request_id 和分页，且仅 admin 可读。
- `GET /audit-logs` 当前连接未用 `finally` 关闭；本 PRD 抽取共享 filters 时必须一并修复并测试，
  不能让列表、导出或 outbox 在 Windows 留下 SQLite 句柄。
- 控制台已有审计列表、筛选、分页、摘要和 admin-only 导航；developer/viewer 无入口，后端仍返回 403。
- 每条管理审计提交后尽力投递 `audit.recorded`。

PRD-058 的增量缺口是：

1. 没有 `GET /audit-logs/export`，也没有固定 schema、公式注入防护、行数/字节上限和安全下载头。
2. 没有持久 `AuditExport` 事实，无法用导出 ID、筛选指纹、快照边界、行数、字节和 SHA-256
   复核本地 CSV。
3. 没有 `audit.exported` 事件及专用 payload/envelope 安全合同。
4. PAGE-084 没有导出命令、下载状态、导出 ID/hash/request_id 反馈和离页/换身份取消逻辑。
5. 现有页面动作/对象下拉停留在 PRD-034 初始枚举，缺少 Policy 和后续治理动作；至少必须补齐
   Excel 本次验收所需 Provider/Key/预算/DLP/Policy 和 `audit.exported`。
6. 虽然数据库已有不可变门禁，本 PRD 仍需直接复验 `UPDATE/DELETE/REPLACE/INSERT OR REPLACE/rowid`
   绕过路径以及没有写接口/删除控件，才能形成“不可删除”的当前源码证据。
7. `update_application` 跨项目迁移会批量改写关联 Virtual Key 的 `project_id`，当前没有逐 Key AuditLog；
   这是 Excel“修改 Key 均记录”的真实旁路，必须在同一事务补齐 `virtual_key.updated`，审计失败回滚
   Application 与全部 Key 变更。运行期 `last_used_at`、Provider 健康 lease 等非管理员配置状态不纳入。

### 1.4 实现范围

本 PRD 实现：

- 0049 数据库迁移与 down migration。
- 同步、有界、快照一致、可按同一 `request_id` 确定性重放的审计 CSV 导出。
- `AuditExport` 不可变复核事实、`audit.exported` 持久事件事实和提交后 Webhook 投递。
- PAGE-084 导出按钮、下载、成功/失败反馈、身份 owner、Abort 和页面刷新。
- Provider/Provider Key/预算/DLP/Policy 审计完整性专项，以及审计日志不可变专项。
- Application 跨项目导致的 Virtual Key 间接修改审计，确保 Key 没有批量更新旁路。
- 统一错误码、API 文档、测试记录、README 和目录说明。

### 1.5 明确不做

- 不做 WORM 桶、区块链、第三方时间戳、电子签章或监管级不可抵赖。
- 不做 S3/MinIO 长期保存、异步导出任务、导出历史列表或再次下载接口；MVP 由管理员保存本地文件，
  服务端只保留不可变 hash/规模/筛选/快照证据。
- 不做跨租户超级管理员导出。
- 不做全文检索、任意 SQL/JSON 字段筛选或任意列选择。
- 不在 PRD-059 前新增 SecurityAdmin、FinanceAdmin、OpsAdmin；当前安全负责人继续映射为 admin。
- Provider“测试连接”写入的 `health_status/last_tested_at`、后台健康 probe/lease 和 Key `last_used_at`
  属于运行观测，不是 Excel 所称管理员配置修改，不伪装成 `provider.updated`；本 PRD 验收配置字段修改、
  Key 间接归属修改和既有专用治理动作。若后续需要审计测试命令，应新增独立 action，而非污染更新语义。
- 不把 MCP、硬件盒子、信创、复杂数据出境或 SIEM 格式全集引入 M1 主链路。

## 2. 涉及模块

| 模块 | 修改职责 |
| --- | --- |
| `infra/migrations/0049_audit_log_immutable_export.sql/.down.sql` | 新增稳定 AuditLog 导出序列、AuditExport、source outbox/lease 和不可变/owner/冲突门禁；保真重建 audit_logs 以登记 `audit.exported`。 |
| `control-plane-api/src/metahub_control/audit/filters.py` | 抽取列表与导出共用的严格筛选规范化和 tenant-qualified SQL 条件。 |
| `control-plane-api/src/metahub_control/audit/export_service.py` | 快照、CSV、公式防护、大小门禁、持久复核、幂等重放、审计与事件。 |
| `control-plane-api/src/metahub_control/audit/export_outbox.py` | source event 领取、租约接管、退避、CAS 终结和 `audit.exported` 可恢复投递。 |
| `control-plane-api/src/metahub_control/audit/export_scheduler.py` | 启动即恢复及低频周期 drain；请求路径只唤醒 worker，不直接取得恢复权限。 |
| `control-plane-api/src/metahub_control/audit/{models.py,routes.py,service.py,__init__.py}` | 导出 DTO/响应元数据、固定路由、预生成 AuditLog ID、审计 action/summary 白名单、公共导出和列表连接 finally-close。 |
| `control-plane-api/src/metahub_control/projects/service.py` | 应用跨项目迁移时，为每个被间接修改 `project_id` 的 Virtual Key 写同事务审计，关闭 Key 旁路。 |
| `control-plane-api/src/metahub_control/auth` | 新增独立 `require_audit_exporter`，当前仅允许 admin，便于 PRD-059 后独立演进。 |
| `shared-sdk/python/metahub_shared/runtime/{errors.py,i18n.py,webhooks.py}` | 统一错误、中英文标签、`audit.exported` 类型和严格 payload/envelope 合同。 |
| `control-plane-api/src/metahub_control/app.py` | 安装 source outbox scheduler，并暴露安全导出 ID/ETag 响应头，不扩大 Gateway CORS。 |
| `console-web/src/lib/auditLogs.js` | 导出请求、CSV/文件名/ETag/导出 ID 白名单校验、统一错误和下载 helper。 |
| `console-web/src/features/audit-logs/*` | PAGE-084 导出状态、完整身份 owner、Abort、按钮和安全反馈。 |
| `console-web/src/app/main.js`、`src/styles/main.css` | active navigation 绑定、离页/注销清理、响应式布局。 |
| `control-plane-api/tests`、`shared-sdk/python/tests`、`console-web/tests` | 迁移、主流程、异常、权限、租户、事件、CSV、前端生命周期和视觉合同。 |
| `docs/api/audit-exports.md`、`docs/testing/PRD-058-tests.md` | 公开合同和最终直接证据。 |

## 3. 数据表和字段设计

### 3.1 `audit_exports`

同步导出不保存 CSV BLOB，但保存足以复核下载文件的不可变终态：

| 字段 | 类型/约束 | 业务含义 |
| --- | --- | --- |
| `audit_export_id` | TEXT PK，`aex_` + 32 hex | 导出对象稳定 ID。 |
| `tenant_id` | TEXT NOT NULL | 唯一租户边界；所有查询和唯一约束 tenant-first。 |
| `requested_by` | TEXT NOT NULL | 已认证 admin 用户 ID；INSERT 时验证同租户归属。 |
| `audit_log_id` | TEXT NOT NULL，唯一 | 预生成的自身 `audit.exported` AuditLog ID；延迟 FK 在 commit 时保证一一存在。 |
| `status` | TEXT NOT NULL，固定 `completed` | 同步导出只有完整终态，绝不保存半份或 pending 假状态。 |
| `format` | TEXT NOT NULL，固定 `csv` | 阻止表演变为任意文件生成器。 |
| `schema_version` | INTEGER NOT NULL，固定 1 | 冻结 CSV 列顺序和复核算法。 |
| `filters_json` | TEXT NOT NULL，canonical JSON object | 保存规范化时间/操作者/动作/对象/request_id 条件，不含 tenant。 |
| `filter_fingerprint` | TEXT NOT NULL，64 位小写 hex | 对 canonical filters 的 SHA-256；审计/Webhook 只外发指纹。 |
| `snapshot_sequence` | INTEGER NOT NULL，>=0 | 导出开始时当前租户稳定序列的最大值；不依赖可能被 `VACUUM` 或重建改变的普通 rowid。 |
| `snapshot_audit_log_id` | TEXT NULL | 快照边界行的稳定 ID；空租户导出时为 NULL。 |
| `snapshot_created_at` | TEXT NULL | 快照边界时间，仅用于人工复核。 |
| `row_count` | INTEGER NOT NULL，0..10000 | CSV 数据行数，不含表头。 |
| `content_size_bytes` | INTEGER NOT NULL，1..16 MiB | 最终带 BOM CSV 字节数。 |
| `content_sha256` | TEXT NOT NULL，64 位小写 hex | 下载文件 SHA-256；作为 ETag 和审计摘要。 |
| `file_name` | TEXT NOT NULL，ASCII `.csv` basename | 服务端生成，不含租户名、邮箱、对象名或路径。 |
| `request_id` | TEXT NOT NULL | 当前 Control operation；与 tenant/requested_by 组成确定性重放键。 |
| `trace_id` | TEXT NOT NULL | 事件、审计、API 和日志关联。 |
| `event_id` | TEXT NOT NULL，tenant 内唯一 | `audit.exported` 持久事件 ID。 |
| `event_payload_json` | TEXT NOT NULL，安全 canonical JSON | 只含导出 ID、规模、hash、指纹、schema/status。 |
| `created_at` | TEXT NOT NULL | 完成时间。 |
| `updated_at` | TEXT NOT NULL 且等于 created_at | 满足通用对象合同；对象不可变所以不会更新。 |

索引和约束：

1. `UNIQUE (tenant_id, audit_export_id)`、`UNIQUE (tenant_id, requested_by, request_id)`、
   `UNIQUE (tenant_id, event_id)`。
2. `idx_audit_exports_tenant_time (tenant_id, created_at DESC, audit_export_id DESC)`。
3. `idx_audit_exports_tenant_actor_time (tenant_id, requested_by, created_at DESC)`。
4. owner INSERT trigger 校验操作者属于同一租户；非零快照序列、边界 AuditLog ID 与时间必须指向
   同租户同一映射，零快照则两个边界字段必须同时为 NULL；request/trace/event 结构必须合法。
5. `audit_logs` 新增候选唯一键 `(tenant_id, audit_log_id)`；AuditExport 使用
   `(tenant_id, audit_log_id) DEFERRABLE INITIALLY DEFERRED` 复合 FK。服务先插入 AuditExport，随后用
   预生成 ID 插入 AuditLog；AuditLog owner trigger 反向校验 export ID、actor、request、trace 和该 ID，
   commit 时复合延迟 FK 同时保证存在且同租户，形成一一对应且不引入循环插入死锁。
   AuditExport INSERT 还要求该预生成 ID 当前尚不存在；任意 AuditLog INSERT 若命中已保留的 export
   audit ID，则无论 action 分支都必须精确满足 `audit.exported/audit_export` 反向绑定，不能拿同租户旧的
   普通 AuditLog 或随后插入的其他 action 填充延迟 FK。
6. immutable UPDATE/DELETE trigger 拒绝任何改写或删除。
7. conflict INSERT trigger 在 PK/rowid/owner-request/event/audit-log 冲突前主动
   `RAISE(ABORT, 'audit_export_immutable')`，
   避免 `REPLACE` 或 `INSERT OR REPLACE` 绕过 DELETE trigger。
8. 负 rowid AFTER INSERT trigger 拒绝异常显式 rowid。

### 3.2 `audit_log_export_sequences`

普通 SQLite 表的 `rowid` 不是长期稳定业务标识：`VACUUM` 或未来保真重建都可能重新编号。因此
0049 新增只增不复用的导出序列映射，所有快照边界只引用该序列：

| 字段 | 类型/约束 | 业务含义 |
| --- | --- | --- |
| `audit_log_sequence` | INTEGER PK AUTOINCREMENT | 全库单调只增且删除后不复用的稳定水位。 |
| `tenant_id` | TEXT NOT NULL | 映射所属租户；导出查询始终 tenant-first。 |
| `audit_log_id` | TEXT NOT NULL，唯一映射 | 指向不可变 AuditLog；每条日志恰好一个序列。 |

- 0049 按 `created_at ASC, audit_log_id ASC` 回填全部历史日志，随后验证日志与映射计数、双向缺失均为 0。
- `AFTER INSERT ON audit_logs` 在同一事务自动追加映射；映射失败必须回滚原审计写入，不能产生无水位日志。
- `(tenant_id, audit_log_id)` 复合 FK 与 owner trigger 双重校验映射归属；tenant+sequence 索引支撑租户
  水位和有界 join，不能只用全局 AuditLog ID 单列 FK。
- UPDATE、DELETE、REPLACE、`INSERT OR REPLACE`、序列或 AuditLog ID 冲突以及负 rowid 绕过均固定拒绝。
- 每次导出在读取水位前再次验证当前租户全部 AuditLog 都有且只有一个映射；缺失或错 owner 时失败关闭，
  不允许用不完整快照生成 CSV。

### 3.3 `audit_logs` 增量

- 0049 以 0048 的当前最终 schema 为来源保真重建 `audit_logs`，新增动作 `audit.exported` 和对象
  `audit_export`。
- 复制必须显式保留原 rowid、全部列、NULL 语义，并用行数与双向 `EXCEPT` 守卫后才切表。
- owner trigger 对 `audit.exported` 要求同租户 `audit_exports.audit_export_id` 已存在，requested_by、
  request_id 和 trace_id 与当前审计一致。
- 保留全部既有 tenant-first 索引、部分唯一索引、owner 分支、不可变、冲突和负 rowid trigger。
- 新增 `UNIQUE (tenant_id, audit_log_id)` 候选键，供 AuditExport 与序列映射使用复合同租户 FK；单列 PK
  继续保留兼容，复合键不是用应用查询替代数据库租户证明。
- 新增日志到稳定导出序列的同事务 AFTER INSERT trigger；导出自身审计获得的序列严格晚于已捕获水位。
- `SUMMARY_FIELDS['audit_export']` 只允许：`status/format/schema_version/row_count/content_size_bytes/
  content_sha256/filter_fingerprint/snapshot_audit_log_id/snapshot_created_at`。

### 3.4 `audit_export_events` source outbox

`audit_exports.event_id/event_payload_json` 是不可变 source event 事实；单独的 `audit_export_events` 只保存
可恢复派发状态，避免为了重试而修改 AuditExport：

| 字段 | 类型/约束 | 业务含义 |
| --- | --- | --- |
| `audit_export_event_id` | TEXT PK，等于 AuditExport event_id | 稳定 source event 身份。 |
| `tenant_id` | TEXT NOT NULL | 唯一租户边界。 |
| `audit_export_id` | TEXT NOT NULL，tenant 内唯一 | 一份导出严格对应一个 source event。 |
| `status` | TEXT NOT NULL | `pending/processing/dispatched/failed`。 |
| `attempt_count` | INTEGER NOT NULL | 领取时单调递增，达到上限后失败关闭。 |
| `next_attempt_at` | TEXT NULL | pending 退避到期时间；终态为 NULL。 |
| `claim_token/claim_owner/lease_until` | TEXT NULL | processing 三元组；仅持有未过期 token 的 worker 可终结。 |
| `last_error_code` | TEXT NULL | 仅固定白名单错误码，不保存异常、URL 或响应正文。 |
| `dispatched_at` | TEXT NULL | 成功终态时间。 |
| `created_at/updated_at` | TEXT NOT NULL | source 创建和状态更新时间。 |

- AuditExport、source outbox、`audit.exported` AuditLog 在同一 `BEGIN IMMEDIATE` 事务写入；任一失败全部回滚。
- owner trigger 把 event/export/tenant 绑定到 AuditExport 的不可变字段；identity immutable、状态机、lease、
  DELETE、REPLACE、冲突 rowid 和负 rowid trigger 防止伪造接管或擦除。
- worker 通过 tenant+status+next_attempt 索引领取 due pending，或接管 lease 已过期的 processing；claim 与
  success/retry/failed 都用 event_id+claim_token+未过期 lease 做 CAS，迟到 worker 不能覆盖新 owner。
- 只有持 source lease 的 worker 才调用
  `dispatch_webhook_event(..., resume_existing_pending=True)`。它从 AuditExport 重新构造并严格校验相同
  event_id/payload；delivery 终态不重复，已有 pending intent 可恢复。
- 路由提交后最多唤醒内部 worker，不直接调用恢复模式。应用启动即 drain，随后周期扫描，因此 commit
  与首次 delivery intent 冻结之间崩溃也能在没有客户端重试时恢复。
- 派发失败按有界指数退避回到 pending，达到上限进入 failed；Webhook delivery 自身继续沿用既有重试和
  dead-letter。两层分别解决 source 崩溃窗口与 Endpoint 网络失败，不混写 exactly-once 承诺。

### 3.5 down migration

- 无 `audit_exports` 或 `audit.exported` 事实时，down 删除 0049 的 source outbox、序列触发器、映射和
  导出对象，保真重建 0048 的
  audit_logs action/object 合同，然后可重新 up。
- 一旦存在 AuditExport、source outbox、`audit.exported` AuditLog、Webhook subscription/delivery/
  attempt/dead-letter 或未知 view/trigger/FK 依赖，down 必须在任何 DDL 前原子拒绝，错误为固定迁移
  guard；不能为了回滚删除审计、订阅或投递证据。
- down 不声称 WORM；具备数据库文件/迁移权限的运维仍可以离线处理，这是本 PRD 明确边界。

## 4. CSV 合同与复核算法

### 4.1 固定 schema version 1

CSV 固定 18 列，顺序如下，不随数据库新增字段自动漂移：

1. `audit_log_id`
2. `tenant_id`
3. `actor_user_id`
4. `actor_email`
5. `actor_role`
6. `action`
7. `object_type`
8. `object_id`
9. `object_name`
10. `before_summary_json`
11. `after_summary_json`
12. `changed_fields_json`
13. `client_ip`
14. `user_agent`
15. `request_id`
16. `trace_id`
17. `created_at`
18. `schema_version`

CSV 不包含数据库 rowid、AuditExport 内部事件 JSON、Webhook、Provider/Virtual Key 明文或完整 hash、
DLP pattern、Prompt、Response、Authorization、Cookie、Token、Secret 或任意原始业务请求/响应正文。

### 4.2 编码和公式防护

- 使用标准库 `csv.writer`、UTF-8 BOM 和 CRLF，兼容中国企业常用 Windows Excel/LibreOffice。
- JSON 列先按 canonical JSON 输出，保证相同快照重放得到相同字节。
- `audit/service.py` 提供列表与导出共用的存量行严格 decoder：重新校验 action/object 精确配对、ID/角色/
  时间/文本边界、摘要白名单、敏感键、标量与总字节限制，并要求 `changed_fields` 是无重复稳定序列且与
  before/after 实际差异完全一致；policy dry-run 等特殊动作沿用写入时的精确合同。
- before/after/changed_fields 解析和校验后再 canonicalize，不能把数据库原文直接复制到 CSV；为兼容
  安全的历史空白/键序差异，不要求存量 JSON 原文已经 canonical。历史行只要出现未知键、嵌套容器、
  非法差异、损坏时间或越界文本，列表
  返回 `audit_log.query_unavailable`，导出转换为 `audit_export.integrity_failed`，两者都不释放部分数据。
- 每个文本单元先把 NUL、CR、LF、TAB、U+200B/U+2060/ESC 及其他 Unicode `C*` 类字符归一为空格，
  再对 `cleaned.lstrip()` 检查 `= + - @`；危险值把单引号放到最终 cell 的第一个字符。最终不变量再次
  断言 `cell.lstrip()` 不以公式前缀开头，防止控制字符或截断顺序绕过。
- 引号和逗号由 `csv.writer` 处理；数值字段以 ASCII 十进制字符串输出。
- 使用数据库 cursor 逐行迭代，不得 `fetchall()`；`csv.writer` 写入 `BytesIO + TextIOWrapper`，每写一行
  flush 并检查实际 UTF-8 BOM 字节数。超过 10000 行或 16 MiB 时立即丢弃完整内存结果并返回统一错误，
  不保存/下载半份 CSV，也不形成 `10000 * 大摘要` 的额外行对象峰值。

### 4.3 快照、事务和确定性重放

1. 从认证 Session 恢复 tenant/actor，后端 `require_audit_exporter` 只允许 admin。
2. 严格拒绝未知 query，特别是 `tenant_id/limit/offset/path/columns`。
3. 规范化筛选并生成 canonical JSON 与 SHA-256 指纹。
4. 开启 `BEGIN IMMEDIATE`。审计导出是低频、有界管理操作；短时写锁换取一致快照和导出留痕
   原子性，避免 SQLite 读快照升级为写事务时出现 `SQLITE_BUSY_SNAPSHOT`。
5. 只按当前 `tenant_id + requested_by + request_id` 查幂等事实：筛选指纹不同返回 409；完全相同才按
   存量 `snapshot_sequence` 重新查询不可变审计行并生成 CSV，校验 row_count/bytes/hash 完全一致，
   不新增 AuditExport、AuditLog 或 source event。另一管理员使用同形 request_id 会创建自己的独立导出，
   既不能读取前一管理员文件，也不会被前一管理员占用 operation ID 拒绝服务。
6. 首次请求先验证当前租户 AuditLog 与序列映射完整，再捕获最大 `audit_log_sequence` 和对应边界
   ID/时间；筛选始终附加 `tenant_id=? AND audit_log_sequence<=?` 并通过映射 join 读取日志。
7. 以 `created_at ASC, audit_log_id ASC` cursor 逐行读取；第 `MAX_ROWS+1` 行或逐行写入后字节超限立即失败。
8. 生成完整 CSV 并完成逐行安全重验、控制字符归一、公式防护、bytes 和 SHA-256 校验。
9. 预生成 export/audit/event ID，用最终 AuditExport 字段构造并严格校验 `audit.exported`
   WebhookEvent，再在同一事务依次插入 `audit_exports`、pending `audit_export_events` 和指定 ID 的
   `audit.exported` AuditLog；延迟 FK、事件构造、任一 INSERT 或 commit 失败全部 rollback。
10. commit 成功后才向路由返回 CSV；网络响应不确定时，客户端用相同 X-Request-ID 可再取完全相同
    字节。导出自身 AuditLog 在快照之后，不会递归出现在自己的 CSV 中。
11. commit 后尽力投递 `audit.recorded` 并唤醒 source worker。只有 CAS 领取 event lease 的 worker 才以
    同一 event ID 调用 `dispatch_webhook_event(..., resume_existing_pending=True)`；启动/周期 drain 可恢复
    commit 后崩溃窗口，终态 delivery 不重复出网。Webhook 失败不反转已提交导出，只记录固定错误码和
    安全 ID；这里承诺“持久 source event + lease 恢复”，不是网络 exactly-once。
12. 幂等重放不改写 AuditExport 的原始 `trace_id` 或 source envelope；HTTP 中间件仍为本次尝试返回当前
    `X-Trace-ID`，用同一 request_id 把多次传输尝试关联到一个不可变导出事实。

## 5. API 设计

### 5.1 `GET /audit-logs/export`

权限：`admin`。PRD-059 前安全负责人映射为 admin；developer/viewer 后端 403，匿名 401。

允许查询参数：

| 参数 | 语义 |
| --- | --- |
| `created_from` | 可选 timezone-aware ISO-8601，包含边界。 |
| `created_to` | 可选 timezone-aware ISO-8601，不包含边界，必须晚于 from。 |
| `actor_user_id` | 可选，精确匹配当前租户操作者。 |
| `action` | 可选，精确匹配稳定动作。 |
| `object_type` | 可选，后端白名单对象类型。 |
| `object_id` | 可选，精确匹配对象。 |
| `request_id` | 可选，筛选被审计 operation 的 request_id。 |

请求 `X-Request-ID` 是导出 operation 的幂等键，不等于 query 参数 `request_id`。两者由 HTTP
位置区分；前端必须为每次新点击生成新 operation ID，网络结果不确定重试时复用原值。

成功响应：

- HTTP 200。
- `Content-Type: text/csv; charset=utf-8`。
- `Content-Disposition: attachment; filename="metahub-audit-<UTC日期>-<短ID>.csv"`。
- `Cache-Control: no-store, private`、`Pragma: no-cache`、`X-Content-Type-Options: nosniff`。
- `ETag: "<content_sha256>"`。
- `X-MetaHub-Audit-Export-ID: <audit_export_id>`。
- `X-MetaHub-Audit-Export-Row-Count: <0..10000>`。
- 共享中间件继续返回 `X-Request-ID/X-Trace-ID`。

`ETag` 已在 Control 现有 CORS 合同中暴露；0049 只给 Control 的 `extra_cors_expose_headers` 增加
两个 `X-MetaHub-Audit-Export-*` 头，不能扩大 Gateway CORS。

路由声明必须位于动态审计资源路由之前；本 PRD不新增 DELETE/PATCH/PUT 审计接口。

### 5.2 公开 DTO 与 OpenAPI

CSV 响应没有 JSON 成功体。内部 `AuditExportResult` 只在服务/路由间携带：

- `audit_export_id`
- `file_name`
- `content_sha256`
- `content_size_bytes`
- `row_count`
- `request_id/trace_id`
- `content: bytes`

任何 API/前端 DTO 都不返回 `filters_json`、`event_payload_json`、`snapshot_sequence` 或底层数据库路径。

## 6. `audit.exported` 事件设计

`WEBHOOK_EVENT_TYPES` 与可订阅目录新增 `audit.exported`。事件信封：

- `event_id`：持久 `aexevt_` ID，重放不变化。
- `tenant_id`：AuditExport 租户。
- `actor_id`：requested_by。
- `object_id`：audit_export_id。
- `trace_id`：导出 operation trace。
- `timestamp`：AuditExport created_at。

payload 固定字段：

```json
{
  "audit_export_id": "aex_...",
  "status": "completed",
  "format": "csv",
  "schema_version": 1,
  "row_count": 12,
  "content_size_bytes": 4096,
  "content_sha256": "64-lower-hex",
  "filter_fingerprint": "64-lower-hex"
}
```

共享 webhook builder 必须专门校验精确键集合、类型、范围、ID 前缀、actor/object/trace/timestamp
与持久记录一致；通用“无敏感键”检查不能替代专用合同。

## 7. 前端页面与交互设计

PAGE-084 延续现有 audit-logs 页面，不新建营销页或平行导航：

1. 筛选区继续提供时间、动作、对象类型、操作者、对象 ID 和 request_id。
2. 在命令区增加带下载符号和文字的“导出 CSV”按钮；没有 lucide 依赖时使用熟悉的下载符号，
   并保留 `title/aria-label`。
3. 导出使用当前筛选但不发送 list 的 limit/offset，也不发送 tenant_id。
4. 请求中按钮固定尺寸并 disabled/`aria-busy=true`，避免重复点击改变布局。
5. 成功后使用一次性 object URL 下载并立即 revoke；只接受安全 ASCII `.csv` 文件名、`aex_` ID、
   64 hex ETag、0..10000 行数头和非空 CSV blob。
6. 成功反馈只显示导出 ID、SHA-256、行数和 request_id；随后刷新列表，
   让新增 `audit.exported` 行可见。
7. 失败反馈显示统一 code/request_id，不渲染原始响应、CSV 或 header dump。
8. 导出状态绑定 tenant/user/role/session_id/session_revision/navigation/generation；离页、pagehide、注销、
   401、换租户、角色变化或 Session revision 变化会 abort 并清空 Blob/反馈。
9. developer/viewer 无导航、无页面 DOM、无导出按钮、无后台列表或导出预取；这只是前端体验，后端
   仍独立拒绝。
10. 1280x720 与 390x844 验收按钮、筛选、反馈、表格/移动记录、长 hash/ID 换行、焦点和零横向溢出。

动作和对象下拉至少补齐：

- Provider、Provider Key、Virtual Key、Budget、DLP。
- Policy、Policy Snapshot、Policy Dry Run。
- RoutingPolicy、CacheAffinity、ModelQuality、FinOps、CostAnomaly、ErrorMap。
- Webhook replay/rotate/archive、Usage export、`audit.exported`。

## 8. 错误码设计

| 错误码 | HTTP | retryable | 场景 |
| --- | ---: | --- | --- |
| `auth.unauthenticated` | 401 | 否 | 缺少或失效 Session。 |
| `auth.permission_denied` | 403 | 否 | developer/viewer 调用导出。 |
| `audit_log.filter_invalid` | 422 | 否 | action/object/ID 筛选非法。 |
| `audit_log.time_range_invalid` | 422 | 否 | 时间无时区、解析失败或 to<=from。 |
| `audit_export.filter_invalid` | 422 | 否 | 未知 query、tenant_id/limit/columns 等夹带字段。 |
| `audit_export.request_conflict` | 409 | 否 | 同租户同管理员同 operation request_id 对应不同筛选。 |
| `audit_export.too_large` | 413 | 否 | 超过 10000 行或 16 MiB。 |
| `audit_export.integrity_failed` | 500 | 否 | 幂等重放的行数/bytes/hash 与持久事实不一致。 |
| `audit_export.unavailable` | 503 | 是 | 数据库、CSV 生成、AuditExport/审计事务暂不可用。 |

所有错误沿用统一正文 `code/message/suggestion/diagnosis/request_id/trace_id`。错误日志只允许固定
event/error_code、tenant、actor、audit_export_id、request_id、trace_id；不记录筛选、CSV、摘要、
邮箱、IP、User-Agent、SQL、数据库路径或原始异常文本。

## 9. 安全与权限约束

1. tenant/actor 只从 bearer Session 恢复；query/body/header 不能指定 tenant。
2. `require_audit_exporter` 在后端只允许 admin；前端隐藏按钮不能替代权限。
3. 所有审计和导出 SQL 第一条件为 `tenant_id=?`；快照引用、操作者和审计对象由 trigger 再校验。
4. `audit_logs`、`audit_log_export_sequences`、`audit_exports` 均拒绝 UPDATE/DELETE/REPLACE/冲突 rowid；
   历史 AuditLog 复制仍保留 rowid 以做迁移保真，但导出水位只使用稳定 AUTOINCREMENT sequence。
5. 导出构建、AuditExport、AuditLog、持久事件事实处于同一事务；审计失败不释放 CSV。
6. Webhook 属于 commit 后副作用；失败不能回滚，但事件 ID、payload 和 source 状态已持久保存；只有
   source lease owner 可恢复同一事件仍为 pending 的 delivery，不重复发送终态，也不虚写 exactly-once。
7. CSV schema 是显式白名单；不能 `SELECT *` 后自动写全部列。
8. CSV、筛选和完整摘要不写结构化日志、Webhook 或普通 API JSON。
9. 文件名由服务端生成 ASCII basename；前端拒绝路径、控制字符、非 csv 扩展和超长名称。
10. GET 导出响应禁止缓存和 MIME 嗅探；操作 request_id 与认证管理员共同支持网络不确定后的同字节
    重放，不同管理员同形 request_id 形成独立事实且不能相互读取。
11. 不读取客户端伪造的 X-Forwarded-For；CSV 里的 client_ip 只是原 AuditLog 已保存的安全事实。
12. 开发与测试临时数据只位于 `D:/MetaHub/.tmp`，不使用 `C:/tmp`、不提权；pytest 链接只删除
    经确认的链接节点并保留目标目录与测试证据。

## 10. 测试用例

### 10.1 迁移与不可变

1. 空库 0001->0049，连续版本、checksum、表/索引/trigger/action/object 合同正确。
2. 0048 历史审计升级保留 rowid、NULL、全部列、计数和双向 EXCEPT；稳定序列按确定顺序完整回填，
   重复应用保持幂等。
3. 无 PRD-058 事实/订阅/依赖时 0049 down/up；有 AuditExport、source event、`audit.exported` 审计、
   Webhook subscription/delivery/attempt/dead-letter 或未知依赖时 down 在 DDL 前原子拒绝。
4. audit_logs、audit_log_export_sequences 和 audit_exports 的 UPDATE、DELETE、REPLACE、
   INSERT OR REPLACE、PK/rowid 冲突、负 rowid 全部拒绝，原行 hash 不变。
5. requested_by、snapshot、AuditLog object/request/trace/audit_log_id 跨租户或错 owner 写入被 trigger 拒绝；
   直接引用另一租户日志由复合 deferred FK 拒绝，引用同租户既有普通日志或用其他 action 填充预留 ID
   由双向 trigger 拒绝，缺少自身审计也拒绝 commit。
6. source outbox identity/状态机/claim/lease/CAS/DELETE/REPLACE/rowid 门禁正确，过期 processing 可接管，
   旧 token 不能终结新 owner。

### 10.2 后端主流程 TC-0115

1. admin 分别修改 Provider、Provider Key、Virtual Key、Budget、DLP、Policy，均产生同事务审计；
   Application 跨项目导致的每条 Virtual Key `project_id` 间接修改也逐条留痕，任一审计失败时整个
   管理事务回滚。
2. 创建两租户、多操作者、多对象、多时间记录；导出时间/actor/object 组合只包含目标租户和目标行。
3. CSV 为 BOM+CRLF、固定 18 列、稳定升序；JSON canonical，逗号/引号/中文正确。
4. 公式攻击覆盖前导空白、NUL、TAB、CR/LF、U+200B/U+2060/ESC 后 `= + - @`，Excel 打开只显示文本。
5. 空结果返回只有表头 CSV，仍生成 AuditExport、AuditLog 和 `audit.exported`。
6. AuditExport row_count/bytes/SHA 与实际文件一致；ETag、导出 ID、文件名、request/trace 正确；
   `audit_log_id` 与唯一自身审计双向一致。
7. 导出自身审计不进入自己的快照；下一次导出可按 `action=audit.exported` 导出先前事实。
8. 同一管理员使用相同 operation request_id + 相同筛选返回相同 export ID/文件名/bytes/hash，且
   AuditExport、AuditLog、source event 和 Webhook 终态出网不重复；`VACUUM` 后重放仍为相同字节。
9. 第二位管理员复用相同 request_id 时创建独立 owner/export/audit/event，不能得到第一位管理员的
   export ID 或内容；两条记录各自幂等。

### 10.3 异常、权限与安全 TC-0116

1. developer/viewer 403、匿名 401；畸形 query 仍先执行认证/RBAC，不泄漏筛选合同。
2. 第二租户同形筛选返回空 CSV，不能探测第一租户 actor/object/request。
3. `tenant_id/limit/offset/path/columns`、未知 action/object、超长 ID、无时区时间、to<=from 均统一失败。
4. 同管理员同 request_id 不同筛选 409；超过行数/字节 413；无半份 response、AuditExport、AuditLog
   或 source event；cursor 逐行实现不得调用 `fetchall()`，每行都执行 16 MiB 门禁。
5. 数据库查询、BEGIN/insert/audit/commit、CSV writer、hash 和事件构造失败映射统一错误；不泄漏 SQL、
   路径、异常或敏感数据。
6. list/export/outbox 的成功、领域错误、SQLite 错误和取消路径都关闭连接；Windows 可立即重命名/删除
   专项数据库，证明没有残留句柄。
7. 篡改存量 AuditExport 的复核夹具导致重放 hash/rows/bytes 不一致时 500，绝不释放文件。
8. 篡改历史 AuditLog 的 action/object、摘要敏感/未知/嵌套键、changed_fields 重复/漏项/伪差异、时间
   或文本边界时，列表与导出分别稳定失败且不返回局部行/CSV；安全的 JSON 空白/键序差异可读取，
   导出仍输出 canonical JSON。
9. CSV/响应/AuditExport/AuditLog/Event/Webhook/日志扫描不含测试 Provider Key、Virtual Key、Token、
   Prompt、Response、Authorization、Cookie、Secret、DLP pattern。
10. Webhook HMAC 正确，event_id 稳定，payload/envelope 精确；投递失败不影响已提交下载；模拟 commit
   后、首次 intent 冻结前崩溃时由启动 worker 无客户端重试恢复；模拟派发中崩溃时 lease 接管只恢复
   pending intent，已成功 delivery 不重复发送，非 lease owner 调恢复模式的路径不存在。

### 10.4 前端与 Browser

1. 客户端映射全部筛选，不发送 tenant/limit/offset；Bearer、operation request_id、Accept 正确。
2. 成功校验 content-type、filename、aex ID、ETag、row count、request/trace，下载后 revoke object URL。
3. 错误只显示 code/request_id；未知/恶意 header 回退安全值，不进入 DOM/下载路径。
4. busy 防双击；相同 ticket 网络重试复用 request_id；迟到成功/失败不能覆盖新 owner。
5. 离页、注销、401、角色/租户/Session revision 变化 abort 并清除 Blob/反馈。
6. admin 可见；developer/viewer 无导航/DOM/预取，手写 hash 回到后端允许入口。
7. 15 秒自动刷新和筛选/分页刷新只调用列表 API，绝不调用 `/audit-logs/export`；导出只能由管理员
   显式点击触发，页面装载、重渲染和 timer 都不得产生带副作用 GET。
8. 1280x720、390x844 真实 Chrome：筛选、导出、成功/错误、长 hash/ID、移动行、焦点、无重叠、
   无横向溢出、截图非空、Console/Page error/blocked request/敏感命中均为 0。

### 10.5 回归与运行验收

1. PRD-058 专项 Python/Console/shared Webhook。
2. 受影响相邻回归：PRD-034 audit、PRD-043 CSV、Provider/Key/Budget/DLP/Policy、Webhook、Console
   navigation/main/audit；不因局部问题反复重跑根级全量。
3. Node 示例、Console 完整回归、lint、Python 语法、Ruff 致命规则和中文注释门禁。
4. 根级 Python 全量只在代码冻结后执行一次；若有失败，按失败 node 和相关文件定向闭环并如实记录，
   不把定向结果改写成一次全绿。
5. 隔离运行库应用 0049，验证 migration count/head/checksum、integrity/FK、trigger、不可变和零跨租户。
6. 真实 Control/Console/Gateway 健康、admin 导出、权限、第二租户、CSV/hash、Webhook/HMAC。
7. 停服后端口、Session、sidecar、spool、lease、pending 和日志隐私终态；链接清理严格遵守
   `D:/MetaHub/.tmp` 可见范围和只删链接节点约束。

## 11. 代码修改计划

1. 完成两轮设计自审并回写本文件；在此之前不编辑产品代码。
2. 新增 0049 up/down 和迁移专项，先验证历史保真、不可变和 down guard。
3. 抽取 audit filters，新增共享错误/Webhook 合同与单元测试。
4. 实现 AuditExport/CSV/复核/幂等/审计/事件服务和 admin-only route。
5. 补齐 Provider/Key/Budget/DLP/Policy 完整性与异常/权限/租户集成测试。
6. 实现 PAGE-084 客户端、owner 状态、按钮、反馈、下载、生命周期和响应式样式。
7. 更新 API、README、目录说明和测试记录。
8. 执行专项、受影响回归、代码门禁，再进行隔离运行库、真实 Runtime、Browser 和停服验收。
9. 只有全部直接证据通过后才把 PRD-058 标记完成，并从 Excel 确认下一条未完成 PRD。

## 12. 设计自审第一轮

状态：已完成。审查范围是需求边界、数据一致性、CSV 安全、迁移可逆性和 GET 副作用。

### 12.1 发现与修正

1. **阻断：直接把 `before_summary_json/after_summary_json` 原文写入 CSV 会绕过当前读取边界。**
   数据库损坏或错误迁移可能把未知键、嵌套值或非 canonical JSON 带入文件。已修正为逐行复用
   action/object/summary 白名单，解析后 canonicalize；任何损坏使整个导出失败，不返回部分 CSV。
2. **阻断：控制字符和公式判定顺序不当会漏掉 TAB/CR/LF/U+200B 等引导的公式。** 已冻结为先把
   所有 Unicode `C*` 类字符归一为空格，再对 `cleaned.lstrip()` 判定 `=+-@`，危险时把单引号放到最终
   cell 首位并复验最终不变量；一条审计始终对应一个物理行。
3. **一致性：仅保存 hash 而不向浏览器返回行数，管理员无法直接对照页面与 AuditExport。** 已增加
   `X-MetaHub-Audit-Export-Row-Count`，与导出 ID 一并只在 Control CORS 暴露。
4. **事务：如果 `audit.exported` 事件在 commit 后首次构造，payload 合同错误会留下没有合法事件的
   AuditExport。** 已改为在事务内用最终字段构造并严格校验 WebhookEvent，持久化同一 event ID/payload
   后再 commit；网络投递仍严格在 commit 后。
5. **GET 副作用：代理或客户端重试可能重复导出审计。** Excel 已明确指定 GET，不能擅自改 POST；
   设计以 tenant+requested_by+当前 operation 的 X-Request-ID 作为幂等键。相同 owner/筛选确定性重建
   相同 CSV，不重复 AuditExport/AuditLog/Event；同 owner/ID 不同筛选稳定 409，另一 actor 相互隔离。
6. **SQLite：普通读事务在生成 CSV 后升级写事务可能触发 busy snapshot。** 已明确采用短时
   `BEGIN IMMEDIATE`，并用 10000 行/16 MiB/单次完整内存结果约束锁持有时间；不在事务内投递网络。
7. **迁移：只新增 AuditExport 表无法让 export 在现有 audit_logs CHECK 中落动作。** 0049 必须保真
   重建当前 0048 审计最终合同，复制 rowid/NULL/全列并恢复所有后续 action、owner 和冲突门禁；同时
   更新冻结在旧版本的迁移测试 `LATER_MIGRATIONS`，不能把 0049 跳过后误测旧 down。
8. **筛选漂移：列表与导出各写一套规范化会导致页面所见与文件不同。** 已规划抽取 `audit/filters.py`，
   两条路径共享 action/object/time/ID 规范化、tenant SQL 和 canonical filters，仅分页属于列表。

第一轮结论：以上阻断和重要问题已经回写设计；可以进入第二轮自审，仍禁止编码。

## 13. 设计自审第二轮

状态：已完成。审查范围是租户/RBAC、快照长期稳定性、幂等主体、事件恢复、前端 owner 和后续迁移兼容。

### 13.1 发现与修正

1. **阻断：普通 `audit_logs.rowid` 不能承担持久快照水位。** `VACUUM` 可能重新编号普通 rowid，
   未来为扩展 action 而保真重建表也可能改变未显式复制的 rowid；已新增
   `audit_log_export_sequences`，用 `INTEGER PRIMARY KEY AUTOINCREMENT` 为每条 AuditLog 分配只增不复用
   水位。导出只按 `tenant_id + audit_log_sequence` join，AuditExport 字段改为 `snapshot_sequence`。
2. **阻断：只有首次迁移回填序列，运行期 trigger 被遗漏或损坏时会静默导出不完整文件。** 已规定
   AuditLog INSERT 与映射 AFTER trigger 同事务，并在每次捕获水位前验证当前租户日志/映射一一对应；
   任何缺失、重复或错 owner 都返回不可用且不释放 CSV。
3. **越权：只比较 tenant/request_id/filter 会允许同租户另一管理员复用猜中的 operation ID。** 幂等键
   已改为 `tenant_id + requested_by + request_id`；同 owner 不同筛选 409，另一 actor 创建独立导出，
   查询从不命中前一 actor 的事实，因此既不泄漏文件，也不形成跨 actor 的 request-id 拒绝服务。
4. **可靠性：请求路径不能直接使用 `resume_existing_pending=True`。** 现有 Webhook 合同明确只允许持有
   source outbox lease 的 worker 使用恢复模式；已新增 `audit_export_events` source outbox、claim token、
   lease、退避和启动/周期 drain。路由只提交 source 并唤醒 worker；worker CAS 领取后才恢复 delivery，
   因此 commit 后、首次 intent 前崩溃无需客户端重试也可恢复。
5. **自递归：若先写 `audit.exported` AuditLog 再取水位，文件会包含自身并使重放 hash 不稳定。** 已冻结
   顺序为完整性校验 -> 捕获 sequence -> 生成 CSV -> 插入 AuditExport -> 写审计；AFTER trigger 给自身
   分配更大 sequence，因此本次文件不含自身，下一次才可查询该事实。
6. **租户边界：仅靠应用 join 不足以证明 snapshot ID/sequence 属于同租户。** AuditExport owner trigger
   必须把非零 sequence、AuditLog ID、边界时间三者与同租户映射逐项绑定；空租户只接受
   `snapshot_sequence=0` 且 ID/时间同时为 NULL。
7. **迁移兼容：序列表引用 AuditLog 后，未来重建 audit_logs 必须显式处理外部 trigger/FK。** 0049
   prerequisite/终态检查登记全部映射对象；0049 down 在无正式导出事实时先解除 sequence trigger/FK
   依赖再重建 0048 合同。有正式导出事实仍在任何 DDL 前失败关闭。
8. **前端 owner：Blob 已下载不代表迟到请求可以跨身份更新反馈。** 导出 ticket 同时冻结 tenant、user、
   role、session_id、session_revision、navigation 和 generation；每次读取响应头、创建 URL、触发下载与
   写反馈前都重新核对 owner，失配立即 abort/revoke，且不把 Blob 或筛选写入长期状态。
9. **CSV 安全：先判定公式再清理字符仍会被 NUL/U+200B 等不可见字符绕过。** 已改为复用 Usage 导出
   的成熟顺序：先归一全部 Unicode `C*` 类，再判定 `cleaned.lstrip()`，危险值把单引号置于最终 cell
   首位并复验；测试覆盖 NUL、TAB、CR/LF、U+200B、U+2060 和 ESC。
10. **资源边界：`fetchall()` 即使只返回 10001 行，也会先物化全部大摘要。** 已冻结为 cursor 逐行读取，
    `BytesIO + TextIOWrapper + csv.writer` 逐行写入并检查真实编码字节；行数或 16 MiB 任一先到即失败，
    不保留半份制品。
11. **可复核性：AuditExport 没有自身 AuditLog ID 时只能靠 request/action 猜测一一对应。** 已增加预生成
    `audit_log_id`、`audit_logs(tenant_id,audit_log_id)` 候选唯一键和复合 deferred FK；AuditLog owner
    trigger 反向绑定 export/actor/request/trace/audit ID，任一侧缺失、跨租户或错配都不能 commit。
12. **验收旁路：Application 跨项目会批量改写 Virtual Key.project_id，却没有 Key 审计。** 已纳入
    `projects/service.py`：更新前冻结受影响 Key 摘要，更新后逐 Key 写 `virtual_key.updated`，全部处于
    Application 同一事务，任一审计失败全部回滚；运行期 last_used/健康 lease 不冒充管理员修改。
13. **down 兼容：只 guard AuditExport 无法保护已订阅或已派发的 `audit.exported`。** down 前置门禁已扩展
    到 source outbox、审计、Webhook subscription/delivery/attempt/dead-letter 及未知 view/trigger/FK；
    任一存在都在第一项持久 DDL 前原子拒绝。
14. **资源生命周期：现有 `list_audit_logs` 没有 finally-close，改共享筛选时可能把泄漏复制到导出。**
    已把列表连接修复纳入本 PRD；list/export/outbox 在成功和全部异常路径显式关闭，Windows 句柄专项
    用数据库重命名/删除证明释放，而不是只检查 mock 调用次数。
15. **验收边界：管理员点击 Provider 测试会写健康观测，但不等于修改 Provider 配置。** 已明确
    `health_status/last_tested_at`、后台 probe/lease、Key last_used 不生成伪 `*.updated`；验收覆盖真正配置
    PATCH、Application 间接 Key 归属更新以及已有独立治理 action，未来测试命令审计需独立 action。
16. **FK 与语义绑定：单列延迟 FK 只能证明 AuditLog ID 存在，恶意 SQL 可引用另一租户或同租户普通
    日志。** 已改为
    `(tenant_id,audit_log_id)` 复合延迟 FK，序列映射也使用同一复合 parent key；trigger 是附加业务校验，
    不再承担唯一的租户证明。AuditExport INSERT 拒绝已存在 ID，AuditLog INSERT 对任何命中预留 ID 的
    action 都执行反向精确绑定，关闭同租户语义替换。
17. **读路径完整性：现有 DTO decoder 只检查 JSON 顶层类型，可能把未知 action、敏感键或伪造的
    changed_fields 当成合法事实。** 已要求列表与导出共用严格存量行 decoder，复演写入白名单并验证
    真实差异；安全历史 JSON 先解析再 canonicalize，列表/导出分别映射稳定错误，均失败关闭。
18. **GET 副作用：页面 15 秒自动刷新若误复用导出 helper 会周期性创建审计。** 已冻结自动刷新只调用
    list，导出仅绑定管理员显式点击，并加入 timer/装载/重渲染零 export 请求测试。

第二轮结论：稳定快照、租户/RBAC、复合幂等 owner、事务顺序、source lease 恢复、CSV 注入与内存边界、
AuditExport/AuditLog 一一绑定、Key 旁路、down guard 和前端生命周期风险均已关闭；可以进入迁移、
后端、前端和测试实现。

## 14. 编码门禁

当前门禁：已完成。设计初稿、两轮编码前自审及最终交叉复核均已完成并回写；实现没有偏离本文件
冻结的快照、复合 FK、source lease、租户、权限、错误、隐私、连接生命周期和测试合同。

## 15. 实现与验收结论

- 0049 已在开发库正式升级到 `49/0049`，`integrity_check=ok`、外键违规 0，19 个 PRD-058
  关键 trigger、稳定序列映射和 AuditLog/AuditExport 不可变探针通过。
- 首轮 Runtime 暴露的 pending-resume 身份缺口已修复：恢复路径从不可变 `audit_exports` 取回原始
  request ID，并在出网前绑定校验 tenant/event/export/actor/trace/timestamp/payload；错配失败关闭。
- 当前代码直接回归为受影响 Python `39 passed`、Console PRD-058 `15 passed`、Runtime 验收器
  合同 `9 passed`，中文注释和 Ruff 致命规则通过。
- 权威 Runtime、Browser 和 final audit 均为 `status=passed`；1280x720/390x844、三角色、异常、
  CSV 下载、Webhook HMAC、租户隔离、统一错误码/request ID 和隐私均有直接证据。
- 正式 artifact 为 `.tmp/prd058/artifacts/20260731T124500-r14`，58 个文件、manifest `56/56`、
  ReparsePoint/SQLite sidecar/数据库文件/敏感明文命中均为 0；final audit SHA-256 为
  `a555fa14edefc7540de4c9af9b4f4f126e932af5b83c70ea021995ff1024fef4`。
- 完整且如实分级的测试与运行证据见 `docs/testing/PRD-058-tests.md`；根级 Python 历史未被改写为
  一次全绿。

PRD-058 已达到可运行、可验收和可复核条件。按 Excel 两张核心表第 60 行重新识别，下一条未完成项
是 `PRD-059 - 细粒度角色权限矩阵`（`M1-企业POC / P1 / RBAC增强`）。

`all_108_prds_complete=false`。
