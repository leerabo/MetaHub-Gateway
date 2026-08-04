# PRD-044 - 策略引擎 DSL 最小版

状态：已完成。设计文档与两轮设计自审、DSL、控制面 API、0033 迁移、控制台、专项/全量回归、运行库迁移、真实 API、三服务和浏览器运行验收均已通过；`active` 未发布且 Gateway 未挂载策略属于本 PRD 的明确边界。

## 需求理解

### 来源与顺序

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 第 45 行明确：PRD-044 属于 `M1-企业POC`、`P1`、核心层，模块为“策略引擎”，名称为“策略引擎DSL最小版”。
- 启动本条开发时，README、`docs/prd` 与 `docs/testing` 的完整完成证据连续到 PRD-043，因此 PRD-044 是按既定优先级排序后的下一条未完成 PRD；当前全部设计、实现、测试和运行验收证据已闭环。
- 指定页面为“策略管理页、策略编辑器”；指定接口为 `POST/GET/PATCH /policies` 与 `POST /policies/validate`；核心对象为 Policy、PolicyRule、PolicyAction。
- 指定变量为 `tenant`、`env`、`project`、`model`、`data_class`、`budget_remaining`；指定动作为 `allow`、`deny`、`mask`、`log_only`、`route_to`。
- `TC-0087` 要求非法表达式不能保存、命中结果可解释、执行超过阈值时自动拒绝并告警；`TC-0088` 要求阻断自定义脚本、复杂递归与外部函数，并覆盖 DSL 注入、优先级、异常和权限。
- 稳定事件为 `policy.created` 与 `policy.validation_failed`。所有响应继续使用统一错误结构与 `request_id/trace_id`。

### 用户价值与交付闭环

企业管理员需要用一套小而确定的条件语言描述路由、预算和安全决策，而不是在 Gateway 内嵌任意脚本。本 PRD 交付以下闭环：

1. 管理员可创建、查询和更新当前租户的策略；每条策略包含一个条件规则和一个动作。
2. 编辑器可在保存前调用后端校验；保存接口仍会重新编译，不能信任前端“已校验”状态。
3. 相同 DSL 在控制面与未来数据面共用同一纯编译/求值模块，输出稳定 AST、条件摘要和可解释结果。
4. 多策略按优先级确定执行顺序；`log_only` 记录命中后继续，其余动作在首个命中时终止；无命中默认放行。
5. 整组策略求值超过时间预算时必须 fail closed，固定返回 `deny`、`policy.evaluation_timeout` 与告警标志，不能继续路由上游。
6. 创建、更新、校验失败、审计、Webhook、日志和响应均可按 tenant、actor、policy、request_id/trace_id 追踪；DSL 正文只在 admin 成功管理响应中返回供编辑，绝不进入错误、日志、审计、事件或 Webhook，请求 Prompt 始终不进入策略域。

### 当前角色映射

- Excel 角色为“管理员/安全/财务”，但当前稳定 RBAC 只有 `admin/developer/viewer`；独立安全/财务角色与权限矩阵尚未交付。
- PRD-044 不提前扩展角色枚举。策略可改变路由、预算与安全行为，当前读写和校验均使用新增的 `require_policy_admin`；该依赖委托既有 admin 角色守卫而不复制角色判断，仅允许 admin。
- developer/viewer 不显示策略导航，也不能后台预取策略；即使手写 HTTP 请求，后端仍返回 `auth.permission_denied`。
- 前端角色判断只优化体验，认证上下文、角色守卫、tenant-qualified SQL 和数据库归属约束共同构成真实边界。

### 架构边界

- 架构要求控制面配置“先校验、后保存、再编译、最后下发”，数据面热路径不能每次查询复杂数据库。本 PRD 完成校验、保存和确定性编译，不在每次 Gateway 调用时读取策略表。
- PRD-045 负责策略快照/发布及数据面消费。本 PRD 不建 `snapshot_version`、发布状态、轮询器或 Gateway 热路径挂载，也不宣称策略已影响真实模型调用。
- PRD-046 的完整 Dry Run 页面不在本 PRD。`/policies/validate` 可接受一个可选、无副作用的示例上下文，用于证明单条草稿的可解释求值；控制台本期只展示编译校验，不建设批量 Dry Run 工作台。
- 架构把复杂 DSL、灰度路由和部门/用户级条件放在后续阶段。本期只有白名单变量、有限比较、有限布尔组合和单动作，不支持函数、属性访问、循环、脚本、正则、模板、网络或文件访问。
- `mask` 本期只形成可解释决策，不修改 Prompt/Response；实际脱敏仍由 DLP 能力执行。`route_to` 只返回已校验的当前租户模型目标，不在本期发起路由。
- `data_class` 当前没有独立实体；本期把它作为调用方提供的有界字符串上下文。`budget_remaining` 使用 CNY Decimal，未来由预算规则的 `max(limit-current, 0)` 结果适配，策略引擎不重复计算账本。

### 明确非目标

- 不使用 Python `eval/exec/ast.parse` 执行用户文本，不引入 JavaScript、Lua、CEL、JSONLogic 或第三方在线解释服务。
- 不支持自定义函数、外部函数、变量属性访问、对象/字典字面量、赋值、循环、正则、注释、插值、宏、导入或递归策略调用。
- 不支持多规则树、多动作链、百分比分流、灰度发布、质量/成本评分、部门/用户变量或任意 metadata 路径。
- 不做策略快照、发布、回滚、数据面缓存和 Gateway 执行挂载，这些属于 PRD-045 及后续能力。
- 不做完整 Dry Run 页面、历史命中查询、策略模板市场、审批流或复杂版本差异页面。
- 不新增 security/finance 角色，不改变既有预算、DLP、模型别名和 Fallback 的运行语义。

## 涉及模块

- `shared-sdk/python/metahub_shared/runtime/policy_dsl.py`：无外部副作用的词法、解析、AST 编译、单表达式/策略集纯求值、解释与超时 fail-closed。
- `shared-sdk/python/metahub_shared/runtime/config.py`：策略求值时间预算配置，默认 5 ms，并限制为安全范围。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`error_diagnosis.py`：策略非法、query、重复、不存在、版本冲突、路由目标、空更新、配额和不可用错误码及安全诊断。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：加入 `policy.created`、`policy.validation_failed` 可订阅事件白名单。
- `infra/migrations/0033_policy_engine_minimal_dsl.sql` 与 down migration：Policy/Rule/Action/Event、索引、租户归属触发器、审计白名单。
- `control-plane-api/src/metahub_control/policies/`：models、routes、service 与包入口；实现 admin-only CRUD、校验、事件、审计和模型目标归属验证。
- `control-plane-api/src/metahub_control/auth/dependencies.py`：新增 `require_policy_admin` 后端权限依赖。
- `control-plane-api/src/metahub_control/audit/service.py`：加入 policy 创建/更新动作与安全摘要字段。
- `control-plane-api/src/metahub_control/console/service.py`、`app.py`：注册策略 API 与 admin-only 导航。
- `console-web/src/lib/policies.js`：策略列表、校验、创建、更新客户端；不接受 tenant_id。
- `console-web/src/features/policies/renderPoliciesPanel.js`：策略管理页、单一编辑器、校验结果和响应式列表。
- `console-web/src/app/main.js`、`src/styles/main.css`：active-only 加载、内容渲染、事件绑定、编辑状态和响应式布局。
- `control-plane-api/tests/unit/test_policy_dsl.py`、`tests/integration/test_policies.py`、`test_db_migrations.py`：DSL、API、事件、迁移专项测试。
- `console-web/tests/unit/policies.test.mjs` 及导航/渲染回归：客户端、权限、校验失效、转义和响应式结构。

## DSL 设计

### 变量语义

| 变量 | 求值类型 | 稳定语义 |
| --- | --- | --- |
| `tenant` | string | 认证上下文中的 `tenant_id`，客户端不能覆盖真实运行值。 |
| `env` | string | Environment 稳定 code，例如 `dev/test/prod`。 |
| `project` | string | 当前请求归属的 `project_id`；没有归属时调用方提供空字符串。 |
| `model` | string | 路由前的请求模型/稳定别名字符串，不是 Provider Key 或 Endpoint。 |
| `data_class` | string | 调用方提供的数据分类标签；最大 64 字符，不读取 Prompt 正文。 |
| `budget_remaining` | decimal | 当前预算口径的剩余 CNY，最小为 0；禁止 NaN、Infinity 和 float 隐式误差。 |

控制面可选示例上下文只能包含上述六个字段，`extra=forbid`；缺失变量在表达式实际引用时返回固定上下文错误，不能用 `None` 悄悄比较。

### 文法

DSL v1 使用 ASCII 关键字和 JSON 双引号字符串：

```text
expression  := or_expr
or_expr     := and_expr ("or" and_expr)*
and_expr    := unary_expr ("and" unary_expr)*
unary_expr  := "not" unary_expr | "(" expression ")" | comparison
comparison  := variable operator scalar
             | variable ("in" | "not in") list
variable    := tenant | env | project | model | data_class | budget_remaining
operator    := == | != | < | <= | > | >=
scalar      := json_string | decimal
list        := "[" scalar ("," scalar)* "]"
```

类型约束：

- `tenant/env/project/model/data_class` 只允许 `==`、`!=`、`in`、`not in` 与字符串值。
- `budget_remaining` 只允许 `==`、`!=`、`<`、`<=`、`>`、`>=` 与十进制数字；v1 不接受布尔或 null 字面量。
- 列表必须为 1-20 个同类型标量，空列表和嵌套列表均按文法错误拒绝。
- Decimal 词法固定为 `-?(0|[1-9][0-9]{0,11})(\.[0-9]{1,12})?`：最多 12 位整数、12 位小数，不允许指数、前导加号、多余前导零、NaN 或 Infinity，绝对值不得超过 `999999999999.999999999999`。
- Decimal canonical 值使用普通十进制字符串，去除小数末尾 0 和多余小数点，并把 `-0` 归一为 `0`；因此 `100.00`、`100.0` 与 `100` 产生相同 AST/hash。示例上下文的 `budget_remaining` 另外要求非负。
- `and` 优先级高于 `or`，`not` 最高；括号只改变组合顺序，不产生运行时调用。

合法示例：

```text
env == "prod" and data_class in ["confidential", "restricted"]
project == "prj_finance" and budget_remaining < 100.00
model != "deepseek-chat" or tenant == "ten_default"
```

非法示例：

```text
__import__("os").system("whoami")
tenant.__class__ == "x"
env == "prod"; deny()
project matches ".*"
budget_remaining < external_limit()
```

### AST 与 hash 合同

AST schema 是 PRD-045 快照必须复用的跨版本合同，v1 固定如下：

```json
{
  "kind": "logical",
  "operator": "and",
  "children": [
    {
      "kind": "comparison",
      "variable": "env",
      "operator": "eq",
      "value": {"kind": "string", "value": "prod"}
    },
    {
      "kind": "comparison",
      "variable": "budget_remaining",
      "operator": "lt",
      "value": {"kind": "decimal", "value": "100"}
    }
  ]
}
```

- 比较操作符 canonical 名为 `eq/ne/lt/lte/gt/gte/in/not_in`；字符串先做 Unicode NFC，再原样保存；Decimal 按上一节规范字符串保存。
- `in/not_in` 的 value 为 `{"kind":"list","items":[...]}`，items 保留用户顺序；列表重复值不自动去重，保证解释与源语义一致。
- 相邻同操作符的 `and` 或 `or` 在 AST 中扁平为一个 logical children 数组并保留从左到右顺序；不同逻辑操作符不重排。`not` 使用 `{"kind":"not","child":...}`；括号不产生节点。
- canonical JSON 固定使用 Unicode NFC 值、字典 key 升序、数组顺序不变、无多余空白、`ensure_ascii=false`，再编码为 UTF-8。
- `condition_hash = SHA-256(b"policy-dsl-v1\x00" + canonical_json_utf8)`，输出小写 64 位十六进制。`canonical_expression` 必须由 AST 重新生成，仅供展示，不作为保存或执行的信任凭证。
- canonical action 固定为 `{"target_model_id":null,"type":"deny"}` 形状并使用同一 JSON 序列化规则；route_to 才允许非空 target_model_id。

### 编译与资源上限

- 源文本最大 1,024 个 Unicode code point 且 UTF-8 最大 2,048 bytes；token 间允许空格、TAB、CR/LF，禁止 NUL 和字符串解码后的控制字符。
- 最大 128 个 token、64 个 AST 节点、AST 深度 8、16 个比较、3 层括号、全表达式最多 1 层 `not` 嵌套、20 个列表元素、单字符串最大 128 字符；括号不能绕过 not 深度。
- AST 节点计数包含 logical、not、comparison、list 以及每个 string/decimal literal；括号只计括号深度、不计 AST 节点。AST 深度按上述节点逐层计算，括号深度独立限制。
- 词法器逐字符扫描，只识别白名单 token；`.`、`;`、`{}`、反引号、单引号、`#`、赋值和未知标识符立即失败。
- JSON 字符串由标准 `json.loads` 解码后再次检查长度和控制字符；解析器不执行任何代码。
- 编译结果包含 `dsl_version='policy-dsl-v1'`、规范 AST、稳定 canonical JSON、引用变量列表和 condition_hash。
- 数据库存储管理员原始条件表达式供再次编辑，同时保存 canonical AST/hash；API、审计、事件和日志不能把 AST 当成可执行代码。

### 动作语义

| 动作 | 参数 | v1 求值结果 |
| --- | --- | --- |
| `allow` | 无 | 首个命中后返回放行。 |
| `deny` | 无 | 首个命中后返回拒绝。 |
| `mask` | 无 | 返回需要脱敏的决策与命中解释；本 PRD 不修改内容。 |
| `log_only` | 无 | 记录本条解释并继续求值后续策略。 |
| `route_to` | `target_model_id` | 返回当前租户已校验 active 模型 ID；本 PRD 不发起真实路由。 |

- 非 `route_to` 动作必须没有 target；`route_to` 必须提供目标。
- 保存/校验时目标必须是当前认证 tenant 的具体 active `model_id`；不支持 alias、provider、URL 或 Endpoint。不存在、跨租户和 disabled 统一按非法目标处理，不暴露资源是否存在。PRD-045 生成快照时必须再次校验目标仍 active。
- 客户端不能提交脚本、参数字典或多动作数组。

### 策略集求值与解释

1. 只处理本地 active（下一快照候选）策略，稳定按 `priority ASC, policy_id ASC` 排序；数值越小优先级越高，ID 用于解决相同优先级的确定性顺序。
2. 每条表达式使用短路布尔语义。解释只记录实际访问过的比较节点，不伪造未执行分支。
3. 命中 `log_only` 时追加安全解释后继续；命中 `allow/deny/mask/route_to` 时立即返回。
4. 没有决定性命中时返回 `allow` 与 `policy.no_match_default_allow`；已有 `log_only` 命中仍保留在结果中。
5. 解释结果包含 reason_code、policy/rule/action ID、priority、condition_hash、比较变量/操作符/value_type/类型化期望值/实际值/布尔结果、已检查策略数和命中顺序；Decimal 的 expected/actual 固定为 canonical 字符串。
6. 解释不包含 Prompt、Response、Token、Provider Key、AST JSON、SQL 或原始异常；日志只记录 ID、hash、reason_code、计数和耗时，不记录上下文实际值。
7. 最多保留 20 条 log_only 命中和 64 条 comparison 解释；超出时设置 `explanation_truncated=true`，但求值和最终决策继续完整执行，不能因为展示截断改变结果。
8. 相同 priority 使用 policy_id 只能保证确定性，不提供业务可控顺序；API/控制台提示管理员为存在先后关系的策略使用不同 priority。

### 超时 fail-closed

- `METAHUB_POLICY_EVALUATION_TIMEOUT_MS` 默认 5，允许 1-100 ms；调用方不能通过 API 指定或放宽阈值。
- 求值器在策略边界和 AST 节点边界检查单调时钟，时间预算覆盖整个策略集，不对每条策略重新计时。
- 超时不抛出未捕获异常，不继续计算，不返回部分 allow/route 决策；固定返回 `deny`、`policy.evaluation_timeout`、`timed_out=true`、`alert_required=true`。
- shared 求值器保持纯函数，不接受回调、不写日志/数据库/Webhook；只返回 fail-closed 决策和告警标志。控制面在示例求值结果为 timeout 时最多追加一次 `policy.validation_failed`，未来数据面调用方在 PRD-045 接入后使用 `gateway.request_failed` 或后续专属运行事件告警，不能滥用 validation_failed。
- 调用方处理告警失败仍不能把 deny 改成 allow，也不能继续 route_to；future Gateway 对超时应返回策略阻断语义并保留 `policy.evaluation_timeout` reason，而不是把它当成可重试的控制面 503。
- 单元测试通过注入单调时钟确定性触发超时，不依赖不稳定的 `sleep`。真实配置仍保留时间门限，节点/token 上限构成第二层资源保护。

## 数据表和字段设计

### policies

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `policy_id` | TEXT PK | 服务端生成 `pol_` ID。 |
| `tenant_id` | TEXT NOT NULL FK | 认证租户；所有读写第一条件。 |
| `name` | TEXT NOT NULL | 1-80 字符；`UNIQUE(tenant_id, name)`。 |
| `description` | nullable TEXT | 最大 500 字符；审计只保存 hash。 |
| `status` | active/disabled | active 仅表示纳入本地策略集和下一快照候选，不代表已发布到 Gateway；disabled 不参与求值。 |
| `priority` | INTEGER 1-10000 | 数值越小越先执行。 |
| `version` | INTEGER >= 1 | 每次有效 PATCH 加 1，为 PRD-045 快照预留稳定输入。 |
| `dsl_version` | 固定 policy-dsl-v1 | 防止未来解释器升级改变历史语义。 |
| `created_by/updated_by` | FK users | 当前 admin；触发器校验同租户。 |
| `created_at/updated_at` | UTC TEXT | 创建与最近修改时间。 |

- `UNIQUE(tenant_id, policy_id)` 供子表使用复合外键；`idx_policies_tenant_status_priority` 支撑当前租户稳定读取。
- 每租户最多 200 条策略，配额在写事务内再次计数，避免并发越限。
- 0033 先为既有 users 建 `UNIQUE(tenant_id, user_id)` 索引；created_by/updated_by 使用 `(tenant_id, user_id)` 复合外键，不把全局 user_id 主键误当成租户归属证明。
- identity trigger 冻结 policy_id/tenant_id/dsl_version/created_by/created_at；version trigger 只允许每次 UPDATE 精确 `OLD.version + 1`，status/priority/name/长度由 DB CHECK 再兜底。

### policy_rules

最小版每个 Policy 恰好一个 PolicyRule；一对一结构保留对象边界，后续扩展不能改变 v1 行的语义。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `policy_rule_id` | TEXT PK | 服务端生成 `prule_` ID。 |
| `tenant_id` | TEXT NOT NULL | 与父 Policy 相同。 |
| `policy_id` | TEXT NOT NULL | `UNIQUE(tenant_id, policy_id)`。 |
| `condition_expr` | TEXT NOT NULL | 仅 admin 管理 API 返回；日志/事件/审计禁止保存。 |
| `condition_ast_json` | JSON object | canonical AST，不作为任意脚本执行。 |
| `condition_hash` | 64 字符 SHA-256 | 审计、事件、解释关联键。 |
| `variables_json` | JSON array | 去重排序后的白名单变量。 |
| `created_at/updated_at` | UTC TEXT | 规则生命周期。 |

- 复合外键 `(tenant_id, policy_id)` 指向父表；INSERT/UPDATE 触发器再次拒绝父子 tenant 错配和归属字段改写。
- identity trigger 冻结 policy_rule_id/tenant_id/policy_id/created_at；规则更新只能替换正文、AST/hash/variables 和 updated_at。
- 条件正文只在当前租户 admin API 和本地数据库中出现，不进入普通请求日志或 Webhook。

### policy_actions

最小版每个 Policy 恰好一个 PolicyAction：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `policy_action_id` | TEXT PK | 服务端生成 `pact_` ID。 |
| `tenant_id` | TEXT NOT NULL | 与父 Policy 相同。 |
| `policy_id` | TEXT NOT NULL | `UNIQUE(tenant_id, policy_id)`。 |
| `action_type` | 固定五种枚举 | allow/deny/mask/log_only/route_to。 |
| `target_model_id` | nullable TEXT | route_to 必填，其余动作必须 NULL。 |
| `created_at/updated_at` | UTC TEXT | 动作生命周期。 |

- 0033 先为既有 models 建 `UNIQUE(tenant_id, model_id)` 索引；target 使用 `(tenant_id, target_model_id)` 复合外键。INSERT/UPDATE 触发器再校验目标 active，服务层同样在事务内复核。
- 父子复合外键、归属不可变触发器和动作/target CHECK 防止内部 SQL 构造跨租户或悬空动作。
- identity trigger 冻结 policy_action_id/tenant_id/policy_id/created_at；动作更新只能替换 action_type/target 和 updated_at。

### policy_events

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `policy_event_id` | TEXT PK | 服务端生成 `pevt_` ID。 |
| `tenant_id` | TEXT NOT NULL | 当前租户。 |
| `policy_id` | nullable TEXT | 创建/更新失败时可能只有已有 Policy ID；新草稿可为空。 |
| `event_type` | policy.created/policy.validation_failed | Excel 指定稳定事件。 |
| `actor_user_id` | TEXT NOT NULL | 当前 admin；触发器校验同租户。 |
| `request_id/trace_id` | TEXT NOT NULL | 请求追踪。 |
| `reason_code` | TEXT NOT NULL | 完整稳定码：`policy.created`、`policy.expression_invalid`、`policy.action_invalid`、`policy.route_target_invalid`、`policy.context_invalid` 或 `policy.evaluation_timeout`。 |
| `payload_json` | JSON object | 仅安全白名单摘要。 |
| `created_at` | UTC TEXT | 事件发生时间。 |

- `policy.created` payload：policy_id、version、status、priority、action_type、target_model_id、condition_hash。
- `policy.validation_failed` payload：policy_id（可空）、condition_hash（编译成功后才有）、draft_fingerprint（仅 canonical AST/action 已形成时才有）、expression_length、action_type、diagnostic_codes；词法/文法失败时不对低熵原文直接做 SHA，只记录 request_id、长度和固定诊断码。
- 表级 CHECK 固定两类事件的 policy_id/reason 组合、payload 必须为 JSON object、request/trace/reason 长度和 diagnostics 数量上限；validation_failed reason 只允许五类领域失败/timeout。payload 只能由白名单 builder 构造，禁止序列化 DTO、异常 repr 或非法 action 原文。
- 事件为追加事实，UPDATE/DELETE 触发器拒绝改写；按 tenant+time、tenant+request 建索引。
- created 事件使用 partial unique `(tenant_id, policy_id) WHERE event_type='policy.created'`，并用 CHECK 保证 created 必须有 policy_id/reason=`policy.created`。
- validation_failed 使用 partial unique `(tenant_id, actor_user_id, request_id) WHERE event_type='policy.validation_failed'`，只去重事件/Webhook 副作用；重复 X-Request-ID 仍重新确定性计算响应，不能声称从不完整事件 payload 回放完整 diagnostics/evaluation。
- 幂等插入只对对应 partial unique 冲突使用定向 `ON CONFLICT DO NOTHING`，随后按 tenant+actor+request 回读同一失败事件；不能用宽泛 `INSERT OR IGNORE` 吞掉 owner/FK/CHECK 安全错误。
- Webhook 必须从已提交 policy_events 行重建，event_id 固定为 `evt_{policy_event_id}`；重复 event 依赖既有 original-event 唯一索引去重。Webhook 只在 commit 后尽力分发；created/validation_failed 与 audit.recorded 各自独立 try/catch，一路外部失败不阻断另一路，也不改变 201/200/422 业务结果。

### audit_logs 扩展

- 新动作：`policy.created -> policy`、`policy.updated -> policy`。
- 安全摘要字段：name、status、priority、version、dsl_version、action_type、target_model_id、condition_hash、variables、description_hash。
- 审计不保存 `condition_expr`、AST、示例上下文或 validation diagnostics 正文。
- 审计最终拒绝字段补充 `condition_expr/condition_ast/canonical_expression/context`；即使未来误扩 SUMMARY_FIELDS，也不能把治理正文写入 audit。
- 创建/更新与 audit 同事务；审计拒绝或 commit 失败时策略、规则和动作一起回滚。
- 0033 up 按 0032 完整字段逐行复制所有旧审计并扩展 CHECK，重建全部索引、owner 与 immutable 触发器。down 不能直接 DELETE policy audit（会被 immutable trigger 拒绝），必须在 `foreign_keys=OFF` 的显式事务中重建 0032 完整 audit schema，以 `SELECT ... WHERE action NOT IN ('policy.created','policy.updated')` 复制历史，再 rename 并重建全部 0032 索引/触发器；随后按 Event -> Action -> Rule -> Policy 顺序删除本 PRD 表，最后删除本 PRD 新增的 users/models 复合唯一索引。

## API 设计

所有 Policy DTO 的顶层及 rule/action/context 每层均使用 `extra='forbid'`；客户端不能在 body 提交 tenant_id、ID、version、AST、hash、actor、事件或时间。为让 `/validate` 的领域错误统一形成 diagnostics/event，condition/action/context 的领域值先以有界宽类型进入 service，再由同一规范化器判断；缺少 rule/action 对象、额外字段或非法 JSON 等结构错误仍由统一 422 截断。响应统一携带 `request_id/trace_id`。

### GET /policies

- 权限：`require_policy_admin`，当前仅 admin。
- 可选筛选：`status=all|active|disabled`、`action=allow|deny|mask|log_only|route_to`；路由显式校验 query key allowlist，未知或超长参数返回 `policy.query_invalid`，不能依赖 FastAPI 默认忽略未知 query。
- SQL 必须以 `p.tenant_id=?` 开始，Rule/Action JOIN 同时带 tenant_id；按 `priority ASC, policy_id ASC` 返回。
- 返回嵌套 PolicyInfo：明确携带 tenant_id、policy 元数据、一个 RuleInfo、一个 ActionInfo；不返回内部事件或审计。
- 数据库若出现缺 Rule/Action 的不完整 Policy，整个请求返回 `policy.unavailable`，不能把半条策略交给编辑器。

### POST /policies/validate

请求：

```json
{
  "rule": {"condition_expr": "env == \"prod\" and budget_remaining < 100"},
  "action": {"type": "deny", "target_model_id": null},
  "context": {
    "env": "prod",
    "project": "prj_finance",
    "model": "deepseek-chat",
    "data_class": "confidential",
    "budget_remaining": "50.00"
  }
}
```

- `context` 可选；控制台本期不提供完整 Dry Run 表单。Context DTO 不接收 tenant，求值时始终由 AuthContext 注入真实 tenant_id；其他字段按类型和长度规范化，仅要求提供表达式实际引用的字段。
- 校验成功返回 `valid=true`、dsl_version、draft_fingerprint、condition_hash、variables、canonical_expression 和可选 evaluation。
- `draft_fingerprint = SHA-256(b"policy-dsl-v1\x00" + canonical_ast_utf8 + NUL + canonical_action_json_utf8)`，只在 AST 和合法动作均形成后返回；无效原文不生成该指纹，保存接口也不信任该值。
- evaluation 只解释这一条草稿，policy_id 使用临时安全 ID；跨已保存策略的批量 Dry Run 留给 PRD-046。
- DSL/action/target/context 的领域值无效是编辑器预期结果：返回 HTTP 200、`valid=false` 和最多 8 条固定 diagnostics，每项仅含 code、field、start/end（可用时）与安全 message；位置使用 0-based Unicode code-point offset、end-exclusive，并按 start/end/code 稳定排序。同时幂等追加并分发一次 `policy.validation_failed`。action 非法时事件 action_type 只能为 null/`invalid`，不能原样保存攻击者输入。
- 带 context 的表达式编译成功但求值超时时返回 HTTP 200、`valid=true` 和 fail-closed deny evaluation，reason 为 `policy.evaluation_timeout`；控制面把本次验证超时记录为 validation_failed。它不走 503，也不把有效 AST 误报为语法非法。
- 认证、权限、请求 JSON/DTO 结构错误、数据库或失败事件不可用仍走统一 401/403/422/503 错误响应；结构错误不写 validation_failed。
- 校验接口不保存 Policy/Rule/Action，不返回可执行对象，也不接受自定义 timeout。
- 未提供 context 时 response 省略 evaluation 字段，不返回 null 或伪造的未命中结果。
- Context 的 `budget_remaining` 只接受十进制 JSON string，拒绝 JSON float、bool、NaN/Infinity、数组和对象；priority 同样显式拒绝 bool，避免 Python 把 bool 当 int。

### POST /policies

请求：

```json
{
  "name": "生产环境低预算阻断",
  "description": "预算接近耗尽时阻断高成本请求",
  "status": "active",
  "priority": 100,
  "rule": {"condition_expr": "env == \"prod\" and budget_remaining < 100"},
  "action": {"type": "deny", "target_model_id": null}
}
```

行为：

1. 从 AuthContext 确定 tenant/actor，规范化元数据并重新编译表达式；前端 validation fingerprint 不构成信任边界。
2. DSL/action 的确定性校验可在写锁前完成；任何失败用独立事务幂等写 `policy.validation_failed` 并 commit 后再投递 Webhook，随后返回固定 422，不能写半条策略。失败事件无法持久化时返回 `policy.unavailable` 503，不能伪装成已可追踪的 422。
3. `BEGIN IMMEDIATE` 内再次检查租户配额、名称唯一性，并在同一数据库快照内复核 route target 的 tenant+active；随后原子写 Policy、Rule、Action、audit 与 `policy.created` event。
4. create 在锁内发现 target 无效时同样先 rollback/释放锁，再独立提交 policy_id=null 的 validation_failed 并投递；duplicate/quota/数据库故障不是 DSL validation_failed，直接返回各自统一错误。
5. commit 后分别尽力投递 audit.recorded 与 policy.created Webhook，并写脱敏结构化日志。
6. 返回 201 PolicyResponse 与 `ETag: "1"`；响应包含 condition_expr 供 admin 编辑，但事件/日志不包含正文。

### PATCH /policies/{policy_id}

- admin-only；path ID 限定 `pol_` + 32 位小写十六进制。客户端必须携带 `If-Match: "<version>"`，取值来自 PolicyInfo；缺失、畸形或已过期均返回 409 `policy.version_conflict`，避免多管理员/15 秒刷新静默覆盖。
- 可编辑 name、description、status、priority、rule、action；空 PATCH 返回 `policy.update_empty`。
- 修改规则或动作时可在锁外先做纯编译；随后 `BEGIN IMMEDIATE`，按 `tenant_id + policy_id` 权威重读完整 Policy/Rule/Action，比较 If-Match version，并在同一快照复核 target 的 tenant+active，再更新三对象、version、updated_by/time 和 `policy.updated` audit。UPDATE 仍带 tenant+policy+old_version 条件。
- 事务内发现 target 无效时必须先 rollback 并释放 SQLite 写锁，再在独立 `BEGIN IMMEDIATE` 中幂等插入 validation_failed、commit、提交后投递 Webhook，最后返回原 422；不能在将要回滚的业务事务里写事件，也不能持锁另开连接写。事件持久化失败升级为 503，原策略始终不变。
- 任何有字段的成功 PATCH 把 version 精确加 1；响应携带新 version/ETag。数据库触发器拒绝 version 跳号或倒退。
- 本 PRD 没有 policy.updated Webhook；避免超出 Excel 事件合同。PRD-045 发布时再定义配置变更/快照事件。
- PATCH 不允许修改 tenant、ID、dsl_version、version、created_by 或 created_at。

### 可解释 evaluation 响应

```json
{
  "matched": true,
  "decision": "deny",
  "reason_code": "policy.rule_matched",
  "policy_reason": {
    "policy_id": "draft",
    "priority": 100,
    "condition_hash": "...",
    "comparisons": [
      {"variable": "env", "operator": "==", "value_type": "string", "expected": "prod", "actual": "prod", "matched": true}
    ]
  },
  "timed_out": false,
  "alert_required": false,
  "elapsed_us": 82
}
```

- timeout 时 `decision=deny`、`reason_code=policy.evaluation_timeout`、`policy_reason` 只保留已检查数量，不返回部分比较值。
- route_to 命中时返回 action 与 target_model_id；mask 命中只返回需要脱敏，不返回或修改用户内容。
- PRD-044 的用户可见解释闭环仅为单草稿 validate；多策略优先级由 shared policy-set evaluator 专项测试证明，PRD-046 才暴露已保存策略集 Dry Run。本文不会宣称 active 策略已经在线执行。

## 前端页面与交互设计

- 新增 admin-only “策略管理”导航，归入“安全与治理”，位置在 DLP 之前；developer/viewer 的后端导航响应不包含该项。
- 页面采用紧凑单编辑器 + 策略列表，不嵌套卡片。编辑器支持新建和选择已有策略编辑，清晰显示当前模式和版本，并把 active 标为“启用（待发布）”。
- 编辑字段包括名称、描述、状态、优先级、条件表达式、动作；选择 `route_to` 时才显示当前租户 active 模型下拉框，不能自由填写 ID。
- 条件编辑区展示固定变量/动作提示和短示例，不在页面正文讲解系统架构、快捷键或实现细节。
- “校验”调用 `/policies/validate`。成功显示固定编译状态、变量、hash 摘要；失败显示字段/位置诊断和 request_id，表达式始终用 textContent/转义后 HTML 展示。
- “保存”默认禁用，只有当前表单快照校验成功后启用；任一 input/change、选择其他策略、离开页面、登出或身份变化都立即清除校验结果并禁用保存。
- 策略编辑器使用按 `tenant_id + user_id` 隔离的内存 dirty draft；input 立即写回模块态，15 秒整页刷新在同身份同页面不得覆盖未保存输入。离开策略页、登出、tenant/user/role 变化必须清空草稿、校验和 Policy 数据。
- validate/save 共用单调 revision、AbortController 和精确表单 snapshot。每次 input、A->B 校验、导航或身份变化都先递增 revision/abort 旧请求；响应只有在 revision、controller、snapshot、tenant/user 和 active navigation 全部仍一致时才能更新 DOM/启用保存。
- 后端仍在 POST/PATCH 重编译，因此手工改 DOM、重放请求或伪造校验状态无法保存非法 DSL。
- 列表按优先级展示名称、状态、版本、条件、动作/目标、更新时间和编辑命令；长表达式允许内部换行/横向滚动，不造成根节点溢出。
- active-only loader 显式接收 consoleShellState 和 refresh generation，只有 activeNavigationItemId=policies 且角色为 admin 时读取 `/policies`；响应提交前再次核对 generation/identity/navigation。route_to 选项复用控制台已通过 `/models` 后端权限与 tenant 过滤获得的模型状态，再筛选 active Model，不额外重复拉取策略或目标。其他页面、developer/viewer 和无会话状态不发 `/policies` 请求。
- API 错误展示固定 code/message/request_id；401 清会话，403 不保留旧策略 DOM，5xx 保留可重试状态但不显示原始异常。
- name、description、model display/id、diagnostic message、request_id、canonical_expression 与错误 message 都必须按文本/属性上下文分别转义，不能把对象 JSON 塞进 data-*。
- 1280x720、900x800、390x844 下编辑器、校验结果、动作选择和列表无重叠/截断；移动端行使用字段标签，textarea/select/button 尺寸稳定，最长无空格 DSL token 可断行或在受控容器内滚动，根节点满足 `scrollWidth <= clientWidth`。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `policy.payload_invalid` | 422 | name/description/status/priority 等策略元数据非法。 |
| `policy.expression_invalid` | 422 | 词法、文法、类型或资源上限不合法；保存拒绝。 |
| `policy.action_invalid` | 422 | 动作枚举或 target 组合非法。 |
| `policy.route_target_invalid` | 422 | target 不存在、跨租户、disabled 或非 route_to 携带 target。 |
| `policy.context_invalid` | 422 | 示例上下文缺失引用变量或字段类型/长度错误。 |
| `policy.query_invalid` | 422 | GET 出现未知筛选、非法枚举或超长 query。 |
| `policy.duplicate` | 409 | 当前租户名称重复。 |
| `policy.not_found` | 404 | Policy 不存在或不属于当前租户。 |
| `policy.version_conflict` | 409 | PATCH If-Match 缺失、畸形或与事务内最新版本不一致。 |
| `policy.update_empty` | 422 | PATCH 没有可编辑字段。 |
| `policy.quota_exceeded` | 422 | 当前租户达到 200 条上限。 |
| `policy.unavailable` | 503 | 数据库、审计、事件或不完整三对象导致服务不可用。 |
| `auth.token_missing` | 401 | 匿名请求。 |
| `auth.permission_denied` | 403 | developer/viewer 或其他非 admin。 |

HTTP 合同固定为 POST create=201、GET/PATCH=200、validate 领域错误=200、DTO/JSON 结构错误=422。`/policies/validate` 对可预期草稿错误返回 `valid=false` diagnostics；同一错误在 POST/PATCH 保存路径返回上表统一 HTTP 错误。`policy.evaluation_timeout` 是求值 reason code，不是控制面可重试 503：validate 返回 200 + valid AST + deny evaluation，未来 Gateway 接入后按策略阻断响应并保留该 reason。所有成功/错误响应与响应头都带 request_id/trace_id，并设置 `Cache-Control: no-store`；错误不回显 SQL、AST、表达式全文、模型是否属于其他租户或内部异常。

稳定求值 reason code 为 `policy.rule_matched`、`policy.no_match_default_allow`、`policy.evaluation_timeout`、`policy.context_missing`；领域事件 reason 也始终使用完整 `policy.*` 名称，不使用裸 `created/invalid` 字符串。

## 安全与权限约束

1. 所有 API 不接受 tenant_id；服务层只从 AuthContext 读取租户，所有父子 JOIN/UPDATE/SELECT 显式携带 tenant_id。
2. GET/POST/PATCH/validate 全部复用同一 require_policy_admin；401/403 必须在任何 DB 业务/event/audit/Webhook 副作用前结束。
3. Policy、Rule、Action、Event、User、Model 归属由复合外键和触发器双重约束；tenant/parent/actor 字段创建后不可改写。
4. Policy ID/target ID 的跨租户与不存在语义统一，禁止资源枚举；事件和审计查询继续由既有 admin-only 边界保护。
5. DSL 只进入白名单词法器和自建 AST；禁止 `eval/exec`、动态 import、反射、属性访问、任意函数和正则，不能访问网络、文件、环境变量或数据库。
6. 长度、token、节点、深度、比较和列表上限在编译前/中逐层检查；求值再受全局单调时钟预算限制。
7. 超时必须 fail closed；调用方告警、Webhook 或日志失败都不能把 deny 改成 allow，也不能继续执行 route_to。
8. 只有 admin 管理成功响应可返回编辑所需的 condition_expr/canonical_expression，并统一 `Cache-Control: no-store`；错误、日志、审计、policy/Webhook/console event 禁止表达式、AST、description 和示例上下文正文。结构化日志只保留 tenant/policy/hash/reason/count/duration/request_id/trace_id。
9. 前端不把 Policy/DSL JSON 写入 localStorage/sessionStorage/data-*、URL/hash/query 或 console event；未保存草稿只保存在当前身份作用域的内存态。
10. validation_failed payload 只保存摘要和固定诊断码；输入位置是整数，不保存带片段的错误信息。
11. 创建/更新的三对象、审计与领域事件同事务；失败事件独立原子追加；外部 Webhook 仅在 commit 后执行。
12. route_to 目标必须当前租户 active Model，前端选项源自受权 API，后端仍在写事务内复核，不信任下拉框。
13. 本 PRD 不接 Gateway，因此不会意外让未发布策略改变线上流量；PRD-045 必须从已编译版本生成不可变快照后才能下发。

## 测试用例

### DSL 单元测试

1. 六变量、五动作、字符串/Decimal、所有允许操作符、and/or/not/括号/list 编译与 canonical hash 稳定。
2. `and`/`or` 优先级、括号、短路和 not 结果正确；空列表、混合类型、未知变量/操作符和类型不匹配均固定拒绝。
3. 阻断 `eval/exec/__import__`、函数调用、属性/dunder、赋值、分号、注释、反引号、单引号、模板、正则、对象、嵌套列表和 Unicode 混淆标识符。
4. 字符/UTF-8 byte、token、节点、比较、深度、not、列表、字符串和数字上限逐项边界测试；失败只返回固定诊断。
5. 多策略按 priority+ID 稳定执行；log_only 继续，决定性动作终止，无命中默认 allow；route_to/mask 只形成决策。
6. 可解释结果与实际短路节点一致，不包含表达式/AST/Prompt/Token；Decimal 比较无 float 漂移。
7. 注入 fake monotonic clock 触发整组超时，纯求值结果固定 deny + alert_required 且不产生副作用；控制面集成测试再证明同一 request_id 只写/投递一次超时 validation_failed，告警失败仍不改变 deny。

### 迁移测试

1. 空库 0001-0033、重复 migrate、0032 历史升级成功；四表、索引、触发器和审计新白名单存在。
2. Policy 缺 tenant/user、父子 tenant 错配、action/target 错配、非法 status/version/priority/JSON/hash 被数据库拒绝。
3. 复合 FK 列序、users/models parent unique、两个 partial unique SQL 和全部 identity/version/owner/immutable trigger 正确；跨租户 Rule/Action/target/actor/event、归属字段 UPDATE、version 跳号和 event UPDATE/DELETE 均被拒绝。
4. 先种齐 0032 的 14 个 audit action/8 个 object type、Usage export job/event 和 Webhook delivery；up/down/up 后非 policy 行逐字段一致，5 个审计索引和 3 个触发器完整恢复。
5. 0033 down 通过重建过滤 policy audit，再按 event/action/rule/policy/index 顺序回滚；直接 DELETE 不被误用，历史 audit/usage/webhook 全保留。
6. `integrity_check=ok`、`foreign_key_check` 为空；运行库迁移版本最终为 0033。

### 后端主流程

1. admin validate 合法 DSL 返回 hash/variables/canonical；带上下文返回匹配/不匹配、五动作和可解释 comparison。
2. admin 创建五类策略；GET 按 priority+ID 排序且筛选正确；Rule/Action 一对一、version=1、request_id/trace_id 正确。
3. PATCH 元数据/表达式/动作/target/状态成功，version 增长、hash/AST/variables 更新且旧对象不残留。
4. PATCH 必须 If-Match；并发 A/B 只有当前 version 成功，事务内 before audit 正确，旧 version/缺失/畸形 header 返回 version_conflict 且无覆盖。
5. create/patch 写 audit 安全摘要；created event 从持久行使用稳定 event_id，Endpoint 可订阅、HMAC 与最小 payload 正确；policy/audit 两路 Webhook 分别失败均不回滚已提交策略或阻断另一事件。
6. 无匹配默认 allow、log_only 后续 decisive、同优先级 ID 决胜和 timeout deny 由共享求值器证明。
7. POST=201、GET/PATCH=200、validate 领域错误=200、结构错误=422；每类响应 body/header 的 request_id/trace_id/no-store 合同正确。
8. TestClient 替换 evaluator 确定性返回 timeout：HTTP 200/valid=true/deny、单条 validation_failed/单个 original delivery；告警投递失败仍保持相同 deny 响应。

### 异常、权限与安全

1. save 绕过前端直接提交非法 DSL 仍 422 且无 Policy/Rule/Action；validation_failed event 有 request_id/长度/固定诊断且无正文；只有编译成功后才允许 hash/fingerprint。
2. developer/viewer 访问 GET/POST/PATCH/validate 均 403，匿名 401；前端隐藏不作为测试替代。
3. 两租户同名可创建；跨租户 Policy PATCH 404，跨租户/disabled route target 统一 422且不泄露目标状态。
4. 空 PATCH、重复名称、超配额、unknown query、额外字段、过长/非法 path ID/文本和不完整 context 返回固定错误；action enum、budget float/NaN/bool/object、priority bool 的 200/422 分层符合合同。
5. 锁内发现跨租户/disabled target 后业务 rollback、释放锁、失败事件独立 commit；失败事件 commit 失败返回 503。created event/audit commit 失败回滚业务，两个 Webhook 各自失败不输出 SQL/异常/表达式。
6. 重复 validation request 只产生一条 event/一个 original delivery，但重新确定性计算响应，不宣称回放未存储的 diagnostics；定向 conflict 不吞 owner/FK/CHECK 错误。
7. 使用唯一 sentinel 覆盖 expression、description、context、非法 action、Authorization/Cookie，并核对日志、audit_logs、policy_events、webhook_deliveries、console_page_events 均无 sentinel；Provider Key/Token/Prompt/Response 同样零命中。
8. 在 context 伪造 tenant 字段，以及伪造 route target、AST/hash/version/actor 字段均被 DTO、服务或数据库拒绝。
9. developer/viewer/匿名除 401/403 外，断言 policy/event/audit/Webhook delivery 均无副作用。

### 前端

1. client 请求不含 tenant_id/AST/hash/version；GET/validate/create/PATCH 与统一错误解析正确。
2. admin 有导航和编辑器；developer/viewer 无导航、无 DOM、无后台 `/policies` 请求；既有模型目录加载行为不因本 PRD 扩权，畸形 hash 回退 overview。
3. 合法校验启用保存；任一字段变化、切换编辑对象、离页/登出后校验失效；直接 DOM 解锁仍由后端拒绝非法保存。
4. 延迟 A 响应、A->修改->B 后 A 最后返回、自动刷新、离页、登出和身份切换均不能恢复旧 validation 或启用保存；Abort/revision/snapshot/identity/navigation 门禁逐项覆盖。
5. 同身份同页面 15 秒刷新保留 dirty draft；离页、登出、tenant/user/role 变化清空草稿与 Policy DOM。
6. action 切换正确清空/要求 target；route_to 下拉仅含后端 active 当前租户模型。
7. name/description/model/非法表达式/diagnostic/request_id/canonical/error 的 HTML/属性注入和超长 token 全部转义，不调用前端 eval，也不把 JSON 放 data-*。
8. 创建后列表更新、编辑回填/ETag version 更新、筛选/空态/错误态/并发按钮状态正确。
9. 1280x720、900x800、390x844 的表单、校验区、列表和长无空格表达式无重叠、截断或根节点横向溢出。

### 回归与运行验收

1. PRD-015/029/036 的模型别名、Environment、Fallback 回归通过；PRD-023/024 的预算/DLP 回归通过。
2. PRD-034 审计、PRD-025/042 Webhook、PRD-043 用量导出与 shared error/runtime 回归通过。
3. Python 全量、Node 示例、Console 全量/lint、中文注释门禁通过；配置边界测试和 `.env.example`/开发文档同步完成。
4. 0033 up/down/up 在副本验证后应用运行库，完整性与外键检查通过；三服务健康均 HTTP 200。
5. 真实 API 完成合法校验、非法注入拒绝、五动作创建、优先级列表、PATCH 版本、developer/viewer 权限和事件/审计检查。
6. 真实浏览器完成 admin 创建/校验/编辑/切换/错误显示；developer/viewer 强制 `#policies` 回退且 DOM/网络都无 `/policies`。1280x720、900x800、390x844 检查 `scrollWidth<=clientWidth`、console/page/HTTP error=0 并生成遮罩邮箱/tenant/session 的截图。
7. 日志扫描敏感模式零命中；运行库无孤立 Policy/Rule/Action、无失败迁移和外键错误。

## 代码修改计划

1. 完成两轮设计自审，修正 DSL 语义、范围、权限、租户、超时、事件、迁移和前端校验风险。
2. 实现 shared 受限 DSL 编译/求值器、配置和固定错误码，并先用单元测试锁定注入、优先级、解释和超时语义。
3. 实现 0033 正反迁移、Policy/Rule/Action/Event、审计与 Webhook 白名单、admin-only 守卫。
4. 实现 policies models/routes/service，保证三对象、审计、事件事务原子与 tenant-qualified target 校验。
5. 实现控制台 API、active-only 加载、策略编辑器、校验失效、列表和响应式样式。
6. 增加迁移、API 主流程、异常、权限、跨租户、审计、事件、前端和浏览器测试。
7. 执行专项、全量、运行库迁移/回滚、真实 API、日志与三档浏览器验收。
8. 更新 README、目录说明与 `docs/testing/PRD-044-tests.md`；全部证据通过后才把本文件状态和总体完成清单更新为已完成。

## 设计自审 1

第一轮检查需求边界、DSL 文法/语义、与 PRD-045/046 的隔离、API 闭环和可解释/超时验收，修订如下：

- **阻断：原 grammar 把 and/or 写在同一层，却又声明 and 优先。** 已拆为 or_expr -> and_expr -> unary_expr -> comparison，固定短路和 AST 扁平规则。
- **阻断：只写“canonical AST”不足以支撑 PRD-045。** 已定义每类节点、操作符名、logical 扁平/顺序、Unicode/Decimal 规范、JSON 序列化和 condition_hash 字节合同。
- **阻断：纯求值器内置 on_timeout 回调会引入阻塞、重入和副作用。** 已移除回调；shared 只返回 deny + alert_required，控制面/未来数据面在结果之后独立告警。
- **阻断：timeout 同时被写成 200 decision、503 error 和 validation 失败。** 已固定控制面 validate 为 200 + valid AST + deny evaluation；future Gateway 按策略阻断并保留 reason，不把运行超时当可重试 503。
- **重要：Decimal 的词法、上限和 canonical 缺失。** 已固定 12 位整数/12 位小数、绝对值上限、无指数/前导零以及去尾零和负零归一规则。
- **重要：无效 validate 可造成事件/Webhook 洪泛。** 第一轮加入单请求去重与 diagnostics 8 条上限；第二轮进一步按 created/validation_failed 拆成两个 partial unique，避免错误复用 request_id 语义。
- **重要：route target 在事务前验证存在 TOCTOU。** 已要求 `BEGIN IMMEDIATE` 同一快照内复核 tenant+active；PRD-045 生成快照时还要再次校验。
- **租户：validate context 不应让客户端提供 tenant。** 已从 DTO/示例移除 tenant，求值时只由 AuthContext 注入；额外 tenant 字段按结构错误拒绝。
- **边界：validation_failed 不能被未来运行超时滥用。** 已列明 validate/save/timeout 触发矩阵；未来数据面使用 gateway.request_failed 或后续专属事件。
- **验收：多策略解释没有公开 API。** 已明确本期单草稿 validate 提供用户可见解释，shared policy-set 测试证明优先级，已保存策略集 Dry Run 留给 PRD-046。
- **语义：active 容易被误读为已在线。** 已定义为本地策略集/下一快照候选，控制台标“启用（待发布）”，本期不宣称影响 Gateway。
- **安全：无效原文直接 SHA 可被低熵字典反推。** 词法/文法失败不生成原文 hash；draft fingerprint 只从 canonical AST/action 生成。
- **兼容：diagnostic 位置、资源计数和解释上限不明确。** 已固定 0-based Unicode code-point/end-exclusive、稳定排序、节点计数规则、20 条 log_only/64 条 comparison 上限与 truncation 标志。
- **收敛：v1 布尔字面量没有对应变量。** 已从文法删除，空列表也明确拒绝，避免扩大没有业务价值的解析面。

第一轮结论：上述阻断和重要问题均已反映到设计；当前仍未编码，必须完成第二轮权限、租户、迁移、前端和测试审查后才能进入实现。

## 设计自审 2

第二轮检查权限、租户、迁移回滚、事务、审计/Webhook、敏感信息、前端异步状态和测试可执行性，修订如下：

- **阻断：一个通用 event UNIQUE 同时约束 created/failed，且事件 payload 无法回放完整 validate 响应。** 已拆为 created 按 tenant+policy、validation_failed 按 tenant+actor+request 的 partial unique；后者只去重副作用，响应仍重新确定性计算。
- **阻断：写事务内发现非法 target 后写失败事件会一起 rollback，另开连接又会锁冲突。** 已固定 rollback/释放锁 -> 独立 BEGIN IMMEDIATE 插 event -> commit -> Webhook -> 原 422 的顺序；事件写失败升级 503，业务保持原状。
- **阻断：Pydantic Literal/Decimal 会抢先返回 422，使 validate 无法形成 valid=false/event。** DTO 现在只严格守结构/extra，领域值以有界宽类型进入 service；JSON/结构错误 422 不写事件，领域 DSL/action/target/context 错误 200+diagnostics+event。
- **阻断：PATCH 锁外回读会 lost update 并产生错误 before audit/version。** 已要求 If-Match，BEGIN IMMEDIATE 后 tenant-qualified 权威重读三对象、比较版本、复核 target、条件更新和 version+1；冲突固定 409。
- **租户：users/models 只有全局主键，不能直接支撑复合 FK。** 0033 先建 `(tenant_id,user_id)` 与 `(tenant_id,model_id)` 唯一索引，再对 actor/creator/target 使用复合 FK，并保留 owner/active trigger；down 最后删除专属索引。
- **迁移：immutable audit trigger 会阻断直接删除 policy audit。** up/down 都按 0032 全字段重建 audit；down 以过滤复制恢复全部 14 action/8 object、5 索引和 3 trigger，再按 event/action/rule/policy/index 删除。
- **数据库：身份、版本与事件关联约束不足。** 已增加 Policy/Rule/Action identity trigger、Policy version 精确 +1、事件相关 CHECK、JSON/长度上限、两类 partial unique 和 immutable event。
- **Webhook：重新 build 随机 event_id 会破坏原始事件去重。** 现在只从已提交 policy_events 行构造稳定 `evt_{policy_event_id}`；定向 conflict 不吞安全错误，policy/audit 两路 dispatch 独立失败隔离。
- **安全：原文返回与“完全不扩散”表述冲突。** 已限定只有 admin 成功管理响应可返回编辑正文且 no-store；错误、日志、审计、事件/Webhook/console event 禁止正文，并扩 audit 最终拒绝字段和 sentinel 扫描。
- **API：FastAPI 默认忽略 unknown query，path/version 也缺稳定合同。** 已增加 query allowlist/`policy.query_invalid`、`pol_` path 格式、If-Match/ETag、完整状态码与 response header 合同，并要求注册 ErrorSpec/diagnosis。
- **权限：新依赖若复制角色判断会漂移。** `require_policy_admin` 必须委托既有 admin 守卫；四个接口一致，401/403 后还断言 DB/Webhook 无副作用。
- **前端：只在 input 清 validation 不能阻止旧异步响应复活保存按钮。** 已加入 revision + AbortController + 精确 snapshot + tenant/user/navigation 门禁，并覆盖 A/B 乱序、离页、登出和自动刷新。
- **前端：15 秒整页刷新会丢未保存编辑。** 已定义按 tenant+user 隔离的内存 dirty draft；同身份同页保留，离页/身份/角色变化清空，active-only loader 返回前再次核对 generation。
- **XSS/响应式：正文之外的名称、模型、诊断和 request_id 也可注入。** 已要求逐上下文转义所有动态字段、不把 JSON 放 data-*，三档验收增加长无空格 token 和 `scrollWidth<=clientWidth`。
- **测试：正常路径不足以证明事务/告警。** 已补锁内失败后独立事件、event commit/audit commit、两路 Webhook、重复 request、timeout TestClient、复合 FK/partial unique、全量迁移历史和敏感 sentinel 故障注入。

第二轮结论：阻断和重要问题均已写入最终设计，编码前设计门禁通过；此后专项、全量、迁移、真实 API 和浏览器完成门禁也已逐项通过，证据记录在 `docs/testing/PRD-044-tests.md`。
