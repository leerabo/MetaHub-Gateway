# PRD-066 - 企业 Chat 演示入口

状态：**设计与两轮编码前自审、实现、0058、专项与必要分层回归、真实 HTTP/Provider、
应用内 Browser 双视口、同会话撤权/恢复、注销和运行清理均已通过；PRD-066 已完成。
该状态不代表全部 108 条 PRD 已完成。**

## 1. 权威来源与需求理解

### 1.1 PRD 身份

本设计已经交叉核对 Excel `01_PRD总表`、`03_M2-M3扩展交付` 以及系统架构文档，
不是仅凭 PRD 编号推断下一项。

| 字段 | 权威值 |
| --- | --- |
| PRD_ID | `PRD-066` |
| 里程碑 | `M2-企业增强` |
| 优先级 | `P2` |
| 能力层 | 扩展层 |
| 模块 | 企业 Chat |
| 名称 | 企业 Chat 演示入口 |
| 页面 | `PAGE-093 企业 Chat` |
| API | `POST /chat/sessions`、`POST /chat/sessions/{id}/messages` |
| 数据对象 | `ChatSession`、`ChatMessage` |
| 事件 | `chat.message_sent` |
| 主流程验收 | `TC-0131` |
| 异常/权限验收 | `TC-0132` |

### 1.2 用户目标

业务用户可以在控制台选择当前租户内可用的项目和模型别名，创建一个短生命周期会话，
发送非流式文本消息并查看回复。每次发送必须真实经过 MetaHub Gateway 的鉴权、环境状态、
策略快照、模型权限、路由、DLP、限流、预算、Provider Adapter、Fallback、缓存亲和、
RequestLog 和 Usage/成本链路。页面同时展示本次请求的 Token、人民币成本、策略结果、
Fallback、缓存状态和 `request_id`；`trace_id` 保留在 API 响应、RequestLog 和后端追踪链路中，
不进入页面治理摘要。

这里的“演示入口”不是另建一条直连 Provider 的捷径。它的价值是让企业用户在不接触
Virtual Key 明文的前提下体验已经配置好的治理能力，并用同一条调用记录解释成本、失败和安全决策。

### 1.3 验收口径

1. 所有 Chat 请求都经数据面完整治理流水线，不能复用会直接调用 Adapter 的控制面调试台；
2. 用户没有 `chat.use` 后端权限时，选项、建会话、发送、清空接口都返回统一 403；
3. 用户不能选择其他租户、已禁用或当前项目/环境不可执行的模型；
4. 浏览器永远拿不到 Virtual Key、Provider Key、内部服务 Token 或一次性 execution grant；
5. Prompt、Response 只存在于当前页面内存和本次网络调用栈，不进入数据库、日志、URL、
   Storage、Webhook、验收 JSON 或截图旁路文件；
6. 成功和失败请求都有 `request_id/trace_id`、RequestLog、用量/成本或明确的未计费失败状态；
7. 清空会话立即清除页面正文并终止后端 Session，不能复活旧内容或继续发送；
8. 桌面 `1280x720` 与移动端 `390x844` 均可完成创建、发送、查看治理摘要和清空流程，
   不出现重叠、裁切或横向溢出。

## 2. 产品边界与非目标

### 2.1 本期实现

- 新增 PAGE-093“企业 Chat”，复用现有控制台 Shell、登录态、导航和错误展示；
- 新增后端权限 `chat.use`，默认授予 `admin`、`developer` 和 `viewer`，实际授权仍逐请求从
  当前租户数据库权限矩阵读取；
- 新增安全选项目录 `GET /chat/options`；
- 新增、发送和清空会话接口；
- 新增 Control -> Gateway 的内部 Chat 执行协议，使用独立部署 Token 和短时单次 grant；
- 新增 tenant-scoped `ChatSession`、仅元数据 `ChatMessage` 和短时 execution grant；
- 新增 `chat.message_sent` 严格 Webhook 事件；
- 新增 0058 up/down migration、专项测试、真实 HTTP 和双视口浏览器验收。

### 2.2 明确不做

- 不做 ChatGPT 的完整替代，不做多会话历史、搜索、分享、导出、收藏、重命名或长期正文保存；
- 不做流式输出、附件、图片、工具调用、函数调用、联网搜索、语音或 MCP 主链路；
- 不允许用户输入 System 消息、任意 OpenAI 扩展字段、Provider 参数或 Endpoint；
- 不允许浏览器选择、读取或保存 Virtual Key；
- 不新增 OpenRouter 或硅基流动为默认上游。默认上游仍是智谱、通义、文心、火山、混元、
  DeepSeek 官方或云服务，以及客户本地 OpenAI-compatible Endpoint；
- 不引入硬件盒子、信创、复杂数据出境或跨租户集团会话；
- 不把 Prompt/Response 生成摘要或 hash 作为“脱敏后正文”落库，因为正文指纹仍可能成为
  可关联敏感数据。

## 3. 现状调查与关键差距

### 3.1 不能复用的现有入口

`console-web/src/lib/gatewayApi.js` 要求调用方持有 Virtual Key 明文，适用于开发者接入示例，
不适用于企业业务用户页面。把 Key 交给浏览器会扩大泄露面，并让普通业务用户绕过后端会话和
权限约束。

`POST /debug/chat-completions` 虽然会记录 RequestLog 和 Usage，但其 service 直接解析路由并
调用 Provider Adapter，源码明确注明不消费 Gateway Policy 快照。它不会经过完整的限流、预算、
DLP、Fallback、缓存亲和等数据面状态机，因此不能成为 PAGE-093 的执行链路。

### 3.2 可以复用的现有能力

- Gateway `POST /v1/chat/completions` 的 `_handle_gateway_request` 已包含完整治理顺序；
- `GatewayAuthContext` 可以表达 tenant、Virtual Key、环境、项目和应用可信身份；
- `validate_requested_model_authority` 已阻断不存在和已禁用别名/模型；
- RequestLog、UsageLedger、RoutingAttempt、Policy、Budget、DLP、Fallback 和 CacheAffinity
  已保存安全元数据，可用于生成本次治理摘要；
- Control 的 `AuthContext` 和数据库权限快照可用于后端 `chat.use`；
- 统一 FastAPI 中间件已经生成并返回 request_id/trace_id；
- Webhook 已有严格 payload 白名单、同步派发、重试、死信和重放模式，可直接复用而不新建 worker；
- Console 已有 navigation 后端目录、permission revision/fingerprint 和页面 owner 状态机模式。

### 3.3 必须补齐的能力

1. 当前没有企业 Chat 独立权限、导航和页面状态；
2. 当前没有浏览器不接触 Virtual Key 的 Gateway 调用协议；
3. 当前没有项目/环境/别名/Key 组合的后端安全目录；
4. 当前没有短时 ChatSession、消息并发 CAS、单次 grant 或幂等防重计费；
5. 当前没有只保存执行元数据的 ChatMessage；
6. 当前没有 `chat.message_sent` 持久事件和严格订阅合同；
7. 当前前端两套调试台已经共享 `gatewayDebugState`，PAGE-093 不能继续复用该可变 owner。

## 4. 总体架构

```text
PAGE-093（仅页面内存正文）
  │
  ├─ GET /chat/options ────────────────> Control：AuthContext + chat.use + tenant 目录
  ├─ POST /chat/sessions ──────────────> Control：冻结 project/env/alias/唯一 active Key
  │
  ├─ POST /chat/sessions/{id}/messages
  │      │
  │      ├─ Control 事务：owner/RBAC/Session CAS/幂等/创建 metadata message + 单次 grant
  │      ├─ Control server-side HTTP（独立内部 Token + grant，浏览器不可见）
  │      │       │
  │      │       └─ Gateway 内部入口：原子消费 grant + 实时复核全部 owner
  │      │                 │
  │      │                 └─ 进入与 /v1/chat/completions 相同的治理执行器
  │      │                       auth -> env -> policy -> alias -> route -> DLP
  │      │                       -> rate limit -> budget -> adapter -> fallback/cache
  │      │                       -> request_logs -> usage_ledger
  │      └─ Control 事务：只读治理元数据，终结 message/session，冻结稳定 event ID
  │
  └─ DELETE /chat/sessions/{id} ───────> Control：终态清空 + 撤销未消费 grant
```

### 4.1 可信边界

- Browser -> Control：现有登录 Session Bearer；身份、tenant、role 和权限只从 AuthContext 恢复；
- Control -> Gateway：独立 `METAHUB_INTERNAL_CHAT_TOKEN`，使用
  `X-MetaHub-Internal-Chat-Token`，不复用策略同步 Token；
- 单次调用：Control 生成至少 256 bit 随机 grant，数据库只保存 SHA-256；明文只存在于
  Control/Gateway 当前调用栈；
- Gateway -> Provider：继续使用服务端解密 Provider Key，页面和 Control Chat 响应永远不接触；
- Database：只保存租户、稳定资源 ID、状态、计数、金额、固定错误码和链路 ID；不保存正文。

### 4.2 Gateway 复用方式

公开 `/v1/chat/completions` 保持现有合同。实现时把完整治理主体整理为可传入“已认证调用上下文 +
已校验 payload”的内部函数；公开入口仍先走 Virtual Key Bearer 鉴权和 JSON 读取，内部入口则先走
内部 Token 与 grant 消费，生成同形 `GatewayAuthContext` 后调用同一治理主体。

禁止通过伪造 `Authorization: Bearer <virtual-key>`、从数据库解密 Virtual Key、内部 HTTP 回调公开入口、
复制一份简化状态机或直接调用 Adapter 来“复用”Gateway。内部入口的最终 RequestLog endpoint 使用
固定 `/internal/chat/completions`，但策略、路由、预算、用量及 Provider 调用逻辑必须与公开入口共用代码。

## 5. 涉及模块和所有权

| 模块 | 计划文件 | 职责 |
| --- | --- | --- |
| Shared RBAC | `shared-sdk/python/metahub_shared/rbac.py` | `chat.use`、catalog revision 3、角色上限和默认矩阵 |
| Shared Config | `shared-sdk/python/metahub_shared/runtime/config.py` | 独立内部 Chat Token、Gateway URL 校验和安全摘要屏蔽 |
| Shared Errors/i18n | `shared-sdk/python/metahub_shared/runtime/errors.py` 等 | 统一 Chat 与内部执行错误码及中英文展示 |
| Shared Webhook | `shared-sdk/python/metahub_shared/runtime/webhooks.py` | `chat.message_sent` 精确 payload/envelope 校验 |
| Control Chat | `control-plane-api/src/metahub_control/enterprise_chat/` | options、Session、message CAS、grant、Gateway 调用、元数据终结 |
| Control Auth | `control-plane-api/src/metahub_control/auth/dependencies.py` | `require_enterprise_chat_user`，只按实时 permission 放行 |
| Control App | `control-plane-api/src/metahub_control/app.py` | 注册 Chat router，不新增常驻 worker |
| Gateway Chat | `gateway-data-plane/src/metahub_gateway/enterprise_chat/` | 内部 Token、grant 原子消费和可信 GatewayAuthContext |
| Gateway Pipeline | `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 抽取公开/内部共用治理执行函数，不改变公开 API |
| Gateway App | `gateway-data-plane/src/metahub_gateway/app.py` | 注册内部 Chat router |
| Migration | `infra/migrations/0058_enterprise_chat.sql/.down.sql` | RBAC revision 3、三表、索引、owner/状态 trigger、down guard |
| Console Client | `console-web/src/lib/enterpriseChat.js` | 严格 Control API client，不访问 Gateway |
| Console State | `console-web/src/features/enterprise-chat/enterpriseChatPageState.js` | 页面正文、owner、generation、abort 和 in-flight 状态 |
| Console Render | `console-web/src/features/enterprise-chat/renderEnterpriseChatPanel.js` | PAGE-093 交互和治理摘要 |
| Console Orchestration | `console-web/src/app/main.js` | 导航加载、发送、清空、离页/降权清理 |
| Console Nav | `console-web/src/features/developer-start/renderDeveloperStart.js` 等 | `enterprise-chat` 导航与后端 permission 快照接线 |
| Tests/Docs | 对应 Python、Node、acceptance、docs | 主流程、异常、权限、租户、迁移、隐私和双视口验收 |

## 6. 数据模型和 0058 迁移

### 6.1 RBAC 目录 revision 3

新增：

| 字段 | 值 |
| --- | --- |
| code | `chat.use` |
| category | `chat` |
| name | `使用企业 Chat` |
| description | `使用受 Gateway 全链路治理且不长期保存正文的企业 Chat。` |
| risk_level | `write` |

产品目录从 50 项提升到 51 项，角色模板关系从 131 条提升到 134 条。默认及可授予角色只有
`admin/developer/viewer`；FinanceAdmin、SecurityAdmin、OpsAdmin 不因能查看成本、安全或运维信息
而自动获得发送模型请求的权限。

存量角色只有在“旧 hash、revision 和真实关系集合”都精确等于 revision 2 默认模板时，才自动加入
`chat.use` 并把角色 permission revision 增加 1。任何租户自定义 Developer/Viewer 矩阵都保持原样，
由管理员通过现有 RBAC API 显式授权。Admin 不可变矩阵随目录完整扩展。

规范新 hash：

| 角色 | revision 2 默认 hash | 加入 `chat.use` 后 hash |
| --- | --- | --- |
| admin | `1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73` | `af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef` |
| developer | `220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f` | `ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3` |
| viewer | `b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a` | `77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228` |

### 6.2 `chat_sessions`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `chat_session_id` | TEXT PK，`chs_` + 32 小写 hex | 页面会话 ID |
| `tenant_id` | TEXT NOT NULL | 第一 owner 条件 |
| `owner_user_id` | TEXT NOT NULL | 只能由创建用户继续操作 |
| `owner_auth_session_id` | TEXT NOT NULL | 创建时登录 Session；注销后不可复用 |
| `project_id` | TEXT NOT NULL | 选中的 active 项目 |
| `environment_id` | TEXT NOT NULL | 项目/Key 所属 active 环境 |
| `model_alias_id` | TEXT NOT NULL | 选中 active 别名的稳定 ID |
| `model_alias` | TEXT NOT NULL | 冻结请求时别名字符串，便于追踪 |
| `virtual_key_id` | TEXT NOT NULL | 后端唯一解析出的 active Key ID，不向页面返回 |
| `status` | `active/cleared/expired` | 会话终态 |
| `message_sequence` | INTEGER >= 0 | 成功占位次数的单调序列 |
| `state_revision` | INTEGER >= 1 | CAS 和迟到响应淘汰依据 |
| `expires_at` | UTC TEXT | 创建后最多 8 小时 |
| `cleared_at` | UTC TEXT NULL | 清空终态时间 |
| `created_at/updated_at` | UTC TEXT | 生命周期事实 |

复合外键分别连接 `(tenant,user)`、`(tenant,auth session)`、`(tenant,project)`、
`(tenant,environment)`、`(tenant,alias)` 和 `(tenant,virtual key)`。INSERT trigger 还要验证：
用户和登录 Session active 且一致；项目、环境、别名、Key active；Key 的 project/environment 与 Session
精确一致；别名属于相同环境；该项目/环境下恰好有一个 active Virtual Key。

兼容 0021 之前产生的合法记录：Key 或 Alias 的 `environment_id` 为 NULL 时，只能按同租户且
`environment` code 精确匹配到唯一 active Environment，再把解析出的真实 `environment_id` 冻结进
ChatSession；不能把 NULL 当作跨环境通配。`project_id IS NULL` 的旧 Virtual Key 不具备 PAGE-093 的项目
归属，options 必须排除，不能把它自动绑定到用户选择的项目。

当前 `sessions` 只有全局 `session_id` 主键；0058 只增加
`uq_sessions_tenant_session_prd066(tenant_id,session_id)` 作为复合 owner 父键，不换表、不改旧 Session
行为。其他父表优先复用现有 tenant-qualified 唯一索引；缺失时仅补必要复合唯一索引。

### 6.3 `chat_messages`

该表表达一次用户发送的执行元数据，不是正文消息仓库。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `chat_message_id` | TEXT PK，`chm_` + 32 hex | 执行对象 ID |
| `tenant_id` | TEXT NOT NULL | owner |
| `chat_session_id` | TEXT NOT NULL | 所属会话 |
| `owner_user_id` | TEXT NOT NULL | 创建用户快照 |
| `client_message_id` | TEXT NOT NULL，UUID | 页面每次点击生成的防重 ID |
| `message_sequence` | INTEGER >= 1 | 会话内单调顺序 |
| `gateway_request_id` | TEXT NOT NULL | Gateway request_logs 关联键 |
| `gateway_trace_id` | TEXT NOT NULL | 跨服务 trace |
| `status` | `in_flight/succeeded/failed` | 有限状态机 |
| `input_tokens/output_tokens/total_tokens` | INTEGER NULL | 成功或可确认 usage 时填写 |
| `cache_tokens` | INTEGER NULL | 安全缓存用量，不是正文 |
| `cost_cny_micros` | INTEGER NULL >= 0 | 避免浮点金额漂移 |
| `policy_decision` | 固定枚举/NULL | allow/deny/route_to 等安全摘要 |
| `policy_reason_code` | 固定 code/NULL | 不含策略正文 |
| `fallback_triggered` | INTEGER 0/1/NULL | 是否使用备用 |
| `cache_status` | `hit/miss/not_configured/unknown`/NULL | 仅由持久选择事实推导 |
| `error_code` | TEXT NULL | 统一固定错误码，不存自由正文 |
| `webhook_event_id` | TEXT NOT NULL，`evt_` + message ID | 稳定事件幂等键，不另建 source/outbox |
| `started_at/completed_at` | UTC TEXT | 执行时间 |

唯一约束：

- `(tenant_id,chat_session_id,client_message_id)` 防重复计费；
- `(tenant_id,chat_session_id,message_sequence)` 冻结顺序；
- `(tenant_id,gateway_request_id)` 保证一条 ChatMessage 对应一个 Gateway 调用；
- partial unique `(tenant_id,chat_session_id) WHERE status='in_flight'` 保证每会话最多一个发送。

表内不得出现 `content`、`messages_json`、`prompt_hash`、`response_json`、预览、自由错误正文、
Endpoint、Key 前缀或用户邮箱。

### 6.4 `chat_execution_grants`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `chat_execution_grant_id` | TEXT PK，`chg_` + 32 hex | grant 事实 ID |
| `tenant_id` | TEXT NOT NULL | owner |
| `grant_hash` | 64 位小写 SHA-256 UNIQUE | 只存高熵明文的不可逆摘要 |
| `chat_session_id/chat_message_id` | TEXT NOT NULL | 精确绑定调用 |
| `owner_user_id/owner_auth_session_id` | TEXT NOT NULL | 实时复核主体 |
| `project_id/environment_id/model_alias_id` | TEXT NOT NULL | 资源 owner |
| `virtual_key_id` | TEXT NOT NULL | 可信 GatewayAuthContext 来源 |
| `gateway_request_id/gateway_trace_id` | TEXT NOT NULL | 防止 grant 改绑链路 |
| `status` | `issued/consumed/revoked` | 单次状态机；过期由 `expires_at` 派生，不靠 worker 改状态 |
| `expires_at` | UTC TEXT，创建后 30 秒 | 只覆盖一次内部 HTTP |
| `issued_at/consumed_at/revoked_at` | UTC TEXT | 状态时间 |

消费使用 `BEGIN IMMEDIATE`：按 hash 加载后逐项复核，再执行带 `status='issued'` 和未过期条件的
CAS UPDATE；rowcount 必须为 1。任一失败都不得产生 Gateway 上游调用。消费前必须再次验证当前用户、
登录 Session、`chat.use`、ChatSession、项目、环境、别名、Virtual Key 仍 active 且 owner 一致。

### 6.5 事件持久事实边界

`chat.message_sent` 不新增第四张 source/outbox 表，也不新增常驻恢复 worker。ChatMessage 在创建时即
冻结唯一 `webhook_event_id=evt_<chat_message_id>`；终结事务提交后，Control 从同一 tenant-qualified
Message 和 Session 重建严格安全 payload，通过 FastAPI `BackgroundTasks` 的安全 wrapper 调用现有
`dispatch_webhook_event`，避免客户 Endpoint 延迟阻塞 Chat 响应。现有 Webhook delivery、attempt、
dead-letter 和重放表继续承担投递事实，不能在 Chat 域再复制一套状态机。

Webhook 派发失败不得回滚已经完成的 Gateway 调用或让页面误以为模型未计费；调用只记录固定
`webhook.delivery_unavailable` 安全日志并保留稳定 event ID。M2 演示入口不承诺进程在 Message 提交后、
派发调用前崩溃时补投，后续若产品要求 exactly-once source recovery，应作为独立 PRD 复用统一 outbox，
不能在本条中临时实现一个 Chat 专用 worker。

### 6.6 迁移边界

0058 up 必须：

1. 在首个持久 DDL 前验证 0057 是栈顶、核心表列/索引/trigger 精确匹配且完整性通过；
2. 按 0054 模式把 RBAC 目录原子提升为 revision 3；
3. 只扩展精确旧默认 Admin/Developer/Viewer，保留所有自定义矩阵；
4. 为既有 tenant 创建新 permission 投影，为未来 tenant 更新初始化 trigger；
5. 创建三张 Chat 表、复合索引、owner/状态/不可变 trigger；
6. 为 `sessions` 等实际缺失的父键补最小 tenant-qualified unique index，不重建无关表；
7. 提交前验证每租户 51 项权限、134 条角色模板、矩阵 hash、FK 和 integrity；
8. 更新 `schema_migrations` 为 0058。

0058 down 必须在 DDL 前失败关闭，如果存在：任何 Chat 数据、任何 `chat.message_sent` Endpoint 订阅、
Delivery/Attempt/DeadLetter、任何显式涉及 `chat.use` 的权限变更事件、自定义持有 `chat.use` 的矩阵、
未知依赖对象或后续迁移。只有 canonical 空状态才恢复 revision 2、50 项目录和旧默认矩阵；down 后再次
执行 `foreign_key_check` 与 `integrity_check`。

## 7. API 设计

所有公开接口先认证、再验证 `chat.use`、最后解析路径/query/body，避免无权用户用畸形输入探测资源。
响应统一带 `Cache-Control: no-store`，错误保持 `code/message/request_id/trace_id`。

### 7.1 `GET /chat/options`

用途：返回当前用户可创建 ChatSession 的安全目录。

`chat.use` 本身授权读取这份最小安全目录；业务用户不需要额外获得 `project.read`、`model.read` 或
`virtual_key.read`，否则默认 Viewer 无法使用本页面。该豁免只限项目/环境/别名的展示 ID、名称和
availability，不能复用 Chat options 接口读取 CRUD 详情、描述、Key 元数据或 Provider 配置。

请求：不接受 query；重复、未知 query 均为 `chat.request_invalid`。

成功响应示例：

```json
{
  "options_revision": "sha256-safe-catalog",
  "projects": [
    {
      "project_id": "prj_...",
      "name": "客服助手",
      "environment_id": "env_...",
      "environment_name": "生产",
      "availability": "ready"
    }
  ],
  "model_aliases": [
    {
      "model_alias_id": "mal_...",
      "alias": "enterprise-chat",
      "display_name": "企业问答模型",
      "environment_id": "env_...",
      "status": "active"
    }
  ],
  "limits": {
    "max_history_messages": 20,
    "max_message_chars": 8000,
    "max_history_bytes": 65536,
    "session_ttl_seconds": 28800
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

目录只返回 active 项目/环境/别名，且项目环境下 active Virtual Key 数量必须恰好为 1。多个 Key 时
返回 `availability=ambiguous_key` 但不返回 Key ID/前缀；零个 Key 时为 `key_unavailable`。前端禁用
不可用组合，后端建会话仍重新计算，不能相信 options_revision 或按钮状态。

当前数据模型没有“项目默认 Chat Key”字段，因此本期冻结唯一 active Key 的 fail-closed 契约：零把和
多把都不能执行，绝不按创建时间、名称或查询顺序静默取第一把。Virtual Key 是服务身份和计费/预算/
限流 owner，不是业务用户的交互选项；把 Key ID、前缀或列表下发浏览器既无法解决选择歧义，又会扩大
凭据关联信息暴露面。未来若需要多 Key，应先通过独立 PRD 增加项目级显式默认配置，再由后端选择。

### 7.2 `POST /chat/sessions`

认证：`chat.use`。

请求 exact-set：

```json
{
  "project_id": "prj_...",
  "environment_id": "env_...",
  "model_alias_id": "mal_..."
}
```

不接受 tenant、user、role、permission、Virtual Key、Provider、Endpoint、model alias 文本、Session
状态或 TTL。后端按可信 tenant 精确加载资源并要求：项目/环境/别名 active、owner 一致、别名可用于
该环境、存在且仅存在一把 active Project Key。创建时重新读取实时 `chat.use`。

本期“有权模型”的后端定义是：调用者持有实时 `chat.use`，别名属于当前 tenant 和所选 active 环境，
别名自身 active，且后续 Gateway `validate_requested_model_authority` 与完整策略/路由再次通过。现有系统
没有用户级模型 ACL，因此不能由角色名虚构第二套 allowlist；未来新增模型 ACL 时，options、建会话、
grant 消费和 Gateway 权威校验必须同时接入，任一层不通过即 fail closed。

成功返回安全 ID、展示标签、限制、过期时间和 revision，不返回 Virtual Key 或内部 grant。

### 7.3 `POST /chat/sessions/{chat_session_id}/messages`

认证：`chat.use`，且 Session 必须属于当前 tenant、当前 user 和当前登录 Session。

请求 exact-set：

```json
{
  "client_message_id": "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
  "history": [
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好，请问需要什么帮助？"},
    {"role": "user", "content": "请总结本月用量"}
  ]
}
```

约束：

- 只允许 `user/assistant` 纯文本，第一条必须 user，角色严格交替，最后一条必须 user；
- 1..20 条，每条 trim 后 1..8000 字符，总 UTF-8 字节数不超过 65536；
- Control 对 wire JSON 设置 512 KiB 硬上限：先校验唯一、规范的 `Content-Length`，再逐块累计
  实际流量并核对声明长度；即使客户端省略或伪造长度，也不能在业务上限校验前无界缓存正文；
- 任意层 JSON 重复键均固定 422，禁止代理、审计器和应用解析器对同一请求产生不同解释；
- 不接受 system、name、tool、attachment、temperature、max_tokens、stream、model、tenant 等字段；
- 正文不写日志或数据库；Pydantic/错误响应不得回显 value；
- `client_message_id` 使用 canonical UUID，同一 Session 重复值固定 409，绝不二次调用；
- Session 过期/清空、owner 不匹配、已有 in-flight 或资源实时失效均在 grant 前阻断。

成功响应只白名单投影：

```json
{
  "chat_session_id": "chs_...",
  "chat_message_id": "chm_...",
  "client_message_id": "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
  "assistant": {"role": "assistant", "content": "..."},
  "execution": {
    "request_id": "req_...",
    "trace_id": "trc_...",
    "input_tokens": 42,
    "output_tokens": 18,
    "total_tokens": 60,
    "cache_tokens": 0,
    "cost_cny": "0.001234",
    "policy": {"decision": "allow", "reason_code": "policy.default_allow"},
    "fallback_triggered": false,
    "cache_status": "not_configured"
  },
  "session_revision": 3
}
```

Gateway 返回的未知字段、Provider 名、内部模型 ID、Key、Endpoint、原始 error、tool calls 和非文本
content part 全部丢弃。assistant 正文只用于当前 HTTP 响应，不进入 Chat metadata 更新或事件。

Token、缓存 Token 和成本必须按当前 tenant+gateway request ID 聚合全部 `usage_ledger` 行，不能只取
最新一条，否则 Fallback 的主尝试成本会被遗漏。`cache_status` 规则冻结为：聚合 cache_tokens 大于 0
是 `hit`；存在 CacheAffinity selection 但 cache_tokens 为 0 是 `miss`；没有 selection 是
`not_configured`；usage 不能确定时为 `unknown`。金额从正式 `cost_cny` 用 Decimal 转为六位微元整数，
不使用 float，响应再格式化为固定小数文本。

若调用失败，Control 终结 message 和 Session CAS 后传播统一 Gateway/Chat 错误；页面可以显示固定错误
说明和 request_id。网络响应丢失后同一 client_message_id 不能重发，因为系统不能在不保存正文的前提下
重放 assistant 结果；页面应明确展示“为避免重复计费，生成新消息后重试”。

### 7.4 `DELETE /chat/sessions/{chat_session_id}`

用途：清空并终止当前会话。没有 body/query。

只有 owner user、owner auth Session 和 `chat.use` 可执行。后端把 active Session 原子转为 cleared、
revision +1，并撤销 issued grant。in-flight 时先对本地 Session 标记 clearing 还是立即 cleared 必须避免
与已经消费的 Gateway 调用产生错误成功响应；实现采用：存在 in-flight Message 时固定返回
`chat.session_busy`，页面不把浏览器 abort 当作服务端取消证明，收到终态后再清空。没有 in-flight 时在
同一事务撤销该 Session 全部 issued grant 并转 cleared。成功 204 后页面同步清除全部正文和安全摘要。

### 7.5 Gateway 内部入口

`POST /internal/chat/completions`

请求头：

- `X-MetaHub-Internal-Chat-Token`：独立服务身份；
- `X-Request-ID/X-Trace-ID`：必须与 grant 冻结值一致；
- `Content-Type: application/json`。

请求体只包含 grant 明文与同一有界 history。内部入口不加入公开 CORS allow-header，不接受 Cookie、
用户 Session、Virtual Key Bearer、tenant 或资源 ID。内部 Token 缺失/错误分别沿用
`auth.internal_token_required/invalid`；grant 对外只返回统一 `chat.execution_invalid`，不区分过期、重放、
跨租户或资源撤销，避免形成内部状态枚举。

内部入口采用与 Control 相同的 512 KiB wire 上限、声明/实际长度核对和重复键拒绝；这些检查必须在
内部 Token、Cookie/Bearer 冲突检查之后执行，既避免无界内存占用，也不让未认证调用者借正文错误探测合同。

Gateway 原子消费 grant 后构造可信 `GatewayAuthContext`，把 payload 固定为：

```json
{"model": "<grant绑定别名>", "messages": [...], "stream": false}
```

不能让 Control body 覆盖 model、stream 或 Key。进入共用治理函数后，任何阶段失败仍写已认证失败 RequestLog；
失败响应不携带 grant、Token、正文或底层数据库错误。

## 8. 前端页面与交互设计

### 8.1 导航和权限

后端 navigation 只有当前 permission 快照包含 `chat.use` 才下发 `enterprise-chat`。前端
`permissions.js` 只用于减少误点，不是授权边界。直接输入 `#enterprise-chat` 但无后端导航项时，不能预取
options、不能渲染受保护 DOM，并回到后端允许的安全入口。

### 8.2 页面结构

- 顶部紧凑工具栏：项目下拉、模型别名下拉、新建/清空图标按钮；
- 中间消息区：单列对话，不使用卡片嵌套；用户和助手气泡清晰但保持企业工具风格；
- 底部固定输入区：多行输入、发送图标按钮和明确 loading/disabled 状态；
- 每条成功 assistant 回复下方展示一行紧凑治理摘要：Token、CNY、Policy、Fallback、Cache、request ID；
- 首屏直接是可用 Chat 工具，不做营销 Hero 或功能说明文案；
- 清空使用确认对话框，确认后先等待后端 204，再清除 owner 状态；注销/离页/降权则无需确认立即清除。

### 8.3 独立状态机

新增 `enterpriseChatPageState`，不能复用 `gatewayDebugState`。状态包含：

- owner：tenant ID、user ID、auth Session ID、permission revision、authorization fingerprint；
- page generation、request generation、AbortController；
- options 安全目录、选中的 project/environment/alias；
- ChatSession ID/revision/expiry；
- 当前页面 messages 正文和对应安全 execution 摘要；
- draft、sending、clearing、error。

正文数组只能存在于该 JS 模块闭包内存，不进入 URL、hash query、LocalStorage、SessionStorage、IndexedDB、
Cache API、Service Worker、analytics、console 或访问事件。render 函数通过 `textContent` 投影，不允许
`innerHTML` 或 Markdown HTML。

### 8.4 owner 与迟到响应

以下任一事实漂移都 abort 并清空正文：注销、tenant/user/auth Session 变化、permission revision 或
authorization fingerprint 变化、后端导航撤销 `enterprise-chat`、离开页面、`pagehide`、Session 过期、
后端 401/403。

options/session/message 的 200、401、403、409、422、5xx 只有在 tenant/user/session/fingerprint、
page generation、request generation 和 ChatSession revision 全部匹配时才可生效。迟到响应一律丢弃，
尤其不能把上一租户 assistant 正文投影到当前页面。

现有 Console 周期性全页 refresh 不能销毁合法 draft、会话或在途请求；只有授权 owner 漂移才清理。

### 8.5 响应式约束

- 桌面布局使用固定最大内容宽度和 `minmax(0,1fr)`；工具栏不依赖 viewport 字号缩放；
- 移动端项目/模型选择换行，发送按钮保持稳定 40px 方形，不挤压 textarea；
- 长英文 request ID 使用 `overflow-wrap:anywhere`，治理摘要换行而非横向滚动；
- 消息正文保留换行、允许长单词换行，不与输入区或摘要重叠；
- 页面有界最大内存消息数 20，超限时要求清空或新建，不静默丢掉上下文。

## 9. `chat.message_sent` 事件设计

事件只在 ChatMessage 到达 `succeeded/failed` 终态并与 RequestLog 关联后派发。`event_id` 唯一冻结为
`evt_{chat_message_id}`，该值已随 Message 持久化；禁止临时生成第二个 ID 或按重试次数变化。

payload exact-set：

```json
{
  "schema_version": 1,
  "chat_session_id": "chs_...",
  "chat_message_id": "chm_...",
  "project_id": "prj_...",
  "environment_id": "env_...",
  "model_alias_id": "mal_...",
  "gateway_request_id": "req_...",
  "status": "succeeded",
  "input_tokens": 42,
  "output_tokens": 18,
  "total_tokens": 60,
  "cache_tokens": 0,
  "cost_cny_micros": 1234,
  "policy_decision": "allow",
  "policy_reason_code": "policy.default_allow",
  "fallback_triggered": false,
  "cache_status": "not_configured",
  "error_code": null,
  "completed_at": "2026-08-02T00:00:00Z"
}
```

成功事件要求 Token/成本为非负整数、`error_code=null`；失败事件允许无法确认的 Token/成本为 null，
但 `error_code` 必须是统一固定码。`policy_decision/reason_code` 在进入策略前失败时允许 null。BackgroundTask
只调用捕获全部异常的安全 wrapper，日志仅写 event ID、tenant、request/trace 和固定错误码，不能输出
Endpoint URL、异常正文或事件 payload。

信封 tenant、actor、object、trace、source_request 必须与 tenant-qualified Message/Session 逐字一致。payload 不含正文、正文 hash、
项目/模型展示名、Key、Provider、Endpoint、邮箱、完整 permission、自由错误正文或内部 Token。

## 10. 错误码设计

| code | HTTP | 场景 | retryable |
| --- | ---: | --- | --- |
| `auth.token_missing/invalid/expired` | 401 | 浏览器登录态无效 | 否 |
| `auth.permission_denied` | 403 | 缺少实时 `chat.use` | 否 |
| `auth.internal_token_required` | 401 | Gateway 内部身份缺失 | 否 |
| `auth.internal_token_invalid` | 403 | Gateway 内部身份错误 | 否 |
| `chat.request_invalid` | 422 | query/body/history/UUID 非法或含额外字段 | 否 |
| `chat.session_not_found` | 404 | Session 不存在或不属于当前 owner | 否 |
| `chat.session_expired` | 409 | Session 已过期 | 否 |
| `chat.session_cleared` | 409 | 已清空 Session 再使用 | 否 |
| `chat.session_busy` | 409 | 已有 in-flight 发送或清空竞争 | 是 |
| `chat.message_duplicate` | 409 | client_message_id 已使用，阻止重复计费 | 否 |
| `chat.project_unavailable` | 409 | 项目/环境禁用或归属变化 | 否 |
| `chat.model_unavailable` | 409 | 别名/模型禁用或环境不匹配 | 否 |
| `chat.virtual_key_unavailable` | 409 | 没有或存在多把 active Project Key | 否 |
| `chat.execution_invalid` | 403 | grant 过期、重放、改绑或实时授权失效 | 否 |
| `chat.gateway_unavailable` | 503 | 内部网络、数据库或 Gateway 暂不可用 | 是 |
| `chat.response_invalid` | 502 | 上游成功形状不含受支持文本回复 | 是 |
| 现有 `gateway.*`/`budget.*`/`policy.*`/`dlp.*` | 原状态 | 共用治理链拒绝 | 按目录 |

内部异常、SQL、数据库路径、配置 URL、Provider 正文和 payload 字段值不得拼入 message。所有错误 Header/body
中的 request_id/trace_id 必须一致；Control 调 Gateway 时沿用同一链路 ID。

## 11. 安全、权限和租户隔离

1. 所有公开 Chat 路由先通过 `require_enterprise_chat_user`，权限来自当前 tenant 的数据库矩阵；
2. Session、Message、Grant、Event 的每次 SELECT/UPDATE 都以 `tenant_id` 为第一条件，并叠加 owner user；
3. 客户端不能提交 tenant/user/role/permission/Key/Provider/Endpoint/alias 文本；
4. options 只是提示，建会话和发送必须实时重新验证资源与权限；
5. 多把 active Project Key 不能静默选第一条，也不向浏览器暴露 Key 列表，固定返回不可用；
6. grant 使用高熵随机值、只存 hash、30 秒 TTL、单次 CAS、逐字段 owner 复核；
7. `METAHUB_INTERNAL_CHAT_TOKEN` 与策略同步 Token、登录 secret、Key 加密 secret 分域；生产必须高熵，
   内部目标必须 HTTPS；dev/test 的 HTTP 只允许字面量 loopback；
8. Gateway 内部入口不信任 Control body 里的身份字段，不接受任何身份字段；
9. Prompt/Response 不进入普通日志、异常 traceback、数据库、Webhook、DOM 属性、URL、Storage 或证据文件；
10. 结构化日志只写 tenant、稳定对象 ID、request/trace、固定 event/error 和有界计数；
11. assistant 文本只通过 `textContent`，禁止富文本 HTML 注入；
12. 清空、离页、注销、降权和 owner 漂移必须同时 abort 网络与清内存，不能只隐藏 DOM；
13. 失败请求仍可追踪；Provider 已调用但响应丢失时保留 RequestLog/Usage，不通过重试伪装为未调用；
14. 事件 payload 先经 shared 通用敏感键扫描，再经 Chat exact-set validator；
15. 前端隐藏按钮不替代任何后端权限、租户或状态检查。

## 12. 并发、幂等和失败恢复

### 12.1 发送事务

准备事务：

1. `BEGIN IMMEDIATE` 加载 tenant+owner Session；
2. 实时读取 `chat.use` 和资源 active 状态；
3. 检查 Session active、未过期且不存在 in-flight Message；
4. 检查 client_message_id 未使用；
5. 分配 sequence/message/request/trace/grant；
6. 插入 in-flight Message 和 issued Grant；partial unique index 同时封锁并发第二条；
7. CAS 增加 Session message_sequence/state_revision；
8. commit 后才发内部 HTTP。

终结事务：

1. 以 tenant+session+message+in_flight 精确加载；
2. 从 RequestLog/Usage/Route/Cache 安全表读取本次元数据；
3. Message 转 succeeded/failed；
4. Session revision+1；Message 终态自然释放 partial unique in-flight 槽位；
5. 同事务提交；
6. 提交后把 Message 已冻结的 event ID 交给现有 Webhook 安全后台任务，派发失败不改写 Chat 终态。

任何终结写失败都不能删除 Gateway 已产生的治理事实或自动重发 Provider 请求。本期不新增后台
reconciliation worker；如果进程中断留下 in-flight，Session 在 8 小时 TTL 后自然失效，用户可新建
会话。清空或再次发送遇到超过内部请求最大时限两倍的旧 in-flight 时，只允许按同 tenant+request ID
读取已有 RequestLog 终态并释放 Session；无法证明终态时保持 busy，绝不猜测“未调用”后重试。

### 12.2 进程/网络故障

- Control 在 commit 前崩溃：没有内部调用；
- commit 后、发送前崩溃：grant 自然过期；旧 Session 最多保留到 TTL，不能自动重发；
- Gateway 消费前网络失败：grant 未消费，Control 不自动重试；到期后安全失败；
- Gateway 已消费、Provider 前失败：RequestLog 固定失败；正常响应路径终结 Message；
- Provider 已调用、响应丢失：RequestLog/Usage 是权威成本事实，页面不重放回复；
- Control 收到响应但终结事务失败：保留 Gateway 治理事实，不重放调用或回复；
- Webhook 网络失败：复用现有 delivery retry/dead-letter，不影响 Chat HTTP 已确定结果。

Control 在可确认 Gateway 尚未消费 grant 的连接失败中，把 Message 终结为固定
`chat.gateway_unavailable` 并撤销 issued grant；如果消费状态未知，则也不重试，只保留同一
gateway request ID 供 RequestLog/Usage 追踪。任何分支都不能因为页面重试而复用旧 client_message_id。

## 13. 兼容性、性能和运行配置

- 公开 `/v1/chat/completions` 请求/响应、错误和测试必须保持完全兼容；
- `/debug/chat-completions` 保留既有调试用途，但 PAGE-093 不引用；
- Chat 仅非流式，避免 M2 演示入口引入跨 Control/Gateway 双层 SSE 生命周期；
- 单 Session 最多 20 条页面消息、64 KiB UTF-8 历史、8 小时 TTL、一个 in-flight；
- 内部 HTTP 使用明确 connect/read/write/pool timeout，不能无界等待；
- Control 不创建全局共享正文缓存；httpx 客户端只复用连接池，request body 在调用结束后释放；
- SQL 只走 tenant+owner、tenant+request 和 tenant+status 索引；不新增 Chat 后台扫描；
- `METAHUB_INTERNAL_CHAT_TOKEN` 在本地开发有仅用于回环的显式 dev 默认值，prod 禁止默认值；
- 配置摘要只显示 masked/missing，不回显 Token 或 Gateway 内部 URL 凭据；
- 真实运行证据统一放在 `.artifacts/prd066-*`，不访问 `.tmp`，不申请沙箱外权限。

## 14. 测试用例

### 14.1 迁移和数据库约束

1. fresh 0058、0057 -> 0058、重复 migration manager、canonical down/re-up；
2. revision 3 恰好 51 项权限、134 条角色模板，每 tenant 完整投影；
3. 旧默认 Admin/Developer/Viewer 自动补权并 hash 正确，自定义矩阵不扩权；
4. Finance/Security/Ops 默认无 `chat.use`，Admin 不可变矩阵完整；
5. 新 tenant 自动得到 revision 3 投影和六角色正确默认矩阵；
6. 三表非法 ID、跨 tenant FK、owner 不匹配、状态倒退、重复 client ID/request ID/sequence 被拒绝；
7. Session/Message in-flight 复合约束、grant 单次状态、过期和不可变字段正确；
8. Message 的稳定 event ID 不能重复或更新，事件只能从终态 tenant-qualified Message 重建；
9. 有 Chat 数据、订阅/投递/死信、权限事件、自定义矩阵或后续迁移时 down guard 拒绝；
10. canonical 空库 down 保真 rowid、权限、角色 revision/hash、trigger/index；
11. `integrity_check=ok`、`foreign_key_check=[]`。

### 14.2 Control 主流程、异常和权限

1. 三个默认角色有 `chat.use`；撤权后 options/create/send/delete 都实时 403；
2. 第二租户同名 project/alias/session/client ID 不可见且计数不串租户；
3. options 只返回安全字段，零/一/多 active Key availability 正确；
4. create 精确绑定 project/env/alias/唯一 Key；跨 tenant、disabled、mismatch、额外字段失败；
5. send 正常返回 assistant 文本及治理摘要，数据库/日志无正文 canary；
6. system/tool/附件/空白/超长/过多/非交替/额外字段、重复键和超过 512 KiB 的 wire 正文全部 422，
   且不产生上游调用；无权请求仍先返回 401/403；
7. duplicate client ID、并发双发、过期、cleared、换登录 Session、换用户正确 404/409；
8. Policy deny、DLP block、budget exhaust、rate limit、disabled alias/Key/env 和 Provider 失败正确传播；
9. Gateway 网络超时、非法响应、终结写失败均不自动重发且保留可追踪 Gateway 事实；
10. 清空撤销 issued grant，in-flight 竞争不产生伪成功；
11. 所有错误有统一 request/trace，正文/Key/Token/Endpoint 不泄露。

### 14.3 Gateway 内部入口和完整治理证明

1. 内部 Token 缺失/错误、Cookie/Virtual Key Bearer 冒充均拒绝；
2. grant 明文不落库，hash 查询成功；过期、重复消费、改 request/trace、跨 owner 统一拒绝；
3. 消费时撤销 `chat.use`、Session clear、用户/登录 Session/项目/环境/别名/Key 禁用均阻断 Provider；
4. 公开和内部入口调用同一治理执行函数；源代码门禁禁止内部 route 调 Adapter；
5. 内部成功真实产生 Policy、DLP、RateLimit、Budget、RoutingAttempt、Fallback/Cache、RequestLog、Usage；
6. 公开 `/v1/chat/completions` 既有集成测试全部兼容；
7. 敏感 canary 不命中 Gateway/Control 日志、数据库、事件或错误。

### 14.4 Webhook

1. 成功和失败 message 均使用 Message 创建时冻结的唯一 event ID；
2. payload/envelope exact-set、tenant/actor/object/request/trace/time 逐字绑定；
3. Prompt、Response、content/messages/Key/Token/Endpoint/自由 error 任一字段被 shared validator 拒绝；
4. 订阅、签名、现有 delivery 重试、dead-letter 和重放使用同一 event_id，不新增 Chat worker；
5. 第二 tenant Endpoint 不接收第一 tenant 事件。

### 14.5 Console 状态机和页面

1. 无 `chat.use` 时无导航、无预取、无 DOM，直接 hash 安全回退；
2. options、创建、发送、治理摘要、清空正常；
3. 项目/模型切换使旧 Session/正文清空，不复用旧资源；
4. logout、pagehide、离页、换 tenant/user/session、permission revision/fingerprint 漂移清正文并 abort；
5. 迟到 options/session/message/clear 响应按完整 owner+generation+revision 淘汰；
6. 周期 refresh 不销毁同 owner draft、会话和 in-flight；
7. 401/403 清全部正文，409 只保留符合 owner 的安全错误状态，5xx 不伪造成功；
8. 文本用 textContent，无 innerHTML；URL/Storage/console/analytics 没有正文；
9. 1280x720 和 390x844 无溢出、重叠、裁切，长 request ID 和中文/英文正文安全换行。

### 14.6 实际运行验收

1. 在 `.artifacts/prd066-runtime/<run-id>` 创建隔离数据库并真实升级 0058；
2. 启动隔离 Gateway/Control/Console，健康检查 200/200/200；
3. 用真实登录 Session 完成 options/create/send/delete；
4. 使用 loopback mock Provider，但请求必须经过真实 Gateway 全治理路径，不伪造 Control 响应；
5. 实际撤销 `chat.use`、禁用 alias/Key、制造第二 tenant 和 duplicate/concurrency；
6. 浏览器完成桌面/移动流程，截图非空并执行像素、溢出和隐私扫描；
7. 扫描日志、数据库、Webhook、DOM、URL、Storage、Console 和证据，敏感 canary 命中为 0；
8. 最终关闭隔离服务/Profile，验证无活动 Session/grant、数据库完整、开发三服务恢复健康。

## 15. 代码修改计划

1. 完成本文两轮编码前自审并修正文档；
2. 新增 0058 up/down 和迁移专项测试，先冻结 RBAC/三表/owner/down 合同；
3. 更新 shared RBAC、配置、错误、i18n 和 Webhook validator；
4. 实现 Gateway 内部 Token、grant 消费和共用治理函数；
5. 实现 Control options/Session/message/clear、Gateway client、终结与现有 Webhook 派发复用；
6. 实现 Console 独立 API client、状态机、PAGE-093 renderer、导航和样式；
7. 先跑每个修改节点的定向测试，失败只复测相关节点；
8. 专项全绿后执行一次受影响组合回归和一次必要全量回归；
9. 执行 0058 实际 migration、真实 HTTP、双视口 Browser、隐私和健康验收；
10. 更新 README、目录说明、API 文档和 `docs/testing/PRD-066-tests.md`；
11. 运行 `scripts/ci/check-comments.py`，全部通过后才把 PRD-066 标记完成。

## 16. 编码前自审

### 16.1 第一轮：Gateway 旁路、租户、RBAC、资源绑定和敏感内容

状态：**发现问题，完成修订后通过**。

1. 初稿已明确排除 `/debug/chat-completions` 和浏览器直连 Gateway，但“复用完整治理链”仍可能被实现成
   内部 HTTP 伪造 Virtual Key。现冻结为公开/内部入口只共享同一治理执行函数：公开入口用 Bearer Key，
   内部入口必须独立 Token + 单次 grant 生成可信 `GatewayAuthContext`；禁止解密/伪造 Virtual Key、复制
   简化流水线或直接调用 Adapter。
2. 初稿提出项目/环境选择，却没有解释多把 active Key 的确定性。现冻结为项目环境下恰好一把 active
   Key 才可建会话，零把和多把均 fail closed；不按 SQL 顺序选第一把，也不把 Key ID/前缀/列表暴露给
   浏览器。后续多 Key 必须先有项目级显式默认配置，不能在本 PRD 隐式发明策略。
3. “无权模型”可能被误解为按角色名建立前端 allowlist。现明确当前权威合同是实时 `chat.use`、同租户
   active alias/environment 及 Gateway `validate_requested_model_authority`/Policy/Route；现有系统没有
   用户级模型 ACL，不能虚构第二套目录。未来 ACL 必须同时接入 options/create/grant/Gateway 四层。
4. 初稿四表和 Chat 专用 source worker 重复了现有 Webhook delivery/retry/dead-letter，扩大迁移和后台
   恢复范围。已删掉 `chat_message_events` 和专用 worker，改为三表；Message 创建时冻结 event ID，终态
   提交后复用 `dispatch_webhook_event`，派发失败不回滚已计费模型调用。
5. 复核所有身份入口：公开 API 不接受 tenant/user/role/permission/Key/Provider/Endpoint；Session、
   Message、Grant 查询以可信 tenant 为第一条件并叠加 owner user/auth Session；Gateway 消费时再次复核
   RBAC 和全部资源 active/owner。未发现剩余客户端可控租户边界。
6. 复核敏感数据生命周期：history/assistant 正文只存在页面闭包、Control/Gateway 当前调用栈和成功 HTTP
   响应；数据库、普通日志、事件、URL、Storage、配置摘要和验收证据都没有正文或正文 hash 字段。内部
   Token、grant、Virtual/Provider Key 均不进入响应或日志；grant 只持久化 256 bit 随机值的 SHA-256。

第一轮修订后，Gateway 必经链路、tenant、后端 RBAC、项目/环境/别名/Key 绑定和敏感信息边界闭合，
且删除了不影响 PRD 验收的独立事件恢复系统，可以进入第二轮设计自审。

### 16.2 第二轮：并发、幂等、失败恢复、事件、迁移回滚和前端 owner

状态：**发现问题，完成修订后通过，可以进入编码**。

1. 初稿使用 Session `send_state/in_flight_message_id`、Message `prepared` 和循环复合 FK，虽然可表达
   并发，但增加了写入顺序和 down migration 风险。现删除循环指针和 prepared 状态：Message 直接以
   in-flight 创建，partial unique index 保证每 Session 仅一条在途；Session 只维护单调 sequence/revision。
2. 初稿把 grant 过期设计为持久 `expired` 状态，需要扫描任务才能与时间同步。现只保留
   issued/consumed/revoked，过期由 canonical UTC `expires_at` 在消费条件中派生；没有 worker 也不会把
   已过期 grant 当成有效。清空只撤销仍 issued 的 grant。
3. 复核幂等与丢响应：`client_message_id`、Session sequence 和 Gateway request ID 三层唯一；重复 client
   ID 固定 409 且不调用上游。Provider 响应丢失时系统保留 RequestLog/Usage，但不保存或重放 assistant
   正文，也不自动重试，从而避免重复计费。旧 in-flight 只有能从同租户 RequestLog 证明终态时才释放。
4. 复核事件范围后继续压缩实现：不新增 source 表或恢复调度；Message 冻结 event ID，终态提交后通过
   安全 BackgroundTask 复用现有 Webhook delivery/retry/dead-letter。失败不回滚 Chat，wrapper 不记录
   Endpoint、payload 或异常正文。成功/失败 payload 的 null/整数状态合同已经补齐。
5. 复核 Usage/Fallback 后发现“取一条 usage”会漏掉主尝试成本。现冻结为按 tenant+request 聚合全部
   UsageLedger 行，并用 Decimal 计算微元；cache hit/miss/not_configured/unknown 也按 Usage 与
   CacheAffinity 持久事实推导，不采信 Provider 自由字段。
6. 复核 0021 兼容性：合法旧 Alias/Key 的 environment_id 可能为 NULL。现要求按同租户 environment code
   唯一解析后冻结真实 ID，NULL 绝不是跨环境通配；没有 project_id 的旧 Key 不适合项目 Chat，直接排除。
7. 复核权限最小化：默认 Viewer 没有 project/model/key read，但仍应使用 Chat。现明确 `chat.use` 只授权
   options 的最小展示目录，不授予 CRUD 详情或 Key/Provider 元数据，避免为了页面误扩三个广域读权限。
8. 复核迁移回滚：0058 只建三张 Chat 表和必要 tenant-qualified parent index；canonical 空状态可 down，
   真实 Chat/订阅/投递、显式权限事件、自定义 `chat.use` 矩阵、未知依赖或后续版本一律在 DDL 前拒绝。
9. 复核前端 owner：tenant/user/auth Session/permission revision/fingerprint、page/request generation 和
   Chat revision 必须全部匹配；离页、注销、降权和 pagehide abort 并清正文，周期 refresh 在 owner 不变时
   保留草稿/在途。迟到 assistant 响应无法跨租户或跨 Session 投影。
10. 测试范围保留主流程、异常、权限、租户、隐私、完整 Gateway 治理和双视口；局部失败只定向复测，
    专项全绿后执行一次受影响组合和一次必要全量回归，不因单点修改反复从头运行全部套件。
11. 编码期安全复核发现“先完整读取 body、后校验 64 KiB history”仍允许超大 wire JSON 占用无界内存，
    且标准 JSON 解析会静默接受重复键。现补充 512 KiB 原始流上限、规范 Content-Length/实际长度核对和
    任意层重复键拒绝；认证与实时权限依赖仍先执行，避免修复资源风险时倒退成未授权解析探针。

第二轮修订后，状态机、幂等、失败语义、轻量事件复用、迁移回滚、兼容性和前端 owner 均具有可直接
实现和验收的确定合同；未发现剩余编码前阻断项。

## 18. 实现后验收状态

- 兼容修复前的必要全量回归基线为 Python `1907 collected / 1886 passed / 21 excluded`；21 项均属于
  用户明确禁止执行的 PRD-050 静态证据文件。首轮 `1898` 项回归从未覆盖索引续跑，失败仅定向修复，
  没有因局部变更反复执行已经通过的前缀。
- 本轮只执行受影响范围：Python `69 passed`，Console PAGE-093/Browser `20 passed`，启动器专项
  `7 passed`；`py_compile`、相关静态检查和中文注释门禁通过。此前 Node 示例 `33 passed`、
  Console `594 passed`、Console lint `184 files` 的必要全量结果继续作为兼容修复前基线。
- 开发库正式迁移 CLI 幂等确认 `0001..0058`；迁移后 Gateway/Control/Console 为 `200/200/200`。
- 权威 Runtime 为 `.artifacts/prd066-runtime/run-20260802T0955-CST`：主流程、401/409/404/403、
  request/trace、租户、权限、Provider、RequestLog、Routing、Budget、Usage、成本、隐私和关闭终态通过。
- 应用内 Browser 已直接加载 `http://localhost:5173/#enterprise-chat`，完成 `1280x720` 与
  `390x844` 双视口、两次真实 Session/Message、治理摘要和清空；Provider 为 `2` 次成功、`0` 次非法，
  截图无溢出、重叠或敏感信息命中。
- 重新连接应用内 Browser 后完成同一已登录 Developer Session 的权限闭环：撤销 `chat.use` 并刷新时，
  企业 Chat 导航和受保护区域均消失，页面安全回落概览；恢复权限后 PAGE-093、项目/环境、模型选项
  与 `GET /chat/options 200` 均恢复；`POST /auth/logout 200` 后只保留登录页，受保护 DOM 和临时身份消失。
- 最终数据库状态为默认租户 Developer `chat.use=true`，active Session、在途消息、issued grant 和外键
  违规均为 `0`。故障注入停栈后 `8000/8001/11434` 全部释放，`5173` 保持运行；本轮临时 Provider Key
  为 disabled，`provider_key.created/provider_key.updated` 审计各 `1` 条且 active 临时 Key 为 `0`。
- 脱敏收口证据位于
  `.artifacts/prd066-browser-iab-20260802b/browser-closeout-evidence.json`；其中不包含账号、口令、Key、
  Token、Prompt、Response、tenant_id、user_id 或数据库路径。

完成判定：PRD-066 的设计、两轮自审、实现、迁移、测试、文档和实际运行验收全部通过，现标记完成；
总体目标仍在进行中，`all_108_prds_complete=false`。
