# PRD-034 管理操作审计日志设计

## 文档状态

- PRD：PRD-034 - 管理操作审计日志
- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：已实现，自动化与运行态验收通过
- 输入来源：`01_PRD总表`、`04_页面清单`、`05_API接口清单`、`06_数据对象`、`07_Webhook事件`、`08_验收用例`

## 需求理解

1. 系统必须持久记录控制面关键管理操作，MVP 验收至少覆盖 Provider、Provider Key/Virtual Key 状态、预算规则/阻断模式和 DLP 规则修改。“Key”按两类 Key 都覆盖，避免产品语义歧义造成审计缺口。
2. 每条记录必须包含当前租户、操作者、操作时间、动作、对象类型、对象 ID、变更前摘要、变更后摘要、请求 IP、User-Agent、`request_id` 和 `trace_id`。
3. `GET /audit-logs` 只查询当前认证租户的数据，并支持控制台需要的分页和安全筛选。
4. 当前 RBAC 只有 `admin/developer/viewer`，MVP 将“安全负责人”映射为 `admin`；只有 admin 能读取审计日志，后续独立安全角色不在本 PRD 扩展。
5. 不提供删除或修改审计日志的 API；数据库层同时禁止 `UPDATE/DELETE`，避免普通业务代码误删或篡改。
6. “不做 WORM 存证”表示本功能是业务数据库内的追加只读审计，不承诺独立介质、第三方时间戳、不可抵赖签名或监管级留存。
7. 仅成功提交的管理变更写入业务审计。参数校验失败、权限拒绝和业务事务回滚仍由统一访问/错误日志追踪，不能生成“已修改”的虚假审计事实。
8. 审计摘要必须使用允许字段清单生成，严禁将 Provider Key 明文、Token、Authorization、Cookie、DLP 原始命中内容、密码或任意请求体直接序列化入库。
9. 每条成功审计产生 `audit.recorded` 事件；外部 Webhook 在主事务提交后尽力投递，投递失败不能回滚已成功的管理变更和审计记录。

## 涉及模块

- `infra/migrations`：新增 `0024_audit_logs` 正向/回滚迁移。
- `control-plane-api/src/metahub_control/audit`：复用已有预留包，落地审计模型、查询路由、查询服务、写入辅助函数和事件投递，不创建平行包。
- `control-plane-api/src/metahub_control/providers`：Provider、Provider Key 创建/修改时写审计；验收重点是 Key 状态变化。
- `control-plane-api/src/metahub_control/virtual_keys`：Virtual Key 创建和状态修改时写审计，绝不记录明文 Key 或完整哈希。
- `control-plane-api/src/metahub_control/budgets`：预算规则 upsert、模式切换及状态变化时写审计。
- `control-plane-api/src/metahub_control/dlp`：DLP 规则创建/修改时写审计，摘要不保存原始 pattern。
- `control-plane-api/src/metahub_control/auth`：新增 admin 审计读取依赖。
- `control-plane-api/src/metahub_control/webhooks` 与 shared runtime：注册并投递 `audit.recorded`。
- `console-web/src/features/audit-logs`：审计日志列表、筛选、分页和安全详情摘要。
- `console-web/src/lib/auditLogs.js`：审计日志 API 客户端。
- `console-web/src/app/main.js`、控制台导航与样式：注册页面、加载状态和交互。
- `control-plane-api/tests`、`console-web/tests`：集成、迁移、权限、脱敏、不可变和页面测试。

## 数据表/字段设计

### `audit_logs`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `audit_log_id` | TEXT | PK | `aud_` 前缀随机 ID |
| `tenant_id` | TEXT | NOT NULL, FK | 所有查询的第一隔离条件 |
| `actor_user_id` | TEXT | NOT NULL | 已认证操作者快照 ID；INSERT 触发器校验当时归属，不用删除限制型外键影响审计留存 |
| `actor_email` | TEXT | NOT NULL | 操作时的账号快照，便于账号后续改名后追踪 |
| `actor_role` | TEXT | NOT NULL | 操作时的角色快照 |
| `action` | TEXT | NOT NULL | 稳定动作码，例如 `provider_key.updated` |
| `object_type` | TEXT | NOT NULL | `provider`、`provider_key`、`budget_rule`、`dlp_rule` |
| `object_id` | TEXT | NOT NULL | 当前租户内对象 ID |
| `object_name` | TEXT | NULL | 安全展示名或前缀，不保存秘密值 |
| `before_summary_json` | TEXT | NOT NULL | 允许字段构造的变更前 JSON 对象 |
| `after_summary_json` | TEXT | NOT NULL | 允许字段构造的变更后 JSON 对象 |
| `changed_fields_json` | TEXT | NOT NULL | 实际变化字段名数组，便于检索和页面展示 |
| `client_ip` | TEXT | NULL | 直接 TCP 对端地址；MVP 不信任任意 `X-Forwarded-For` |
| `user_agent` | TEXT | NULL | 规范化并截断后的 User-Agent |
| `request_id` | TEXT | NOT NULL | 对应控制面请求 ID |
| `trace_id` | TEXT | NOT NULL | 对应端到端 Trace ID |
| `created_at` | TEXT | NOT NULL | UTC 创建时间；审计行无 `updated_at` |

### 索引与约束

1. `idx_audit_logs_tenant_time (tenant_id, created_at DESC, audit_log_id DESC)`：默认时间倒序分页。
2. `idx_audit_logs_tenant_actor_time (tenant_id, actor_user_id, created_at DESC)`：按操作者筛选。
3. `idx_audit_logs_tenant_object_time (tenant_id, object_type, object_id, created_at DESC)`：对象追溯。
4. `idx_audit_logs_tenant_action_time (tenant_id, action, created_at DESC)`：动作筛选。
5. `idx_audit_logs_tenant_request (tenant_id, request_id)`：按请求链路定位。
6. `trg_audit_logs_owner_insert`：验证 `actor_user_id` 属于同一 `tenant_id`，防止跨租户拼接单列外键。
7. `trg_audit_logs_immutable_update`、`trg_audit_logs_immutable_delete`：对任何更新或删除执行 `RAISE(ABORT, 'audit_log_immutable')`。
8. 不对 `before/after` JSON 建表达式索引，MVP 只按固定元数据筛选，避免无界 JSON 搜索拖慢控制面。

### 摘要字段白名单

- Provider：`name`、`provider_code`、`provider_type`、脱敏后的 `base_url`、`status`、`priority`、`timeout_ms`；URL 必须去除 userinfo、query 和 fragment，不保存认证头或密钥。
- Provider Key：`provider_id`、`name`、`key_prefix`、`weight`、`status`、`last_used_at`；绝不保存 `encrypted_key`、指纹全值或明文。
- Virtual Key：`name`、`key_prefix`、`environment_id`、`project_id`、`application_id`、`status`、`expires_at`；绝不保存明文、完整哈希或 Authorization。
- BudgetRule：`environment_id`、`scope_type`、`scope_id`、`name`、`daily_budget_cny`、`warning_threshold_percent`、`exhausted_action`、`status`。
- DLP Rule：`name`、`rule_type`、`action`、`status`、`pattern_hash`；不保存 `pattern` 或命中文本。
- 所有摘要只接受显式构造的字典，审计公共函数不接收 Pydantic 请求对象或数据库整行自动序列化。

## API 设计

### `GET /audit-logs`

权限：`admin`。

查询参数：

- `actor_user_id`：可选，精确匹配当前租户操作者。
- `action`：可选，精确匹配稳定动作码。
- `object_type`：可选，限定支持的对象类型。
- `object_id`：可选，精确匹配对象 ID。
- `request_id`：可选，精确匹配链路。
- `created_from`、`created_to`：可选 ISO-8601 UTC 时间，闭区间起点、开区间终点。
- `limit`：默认 20，范围 1-100。
- `offset`：默认 0，最大值受统一分页约束。

成功响应：

```json
{
  "audit_logs": [
    {
      "audit_log_id": "aud_xxx",
      "tenant_id": "tenant_xxx",
      "actor_user_id": "user_xxx",
      "actor_email": "admin@example.com",
      "actor_role": "admin",
      "action": "provider_key.updated",
      "object_type": "provider_key",
      "object_id": "provider_key_xxx",
      "object_name": "生产 Key / sk-abcd",
      "before_summary": {"status": "active", "weight": 100},
      "after_summary": {"status": "disabled", "weight": 100},
      "changed_fields": ["status"],
      "client_ip": "127.0.0.1",
      "user_agent": "MetaHub Console/...",
      "request_id": "req_xxx",
      "trace_id": "trc_xxx",
      "created_at": "2026-07-13T08:00:00Z"
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0,
  "request_id": "req_query",
  "trace_id": "trc_query"
}
```

### 不提供的接口

- 不提供 `DELETE /audit-logs/{id}`、批量删除或清理 API。
- 不提供 `PATCH/PUT /audit-logs/{id}`。
- 对上述未注册路由，框架返回 404/405；数据库不可变触发器作为第二道防线。

### 内部写入接口

`record_audit_log(connection, context, *, action, object_type, object_id, object_name, before_summary, after_summary, request_meta)`：

1. 使用调用方现有数据库连接，在业务 `commit()` 前插入审计行。
2. 对摘要递归执行最终敏感键防线和 JSON 大小限制；敏感键使用“规范化后的完整键名/明确后缀”匹配，不能用 `key`、`token` 等宽泛子串误伤 `provider_key_id` 等安全标识。命中敏感键直接拒绝事务，不静默保存危险数据。
3. 计算排序后的 `changed_fields`，无实际变化时不产生审计记录。
4. 返回安全的 `AuditRecordedEvent`，调用方在成功提交后投递 Webhook。

## 前端页面/交互设计

### 页面入口

- 导航名称：`审计日志`，归入治理/安全分组。
- 仅 admin 接收该导航项并加载数据；developer/viewer 即使手写 hash 或 API 请求也会被后端拒绝。
- 首屏直接展示可扫描的日志表格，不使用营销 Hero 或卡片嵌套。

### 筛选区

- 时间范围：开始/结束时间输入。
- 动作：下拉菜单，选项来自前端已知稳定动作码。
- 对象类型：下拉菜单。
- 操作者、对象 ID、request_id：文本输入。
- `查询` 和 `重置` 命令按钮；筛选提交后 offset 归零。

### 列表与详情

- 桌面列：时间、操作者、动作、对象、变更字段、来源 IP、request_id。
- 单行展开详情显示 before/after 安全摘要、User-Agent 和 trace_id，不请求第二个详情接口。
- 长 ID 完整换行，不用省略号隐藏关键定位信息；request_id 可点击复制，复制按钮使用图标和 tooltip。
- 空状态、加载状态、统一错误状态均在当前页面原位显示；错误反馈包含安全错误码和 request_id。
- 移动端使用字段标签的纵向记录布局，保证时间、操作者、动作、对象和 request_id 不丢失，不出现横向溢出。
- 分页使用上一页/下一页图标按钮与当前范围文本，按钮有稳定宽高，加载时不引起布局跳动。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `audit_log.filter_invalid` | 422 | 动作、对象类型、ID 或筛选长度非法 |
| `audit_log.time_range_invalid` | 422 | 时间格式非法或结束时间不晚于开始时间 |
| `audit_log.pagination_invalid` | 422 | limit/offset 超界 |
| `audit_log.summary_invalid` | 500 | 内部调用尝试记录敏感键、非 JSON 值或超大摘要；事务回滚 |
| `auth.permission_denied` | 403 | developer/viewer 查询审计日志，复用现有 RBAC 错误码 |
| `audit_log.record_unavailable` | 503 | 审计写入或事务提交失败，业务同步回滚 |
| `audit_log.query_unavailable` | 503 | 审计查询或历史数据解析失败 |

所有错误沿用统一异常处理中间件，响应必须携带 `request_id` 和 `trace_id`。

## 安全与权限约束

1. 查询只从 `context.user.tenant_id` 取租户条件，不接受客户端 `tenant_id`。
2. 后端使用 `require_audit_log_reader` 强制 admin 权限，前端隐藏导航仅用于体验。
3. 业务变更与审计插入共用事务：审计插入失败则业务修改回滚；业务修改回滚则没有成功审计事实。
4. Webhook 是提交后的外部副作用，失败仅写安全错误日志和投递记录，不改变业务响应。
5. 数据库禁止审计行 `UPDATE/DELETE`，应用运行账号也不提供删除入口；迁移回滚仍可显式删除整表，因此不属于 WORM。
6. IP 只记录 ASGI 服务器看到的直接对端。部署在可信反向代理后，需要后续统一受信代理配置才解析转发头，MVP 不接受客户端自报 IP。
7. User-Agent 去除 CR/LF、NUL 和其他控制字符并限制 UTF-8 字节长度；页面按文本转义，不作为 HTML 注入。
8. `object_name`、before/after 字符串字段均限制长度，JSON 总大小设置上限，防止日志放大和控制台渲染攻击。
9. 敏感键匹配不区分大小写，并覆盖 `password`、`secret`、`access_token`、`refresh_token`、`authorization`、`cookie`、`plain_value`、`encrypted_key`、`provider_key`、`pattern`、`prompt`、`messages`、`response` 等完整键名；`provider_key_id`、`key_prefix`、`pattern_hash` 属于明确允许的安全字段。
10. DLP 仅保存不可逆 `pattern_hash` 和配置元数据；Provider Key 只保存 prefix、名称、权重和状态。
11. 审计查询不记录新的审计行，避免读取审计导致递归增长；本 PRD 只审计管理写操作。
12. `audit.recorded` payload 只含 audit ID、动作、对象和 changed_fields，不复制完整 before/after 摘要。审计行本身是同事务持久化的事件事实；MVP 沿用现有 Webhook 的提交后尽力投递语义，不宣称至少一次投递或事务 outbox 保证。

## 测试用例

### 主流程

1. admin 修改 Provider Key 或 Virtual Key 状态，业务状态和对应 `provider_key.updated` / `virtual_key.updated` 审计在同一事务提交。
2. admin 修改预算额度、状态或阻断模式，产生 `budget_rule.updated` 审计并列出实际变化字段。
3. admin 修改 DLP 规则，产生 `dlp_rule.updated` 审计，响应和数据库均不包含原始 pattern。
4. admin 使用动作、对象、操作者、request_id 和时间范围筛选，total、limit、offset 与排序正确。
5. 同一请求产生的审计记录包含一致的 request_id/trace_id、操作者、直接对端 IP 和截断后的 User-Agent。
6. 已订阅租户在事务提交后收到 `audit.recorded`，payload 不含 before/after 和敏感配置。

### 异常与权限

1. developer/viewer 调用 `GET /audit-logs` 返回 403 和统一错误体。
2. 跨租户 admin 不能通过 object_id、actor_user_id 或 request_id 查到其他租户记录。
3. 数据库直接 `UPDATE/DELETE audit_logs` 被不可变触发器拒绝。
4. 非法时间范围、未知对象类型、超长筛选、limit/offset 越界返回对应错误码和 request_id。
5. 业务校验失败或权限拒绝不产生成功修改审计。
6. 模拟审计 INSERT 失败，Provider Key/预算/DLP 业务修改一并回滚。
7. 模拟 Webhook 投递失败，业务修改和审计记录仍已提交，安全日志不包含 Endpoint secret 或响应正文。

### 脱敏与前端

1. 创建或修改 Provider Key/Virtual Key 后扫描审计 JSON，明文 Key、完整哈希、密文、Authorization 和 Token 均不存在。
2. 修改 DLP pattern 后扫描审计 JSON，原始正则/关键词和命中文本均不存在，只保留 hash。
3. 恶意对象名、User-Agent 和摘要文本在控制台均被转义，不能注入脚本或属性。
4. 桌面与 390px 窄屏均无文本重叠、截断关键信息或横向溢出。
5. 空状态、错误状态、翻页边界、重置筛选和重复快速点击均保持稳定。

## 代码修改计划

1. 新增 `0024_audit_logs.sql` 和 down 迁移，完成表、索引、跨租户归属和不可变触发器。
2. 新增 `audit_logs` 后端模块，先实现安全摘要校验/写入，再实现 admin 查询 API。
3. 将 Request 的直接对端 IP 和 User-Agent 以显式 `AuditRequestMeta` 传给 Provider、Virtual Key、预算、DLP 写服务。
4. 在四类业务服务的现有事务中，读取 before、完成 update、读取 after、插入审计，然后一次 commit；提交后投递 `audit.recorded`。
5. 注册错误码、路由、RBAC 依赖和 Webhook 事件类型。
6. 增加迁移、集成、事务回滚、权限、跨租户、脱敏、Webhook 失败测试。
7. 增加控制台 API 客户端、审计页面、导航、事件处理、响应式样式和前端单元测试。
8. 两轮独立代码审查后执行聚焦测试、控制面/前端全量测试、中文注释门禁、编译和桌面/窄屏视觉验收。
9. 更新 README 与 `docs/testing/PRD-034-tests.md`，记录最终测试数量、运行地址和已知边界。

## 设计自审

### 第一轮自审

- 状态：已完成
- 审查重点：事务原子性、敏感字段策略、查询边界、事件副作用和迁移可回滚性。
- 发现 1：若敏感键按 `key/token` 子串扫描，会误伤 `provider_key_id`、`key_prefix` 等安全定位字段。已改为完整键名/明确后缀拒绝规则，并为允许字段设置测试。
- 发现 2：现有服务多处直接 `commit()`，审计插入失败时不能依赖连接销毁隐式回滚。实现计划增加明确的 `try/except -> rollback -> raise`，业务更新、审计插入和相关本地事件必须原子提交。
- 发现 3：Webhook 属于外部副作用，不能放在数据库事务中。设计已固定为 commit 后投递，捕获所有投递异常并只记录安全元数据。
- 发现 4：审计表不可变触发器会阻止常规 DML，但 down 迁移仍需先删除触发器再删表。设计保持“追加只读但非 WORM”的准确表述。
- 发现 5：查询时间必须统一 UTC，`created_from` 为包含边界、`created_to` 为不包含边界；实现和测试需覆盖相同时间戳的稳定排序。
- 发现 6：`actor_user_id` 外键只能证明用户存在，不能证明与 `tenant_id` 同属一个租户。已要求 owner insert 触发器校验复合归属。
- 结论：以上问题均有明确设计修正，允许进入第二轮设计自审，仍不允许编码。

### 第二轮自审

- 状态：已完成
- 审查输入：后端接入点分析、前端复用分析、安全与测试威胁审查三份独立结论。
- 发现 1：仓库已有 `metahub_control.audit` 预留入口，初稿拟建 `audit_logs` 包会形成平行模块。已改为复用 `audit` 包。
- 发现 2：PRD 的“Key”存在 Provider Key/Virtual Key 歧义。为满足更严格验收，设计已覆盖两类 Key 的创建与状态修改。
- 发现 3：`actor_user_id` 若使用删除限制型外键，未来用户清理可能阻塞且不应破坏历史审计。改为保留快照 ID，并由 INSERT owner trigger 校验写入时租户归属。
- 发现 4：Provider `base_url` 可能携带 userinfo/query/fragment，不能原样进入摘要。已要求 URL 结构化清洗后再记录。
- 发现 5：现有权限错误码是 `auth.permission_denied`，初稿 `auth.forbidden` 会破坏统一错误语义。已修正。
- 发现 6：现有 Webhook 是同步提交后投递，不是事务 outbox。设计明确审计行是持久事件事实，Webhook 仅沿用尽力投递，不虚假承诺至少一次；可靠 outbox 留给后续通用事件基础设施。
- 发现 7：前端不能在其他页面后台预取敏感审计数据。设计补充只在 admin 且当前导航为 `audit-logs` 时加载，展开详情复用列表数据，不增加详情接口。
- 发现 8：空列表分页必须显示 `0-0 / 0`，移动端必须保留 data-label 和完整 ID；纳入前端测试及 1440/390 视觉验收。
- 发现 9：User-Agent 需按 UTF-8 字节长度限制并删除控制字符，不能只按字符数截断；已修正安全约束。
- 结论：两轮自审问题均已落实到设计和测试计划，设计门禁通过，可以开始编码。

## 实现后独立审查

- 后端审查发现 Virtual Key 限流修改未接入审计，现已纳入 `virtual_key.updated` 同事务审计，并覆盖回滚测试。
- Provider `auth_type` 以及 Provider、Virtual Key、DLP 的描述修改原本可能不产生审计；现对安全字段直接记录，对自由文本只记录 SHA-256 摘要。
- Uvicorn 默认代理头处理可能让本机代理伪造审计 IP；控制面入口现显式设置 `proxy_headers=False`，只采集直接对端合法 IP。
- 所有审计事务统一通过 `commit_audited_transaction` 提交，SQLite commit 故障映射为可重试 503，领域服务显式回滚。
- 稳定动作码及动作/对象组合在服务层校验，数据库同时限制动作白名单及三类 JSON 顶层结构。
- 前端审查发现中窄屏 `<details>` 收缩、移动表头覆盖、错误后无法重置、并发刷新竞态和越界分页问题；均已修复并增加回归测试。
- 代码、迁移、测试和前端关键分支均通过中文注释强制门禁，最终执行结果见 `docs/testing/PRD-034-tests.md`。
