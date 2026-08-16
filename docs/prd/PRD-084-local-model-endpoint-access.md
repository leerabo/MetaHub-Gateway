# PRD-084 本地模型 Endpoint 接入设计

状态：两轮编码前自审与两轮实现后自审均已完成并修正；全部 P0/P1 已关闭。

日期：2026-08-16

## 1. 权威范围与交付边界

### 1.1 交叉确认

本设计以工作簿、README、PRD-083 设计/测试记录和系统架构文档的交叉结果为准，不按编号猜测：

- `01_PRD总表!A85:R85` 与 `03_M2-M3扩展交付!A21:R21`：
  `PRD-084 | M2-企业增强 | P2 | 扩展层 | 本地模型 | 本地模型Endpoint接入`。
- `04_页面清单`：`PAGE-116 本地模型页`，面向管理员和研发。
- `05_API接口清单!API-383..388`：单元格共同描述 `POST /local-endpoints` 与
  `POST /local-endpoints/{id}/test`，不是六个互不相关的接口。PAGE-116 刷新和租户隔离验收需要增加支持性
  `GET /local-endpoints`，不扩大资源语义。
- `06_数据对象`：`OBJ-115 LocalModelEndpoint`。
- `07_Webhook事件`：`EVT-096 local_model.called`。
- `08_验收用例`：`TC-0167/0168`，覆盖 vLLM、Ollama、OpenAI-compatible Endpoint、调用主流程、
  权限、跨租户、安全与异常。
- `12_开发状态` 明确登记 PRD-084 为下一条尚未实现项；PRD-083 已在提交
  `0b7c5e63d70a2d683e18e12dee6de96a99e85bca` 完成，不重复实现。

### 1.2 产品解释

- 本项新增的持久 Provider 类型必须是字面量 `local_endpoint`。不能把旧
  `openai_compatible` 冒充为新类型，也不能修改已应用的 0005/0055 migration。
- `engine_kind` 仅用于 PAGE-116 展示和受控 preset，固定为
  `openai_compatible|vllm|ollama`；三者实际调用均使用 OpenAI-compatible `/v1` 协议。
- Ollama 只支持其 OpenAI-compatible `/v1/models`、`/v1/chat/completions` 和 `/v1/embeddings`；
  不实现 `/api/chat` 等私有协议。
- 本项不管理模型训练、权重、GPU 调度、模型文件、任意脚本或容器生命周期。
- OpenRouter、硅基流动仍只是竞品与迁移兼容对象，不加入本地模型或默认上游目录。
- `all_108_prds_complete=false` 保持不变。

## 2. 现状与关键风险

现有 Provider、Model、Provider Health 和 Gateway 已具备大部分基础能力：租户隔离、后端 RBAC、KeyPool 加密、
模型目录、健康租约/generation、DLP、意图与会话安全、限流、预算、配额、路由、RequestLog、Usage/成本结算和
输出安全。PRD-084 必须作为这些能力的聚合门面进入现有链路，不能新增直连调用器。

仍有四个必须关闭的缺口：

1. `providers.provider_type` 的 0005 CHECK 只有九类，无法写入 `local_endpoint`。
2. Shared Adapter 目录冻结为 `adapter-spec-v1` 九类，新增类型必须发布 v2，不能静默改写 v1 身份。
3. Provider Health 已有 DNS 全地址校验、固定 IP、禁代理和私网部署 allowlist；真实 Gateway 推理目前只有
   `trust_env=false`，对本地 Endpoint 仍存在 SSRF/DNS rebinding 风险。
4. 通用 Provider/Model 页面不能原子完成 Endpoint、初始模型、可选凭据和本地扩展元数据创建，也没有
   `local_model.called` 的租户级不可变来源事实。

此外，新增 Shared spec 后，通用 `POST /providers`、`PATCH /providers/{id}` 和配置导入会自然接受
`local_endpoint`。这些入口若不显式关闭，会生成没有 LocalModelEndpoint owner 的孤立 Provider，构成聚合和事件旁路。

## 3. 总体方案

### 3.1 聚合而不旁路

`LocalModelEndpoint` 是现有 Provider、Model 和 Credential 的租户聚合门面：

```text
LocalModelEndpoint -> Provider(provider_type=local_endpoint)
                   -> initial Model
                   -> optional encrypted Provider Credential
                   -> discovered catalog entries
```

Gateway 仍按以下既有顺序执行：认证与租户 -> Policy -> DLP -> Intent/Session Safety -> Rate Limit ->
Budget/Quota -> Routing Attempt Gate -> Provider Adapter -> RequestLog -> Usage/Cost -> Output Safety。

DLP、预算或权限在 Provider attempt 之前阻断时，不生成 `local_model.called`。本地 Endpoint DNS/ACL preflight
发生在 attempt gate 之后以保证失败请求可追踪，但固定标记为 `execution_status=not_called`，同样不生成 called 事件；
只有通过 preflight 并开始 connect/send 的成功、上游失败、超时、连接中断或请求取消才形成 called 事实。

### 3.2 Adapter 目录 v2

- Shared SDK 发布 `adapter-spec-v2`，保留原九类内容并新增第十类 `local_endpoint`。
- `local_endpoint` 固定使用 `openai_compatible` Adapter `1.0.0`、OpenAI chat protocol、
  `none|bearer`、provider model ID、OpenAI models probe、active 和 local-compatible 分类。
- 新 fixture 必须显式登记 `prd084_local_endpoint_minimum_contract`；exact-set 门禁不得从 spec 动态生成 fixture。
- 0077 将每个租户当前 v1 activation 原子 supersede，并写入 v2 active activation；新租户 seed trigger 改为 v2。
- 0055 的 bootstrap owner trigger 同样硬编码 v1/hash/count；0077 必须先按 exact schema 核对，再用 v2 版本替换。
  Down 按原 SQL 恢复 v1 owner/tenant trigger，不能只改 tenant trigger 后留下永远拒绝新租户的组合。
- Control、Gateway、Console 展示和 migration seed 使用同一个 v2 version/hash/count；未知或漂移制品在网络前失败。

### 3.3 聚合入口封闭

- 通用 `POST /providers` 明确拒绝 `provider_type=local_endpoint`，返回
  `provider.local_endpoint_requires_local_api`。
- 通用 Provider PATCH 禁止把任何 Provider 改成或改出 `local_endpoint`；对 backing local Provider 的 name、URL、
  auth、status 和 type 更新全部拒绝。PRD-084 没有定义更新 API，因此 Endpoint 创建后保持不可变，避免只更新父表而
  绕过 aggregate revision、健康 generation 和审计。
- 配置导入拒绝携带 `local_endpoint` Provider；策略模板、seed 和其他内部管理入口也必须在事务内验证不存在孤立
  `local_endpoint`。
- 0077 为 routing attempt 增加数据库 guard：Provider 类型为 `local_endpoint` 时必须存在同 tenant 的 active
  LocalModelEndpoint owner，否则 attempt gate 在任何网络前失败。

### 3.4 独立权限

PAGE-116 不复用会读取全部 Provider/KeyPool 的 `provider.read` 页面，也不通过同时要求 Provider/Model 权限来拼接授权。
0077 新增后端权限：

- `local_endpoint.read`：读取脱敏 aggregate 与目录摘要；授予 admin、developer、ops_admin。
- `local_endpoint.manage`：原子注册 Endpoint/初始模型/凭据；授予 admin、ops_admin。
- `local_endpoint.test`：执行受限健康和目录探测；授予 admin、developer、ops_admin。

权限进入既有 RBAC snapshot/revision、角色清单、`/auth/me` 和 `/console/navigation` fingerprint。Console capability 只改善
体验，三个 API 分别由后端依赖重新校验；动态撤权立即生效。不得为了 PAGE-116 给 developer 扩大 `provider.read` 或读取 KeyPool。

## 4. 公开 API

所有路由使用现有统一错误中间件返回 `error.code`、`request_id` 和 `trace_id`；body `extra=forbid`，客户端不能提交
`tenant_id`、actor、Provider 类型、health、revision、lease、resolved IP 或内部事件字段。

### 4.1 `POST /local-endpoints`

后端要求 `local_endpoint.manage`。请求：

```json
{
  "provider_code": "local_inference",
  "name": "Local inference",
  "engine_kind": "vllm",
  "base_url": "https://local-model.example/v1",
  "auth_type": "bearer",
  "bearer_token": "one-time-write-only",
  "initial_model": {
    "internal_model_id": "local-chat",
    "provider_model_id": "served-model",
    "display_name": "Local chat",
    "capabilities": {"chat": true, "stream": true},
    "context_window": 32768,
    "input_price_per_1k": 0,
    "output_price_per_1k": 0,
    "currency": "CNY"
  }
}
```

约束：

- `initial_model` 必填，保证资源创建后已有一条可通过统一 API 路由的模型；更多模型仍可由现有 Model API 管理。
- `auth_type=none` 时禁止 `bearer_token`；`bearer` 时 token 必填，只在请求作用域内加密，响应、审计、日志、事件和
  验收证据均不返回明文、密文或完整指纹。
- `base_url` 复用 Provider URL 规范化，并额外要求 path 为空或以 `/v1` 结束，不允许 query、fragment、userinfo。
- `X-Request-ID` 作为租户级幂等键。0077 保存不含 token 的 canonical intent SHA-256；同 request/同意图返回原资源，
  同 request/不同意图返回 `local_endpoint.idempotency_conflict`。
- 幂等 hash 明确排除 `bearer_token`，避免 Token 摘要形成离线猜测面；同 request 重放永远不轮换凭据，也不比较新提交的
  token。需要不同凭据必须使用新的 request ID；本 PRD 不提供凭据轮换或 Endpoint 更新。
- 一个 `BEGIN IMMEDIATE` 原子创建 backing Provider、LocalModelEndpoint、初始 Model、可选 Credential、create intent
  和 AuditLog；任一失败全部回滚。Endpoint URL 和自由文本在审计中只保存不可逆 SHA-256。

返回 `201`；幂等重放返回相同资源和 `200` 语义由响应字段 `created=false` 表示，避免重复创建。

### 4.2 `GET /local-endpoints`

后端要求 `local_endpoint.read`。只返回当前认证租户的 endpoint、初始模型、健康摘要、catalog revision、
发现模型计数、固定 `endpoint_hint` 和 `credential_configured`；不返回完整 URL、token、密文、指纹、probe lease 或 resolved IP。

`endpoint_hint` 只包含 scheme、固定 `[private|public|loopback]` 分类和 `/v1` 协议提示，不包含 hostname、IP、完整端口或
path。响应设置 `Cache-Control: no-store`；Console 不把创建请求中的 URL 写入 Storage、shell snapshot、错误遥测或验收 JSON。

### 4.3 `POST /local-endpoints/{id}/test`

后端要求 `local_endpoint.test`；body 只接受 `{}`。流程：

1. 按可信 tenant 和 local endpoint ID 加载 owner，不存在与跨租户统一 404。
2. 扩展 Provider Health 的现代 probe executor，在同一次受 lease/generation 约束的 `/v1/models` 请求中选择性提取
   安全目录投影；不得先做只读状态探测、再发第二次无 lease 的目录请求。
3. 仅在 2xx 时有界读取 JSON：响应最大 1 MiB、模型最多 200 条、每个 ID 最长 160 字符、拒绝控制字符和重复异常结构。
4. 在新的短事务内再次核对 endpoint revision、provider health generation 与 lease 已释放，追加 catalog revision；
   对发现条目 upsert 为 `available`，上次存在但本次缺失的条目标为 `missing`，不删除任何 Model，也不覆盖管理员价格、
   capability、状态或描述。
5. `_Attempt` 中的目录 tuple 使用 `repr=False`，不得进入 probe log；返回固定健康结果、catalog revision、计数和
   已校验模型 ID 列表，不返回上游 JSON 或异常文本。

并发 probe 返回 `provider_health.probe_in_progress`；配置漂移返回 `provider_health.probe_superseded`；网络策略拒绝、
认证失败、超时和目录格式错误均使用固定错误码。

## 5. 0077 数据模型与迁移

### 5.1 `providers` 重建

0077 使用 `PRAGMA foreign_keys=OFF`、`PRAGMA legacy_alter_table=ON` 和 `BEGIN IMMEDIATE`，在保持原 rowid、全部列值、
列顺序语义、索引和两个 PRD-068/075 parent trigger 的前提下重建 `providers`，唯一变化是 provider_type CHECK 增加
`local_endpoint`。迁移后必须执行 `foreign_key_check` 与 `integrity_check` 门禁。

当前所有引用表继续指向表名 `providers`，不得逐表删除历史事实或临时关闭 owner 约束。0077 自动化必须对 seeded
Provider 逐行比较 rowid 与全部列值，并核对现有 8 个 index/autoindex 与 4 个 schema object 语义。

### 5.2 `local_model_endpoints`

- `local_endpoint_id`、`tenant_id`、`provider_id`、`initial_model_id`；
- `engine_kind`、`revision`、`catalog_revision`、`catalog_status`；
- `last_discovered_at`、`last_catalog_error_code`、`created_by`、`created_at`、`updated_at`；
- `status=active|disabled`（本 PRD 创建时固定 active）、tenant-qualified FK 到 Provider、Model 和 User，`provider_id`
  租户内唯一；Provider 必须为 `local_endpoint`，初始 Model 必须属于同一 Provider，由 insert/update trigger 再校验。
- 反向 trigger 同时保护 backing Provider 和 initial Model：有 aggregate 时 Provider 不能改 tenant/type/base URL/auth/status，
  Model 不能改 tenant/provider 或被删除。不能只保护子表写入而允许父表之后漂移。

### 5.3 `local_model_catalog_entries`

- `local_model_catalog_entry_id`、`tenant_id`、`local_endpoint_id`；
- `provider_model_id`、`availability_status=available|missing`、`first_seen_revision`、`last_seen_revision`；
- `linked_model_id` 可空，只允许同 tenant、同 backing Provider 的 Model；
- `created_at`、`updated_at`，唯一键 `(tenant_id,local_endpoint_id,provider_model_id)`。

目录条目是探测事实，不保存响应 JSON、Endpoint、Token、模型权重路径或自由异常。

### 5.4 `local_endpoint_create_receipts`

保存 `tenant_id/request_id/intent_hash/local_endpoint_id/created_at`，同 tenant/request 唯一且不可变。由于 aggregate 与 receipt
位于同一事务，不引入对外可见 `processing` 状态；崩溃只会全部回滚或完整提交，幂等恢复只返回已提交资源。

### 5.5 `local_model_call_events`

对 routing attempt 的 `started -> succeeded|failed` 终态，0077 trigger 在同一事务为 backing Provider 类型
`local_endpoint` 且 `execution_status!=not_called` 的行插入不可变 source：

- `local_model_call_event_id` 稳定绑定 `routing_attempt_id`；
- `tenant_id/request_id/trace_id/routing_attempt_id/local_endpoint_id/provider_id/model_id`；
- `attempt_sequence/attempt_role/execution_status/outcome/error_code/error_category/http_status/retryable/latency_ms/usage_status/completed_at`。

表和 payload 禁止 URL、Credential、Header、Prompt、Response、raw usage、上游正文或 resolved IP。数据库 trigger
保护 append-only、同租户 owner 和终态一致性。

### 5.6 routing attempt 本地调用 fence

0077 为 `routing_attempts` 增加 `provider_call_status=not_applicable|not_called|called` 和可空
`provider_call_started_at`。创建 local attempt 时固定为 `not_called`，其他 Provider 为 `not_applicable`。Local Adapter
完成 DNS/ACL preflight 后、发送任何字节前，必须用 tenant/attempt/status CAS 将其改为 `called`；CAS 失败则在网络前
失败关闭。终态 trigger 只为 `called` 生成 source/outbox。

Gateway 启动恢复扫描超时的 local `started` attempt：`not_called` 收敛为本地失败且不发 EVT-096；`called` 保守收敛为
`gateway.local_endpoint_outcome_unknown` 并形成事件。恢复同样使用 lease/CAS，不能自动重试模型调用。

## 6. `local_model.called` 事件

Shared webhook catalog 新增 exact validator。事件 ID 由 source ID 稳定派生，payload 固定为：

```json
{
  "schema_version": "1.0",
  "local_endpoint_id": "lme_...",
  "provider_id": "...",
  "model_id": "...",
  "routing_attempt_id": "rta_...",
  "request_id": "req_...",
  "attempt_sequence": 1,
  "attempt_role": "primary",
  "execution_status": "called",
  "outcome": "succeeded",
  "error_code": null,
  "latency_ms": 42,
  "usage_status": "pending",
  "completed_at": "2026-08-16T00:00:00Z"
}
```

0077 同事务再插入 `local_model_call_event_outbox`，只保存 source ID、tenant、`pending|leased|sent|failed`、attempt
count、固定错误码、lease token/deadline 和时间，不复制 payload。Gateway 在 attempt 终态提交后只发送 wake-up；
独立 outbox worker 按 tenant 公平领取，从不可变 source 重建事件，再调用现有 runtime delivery。重复构造使用相同
event ID，现有 `(endpoint,event_id)` 唯一键保证幂等。队列满、进程在 commit 后崩溃或 Webhook 临时失败都由 lease
过期恢复；达到有界重试上限后保持可查询 failed 终态和固定错误码。模型响应不等待外部 Webhook，source 永久保留。

流式调用同样必须有 routing attempt source；若现有流链只以 stream session 记账，编码时先补齐 attempt 终态绑定，
不能仅为非流实现事件。

## 7. 本地 Endpoint 网络安全

### 7.1 部署级配置

- `METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS`：允许的企业私网 CIDR，默认空；租户 API 不能提交或扩展。
- `METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS`：默认 `80,443,8000,8080,8443,11434`，仅部署运维可配置。
- `METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK`：默认 false，prod 固定拒绝 true；仅用于明确的 dev/test 验收。

列表必须 canonical、无重叠/非法项；配置错误使 Control/Gateway readiness 失败关闭。metadata、link-local、multicast、
unspecified 永久拒绝，loopback 只有上述非 prod 开关可放行。

### 7.2 DNS 固定和连接

Control 探测与 Gateway 每个 local attempt 都执行：

1. 规范化 scheme/host/port/path，禁止 proxy、redirect、userinfo、query、fragment 和 Unix socket。
2. 单次 DNS 解析全部 A/AAAA，最多 8 个；混合允许/禁止整次阻断。
3. 按永久 deny、部署 CIDR 与端口白名单判定；客户端不能传 resolved IP。
4. HTTP 请求 URL/Host/TLS SNI 保持原 hostname，TCP 只连接已批准 IP；不得二次 DNS。
5. `trust_env=false`、`follow_redirects=false`、HTTP/2 关闭、单连接、无 keep-alive 跨请求复用。

Gateway 本地请求增加 header/TTFB、整体 deadline、非流成功正文上限和流式累计字节上限，防止本地服务无限占用工作协程。
这些是部署级上限，不由 tenant 放宽。网络错误只映射固定 `gateway.local_endpoint_*` 分类，不记录 Endpoint 或异常正文。

## 8. 用量、成本和失败追踪

- 上游返回标准 usage 时沿用现有归一化；缺失时保留 `estimated=true` 与固定估算原因。
- 初始模型价格必须显式进入现有 Model/PricePlan 口径；零价是管理员明确配置的可审计事实，不由系统把“未知”猜成零。
- Budget、Quota、RequestLog、routing attempt、Usage Ledger、FinOps Ledger 和失败请求继续由现有 Gateway 事务链结算。
- 本地网络策略阻断属于已经创建 attempt 但未发送字节的本地失败，`usage_status=not_applicable`；超时/连接中断按现有
  “可能已到上游”口径保守追踪。
- 日志只保存 tenant/request/trace、资源 ID、固定状态、计数和错误码；不保存 Key、Token、Prompt、Response、URL、
  DNS 地址、上游错误正文或完整 usage 原文。

## 9. PAGE-116

新增独立 `local-models` 导航项和页面，不把工具嵌套进 Provider 卡片：

- 顶部提供 engine/status 筛选和创建命令；主体表格展示 Endpoint 名称、engine、初始模型、健康、目录 revision、
  已发现计数和最近探测时间。
- 创建 Dialog 使用 engine segmented control、auth segmented control、一次性 token 密码输入、Endpoint 和初始模型字段；
  `none` 时移除 token 值并不发送字段。
- 每行测试按钮有明确 loading/disabled 状态；成功后只按 owner token 合并当前资源，切换页面、租户、会话或动态撤权时
  Abort 并拒绝迟到响应。
- capability 只决定按钮呈现；后端始终重新校验组合权限。403 后立即清理表格、Dialog、输入 token 和 URL。
- URL 只存在当前授权 DOM，不进入 Storage、query string、Console error 或浏览器证据 JSON；token 在提交完成或失败后清空。
- 桌面 `1280x720` 与移动 `390x844` 必须无重叠和全局横向溢出，表格在移动端使用受控横向区域。

## 10. 错误与并发合同

新增固定错误至少包括：

- `local_endpoint.not_found` 404；
- `local_endpoint.request_invalid`、`local_endpoint.engine_invalid`、`local_endpoint.url_invalid` 422；
- `local_endpoint.auth_invalid`、`local_endpoint.catalog_invalid` 422；
- `local_endpoint.conflict`、`local_endpoint.idempotency_conflict` 409；
- `local_endpoint.unavailable` 503；
- `gateway.local_endpoint_blocked`、`gateway.local_endpoint_unavailable` 502/503。

跨租户与不存在统一 404；重复 probe 和 generation 漂移沿用 Provider Health 的 409；未知数据库、加密、DNS 和 HTTP 异常
不能把底层文本拼入 API。Create、probe、catalog refresh 和 attempt event 均有唯一键/CAS，重复请求不得产生重复网络调用。

## 11. 迁移与回滚

Up：

1. preflight 核对 0076 栈顶、v1 activation 形状、Provider schema/index/trigger 和 FK/integrity。
2. 重建 Provider CHECK 并恢复全部索引/trigger。
3. 扩展 routing attempt call fence；创建 `local_model_endpoints`、`local_model_catalog_entries`、`local_endpoint_create_receipts`、
   `local_model_call_events` 和 `local_model_call_event_outbox` 五张表及 owner/immutable/state trigger。
4. 按受信顺序临时替换 0055 immutable/owner trigger，supersede v1、激活 v2，再安装 v2 owner/immutable/new-tenant
   trigger；任一 tenant 缺失唯一 active activation 时失败。
5. 新增三个 local endpoint 权限并生成下一版 RBAC snapshot，角色赋权只按 3.4 的固定矩阵；页面导航要求 read。
6. 最终执行 `foreign_key_check` 与 `integrity_check`，任一异常整笔回滚。

Down：

- 必须使用 expected-version trusted transaction；有任何 `local_endpoint` Provider、LocalModelEndpoint/catalog/receipt/event、
  v2 发布收据、`local_model.called` subscription/delivery 或依赖事实时先原子阻断。
- 仅在无 PRD-084 业务事实时删除新对象，恢复新租户 v1 trigger、删除自动生成的 v2 activations、把原 v1 恢复 active，
  回滚 RBAC snapshot/permissions 和 routing attempt 扩展列，再重建原九类 Provider CHECK。
- seeded up/down/re-up 必须逐行证明既有 Provider、Model、Credential、Request、Usage、routing attempt、Adapter activation
  rowid 和全部字段不漂移；四阶段均 `integrity_check=ok`、外键违规 0。

## 12. 测试与实际验收

### 12.1 自动化专项

- 0077 fresh/repeat/down/re-up、seed 保真、业务事实 down guard、schema/index/trigger exact contract。
- Shared v2 catalog/hash/fixture exact set、auth 组合、公开 wire 隐私、`local_model.called` exact validator。
- Control create/list/test：原子回滚、幂等、RBAC 组合、跨租户 404、token 加密、probe lease/generation、目录上限和不覆盖 Model。
- Gateway：DLP/预算/权限阻断网络调用 0；vLLM/Ollama/OpenAI-compatible 非流/流；usage 自报/估算；成功、失败、超时、
  fallback 的 attempt/event/request/usage/cost 一致性。
- 网络：私网 allow/deny、11434、metadata、loopback prod deny、混合 DNS、rebind、Host/SNI、proxy/redirect、header/overall/body/stream
  上限和连接清理。
- Console：exact client、owner/Abort、动态撤权、PAGE-116 创建/测试/刷新、token/URL 不落 Storage/错误证据。

### 12.2 Runtime

从空 SQLite 实际完成 `77/0077 -> 77/0077 -> 76/0076 -> 77/0077`，每阶段检查 migration count/version、
`integrity_check=ok` 和外键违规 0。启动真实 Control、Gateway、两个 OpenAI-compatible stub（vLLM/Ollama profile）和
Webhook receiver，验证创建、探测、统一 `/v1/chat/completions`、DLP/预算阻断、成功/失败/usage/成本与签名事件。

验收结束后 owned 进程、端口、连接、SQLite sidecar、临时 token 和 staging 归零；证据只保留固定 ID、状态、计数、hash
和错误码，不含 Endpoint、Token、Prompt、Response 或上游正文。

### 12.3 Browser

Chromium 在 `1280x720` 与 `390x844` 覆盖列表、创建 Dialog、none/bearer 切换、测试成功/失败、动态撤权和租户切换。
正式 JSON/PNG/manifest 做敏感字段扫描；截图不得包含真实 Endpoint 或 token，使用合成、不可路由占位展示。

## 13. 文档和提交门禁

- 更新 README 当前进度、目录说明、API/运行配置说明、`docs/testing/PRD-084-tests.md` 和 Excel 开发状态。
- 只把 PRD-084 标为完成，明确下一项必须重新交叉确认，不启动 PRD-085。
- 执行敏感信息与禁止文件审计；不提交 `.env`、数据库/sidecar、日志、cache、coverage、build、Office 临时文件、token、
  真实 Endpoint 或诊断 artifact。
- 测试、Runtime、Browser、迁移和审计全部通过后才提交并推送 `origin/main`；保持
  `all_108_prds_complete=false`。

## 14. 编码前自审

### 14.1 第一轮：聚合完整性、探测副作用与恢复

第一轮发现并修正 7 项：

1. **P0：通用 Provider/Import 可直接创建 local 类型，绕过 LocalModelEndpoint owner。** 已增加入口封闭和
   routing-attempt 数据库 guard。
2. **P1：通用 PATCH 可改变 backing Provider，但没有 aggregate revision。** 本 PRD 没有更新 API，故固定拒绝
   backing Provider 的通用更新，避免父子漂移。
3. **P1：健康探测后再目录发现会发两次请求，第二次不受 lease/generation 约束。** 改为现代 probe executor 在同一次
   `/models` 请求中产生安全目录投影，完成 CAS 后再落 catalog revision。
4. **P1：0055 owner trigger 硬编码 v1，只替换新租户 trigger 会拒绝 v2 bootstrap。** Up/Down 都要求成组替换 owner、
   immutable 和 tenant trigger，并验证每租户唯一 active activation。
5. **P1：内存 runtime queue 在 commit 后崩溃会永久丢 EVT-096。** 新增只引用 source 的持久 outbox、租约/CAS、
   startup recovery 和稳定 event ID。
6. **P1：直接插入孤立 local Provider 可能进入 Gateway 且无事件 owner。** routing attempt insert guard 在网络前阻断；
   Control/Import 再做前置友好错误。
7. **P2：初稿没有说明 Credential 与 Model 的创建顺序。** 原子顺序冻结为 Provider -> initial Model ->
   LocalModelEndpoint -> optional encrypted Credential -> receipt/Audit，所有 FK 在 commit 前成立。

第一轮修订后继续进行第二轮，尚未开放编码门禁。

### 14.2 第二轮：角色可达性、秘密边界与真实调用语义

第二轮发现并修正 8 项：

1. **P0：developer 没有 provider.read，复用旧权限会让 PAGE-116 对“研发”Persona 不可用。** 改为三个独立后端权限，
   developer 仅有 read/test，不扩大 Provider/KeyPool 读取面。
2. **P1：旧 Provider 页面会加载每个 KeyPool 并把完整 URL 放入 DOM/title。** PAGE-116 使用独立 aggregate/state/client；
   list/create 响应只返回固定 endpoint hint，完整 URL 只在一次性创建 Dialog 内存中存在。
3. **P1：幂等 hash 若包含 token digest 会产生额外秘密猜测面。** intent hash 排除 token；重放不比较、不轮换凭据。
4. **P1：只校验 LocalModelEndpoint 子表写入，父 Provider/Model 后续仍可漂移。** 增加父表反向 update/delete trigger，
   通用 API 也以前置固定错误拒绝。
5. **P1：routing attempt 不证明实际开始本地调用，ACL 阻断可能被错误发布为 `local_model.called`。** 新增持久
   provider-call fence；仅 `called` 终态生成 source/outbox，`not_called` 仍由 attempt/request log 追踪但不发布事件。
6. **P1：call fence 后进程崩溃会留下不确定 started 行。** startup recovery 对 called 使用 outcome-unknown，禁止自动重试；
   not-called 使用本地失败，两者都通过 CAS 唯一收敛。
7. **P1：目录 ID 若直接挂在 probe log/异常 repr 会扩大上游元数据传播。** catalog tuple `repr=False`，健康日志只保留
   固定计数/状态；完整上游 JSON 永不落库。
8. **P2：同事务 create intent 不需要对外 processing 状态。** 改为不可变 completed receipt，崩溃由 SQLite 原子事务决定
   全有或全无，减少伪恢复分支。

第二轮修订后再次交叉检查权限、租户 owner、迁移 Up/Down、DNS pinning、流/非流、usage/cost、事件恢复、Console owner
和隐私合同，未遗留 P0/P1；编码门禁通过。实现不得弱化上述约束。

## 15. 实现后第一轮自审：入口、隐私与可靠投递

实现后第一轮按 Control -> Gateway -> Console -> event/outbox 的真实调用链检查，发现并修正 5 项：

1. **P1：Local Endpoint 探测继承通用 Provider 的公网默认允许。** Local API 现固定为部署级私网 CIDR 闭集；
   dev/test 可由独立开关允许 loopback，生产不能借通用 Provider Health 的公网规则放开。
2. **P1：Control 只校验每个 DNS 地址，却没有限制唯一地址总数。** Control 与 Gateway 都在映射 IPv4 后去重，
   同一次解析最多接受 8 个地址；混合、超量或任一未授权地址使整次请求失败关闭。
3. **P1：Audit `object_name` 曾直接接收 Endpoint 显示名称。** 审计对象名改用受控 `provider_code`，自由名称仅保存
   不可逆 `name_hash`；URL、Token、Prompt、Response 和 DNS 地址仍禁止进入 Audit 与日志。
4. **P1：新增权限已进入 API，但导航/capability 与旧 Provider/Config 入口没有形成同一封闭矩阵。** PAGE-116 现在只由
   后端 `local_endpoint.read/manage/test` 和导航事实驱动；通用 Provider create/patch、配置导入和旧健康入口均拒绝
   `local_endpoint` 聚合旁路，前端隐藏按钮不构成授权。
5. **P1：durable delivery 的 pending/leased 恢复与终态 CAS 不完整。** source outbox 改为唯一 source owner、租约 generation、
   pending/leased/sent/failed 闭集和 stale lease 回收；发送前后都按 token/generation CAS，重复 worker 不会重复提交终态。

本轮同时复核 Console owner/Abort、租户切换、动态撤权、URL/Token 擦除和固定错误码；修正后没有未关闭 P0/P1。

## 16. 实现后第二轮自审：永久网络拒绝与 called 用量事实

第二轮由独立只读审查与主 Agent 复核共同完成，发现并修正 4 项：

1. **P1：IPv6 ULA allowlist 可包含云 metadata 地址。** Shared 固定永久拒绝两个 IPv4 metadata 地址、其 IPv4-mapped
   形式以及 `fd00:ec2::254`、`fd20:ce::254`；Control/Gateway 在部署 CIDR 判断前统一复用该集合，`fc00::/7`
   不能覆盖永久拒绝。两个服务均用可包含目标的宽测试 allowlist 证明 deny 优先。
2. **P1：本地调用越过 send fence 后取消仍写 `usage_status=not_applicable`。** `ProviderRequest` 仅在持久
   `not_called -> called` CAS 成功后设置内存标记；主、Fallback 与流式取消或本地失败据此写 `unavailable`，围栏前
   阻断仍为 `not_applicable`，且客户端取消始终不计入模型质量。
3. **P2：上述语义只靠当前调用方传参，未来分支可回归。** 0077 routing-attempt trigger 现强制本地 `called -> failed`
   只能写 `unavailable`、`not_called -> failed` 只能写 `not_applicable`；错误 flag 会被持久层拒绝且不生成不可变事件。
4. **P2：修正前 Runtime/Browser 证据可能携带过期 provenance，Browser runner 还曾引用旧电脑级 Node。** 修正前 run
   全部作废并按 owner marker 清理；正式批次只用 `C:\MetaHub` 内工具，从当前源码和空库重跑，manifest 与 source hash
   必须由主 Agent 再计算后才可写入测试记录和 Excel。

第二轮补齐主/备用/流式取消、attempt/event/质量计数、完整 metadata 矩阵与 migration trigger 回归。最终没有未关闭
P0/P1；PRD-084 完成不改变 `all_108_prds_complete=false`，也不授权启动 PRD-085。
