# PRD-061 - 内部 Provider 状态页

状态：**已完成；两轮编码前独立自审、实现、专项与直接影响回归、真实运行、Edge 双视口和收口复核均通过**。

总体目标仍在进行，`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表!A62:R62` 与 `02_M0-M1核心开发!A62:R62` 一致定义：
  `PRD-061 | M1-企业POC | P1 | 核心层 | 状态页内部版 | 内部Provider状态页`。
- 主要用户是开发者和运维。用户故事是查看当前可用 Provider 状态及历史故障。
- 明确范围是：控制台按 Provider 独立展示可用性、最近错误、P95 延迟、成功率和维护备注。
- 明确边界是：不做公网状态页和订阅，不把内部 Provider、Endpoint 或运行指标暴露给匿名用户。
- 页面为 `PAGE-088 状态页`；核心表冻结的是一个 `GET /status/providers`，API 清单把路径机械拆成
  `GET/status/providers` 三行，不得据此实现三个接口。
- 核心对象为 `OBJ-082 ProviderStatusSnapshot`；事件为 `EVT-068 status.viewed`。
- `TC-0121` 要求状态同时来自真实调用和探测，并按 `provider_id` 独立展示；`TC-0122` 要求无权限、
  异常输入和依赖故障返回统一错误码，不泄露敏感信息且状态可追踪。
- PRD-060 已完整完成。按 Excel 两张核心表的里程碑、优先级和实际行顺序重新识别，PRD-061 是下一条
  未完成项，不是仅按编号推断。

### 1.2 架构约束

- Provider 健康属于可观测与调试层；状态页是控制面低频查询，不能让数据面热路径同步调用控制面，
  也不能在状态页请求中现场探测上游。
- Provider 适配层继续面向智谱、通义、文心、火山、混元、DeepSeek 官方或云服务及客户本地
  OpenAI-compatible Endpoint；OpenRouter、硅基流动仅是竞品和迁移兼容对象。
- 架构要求同时呈现真实调用 Trace 与 Provider 探测，但两类流量的频率、请求形态和负载不同，不能混成
  同一个延迟分布。页面分别返回调用 P95 与探测 P95；真实调用成功率也与探测成功率分开。页面必须标明
  统计窗口和各自样本数，不能把无样本显示为 `0%` 或 `0ms`。
- 所有查询必须以认证上下文 `tenant_id` 为首条件。API 请求不能接收或覆盖 `tenant_id`；跨租户
  Provider ID 与不存在资源使用相同空结果/404 语义，不能形成枚举侧信道。
- 后端按 PRD-059 的实时权限快照同时执行 `provider.health.read` 与 `request_log.read`；前者授权读取
  Provider 健康，后者授权读取调用级错误和 request/trace 标识。前端隐藏导航不构成授权。
- Endpoint 对外只返回去除 userinfo、query、fragment 的安全投影；Provider Key、Authorization、
  Credential ID、原始上游错误正文、Prompt 和 Response 均不能进入响应、事件、日志或页面 DOM。
- 所有成功响应携带 `request_id/trace_id`；所有错误走统一错误注册表和中间件。
- 中国境内企业数据默认留在境内开发/部署数据库；本 PRD 不引入外部监控 SaaS、第三方状态页或出境链路。

### 1.3 已有能力与真实缺口

已有能力：

1. PRD-035 已提供 Provider 周期/手工探测、当前健康聚合、探测日志、Endpoint 清洗、状态新鲜度和
   `GET /provider-health`；现有健康页面向配置诊断和立即探测。
2. `routing_attempts` 对每次真正获得上游调用资格的非流式主/备用尝试分别记录 `provider_id`、终态、
   归一错误码、Provider 延迟和时间。Fallback 主 Provider 失败不会被备用 Provider 成功覆盖。
3. `stream_sessions` 按 Provider 记录流式调用终态、归一错误码和精确起止时间；客户端主动断开可与真实
   上游失败区分。
4. Provider 探测日志按 `request_id` 保存逐凭据尝试，错误只使用固定分类；同一探测可能先遇到坏 Key，
   后由另一把 Key 成功。
5. Provider 既有 PATCH 已具备 `provider.write`、租户绑定、原子审计和字段哈希能力，可扩展一个语义明确的
   `maintenance_note` 字段；既有 `description` 继续作为管理说明，不能扩大给状态页读者。
6. Console 已有后端导航定义、细粒度权限快照、统一访问事件、一次进入只记录一次的访问状态机和
   Provider 健康页面生命周期。

真实缺口：

1. `/provider-health` 只展示探测聚合，不统计真实调用 P95、调用成功率或跨调用/探测的最近故障。
2. 最终 `request_logs` 只能表达客户端请求最终结果；发生 Fallback 时会掩盖首个 Provider 失败，不能作为
   Provider 独立成功率的权威事实源。
3. 当前没有 `GET /status/providers`、`ProviderStatusSnapshot` DTO 或 PAGE-088。
4. `routing_attempts` 与 `stream_sessions` 缺少 `(tenant_id, provider_id, completed_at)` 查询索引；直接聚合
   会随历史数据增长退化。
5. `console_page_events.event_type` 的数据库 CHECK 尚不允许 `status.viewed`。
6. `providers` 尚无声明为“状态页可见”的维护备注字段，直接复用自由文本 `description` 会扩大历史数据的
   可见范围。

### 1.4 实现范围

- 新增只读 `GET /status/providers`，按当前租户返回 Provider 当前健康、24 小时调用与探测指标、最近
  错误和最近 7 天最多 5 条故障记录。
- 支持 `provider_id`、`provider_type`、`health_status`、`provider_status`、`limit/offset` 筛选；统计窗口
  在 MVP 固定为 24 小时，避免任意超大窗口制造昂贵查询。响应显式返回窗口起止时间。
- 真实调用使用 `routing_attempts` 与 `stream_sessions`，探测使用按 `provider_id + request_id` 聚合后的
  `provider_health_probe_logs`；不读取或保存 Prompt/Response。
- PAGE-088 复用并升级 PRD-035 既有 `provider-health` 导航，而不新增第二个相似导航：Dashboard 继续使用
  `/provider-health` 摘要；进入该导航时改用 `/status/providers` 展示 SRE 指标和历史故障，并保留既有
  “立即探测”能力。PAGE-088 不新增任何写 API，探测仍走 PRD-035 已有权限和状态机。
- `status.viewed` 仅写入租户内 `console_page_events`，每次真正进入页面最多记录一次；15 秒刷新、筛选和
  同页重绘不重复写。它不是 Webhook 可订阅事件，也不新增公网订阅能力。
- 新增 0053 up/down：增加 `providers.maintenance_note`、调用/流/探测查询索引，并保真扩展/回退控制台
  事件 CHECK。
- 补齐 API、迁移、指标准确性、租户、权限、异常、前端生命周期、双视口和真实运行验收。

### 1.5 明确不做

- 不做公网/匿名状态页、RSS、邮件/短信/企微订阅、外部 incident 通知或 SLA 承诺。
- 不新增状态写接口、维护窗口调度、自动故障单、Provider 启停、Key 管理或第二套页面内手工探测；仅保留
  PRD-035 已有探测入口和独立权限。
- 不新建另一套探测器，不改变 PRD-035 健康状态机，不让页面派生指标反向控制数据面路由。
- 不把 429、客户端断开、本地预算/DLP/权限阻断等全部粗暴计为 Provider 失败。
- 不按 `provider_type` 合并同厂商多个 Endpoint；同类型的每个 `provider_id` 必须独立统计。
- 不使用 OpenRouter/硅基流动作为默认 Provider，也不引入硬件盒子、MCP 主链路、信创或复杂出境能力。

## 2. 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| Provider 状态领域 | `control-plane-api/src/metahub_control/provider_status/` | DTO、只读路由、租户内聚合、P95 与故障投影 |
| Provider 健康 | `control-plane-api/src/metahub_control/provider_health/` | 复用一致的 effective health 与 Endpoint 清洗规则，不改变探测状态机 |
| 后端权限 | `control-plane-api/src/metahub_control/auth/` | 新增 `require_provider_status_reader`，实时同时校验 `provider.health.read + request_log.read` |
| 应用装配 | `control-plane-api/src/metahub_control/app.py` | 注册 Provider 状态 router |
| 错误注册表 | `shared-sdk/python/metahub_shared/runtime/errors.py` | Provider 状态筛选、分页、查询不可用统一错误码 |
| Provider 管理 | `control-plane-api/src/metahub_control/providers/` | 既有 Create/PATCH 增加维护备注，保持 `provider.write`、租户绑定和哈希审计 |
| 审计摘要 | `control-plane-api/src/metahub_control/audit/service.py` | Provider 摘要白名单增加维护备注哈希，继续拒绝原文和未知字段 |
| 数据库 | `infra/migrations/0053_provider_status_page.sql/.down.sql` | `maintenance_note`、Provider 时间索引、`status.viewed` CHECK 保真升级/回退 |
| Console 导航/事件 | `control-plane-api/src/metahub_control/console/` | 升级既有 `provider-health` 导航、`status.viewed` 白名单和严格路径映射 |
| Console API | `console-web/src/lib/providerStatus.js` | 只读请求、筛选和统一错误净化 |
| PAGE-088 | `console-web/src/features/provider-health/` | 在既有探测页上增加紧凑指标、故障历史和维护备注，保留 PRD-035 行为 |
| Console 装配 | `console-web/src/app/main.js`、`features/developer-start/` | owner-bound 加载、Abort/generation、15 秒刷新和访问事件 |
| 样式 | `console-web/src/styles/main.css` | 1280/390 响应式、稳定指标轨道和长 ID 换行 |
| 测试/验收 | Python、Node、`scripts/acceptance/`、`docs/testing/` | TC-0121/0122、迁移、指标、权限、隐私和 Browser |

## 3. 数据表和字段设计

### 3.1 ProviderStatusSnapshot 是只读投影，不新建事实表

`ProviderStatusSnapshot` 由现有不可变/可追踪事实按请求时刻计算。另建快照表会产生异步刷新、过期、
重复计数和跨租户 owner 的新状态机，却不能改善 24 小时低频控制面查询；因此本 PRD 不复制 Provider、
调用或探测事实。

单项字段：

| 字段 | 类型 | 来源与约束 |
| --- | --- | --- |
| `provider_id/tenant_id` | string | `providers`；所有 JOIN 同时匹配 tenant_id 与 provider_id |
| `provider_code/name/provider_type` | string | Provider 元数据；按 provider_id 独立，不按类型聚合 |
| `base_url` | string | 删除 userinfo/query/fragment 后的安全 Endpoint |
| `auth_type` | string | 只返回认证类型，不返回 Header 名称、Credential 或 Key |
| `provider_status` | active/disabled | 配置状态 |
| `health_status` | unknown/healthy/unhealthy | PRD-035 原始状态 |
| `effective_health_status` | healthy/unhealthy/observing/unknown/disabled | 复用 PRD-035 状态与新鲜度规则 |
| `availability_status` | available/degraded/unavailable/unknown/disabled | 对 effective 状态的稳定产品层映射，不反向写健康状态 |
| `is_health_stale` | bool | 探测健康证据是否超过共享 stale 配置 |
| `maintenance_note` | string/null | `providers.maintenance_note`；明确允许状态页读者查看，前端必须 HTML 转义 |
| `terminal_call_count` | integer | 24 小时内非流式与流式全部终态事实，包含可计分与排除项 |
| `eligible_call_count` | integer | Provider 归因成功率分母，恒等于 `success_count + provider_failure_count` |
| `success_count` | integer | 通过冻结资格合同的非流式成功与流式 completed 数 |
| `provider_failure_count` | integer | 通过冻结质量白名单、可归因给 Provider 的失败数 |
| `credential_failure_count` | integer | 终态中认证/Key 错误数，是 `excluded_call_count` 的可核对子集 |
| `excluded_call_count` | integer | `terminal_call_count - eligible_call_count`；含凭据、本地、客户端和配置错误 |
| `success_rate_bps` | integer/null | Provider 归因成功率，万分比；无 eligible 样本为 null |
| `probe_total/success/failure/inconclusive_count` | integer | 24 小时内按 request_id 聚合后的完整探测计数 |
| `probe_success_rate_bps` | integer/null | 确定性探测成功率，inconclusive 不进入分母 |
| `call_latency_sample_count` | integer | 通过 Provider 归因合同的非流式真实调用延迟样本数 |
| `call_latency_p95_ms` | integer/null | 仅非流式真实调用 nearest-rank P95；不混入完整流会话或探测 |
| `stream_first_chunk_sample_count` | integer | 有合法 `first_chunk_at` 的终态流会话数 |
| `stream_first_chunk_p95_ms` | integer/null | `first_chunk_at-started_at` 的 nearest-rank P95；握手失败不伪造 0ms |
| `probe_latency_sample_count` | integer | 确定性探测组延迟样本数 |
| `probe_latency_p95_ms` | integer/null | 仅探测组 nearest-rank P95；无确定性探测为 null |
| `latest_call_at/latest_probe_at` | datetime/null | 两类数据源最近确定性观测时间 |
| `last_tested_at/last_probe_latency_ms` | datetime/integer/null | PRD-035 当前探测投影，供既有手工探测交互继续使用 |
| `last_error` | object/null | 最近调用或探测故障的安全投影 |
| `recent_failures` | object[] | 最近 7 天按时间倒序最多 5 条，不含正文/Endpoint/凭据 |
| `provider_created_at/provider_updated_at` | datetime | Provider 元数据时间；不得冒充快照生成时间 |

`availability_status` 必须由后端按固定一对一映射生成：`healthy -> available`、
`observing -> degraded`、`unhealthy -> unavailable`、`unknown -> unknown`、`disabled -> disabled`。
前端只能翻译该枚举，不能结合成功率、最近错误或本地时钟再次推导另一个可用性结论。

故障投影字段固定为：`source`（`non_stream_call/stream_call/probe`）、`failure_kind`
（`provider/credential/excluded/probe`）、`eligible_for_success_rate`、`error_code`、`request_id`、`trace_id`、
`latency_ms`、`observed_at`。内部使用 `stable_id` 稳定排序但不对外返回 routing attempt、stream session、
probe log 或 Credential ID。所有错误码来自 Adapter/流会话/探测固定枚举，不返回 raw message、响应正文或
HTTP Header。服务层必须对三类来源分别执行错误码白名单和 request_id/trace_id 格式校验；任何未知错误码、
控制字符、超长标识或不可解析时间都统一失败关闭为 503，不能把历史自由文本原样送入 API 或 DOM。

### 3.2 真实调用样本口径

成功率名称冻结为“Provider 归因成功率”，不是所有请求的业务成功率。其权威来源是逐 Provider 的
`routing_attempts + stream_sessions`，绝不能使用会被 Fallback 最终结果覆盖的 `request_logs`。

非流式 `routing_attempts` 资格矩阵：

| 事实 | terminal | eligible | success | provider_failure | credential_failure | excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `succeeded AND usage_status!='not_applicable'` | 1 | 1 | 1 | 0 | 0 | 0 |
| `failed AND usage_status='unavailable'` 且质量白名单 | 1 | 1 | 0 | 1 | 0 | 0 |
| `failed` 且码为 `gateway.upstream_auth_failed` 或 `provider_key.unavailable` | 1 | 0 | 0 | 0 | 1 | 1 |
| 其他 `succeeded/failed` 终态 | 1 | 0 | 0 | 0 | 0 | 1 |
| `started` | 0 | 0 | 0 | 0 | 0 | 0 |

- 质量白名单精确为 `error_category IN ('timeout','rate_limited','upstream_5xx')`，或
  `error_code='gateway.upstream_transport_failed'`；同时必须有 `usage_status='unavailable'`。
- 成功条件显式包含 PRD-048 冻结的 `usage_status!='not_applicable'`。所有 terminal 行（包括 excluded）
  都必须有可解析的 `completed_at` 和非负整数 `latency_ms`；不能先按字符串时间裁窗口再让坏时间行消失。
  服务先对本页候选 Provider 的终态事实执行类型、UTC 时间和状态矩阵预检，再进入 24 小时窗口聚合；
  质量码/分类与非 `unavailable` 组合、计数恒等式或时间合同矛盾时整个接口失败关闭为 503。
- `credential_failure_count` 是 excluded 的子集而非额外分母；上游配额、模型、请求、客户端取消、本地
  URL/协议/Adapter/审计错误仍进入 terminal/excluded，并进入安全故障历史，但不处罚 Provider 成功率。
- Fallback 的主、备尝试分别归属各自 Provider；同一客户端 request 可给两个 Provider 各贡献一个终态。

流式 `stream_sessions` 资格矩阵：

| 事实 | terminal | eligible | success | provider_failure | credential_failure | excluded |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `completed` | 1 | 1 | 1 | 0 | 0 | 0 |
| `timed_out` | 1 | 1 | 0 | 1 | 0 | 0 |
| `upstream_failed` 且命中四码白名单 | 1 | 1 | 0 | 1 | 0 | 0 |
| `upstream_failed` 且码为 `gateway.upstream_auth_failed` 或 `provider_key.unavailable` | 1 | 0 | 0 | 0 | 1 | 1 |
| 其他 `upstream_failed` 或 `client_disconnected` | 1 | 0 | 0 | 0 | 0 | 1 |
| `starting/active` | 0 | 0 | 0 | 0 | 0 | 0 |

- 四码白名单精确为 `gateway.upstream_rate_limited`、`gateway.upstream_timeout`、
  `gateway.upstream_stream_timeout`、`gateway.upstream_stream_failed`。前三个仅兼容 0037 前已存在的历史
  `upstream_failed` 事实；当前运行时的握手/读取质量失败统一写
  `gateway.upstream_stream_failed`，而 `timed_out` 独立写 `gateway.upstream_stream_timeout`。状态页不得据此
  拒绝合法历史事实，也不得把当前本地处理错误改写成旧质量码。
- 所有 terminal 行（包括 excluded）必须有合法 `completed_at >= started_at`；不能只验证 eligible 行后
  静默漏掉坏时间的客户端断开、认证或本地失败。`timed_out` 错误码、completed 空错误码和
  upstream_failed 白名单的矛盾组合均失败关闭为 503，不能按猜测归类。
- 成功率不使用完整流会话时长。首块延迟只对 `first_chunk_at` 非空且满足
  `started_at <= first_chunk_at <= completed_at` 的终态流计算；按 UTC 整数微秒差向下截断毫秒，禁止
  `julianday` 浮点近似。没有首块的握手失败只计成功率/故障，不进入首块 P95。
- 六个计数字段必须满足：`eligible_call_count=success_count+provider_failure_count`、
  `excluded_call_count=terminal_call_count-eligible_call_count`、
  `credential_failure_count<=excluded_call_count`；任一不成立返回 503。

### 3.3 探测样本口径

- 同一 `tenant_id + provider_id + request_id` 的多条凭据尝试聚合为一个探测组，避免坏 Key 后成功的探测
  被错误计算成一次失败加一次成功。
- 必须先读取并完整聚合同一次探测事务的全部 attempt。持久分组身份为
  `(provider_id, request_id, health_generation, created_at)`：`request_id/trace_id` 可由调用方透传，
  Provider 状态不变化时 `health_generation` 也不递增，而正式探测事务会为整组 Key attempt 写入同一个
  `created_at`。再以组级 `observed_at=MAX(checked_at)` 判断是否落入
  `[window_started_at, window_ended_at)` 或 7 天历史窗口；禁止先按窗口裁 attempt，避免跨边界探测变形。
- 两阶段 SQL 先用时间索引找出窗口内出现过 attempt 的候选分组身份，再用 request 索引回取候选分组
  的全部 attempt，最后以完整组的 `MAX(checked_at)` 二次判窗。组级 MAX 落窗必然至少有一条 attempt
  落窗，因此候选查询不会漏组；含未来 attempt 的组会在二次判窗时被排除。任一候选组存在不可解析
  `checked_at`、负数/非整数 latency、重复 attempt_sequence 或不一致 trace_id 时整体返回 503。
- 任一 attempt `success` 则整组成功；否则任一 `failed` 则整组失败；全部 inconclusive 则整组
  inconclusive 且不进入成功率/P95 分母。
- 代表 attempt 与 PRD-035 聚合一致：从与组级结果相同 `result_status` 的行中按
  `attempt_sequence ASC, probe_log_id ASC` 取第一条。探测 P95、失败错误码和展示延迟都取这同一代表行，
  不用最大 latency 冒充整组总耗时，也不对多 Key latency 求和。
- 失败组生成故障；成功组中的中间坏 Key 不生成 Provider 故障。inconclusive 只计 `probe_total` 的观测总数，
  不进入成功/失败分母或故障集合。

### 3.4 P95 与成功率公式

- 统计窗口固定 `[now-24h, now)`；同一请求内只生成一次 UTC `now`，所有 SQL 复用该边界。
- `call_latency_p95_ms`、`stream_first_chunk_p95_ms`、`probe_latency_p95_ms` 使用三个完全独立的样本集：
  eligible 非流式调用、已产生首块的终态流、确定性探测代表行。完整流会话时长不进入任何 P95。
- nearest-rank：分别对同一 Provider、同一来源的 N 个非负整数毫秒升序排列，排名
  `ceil(0.95 * N)`，以 1 为起点；单样本 P95 等于该样本。
- SQL 使用窗口函数 `ROW_NUMBER/COUNT` 和正整数除法排名 `((95*N+99)/100)`；SQLite 两侧均为整数时结果
  截断为整数，不使用不存在的 `//` 运算符，也不把全部历史样本拉到 Python。
- Provider 归因成功率 `success_rate_bps = round_half_up(success_count * 10000 / eligible_call_count)`；
  探测成功率同理。无分母时返回 null，UI 显示“暂无样本”，不能显示 0%。
- SQL 只负责返回有界计数与排序样本；Python 使用任意精度整数计算 half-up，避免 SQLite 乘以 10000 时
  溢出。所有送入 JSON 的计数、延迟和样本数还必须不超过 JavaScript 安全整数
  `9_007_199_254_740_991`，越界统一返回 503，不能让页面静默丢失精度。
- 页面不返回一个会被误解为全租户 SLA 的平均成功率；每个 Provider 的计数和百分比必须可相互核对。

### 3.5 故障集合与稳定排序

- 故障集合独立于成功率资格：纳入所有带非空固定错误码的非流式 failed、流式非 completed 终态，以及
  组级 failed 探测；因此认证、Key、配额、模型、请求、本地处理和客户端断开等 excluded 错误仍可排障。
- `last_error` 与每 Provider 最近 7 天最多 5 条 `recent_failures` 使用同一 UNION；排序精确冻结为
  `observed_at DESC, source ASC, request_id ASC, stable_id ASC`。`stable_id` 分别使用
  `routing_attempt_id/stream_session_id/代表 probe_log_id`，只参与内部稳定排序，不进入响应。
- `failure_kind` 由同一资格矩阵产生；`eligible_for_success_rate` 只在 provider_failure 为 true。错误正文、
  `result_summary`、Endpoint、Credential ID、Key 前缀/指纹不得进入 UNION、DTO 或日志。

### 3.6 0053 数据库迁移

Up：

1. 在任何 DDL 前验证 0052 是栈顶、依赖表与关键列存在，防止在损坏历史栈上部分应用。
2. 为 `providers` 新增 `maintenance_note TEXT NULL`，只允许 trim 后 1..240 个字符且拒绝控制字符；存量行
   回填 NULL，绝不从 `description` 复制未知历史文本。
3. 新增 partial index：
   `idx_routing_attempts_tenant_provider_completed(tenant_id, provider_id, completed_at DESC, routing_attempt_id DESC)`，
   仅覆盖 terminal 且 completed_at 非空行。
4. 新增 partial index：
   `idx_stream_sessions_tenant_provider_completed(tenant_id, provider_id, completed_at DESC, stream_session_id DESC)`，
   仅覆盖终态且 completed_at 非空行。
5. 新增扩展时间索引
   `idx_provider_health_logs_tenant_provider_time_request(tenant_id, provider_id, checked_at DESC, request_id, attempt_sequence, probe_log_id)`，
   并新增完整分组索引
   `idx_provider_health_logs_tenant_provider_request_sequence(tenant_id, provider_id, request_id, attempt_sequence, checked_at, probe_log_id)`；
   前者选窗口候选 request，后者回取该 request 跨窗口边界的全部 attempt。
6. 按 0030 已验证模式保真重建 `console_page_events`，只把 CHECK 扩展为允许 `status.viewed`；显式复制 rowid
   和全部列，恢复三个既有索引，并用双向 `EXCEPT`、行数和 rowid 校验不漂移。该表不是 AUTOINCREMENT，
   不虚构 `sqlite_sequence` 保真要求。
7. 0053 SQL 自身以 `BEGIN IMMEDIATE` 开始、只在全部 DDL/保真校验/版本前置条件通过后 `COMMIT`；迁移器
   `executescript` 的异常分支执行 rollback。前置检查、事件表重建、索引和加列之间不得出现中间 COMMIT。

Down：

1. 若存在 0053 后续迁移、未知依赖，或任一 `maintenance_note IS NOT NULL`，必须在首项持久 DDL 前原子
   阻断；维护备注属于用户业务数据，down 不得静默删除或塞入语义不同的 `description`。
2. `status.viewed` 降级为 `console.page_viewed`，保留同一 event_id、tenant/user/session、request/trace、
   payload 和 created_at；不删除访问证据。
3. 保真重建 0030 事件 CHECK、恢复索引，删除四个 0053 查询索引，并在空备注前提下删除
   `providers.maintenance_note`。
4. up/down/re-up 后执行 `integrity_check`、`foreign_key_check`、差集和栈版本校验；任一步失败整体回滚。

## 4. API 设计

### 4.1 GET /status/providers

权限：新增后端依赖 `require_provider_status_reader`，每次请求从当前数据库权限快照同时要求
`provider.health.read + request_log.read`。两项权限当前默认都授予 admin、developer、ops_admin；不能按角色名
硬编码，不能使用二选一，也不能因为前端导航可见就跳过依赖。只撤销任一项后旧 Session 下一请求立即 403。

查询参数：

| 参数 | 约束 | 说明 |
| --- | --- | --- |
| `provider_id` | 可选，trim 后 1..128 | 当前租户单 Provider；跨租户只得到空集合 |
| `provider_type` | 可选，现有 Provider 类型白名单 | 未知值返回 `provider_status.filter_invalid` |
| `health_status` | 可选 effective 枚举 | healthy/unhealthy/observing/unknown/disabled |
| `provider_status` | 可选 | active/disabled |
| `limit` | 十进制整数字符串，1..100，默认 50 | 分页上限阻止大查询 |
| `offset` | 十进制整数字符串，0..10000，默认 0 | 稳定分页 |

请求不接受 `tenant_id`、window、URL、Header、Key 或 raw error 过滤。路由先以原始字符串接收
`limit/offset`，再调用共享的严格十进制解析器，确保 `true`、浮点、符号、空白和越界值都返回本接口的
`provider_status.pagination_invalid`，而不是被 FastAPI 提前转换成通用 422；服务层重复校验全部边界，
避免内部调用绕过路由。

排序固定为 `unhealthy -> observing -> unknown -> healthy -> disabled`，同状态按 `provider_id ASC`。
Provider 元数据和 effective 状态筛选先于 summary/total，分页最后执行；summary 桶与 total 可核对。
指标和故障只对分页后的最多 100 个 Provider 聚合，避免为不可见页面加载全租户历史。所有 Provider、调用、
流会话、探测和故障查询在同一只读事务、同一 UTC `now` 下完成；GET 不产生任何业务写入。

响应：

```json
{
  "provider_statuses": [],
  "summary": {
    "total": 0,
    "healthy": 0,
    "unhealthy": 0,
    "observing": 0,
    "unknown": 0,
    "disabled": 0
  },
  "window_started_at": "2026-07-30T00:00:00.000Z",
  "window_ended_at": "2026-07-31T00:00:00.000Z",
  "history_started_at": "2026-07-24T00:00:00.000Z",
  "total": 0,
  "limit": 50,
  "offset": 0,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

数据库连接/SQL/Schema 失败统一为 503，不返回 SQL、文件路径、表名或底层 exception 文本。

### 4.2 Provider 维护备注写入合同

- 不新增状态页写接口；复用既有 `POST /providers` 与 `PATCH /providers/{provider_id}`，仍由后端
  `provider.write` 校验，并只使用认证上下文的 tenant_id。
- Create 可选 `maintenance_note`；PATCH 省略字段表示不修改，显式 null 或 trim 后空串表示清空，非空值
  trim 后必须为 1..240 个字符且不得含 C0/C1 控制字符。未知字段继续严格拒绝。
- `CreateProviderRequest/UpdateProviderRequest` 使用 `extra="forbid"`；PATCH 必须读取
  `payload.model_fields_set` 判断 maintenance_note 是否出现，不能沿用 `value is not None`，否则显式 null
  会被误判为未修改。该 presence 规则只改变维护备注，不顺带重写既有 description 兼容语义。
- `ProviderInfo` 返回该字段供 Provider 管理页确认；状态页只读投影同一列。服务日志不记录原文，
  `provider.created/provider.updated` 审计只新增 `maintenance_note_hash=sha256(UTF-8)`，并同步扩展
  `SUMMARY_FIELDS["provider"]` 白名单；不把原文写入 AuditLog/Webhook，也不能用放宽通用摘要校验代替精确字段。
- 更新与审计同一事务；跨租户 provider_id 继续使用 `provider.not_found`，不能修改或读取他租户备注。

### 4.3 EVT-068 status.viewed

- 复用 `POST /console/events`，不新增第二个事件 API。
- 复用的导航 ID/target 固定为 `provider-health`，path/hash 固定为
  `/#provider-health`/`#provider-health`，event_type 必须为 `status.viewed`。
- payload 继续只有 `source/hash/viewport_class`；不得加入 Provider ID、Endpoint、错误码、指标、Token 或用户输入。
- 同一 Session 每次从其他页面进入 PAGE-088 只尝试一次；定时刷新、筛选和失败重试不重复记录。
- 该事件只进入内部 `console_page_events`，不加入 Webhook 订阅白名单；Excel 的“事件/Webhook”列不意味着
  必须突破“不做订阅”的明确边界。

## 5. 前端页面和交互设计

### 5.1 PAGE-088 Provider 状态

- 复用既有“Provider 健康”导航，不增加近似入口；该导航的后端 `required_permissions` 升级为
  `provider.health.read + request_log.read`，页面标题升级为“Provider 状态”。
- 页面顶部使用紧凑摘要带，不使用营销式大卡片：健康、异常、观察中、未知、已禁用和 Provider 总数；
  不伪造一个跨 Provider 平均成功率。
- 紧接统计口径行，显示固定“过去 24 小时”和生成时间；无样本明确显示“暂无调用样本”。
- 筛选区提供 effective 健康状态、配置状态、Provider 类型和重置；筛选提交才刷新，不把输入拼入 HTML。
- 主体每个 Provider 使用一张独立、非嵌套状态行/卡片：
  - 名称、code、类型和安全 Endpoint；
  - 当前 effective 状态与探测新鲜度；
  - Provider 归因成功率及 terminal/eligible/success/provider failure/credential/excluded 六项可核对计数；
  - 非流式调用 P95、流式首块 P95，以及独立的探测成功率、探测样本和探测 P95；
  - 最近错误码/时间和维护备注；
  - 原生 `<details>` 展开最近故障，展示来源、时间、固定错误码和可换行 request_id。
- PAGE-088 保留 PRD-035 已有“立即探测”，仍由 `provider.health.probe` 独立后端校验；不增加启停、编辑备注
  或 Key 操作，也不允许只有读权限的用户看到探测按钮。
- 15 秒自动刷新只在 active 导航、Token、tenant、permission fingerprint 和后端 navigation owner 均未变化时执行。
  每轮 ticket 同时冻结 canonical filter key、tenant/user/session、Token 指纹、permission revision、导航 ID、
  generation 和 AbortController；接纳成功或错误响应前逐项复核。筛选、离页、登出、401/403 或权限 revision
  变化立即推进 generation、取消请求并清空旧状态。
- `status.viewed` 在 PAGE-088 shell 已按当前后端 navigation owner 成功激活后通过通用 visit tracker 上报，
  不以状态 API 200 为前提，因此 503/空态页面访问也可追踪；每次进入最多一次，事件失败是非阻断告警。
- Dashboard 继续按单独的 `provider.health.read` 显示既有健康摘要；只有同时具备
  `provider.health.read + request_log.read` 且后端导航快照确实含 `provider-health` 时才渲染“查看详情”链接，
  避免自定义角色看到一个必然回退的无权入口。

### 5.2 响应式与可访问性

- 1280x720 使用紧凑 4 列指标轨道；小于 900px 改为 2 列；390x844 单列。调用与探测指标使用不同标签，
  不能只靠颜色区分来源。
- 卡片/行使用固定 grid track、`minmax(0,1fr)` 和 `overflow-wrap:anywhere`，长 Provider code、URL、request_id
  不得扩大页面宽度。
- 状态同时使用中文文字、边框/颜色和可访问 label，不能只靠颜色。
- `<details>/<summary>` 使用浏览器熟悉 disclosure，不另造文字胶囊按钮；所有筛选 label 与 select 绑定。
- loading、空结果、统一错误、部分事件告警和成功数据都有稳定最小高度，异步状态不能造成明显布局跳动。
- 所有数据库文本执行 HTML/属性上下文转义；维护备注中的 `<script>`、引号和反引号只能显示为文本。

## 6. 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `provider_status.filter_invalid` | 422 | Provider 类型、状态或 ID 非法 |
| `provider_status.pagination_invalid` | 422 | limit/offset 越界 |
| `provider_status.query_unavailable` | 503 | Provider、调用、探测或故障聚合读取失败 |
| `auth.permission_denied` | 403 | 当前实时权限快照缺少 `provider.health.read` 或 `request_log.read` 任一项 |
| `provider.maintenance_note_invalid` | 422 | 维护备注超长、含控制字符或格式非法 |
| `auth.unauthorized` | 401 | 缺少、失效或被撤销 Session |

所有错误使用统一 envelope，携带 `request_id/trace_id`。P95/成功率无样本不是错误；返回 null 与零样本数。

## 7. 安全与权限约束

1. 状态路由依赖在每次请求从数据库加载当前 permission revision，并同时要求健康与请求日志读取权限；
   不按 role string 放行，也不按字段做条件隐藏。
2. 所有根查询和 UNION 分支以 `tenant_id=?` 为首条件，Provider ID 仅作为租户条件后的第二维度。
3. Provider 与调用/流/探测 JOIN 必须同时匹配 `tenant_id + provider_id`，不能只依赖单列外键。
4. 状态接口只读；连接不执行现场探测、Provider 更新或后台缓存回填。
5. Endpoint 清除 userinfo/query/fragment；响应不包含 Credential ID、Key prefix/fingerprint、Authorization、
   Header、原始 HTTP body、错误 message、Prompt 或 Response。
6. 故障只使用归一错误码、request/trace 和时间；logger 不记录 query payload、维护备注原文、底层
   sqlite/http 异常文本。维护备注只在明确授权的 Provider/状态响应中出现，AuditLog/Webhook 只保留哈希。
7. `status.viewed` payload 由后端按导航定义逐字段复核，只保存固定枚举，不保存当前筛选或 Provider 数据。
8. 服务对异常 Schema、整数溢出和不可解析历史时间失败关闭为统一 503，不能用猜测值继续显示“准确指标”。
9. 页面状态以 tenant/session/permission/navigation owner 绑定，迟到响应不能写入下一租户或下一登录态。
10. 测试使用跨租户同 code/同 type Provider 证明独立归属，并对响应、事件、日志、DOM 扫描敏感 canary。

## 8. 测试用例

### 8.1 迁移

- 0052 -> 0053 up、repeat no-op、down/re-up；fresh 0001..0053。
- 四个查询索引列顺序、partial predicate 和调用/流/探测 candidate + 完整 request 分组 query plan 实际命中。
- `maintenance_note` 存量 NULL、约束与 down 非空阻断；空备注时可 down/re-up 且 Provider 全列/rowid 不漂移。
- `console_page_events` rowid、全列、三个索引和既有事件保真；该表不是 AUTOINCREMENT，不检查 sqlite_sequence。
- 0053 接受 `status.viewed`，拒绝未知事件；down 将其精确降级为 `console.page_viewed` 且不删除证据。
- 后续迁移、未知依赖或缺失核心表/列时在首项持久 DDL 前原子阻断。
- up/down/re-up 后 `integrity_check=ok`、`foreign_key_check` 为空。

### 8.2 指标准确性主流程 TC-0121

- 两个同类型 Provider 独立产生非流式成功/失败/Fallback；主失败只计主 Provider，备成功只计备 Provider。
- 非流式 terminal/eligible/success/provider failure/credential/excluded 六项逐行验证，显式覆盖
  `succeeded + usage_status='not_applicable'` 排除和质量分类矛盾 503。
- 流式 completed/timed_out/upstream_failed 正确计入，四个错误码逐个覆盖；client_disconnected、认证和本地
  失败只进入 terminal/excluded/故障历史，不污染成功率。
- 探测一把坏 Key 后另一把成功只形成一个成功组；全失败形成一个失败组；全 429/inconclusive 不进分母。
- 以 1、2、19、20、21 个边界样本分别验证非流式调用、流式首块和探测 nearest-rank P95；另以“少量
  调用 + 数千探测 + 长完整流”证明三个分布完全独立，探测量和完整流时长不能改变调用 P95。
- 成功率使用有界整数 half-up，并逐 Provider 由 `success_count/eligible_call_count` 重算，不平均百分比。
- 无调用、仅探测、仅调用、无任何样本分别返回正确 null/计数。
- 24 小时左闭右开边界和 7 天故障边界稳定；同一 UTC now 被全部查询复用。
- 探测跨 24 小时边界时先完整分组再按 MAX(checked_at) 纳入；P95 使用与组结果一致的第一条代表 attempt，
  不取 max/sum latency。成功探测中的中间坏 Key不形成故障。
- 相同 request/trace 在不同健康代次以及状态不变的同一健康代次内重复使用时，均按各自探测事务独立统计；
  同一事务的重复 attempt_sequence 或不一致 trace_id 仍失败关闭为 503。
- 同毫秒多源错误按 `observed_at DESC, source, request_id, stable_id` 稳定排序；认证、Key、配额和本地错误
  虽不进入成功率，仍进入安全故障集合。
- `providers.maintenance_note` 只读投影；`provider_created_at/provider_updated_at` 不因状态 GET 改变，
  响应级 `window_ended_at` 是唯一快照生成时刻。

### 8.3 异常、权限和安全 TC-0122

- admin/developer/ops_admin 可读；viewer/finance/security_admin 后端 403；匿名 401。
- 分别撤销 `provider.health.read`、仅撤销 `request_log.read` 后，旧 Session 的 API 立即 403、导航消失、
  在途响应被 generation/owner 丢弃；前端隐藏不能改变后端结果。
- 另一租户 Provider、routing attempt、stream session、probe log 和维护备注均不能出现在响应或 summary。
- 非法状态/type/ID、bool/浮点/带符号/带空白的 limit、越界 offset 返回路由专属统一码与 request_id，
  不得被框架提前转换为通用 validation error。
- 缺失表、错误列、连接故障、聚合 SQL 故障统一 503，不暴露数据库路径/SQL/exception。
- Endpoint 带 userinfo/query/fragment，错误正文、Key、Token、Prompt/Response canary；扫描 API、日志、事件、DOM
  和测试证据均不得命中。
- 维护备注覆盖 240/241 边界、控制字符、显式清空、跨租户、PATCH 权限和 XSS；敏感 canary 只允许出现在
  当前租户授权 Provider/状态响应的预期字段及转义后 DOM，AuditLog/Webhook/logger 只能出现哈希或完全不出现。
- Provider Create/PATCH 未知字段严格 422；维护备注省略、null、空白和非空四态分别验证，审计摘要白名单
  接受 hash、拒绝原文与伪造未知键。
- 历史错误码、request_id/trace_id、计数或延迟超出冻结白名单/格式/JavaScript 安全整数时统一 503，
  响应、日志和 DOM 不得回显恶意原值。
- 指标查询不执行网络，不修改 Provider、probe lease、request log、routing attempt 或 stream session。

### 8.4 Console

- client 只发送 Bearer 和筛选参数，不发送 tenant_id、URL、Key 或任意窗口。
- PAGE-088 双权限导航、loading、空态、错误、摘要、筛选、维护备注、三类 P95、六项计数、百分比、
  故障 details 和 XSS 转义。
- 同 Session 一次进入只记录一条 `status.viewed`；15 秒刷新/筛选不重复，离开后再次进入可新增一条。
- event 的 nav/target/path/hash/source/viewport 任一伪造均 422；Provider/filter 字段不能进入 payload。
- Abort/generation 覆盖慢 GET 跨越筛选、离页、登出、401、Token/tenant/revision/fingerprint 变化；测试必须
  证明旧 canonical filter key 的迟到成功和迟到错误都不能覆盖新筛选结果。
- 1280x720 与 390x844 无横向溢出、重叠、按钮文字溢出、空白主区域或控制台错误。

### 8.5 实际运行验收

- 开发库真实迁移至 0053；确认 53 条连续迁移、完整性、外键和核心 trigger。
- 启动 Gateway/Control/Console，三个健康检查 HTTP 200。
- 使用本地 admin 登录；至少执行一次真实非流式调用和一次真实 Provider 探测，使两类事实进入状态 API；
  流式链路至少执行一次专项集成验证。精确 P95 边界另使用受控数据库事实，不用手工插表替代真实接线验收。
- Developer/OpsAdmin 正常访问；Viewer 或移除权限的用户真实返回 403；跨租户 ID 不泄漏。
- Microsoft Edge 1280x720 和 390x844 验收 PAGE-088；截图、网络、DOM、Storage、console/page error 和敏感扫描通过。
- 验收后撤销本任务会话并停止临时 Control/fake upstream；checkpoint SQLite 并确认无待写 WAL、无 journal、
  无临时端口。Gateway/Control/Console 作为后续开发基线保留运行时，允许 SQLite 保留活动连接所需 SHM。

## 9. 代码修改计划

1. 完成本文两轮编码前自审，先关闭指标、权限、租户、事件和迁移边界。
2. 新增 0053 up/down 及迁移专项测试，先证明索引与事件表保真。
3. 新增 Provider 状态 models/routes/service，完成指标 SQL、P95、故障投影和统一错误。
4. 注册 router、错误码和后端权限合同；完成主流程、边界、租户、权限和敏感信息测试。
5. 新增 Console status client，升级既有 Provider 健康 renderer 和 owner-bound 生命周期；接入访问事件和响应式样式。
6. 完成 Console unit/lint、中文注释门禁和直接受影响回归。
7. 更新 README、目录说明、API 文档和 `docs/testing/PRD-061-tests.md`。
8. 迁移开发库、启动三服务、执行 API 与 Edge 双视口验收，最后停服并复核静止终态。

测试只覆盖 PRD-061 专项与直接受影响回归，不因一个局部问题重复运行无关根级全量。pytest 固定使用
`TEMP/TMP=D:/MetaHub/.artifacts/prd061-temp`，并在
`D:/MetaHub/.artifacts/prd061-pytest/` 下为每组测试使用明确命名的独占 `--basetemp`。全程不提权、
不使用 `C:/tmp`，也不访问、枚举、修改或删除整个 `D:/MetaHub/.tmp`。

## 10. 第一轮编码前自审

状态：**修正后通过**。

| 检查项 | 初稿风险 | 修正 |
| --- | --- | --- |
| 延迟分布 | 把非流式完整调用、流式完整会话和高频轻量探测混成一个 P95，会由流量结构决定产品指标 | 拆成非流式调用 P95、流式首块 P95 与探测 P95，各自返回样本数和 null 空样本语义 |
| 页面重复 | 新增 `provider-status` 导航会与 PRD-035 健康页重叠，制造两套刷新/权限/探测体验 | PAGE-088 复用 `provider-health` 导航；Dashboard 保持旧摘要，详情页升级为新状态投影 |
| 既有写能力 | 初稿为了只读页面移除立即探测，会回归 PRD-035 | 保留既有探测按钮和 `/probe`；读/探测权限继续拆分，不新增写 API |
| 快照一致性 | 多次 SELECT 若不显式开启事务，Provider、调用和探测可能来自不同数据库时刻 | 单连接显式只读事务、单一 UTC now；异常整体返回统一 503 |
| 聚合范围 | 为 summary 加全租户调用率会迫使不可见 Provider 历史全部参与昂贵查询 | summary 只统计 Provider 状态；指标/故障只查询分页后的最多 100 个 Provider |
| 迁移索引 | 只给调用与流建索引，探测按 request 分组仍可能退化 | 增加 tenant/provider/time/request/sequence 探测聚合索引并要求 query plan 测试 |
| 迁移保真 | 非 AUTOINCREMENT 的事件表被错误写入 sqlite_sequence 验收要求 | 保留 rowid/全列/索引即可，不虚构不存在的 sequence |
| 可用性术语 | 仅返回 health 枚举，UI 需要自行猜测“可用/降级” | 后端冻结 availability 映射，前端只翻译稳定枚举，不反写路由健康 |
| 成功率口径 | `call_total` 混淆全部终态与质量资格，且遗漏成功行的 usage_status 合同 | 冻结六项计数和非流/流资格矩阵，成功显式要求 `usage_status!='not_applicable'`，矛盾事实 503 |
| 故障可见性 | 若故障集合复用成功率分母，认证、Key、配额和本地错误会从排障页消失 | 故障 UNION 独立于成功率资格，并冻结 failure_kind、eligible 标志和四键稳定排序 |
| 细粒度权限 | 只要求健康权限会绕过可单独撤销的调用日志权限 | 新增双权限 `require_provider_status_reader`；API、导航和迟到响应分别覆盖单项撤销 |
| 维护备注 | 复用 description 会扩大既有自由文本对 Developer 的可见范围 | 0053 新增 maintenance_note；既有 Provider 写权限管理，审计只存哈希，down 有数据时失败关闭 |
| 探测边界 | 先裁窗口或取 max latency 会拆散跨边界多 Key 探测并伪造组耗时 | 先按探测事务完整分组，再按 MAX(checked_at) 纳入；P95 使用与组结果一致的确定性代表 attempt |
| 运行验收 | 只插受控数据库事实无法证明真实 Gateway 调用和探测链路接通 | 强制一次真实非流式、一次真实探测和一次流式专项；受控事实只负责精确 P95 边界 |

第一轮独立复审提出的 P95、资格计数、双权限、维护备注、探测分组、稳定排序和真实验收问题均已逐项
写回可实现合同。没有进入编码；第二轮必须继续审查 SQL 资格矩阵、P95 整数算法、maintenance_note
up/down 原子性、事件 owner 和前端迟到响应。

## 11. 第二轮编码前自审

状态：**修正后通过，可以进入编码**。

| 检查项 | 第二轮发现 | 修正 |
| --- | --- | --- |
| 可用性枚举 | 文档声明“冻结映射”但未给出五个状态的逐项关系，前后端可能各自推导 | 明确 `healthy/observing/unhealthy/unknown/disabled` 到产品枚举的一对一映射，禁止前端二次推导 |
| 流式历史兼容 | 四码白名单未区分 0037 历史事实与当前运行时唯一码，容易把旧码误当成新写合同 | 保留历史四码读取；明确当前 `upstream_failed` 质量事实只写 `gateway.upstream_stream_failed`，timeout 使用独立终态 |
| 终态时间完整性 | 只验证 eligible 行会让坏时间的认证、客户端断开或本地失败从 terminal/excluded 静默消失 | 对分页候选 Provider 的全部终态先做状态、时间、延迟预检，任何不可判窗事实统一 503 |
| 探测两阶段查询 | 仅写“先完整分组”不足以证明时间候选查询不会漏掉跨边界 request | 冻结“窗口候选 request -> request 索引回取全部 attempt -> 组级 MAX 二次判窗”，并补未来行和坏事实失败关闭 |
| P95 整数算法 | 需要复核 SQLite 是否会把排名做成浮点或使用不存在的运算符 | 三个独立样本集统一使用正整数 `((95*N+99)/100)` 和窗口函数，边界 N=1/2/19/20/21 均与 nearest-rank 一致 |
| 迁移原子性 | `ALTER TABLE`、事件表重建和 down 阻断若跨事务会留下半结构 | 0053 up/down 自带单个 `BEGIN IMMEDIATE/COMMIT`，全部前置检查位于首项持久 DDL 前，迁移器异常执行 rollback |
| 事件语义 | 若把 status API 200 当访问前提，空态或 503 页面不会产生 PAGE-088 访问事实 | `status.viewed` 绑定已授权 navigation shell，每次进入最多一次，状态数据请求成功与否不改变访问语义 |
| 迟到响应 | 只有 Abort 和通用 generation 时，慢筛选响应的身份约束不够直观、测试也难精确断言 | 请求 ticket 增加 canonical filter key 与完整身份/权限/navigation owner，成功、401/403 和普通错误均先验 owner |
| 路由错误归属 | typed `limit/offset` 会由 FastAPI 抢先返回通用 422，无法满足 PRD 专属错误码 | 路由收原始字符串并调用严格十进制解析，服务层再做同一边界复核 |
| PATCH presence | 仅按 `value is not None` 无法区分省略与显式 null，维护备注不能可靠清空 | 仅对新字段使用 `model_fields_set`，并以 `extra="forbid"` 拒绝未知字段 |
| 审计兼容 | Provider 摘要新增 hash 若不同步白名单，会让创建/更新事务在审计阶段回滚 | 精确扩展 `SUMMARY_FIELDS["provider"]`，Audit/Webhook 只允许不可逆 hash |
| 数值兼容 | SQLite 乘法和 JavaScript Number 都可能让极端计数或延迟溢出/失真 | Python 任意精度计算比例，JSON 标量限定到 JS 安全整数，越界 503 |
| Dashboard 权限 | 仅健康读取的自定义角色可看摘要，却可能得到一个无权详情链接 | 摘要仍按健康读权限；详情链接同时要求双权限和后端导航 owner |
| 沙箱测试路径 | 早期方案仍使用 `.tmp`，与接手后的明确隔离要求冲突 | TEMP/TMP 与独占 basetemp 全部迁至 `D:/MetaHub/.artifacts/prd061-*`；整个 `.tmp` 禁止访问、枚举、写入或删除，且不提权、不访问工作区外路径 |

第二轮逐项对照了运行时 `safe_complete_routing_attempt`、`safe_finish_stream_session`、
`_stream_session_handshake_facts`、PRD-048 的 0037 历史回填合同、PRD-035 探测代表 attempt 选择，以及
迁移器 `executescript + rollback` 行为。修订后未发现 P0 阻断项；实现必须按本节冻结的失败关闭和 owner
合同落地；在当时尚未完成专项测试与实际运行验收，因此该轮自审结束时仍不得把 PRD-061 标记为完成。

## 12. 实现与运行收口复核

状态：**修正后通过**。

1. 开发库运行验收复用了同一 `request_id/trace_id` 连续执行两次人工探测。Provider 状态未变化，
   `health_generation` 同为 7，两个事务都从 `attempt_sequence=1` 开始；旧三元分组会把合法事实误判为坏数据并返回 503。
2. 实现改为四元分组 `(provider_id, request_id, health_generation, created_at)`；`created_at` 是一次正式探测事务
   内全部 Key attempt 的共同持久时间，不进入公开 DTO，也不替代各 attempt 的 `checked_at`。
3. 新增跨 generation 和同 generation 两条独立回归；Provider 状态专项最终 `8 passed`。加载当前源码的临时
   HTTP 实例对原始重复事实返回 200，随后精确删除 2 条受控事实；既有 8001 进程也恢复 200。
4. Edge 最终批次覆盖 loading、真实 200、筛选/重置、立即探测、503、空态、恢复、访问事件、隐私、注销和
   1280x720/390x844 布局；外部请求、page/console error、敏感命中均为 0。
5. 数据库 checkpoint 为 `0,0,0`，`integrity_check=ok`、外键违规 0、迁移 `0001..0053` 连续；临时 Control、
   fake upstream、受控事实和未撤销的任务 Headless Session 均为 0。完整证据见
   `docs/testing/PRD-061-tests.md`。

本节是实现期运行事实触发的额外收口复核，不替代第 10、11 节已经完成的两轮编码前自审。PRD-061 至此满足
设计、实现、测试、文档和实际运行验收门禁；`all_108_prds_complete=false`。
