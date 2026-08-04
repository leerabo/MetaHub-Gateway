# PRD-037 - SSE 流式代理与超时看门狗

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：两轮设计审核、编码、全量测试与本地运行态验收完成
- 测试证据：`docs/testing/PRD-037-tests.md`
- 权威输入：Excel `01_PRD总表`、`04_页面清单`、`05_API接口清单`、`06_数据对象`、`07_Webhook事件`、`08_验收用例`
- 关联对象：`StreamSession`
- 关联页面：API 调试台流式开关
- 关联接口：`POST /v1/chat/completions`，请求体 `stream=true`
- 关联事件：`stream.timeout`
- 前置依赖：PRD-014 OpenAI-compatible API、PRD-030 Provider Adapter、PRD-036 非流式 Fallback

## 需求理解

1. developer 使用 OpenAI-compatible Chat Completions 的 `stream=true` 时，网关必须以 SSE 原样、增量透传上游非空字节块，不能先缓存完整响应再返回。
2. 上游连接建立并返回成功响应头后，首个数据块和任意相邻数据块之间都受 Inter-Token Timeout 看门狗约束。超过配置阈值仍无非空字节块时，网关主动取消读取、关闭上游响应和 HTTP 客户端，并把本次请求标记为 `gateway.upstream_stream_timeout`。
3. 任意非空传输块都证明上游仍存活并重置看门狗，包括 SSE comment/heartbeat。Usage collector 仍只解析 `data:`，不会把 heartbeat 当成模型输出或 Token。
4. 客户端断开检测必须与上游等待并发执行。只在收到下一个上游块后检查断开无法处理僵尸连接，不满足验收。
5. 流式响应头一旦发出，HTTP 状态已固定为 200，不能再改成统一 JSON 错误。此时超时通过标准 SSE `event: error` 携带统一 `error.code/message/request_id/trace_id`，随后关闭流；不发送 `[DONE]`，避免客户端把错误误判成正常完成。
6. 上游握手阶段失败发生在下游 `StreamingResponse` 返回前，继续使用现有统一 JSON 错误和 HTTP 状态。握手成功后的读异常使用 SSE 错误事件和调用日志。
7. 每个流式请求在发送上游网络请求前创建可审计 `StreamSession`。创建失败时 Fail Closed，不能发出一笔无法追踪、无法确认资源终态的上游请求。
8. StreamSession 只保存租户、链路、模型、Provider、时间、分片数、已发字节数和统一错误码，不保存 Prompt、Response、Authorization、Provider Key、上游 Header 或 Endpoint URL。
9. 每个块只更新内存计数，终态一次性落库，避免数据库 I/O 阻塞首字节和块间透传。会话创建与握手成功各写一次状态，终态通过条件更新保证幂等。
10. 客户端断开、调用方关闭异步生成器、任务取消、上游异常、超时和正常结束都必须进入同一个 finally 清理路径，显式且幂等地关闭上游资源、结束 StreamSession、写 request_logs/usage_ledger，并释放限流并发租约。
11. `stream.timeout` 只在会话已成功落为 `timed_out` 后入有界非阻塞事件队列，确保事件引用已提交事实且 Webhook 网络 I/O 不阻塞流关闭。
12. 流式请求始终只调用路由计划中的初始候选。握手失败、首块超时或中途超时均不允许切换模型，也不创建 PRD-036 的 Fallback attempt。
13. 网关采用下游背压：当前块未被 ASGI 消费前不主动读取更多上游块，不引入无界缓冲队列。看门狗只测量“正在等待下一上游块”的时间；下游发送阻塞由 ASGI 取消和客户端断开检测收敛。
14. API 调试台必须真正增量读取 ReadableStream、实时展示预览，并允许用户主动停止。Virtual Key 只在本次内存请求中使用，不写 localStorage、状态快照或日志。
15. MVP 不做流中模型切换、断点续传、已输出内容回放、多 Provider 拼接、复杂自适应超时和跨进程流会话恢复。

## 涉及模块

- `infra/migrations/0027_stream_sessions.sql`：新增流会话表及 request_logs 流摘要字段、租户一致性触发器和索引。
- `gateway-data-plane/src/metahub_gateway/streaming/sessions.py`：StreamSession 创建、状态推进、幂等终结和 timeout 事件构造。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`：编排上游读取、客户端断开侦测、Inter-Token Timeout、SSE 错误和统一清理。
- `gateway-data-plane/src/metahub_gateway/providers/base.py`：为上游流响应提供显式、并发安全、幂等 `aclose()`。
- `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py`：连接、写入和连接池使用有界超时，流式 body read timeout 交给看门狗。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`：RequestLogContext 增加流会话安全摘要并写入 request_logs。
- `control-plane-api/src/metahub_control/request_logs`：列表展示流状态，详情返回 StreamSession 安全字段。
- `shared-sdk/python/metahub_shared/runtime/config.py`：增加流式块间与握手超时配置及边界校验。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`：新增流会话和流超时统一错误码与诊断建议。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：注册 `stream.timeout`。
- `console-web/src/lib/gatewayApi.js`：增量读取 SSE、AbortSignal、受限预览和标准流错误识别。
- `console-web/src/features/observability/renderObservabilityPanels.js`、`console-web/src/app/main.js`：实时结果区、停止按钮和流生命周期管理。
- 控制面、数据面、前端、迁移测试和开发文档。

## 数据表/字段设计

### stream_sessions

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `stream_session_id` | TEXT PK | `str_<uuid>`，不可由客户端指定 |
| `tenant_id` | TEXT NOT NULL | 所有查询和更新的租户边界 |
| `request_id` | TEXT NOT NULL | 客户端请求 ID |
| `trace_id` | TEXT NOT NULL | 链路 ID |
| `virtual_key_id` | TEXT NULL | Virtual Key 元数据 ID，不保存明文 |
| `environment_id` | TEXT NULL | 环境 ID |
| `environment_code` | TEXT NULL | 环境安全展示值 |
| `project_id` | TEXT NULL | 项目归属 |
| `application_id` | TEXT NULL | 应用归属 |
| `model_id` | TEXT NOT NULL | 实际调用模型 |
| `provider_id` | TEXT NOT NULL | 实际调用 Provider |
| `provider_code` | TEXT NOT NULL | 安全展示字段 |
| `status` | TEXT NOT NULL | `starting/active/completed/timed_out/client_disconnected/upstream_failed` |
| `inter_token_timeout_ms` | INTEGER NOT NULL CHECK > 0 | 本次请求采用的阈值快照 |
| `chunk_count` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 已收到的非空传输块数 |
| `bytes_sent` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 已交给下游生成器的字节数 |
| `first_chunk_at` | TEXT NULL | 首块时间 |
| `last_chunk_at` | TEXT NULL | 最近块时间 |
| `error_code` | TEXT NULL | 统一安全错误码 |
| `started_at` | TEXT NOT NULL | 创建时间 |
| `completed_at` | TEXT NULL | 终态时间 |
| `created_at` | TEXT NOT NULL | 审计创建时间 |
| `updated_at` | TEXT NOT NULL | 最近状态时间 |

约束与索引：

- 唯一 `(tenant_id, request_id)`，同一请求只能建立一个流会话；唯一冲突 Fail Closed，禁止重复上游调用。
- 外键关联 tenant、Virtual Key、environment、project、application、model、provider。INSERT trigger 与归属字段 UPDATE trigger 都校验所有非空引用和 model/provider 属于同一租户，禁止维护脚本通过更新跨租户移动会话。
- 状态只允许 `starting -> active -> terminal`、`starting -> upstream_failed` 或在握手成功但 active 写入失败时保持 starting 并关闭上游。数据库状态迁移 trigger 禁止终态回退、active 回到 starting 和终态互改；应用终态更新使用 `WHERE tenant_id=? AND stream_session_id=? AND status IN ('starting','active')`，重复清理不覆盖第一个终态。
- 索引 `(tenant_id, request_id)`、`(tenant_id, status, started_at DESC)`、`(tenant_id, model_id, started_at DESC)`。
- 不在每个分片写数据库。`chunk_count/bytes_sent/first_chunk_at/last_chunk_at` 在进程内累计，正常或异常终止时一次回写。
- `starting` 长期未完成表示进程在握手前后异常退出，本 PRD 只如实保留，不引入后台修复任务。

### request_logs 扩展

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `stream_session_id` | TEXT NULL | 关联同租户、同 request_id 的 StreamSession |
| `stream_status` | TEXT NULL | 与 StreamSession status 相同的终态摘要 |

- INSERT/UPDATE trigger 校验 `stream_session_id` 必须属于相同 `tenant_id/request_id`；`stream_status` 非空时还必须等于关联会话当前状态，且 request_logs success 只能对应 completed，其余流终态只能对应 failed。
- 非流式请求两个字段为空。流式终态 request_logs 使用现有 `success/failed`：`completed` 对应 success，其余终态对应 failed；细分结果由 `stream_status/error_code` 表达。
- request_logs 写入继续是容错路径，不覆盖已经发送给客户端的数据；StreamSession 的创建是上游调用前的强制审计闸门。

### 回滚

`0027_stream_sessions.down.sql` 先删除 request_logs 关联触发器和索引，再删除 request_logs 新字段，最后删除 StreamSession 索引、租户触发器和表。迁移测试覆盖 0026 数据上行、回滚到 0026、再次上行以及 `PRAGMA foreign_key_check`。

## API 设计

### POST /v1/chat/completions

请求继续兼容 OpenAI Chat Completions：

```json
{
  "model": "customer-service-chat",
  "messages": [{"role": "user", "content": "你好"}],
  "stream": true,
  "stream_options": {"include_usage": true}
}
```

握手成功响应：

- HTTP 200
- `Content-Type: text/event-stream; charset=utf-8`
- 原样透传上游 SSE 非空字节块，不改写正常 `data:` 事件。
- 保留现有 `x-request-id/x-trace-id` 响应头中间件契约。

握手前错误：

- 继续由统一异常处理返回 JSON：`{"error":{"code","message","request_id","trace_id"}}` 和对应 HTTP 状态。
- 包括鉴权、权限/租户、路由、DLP、限流、预算、StreamSession 创建失败、上游建连/响应头超时和非 2xx。

握手后超时事件：

```text
event: error
data: {"error":{"code":"gateway.upstream_stream_timeout","message":"Upstream stream timed out.","request_id":"req_xxx","trace_id":"req_xxx"}}

```

- JSON 使用紧凑、确定性序列化；不包含上游异常字符串、URL、Header、Key、Prompt 或已输出正文。
- 发送错误事件后立即关闭 SSE，不发送 `[DONE]`，客户端必须按 error 终止处理。
- 客户端主动断开时无可接收错误，不发送 SSE，只写安全日志和会话终态。

### GET /request-logs

现有 admin/developer 列表项新增：

```json
{
  "stream_session_id": "str_xxx",
  "stream_status": "timed_out"
}
```

非流式请求返回 null。列表不返回逐块时间和计数，避免扩大高频响应体。

### GET /request-logs/{request_id}

详情新增可空 `stream_session`：

```json
{
  "stream_session_id": "str_xxx",
  "status": "timed_out",
  "inter_token_timeout_ms": 30000,
  "chunk_count": 5,
  "bytes_sent": 2048,
  "first_chunk_at": "2026-07-14T10:00:00Z",
  "last_chunk_at": "2026-07-14T10:00:03Z",
  "error_code": "gateway.upstream_stream_timeout",
  "started_at": "2026-07-14T09:59:59Z",
  "completed_at": "2026-07-14T10:00:33Z"
}
```

- 服务端必须同时使用认证租户和 request_id 查询，不接受 tenant_id 参数；跨租户返回 404 防枚举。
- DTO 不包含 endpoint URL、请求/响应正文、Credential ID 或任何 Key 值。

## 流式执行状态机

1. 完成鉴权、请求校验、单候选路由、DLP、限流和预算准入。
2. 使用本次安全上下文创建 `stream_sessions.starting`。数据库不可用或唯一冲突返回 `gateway.stream_session_unavailable`，不调用 Provider。
3. Adapter 使用有界 connect/write/pool timeout 建立上游连接，body read timeout 设为无穷，由编排器的块间看门狗负责。握手失败把会话终结为 `upstream_failed` 后抛统一 JSON 错误。
4. 上游返回 2xx 响应头后，把会话推进为 `active` 并返回下游 StreamingResponse。状态推进失败时关闭上游并 Fail Closed，避免产生无法确认活动态的流。
5. 自定义 StreamingResponse 作为 ASGI `receive` 的唯一消费者，沿用框架并发监听 `http.disconnect` 并把结果写入共享 Event；生成器为下一上游块创建唯一读取任务，同时等待该 Event。使用 `asyncio.wait(..., timeout=inter_token_timeout)` 等待先完成者，禁止业务层另行调用 `Request.is_disconnected()` 争抢 receive 通道。
6. 读取任务先完成且返回非空块：取消当前 Event 等待任务，更新内存计数和时间，collector 计量，`yield` 原始块。下游消费后才进入下一轮，形成自然背压并重置看门狗。若 `asyncio.wait` 到达 timeout 边界时读取任务已经 done，必须先消费该结果，不能制造边界假超时。
7. 读取任务正常 StopAsyncIteration：会话终态 `completed`，request_logs success，写最终 Usage 并释放租约。
8. 客户端断开先发生：取消并显式 await 读取任务，关闭上游，终态 `client_disconnected`，request_logs failed/error `gateway.client_disconnected`，写部分估算 Usage 并释放租约。若断开检测和块读取在同一轮同时完成，断开优先，不能再向已关闭下游 yield 新块。
9. timeout 先发生：取消并显式 await 读取任务，立即关闭上游；先把会话提交为 `timed_out` 并非阻塞 enqueue `stream.timeout`，再向仍连接的客户端发送标准 SSE error。request_logs 在统一 finally 中写 failed/error `gateway.upstream_stream_timeout`，同时写部分估算 Usage并释放租约。即使发送错误事件时客户端又断开，已提交的 timeout 事实和事件也不回退成 client_disconnected。
10. 上游读取抛异常：关闭资源，向仍连接的客户端发送 `gateway.upstream_unavailable` SSE error，终态 `upstream_failed`，写失败日志和部分估算 Usage。
11. 外层任务被取消或生成器收到 GeneratorExit：不得把取消误当普通上游异常；先设置 `client_disconnected`，再在 finally 取消并 await 所有子任务、幂等关闭上游和释放租约，最后重新抛出取消/GeneratorExit。
12. finally 中的终态持久化失败只写不含异常文本的 warning，不覆盖原始流结果；关闭和租约释放仍继续执行。

### 并发与取消细节

- `ProviderStreamResponse.aclose()` 使用异步锁和 `_closed` 标记，`iter_bytes()`、看门狗和 finally 可重复调用但底层 `stream_context.__aexit__` 与 `client.aclose()` 只执行一次。
- 取消读取任务后必须捕获并消费其 `CancelledError`。Python 中该异常不应被普通 `except Exception` 假定捕获，外层取消与内部取消必须区分。
- 每轮最多一个 `anext()` 在执行；不得同时读取同一个 httpx response。
- `asyncio.wait` 返回后按“已确认断开 > 已完成块任务 > 真正 timeout”判定。只有块任务仍未完成且断开任务未报告 true 时才能进入 timeout，避免调度抖动造成假阳性。
- 客户端断开由 StreamingResponse 独占 ASGI `receive` 通道并监听 `http.disconnect`，不使用定时轮询；Inter-Token Timeout 默认 30s，范围 1..600s。
- `bytes_sent` 在 yield 前累计，语义是“交给 ASGI 发送的字节”，不是 TCP 已确认字节；字段名称和控制台文案必须保持这个边界。

## 运行配置

| 环境变量 | 默认值 | 边界 | 说明 |
| --- | --- | --- | --- |
| `METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS` | 30 | 1..600 | 首块和相邻非空块最大等待 |
| `METAHUB_STREAM_CONNECT_TIMEOUT_SECONDS` | 30 | 1..120 | TCP/TLS/响应头连接阶段上限 |
| `METAHUB_STREAM_WRITE_TIMEOUT_SECONDS` | 30 | 1..120 | 上游请求体写入上限 |
| `METAHUB_STREAM_POOL_TIMEOUT_SECONDS` | 5 | 1..60 | 连接池获取上限 |

这些值进入现有安全配置摘要但不属于敏感项。非法值沿用 ConfigError 阻止服务以无界或过小参数启动。

## 前端页面/交互设计

### API 调试台

- 保留现有“流式响应”复选框。选中后提交按钮进入运行态，结果区立即显示“等待首个分片”，不能等完整响应后才出现内容。
- `gatewayApi.sendChatCompletion` 使用 `response.body.getReader()` 和 streaming `TextDecoder`，每次读取后调用 `onStreamUpdate`，同时保留最多 256 KiB 预览；超限时只丢弃最旧显示内容并提示预览已截断，不影响网络读取。
- 结果区使用 `textContent`/模板转义展示原始 SSE 预览，绝不把模型输出当 HTML。通过 `aria-live=polite` 更新块数、字节数和状态。
- 流式请求运行时显示带停止图标和 tooltip 的“停止”按钮。点击调用 AbortController，界面状态为“已停止”，不会把用户主动停止误报为 Provider 超时。
- 收到标准 `event:error` 时解析 `error.code/message/request_id/trace_id`，结果状态显示失败并保留已收到预览；未知或畸形 SSE 仍按原始文本显示，不执行其中内容。
- 非流式路径保持现有 JSON 结果。浏览器不持久化 Virtual Key，重新渲染/切换页面后输入保持为空；错误展示不得包含完整 Authorization。
- 连续提交前先取消仍活动的上一次流，避免一个页面持有多个隐式连接。页面离开或组件销毁时同样 abort。
- 390/720/1100/1440 宽度下操作按钮不重叠，结果文本使用 `overflow-wrap:anywhere`，预览区有稳定最小高度且不会因状态文字改变发生横向跳动。

## 错误码设计

| 错误码 | HTTP/载体 | 使用场景 | 客户端建议 |
| --- | --- | --- | --- |
| `gateway.stream_session_unavailable` | 503 JSON | 会话创建或 active 推进失败，尚未发 SSE 头 | 稍后重试并携带 request_id 排查数据库 |
| `gateway.upstream_stream_timeout` | SSE error；握手后 HTTP 仍为 200 | 首块或相邻块超过阈值 | 终止读取，可按业务策略重新发起全新请求 |
| `gateway.client_disconnected` | 仅日志/会话 | 浏览器断开、abort、生成器关闭 | 不向已断开的客户端响应 |
| `gateway.upstream_unavailable` | JSON 或 SSE error | 握手前网络错误或握手后读异常 | 检查 Provider/网络，按统一错误处理 |
| `gateway.upstream_timeout` | 504 JSON | 连接、写入、池等待等握手前 timeout | 可重试全新请求 |

`gateway.upstream_stream_timeout` 诊断原因固定为“上游在流式输出期间超过块间等待阈值”，建议检查 Provider 延迟、模型排队、网络和配置阈值。错误响应不得回显实际 URL、原始异常、响应片段或 Key。

## 安全与权限约束

1. Gateway 鉴权、环境状态、DLP、限流和预算规则不因 stream=true 绕过；tenant_id 只来自 Virtual Key 上下文。
2. StreamSession 和 request_logs 的写入、更新、查询全部携带 tenant_id。数据库 trigger 防止直接 SQL 关联其他租户的 Key、环境、项目、应用、模型或 Provider。
3. 调用日志查询权限沿用现有 admin/developer 后端 RBAC；viewer 无权限不能依靠前端隐藏。跨租户 ID 返回 404。
4. Provider Key 只存在于 Adapter 上游请求头内存中。StreamSession、request_logs、Webhook、结构化日志、异常和前端均禁止明文、哈希或可还原片段。
5. `stream.timeout` payload 白名单：`stream_session_id/request_id/model_id/provider_id/provider_code/inter_token_timeout_ms/chunk_count/bytes_sent`。不含 URL、Credential、Prompt、Response、Header。只有 timed_out 条件更新成功或读取到已经是 timed_out 的幂等事实时才允许发布；终态提交失败不得发布一个无法查询的事件。
6. 日志异常只记录异常类型，不记录 `str(exc)`；SSE error message 来自统一错误码表，不使用上游正文。
7. 前端预览只能放在页面内存和 DOM textContent，不进入 localStorage、sessionStorage、控制面事件或前端埋点。
8. 预览有 256 KiB 上限，SSE collector 单事件保持现有 1 MiB 上限，防止恶意或异常 Provider 导致无限内存增长。
9. 超时不触发流中 Fallback。客户端若自行发起新请求，将获得新 request_id、新 StreamSession，并重新执行全部治理校验。

## 测试用例

### 主流程 TC-0073

1. 正常 SSE 多块原样透传；每块间隔低于阈值会重置看门狗，最终会话 completed、request_logs success、Usage 可追踪、租约释放。
2. 首块和后续块时间戳、chunk_count、bytes_sent 正确；heartbeat 重置看门狗但不增加输出 Token。
3. API 调试台开启 stream 后逐块触发 UI 更新，最终显示完成、request_id/trace_id 和有界预览。
4. Adapter 使用有界握手 timeout、无 body read timeout；body 空闲由编排器关闭。

### 异常、边界与权限 TC-0074

1. 首块超时：上游读取任务被取消，response/client 各关闭一次，会话 timed_out，request_logs 记录 `gateway.upstream_stream_timeout`，返回标准 SSE error，限流租约释放。
2. 中途超时：已收到内容保持原样，仅追加标准 SSE error；部分 Usage 估算入账，不发送 `[DONE]`。
3. 客户端在上游无块期间断开：ASGI `http.disconnect` 事件到达后立即取消读取和关闭上游，会话 client_disconnected，无 timeout Webhook，不泄漏任务或连接。
4. 调用方 `aclose()` 和 ASGI CancelledError：清理幂等，底层关闭一次，外层取消继续传播。
5. 上游中途读异常：标准 SSE unavailable error、会话 upstream_failed、失败日志和部分 Usage 均存在，错误/日志不包含上游异常字符串或 Key。
6. StreamSession 创建失败：Provider Adapter 未被调用，返回统一 503 并释放治理资源。
7. active 状态推进失败：已建立上游立即关闭，未返回 StreamingResponse，统一 503。
8. 正常、超时、读异常和客户端断开均断言没有调用 fallback adapter、没有第二模型输出、没有 route.fallback_triggered。
9. timeout 事件只发布一次且在会话提交后，队列满时不阻塞关闭；payload 不含 URL、Key、Prompt 或 Response。
10. 迁移唯一约束、状态 CHECK、跨租户触发器、request_logs 关联触发器、0027 回滚和重放通过。
11. request-log admin/developer 可读本租户 StreamSession；viewer 返回 403；其他租户 request_id 返回 404。
12. 配置默认值和上下界通过；0、负数、超上限和非数字阻止配置加载。
13. 前端 AbortSignal、生效停止、SSE error 解析、TextDecoder 跨块 UTF-8、256 KiB 截断和不使用 innerHTML 通过。
14. 日志捕获断言 Provider Key、Authorization、Prompt、响应正文和原始异常文本均未出现。
15. 全量控制面、数据面、前端测试与中文注释门禁通过。

## 代码修改计划

1. 新增 0027 up/down migration，更新迁移版本、结构、触发器和回滚测试。
2. 新增 streaming 包和 StreamSession 服务，先实现强制创建、active 推进、终态幂等和安全事件构造测试。
3. 扩展运行配置、统一错误码、诊断和 Webhook 白名单，并补边界测试。
4. 将 ProviderStreamResponse 改为可幂等关闭，Adapter 使用有界握手超时；补关闭一次和 timeout 参数测试。
5. 重构 `_stream_response` 为明确状态机，接入 Request 断开检测、块间 timeout、SSE error、会话终态、Usage/request_logs 和租约清理。
6. 扩展 RequestLogContext、控制面列表/详情 DTO 和租户查询，加入 StreamSession 安全展示及权限测试。
7. 重构前端 Gateway client 为真正增量读取，接入 AbortController、停止按钮、实时状态、错误解析和预览上限。
8. 更新 README/开发文档和 PRD 测试证据，运行格式/静态检查、PRD 定向测试、控制面/数据面/前端全量测试及中文注释门禁。
9. 应用 0027 到本地开发库，重启三个服务，使用浏览器验证 API 调试台的流式开关、实时结果和停止交互。

## 第一轮设计审核：协议与并发正确性

- 审核状态：通过，已完成第 1 次修订。
- 结论 1：HTTP 200 后不能返回 JSON 或修改状态码，超时采用固定 SSE error 且不发送 `[DONE]`；握手前继续走统一 JSON。
- 结论 2：原初稿没有明确 timeout 边界的任务完成竞态。已增加“断开 > 已完成块 > timeout”的判定顺序，防止调度抖动造成假超时或向断开客户端继续发送。
- 结论 3：原初稿的 timeout 持久化与 SSE error 顺序不够严格。已改为先取消读取并关闭上游、提交 timed_out、入非阻塞事件队列，再尝试发送错误事件，保证事件引用已提交事实。
- 结论 4：外层取消和内部取消必须分开处理，内部被取消的 `anext` 要显式 await，外层 CancelledError/GeneratorExit 继续传播；Provider close 必须幂等。
- 结论 5：不引入预读队列，以 ASGI yield 形成背压。看门狗不把慢客户端发送时间误算为上游块间停顿，符合本 PRD 的 Inter-Token Timeout 边界。

## 第二轮设计审核：租户审计与可运维性

- 审核状态：通过，已完成第 2 次修订，可以编码。
- 结论 1：StreamSession 创建和 active 推进是上游调用/下游流启动前的强制闸门；终态 request_logs/Usage 是容错写入，二者职责不能混淆。
- 结论 2：原初稿只说明 INSERT 租户触发器，无法防止直接 UPDATE 移动归属。已补归属字段 UPDATE trigger 和数据库状态迁移 trigger。
- 结论 3：request_logs 仅校验会话 ID 仍可能保存矛盾状态。已要求 trigger 同时校验 tenant/request、stream_status 与会话状态，并约束 success/failed 映射。
- 结论 4：timeout Webhook 必须基于已提交 timed_out 事实；终态写入失败时只记安全 warning，不发布事件，不阻塞连接关闭和租约释放。
- 结论 5：控制面查询继续使用后端 admin/developer RBAC 与 tenant_id 条件，跨租户返回 404；前端隐藏按钮不参与安全判定。
- 结论 6：会话、请求日志、Webhook、SSE error 和结构化日志均只使用白名单元数据，Provider Key、请求/响应正文、URL/Header 和原始异常字符串没有持久化或回显路径。

## 编码期并发校正

- 真实 ASGITransport 集成测试发现，Starlette `StreamingResponse` 已经并发独占 `receive` 监听断开；业务生成器再次调用 `Request.is_disconnected()` 会形成两个消费者并导致响应无法结束。
- 实现已修订为 `_GatewayStreamingResponse.listen_for_disconnect()` 单点消费 `http.disconnect`，通过共享 Event 通知块读取状态机。该方案仍能在上游空闲时立即取消读取和关闭连接，同时保持 ASGI 通道语义正确。
- 集成测试同时发现 `asyncio.wait` 默认等待全部任务完成会令正常块错误等待到 timeout。实现明确使用 `return_when=FIRST_COMPLETED`，并保留“断开 > 已完成块 > timeout”的竞态判定顺序。

## 运行态验收校正

- 真实浏览器跨域调用确认，链路 ID 即使存在于 Gateway 响应头，未配置 `Access-Control-Expose-Headers` 时前端仍不可读取。共享 FastAPI 工厂现只暴露 `X-Request-ID` 与 `X-Trace-ID`，并以集成测试防止误暴露 Authorization 等敏感头。
- 浏览器截图确认组件级 `display` 可能覆盖 HTML `hidden` 默认样式。控制台加入全局 `[hidden]` 高优先级规则，完成态不再同时显示等待占位，停止按钮也不会残留占位或焦点入口。
- 1440x900 和 390x844 实机布局均无页面横向溢出；真实 SSE 请求完成 4 个分片、226 字节，页面显示 request_id/trace_id、Usage 数据和 `[DONE]`，证明增量读取、审计与前端展示闭环可用。
