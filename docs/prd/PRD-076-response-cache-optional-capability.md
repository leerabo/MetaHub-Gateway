# PRD-076 - Response Cache 可选能力

状态：设计与两轮独立自审已完成，发现均已回写修正；实现、专项测试和最终运行验收正在进行中，尚未标记完成。本文冻结需求、数据、并发、计费和安全边界，后续编码不得绕过这些门禁。

总体目标仍在进行中：`all_108_prds_complete=false`。本条只处理 PRD-076，不包含 PRD-077。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

主 Agent 直接以 OOXML 结构读取工作簿，并交叉确认以下行：

- `01_PRD总表!77` 与 `03_M2-M3扩展交付!13`：
  `PRD-076 | M2-企业增强 | P2 | 扩展层 | 缓存策略 | Response Cache可选能力`。
- 用户为管理员/财务；用户故事是重复请求直接返回缓存以降本。
- 范围明确要求 Cache Key 至少包含 `tenant_id / virtual_key / data_class / model / prompt_hash`，并支持 TTL、手动失效和收费策略开关。
- `04_页面清单!107`：PAGE-106 缓存策略页。
- `05_API接口清单!348:353`：表格把 method/path token 拆行；结合 PRD 总表，接口重组为：
  `GET /cache-policies`、`PATCH /cache-policies`、`POST /cache/invalidate`。
- `06_数据对象!104:105`：`ResponseCacheEntry`、`CachePolicy`；对象表备注要求字段按本 PRD 细化，不能把通用建议字段机械照搬。
- `07_Webhook事件!86:87`：`cache.hit`、`cache.invalidated`。
- `08_验收用例!152:153`：跨租户必须 miss、命中日志显示节省金额、可按标签失效，并覆盖无权限、异常输入、依赖故障、缓存投毒、统一错误和敏感信息保护。
- `09_CodexClaude提示词!77`：先设计，再修改表结构/API，再编码，最后补测试。

### 1.2 README、PRD 与测试记录交叉结果

- README 已完成列表止于 PRD-075，当前下一项要求在 PRD-075 推送后重新交叉确认。
- `docs/prd` 与 `docs/testing` 存在 PRD-075 的完整设计和测试记录，不存在 PRD-076 文档。
- 代码与迁移中不存在 `ResponseCacheEntry`、`CachePolicy`、`cache.hit`、`cache.invalidated`、`/cache-policies`、`/cache/invalidate` 或 `0069` 实现。
- 开始编写本设计前，Git 基线为 `c0b3947921b98309ab2e39a74110fe8586f50fd1`，`HEAD == origin/main` 且工作区干净；设计文件落盘后不再把“工作区干净”误写成当前事实。

因此，PRD-076 是经权威资料交叉确认的下一条未完成项，不是仅凭连续编号猜测。

### 1.3 架构约束与 PRD-050 边界

系统架构第 7.4 节冻结：缓存域必须包含 tenant、Virtual Key、数据分级、模型 Endpoint 和 Prompt 指纹；语义缓存因缓存投毒、权限越权和复杂向量基础设施风险不进入既有 MVP 范围。ADR-005 重申相同结论。

PRD-050 只实现 Provider Prompt Cache 的路由亲和元数据：

- 不保存或返回模型 Response。
- `estimated_hit` 只是预计上游 Prompt Cache 亲和，不是 Response Cache 命中。
- `usage_ledger.cache_tokens` 是 Provider 实报缓存相关 Token，不是 MetaHub Response Cache 命中量。
- PRD-076 必须使用独立对象、状态、事件和节省金额口径，禁止改写 PRD-050 事实。

### 1.4 Excel 原文歧义

Excel 非范围原文为“`M2不做语义缓存默认开启。`”，缺少标点，且“默认开启”与标题“可选能力”、缓存投毒风险互相冲突。

本设计采取保守且可审计的解释：

1. M2 不实现语义缓存，只实现精确请求缓存。
2. Response Cache 全局和租户策略均默认关闭，必须显式配置后才启用。
3. 不把歧义解释为“语义缓存默认开启”，也不引入向量数据库、相似度阈值或文本归一匹配。

该歧义必须在两轮设计自审中再次检查，任何实现不得悄然扩大范围。

## 2. 需求理解与冻结范围

### 2.1 本期必须交付

1. 当前租户、当前 Virtual Key、可信数据分级、实际解析模型和完整请求指纹共同隔离的精确 Response Cache。
2. 每个 ModelAlias 独立的 CachePolicy：显式启停、可信且单一的 `data_class` 声明、TTL、缓存命中收费开关和服务端管理标签。
3. 公共 OpenAI-compatible 非流式 Chat 主链路的 miss、单写者填充、hit、过期、腐坏淘汰和容量淘汰。
4. `GET/PATCH /cache-policies` 与 `POST /cache/invalidate`，全部使用后端动态 RBAC、Session tenant 和统一错误体。
5. 全租户手动失效和标签失效；并发失效必须围栏在途旧填充，不能出现删除后迟到写回。
6. Response 密文存储、Prompt/完整请求不可逆 HMAC、独立密钥域和 owner-bound AAD。
7. cache hit 的 RequestLog 投影、逻辑 Usage、FinOps charge、节省金额和失败状态追踪。
8. `cache.hit` 与 `cache.invalidated` 的严格安全 Webhook payload，以及可恢复 source outbox。
9. PAGE-106 缓存策略页和既有调用日志详情扩展。
10. 0069 正反迁移、专项/必要回归、真实 Runtime、双视口 Browser 和敏感信息审计。

### 2.2 明确非范围

- 语义缓存、向量库、Embedding 相似度、近似匹配、自动 Prompt 归一或跨语言复用。
- 流式 Chat、Embeddings、Enterprise Chat、Batch、内部 trusted invocation 的 Response Cache。
- PRD-075 在线实验请求的 Response Cache；实验统计必须继续以真实 Provider exposure、latency、Usage 和 FinOps 为准。
- 跨地域复制、多 Gateway 分布式一致性、Redis 集群、CDN 缓存或全局多活。
- 缓存正文浏览、单条正文下载、Prompt/Response 搜索、Cache Key/Prompt hash 对外展示。
- 自动训练、语义质量判断、人工编辑缓存内容或从客户端 metadata/header 信任数据分级和标签。
- PRD-077 管理员 AI 助手。

### 2.3 运行范围

只有同时满足以下条件的请求才候选使用 Response Cache：

- `request_kind == "chat"` 且 `stream` 明确不是 `true`。
- 来自公共 Virtual Key API，不是 Enterprise Chat、Batch 或其他 trusted invocation。
- 请求模型精确命中当前 tenant/environment 的 active ModelAlias，并存在该 Alias 的 enabled CachePolicy。
- 当前请求不属于 active PRD-075 Experiment scope；包括已分配请求和因采样桶未命中的同 scope 请求，均旁路缓存，避免改变实验总体 exposure 与成本基线。
- 当前 Alias 被授权管理员明确声明为单一可信数据分级；混合分级、无法证明单一分级或允许客户端改变分级的 Alias 必须保持 disabled，并先拆分 Alias/Virtual Key 边界。
- Policy、路由、DLP、Intent、Session Risk 和 Rate Limit 已通过。
- 当前路由有完整 tenant、Virtual Key、Alias、resolved model、Provider/Adapter revision 和独立缓存密钥配置。
- 完整请求可在有界 canonical 规则下唯一编码。

Direct concrete Model、Policy `route_to`、Embeddings、stream 和内部调用均明确旁路。这样不改变 PRD-075、PRD-074、PRD-066 和现有 direct-route 合同。

## 3. 核心设计决策

### 3.1 双重显式启用

- 全局配置 `METAHUB_RESPONSE_CACHE_ENABLED` 默认 `false`。
- 每个 tenant + ModelAlias 的 CachePolicy 默认不存在；不存在和 disabled 都是 miss/bypass，不会读取缓存条目。
- 只有全局启用、密钥配置完整、数据库 key-version 状态与本进程一致且 CachePolicy enabled 时才执行 lookup/fill。
- 生产环境启用时必须显式提供独立 HMAC 与加密 Secret；dev/test 可使用仓库公开本地占位，但占位不得用于 prod。
- Control 幂等 owner 和 failure spool 分别使用独立、版本化的 `METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET` 与 `METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET`；二者不随 cache-key HMAC 轮换，均使用 `repr=False/exclude=True` 且禁止日志/证据。轮换前必须保留旧验证版本或证明对应 owner/spool retention 已清零。
- HMAC 和 Encryption 均配置非敏感正整数 version。热路径必须与数据库单例 key-state 的 active version 一致；不一致时固定旁路并报告 `response_cache.configuration_unavailable`，不能猜测使用旧 Secret。
- 本期不允许在线直接替换 Secret。轮换必须先全局停用，执行受信维护事务推进全部 policy/key generation、删除 entry/fill/tag 派生数据并切换数据库 active version，再部署新 Secret/version 后启用。旧进程的 lease commit 因 active version 与 generation 漂移而固定失败；维护事务和旧/新进程交错必须有测试。

### 3.2 可信 `data_class`

现有 GatewayAuthContext 没有通用可信数据分级，客户端 Header、OpenAI `metadata` 和请求正文都不可作为授权来源。

本期由 CachePolicy 保存服务端受控 `data_class`：

- enabled 时必填，格式为 `^[a-z0-9][a-z0-9._-]{0,63}$`。
- enabled 时 `single_data_class_confirmed` 必须严格 true；disabled 请求必须为 false，`data_class` 可为 null 或保留 canonical machine code。disabled 会清除确认 actor/time，重新启用必须再次明确确认。
- Control API 只允许具有 `response_cache.write` 的当前租户用户修改。
- Gateway 只读取当前 tenant + active Alias 的权威 policy 行，不接受客户端覆盖。
- `data_class` 可在 PAGE-106 授权页面展示，但不得进入 RequestLog、Webhook或结构化进程日志。Browser PNG 只允许文档明确列出的纯合成 fixture 值；任何真实/随机隐私 canary 不得进入截图或验收摘要。
- `data_class` 是该 Alias 唯一可信工作负载分类声明，不是从每个 Prompt 猜测出来的标签。PATCH 启用时必须提交 `single_data_class_confirmed=true`，服务端保存确认事实、actor 和版本；false、缺失或 Alias 已标记为混合分类时拒绝启用。
- 同一 Alias 若承载多种真实分级，管理员必须拆成不同 Alias，并分别配置 CachePolicy；不能靠填写一个静态值把混合工作负载伪装成已经完成五维隔离。

### 3.3 精确请求身份

Shared 新增版本化纯函数，完整身份分三层构造：

1. `prompt_hash`：对 Policy mask 后的完整 Chat JSON 做有界、typed canonical，再以 Response Cache 专用、版本化 HMAC-SHA256 域生成。名称沿用 Excel，但它覆盖所有可能改变 Response 的字段，不只 messages；未知字段也参与，避免 Adapter 新增透传参数后发生欠键。
2. `credential_response_scope`：lookup 前只允许读取无副作用、无 Secret 解密、无 `last_used`/selection 写入的权威快照。`auth=none` 或固定环境 Credential 使用配置 version/fingerprint 域；托管 KeyPool 只有恰好一个当前可选 Credential 时才 eligible，并绑定不可逆 credential ID/key fingerprint/version。存在多个可选 Credential、选择依赖运行状态或无法证明唯一响应域时固定 bypass。成员、状态、fingerprint 或 Secret version 变化均 miss；miss 的真实 KeyPool 选择仍位于既有安全门后，fill commit 必须验证实际 Credential 与冻结 scope 一致。
3. `route_revision_digest`：对 tenant、ModelAlias、resolved model、Provider、provider type/auth/base URL、upstream model、能力、Adapter catalog version/hash、当前路由 Policy snapshot ID/version/status和上述 credential response scope 做独立 HMAC。Provider Key/Token 明文本身绝不参与或落库；Endpoint/Adapter/模型/Credential 绑定变化必须 miss，不提供宽泛 `credential_response_invariant` 例外。
4. `cache_identity_digest`：对 schema version、HMAC key version、`tenant_id`、`virtual_key_id`、`data_class`、`model_id`、`prompt_hash`、`route_revision_digest`、CachePolicy ID/version/generation 生成稳定摘要，不含 key generation。
5. 数据库先以 `(tenant_id,hmac_key_version,cache_identity_digest)` 读取 key generation，再生成最终 `cache_key_digest = HMAC(identity_digest + generation)`。entry/lease 同时保存两个不可逆 digest，owner trigger 验证同一 identity/generation 关系；这样安全淘汰可以稳定推进 generation，不形成自引用循环。

逻辑 Cache Key 因此至少包含 Excel 要求的五个维度，并额外绑定可信路由和策略代际。数据库只保存最终不可逆 digest，不单独保存 Prompt hash、正文 canonical、Cache Key 明文、Virtual Key prefix 或 Endpoint URL。

Canonical 规则：

- 只接受 JSON object，递归只允许 null/bool/int/finite IEEE-754 binary64/string/list/object。
- canonical 不是 `json.dumps`。每个节点写入固定 type tag 和无符号长度前缀；int 使用无前导零十进制，float 使用 IEEE-754 binary64 big-endian 16 位 hex，因此 `1`、`1.0`、`-0.0` 明确不同，`1.0` 与解析后相同的 `1e0` 相同。NaN/Infinity 固定拒绝。
- 保留字符串、数组顺序、数值类型和所有字段；不 trim、不大小写折叠、不做 Unicode 兼容归一。object key 按 Unicode code point 稳定排序，字符串统一以原值 UTF-8 长度前缀编码。
- 顶层和每个 HMAC 都包含固定 ASCII domain label、schema version 和字段长度，禁止不同层或拼接边界产生二义性。
- 最大深度 24、节点 8192、canonical 2 MiB、字符串 1 MiB；超限仅标记 ineligible 并走 Provider，不截断后生成可能碰撞的摘要。
- `stream=false` 与省略 `stream` 可按服务端实际相同行为统一为一个显式 false；任何其他字段不删除。
- 重复 JSON key 在 HTTP 解析阶段固定拒绝，不能让“最后一个值”与实际 Provider 语义分叉。

### 3.4 加密与内存边界

- Response payload 使用 AES-256-GCM；根密钥经 HKDF-SHA256 按 `tenant_id + entry_id + encryption_key_version` 派生每条 entry 的独立 AES key，每条 entry 再生成 96-bit 随机 nonce。
- 数据库仍对 `(encryption_key_version, nonce)` 建全局唯一约束；极低概率冲突时重新生成 entry/nonce，禁止复用。AES 根密钥来自 `METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET`，与 HMAC 根密钥完全独立。
- AAD 采用 exact typed canonical，只绑定加密时已经存在且永久不可变的 entry/source 元数据：格式/key schema、HMAC/encryption key version、tenant、entry、policy ID/version/generation、identity/key digest及 key generation、Alias/model/provider、route/credential revision、source request/trace/Usage/FinOps owner、冻结 input/output/total token、source generation cost/status、expires_at 和 plaintext byte count。解密时只从当前 entry 行重建 AAD，任何换行、延长 TTL 或错绑 source 账务都会验真失败。
- `current_reference_cost/billed_cost/saved_cost` 是未来每次 hit 才形成的动态事实，绝不进入 entry AAD；它们由 tenant-qualified hit/Usage/PriceSnapshot/FinOps owner trigger 和同一 settlement 事务保护。
- HMAC Secret 与 Encryption Secret 必须彼此独立，也不能等于 Auth、Provider Key、CacheAffinity、Prompt Template、File、Batch 或 Session Risk Secret。
- 配置字段使用 `Field(exclude=True, repr=False)`；异常只返回固定错误，不序列化 secret、nonce、ciphertext、Prompt 或 Response。
- 明文 Response 只在本次 Provider finalizer、加密调用或 cache-hit 解密/Output Safety/JSONResponse 栈内存在；不进入状态快照、日志、Webhook、DOM 或验收 JSON。
- 单条明文默认上限 1 MiB；超限响应正常返回但不缓存。
- AES tag、AAD、JSON 或 owner 校验失败时，在同一事务推进该 cache key generation、删除 entry 和同 key fill，记录固定 `corrupt_miss`/system invalidation 事实后按 miss 走 Provider；绝不返回可疑正文，也不能允许旧 fill 迟到回写。

### 3.5 请求顺序与安全闸门

公共 Chat 顺序冻结为：

1. Virtual Key 鉴权并在读正文前创建不可变 request/scope claim。
2. 继续执行 PRD-070 的正文读取前 Session Risk identity/overlay preflight；冻结会话必须在读取 Prompt 前拒绝。
3. 严格读取 JSON，验证模型权限与 Environment。
4. Policy snapshot 评估和 mask。
5. PRD-075 Experiment scope 权威解析；active scope 全部设置 cache bypass。
6. 正常路由解析并冻结 route/credential revision。
7. DLP。
8. current-turn Intent 分类与 Session Risk 累计；不能与第 2 步合并或后移。
9. Rate Limit。
10. Response Cache eligibility、identity generation lookup、entry lookup/decrypt 或 fill lease reservation。
11. candidate hit 先冻结唯一 `pricing_observed_at`、current-reference PriceSnapshot owner/版本/plan/as-of，并按 charge 模式创建 cache budget reservation；普通 miss 沿用正常主/备最坏成本。
12. 为 Chat 创建本请求唯一 pending Output Safety owner/snapshot。candidate response 只在内存扫描形成临时 terminal，二次资格验证前不得持久终结；普通 miss 沿用现有 Provider 扫描。
13. candidate allowed 后在唯一 hit settlement 事务内重新验证 entry TTL、policy、route/credential、tag/identity/key generation、active key version 和 Output Safety ruleset owner，并原子终结 scan、提交 RequestLog、Usage/PriceSnapshot/FinOps、预算结算、hit、request fact、event/outbox。commit 才是 hit 线性化点，随后才能返回正文。
14. candidate 在二次验证时过期或任一 owner 漂移，丢弃临时 terminal但保持同一 pending Output Safety owner；在一个预算替换事务中释放 cache reservation并重新执行普通主/备最坏成本 Guard。只有替换成功才调用 Provider，随后用同一 pending owner 扫描 Provider response；不能带着免费/低价 cache 预留出网。
15. candidate 被 Output Safety blocked/unavailable 或明确 settlement rollback 且未调用 Provider时，终态事务必须把本 request 的预算 outstanding 结算为 0。Budget 替换失败按现有预算错误收口，并把未使用的 Output Safety owner终结为 not_applicable。
16. miss 执行 Provider/Fallback；只有 initial route 成功、实际 Credential 与冻结 scope 一致、Output Safety 放行、RequestLog 成功且 Usage/FinOps 已在主库提交时才提交加密 entry。

Cache lookup 不能跳过 Session Risk preflight、Policy、DLP、Intent、Session Risk 累计或 Rate Limit。cache hit 仍重新执行当前 Output Safety；规则变化不会让旧安全结论代替当前安全结论。

### 3.6 PRD-075 实验隔离

`ExperimentRouteResolution` 增加无默认值的三态 `response_cache_disposition = not_in_scope | active_scope_bypass | unresolved`，不暴露实验桶，也避免新增字段漏赋默认允许缓存：

- active scope 的 sampled、unsampled 和不可用 arm 都为 `active_scope_bypass`；当前 repository 的 unsampled 早退必须显式返回该值。
- 没有 active Experiment、非 Alias、非候选请求为 `not_in_scope`。
- 解析异常或无法证明范围为 `unresolved`，沿用实验固定失败关闭，绝不能尝试缓存。
- `active_scope_bypass` 时 Response Cache 不 lookup、不 reserve、不 fill；RequestLog 只显示固定 `bypassed_experiment`。
- 该改动不改变 Assignment、variant、统计或已冻结 PRD-075 API，只补充后续能力的旁路信号。

### 3.7 单写者填充与并发

miss 事务尝试领取 `(tenant_id, hmac_key_version, cache_key_digest)` 唯一 fill lease：

- 无 lease 或 lease 已过期时创建新 owner/generation/token；当前请求成为 fill owner。
- 其他并发请求看到有效 lease 时直接走 Provider，但不获得写权限，不等待、不轮询，也不能覆盖首个 owner。
- owner Provider 失败、Fallback 成功、Output Safety 阻断、Usage 仅 spooled、请求取消或超限时不写 entry，并尽力释放 lease；释放失败由租约过期恢复。
- lease 同时冻结 active HMAC/encryption key version 和当前 key generation。commit 必须重新验证 tenant/request/trace、lease token/generation、数据库 active key versions、key generation、policy enabled/version/generation、route/credential revision、全部 tag generation 和有效期；任一漂移只跳过缓存写，不改变已完成的模型响应。
- 相同 key 已由更新 owner 写入时，旧 owner 不能覆盖。
- 每租户 ready entry 上限默认 10,000；commit 前先删除过期 entry，再按 expires_at/created_at 稳定淘汰最旧条目。淘汰只删除密文缓存，不删除 hit/invalidation/Usage/FinOps 审计事实。

### 3.8 标签与失效围栏

每个 fill 在内存生成以下服务端可信标签：

- `policy:<cache_policy_id>`
- `model_alias:<model_alias_id>`
- `model:<model_id>`
- `virtual_key:<virtual_key_id>`
- `data_class:<data_class>`
- CachePolicy 配置的 0-16 个 `tag:<machine_code>`

条目表只保存专用 HMAC 后的 tag digest 和 tag kind；不保存 Virtual Key、data class 或自定义标签明文。CachePolicy 自身保存授权管理员配置的 machine-code tags，以便 PAGE-106 展示。

失效合同：

- `scope=all`：删除当前 tenant 的全部 entry/fill，并推进所有 tenant policy generation。
- `scope=tags`：1-16 个去重排序 tag，采用 `match_mode=all`；只删除同时具备全部 tag 的 entry。
- 每个 tag 有 tenant + HMAC key version-qualified generation；fill lease 冻结自己的 tag generation。标签失效先推进当前数据库中全部相关 key version 的所选 tag generation，再删除匹配 entry/fill，因此即使当时 0 条 entry，旧在途请求也不能迟到回填同标签。
- Policy 任何真实变更都会 version+1、generation+1，删除该 policy 的 entry/fill；no-op 不推进版本。
- 新增 tenant-qualified `response_cache_key_generations`。腐坏或当前 Output Safety 明确 blocked 时，在一个事务内推进该 key generation、删除 entry/fill 并形成 system invalidation；所有 fill/hit 均冻结并二次验证 key generation，旧 owner 不能把被安全淘汰的正文重新填回。
- 0 条匹配是成功且形成不可变 invalidation 事实，便于审计操作确实发生。

`cache.invalidated` 只覆盖手动失效、Policy 真实变更和安全淘汰。安全原因固定为 `system_corrupt/system_output_blocked`，使用稳定 system actor/idempotency owner；普通 TTL 与容量清理不产生业务 Webhook，避免把正常生命周期伪装成管理员或安全事件。

### 3.9 TTL 与时间

- TTL 范围 60 秒至 86,400 秒，默认建议 900 秒。
- `created_at/expires_at` 全部取 SQLite 事务 UTC 秒，不接受客户端时间。
- lookup 和 hit settlement 二次验证都要求 `unixepoch(expires_at) > unixepoch(transaction_now)`；边界相等即过期。Output Safety 或其他处理跨过 TTL 时 candidate 必须转 miss。
- 过期 entry 在 lookup/commit/invalidation 时有界清理；不依赖常驻外部缓存服务。
- 客户端和 Console 不倒计时决定有效性，后端返回的 `expires_at` 也不暴露单条 entry。

## 4. 计费、Usage 与节省金额

### 4.1 独立事实口径

Response Cache hit 不得写成 PRD-050 `estimated_hit`，也不得把 `usage_ledger.cache_tokens` 当成命中量。

每个真正返回给客户端的 hit 必须形成：

- 一个 `response_cache_hits` 不可变事实。
- 一个 `response_cache_request_facts` 的 `status=hit` 投影。
- 一条逻辑 `usage_ledger`，`usage_source='response_cache'`，input/output/total 来自 entry 的冻结 Provider Usage，`cache_tokens=0`，不复制 Provider raw usage；其中 cost 表示本次租户缓存服务收费，不伪装成新的 Provider 网络成本。
- 一条由既有 Usage owner 同事务产生的正式 `finops_ledger_entries.entry_type='usage_charge'`。PRD-051 的正式账本表达租户 Usage 成本/收费归属，并不等同于云厂商应付账款；0069 必须让 FinOps/Usage/预算/导出按 `usage_source='response_cache'` 明确识别其来源。
- 一个 `cache.hit` source event/outbox。

hit 的唯一线性化点是主数据库 `BEGIN IMMEDIATE` settlement commit：事务内二次验证 entry TTL、policy、route/credential、tag/identity/key generation、active key version 和 pending Output Safety owner，并原子提交 scan terminal、RequestLog success、Usage、PriceSnapshot、FinOps、预算结算、hit、request fact、event/outbox。任一明确 rollback 都不得返回缓存正文，也不得生成部分 hit。

hit settlement 本身不使用 Usage crash spool，因为 hit、账务和事件不能分裂恢复。request owner 在事务前预生成全部 hit/Usage/PriceSnapshot/FinOps/event ID；commit 抛错后必须用 fresh connection exact read-back：完整 owner 全部存在且互相匹配则按成功继续，明确全部不存在才用独立短事务写固定 503 RequestLog/request fact并把预算结算为 0，任何 partial owner 固定进入 integrity failure且不得返回正文。

数据库不可读时不能把未知 commit 结果武断记成失败：写 `outcome_unknown` 恢复记录，字段只含 tenant/request/trace/policy/version、预生成 settlement IDs 和固定 error，不含 entry/digest/Prompt/Response/金额/Token。记录使用独立 `METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET` + version 做 HMAC，原子 replace、file/directory fsync，与 cache key Secret 轮换隔离。恢复器先 exact read-back：完整 settlement 则仅移除 spool，全部不存在才恰好补一个失败终态并释放预算，partial 则隔离告警；恢复期间预算保守保持 outstanding。key rotation、down 和最终验收前必须先证明 pending/quarantine 都为 0。

cache candidate 被当前 Output Safety 明确 blocked 时并未向客户端交付，也没有 Provider 网络成本：不创建 hit、response-cache Usage 或 FinOps charge；以新增 `not_billable_response_cache_candidate` disposition 原子提交 Output Safety blocked 事实、固定失败 RequestLog/request fact、预算 0 结算和 key-generation 安全失效，不进入 PRD-071 Provider 成本争议链。Output Safety unavailable 则以同一失败事务终结 scan、预算归零、固定 503并保留 entry，避免把扫描器故障误判为缓存投毒。

### 4.2 收费开关

`charge_cache_hits=false`：

- hit 的 `billed_cost_cny` 为 0。
- Budget 前持久冻结 `source='response_cache_reference'` 的 current-reference PriceSnapshot；settlement 另创建 `source='response_cache_no_charge'`、`price_schema_version='response-cache-no-charge-v1'` 的零价 billed PriceSnapshot。
- 仍创建 0 元正式 usage charge，证明调用发生且未收费；不能直接省略财务事实。
- Budget 使用可信 0 元 override，不得因原 Provider 估算成本误阻断免费 hit。

`charge_cache_hits=true`：

- 以 Budget 时冻结的 `pricing_observed_at`、当前 resolved model 有效 PricePlan/带版本目录价和冻结 token 数生成唯一 current-reference PriceSnapshot；同一 snapshot 同时作为 billed 价格证据，settlement 禁止重新读取更新后的价格。
- Price 缺失沿用既有 `finops_unpriced` 事实，不把未知冒充免费；存在 block Budget 时继续按既有缺价失败关闭。
- Budget 使用相同 token 数和 price-as-of，避免按请求 max_tokens 过度预留。

两种模式都不产生 RoutingAttempt/StreamSession，因为没有 Provider 网络调用。

### 4.3 节省金额

- entry 只在 source Usage/FinOps 已主库提交后写入，并冻结 source `usage_ledger_id`、`finops_ledger_entry_id`、`source_generation_cost_cny_micros` 和证据状态。该值只描述生成缓存内容时的历史正式租户成本，不得直接命名为本次节省。
- `current_reference_cost_cny` 由 Budget 前冻结的 immutable reference PriceSnapshot ID/schema/source/plan/as-of 与冻结 token 计算，表示若本次按同一 resolved model 正常计费的反事实租户成本；它不是未经证据的云厂商实际成本。
- `billed_cost_cny` 来自本次 response-cache Usage/FinOps：charge off 为有明确免费快照的 0，charge on 与同一 current reference 价格证据一致。
- Excel 要求展示的 `saved_cost_cny = current_reference_cost_cny - billed_cost_cny`。本期两者使用同一非负价格合同，因此 charge off 显示完整当前参考成本，charge on 通常显示 0；不得用历史 source cost 冒充节省。
- hit owner 内部保存 source、current-reference 与 billed PriceSnapshot ID/version/status；charge off 同时绑定 reference 和 no-charge 两个 snapshot，charge on 两者指向同一 reference snapshot。Budget、settlement、billed 和 saved 必须复用该证据，不对 API/Webhook 暴露内部 ID。
- source/current/billed 任一相关价格为 USD、unpriced、损坏或无法 CNY 归一时，各自状态独立为 `unavailable`，依赖它的 saved amount 为 `null`；页面显示“暂不可用”，禁止显示伪造 0。
- 命中日志详情只展示 hit 状态、收费模式、source generation/current reference/saved/billed 四个金额及证据状态、policy ID/version；不返回 source request、entry、key、Prompt hash、tags、data class 或密文身份。

## 5. 权限与租户隔离

RBAC catalog revision 从 11 提升到 12，新增三个独立权限：

| 权限 | 风险 | admin | finance_admin | developer/viewer/security/ops |
| --- | --- | ---: | ---: | ---: |
| `response_cache.read` | read | 默认授予 | 默认授予 | 不授予 |
| `response_cache.write` | critical | 默认授予 | 默认授予 | 不授予 |
| `response_cache.invalidate` | critical | 默认授予 | 默认授予 | 不授予 |

约束：

- GET 只需 read；PATCH 同时要求 read+write；invalidate 同时要求 read+invalidate。
- Control 依赖必须在读取 body 前执行实时数据库授权；service 写事务内再次确认 role matrix revision/fingerprint，防止依赖返回后撤权竞态。
- tenant、actor、Session、operation request/trace 全部由后端认证上下文恢复；请求 body 不接受 tenant/actor。
- Gateway tenant、Virtual Key、Environment、Project、Application 全部来自鉴权 claim；客户端不能覆盖 data class、policy、tags、digest、hit、费用或 source owner。
- 所有主表使用 tenant-qualified PK/UNIQUE/FK 或 owner trigger；跨租户 ID 与不存在 ID 同形，不能枚举。
- 前端只负责裁剪入口与按钮，手写 API 仍由后端拒绝。
- Console owner 包含完整 permission set/revision/fingerprint。任何权限或 navigation 漂移都先清除全部旧 owner DOM/state 并 Abort；若重新认证后的新 owner仍有 read，必须通过新 GET 重建只读页面，不能沿用旧 owner 已接受事实。只撤销 write/invalidate 的最终视觉效果可以是只读页保留，但它来自新 owner 的重新加载，而不是局部保留旧 DOM。

## 6. 数据库设计（0069）

### 6.0 `response_cache_key_state`

单例表保存 schema version、active HMAC/encryption key version、非敏感 Secret fingerprint、generation 和更新时间；不保存 Secret。Gateway lookup/fill/hit 每次事务都验证当前进程配置与该行一致。受信轮换事务必须在一个 `BEGIN EXCLUSIVE` 中全租户推进 policy/key generation、清空 entry/fill/tag 派生数据并切换 version/fingerprint；旧进程任何写入随后都因 key-state 漂移失败。

### 6.1 `response_cache_policies`

核心字段：

| 字段 | 约束与用途 |
| --- | --- |
| `cache_policy_id` | `rcp_<32hex>`，tenant-qualified 唯一 |
| `tenant_id` | 必填 tenant owner |
| `model_alias_id` | 同 tenant active Alias；`UNIQUE(tenant_id, model_alias_id)` |
| `status` | `enabled/disabled`；默认显式创建时由 body 指定 |
| `data_class` | enabled 时必填的 machine code |
| `ttl_seconds` | 60..86400 integer |
| `charge_cache_hits` | 严格 0/1 |
| `tags_json` | canonical 去重排序 machine-code 数组，最多 16 个 |
| `single_data_class_confirmed` | enabled 时严格 1，并绑定本次 actor/version；混合分类 Alias 不得启用 |
| `version` | 从 1 开始，真实变更 +1 |
| `invalidation_generation` | 从 1 开始，配置变化/全量失效 +1 |
| `created_by/updated_by` | 同 tenant User |
| `created_at/updated_at` | UTC Z |

除允许的完整 CAS update 外禁止部分更新、跨 owner 改写、REPLACE 和 DELETE。Alias 被禁用时 policy 可保留但 Gateway 不启用；Alias 删除仍由既有业务约束决定，0069 不级联删除缓存策略事实。

### 6.2 `response_cache_policy_events`

保存每次 PATCH 的不可变 idempotency/audit owner：event ID、tenant、policy、actor、Idempotency-Key HMAC、operation request/trace、request fingerprint、event type、before/after safe JSON、完整安全 response snapshot、result version、changed、invalidated/fenced counts、created_at。

`UNIQUE(tenant_id, actor_user_id, idempotency_hmac_version, idempotency_key_hmac)`；同 Idempotency-Key 同 fingerprint 从 response snapshot 精确重放原结果，不重复 Audit/Event/Outbox，不同 fingerprint 固定 409。Control idempotency 使用独立 active + retained verification keyring；service 写入前以全部仍有效版本查询，存在任何旧 owner 即重放/冲突，含业务事实的旧版本不得移除。operation request/trace 仅用于本次链路，不能代替幂等 owner。before/after/response 只含 tenant、policy、status、data_class、TTL、charge flag、tags、version 和安全计数，不含 Prompt/Response/Key。

### 6.3 `response_cache_entries`

核心字段：entry ID、tenant、policy ID/version/generation、key schema/HMAC/encryption key version、`cache_identity_digest`、key generation、最终 cache key digest、model alias/model/provider、route/credential revision digest、nonce/ciphertext、plaintext byte count、冻结 token 数、source request/trace/Usage/FinOps、source generation CNY micros/status、expires_at、created_at。

约束：

- `UNIQUE(tenant_id, key_schema_version, cache_key_digest)`。
- `UNIQUE(encryption_key_version, nonce)`；碰撞由应用重新生成，绝不能 UPDATE nonce/ciphertext。
- 密文 owner 与 source RequestLog/Usage/FinOps 必须属于同 tenant，且 source request 成功、Usage/FinOps owner 一致。
- 只允许 INSERT 和受控 DELETE；正文、owner、digest、TTL、source cost 不可 UPDATE/REPLACE。
- 不保存 Prompt hash、canonical body、Response 明文、Key prefix、Credential、Endpoint URL 或 Provider Key。

### 6.4 `response_cache_entry_tags` 与 `response_cache_tag_generations`

- entry tag 表字段为 tenant、HMAC key version、entry ID、tag kind、tag digest；entry 删除时级联删除。tag value 不写表。
- generation 表字段为 tenant、HMAC key version、tag kind、tag digest、generation、updated_at；只允许同租户 +1。
- tag value 明文不进入 entry 表、generation 表、日志或 Webhook。Browser 证据仅允许固定、公开、纯合成 fixture tag；随机隐私 canary 流禁止截图并在清理前做 DOM/网络与非 PNG artifact 扫描。

### 6.4A `response_cache_key_generations`

字段为 tenant、HMAC key version、`cache_identity_digest`、generation、updated_at，`UNIQUE(tenant_id,hmac_key_version,cache_identity_digest)`。lookup 先用 identity 读取 generation，再派生最终 key；entry/fill owner trigger 验证 identity + generation 与最终 key 的关系。腐坏和 Output Safety blocked 使用受控事务 +1 并删除同 identity entry/fill；普通 TTL/容量删除不推进 generation。

### 6.5 `response_cache_fill_leases` 与 `response_cache_fill_tags`

lease 字段：tenant、HMAC/encryption key version、cache identity/final key digest/key generation、policy/version/generation、owner request/trace、lease generation/token/owner ID、route/credential revision、expires/created/updated。

fill tag 字段冻结每个 tag digest 的 expected generation。commit 必须在同一个 `BEGIN IMMEDIATE` 中逐项验证 lease、policy 和 tag generation，再插入 entry 并删除 lease。旧 generation/token 更新 0 行，不得猜测成功。

### 6.6 `response_cache_request_facts`

一条 Gateway request 对应一个安全 cache 投影：tenant/request/trace、policy ID/version、状态、可空 hit ID、hit flag、charge flag、saved/billed CNY micros 与 evidence status、固定 failure code、created/updated。

允许的单向状态包括：

```text
eligible -> miss_owner -> fill_stored | fill_skipped
eligible -> miss_non_owner
eligible -> hit_candidate -> hit | hit_blocked | settlement_failed
eligible -> corrupt_miss -> fill_stored | fill_skipped
disabled | ineligible | bypassed_experiment | unavailable_bypass
```

表不保存 entry ID、cache key、prompt hash、tag、data class 或正文。RequestLog API 只从本表和 hit 表做 tenant-qualified join。

### 6.7 `response_cache_hits`

不可变字段：hit ID、tenant、policy/version、request/trace、Virtual Key/model/provider、source owner ID 快照、charge flag、source generation/current reference/saved/billed 金额及独立状态、source/reference/billed PriceSnapshot owner、本次 usage/finops ID、pricing_observed_at、created_at。

source entry 后续可因过期/失效被删除，因此 hit 只保存稳定 ID 快照，不对 entry 建长期 FK；insert trigger 在形成 hit 的事务中验证当时 entry owner。Usage/FinOps、RequestLog 和 hit 必须一一匹配。

### 6.8 `response_cache_invalidations`

不可变字段：invalidation ID、tenant、actor type/user、scope、match mode、selector HMAC、tag count、matched/deleted/fenced counts、reason code、Idempotency-Key HMAC/request fingerprint、完整安全 response snapshot、operation request/trace、created_at。

用户操作使用独立 Control idempotency Secret/version 的 actor-scoped HMAC，并建立 `UNIQUE(tenant_id,actor_type,actor_user_id,idempotency_hmac_version,idempotency_key_hmac)`；系统腐坏/Output Safety 淘汰使用稳定 system actor type 和 `system_corrupt/system_output_blocked`，不伪造管理员。Policy 真实变更使用 `policy_changed`。TTL/容量清理不写本表。

### 6.9 `response_cache_events` 与 `response_cache_event_outbox`

- source event 只允许 `cache.hit/cache.invalidated`，payload 使用 strict canonical JSON。
- outbox 状态、attempt、generation、lease owner/token/expiry、snapshot frozen time、next attempt、error code、dispatched time全部 tenant-qualified。
- source row 与 outbox 在 hit/invalidation 同一事务创建。
- Control scheduler 以 90 秒 lease、最多 10 次和有界退避恢复；freeze subscription snapshot 后再网络发送。
- delivery 创建前/后崩溃均从同一 source owner 重建，事件 ID 稳定，不能因重试改变 payload。

### 6.10 `usage_ledger` 兼容扩展

SQLite 0069 保真重建 `usage_ledger`，唯一 Usage 表语义变化是 `usage_source` CHECK 增加 `response_cache`。列顺序、历史行、rowid/sequence、NULL 分布、索引、append-only/owner/FinOps/Output Safety dispute/experiment settlement/usage export sequence 等全部依赖 trigger 和外部 FK 必须逐项冻结、解除并恢复；迁移内比较总数、NULL 计数和全列双向 `EXCEPT`。

新 cache-hit Usage 的 normalized/raw JSON 使用 exact 白名单；raw 只含空对象或冻结 token 计数，不包含 Provider raw usage、Prompt 或 Response。

### 6.10A Output Safety 兼容扩展

0069 保真重建 `output_safety_scans` 及依赖的 `output_safety_events`/FinOps dispute/RequestLog owner triggers、索引和 FK，只增加 `not_billable_response_cache_candidate`：仅 `scan_mode=non_stream_full + completed + blocked + exposure=none + response-cache candidate` 可使用；所有真实 Provider 路径仍必须 `dispute_pending`。`security.output_blocked` exact payload/validator允许该 disposition，但 FinOps dispute trigger 只对 `dispute_pending` 创建 marker。历史行、rowid、NULL 分布和双向 `EXCEPT` 必须保真。

### 6.11 Audit 与 down guard

0069 还必须保真重建具有 exact CHECK 的 `audit_logs`，只增加管理 Audit action `response_cache.policy_created`、`response_cache.policy_updated`、`response_cache.invalidated` 及 object type `response_cache_policy/response_cache_invalidation`。列、历史行、索引、不可变/owner/Webhook 触发器逐项恢复；before/after 只保存安全配置和计数。

迁移测试先冻结受影响 `sqlite_schema.sql`、索引/trigger/FK 依赖图；up/down 的复制验证包含 rowid、NULL 分布和全列双向 `EXCEPT`。任一 preflight/down guard 失败都必须发生在 DDL 前，并证明业务表与 schema 均未改变。

0069 down 在任何 policy/event、entry/fill/key generation、request fact/hit/invalidation、source/outbox、cache Audit、cache Webhook subscription/delivery/attempt、`usage_source='response_cache'` Usage、cache Output Safety disposition、专用 PriceSnapshot/预算或相关 FinOps 事实、非默认 key-state/version/generation 存在时，必须在 DDL 前固定失败 `prd076_down_business_facts_present`。只有完全空的新事实库允许保真恢复 0068 的 RBAC revision 11、Usage/Audit/Output Safety CHECK。

SQL 无法检查外部 failure spool，因此 down/rotation 只能经受信 runner 执行：runner 在打开迁移事务前证明 pending/quarantine=0，再执行 SQL 内部 guard；直接绕过 runner 的 down 在生产文档中明确禁止，Runtime 必须覆盖该双层门禁。

## 7. Control API 设计

### 7.1 通用合同

- 继续使用现有 `Authorization: Bearer <Session access token>`；Console fetch 固定 `credentials: "omit"` 但显式发送当前内存 Bearer，禁止 Cookie、tenant/actor body 或自定义租户 Header。Token 绝不进入日志、DOM、错误或验收证据。
- 单个 Authorization、Idempotency-Key、X-Request-ID 等安全 Header 必须在 raw header 层拒绝重复/折叠歧义。认证/权限依赖在读取 body 前执行，随后 strict JSON、UTF-8、media type、duplicate/extra key、bool-as-int、深度/节点/字节上限校验。
- GET 只允许 `limit/cursor`，拒绝未知/重复 query 和任何 body；PATCH body 上限 16 KiB，invalidate 上限 8 KiB，JSON 深度 8、节点 256。写接口要求单一 `Idempotency-Key`，16..128 个可见 ASCII；服务端只保存独立 Control idempotency keyring 生成的 versioned HMAC，并以全部 retained verification version 防重复。
- 所有成功与错误响应都精确包含 `Cache-Control: no-store, private`、`X-Request-ID/X-Trace-ID`，并与 body 的 `request_id/trace_id` 一致。Console 对缺失、重复或不一致一律 `response_cache.response_invalid`，不渲染业务数据。
- API 不返回 cache entry、cache key、Prompt hash、route digest、ciphertext、source request、Usage/FinOps owner 或任何正文。

### 7.2 `GET /cache-policies`

要求 `response_cache.read`。query 为 `limit`（默认 50，1..100）与可空 opaque HMAC cursor；cursor 绑定 tenant、Alias 快照上界、排序和 limit。返回当前 tenant 的可配置 active Alias 安全摘要及可空 policy，排序固定 `(environment_code,alias,model_alias_id)`：

```json
{
  "tenant_id": "tnt_...",
  "items": [
    {
      "tenant_id": "tnt_...",
      "model_alias_id": "mla_...",
      "alias": "chat-default",
      "environment_id": "env_...",
      "environment_code": "prod",
      "cache_policy": {
        "cache_policy_id": "rcp_...",
        "tenant_id": "tnt_...",
        "model_alias_id": "mla_...",
        "status": "enabled",
        "data_class": "internal",
        "single_data_class_confirmed": true,
        "ttl_seconds": 900,
        "charge_cache_hits": false,
        "tags": ["finance"],
        "version": 3,
        "ready_entry_count": 12,
        "updated_at": "2026-08-10T00:00:00Z"
      }
    }
  ],
  "limit": 50,
  "next_cursor": null,
  "has_more": false,
  "global_enabled": true,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

GET 与 PATCH 复用同一个 exact `CachePolicySummary`，字段、nullability 和类型完全一致。envelope、item 和 policy 的 `tenant_id` 必须与当前 Auth owner 完全相同，policy 的 `model_alias_id` 必须等于父 item；Console 逐层镜像校验，任一漂移拒绝整个响应。ready count 只按当前 tenant+policy 查询，不返回 entry 标识。

Selector 不由服务端另发一份可漂移 DTO：PAGE-106 只能从已经校验的 item/policy 派生稳定排序、去重的 `policy:<policy_id>`、`model_alias:<alias_id>` 和 policy `tags`；policy 为 null 时只有 model_alias selector。finance_admin 因而取得完成工作所需的窄 Alias/selector 摘要，不获得 routing/model/Virtual Key 通用读取权限。高级 API 可接受调用方已知且后端验证 owner 的 model/virtual_key/data_class selector，但 UI 不枚举额外模型或 Key。

### 7.3 `PATCH /cache-policies`

要求 `response_cache.read + response_cache.write`。完整请求快照：

```json
{
  "model_alias_id": "mla_...",
  "status": "enabled",
  "data_class": "internal",
  "single_data_class_confirmed": true,
  "ttl_seconds": 900,
  "charge_cache_hits": false,
  "tags": ["finance"],
  "expected_version": 2
}
```

- 新建使用 `expected_version=0`；已有配置必须精确 CAS。
- tags 服务端规范化去重排序，重复输入固定 422，不静默去重隐藏客户端错误。
- enabled 时全局 feature/secrets 必须可用；否则 503 且不写 policy。
- HTTP 200 exact response：

```json
{
  "tenant_id": "tnt_...",
  "cache_policy": {
    "cache_policy_id": "rcp_...",
    "tenant_id": "tnt_...",
    "model_alias_id": "mla_...",
    "status": "enabled",
    "data_class": "internal",
    "single_data_class_confirmed": true,
    "ttl_seconds": 900,
    "charge_cache_hits": false,
    "tags": ["finance"],
    "version": 3,
    "ready_entry_count": 0,
    "updated_at": "2026-08-10T00:00:00Z"
  },
  "changed": true,
  "idempotent_replay": false,
  "invalidated_entry_count": 12,
  "fenced_fill_count": 1,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

- 同 actor + Idempotency-Key 同 fingerprint 重放持久安全业务结果，`idempotent_replay=true` 且使用本次链路 ID；不同意图固定 409。提交后响应丢失再重放必须返回完全相同的 policy/version/counts，不能重算已变化的数量。

### 7.4 `POST /cache/invalidate`

要求 `response_cache.read + response_cache.invalidate`。请求：

```json
{
  "scope": "tags",
  "match_mode": "all",
  "tags": ["tag:finance"],
  "reason_code": "manual_finops"
}
```

- `scope=all` 时 tags 必须为空；`scope=tags` 时 1-16 个 canonical tag。
- tag namespace 只允许 `policy/model_alias/model/virtual_key/data_class/tag`；owner ID namespace 必须验证当前 tenant，不存在与跨租户同形。
- reason 只允许 `manual_finops/manual_security/manual_configuration/manual_other`，不接受自由文本进入审计。
- response 返回 invalidation ID、scope、tag count、matched/deleted/fenced counts、`idempotent_replay` 和链路 ID。
- 0 match 仍为 200，且生成不可变 invalidation/event/outbox。

HTTP 200 exact response：

```json
{
  "tenant_id": "tnt_...",
  "invalidation": {
    "cache_invalidation_id": "rci_...",
    "tenant_id": "tnt_...",
    "scope": "tags",
    "match_mode": "all",
    "tag_count": 1,
    "matched_entry_count": 4,
    "deleted_entry_count": 4,
    "fenced_fill_count": 1,
    "reason_code": "manual_finops",
    "created_at": "2026-08-10T00:00:00Z"
  },
  "idempotent_replay": false,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

相同 actor + Idempotency-Key + fingerprint 重放以上安全业务事实并把 `idempotent_replay` 改为 true；不同 fingerprint 固定 409，不重复推进 generation、Audit 或 event/outbox。

### 7.5 RequestLog Response Cache 投影

既有 `GET /request-logs/{request_id}` 增加可空 exact 字段 `response_cache`；历史/不适用请求为 null，存在 PRD-076 request fact 时为：

```json
{
  "tenant_id": "tnt_...",
  "status": "hit",
  "hit": true,
  "charge_cache_hits": false,
  "cache_policy_id": "rcp_...",
  "cache_policy_version": 3,
  "source_generation_cost_status": "known",
  "source_generation_cost_cny": "0.010000",
  "current_reference_cost_status": "known",
  "current_reference_cost_cny": "0.012300",
  "saved_cost_status": "known",
  "saved_cost_cny": "0.012300",
  "billed_cost_status": "known",
  "billed_cost_cny": "0.000000"
}
```

- `tenant_id` 必须与外层 RequestLog/Auth tenant 镜像；金额只能是非负、六位小数普通字符串或 null。
- status exact enum：`disabled/ineligible/bypassed_experiment/unavailable_bypass/corrupt_miss/miss_owner/miss_non_owner/fill_stored/fill_skipped/hit/hit_blocked/settlement_failed`。
- `hit=true` 当且仅当 status=hit；此时 policy、charge flag 与四组状态必填，`known` 必须配非空金额，`unavailable` 必须配 null。saved 只有 current-reference 与 billed 都 known 时才可 known，且服务端整数微元等式必须成立。
- 非 hit 状态 `hit=false`；所有四组金额状态固定 `not_applicable` 且金额 null。eligible miss/fill/block/settlement failure 可返回 policy/version/charge flag，disabled/ineligible/bypass 可以为 null；不得伪造节省。
- DTO 禁止 entry/source owner、PriceSnapshot/Usage/FinOps ID、identity/key/route digest、tag、data class、Prompt/Response 或 Provider Credential。Control 与 Console 都做 exact extra/missing/type/组合校验。

## 8. Webhook 合同

### 8.1 `cache.hit`

payload exact 字段：

```json
{
  "schema_version": 1,
  "cache_hit_id": "rch_...",
  "cache_policy_id": "rcp_...",
  "cache_policy_version": 3,
  "charge_cache_hits": false,
  "source_generation_cost_status": "known",
  "source_generation_cost_cny": "0.010000",
  "current_reference_cost_status": "known",
  "current_reference_cost_cny": "0.012300",
  "saved_cost_status": "known",
  "saved_cost_cny": "0.012300",
  "billed_cost_status": "known",
  "billed_cost_cny": "0.000000"
}
```

event ID 使用持久 `rcev_`；actor 为 null；object ID 为 hit ID；trace 为当前请求 trace。

### 8.2 `cache.invalidated`

payload exact 字段：

```json
{
  "schema_version": 1,
  "cache_invalidation_id": "rci_...",
  "scope": "tags",
  "match_mode": "all",
  "tag_count": 1,
  "matched_entry_count": 4,
  "deleted_entry_count": 4,
  "fenced_fill_count": 1,
  "reason_code": "manual_finops"
}
```

用户失效 actor 绑定 Session user；系统安全淘汰 actor 为 null 且 reason 只允许 `system_corrupt/system_output_blocked`，Policy 变更为 `policy_changed`。普通 TTL/容量淘汰不发本事件。两类 payload 均禁止 Prompt、Response、Cache Key、Prompt hash、tag value、data class、Virtual Key、Provider Key、Token、Endpoint、nonce/ciphertext 和 source request ID。

### 8.3 `security.output_blocked` 兼容

既有 Output Safety event schema 保持 version 1 和字段集合，只把 `billing_disposition` exact enum 扩为 `dispute_pending/not_billable_response_cache_candidate`。后者只允许非流式 cache candidate、`exposure_state=none`、0 emitted counts；Webhook validator、source/outbox owner 与 scan 行必须完全一致。Provider 调用产生的 blocked 事件仍只能是 `dispute_pending`，不能借 cache 分支免除已发生的真实成本。

## 9. 统一错误码

| HTTP | code | 语义 |
| ---: | --- | --- |
| 401 | `auth.token_missing` / `auth.token_invalid` / `auth.token_expired` / `auth.session_revoked` | 沿用现有 Bearer Session 错误目录 |
| 403 | `auth.permission_denied` | 后端实时权限不足 |
| 404 | `response_cache.policy_not_found` | 当前 tenant 不存在目标 policy/Alias owner |
| 409 | `response_cache.version_conflict` | expected_version 与权威版本冲突 |
| 409 | `response_cache.request_conflict` | 同 actor + Idempotency-Key 对应不同意图 |
| 422 | `response_cache.input_invalid` | body/header/media/类型/TTL/tag/data class 非法 |
| 422 | `response_cache.query_invalid` | GET query 非法 |
| 503 | `response_cache.configuration_unavailable` | 全局 feature 或独立 Secret 不可用 |
| 503 | `response_cache.unavailable` | Control 存储、owner 或 outbox 事务不可用 |
| 503 | `gateway.response_cache_settlement_unavailable` | hit 的日志/Usage/FinOps/event 无法原子提交 |
| client | `response_cache.response_invalid` | Console 收到非 exact/no-store/owner 漂移响应，仅客户端固定错误，不由 Control 伪造 HTTP 状态 |

每个 401/403/404/409/422/503 都必须测试统一 body、响应头与 body 的 request/trace 一致、`no-store, private`，且错误响应不回显输入/Header。Gateway lookup、decrypt、capacity cleanup 故障默认安全旁路 Provider，并在 RequestLog 保存 `unavailable_bypass/corrupt_miss`；只有已经选定要返回 cache hit、但原子结算失败时才返回 503，绝不在未追踪情况下泄露缓存正文。

## 10. Console PAGE-106

### 10.1 页面组成

- 导航位于“调用与成本”，模型价格之后；只由后端导航中的 `response_cache.read` 决定。
- 页面为 Alias 策略表、右侧/文档流策略编辑器和标签失效对话框，不列出缓存正文或单条 Cache Key。
- 策略编辑使用 status toggle、TTL 数字输入、data class 文本输入、收费 toggle 和 machine tag 编辑器。
- 启用时显示并要求确认“此 Alias 只承载一个可信 data class”；混合分类说明是安全阻断状态，不把确认藏在默认值中。
- 失效工具使用 scope segmented control、tag multiselect、reason menu 和明确确认按钮。
- 调用日志详情新增“Response Cache”区，严格消费 7.5 DTO，显示 status、hit/miss、source generation/current reference/saved/billed 四组金额及证据状态，不与 Prompt Cache 亲和/Provider cache_tokens 合并。

### 10.2 权限与异步状态

- 页面 owner 精确绑定 tenant、Bearer Session、完整 permission set、RBAC revision/fingerprint 和后端 navigation。任一字段漂移都先清全部 DOM/state 再 Abort；新 owner 若仍有 read，再通过 fresh GET 重建只读页。撤销 write/invalidate 的最终页面可保留只读事实，但不能沿用旧 owner DOM、草稿或对话框。
- load/save/invalidate 三个 lane 各自使用 AbortController + generation；新 owner、离页、注销和撤权均 clear-before-abort。
- 接入现有 Shell `blocksShellDomReplacement`：后台 auth/navigation 刷新始终先完成权威校验；owner 完全相同时，`dirty/saving/conflict/unknown-result/retrying`、打开 invalidate Dialog 或 in-flight POST/PATCH 都阻止普通 DOM replacement；owner 任一漂移则无条件 clear-before-abort，任何撤权都不能被草稿/未知结果阻挡。单测冻结该真值表。
- GET 不覆盖 dirty/saving/conflict；409 必须显式 rebase，禁止 last-write-wins。
- 未知写结果只复用同一 intent 的 Idempotency-Key；每个 HTTP attempt 使用新的 X-Request-ID/服务端 trace。新意图生成新 Idempotency-Key，私有 operation ticket 必须把稳定 intent owner 与当前 attempt IDs 分开。
- 当前授权 owner 的私有内存渲染投影可以包含 data class/tags 草稿，否则无法校验和 rebase；但 localStorage/sessionStorage/URL/日志/DOM data attribute/验收 snapshot 禁止保存这些草稿。Bearer、permission fingerprint、request/trace、Idempotency-Key、Controller/generation 只能存在于不可序列化私有闭包；Prompt/Response、Cache Key 或 digest 在任何前端状态中都禁止出现。

### 10.3 响应式与可访问性

- 1280x720 使用紧凑表格/侧栏；390x844 转为单列文档流，Dialog 内滚动且 action 始终可达。
- 字段错误与固定后端错误使用 `aria-live`；toggle、menu、Dialog 具备 label、焦点圈和关闭后焦点恢复。
- 无横向溢出、文字/控件重叠或截断；危险失效动作不依赖颜色表达。

## 11. 涉及模块与代码修改计划

| 模块 | 计划 |
| --- | --- |
| `infra/migrations/0069_response_cache_optional_capability.sql/.down.sql` | RBAC revision 12、cache policy/entry/fill/tag/hit/invalidation/event/outbox、Usage/Audit/Output Safety 保真扩展、owner/immutable/down guard |
| `shared-sdk/python/metahub_shared/response_cache/` | canonical/HMAC、AES-GCM、tag、事件和 DTO 严格合同 |
| `shared-sdk/python/metahub_shared/rbac.py` | 三权限与 admin/finance_admin 默认矩阵 |
| `shared-sdk/python/metahub_shared/runtime/config.py` | 全局开关、cache HMAC/encryption、Control idempotency 与 failure spool 独立 secrets/version、容量/租约上限和 repr/secret 验证 |
| `shared-sdk/python/metahub_shared/runtime/webhooks.py` | `cache.hit/cache.invalidated` exact payload/envelope 白名单 |
| `gateway-data-plane/src/metahub_gateway/response_cache/` | eligibility、lookup、fill lease、crypto store、hit settlement、invalidation/cleanup |
| `gateway-data-plane/src/metahub_gateway/response_cache/failure_spool.py` | 仅固定失败 owner 的 HMAC+fsync 恢复，不保存 hit 或任何正文/摘要 |
| `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 安全顺序、实验旁路、hit/miss/fill finalizer、取消/失败收口 |
| `gateway-data-plane/src/metahub_gateway/budgets/` | 可信 cache-hit token/零收费预算上下文 |
| `gateway-data-plane/src/metahub_gateway/usage/ledger.py` | `response_cache` logical Usage、零收费/当前价证据和原子 hit settlement 可复用边界 |
| `gateway-data-plane/src/metahub_gateway/experiments/` | active scope cache-bypass 信号，不改变实验统计 |
| `gateway-data-plane/src/metahub_gateway/output_safety/` | cache candidate 的 not-billable blocked disposition 与 key fence，不改变 Provider 成本争议合同 |
| `control-plane-api/src/metahub_control/response_cache/` | policy GET/PATCH、invalidate、实时 RBAC、CAS/idempotency、Audit、outbox recovery |
| `control-plane-api/src/metahub_control/request_logs/` | tenant-qualified Response Cache 安全详情投影 |
| `control-plane-api/src/metahub_control/console/service.py` | PAGE-106 后端导航权限 |
| `control-plane-api/src/metahub_control/webhooks/` | cache source outbox drain 与 source owner recovery |
| `console-web/src/lib/responseCache.js` | strict no-store API client 和 DTO/错误校验 |
| `console-web/src/features/cache-policies/` | owner/generation/CAS 页面状态、渲染和事件绑定 |
| `console-web/src/features/request-logs/` | Response Cache 命中、节省/收费展示 |
| `console-web/src/app/main.js` 等 | 导航接线、离页/撤权清理、样式/i18n/权限常量 |
| `.env.example`、Compose examples/profiles | 安全开关、共享 Secret 和有界参数 |
| `scripts/maintenance/response-cache-key-rotate.py` | 停服轮换 key-state 的全租户 generation fence/purge；不接受或输出 Secret |
| `docs/api/response-cache.md` | API、Webhook、错误和财务口径 |
| `scripts/acceptance/prd076-runtime-acceptance.py` | fresh 0069、真实服务/Provider/Webhook、命中/隔离/收费/失效/恢复/隐私 |

所有新增或修改的 Python/JavaScript/SQL 模块、类、结构、字段、函数和关键分支必须提供解释“为什么”的详细中文注释，并通过仓库中文注释门禁；禁止用批量空洞模板注释冒充设计说明。

## 12. 测试设计

### 12.1 Shared 单元测试

1. typed canonical 对完全相同 payload 稳定；`1/1.0/1e0/-0.0`、极小/极大 finite float、tenant/Virtual Key/data class/model/body/options/route/policy/credential revision/HMAC version 任一语义变化按冻结规则产生相同或不同 key。
2. stream/Embeddings/内部/超限/非 JSON 值不生成 context；不截断后 hash。
3. repr 不包含 Prompt、Response、digest、Secret、Key 或 tag value。
4. per-entry HKDF + AES-GCM round-trip、nonce 唯一冲突重试、AAD 中任一 owner/TTL/route/token/source generation cost/version 漂移、tamper、非法 UTF-8、超限均固定失败；未来 hit reference/saved/billed 变化不参与 entry AAD。
5. tag namespace、排序、重复、owner 和 generation canonical。
6. 两个 Webhook exact payload/envelope 的正反、extra/missing/type/金额/id/敏感字段测试。
7. RBAC revision 12、69 个 permission template、185 条默认关系及 role matrix hash 精确测试。

### 12.2 Migration 测试

1. 空 SQLite 正向到 `69/0069`、重复 migrate、integrity ok、FK 0。
2. 0068 -> 0069 的 Usage/Audit/Output Safety scans+events 全列逐行、rowid、NULL、索引、trigger、外部 FK/依赖图保真；只增加 response_cache source、Cache Audit 枚举和 cache-only not-billable disposition。
3. policy、entry、tag、lease、hit、invalidation、event/outbox 的 ID、CHECK、owner、跨租户 FK、immutable、REPLACE、CAS/generation 反例。
4. cache hit Usage/FinOps 一一 owner，0 charge 与 charge 模式，source generation/current reference/saved/billed 不混用。
5. 独立空迁移库先执行 69 -> 68 -> 69，每阶段 integrity/FK；业务验收库产生事实后只验证 down runner/SQL guard 固定失败且 schema/业务数据不变，不能在同一个已产生事实的库声称成功 down。
6. key-state 初始化、停服轮换事务、旧/新进程交错、旧 lease/fill/tag 全部被 version+generation 围栏，Secret/fingerprint 不落审计或输出。

### 12.3 Control 集成测试

1. 匿名 401；admin/finance defaults；其他角色 403；动态撤权立即生效。
2. 认证先于 body：无权 + 非法 JSON 仍 403，不泄露 parser/schema。
3. tenant 只来自 Session；跨租户 Alias/policy/tag 同形，不能通过 body/header 覆盖。
4. GET 未配置/配置/全局关闭、tenant 镜像、窄 Alias/selector、分页 cursor、entry count、GET body/query/header 反例、no-store 和 exact response。
5. PATCH create/update/no-op、单一 data class 确认、CAS 409、稳定 Idempotency-Key + 每 attempt 新 X-Request-ID、跨 key-version replay/intent conflict、提交后丢响应重放相同 counts、非法 TTL/data class/tag/bool/extra/duplicate key/header。
6. Policy 变更删除 entry/fill、推进 generation，Audit/policy event 同事务；失败整体回滚。
7. all/tag invalidation、all-match、0 match、数据库 idempotency UNIQUE、并发相同/不同 intent、跨 Control idempotency version replay、tag generation fence、动态撤权。
8. invalidation/event/outbox、Webhook snapshot/retry/recovery/dead-letter 和 source owner tamper。
9. RequestLog 7.5 exact DTO 覆盖全部 status/nullability、四组金额 known/unavailable/not_applicable 与微元等式；跨 tenant 404，不返回 source owner/digest/tag/data class/正文。
10. 逐端点覆盖 401/403/404/409/422/503 的 exact error、no-store、Header/body request/trace 一致与输入不回显。

### 12.4 Gateway 专项

1. 全局/policy disabled 不查 entry；公共非流 Chat exact miss -> fill -> hit，第二次 Provider 调用数不增加。
2. tenant、Virtual Key、data class、resolved model、Prompt、任一完整请求字段、route/credential revision、单/多 KeyPool eligibility、HMAC/encryption version、policy/identity/key generation 变化均 miss。
3. stream、Embeddings、Direct、route_to、Enterprise Chat、Batch 和 active Experiment sampled/unsampled 全旁路。
4. DLP/Intent/Session Risk/Rate Limit 在 lookup 前阻断；阻断请求不读取/写入 cache。
5. hit 重新 Output Safety；blocked 不返回正文、不收费、使用 not-billable disposition 并推进 key generation；unavailable 保留 entry 且固定失败；规则/持久化失败统一收口。
6. fill 只发生于 initial route success + output allowed + RequestLog + committed Usage/FinOps；Provider error、Fallback success、spooled Usage、取消、过大 Response 均不写。
7. 两个并发 miss 只有一个 fill owner；lease 过期可接管；旧 owner/token/generation 不能覆盖。
8. all/tag/policy update、corrupt/Output Safety key fence 与在途 fill 交错，线性化点后旧写回为 0；0-match tag 仍 fence。
9. TTL 精确边界、容量淘汰、corrupt ciphertext/AAD/JSON 删除后 miss、cache DB 异常安全 bypass。
10. charge off 产生 response_cache Usage + 0 元 FinOps + 0 预算且 saved=current reference；charge on 使用当前价和精确 token 且 saved=reference-billed；source generation 不冒充 saved，缺价/预算阻断保持既有错误。
11. lookup 后跨 TTL/policy/route/tag/key-version 的 hit 二次验证转 miss，并原子替换 Budget 后复用 pending Output Safety owner；commit unknown 先 read-back，failure spool outcome_unknown 恢复完整/空/partial 三态且预算 exactly-once；成功时 scan/RequestLog/Usage/PriceSnapshot/FinOps/budget/hit/event/outbox owner 一致。
12. `cache_tokens` 与 PRD-050 estimated_hit 不变；日志和结构化异常无 Prompt/Response/Key/Token/digest/ciphertext。

### 12.5 Console 单元测试

1. API client exact tenant DTO、分页、Bearer + credentials omit、no-store、Header/body request/trace 镜像、固定错误和敏感字段拒绝。
2. read/write/invalidate 三能力 owner；任何 permission fingerprint 漂移全清并 fresh GET，导航只依赖 read，按钮不承担授权。
3. clear-before-abort、Shell replacement guard、三 lane generation、迟到成功/失败、新 tenant/session/RBAC owner 不复活。
4. dirty/saving/conflict 不被 GET 覆盖；409 显式 rebase；未知结果同 intent retry。
5. tag all-match、0 match success、Dialog focus/关闭、撤权销毁草稿与 DOM。
6. RequestLog 严格显示 hit/miss 与 source/current/saved/billed 四组 known/unavailable，不适用状态全 null，且不与 CacheAffinity/cache_tokens 合并。
7. 390/1280 CSS 无横向溢出、重叠、文字溢出和不可达操作。

### 12.6 Runtime 与 Browser 验收

- Runtime 在仓库内独占空 SQLite 启动真实 Gateway/Control/Console、本地 OpenAI-compatible Provider 和 Webhook；不 mock UI API；三服务 health 均为 200。
- 在不启动服务、不创建任何 PRD-076 事实的独立迁移 DB 先实际执行 `69 -> 68 -> 69` 并逐阶段记录 schema/integrity/FK；随后另建业务验收 DB 启动服务，产生事实后验证 down guard 在 DDL 前固定失败且 DB/schema 不变。
- 业务 Runtime 覆盖 admin/finance/无权角色、首次 miss/第二次 hit、Provider 调用计数、五维隔离、charge on/off、预算、TTL、并发 fill、标签/全量失效、Control 停机 outbox 恢复、commit-unknown failure spool 恢复、pending/quarantine 0 和所有 owned process 清理。
- Browser 通过真实 UI 点击创建/更新策略和标签失效；1280x720、390x844 覆盖 empty/configured/editing/真实双 Session 409/conflict/dialog/hit-log/read/write/invalidate 分级撤权。
- 页面检查 horizontal overflow、重叠、文本溢出、焦点恢复；覆盖 dirty/in-flight PATCH、打开/in-flight invalidate、迟到成功/失败不落地，以及 read 撤权后导航/DOM/state 清零和后端 403。
- artifact 采用 exact 安全白名单，可保存 schema/status/count/金额证据状态、固定 error code、脱敏 request/trace 和 PNG；截图非空、像素方差与 SHA-256 manifest 全匹配。截图流只使用公开纯合成 `fixture-public/fixture-tag`，并以 DOM allowlist + 人工/自动视觉复核证明没有随机隐私 canary。
- 隐私 canary 流与截图流完全分离且禁止调用 screenshot；对 Provider Key、Virtual Key、Authorization、Token、Password、Prompt、Response、真实 data class/custom tag、cache payload/key/hash、nonce/ciphertext、数据库路径分别使用独立随机 canary，并扫描 DOM/网络记录及非 PNG artifact 的原文/URL/base64/hex，命中均为 0。
- 最终清理固定 Browser profile、临时身份、端口、进程、数据库及 WAL/SHM/journal、failure spool 和验收临时目录；只保留白名单 artifact。

## 13. 实现步骤

1. 完成本设计第一轮自审，修正需求、权限、租户、投毒和 API 边界。
2. 完成第二轮独立自审，修正并发、失效、财务、兼容和运行验收边界。
3. 冻结 0069 schema/RBAC/Usage 保真迁移及 down guard，先写迁移专项。
4. 实现 Shared canonical/crypto/tag/Webhook/config/RBAC 合同及单测。
5. 实现 Control CachePolicy/invalidation/outbox/RequestLog API 及集成测试。
6. 实现 Gateway eligibility/lookup/fill/hit/settlement/budget/experiment bypass，按受影响路径逐组测试。
7. 实现 PAGE-106、RequestLog 扩展和 Console 状态机/单测。
8. 更新公开 API、README、目录说明和测试记录。
9. 执行最终迁移、必要回归、中文注释、真实 Runtime、Browser 和敏感/禁止文件审计。
10. 显式列出暂存文件，提交并推送 origin/main；随后只交叉确认下一项，不在同一 PRD 提交混入 PRD-077。

## 14. 设计自审记录

### 14.1 第一轮自审

状态：已完成并修正。主 Agent 与三个独立只读审查分别覆盖 Excel/架构、安全后端和 API/Console；编码仍未开始。

本轮发现与修正：

1. 认证误写为 Cookie 且禁止 Authorization：改为现有 Bearer Session + `credentials: omit`，沿用四个现有 401 code，并冻结 raw Header、no-store 和 request/trace 镜像。
2. Policy 静态 data class 可能形成伪隔离：增加单一分类确认事实，混合/未知分类 Alias 禁止启用，必须先拆 Alias/Virtual Key。
3. route digest 排除 Credential 会跨上游账号复用：加入不可逆 credential scope/key-pool revision；Secret 明文仍禁止落库。
4. typed canonical 的 float/拼接边界不确定：冻结 type tag、长度前缀、binary64 hex、domain/version 及 `1/1.0/1e0/-0.0` 语义。
5. AES nonce/AAD 与轮换不足：改为 per-entry HKDF、nonce 唯一冲突重试、完整不可变元数据 AAD，并新增数据库 key-state 和停服 fence/purge 轮换。
6. 请求顺序遗漏正文前 Session Risk preflight：恢复 PRD-070 两阶段合同，Cache lookup 只能位于全部输入安全门和 Rate Limit 之后。
7. lookup 后 TTL/policy/route/失效竞态：增加 hit settlement 二次资格验证和唯一线性化点；腐坏/Output Safety blocked 增加 key generation 围栏。
8. source 历史成本误命名为 saved：拆分 source generation、current reference、billed、saved，saved 仅等于当前参考价减本次收费；未知证据保持 null。
9. blocked cache candidate 的账务/争议未定义：新增 not-billable disposition，不创建 hit/Usage/FinOps，并与 key fence、失败 RequestLog 原子收口。
10. settlement 主库失败无法追踪：保留 hit 全原子且不 spool，只增加无正文/摘要/金额的 HMAC+fsync 失败终态恢复记录。
11. GET DTO 无 tenant、写响应无法重放 counts、API schema/分页/Header 上限不完整：补充逐层 tenant mirror、Idempotency-Key、安全 response snapshot 和三个端点 exact contract。
12. Console owner 局部撤权与完整 fingerprint 矛盾、snapshot 语义不清：任何 owner 漂移全清后 fresh GET，接入 Shell replacement guard，并区分私有内存草稿与禁止持久/证据序列化。
13. 0069 漏列 Audit 与 Usage 依赖图：补充 Audit 保真重建、Usage 的 FinOps/Output Safety/Experiment/Export 依赖以及 DDL 前 schema/rowid/NULL/双向 EXCEPT 门禁。
14. 系统失效与验收清理不闭环：冻结 system reason 与 TTL/容量非事件边界，扩展分级撤权、真实 409、69->68->69、health、独立 canary 和 WAL/SHM/profile/spool 清理。

### 14.2 第二轮自审

状态：已完成并修正。三个独立只读审查对第一轮修订版再次检查并发、财务、迁移、API/Console和兼容合同；主 Agent 逐项核对反例并回写正文。

本轮发现与修正：

1. key generation 与最终 key digest 循环定义：拆出不含 generation 的 identity digest，先查 generation 再派生 final key，entry/lease 双摘要 owner 校验。
2. Entry AAD 错绑未来 hit 金额：AAD 只含创建时永久不可变 source 元数据；reference/billed/saved 改由 hit settlement owner 保护。
3. 多 KeyPool Credential 响应域不确定：lookup snapshot 禁止解密/选择副作用，仅无认证、固定凭据或唯一可选 Credential eligible；实际 fill Credential 必须回验。
4. candidate 转 miss 会沿用低价 Budget/已终结 scan：candidate 只产生内存 terminal，验证失败原子替换为普通主备 Budget，并复用唯一 pending Output Safety owner。
5. blocked/unavailable/settlement failure 泄漏预算预留：所有未出网且未交付正文终态必须同事务归零；unknown outcome 保守保持到恢复器 exactly-once 决议。
6. commit 异常可能实际已提交：全部 settlement ID 预生成，先 fresh read-back；不可读才写 outcome_unknown，恢复按完整/全空/partial 三态处理。
7. current reference 没有不可变证据：Budget 前冻结 pricing_observed_at/reference PriceSnapshot；charge off另有 billed zero snapshot，charge on复用 reference，所有金额和预算使用同一证据。
8. cache-only Output Safety disposition 无法通过 0063 CHECK：0069 增加 scans/events及 owner trigger 保真重建，Provider blocked仍 dispute_pending，cache blocked才 not-billable。
9. Idempotency-Key 与 X-Request-ID 混用：稳定 intent 只复用 Idempotency-Key，每 attempt 使用新 request/trace；错误语义和 Console ticket 已分开。
10. 幂等 HMAC 轮换和 invalidation UNIQUE 不闭环：增加独立 Control active+retained keyring、跨版本查询及 policy/invalidation tenant+actor+version+HMAC 唯一约束。
11. GET/PATCH Policy DTO/selector owner 不一致：统一 `CachePolicySummary`，逐层 tenant/Alias mirror；selector 改由已验证 item/policy 派生，避免第二份漂移 DTO。
12. RequestLog 四组金额无 exact 合同：新增 7.5 DTO、全 status/nullability/金额等式和敏感字段拒绝矩阵。
13. Shell guard 遗漏 saving/conflict/unknown/in-flight 状态：冻结同 owner保护状态真值表，owner 漂移无条件全清；未知写每 attempt 新链路。
14. PNG 与 tag/data class 禁证据冲突：截图只用公开合成 fixture并做 DOM/视觉 allowlist；随机隐私 canary 独立流且禁止截图。
15. 69->68->69 与业务 down guard 时序冲突：独立空迁移库先完成 round-trip；业务库产生事实后只验证 down 在 DDL 前拒绝且 schema/data 不变。

## 15. 当前门禁

第 14 节两轮设计自审均已完成并修正，PRD-076 的迁移、Shared、Control、Gateway、Console、维护工具、
专项测试、必要回归和实际运行验收也已完成。0069 已在空 SQLite 上通过 fresh/repeat/down/re-up，最终为
`69/0069`、`integrity_check=ok`、外键违规 0；真实 Runtime 覆盖 hit/miss/fill、隔离、并发、收费、预算、
失效、停机恢复、spool/outbox 和 down guard。PAGE-106 Browser 覆盖双视口、UI 创建/更新/冲突/失效、
动态撤权、RequestLog hit、布局、隐私与清理。

权威 Runtime 为 `.artifacts/prd076-runtime/prd076-final-20260811-06/`，JSON SHA-256 为
`2fcfe8579b40a6d13ed9addc09193074ac036bb9d8f80ed3b12ee3dd98ffe19d`；权威 Browser 为
`.artifacts/prd076-browser/prd076-browser-final-20260811-24/`，manifest SHA-256 为
`514918fd6f4fb41de3a15b13e92567b4d288cf4c0c5fb6377610d5277a6c0976`。两者均保持
`all_108_prds_complete=false`，Provider Key、Token、Prompt 与 Response 明文命中为 0。

因此 PRD-076 当前状态为“已完成”。Excel 多表交叉确认下一条未完成项为 PRD-077，但本条不启动或实现
PRD-077。
