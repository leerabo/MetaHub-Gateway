# PRD-035 - Provider 健康探测与状态展示

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：设计初稿，等待两轮独立设计审核
- 权威输入：Excel `01_PRD总表`、`04_页面清单`、`05_API接口清单`、`06_数据对象`、`07_Webhook事件`、`08_验收用例`
- 架构约束：Provider 健康属于可观测与调试层；健康状态参与路由，但数据面热路径不得同步调用控制面或现场探测上游

## 需求理解

1. 对当前租户的 Provider Endpoint 执行手工与周期性健康探测，至少验证 Endpoint 连通性及已配置认证凭据是否可用。
2. 提供 `POST /providers/{id}/probe` 和 `GET /provider-health`；保留现有 `POST /providers/{id}/test` 作为兼容入口，内部统一走新的手工探测实现。
3. Provider 健康页和 Dashboard 卡片展示 Provider 状态、最新探测结果、最近错误、最近成功时间、探测延迟和下次计划时间。
4. 周期探测必须抑制瞬时网络抖动：连续失败达到阈值后才自动标记 `unhealthy`；手工探测是用户明确发起的诊断，失败可立即标记 `unhealthy`，兼容 PRD-011 既有语义。
5. 任一成功探测立即恢复为 `healthy` 并清零连续失败计数，控制台下一次刷新自动从红色恢复为绿色。
6. 只有从非 `unhealthy` 转为 `unhealthy` 时触发一次 `provider.unhealthy`；持续失败不重复告警，恢复后再次转坏可再次告警。
7. 数据面始终避开手工 `disabled` Provider，并避开“健康状态为 unhealthy 且结果仍新鲜”的 Provider；健康结果过期时按 `unknown` Fail Open，避免控制面调度停止造成永久路由中断。
8. 每次探测持久化一条安全结果日志，保留 request_id/trace_id；禁止保存或记录 Provider Key、Authorization、上游响应正文和 URL 查询参数。
9. 不建设公开状态页，不引入 Celery、独立调度集群、Redis 健康缓存或完整 SLA/错误率聚合平台；这些不允许拖慢 M0.5。

### 与 PRD-036 的边界

- PRD-035 只在请求路由前读取已持久化且新鲜的健康快照，过滤 disabled/fresh unhealthy Provider 或 Key。
- PRD-035 不处理真实模型请求已发出后的超时、5xx、429，不执行请求重试，不记录 fallback 尝试链，也不在流式输出期间切换模型。
- 主模型调用失败后的非流式备用模型 Fallback、触发条件、尝试顺序和 `route.fallback_triggered` 全部属于 Excel PRD-036，本 PRD 不提前实现。

## 涉及模块

- `infra/migrations/0025_provider_health.sql`：扩展 Provider 当前健康聚合字段，新增探测结果日志、索引和租户归属约束。
- `control-plane-api/src/metahub_control/provider_health`：健康模型、查询 API、探测服务、租约领取、周期任务和事件投递。
- `control-plane-api/src/metahub_control/providers`：新建/修改/启停 Provider 时初始化或重置探测计划；兼容旧 `/test`。
- `control-plane-api/src/metahub_control/auth`：新增 Provider 健康读取/操作权限依赖。
- `control-plane-api/src/metahub_control/app.py`：注册健康路由，并在应用生命周期启动/停止轻量周期任务。
- `gateway-data-plane/src/metahub_gateway/routing`：在已有 Provider 状态过滤中加入新鲜 unhealthy 判断和可解释候选拒绝原因。
- `shared-sdk/python/metahub_shared/runtime/config.py`：增加探测开关、间隔、超时、失败阈值、租约和健康新鲜度配置。
- `shared-sdk/python/metahub_shared/runtime/errors.py`：注册健康查询、探测竞争、凭据不可用和数据层错误码。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：注册 `provider.unhealthy`。
- `console-web/src/lib/providerHealth.js`：健康列表和手工探测客户端。
- `console-web/src/features/provider-health`：Provider 健康页。
- `console-web/src/features/dashboard`：Provider 健康摘要卡片。
- `control-plane-api/src/metahub_control/console`、`console-web/src/app/main.js`：角色化导航、当前页加载、刷新和事件处理。
- 后端、数据面、前端和迁移测试：主流程、恢复、误报、并发、权限、跨租户、脱敏和路由覆盖。

## 数据表/字段设计

### providers 聚合字段扩展

沿用 `providers.health_status/last_tested_at/last_test_error`，避免建立两个可漂移的“当前状态”来源；新增：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `last_success_at` | TEXT NULL | 最近成功探测 UTC 时间 |
| `last_failure_at` | TEXT NULL | 最近失败探测 UTC 时间 |
| `last_probe_latency_ms` | INTEGER NULL CHECK >= 0 | 最近探测耗时 |
| `last_probe_http_status` | INTEGER NULL CHECK 100..599 | 最近上游 HTTP 状态；网络错误为空 |
| `last_probe_error_code` | TEXT NULL | 固定安全分类，不保存上游正文 |
| `consecutive_probe_failures` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 周期误报抑制计数 |
| `health_generation` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 健康相关配置代际，阻止旧 Endpoint/Key 结果回写 |
| `health_updated_at` | TEXT NULL | 健康聚合更新时间；探测不得污染 Provider 配置 `updated_at` |
| `next_probe_at` | TEXT NULL | 下次周期探测时间；disabled Provider 为空 |
| `probe_lease_token` | TEXT NULL | 多进程/手工并发领取令牌 |
| `probe_lease_until` | TEXT NULL | 进程异常后的租约自动过期时间 |

状态仍为 `unknown/healthy/unhealthy`。周期任务第 1、2 次失败只增加计数并保留原状态；达到阈值后转为 `unhealthy`。手工失败立即转为 `unhealthy`。成功立即转 `healthy`。

0025 应用时不能继承 PRD-011 匿名 `/test` 写入的旧健康结论：所有现存 Provider 统一重置为 `unknown`、`health_updated_at=NULL`、失败计数为 0、租约为空；active Provider 回填 `next_probe_at=now`，disabled 保持空。Gateway 只有在 `health_updated_at IS NOT NULL` 时才把 unhealthy 当作新版可摘流事实。

Provider 创建时 `health_status=unknown`、`health_generation=0`、`next_probe_at=now`。修改 `provider_type/base_url/auth_type/status`，以及 Provider Key 创建/启停时，必须在原管理事务内递增 generation、重置为 unknown、清除现有 lease；active Provider 立即到期，disabled 清空排期。健康探测只更新 `health_updated_at`，不修改表示配置变化的 `providers.updated_at`。

`health_updated_at` 专指“最近一次成功应用的 conclusive 聚合健康证据时间”。`inconclusive` 只能写 attempt 日志、释放租约并推进 active Provider 的 `next_probe_at`，不得修改 Provider/Key 健康状态、失败计数、最近成功/失败时间或 health_updated_at；因此旧 unhealthy 会按新鲜度窗口正常过期并 Fail Open。

### provider_credentials 探测字段扩展

Provider 聚合不能把单把坏 Key 放大成整个 Endpoint 故障，因此在现有 Key 运行状态之外新增内部探测状态：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `probe_health_status` | TEXT CHECK `unknown/healthy/unhealthy` | 仅认证探测状态，不改变 admin 管理状态 |
| `probe_last_tested_at` | TEXT NULL | 最近认证探测时间 |
| `probe_last_success_at` | TEXT NULL | 最近认证成功时间 |
| `probe_last_error_code` | TEXT NULL | 固定安全分类 |
| `probe_consecutive_failures` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 认证失败计数 |

API 不返回这些字段或 credential ID。401/403 只更新对应 Key；网络/TLS/5xx 不归咎于 Key。数据面 KeyPool 跳过“新版且新鲜 unhealthy”的 Key，过期结果按 unknown Fail Open；周期探测仍会覆盖这些 Key，以便认证恢复后重新进入 KeyPool。

### provider_health_probe_logs

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `provider_health_probe_id` | TEXT PK | `php_<uuid>` |
| `tenant_id` | TEXT NOT NULL | 当前认证或 Provider 所属租户 |
| `provider_id` | TEXT NOT NULL | 被探测 Provider |
| `provider_credential_id` | TEXT NULL | 内部认证尝试维度；API/Webhook 不返回 |
| `health_generation` | INTEGER NOT NULL | 领取时配置代际，用于审计迟到结果 |
| `attempt_sequence` | INTEGER NOT NULL CHECK >= 1 | 同一 request_id 内的尝试顺序 |
| `trigger_type` | TEXT CHECK `manual/scheduled/legacy_test` | 新状态机探测来源；`legacy_test` 为后续迁移预留，当前旧入口不写本表 |
| `result_status` | TEXT CHECK `success/failed/inconclusive` | 本次结果；inconclusive 不改变聚合状态 |
| `auth_result` | TEXT CHECK `not_required/accepted/rejected/unavailable/unverified` | Provider 能力矩阵下的认证结论 |
| `credential_source` | TEXT CHECK `none/managed/environment` | 只记录凭据来源，不记录凭据 ID、前缀或指纹 |
| `credential_attempt_count` | INTEGER NOT NULL DEFAULT 0 CHECK >= 0 | 本次尝试的凭据数量，仅用于判断 KeyPool 聚合可用性 |
| `http_status` | INTEGER NULL CHECK 100..599 | 安全 HTTP 状态 |
| `latency_ms` | INTEGER NOT NULL CHECK >= 0 | 探测耗时 |
| `error_code` | TEXT NULL | 固定分类：timeout/auth_failed/credential_unavailable/unreachable/invalid_endpoint/unexpected_status |
| `result_summary` | TEXT NOT NULL CHECK UTF-8 bytes <= 240 | 服务端固定安全摘要，按 UTF-8 安全截断；数据库用 BLOB length 兜底 |
| `request_id` | TEXT NOT NULL | 手工请求或调度生成 ID |
| `trace_id` | TEXT NOT NULL | 调度与事件链路 ID |
| `checked_at` | TEXT NOT NULL | UTC 探测时间 |

每个网络尝试写一条日志，因此同一 request_id 可能有多条 attempt；GET 页面只返回服务层聚合后的最新安全结果。

约束与索引：

- 外键 `provider_id -> providers.provider_id`，INSERT trigger 校验 Provider 与 tenant_id 同属一个租户。
- 索引 `(tenant_id, checked_at DESC)`、`(tenant_id, provider_id, checked_at DESC)`、Provider due 索引 `(status, next_probe_at)`。
- 日志没有更新/删除 API；MVP 不声明 WORM，后续可由统一留存策略清理。
- `provider_credential_id` 非空时，INSERT trigger 同时校验 Key、Provider、tenant_id 三者归属。
- down 迁移采用项目 SQLite >= 3.35 已支持的确定性 `ALTER TABLE ... DROP COLUMN`：先删除 0025 trigger/index/log 表，再按依赖反序删除 Provider/Key 新字段，保留 0005 原健康字段；不在运行时二选一。迁移前后执行 `PRAGMA foreign_key_check`，并测试“0024 数据 -> 0025 up -> 健康写入 -> down -> 再 up”往返。

## API 设计

### POST /providers/{provider_id}/probe

- 权限：admin、developer；viewer 后端返回 403。
- 请求体：路由使用 `Body(default=None)` 的可选空 Pydantic 模型，模型设置 `extra="forbid"`；省略或 `{}` 均合法，显式提交 URL、tenant_id、header、Key 或其他字段返回 422。超时和探测策略由服务端配置，避免调用者制造超长出站请求。
- 行为：按 `tenant_id + provider_id` 加载配置，领取短租约，按 KeyPool 规则加载 active 托管凭据或进程级安全兜底；执行不跟随重定向的安全健康探测；同事务写探测日志和 Provider 聚合状态；提交后尽力投递状态转换事件。
- disabled Provider 允许手工探测，便于启用前验证；周期任务不会探测 disabled Provider。
- 200 响应：`provider_health`、`probe_result`、`request_id`、`trace_id`。上游失败属于被成功执行的探测结果，仍返回 200 + `result_status=failed`；接口自身权限、归属、数据库或竞争错误使用统一非 2xx。

### POST /providers/{provider_id}/test

- 标记为兼容入口并继续保持 admin-only，不因新健康接口向 developer 扩权；保留现有请求体 `timeout_seconds`，服务端仍夹紧到安全范围。
- 为避免破坏 PRD-011 已发布合同，当前继续保留 admin-only、请求字段、timeout 0.5..15 秒夹紧、HTTP 200 表达上游失败、3xx 口径、404 根路径 fallback，以及 `provider/test_result/request_id/trace_id` 响应结构。
- 旧入口仍可能写 `providers.health_status/last_tested_at/last_test_error`，但绝不写 `health_updated_at`、Key probe 字段或 `provider_health_probe_logs`；因此它只是兼容展示状态，不能形成数据面自动摘流的新版证据。
- 新 `/probe` 是唯一进入 PRD-035 租约、认证凭据、严格 SSRF 和探测日志状态机的入口；后续若迁移旧入口，必须另立兼容弃用计划，不能在本 PRD 静默改变行为。

### GET /provider-health

- 权限：admin、developer；viewer 后端返回 403。
- 查询参数：`health_status`、`provider_status`、`provider_type`、`provider_id`、`limit`（1..100，默认 50）、`offset`（0..10000）。不接受 tenant_id。专用查询依赖读取原始值并显式映射业务错误，避免 FastAPI 默认 validation code 破坏 `filter_invalid/pagination_invalid` 合同。
- 按 `tenant_id` 首条件查询，默认 unhealthy/observing 优先，再按 `health_updated_at DESC, provider_id ASC`。
- 返回：`provider_health[]`、`summary`、`total`、`limit`、`offset`、`request_id`、`trace_id`。
- 单项同时返回原始 `health_status` 和派生 `effective_health_status`。effective 取值互斥：provider disabled -> disabled；连续失败未达阈值 -> observing；健康证据过期或从未有新版证据 -> unknown；其余为 healthy/unhealthy。另返回最近结果、最近成功/失败、连续失败、下次探测和 `is_health_stale`；不返回凭据 ID、Key 前缀、URL userinfo/query/fragment 或上游正文。
- `summary` 基于应用全部筛选后的结果、在分页前统计；total 与 healthy/unhealthy/unknown/observing/disabled 五个互斥桶之和一致。active 是额外配置状态计数，不参与上述互斥求和。列表排序、页面颜色和健康比例使用 effective 状态，Gateway 仍只用原始状态 + health_updated_at。

### Dashboard

不新增公开接口。admin/developer 进入 Dashboard 时并行调用 `GET /provider-health?limit=5` 渲染摘要卡片；viewer 不加载健康接口，也不显示卡片。这样不扩大现有 viewer Dashboard 的 Provider 元数据可见范围。

## 探测与调度设计

1. 仅控制面 `metahub_control.app` 组合现有 `app.router.lifespan_context` 后增加健康调度，不替换共享工厂生命周期。启动单个 task；shutdown 先停止领取，再有界等待已领取的 `to_thread` 工作完成，最后取消并 await 调度循环，避免误以为取消 awaiter 能终止底层线程。
2. scheduled 与 manual/legacy 使用两个原子 claim 条件：scheduled 只领取 active + due + lease 可用行；manual/legacy 按当前租户可领取 active 或 disabled 行，不要求 due。disabled 手工完成后保持 `next_probe_at=NULL`；active 手工完成后推进一个完整间隔，避免紧接着被周期任务重复探测。
3. 每轮使用有界执行槽；每个空闲槽即时领取一条 scheduled due Provider，而不是先批量占有再串行处理。领取使用短 `BEGIN IMMEDIATE` 和条件 UPDATE，同时快照 `health_generation`。网络请求完全在事务外执行。
4. 租约期限大于单次超时；进程崩溃后自动可重领。完成事务先以 `provider_id + lease_token + health_generation` 做条件 UPDATE，rowcount 必须为 1；调度任务静默丢弃 superseded 结果，手工请求返回 `provider_health.probe_superseded` 409。两者都整笔回滚，不更新 Key 探测状态、不插入 attempt 日志、不发事件。租约过期重领只能保证至多一次状态提交，不能保证至多一次 HTTP 请求。
5. 周期默认间隔 60 秒、超时 5 秒、连续失败阈值 3、租约 20 秒、新鲜度 180 秒；分别由 `METAHUB_PROVIDER_HEALTH_ENABLED/INTERVAL_SECONDS/TIMEOUT_SECONDS/FAILURE_THRESHOLD/LEASE_SECONDS/STALE_SECONDS/CONCURRENCY` 配置并做上下界校验。控制面和 Gateway 共用同一 AppConfig 新鲜度字段；测试直接调用单轮函数，不等待真实 sleep。
6. 当前控制面镜像不包含数据面包，且现有 Adapter 协议没有 health 方法，因此探测实现位于控制面 `provider_health`，不跨服务导入 Gateway。定义明确能力矩阵：内置 OpenAI-compatible 家族使用无计费副作用的 `GET {base_url}/models`；`custom/migration_compatible` 或当前无法确认受保护资源的类型只能得到 `auth_result=unverified` 和 `result_status=inconclusive`，不得据此标 healthy/unhealthy。后续厂商专用 Adapter 必须补 probe contract 后才能参与自动摘流。
7. HTTP 使用 streaming response 只读取状态、不下载或保存 body；`trust_env=False`、TLS 默认校验、不跟随重定向，3xx 明确判失败。探测底层吞掉原始 request/response 异常，只向上返回固定枚举，不记录异常 repr、请求头或 httpx debug 内容。
8. `auth_type=none` 不发送 Authorization；其他现有类型保持数据面当前 Bearer 语义，不能虚构尚无 header-name 数据模型的自定义 Header。托管 Key 存在时不回退环境变量，也不复用会更新 `last_used_at` 的数据面 Key 选择函数。
9. Provider 聚合可用性以“Endpoint 可达且 KeyPool 至少一把当前可选凭据被 probe contract 接受”为准：401/403 只标记对应 Key unhealthy并继续；429 对应 Key进入现有短冷却并继续；任一 accepted 即 Provider 成功。网络/TLS/5xx 属于 Endpoint 级故障，不归咎于 Key，可结束本次探测。所有候选均为 429 时整体 inconclusive，不增加 Provider 失败计数、不刷新健康时间；只在 generation CAS 成功后提交仍属 active Key 的冷却。所有可选 Key rejected/unavailable 才形成 Provider 失败。超过 20 把采样上限且尚无成功时同样 inconclusive。
10. 若没有可用凭据或全部解密失败，结果分类为 `credential_unavailable`。探测更新 Key 专用 probe 字段，但不修改 `last_used_at`、权重或 admin 管理状态；Key 的 fresh unhealthy 过滤同样有新鲜度边界。
11. SQLite 文件库为健康任务连接设置 5 秒 `busy_timeout` 并启用 WAL；claim/complete 对 BUSY/LOCKED 最多有限退避重试 3 次。默认并发 1、最大 4，避免健康探测争用用量、审计和 KeyPool 写锁；并发测试使用真实文件数据库和多个连接。
12. Provider 可能是客户内网 Endpoint。默认拒绝 loopback、link-local、multicast、unspecified、云 metadata 地址和非批准端口；私网地址必须命中部署级 `METAHUB_PROVIDER_HEALTH_PRIVATE_CIDRS` allowlist。每次请求前解析并校验全部 A/AAAA，禁止 userinfo/fragment 和重定向；probe API 不接受 URL。MVP 明确记录 DNS 校验与连接之间仍存在的极短 rebinding 残余风险，生产应配合出站网络 ACL。

## 数据面路由设计

- 查询候选时同时读取 `provider_status/health_status/health_updated_at`；KeyPool 同时读取 Key 的 `probe_health_status/probe_last_tested_at`。
- `provider_status != active` 始终拒绝，原因 `provider_disabled`。
- 只有 `health_updated_at IS NOT NULL`、`health_status=unhealthy` 且健康时间在配置的新鲜度窗口内，才以 `provider_unhealthy` 拒绝；迁移前旧匿名测试结果永远不能摘流。
- KeyPool 先跳过新版且新鲜 unhealthy 的单把 Key；过期 Key 结论按 unknown 允许。至少一把可选 Key 存在时，单把坏 Key 不影响整个 Provider 候选。
- unhealthy 结果超过新鲜度窗口后视为 stale/unknown，允许候选并在 routing decision 安全摘要记录 `provider_health_stale=true`，避免调度停止导致永久 Fail Closed。
- 别名已有 default/fallback 候选解析时可选择下一个健康候选；直接模型没有候选时返回统一 `gateway.provider_unhealthy` 503。这里不执行请求失败后的重试或中途 Fallback，后者属于 PRD-036。
- 混合拒绝原因优先级固定为：存在健康候选则正常选择；否则 `provider_disabled` 优先表示明确管理禁用，`provider_unhealthy` 次之，最后才是 model/provider missing，保证 API 错误和 routing decision 稳定可测。
- 路由只读数据库当前状态，不现场探测、不调用控制面 API，不增加外部网络往返。

## 前端页面/交互设计

### PAGE-051 Provider 健康页

- admin/developer 导航可见，viewer 不显示；直接访问仍由后端权限拒绝。
- 顶部为紧凑摘要条：健康、异常、观察中、未知、已禁用数量，不使用营销式大卡片。
- 筛选区：健康状态、配置状态、Provider 类型；提供重置按钮。
- 主列表按 Provider 展示名称/code、类型、已清洗 Endpoint、配置状态、健康状态、最近探测、最近成功、延迟、错误分类、下次探测。
- 状态颜色：healthy 绿色；fresh unhealthy 红色；连续失败未达阈值为琥珀色“观察中”；unknown/stale 灰色；disabled 单独灰色。
- admin/developer 均有“立即探测”图标按钮及 tooltip；单行探测时仅锁定该行，完成后局部更新并显示 request_id。
- 15 秒自动刷新只在当前导航为 `provider-health` 且角色允许时执行。健康模块使用独立 request generation + AbortController：POST 开始时取消/作废在途 GET；POST 结果只有在登录 token、角色、当前导航和行操作 token 均未变化时合并，随后发起新 GET，避免旧轮询覆盖刚完成的探测。
- 最近错误只显示服务端固定分类和安全摘要，长 Provider/request ID 完整换行。

### PAGE-052 Dashboard 卡片

- admin/developer 显示一块 Provider 健康摘要，包含健康比例、异常/观察中数量和最多 3 个异常 Provider。
- 点击跳转 `#provider-health`；viewer 不请求也不渲染该块。
- Dashboard 健康请求使用独立 catch/allSettled；原有用量/成本/错误指标不受健康接口失败影响，健康卡片单独降级并显示统一错误码/request_id。

### 响应式与可访问性

- 1440 宽使用紧凑表格，1100 以下切为单项卡片布局，720/390 单列；长 ID 和 URL 使用 `overflow-wrap:anywhere`，不得横向溢出。
- 操作按钮使用现有图标库/按钮体系和 tooltip；状态同时有文字，不只依赖颜色。
- 页面没有 Provider 启停或 Key 管理控件，避免 developer 借健康页获得 admin 配置能力。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `provider.not_found` | 404 | Provider 不存在或不属于当前租户，复用现有防枚举语义 |
| `provider_health.filter_invalid` | 422 | 状态、类型或 Provider ID 筛选非法 |
| `provider_health.pagination_invalid` | 422 | limit/offset 越界 |
| `provider_health.probe_in_progress` | 409 | 同一 Provider 已被手工或周期任务领取 |
| `provider_health.probe_superseded` | 409 | 手工探测期间 Provider 类型、Endpoint、状态或 KeyPool 已变化，旧结果被 CAS 丢弃 |
| `provider_health.record_unavailable` | 503 | 租约、探测日志或状态事务写入失败 |
| `provider_health.query_unavailable` | 503 | 健康列表查询失败 |
| `auth.permission_denied` | 403 | viewer 或未允许角色调用健康接口 |
| `gateway.provider_unhealthy` | 503 | 直接模型唯一 Provider 为新鲜 unhealthy，数据面无可用候选 |

所有 HTTP 错误使用统一异常处理中间件，响应携带 request_id、trace_id。`credential_unavailable`、`auth_failed`、`timeout` 等只属于 200 probe_result 的固定分类，不注册成同名 HTTP API 错误；上游失败不是 probe API 自身失败。

## 安全与权限约束

1. GET/POST 仅 admin、developer；viewer 后端拒绝。Provider 配置和 Key 管理仍仅 admin。
2. tenant_id 只来自 AuthContext 或 Provider 归属；调度器从 Provider 行恢复 tenant_id，接口不接受 tenant_id。
3. Provider Key 仅在最小作用域内存对象中解密并写入出站 Authorization；原始 httpx request/response/exception 不得穿透探测边界，数据库探测日志、应用/httpx 日志、Webhook、API DTO 和异常链中不得出现明文、密文、完整指纹或 Authorization。
4. 不保存上游响应 body、header、TLS 证书正文、URL userinfo/query/fragment；错误只使用固定分类和状态码。
5. probe API 不接受任意 URL；`trust_env=False`、不跟随重定向、超时有硬上限，降低 SSRF 放大和代理污染风险。
6. 手工与周期探测共用租约，防止并发风暴；每轮有批量上限，disabled Provider 不参与周期探测。
7. Webhook 只在状态转换时发送，payload 仅含 provider_id/code、健康状态、失败分类、连续失败数、checked_at 和 trace_id。event_id 固定为 `evt_<transition_probe_id>`，同一逻辑转换的全部传输重试复用该 ID。
8. 探测状态和日志同一事务提交；Webhook 提交后尽力投递，投递失败不能反转健康事实。
9. 路由只使用新鲜聚合状态，过期健康结果 Fail Open 并保留可解释原因。
10. 现有管理审计不记录周期探测；探测本身由专用结果日志追踪，避免审计日志被每分钟噪声淹没。
11. `provider.unhealthy` 沿用现有同步、最多 3 次立即重试的提交后 best-effort 语义；合同是“每次状态转换产生一个稳定逻辑事件，传输可能重复也可能丢失”，接收端按 event_id 幂等。没有 outbox/pending 恢复器，不承诺至少一次投递。

## 测试用例

### 主流程与恢复

1. admin/developer 手工探测可达且认证成功的 Provider，返回 success、写日志、状态 healthy、最近成功/延迟/下次时间正确。
2. 错误 Endpoint 手工探测后立即 unhealthy，固定错误摘要不含上游正文；恢复 Endpoint 后再次探测自动 healthy、失败计数归零。
3. 周期任务连续 1/2 次失败只进入 observing，不产生 unhealthy 事件；第 3 次转 unhealthy 并只发一次 `provider.unhealthy`。
4. unhealthy 后持续失败不重复事件；成功恢复后再次达到阈值可再次事件。
5. 每轮只领取到期 active Provider；disabled 不探测；重新启用、修改 Endpoint 或 KeyPool 后 generation 递增并立即重新排期。
6. 多个调度执行者并发领取同一 Provider，正常租约期内只有一个 HTTP 探测；租约过期可发生重复 HTTP，但只有匹配 token+generation 的一个结果能提交日志、状态和事件。
7. Provider/Key 配置在探测中途变化时，迟到完成事务 rowcount=0 并整体回滚，不留下旧 generation 日志或事件。
8. 文件 SQLite 多连接竞争时 BUSY/LOCKED 有界重试且任务循环继续，不产生重复状态提交。
9. 连续 inconclusive 只写日志和排期，不刷新旧 unhealthy 的 health_updated_at；旧状态到达 stale 窗口后正常 Fail Open。
10. lifespan 开关关闭不启动、TestClient 只启动一次；网络线程执行中 shutdown 停止新领取、有界等待且无遗留 asyncio task。

### 认证、脱敏与安全

11. bearer Provider 使用 active 托管 Key 发出 Authorization；测试服务器能确认认证被接受，但数据库/API/应用与 httpx caplog/异常链/Webhook 均扫描不到明文和密文。
12. auth_type=none 不发送 Authorization；无凭据、解密失败、401/403、超时、连接/TLS失败、非法 URL、重定向和 5xx 分类正确；全部凭据仅收到 429 时只冷却 Key，Provider 结果为 inconclusive 且不累计失败。
13. KeyPool 一把坏 Key、一把好 Key时 Provider 仍为 healthy 且坏 Key被新鲜 probe 状态过滤；全部 active Key 认证失败时才进入 Provider 失败；超过凭据采样上限时不得误摘流。
14. 不支持或无法验证受保护资源的 Provider 类型返回 unverified/inconclusive，聚合保持 unknown/原状态，不参与自动摘流。
15. 上游返回恶意 HTML/超长正文或敏感 token，探测记录和响应只包含固定摘要，客户端不下载正文。
16. probe 请求不能提交 URL、tenant_id、header 或 Key；多余字段按严格模型拒绝。
17. loopback/link-local/metadata/多播/未批准端口被拒绝；私网 only allowlist、IPv4/IPv6、多地址 DNS 和 DNS 变化路径有测试。

### 权限与租户

16. admin/developer 可调用新 GET/POST；viewer 403；未登录 401；旧 `/test` 仍 admin-only。
17. 使用其他租户 Provider ID 手工探测返回 404；GET 任何筛选都不返回其他租户数据。
18. 直接插入 tenant_id 与 Provider/credential 归属不一致的探测日志由 trigger 拒绝。

### 路由

19. 迁移前已有匿名 unhealthy 的 Provider 被重置 unknown；`health_updated_at=NULL` 不能摘流。
20. explicit disabled Provider 始终不参与直接或别名路由。
21. 新版新鲜 unhealthy 候选被拒绝，别名选择下一健康候选；候选摘要记录 `provider_unhealthy`。
22. 直接模型唯一 Provider 新鲜 unhealthy 返回 `gateway.provider_unhealthy`，失败请求仍进入 request_logs。
23. Provider/Key unhealthy 状态过期后按 stale unknown 放行，并记录 stale 可解释摘要；数据面不执行现场探测。
24. 混合 missing/disabled/unhealthy 候选的错误优先级稳定。

### API 与前端

25. GET 状态/类型/Provider 筛选、summary、total、limit/offset、稳定排序和统一 request_id 正确；非法参数返回统一错误。
26. `provider.unhealthy` 每次转换使用稳定 event_id；持续失败无新逻辑事件，Webhook 传输重试复用同一 ID，投递失败不回滚健康状态。
27. Provider 健康页角色导航、手工探测单行 loading、筛选重置、错误状态和 HTML 转义正确；延迟 Promise 跨越导航、定时刷新、POST/GET、退出登录和 token 更新时，由独立 generation 与 AbortController 保证旧响应不能覆盖新状态。
28. Dashboard admin/developer 卡片独立加载；viewer 不发健康请求；健康接口失败不破坏原 Dashboard。
29. 1440/1100/720/390 下无横向溢出、文字重叠或长 ID 截断，状态不只依赖颜色。
30. 旧 `/providers/{id}/test` 的 PRD-011 兼容矩阵 golden tests 通过；其结果不写 `health_updated_at`、Key probe 状态或新探测日志，不能参与数据面摘流。
31. 0024 旧库升级时匿名健康状态被重置、字段/索引/trigger/CHECK 生效；执行 0024 -> 0025 -> down -> 0025 往返后 `foreign_key_check` 为空且迁移结果确定。

## 代码修改计划

1. 新增 `0025_provider_health.sql/down.sql`，先完成字段、日志、索引、归属和回滚测试。
2. 新增 `provider_health` 模块，先实现纯探测分类和聚合状态机，再实现租约、日志、查询及事件。
3. 接入 Provider 新建/修改/启停排期和旧 `/test` 兼容；注册权限、错误码、Webhook 事件和路由。
4. 实现轻量应用生命周期调度器和可直接测试的单轮执行函数。
5. 数据面候选查询加入健康字段、新鲜度判断、路由决策原因和 `gateway.provider_unhealthy`。
6. 实现 Provider 健康 API 客户端、页面、导航、手工探测、自动刷新和 Dashboard 摘要卡片。
7. 增加迁移、控制面、数据面、Webhook、脱敏、并发、权限、前端与响应式测试。
8. 进行两轮独立代码审查，修复后执行聚焦、控制面/数据面/前端全量、编译、中文注释门禁和运行态验收。
9. 更新 README、目录文档和 `docs/testing/PRD-035-tests.md`。

## 设计自审

### 第一轮自审

- 状态：已完成；初审发现 2 个 P0、5 个 P1、2 个 P2，修订后通过。
- P0-1：旧 `/test` 的匿名 unhealthy 不能直接参与路由。0025 现要求全部旧聚合重置 unknown，Gateway 仅使用 `health_updated_at` 非空的新版结果。
- P0-2：单把坏 Key 不能放大为整个 Provider 故障。设计新增 Key probe 状态和逐凭据 attempt；至少一把可用时 Provider 保持 healthy，KeyPool 先过滤新鲜坏 Key。
- P1-1：`GET /models` 2xx 不一定能证明所有认证类型。设计增加 Provider probe capability matrix 和 accepted/rejected/unverified 口径；不支持类型 inconclusive，不摘流。
- P1-2：仅 lease token 不能覆盖配置变化。设计新增单调 health_generation，完成事务以 token+generation CAS，失败时日志/状态/事件整体回滚。
- P1-3：SQLite 多线程可能写锁冲突。设计固定网络在事务外、短 `BEGIN IMMEDIATE`、busy_timeout、WAL、有限退避和默认单并发。
- P1-4：保存 URL 不足以构成 SSRF 边界。设计增加地址/端口拒绝、私网 CIDR allowlist、全 A/AAAA 预检和出站 ACL 残余风险说明。
- P1-5：旧 `/test` 不能污染新版路由健康证据。最终采用完整 PRD-011 行为兼容，并以 `health_updated_at`、Key probe 字段和新日志隔离保证旧匿名结果永不摘流；收紧行为留给独立弃用计划。
- P2-1：事件重试可能重复或丢失。设计用 transition probe ID 生成稳定 event_id，明确 best-effort 与接收端幂等合同。
- P2-2：httpx 异常可能携带 Authorization。设计要求底层吞掉原始 request/response/exception，只返回固定枚举，并覆盖应用/httpx 日志和异常链脱敏测试。
- 结论：第一轮问题均已落实，可以进入第二轮设计审查。

### 第二轮自审

- 状态：独立初审完成，发现 2 个 P0、7 个 P1、3 个 P2；修订后由主流程逐项核对正文与测试落点，确认关闭。原审查代理复审时触发工具用量上限，未伪造其复审结论。
- P0-1：inconclusive 若刷新 `health_updated_at`，会让旧 unhealthy 永不过期。现限定该字段只由 accepted/rejected 的确定性证据更新，inconclusive 仅写日志和下次排期。
- P0-2：修改 `provider_type` 也会使在途探测失效。现已将其纳入 health_generation 递增与健康重置条件。
- P1-1：disabled 手工探测与 active 定时领取条件冲突。现拆分领取规则：调度仅领取 active 到期项，手工探测可领取当前租户的 active/disabled 项并受独立租约约束。
- P1-2：调度生命周期不能覆盖既有 lifespan，关闭时也不能遗留线程任务。现要求组合原 lifespan，shutdown 先停新领取，再有界等待 `to_thread` 完成并清理 task。
- P1-3：raw 与 effective 状态及 summary 口径不清。GET 同时返回原始状态和互斥 effective_status，summary 按筛选后、分页前的 effective_status 聚合。
- P1-4：空 body、非法 query 和 superseded 冲突缺统一错误映射。现使用可选严格空模型，补齐参数错误映射和 `provider_health.probe_superseded` 409。
- P1-5：全部凭据 429 不能判 Provider 故障。现定义为 inconclusive，仅更新对应 Key 冷却状态，不累计 Provider 失败或刷新健康证据时间。
- P1-6：down migration 需可重复验证。现按 SQLite 3.35+ 明确删除顺序和 `DROP COLUMN`，并增加往返迁移与 `foreign_key_check` 测试。
- P1-7：前端 POST/GET、定时刷新及离开页面存在响应覆盖竞态。现以独立 generation 与 AbortController 管理各请求域，Dashboard 使用独立异常隔离。
- P2-1：summary、total 与分页口径需明确。现统一为应用筛选后的全量集合，分页只影响 items，active 总数与各 effective bucket 互斥可核对。
- P2-2：摘要长度和 HTTP 状态约束不能只靠应用。现按 UTF-8 字节截断并增加数据库长度与 HTTP 状态 CHECK。
- P2-3：旧 `/test` 与新状态机边界缺失。现明确旧入口不产生 inconclusive，也不写任何新版健康证据；新 `/probe` 独立返回完整结果分类。
- 结论：全部发现已写入设计与测试计划并完成逐项关闭性核对，可以进入编码。
