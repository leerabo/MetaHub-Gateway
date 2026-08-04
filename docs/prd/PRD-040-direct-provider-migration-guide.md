# PRD-040 - 从直连模型厂商迁移指南

状态：已实现，已完成两轮独立设计自审、专项测试、全量 CI、真实调用与桌面/移动浏览器验收。

## 需求理解

PRD-040 属于 `M0.5-W3/4`、`P0` 的开发者门户能力，目标是让中国境内企业客户把一个直接调用模型厂商或云端模型 Endpoint 的 OpenAI-compatible Demo，以最小代码改动切换到 MetaHub Gateway。权威 Excel 要求页面覆盖：

- 把模型厂商 `base_url` 替换为 MetaHub Gateway 的 `/v1` 地址。
- 把 Provider Key 替换为控制台签发的 Virtual Key。
- 把厂商模型 ID 替换为当前租户的模型别名。
- 解释直连错误与 MetaHub 统一错误码的差异。
- 提供可实际执行的回滚方式。
- 页面访问记录 `migration.guide_viewed`，且事件可按 `tenant_id`、用户和 `request_id` 追踪。
- 不扫描客户代码库，不上传源码，不自动改写项目，也不把 OpenRouter 或硅基流动写成默认模型厂家。

架构文档把迁移指南归入 Developer Portal，并明确 SDK/文档应保持 OpenAI-compatible 习惯。该能力只影响控制面和控制台，不进入 Gateway 数据面热路径。默认上游仍是智谱、通义、文心、火山、混元、DeepSeek 官方或云上服务，以及客户本地 OpenAI-compatible Endpoint；OpenRouter、硅基流动只作为竞品和迁移兼容对象。

本 PRD 的“API/动作=无”解释为：不新增迁移指南业务 API，也不提供自动迁移动作。页面复用现有 `POST /console/events` 记录访问事件；迁移内容由版本化前端常量生成，并使用当前控制台已有的公开 Gateway URL、认证租户和模型别名状态进行安全填充。

## 涉及模块

- `console-web/src/features/migration-guide/`
  - 新增迁移指南数据构造与渲染模块。
  - 展示准备检查、三项替换、Python/Node 前后代码、错误码差异、验证和回滚步骤。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 增加 `migration-guide` 导航内容分派。
- `console-web/src/app/main.js`
  - 在迁移指南完成首屏渲染后复用控制台事件客户端记录 `migration.guide_viewed`。
  - 使用唯一访问状态机避免侧栏点击、hash 直达和自动刷新双写；离开后再次进入视为新访问。
- `console-web/src/lib/consoleShell.js`
  - 沿用 `recordConsolePageView`，只传现有安全字段，不新增 API。
- `control-plane-api/src/metahub_control/console/`
  - 增加迁移指南导航定义。
  - 扩展现有导航响应，返回经过后端运行配置校验的公开 Gateway `/v1` 地址或阻断状态。
  - 扩展事件 DTO 支持 `migration.guide_viewed`，并校验事件类型、path、hash 和 payload 枚举必须与后端导航定义匹配。
- `control-plane-api/src/metahub_control/model_aliases/`
  - 在既有列表响应补充默认模型和 Provider 状态，供页面只列出当前 Environment 的 active 候选；不新增接口。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增 `console.event_unavailable`，并更新 `console.event_invalid` 建议。
- `shared-sdk/python/metahub_shared/db/migrate.py`
  - 迁移或回滚脚本失败时先 rollback，再记录失败或抛统一错误，避免提交显式事务中的半迁移状态。
- `infra/migrations/0029_migration_guide_event.*.sql`
  - 扩展 `console_page_events.event_type` CHECK。
- `control-plane-api/tests/integration/test_migration_guide.py`
  - 覆盖导航 RBAC、事件写入、租户隔离和伪造事件拒绝。
- `console-web/tests/unit/migrationGuide.test.mjs`
  - 覆盖内容完整性、安全转义、事件请求和响应式结构。
- `console-web/src/styles/main.css`
  - 增加工作型指南、代码对照、错误映射和 720px 响应式样式。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-040-tests.md`
  - 更新实现边界、目录和验收证据。

## 数据表/字段设计

### `MigrationGuide`

`MigrationGuide` 是版本化只读展示模型，不创建业务表，也不作为租户业务数据持久化。租户归属只记录在访问事件中；页面不复制或缓存 `tenant_id`。最小字段为：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 固定 `direct-provider-to-metahub` |
| `version` | string | 固定内容版本，首版为 `1.0.0` |
| `status` | enum | 固定 `active` |
| `gateway_base_url` | string/null | 现有导航响应返回的可信 `/v1` 地址；配置不可信时为 null |
| `gateway_base_url_status` | enum | `ready` 或 `configuration_required` |
| `environment_options` | array | 当前租户 active Environment 与可用 alias 的交集 |
| `selected_environment_id` | string/null | 用户显式选择；多候选时不静默默认 |
| `selected_model_alias` | string/null | 用户显式选择且与 Environment 一致的 active alias |
| `sections` | array | 受控迁移步骤、代码、错误映射和回滚清单 |

不设置 `created_at/updated_at`，因为对象不持久化；内容版本由代码和文档历史追踪。页面不接收 `tenant_id` 参数。若 Gateway URL 不可信，或尚未选择 Environment/alias，所有 MetaHub 代码复制按钮必须禁用，不能回退到 localhost 或跨环境首项。

### `console_page_events` 变更

迁移 `0029` 使用 SQLite `BEGIN IMMEDIATE` 包裹建新表、复制、替换和索引恢复，把事件约束从：

```sql
CHECK (event_type IN ('console.page_viewed', 'dashboard.viewed'))
```

扩展为：

```sql
CHECK (
  event_type IN (
    'console.page_viewed',
    'dashboard.viewed',
    'migration.guide_viewed'
  )
)
```

不新增字段和索引，继续使用：

- `event_id`：服务端生成 `cpe_...`。
- `tenant_id`：认证上下文恢复。
- `user_id/session_id`：认证上下文恢复。
- `event_type`：固定 `migration.guide_viewed`。
- `navigation_item_id/target_section`：固定 `migration-guide`。
- `path`：固定 `/#migration-guide`。
- `request_id/trace_id`：统一中间件产生或透传。
- `payload_json`：只允许 `source`、`hash`、`viewport_class` 等现有低风险标量。
- `created_at`：服务端 UTC 时间。

回滚 `0029` 同样使用 `BEGIN IMMEDIATE`，把 `migration.guide_viewed` 降级为 `console.page_viewed` 后复制到旧结构，保留访问轨迹，不删除历史事件。回滚后事件名称精度降低，但 tenant/user/request/path 均保留。迁移 runner 在 `executescript` 失败后必须先 `rollback()`，再记录失败状态；否则显式事务内的部分 DDL 可能被后续错误记录提交。

## API设计

### 不新增业务 API

迁移指南不新增 GET/POST 业务接口，不扫描代码库，也不上传或保存客户源码。

现有 `GET /console/navigation` 增加两个只读字段，不产生额外动作：

- `gateway_base_url: string | null`：由控制面 `METAHUB_GATEWAY_API_BASE_URL` 严格规范化。
- `gateway_base_url_status: ready | configuration_required`：非 dev 环境配置为 loopback、URL 含凭据/query/fragment、协议非 http/https 或路径不是空/`/v1` 时返回阻断状态和 null。

规范化必须幂等处理尾斜杠与已有 `/v1`。前端当前硬编码的 `http://localhost:8000` 不能作为迁移指南权威值；dev 环境可以显式使用 loopback，其他环境拿不到可信配置时必须禁用复制。

现有 `GET /model-aliases` 只补充 `default_model_status/default_provider_status` 字段。页面按用户显式选择的 active Environment 过滤 `alias.status=active`、`environment_status=active`、`default_model_status=active`、`default_provider_status=active` 的候选；不新增迁移动作，也不静默选择其他环境。

### 复用 `POST /console/events`

请求示例：

```json
{
  "event_type": "migration.guide_viewed",
  "navigation_item_id": "migration-guide",
  "target_section": "migration-guide",
  "path": "/#migration-guide",
  "payload": {
    "source": "sidebar",
    "hash": "#migration-guide",
    "viewport_class": "desktop"
  }
}
```

接口继续通过 Bearer Session 鉴权，不接收 `tenant_id`、`user_id` 或 `actor_id`。响应沿用：

```json
{
  "accepted": true,
  "event_id": "cpe_...",
  "request_id": "console_shell_...",
  "trace_id": "trc_..."
}
```

后端增加事件与导航的精确匹配：

- `overview` 只接受 `dashboard.viewed`。
- `migration-guide` 只接受 `migration.guide_viewed`。
- 其他当前可见入口只接受 `console.page_viewed`。

所有控制台事件还必须满足：`target_section=item.target_section`、`path=/#${item.id}`、`payload.hash=#${item.id}`；不保存 query string。`source` 只允许 `sidebar/direct_hash`，`viewport_class` 只允许 `mobile/desktop`，未知值、过长值或把 Key/Prompt 藏入合法字段都会返回 `console.event_invalid`。这样可以防止调用方把迁移事件伪造到其他页面，或让 viewer 越权写入不可见入口。

事件连接、INSERT 或 commit 失败时服务层 rollback 并抛 `console.event_unavailable`（503、retryable）；前端在指南已渲染后 fail-open，只显示一次非阻断告警和 `request_id`。

## 前端页面/交互设计

入口位于“开发者”导航组，顺序为“快速开始 → SDK 示例 → 迁移指南 → API 文档 → 调试台”，admin/developer 可见，viewer 不显示。

页面采用密集、工作型参考指南，不使用营销 Hero 或装饰性卡片。主要结构：

1. **适用范围**：首版“三项替换”保证只覆盖服务端 Python/Node 的 `/v1/chat/completions`，分别提供非流式和流式验证。`/v1/embeddings` 只给接口提示；Responses、Assistants、厂商专有 SDK/参数、浏览器直连不属于直接替换范围。
2. **迁移前检查**：确认上游 Endpoint 已由管理员注册、模型已进入目录、Environment/模型/Provider/模型别名 active，并准备与所选 Environment 一致的 Virtual Key。
3. **三项替换表**：并列展示直连值、MetaHub 值、改动位置和安全注意事项。
4. **代码前后对照**：Python 与 Node tabs；每种语言展示可直接运行的“直连模型厂商”和“切换 MetaHub”完整代码及安装命令，并提供独立复制按钮。两份代码只有配置三元组不同：Python 使用 `base_url`，Node 使用 `baseURL`；代码只使用环境变量名，不包含任何真实 Key，Node 明确仅限服务端。
5. **错误码差异**：按鉴权、限流、配额、模型、超时、上游不可用、预算和 DLP 分类，说明统一错误码与处理动作。
6. **验证清单**：执行非流式 Demo，通过 SDK `_request_id` 或响应头保存 `request_id`，到调用日志确认模型别名、Provider、tokens 和成本；流式 Node 使用 `.withResponse().request_id`，并验证握手后 SSE error 分支。
7. **回滚决策与步骤**：只有接入配置错误、兼容性回归或 MetaHub 技术故障允许在审批后回直连；DLP、预算、限流和明确安全策略阻断禁止自动绕过。部署使用 `LLM_ROUTE=direct|metahub` 一次选择完整配置对象，不能依次改写三个共享变量；切换前确认直连路径具备等价治理或获批紧急例外，执行 canary、流量排空和日志保留，稳定后再决定是否禁用 Virtual Key。
8. **边界提示**：不扫描代码库；不建议浏览器保存长期 Key；Provider Key 只进入控制面 KeyPool，不进入示例、事件或日志。

可运行 Demo 契约：

- Python 与仓库示例保持官方 `openai>=1.40,<2` 兼容范围；Node 锁定仓库已验证的 `openai@6.47.0` 和 Node.js 22+。
- 直连变量一一映射：`PROVIDER_BASE_URL` → `METAHUB_BASE_URL`，`PROVIDER_API_KEY` → `METAHUB_VIRTUAL_KEY`，`PROVIDER_MODEL` → `METAHUB_MODEL_ALIAS`。
- 同一份 messages、temperature、stream 和消费逻辑保持不变；厂商专有字段必须先移除或确认 Adapter 支持，不能宣称直接兼容。
- E2E 固定同一个本地 fake OpenAI-compatible Provider：先直连成功，再只替换配置三元组通过 MetaHub 成功，最后恢复原三元组验证回滚。

动态值规则：

- `gateway_base_url` 只使用后端导航响应的可信字段；`configuration_required` 时显示配置阻断，不回落 localhost。
- 用户先选择 active Environment，再选择同 `environment_id` 且 alias/默认模型/Provider 均 active 的模型别名；多候选不静默选第一项。
- 选择值执行 HTML/属性/JavaScript 字符串上下文转义；没有候选时显示跳转 Environment、模型别名和 Provider 健康页的操作提示。
- 页面不提供 Provider Key、Virtual Key 明文输入框。

访问事件使用唯一状态机：只有后端确认 active item 后，`非迁移页 → 迁移页` 才生成一个 visit generation；初始 hash 视为一次进入。侧栏点击不再单独写迁移事件，首屏渲染后的 tracker 是唯一 owner；`attempted/inFlight` 阻止并发和 15 秒刷新重放。非 401 失败本次 visit 不重试；离开、登出或 Session 变化后才能创建下一次 visit。

响应式要求：桌面采用两列前后对照；小于 720px 时单列堆叠。长命令和代码只在自身 `pre` 中横向滚动，文档根节点不得产生横向溢出。迁移指南使用专属 data 属性和独立 `activeMigrationLanguage`，不与 SDK tabs 共用状态；tabs 支持鼠标、方向键、Home/End、Enter/Space。四个代码块各自拥有唯一复制 target/反馈节点，并捕获 Clipboard API 拒绝。

## 错误码设计

新增一个控制台遥测错误码 `console.event_unavailable`；Demo 调用错误全部复用现有 Gateway 注册表。

### Demo 调用错误对照

| 直连厂商典型表现 | MetaHub HTTP / code | 可重试 | 责任人 | 处理动作 |
| --- | --- | --- | --- | --- |
| 客户 API Key 缺失或无效，401/403 | 401 `gateway.auth_missing/auth_invalid` | 否 | 开发者 | 使用同 Environment 的 active Virtual Key |
| 直连 Key 有效但 MetaHub KeyPool 中 Provider Key 无效，401/403 | 502 `gateway.upstream_auth_failed` | 否 | 管理员 | 检查 Provider Key 状态与模型权限，业务方不接触上游 Key |
| 厂商余额/配额不足，402 | 402 `gateway.upstream_quota_exceeded` | 否 | 管理员 | 检查上游余额、计费和项目配额 |
| 厂商模型不存在/无权限，404 | 404 `gateway.upstream_model_not_found` | 否 | 管理员 | 检查 `provider_model_id/upstream_model_name` |
| 厂商限流，429 | 429 `gateway.upstream_rate_limited` | 是 | 管理员/开发者 | 有限退避或启用其他健康 Key；当前实现不承诺 `Retry-After` |
| 厂商参数非法，400/422 | 502 `gateway.upstream_request_invalid` | 否 | 开发者 | 移除厂商专有参数并核对模型能力 |
| 厂商请求超时，408/504 | 504 `gateway.upstream_timeout` | 是 | 管理员/开发者 | 用 request_id 检查上游延迟和超时设置 |
| 厂商网络/5xx | 503/502 `gateway.upstream_unavailable/upstream_error` | 是 | 管理员 | 检查 Provider 健康、路由和 fallback 记录 |
| MetaHub 环境、alias、模型或 Provider 不可用 | 403/404/409/503 `gateway.environment_disabled/model_not_found/model_alias_disabled/model_disabled/provider_unhealthy` | 依 code | 管理员 | 修复控制面配置，不回填厂商模型 ID 到 alias 字段 |
| MetaHub 租户限流 | 429 `gateway.rate_limit_exceeded` | 是 | 管理员/开发者 | 降低速率或调整 Virtual Key 限额 |
| MetaHub 预算/DLP 阻断 | 402/403 `gateway.budget_exhausted/dlp_blocked` | 否 | 管理员 | 按治理流程处理，禁止自动回直连绕过策略 |

握手前流式失败使用普通 JSON 错误和相应 HTTP 状态；响应头发出后的失败保持 HTTP 200，在 SSE 中发送 `event: error`，可能包含 `gateway.upstream_stream_timeout`、`gateway.client_disconnected` 或 `gateway.upstream_unavailable`。客户端必须解析 SSE error，不能只看握手状态。

### 指南访问遥测错误

| 场景 | HTTP | 错误码 | 页面处理 |
| --- | --- | --- | --- |
| 未登录 | 401 | `auth.token_missing/token_invalid` | 清理会话并回登录页 |
| viewer 手写入口 | 422 | `console.navigation_invalid` | 后端拒绝 active item |
| event/nav/path/hash/payload 不匹配 | 422 | `console.event_invalid` | 不写事件，保留统一 request_id |
| 事件数据库不可用 | 503 | `console.event_unavailable` | 已渲染指南继续可读，只显示一次非阻断告警 |

所有 API 错误响应继续携带统一 `code/message/request_id/trace_id/suggestion`。错误映射测试直接读取 `ERROR_SPECS` 校验 HTTP 状态和 retryable，避免文档事实漂移；指南不保存厂商原始响应正文、Prompt 或 Key。

## 安全与权限约束

- 后端导航定义只向 admin/developer返回 `migration-guide`；viewer 手写 hash 不会得到合法 active item。
- `/console/events` 重新按认证用户可见导航校验，前端隐藏不作为权限依据。
- 事件 DTO 禁止额外字段；`tenant_id/user_id/session_id` 均从 Session 恢复。
- `migration.guide_viewed` 只能用于 `migration-guide`；path/hash 由导航 ID 规范化，禁止 query string 和跨入口伪造。
- payload 同时校验键和值的枚举；即使敏感内容藏在合法 `source/hash/viewport_class` 字段中也会被拒绝。
- 示例只出现 `PROVIDER_API_KEY`、`METAHUB_VIRTUAL_KEY` 等变量名，不包含可用密钥格式、真实 Header 或本地 `.env` 内容。
- 页面不读取文件系统、不接收仓库地址、不执行扫描、不上传源码。
- 回滚方案不得建议把 Provider Key 写进日志、浏览器或 MetaHub Virtual Key 字段；两套凭据必须隔离。DLP、预算、限流和安全策略阻断不得触发自动直连。
- 访问事件失败不能阻止用户阅读静态指南，但必须以统一错误与 request_id 形成可追踪状态。
- 本 PRD 不改变 Gateway 调用、用量、成本和失败日志链路；验收 Demo 仍必须通过现有 request log/usage ledger 证明可追踪。

## 测试用例

### 数据迁移

1. 全新数据库应用 `0001..0029`，`schema_migrations` 为 29 条。
2. 从 `0028` 升级保留已有 `console.page_viewed`、`dashboard.viewed`，并允许 `migration.guide_viewed`。
3. `0029` 回滚把迁移事件降级为 `console.page_viewed`，保留 tenant/user/request/path/payload。
4. 再次升级成功，三个既有索引完整，`PRAGMA foreign_key_check` 为空、`integrity_check=ok`，三类事件约束正确。
5. 注入复制/重命名中途失败，迁移 runner 先 rollback；原表事件总数和内容不变，临时表不成为唯一副本。
6. `BEGIN IMMEDIATE` 期间并发写入被安全串行或失败，不出现事件丢失、重复或半迁移结构。

### 后端主流程与权限

1. admin/developer 导航包含 `migration-guide`，顺序位于 SDK 示例和 API 文档之间。
2. viewer 导航不包含该入口，手写 `active_item_id=migration-guide` 返回统一导航错误。
3. developer 通过现有 `/console/events` 写入 `migration.guide_viewed`，响应含 event/request/trace ID。
4. 数据库事件 `tenant_id/user_id/session_id/navigation/path` 与认证上下文一致，不接受请求体覆盖。
5. 第二租户写入事件不会污染第一租户查询或对象归属。
6. `migration.guide_viewed` 配其他入口、`console.page_viewed` 配迁移入口、伪造 target/path/hash、query string、非法 source/viewport 均拒绝且不落库。
7. 把 Key、Prompt 或 Token 藏进合法字段值仍被拒绝。
8. 数据库连接、INSERT、commit 失败均 rollback 并返回 `console.event_unavailable` 与 request_id，不泄露 SQL、路径或凭据。
9. 导航响应在 dev 返回规范化可信 `/v1`；非 dev loopback、URL 凭据/query/fragment/非法路径返回 `configuration_required`。

### 前端内容与交互

1. 页面完整出现 base URL、Key、model alias、错误差异和回滚五个 Excel 必选主题。
2. Python/Node 前后代码可以复制；MetaHub 代码只使用 `METAHUB_VIRTUAL_KEY`，无真实 Key。
3. Gateway URL、Environment、模型别名和错误状态动态文本全部转义；不可信 URL 和跨环境 alias 禁用复制。
4. 多个候选要求用户显式选择 Environment 与 alias；缺失时显示配置提示，不把上游 model ID 当 alias。
5. 初始 hash、侧栏点击、并发刷新、失败刷新和离开重入五种路径分别验证：每个 visit 最多一次事件尝试。
6. 事件在指南渲染后发送；401 清理会话，非 401 只显示一次非阻断告警且不自动重试。
7. 专属 tabs 完成 ARIA 键盘交互；四个复制按钮逐一点击，Clipboard rejection 有独立安全反馈。
8. viewer 即使构造前端状态也不渲染迁移内容，后端事件仍拒绝。
9. 1280x720 与 390x844 实测根节点 `scrollWidth <= clientWidth`，无文本重叠或按钮越界。
10. 错误映射从 `ERROR_SPECS` 校验 HTTP/retryable；流式页面明确区分握手前 JSON 与握手后 SSE error。

### 端到端验收

1. 固定同一个本地 fake OpenAI-compatible Provider，先以直连三元组执行 Python/Node Demo 成功。
2. 只替换 `base_url/api_key/model` 三元组后，非流式和流式 MetaHub 请求均成功并取得 `request_id`。
3. request log 记录 alias、resolved model、Provider、成功/失败状态；usage ledger 记录 tokens 和成本。
4. 经允许回滚的技术故障场景按 `LLM_ROUTE` 原子恢复完整直连配置，确认同一 Demo 可再次执行；治理阻断场景明确禁止直连。
5. 浏览器进入指南页后数据库存在 `migration.guide_viewed`，且 payload 无 Key、Prompt、源码或响应正文。

## 代码修改计划

1. 修正迁移 runner 的异常 rollback；新增显式事务的 `0029_migration_guide_event` 正向/回滚迁移和原子性/往返测试。
2. 扩展控制台导航响应、事件 DTO、可信 URL 与 event/nav/path/payload 校验，新增 `console.event_unavailable` 和专用集成测试。
3. 扩展模型别名只读状态字段；新增迁移指南展示模型、显式 Environment/alias 选择、Python/Node tabs、四块复制和响应式样式。
4. 在主应用中实现 post-render 唯一访问 owner、visit generation、自动刷新去重、离开重置、401 和非阻断错误状态。
5. 增加前端内容、安全转义、事件客户端、键盘和开发者壳层测试。
6. 更新 README、目录说明和 PRD-040 测试记录。
7. 运行专项测试、迁移测试、全量 CI、Ruff、前端 lint 和中文注释门禁。
8. 启动服务，在桌面与移动端完成页面、复制、事件和真实 Demo 迁移/回滚验收。

## 设计自审 1：控制面、数据迁移与安全

第一轮独立审查判定初稿不可编码，发现：迁移 runner 在显式事务失败后可能提交半迁移；event/nav 配对不足以阻止 path/hash/query 和合法字段藏敏感值；数据库失败缺少正式 503 错误；前端硬编码 Gateway URL；错误映射状态不准确。

设计已修正为：

- runner 异常先 rollback，0029 正反脚本使用 `BEGIN IMMEDIATE`，增加失败注入、并发、计数、索引、外键和完整性测试。
- 后端集中校验 event/nav/target/path/hash/source/viewport，canonical path 不保存 query。
- 新增 `console.event_unavailable` 503，并对连接、INSERT、commit 回滚后映射。
- 现有导航响应从运行配置提供严格校验的 Gateway `/v1`，非 dev loopback 或不可信 URL 阻断复制。
- 错误对照以注册表和 Adapter 归一化为事实源，修正 409 等状态并补流式 SSE error。

结论：五个阻断项和三项重要问题均已进入设计与测试计划，控制面方向可以编码。

## 设计自审 2：迁移可执行性与前端交互

第二轮独立审查判定初稿不可编码，发现：未限定三项替换的 API/SDK 范围；alias 可能跨 Environment；E2E 没固定同源 Demo；回滚可能绕过治理；点击、hash 和刷新会双写；tabs/复制与现有 SDK 全局绑定冲突。

设计已修正为：

- 首版保证限定服务端 Python/Node Chat Completions，提供可运行 direct/metahub 非流式与流式代码和明确变量映射。
- 用户显式选择 active Environment 和同环境、模型、Provider 均 active 的 alias；多候选不静默默认。
- E2E 固定同一 fake Provider，直连 → 只换三元组经 MetaHub → 原子回滚直连。
- 回滚增加允许/禁止决策矩阵、审批、等价治理、canary、流量排空、日志和凭据隔离。
- post-render tracker 成为唯一事件 owner，使用 generation/attempted/inFlight，失败本次 visit 不重试。
- 使用迁移指南专属 tabs/状态/data 属性，四个复制反馈隔离并捕获 Clipboard rejection，补桌面/移动端真实交互验收。

结论：迁移内容、状态机和交互已形成可执行契约。两轮审查问题全部吸收，可以开始编码。

## 实现与验收结果

- 新增 admin/developer 可见的“迁移指南”入口；viewer 的手写入口和事件请求由后端可见导航校验拒绝。
- 导航响应提供经过后端规范化的 Gateway `/v1` 地址；模型别名响应增加默认模型和 Provider 状态，页面只允许显式选择同一 active Environment 下的可用别名。
- `migration.guide_viewed` 复用 `POST /console/events`，严格配对 event、navigation、target、path、hash、source 和 viewport；数据库不可用统一返回可重试 503。
- `0029_migration_guide_event` 已应用到运行库；迁移执行器在 SQL 异常后先 rollback，再记录失败状态，正反迁移均保留历史与索引。
- 页面实现 Python/Node 专属 ARIA tabs、四块独立复制、错误对照、SSE 边界、验证清单和治理安全回滚矩阵；不接收 Key，不扫描仓库。
- 后端相关 23 项集成测试、控制台 148 项、全仓 228 项 Python、33 项 Node 示例、Ruff、lint 和中文注释门禁全部通过。
- 同一个本地 OpenAI-compatible Provider 已完成 Python/Node 直连、只替换三元组经 MetaHub 的非流式/流式请求，以及审批允许场景的完整配置回滚直连。
- 4 条 MetaHub 验收请求均在 `request_logs` 成功，`usage_ledger` 均记录输入 4、输出 3、总计 7 tokens 和 0 CNY；两条流式记录状态为 `completed`。
- 浏览器首次 hash 访问和离开后侧栏重入各写一条事件，配置刷新未重复写入；事件路径均为 `/#migration-guide`，payload 仅含 hash、source 和 viewport_class。
- 1280x720 与 390x844 实测无根节点横向溢出，四个复制按钮、Python/Node Arrow 键切换和显式 Environment/alias 选择均通过。
