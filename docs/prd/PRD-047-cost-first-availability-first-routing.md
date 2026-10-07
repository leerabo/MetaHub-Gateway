# PRD-047 - 成本优先/可用性优先路由

状态：已完成。两轮独立设计自审、实现期第三轮安全复审、代码实现、专项与根级全量、0036 迁移、真实 API/三服务/Webhook/Usage、停止后数据库与日志复核，以及真实 Chrome 三档验收均已通过。

## 1. 需求来源与顺序确认

- Excel `01_PRD总表!48` 与 `02_M0-M1核心开发!48` 均定义本条为 `PRD-047`、`M1-企业POC`、`P1`、核心层，模块为“路由引擎”，名称为“成本优先/可用性优先路由”。
- 启动本条设计时，README、`docs/prd`、`docs/testing` 和最终运行证据连续完成到 PRD-046，且 README 当时明确登记 PRD-047 尚未进入设计或实现。因此 PRD-047 是按 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3` 识别出的下一条未完成 PRD；本条完成后的下一项仍必须重读 Excel，不按编号猜测。
- Excel `04_页面清单!68` 指定 `PAGE-067 - 路由策略页`，角色为管理员/开发者。
- Excel `05_API接口清单!224:227` 把一个路径错误拆成 `API-223..226` 四行；按 `01_PRD总表!48` 的权威接口合并为唯一业务入口 `PATCH /model-aliases/{id}/routing-mode`，不能实现为四个端点。
- Excel `06_数据对象!67:68` 指定 `OBJ-066 - RoutingPolicy` 与 `OBJ-067 - RoutingCandidate`；所有业务事实必须由 `tenant_id` 约束。
- Excel `07_Webhook事件!54` 指定 `EVT-053 - route.policy_selected`，必须使用统一 Webhook 信封、签名和投递记录，不能只写普通日志。
- Excel `08_验收用例!94:95` 指定 `TC-0093/0094`：`cost_first` 选择低成本健康模型，`availability_first` 避开故障 Provider，并覆盖选择逻辑、边界成本、依赖故障、无权限、统一错误码、敏感信息保护和状态追踪。
- 系统架构 7.2 规定固定路由、健康过滤、成本优先或手工优先级属于当前路由核心；按用户/部门/项目路由属于 M2。架构的质量-成本反馈、A/B、缓存感知不是本条范围。

## 2. 需求理解

### 2.1 交付目标

管理员可以为一个租户、一个 Environment 内的 ModelAlias 选择以下模式：

1. `fixed`：完整保持现有 default -> fallback 的调用前健康过滤与稳定顺序。
2. `cost_first`：先剔除当前不可调用候选，再在同一请求、同一价格时点按统一比较币种的本请求估算成本从低到高排序；原币种金额仍作为计费事实保留。
3. `availability_first`：先剔除当前不可调用候选，再优先选择具有新鲜 healthy 证据的 Provider；unknown 或 stale 证据作为次级候选。

每次真实 Alias 路由必须冻结并追踪：RoutingPolicy ID/版本/模式、候选资格与排序、最终 Model/Provider、稳定原因码和 Webhook event ID；只有实际进入成本比较时才增加价格 `as_of` 与输入/输出 token 估算。手工 SQL 或旧测试夹具缺少策略行时只允许走 nullable、`implicit fixed` 的 legacy 兼容路径，且不发送 `route.policy_selected`；正常 0036 运行库必须有持久策略。调用日志详情必须能解释“为什么选中”和“为什么剔除”，但不能展示 Provider Key、环境变量名、Endpoint、Prompt、Response 或上游错误正文。

### 2.2 “不同项目”的准确边界

Excel 用户故事中的“不同项目选择省钱或稳定”不等于在本条创建 Project override。架构 7.2 把按项目路由放在 M2，且 Excel 的唯一配置入口明确挂在 ModelAlias 上。

本条的合同是：项目或应用在请求中选择不同的公开 ModelAlias，例如分别使用企业自行命名的“省钱”Alias 和“稳定”Alias；每个 Alias 独立持有 RoutingPolicy。`project_id` 仍作为可信 Virtual Key 归属进入调用日志和 Webhook 安全摘要，但不参与本条策略查找键，也不能由客户端请求体覆盖。

### 2.3 RoutingPolicy 与 RoutingCandidate 的生命周期

- `RoutingPolicy` 是一对一、可版本追踪的持久配置对象。每个 ModelAlias 只有一条当前策略，创建 Alias 时默认生成 `fixed v1`，迁移为所有存量 Alias 回填 `fixed v1`。版本覆盖完整的初始路由配置：模式，以及 Alias 的名称、Environment、状态、default/fallback 目标；这些字段任一真实变化都必须在同一事务令策略版本严格 +1，同值更新不增版本。
- Alias 的 `default_model_id/fallback_model_id` 继续是候选成员的唯一配置源。Excel 没有候选增删 API，本条不得另建可编辑候选配置表，否则会与 Alias 双写漂移。
- `RoutingCandidate` 是一次真实路由请求中派生并持久化的评估事实，不是第二份候选配置。每个 Alias 请求最多产生 default/fallback 两行，记录配置角色、健康/KeyPool/价格快照、排序名次、选中或剔除状态和原因码。
- 控制台 RoutingPolicy 页面展示的“当前候选”由 Alias 主备关系实时派生；调用日志详情展示的是请求发生时不可变的 RoutingCandidate 事实。两者不能混称。

### 2.4 与既有路由合同的关系

- Policy DSL 的 `route_to` 继续最高优先：它只解析一个具体 Model，不能继承原 Alias 的 RoutingPolicy 或 PRD-036 Fallback。
- direct internal model 继续走单候选直连，不读取 Alias RoutingPolicy。
- PRD-047 是调用 Provider 前的初始候选排序；PRD-036 是一次非流式上游调用失败后的第二次尝试。两者必须严格分离。
- 动态模式初始选中 Alias default 时，只有同一冻结事务中 fallback candidate 的状态为 `standby` 且已通过该动态模式 Health/KeyPool 资格，才可附加现有 enabled PRD-036 runtime fallback；rejected candidate 绝不附加。FallbackPolicy ID/version/conditions 与两条 Route 在同一事务构造，不能在选路提交后另开连接重读。动态模式初始选中 Alias fallback 时，本次只是“初始路由选择”，`fallback_triggered=false`，第一次 `routing_attempt` 仍记为 `primary`，并且不得反向重试 default。
- Budget Guard 在 Provider 前仍按主备最坏成本之和准入，但不得因备用成功写入一条 Usage 就把主失败的未知账单整笔结清。每次 Guard 返回内存中的主/备分项估算；原始总预留保持不可变，`outstanding_cost_cny` 只覆盖终态后仍未知的分项：主成功为 0，主上游失败且备用成功为主估算，双上游失败为主备之和，本地 Key/闸门失败且未调用 Provider 为 0。Usage 与覆盖额必须在同一事务提交；写账失败则保留原总预留 Fail Closed。
- `fixed` 继续沿用现有流式单候选、非流式受控 Fallback、Provider 新鲜 unhealthy 摘流和错误优先级；迁移上线不能自动改变存量流量。
- 能力校验仍发生在初始路由选定后。PRD-047 不根据请求能力重新排序，也不静默改变 PRD-015/036/045 已验收的兼容顺序。

### 2.5 明确非目标

- 不做质量评分、用户反馈闭环、质量-成本路由、A/B、灰度、缓存感知或自动学习权重。
- 不新增 Project/User/Department override，不新增任意数量候选的管理 API。
- 成本比较使用带版本的冻结汇率事实归一到 CNY；这只影响排序，不改写 USD/CNY 原币计费金额。未知币种或缺少汇率事实时才回退固定顺序。
- 不把 OpenRouter、硅基流动作为默认模型厂家；它们仍只是对标与迁移兼容对象。
- 不实现 PRD-057 负责的持久崩溃恢复、dead-letter 或分布式 Webhook 队列。复用现有有界异步运行时队列；事件入队失败可追踪但不得阻塞模型热路径。
- 不在本条重排 Policy、路由、DLP、限流、预算和 Provider 调用的既有流水线。
- 不实现硬件盒子、MCP 主链路、信创或复杂数据出境能力。

## 3. 架构与一致性设计

### 3.1 请求时序

```text
认证/Environment -> 请求模型权限预检 -> 已发布 Policy DSL
  -> route_to ? 单目标路由 : Alias/Direct 路由
  -> Alias 时冻结 RoutingPolicy + 两候选，并按 mode 读取最小 Health/KeyPool/Price 事实
  -> shared 两阶段纯资格/排序器 -> 原子写 RoutingDecision + RoutingCandidate facts
  -> handler 提交 route.policy_selected 到有界异步队列
  -> 既有能力校验 -> DLP -> 限流 -> 预算 -> Provider Key 实际选择 -> Provider
  -> 非流式且满足条件时执行 PRD-036 第二次尝试
  -> RequestLog / Usage / PriceSnapshot 按实际最终模型归因
```

### 3.2 一致性快照与事务

Gateway 对 Alias 路由只捕获一次 UTC `as_of`，在一个短事务内先读取 Alias、RoutingPolicy、default/fallback Model、Provider 与健康事实，再按冻结 mode 使用最小依赖矩阵：

| mode/分支 | Provider Health | KeyPool | PricePlan/目录价 | token estimate |
| --- | --- | --- | --- | --- |
| `fixed` | 必需 | 不读取 | 不读取 | 不计算 |
| `availability_first` | 必需 | 必需 | 不读取 | 不计算 |
| `cost_first`，0/1 条健康+Key 合格候选 | 必需 | 必需 | 不读取 | 不计算 |
| `cost_first`，恰有 2 条健康+Key 合格候选 | 必需 | 必需 | 同一 `as_of` 必需 | 此时才计算 |

shared 算法拆为无副作用的两阶段：第一阶段只做候选资格与健康/Key 排序需求判断；只有返回 `needs_cost_comparison=true` 时，Gateway 才调用有界 token 估算器、读取两条价格并执行第二阶段成本排序。未被当前分支使用的 candidate 字段固定为 `not_evaluated/NULL`，decision 的 `price_as_of/estimated_*_tokens/token_estimate_truncated` 也为 NULL。事务内不解密 Key、不访问网络、不等待 Webhook，也不写 `last_used_at`。

排序完成后，在同一短事务中写一条 `routing_decisions` 与完整候选集合，再提交后返回路由。历史 PRD-047 文本曾以两候选为上限；当前实现由 0173/0181 扩展为 1 个默认模型和最多 31 个备用模型，共 32 条候选，SQLite 本地部署仍使用短 `BEGIN IMMEDIATE` 保证读写事实一致。动态模式的决策事实写入失败时返回 `gateway.routing_decision_unavailable`，不得发生不可解释的动态调用；`fixed` 保留既有 best-effort 决策记录兼容语义。

当前 mode 实际必需的读取或锁竞争导致无法形成最小快照时统一为可重试 `gateway.routing_snapshot_unavailable`；当前分支实际读取到的价格 Decimal/币种、健康时间/枚举、Policy 版本/模式或租户关系损坏时统一为不可重试 `gateway.routing_snapshot_invalid`（Policy 行自身损坏可进一步使用更具体的 `gateway.routing_policy_invalid`）。不读取的依赖不得影响该模式：价格表损坏不能阻断 fixed/availability，KeyPool 故障不能阻断 fixed；cost 只有需要两候选比较时才受价格故障影响。只有“已进入价格比较且价格字段合法，但没有已生效计划且目录无版本”才是正常 `price_missing`；解析失败绝不能伪装成缺价后 fixed 放行。上述错误都必须由外层写失败 request log 并保留 request/trace ID，禁止裸 500。

该矩阵只约束 PRD-047 初始 Router 阶段；路由之后的既有 Budget/Usage 可能为自身职责读取价格。测试必须分别证明“未读取的坏依赖不影响路由选择”与完整请求继续遵守既有 Guard 错误语义，不能把下游独立失败误报成 Router 失败或反过来。

控制面四类写入口（Alias create、通用 Alias PATCH、Fallback PATCH、routing-mode PATCH）全部以 `BEGIN IMMEDIATE` 开始，在锁内按认证 tenant 加载 Alias/Policy、先做 CAS，再判断 no-op/校验目标、写业务对象、audit 与持久配置事件，最后一次提交。版本一致性由可执行的 SQLite trigger 而不是“服务约定 + 无法延迟的跨表断言”保证：

- `AFTER INSERT ON model_aliases` 为新 Alias 自动插入 `fixed v1`，ID、tenant、actor 与时间只来自 NEW 行；服务不得再插第二份 Policy。迁移回填发生在安装 trigger 之前。
- 同字段的 `BEFORE UPDATE` 在发生真实变化但找不到同 tenant Policy 时立即 RAISE，阻止损坏库继续改图；随后 `AFTER UPDATE` 把唯一 Policy 的 `version=version+1`、`updated_at=NEW.updated_at`。同一 SQL UPDATE 无论改变几个字段只触发一次；description 变化不触发。
- API 在更新 Alias 前必须已验证 expected version；UPDATE 后读取 trigger 生成的新版本，再写 `updated_by`、audit/event。`updated_by` 是 API 最后操作者的 best-effort 元数据，允许同版本单独更新；直接 SQL 绕过仍会 bump version，但没有可信 actor 时保留旧值，不能伪造用户。
- `BEFORE DELETE` Policy 在 Alias 仍存在时拒绝删除；正常 0036 库因此不会产生 implicit 行。implicit fixed 只兼容损坏/旧测试数据的只读与 Gateway legacy 路径，任何写 API 遇到缺 Policy 都失败关闭。
- routing-mode 直接更新 Policy 时必须令 version 精确 +1；Policy trigger 禁止 mode/status 变化不增版本、版本倒退或跳号。

Webhook 网络 I/O 只能在提交之后执行，外部失败不能回滚已生效策略。PRD-057 仍负责自动扫表恢复和 dead-letter；本条通过不可变事件事实与同 request_id 重试恢复关闭“提交后无事实”的窗口。

### 3.3 控制面故障边界

PRD-045 的不可变 Policy snapshot 不包含 ModelAlias/RoutingPolicy。本条 Gateway 继续读取共享业务数据库，未新增 RoutingPolicy 的独立 LKG/Redis 快照。此项是已知架构差距，不能在 README 宣称已覆盖“控制面故障下的全部路由配置 LKG”；后续配置分发 PRD 再处理。数据库读取失败必须使用统一错误码，不得猜测动态策略。

## 4. 涉及模块

### 4.1 Shared SDK

- 新增 `metahub_shared.runtime.routing_policy`：定义不可变 RoutingPolicy/RoutingCandidate 输入、KeyPool 可用性枚举、价格事实、两阶段纯资格/排序结果和稳定原因码；第一阶段显式返回是否需要成本事实。
- 提供 `estimate_request_tokens` 或等价纯函数：只在两条 cost eligible 候选需要比较时读取内存中内容长度和 `max_tokens/max_completion_tokens`，不返回、记录或散列正文。
- `errors.py`、`error_diagnosis.py`、i18n 错误目录登记 PRD-047 统一错误。
- `webhooks.py` 增加 `route.policy_selected` 白名单与精确 payload 校验，事件必须使用稳定持久 event ID。
- 纯排序器不接收数据库连接、环境变量、日志对象或 HTTP 客户端，Gateway 和 PRD-046 Dry-Run 必须复用同一算法与测试向量。

### 4.2 Control Plane API

- `model_aliases/models.py`：增加 RoutingPolicy、当前候选摘要、严格 PATCH 请求/响应 DTO；ModelAlias 查询响应携带当前策略。
- `model_aliases/routes.py`：新增 admin-only `PATCH /model-aliases/{model_alias_id}/routing-mode`；使用认证依赖后的有界严格 JSON reader，并让所有 Alias 写路由把 `AuditRequestMeta` 传给服务层。
- `model_aliases/service.py`：Alias INSERT 后由数据库 trigger 自动产生 `fixed v1`；四类写入口统一锁内读取/CAS/no-op/审计/持久事件，更新 Alias 路由字段时不复制候选配置；实现租户限定模式更新、同 request_id 恢复、配置 Webhook 和安全日志。
- `audit/service.py`：登记 `routing_policy.created/updated -> routing_policy`，增加精确安全摘要字段，不允许整个 Alias/Policy 行自动序列化。
- `providers/service.py`：Provider 创建/更新运行日志只保留 code/type/auth 等非敏感分类，不记录 Endpoint 或 URL hash；持久审计只保存规范化完整 URL 的 `base_url_hash`，避免 host/path 中的内网拓扑或 Token 扩散。
- `policies/dry_run_service.py`：读取 Alias RoutingPolicy、价格/健康/托管 Key 安全快照，调用 shared 排序器；不得继续用固定顺序伪装动态模式。
- `request_logs/models.py/service.py`：返回 policy ID/version/mode、price as_of、token 估算和类型化 RoutingCandidate 事实。
- `console/service.py`：新增 admin/developer 可见的 `routing-policies` 导航；viewer 不可见。

### 4.3 Gateway Data Plane

- `routing/models.py`：在同一冻结事务读取 RoutingPolicy、候选与 PRD-036 FallbackPolicy，调用 shared 排序器、原子写决策和候选、只把 eligible standby 附加为运行时 fallback，并为 Route/Plan 携带 policy 与稳定 event ID。
- `providers/keypool.py`：新增无解密、无写库、可传入现有连接与 `as_of` 的 KeyPool 快照函数；实际 `select_provider_key` 保持 TOCTOU 复验和统一错误处理。进程级环境 Key 只能在非生产环境服务 `AppConfig.default_tenant_slug` 对应的 active tenant，且必须同时匹配 active Provider、`provider_code` 和运维基准 `config.provider_base_urls[provider_code]`；实际调用还要比对已冻结 Route 的 Endpoint。Key 只从已加载、不可序列化的 `config.provider_api_keys` 读取；生产环境或其他租户即使复制相同 code/URL 也只能使用自己的托管 Key，防止全局 Secret 被发往未授权 Endpoint。
- `api/openai_compatible.py`：把有界、延迟求值的请求形状提供给路由包装器，只有 shared 第一阶段要求成本比较时才生成长度型 token 估算；成功获得 Alias 初始路由后构造并 `put_nowait` 入队 `route.policy_selected`；fixed/availability/direct/route_to 不计算 token，其中 direct/route_to 不发该事件。
- `budgets/service.py`、`usage/ledger.py` 与 shared `budget_reservations.py`：统一价格上限/精度、CNY Budget 币种和分项预留结算；任何非 CNY 价格在存在 active CNY 预算时复用 `gateway.budget_unavailable`，无预算规则则不新增价格依赖，Usage 仍保留合法 USD 原币事实。
- `events/dispatcher.py`：把当前硬编码的 `route.fallback_*` 失败日志泛化为运行时事件通用安全日志；队列满或 worker 不可用不反压热路径。
- Usage、request log、routing attempt 继续按实际调用模型归因；动态选中 fallback 不得写 `fallback_triggered=true`。

### 4.4 Console Web

- 新增 `features/routing-policies/renderRoutingPolicyPanel.js`，复用同一 ModelAlias state，不创建独立候选配置状态。
- `lib/modelAliases.js` 增加 routing-mode PATCH client，必须发送 Bearer 与新 `X-Request-ID`，请求体不得含 `tenant_id`。
- `app/main.js` 增加页面分发、admin 保存动作、developer 只读门禁、401/403/版本冲突处理，以及绑定身份/导航/版本/刷新代次的草稿与 operation ticket；离页、登出、身份变化和迟到响应均安全清理。
- PRD-046 Dry-Run 页面展示本次 mode、policy version、price as_of、候选 rank/资格/安全价格/健康/KeyPool 状态和 selection reason；Prompt 生命周期继续不进入应用状态。
- 调用日志详情展示冻结策略与类型化候选，不再要求用户解析原始 `candidate_summary` JSON。

### 4.5 数据库、测试和文档

- 新增 `infra/migrations/0036_cost_availability_routing.sql` 及 `.down.sql`，包含 audit_logs 保真重建、父级复合唯一索引、Policy/Event/Candidate、Decision 重建、Alias 自动版本 trigger 和 Webhook 回滚清理。
- 新增 shared/Gateway/Control/Console 单元与集成测试、迁移往返测试、运行验收脚本与浏览器验收脚本。
- 更新 README、目录说明、API 文档、`docs/testing/PRD-047-tests.md`；最终批次、数据库 hash、日志扫描与浏览器证据只能在实际通过后回写。

## 5. 数据表和字段设计

### 5.1 `model_alias_routing_policies`（OBJ-066 RoutingPolicy）

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `routing_policy_id` | TEXT PK，`rpol_` + 32 hex | 稳定 RoutingPolicy ID。 |
| `tenant_id` | TEXT NOT NULL | 认证 tenant；所有查询、唯一键和复合关联的第一列。 |
| `model_alias_id` | TEXT NOT NULL | 当前 tenant 的 ModelAlias；一对一且不可换绑。 |
| `routing_mode` | TEXT CHECK 三枚举 | `fixed/cost_first/availability_first`。 |
| `status` | TEXT CHECK=`enabled` | 本期不提供独立 disable；Alias 自身 status 决定是否可路由。字段保留对象状态合同。 |
| `version` | INTEGER CHECK>=1 | 模式或 Alias 路由相关字段每次真实变化严格 +1；重复同值 PATCH 不增版本。 |
| `created_by` | TEXT NULL | 存量迁移可空；新建来自认证用户。 |
| `updated_by` | TEXT NULL | 最近真实变更用户。 |
| `created_at` | TEXT NOT NULL | UTC 秒级。 |
| `updated_at` | TEXT NOT NULL | UTC 秒级。 |

约束：`UNIQUE(tenant_id, model_alias_id)` 与 `UNIQUE(tenant_id, routing_policy_id)`；复合 FK 绑定 Alias、creator/updater；禁止修改 ID/tenant/Alias，禁止删除仍绑定 Alias 的 Policy。mode/status 变化必须 `version=OLD.version+1`，版本禁止倒退/跳号。Alias 的 AFTER INSERT/UPDATE trigger 按 3.2 的精确机制创建或推进版本，API 不重复 bump；`updated_by` 的同版本元数据更新不改变执行语义。

Excel 对象模板中的 `priority/condition_expr/action` 不机械落库：本条没有条件 DSL，候选手工优先级已由 Alias default/fallback 角色表达，动作由强枚举 `routing_mode` 表达。伪造 JSON 字段会重复保存同一含义并扩大不受控 DSL 面，因此按“字段需结合具体 PRD 细化”的说明收敛为强类型字段。

### 5.2 `routing_candidates`（OBJ-067 RoutingCandidate）

该表保存请求时不可变评估事实，不是候选配置表。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `routing_candidate_id` | TEXT PK，`rcand_` + 32 hex | 本次候选事实 ID。 |
| `tenant_id` | TEXT NOT NULL | 继承认证 tenant。 |
| `routing_decision_id` | TEXT NOT NULL | 同租户路由决策；每个决策最多两个候选。 |
| `routing_policy_id` | TEXT NULL | 正常迁移后非空；缺行兼容 fixed 时可空。 |
| `model_alias_id` | TEXT NOT NULL | 本次公开 Alias。 |
| `model_id` | TEXT NOT NULL | Alias 配置的 default/fallback Model ID。 |
| `provider_id` | TEXT NULL | 图损坏时可空；不能保存 base_url/auth。 |
| `candidate_role` | TEXT CHECK | `default/fallback`。 |
| `configured_priority` | INTEGER CHECK 0/1 | default=0，fallback=1；稳定 tie-break。 |
| `rank` | INTEGER NULL CHECK 1/2 | 排序后名次；硬拒绝候选为空。 |
| `status` | TEXT CHECK | `selected/standby/rejected`。 |
| `reason_code` | TEXT NOT NULL | 稳定选择、等待或剔除原因。 |
| `health_state` | TEXT CHECK | `healthy/unhealthy/unknown`，是有界归类而非原始探测正文。 |
| `health_fresh` | INTEGER CHECK 0/1 | 是否位于同一 `as_of` 新鲜窗口。 |
| `keypool_state` | TEXT CHECK | `available/unavailable/unknown/not_evaluated`。不含 Key ID、前缀或数量细节。 |
| `price_available` | INTEGER CHECK 0/1 | 是否存在可比较的显式价格。 |
| `price_source` | TEXT CHECK | `workspace_provider_cost_rate/price_plan/model_catalog/missing/not_evaluated`。 |
| `price_schema_version` | TEXT NULL | 安全版本标识；无明确定价时为空。 |
| `currency` | TEXT NULL CHECK | `CNY/USD` 或空。 |
| `estimated_cost_amount` | TEXT NULL | 与排序完全相同的 15 位小数 Decimal 字符串；只用于解释，不替代 Usage 实际成本。 |
| `created_at` | TEXT NOT NULL | 与决策同一 UTC 时间。 |
| `updated_at` | TEXT NOT NULL | 创建后不变，保留对象字段合同。 |

约束：

- 当前候选事实使用 `(tenant_id, routing_decision_id, model_id)` 唯一键；CHECK 强制 default 使用 priority 0、fallback 使用 priority 1..31；partial unique rank 保证同一决策的 rank 1..32 不重复。历史版本的 candidate_role 唯一键和 rank 1/2 约束由 0173/0181 扩展迁移替换。
- CHECK 固定 `selected -> rank=1`、`standby -> rank=2`、`rejected -> rank IS NULL`；失败 decision 只能关联 rejected，selected candidate 的 model/provider 必须等于 decision 最终选择。
- 价格字段成组一致：`not_evaluated/missing` 时版本、币种、成本均为空；`price_available=1` 时 source 可为 workspace_provider_cost_rate、price_plan 或 model_catalog，且版本、币种、15 位成本均非空。health/key/status/reason 均为稳定白名单。
- 复合 FK 对 decision/policy/Alias/Model/Provider 做 tenant 绑定；insert trigger 进一步验证 candidate.policy/alias 与 decision 完全相同、Model 实际 provider 与 candidate provider 一致，并按 role 核对插入时 Alias 的 default/fallback_model_id；fallback 为 NULL 时禁止 fallback 行。该核对发生在同一冻结事务，后续 Alias 合法升级不回写不可变历史。
- UPDATE/DELETE trigger 禁止修改历史候选。请求日志按 `(tenant_id, routing_decision_id, rank, configured_priority)` 查询。

### 5.3 `routing_policy_events`（配置事件事实）

该表只保存低频 `configuration_changed` 事实；运行时 `request_selected` event ID 已由对应 `routing_decisions` 持久化，不重复写表。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `routing_policy_event_id` | TEXT PK，`rpe_` + 32 hex | Webhook 直接复用的稳定 event ID。 |
| `tenant_id` | TEXT NOT NULL | 认证 tenant。 |
| `routing_policy_id` | TEXT NOT NULL | 同 tenant Policy。 |
| `model_alias_id` | TEXT NOT NULL | 同 tenant Alias。 |
| `routing_policy_version` | INTEGER >=1 | 提交后的配置版本。 |
| `event_type` | TEXT CHECK | 固定 `route.policy_selected`。 |
| `trigger_type` | TEXT CHECK | 固定 `configuration_changed`。 |
| `actor_user_id` | TEXT NOT NULL | API 认证 admin；直 SQL trigger 不伪造事件。 |
| `request_id` / `trace_id` | TEXT NOT NULL | 配置请求链路。 |
| `request_fingerprint` | TEXT，64 lower hex | 绑定 path、expected version、规范化安全更新及必要的 description hash，不含描述明文或敏感正文。 |
| `payload_json` | JSON object，<=8 KiB | §8.1 精确安全 payload，提交后只从该行重建。 |
| `created_at` | TEXT NOT NULL | UTC 秒级。 |

约束：`UNIQUE(tenant_id, routing_policy_id, routing_policy_version, trigger_type)`、`UNIQUE(tenant_id, actor_user_id, request_id)`；复合 FK 绑定 policy/Alias/user，insert trigger 核对 payload 中 ID/version/trigger 与列值，UPDATE/DELETE 禁止。Alias create 写 `routing_policy.created` audit 与 v1 配置事件；真实模式/路由图变化写 `routing_policy.updated` 与新版本事件；description、Fallback conditions、同值 no-op 不写事件。每个可能产生配置事件的入口在取得写锁后、检查 Alias 重名/存在、CAS 或生成新 ID之前，先按 tenant+actor+request 查询恢复事实；相同 fingerprint 加载既有 event/audit/Policy 核对后重派同 event ID并返回原资源，因而 Alias create 响应丢失也不会先撞 duplicate。不同 fingerprint 返回 409，普通 CAS conflict 不新建事实。

### 5.4 扩展 `routing_decisions`

新增：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `routing_policy_id` | TEXT NULL | direct、route_to 和历史记录为空。 |
| `routing_policy_version` | INTEGER NULL | 本请求冻结版本。 |
| `routing_mode` | TEXT NULL | 本请求冻结模式。 |
| `price_as_of` | TEXT NULL | 只有两条 cost eligible 候选实际比较价格时保存；其他分支为空。 |
| `estimated_input_tokens` | INTEGER NULL | 只有实际成本比较时保存长度估算；不保存输入。 |
| `estimated_output_tokens` | INTEGER NULL | 只有实际成本比较时保存有界输出估算。 |
| `token_estimate_truncated` | INTEGER NULL | 只有实际估算时保存是否触发 fail-safe 饱和；不保存正文。 |
| `routing_policy_event_id` | TEXT NULL UNIQUE | 已提交路由事实对应的稳定 Webhook event ID。 |

新增 CHECK 要求 policy ID/version/mode 三者全空或全非空；implicit fixed/direct/route_to/历史为全空，正常 Alias Policy 为全非空。`routing_policy_event_id` 只在全非空且 selected 时存在。0035 历史库允许同一 `(tenant_id, request_id)` 留下多条全 NULL Policy Decision，0036 必须逐行保真，不能删除或改写 request_id；迁移后使用 `WHERE routing_policy_id IS NOT NULL` 的部分唯一索引约束 PRD-047 Policy-bound 决策一请求一条，并保留 `UNIQUE(tenant_id, routing_decision_id)`。调用日志优先选择 Policy-bound 权威事实；纯历史重复按 `created_at DESC, routing_decision_id DESC` 确定性选择最新一条。`candidate_summary` 继续保存最多两项的兼容安全 JSON，但新请求日志 UI 以 `routing_candidates` 强类型行作为权威解释。

### 5.5 迁移与回滚

Up migration `0036` 全部位于 `PRAGMA foreign_keys=OFF + BEGIN IMMEDIATE` 的原子事务。除了新表自己的复合 FK，迁移必须安装下列可执行 owner trigger；不能只在服务层按 tenant 查询，因为导入脚本、旧测试夹具和直接 SQL 同样不得写出跨租户关系：

| 被保护对象 | INSERT/UPDATE 必须检查的归属与图一致性 | 实现约束 |
| --- | --- | --- |
| `models` | `provider_id` 必须命中 `(NEW.tenant_id, NEW.provider_id)` 的 Provider。 | `BEFORE INSERT` 与 `BEFORE UPDATE OF tenant_id, provider_id` 分别安装 trigger；不存在或跨租户统一 `RAISE(ABORT, 'models.provider_owner_invalid')`。 |
| `model_aliases` | `environment_id` 必须非空并命中同 tenant Environment，兼容列 `environment` 必须等于该 Environment 的 code；default Model 必须同 tenant，fallback 为空或同 tenant；fallback 非空时必须与 default 不同。 | `BEFORE INSERT` 与 `BEFORE UPDATE OF tenant_id, environment_id, environment, default_model_id, fallback_model_id` 分别安装 owner trigger，并先执行 owner 校验，再由既定 AFTER trigger 创建/升级 Policy。 |
| `routing_decisions` | 非空 `virtual_key_id`、`environment_id`、`model_alias_id`、`selected_model_id`、`provider_id`、`routing_policy_id` 均必须命中同 tenant 父对象；非空 Environment 的 code 必须与 `environment` 相同；Alias 的 `environment_id` 必须与 Decision 一致。Virtual Key 非空 ID 正常要求精确一致；PRD-030 遗留 `vk.environment_id=NULL` 只在同 tenant Key code、Decision code 和已验证 Environment ID 三者对应时按现有 Gateway 鉴权规则兼容。非空 selected Model 的 `provider_id` 必须等于 Decision Provider。Policy 非空时还必须属于该 Alias，且持久化 version/mode 等于本请求冻结值。 | `BEFORE INSERT` 与覆盖全部关系列的 `BEFORE UPDATE OF ...` owner/graph trigger 成对安装。失败 Decision 允许 selected Model/Provider/Policy 为空；正常 Alias 动态决策必须由 all-null/all-present CHECK 与 trigger 形成完整图。 |

精确复合 FK 父键集合为：0036 新建 `uq_environments_tenant_environment_prd047`、`uq_model_aliases_tenant_id_prd047`、`uq_providers_tenant_id_prd047`，复用 0034 的 `uq_virtual_keys_tenant_key_prd045` 和 0033 的 `uq_models_tenant_model_prd044`；RoutingPolicy 自身提供 `UNIQUE(tenant_id, routing_policy_id)`。这些索引既服务新表复合 FK，也让上述 owner trigger 使用稳定索引查找。Candidate 还必须由 CHECK 固定 `default -> priority 0`、`fallback -> priority 1`，并由 insert trigger 按 `candidate_role` 核对同一冻结事务中 Alias 当时的 `default_model_id/fallback_model_id`；不能仅证明 Model 同 tenant 就接受伪造角色。

1. 创建上述 Environment/Alias/Provider 父级复合唯一索引；复用 User/Model/VirtualKey 既有复合唯一索引。对存量 Model->Provider、Alias->Environment/default/fallback 和 Decision owner 图做 guard；Decision 的 legacy NULL-Key Environment 解析必须镜像现有 Gateway 鉴权，其他错配仍失败关闭。
2. 创建 RoutingPolicy，回填全部存量 Alias 为 `fixed v1`，时间取 Alias、actor 取同 tenant created_by；通过计数/悬空 guard 后安装 Model/Alias owner trigger、Alias AFTER INSERT/UPDATE 和 Policy identity/version/delete trigger。
3. 以 0035 全字段重建 `routing_decisions`，增加 PRD-047 列、all-null/all-present CHECK，逐字段复制历史并做计数 guard；重命名后创建 `uq_routing_decisions_tenant_id_prd047`、仅约束 `routing_policy_id IS NOT NULL` 的 request 部分唯一索引、policy owner insert/update trigger，并恢复 0035 三个既有索引和环境索引。历史重复 request 的公共列、行数和 request_id 必须原样保留。
4. 创建不可变 `routing_policy_events` 和 `routing_candidates`，安装上述复合 FK、内部一致性 CHECK/trigger 与查询索引。
5. 以当前 0035 的 17 列重建 `audit_logs`，扩展 `routing_policy.created/updated` 与 object `routing_policy`。历史行数、主键、tenant/actor、action/object、request/trace 和时间等链路字段必须逐值保留；唯一安全例外是对 `object_type='provider'` 的 `before_summary_json/after_summary_json` 删除旧 `base_url` 明文字段。迁移不伪造 URL hash；后续新 Provider 审计由服务层只写 `base_url_hash`。复制计数必须相等，并精确恢复五个普通索引、`idx_audit_logs_dry_run_request_unique`、owner/immutable 三 trigger。
6. 运行 `foreign_key_check`、`integrity_check`、重复策略/request、候选/事件 owner、跨租户关系和非 fixed 存量 guard；任何非零结果阻止提交。

Down migration 同样在单一显式事务内按依赖顺序执行：

1. 先删除 `event_type='route.policy_selected'` 的全部 `webhook_delivery_attempts`，再删 original/replay `webhook_deliveries`；使用 `json_each/json_group_array` 从每个 Endpoint 的 `event_types_json` 精确移除该值，保留其他类型原顺序并把空集写成 `[]`。
2. 重建 0035 `audit_logs`：只过滤 `routing_policy.created/updated`，其余行的链路字段和已安全化摘要逐值保留，恢复五个普通索引、Dry-Run partial unique 与三个 trigger；不可直接 DELETE 绕过 immutable trigger。Up 已删除的历史 Provider `base_url` 属于不可逆安全清理，down 不得恢复或伪造原 URL。
3. 显式删除 Model/Alias/RoutingDecision owner/graph trigger 以及 Alias/Policy/Candidate/Event 的全部 PRD-047 trigger，再删除 candidate/event facts；按 0035 字段集合重建 routing_decisions 并逐字段复制所有历史基础列，恢复普通索引。
4. 删除 RoutingPolicy，再删除仅由 0036 创建且仍存在的 Environment/Alias/Provider 父唯一索引；Decision 的 0036 unique 已随第 3 步旧表重建删除，0033/0034 的 User/Model/VirtualKey 索引保留。
5. 用 schema/计数/FK/integrity guard 验证 0035 `sqlite_master`、非 PRD-047 Webhook/Decision 历史逐字段保真，并对 Audit 单独验证行与链路字段保真、Provider `base_url` 唯一例外已清理且不可恢复。

回滚后 Alias 主备和 PRD-036 Fallback 不变；PRD-047 动态候选/事件扩展事实按明确破坏性边界删除。必须通过 `0035 -> 0036 -> 0035 -> 0036`、订阅数组保序/空数组、original+replay 清理、Provider 历史 URL 安全清理与 down 不恢复、其他事件不受影响、外键 0 违规和 `integrity_check=ok`。

### 5.6 `budget_reservations` 未结算分项扩展（0036 同批）

尚未封账发布的 `0036` 同批在既有预留表增加 nullable `outstanding_cost_cny NUMERIC CHECK >= 0`。`reserved_cost_cny` 继续保存 Provider 前主备最坏总额且不得因终态改写；新列为空表示沿用旧合同：没有 Usage 时总额 outstanding，有 Usage 时由 Usage 替代。新列非空表示运行状态机已确认未结算分项，无论同 request 是否已有备用 Usage，都只把该覆盖额加入 CNY spent。这样既兼容全部历史行，也能保留“主失败、备成功”的未知主账单。

更新覆盖额必须带 `tenant_id + request_id`，并与 Usage insert、成功 attempt 关联及 request log 成本更新同一事务提交。失败请求在外层根据受控 `UpstreamApiError` 与实际尝试序列精确写 0/主/主备金额；更新失败保留原总预留并记录不含价格原值、SQL 或路径的固定告警。Down migration只删除新增 nullable 列，不删除或重估历史预留/Usage。

## 6. 路由算法合同

### 6.1 公共输入、token 估算与成本精度

排序器输入最多两项，调用方已按 tenant 加载并归一化。下列估算规则只在 `cost_first` 第一阶段确认恰有两条 eligible 候选后执行；fixed、availability、cost 单候选、direct 与 route_to 均不得计算或持久化 token。Gateway 与 Dry-Run 共用一个有界纯估算器，不得分别复制规则：

- Chat 的 `content` 为字符串时统计 Unicode code point；为 content-parts 数组时，只统计字符串项和对象中字符串 `text` 字段，图片 URL、工具参数、null、bool、float 与其他对象贡献 0。
- Embeddings 的 `input` 为字符串或嵌套字符串数组时统计 code point；true integer（明确排除 bool）token ID 每项直接计 1；其他类型贡献 0。字符串估算为 `ceil(codepoints/4)`，再加直接 token ID 数，最终最少为 1。
- 遍历最多 100,000 个容器/标量节点，输入估算最多 1,000,000 token；超过任一上限时以 1,000,000 fail-safe 饱和并令 `token_estimate_truncated=true`，不把正文或截断片段写入任何对象。
- 输出优先使用 `type(value) is int` 且为正数的 `max_completion_tokens`，其次 `max_tokens`；bool、零、负数、字符串、float 或缺省均计 0。大于 131,072 时饱和为 131,072 并标记 truncated；Embeddings 输出固定 0。
- 估算器只消费已经通过现有结构校验的内存 payload，不新增 Prompt 持久化，也不替代 Adapter 对上游参数的正式校验。

适配后统一输入是不可变 `RoutingTokenShape(char_codepoints, direct_token_count, requested_output_tokens, truncated)`。Gateway 仅在 cost 第二阶段延迟把 `messages/input` 适配为该形状；PRD-046 Dry-Run 把 `prompt` 精确适配为一条 Chat 文本 content，把 `max_output_tokens` 适配为 `max_completion_tokens` 语义，不构造历史流量或额外消息。Dry-Run 在单一 `as_of` 下只计算一份 `TokenEstimate`，路由成本和预算成本共同引用；Gateway 若路由阶段已计算，则把同一 estimate 传给后续 Budget Guard，否则 Budget 使用相同 shared adapter 计算，禁止保留两套字符规则。

成本公式使用 Decimal：

```text
estimated_cost =
  ((estimated_input_tokens - estimated_cache_tokens) * input_price_per_1k
   + estimated_cache_tokens * cache_price_per_1k
   + estimated_output_tokens * output_price_per_1k) / 1000000
```

现有价格服务允许每百万 Token 单价最多 12 位小数，除以 1000000 后最多需要 18 位小数；路由比较统一量化到 15 位小数。计算必须在独立 `decimal.localcontext(prec=50)` 中完成并拒绝非有限值，避免默认 28 位上下文在最大 token/单价附近提前舍入。本条固定 `COST_QUANT=Decimal("0.000000000000001")` 与 `ROUND_HALF_UP`：先按公式计算，再量化为 15 位小数；排序、平局判定、`routing_candidates.estimated_cost_amount`、API 与 UI 全部使用这一个量化值。严禁按更细的内存值选中后只保存 6 位解释，也不使用 float。差值小于半个 `COST_QUANT` 时按量化结果视为同价并走稳定 tie-break。

### 6.2 通用资格过滤

所有模式继续剔除：Model 缺失/disabled、Provider 缺失/disabled、具有新鲜 unhealthy 证据的 Provider。unknown、无时间戳或 stale 健康证据按现有 Fail Open 合同保留。

“无时间戳”是合法未知状态；非空但无法解析的健康时间、越界价格、未知币种/状态或跨 tenant 关联属于快照损坏，必须按 `gateway.routing_snapshot_invalid` 失败关闭，不能当 unknown、missing 或 disabled 猜测。

仅 `cost_first/availability_first` 把 KeyPool 快照为 `unavailable` 的候选剔除。`fixed` 的 `keypool_state=not_evaluated`，继续让实际 Provider Key 选择阶段决定 `provider_key.unavailable`，避免迁移改变存量语义。

### 6.3 KeyPool 无副作用快照

- `auth_type=none`：`available`。
- 当前 tenant/provider 存在至少一条 `aesgcm-v1:` 托管密文时，禁止环境变量回退；至少一把同时满足 active 或冷却已过期、且没有新鲜 unhealthy probe 的密文候选才为 `available`，否则 `unavailable`。
- 不存在任何托管密文时，生产环境直接禁用进程级 Provider Key。非生产 Gateway 先用同一数据库连接确认 `tenant_id` 对应 active tenant 的 slug 精确等于 `config.default_tenant_slug`，再要求 active Provider 的 `provider_code` 与运维配置存在且数据库/冻结 Route `base_url` 均与 `config.provider_base_urls[provider_code]` 精确匹配，最后判断已加载 `config.provider_api_keys[provider_code]` 是否非空；只保存布尔状态，不记录变量名、值或 Endpoint。生产、非默认租户、非 active 对象、code 错配或未绑定基准 URL 的 Provider 永远不能读取或使用进程级 Provider Key。
- 旧明文/测试行不是可用托管 Key，也不能使 `managed_key_exists=true`。
- 快照不解密、不更新 `last_used_at`、不解除 rate limit。排序后到实际选择之间存在 TOCTOU；实际 `select_provider_key` 仍需解密、再次校验并可能返回 `provider_key.unavailable`，不得静默改选第二候选。
- Control Dry-Run 无法可信读取 Gateway 进程环境变量：有托管 Key 或 `auth_type=none` 时可给出确定状态；否则为 `unknown` 并保持 `gateway_parity=partial/coverage_complete=false`。

### 6.4 `fixed`

按 `default, fallback` 固定顺序取第一条通过既有 Model/Provider 健康过滤的候选。价格和 KeyPool 不参与排序。失败错误优先级继续沿用现有 `gateway.provider_not_found/gateway.provider_unhealthy/gateway.model_disabled`，确保存量别名和迁移前行为相同。

### 6.5 `cost_first`

1. 第一阶段先执行通用资格与动态 KeyPool 过滤，不读取价格、不计算 token。
2. 无合格候选：写失败决策和全部候选原因，返回 `gateway.no_route_available`。
3. 仅一条合格候选：直接选中，原因 `routing.single_eligible_candidate`，不要求价格。
4. 至少两条合格候选时才进入第二阶段：计算一次有界 token 估算并读取价格；两者均有价格且具备同一比较币种/版本汇率事实则按归一化成本升序，再按 `configured_priority`、`model_id` 排序。
5. 任一合格候选缺价：不得把兼容默认 0 当免费；整体回退 fixed 稳定顺序，原因 `routing.cost_fixed_fallback.price_missing`。
6. 合格候选缺少可用汇率：禁止直接比较不同币种的数字，整体回退 fixed 稳定顺序，原因 `routing.cost_fixed_fallback.currency_incomparable`。
7. 同价按 default、fallback、`model_id` 稳定打破平局，原因 `routing.cost_tie_stable_priority`。

有效价格使用同一 `as_of`：取 `effective_from <= as_of` 中最新 PricePlan；否则只有模型目录 `price_schema_version` 非空且两个价格均为合法非负 Decimal 时才可回退目录价。未来计划不生效。显式 PricePlan 零价或带版本目录零价是真实免费；无版本的历史默认零价是缺价。

### 6.6 `availability_first`

1. 执行通用资格与动态 KeyPool 过滤；新鲜 unhealthy 与 KeyPool unavailable 均被拒绝。
2. 合格候选按健康层级排序：新鲜 `healthy` 为 0；unknown、无时间或 stale 为 1。
3. 同层级按 default、fallback、`model_id` 稳定排序。当前 Key 数量、权重和额度不作为模型级评分；Key 权重仍只在实际 KeyPool 内部选择时使用。
4. 无合格候选返回 `gateway.no_route_available` 并持久化全部安全剔除原因。

### 6.7 稳定原因码

至少包括：`routing.fixed_priority`、`routing.cost_lowest`、`routing.cost_tie_stable_priority`、`routing.cost_fixed_fallback.price_missing`、`routing.cost_fixed_fallback.currency_incomparable`、`routing.availability_healthy`、`routing.availability_unknown`、`routing.single_eligible_candidate`、`routing.candidate.model_missing`、`routing.candidate.model_disabled`、`routing.candidate.provider_missing`、`routing.candidate.provider_disabled`、`routing.candidate.provider_unhealthy`、`routing.candidate.keypool_unavailable`。

原因码是稳定机器合同；不得把 SQL、URL、Key 状态明细、原始探测消息或价格正文拼入 reason。

## 7. API 设计

### 7.1 `PATCH /model-aliases/{model_alias_id}/routing-mode`

权限：后端 `require_current_admin`；developer/viewer 直接调用均返回 403。路径 ID 只在认证 tenant 内查找；不存在与跨租户统一 404，防止枚举。

该端点不声明 Pydantic Body 参数，确保 FastAPI 先执行 admin dependency，再由路由调用 8 KiB 有界流式 JSON object reader：只接受 `application/json`、拒绝超限 wire、空 body、非 UTF-8/JSON、非 object、重复 key、extra key 和控制字符。reader 返回宽类型后由 service 精确映射：结构/mode 问题为 `model_alias.routing_mode_invalid`，expected version 问题为 `model_alias.routing_policy_version_invalid`；不能让默认 RequestValidationError 抢先变成 `infra.validation_failed`，也不能在 401/403 前解析攻击者正文。

严格请求体：

```json
{
  "routing_mode": "cost_first",
  "expected_version": 1
}
```

- `routing_mode` 必填且仅三枚举。
- `expected_version` 必填，且必须为真正正整数。这是全新端点，没有需要保留的无 CAS 客户端；服务必须在事务锁内先比较版本，再判断是否为同模式 no-op，不允许缺省后静默覆盖。
- `extra=forbid`；禁止 `tenant_id/model_alias_id/candidates/condition_expr/action`。
- 同模式且版本匹配的普通重复 PATCH 返回当前对象、`changed=false`，不增版本、不写 audit/Webhook。若首次成功响应丢失，只有复用相同 request_id 且 fingerprint 完全一致的重试可按 §5.3 恢复原成功事实并重派同 event；新 request_id 携带旧 expected_version 仍返回 409，客户端随后 GET 权威对象，不能把任意冲突当成功。

成功 200：

```json
{
  "routing_policy": {
    "routing_policy_id": "rpol_...",
    "model_alias_id": "...",
    "tenant_id": "...",
    "routing_mode": "cost_first",
    "status": "enabled",
    "version": 2,
    "candidates": [],
    "warnings": [],
    "created_at": "...",
    "updated_at": "..."
  },
  "changed": true,
  "request_id": "...",
  "trace_id": "..."
}
```

`candidates` 是当前 Alias 主备的安全配置摘要，最多两项；`warnings` 可返回 `price_missing/currency_incomparable/keypool_gateway_env_unknown` 等有界代码，不返回价格原始配置、Endpoint 或 Key 元数据。响应和 Header 均继续携带统一 request/trace ID。

### 7.2 现有 ModelAlias/Fallback 写接口的版本扩展

- `PATCH /model-aliases/{id}`：当请求包含 `alias/environment/environment_id/status/default_model_id/fallback_model_id` 任一字段时，严格请求体必须同时提供正整数 `expected_routing_policy_version`；只有 description 变化时可省略。锁内版本不匹配先返回同一 409，不能继续判断字段是否同值。
- `PATCH /model-aliases/{id}/fallback`：请求体固定增加必填正整数 `expected_routing_policy_version`。只改 PRD-036 enabled/conditions 且目标不变时策略版本不增，但仍用 CAS 阻止与并发候选变更交错；目标真实变化时版本 +1。
- Console 所有 Alias 启停、目标、Fallback 保存动作必须发送当前版本，成功后刷新；现有测试、示例和 API 文档同步升级，不能保留一个绕过版本推进的旧写入口。

### 7.3 ModelAlias 查询扩展

现有 `GET /model-aliases` 的每项增加 nullable `routing_policy`，并提供派生 `routing_candidates` 摘要，admin/developer 均可读。正常 0036 数据返回持久对象与 `routing_policy_implicit=false`。手工 SQL/旧测试夹具缺行时返回 `routing_policy=null`、`effective_routing_mode="fixed"`、`routing_policy_implicit=true`；Gateway 同样使用 policy ID/version 均为空的 legacy fixed，且不生成 `route.policy_selected`。禁止合成未落库 ID 或伪造 v1。

### 7.4 调用日志详情扩展

现有日志详情 `routing_decision` 增加 policy/version/mode/as_of/token estimates/event ID，并增加强类型 `routing_candidates` 数组。查询先以 `(tenant_id, request_id)` 限定租户，优先选择 Policy 非空事实；只有历史全 NULL Policy 重复时才按时间和 Decision ID 取最新，再以 `(tenant_id, routing_decision_id)` 加载候选。跨租户返回既有 404。candidate JSON 解析失败不得把原始字符串回显到页面。

## 8. Webhook 与审计设计

### 8.1 `route.policy_selected` 两类触发

为满足 Excel“执行或状态变化时触发”，同一事件类型允许两个精确 payload 变体：

- `trigger=request_selected`：持久 RoutingPolicy 的 Alias 初始路由事实提交后触发；`event_id` 等于 `routing_policy_event_id`，`actor_id=null`，object 为 RoutingPolicy ID。Virtual Key 是凭据标识而非人类 actor，调用归属只留在 tenant-qualified request log；implicit fixed 不触发。
- `trigger=configuration_changed`：Alias 创建、模式或 Alias 路由相关字段真实变更时，在业务事务内先写 §5.3 不可变事件行；提交后只能从该行重建信封。event ID 直接使用持久 `routing_policy_event_id`，actor 为认证用户，object 为 RoutingPolicy ID。

运行时安全字段只允许：`trigger/request_id/routing_decision_id/model_alias_id/routing_policy_id/routing_policy_version/routing_mode/selected_model_id/selected_provider_id/candidate_count/eligible_candidate_count/selection_reason/project_id`。配置变更字段只允许：`trigger/request_id/model_alias_id/routing_policy_id/routing_policy_version/change_kinds/changed_routing_fields/previous_mode/routing_mode`；`change_kinds` 是按 `created/mode/candidates/alias_state` 固定顺序去重的非空数组，可表达创建或一次 Alias PATCH 同时改变状态与候选；字段数组只允许 `alias/environment/environment_id/status/default_model_id/fallback_model_id/routing_mode` 的稳定名称，不携带前后 Model ID。可空字段必须显式出现，validator 同时拒绝缺字段和额外字段。

禁止字段：Prompt、Response、messages/input、Authorization、Provider/Virtual Key 明文、Key ID/前缀/数量、Endpoint/base_url、价格正文、condition、错误原文、Secret、Header。

### 8.2 投递边界

- 配置变更是低频控制面动作，event/audit 与配置同事务提交后复用现有同步 dispatcher；投递失败只写安全告警/已有 delivery 事实，不回滚配置。若在 commit 后、dispatch 前退出，不可变 event 仍可查询；同 request_id+fingerprint 重试必须重派同 event ID，自动后台恢复与 DLQ 留给 PRD-057。
- 请求选路是数据面热路径，handler 只调用现有有界队列的 `put_nowait`。worker 用线程执行 shared delivery core，HTTP/重试不能阻塞 ASGI 请求。
- 队列满、worker 未就绪或退出排空超时只记录 event ID、tenant、trace 和固定原因；不记录 payload。当前 best-effort 丢失边界明确留给 PRD-057。
- Endpoint 订阅白名单加入新事件；HMAC、幂等 delivery `(tenant, endpoint, event_id)` 和 Secret version 继续复用 PRD-025/042 合同。

### 8.3 管理审计

Alias 创建写 `routing_policy.created`，模式或 Alias 路由字段变更写 `routing_policy.updated`，object_type 均为 `routing_policy`；before/after 只含 `model_alias_id/alias/environment/status/default_model_id/fallback_model_id/routing_mode/version` 的安全快照，created 的 before 固定空对象，changed_fields 精确、tenant/actor/request/trace 完整。RoutingPolicy audit 与 Alias 业务事务同一提交，任何失败整体回滚。真实请求不写管理 audit，而由 routing decision/candidates/request log/Usage/Webhook 串联。

## 9. 前端页面和交互设计

### 9.1 PAGE-067 路由策略页

- 新增配置组导航 `routing-policies`，顺序紧邻 ModelAlias；admin/developer 可见，viewer 后端导航中不存在，前端不预取、不渲染 DOM，手写 hash 回退到首个可见页面。
- 页面使用紧凑表格/响应式列表，展示 Alias、Environment、default/fallback 安全摘要、当前模式、策略版本、更新时间和操作。
- admin 通过三段单选控制选择“固定 / 成本优先 / 可用性优先”，再显式点击保存；不能在切换控件时立即改线上流量。
- 草稿 Map 绑定 `tenant_id + user_id + role + session_revision + navigation_id + model_alias_id + base_policy_version`。每行分别维护只用于淘汰旧 GET 的 `refresh_generation`，以及只在保存、明确取消或身份/页面失效时变化的 `operation_generation`；两种代次不得互相推进。15 秒自动刷新对 clean 行采用服务器权威值；对 dirty/busy 行只把结果写入 `side_authoritative_snapshot`，不改变草稿的 `base_policy_version`、当前 ticket 或 `operation_generation`，不能用重渲染丢弃未保存选择。
- 保存 ticket 绑定身份、页面、Alias、draft revision、提交时的 base version、唯一 operation generation 和 AbortController。保存期间只禁用当前行并暂停该行权威合并；后台 GET 即使看到新版本也不能让合法 PATCH 200 过期。成功响应只核对 ticket 仍是该行当前 ticket、身份/Session/导航仍有效、Alias 与 operation generation 未被明确作废，随后以 PATCH 响应作为权威值清空草稿/ticket，再触发一次 GET 对账；不得要求 `refresh_generation` 或旁路快照版本仍等于提交时值。
- 409 终止当前 ticket，把最新服务端对象写入旁路权威快照并要求管理员基于新版本重新选择，不能静默覆盖。其他失败保留已生效模式与用户选择，显示统一 error/request_id。只有离页、登出、401、tenant/user/role/session 变化或用户明确取消才推进失效代次、abort 请求并清空对应草稿/ticket；这些条件后的迟到 200/409 不得复活页面、Session 或覆盖新身份。重复点击在同 ticket 下只产生一个 PATCH。
- developer 看到相同只读字段，不输出 form、保存按钮或写请求。前端隐藏只用于体验，后端仍做权限校验。
- 页面不得声称某模型“必然被选中”；静态列表是配置候选，实际选择以 Dry-Run 或调用日志请求快照为准。

### 9.2 Dry-Run 和调用日志

- Dry-Run 路由区显示 mode、policy ID/version、price as_of、selection reason、candidate rank/role/model/provider/eligibility、有效价格 Decimal+币种、健康归类和 KeyPool `available/unavailable/unknown`。
- Dry-Run 的 prompt/max_output 按 §6.1 canonical Chat 适配，路由和预算复用同一 TokenEstimate 与同一 `as_of`；必须用结构化 Gateway content parts 与等价 Dry-Run prompt 向量证明可比结果，未来 PricePlan 也只能在该时点同时生效或不生效。
- Control 无法确认 Gateway 环境变量 Key 时明确显示 partial；`gateway_parity` 保持 `partial`、`coverage_complete=false`、`provider_credentials_keypool=false`，不能宣称线上完全等价。
- 调用日志展示真实冻结事实，并区分“动态初始选择了 fallback 角色”与“上游失败后触发 PRD-036 Fallback”。后者只由 `fallback_triggered=true` 和第二次 attempt 表达。
- 所有文本经现有 escaping；不向 DOM 注入 candidate_summary 原始 JSON、Prompt、Key 或上游错误正文。

### 9.3 响应式和可访问性

- 验收视口：1280x720、900x800、390x844；每档 `html/body.scrollWidth <= viewport`，无不可达按钮、遮挡或横向溢出。
- 1280：表格列完整，展开编辑器不覆盖相邻行；900：摘要与编辑区收敛为两列或单列；390：单元格使用 `data-label`，模式控件自适应纵向/换行，保存按钮可达且文本不溢出。
- 单选组有可访问 label、当前值和键盘焦点；提交状态用 `aria-busy/disabled`，错误反馈使用现有 live region。图标按钮沿用现有图标库和 tooltip，不手绘 SVG。
- Browser 验收只允许精确的 Console/Control 回环 origin；即使也是 localhost，host 或端口不同也必须拦截。数据库、runtime 状态和截图目录必须位于 `artifacts/prd047` 且符号链接不得逃逸；运行前删除旧 result/截图，防止陈旧制品被误当当前证据。
- 每个角色在截图前扫描 DOM、URL、localStorage 和 sessionStorage；明文密码、Bearer/Virtual/Provider Key 和 Prompt 哨兵不得出现。登录 Token 只允许在当前页面的固定 sessionStorage 槽位，logout 后 Token/上下文槽位必须同时消失；console 错误只记固定类别，不输出原文。
- 截图必须解析 PNG 签名/chunk/filter，核对视口宽度、最小高度、文件上限、可见像素和非单色；这只证明制品非空，遮挡、裁切和内容正确性仍要求对三张最终截图做真实目视验收。

## 10. 错误码设计

| 错误码 | HTTP | 场景 | retryable |
| --- | ---: | --- | --- |
| `model_alias.routing_mode_invalid` | 422 | 非三枚举、类型错误或 extra 字段 | false |
| `model_alias.routing_policy_version_invalid` | 422 | expected_version 非正整数 | false |
| `model_alias.routing_policy_version_conflict` | 409 | 乐观版本不匹配 | false |
| `model_alias.routing_policy_request_conflict` | 409 | 同 tenant/actor/request_id 被不同配置意图复用 | false |
| `model_alias.routing_policy_update_failed` | 503 | 配置事务或审计事实无法提交 | true |
| `gateway.no_route_available` | 503 | 动态模式所有候选均不可调用 | true |
| `gateway.routing_policy_invalid` | 503 | 持久策略模式/版本损坏 | false |
| `gateway.routing_snapshot_unavailable` | 503 | 数据库读取、锁或 KeyPool 快照依赖暂时不可用 | true |
| `gateway.routing_snapshot_invalid` | 503 | 价格、健康、枚举或租户关系等持久事实损坏 | false |
| `gateway.routing_decision_unavailable` | 503 | 动态决策/候选事实无法原子持久化 | true |
| `gateway.budget_unavailable` | 503 | active CNY Budget 所需价格/用量/预留事实缺失、损坏或暂不可用 | true |
| `provider_key.unavailable` | 503 | 实际调用阶段无可用托管 Key，且不满足非生产默认租户/code/基准 Endpoint 环境 Key 边界 | true |

不存在/跨租户 Alias 继续使用既有不泄露存在性的 404；admin 权限不足继续 `auth.permission_denied` 403。所有 API 错误通过统一 envelope 返回 `code/message/request_id/trace_id`，诊断目录只能使用固定说明，不能附带 SQL、路径、价格、Endpoint 或 Key 细节。

`record_audit_log` 的白名单、大小或存储失败必须在服务边界回滚并统一转换为 `model_alias.routing_policy_update_failed`，不能把 `audit_log.*` 或 SQLite CHECK 细节直接暴露。相同 request_id 携带不同配置 fingerprint 使用 `model_alias.routing_policy_request_conflict` 409；精确重复则按 §5.3 恢复同一事实。

## 11. 安全、权限与租户约束

- tenant 只能来自认证 Session/Virtual Key；PATCH、列表、Dry-Run、Gateway、日志和候选查询全部使用 tenant-qualified SQL。
- 路径 ID、expected_version、Alias 名或请求 body 不能改变 tenant。跨租户对象与不存在对象使用相同响应。
- admin 写、admin/developer 读、viewer 拒绝；浏览器按钮隐藏不能替代 FastAPI dependency 与服务层 tenant 复验。
- 数据库用复合唯一键、FK 与 trigger 防止跨租户 Alias/Policy/Decision/Candidate/Model/Provider 关系；只依赖单列外键不够。
- KeyPool 排序快照不解密、不更新使用时间；明文 Key、Token、Secret、Prompt、Response 不得进入日志、事件、audit、候选或异常。进程级 Provider Key 不能表达租户所有权，因此生产完全禁用；非生产也只允许 active 默认租户中 code 和数据库/冻结 Route Endpoint 均匹配运维基准的 active Provider 使用。
- Provider 审计不得保存可逆 Endpoint；只允许保存规范化 URL 的 SHA-256 变化摘要，必须删除 userinfo、path、query 和 fragment 的任何明文。path 可能承载客户 Token，不能因 query 已移除就视为安全。
- 目录/PricePlan 单价统一要求有限、非负、每百万 Token 不超过 `1000000000` 且小数位不超过 12；损坏财务事实不得在 Budget 聚合中静默归零。CNY 预算没有版本化汇率时，USD 只能进入 Usage 原币账本，不能以 0 CNY 通过 Guard。数据库/API 字段名仍保留 `_per_1k` 以兼容历史客户端。
- 结构化日志只记录稳定 ID、模式、版本、reason 和计数。禁止 `logger.exception` 输出可能含 URL/SQL/上游正文的 traceback；异常只记固定 code 和异常类型白名单。
- Webhook payload 使用递归敏感键扫描、精确字段集合、稳定 event ID、HMAC 与租户内订阅过滤。
- 所有新模块、类、数据结构、常量、字段、函数、事务边界、权限/租户判断、异常转换、回退与安全过滤必须有解释业务原因的中文注释，并通过 `scripts/ci/check-comments.py`。

## 12. 测试用例

### 12.1 Shared 纯排序单元测试

1. fixed default/fallback 顺序与现有行为一致。
2. cost_first 两条 healthy/可用/同 CNY，按本请求 Decimal 成本选择低价。
3. 输入便宜但输出贵，`max_tokens` 改变排序结果；Embeddings 输出为 0。
4. 当前有效 PricePlan 覆盖目录价；未来计划不生效；相同 effective time 使用稳定已有排序规则。
5. 显式 PricePlan 零价胜出；无 schema version 的目录 0 是缺价。
6. 任一缺价整体 fixed 回退并记录 price_missing；CNY/USD 整体 fixed 回退且绝不直接数值比较。
7. 同价按 default/fallback/model_id 稳定排序；重复执行结果完全相同。
8. availability_first 新鲜 healthy 胜 unknown/stale；新鲜 unhealthy 被拒绝；stale unhealthy Fail Open。
9. KeyPool unavailable 被动态模式拒绝，fixed 不评估 KeyPool。
10. 无候选返回统一失败；输入超过两项被拒绝或有界截断按正式合同处理。
11. 低于/等于/高于 15 位量化半单位的成本差覆盖 ROUND_HALF_UP、tie-break 和持久值可复现性。
12. Chat string/content parts、Embedding string/string array/token ID array、嵌套/混合/null/bool/float、max_completion_tokens/max_tokens 优先级、超大整数与 100,000 节点饱和合同。
13. fixed/availability/cost 单 eligible 的延迟估算回调调用数为 0；cost 两 eligible 恰为 1，且两阶段结果使用同一候选快照。
14. 纯函数不访问数据库、环境变量、日志或网络。

### 12.2 KeyPool 测试

1. auth none 可用。
2. 非生产 default tenant、active Provider code 和运维基准 URL 均匹配且无托管 Key 时，Gateway 环境 Key可用；环境值不出现在结果/日志。
3. 生产环境始终禁用进程级 Provider Key；存在任何 aesgcm 托管 Key 时也禁止环境回退。
4. active、冷却过期、冷却未过期、disabled、新鲜 unhealthy、stale unhealthy 的准确状态。
5. 旧明文行不能冒充托管可用 Key。
6. 快照前后 `last_used_at/status/rate_limited_until` 完全不变，decrypt 函数调用为 0。
7. 快照可用但实际解密失败/并发禁用时仍返回 `provider_key.unavailable`，不静默换模型。
8. 第二 tenant 创建同名 Provider 与自有 Endpoint 时，快照必须 unavailable、实际选择必须 `provider_key.unavailable`，且明文环境 Key不得进入请求、日志、审计或错误。

### 12.3 Control API/RBAC/租户测试

1. 0036 后存量 Alias 全部 fixed v1；新建 Alias 同事务创建 fixed v1。
2. admin 三模式 PATCH 成功、版本 +1、request/trace ID、audit 和配置 Webhook 正确。
3. 同值幂等不增版本、不重复事件；expected_version 冲突 409 且数据库不变。
4. 通用 Alias PATCH/Fallback PATCH 对 alias/environment/status/default/fallback 的每次真实变化同事务版本 +1；description、fallback conditions 与同值不增；audit/config event 的 changed fields 精确。
5. 模式 PATCH 与候选 PATCH 使用两个真实连接竞争，版本连续且没有“同版本不同候选图”；直接 SQL Alias UPDATE 也由 AFTER trigger bump，一条多字段 UPDATE 只 +1；旧 expected_version 必定 409。
6. developer/viewer PATCH 均 403；developer GET 有只读策略；viewer 导航和数据请求均无。
7. 跨租户 path ID 404，不能更新/读取/审计/投递其他 tenant。
8. 权限先于 body 读取；非法 content-type/mode、缺失/extra/bool version、重复 key、空/超 8KiB body、非 UTF-8/JSON/对象均返回精确领域码和 request_id，不落 audit/event。
9. Alias create 自动 fixed v1 并同事务写 `routing_policy.created` audit/event；update 写 updated；no-op/CAS conflict 不写。DB/audit/event 提交失败整体回滚，提交后 dispatcher 抛错不回滚且 event 可查。
10. 相同 request_id+fingerprint 重试不产生第二 version/audit/event/delivery并重派稳定 event ID；不同 fingerprint 409。GET Alias 返回安全对象；缺策略行返回 nullable implicit fixed、写 API fail closed且无虚构 ID/event。
11. 对 owner trigger 做逐关系列直接 SQL INSERT/UPDATE 反例：Model 指向其他 tenant Provider；Alias 指向其他 tenant Environment/default/fallback Model、`environment` code 不一致、default 与 fallback 相同；RoutingDecision 的 VirtualKey/Environment/Alias/selected Model/Provider/RoutingPolicy 任一跨 tenant、VirtualKey/Alias 环境不一致、Policy 不属于 Alias或 version/mode 不一致、selected Model 的 Provider 不匹配，均必须在数据库层 ABORT，且合法同租户控制组成功。
12. Candidate 直接 SQL 反例覆盖 `default+priority 1`、`fallback+priority 0`、default role 指向 Alias fallback、fallback role 指向 Alias default、Alias 无 fallback 却插 fallback 行；全部拒绝且既有 Decision/Candidate 不变。UPDATE/DELETE 不可变 trigger 继续拒绝历史篡改。

### 12.4 Gateway 集成测试

1. TC-0093：cost_first 选择低成本 healthy 且 Key 可用模型；Usage/RequestLog/PriceSnapshot 归因实际模型。
2. TC-0093：availability_first 避开新鲜 unhealthy Provider，选择 healthy；unknown/stale 次级。
3. 动态模式过滤所有 Key 不可用候选；全部不可用返回 `gateway.no_route_available`、request_id 和失败 request log。
4. fixed 迁移前后黄金测试：选择、错误码、流式、PRD-036 Fallback 全部一致。
5. route_to 和 direct model 不读取 RoutingPolicy、不发 route.policy_selected、不继承 Alias fallback。
6. 动态初始 default + PRD-036：失败后第二 attempt；动态初始 fallback：一次 primary attempt、`fallback_triggered=false`、不反向重试。
7. 动态 default 选中但 fallback 因 Provider/Key 被 rejected 时不附加 runtime fallback；eligible standby 才附加，FallbackPolicy ID/version/conditions 与 route decision 来自同一冻结事务。
8. 能力不支持仍在选路后返回既有错误，候选排序不偷偷按能力改写。
9. 决策、最多两候选、policy/version/mode/as_of/event ID 原子持久化；动态决策写失败则不调用 Provider。
10. 事件在决策提交后入队；慢/失败接收端不增加主请求等待，队列满只记录安全丢弃。
11. 日志、DB、Webhook、响应扫描无 Prompt/Response/Bearer/Provider Key/Token/base_url。
12. Decimal/币种/健康时间/Policy/tenant 图损坏映射 snapshot_invalid，锁与查询故障映射 snapshot_unavailable；两类都写失败 request log/request_id 且不裸 500、不调用 Provider。
13. mode-aware 故障矩阵必须逐项注入：① fixed 面对价格坏数据/查询失败和 KeyPool 查询失败仍保持黄金行为，token/price 字段为 NULL、candidate keypool=`not_evaluated`；② availability 面对价格坏数据/查询失败仍按 Health+Key 选择且 price/token 为 NULL/not_evaluated，但 KeyPool 查询失败为 snapshot_unavailable；③ cost 0/1 eligible 不读价格、不算 token并可成功，恰有 2 eligible 时价格坏数据才为 snapshot_invalid、价格查询失败才为 snapshot_unavailable。
14. active CNY 预算下 USD、混合 CNY/USD、超过价格上限、超过 12 位小数和极端指数全部 503 `gateway.budget_unavailable`、Provider 0 次；带版本 CNY 真零价继续通过，无预算规则不新增价格依赖且 Usage 对坏价安全降级。
15. 预算分项结算覆盖主成功、主上游失败无 fallback、主失败备成功、双上游失败、第二尝试本地失败和 Usage 写入回滚；逐项断言原始总预留、`outstanding_cost_cny`、Usage、attempt `estimated_cost_cny`、tenant/request 隔离和次请求 projected cost。

### 12.5 Dry-Run/请求日志/Console 测试

1. Dry-Run 与 Gateway 使用同一向量，cost/availability 选择一致；mode/version/as_of 冻结可见。
2. canonical Dry-Run `prompt/max_output_tokens` 与等价 Gateway 单 Chat content/`max_completion_tokens` 产生同一 TokenEstimate、路由成本和 Budget 成本；structured parts、未来 PricePlan 时点均锁定。
3. 无 Gateway 环境 Key 时 KeyPool=`unknown`、parity partial，不伪装完整覆盖；managed Key 确定向量与 Gateway 一致；Provider 调用、解密、Key 写入均为 0。
4. 请求日志按 tenant 唯一 decision 返回强类型 candidate facts；跨租户 404；损坏 JSON 不回显。
5. API client 精确 PATCH path/body/Auth/X-Request-ID，不发送 tenant；403/409/422/非 JSON 错误保留 request_id。
6. admin 可编辑、developer 只读、viewer 无 DOM/预取；保存 PATCH 未返回时触发一次或多次 15 秒 GET，GET 只推进 refresh generation/旁路快照而不推进 operation generation/base version，随后合法 PATCH 200 仍被接纳并成为权威状态；重复点击只有一个请求。
7. logout/tenant/session/导航切换后的迟到 200/409 被丢弃；401 清 Session，离页返回不复活旧 ticket。
8. XSS 字符被转义；价格 0 与缺价 UI 明确区分；动态初始 fallback 不显示“已触发 Fallback”。
9. 三视口无横向溢出、遮挡或不可达操作，键盘与 live region 可用；PNG 尺寸、非空/非单色自动校验与三张截图目视验收分层记录。
10. Browser 只允许精确 Console/Control 回环 origin，不同 host/端口即使为 localhost 也拦截；违规只保存固定类别，不记录 URL。
11. admin/developer/viewer 的 DOM、URL、localStorage 和非授权 sessionStorage 槽位均无密码、Token、Virtual/Provider Key 或 Prompt 哨兵；当前 Token 只在固定槽位，logout 后认证槽位为 0。
12. Browser 在任何 Chrome/页面/后置失败下都进入 finally cleanup；清理前在写锁内重新校验完整 owner/命名图，tenant-qualified UPDATE 精确影响行数，缺行、跨租户或清理失败都禁止生成 passed 制品。

### 12.6 迁移、回归与运行验收

- 专项：shared、Alias API、routing、KeyPool、Dry-Run、request log、Webhook、Console。
- 迁移：空库、包含 legacy NULL-env Virtual Key 和重复 Decision request 的真实 0035 运行库副本、0036 往返、重复 up 防护、WAL/清理；逐项断言 parent unique/composite FK、Model/Alias/RoutingDecision owner trigger、Candidate role/priority/Alias target 的跨租户与内部不一致 DML 反例、Alias SQL 版本 trigger、Policy-bound Decision request 部分唯一、历史重复逐行保真、Audit CHECK/白名单、Provider 旧 `base_url` 清理唯一例外、Webhook original+replay/订阅 down 清理，以及原始预留/历史 Usage 语义保真；down 后还要断言全部 PRD-047 owner/graph trigger 与 Environment/Alias/Provider 新索引均不存在，并比较 0035 `sqlite_master` 与非 PRD-047 历史。比较必须显式排除已不可逆清理的 Provider URL 明文，但 Audit 行数、主键和链路字段仍必须一致。
- 全量：根级 Python pytest、Node 示例、Console 全量、Console lint、中文注释、Python compile、Node check、目标 Ruff。
- 真实 API：复制开发库到 PRD-047 独立运行库，启动 Gateway/Control/Console 与本地 Provider/Webhook receiver；使用 admin/developer/viewer 和第二 tenant 验证主、异常、权限、租户、成本、健康、KeyPool、事件、日志与 Usage。
- 浏览器：1280x720、900x800、390x844；admin 写、developer 只读、viewer 直达 hash；截图、像素/布局、console error、network、DOM/sessionStorage/localStorage 敏感扫描。
- 清理：在写入前复验 runtime 状态中完整 tenant/Environment/User/Session/Alias/Policy/Model/Provider/Key/Webhook owner 图和验收命名；所有 UPDATE 带 tenant/owner 条件并断言精确行数，任何前置失败也进入 finally。恢复原有资源状态、撤销临时 Session，确认可调用 active 临时入口为 0；RoutingPolicy 因 0036 只允许 `enabled` 且被历史 Decision 引用，保留为 disabled Alias 下不可达配置事实并单独核对。停止服务并确认 8000/8001/5173 无监听。

## 13. 代码修改计划

1. 先新增 shared RoutingPolicy 纯排序数据结构、算法、价格/健康/KeyPool 状态合同和单元测试。
2. 编写 0036 up/down，覆盖 Audit 行/链路保真与旧 Provider URL 唯一安全清理例外、父复合索引、Alias 自动 Policy/版本 trigger、配置事件、Decision/Candidate 强约束、Webhook down 清理与 sqlite_master 往返测试。
3. 扩展 Control ModelAlias GET/PATCH、认证后严格 JSON reader、四入口统一事务/CAS、audit 白名单、持久配置事件、同 request_id 恢复、RBAC 和并发测试。
4. 新增 Gateway KeyPool 无副作用快照，重构 Alias 路由为 mode-aware 冻结事实 + shared 两阶段排序 + 同事务 FallbackPolicy + 原子持久化。
5. 把 policy/version/event receipt 贯穿 RoutePlan，在 handler 提交后入队，泛化异步事件队列日志。
6. 同步 PRD-046 Dry-Run canonical input/同一 TokenEstimate/as_of 与请求日志 DTO/服务，补精确 parity 和安全候选展示。
7. 新增 PAGE-067、API client、身份绑定草稿/ticket、定时刷新/迟到响应门禁、权限与响应式测试。
8. 更新 README、目录说明、API/开发文档和 PRD-047 测试记录。
9. 依次执行专项、迁移、全量、真实三服务/API、Webhook、数据库/日志/资源清理和三档浏览器验收；仅在全部证据通过后把状态改为完成。
10. 实现期安全复审新增：0036 同批分项预留、默认租户与基准 URL 环境 Key、CNY Budget/价格上限、Provider URL 审计 hash 和 owner-safe Browser cleanup；先补失败复现，再修实现并重跑全部门禁。

## 14. 验收门槛

- `fixed/cost_first/availability_first` 均可由 admin 配置，developer 只读，viewer 后端拒绝。
- cost_first 在健康、Key 可用、同币种有价候选中选择本请求估算成本最低者；缺价、零价、未来价、同价和混币均有确定且测试锁定的行为。
- availability_first 必须避开新鲜 unhealthy Provider 和不可用 KeyPool；unknown/stale 合同与既有 Fail Open 一致。
- 存量 fixed、route_to、direct、PRD-036、流式和能力校验无回归。
- 每请求 policy/version/mode/candidates/reason/event/request_id 可追踪，最终 Usage/成本/失败请求归因实际模型。
- 所有业务事实 tenant 隔离；后端权限、统一错误、request/trace ID 和敏感过滤完整。
- 0036 up/down（含分项预留扩展）、专项和全量回归、真实 API、三服务、Webhook、日志/数据库清理、三档浏览器全部通过。
- 中文注释检查继续通过；任何未运行或失败项都阻止“已完成”标记。

## 15. 已关闭自审问题

1. `routing_candidates` 作为每请求不可变事实是否完整满足 Excel OBJ-067，同时避免与 Alias 主备双写？
2. 缺价/混币退回 fixed 是否比运行时失败更符合可用性边界，且 UI/日志是否足够避免误以为发生了成本比较？
3. 动态模式决策写失败 fail closed、fixed best-effort 的兼容分界是否明确？
4. `route.policy_selected` 的 request/config 两种精确 payload 是否满足 Excel“执行或状态变化”，又不扩大敏感面？
5. Dry-Run 对 Gateway 环境 Key 保持 unknown/partial 是否诚实，是否仍可验证动态排序主合同？
6. expected_version 必填、先 CAS 后 no-op，以及 Alias 路由字段同事务版本推进，是否完整阻止同版本对应不同候选图？

## 16. 两轮设计自审记录

### 16.1 第一轮自审

状态：通过。第一轮由独立 Agent 核对 Excel、架构、现有 Alias/PRD-036/KeyPool/Price/Webhook 热路径；初审不通过，按 1 项阻断、6 项重要问题修订后，复核又发现并关闭 1 项 mode-aware 依赖问题，最终复核确认无残余阻断。

第一轮发现并修订：

1. **阻断：RoutingPolicy 版本原先只覆盖 mode，同一版本可对应不同 default/fallback 图。** 已把 Alias 名称、Environment、状态、default/fallback 真实变化纳入同事务版本 +1，并补 audit、配置事件、两个真实连接竞争和同值不增测试；description 与 PRD-036 conditions 明确不影响初始策略版本。
2. **重要：implicit fixed 同时被写成合成 v1 和 nullable policy，身份矛盾。** 已固定缺行只返回 `routing_policy=null/effective fixed/implicit=true`，Gateway 使用 nullable legacy fixed 且不发 policy-selected；正常迁移和服务创建必须持久策略，禁止发明 ID。
3. **重要：新 PATCH 把 expected_version 设为可选会允许静默覆盖。** 已改为必填正整数，事务内先 CAS 再判断同值；响应丢失重试使用旧版本返回 409，客户端 GET 确认。
4. **重要：未量化排序和固定字符串解释精度不一致。** 已按现有单价 12 位上限固定 15 位 `COST_QUANT`、`ROUND_HALF_UP`，比较、平局、持久/API/UI 使用同一值，并补半单位边界测试。
5. **重要：价格、健康、KeyPool/数据库坏数据和依赖故障可能裸 500 或伪装缺价。** 已增加 snapshot unavailable/invalid 统一码；缺价格式与损坏严格区分，要求失败 request log/request_id 且 Provider 调用为 0。
6. **重要：token 估算没有覆盖 Gateway 当前接受的结构化 content/input。** 已固定 content parts、Embedding token ID/字符串数组、bool/float/null、输出参数优先级、节点/token 饱和上限与 truncated 事实，并要求 Gateway/Dry-Run 共用纯函数。
7. **重要：运行时 actor 使用 Virtual Key ID 同时违反 actor 语义与 Key ID 禁止边界。** 已固定 runtime actor 为空；配置变更使用认证用户，并为 mode、候选与 Alias 状态变化定义精确 change_kind/changed fields payload。
8. **复核重要：快照章节像是所有模式均读取 Price/KeyPool/token，与 fixed 兼容和最小隐私面冲突。** 已改为 mode-aware 最小矩阵与 shared 两阶段资格/成本排序：fixed 不读 Key/Price、不算 token；availability 只增加 Key；cost 仅两条 eligible 时读取 Price/估算 token。错误只由当前分支实际依赖触发，并补依赖故障与 NULL/not_evaluated 测试。

### 16.2 第二轮自审

状态：通过。初审及首次复核均曾不通过；修订初审的 5 项阻断/5 项重要问题，以及首次复核新增的 1 项阻断/2 项重要问题后，由同一独立审查者完成最终复核，确认全部问题关闭且未发现新的阻断或重要缺口，允许进入编码。

第二轮发现并修订：

1. **阻断：0035 audit CHECK/应用白名单不接受 RoutingPolicy。** 已要求 17 列保真重建，加入 created/updated + object、复制 guard、五普通索引 + Dry-Run unique + 三 trigger，并同步 AuditRequestMeta 与 summary 白名单；down 过滤后恢复 0035。
2. **阻断：down 没有清理旧应用无法识别的 Webhook。** 已固定 attempts -> original/replay deliveries -> 结构化订阅 JSON 顺序，覆盖保序、空数组与其他事件不变。
3. **阻断：服务 bump + trigger 兜底无法在 SQLite 做 commit-time 证明。** 已改为 Alias AFTER INSERT 自动 fixed v1、AFTER UPDATE 路由字段自动 +1；四 API 统一锁内 CAS，Policy mode 自身精确 +1，直 SQL 也不能形成同版本不同图。
4. **阻断：复合 FK 缺父 UNIQUE。** 已列 Alias/Provider/Decision 0036 unique，复用 0033 User/Model，并固定 Candidate 对 Decision/Policy/Alias/Model/Provider 的 composite FK 与图内一致 trigger。
5. **阻断：配置事件无持久事实，commit 后可能永久无证据。** 已新增不可变 routing_policy_events，同事务保存 payload/event ID/fingerprint；同 request 精确恢复重派，自动扫描/DLQ 仍留 PRD-057。
6. **重要：动态 default 可能附加已 rejected fallback。** 已限定只有同一快照 eligible standby 可附加，并同事务冻结 FallbackPolicy ID/version/conditions。
7. **重要：Dry-Run prompt/max_output 与 Gateway shared estimator 无映射。** 已定义 canonical Chat 适配，路由/预算复用同一 TokenEstimate 与 as_of，并补等价结构化向量。
8. **重要：Pydantic 默认把精确领域 422 抢成 infra.validation_failed。** 已改为 admin dependency 后 8 KiB 严格 JSON reader，覆盖 content-type、重复 key、extra、bool/version 和 audit 错误收敛。
9. **重要：15 秒刷新与迟到响应可丢草稿或跨身份覆盖。** 已定义 tenant/user/role/session/navigation/Alias/version/generation 绑定的 draft/ticket，dirty merge、abort 和迟到响应丢弃。
10. **重要：Decision/Candidate 内部一致性与 request 唯一不足。** 已增加迁移前重复 guard、tenant/request unique、policy triple、rank/status、price group、selected model/provider 与 candidate/decision 图一致约束及 DML 反例。
11. **首次复核阻断：Alias/Model/Decision 仍可被直接 SQL 写成跨租户 owner 图。** 已补 Model->Provider、Alias->Environment/default/fallback、Decision->VirtualKey/Environment/Alias/Model/Provider/Policy 的 INSERT/UPDATE trigger 矩阵、父复合唯一索引、环境和 Model/Provider 图一致性、down 清理及逐关系 DML 反例。
12. **首次复核重要：Candidate role/priority 未与 Alias 当时目标形成强约束。** 已补 CHECK 固定 default=0/fallback=1，并要求 insert trigger 在同一冻结事务按 role 核对 Alias default/fallback target；无 fallback 或角色反转的 DML 必须拒绝。
13. **首次复核重要：后台刷新推进 generation 会误丢合法保存响应。** 已拆分只淘汰 GET 的 refresh generation 与只由保存/明确取消/身份页面失效推进的 operation generation；dirty/busy 刷新仅更新旁路快照，不改变 ticket/base version，并补“保存期间刷新后 200 正常生效”测试。

### 16.3 真实运行库迁移前置复核

状态：通过。实现后首次对开发库 0034 副本实际执行 0035/0036 时，0036 preflight 安全中止；未清洗或覆盖开发库。逐条件诊断确认设计阶段遗漏了两类已验收历史兼容事实，修订后由独立 Agent 交叉复核：

1. **legacy Virtual Key 的 Environment 规则比现有鉴权更严。** 开发库存在 PRD-030 明确保留的 `virtual_keys.environment_id=NULL` Key，Gateway 会在同 tenant 下按 Key code 解析 Environment。0036 已改为镜像该规则：只有 Key code、Decision code 与同租户 Environment ID 完整对应时兼容；错误 code、ID 或 tenant 仍由 preflight 和 INSERT/UPDATE trigger 拒绝，且不回填或改写 Key。
2. **全表 request 唯一会阻断并迫使丢弃 0035 历史。** 开发库同一历史 request 有多条 Policy=NULL Decision。0036 已改为逐行保留全部公共列和 request_id，仅对 Policy 非空的 PRD-047 决策建立部分唯一索引；调用日志优先新 Policy 事实，纯 legacy 重复确定性取最新。该边界不放宽 PRD-047 Alias 决策的一请求一事实合同，也不修改 direct/route_to 历史。
3. **配置事件数组形状合法仍可能无法恢复投递。** 数据库原约束只验证字段集合、白名单和规范顺序，直接 SQL 仍可写入与 Webhook validator 语义矛盾的事件。0036 INSERT trigger 已继续约束 created 必须为 fixed v1 初始事实、无 previous mode 且完整列出七个字段；普通更新必须有前态，`mode/candidates/alias_state` 与对应字段类别双向一致，mode 更新必须真实改变模式，非 mode 更新必须保持模式。任何可提交事件都能通过同一应用 validator 重建，异常事件不能成为永久不可投递事实。

### 16.4 实现期第三轮安全与封账复审

状态：通过并完成。两名独立 Agent 对最新实现、预算/Usage、Provider Key、审计持久化和 Runtime/Browser 验收脚本进行只读交叉审查，并用内存数据库或纯函数复现；主 Agent 逐项核对调用链后，将下列问题升级为封账阻断并完成修订、实现与最终复跑：

1. **阻断：非默认租户可借同名 Provider 读取全局环境 Key。** `provider_code` 只在 tenant 内唯一，而进程环境变量全局；租户可把同 code 指向自有 Endpoint。现固定环境 Key 只能服务配置的 default tenant 且必须匹配运维基准 URL，其他租户必须使用自己的托管 Key，并补快照与实际调用双层测试。
2. **阻断：USD 被当作可信 0 CNY，极端有限 Decimal 可绕过或打断预算。** 现固定 active CNY Budget 只接受 CNY；Budget、Usage、Model 目录与 shared 路由统一 `1e9/12 位`价格合同，非法事实映射稳定错误或安全降级，禁止裸 `decimal.Overflow/InvalidOperation` 和 spent 静默归零。
3. **阻断：request 级主备预留被任一 Usage 整笔替代。** 这虽是 PRD-036 记录过的 MVP 边界，但 PRD-047 的动态主备和成本模式会扩大影响。现把 nullable outstanding 覆盖额纳入尚未发布的 0036，保留原始总预留并按实际上游尝试精确结算未知分项。
4. **阻断：Provider audit URL 保留 path。** path 可直接承载客户 Key/Token；现审计只保存不可逆 URL hash，并要求持久 `audit_logs` 与运行日志共同扫描哨兵。
5. **阻断：Browser cleanup 信任未完整复验的 runtime ID，且 0-row 清理仍可 passed。** 现要求整个资源图 owner/命名复验、tenant-qualified UPDATE、精确影响行数与前置失败 finally 清理；由于 0036 Policy 状态当前只允许 `enabled` 且历史事实引用它，验收须把它明确核对为 disabled Alias 下不可调用的配置事实，不能误报为 active 入口。

第三轮兼容性复核结论：上述修订不改变无预算规则的 Provider 可用性，不对 USD Usage 伪造汇率，不让非默认租户继承系统 Secret，不改写原始预留金额，也不扩大 API 响应字段。0036 新增可空列对 0035 历史行为向后兼容，down 不丢失原有业务事实。新增实现、专项、迁移、全量及最新 Runtime/Browser 已全部通过，本轮状态正式关闭为“通过并完成”。

### 16.5 最终运行与封账复核

状态：通过。最终成功批次为 `artifacts/prd047/20260719T145100`，只使用开发库的 SQLite Backup 副本，不修改源库。首次仅应用 0035/0036，二次迁移 `applied=[]`；55 张共同业务表逐表保真。Runtime 与真实 Chrome 均为 `status=passed`，admin/developer/viewer、第二租户、无 KeyPool、Budget、Webhook、Usage、三档布局和 owner-safe cleanup 全部闭合。

停止服务并 checkpoint 后，证据库 SHA-256 为 `daf0acfc9d88aa79681cbcbc83d763738ee280f23059abca0d78e1f9e9972039`，36 条迁移/最高 0036、`integrity_check=ok`、外键违规 0、WAL/SHM 和三个端口监听均为 0。Gateway `32/32`、Control `205/205` 条 MetaHub JSON 可逐行解析；`httpx/httpcore`、完整 Endpoint、Prompt、密码、Bearer/JWT、完整 Key/Secret、Traceback/ERROR 命中均为 0，503 只保留无 KeyPool 同一 request_id 的三条预期结构化事件。

代码级最终结果为 Python `574/574`、Node 示例 `33/33`、Console `228/228`、Console lint `106 files`、Runtime 专项 `6/6`、Browser 专项 `16/16`，中文注释、compile、`node --check` 和改动范围 Ruff 均通过。完整分层证据、截图 hash、作废批次和清理计数见 [PRD-047 测试记录](../testing/PRD-047-tests.md)。
