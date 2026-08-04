# PRD-050 Prompt Cache 感知粘性路由设计

> 状态：已完成。两轮编码前自审、实现、当前源码相关验证、真实 Runtime/Browser、日志隐私、资源清理和停止后数据库终审均已通过；权威证据见第 12 节和测试记录。

## 1. 权威需求与需求理解

### 1.1 需求来源

- Excel `01_PRD总表` 第 51 行：`PRD-050`，`M1-企业POC`，`P1`，核心层，缓存感知路由。
- 用户：管理员 / 财务。当前基础 RBAC 只有 admin、developer、viewer，尚无 finance 角色；本期由 admin 承担财务配置职责，developer 只读，viewer 不可见且后端拒绝。
- 用户故事：长系统提示词应尽量命中上游原生 Prompt Cache，从而减少重复输入成本。
- 范围：计算由租户、Virtual Key、数据分级和 Prompt 指纹组成的亲和键；同一键优先复用同一 Provider Key / 节点；记录缓存预计命中。
- API：`PATCH /model-aliases/{id}/cache-affinity`。
- 页面：`PAGE-072 路由策略页`、`PAGE-073 日志详情`。
- 数据对象：`OBJ-070 CacheAffinity`。
- 事件：`EVT-056 cache.affinity_selected`。
- 测试：`TC-0099` 主流程；`TC-0100` 边界、异常、权限与安全。
- 验收：相同长 Prompt 请求命中同一路由；缺 tenant、key 或 data_class 时不得启用跨域缓存。
- 非目标：语义缓存。

系统架构第 7.4 节进一步冻结以下边界：

1. 只对长 System Prompt 或固定前缀计算 `prompt_fingerprint`。
2. Cache Key 必须包含 `tenant_id`、`virtual_key_id`、`data_class`、`model_endpoint_id` 和 `prompt_fingerprint`。
3. 同一指纹优先复用同一上游 Key 或端点，提高 Provider 原生缓存命中率。
4. MVP 不缓存模型响应，不引入向量检索，避免缓存投毒、权限越权与复杂基础设施。

### 1.2 本条交付目标

本条交付的是“路由亲和元数据”，不是响应缓存：

1. admin 可以为当前租户的一个 ModelAlias 配置是否启用、可信 `data_class`、长前缀门限和亲和有效期。
2. Gateway 在 RoutingPolicy 冻结事务内计算不可逆亲和摘要；若已绑定端点仍满足当前 Policy、健康、能力、质量门限和 KeyPool 硬约束，则在 Decision 提交前优先该端点。
3. 模型端点确定后，KeyPool 在仍然健康、active、未限流且可解密的候选中优先复用绑定的 Provider Credential；绑定失效时按既有确定性规则重绑。
4. 每次启用亲和的真实 Key / 节点选择形成租户限定的 Selection；选择快照不可变，Provider 终态只允许单向 CAS，并投递一次 `cache.affinity_selected`。
5. 日志详情同时展示“预计命中”和 Provider 实报 `cache_tokens`，不得把预计命中伪装成实际缓存命中或直接改写成本。

### 1.3 本期明确不做

- 不保存、返回或复用 Prompt / Response 正文。
- 不实现语义缓存、相似度检索、向量数据库、Response Cache 或跨请求答案复用。
- 不在 RoutingDecision 已提交后改写模型或 Provider；端点亲和必须在当前合格候选集合内、Decision 提交前完成。
- 不绕过 `fixed`、`cost_first`、`availability_first`、`quality_cost` 的资格、质量 floor、价格 / 质量依赖和冻结事实。`fixed` 继续服从主备硬顺序；动态模式只在仍合格候选内加入亲和优先级。
- 不从任意请求 Header、OpenAI `metadata`、请求正文或客户端自报字段读取可信 `data_class`。
- 不借本 PRD 开放 Policy DSL 的 `data_class`。PRD-045 对 active Policy 的现有发布限制继续生效。
- 不新增 finance 角色，不改变 PRD-008 的 admin/developer/viewer 角色目录。
- 不对 Direct Model 或 Policy `route_to` 自动继承 Alias 的 CacheAffinity。
- 不在一次 Provider 内新增隐藏重试；Key 失效后的下一次请求可重绑，本次调用继续遵循既有一次主调用和一次受控 Fallback 上限。

## 2. 核心语义冻结

### 2.1 端点偏好先冻结，Key 亲和后执行

现有 Gateway 会在 `routing/models.py` 中原子冻结 RoutingPolicy、Candidate 和 Decision，然后才执行 DLP、限流、预算与 Provider 调用。本期把“端点偏好”放入该冻结事务，把“Credential 选择”保留在调用前 KeyPool；任何实现都不能在 Decision 提交后再换模型或 Provider，否则日志解释和真实流量会分叉。

因此顺序固定为：

1. 认证并取得可信 tenant / Virtual Key。
2. 在同一 RoutingPolicy 事务内读取 Alias CacheAffinity、计算请求 scope，并加载该 scope 下与当前候选 endpoint revision 对应的 Binding。未过期 pending 指针和 warm Binding 都可提供端点偏好，但只有 warm 可能形成预计命中。
3. 先执行既有确定性选择，再由共享纯函数在不降低健康 / 质量证据层级的前提下应用端点偏好；`fixed` 永不重排。Decision 与升格 Candidate 使用安全 `routing.cache_affinity_*` 最终 reason，降格 Candidate 使用既有 standby reason；原算法 rank/status/reason 和配置版本进入安全 candidate_summary，不保存摘要。
4. 对最终端点创建或复用一条完整五维 Binding，并由 scope 指针建立 30 秒 pending reservation；Policy、Candidate、Decision、scope 和 reservation 在同一事务提交。这样首个并发请求提交后，后续请求能在 Provider 调用前收敛到同一端点。
5. 执行能力校验、DLP、限流和预算。
6. 创建 routing attempt；流式请求也必须先创建 `StreamSession(starting)` 执行门。
7. KeyPool 在已选模型端点内部完成粘性 Key 选择并原子提交目标 generation、Selection（其 ID 即 runtime event_id）和 `last_used_at`；事务提交后才允许把明文 Key 交给 Adapter。
8. 构造上游请求并发送。非流式 routing attempt 成功终态提交后、或流式 `StreamSession(completed)` 成功提交后，才以 Selection 冻结的完整目标 / generation 做 CAS warm 确认；失败终态按并发计数释放 pending reservation。

DLP、限流、预算、权限、attempt 或 stream 执行门在第 7 步之前阻断时，不得创建 Selection 或运行时 `cache.affinity_selected`。Selection 前没有持久使用者计数，任何请求都不得主动释放共享 route reservation；它只按 30 秒短租约失效，避免一个失败请求误清除另一个并发请求正在依赖的指针。路由 Decision 和短期 reservation 只表示“曾冻结端点”，不代表已经调用上游或预计命中。

### 2.2 model_endpoint_id 的本地映射

当前仓库没有独立 `model_endpoints` 表；`models.model_id` 是稳定本地 ID，但 `provider_model_id/upstream_model_name` 和 Provider `base_url` 可以原地修改，单独使用 model_id 会把旧 Binding 带入新的上游缓存域。

本期对外仍将 `models.model_id` 作为可审计 `model_endpoint_id`，内部另计算不可外发的 `endpoint_revision_digest`：域分隔 HMAC 输入至少包含 tenant、model_id、provider_id、provider type / auth type、规范化 base_url 和 provider_model_id。任一 Endpoint URL、Provider 或上游模型映射变化都会造成 miss；URL 和上游模型名不写 Binding、日志、API 或 Webhook。

亲和不能越过有效 endpoint revision：Fallback 选到不同模型时使用独立 revision；同一 model_id 的上游映射变化也不会复用旧域。

### 2.3 可信 data_class 来源

当前 GatewayAuthContext 没有可信 data_class；PRD-045 还明确禁止用任意 Header 或正文伪造分类。本期在每个 Alias 的 `CacheAffinity` 配置中保存服务端受控的 `data_class`：

- 配置由当前租户 admin 通过 Control API 写入。
- 一个 Alias 最多一条 CacheAffinity 配置；该分类代表此 Alias 承载的工作负载分类。一个 Alias 不能混用多个分类，混合分类必须拆成不同 Alias，否则静态分类会掩盖真实安全域。
- `status='enabled'` 时 data_class 必填，只允许 `^[a-z0-9][a-z0-9._-]{0,63}$` 的稳定机器 code。
- `status='disabled'` 时 data_class 可空；缺配置、禁用或空分类都直接关闭亲和。
- data_class 可在 PAGE-072 的配置区展示，但不得进入运行日志、Selection DTO、Webhook 或结构化进程日志。
- Virtual Key 仍是运行时隔离维度，所以同一 Alias 下不同 Key 永远不会共享 Binding。

该来源只服务 CacheAffinity，不成为通用请求分类，也不改变 Policy runtime context。

### 2.4 长前缀与精确指纹

仅 Chat Completions 的开头连续 `system` / `developer` 消息可参与指纹：

- 遇到首条 `user`、`assistant`、`tool` 或其他角色即停止收集。
- content 可以是字符串，或全部由 `text` / `input_text` 文本 part 组成的数组。
- 任一未知、图片、音频、文件或结构不完整 part 都关闭本次亲和，不能猜测 Provider 的序列化语义。
- leading message 只允许 `role/content/name`，content part 只允许对应方言的 `type/text` 精确字段；出现 extra key、非字符串 name/text、控制字符或孤立 Unicode surrogate 时关闭亲和。
- `tools`、`tool_choice`、`response_format` 和 `parallel_tool_calls` 存在时纳入 canonical；对象键按 Unicode code point 排序、数组顺序保留，null 与缺失保持不同。未知复杂结构、深度超过 16、节点超过 4096、tools 超过 128 或 canonical UTF-8 超过 512 KiB 时关闭亲和。
- canonical 值保留角色、消息边界、part 类型和文本原值；不 trim、不改大小写、不做 Unicode 等价化，也不做语义归一。采样参数和 user/assistant/tool 后缀不进入固定前缀摘要。
- 字符总数小于 `minimum_prefix_chars` 时关闭；超过 131072 Unicode code point 时关闭而不是截断，避免不同长 Prompt 因截断碰撞。
- Embeddings、空前缀、短前缀和结构不明确的请求继续正常调用，但不产生亲和事实。

`prompt_fingerprint` 使用 HMAC-SHA256 和独立域分隔字符串。根密钥来自新增 `METAHUB_CACHE_AFFINITY_HMAC_SECRET`，在 `AppConfig` 中必须 `exclude=True/repr=False`，配置摘要只能显示 masked；prod 必须显式配置至少 32 字符、不得使用默认值、不得等于 `auth_token_secret`。Secret 轮换只造成旧 Binding miss，不尝试双密钥兼容，也不影响登录 Token。数据库只保存小写 64 位摘要，不保存正文、长度或可逆内容。

### 2.5 cache_affinity_key

完整亲和键严格包含：

```text
tenant_id
virtual_key_id
data_class
model_endpoint_id (= model_id)
prompt_fingerprint
```

实现使用两层域分隔摘要：

- `affinity_scope_digest = HMAC(tenant_id, virtual_key_id, data_class, prompt_fingerprint)`，只用于找到 `cache_affinity_scopes` 分组和当前 endpoint reservation 指针。
- `affinity_key_digest = HMAC(scope_digest, model_endpoint_id, endpoint_revision_digest)`，对应架构完整五维键；同一 scope 的主端点、Fallback 和跨 Alias 端点分别拥有独立 Binding 行，不能原地覆盖彼此。

Binding 的唯一身份不能加入 Alias、cache_affinity_id 或配置 version，否则两个 Alias 在相同五维域下无法复用，且 no-op 配置更新会人为制造 miss。配置 ID / version 只进入 Selection 审计。两个 Alias 只有在 tenant、Virtual Key、可信 data_class、Prompt 和 endpoint revision 全部相同且各自启用时才可复用同一 Binding。

scope 的 `preferred_binding_id + preferred_lease_generation` 只是短期并发协调指针，不参与亲和身份，也不删除其他 endpoint Binding。指针目标不在当前 Alias 合格候选中时，路由器可继续检查该 scope 下其他合格 warm Binding；Fallback 成功不会覆盖 `fixed` 的主备硬顺序。

tenant、Virtual Key、Alias、data_class、model_id、Prompt 指纹任一缺失时，本次亲和上下文为 None；KeyPool 只能走原有选择算法，不读取任何 Binding。

### 2.6 预计命中与实际命中

- Binding 不保存会被最后一个 Alias 覆盖的全局 `warm_status/expires_at`。`last_success_at` 是该完整 endpoint / Credential target 最近成功终态；当前请求仅在 `as_of < last_success_at + 当前配置 ttl_seconds` 时把它解释为 warm，等号边界已过期。
- pending reservation 与 warm 证据正交：`pending_lease_generation/pending_lease_started_at/pending_lease_expires_at` 只在 30 秒内协调并发首批端点；复用 pending 时 `estimated_hit=false`。租约续订不能改写 `last_success_at`。
- `estimated_hit=true`：同一完整亲和键在当前 Alias TTL 下为 warm，绑定端点 / Credential 在本次仍可选，且 KeyPool 原样复用 Selection 冻结的 target generation。
- `estimated_hit=false`：首次建绑定、复用 pending、当前 TTL 已过期，或 Key 不可用后发生 target generation 重绑。
- 每次 Credential / `environment/none` 目标变化严格推进 `target_generation` 并清空 `last_success_at`；Endpoint revision 变化直接使用另一 Binding 行。
- 非流式 routing attempt 成功或流式 completed 已提交后，才把相同 Binding + target generation 的 `last_success_at` 推进。旧 generation、已淘汰 Binding 或目标已变化时 CAS no-op；存储失败只记录包含 request_id / trace_id 和固定错误码的 warning，绝不能改写已经确定的客户端成功结果。
- 握手失败、429、5xx、timeout、watchdog 或 client disconnect 不生成 warm 证据；最后一个同 lease generation 的活动 Selection 失败时，只有 `last_success_at IS NULL OR last_success_at < pending_lease_started_at` 才释放 scope 指针并使租约过期。任何同 target 的迟到成功发生在本轮 lease 起点之后都保留偏好；仍有并发 Selection 时由最后一个终态处理。
- 现有 `usage.cache_tokens` 是 Provider 方言归一后的“缓存相关 Token”；某些方言会合并 cache read 与 cache creation，不能统一宣称为实际命中。页面并列展示“预计复用”与“上游报告缓存相关 Token”，不从后者推导布尔命中。
- 预计命中不会直接减价。最终费用仍由 Provider usage、PricePlan、Usage Ledger 和现有成本逻辑计算。

### 2.7 Key 健康与重绑

亲和只是优先级，不是强制路由：

- 绑定 Key 必须仍为同租户、同 Provider、托管密文、active 或冷却已结束、健康探测未显示新鲜 unhealthy，并且能够成功解密。
- 禁用、仍在限流冷却、新鲜 unhealthy、跨 Provider、跨租户、密文损坏或已删除 Key 一律不得因亲和被使用。
- 绑定不可用时，KeyPool 使用现有 `last_used_at + weight + provider_credential_id` 确定性顺序选择新 Key，并在同一事务重绑。
- 一旦租户存在托管 Key，仍禁止回退环境变量；本期不能利用亲和绕过已有 KeyPool 治理。
- 环境变量 Key 或 `auth_type=none` 没有 Credential ID，只能记录同一 model endpoint / Provider 节点绑定，不伪造数据库 Key 身份。

### 2.8 并发与事务

本期明确使用两个 `BEGIN IMMEDIATE` 事务，端点先冻结、Key 后选择；二者通过不含敏感内容的 reservation handle 衔接：

1. 路由事务先执行原有算法，再读取 scope 指针及当前候选对应的 Binding。有效指针优先；否则按“当前 TTL warm、最近 `last_success_at`、当前 live pending、最近 `last_selected_at`、model_id”确定兼容偏好。最终端点的 Binding 缺失时创建；过期 pending 周期推进 lease generation 时必须原子把 `active_selection_count` 重置为 0，旧 lease Selection 不计入新周期。scope 指针、Decision 和 Candidate 一次提交。
2. 动态候选纯函数只可重排 `selected/standby`，所有 `rejected`、价格、质量、健康和统计字段原样保留。`availability_first` 不得让 unknown / stale 覆盖 fresh healthy；`quality_cost` 不得让 cold 或低于 floor 的候选覆盖 ready-pass。`cost_first` 可在合格候选内牺牲软成本排序换取亲和；`fixed` 完全不重排。
3. 发生重排时纯函数交换合格 Candidate 的最终 rank / status：升格 selected 与 `RoutingSelectionResult.selection_reason` 使用 `routing.cache_affinity_pending_preferred` 或 `routing.cache_affinity_warm_preferred`，降格行使用既有 `routing.candidate.standby`；0040 只扩展 Candidate reason CHECK，不新增列。candidate_summary 为每个候选保存 base rank/status/reason 与 final rank/status/reason，并在 `cache_affinity` 白名单保存配置 ID / version、`pending/warm`、`already_selected/reordered/tier_blocked`、最终 reason 和 base selection reason。禁止 scope、Binding、digest、data_class 或 Credential；价格 / 质量证据不可重算或丢失。
4. 路由事务之后的 DLP / 限流 / 预算 / attempt / stream gate 失败不创建 Selection，也绝不主动释放 route reservation；30 秒租约是 Selection 前唯一收敛机制。只有已经提交 Selection 的 Provider 终态才允许操作活动计数和当前指针。
5. KeyPool 事务必须先重验 CacheAffinity ID / version / enabled 配置，再重读 Binding、Endpoint、Provider 和 Key 图。绑定 Key 仍合格则复用，否则按现有确定性规则选择并推进 target generation；Selection 冻结 target / lease generation，`outcome_status=pending`，相同 lease generation 的活动计数加一，同时写 `last_used_at`。Route 与 Key 事务之间配置发生任何变化都在 Provider 前失败关闭。
6. Selection 使用 `(tenant_id, request_id, attempt_sequence)` 唯一键；同一 request / attempt 再次进入时恢复同一 Selection 和目标，不重复计数或发事件，意图不一致失败关闭。
7. KeyPool 单一函数负责连接、候选解密、Binding / Selection、`last_used_at`、commit 和 finally close；旧 `_record_key_selected` 变为不自行 commit 的 connection-local helper。commit 后才返回明文 Key，失败时必须丢弃内存明文。
8. Selection 提交后、Provider 调用前立即入队一次 `request_selected` runtime Webhook。队列故障不回滚已提交事实；warm / failure 确认不重复发该事件。
9. outcome CAS 与 lease CAS 必须解耦：任何 pending Selection 都可依据已提交 execution terminal 单向写 outcome。成功只要 target generation 仍一致，就可跨 lease 用 `max(existing, completed_at)` 更新真实 warm 证据；Selection 的 lease generation 仍是当前 generation 时才更新当前活动计数。失败也只在当前 lease 活动数归零且本轮起点之后没有同 target 成功时释放指针。旧 lease 不得改当前计数 / 指针，旧 target 不得 warm；事务失败整体回滚并写固定 warning。
10. 缺配置、disabled、短 / 不可规范化前缀属于“未启用”，可以走旧路径；一旦构造有效亲和上下文，路由 reservation 或 KeyPool Selection 事务失败均在 Provider 前返回 `gateway.cache_affinity_unavailable`。基础 KeyPool 无可用 Key 仍返回既有 `provider_key.unavailable`。

容量按 scope 而不是单 Binding 限制：每个 tenant 最多 100000 个 scope、每个 tenant + Virtual Key 最多 10000 个 scope、每个 scope 最多 8 个 endpoint Binding。每次最多处理 64 个候选 Scope：同一事务先 CAS 清除过期 / 悬空 preferred pointer，再删除无 live lease、无 pending Selection 且 `last_success_at IS NULL` 或 `last_success_at + 86400s <= now` 的安全 Binding，最后删除空 Scope。每 scope 8 行上限可只淘汰最旧安全 Binding；tenant / Key scope 上限必须真正删除空 Scope。容量仍满时按 `last_selected_at, id` 确定性 LRU；Selection 历史没有长期父 FK。无安全淘汰目标时 Provider 前 503。

Selection outcome 另有有界恢复，不能依赖清理器猜测：每次路由 / Key 事务最多惰性对账 64 条“execution gate 已终态但 outcome 仍 pending”的 Selection，并按同一终态规则补 warm / failure。单 Gateway 启动恢复在接流量前按稳定 ID 每批 64 行循环，直到没有遗留 pending：先按既有 succeeded / failed attempt 和 completed / 其他终态 stream 恢复 outcome 及可用 warm；再把旧 `routing_attempts.started` 原子推进 failed、旧 `stream_sessions.starting/active` 推进 upstream_failed，统一写 `gateway.execution_interrupted`、usage not_applicable / completed_at 且不产生质量观察；随后失败化对应 Selection。全部批次完成后，一个最终 `BEGIN IMMEDIATE` 按“当前 lease generation 的 pending Selection 数”重算每条 Binding.active_selection_count；计数为 0 且本轮起点后无成功时使 lease 过期并 CAS 清 scope pointer，已有同 target 成功则保留 warm 偏好。任一步失败都整体回滚当前批次并阻止 Gateway 启动 / ready；启动恢复是唯一可批量修复旧进程计数的特例，正常旧 lease 回调仍绝不改当前计数 / 指针。

清理只把当前进程仍 pending 的 Selection 视为活动；启动恢复后，遗留 started attempt 或 starting / active stream 不再单独阻止 Scope 淘汰。`BEFORE DELETE Binding` 在 scope 指针仍引用目标时必须拒绝，`BEFORE DELETE Scope` 在仍有 Binding 时必须拒绝；清理器先 CAS 清指针、再删安全 Binding、最后删空 Scope，默认 `recursive_triggers=0` 也不会形成悬空指针。

### 2.9 单节点 POC 与配置一致性

PRD-045 的 LKG 快照只覆盖 Policy，不包含 ModelAlias / CacheAffinity；当前 KeyPool 与动态路由也直接读取共享 SQLite。本期保持 M1 单节点 POC 边界：配置、Binding、路由 Decision 在同一开发数据库内以事务保持一致，不伪装成多节点分布式缓存。未来多 Gateway 节点必须把 CacheAffinity 配置和 Binding 纳入带版本的共享存储 / 快照同步，再声明跨节点粘性；该后续工作不能削弱本期 tenant / Key / data_class / endpoint 五维隔离。

本期运行合同只承诺一个 Gateway 节点、一个有效 HMAC Secret。不同 Secret 产生完全不同的 scope / Binding 摘要，只能导致隔离 miss 和额外可淘汰行，不能跨 tenant / Virtual Key 命中；使用不一致 Secret 的多实例粘性不在本期验收声明内。Secret 从 A 切到 B 后不兼容 A 域；若运维明确回滚为完全相同的 A，尚未淘汰且在当前 Alias TTL 内的 A 域 Binding 会确定性恢复可见，这是灾备回滚语义而非双密钥兼容。专项测试固定 A -> B miss、B -> A 恢复 / 过期边界，生产文档要求 Secret 变更与单节点重启同时执行。

## 3. 权限、租户与敏感信息边界

### 3.1 Control API

- `GET /model-aliases` 继续由 `require_model_catalog_reader` 保护；admin/developer 可读取当前租户 CacheAffinity 摘要，viewer 后端 403。
- `PATCH /model-aliases/{id}/cache-affinity` 使用 `require_current_admin`；依赖必须在读取正文前完成。
- 服务层所有 Alias / 配置查询必须显式包含认证上下文 `tenant_id`。猜测其他租户 Alias ID 与不存在 ID 返回同一 404，禁止枚举。
- admin 是当前 RBAC 下的财务配置代理；developer 只读，viewer 无导航、无 DOM、无预取。
- 前端禁用或隐藏控件仅改善体验，不能代替后端 RBAC。

### 3.2 Gateway

- tenant_id、virtual_key_id、environment 和 Alias 身份全部来自已认证 Virtual Key 与租户限定路由结果。
- 客户端不能提交 cache_affinity_id、data_class、fingerprint、Binding、Provider Credential 或 expected_hit。
- Direct Model、Policy route_to、无持久 Alias 配置、禁用配置、短 Prompt 全部回到原有路径。
- Fallback 使用相同可信 tenant / Virtual Key / data_class / Prompt 指纹，但以备用 `model_id` 和 attempt_sequence=2 形成独立键。

### 3.3 日志、API 与 Webhook 禁止项

下列内容不得进入应用日志、API DTO、Webhook payload、审计 metadata 或浏览器 DOM：

- Prompt / Response、canonical prefix、Prompt 长度、prompt_fingerprint、affinity_key_digest。
- Authorization、Virtual Key 明文、Provider Key 明文 / 密文 / fingerprint。
- 运行 Selection 的明文 data_class。
- Provider base_url、上游 Header 或原始错误正文。

配置 API 可以向有权限的当前租户用户返回 Alias 配置中的 data_class；日志详情只返回请求级 Selection ID、配置 ID / 版本、预计命中、稳定原因、模型、Provider 和非稳定目标类型。不得返回 Key prefix、Credential ID、Binding ID 或任何可跨请求建立 Credential / Prompt 关联图的值。

## 4. 涉及模块

| 模块 | 设计职责 |
| --- | --- |
| `infra/migrations/0040_prompt_cache_affinity_routing.sql` | 保真重建 routing_candidates 并扩展两个 reason；新建配置、scope、完整 endpoint Binding、运行选择和配置幂等事件表；补租户 / REPLACE 守卫、容量索引与审计枚举 |
| `infra/migrations/0040_prompt_cache_affinity_routing.down.sql` | 非空业务事实 preflight guard；仅空事实库可保真恢复 0039 Candidate / Audit 合同 |
| `shared-sdk/python/metahub_shared/runtime/config.py`、`.env.example` | 独立 HMAC Secret 的 exclude / prod 校验和安全示例 |
| `shared-sdk/python/metahub_shared/runtime/cache_affinity.py` | 有界 canonical、版本化 HMAC 指纹、scope / endpoint digest 和 repr-safe 类型 |
| `shared-sdk/python/metahub_shared/runtime/errors.py` | Control 配置统一错误码及 Gateway 亲和执行门故障建议 |
| `shared-sdk/python/metahub_shared/runtime/webhooks.py` | 精确校验 request_selected / configuration_changed 两种事件信封 |
| `gateway-data-plane/src/metahub_gateway/cache_affinity/` | 事务内 scope / Binding reservation、显式请求上下文、Selection 终态和 warm CAS |
| `gateway-data-plane/src/metahub_gateway/app.py` | 在接流量前安装单节点遗留 execution gate / Selection 恢复；恢复失败使 lifespan 启动失败 |
| `gateway-data-plane/src/metahub_gateway/routing/models.py` | 在当前合格 Candidate 内应用 pending / warm endpoint 偏好并原子冻结安全 reason；GatewayRoute 不携带亲和摘要 |
| `gateway-data-plane/src/metahub_gateway/providers/keypool.py` | 单事务 Key 选择、Binding 复用 / 重绑、Selection、提交与 finally close |
| `gateway-data-plane/src/metahub_gateway/providers/base.py` | ProviderRequest 仅在内存携带待入队安全事件与 Selection handle |
| `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py` | 统一选择 Provider access，不把亲和元数据写入上游 payload / Header |
| `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 显式传递 repr-safe context；重排 stream gate；Selection 后 / Provider 前入队一次事件，成功终态后只确认 warm |
| `control-plane-api/src/metahub_control/model_aliases/` | CacheAffinity DTO、严格 PATCH、CAS、幂等恢复、审计和配置变更事件 |
| `control-plane-api/src/metahub_control/request_logs/` | 读取同租户 Selection 的历史安全快照，不实时回算 Credential 标签 |
| `control-plane-api/src/metahub_control/audit/` | 接受 CacheAffinity 配置变更的固定 action / object / metadata 白名单 |
| `console-web/src/lib/modelAliases.js` | CacheAffinity PATCH 与统一错误解析 |
| `console-web/src/features/routing-policies/` | PAGE-072 配置状态、admin 编辑、developer 只读和竞态安全 |
| `console-web/src/features/request-logs/` | PAGE-073 预计复用与上游缓存相关 Token 对照 |
| `scripts/acceptance/prd050-runtime-acceptance.py` | 真实迁移、配置、同 Prompt 粘性、隔离、事件、日志与隐私验收 |
| `scripts/acceptance/prd050-browser-acceptance.mjs` | 三角色三视口真实 UI、RBAC、响应竞态与视觉验收 |

## 5. 数据库设计

### 5.1 `cache_affinities`

| 字段 | 类型与约束 | 说明 |
| --- | --- | --- |
| `cache_affinity_id` | TEXT PK，`caf_<32hex>` | 配置稳定 ID |
| `tenant_id` | TEXT NOT NULL | 强制租户边界 |
| `model_alias_id` | TEXT NOT NULL | 一个 Alias 最多一条配置 |
| `status` | `enabled/disabled` | 启停状态 |
| `data_class` | TEXT NULL | enabled 时必填的机器 code |
| `minimum_prefix_chars` | INTEGER 256..16384 | 长前缀启用门限，默认 1024 |
| `ttl_seconds` | INTEGER 60..86400 | Binding 有效期，默认 3600 |
| `version` | INTEGER >=1 | 独立 CAS 版本，不推进 RoutingPolicy.version |
| `created_by/updated_by` | TEXT NOT NULL | 认证 Session 用户 |
| `created_at/updated_at` | TEXT NOT NULL | UTC 时间 |

约束：`UNIQUE(tenant_id, model_alias_id)`；触发器校验 Alias 和 actor 均属于同一 tenant。`enabled` 且 data_class 为空或不合机器 code 时数据库拒绝，不能只信服务层。

### 5.2 `cache_affinity_scopes`

Scope 是不含 endpoint 的四维安全分组；它允许同一 Prompt 域保留主端点、Fallback 和跨 Alias 的多条完整 Binding：

| 字段 | 类型与约束 | 说明 |
| --- | --- | --- |
| `cache_affinity_scope_id` | TEXT PK，`cafs_<32hex>` | 可淘汰内部 scope ID |
| `tenant_id` | TEXT NOT NULL | 强制租户边界 |
| `virtual_key_id` | TEXT NOT NULL | 可信 Virtual Key 维度 |
| `digest_schema_version` | INTEGER = 1 | canonical / HMAC 合同版本 |
| `affinity_scope_digest` | TEXT NOT NULL | tenant + Key + class + Prompt 的 HMAC |
| `preferred_binding_id` | TEXT NULL | 当前并发 reservation 指针；无长期 FK |
| `preferred_lease_generation` | INTEGER NULL | 防止旧释放回调清除新指针 |
| `last_selected_at` | TEXT NOT NULL | 容量淘汰的稳定排序时间 |
| `created_at/updated_at` | TEXT NOT NULL | 生命周期时间 |

唯一键为 `(tenant_id, virtual_key_id, digest_schema_version, affinity_scope_digest)`。tenant / Virtual Key / schema / digest 不可 UPDATE；指针写入时触发器必须验证目标 Binding 属于同 tenant + scope 且 lease generation 相等。Scope 不保存 Alias、配置 ID、data_class 或 Prompt 属性，Selection 对它不设长期 FK。

### 5.3 `cache_affinity_bindings`

Binding 是“一个 scope + 一个完整 endpoint revision”的可淘汰运行状态；Endpoint 身份不可原地改写：

| 字段 | 类型与约束 | 说明 |
| --- | --- | --- |
| `cache_affinity_binding_id` | TEXT PK，`cab_<32hex>` | 当前完整五维 Binding ID |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `cache_affinity_scope_id` | TEXT NOT NULL | 四维 scope；父 scope 删除时一起清理 |
| `affinity_key_digest` | TEXT NOT NULL | scope + endpoint revision 的完整 HMAC |
| `model_endpoint_id` | TEXT NOT NULL | 可审计 models.model_id |
| `endpoint_revision_digest` | TEXT NOT NULL | URL / 上游模型映射的不可逆版本域 |
| `provider_id` | TEXT NOT NULL | 当前 endpoint Provider |
| `provider_credential_id` | TEXT NULL | 托管 Key 快照；不设长期 FK |
| `target_kind` | `unassigned/managed/environment/none` | 不含稳定 Key 标签的目标类型 |
| `target_generation` | INTEGER >=1 | Credential / target ABA 防护版本 |
| `pending_lease_generation` | INTEGER >=1 | 冷并发 reservation 版本 |
| `pending_lease_started_at` | TEXT NOT NULL | 失败释放与迟到成功的比较起点 |
| `pending_lease_expires_at` | TEXT NULL | 固定 30 秒短租约；不代表 warm |
| `active_selection_count` | INTEGER >=0 | 当前 lease generation 的非终态选择数 |
| `last_success_at` | TEXT NULL | 当前 target 最近成功终态；唯一 warm 时间证据 |
| `selection_count` | INTEGER >=0 | 绑定被选次数 |
| `estimated_hit_count` | INTEGER >=0 | 当前 TTL 下 warm 复用次数 |
| `last_selected_at` | TEXT NOT NULL | 最近 Selection / reservation 时间 |
| `created_at/updated_at` | TEXT NOT NULL | 生命周期时间 |

同时约束 `UNIQUE(tenant_id, cache_affinity_scope_id, model_endpoint_id, endpoint_revision_digest)` 和 `UNIQUE(tenant_id, affinity_key_digest)`。scope / endpoint / revision / Provider 身份不可 UPDATE；endpoint 变化必须新建另一行。Credential 或 target_kind 变化必须 `target_generation+1`、清空 `last_success_at`、开启新 lease；只续同一目标的过期 lease仅推进 lease generation，但两种推进都必须把属于旧 lease 的 `active_selection_count` 原子重置为 0。

Credential 不设长期 FK，避免物理删除与历史 / 并发 CAS 冲突；写目标和每次使用时都通过 tenant + Provider 关系触发器 / 服务复验，缺失或失效即按 ineligible 重绑。Binding -> Scope 使用 `ON DELETE RESTRICT`；Binding 删除前必须 CAS 清 preferred pointer，Scope 只在 Binding 全空且没有 pending Selection 时删除。直接删除当前指针 Binding 或非空 Scope 的触发器必须拒绝。PK、两个唯一键、rowid、UPDATE OR REPLACE 及 scope 指针都覆盖默认 `recursive_triggers=0` 的 REPLACE 旁路。

### 5.4 `cache_affinity_selections`

Selection 在 Provider 前提交；选择身份和快照不可变，仅 `outcome_status` 允许一次受约束的 pending -> succeeded / failed CAS：

| 字段 | 类型与约束 | 说明 |
| --- | --- | --- |
| `cache_affinity_selection_id` | TEXT PK，`cas_<32hex>` | 同时作为 runtime Webhook event_id |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `request_id/trace_id` | TEXT NOT NULL | 请求链路 |
| `attempt_sequence` | INTEGER 1..2 | 主 / 备选择 |
| `routing_attempt_id/stream_session_id` | TEXT 二选一 | 已提交的非流 / 流执行门 |
| `cache_affinity_id/version` | TEXT / INTEGER | 本次 Alias 配置快照 |
| `cache_affinity_scope_id_snapshot` | TEXT NOT NULL | 仅库内 CAS / 调试；无长期 FK |
| `cache_affinity_binding_id_snapshot` | TEXT NOT NULL | 仅库内 CAS / 调试；无长期 FK |
| `model_alias_id/model_endpoint_id` | TEXT NOT NULL | 本次 Alias 与实际端点 |
| `endpoint_revision_digest_snapshot` | TEXT NOT NULL | 仅库内完整目标 CAS；不进 DTO / 日志 |
| `provider_id/provider_code_snapshot` | TEXT NOT NULL | 选择时安全 Provider 快照 |
| `provider_credential_id_snapshot` | TEXT NULL | 仅库内目标 CAS；无 FK、不进入 DTO / Webhook |
| `target_kind/target_generation` | TEXT / INTEGER | 选择时完整目标与 ABA 版本 |
| `pending_lease_generation_snapshot` | INTEGER NOT NULL | 失败释放只能作用于同一租约 |
| `ttl_seconds_snapshot` | INTEGER NOT NULL | 解释选择时 warm 边界，不回写 Binding |
| `warm_evidence_at_snapshot` | TEXT NULL | 选择时 last_success_at 时间证据 |
| `endpoint_preference_applied/state` | BOOLEAN / `none/pending/warm` | 是否重排及偏好状态 |
| `binding_state_before` | `missing/pending/warm/expired/ineligible` | 选择前派生状态 |
| `selection_reason` | 固定枚举 | binding_created / binding_pending_reused / binding_warm_reused / binding_rebound |
| `estimated_hit` | BOOLEAN | 仅 warm_reused 且 target 未变为 true |
| `outcome_status/outcome_at` | `pending/succeeded/failed` / TEXT NULL | Provider 终态 CAS |
| `created_at` | TEXT NOT NULL | 选择提交时间 |

`UNIQUE(tenant_id, request_id, attempt_sequence)`；插入触发器要求 Gateway Claim 的 tenant、trace、Virtual Key、配置、Alias、Model、Provider、Binding 和已提交 execution gate 全部一致，并验证 `estimated_hit` 与 warm / target 快照。Scope、Binding、Config 和 Credential 都不设长期历史 FK，因此父状态淘汰 / 删除后 DTO 不漂移。

除 outcome 两列外，UPDATE / DELETE 及 PK / request 唯一键的 INSERT OR REPLACE / UPDATE OR REPLACE 全部拒绝；outcome 只允许同租户事务由 pending 单向变为一个终态，重复同终态幂等，反向或互换拒绝。终态恢复可以跨 lease 写 outcome；只有 target generation 一致的 succeeded 可写 warm，只有 lease generation 仍是当前值才可改当前活动计数 / 指针。Selection 不保存 Prompt 指纹、scope / affinity digest、data_class 或 Key prefix；所有 snapshot ID / revision 只留数据库，不进入 RequestLog DTO、Webhook、进程日志或 DOM。

### 5.5 `cache_affinity_config_events`

配置写入使用独立不可变事件支持 `X-Request-ID` 幂等恢复：

- ID：`cae_<32hex>`；真实 created / updated 同时作为 configuration_changed Webhook event_id。
- 字段：tenant、cache_affinity、model_alias、配置版本、actor_user_id、operation_request_id、trace_id、request_fingerprint、event_type、before_json、after_json、created_at。
- event_type 固定为 `created/updated/noop`；no-op 也消费 request_id 并写事件，但不升版本、不写变更 Audit、不投递 Webhook。
- `UNIQUE(tenant_id, actor_user_id, operation_request_id)`。
- 相同 actor + request_id + fingerprint 只有在当前配置仍等于事件 after/version 时恢复原结果；配置后来已演进则返回统一 `version_conflict`，错误体不携带领域快照。调用方必须重新读取当前租户 `GET /model-aliases`、显式 rebase，并用新的 request_id 与最新 expected_version 提交，避免旧 200 覆盖页面。不同 fingerprint 固定 409。
- before/after 只包含 status、data_class、minimum_prefix_chars、ttl_seconds、version；不包含 Prompt、Key 或 Endpoint。
- UPDATE、DELETE、PK / 唯一 request 键的 INSERT OR REPLACE / UPDATE OR REPLACE 由直接守卫拒绝。

### 5.6 审计与回滚

- `audit_logs` 新增 `cache_affinity.created`、`cache_affinity.updated` action 和 `cache_affinity` object_type。
- Audit、ConfigEvent、配置行在同一事务提交；任何一步失败全部回滚。配置表自身也必须有 PK、tenant+Alias 唯一键、rowid 的 REPLACE 冲突守卫。
- changed 配置提交后以同一 cae_ ID 投递 configuration_changed；no-op 不制造状态变化事件。
- 0040 down 在任何配置、Scope、Binding、Selection（含 outcome）、ConfigEvent、相关 Audit、affinity Candidate / Decision、`cache.affinity_selected` subscription、delivery 或 delivery attempt 存在时，必须用固定 `prd050_down_business_facts_present` 在任何 DDL / 数据变更前原子拒绝。仅完全空事实库允许恢复 0039 Candidate / Audit CHECK；不得静默删除生产事实。
- 空事实 up/down/up、非空 down 拒绝、默认 recursive_triggers=0 REPLACE 防护和 0039 历史保真都必须专项测试。

### 5.7 `routing_candidates` 兼容重建

现有 0039 `routing_candidates.reason_code` 有严格枚举，不能直接写亲和最终原因。0040 up 必须按 0039 当前全列顺序保真重建该表，唯一变化是 reason CHECK 增加 `routing.cache_affinity_pending_preferred` 和 `routing.cache_affinity_warm_preferred`；不新增字段，不改变历史行的 ID、rank、status、reason、价格、质量、版本、时间或 NULL 语义。

重建后逐个恢复 0039 的 tenant owner graph、quality graph、immutable / no-delete、PK / UNIQUE / rowid REPLACE 守卫及全部索引；迁移在提交前比较源 / 目标总数、每列 NULL 计数和全列双向 `EXCEPT` 差集，任一不一致通过 guard CHECK 使事务整体回滚。新 reason 只允许 `status='selected' AND rank=1`；降格 standby 仍使用既有枚举，避免 RequestLog / Console 出现 final status 与 base reason 混搭。

0040 down 的 preflight 必须先检查新 reason Candidate、含 `candidate_summary.cache_affinity` 的 Decision 及 5.6 所列全部事实；有任一行即在 DDL 前拒绝。仅空事实库可按相同保真程序重建回 0039 reason CHECK 并恢复全部 PRD-048 索引 / trigger，随后再删除 PRD-050 空表和恢复 Audit CHECK。

## 6. API 设计

### 6.1 `GET /model-aliases`

既有响应的每个 `model_alias` 新增：

```json
{
  "cache_affinity": {
    "cache_affinity_id": "caf_...",
    "tenant_id": "tenant-id",
    "model_alias_id": "alias-id",
    "status": "enabled",
    "data_class": "internal",
    "minimum_prefix_chars": 1024,
    "ttl_seconds": 3600,
    "version": 3,
    "created_at": "...",
    "updated_at": "..."
  }
}
```

无配置时为 `null`。不返回 actor、Binding、fingerprint、digest、Key 数量或 Provider Credential。

### 6.2 `PATCH /model-aliases/{model_alias_id}/cache-affinity`

请求示例：

```json
{
  "status": "enabled",
  "data_class": "internal",
  "minimum_prefix_chars": 1024,
  "ttl_seconds": 3600,
  "expected_version": 0
}
```

合同：

- 新建必须 `expected_version=0`；已存在配置必须精确等于当前 version。
- 请求是完整配置快照，五个字段全部必填；禁用时允许 `data_class=null`。
- 使用认证后的 8 KiB 有界严格 JSON reader：只接受 UTF-8 `application/json` object，拒绝重复键、extra key、控制字符、深层 / 过宽结构、bool 冒充整数和非法枚举。
- no-op 返回 200、`changed=false`、版本不变并写不可变 noop 幂等事件；真实变化严格 `version+1`。
- `X-Request-ID` 同 actor / 同意图在当前配置仍等于原 after/version 时安全重放；后续版本已演进则返回统一 409，不在错误体返回当前配置。Console 必须通过同租户列表 GET 刷新、由用户显式采用最新值，再以新 request_id 提交；同 ID 不同意图固定 409。
- 响应：`cache_affinity`、`changed`、顶层 `request_id/trace_id`。
- admin dependency 必须先于正文读取；developer/viewer 的攻击正文不能影响 403 结果。

### 6.3 `GET /request-logs/{request_id}` 扩展

`request_log` 新增 `cache_affinity_selections` 数组，最多两项：

```json
{
  "cache_affinity_selection_id": "cas_...",
  "cache_affinity_id": "caf_...",
  "cache_affinity_version": 3,
  "attempt_sequence": 1,
  "selection_reason": "binding_warm_reused",
  "estimated_hit": true,
  "endpoint_preference_applied": true,
  "target_kind": "managed",
  "model_id": "model-id",
  "provider_id": "provider-id",
  "provider_code": "deepseek",
  "created_at": "..."
}
```

查询必须绑定认证 tenant + request；跨租户 request_id 沿用 `request_log.not_found`。详情不返回 data_class、Prompt 指纹、digest、Credential ID、Key prefix、Binding ID 或稳定目标引用；真实验收在内存假上游比较 Authorization 后只保存布尔结论。

### 6.4 Webhook `cache.affinity_selected`

事件有两种精确 trigger。runtime `request_selected` 的 event_id 等于已提交 Selection ID：

```json
{
  "trigger": "request_selected",
  "request_id": "req-...",
  "cache_affinity_selection_id": "cas_...",
  "cache_affinity_id": "caf_...",
  "cache_affinity_version": 3,
  "model_alias_id": "alias-id",
  "model_endpoint_id": "model-id",
  "selected_provider_id": "provider-id",
  "target_kind": "managed",
  "selection_reason": "binding_warm_reused",
  "estimated_hit": true
}
```

Control 配置真实变化使用 ConfigEvent 的 cae_ ID：

```json
{
  "trigger": "configuration_changed",
  "request_id": "req-config-...",
  "cache_affinity_id": "caf_...",
  "cache_affinity_version": 3,
  "model_alias_id": "alias-id",
  "change_kind": "updated",
  "changed_fields": ["status", "ttl_seconds"],
  "status": "enabled"
}
```

- request_selected 的 `actor_id=null`；configuration_changed 的 actor 是认证 admin。
- 两种信封均 `object_id=cache_affinity_id`、`trace_id` 必填；changed_fields 按固定业务顺序去重。
- 不外发 Virtual Key ID、data_class、Prompt 摘要、Provider Credential、Key prefix、Endpoint URL、digest 或成本。
- Selection / ConfigEvent 提交后再异步投递；队列满 / Webhook 失败不回滚已提交事实或 Provider 结果。noop 不投递。

## 7. 统一错误码

| HTTP | 错误码 | 场景 |
| --- | --- | --- |
| 401 | 现有 `auth.*` | Control Session 无效 |
| 403 | `auth.permission_denied` | developer/viewer 写配置，或 viewer 读取 |
| 404 | `model_alias.not_found` | 当前租户 Alias 不存在；跨租户同码防枚举 |
| 409 | `cache_affinity.version_conflict` | expected_version 与当前配置不一致 |
| 409 | `cache_affinity.request_conflict` | 同 actor + X-Request-ID 对应不同意图 |
| 422 | `cache_affinity.input_invalid` | wire、字段、类型、data_class、门限或 TTL 非法 |
| 503 | `cache_affinity.update_failed` | 配置、事件或审计事务失败 |
| 503 | `gateway.cache_affinity_unavailable` | 有效亲和上下文的图、锁、Binding / Selection / commit 任一步无法安全完成 |
| 503 | 现有 `provider_key.unavailable` | 基础 KeyPool 无可用 Key |
| 503 | `gateway.execution_interrupted` | 仅用于单节点启动恢复给旧 execution gate 写固定本地中断事实，不作为新请求的 Provider 错误 |

所有错误由统一 ApiError 生成，响应包含 `request_id/trace_id`，不回显 SQL、路径、正文、摘要或 Key。405、404、503 也必须保持统一错误体与链路头。

## 8. 前端页面与交互

### 8.1 PAGE-072 路由策略页

复用现有 `routing-policies` 导航，不新增重复路由页面：

- 在每个 Alias 行的 RoutingPolicy 区之后增加未嵌套卡片的“Prompt Cache 亲和”配置行。
- admin 使用 checkbox / toggle 启停、文本输入 machine code、数值 stepper 设置最小前缀、select 选择 5 分钟 / 1 小时 / 6 小时 / 24 小时 TTL，并使用保存 / 取消命令。API 返回 60..86400 内非预设 TTL 时，select 必须注入只代表当前值的“自定义 N 秒”安全 option 并原样保留；用户选择预设前，编辑其他字段不得暗改 TTL。
- developer 只看到只读状态、分类 code、门限、TTL、版本和更新时间。
- viewer 不生成 PAGE-072 DOM，不发送 ModelAlias 请求；手写 API 仍由后端拒绝。
- 新配置以 expected_version=0；保存 ticket 绑定 tenant、user、role、Session、Alias、草稿 revision、base version、operation generation 和 request_id。
- 身份 / 角色 / Session / 导航变化、取消或登出要 Abort 并推进 generation；迟到 GET / PATCH 不能覆盖新身份。
- 409 展示版本冲突并要求显式载入最新值，不做 last-write-wins。
- 请求结果未知时，仅同一意图复用原 request_id；修改草稿后生成新 request_id。

### 8.2 PAGE-073 日志详情

- 在反馈区之后、长路由证据之前展示“Prompt Cache 亲和”。
- 每次 attempt 显示预计命中 / 新建 / 重绑、配置版本、模型、Provider 和非稳定目标类型；不得显示 Key prefix 或 Credential / Binding 标识。
- Usage 区继续展示实际 `cache_tokens`；页面明确区分“预计复用绑定”和“上游实报缓存 Token”。
- 无 Selection 时显示未启用 / 不满足长前缀条件的中性状态，不猜测原因或展示 Prompt 属性。
- 所有动态值用 textContent / escapeHtml；不得把响应对象插入可执行 HTML。

### 8.3 响应式与可访问性

- 1280x720、900x800、390x844 三档均不得横向溢出、重叠或截断关键操作。
- 开关、输入、select、保存状态、冲突与错误都有明确 label / aria 关系；键盘可以完成编辑。
- 固定表格 / 工具栏尺寸，保存中、错误和长 ID 不得导致布局跳动。
- 不在页面展示教学文案、Prompt、Secret 或测试捷径。

## 9. 测试设计

### 9.1 纯函数与 shared 单元测试

1. 相同 leading system/developer 结构产生稳定 golden HMAC；user 内容变化不影响。
2. 角色、消息边界、part 类型、文本或 Unicode code point 任一精确变化产生不同指纹。
3. 短前缀、超长前缀、未知 part、Embeddings、空前缀返回 disabled。
4. tenant、Virtual Key、data_class、model_id、指纹任一变化产生不同 affinity digest；任一缺失不生成。
5. 原始 Prompt、长度和 digest 不出现在 repr、日志 DTO 或 Webhook payload。
6. Webhook 两种 trigger 的精确字段、runtime `cas_` 与配置 `cae_` event ID、actor/object/trace 绑定和敏感字段递归拒绝。
7. `apply_cache_affinity_endpoint_preference` 对 fixed / rejected / availability health tier / quality ready-cold-floor 的表驱动测试；原价格、质量、健康、公式版本和依赖执行标志逐字段不变。
8. A / B Secret 产生隔离摘要；A -> B 必 miss，明确回滚 A 时只在当前 TTL 内恢复 A 域，等号过期不恢复；测试只在单 Gateway 写节点执行。

### 9.2 Control API 集成测试

1. admin 以 expected_version=0 创建，再按版本更新、禁用、启用；no-op 不升版本。
2. developer/viewer/匿名 PATCH 在正文解析前 403/401；GET 权限按既有目录执行。
3. 跨租户 Alias ID 与不存在 ID 同 404；数据库无跨租户写入。
4. 非法 media type、UTF-8、JSON、重复 / extra key、bool 整数、data_class、门限、TTL、深度、节点数均固定 422。
5. 相同 request_id + 同指纹重放原结果；不同指纹 409；不同 actor 不互相恢复。
6. CAS 冲突只返回统一 409 错误体和 `request_id/trace_id`，错误体不返回当前配置；客户端随后重新读取同租户 `GET /model-aliases`、由用户显式 rebase，并使用新的 request_id 与最新 expected_version 提交。
7. Config、ConfigEvent、Audit 任一步注入失败时整体回滚。
8. 405/404/503 仍有统一错误体、request_id/trace_id 与 CORS 合同。

### 9.3 Gateway / KeyPool 集成测试

1. 冷 scope 的首个 route reservation 提交后，N 个并发请求使用同一 endpoint / Credential；只有一个 scope + endpoint Binding，全部 `estimated_hit=false`，成功后的下一次才 warm_reused。
2. Selection 前准入失败绝不释放共享 reservation；pending lease 到期 / 续租、最后 Selection 失败释放、并发活动数、跨 lease 成功和旧 target 成功分别覆盖。
3. pending / warm endpoint 可在动态模式中偏好；availability 不用 unknown 覆盖 healthy，quality_cost 不用 cold / below-floor 覆盖 ready，fixed 保持主备硬顺序；升格 Candidate / Decision 使用 affinity reason、降格使用 standby，candidate_summary 保留原算法证据。
4. 同 scope 的主端点、Fallback 和跨 Alias 不同 endpoint revision 形成多条 Binding 且互不覆盖；五维域完全相同的两个 Alias 可共享同一 Binding。
5. 短 TTL Alias 判定 expired 与长 TTL Alias 同时判定 warm；只按 `last_success_at + 当前请求 ttl` 读取，任何调用都不写共享 expires_at。
6. tenant、Virtual Key、data_class、endpoint revision、Prompt 任一变化都不复用；Provider base_url、provider_id、auth type 或 provider_model_id 原地变化产生新 revision miss。
7. 缺配置、disabled、空 data_class、短 / 非结构化 Prompt、Direct Model、route_to 不读取 scope / Binding、不写 Selection。
8. 绑定 Key disabled、rate_limited、fresh unhealthy、删除或解密失败时按旧排序重绑并推进 target generation；托管 Key 存在时不回退环境变量。
9. generation 1 请求迟到成功不能 warm 已重绑的 generation 2；同代成功乱序只推进 `max(last_success_at)`，重复终态幂等。
10. 非流式只有已提交 succeeded routing attempt、流式只有已提交 completed StreamSession 才 warm；握手成功、active、失败、timeout、watchdog 和 disconnect 都不 warm。
11. Provider 已成功且执行终态已提交后，warm CAS 0 行或存储异常不改写客户端结果，只产生含 request_id / trace_id 的固定脱敏 warning。
12. DLP、限流、预算、能力、attempt / stream gate 失败无 Selection / runtime Webhook；路由 reservation 只按短租约过期，不能主动释放或产生预计命中。
13. Selection 在 Provider 前提交并只发一次 request_selected；warm / failure 终态不重复发事件。Route 与 Key 事务间配置变化必须 Provider 前 503。
14. 有效亲和事务任一步故障完整回滚并在 Provider 前统一 503；基础 KeyPool 故障保持原码。
15. 租户 100000 / tenant+Key 10000 scope、每 scope 8 Binding、NULL 成功时间、64 Scope 上限、清指针 -> 删 Binding -> 删空 Scope、确定性 LRU 与无安全淘汰目标 503 全部覆盖。
16. A -> B Secret 必 miss、B -> A 按冻结回滚语义恢复或过期；多 Gateway 不一致 Secret 明确不作为本期粘性验收，不宣称跨节点一致。
17. 终态 outcome 惰性对账和 `app.py` 单节点启动恢复按 64 行批次循环至空：断言 gate 与 Selection 都成为终态，旧 started / starting / active 固定写 `gateway.execution_interrupted` 且不写质量观察；最终重算“generation 仍相同且 count>0”的 active count并按成功证据保留 / 清除 pointer。注入任一步失败时 Gateway lifespan 启动失败且不 ready。
18. cache_tokens 原样保持“缓存相关 Token”；estimated_hit 不改 Token / 成本；结构化日志不含 Prompt、digest、data_class、Key、Authorization、Endpoint URL 或上游模型名。

### 9.4 日志详情与 Console 测试

1. admin/developer 只能读取当前租户 Selection；viewer 403，跨租户 request 404。
2. DTO / DOM 显示预计命中、原因、目标类型、模型和 Provider；不显示 Key prefix、Credential / Binding ID、data_class、digest 或 Prompt 属性。
3. Routing page 新建、保存、noop 幂等恢复、后续演进冲突、409 rebase、网络结果未知重试、取消、Abort、身份 / 导航变化和迟到响应完整覆盖。
4. developer 只读、viewer 零 DOM / 零预取；后端权限测试独立存在。
5. 三视口真实 Chrome 覆盖 loading、empty、configured、editing、saving、conflict、error、log detail pending / warm / rebound。
6. 页面无横向溢出、重叠、裁切、敏感文本、外部请求或未知 Console error。

### 9.5 迁移、全量和运行验收

1. 0040 新库 up、0039 存量库 up、空事实 up/down/up、非空事实 down 原子拒绝、重复 up、失败回滚和 checksum 漂移。
2. routing_candidates up/down 全列逐行保真、全部 PRD-048 索引 / trigger 恢复、新 reason 仅 selected rank1、降格 standby 和 RequestLog / DOM 最终原因一致。
3. 默认 recursive_triggers=0 下直接 SQL 覆盖跨租户 / 父对象、immutable UPDATE / DELETE、PK / UNIQUE / rowid 的 INSERT OR REPLACE 与 UPDATE OR REPLACE。
4. Python 根级全量、Node 示例、Console 全量、Console lint、ruff、compileall、中文注释检查全部通过。
5. 使用独立验收数据库启动 Gateway 8000、Control 8001、Console 5173 和本地上游；三服务健康 200。
6. Runtime 验收真实创建两个 Key，配置 CacheAffinity，发送相同 / 不同维度 Prompt，核对上游内存 Authorization 布尔一致性、Selection、两种 Webhook、RequestLog、Usage 和隔离；制品不保存 Key / prefix。
7. Browser 验收使用真实登录和 API，不 mock；覆盖 admin/developer/viewer 与三视口截图。
8. 停服后执行日志 JSON / 生命周期白名单、隐私扫描、SQLite checkpoint，并核对开发库从 38/0038 实际应用 0039 与 0040 后达到 40/0040、integrity、FK、WAL/SHM、端口和最终 SHA。
9. 最终数据库 checkpoint 后禁止再次由 SQLite 打开；最终批次证据不得被失败重跑覆盖。

## 10. 代码修改计划

1. 完成本设计两轮独立自审并修正阻断项。
2. 新增 shared CacheAffinity 纯函数、独立 HMAC 配置、两类 Webhook 合同与错误码测试。
3. 编写 0040 正反迁移，保真重建 routing_candidates 的 reason CHECK 与全部索引 / trigger，并补 REPLACE / down guard 及迁移专项测试。
4. 实现 Control CacheAffinity DTO、严格 PATCH、CAS / noop 幂等、审计和 Alias 读取扩展。
5. 在现有 RoutingPolicy 冻结事务内实现 pending / warm endpoint 软偏好，并显式传递 repr-safe 请求上下文。
6. 实现 KeyPool 单事务粘性、Selection、stream gate 重排；Selection 提交后 / Provider 前入队 runtime event，执行终态提交后只做 warm / failure CAS。
7. 扩展 RequestLog Detail / PAGE-073 与 routing-policies race-safe PAGE-072。
8. 增加主流程、异常、权限、跨租户、并发、Fallback、流式、容量、日志隐私与 Webhook 测试。
9. 所有新增 / 修改模块、类、结构、字段和函数补详细中文说明；权限、租户、HMAC、事务、状态跳转、回退和脱敏分支解释“为什么”，并运行 `C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\ci\check-comments.py`。
10. 更新 README、directory-map、API 文档和 `docs/testing/PRD-050-tests.md`。
11. 执行专项、全量、真实服务、Browser、日志与静止数据库验收；全部通过后才把状态改为已完成。

## 11. 设计自审记录

### 11.1 第一轮自审

状态：已完成并修正。主 Agent 与两个独立审查者对照 Excel、架构和现有路由 / KeyPool / Stream 代码，关闭以下问题：

1. 原设计只做 Credential 粘性，未满足“同一路由 / 节点”；改为 warm Binding 在 RoutingDecision 前、当前合格动态候选内软偏好，fixed / hard gate 不被覆盖。
2. model_id 不能代表可变 Endpoint；增加 endpoint_revision_digest，URL / 上游模型映射变化必 miss。
3. auth secret 与 Prompt HMAC 耦合；改成独立 Secret、prod 校验和版本化域。
4. 并发预绑定被误报 hit；增加 pending / warm，只有既有成功终态才 estimated_hit=true。
5. fail-open 会产生 enabled 但无 Selection 的调用；有效上下文后改为 Provider 前 fail closed。
6. stream gate 实际晚于 Key；计划重排为 StreamSession starting 先于 Selection，并冻结失败终态。
7. Binding 清理与历史 FK 冲突；Selection 改为插入时验证的无长期 FK 快照，Binding Credential 可空。
8. 配置 / Binding / Selection / Event 的 REPLACE 旁路与 down 丢事实未定义；增加全部冲突守卫和非空 down preflight。
9. no-op 不消费 request_id；改为 noop ConfigEvent，并阻止旧版本重放覆盖页面。
10. canonical 忽略 extra / tools、TTL / 容量不完整、Key prefix 泄露；增加精确白名单、有界结构、滑动 TTL、容量淘汰并删除稳定 Key 标签。
11. Excel 的“执行或状态变化”只实现 runtime；增加 configuration_changed 事件变体。
12. cache_tokens 方言可能合并创建 / 读取；统一改称“上游缓存相关 Token”，不宣称实际命中。

第一轮结论：修订后可以进入第二轮安全与可验收性审查，产品代码仍未开始。

### 11.2 第二轮自审

状态：已完成并通过。三名独立审查者分别从 SQLite / 并发状态机、RoutingSelection / terminal 时点、权限 / 隐私 / 可验收性角度闭环复核；本轮已逐项处理：

1. 新增 scope 指针表；Binding 改为每个完整 endpoint revision 一行，主端点、Fallback 和跨 Alias 端点不再互相覆盖。
2. pending reservation 以 lease generation、短租约和活动 Selection 计数参与 endpoint preference；失败释放和崩溃过期都有确定性合同，预计命中仍仅来自成功证据。
3. warm 确认绑定 target generation 与 execution terminal CAS；旧代 / 目标变化 no-op，存储失败不改写 Provider 成功结果。
4. 删除全局 expires_at；每个请求以 `last_success_at + 当前 Alias ttl_seconds` 判断，避免跨 Alias 覆盖。
5. 冻结动态重排纯函数：availability 保留健康层级，quality_cost 保留 ready / floor，全部价格、质量和公式证据不变；fixed 完全不重排。
6. Candidate final reason 与 RequestLog 不能混用 base reason；0040 保真扩展两个 reason 枚举，candidate_summary 只补安全 base/final 解释，不新增敏感列。
7. 单 Gateway 写节点、Secret A -> B -> A 和多实例非目标已给出可测试预期，不声明跨节点粘性。
8. 非流 / 流 warm 时点、Selection 事件时点、no-op 终态、down guard、REPLACE 与 PAGE-073 隐私均已补齐。

第二轮结论：Scope / Binding / Selection 状态机、Candidate reason 迁移、跨 Alias TTL、启动恢复、Webhook 时点、Secret 单节点边界和测试合同均无剩余设计阻断，可以开始编码；未经实现测试与运行验收仍不得把 PRD-050 标记完成。

## 12. 实现与验收回写

状态：已完成。2026-07-26 已完成实现、当前源码相关回归、真实 Runtime、真实 Browser、日志隐私、资源清理和停止后 SQLite 终态审计；该结论仅适用于 PRD-050，不代表全部 108 条 PRD 已完成。

### 12.1 实现结果

- 0040 已创建五张 CacheAffinity 业务表，并以 tenant-qualified 外键、8 个运行时关键 trigger、冲突/不可变/REPLACE 守卫约束配置、Scope、Binding、Selection 和 ConfigEvent。当前迁移源的 loader checksum 为 `dc4289d44091e231f2c288398c58ab8eccc075fd7bcbbc869f2b7b7c8257f090`。
- Control API 已实现 admin-only CacheAffinity PATCH、admin/developer 读取、viewer 后端拒绝、跨租户同形 404、严格 DTO、CAS/no-op 幂等、统一错误码、`request_id/trace_id`、Audit/Event/Webhook。
- Gateway 已实现 tenant + Virtual Key + data_class + endpoint revision + Prompt fingerprint 隔离的 pending/warm 粘性路由，Provider 前失败关闭，非流式/流式真实终态 CAS，以及 RequestLog 安全 Selection 投影。
- PAGE-072 已实现 admin 编辑、developer 只读、viewer 无受限 DOM/预取；PAGE-073 分开展示预计命中与 Provider 实报缓存相关 Token。前端权限表现没有替代后端 RBAC。
- Provider Key、Virtual Key、Authorization、Prompt、Response、data_class、内部 digest、Credential/Binding/Scope ID 和 Endpoint URL 均受 DTO、DOM、Webhook、日志和验收制品白名单约束。

### 12.2 最终证据

- 权威批次：`artifacts/prd050/20260725T005725`。Runtime 状态为 `passed`；修复验收器重复导航竞态后，`browser-final_sandbox/browser-result.json` 状态为 `passed`。
- Browser 形成 admin/developer/viewer = `30/12/3`、共 45 张真实 Chrome PNG；1280x720、900x800、390x844 各 15 张。45 个实际 SHA-256 全部唯一且与元数据一致，非空像素/隐私遮罩合同均为 45/45，45 条布局证据的横向溢出、裁切和重叠均为 0。
- 后端 RBAC 实测：admin 可写；developer 写入 403 且页面只读、零 PATCH；viewer 读写均 403 且页面回退 overview、零目标请求。三角色均注销成功，两个临时用户禁用，配置恢复且请求事实未改写。
- 六份服务日志共重组 2,044 条结构化记录和 2,040 条访问记录；5xx、ERROR/CRITICAL、精确口令、通用 Secret/Token、敏感 JSON 字段及非服务日志敏感命中均为 0。
- 停服后数据库为 40/0040，`integrity_check=ok`、外键违规 0、pending Selection/active selection/live lease/started attempt/active stream/临时 Browser 用户与 Session 均为 0，WAL/SHM/journal 均不存在；最终数据库 SHA-256 为 `5b895e79fc11dda8c77d1a9131bebc407ae9e68acc6102bf2883d742a25238c7`。
- 当前源码定向验证为 Browser 合同 4/4、CacheAffinity + Dashboard 15/15、静态 verifier 首轮 20/21 后修正唯一旧 SHA 绑定并复测 1/1、Browser 脚本语法和中文注释门禁通过。2026-07-24 的 Python 921/921、Console 354/354、Browser 单测 71/71、Node 示例 33/33 和 lint 118 files 只作为变更前全量基线，本轮没有伪称重新执行。
- 本轮严格使用 `D:/MetaHub/.tmp` 和工作区制品目录，没有使用 `C:/tmp`、提权、CIM 或端口 owner 扫描，也没有因验收器局部修复重跑无关 Python 全量。

完整命令、失败证据保留、风险分层测试口径和最终制品索引见 [PRD-050 测试记录](../testing/PRD-050-tests.md)。
