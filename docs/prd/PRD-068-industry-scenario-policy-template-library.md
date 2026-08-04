# PRD-068 - 行业/场景策略模板库

状态：**已完成**。国产模板跨环境
direct-model 旁路、upstream model 冻结、持续发布不变量、0060、数据面 mask、PAGE-095 及其失败关闭
边界均已实现并通过专项；唯一一次根级 Python 全量的 29 个原失败节点也已定向闭环。正式 API 真实
apply 已将基线推进到 Snapshot v12，数据库完整性和外键检查通过；应用内 Browser 的 `1280x720` 与
`390x844` 双视口、Snapshot 入口、权限状态和注销清理也已通过。详细证据见
`docs/testing/PRD-068-tests.md`。

总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表!A69:R69` 与 `03_M2-M3扩展交付!A5:R5` 一致定义：
  `PRD-068 | M2-企业增强 | P2 | 扩展层 | 策略模板库 | 行业/场景策略模板库`。
- 本条主要用户是安全、财务和交付人员。目标是在当前租户快速启用五个内置场景：
  低成本模式、高安全模式、只用国产模型、预算防爆、客服脱敏。
- Excel 只定义两个 HTTP 端点：`GET /policy-templates` 与
  `POST /policy-templates/{id}/apply`。`API-305..310` 是这两个端点被拆成六个词元后的追踪编号，
  不是六个独立接口。
- `PAGE-095` 是策略模板库页面；`OBJ-092` 是 `PolicyTemplate`；`EVT-075` 是
  `policy_template.applied`；`TC-0135/TC-0136` 分别覆盖主流程和异常、权限、安全、适配与回滚。
- PRD-067 已完成，仓库设计和测试文档连续到 PRD-067，README 已明确把当前开发项切换到 PRD-068；
  因此本条是按里程碑、优先级和 Excel 行序确认的下一条未完成 PRD，不是仅按编号猜测。

### 1.2 架构和既有合同

- 控制面负责策略配置；数据面只消费不可变 PolicySnapshot，热路径不能查询模板库或复杂控制面表。
- PRD-044 的 Policy DSL v1 已提供可信变量 `tenant/env/project/model/budget_remaining`，以及
  `allow/deny/mask/log_only/route_to` 动作；PRD-045 已提供 active candidate 编译、双 CAS、单调快照、
  上一版回滚、Gateway 缓存和 LKG。
- 现有快照只包含 `Policy/Rule/Action`，不包含 ModelAlias RoutingPolicy、BudgetRule 或 DlpRule。
  本条不能把路由、预算和 DLP 表的跨域写入伪装成可由 PolicySnapshot 回滚的内容。
- 现有发布器因没有真实执行器而拒绝 `mask`。本条的“客服脱敏”只有在补齐真正的 Gateway 输入脱敏执行器、
  发布器和回滚合同后才可标记兼容；不能用 `log_only` 或 `block` 冒充脱敏。
- 国产模型判断必须复用 PRD-063 的 `DOMESTIC_PROVIDER_TYPES`：
  `zhipu/dashscope/wenxin/volcengine/hunyuan/deepseek`。不得按 Provider 名称或 code 猜测，
  OpenRouter、硅基流动和客户本地 OpenAI-compatible Endpoint 均不属于该集合。
- 所有管理 API 必须从 Session 恢复 tenant/actor，并逐请求读取当前数据库权限矩阵；前端隐藏按钮不构成授权。

### 1.3 本期交付范围

1. 为每个租户物化五条只读、不可编辑的内置 PolicyTemplate；现有租户由迁移 seed，新租户由数据库触发器 seed。
2. `GET /policy-templates` 返回模板目录、适配状态、绑定选项、通用影响说明；带模板和绑定查询时返回
   当前租户的精确影响、preview nonce、head/candidate 前置条件和 tenant-bound `impact_hash`。
3. `POST /policy-templates/{id}/apply` 在单个 `BEGIN IMMEDIATE` 中创建一个模板来源 Policy、
   生成并推进不可变 PolicySnapshot、写应用记录/明细、Policy/Snapshot/Template 事件和三类审计。
4. Gateway 真正执行 `mask`：把它作为非终止修饰动作，在内存副本中完整脱敏 Chat 用户文本和文本型
   Embeddings 输入；不改客户端对象，不记录原文、命中文本或正文 hash。本期不借机新增仓库尚未承诺的 Responses API。
5. 既有 `POST /policy-snapshots/{id}/rollback` 在目标属于未回滚 template application 时同步停用该应用新增的
   Policy，并证明 current artifact 只删除这一项、保留其余已发布项；该分支生成补偿性 publish Snapshot，
   不伪装成 PRD-045 的“逐项复制上一版”rollback。立即撤销时内容与应用前来源一致，且下一次 publish 不会重上线模板。
6. PAGE-095 提供目录、绑定、精确影响预览、确认应用和结果快照入口；桌面与移动端均可完整操作。
7. 新增 `policy_template.read` 和 `policy_template.apply` 两个后端权限，授权上限和新租户默认值仅授予
   Admin、FinanceAdmin、SecurityAdmin、OpsAdmin；Developer/Viewer 不获得页面、预取或 API 权限。

### 1.4 明确不做

- 不做在线模板市场、购买、评分、上传、远程抓取、跨租户共享、客户自定义模板 CRUD 或模板脚本。
- 不创建 Provider、Provider Key、Model、ModelAlias、Project 或 BudgetRule；依赖缺失时返回适配失败。
- 不把 OpenRouter/硅基流动设为默认或国产上游，不增加境外模型主链路。
- 不扩展任意函数、正则、宏、代码执行、多动作链或任意 metadata；模板只能生成现有受限 DSL。
- 不提供任意历史快照跳转。非模板回滚继续遵循 PRD-045；模板 previous 路径只定位 application，撤销生成补偿性 publish。
- 不做输出侧脱敏；PRD-068 的客服脱敏只处理发送给 Provider 前的输入，输出安全属于后续 PRD-071。

## 2. 五个模板的可执行语义

五个模板都要求绑定一个当前租户 active Project。每次应用只新增一个 active Policy；同一 Project 同时只允许
一个未回滚的模板应用，避免模板之间形成无法由单条 Snapshot 回滚证明的组合状态。模板优先级由服务端目录固定，
但按安全动作与优化动作分层：高安全、国产、预算防爆和客服脱敏使用 `priority=10`，低成本路由使用
`priority=1000`。低成本必须排在常规安全策略之后，不能为了节省成本绕过既有 deny；页面必须展示实际求值顺序、
先行决定性 Policy 和遮蔽影响。

所有模板还要求租户已有一个 published Snapshot head。无 head 时 GET 返回
`incompatible + snapshot_baseline_required`，由具有 `policy.publish` 的人员先发布当前 active candidate 基线；
apply 不能偷偷创建额外基线快照，否则 Finance/Ops 的一次受限命令会产生未在影响预览中声明的第二次发布，
也会让三审计/三事件和回滚合同失去一一对应。

| template_code | 输入绑定 | 服务端生成的条件和动作 | 适配前置条件 | 实际效果 |
| --- | --- | --- | --- | --- |
| `low_cost_mode` | `project_id` | `project == <id>` + `route_to(<derived_model_id>)`，priority 1000 | 在同一 `preview_as_of`，当前租户至少有一个 active Model、active Provider、按既有资格算法为 available 的 KeyPool（`auth_type=none` 可无凭据），且 Model 显式声明 `chat=true/embeddings=true/stream=true` 并有当前有效 CNY 价格；Provider adapter 分类只能是国产云、local-compatible 或 customer-managed，显式排除 migration platform；价格先取生效 PricePlan，缺失时才取带 schema version 的 Model 目录缓存；按 1K 输入+1K 输出标准篮子总价、model_id 选择最低项 | Policy DSL v1 没有 request kind，故目标必须覆盖本期全部开放核心请求形态；仅对未被安全策略决定的请求固定路由到预览时最便宜模型，运行时仍按请求类型复核 capability，KeyPool/健康漂移则失败关闭 |
| `high_security_mode` | `project_id`, `model_alias_id` | `project == <id> and (env != <alias_env_code> or model != <alias>)` + `deny`，priority 10 | Alias、其 Environment、默认/备用 Model 和 Provider 均属于当前租户且 active；Alias 环境 code/ID 一并进入影响指纹 | 该 Project 只能在选定环境请求明确批准的模型别名；同名跨环境 Alias 也不能误获准 |
| `domestic_models_only` | `project_id` | `project == <id> and (env not in <frozen_env_codes> or model not in <derived_alias_names>)` + `deny`，priority 10 | 至少一个“全环境纯国产”Alias 名称，且每个允许名称在每个 frozen active Environment 都存在 active Alias；所有实例的默认/备用 Model active，Provider active 且 `provider_type` 属于国产权威集合；环境与名称列表各不超过 DSL 20 项 | 该 Project 只能在预览冻结的环境集合中请求服务端证明全环境覆盖的纯国产 Alias；某环境缺 Alias 时不能回退同名境外 direct Model，新环境默认被 deny，OpenRouter、硅基流动和 local/custom 一律排除 |
| `budget_burst_guard` | `project_id`, `threshold_cny` | `project == <id> and budget_remaining < <threshold>` + `deny`，priority 10 | Project 至少有一个 active Virtual Key，且锁内逐 Key 证明存在可适用的 active tenant/project/application/VirtualKey 预算事实；阈值是 0.000001..999999999999.999999 的 CNY Decimal | Gateway 读取当前请求权威最小剩余额度，低于阈值时在 Provider 前拒绝；新建但无预算事实的 Key 由缺失上下文继续 fail closed |
| `customer_service_redaction` | `project_id` | `project == <id>` + 非终止 `mask`，priority 10 | Project active；Control 的 operator-attested 部署门禁已启用，且当前 Gateway readiness 明确声明 `policy-mask-v1` | 对 Chat 用户文本和文本型 Embeddings input 中的手机号、18 位身份证号、电子邮箱做固定占位替换，再进入路由、DLP、Token/预算和 Provider 链路 |

### 2.1 求值顺序与遮蔽合同

- `mask` 是本期新增的**非终止修饰动作**。Shared 求值器保持唯一的 `(priority, policy_id)` 单次有界扫描：
  命中 `mask` 或 `log_only` 时记录后继续，命中首个 `allow/deny/route_to` 时结束；没有决定性命中时默认 allow。
  这样既不把 mask 短路成最终决定，也不为第二遍求值引入不同的超时、缺失上下文或解释顺序。
- `PolicyEvaluationResult` 新增有界 `mask_matches/mask_required` 安全摘要；Gateway 只有在最终决定允许继续时才执行一次
  copy-on-write 脱敏。最终为 deny/timeout/context-missing 时不处理正文，也不进入任何 Provider 前置阶段。
- 精确预览用三值逻辑把既有 Policy AST 中 `project` 固定为目标 Project、其他变量视为 unknown；只有能证明
  `false` 的规则才视为不重叠。分析必须使用包含派生 Policy ID 的**完整实际排序集**，同时检查模板前后，不能只看先行项：
  - 高安全/国产：前置可重叠 `allow/route_to` 会绕过 deny，后置可重叠 `route_to` 会在模板允许后把模型改出批准集合，均为 incompatible；任意位置更严格 deny 为 warning。
  - 预算防爆：前置可重叠 `allow/route_to` 为 incompatible；后置动作不能绕过已经命中的预算 deny，可按真实影响给 warning。
  - 客服脱敏：前置可重叠 `allow/route_to` 会跳过 mask，故为 incompatible；前置 deny 只会更严格，后置决定动作会在 mask 后执行。
  - 低成本：任何后置可重叠 deny 或 mask 都会被 route_to 短路，分别造成安全拒绝或输入脱敏被绕过，
    故为 incompatible；前置 allow/route_to 使模板无效也为 incompatible，前置 deny 与被模板遮蔽的
    后置 allow/route_to/log_only 均显示 warning。
- compatibility 状态固定为 `unbound/ready/ready_with_warnings/incompatible`。warning、阻断 Policy ID、
  排序键和解释码全部进入 `impact_hash`；Console 确认框必须展示，但客户端不能提交或覆盖排序结论。
- 国产候选按 Environment code、Alias 名称字典序构造。允许名称必须在预览时每个 active Environment 中都有 active
  同名 Alias，任一环境缺失即不允许该名称；合格名称或 frozen Environment 任一超过 DSL 20 项时分别返回 incompatible
  `domestic_alias_limit_exceeded/environment_limit_exceeded`，不得静默截断。条件同时检查 env 与 model，专门阻断
  “当前环境无 Alias -> Gateway 回退同名 direct Model”的跨环境旁路；新建环境不在 frozen list 中也默认 deny。
- 派生表达式还必须原样通过 shared compiler 的全部既有上限，包括 1,024 code point、2,048 UTF-8 byte、128 token、
  64 AST node、16 comparison、`MAX_AST_DEPTH=8`、`MAX_PARENTHESIS_DEPTH=3` 与 `MAX_NOT_DEPTH=1`。即使环境和
  Alias 各未超过 20 项，只要完整 canonical source 超过任一上限，也返回 incompatible
  `domestic_expression_limit_exceeded`；禁止截断名称、缩短环境集合、拆成语义不同的近似多规则或绕过 shared compiler。
- 应用成功后的安全语义不是一次性预览。普通 Snapshot publish、后续模板 apply 与模板补偿撤销共用
  `_validate_active_template_invariants(connection, tenant_id, candidate)`：对每条 active application 证明 generated item
  仍逐字段一致，并用当前完整排序集重跑上述双向遮蔽矩阵。任一新 Policy 令已应用模板可被绕过时固定 409、head 不变；
  不能只冻结模板自身图，却允许下一次普通发布把绕过项送进 Gateway。

### 2.2 DSL 构造安全

- 客户端永远不提交 `condition_expr`、action、priority、Policy ID、AST 或 hash。
- 服务端按固定 `template_code` 分支生成表达式，字符串字面量使用 canonical JSON 编码后交给 shared compiler，
  不能使用 SQL 拼接或对数据库模板正文做 `format/eval`。
- `policy_templates.condition_expr` 和 `action_json` 是只读展示蓝图；执行前必须与代码内固定目录和
  `definition_hash` 一致，否则 `policy_template.unavailable` 失败关闭。
- 精确预览生成 32 字节随机 `preview_nonce`。派生函数使用 domain-separated SHA-256，并至少绑定
  `tenant_id/template_code/template_version/bindings_hash/preview_nonce/resource_type`；由它生成本次
  application/policy/rule/action ID。Policy ID 保持现有 `pol_<32 lowercase hex>` wire；其真实派生值必须先生成再参与
  完整排序和遮蔽证明，不能依赖特制前缀、概率顺序或客户端提供 ID。
- nonce 只是确定性规划盐，不是“GET 曾签发”的 bearer 凭证，也不要求持久 preview 表。apply 必须带回 nonce，
  在锁内完全重算；成功 application 对 `(tenant_id, preview_nonce)` 唯一，其他 request 复用同一 nonce 返回冲突。

### 2.3 输入脱敏执行合同

- `mask` 只在 PolicySnapshot 已验证、修饰规则命中且最终请求可继续时执行；插入点固定在
  `policy_execution_error` 通过之后、`resolve_*route*` 之前，完整顺序为
  `Policy -> mask -> Routing/Cache Affinity -> DLP -> Rate Limit -> Budget -> Provider`。
- 只复制并替换 Chat `role=user` 的字符串 content、裸字符串 part、精确 `type=text` part，以及 Embeddings 的
  `str/list[str]` input。System/Assistant、URL、image/audio/file 和工具结构原样保留且不递归改写；未知 text 对象、
  嵌套/混合数组、数字 token 数组或超出完整遍历上限时整体 422，不能只脱敏前缀后放行剩余原文。
- 完整遍历上限冻结为最多 100 条 Chat message、每条 100 个 content part、总计 20,000 个 Unicode code point；
  Embeddings 最多 2,048 个字符串且总计 20,000 code point。先完整校验形态和计数，再创建 copy-on-write 副本，
  任一越界都不得留下部分改写结果。
- 仓库当前没有 `/v1/responses` 路由，README 也未承诺 Responses API；本 PRD 不新增或宣称该主链路。
- 受支持文本使用三个预编译、有界模式替换为 `[MOBILE_REDACTED]`、`[CN_ID_REDACTED]`、
  `[EMAIL_REDACTED]`。不记录原文、匹配片段、可逆 hash、位置或替换后的完整 Prompt。
- 命中 `mask` 但正文结构无法安全理解时返回 422 `gateway.policy_mask_unsupported`；执行器或 capability 损坏返回
  503 `gateway.policy_mask_unavailable`。两者都不得把原文继续发给 Provider。
- `request_logs` 新增 `policy_mask_applied` 与 `policy_mask_policy_id` 两个安全元数据字段；最终决定性
  `policy_decision/action_type/policy_id` 保持既有含义，从而同时追踪 mask 与 deny/route_to。不得新增明文、
  命中内容、位置或正文 hash 列；Dry-Run/Request Explain 只报告 modifier 是否命中和稳定 Policy ID。

## 3. 权限与租户隔离

### 3.1 新权限

| permission code | 风险 | 默认/上限角色 | 用途 |
| --- | --- | --- | --- |
| `policy_template.read` | read | Admin、FinanceAdmin、SecurityAdmin、OpsAdmin | 读取五条租户内目录、适配选项和影响预览 |
| `policy_template.apply` | critical | Admin、FinanceAdmin、SecurityAdmin、OpsAdmin | 仅执行服务端固定模板并原子发布快照 |

- `CATALOG_REVISION` 从 4 提升为 5；Admin 继续拥有全集。Developer/Viewer 的集合和 hash 不因本条增加权限。
- 运行时只认数据库 `role_permissions`；shared 默认集合只用于产品上限、新租户初始化和迁移校验。
- `read` 与 `apply` 必须分开守卫。应用权限是受限业务命令，不隐式授予原始 `policy.write/publish`；
  Finance/Ops 因而不能借此调用 `/policies` 或 `/policy-snapshots/publish`。
- apply 先要求当前数据库中的 `policy_template.apply`，再按模板类别执行数据库权限的职责分离复核，不按角色名补权：
  `high_security_mode/domestic_models_only/customer_service_redaction` 还要求 `policy.write`；
  `budget_burst_guard` 还要求 `billing.budget.write`；`low_cost_mode` 还要求 `billing.budget.write` 或 `routing.write`
  至少一项。这个复合守卫只授权服务端固定命令，不隐式授予调用方原始 CRUD/publish；预览返回当前模板的
  `can_apply/requires_permissions` 安全摘要，路由与写锁内必须用数据库矩阵各复核一次。
- 既有快照 rollback 仍要求 `policy.publish`。Finance/Ops 应用模板后如需回滚，由具有发布权限的 Admin/Security
  执行；PAGE-095 只展示可交接的 rollback target，不给未授权角色伪造回滚按钮。

### 3.2 隔离规则

- 所有 `policy_templates`、application、item、event 查询和写入都以 Session tenant 为首要条件。
- Project、Alias、Model、Provider、Budget、Policy、Snapshot、actor 的所有关联使用 `tenant_id + resource_id`
  复合外键或同等数据库触发器；全局主键不能代替 owner 证明。
- 路径 ID 不存在和跨租户统一返回 `policy_template.not_found`；绑定资源不存在、跨租户或 disabled 统一返回
  `policy_template.binding_invalid`，不得暴露对象是否存在于其他租户。
- `tenant_id`、actor、权限、资源版本、快照版本和价格版本都不允许来自请求正文。

## 4. 数据库设计

迁移编号：`0060_industry_scenario_policy_template_library.sql`，同时提供 `.down.sql`。

### 4.1 `policy_templates`

| 字段 | 约束和用途 |
| --- | --- |
| `tenant_id` | NOT NULL，复合主键/外键第一维 |
| `policy_template_id` | 固定 `ptpl_*`，与 tenant 组成主键；五个租户可见稳定路径 ID |
| `template_code` | 五值枚举，tenant 内唯一 |
| `name`, `description`, `category`, `scenario` | 有界中文展示元数据，不进入运行日志 |
| `status` | 固定 `active` |
| `version` | 固定从 1 开始；本 PRD 不提供更新 API |
| `priority` | `low_cost_mode=1000`，其余四模板为 10；按 template_code 做 CHECK/目录双校验并进入 definition hash |
| `dsl_version` | 固定 `policy-dsl-v1` |
| `condition_expr` | 只读蓝图，不直接执行 |
| `action_json`, `binding_schema_json` | 精确 JSON object/array，大小和字段集受 CHECK/服务目录双重约束 |
| `definition_hash` | 对不含 tenant/time 的 canonical 定义计算 SHA-256 |
| `created_at`, `updated_at` | UTC 时间；seed 时相同 |

表禁止 UPDATE/DELETE。迁移为所有现有 tenant 插入五条；`AFTER INSERT ON tenants` 触发器为未来 tenant 插入。

### 4.2 `policy_template_applications`

| 字段组 | 字段与约束 |
| --- | --- |
| 身份 | `application_id` 全局主键，`tenant_id`，复合唯一 owner |
| 模板 | `policy_template_id`, `template_version`, `template_definition_hash`，复合 FK |
| 幂等 | `actor_user_id`, `request_id`, `trace_id`, `request_fingerprint`；`UNIQUE(tenant_id, request_id)`，命中后再校验原 actor/fingerprint |
| 预览 | `preview_nonce`, `preview_as_of`, `applied_as_of`, `bindings_json`, `bindings_hash`, `impact_hash`, `applicability_fingerprint`, `before_candidate_hash`, `after_candidate_hash`；`UNIQUE(tenant_id, preview_nonce)` |
| 快照 | `previous_snapshot_id/version`, `result_snapshot_id/version`, `rollback_snapshot_id/version`；previous/result ID 均有 tenant 内唯一定位约束 |
| Policy | `generated_policy_id` 使用 `(tenant_id,id)` FK 且 tenant 内唯一；`generated_policy_version=1` 是应用时不可变事实，由 insert trigger 核验而不参加 FK；`rolled_back_policy_version` 仅在撤销时写 2 |
| 状态 | `status in ('applied','rolled_back')`；只允许一次 `applied -> rolled_back` |
| 投递恢复 | `policy_audit_log_id/policy_event_id`, `snapshot_audit_log_id/snapshot_event_id`, `apply_audit_log_id/template_event_id`；回滚另存三条 audit ID 与 rollback snapshot event ID |
| 回滚幂等 | nullable `rollback_actor_user_id`, `rollback_request_id`, `rollback_trace_id`, `rollback_request_fingerprint`；仅 rolled_back 时全有 |
| 时间与 actor | `created_at`, `applied_as_of`, `rolled_back_at`, `rolled_back_by`；apply/rollback actor 都使用同租户复合 owner 约束 |

同一 tenant+project 在 `status='applied'` 时只允许一个模板应用；project ID 从规范化 bindings 投影到独立列，
不能依赖 JSON 文本唯一性。`UNIQUE(tenant_id,previous_snapshot_id)` 与
`UNIQUE(tenant_id,result_snapshot_id)`、`UNIQUE(tenant_id,generated_policy_id)` 与 nullable
`UNIQUE(tenant_id,rollback_snapshot_id)` 使既有 rollback 路径无需猜 Project 即可唯一定位 application；快照 ID 全局单调新建，
后续 application 不会复用历史 previous。application 与预生成 Audit/Event ID 使用
复合 FK；六个 apply fact ID、四个 rollback fact ID 各自 tenant 内一对一 UNIQUE，apply facts 全部 NOT NULL，
rollback facts、rolled_back actor/time/version 按 status 做全有或全无 CHECK。只有 application 指向稍后插入的 Template
Audit/Event 两条环边使用 `DEFERRABLE INITIALLY DEFERRED`，其余 owner FK 立即校验。除一次受控回滚状态转换外，
其余字段不可变；`generated_policy_version` 不可做当前 Policy version FK，否则 version+1 的受控撤销会被错误阻断。

### 4.3 `policy_template_application_items`

每次应用按稳定顺序写两条不可变摘要：`policy/create` 与 `policy_snapshot/publish`。字段包含 tenant、application、
resource_type、operation、resource_id、before_version/hash、after_version/hash、created_at；不保存 Prompt、Response、
Provider Key、Token、模板渲染正文或匹配文本。

### 4.4 `policy_template_application_dependencies`

- 保存 apply 时参与适配证明的 tenant-bound 资源摘要：Project、Environment、Alias、Model、Provider、PricePlan、
  BudgetRule、VirtualKey 和 runtime capability；字段包括 `resource_type/resource_id/relation/version_or_hash`，
  以及用于国产同名 Alias 防漂移的有界 `resource_name`，不保存 URL、Key、价格正文或预算余额。
- 任一 application 的 generated Rule/Action 与 Policy 语义均永久冻结：普通 Policy API 不能修改、禁用、删除或替换
  表达式/动作/priority/target。唯一例外是受控 rollback 先把 application 以预生成 rollback facts 原子转换为
  `rolled_back`，再允许 Policy 从 active/version 1 精确变为 disabled/version 2；完成后任何 version 3 更新或删除仍被拒绝，
  失败时整笔恢复为 applied/version 1。
- 对 high-security/domestic 的 active application，禁止 Environment code、Alias 环境/名称/默认/备用绑定，冻结
  Model.provider_id/provider_model_id/upstream_model_name/core capabilities，并禁止 Provider.provider_type/base_url/auth_type
  发生会削弱证明的变更；国产模板还禁止插入、改名或把 disabled 实例重新
  启用为一个同名非受证明 Alias。
- low-cost 同样冻结目标 Model 的 provider_id/provider_model_id/core capabilities，以及 Provider 的
  provider_type/base_url/auth_type 和 adapter classification，防止同一 model_id 被重绑到 migration/OpenRouter 等目标。
  Model/Provider/Project 可从 active 变 disabled 作为应急 fail closed，但不得借重新启用同时改变受保护语义；
  KeyPool 凭据轮换和健康状态不冻结，缺 Key/不健康在运行时失败关闭。需要重绑、改分类或换目标时必须先回滚模板。
- 低成本预览与 apply 必须复用现有 `snapshot_provider_key_pool` 资格语义：`auth_type=none` 可无 Key；有托管密钥时只认
  `aesgcm-v1` 且 status/rate-limit/probe freshness 可选择的 credential，不得回退环境变量；没有托管密钥时仅默认租户、
  配置完全匹配的环境 Key 可用。该纯资格算法下沉 shared 供 Control/Gateway 同源调用，不在 Control 复制近似 SQL。
  只有 `keypool_state=available` 可入选；`unavailable/unknown/not_evaluated` 都不能按可用猜测。Provider 新鲜 unhealthy
  直接排除，unknown/stale health 形成 warning 并进入 CAS，运行时仍按最新状态失败关闭。
  CAS 只保存 keypool state 与对 credential ID/status/rate_limited_until/probe status/time、环境 Key presence 做的安全 hash，
  不保存密文、key prefix、环境变量名或明文；Provider health state/generation/updated_at 同样进入 applicability fingerprint。
- low-cost 的选择结果是应用时固定路由，不随价格自动重算；PricePlan 变化只把 Console 状态标为 stale，
  不阻塞正常定价维护。若需重新选择最便宜模型，必须回滚并重新预览/应用。

### 4.5 运行能力部署门禁

- 新增非敏感配置 `METAHUB_POLICY_MASK_RUNTIME_ENABLED`，默认 `false`。Gateway 新版本只有在实现和自检
  `policy-mask-v1` 后才在 readiness/health 安全投影中声明该 capability；旧 Gateway 会继续把 mask 失败关闭。
- Control 只有在该 operator-attested gate 为 true 时才把客服模板判为 ready。生产变更顺序固定为：先滚动升级全部
  Gateway、由负载均衡 readiness 证明所有接流实例都声明 capability，再启用 Control gate；回滚顺序相反。
  该配置是部署级人工证明，不把“任意一个实例曾心跳”误当成全实例证明；mixed-version、gate false 和自检失败都禁止 apply。
- application dependency 持久化 `policy-mask-v1` 与部署 gate 版本摘要；不保存主机名、内部地址或配置全文。

### 4.6 `policy_template_events`

- 只允许 `policy_template.applied`；绑定 tenant、application、template、result snapshot、actor、audit、request/trace。
- payload 必须恰好九个 key：`template_code/template_version/application_id/generated_policy_id/
  previous_snapshot_version/result_snapshot_version/impact_hash/bindings_hash/changed_resource_types`；拒绝缺失或额外键，
  不含 DSL、具体绑定值、价格、预算阈值或敏感正文。
- source 行不可更新/删除，也不复制 delivery 状态；提交后由既有 Webhook delivery/attempt/dead-letter 表承载
  投递状态和重放，template event 始终只是稳定来源事实。
- migration 只扩展 Webhook 事件目录和 subscription/delivery CHECK，不自动改写任何 endpoint 的订阅 JSON；用户在升级后
  主动订阅该事件即形成治理事实并阻断 down，避免迁移自己生成的订阅让空库也永远无法降级。

### 4.7 既有表变更

- 扩展 `audit_logs`：新增 `policy_template.applied`、`policy_template.rolled_back`，object type
  `policy_template_application`；重建时逐列保真全部 0059 数据、索引和不可变/owner 触发器。
- 权限目录增加两项并把角色 catalog revision 提升至 5，更新现有租户矩阵和新租户 seed/hash。
- 不改变 PolicySnapshot wire schema；`mask` 本来就是已知动作枚举，只移除发布/运行层的旧拒绝分支。
- `request_logs` 增加 `policy_mask_applied INTEGER NOT NULL DEFAULT 0 CHECK IN (0,1)` 与
  `policy_mask_policy_id`。ID 保存首个按 `(priority,policy_id)` 命中的 mask，即使随后 422/503 也保留；applied 只有完整
  copy-on-write 成功后才为 1，约束为 `applied=1 => id IS NOT NULL`。upsert 单调合并：true 不得被后续默认 false 覆盖，
  两个不同非空 ID 视为存储冲突；spool、查询和 Explain 使用同一安全投影，禁止正文或命中 hash 进入新列。
- 为 application 的同租户 deferred FK 增加 `policy_events(tenant_id, policy_event_id)` 与
  `policy_snapshot_events(tenant_id, policy_snapshot_event_id)` 唯一索引；不能只依赖全局 event 主键证明 owner，
  down 在移除 application 后精确删除这两个辅助索引。
- 同理增加 `audit_logs(tenant_id,audit_log_id)` owner 唯一索引。apply 阶段只有 application 指向未来 Template
  Audit/Event 的两条环边 deferred；受控撤销为先打开 graph 写保护，指向未来 compensating Snapshot、三 Audit 和
  snapshot event 的 rollback 边也必须 deferred。所有 ID 先生成、事实后插入、提交前逐项按 tenant+ID 回读，
  提交后才独立派发。
- down guard 在首项持久 DDL 前同时检查 application/item/event、模板 Audit、用户新增的 Webhook subscription、
  delivery/attempt/dead-letter、role_permission_events、自定义权限矩阵、catalog/hash/revision、未知后续 schema 引用、
  `foreign_key_check` 和 `integrity_check`。存在任一应用或治理证据时失败关闭；无业务事实时逐列精确
  恢复 request_logs、Audit、Webhook CHECK 与 revision 4，不能删除 delivery 绕过门禁。guard 还必须阻断任一
  `policy_mask_policy_id/applied` 证据、active mask Policy 或含 mask item 的 published Snapshot；unknown schema 扫描覆盖
  两个新列名。request_logs down 换表按完整 0059 列、rowid、全部既有 index/trigger 做双向 EXCEPT 保真。

## 5. API 设计

所有响应设置 `Cache-Control: no-store`，成功和失败都由共享中间件携带 `request_id/trace_id` 和响应头。

### 5.1 `GET /policy-templates`

无绑定时返回五条目录及 generic impact；精确预览使用同一路径：

```http
GET /policy-templates?template_id=ptpl_budget_burst_guard&project_id=prj_x&threshold_cny=100
Authorization: Bearer {session}
```

允许的 query 仅为 `template_id/project_id/model_alias_id/threshold_cny`，重复、未知、超长或模板不需要的绑定
返回 `policy_template.query_invalid`。无 `template_id` 时不得夹带绑定。

单条模板的安全投影包含：

- `policy_template_id, tenant_id, template_code, name, description, category, scenario, status, version, priority`；
- `condition_blueprint, action, required_bindings, binding_options`；选项只含当前租户安全 ID/name/status/价格摘要；
- `compatibility.status/reason_codes/warning_codes`，状态值为
  `unbound/ready/ready_with_warnings/incompatible`；
- `impact`：Project、生成资源种类、Policy evaluation order、before/after policy count、
  derived target/allowed aliases 的安全目录字段、head 版本、rollback target 和潜在 shadowing Policy ID；
- ready/ready_with_warnings 时额外返回 `preview_nonce, preview_as_of, impact_hash, before_candidate_hash,
  after_candidate_hash, expected_head_version, generated_resource_ids`。每次真实 GET 必须生成新 nonce，因此派生 ID、
  after/impact hash 可以变化；只有“给定相同 nonce 与相同数据库事实”的纯 planner 输出必须逐字节稳定。
- 精确预览同时设置强响应头 `ETag: "<head version>"` 与
  `X-Policy-Candidate-ETag: "<before_candidate_hash>"`；JSON 中保留同值便于 Console 展示，但 apply header 只能取该次
  响应冻结值，不能由客户端自行拼接。

跨租户资源不出现在 binding options；响应不含 Provider URL、Key、Credential、Prompt/Response、完整价格规则或 SQL。

### 5.2 `POST /policy-templates/{policy_template_id}/apply`

```http
POST /policy-templates/ptpl_budget_burst_guard/apply
Authorization: Bearer {session}
Content-Type: application/json
If-Match: "3"
If-Policy-Candidate-Match: "<before_candidate_hash>"
X-Request-ID: req_...

{
  "template_version": 1,
  "preview_nonce": "<64 lowercase hex>",
  "preview_as_of": "2026-08-02T12:00:00.000000Z",
  "impact_hash": "<64 lowercase hex>",
  "bindings": {
    "project_id": "prj_x",
    "threshold_cny": "100"
  },
  "reason": "客服项目预算防爆"
}
```

- 正文仅接受 UTF-8 JSON object，最大 32 KiB、深度 8、节点 128；拒绝重复 key、NaN/Infinity、未知字段、
  bool 冒充数字、tenant/actor/Policy/快照字段和非法控制字符。
- `preview_as_of` 必须原样带回并进入 request fingerprint/impact canonical 输入。锁内先用该时点、nonce 与当前数据重建
  `preview_plan` 并核对原 impact hash；再取得单一 `applied_as_of` 构建 `current_plan`。两者另计算排除观察时钟、
  但包含价格 source/version/effective interval/amount、授权、head/candidate 与全部依赖的
  `applicability_fingerprint`，并要求 applied_as_of 仍落在同一价格有效区间。application 同时持久化两时点与 fingerprint；
  无事实变化时允许通过，跨 PricePlan 生效边界或任一依赖漂移为 409，不拿 applied_as_of 替换 preview hash。
- `If-Match` 复用现有强 head ETag 格式 `"N"`，candidate header 必须是强 quoted lowercase SHA-256
  `"<64 hex>"`；拒绝 weak、`*`、多值、未加引号或前后空白变体。无 head 只能用 `"0"` 表达读到的状态，
  但模板会先以 `snapshot_baseline_required` 判 incompatible，绝不会成功创建首个模板快照。
- 缺任一强前置 header 返回 428 `policy_template.precondition_required`；Content-Type 不是
  `application/json` 返回 415 `policy_template.media_type_unsupported`。header/JSON 形态非法仍为 422，
  与锁内事实漂移的 409 严格分开。
- 服务端持写锁后重算权限、模板、bindings、适配依赖、before/after candidate 和 impact hash；任一漂移不写任何对象。
- 成功为 `201`，响应含 `application`、`generated_policy` 的安全摘要、`policy_snapshot`、
  `rollback_target_snapshot_id`、`idempotent_replay`、`request_id/trace_id`，并按同一强格式设置新 head `ETag: "N"`。
- 原样使用 tenant+request ID 重试，先校验原 actor/fingerprint，再从持久 application/event/audit 恢复同一结果并
  补派相同 event ID；不同 actor/意图统一 request conflict。application 后来已回滚时仍回放原 apply 成功事实，
  响应同时显示当前 `status=rolled_back`，绝不重新应用，也不让同一成功请求随时间变成 409。

## 6. 单事务应用流程

1. 路由先完成 Session、`policy_template.apply`、tenant 内模板路径解析及第 3.1 节分类复合权限校验，全部通过后才读取
   有界正文；写锁内仍会复核同一矩阵，不能把外层检查当持久授权。
2. 服务层规范化路径、header 和正文，计算包含 reason hash 但不含 reason 正文的 request fingerprint。
3. `BEGIN IMMEDIATE`，按 tenant+request 查 application；命中则校验原 actor/fingerprint，并一次性恢复、交叉核验
   Policy Audit/Event、Snapshot Audit/Event、Template Audit/Event 六个持久投递事实。任一缺失、重复或漂移都
   503。若 application 未命中，还要检查同 tenant 的 PolicySnapshot request_id 是否已被其他 publish/rollback 占用，
   命中即 request conflict，不能等数据库唯一约束把它折叠为 503。全部核验后回滚只读事务，再逐一尝试六个稳定 ID；
   外部可暂时部分成功，由 delivery 幂等键和同 request 回放补齐，单次失败不得跳过后续派发。
4. 重读当前数据库权限，防止长请求期间被撤权；同时复核 `policy_template.apply` 与第 3.1 节模板分类复合权限，
   再按 tenant 加载模板、Project 和模板专属依赖。
5. 加载当前 snapshot head，比较 `If-Match`；重编 before candidate，比较 candidate header。
6. 在写锁内按 5.2 节先重建 preview plan，再以新的 `applied_as_of` 重建 current plan并比较
   `applicability_fingerprint`；验证 template version、preview nonce 派生 ID、preview_as_of、bindings/impact hash、
   策略 quota 和同 Project 未回滚应用。`impact_hash` 精确覆盖 tenant、actor 授权 revision/hash、模板
   code/version/definition、preview_as_of、bindings、nonce/派生 ID、head/version、before/after candidate、Project 状态、
   Policy 数量与完整排序、价格 plan/source/version/effective_at/金额、Alias/Environment/Model/Provider 状态与
   provider_type、Provider health generation/time、KeyPool eligibility hash、逐 Key 预算适配事实、mask capability/version；
   任何跨价格生效点或依赖变化都冲突。
7. 先用 connection-level Policy helper 插入 Policy/Rule/Action，回读完整图，写 `policy.created` Audit 和持久事件；
   再次编译 active candidate，必须等于预览的 after hash。
8. 对加入新模板后的 candidate 调用 active template invariant helper；通过后才用 connection-level Snapshot helper 创建
   `head+1` building/items，封印 published，写 snapshot event、推进 head、写 snapshot Audit。普通 publish 在 candidate
   双 CAS 之后、创建 building 行之前调用同一 helper；补偿撤销在 application 转为 rolled_back 后对其余 active 应用调用。
9. 所有 application、三条 Audit、三条 source event 的 ID 在插入前生成；Policy 与 Snapshot 的前四个事实完成后
   插 application，insert trigger 精确核验其 tenant/actor/request/trace/resource/hash；随后写 items/dependencies、
   Template Audit 和 Event，二者的反向 owner trigger 校验 application 预留 ID 与 payload。六事实共用一个 applied_at，
   提交前按显式 tenant+ID 严格解码回读。deferred FK 只解决最后两条环边，不能允许缺失或语义漂移事实提交。
10. `commit_audited_transaction` 一次提交。任一 SQL、审计、触发器、hash 或编译故障整体回滚。
11. 提交后分别派发三个 Audit、Policy created、PolicySnapshot published 和 Template applied Webhook；六次派发
    各自包在独立失败边界内，前一个抛错不能跳过后五个，也不能反转 201。原稳定 event ID 由 outbox/幂等回放补投。

现有 Policy 和 Snapshot 顶层 service 继续使用同一批 connection-level helper，避免模板服务复制一套稍有差异的
编译、owner、审计或封印逻辑。

### 6.1 Apply 审计与事件精确合同

- `policy.created`：沿用现有 Policy 白名单，`before={}`；`after` 精确包含
  `name/status/priority/version/dsl_version/action_type/target_model_id/condition_hash/variables/description_hash`，
  `changed_fields` 为这些 key 的字典序全集。
- `policy_snapshot.published`：沿用 PRD-045 的 current head `before` 与新 artifact `after`；字段只含版本、
  content/artifact hash、policy_count、operation、previous/rollback source version、approval_status、change_reason_hash，
  `changed_fields` 由既有 audit helper 对 before/after 唯一重算。
- `policy_template.applied`：`object_type=policy_template_application`、`object_id=application_id`、`before={}`；
  `after` 精确包含 `status=applied/template_code/template_version/generated_policy_id/previous_snapshot_version/
  result_snapshot_version/bindings_hash/impact_hash/changed_resource_types`，`changed_fields` 为这九项的字典序全集。
- 三条 source event 分别是既有 `policy.created`、既有 `policy_snapshot.published` 与 EVT-075
  `policy_template.applied`；后者 payload 只使用 4.6 节安全摘要。apply 回放按显式 ID 核验 tenant、actor、
  action/object、request/trace、before/after/changed_fields、event type/payload/audit 关联和资源版本，任一不一致整体 503，
  不得根据当前模板或请求正文重新推导历史投递事实。

## 7. 模板感知回滚

`POST /policy-snapshots/{previous_id}/rollback` 仍只接受该 application 在 apply 前记录的
`previous_snapshot_id`，但模板分支允许 current head 是 result snapshot 或其后的已发布 head。这样不会新增第三个
模板端点，也不会把任意历史跳转开放给普通 Snapshot；路径 previous ID 只用于唯一定位 application 和校验用户意图。
模板撤销的精确含义是“从当前已发布制品中删除该 application 唯一生成的 Policy item，并保留之后已经发布的其他
item”，其结果是一个 wire v1 补偿性 **publish**，而不是声称复制 previous source 的 PRD-045 rollback。在原写事务中执行：

1. 目标必须等于 application.previous_snapshot_id；否则沿用 `policy_snapshot.rollback_target_invalid`。
2. current head 的已验证 artifact 必须仍恰好包含 application 记录的 generated Policy ID/version/hash；生成 Policy
   也必须属于当前 tenant、active 且 graph hash 一致。缺失、重复或漂移返回 `policy_template.rollback_conflict`。
3. 预生成 compensating Snapshot、三条 Audit 和 snapshot event ID；先以 deferred FK 和版本 CAS 把 application 填入
   rollback request/trace/fingerprint、snapshot/audit/event ID 并转换为 `rolled_back`。这个顺序只用于在同事务打开
   generated graph 的受控写保护；任一后续失败都会回滚，外部永远看不到半完成 rolled_back。
4. 以版本 CAS 把生成 Policy 设为 disabled、version+1，并写 `policy.updated` Audit；随后重编 active candidate，
   并从 rollback 前 current artifact 精确删除 generated item 后重新构建 expected artifact；
   两者 content hash、item 顺序和逐项 graph hash 必须完全一致。立即回滚时该结果自然等于 previous source；存在
   后续已发布变更时保留其余 item。任何未发布 candidate 漂移都会 conflict，不能用线上回滚掩盖控制面漂移。
5. 以 `operation=publish`、`previous=current head`、`rollback_source=NULL` 创建新单调 Snapshot；items 使用第 4 步
   已证明的 current-minus-generated 制品，写既有 `policy_snapshot.published` Audit/Event，再写
   `policy_template.rolled_back` Audit，并在提交前回读 application、Policy、Snapshot、三 Audit 与 event。
6. 一次提交后分别派发 Policy updated、Snapshot published、Template rolled_back 三条 Audit与 snapshot event。
   Template 的 EVT-075 只描述 applied，本期不伪造第二种领域 Webhook。
7. 幂等识别必须先于“当前 head 是否属于未回滚 application”的新操作判断。已成功 rollback 的相同 request 从
   `rollback_snapshot_id -> application` 恢复并核验三条 Audit 和一条 snapshot event 后补派；任一事实异常 503，
   不得因 head 已推进而误报 target invalid，也不得再次停用 Policy。

模板撤销审计同样冻结：`policy.updated` 与 `policy_snapshot.published` 完全复用既有白名单；
`policy_template.rolled_back` 的 before/after 都包含 apply 审计的九个稳定字段，before 为 `status=applied` 且无
`rollback_snapshot_version`，after 只把 status 改为 rolled_back 并加入该版本，因此 changed_fields 精确为
`["rollback_snapshot_version","status"]`。Audit actor 已表达 rolled_back_by，摘要不重复保存邮箱、角色或时间。

无 previous head 的 application 已在预览阶段禁止，因此不存在“回滚到虚构 v0”。非模板产生的 current snapshot
完全保持 PRD-045 的“只复制当前上一版”rollback 行为；模板分支不改变旧 Snapshot wire/hash，也不把 compensating
publish 的 content hash 伪装成 previous source hash。

## 8. 前端页面与交互

- 导航仅在后端 capabilities 含 `policy_template.read` 时显示“策略模板库”；直接访问仍由 API 权威拒绝。
- 页面是工作台布局：左侧五条紧凑模板列表，右侧未套卡片的详情、绑定表单和影响预览；不做营销 Hero。
- 项目、Alias 使用 select；预算阈值使用带步进的数值输入；刷新使用熟悉的刷新图标并有 tooltip。
- 点击模板后先显示通用影响；选择绑定后请求精确 GET。只有 `ready/ready_with_warnings` 且拥有 apply 权限时
  显示“应用模板”；warning 必须进入确认框，`incompatible` 只能展示稳定阻断原因和修复入口。
- 应用前确认对话框逐项展示 affected Project、生成 Policy、动作、优先级、before/after policy count、
  新 snapshot version、rollback target 和 shadowing warning；正文不展示 Provider URL、Key 或内部异常。
- apply 期间锁定当前 nonce/绑定，禁止重复提交；409 时清除旧预览并自动重新获取，不静默重试写请求。
- 成功后显示 application ID、Snapshot ID/version 和“查看策略快照”命令；没有 `policy.publish` 时只显示交接信息，
  不显示回滚按钮。
- 403、Session 注销、租户切换或权限 revision 变化立即清空页面状态、nonce、绑定和预览；不写 URL、Storage。
- 1280x720 和 390x844 均不得横向溢出；移动端列表与详情分段显示，操作栏不遮挡正文或系统导航。

## 9. 错误码设计

| HTTP | code | 场景 |
| --- | --- | --- |
| 401/403 | 既有 `auth.*` | Session 无效、缺 read/apply/publish，或写事务内撤权 |
| 404 | `policy_template.not_found` | 当前 tenant 不存在模板；跨租户同形 |
| 415 | `policy_template.media_type_unsupported` | apply 不是 `application/json`，正文不进入解析器 |
| 428 | `policy_template.precondition_required` | 缺 `If-Match` 或 `If-Policy-Candidate-Match` 强前置条件 |
| 413 | `policy_template.payload_too_large` | apply 正文超过 32 KiB |
| 422 | `policy_template.query_invalid` | GET query 未知、重复、越界或绑定组合非法 |
| 422 | `policy_template.payload_invalid` | JSON、字段集、nonce、reason 或基本 wire 非法 |
| 422 | `policy_template.binding_invalid` | Project/Alias/阈值非法；跨租户/disabled 同形 |
| 409 | `policy_template.not_applicable` | 缺基线快照、价格、国产 Alias、预算事实、mask runtime capability 或资源 quota |
| 409 | `policy_template.version_conflict` | 模板版本已变化 |
| 409 | `policy_template.impact_conflict` | 影响 hash、head、candidate、价格/别名/预算依赖或 preview 派生结果漂移 |
| 409 | `policy_template.request_conflict` | tenant 内 request ID 属于不同 actor 或不同规范化意图 |
| 409 | `policy_template.nonce_conflict` | 成功 application 已使用同一 preview nonce，但 request ID 不同 |
| 409 | `policy_template.application_conflict` | 同 Project 已有未回滚模板应用 |
| 409 | `policy_template.rollback_conflict` | 模板 Policy 或其他 active candidate 在回滚前已漂移 |
| 409 | `policy_snapshot.template_invariant_conflict` | 普通/模板发布的 candidate 会绕过某条 active template application；head 不变 |
| 503 | `policy_template.unavailable` | 数据库、目录、审计、事件、编译或封印无法安全完成 |
| 422 | `policy_snapshot.action_unsupported` | 仍用于未知动作；本版本不再用于已支持的 mask |
| 422 | `gateway.policy_mask_unsupported` | mask 命中但输入结构或完整遍历上限不能安全脱敏；不可重试，Provider 未调用 |
| 503 | `gateway.policy_mask_unavailable` | redactor/capability 损坏或内部执行失败；可重试，Provider 未调用 |

错误响应只返回固定英文 code/message/suggestion、request_id/trace_id；不得拼接 SQL、DSL、bindings、
价格、Prompt、Response、Provider Key、Token、Cookie、Authorization、异常正文或数据库路径。
所有新 ErrorSpec 必须冻结 `http_status/retryable/message/suggestion`；支持 mask 后同步删除旧
`gateway.policy_action_unsupported` suggestion 中“移除 mask”的过期指引。

## 10. 测试用例

### 10.1 迁移和数据完整性

1. 空库 0001..0060、0059->0060、0060 幂等重跑；migration ledger、checksum、最高版本正确。
2. 每个现有 tenant 精确五模板，新 tenant 自动五模板；定义 hash、版本和顺序稳定，低成本 priority 1000、
   其余 priority 10 的 CHECK 生效，UPDATE/DELETE 被拒绝。
3. 权限 revision 5、六角色上限/default/hash 与迁移完全一致；Developer/Viewer 不增加权限。
4. application、item、event 的复合 FK、previous/result 唯一定位、状态机、不可变 trigger、跨租户负例全部生效；
   active application 的 generated Policy/Rule/Action 以及受保护 Alias/Model/Provider 弱化更新被数据库拒绝，
   受控 rollback 同事务转换可以通过且故障回滚后仍为 applied。
5. 0059 Audit/Webhook/PromptTemplate 历史逐列保真；down/up 在无应用时通过，有应用时 down guard 失败关闭。
6. `PRAGMA integrity_check=ok`、`foreign_key_check` 为 0；迁移测试只使用 `D:/MetaHub/.tmp`。

### 10.2 后端主流程

1. GET 无绑定固定返回五模板；每个模板的 required bindings、generic impact 和安全选项正确。
2. 五模板分别在可适配租户生成 ready 精确预览；同 nonce+同事实的纯 planner 逐字节稳定，两次真实 GET 的 nonce、
   派生 ID、after/impact hash 不同而 before candidate 可相同。
3. 五模板分别 apply：只新增一个 Policy，Snapshot `head+1`，Policy/Snapshot/Template Audit 与 source event 同事务。
4. 低成本按同一 as_of、PricePlan 优先/目录回退选择 CNY 标准篮子总价最低 active Model；目标必须显式支持
   Chat/Embeddings/Stream 且 KeyPool 资格 available（none auth 可无凭据），排除 migration platform，同价按 model_id 稳定；无事实变化时
   preview/apply 时钟不同仍可通过，价格跨生效点使旧预览冲突。
5. 高安全同时锁定 Alias environment/code 与实际 upstream model；国产模板要求每个允许名称在每个 frozen active
   Environment 都存在纯国产 Alias，条件同时检查 env/model。覆盖当前环境缺 Alias 但同名境外 direct Model 的回退旁路，
   排除 OpenRouter/硅基流动/local/custom/migration，并覆盖后续 Environment/Alias/Model/Provider 漂移防线。
6. 预算模板逐 active Virtual Key 证明预算覆盖并使用真实 `budget_remaining`；无 Key/无覆盖时不允许应用，
   应用后新增无预算 Key 在 Gateway 缺上下文时 fail closed。
7. 客服模板证明 mask modifier 不短路后续 deny/route_to；Chat、Embeddings、主/备用/流式 fake upstream 只收到
   占位符，原 payload 不变，手机号、身份证、邮箱原文在 DB、日志、事件和响应证据中零命中。
8. apply 原请求丢失后相同 request ID 原样回放，六个 Audit/Event ID 相同且不重复创建；回滚后重放 apply 只返回
   原成功事实与当前 rolled_back 状态，绝不重新应用。
9. 全排序遮蔽矩阵覆盖：低成本后置 deny、高安全/国产后置 route_to、各模板前置 allow/route_to 均阻断；
   安全的先行 deny 与被低成本遮蔽的非安全动作只形成稳定 warning，所有排序 ID/reason 均进入 impact hash。
10. 分类复合权限按数据库矩阵执行：Finance 只能应用成本/预算，Security 只能应用三类安全模板，Ops 只能应用
    低成本，Admin 可应用全部；单独拥有 `policy_template.apply` 不能跨类别，写锁内撤销任一组成权限均零写入。
11. 模板应用后分别新增高安全/国产的后置 route_to、预算/脱敏的前置 allow、低成本的后置 deny；普通 publish 均返回
    `policy_snapshot.template_invariant_conflict` 且不创建 building/item/event/audit、不推进 head。禁用绕过 Policy 后可发布。
12. 低成本 KeyPool 资格与 Gateway 现有算法逐例同源：none auth、托管 Key 优先、rate-limit/probe freshness、默认租户
    环境 Key fallback 和非默认租户拒绝；资格/健康在 preview 后漂移使 apply 409，响应与事件不含 credential ID/Key 元数据。
13. 国产模板在 dev 有纯国产 Alias、prod 缺 Alias 但有同名境外 direct Model 时必须 deny；新建环境默认 deny；
    active 环境/名称超过 20 项不截断而 incompatible。高安全/国产依赖 Model 的 provider_model_id 或
    upstream_model_name 改指另一个上游时，数据库 trigger 与服务层都拒绝。
14. 国产派生表达式分别覆盖 source code point、UTF-8 byte、token、AST node/depth、parentheses/not depth 和 comparison
    超限；必须包含“中文 Alias 使 code point 未超而 UTF-8 byte 先超限”的用例。全部返回
    `domestic_expression_limit_exceeded`，不得截断或拆成近似多规则后进入 apply。

### 10.3 异常、并发和权限

1. 缺/坏 Session、缺 read/apply、页面可见但 apply 被撤权、写锁内撤权均返回统一 401/403 且零写入。
2. 跨租户模板/Project/Alias/Model/Snapshot 与不存在同形；第二租户不能观察第一租户选项、hash 或 ID。
3. 非 JSON、超限、重复 key、未知字段、bool 数值、控制字符、非法 Decimal/nonce/hash/header 全部定码拒绝。
4. 模板版本、head、before candidate、impact、价格、Alias、Budget、分类权限或部署 gate 任一预览后变化，apply 整体回滚。
5. 同 Project 重复应用、新 request 复用 nonce、同 tenant 不同 actor 复用 request、同 request 不同 body、
   两并发 apply 只有一个成功；缺 header 为 428、错误 media type 为 415。
6. 在 Policy insert、Rule、Action、Snapshot building/item/seal/head、三个 Audit、三个 event 和 commit 处故障注入，
   每个断点都验证零半成品；提交后 Webhook 失败不反转 201。
7. mask 对未知 text part、嵌套/混合 input、token ID、超限正文返回 422，对执行器损坏返回 503；两者只保留
   已冻结 RequestLog，Routing/DLP/Rate/Budget/Affinity/Attempt/Stream/Provider/Usage/Cost 均不产生，日志不含
   `repr(payload)`。
8. `policy-mask-v1` gate 默认 false、mixed-version、Gateway readiness 缺 capability、自检失败均使客服模板 incompatible；
   全 Gateway 就绪且 operator gate 开启后才可 apply，关闭 gate 不删除已发布快照但新 apply 继续失败关闭。

### 10.4 回滚和兼容

1. 模板 apply 后立即以既有 Snapshot rollback 路径撤销：生成 `operation=publish` 的新单调补偿版本、模板 Policy
   disabled、application rolled_back；active candidate 与应用前来源 hash 相同，snapshot provenance 仍是
   `previous=current/rollback_source=NULL`，下一次 publish 不会重新上线模板；无基线 head 时 apply 必须先失败并给出
   `snapshot_baseline_required` 适配原因，不能产生无法回滚的首个模板快照。
2. 普通 API/SQL 对 active application 的模板 Policy 修改、停用、删除在源头被拒绝；若预迁移损坏图或出现其他
   未发布 candidate 漂移则 rollback conflict、旧 head 不变。current head 含后续已发布变更时，模板感知回滚只移除
   精确 generated item 并证明其余 current artifact 逐项保真。
3. 非模板 Snapshot rollback 行为保持 PRD-045；Gateway cache/LKG、internal wire 和旧 snapshot hash 不回算。
4. mask shared serialization/deserialization、Control compile、Gateway执行、Dry-Run、Request Explain 相邻链路一致。

### 10.5 Console 与运行验收

1. Console API client、页面 state/render/event tests 覆盖五模板、绑定、预览、确认、成功、409 重取和失败态。
2. Admin/Finance/Security/Ops 可按数据库权限访问；Developer/Viewer 无导航、DOM、prefetch，直接 API 为 403。
3. 1280x720 与 390x844 应用内 Browser 验收已通过：登录、五模板、已应用后的 incompatible 防重复、
   预算模板 ready-with-warning、完整确认摘要、Snapshot v12、权限状态和注销均符合预期；页面无横向
   溢出和 Console error，模板 DOM/URL 无 nonce、candidate/impact hash、Token 或 tenant 标识，相关模块
   也无 Storage 写入。
4. 专项绿后只执行一次最终全量 Python/Node/Console 回归；局部修复只重跑相关节点，不反复从头跑全量。
5. 运行 `scripts/ci/check-comments.py`、Console lint、0060 真实开发库升级/幂等、三服务 HTTP 200、DB 完整性和资源清理。
6. 正式 API 真实 apply 已完成：Snapshot head 为 v12 且 candidate=current；application 1 条、item/dependency
   各 2 条；`policy.created`、`policy_snapshot.published`、`policy_template.applied` 三类 source event
   与同 action AuditLog 均各 1 条；生成 Policy 为 active；`integrity_check=ok`、外键违规 0；request ID 为
   `req_prd068_runtime_apply_redaction_v12`。
7. Browser 验收发现并闭环两处仅相关前端问题：Candidate CAS 必须匹配 `before_candidate_hash`，不能匹配
   预计写入后的 hash；移动确认框使用固定遮罩、`border-box`、内部滚动和长 ID 断行。PAGE-095 聚焦
   回归更新为 `16 passed`，未重跑无关全量。

## 11. 代码修改计划

### 11.1 Shared 和 Gateway

- `shared-sdk/python/metahub_shared/rbac.py`：revision 5、两权限、角色上限/default。
- `shared-sdk/python/metahub_shared/runtime/errors.py`：模板和 mask 固定错误码。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：`policy_template.applied` 安全 payload builder/validator。
- `shared-sdk/python/metahub_shared/runtime/provider_keypool.py`：Control/Gateway 共用的无副作用 KeyPool 资格快照与安全指纹。
- `shared-sdk/python/metahub_shared/runtime/config.py` 与两服务 health/readiness：默认关闭的部署 gate、
  `policy-mask-v1` capability 安全投影和 mixed-version 失败关闭合同。
- `gateway-data-plane/src/metahub_gateway/policies/redaction.py`：有界、纯函数输入脱敏。
- `gateway-data-plane/src/metahub_gateway/policies/runtime.py` 与 OpenAI 主链路：把 mask 作为 modifier，
  在路由前替换 Chat/Embeddings 内存 payload；求值后先记录首个 mask ID，完整替换成功后才单调写 applied=true。

### 11.2 Control API

- 新增 `metahub_control/policy_templates/{models,routes,service}.py` 与 `__init__.py`。
- `auth/dependencies.py`：read/apply 守卫；写事务内复核复用当前权限加载器。
- `policies/service.py`：抽取 connection-level Policy create helper，既有 create 和模板应用共同复用。
- `policy_snapshots/service.py`：抽取 connection-level publish helper、放行 mask、实现 active template invariant、
  模板感知补偿撤销、六事实回放和多 Audit 派发；普通 publish 同样调用 invariant helper。
- `policies/dry_run_service.py`：mask 不再标记 runtime unsupported；仍不返回正文。
- `app.py`：注册 `policy_templates` 路由；导航 owner 继续由 Control Console 服务按数据库权限裁剪。
- `shared-sdk/python/metahub_shared/runtime/{config,health,fastapi_app}.py`：增加默认关闭的部署门禁和
  Gateway `policy-mask-v1` readiness 安全投影；旧 Gateway 或自检失败继续 fail closed。

### 11.3 数据库、Console 和文档

- 新增 `infra/migrations/0060_*.sql` 与 `.down.sql`、application dependency/RequestLog mask trace 及迁移专项测试。
- 新增 `console-web/src/lib/policyTemplates.js`、
  `console-web/src/features/policy-templates/renderPolicyTemplates.js` 和页面状态模块；接入 `main.js`、导航、样式和单测。
- 新增 `docs/api/policy-templates.md`、`docs/testing/PRD-068-tests.md`；完成后更新 README 和
  `docs/development/directory-map.md`，并把下一条切换到 Excel 确认的 PRD-069。

## 12. 编码前自审记录

### 第一轮：边界、租户、权限、兼容性

状态：**已完成并修订通过**。主 Agent 对 Excel、现有 Policy/Snapshot/Gateway/PRD-063/RBAC 合同及两份独立只读
审查逐项复核，修订如下：

1. **API 边界**：删除不存在的 Responses 运行承诺；本期只支持仓库真实存在的 Chat 与文本型 Embeddings，
   token ID/未知正文失败关闭，不扩 MVP 主链路。
2. **上游边界**：低成本候选显式排除 migration platform，保留国产云与客户 local/customer-managed Endpoint；
   OpenRouter、硅基流动不会因价格低被选成默认上游。
3. **价格合同**：冻结 `applied_as_of`、PricePlan 优先/带版本目录回退、仅 CNY、1K 输入+1K 输出标准篮子及
   model_id tie-break；价格跨生效点进入完整 CAS。
4. **环境隔离**：高安全条件增加 Alias Environment；国产 Alias 名称必须在全部 active 环境实例中都能证明
   default/fallback Provider 属于 `DOMESTIC_PROVIDER_TYPES`，direct model 与同名跨环境绕过均被 deny。
5. **应用后漂移**：增加 tenant-bound dependency 表和 Alias/Model/Provider 弱化变更 trigger；禁用仍可应急
   fail closed，重新绑定或分类必须先回滚模板。
6. **mask 兼容**：把 mask 从终止决定改成先行非终止 modifier，避免跳过后续 deny/route_to；新增持久安全 trace，
   插入点固定在路由/DLP/预算之前且不记录正文或 hash。
7. **优先级**：撤销五模板统一 priority 10；安全模板使用 10，低成本使用 1000。三值 overlap 分析把可能先行的
   allow/route_to 变为 blocker，把安全的先行 deny 变为可见 warning。
8. **RBAC**：read/apply 继续独立，apply 是服务端固定的受限关键命令，不按角色名补权、不借此授予原始 CRUD；
   rollback 仍独立要求 `policy.publish`，写锁内再次读取数据库矩阵。
9. **幂等与 nonce**：application 唯一键收紧为 `(tenant_id, request_id)`，原 actor/fingerprint 再校验；nonce 明确
   只是 domain-separated 规划盐，并增加 tenant nonce 唯一约束，不能冒充 GET 授权凭证。
10. **可回滚前置**：无 published head 时模板不适配，不能产生虚构 v0；模板 rollback 只允许 application 的
    previous ID，并用 current artifact 精确减去 generated item 的证明保留后续已发布项。
11. **统一错误**：补齐 415/428，正文结构 mask 为 422、执行器损坏为 503；403 仅保留认证/授权或策略拒绝，
    所有 ErrorSpec 都冻结 retryable 与安全 suggestion。
12. **租户与敏感数据**：所有 ID、依赖、hash、Audit/Event 关系保持 tenant 复合 owner；Preview/Event/Audit/日志
    不含 Provider URL/Key、Token、价格全文、预算余额、Prompt、Response、命中片段或正文 hash。

结论：边界、权限、租户隔离和既有兼容问题均已形成可实现合同；第一轮通过。编码门禁仍因第二轮未完成而关闭。

### 第二轮：事务、回滚、安全、测试可验收性

状态：**已完成并修订通过**。主 Agent 重新读取 Shared 求值器、Gateway 执行入口、Policy/Snapshot 服务、
真实路由注册点、Alias/Model/Provider 写服务和 0059 数据库结构，并逐项吸收独立数据合同与实现模式审查：

1. **求值与遮蔽**：确认现有 Shared/Gateway/发布器仍把 `mask` 当终止或不支持动作，改为保持一次有界排序扫描的
   modifier；遮蔽分析扩为模板前后完整实际排序集，阻断低成本后置 deny、高安全/国产后置 route_to 及各模板前置
   allow/route_to，避免“策略可创建但安全语义被绕过”。
2. **低成本请求形态**：现有 DSL 没有 request kind，设计不偷偷扩 DSL/wire；候选必须同时声明本期已开放的
   Chat、Embeddings、Stream 核心能力，并复用 Gateway 现有托管 Key 优先、probe/rate-limit freshness、none auth 与
   默认租户环境 Key fallback 资格；KeyPool 安全 hash 和 Provider health generation 进入 preview/apply CAS。
3. **运行能力证明**：仓库没有 Gateway 副本注册表或能力租约，不能用 Control 自身常量冒充数据面已升级。本期明确采用
   默认 false 的 operator-attested 部署门禁：全部接流实例 readiness 自检并声明 `policy-mask-v1` 后才由运维开启；
   mixed-version、自检失败或旧 Gateway 均不可应用且运行时继续拒绝 mask。后续若引入副本注册表再升级为实例租约，
   本条不伪造并不存在的集群权威来源。
4. **职责分离**：单独 `policy_template.apply` 不足以跨域应用；增加按模板类别的数据库权限复合矩阵，并在路由与
   `BEGIN IMMEDIATE` 内各复核一次。判断只按 permission code，不硬编码角色名，前端仅消费 `can_apply` 摘要。
5. **生成图与依赖冻结**：任一 application 的 Policy/Rule/Action 全图永久受保护，仅允许受控撤销的精确版本跳转；
   高安全、国产与低成本涉及的 Alias/Model/Provider 语义依赖完整冻结。撤销先在同一事务原子转换 application 状态，
   再允许受控停用，后续任一步失败整笔恢复。
6. **Policy 版本外键**：`generated_policy_version=1` 只保存应用时事实，不作为当前 Policy version FK；增加
   `rolled_back_policy_version`。否则受控 `version+1` 会被静态复合外键永久卡死。
7. **Snapshot 兼容**：模板撤销定义为 wire v1 补偿性 publish，而不是冒充 PRD-045 的逐字节 source rollback；
   current artifact 精确减去 generated item 并保留后续发布项，`operation=publish/rollback_source=NULL`，旧回滚合同不变。
8. **时间与 CAS**：`preview_as_of` 仍绑定客户端预览，但新 `applied_as_of` 不直接改变 canonical hash；通过包含生效
   事实 ID/version/effective_at/value 的 `applicability_fingerprint` 比较依赖，事实不变时不会恒定 409，跨生效点则冲突。
9. **幂等与 owner**：previous/result/rollback Snapshot、generated Policy 和十个 Audit/Event ID 都有 tenant 内一对一
   唯一性及复合 owner；只有真实插入环使用 deferred FK。六个 apply 派发在事实全部核验后独立尝试，部分外部成功由
   稳定 event ID 和后续回放收敛，不声称跨网络原子。
10. **迁移与证据保真**：模板 priority 改为按 code CHECK；RequestLog mask trace 定义单调合并语义；0060 down guard
    覆盖已发布 mask、运行日志、应用、Audit、Webhook、RBAC 和未知后续引用，并恢复全部既有列、索引和 trigger。
11. **真实实现入口**：路由注册点修正为 `metahub_control/app.py`；事务内撤权复用
    `require_transaction_permission`；Policy 与 Snapshot 抽取 connection-level helper 后由旧接口和模板流程共同复用，
    避免复制一套 owner、编译、封印或审计逻辑。
12. **可验收性**：专项测试冻结五模板、全排序矩阵、分类权限、跨租户、故障注入、幂等回放、补偿发布、mask
    Chat/Embeddings/Stream 和敏感原文零落盘；专项绿后只运行一次最终全量回归，局部修复只重跑相关节点。
13. **持续发布不变量**：最终交叉审查发现“冻结模板图”仍不足以阻止后来新增 Policy 绕过。普通 publish、模板 apply
    和补偿撤销现在必须共用 active application invariant helper，对完整 candidate 重跑 generated item 与双向遮蔽证明；
    高安全/国产后置 route_to、预算/脱敏前置 allow、低成本后置 deny 均固定 409 且 head 不变。
14. **国产跨环境旁路**：Gateway 在当前环境找不到 Alias 时会尝试同名 direct Model，因此只检查 model 名称不安全。
    国产条件改为同时限制 frozen Environment 与全环境覆盖 Alias；每个允许名称必须在每个 active Environment 都有
    纯国产实例，新环境默认 deny。高安全/国产还冻结 provider_model_id/upstream_model_name，防止内部 Model ID 不变但
    实际上游被改指。

结论：事务边界、回滚语义、权限、租户 owner、运行能力门禁、迁移降级和测试判据均已形成可实现合同；第二轮通过，
编码门禁解除。实现过程中如发现合同与真实 schema 不一致，必须先回写本设计并重新审查对应段落，不能静默偏离。
