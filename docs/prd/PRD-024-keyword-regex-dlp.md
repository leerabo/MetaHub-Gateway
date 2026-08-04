# PRD-024 - 关键词/正则敏感词拦截

状态：已实现，已通过 PRD-024 聚焦测试与中文注释门禁。

## 实现结果

- 已新增 `dlp_rules` 与 `dlp_matches` 两张租户级业务表，并扩展 `request_logs` 的 DLP 摘要字段。
- 已提供 admin-only 控制面 API：`GET /dlp/rules`、`POST /dlp/rules`、`PATCH /dlp/rules/{rule_id}`。
- 已在数据面 Chat Completions 热路径接入基础 DLP，执行顺序为鉴权与路由之后、限流与预算之前；`block` 不调用上游，`log_only` 与 `allow` 放行并记录命中摘要。
- 已新增控制台“DLP 规则”页面，并在调用日志详情中展示 DLP 命中摘要和哈希，不展示命中文本明文。
- 已补充控制面、数据面、迁移、调用日志、前端与注释门禁测试；测试记录见 `docs/testing/PRD-024-tests.md`。

## 需求理解

PRD-024 属于 M0-W2/P0 的基础 DLP 能力，目标是在请求进入模型上游之前，用租户级关键词和正则规则拦截明显敏感词或违规意图。M0 只检查 OpenAI-compatible Chat Completions 请求中的用户输入 `messages` 文本，不扫描文件、图片、响应内容、Embeddings input、工具调用结果或多轮会话历史风险。

规则动作支持 `allow`、`log_only`、`block`：

- `allow`：命中后放行，并写入最小命中记录，用于灰度观察或显式低风险规则。
- `log_only`：命中后放行，写入命中记录和安全事件，用于试运行。
- `block`：命中后阻断，不调用上游 Provider，返回统一错误码，并写入失败 `request_logs` 和命中记录。

架构约束要求 DLP 命中日志只记录规则 ID 和匹配摘要哈希，普通日志不保存敏感明文。因此本 PRD 不把 Prompt、完整 messages、命中文本、Authorization、Virtual Key 明文或 Provider Key 写入数据库、日志或前端响应。

## 涉及模块

- `infra/migrations/`
  - 新增 `0016_dlp_rules.sql` 与 down 迁移。
  - 新增 `dlp_rules`、`dlp_matches` 表。
  - 给 `request_logs` 增加 DLP 命中摘要字段，便于日志列表和详情解释阻断原因。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加 DLP 规则校验、规则不存在、正则非法、正则不安全、网关 DLP 阻断和 DLP 不可用错误码。
- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
  - 增加 `gateway.dlp_blocked` 和 `gateway.dlp_unavailable` 的诊断建议。
- `shared-sdk/python/metahub_shared/runtime/safe_regex.py`
  - 统一提供 DLP 正则编译和安全检查，避免控制面保存校验与数据面运行校验口径分叉。
- `control-plane-api/src/metahub_control/dlp/`
  - 新增 DLP 规则 DTO、路由和服务。
  - `GET /dlp/rules` 列出当前租户 DLP 规则。
  - `POST /dlp/rules` 创建规则。
  - `PATCH /dlp/rules/{id}` 更新规则。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增 `require_dlp_admin`，M0 规则管理仅 admin 可用。
- `gateway-data-plane/src/metahub_gateway/dlp/`
  - 新增规则加载、用户消息文本抽取、关键词匹配、正则匹配、命中记录、请求日志上下文补充和阻断错误。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`
  - 在模型路由成功后、限流和预算检查前执行 DLP 检查。
  - DLP 阻断不消耗限流和预算，不调用上游 Provider。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`
  - `RequestLogContext` 增加 DLP 摘要字段，并写入 `request_logs`。
- `control-plane-api/src/metahub_control/request_logs/`
  - 日志列表和详情返回 DLP 摘要字段。
  - 日志详情额外返回 `dlp_matches`，只包含规则 ID、规则名称、动作、类型、摘要哈希和时间。
- `console-web/src/lib/dlp.js`
  - 新增 DLP 规则 API client。
- `console-web/src/features/dlp/renderDlpPanel.js`
  - 新增 DLP 规则页，admin 可管理规则。
- `console-web/src/features/request-logs/renderRequestLogPanel.js`
  - 日志详情新增 DLP 命中说明卡片。
- `console-web/src/features/developer-start/renderDeveloperStart.js`、`console-web/src/app/main.js`
  - 接入 DLP 导航、状态加载和表单提交。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-024-tests.md`
  - 更新能力边界、接口说明和测试记录。

## 数据表/字段设计

### 新表：`dlp_rules`

用途：保存当前租户的关键词或正则 DLP 规则。M0 不做项目、环境或部门维度，所有规则均为租户级。

字段：

- `dlp_rule_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `name TEXT NOT NULL`
- `rule_type TEXT NOT NULL CHECK (rule_type IN ('keyword', 'regex'))`
- `pattern TEXT NOT NULL`
- `pattern_hash TEXT NOT NULL`
- `action TEXT NOT NULL CHECK (action IN ('allow', 'log_only', 'block'))`
- `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))`
- `priority INTEGER NOT NULL DEFAULT 100`
- `description TEXT`
- `created_by TEXT`
- `updated_by TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `UNIQUE (tenant_id, pattern_hash, rule_type)`
- `idx_dlp_rules_tenant_status_priority(tenant_id, status, priority)`

安全说明：

- `pattern` 是管理员配置数据，可在规则管理页展示给 admin；但不能写入普通结构化日志。
- `pattern_hash` 用于排重和命中记录关联，不替代 `dlp_rule_id`。

### 新表：`dlp_matches`

用途：保存请求级 DLP 命中记录，供调用日志详情追溯。

字段：

- `dlp_match_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `request_id TEXT NOT NULL`
- `trace_id TEXT`
- `dlp_rule_id TEXT NOT NULL`
- `rule_name TEXT NOT NULL`
- `rule_type TEXT NOT NULL`
- `action TEXT NOT NULL`
- `message_index INTEGER`
- `match_text_hash TEXT NOT NULL`
- `match_length INTEGER NOT NULL DEFAULT 0`
- `created_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (dlp_rule_id) REFERENCES dlp_rules(dlp_rule_id)`
- `idx_dlp_matches_request(tenant_id, request_id)`
- `idx_dlp_matches_rule_date(tenant_id, dlp_rule_id, created_at)`

安全说明：

- 不保存命中文本、上下文、Prompt、messages 原文或 response。
- `match_text_hash` 使用稳定哈希，便于判断是否重复命中同类文本，但不能反推原文。

### 表变更：`request_logs`

新增字段：

- `dlp_status TEXT`：`clean`、`matched`、`blocked`、`error`。
- `dlp_rule_id TEXT`：第一条命中规则 ID。
- `dlp_action TEXT`：第一条命中规则动作。
- `dlp_match_count INTEGER`：本次请求命中规则数量。
- `dlp_reason_code TEXT`：例如 `dlp.keyword_matched`、`dlp.regex_matched`、`dlp.rule_load_failed`。

说明：

- `request_logs` 只保存摘要，完整命中列表走 `dlp_matches`。
- 阻断请求也必须写 `request_logs`，用于控制台按 `request_id` 排障。

## API设计

所有 DLP 管理 API 都必须带 `Authorization: Bearer <token>`，并从认证上下文恢复 `tenant_id`。请求体和查询参数不接受 `tenant_id`。

### `GET /dlp/rules`

权限：admin。

查询参数：

- `status`：`active`、`disabled`、`all`，默认 `all`。
- `rule_type`：`keyword`、`regex`，可选。
- `action`：`allow`、`log_only`、`block`，可选。

响应：

```json
{
  "dlp_rules": [
    {
      "dlp_rule_id": "dlp_...",
      "tenant_id": "tenant_...",
      "name": "生产敏感词",
      "rule_type": "keyword",
      "pattern": "示例敏感词",
      "pattern_hash": "sha256:...",
      "action": "block",
      "status": "active",
      "priority": 100,
      "description": "生产环境阻断",
      "created_at": "2026-07-09T10:00:00Z",
      "updated_at": "2026-07-09T10:00:00Z"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### `POST /dlp/rules`

权限：admin。

请求：

```json
{
  "name": "生产敏感词",
  "rule_type": "keyword",
  "pattern": "示例敏感词",
  "action": "block",
  "status": "active",
  "priority": 100,
  "description": "生产环境阻断"
}
```

响应：返回创建后的 `DlpRule`。

校验：

- `name` 必填，最大 80 字符。
- `pattern` 必填，最大 512 字符。
- `keyword` 规则按普通字符串匹配，不解释为正则。
- `regex` 规则必须编译成功，并通过安全检查。
- `priority` 范围 1-10000。

### `PATCH /dlp/rules/{id}`

权限：admin。

请求字段均可选，但至少提供一个可更新字段：

```json
{
  "name": "生产敏感词 v2",
  "pattern": "新的关键词",
  "action": "log_only",
  "status": "disabled",
  "priority": 200,
  "description": "试运行"
}
```

响应：返回更新后的 `DlpRule`。

### 调用日志扩展

`GET /request-logs` 列表返回：

- `dlp_status`
- `dlp_rule_id`
- `dlp_action`
- `dlp_match_count`
- `dlp_reason_code`

`GET /request-logs/{request_id}` 详情额外返回：

```json
{
  "dlp_matches": [
    {
      "dlp_match_id": "dlpm_...",
      "dlp_rule_id": "dlp_...",
      "rule_name": "生产敏感词",
      "rule_type": "keyword",
      "action": "block",
      "message_index": 0,
      "match_text_hash": "sha256:...",
      "match_length": 6,
      "created_at": "2026-07-09T10:01:00Z"
    }
  ]
}
```

## 前端页面/交互设计

### DLP 规则页

入口：控制台侧边栏新增“DLP 规则”，归类到“安全与治理”。M0 仅 admin 可见；developer/viewer 不展示入口，后端也会拒绝直接调用。

页面内容：

- 顶部摘要：规则总数、active 规则数、block 规则数、log_only 规则数。
- 创建/编辑表单：
  - 名称
  - 类型：关键词/正则
  - 规则内容
  - 动作：允许、仅记录、阻断
  - 状态：启用/停用
  - 优先级
  - 描述
- 规则表：
  - 名称、类型、动作、状态、优先级、规则内容、更新时间。
  - 正则规则用标签标识。
  - 所有文本 HTML 转义。
- 错误态：
  - 展示统一错误消息、`request_id` 和诊断建议。

交互约束：

- 不提供“测试输入原文保存”能力，避免把敏感样例存到控制台状态或事件。
- 创建/更新时不发送 `tenant_id`。
- 前端隐藏入口只是体验优化，后端必须做 admin 权限校验。

### 调用日志详情

在日志详情中新增 DLP 卡片：

- 无命中：显示 `clean` 摘要或不展示卡片。
- `log_only/allow`：显示命中规则 ID、动作、类型、摘要哈希和时间。
- `block`：突出显示本次请求已被 DLP 阻断，并显示 `gateway.dlp_blocked` 诊断。

## 错误码设计

控制面：

- `dlp.rule_invalid`：422，规则字段非法。
- `dlp.rule_not_found`：404，目标规则不存在或不属于当前租户。
- `dlp.regex_invalid`：422，正则无法编译。
- `dlp.regex_unsafe`：422，正则命中安全约束，如过长、嵌套量词、反向引用或高风险断言。
- `dlp.update_empty`：422，更新请求没有可变更字段。
- `dlp.status_unavailable`：503，DLP 规则表或控制面数据库暂不可读。

数据面：

- `gateway.dlp_blocked`：403，用户输入命中阻断规则。
- `gateway.dlp_unavailable`：503，DLP 规则读取或评估失败，采用 fail closed。

所有错误响应必须包含 `code`、`message`、`request_id`、`trace_id`、`suggestion` 和可选 `diagnosis`。

## 安全与权限约束

- 所有 DLP 规则和命中记录必须带 `tenant_id`。
- DLP 管理 API 仅 admin 可用；M0 不新增 security role。
- 数据面只扫描当前请求中的用户消息文本，不扫描 system、assistant、tool 消息，不扫描 response。
- DLP 匹配不记录 Prompt、messages 原文、命中文本、Provider Key、Authorization 或 Virtual Key 明文。
- `dlp_matches.match_text_hash` 只用于排障和重复判断，不作为明文替代。
- 正则规则必须在控制面保存前编译和安全检查；数据面再次防御式编译，失败时规则视为不可用并记录安全事件。
- 对单次请求的扫描文本长度做上限，超出部分不扫描并记录 `dlp.scan_truncated` 安全元数据；M0 默认上限 20000 字符。
- 对租户 active 规则数量做上限，M0 默认 100 条，避免热路径被规则膨胀拖慢。
- DLP 数据面评估失败 fail closed，返回 `gateway.dlp_unavailable`，避免安全策略静默失效。
- 控制台事件不得携带规则内容之外的用户输入样例；DLP 规则内容只在 admin 规则管理 API 和页面展示。

## 测试用例

### 迁移

1. 空库迁移到 `0016` 成功，创建 `dlp_rules`、`dlp_matches` 并扩展 `request_logs`。
2. 重复迁移幂等。
3. down 迁移删除 DLP 表和 request log DLP 字段，不影响 `budget_*` 表。

### 控制面 API

1. admin 创建 keyword 规则成功，响应包含 `pattern_hash` 和 `request_id`。
2. admin 创建 regex 规则成功，非法 regex 返回 `dlp.regex_invalid`。
3. 高风险 regex 返回 `dlp.regex_unsafe`。
4. admin 更新规则动作、状态和优先级成功。
5. 空 PATCH 返回 `dlp.update_empty`。
6. 不存在或跨租户规则返回 `dlp.rule_not_found`。
7. developer/viewer 调用 DLP 管理 API 返回 `auth.permission_denied`。
8. 请求体带 `tenant_id` 不会改变规则租户归属。

### 数据面

1. keyword + `block`：用户消息命中后返回 403 `gateway.dlp_blocked`，不调用上游，失败 `request_logs` 写入 DLP 摘要，`dlp_matches` 不含原文。
2. regex + `log_only`：命中后继续调用上游，写入 DLP 命中记录和 `security.dlp_matched` 事件。
3. `allow`：命中后继续调用上游，调用日志能看到规则 ID 和动作。
4. 只扫描 `role='user'` 消息；system 消息包含同一词不触发阻断。
5. 多条规则命中时按 `priority ASC, updated_at DESC` 选择阻断动作，日志记录命中数量。
6. 不同租户规则隔离，租户 A 规则不影响租户 B。
7. DLP 规则读取失败返回 `gateway.dlp_unavailable`，并尽量写失败 `request_logs`。
8. 长输入和高风险正则测试在可接受时间内返回，不出现明显 ReDoS。

### 前端

1. admin 可看到 DLP 规则页、创建表单和规则表。
2. developer/viewer 看不到 DLP 规则入口。
3. DLP 表单不发送 `tenant_id`。
4. 规则名称、描述、错误消息和正则内容均 HTML 转义。
5. 日志详情显示 DLP 命中卡片，不展示命中文本明文。

### 回归

1. `scripts/ci/check-comments.py` 必须通过。
2. PRD-024 聚焦后端测试、迁移测试、前端单测、控制台 lint 必须通过。
3. 后端全量 `pytest -q` 必须通过。

## 代码修改计划

1. 迁移：
   - 新增 `0016_dlp_rules.sql`、`0016_dlp_rules.down.sql`。
   - 更新迁移测试期望版本、表字段和回滚断言。
2. 共享错误：
   - 增加 DLP 控制面和数据面错误码。
   - 增加 DLP 阻断和不可用诊断。
3. 控制面权限：
   - 新增 `require_dlp_admin`。
4. 控制面 DLP 模块：
   - 新增 `dlp/models.py`。
   - 新增 `dlp/service.py`。
   - 新增 `dlp/routes.py`。
   - 注册到 `app.py`。
5. 数据面 DLP 模块：
   - 新增 `metahub_gateway/dlp/models.py`。
   - 新增 `metahub_gateway/dlp/service.py`。
   - 在 `openai_compatible.py` 路由成功后、限流预算前接入 DLP。
6. 调用日志：
   - `RequestLogContext` 和 `record_request_log` 支持 DLP 摘要字段。
   - 控制面 request log DTO/查询/渲染增加 DLP 信息。
7. 前端：
   - 新增 `console-web/src/lib/dlp.js`。
   - 新增 `console-web/src/features/dlp/renderDlpPanel.js`。
   - 更新 `renderDeveloperStart.js`、`main.js`、导航和 CSS。
   - 更新调用日志详情 DLP 命中卡片。
8. 测试：
   - `control-plane-api/tests/integration/test_dlp_rules.py`。
   - `gateway-data-plane/tests/unit/test_dlp_service.py`。
   - `gateway-data-plane/tests/integration/test_dlp_guard.py`。
   - `console-web/tests/unit/dlp.test.mjs`。
   - 更新迁移测试和 request log 测试。
9. 文档：
   - 更新 README 当前完成列表、DLP 功能说明和测试命令。
   - 更新 `docs/development/directory-map.md`。
   - 新增 `docs/testing/PRD-024-tests.md`。

## 自检一

- 是否只做 M0 范围：是，只做租户级关键词/正则、用户消息文本检查和三种动作。
- 是否避免敏感明文落日志：是，命中记录只保存规则 ID、动作、摘要哈希和长度。
- 是否后端权限覆盖：是，DLP 管理 API 仅 admin 后端校验。
- 是否失败请求可追踪：是，DLP 阻断和不可用都写 `request_logs` 摘要。
- 是否支持试运行：是，`log_only` 命中后放行并记录。

## 自检二

- 最大安全风险：正则 ReDoS。设计通过保存前安全检查、规则长度限制、扫描文本上限、规则数量上限和性能测试降低风险。
- 最大隐私风险：命中文本可能本身就是敏感内容。设计不保存命中文本，只保存哈希和长度。
- 最大体验风险：admin 需要知道规则是否误拦截。设计在日志详情展示规则 ID、动作和摘要哈希，但不展示原文；后续可做脱敏样例回放。
- 最大一致性风险：控制面规则更新后数据面读取延迟。M0 先按数据库实时读取，后续配置快照按架构升级。

## 设计审核一

- 结论：原设计满足 PRD 的关键词/正则、`allow/log_only/block`、日志可查规则 ID 和动作、仅检查用户消息文本要求。
- 调整：正则安全检查从数据面专属模块移动到 `shared-sdk/python/metahub_shared/runtime/safe_regex.py`，控制面保存和数据面运行共同复用，避免两个服务对“安全正则”的判断不一致。
- 保留：DLP 阻断放在限流和预算之前，避免安全阻断请求消耗限流/预算额度；鉴权和路由仍在 DLP 之前，保证失败日志有可信 tenant_id、Virtual Key 和模型上下文。

## 设计审核二

- 结论：数据边界满足架构要求，`dlp_matches` 不保存命中文本，只保存 `match_text_hash`、长度、规则 ID、动作和链路标识。
- 调整：`match_text_hash` 采用 `tenant_id + rule_id + match_text` 的稳定哈希输入，降低跨租户或跨规则字典反推风险。
- 保留：M0 不新增 security role，DLP 规则管理仅 admin；后续如引入安全负责人角色，再扩展 RBAC 字典和依赖。
