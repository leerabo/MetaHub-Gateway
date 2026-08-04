# PRD-071 - 输出侧安全检测与流式滑窗

状态：**已完成（2026-08-04）；设计、两轮自审、实现、测试与实际运行验收均已通过**。

总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

已从 `企业级AI_Gateway_PRD开发总清单_给CodexClaude.xlsx` 的总表、M2/M3 扩展交付、页面、API、
数据对象、Webhook 和验收用例工作表交叉确认：

- `PRD_ID`：`PRD-071`
- 里程碑：`M2-企业增强`
- 优先级：`P2`
- 能力层：`扩展层`
- 模块：`输出安全`
- 名称：`输出侧安全检测与流式滑窗`
- 主要用户：安全负责人、开发者
- 页面：`PAGE-099 安全事件页`、`PAGE-100 日志详情`
- 内部动作：`API-322 output_scan`
- 核心对象：`OBJ-095 OutputSafetyScan`
- Webhook：`EVT-078 security.output_blocked`
- 主流程用例：`TC-0141`
- 边界/异常/权限用例：`TC-0142`

PRD-070 已完成，Excel 中同里程碑、同优先级的下一条未完成项是 PRD-071；本结论不是按编号猜测。

### 1.2 原始验收合同

1. 非流式响应在完整模型输出返回后执行全文检测。
2. 流式响应按每 N 个 Token 的滑动窗口检测。
3. 命中阻断规则时返回标准 `content_policy_violation` 错误。
4. 流式命中后立即停止继续转发，并记录命中前是否已经向客户端输出内容。
5. 已经发送给客户端的内容不承诺撤回。
6. 对应账单进入争议/冲正复核标记，但本 PRD 不自动生成财务冲正分录。
7. 阻断形成 `security.output_blocked` 可订阅事件，并可在安全事件页和调用日志详情追踪。

### 1.3 架构约束

系统架构文档 4.3 明确：

- 流式输出开始后不能中途切换模型。
- 客户端断开、上游僵死和块间超时必须继续由现有 Stream Watchdog 负责。
- 输出安全与 Usage 统计属于旁路，不能人为缓冲完整响应或拖慢正常流式首字。
- 数据面是模型流量必经路径；输出安全不能由前端或 Provider 自报结果替代。

因此本设计不会为“绝不泄露首个违规 Token”缓冲多个事件，也不会声称可以撤回已发内容。启用输出规则时
以一个完整 SSE event 为最小放行单元：只为完成协议分帧保留到终止空行，检查点到达时先扫描尚未转发的
完整 event；更早已经交给下游的 event 只记录为 `partial` 暴露事实。

## 2. 需求理解

PRD-071 在现有输入 DLP、敏感意图、会话风险和流式 Watchdog 之后，补齐 Provider **输出方向**的安全
控制。它不改变默认上游范围：智谱、通义、文心、火山、混元、DeepSeek 官方/云服务和客户本地
OpenAI-compatible Endpoint 仍是默认上游；OpenRouter、硅基流动只作为竞品或迁移兼容对象。

本条的核心不是保存或展示模型正文，而是形成以下可验证事实：

1. 哪个 tenant 的哪个 request 执行了哪种扫描模式。
2. 扫描使用哪个冻结规则集版本和固定检测器版本。
3. 是否 clean、log-only 命中、阻断或检测失败。
4. 流式阻断前有多少块/估算 Token 已经交给响应生成器。
5. 阻断是否生成账单争议/冲正复核标记和持久事件。
6. 客户端收到的错误是否只含固定码、固定文案及 request/trace ID。

Prompt、Response、命中片段、规则 pattern、Authorization、Provider Key、Virtual Key 明文及底层异常文本
都不属于上述事实。

## 3. 范围与非目标

### 3.1 本 PRD 实现

- Chat Completions 非流式模型输出全文扫描。
- Chat Completions SSE 流式输出的每 N 个估算 Token 滑窗扫描。
- 复用当前 tenant 中 `scan_scope=output|both` 的 active DLP keyword/安全 regex 规则，形成一次请求内冻结规则快照。
- block、log_only、allow 三种既有动作的输出方向解释。
- 标准 JSON/SSE `content_policy_violation` 错误。
- OutputSafetyScan、阻断事件和不可变账单复核标记。
- `security.output_blocked` Webhook 严格合同。
- PAGE-099 输出安全事件页。
- PAGE-100 现有调用日志详情中的输出安全卡片。
- PAGE-099/PAGE-100 复用后端 `request_log.read` 权限，保持 tenant 隔离、统一错误和 request/trace ID。
- DLP 管理 API 与 Console 增加 `scan_scope=input|output|both`；历史规则和新建默认值均为 `input`。
- 0063 迁移、降级门禁、主/异常/权限/兼容测试和实际运行验收。

### 3.2 明确不做

- 不承诺撤回已经发送给客户端的流式内容。
- 不在违规后切换备用模型或重放请求。
- 不把上游 Provider 的 moderation 字段当作唯一权威结论。
- 不引入外部审核模型、远程内容安全 SaaS、MCP、硬件盒子、信创或复杂出境链路。
- 不保存完整 Prompt、Response、命中片段或可字典反推的命中文本 hash。
- 不自动创建 `finops_ledger_entries.reversal`；财务冲正仍由现有 admin-only FinOps 流程执行。
- 不把历史输入 DLP 规则隐式扩展到输出；只有管理员明确设为 `output` 或 `both` 的规则参与输出扫描。
- 不扫描 Embeddings 数组或图片/音频/视频二进制输出。
- 不保证任意厂商私有流协议；只处理 Provider Adapter 已声明的 OpenAI-compatible JSON/SSE 文本投影。

## 4. 现有代码基线

### 4.1 Gateway

- `api/openai_compatible.py` 已有非流式主/备最多两次状态机、StreamSession、SSE Watchdog、客户端断开、
  Usage 收口、统一 SSE error 和预算预留结算。
- `_SseUsageCollector` 已有 1 MiB 单事件内存门禁、SSE 分帧、usage 提取和输出字符计量；当前解析失败会继续
  透传，PRD-071 必须把它收敛为 Usage/输出安全共享的严格有界 framer。
- `dlp/service.py` 已有 tenant-first active 规则加载、关键词/安全 regex、block/log_only 和明文不落库合同；
  0063 必须在不改变历史输入行为的前提下增加方向筛选。
- `streaming/sessions.py` 当前终态只有 completed/timed_out/client_disconnected/upstream_failed。
- `usage/ledger.py` 已保证成功和失败用量、价格快照、FinOps 正式分录及 crash spool 可追踪。

### 4.2 Control 与 Console

- `GET /request-logs/{request_id}` 已按 tenant 和 `request_log.read` 返回路由、DLP、意图、流会话、Usage 等
  安全 DTO。
- PAGE-098 的 owner/generation、严格 DTO、权限裁剪和响应式列表/详情可作为 PAGE-099 的实现模式。
- 控制台导航由后端权限结果决定，但后端路由仍必须逐请求重新校验权限。

### 4.3 数据库与 RBAC

- 当前迁移最高为 0062，RBAC catalog revision 为 7，共 59 个权限模板；PAGE-099/PAGE-100 均可复用既有
  `request_log.read`，本 PRD 不新增权限模板或提升 catalog revision。
- `gateway_request_claims` 在请求正文和 Provider 调用前建立 tenant/request 不可变 owner。
- `request_logs`、`stream_sessions`、`usage_ledger`、`finops_ledger_entries` 已有复合 tenant owner 与状态约束。

## 5. 总体技术设计

```text
认证/tenant -> 输入 DLP/Intent/Session Risk -> Rate/Budget/Route
                                                |
                                                v
                          创建 pending owner -> 冻结 OutputRuleSnapshot
                                                |
                         +----------------------+----------------------+
                         |                                             |
                  非流式 Provider                               流式 Provider
                         |                                             |
                 完整响应内存投影                       SSE 分帧 -> Token 检查点
                         |                                             |
                 output_scan(full)                       output_scan(window)
                         |                                             |
             clean/matched | blocked               clean | blocked/unavailable
                         |                                             |
               返回 JSON 或固定错误             转发块 | 关闭上游 + SSE error
                         +----------------------+----------------------+
                                                |
                    OutputSafetyScan 终态 + source event + Usage/争议标记
                                                |
                               RequestLog / Usage / FinOps / Webhook
```

### 5.1 冻结规则快照

`prepare_output_safety_scan()` 在 tenant 已认证、request claim 已建立、Provider 尚未调用时执行一次：

- 先创建引用 `(tenant_id, request_id)` claim 的 `pending` OutputSafetyScan owner，再读取规则；即使进程随后中断，
  也能把“正在处理/崩溃残留”与历史缺证区分开。
- 只读取 `WHERE tenant_id=? AND status='active' AND scan_scope IN ('output','both')` 的 DLP 规则；输入路径仍只
  读取 `scan_scope IN ('input','both')`，历史 `input` 默认值不改变既有行为。
- 按 priority、updated_at、rule ID 固定排序。
- keyword 保留内存字符串；regex 使用现有 `compile_safe_dlp_regex()` 编译。
- 快照只在当前请求内存中存在，包含规则 ID、类型、动作、优先级和 matcher。
- `ruleset_fingerprint` 由 rule ID、pattern_hash、scan_scope、动作、优先级、updated_at 的 canonical JSON 计算 SHA-256；
  持久层只保存 fingerprint，不保存 pattern_hash 列表或 pattern。
- 数据库、规则或正则损坏时把 pending 推进为 `scan_status=unavailable + decision=unknown`，在 Provider 前返回
  `gateway.output_safety_unavailable`，不产生上游成本。
- 没有 active output/both 规则时仍返回带空规则集 fingerprint 的冻结快照。每个进入该阶段的 Chat 请求必须持久化
  `scan_status=not_configured + decision=none` 扫描事实，不能用“没有 scan 行”推断无规则或 clean；这会增加一条小型元数据写入，
  但消除了历史请求、进程中断和未配置规则之间的安全歧义。Provider/路由在产生可扫描输出前失败时推进为
  `not_applicable`；不得把这种请求记为 clean。

### 5.2 内部 `output_scan`

内部 API 不是对租户公开的 HTTP 文本扫描接口，而是 Gateway 内部纯服务合同：

```python
scan_full_output(snapshot, response_payload) -> OutputScanDecision
SlidingOutputScanner(snapshot, interval_tokens, window_tokens)
  .feed_sse_frame(fragments, terminal=False) -> OutputScanDecision | None
  .finish() -> OutputScanDecision
```

决策复用输入 DLP 的优先语义：任一 block 命中优先阻断，否则选择冻结排序中的首条 match；`match_count`
统计本次请求命中的不同规则数而不是重复检查点次数，避免长流把同一规则重复命中夸大成多条风险。

这样不会新增一个可被用户拿来提交任意敏感正文的管理 API，也避免 Control API 成为第二条内容处理链路。

### 5.3 pending 创建与提交顺序

- 非流式：已提交 claim -> INSERT/COMMIT pending scan -> 冻结规则 -> Provider/Fallback。
- 流式：已提交 claim -> 创建并提交 starting StreamSession -> INSERT/COMMIT 带 `stream_session_id` 的 pending
  scan -> 冻结规则 -> Provider handshake。禁止先插空 session ID 再借终态 UPDATE 补 owner。
- pending INSERT 失败时 Provider 调用数必须为 0；流式已存在的 starting session 用现有 `upstream_failed +
  gateway.output_safety_unavailable + quality=false` 收口，因为尚未 active，不能伪造“输出处理中断”。
- snapshot 失败时非流式 pending -> unavailable；流式 pending -> unavailable 与 starting session 的上述
  upstream_failed 在同一事务提交。只有流已 active 后的本地 parser/matcher 故障才使用新
  `output_safety_unavailable` StreamSession 终态。
- 零 output/both 规则时 pending 可在 Provider 前终结为 `not_configured + decision=none`，随后完全沿用原始
  Provider/Usage 网络路径；其含义只是“当时未配置输出规则”，不声称 Provider 成功或响应完整。

## 6. 非流式全文检测

### 6.1 文本投影

只扫描 Provider Adapter 已确认成功的 OpenAI-compatible Chat 响应中以下模型生成字段：

- `choices[].message.content` 的字符串或 `{type:"text", text:"..."}` 文本 part。
- `choices[].message.refusal`。
- `choices[].text`。
- 兼容响应中的 `choices[].delta.content`。
- `choices[].message.tool_calls[].function.arguments` 字符串。

未知扩展字段不自动进入扫描，避免把 Provider usage、ID 或签名误当正文；但已知文本字段若出现未知结构、
重复/循环对象或超过边界，则失败关闭，不把结构损坏解释为 clean。

### 6.2 全文边界

- 所有已知文本字段必须一次完整扫描，不允许 head/tail 抽样。
- 单响应文本上限为 262,144 code points；超过上限返回 `gateway.output_safety_unavailable`，不向客户端
  发送未经完整扫描的响应。
- 扫描只在内存中进行；决策对象只含数值、枚举、规则 ID 和版本。
- block 命中时客户端收到 HTTP 403、`content_policy_violation`、统一 request/trace ID；响应正文完全不返回。
- log_only/allow 命中形成 `scan_status=completed + decision=matched` 扫描事实后正常返回。
- clean 在证据提交成功后正常返回；证据提交失败时失败关闭。

### 6.3 Provider、Attempt 与 Usage 语义

- Provider 已成功响应，因此 RoutingAttempt 保持 `succeeded`；输出策略阻断不是 Provider 质量失败。
- Cache Affinity lifecycle 与启动恢复必须识别 active 后的 `output_blocked/output_safety_unavailable`，把 Selection
  终结为 Provider 成功的 `succeeded` 而非失败/悬空 pending；握手前 not_applicable 仍沿用真实 Provider 结果。
- 输出策略错误不触发 PRD-036 Fallback。
- 即使响应被阻断或成功响应后的扫描器不可用，Provider 可能已经计费，Usage/PriceSnapshot/FinOps 都按真实
  或估算结果持久化；只有 blocked 增加 dispute。
- scan 事务只冻结 `billing_disposition=dispute_pending` 意图；dispute 必须与 Usage/正式 charge 在另一个单一
  事务创建，`reversal_eligible` 按正式 charge 是否大于零计算。
- `safe_record_usage_ledger()` 为本 PRD 返回明确 `committed/spooled/lost` durability，而不是用同一个 `None`
  混淆 spool 成功和双失败。block/post-provider unavailable 在发送 JSON/SSE 安全错误前要求 committed 或 spooled；
  若主库与 spool 同时失败，非流式和仍连接的流式客户端收到更高优先级
  `finops.usage_record_unavailable`，scan 决策
  保持可查但不发布 Webhook，不能伪称账单争议已可靠落盘。
- 自动安全阻断不直接创建 reversal，避免网关在未完成财务核对时擅自修改正式账本。

## 7. 流式滑窗检测

### 7.1 首字与检查点

- 默认 `interval_tokens=32`，合法范围 8..256。
- 默认 `window_tokens=256`，合法范围 32..1024，且必须大于等于 interval。
- Token 使用与现有本地 Usage 估算一致的确定性近似：CJK 字符按 1 Token，其他非空字符每 4 个向上取整。
- 不为凑满第一个 Token 检查点缓冲多个 event；第一个完整 event 一到终止空行即可放行，额外时延只来自
  必需的协议分帧，而不是 N-Token 整流。
- 达到一个或多个逻辑检查点时，在 `yield` 当前完整 event 之前扫描包含当前文本的最新窗口。
- 一个 Provider event 跨过多个检查点时逐检查点推进文本边界；不会只用最终窗口掩盖中间命中。
- 启用规则时每个 raw chunk 先进入 framer，framer 可产生零个、一个或多个完整 event；每个 event 固定执行
  `usage_collector.observe(frame) -> scan(frame.fragments) -> session.note_chunk(frame.raw_bytes) -> yield raw_bytes`。
  因此命中 event 已计入 Provider observed usage但不计入 emitted；同一 raw chunk 中更早已通过的完整 event
  可以先行发送。零规则模式则保留既有 raw passthrough 与宽容 Usage 观察顺序。

### 7.2 窗口与跨块命中

- 保留最近 `window_tokens` 对应文本，并额外保留最多 512 code points 的规则边界重叠。
- SSE byte 分片由 Usage 与输出安全共用的 `BoundedSseEventFramer` 完成事件边界和 UTF-8 增量解码，禁止两套
  parser 对同一字节流产生不同结论。半个 UTF-8 字符或半条 data 行不进入 matcher。
- 每个合法 JSON 事件只投影 `choices[].delta.content`、`choices[].text`、`choices[].delta.refusal` 和
  `choices[].delta.tool_calls[].function.arguments` 文本；usage、ID、签名及未知扩展字段不进入 matcher。
- 每个 fragment 携带稳定 lane `(choice_index, field_kind, content_part/tool_call_index)`；同一 lane 跨事件连续
  匹配，不同 choice、content/refusal/text 或不同 tool call 绝不拼接，避免制造假命中或让真实分片绕过。
- framer 必须覆盖跨 raw chunk 事件、CRLF/LF、多 `data:` 行按 SSE 规范用换行拼接、注释/keepalive、未知
  字段忽略以及一个 raw chunk 多事件；正文 data 必须是合法 OpenAI-compatible JSON。
- JSON 使用拒绝 duplicate key 的 strict decoder；通过后仍原样转发 frame raw bytes，扫描器和客户端不得因
  重复 `choices` 等键采用不同解释。
- 启用输出规则时禁止透传未闭合 event 的 raw 前缀，否则跨 chunk 命中后追加标准 `event:error` 会落入尚未
  结束的 data 行并形成无效协议；comments/keepalive 也按完整 event 释放。
- 同一关键词/regex 跨 SSE event、跨 TCP chunk 时仍可命中。
- 单 SSE event 延续现有 1 MiB 上限；存在 output/both 规则时，事件/行超限、非法 UTF-8、畸形 JSON或 EOF
  残缺事件均作为扫描不可用，停止流而不是透传未知正文。零输出规则时保持 PRD-070 的 Usage 容错透传，
  framer 只停止解析计量并由既有估算回退收口，不能因未配置能力改变网络行为。异常对象和 `repr` 不得携带
  原始 event/data。
- Provider 的 `[DONE]` 或 EOF 到达时强制执行尾窗检查；尾窗通过后才转发/确认终止事件。
- keyword 的跨窗保证覆盖现有 512 code points 最大 pattern；regex 明确按当前有限滑窗求值，不承诺匹配
  跨越完整窗口以外的任意长上下文。管理页和事件不得把“滑窗未命中”描述成对完整历史输出的全文证明。
- Token 估算忽略空白，因此每 lane 另设 8,192 code points、单请求所有 lane 合计 65,536 code points 的硬内存
  上限；超限按有限窗口保留最新边界并继续计数，绝不能让连续空白/零 Token event 使缓冲无界。规则最大 512
  code points 的 keyword 边界仍完整保留，regex 继续只承诺当前有限窗口。

### 7.3 阻断顺序

命中 block 时严格执行：

1. 当前尚未 `yield` 的完整 SSE event 不发送；同一 raw chunk 中已经通过并发送的更早 event 不承诺撤回。
2. 立即关闭上游流；不 fallback、不重试、不等待更多 chunk。
3. 在一个数据库事务中把 StreamSession 从 active 推进为 `output_blocked`，并把 pending scan 推进为
   blocked、提交 output safety source event；任一失败全部回滚。
4. `output_blocked` 错误码固定为 `content_policy_violation`，质量资格固定为 false。
5. 向仍连接的客户端发送且只发送一个标准 `event: error`。
6. finally 中继续结算 RequestLog、Usage、PriceSnapshot、FinOps、append-only dispute marker、预算预留和并发租约。

SSE error 数据固定为：

```json
{
  "error": {
    "code": "content_policy_violation",
    "message": "Model output was blocked by content policy.",
    "request_id": "req_...",
    "trace_id": "trc_...",
    "output_safety_scan_id": "oss_...",
    "already_emitted": true,
    "emitted_token_count": 41
  }
}
```

客户端不接收规则 ID、规则名、pattern、命中类别文本、Provider 信息或任何异常原文。

### 7.4 已输出状态

- `none`：阻断前没有任何模型正文块交给响应生成器。
- `partial`：至少一个正文块已经交给响应生成器，后续被中断。
- `complete`：流正常结束，或 log_only 命中但完整输出继续发送。
- `unknown`：本地/数据库故障导致无法证明暴露边界；不能显示为 none。

`emitted_token_count` 只按实际已经通过安全检查并交给生成器的模型文本估算；Usage 的 output_tokens 则按
Provider usage 或 Gateway 已收到的完整文本估算。两者故意分开，前者描述客户暴露，后者描述潜在计费。

### 7.5 故障语义

- matcher、SSE parser 或规则快照异常：关闭上游，StreamSession 写 `output_blocked` 之外的
  `upstream_failed` 不合适，因此 0063 同时增加独立 `output_safety_unavailable` 终态，pending scan 推进为
  `scan_status=unavailable + decision=unknown/matched`。
- 能提交失败事实时，向客户端发送 `gateway.output_safety_unavailable` SSE error，并记录 exposure。
- 扫描证据数据库本身不可用时仍停止输出；RequestLog/StreamSession/Usage 采用现有 best-effort/fail-safe
  收口，结构化日志只记录固定错误码和异常类型。
- 输出安全失败不计入 Provider 质量失败，不触发 Fallback。
- timeout、client_disconnected、upstream_failed 等非安全终止若发生在尾窗完成前，OutputSafetyScan 记录
  `scan_status=unavailable + decision=unknown/matched + output_safety.stream_incomplete`，并与原 StreamSession
  终态在同一事务提交；它不覆盖原公开
  timeout/disconnect/upstream 错误，也不创建 output-blocked Webhook 或账单争议标记。

### 7.6 pending 阶段矩阵

| 阶段 | scan_status + decision | Provider/流终态 | 对外错误与账单 |
| --- | --- | --- | --- |
| 规则快照失败，Provider 未调用 | `unavailable + unknown` | 非流无 attempt；流 starting->upstream_failed | `gateway.output_safety_unavailable`，无上游费用 |
| output/both 规则为零 | `not_configured + none` | 原流程不变 | 在 Provider 前即可终结；不启用 event gate，不生成 dispute |
| 非流式主模型失败但允许 fallback | 保持 `pending` | 第一 attempt 原语义 | 等待最终候选，不提前终结或触发扫描 |
| 非流式主备均失败 | `not_applicable + none` | 原 Provider 错误 | 保留原错误，无 dispute |
| 非流式最终候选成功并扫描完成 | `completed + clean/matched/blocked` | 成功 attempt 保持 succeeded | block 不再 fallback；真实 Usage 继续结算 |
| 非流式成功后 scanner/evidence 失败 | `unavailable + unknown/matched` | attempt/affinity 成功 | 503、真实 Usage；不 dispute |
| 流式握手未到 2xx | `not_applicable + none` | 原 `upstream_failed` | 保留原握手错误和质量资格 |
| 流式 active 后正常到 `[DONE]`/EOF | `completed + clean/matched/blocked` | completed 或 output_blocked | 尾窗后终结；blocked 才 dispute |
| 流式 active 后 parser/matcher 本地失败 | `unavailable + unknown/matched` | `output_safety_unavailable` | 固定 503 SSE；Provider/Cache Affinity 不受处罚 |
| 流式 active 后 timeout/disconnect/upstream read failure | `unavailable + unknown/matched` | 保留原终态 | 原错误优先，reason=stream_incomplete，无 dispute |
| 已配置规则且进程在终结前崩溃 | 保持 `pending` | 不伪造终态 | PAGE-100 显示中断待核查，不能推断 clean/not_configured |

矩阵中的 scan 与已有 StreamSession 同时需要终结时必须走同一数据库事务；Fallback 只消费同一个 pending
owner 和冻结 snapshot，不能为第二 attempt 新建 scan。

## 8. 数据库设计与 0063 迁移

### 8.1 `dlp_rules.scan_scope`

0063 为既有 `dlp_rules` 增加 `scan_scope TEXT NOT NULL DEFAULT 'input' CHECK
(scan_scope IN ('input','output','both'))`：

- 所有历史行迁移后固定为 `input`；新建规则省略该字段时也固定为 `input`，防止升级后无意扫描模型输出。
- 输入 DLP 热路径只加载 `input/both`，输出安全只加载 `output/both`。
- DLP list/create/update exact DTO 增加 `scan_scope`，仍分别要求既有 `dlp.read/dlp.write` 后端权限。
- DLP Audit 白名单摘要增加 scan_scope；输入热路径继续精确按 `priority ASC, updated_at DESC, name ASC` 排序，
  不能因输出快照采用 rule ID 兜底而改变 PRD-024 的既有 tie-break。
- DLP Console 使用“输入 / 输出 / 双向”选项控件，并在列表中显示方向；未知值、重复字段和跨 tenant ID 均拒绝。
- 新索引 `(tenant_id,status,scan_scope,priority)` 支撑两条热路径；不能仅靠应用层过滤。

### 8.2 `output_safety_scans`

每个 Chat request 最多一条扫描 owner。Provider 前先 INSERT `pending`，随后只允许一次合法终态 UPDATE；终态
不可再更新，所有行禁止 DELETE/REPLACE：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `output_safety_scan_id` | TEXT PK，`oss_<32hex>` | 稳定扫描 ID |
| `tenant_id` | TEXT NOT NULL | 第一 owner 条件 |
| `request_id` | TEXT NOT NULL | 与 gateway claim 复合 owner |
| `trace_id` | TEXT NOT NULL | 必须与 claim 相同 |
| `stream_session_id` | TEXT NULL | 仅流式模式；同 tenant/request owner |
| `scan_mode` | `non_stream_full/stream_window` | 固定模式 |
| `scan_status` | `pending/not_configured/completed/unavailable/not_applicable` | 扫描生命周期/完整性 |
| `decision` | `none/clean/matched/blocked/unknown` | 内容判定，与完整性正交 |
| `action` | `allow/log_only/block` NULL | 仅 decision=matched/blocked 可用 |
| `dlp_rule_id` | TEXT NULL | 决定性规则 ID；不复制 pattern |
| `rule_type` | `keyword/regex` NULL | 安全枚举 |
| `match_count` | INTEGER >= 0 | 命中规则数量 |
| `ruleset_fingerprint` | 64 位小写 hex NULL | 冻结规则集证据；快照前故障可空 |
| `detector_version` | 固定字符串 | 初版 `output-safety-dlp-v1` |
| `interval_tokens` | INTEGER NULL | 仅流式 |
| `window_tokens` | INTEGER NULL | 仅流式 |
| `checkpoint_count` | INTEGER >= 0 | 实际检查次数 |
| `observed_output_token_count` | INTEGER >= 0 | Gateway 已从 Provider 观察到的估算 Token |
| `scanned_token_count` | INTEGER >= 0 | 已进入扫描器的估算 Token |
| `emitted_token_count` | INTEGER >= 0 | 已向下游生成器交付的估算 Token |
| `emitted_sse_event_count` | INTEGER >= 0 | 已交付正文 SSE 事件数 |
| `emitted_bytes` | INTEGER >= 0 | 已交给 ASGI 的 raw bytes；不冒充 TCP 确认 |
| `exposure_state` | `none/partial/complete/unknown` | 客户暴露边界 |
| `billing_disposition` | `not_required/dispute_pending` | 账单结算意图；非自动冲正状态 |
| `failure_reason_code` | TEXT NULL | 仅 fixed reason，不存异常文本 |
| `started_at/completed_at` | UTC TEXT/NULL | pending 的 completed_at 为空 |

关键约束：

- `UNIQUE(tenant_id, request_id)`；复合 FK 指向 `gateway_request_claims(tenant_id, request_id)`，trigger 复核 trace。
- stream 模式必须有同 tenant/request 的 StreamSession；非流式必须为空。
- 只允许 `pending -> not_configured|completed|unavailable|not_applicable`；并发/重复终结只有全部字段
  精确相同时幂等，任何第二种终态均为完整性冲突。
- not_configured 必须 decision=none、空规则集 fingerprint，action/rule/match/failure 为空；它不等于 clean。
- completed 必须 decision=clean/matched/blocked；blocked 必须 action=block、规则非空、exposure 为 none/partial、
  billing_disposition=dispute_pending；clean/matched 正常结束时 exposure=complete。
- unavailable 必须有固定 failure reason；尾窗前若已知 log_only/allow 命中，可保留 decision=matched 与安全规则
  元数据，否则 decision=unknown，绝不能把不完整历史伪造成 clean。scan_status 与 decision 共同构成最终证据。
- not_applicable 必须 decision=none，只用于已配置规则但 Provider 未产生可扫描成功输出的请求；进程崩溃遗留
  pending 不能伪装成该终态。
- pending/not_configured/not_applicable 的 exposure 固定 unknown、emitted/observed 计数为 0，因为它们不证明响应
  交付；unavailable 只允许 none/partial/unknown，非流式完整响应在返回前失败时为 none。
- 流式握手/上游失败发生在首个可扫描事件前时，原 StreamSession 终态与 scan not_applicable 在同一事务提交；
  非流式主备均失败时由请求异常收口终结 pending，不能留下“仍在扫描”的假运行态。
- pending 到终态的唯一 UPDATE、终态不可变、REPLACE/DELETE 防护和 tenant owner trigger 均由数据库执行。

### 8.3 `finops_ledger_disputes`

账单复核采用 append-only dispute marker，不修改真实 Usage 或正式 FinOps charge：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `finops_ledger_dispute_id` | `fld_<32hex>` PK | 争议标记 ID |
| `tenant_id/request_id/trace_id` | NOT NULL | 与 scan/claim/账本同 owner |
| `output_safety_scan_id` | NOT NULL UNIQUE(owner) | 必须是 completed+blocked scan |
| `usage_ledger_id` | NOT NULL | 实际/估算上游用量事实 |
| `finops_ledger_entry_id` | NOT NULL | 对应正式 charge 分录 |
| `review_status` | 固定 `dispute_pending` | 待人工复核，不自动变更 |
| `reversal_eligible` | 0/1 | 在 charge 事务按 `amount_micros > 0` 冻结；零金额不可冲正 |
| `reason_code` | `output_safety.content_policy_violation` | 固定原因 |
| `created_at` | UTC TEXT | 创建时间 |

该 marker 与 Usage/FinOps charge 在 `record_usage_ledger()` 同一事务幂等创建，禁止 UPDATE/DELETE/REPLACE。
正式上游费用始终保留；管理员仍通过既有 `finops.write`、显式原因、审计和幂等 reversal 流程处理，页面通过
reversal 分录派生“已冲正”，数据面不得自动生成 reversal。零金额 charge 仍保留 dispute_pending 供复核，但
`reversal_eligible=0`，API/Webhook/UI 只能显示“待复核”，不能声称“可冲正”。

### 8.4 `output_safety_events`

- `event_id`：`osevt_<32hex>`。
- tenant、scan、request、trace、发生时间全部与 completed+blocked scan 一致。
- 只保存构造 Webhook 所需的固定字段，不保存通用自由 JSON。
- 同一 scan 唯一事件，append-only。

### 8.5 StreamSession 与 RequestLog 兼容迁移

0063 需要把以下 CHECK/trigger 扩展为精确新状态：

- `stream_sessions.status` 增加 `output_blocked`、`output_safety_unavailable`。
- `request_logs.stream_status` 同步增加两种状态。
- 状态机只允许 active -> 两种新终态；starting 不能直接伪造已输出阻断。
- StreamSession `output_blocked` 只允许 `content_policy_violation`，`output_safety_unavailable` 只允许
  `gateway.output_safety_unavailable`；它们描述扫描终态。RequestLog 默认相同，但若随后 Usage 主库与 spool
  双失败，可在保持 stream_status 的同时记录更高优先级 `finops.usage_record_unavailable`，并由 scan join 保留
  PAGE-099 的安全阶段归类。
- 两种状态的模型质量资格固定 false。

SQLite 通过仓库现有的列/表重建模式完成，必须保持全部既有列、rowid、数据、索引和 trigger 精确一致。
流式终结服务增加“同事务终结 pending OutputSafetyScan”的内部合同：completed、output_blocked、
output_safety_unavailable 以及未完成扫描的原 timeout/disconnect/upstream 终态，都不能把 scan 与 session 分两次
提交后再发布事件。

### 8.6 tenant owner 候选键与复合 FK

0063 明确新增/保留以下候选键，不能只依赖全局 ID 后另存一个可错配 tenant_id：

- `dlp_rules UNIQUE(tenant_id, dlp_rule_id)`。
- `stream_sessions UNIQUE(tenant_id, request_id, stream_session_id)`。
- `output_safety_scans UNIQUE(tenant_id, output_safety_scan_id)` 以及
  `UNIQUE(tenant_id, request_id, output_safety_scan_id)`。
- `usage_ledger UNIQUE(tenant_id, request_id, usage_ledger_id)`。
- `finops_ledger_entries UNIQUE(tenant_id, source_request_id, finops_ledger_entry_id)`。

FK/trigger 图固定为：

- scan `(tenant_id,request_id)` -> claim；可空 `(tenant_id,dlp_rule_id)` -> DLP rule；流式
  `(tenant_id,request_id,stream_session_id)` -> StreamSession。
- dispute `(tenant_id,request_id)` -> claim，`(tenant_id,request_id,output_safety_scan_id)` -> blocked scan，
  `(tenant_id,request_id,usage_ledger_id)` -> Usage，`(tenant_id,request_id,finops_ledger_entry_id)` -> 正式 charge。
- event `(tenant_id,request_id)` -> claim，`(tenant_id,request_id,output_safety_scan_id)` -> blocked scan。

触发器再次核验 trace_id 一致、scan 为 `scan_status=completed + decision=blocked`、Usage/charge 均属于同一 request、
FinOps entry 为对应 usage_charge、dispute eligibility 与 charge 金额一致。跨 tenant、跨 request、跨 trace 或把
非 blocked scan 接到 event/dispute 的写入全部拒绝。

### 8.7 RBAC 保持 revision 7

PAGE-099 与 PAGE-100 均复用 `request_log.read`：Admin、SecurityAdmin、Developer、OpsAdmin 允许；Viewer、
FinanceAdmin 默认拒绝。每个 Control 请求仍由后端重新读取权限矩阵，Console 隐藏导航不参与授权结论。
0063 不新增权限模板、角色关系或 revision，自定义角色保持原样；DLP 方向编辑仍由既有 `dlp.write` 控制。

### 8.8 down migration

存在任一 OutputSafetyScan、dispute、source event、非 `input` DLP 规则、Webhook 订阅/投递、Audit 引用或未知
后续 schema 时原子拒绝降级。空事实降级必须完整移除 `scan_scope` 并恢复旧 StreamSession/RequestLog 状态集合、
原 rowid、索引、trigger 和 revision 7 权限矩阵；不得为了降级删除业务事实或方向配置。

## 9. API 设计

### 9.1 Gateway 内部 `output_scan`

仅代码内部调用，不注册公网 HTTP 路由。tenant、request、trace、route 和规则快照只能来自已认证上下文。

### 9.2 PAGE-099 复用 `GET /request-logs`

不新增第二套公开安全日志 API。PAGE-099 调用现有 RequestLog 列表并固定
`terminal_stage=output_safety`，后端权限仍为 `request_log.read`，tenant 只来自 Control Session：

- RequestLog 查询按 tenant/request LEFT JOIN 唯一 scan；`TERMINAL_STAGE_SQL` 优先把
  `decision=blocked` 或 `scan_status=unavailable` 映射为 `output_safety`，再兼容精确错误码
  `content_policy_violation/gateway.output_safety_unavailable`。因此 Usage durability 双失败即使公开码升级为 FinOps
  503，PAGE-099 仍不会漏掉已持久化安全事件；签名 cursor、时间、request ID 和其他筛选合同保持不变。
- 列表项新增可空 `output_safety` 摘要，只含 scan ID、模式、状态、exposure、emitted 计数和
  billing disposition；无 DLP pattern、命中正文或 Provider secret。
- PAGE-099 只展示 blocked/unavailable 安全事件；clean/matched/not_configured/pending 仅在 PAGE-100 详情中
  作为请求证据显示，不把普通成功请求混入安全事件列表。
- 未知/重复 query、非法 terminal_stage、越界时间和损坏 cursor 继续使用 RequestLog 既有 400 合同。

### 9.3 RequestLog 详情扩展

`GET /request-logs/{request_id}` 仍要求 `request_log.read`，新增可空 `output_safety`：

- 未配置规则：读取持久化 scan 的 `scan_status=not_configured + decision=none` 固定投影。
- 有 scan：返回 scan ID、模式、状态、动作、规则 ID/type、版本、检查/observed/emitted/暴露计数和争议标记。
- `pending` 明确显示“处理中/中断待核查”，不能推断 clean；`not_applicable` 明确显示 Provider 未产生可扫描输出。
- 表行损坏、跨 tenant owner 或 dispute 与 blocked 状态不一致时返回
  `output_safety.integrity_failed`，不能降级为空或 clean。
- 没有 scan 行时，Control 使用 0063 migration applied_at 和 RequestLog 终止阶段区分：迁移前成功 Chat 为
  `legacy_unavailable`；Provider 前失败或非 Chat 为 `not_applicable`；迁移后成功 Chat 缺 scan 则固定
  `output_safety.integrity_failed`，不能误报 not_configured。

### 9.4 DLP 管理 API 扩展

既有 DLP list/create/update DTO 增加必返 `scan_scope`；create 省略时默认 `input`，update 省略时保持原值。
只接受 `input|output|both`，后端继续执行 `dlp.read/dlp.write`、tenant-first owner 和并发版本校验；前端选项
不替代权限检查。

### 9.5 响应头

所有 Control JSON 响应及 Gateway 非流式错误继续返回 `X-Request-ID`、`X-Trace-ID` 和
`Cache-Control: no-store`。SSE 在 HTTP 200 后通过 error event 绑定相同 request/trace ID。

## 10. 错误码

| code | HTTP/流式 | 场景 |
| --- | --- | --- |
| `content_policy_violation` | 403 / SSE error | 模型输出命中 block |
| `gateway.output_safety_unavailable` | 503 / SSE error | 规则、parser、matcher 或证据不可用 |
| `finops.usage_record_unavailable` | 503 / SSE error | Provider 已产生成本但主库与 crash spool 均不可用 |
| `request_log.query_invalid/pagination_invalid/sort_invalid` | 422 | PAGE-099 非法筛选、时间、limit、cursor；沿用现有 RequestLog API 合同 |
| `request_log.not_found` | 404 | PAGE-100 当前 tenant 不存在该 request，跨租户同形 |
| `output_safety.integrity_failed` | 503 | scan/dispute/request owner 或状态组合损坏 |
| `dlp.rule_invalid` | 400 | DLP `scan_scope` 或其他管理字段非法 |
| `auth.token_missing/invalid/session_revoked` | 401 | Control 未认证 |
| `auth.permission_denied` | 403 | 缺少后端权限 |

Excel 对阻断码写明精确 `content_policy_violation`，因此公开 JSON、SSE、RequestLog 和 StreamSession 均保存并
返回该值，不另返回 `gateway.content_policy_violation` 别名；命名空间惯例只用于检测器不可用等内部 Gateway 错误。
错误 message/suggestion 使用固定目录文案，不拼接 rule、pattern、输出、Provider 错误或数据库异常。

## 11. `security.output_blocked` Webhook

exact payload：

```json
{
  "schema_version": 1,
  "output_safety_scan_id": "oss_...",
  "request_id": "req_...",
  "scan_mode": "stream_window",
  "scan_status": "completed",
  "decision": "blocked",
  "error_code": "content_policy_violation",
  "exposure_state": "partial",
  "emitted_token_count": 41,
  "checkpoint_count": 2,
  "dlp_rule_id": "dlp_...",
  "detector_version": "output-safety-dlp-v1",
  "billing_disposition": "dispute_pending",
  "occurred_at": "2026-08-04T05:00:00Z"
}
```

- `build_webhook_event()` 增加精确 payload/envelope validator。
- envelope object_id 必须等于 scan ID，trace/timestamp 必须等于 source event。
- source event 与 blocked scan/StreamSession 同事务；Usage/FinOps/dispute 成功提交或 v5 crash spool 已可靠落盘后，
  才机会性入现有有界 dispatcher。payload 的 billing_disposition 表示结算意图，不伪造财务已冲正状态。
- queue 满、endpoint 失败或死信不回滚阻断和账单标记。
- payload 不含 tenant 明文之外的正文身份数据；tenant 仍只存在标准 Webhook 信封中。
- `reversal_eligible` 依赖后续正式 charge 金额，不能在先提交的 append-only source event 中猜测，因此不进入
  Webhook payload；FinOps/RequestLog DTO 只从已提交 dispute 投影该字段。

## 12. 前端设计

### 12.1 PAGE-099 安全事件页

路由：`#output-safety`，导航名称“输出安全事件”，位于“安全与治理”。

页面复用 RequestLog owner、查询、cursor 和详情数据流，页面结构：

- 紧凑摘要：阻断、部分暴露、待账单复核、运行状态。
- 筛选栏：状态、模式、暴露状态、账单状态、时间范围、request ID。
- 主列表：时间、模式、状态、暴露、已输出 Token、规则引用、账单复核、request ID。
- 详情侧栏：直接复用 PAGE-100 日志详情，展示扫描参数、checkpoint、规则/版本、暴露边界、dispute 和 Webhook 事实。
- 桌面使用列表 + 详情双栏；`390x844` 改为文档流，不允许页面级横向溢出。
- 不使用卡片嵌套；状态使用固定枚举标签，不能展示输出片段。

### 12.2 PAGE-100 日志详情

在现有调用日志详情中新增“输出安全”区块，位于输入 DLP/Intent 之后、Usage/FinOps 之前：

- 显示 scan_status、decision、模式、动作、规则 ID、检查点、扫描/已输出 Token、exposure、账单复核。
- `not_configured` 明确显示“未配置”，不能显示为“安全”。
- blocked 显示“已中断；已发送内容不可撤回”的业务事实，不显示操作教程或原文。
- Usage 区块显示“输出安全账单复核：待争议”；仅 `reversal_eligible=true` 时附加“可冲正”，零金额不得显示，
  且不提供自动冲正按钮。

### 12.3 现有 FinOps 页面兼容扩展

- `billing.finops.read` 的正式账本列表/详情按同 tenant 的 source_request_id 关联 dispute marker，显示
  “输出安全争议待复核”徽标；只有正金额 eligible 时显示“可冲正”。FinanceAdmin 不会因此获得
  request_log.read 或原始安全详情。
- 只返回 scan ID、固定复核状态和 reversal eligibility，不返回 DLP rule、模式、暴露计数或正文。
- 现有 admin-only reversal API 保持显式人工操作；本 PRD 不在安全事件页或 FinOps 页自动提交冲正。

### 12.4 DLP 规则方向交互

- 既有 DLP 新建/编辑表单增加“检测方向”选项：输入、输出、双向；默认输入，并明确保存到 `scan_scope`。
- 列表增加紧凑方向列/筛选，使用既有 `dlp.read/dlp.write` owner 与 stale-response 清理合同。
- 只读用户可以查看方向，写操作仍必须由后端 `dlp.write` 拒绝；前端禁用控件不是权限证明。

### 12.5 权限与 owner 状态

- PAGE-099/PAGE-100 都只有 `request_log.read` 才有导航、DOM 和请求；Admin、Developer、SecurityAdmin、
  OpsAdmin 可读，Viewer、FinanceAdmin 默认无权。
- tenant、user、Login Session、permission revision、authorization fingerprint、navigation、filters、cursor、
  selection 或 generation 任一变化，立即 abort 并销毁旧列表、详情、错误和 cursor。
- 401 结束登录；403 刷新权限并清空页面；跨 tenant 的迟到响应不得写入新 owner。
- 所有字符串 exact-validate 后再 HTML escape；URL/Storage/console 不保存 scan filter 之外的敏感值。

## 13. 安全、租户与权限约束

1. Gateway tenant 只来自 Virtual Key/企业 Chat 可信上下文；Control tenant/actor 只来自 Session。
2. 所有查询和写入把 tenant_id 放在第一条件；数据库复合 FK/trigger 再次证明 owner。
3. PAGE-099/PAGE-100 每次请求都执行后端 `request_log.read`；前端隐藏从不替代后端校验。
4. active 输出 DLP 规则只按当前 tenant 与 `scan_scope IN ('output','both')` 加载；规则 ID 即使被猜中也不能
   跨 tenant 参与 snapshot，历史 input-only 规则不会进入输出扫描。
5. 输出正文只在 Provider response、SSE parser 和当前 scanner 的有界内存中短暂存在。
6. 日志只记录 code、tenant、request/trace、scan ID、规则 ID、枚举、数值和异常类名。
7. 不保存 match hash，避免对低熵违规短语做字典反推。
8. 数据库、Webhook、DOM、URL、Storage、截图 OCR 和运行日志都必须做敏感明文扫描。
9. 规则加载或扫描不可证明时失败关闭；`not_configured` 与 `clean` 必须严格区分。
10. output block 不是 Provider 质量错误，不降低模型评分，也不触发 Fallback。

## 14. 事务、并发与恢复

### 14.1 阻断事务

流式 pending scan -> `completed+blocked`、StreamSession `output_blocked` 和 source event 在一个 `BEGIN IMMEDIATE` 中
提交；非流式没有 StreamSession，其余两项同事务。Usage、正式 FinOps charge 和 dispute marker 随后在
`record_usage_ledger()` 的单一事务提交；主库失败时必须先把 v5 crash spool 原子落盘。扫描事务或结算/spool
任一均未可靠提交时，不发送 `security.output_blocked` Webhook。durability=committed/spooled 才允许派发；lost
改发既有 FinOps 503（客户端仍连接时）并保留 source event 待后续人工核查。

### 14.2 幂等

- 一个 tenant/request 只能有一个 pending owner，并只允许一次终态转换。
- 重复 finalizer 只有在全部终态字段精确相同才视为成功；不同 scan_status/decision/rule/exposure 视为完整性冲突。
- source event 和 dispute marker 对 scan/request 均唯一；spool 重放只可补齐同一 charge/dispute，不得重复。
- StreamSession finalizer 保持现有同终态幂等，不允许晚到 disconnect 覆盖 output_blocked。
- completed clean/matched、unavailable stream_incomplete scan 与对应 StreamSession 终态同事务提交；
  not_configured 已在 Provider 前独立终结。重复 finalizer
  只能接受完全相同的 scan/session 组合。

### 14.3 取消与断开竞争

- 已确认 output block 优先于同轮晚到 disconnect；已先确认 disconnect 时不再向客户端发送 error event。
- 无论哪个分支胜出，都必须关闭上游、取消 read/disconnect task、归还限流租约并结算预算。
- dispute marker 只在已持久化 block 决策且 Usage/charge 入账时创建；纯客户端断开不伪造安全争议。

### 14.4 Usage crash spool

Usage crash spool 对存在 OutputSafetyScan 的 Chat 请求从 v4 升为 v5；无 scan 的 Embeddings、历史/前置失败
请求继续写 v4。reader 精确接受 v1-v5 并拒绝未知 v6。v5 在 v4 字段上只增加
`output_safety_scan_id`、`output_safety_scan_status`、`output_safety_decision`、`billing_disposition` 四个安全字段：

- 不得写入窗口文本、匹配正文、Response payload、pattern、Header、Key、Token 或异常原文。
- blocked 必须携带 scan ID、`output_safety_scan_status=completed`、`output_safety_decision=blocked`、
  `billing_disposition=dispute_pending`；其他 scan 终态为 `not_required`。v1-v4 恢复时固定映射为无输出安全
  争议，不能猜测历史阻断。
- replay 在写 Usage/FinOps charge 的同一事务幂等创建 `finops_ledger_disputes`；重复 replay 不增加第二条事实。
- replay 发现 Usage/FinOps 已存在时不能直接返回，必须先验证或幂等补齐同 owner、同 scan 的 dispute，再返回
  committed 事实；冲突固定完整性失败。
- spool 文件写入/重命名失败仍使用现有固定错误码失败关闭，不能只保存 scan 而丢失账单争议意图。
- pending scan 在 Provider 前已独立 commit，且终态行禁止删除，因此任一 v5 spool 必有 scan 行；0063 down
  只要拒绝存在 scan 即同时证明没有可被旧 v1-v4 reader 遗弃的 v5 文件。新增非 Chat v4 -> down -> 旧 reader
  恢复用例验证该闭环。

## 15. 兼容性与性能

- OpenAI-compatible 请求/响应主结构不改，只有违规非流式改为统一错误、违规流式追加标准 error event。
- 未配置 active `output/both` DLP 规则时网络响应行为与 PRD-070 基线一致，但会增加一条小型
  `not_configured` 元数据行；历史 input-only 规则继续只保护输入，
  用于证明当时没有规则；该行不包含正文且不生成 Webhook。
- Provider Adapter 不新增品牌枚举；本地 OpenAI-compatible Endpoint 同样适用。
- 非流式只在成功响应后扫描一次，复杂度由 100 条规则和 262,144 code points 上限控制。
- 流式首检查点前不阻塞；每检查点只扫描固定窗口，不随完整输出长度无限增长。
- 性能门禁：64 Token 窗口、100 条混合规则的 P95 < 10ms；首字路径无整流缓冲；内存按并发流有界。
- 现有 timeout、disconnect、usage、fallback、session risk、DLP 和 spool v1-v4 读取测试必须保持兼容；
  spool writer/current 合同更新为 v5。

## 16. 测试计划

### 16.1 Shared/纯函数

- 全文 keyword/regex、block 优先级、log_only/allow、零规则、零文本、多 choice/tool arguments。
- 滑窗每 N Token、一个 chunk 跨多检查点、跨 event/chunk/UTF-8 边界、尾窗强制检查。
- framer 覆盖 CRLF/LF、多 data 行、注释/keepalive、单 chunk 多事件、跨 chunk 事件、畸形 UTF-8/JSON、
  duplicate JSON key、event/line 超限、EOF 残缺事件和 `[DONE]` 抑制；通过 frame 原 bytes 不变。
- delta.content/refusal/text/tool arguments exact 投影，同 lane 跨 event 命中，不同 choice/tool lane 不误拼接。
- CJK/ASCII/空白估算、最大窗口、最大响应以及 observed/scanned/emitted 三类计数分离。
- 连续空白/零 Token event 在 lane 及全请求 code-point 硬上限内，内存不随流长增长。
- 决策/repr/异常不含正文、pattern 或命中词。
- 100 规则性能和内存上界。

### 16.2 Gateway 非流式

- input-only 历史规则不扫描输出；output/both 规则分别覆盖非流式和流式，输入路径兼容不变。
- Provider 前存在 pending owner；clean 全文返回，log_only 返回且有 matched scan，零输出规则为 not_configured。
- block 返回 HTTP 403 `content_policy_violation`，正文不返回。
- 主模型成功/备用模型成功两条路径都扫描，block 不再 fallback。
- Provider attempt 保持 succeeded、模型质量不受处罚。
- Cache Affinity Selection 在 output_blocked/output_safety_unavailable 下终结 succeeded，启动恢复无 pending 残留。
- Usage/PriceSnapshot/FinOps 正常入账，append-only dispute 为 dispute_pending/reversal eligible。
- 规则/regex/证据故障返回统一 503，Provider 前规则快照故障调用数为 0。

### 16.3 Gateway 流式

- 首 chunk 在首检查点前直接输出，无整流缓冲。
- checkpoint block 关闭上游、当前块不输出、只发一个标准 error、无 `[DONE]`；当前块计入 observed usage，
  不计入 emitted，且 `session.note_chunk` 仅在扫描通过后调用。
- 跨 raw chunk 单 event 不泄露前缀；同 raw chunk 多 event 只放行命中前已通过的完整 frame，error 自成合法 event。
- 零输出规则保持 raw passthrough/Usage 容错；同一畸形 event 在启用规则时严格失败关闭。
- `already_emitted=false/true` 与 emitted Token/块数准确。
- 尾窗 block、跨 chunk keyword/regex、log_only 完整流、clean 完整流。
- timeout、disconnect、上游读失败、输出安全失败竞争和 finalizer 幂等。
- StreamSession 新终态、RequestLog、Usage、dispute marker、event 一致。
- stage 矩阵覆盖 snapshot failure、fallback pending、全部 attempt 失败、stream starting/active、log_only 后中断、
  零规则断开与进程残留 pending；scan_status/decision 不丢已知事实。
- Provider usage 不存在时，账单按收到文本估算；客户暴露按已发送文本单独记录。

### 16.4 迁移和数据库

- 0062 -> 0063 -> 0062 -> 0063，rowid/schema/index/trigger/权限矩阵精确。
- 历史/new default DLP 均为 input；input/output/both 两条热路径和管理 DTO/索引精确。
- RBAC 保持 revision 7、59 个 permission 与既有角色矩阵；自定义角色不扩权。
- scan/dispute/event tenant、trace、request、stream owner 错配全部被拒绝。
- pending 唯一 owner、合法单次终结；blocked/clean/unavailable/not_applicable 非法组合、第二终态、
  UPDATE/DELETE/REPLACE 被拒绝。
- 有 scan 的 writer v5、无 scan 的 writer v4、reader v1-v5、v6 拒绝；主库失败后 replay 生成唯一 charge/dispute。
- 无 scan 的 Embeddings/前置失败继续写 v4，v4 spool -> 0063 down -> 旧 reader 可恢复；任一 v5 均有 scan 行阻止 down。
- 零金额 charge 的 dispute_pending/reversal_eligible=false；正金额 true，现有 reversal 零金额拒绝合同不变。
- 有业务事实、Webhook/Audit/自定义权限或未知后续依赖时 down 原子拒绝。

### 16.5 Control 与权限

- Admin/SecurityAdmin/Developer/OpsAdmin 通过 `request_log.read` 读取；Viewer/FinanceAdmin 默认 403。
- 自定义角色只有 request_log.read 时可访问 PAGE-099/PAGE-100；DLP 编辑独立要求 dlp.write。
- 跨 tenant request ID 404；签名 cursor 绑定 tenant/filter/revision；PAGE-099 固定 output_safety 阶段。
- 非法 query、重复参数、空/反向时间、损坏 cursor、数据库 owner 污染。
- RequestLog 详情按 scan_status + decision 正确区分 pending、not_configured、completed clean/matched/blocked、
  unavailable unknown/matched 与 not_applicable。
- 迁移前成功日志显示 legacy_unavailable；迁移后成功 Chat 缺 scan 失败关闭。
- FinanceAdmin 只能读取账本复核徽标，不能读取 PAGE-099 或规则/暴露详情。

### 16.6 Webhook

- exact payload、envelope、object/trace/time owner、未知字段和非法枚举。
- Webhook 的 scan_status/decision、durability 门禁精确，并断言 charge-derived reversal_eligible 不进入 payload。
- commit 后入队、队列满不回滚、死信/重放保持原合同。
- payload、日志和签名输入不含正文、pattern、Key、Token。

### 16.7 Console

- strict exact-key RequestLog DTO、固定安全阶段筛选/cursor、详情和 XSS。
- request_log.read 导航/DOM/零预取；Viewer/FinanceAdmin 403 后清零，OpsAdmin 可读。
- DLP scan_scope 列表、创建、编辑、默认 input、只读/写权限和迟到响应 owner 清理。
- 401/403/tenant/Session/revision/fingerprint/迟到响应清理。
- `1280x720`、`390x844` 页面加载、筛选、详情、滚动、焦点和零横向溢出。
- 注销后列表/详情/scan ID DOM 清零；Console error 为 0。

### 16.8 真实运行验收

仓库内本地 OpenAI-compatible Provider 分别返回 clean 非流式、违规非流式和跨 chunk 违规流式：

- Gateway/Control/Console `/health` 均为 HTTP 200。
- 非流式违规 403；流式已有安全前缀后收到唯一 SSE error，Provider 被关闭。
- scan、dispute、event、RequestLog、StreamSession、Usage、FinOps 数量与 owner 精确。
- 匿名 401、Viewer 403、跨 tenant 404、依赖故障 503 均包含 request/trace ID。
- 开发库为 63/0063，`integrity_check=ok`、外键违规 0。
- 数据库、日志、DOM、URL、Storage 和制品敏感明文命中 0。
- 应用内 Browser 双视口实际验收后才能把 PRD-071 标记完成。

## 17. 代码修改计划

### 17.1 Shared

- 新增 `metahub_shared/output_safety/`：模型、Token 估算、全文/滑窗 matcher 和 Webhook 投影。
- `runtime/errors.py`：新增统一错误。
- `runtime/webhooks.py`：注册 `security.output_blocked` exact validator。

### 17.2 Gateway

- 新增 `metahub_gateway/output_safety/`：规则快照、pending/终态持久化、共享有界 SSE 分帧和事件构造。
- `api/openai_compatible.py`：非流式返回前扫描；流式检查点、错误事件和终态收口。
- `streaming/sessions.py`：两个输出安全终态、质量矩阵以及 session/scan 原子终结合同。
- `cache_affinity/lifecycle.py`：识别两个 active 后本地安全终态并按 Provider succeeded 收口/恢复 Selection。
- `usage/ledger.py`：spool v5、v1-v5 读取、blocked Usage/FinOps/dispute 原子结算与幂等重放。
- 配置与 `.env.example`：interval/window/最大全文边界，严格范围校验。

### 17.3 数据库

- `0063_output_safety_streaming_window.sql/.down.sql`。
- `dlp_rules.scan_scope`、scan/event/dispute 新表、StreamSession/RequestLog 状态兼容、owner/状态机/down guard；
  RBAC revision 7 保持不变。

### 17.4 Control

- 扩展 RequestLog list/detail DTO/query、`terminal_stage=output_safety` 和错误解释，不新增第二套 scan HTTP API。
- 扩展 DLP DTO/service 支持 scan_scope，继续使用 dlp.read/dlp.write。
- 扩展 FinOps 读 DTO/query，仅投影 dispute 的固定状态和 scan ID。
- Console 导航继续以 request_log.read 决定 PAGE-099/PAGE-100 可见性。

### 17.5 Console

- 新增 `src/features/output-safety/`，复用 RequestLog client/owner/cursor，不建立第二套数据 client。
- 接入 app navigation、owner state、PAGE-099 样式并固定安全阶段筛选。
- 扩展 RequestLog detail 的 PAGE-100 输出安全与账单复核区块。
- 扩展 DLP 列表/编辑器的 scan_scope 选项与权限状态。
- 在现有 FinOps 列表/详情增加只读复核徽标，不增加自动冲正命令。

### 17.6 测试与文档

- 新增 Shared/Gateway/Control/迁移/Console/Webhook 聚焦测试。
- 新增 `docs/api/output-safety.md`、`docs/testing/PRD-071-tests.md`。
- 更新 README、目录图、配置示例和实际运行/Browser 证据。

## 18. 编码前第一轮自审：流式边界、状态机与兼容性

状态：**通过（2026-08-04）**。

### 18.1 发现与修正

1. **原问题：raw chunk 立即透传与完整 SSE event 扫描冲突，会把 error 拼进未闭合 data 行。**
   修正为启用规则时最多缓冲一个 1 MiB 有界 event，按 frame 放行；`framer -> usage observe -> scan ->
   note_chunk -> yield`，命中 frame 计 observed 不计 emitted，同 raw chunk 更早完整 frame 可先行发送。
2. **原问题：流式投影会漏 refusal/tool arguments，不同 choice 还可能误拼接。**
   冻结四类 exact projection 和 lane 身份，同 lane 跨 event 续接、跨 lane 禁止拼接；strict JSON 拒绝 duplicate
   key，通过 frame 保持原 bytes，Token/空白双重边界保证内存有界。
3. **原问题：扫描完整性和判定挤在同一 status，无法表达“已匹配但尾窗未完成”。**
   拆为 scan_status 与 decision；unavailable 可保留 matched，未知尾部不能伪造成 clean。阶段矩阵覆盖 fallback、
   非流主备失败、stream starting/active、transport/parser failure、零规则和进程残留 pending。
4. **原问题：pending 与 StreamSession 创建顺序冲突。**
   非流式 claim 后独立提交 pending；流式先提交 starting session，再提交带 session ID 的 pending；snapshot 失败
   在 Provider 前按 starting/active 精确终结，禁止用一次终态 UPDATE 补 owner。
5. **原问题：零规则也启用严格 framer 会破坏 PRD-070 网络兼容。**
   pending 在 Provider 前终结 not_configured+none，继续 raw passthrough 与宽容 Usage 解析；只有 output/both
   规则存在时启用 event gate 和严格失败关闭。
6. **原问题：新流终态会让 Cache Affinity Selection 悬空。**
   output_blocked/output_safety_unavailable 都映射 Provider succeeded，RoutingAttempt/质量不受处罚且启动恢复
   无 pending 残留；握手失败保留真实 Provider 结果，输出安全仍不触发 Fallback。
7. **原问题：post-provider scan failure 与 block 的 Usage/账单/公开错误优先级未闭合。**
   两者都结算真实/估算 Usage；只有 block dispute。durability 改为 committed/spooled/lost，双失败时 FinOps 503
   优先且不派发 Webhook，scan 决策仍可查。
8. **原问题：所有请求写 v5 会让无 scan 的 Embeddings spool 在 down 后无法恢复。**
   仅有 scan 的 Chat 写 v5，无 scan 保持 v4；v5 reader existing-ledger 分支也必须验证/补齐 dispute，任一 v5
   都由不可删除 scan 行阻止 down。

### 18.2 第一轮结论

event gate、首事件时延、投影 lane、检查点、尾窗、断开竞争、二维状态机、Fallback/Cache Affinity、
Usage/dispute/spool 与双失败优先级已有唯一合同；不存在以安全功能覆盖原 timeout/DLP/Intent/Session Risk
错误的路径。

## 19. 编码前第二轮自审：租户、权限、数据完整性与隐私

状态：**通过（2026-08-04）**。

### 19.1 发现与修正

1. **原问题：所有历史 active DLP 规则会在升级后突然应用到模型输出。**
   修正为 `scan_scope=input|output|both`，历史行与新建默认均为 input；输入/输出热路径在 SQL 中分别筛选，
   管理 API/Console 明确展示和编辑方向，避免静默扩大治理范围。
2. **原问题：PAGE-099 新建独立 scan API 与 `output_safety.read`，重复 RequestLog owner/cursor/RBAC。**
   修正为 PAGE-099 固定复用 `GET /request-logs?terminal_stage=output_safety`，PAGE-100 增强既有详情；两者后端
   都逐请求校验 `request_log.read`，RBAC 保持 revision 7，OpsAdmin 可读，Viewer/FinanceAdmin 拒绝。
3. **tenant owner 复核。**
   明确 DLP/StreamSession/scan/Usage/FinOps 的复合候选键，以及 scan/dispute/event 的逐边 FK 图；trigger 再验
   trace、request、blocked decision、usage_charge 与金额，禁止只靠全局 ID 加可错配 tenant 字段。
4. **历史与完整性复核。**
   迁移前成功日志显示 legacy_unavailable；迁移后 Chat 以 pending/not_configured/not_applicable 等明确事实区分，
   应有 scan 而缺失时固定 integrity failure，不能降级为空或 clean。
5. **公开兼容合同复核。**
   Excel 精确要求 `content_policy_violation`，因此 JSON/SSE/RequestLog/StreamSession 均使用该值，不在编码时
   临时改成 namespaced 别名；unavailable 仍使用 `gateway.output_safety_unavailable`。
6. **财务与隐私复核。**
   正式 Usage/charge 不因阻断清零，reversal_eligible 按正金额派生，零金额只待复核；FinanceAdmin 仅经 FinOps
   查看最小徽标，人工 reversal 不变。Webhook 不猜测 charge-derived eligibility；所有边界禁止正文、pattern、
   hash、Header、Key、Token 和异常原文。
7. **恢复与降级复核。**
   v5 只保存 scan ID/status/decision/disposition，existing-ledger replay 仍补 dispute；非 Chat v4 可在 down 后由
   旧 reader 恢复，scan/dispute/event、非 input 方向配置或后续依赖存在时 down 原子拒绝。
### 19.2 第二轮结论

规则方向、权限、复合 owner、历史兼容、财务可见性、Webhook、敏感信息和降级门禁均已完成第二轮自审。
主 Agent 已按两份独立交叉审查逐项回查正文和现有代码基线，所有阻断项均有唯一合同；设计现已冻结，可以
开始编码。任何改变 scan_scope 默认值、二维状态机、条件式 spool v5、公开错误码或自动冲正语义的实现都
必须先修订本文并重新自审。

## 20. 实现与最终验收结论

- Shared/Gateway/Control/Console、0063、DLP `scan_scope`、PAGE-099/PAGE-100、FinOps 争议投影与
  `security.output_blocked` 已按本文冻结合同落盘；tenant 从认证上下文恢复，后端逐请求执行 RBAC，
  统一错误均携带 request_id/trace_id，正文、pattern、Key 和 Token 不进入日志或持久证据。
- 最终相关自动化结果为 Shared/Gateway `60 passed`、0063 迁移 `9 passed`、Control 相邻核心回归
  `47 passed`、Console 相邻组合 `59 passed`、PAGE-099/100 最终直接回归 `9 passed`、Runtime 验收器合同
  `7 passed`、Console lint `203 files passed`；`py_compile` 与中文注释门禁通过。
- [正式 Runtime 证据](../../.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/runtime-acceptance.json)
  为 `status=passed`：clean 200、非流阻断 403、安全扫描不可用 503、跨 chunk 流阻断唯一 SSE error、
  401/403/404 权限边界、四类账单/事件 owner、`63/0063`、数据库完整性、外键与一次性资源清理全部通过；
  SHA-256 为 `58592d4a0a9789ecc56273a8e16725ffebea513ab7cce79f31d911659fa2a7ff`。
- [Browser 证据](../../.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/browser-acceptance.json)
  为 `status=passed`：1280x720 列表与详情无覆盖，390x844 无横向溢出，Viewer 前端无入口/DOM/QueryEvent
  且后端直调 403；Prompt/Response 与 DLP pattern 的 DOM 命中为 0，应用错误为 0。SHA-256 为
  `832fe5914a9051447b25f9608cea71e92fd69b08bb370678346043e04f1d3db7`。
- 完整分层结果、正式验收器三次失败的定向闭环与未安装 Ruff 的真实环境说明见
  [PRD-071 测试记录](../testing/PRD-071-tests.md)。PRD-071 可标记完成；该结论只适用于本条，
  全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。
