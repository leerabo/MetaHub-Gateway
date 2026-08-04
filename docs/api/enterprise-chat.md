# 企业 Chat API

本文档描述 PRD-066 PAGE-093 及 PRD-067 模板调用扩展的稳定接口合同。企业 Chat 只提供受 Gateway 完整治理的演示入口，
不替代通用 ChatGPT 产品，也不长期保存 Prompt、Response 或完整对话。所有公开接口都从当前登录
Session 恢复 tenant、用户和权限；浏览器不能提交 tenant、角色、Virtual Key、Provider 或 Endpoint。

## 1. 认证、权限与隐私边界

- 公开 `/chat/*` 必须携带有效 Console Bearer Session，并实时具有 `chat.use`；匿名请求返回 `401`，
  撤权请求返回 `403`，认证和权限判断先于 query/JSON 解析。
- `tenant_id`、用户、登录 Session、项目、环境、模型别名、模板 current head 和唯一 Project Key
  均由后端解析并逐次复核。
- 所有成功及错误响应携带 `request_id`、`trace_id`，并使用 `Cache-Control: no-store`。
- Prompt 和 assistant Response 只存在于浏览器当前页面内存及本次 Control/Gateway 调用栈，不写入
  Chat 表、普通日志、Webhook、URL、Storage、配置摘要或验收证据。
- 每个 Session 最多 20 条消息、64 KiB UTF-8 history、8 小时有效期和一条在途 Message。

## 2. 读取可用目录

`GET /chat/options`

请求不接受 query。响应只返回当前 tenant 下可用于 Chat 的 active 项目、环境、模型别名和可用性，
不返回 Key ID、Key 前缀、Provider、Endpoint、内部模型 ID 或权限列表。项目环境下必须恰好存在一把
active Project Key 才标记为可用；零把或多把都失败关闭，不能按数据库顺序选择第一把。
实时具有 `prompt_template.use` 时响应额外包含 active/current 模板的最小调用投影；缺少该权限时
`prompt_templates=[]`，普通 Chat 项目和模型目录仍可使用。

```json
{
  "projects": [
    {
      "project_id": "prj_...",
      "name": "示例项目",
      "environments": [
        {
          "environment_id": "env_...",
          "code": "prod",
          "name": "生产",
          "available": true
        }
      ]
    }
  ],
  "model_aliases": [
    {
      "model_alias_id": "mal_...",
      "alias": "enterprise-chat",
      "environment_id": "env_...",
      "available": true
    }
  ],
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

Options 只是安全展示目录。创建 Session 和发送 Message 时仍会重新验证 `chat.use` 及全部资源状态，
不能把先前响应当成授权票据。

## 3. 创建 Chat Session

`POST /chat/sessions`

请求必须是 exact-set JSON：

```json
{
  "project_id": "prj_...",
  "environment_id": "env_...",
  "model_alias_id": "mal_..."
}
```

后端验证三个资源属于同一当前 tenant、均为 active、环境绑定一致，并且项目环境下恰好存在一把 active
Project Key。成功返回 `201`，Session 绑定当前用户和当前登录 Session；换用户、重新登录或切换 tenant
都不能继续使用旧 Session。

```json
{
  "session": {
    "chat_session_id": "chs_...",
    "project_id": "prj_...",
    "environment_id": "env_...",
    "model_alias_id": "mal_...",
    "model_alias": "enterprise-chat",
    "status": "active",
    "state_revision": 1,
    "expires_at": "2026-08-02T10:00:00Z"
  },
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

## 4. 发送 Message

`POST /chat/sessions/{chat_session_id}/messages`

请求必须是 exact-set JSON。`client_message_id` 是调用方生成的 UUID，用于阻止网络重试导致的重复计费；
同一 ID 绝不重放旧 assistant 正文。

```json
{
  "client_message_id": "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
  "history": [
    {"role": "user", "content": "请总结本月用量"}
  ]
}
```

History 必须从 `user` 开始、按 `user/assistant` 严格交替并以 `user` 结束。系统、工具、附件、空白正文、
额外字段、超长正文及超量历史一律拒绝。成功响应只投影 assistant 文本和正式治理事实：

选择模板时请求改用“完整历史对 + 精确模板字段对”：`prompt_template_version_id` 与
`template_variables` 必须同时出现且都非 null。此时 history 可以为空，但只能包含此前完整
user/assistant 对；最终 user 由 Gateway 在内存渲染并追加。两个字段只出现一个时固定返回
`chat.request_invalid`，绝不静默降级为无模板调用。

```json
{
  "chat_session_id": "chs_...",
  "chat_message_id": "chm_...",
  "client_message_id": "d4f07f3f-33f6-4ee4-922a-d48c6ec2bcb0",
  "assistant": {"role": "assistant", "content": "..."},
  "execution": {
    "request_id": "req_gateway",
    "trace_id": "trc_gateway",
    "input_tokens": 42,
    "output_tokens": 18,
    "total_tokens": 60,
    "cache_tokens": 0,
    "cost_cny": "0.001234",
    "policy": {"decision": "allow", "reason_code": "policy.default_allow"},
    "fallback_triggered": false,
    "cache_status": "not_configured"
  },
  "session_revision": 3,
  "request_id": "req_control",
  "trace_id": "trc_control"
}
```

Token 与成本按当前 tenant 和 Gateway request ID 聚合全部正式 `usage_ledger` 行；缺少正式 Usage 时
返回 `chat.response_invalid`，不能伪造零 Token 或零成本。`cache_status` 只根据最终成功 RoutingAttempt
及其持久 CacheAffinity/Usage 事实推导：`hit/miss/not_configured/unknown`，不采信 Provider 自由字段。

## 5. 清空 Session

`DELETE /chat/sessions/{chat_session_id}`

仅 owner 用户、owner 登录 Session 且实时具有 `chat.use` 时可以执行。无在途 Message 时，后端在同一事务
把 active Session 转为 `cleared` 并撤销全部 issued grant；成功返回 `204`。存在在途 Message 时返回
`chat.session_busy`，浏览器 abort 不能被当作 Provider 已取消的证明。

## 6. Gateway 内部入口

`POST /internal/chat/completions`

该接口只供 Control 调用，不加入公开 CORS。调用方必须提供：

- `X-MetaHub-Internal-Chat-Token`：与登录、Virtual Key、策略同步 Token 分域的独立服务身份；
- `X-Request-ID`、`X-Trace-ID`：必须逐字匹配 grant 冻结值；
- `Content-Type: application/json`。

正文只能包含一次性 grant 明文、有界 history 和模板模式下的变量 map。Gateway 只保存 grant SHA-256，执行时在单个事务中实时
复核 tenant、用户、登录 Session、`chat.use`、项目、环境、别名和唯一 Project Key，再原子消费 grant。
过期、重放、链路错配、owner 漂移或资源失效统一返回 `chat.execution_invalid`，避免形成状态枚举器。

内部入口生成可信 `GatewayAuthContext` 后调用公开入口复用的同一治理执行函数，因此 Policy、DLP、
RateLimit、Budget、Routing、Fallback、Cache、RequestLog 和 Usage 均不能旁路。它不接受 Cookie、
Bearer Virtual Key、tenant query 或正文身份字段，也不直接调用 Provider Adapter。

## 7. 错误合同

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing/invalid/expired` | Console Session 缺失、无效或过期 |
| 401 | `auth.internal_token_required` | Gateway 内部服务身份缺失 |
| 403 | `auth.permission_denied` | 当前用户实时缺少 `chat.use` |
| 403 | `auth.internal_token_invalid` | Gateway 内部服务身份错误或重复 |
| 403 | `chat.execution_invalid` | grant、链路、owner 或资源实时复核失败 |
| 404 | `chat.session_not_found` | Session 不存在或不属于当前 tenant/user/auth Session |
| 409 | `chat.session_expired` | Session 已过期 |
| 409 | `chat.session_cleared` | 已清空 Session 再使用 |
| 409 | `chat.session_busy` | 同 Session 已有在途 Message 或清空竞争 |
| 409 | `chat.message_duplicate` | `client_message_id` 已使用，阻止重复计费 |
| 409 | `chat.project_unavailable` | 项目、环境或唯一 Project Key 不可用 |
| 409 | `chat.model_unavailable` | 模型别名禁用、归属变化或环境不匹配 |
| 409 | `prompt_template.disabled/version_conflict/not_applicable` | Control 准备阶段模板状态、版本或项目适用性失败 |
| 422 | `chat.request_invalid` | query、JSON、history、UUID 或额外字段非法 |
| 422 | `prompt_template.variables_invalid` | 模板变量缺失、额外、类型或长度非法 |
| 502 | `chat.response_invalid` | Provider 成功形状或正式 Usage 事实不完整 |
| 503 | `chat.gateway_unavailable` | 内部网络、数据库或 Gateway 暂不可用 |

进入共用治理链后的既有 `policy.*`、`dlp.*`、`budget.*`、`gateway.*` 错误保持原 HTTP 语义。
所有错误均使用固定安全 message；SQL、数据库路径、Token、grant、Key、Endpoint、Prompt、Response 和
Provider 自由错误正文不得进入响应或普通日志。

## 8. Webhook 事件

Message 进入 `succeeded/failed` 终态且关联 Gateway RequestLog 后，Control 使用 Message 创建时冻结的
`evt_{chat_message_id}` 派发 `chat.message_sent`。Payload 是 exact-set 治理元数据，不含正文、正文 hash、
Provider、Key、Endpoint、邮箱或权限列表。失败事件必须包含固定 `error_code`；无法确认的 Token/成本使用
`null`，不能伪造为零。事件复用现有签名、delivery retry、dead-letter 和重放，派发失败不回滚已确定的
Chat HTTP 结果或已产生的 Usage。

## 9. 数据库版本

0058 把权限目录升级到 revision 3，新增 `chat.use`，并为 Admin、Developer、Viewer 默认授予该权限；
Finance、Security、Ops 默认不扩权。迁移新增 tenant-scoped `chat_sessions`、`chat_messages` 和
`chat_execution_grants`，使用 owner、状态单调、单 Session 单在途、稳定 event ID、grant 单次消费及
tenant-qualified 外键门禁。down migration 只允许 canonical 空 Chat 状态；存在 Chat 事实、相关 Webhook
状态、自定义矩阵、权限事件或后续迁移时在 DDL 前失败关闭。

0059 在该图上增加模板 Invocation owner 关系。模板调用准备由 Control 记录精确 template/version，
Gateway 在 grant 消费前再次实时复核 `prompt_template.use`、current/active 和项目适用性；晚到失败只映射
固定 `chat.*` 内部错误，不能把模板数据库或密钥细节穿透到数据面。
