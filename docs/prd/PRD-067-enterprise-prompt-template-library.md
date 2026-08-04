# PRD-067 - 企业 Prompt 模板库

状态：**已完成**。设计、两轮主审与后端交叉终审、实现、0059、专项与必要回归、真实 Runtime、
应用内 Browser 双视口、资源清理、文档和中文注释门禁均已通过。后端交叉审查发现的 DLP、加密 AAD、
精确复合 owner 及其衍生合同均已修订并由独立终审确认关闭。
该状态只代表 PRD-067 完成，`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表!A68:R68` 与 `03_M2-M3扩展交付!A4:R4` 一致定义：
  `PRD-067 | M2-企业增强 | P2 | 扩展层 | Prompt模板库 | 企业Prompt模板库`。
- 主要用户是业务管理员和开发者；范围是模板创建、版本、变量、适用项目、推荐模型、
  启用/禁用，以及调用时记录精确模板版本。
- 明确边界是“不做复杂 Prompt IDE”。本条不引入流程编排、可执行脚本、条件/循环、多人实时协作、
  审批流、自动评测、Prompt 市场或跨租户共享。
- 页面是 `PAGE-094 Prompt模板页`；API 聚合编号是 `API-301..API-304`；数据对象是
  `OBJ-090 PromptTemplate` 和 `OBJ-091 TemplateVersion`；Webhook 事件是
  `EVT-074 prompt_template.published`。
- `TC-0133` 要求主流程覆盖创建、不可变版本、变量、适用项目、推荐模型、启停和调用版本记录；
  `TC-0134` 要求异常、越权、安全和“不做复杂 Prompt IDE”边界返回统一错误码，不泄露敏感信息。
- PRD-066 已完成，README 已把当前开发项切换为 PRD-067；`docs/prd` 和 `docs/testing` 原先均无
  PRD-067 文件，因此本条是按里程碑、优先级和 Excel 行序识别出的下一条未完成 PRD。

Excel 的 API 表采用拆分聚合表达：`API-301=POST`、`API-302=GET`、`API-303=PATCH`、
`API-304=prompt-templates`，并没有为每个子资源给出完整路径。实现必须按版本管理需求补全具体路径，
不能把 `prompt-templates` 机械解释成 HTTP 方法。

### 1.2 架构约束

- 所有业务数据必须携带 `tenant_id`，或通过不可绕过的同租户复合关系追溯；所有查询和修改均以
  Session 恢复出的 tenant 为第一过滤条件。
- 权限必须由后端逐请求读取当前 RBAC 矩阵并执行；前端导航、按钮和字段隐藏只改善体验。
- API 继续返回稳定英文错误码、`request_id` 和 `trace_id`，并统一设置 `Cache-Control: no-store`。
- Prompt、变量值、渲染结果、Response、Provider Key、Virtual Key、Token、Cookie、Authorization
  不得进入普通日志、审计摘要、Webhook、异常文本、测试输出或 Browser 证据。
- 管理配置应先校验、再保存、再形成不可变发布版本，并记录版本、变更人、时间、原因和审计事实。
- 企业 Chat 必须继续通过 PRD-066 的一次性 grant 进入 Gateway 完整治理链；模板不能直接调用
  Provider，也不能绕过鉴权、DLP、Policy、预算、路由、用量、成本、RequestLog 和错误归一。
- OpenRouter、硅基流动仍是竞品和迁移兼容对象，不作为本功能默认上游；本条不改变 Provider 范围。

### 1.3 现有能力与真实缺口

可复用能力：

1. PRD-045 已提供 `building -> published` 一次封印、版本 head、不可变 item 和租户复合 owner 模式。
2. PRD-059 已提供产品级 Permission catalog、六角色授权上限、逐请求数据库权限和矩阵 revision/CAS。
3. PRD-066 已提供 PAGE-093、短生命周期 ChatSession、单次 execution grant、Control/Gateway 双重实时
   owner 校验，以及 Prompt/Response 不落库的完整治理调用链。
4. 共享数据库安全层已有 AES-GCM 实现可参考，但通用 `encrypt_secret/decrypt_secret` 使用 AAD=None
   且复用 Provider/Auth 密钥域，**不能**直接用于模板；本条必须实现独立、可绑定 owner 的模板 cipher。
5. AuditLog、不可变 source event、Webhook delivery、统一错误目录和 request/trace 中间件可以复用。

当前缺口：

1. 仓库没有 PromptTemplate、TemplateVersion、版本绑定或模板调用事实。
2. RBAC catalog revision 3 只有 `chat.use`，没有模板读、写、调用三个独立权限。
3. `audit_logs.action/object_type` 是数据库 CHECK 枚举，直接写模板审计会失败；0059 必须保真扩展，
   并恢复 PRD-058 导出序列、复合外键与不可变触发器。
4. Webhook 事件目录没有 `prompt_template.published`，不能把任意 payload 直接交给通用派发器。
5. PRD-066 的 Chat 请求只允许 user/assistant history，Gateway 内部入口也只接收 grant/history；
   需要在不开放客户端 system role 的前提下加入服务端模板渲染。
6. Console 没有 PAGE-094，也没有 PAGE-093 的模板选择、变量表单和授权生命周期 owner。

### 1.4 实现范围

- 新增模板列表、详情、创建、元数据/状态 CAS 更新和发布新版本接口。
- 发布版本由服务端一次封印，正文和变量 schema 加密落库，发布后不得修改或删除。
- 每个版本冻结适用项目和推荐模型别名；绑定关系与版本一起不可变。
- 模板调用仅通过企业 Chat：浏览器提交版本 ID 和变量值，Control 与 Gateway 均实时复核权限、
  tenant、模板状态、当前版本、项目适用性和资源 owner。
- Gateway 使用受限占位符语法在内存中渲染本轮最终 `user` message，再进入既有完整治理编排。
  现有 DLP 只扫描 `role=user`；v1 禁止把模板放入 system role 后误称已受 DLP 治理。
- 调用事实只记录精确 TemplateVersion、Chat message、request/trace、状态和安全错误码；不记录变量值、
  渲染正文、用户 history 或模型 Response。
- 新增 PAGE-094，并在 PAGE-093 中按权限提供模板选择和变量输入。
- 新增 `prompt_template.published` 不可变 source event、AuditLog 和提交后 Webhook 投递。

### 1.5 明确不做

- 不做 Jinja、Mustache 表达式、条件、循环、函数、代码执行、工具调用或任意模板插件。
- 不做长期草稿、分支、合并、审批、多人协作、Diff 编辑器、自动优化、评测集或 Prompt IDE。
- 不允许调用历史版本；历史版本仅用于查看和追溯。调用必须显式匹配模板当前已发布版本。
- 不做跨租户模板共享、公共市场、导入导出、批量发布或模板级自定义 ACL。
- 不改变公开 OpenAI-compatible API；模板能力先只接入已认证的 Console 企业 Chat。
- 推荐模型是 UI 默认建议，不是新的路由策略；最终模型仍由 ChatSession 和 Gateway 路由治理决定。

## 2. 权限模型

0059 把产品权限目录从 revision 3 提升到 revision 4，新增：

| permission code | 风险 | 用途 |
| --- | --- | --- |
| `prompt_template.read` | read | 读取模板、版本历史、解密后的定义和绑定 |
| `prompt_template.write` | critical | 创建、启停、修改元数据和发布新版本 |
| `prompt_template.use` | write | 在企业 Chat 中选择并调用当前模板版本 |

默认产品上限与新租户默认矩阵：

| 角色 | read | write | use |
| --- | --- | --- | --- |
| Admin | 是 | 是 | 是 |
| Developer | 是 | 是 | 是 |
| Viewer | 否 | 否 | 是 |
| FinanceAdmin | 否 | 否 | 否 |
| SecurityAdmin | 否 | 否 | 否 |
| OpsAdmin | 否 | 否 | 否 |

说明：Viewer 已由 PRD-066 默认拥有 `chat.use`，因此仅获得 `prompt_template.use` 的最小调用能力；
其 Chat 选项只返回调用所必需的模板/版本 ID、名称、变量表单、适用项目 ID 和推荐 alias ID 安全投影，
不返回模板正文、描述、历史或 Project/Model 的完整管理目录。
Admin/Developer 才能访问 PAGE-094 和模板定义。运行时绝不依据角色名补权，以上集合只用于产品上限、
新租户默认值和迁移 canonical 矩阵；实际授权始终来自当前 tenant 的 RolePermission。

0059 的可执行 RBAC 常量冻结如下，迁移 pre/post guard 和共享单测必须逐值一致：

| 状态 | Permission 数 | Role template 数 | Admin hash | Developer hash | Viewer hash |
| --- | ---: | ---: | --- | --- | --- |
| revision 3 | 51 | 134 | `af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef` | `ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3` | `77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228` |
| revision 4 | 54 | 141 | `e04e456f0b94541ea75e1b29e6a0716537a0413d5fafacc134a177dc798ab320` | `2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c` | `3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086` |

Finance/Security/Ops 的 permission 集合和 hash 保持不变。Up 只对 preflight 精确匹配上述 revision 3 hash
的 canonical Admin/Developer/Viewer 自动扩权；自定义矩阵不补权。Down 只收缩精确匹配 revision 4
canonical hash 的三角色，其他状态在首项 DDL 前拒绝。

所有写接口使用 `prompt_template.write`，读接口使用 `prompt_template.read`。Chat 发送先要求 `chat.use`，
携带模板时再要求 `prompt_template.use`；无模板的 Chat 不因缺少模板权限受影响。

## 3. 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 共享 RBAC | `shared-sdk/python/metahub_shared/rbac.py` | revision 4、三权限目录与默认上限 |
| 共享模板合同 | `shared-sdk/python/metahub_shared/prompt_templates/` | 占位符解析、严格定义、内存渲染和 Webhook 安全投影 |
| 迁移 | `infra/migrations/0059_*` | 模板/版本/绑定/调用/事件、RBAC、AuditLog 保真 up/down |
| 控制面模板域 | `control-plane-api/src/metahub_control/prompt_templates/` | models、严格 wire、service、routes、审计和事件派发 |
| 认证 | `control-plane-api/src/metahub_control/auth/dependencies.py` | read/write/use 后端依赖 |
| 企业 Chat | `control-plane-api/src/metahub_control/enterprise_chat/` | options 投影、请求合同、调用事务和安全终态 |
| Gateway Chat | `gateway-data-plane/src/metahub_gateway/api/internal_chat.py` | 模板变量严格解析、实时复核、解密并追加最终 user message |
| Audit/Webhook | `control-plane-api/.../audit/`、`shared-sdk/.../webhooks.py` | 动作/对象白名单与 published 事件合同 |
| Console API | `console-web/src/lib/` | 模板 CRUD、版本发布与错误映射 |
| PAGE-094 | `console-web/src/features/prompt-templates/` | 列表、详情、版本历史、创建/发布/启停 |
| PAGE-093 | `console-web/src/features/enterprise-chat/` | 模板选择、变量表单、版本 owner 和失权清理 |
| 导航与装配 | Control `console/service.py`、Console `main.js` | permission-based 导航、路由、预取与生命周期 |
| 文档与测试 | `docs/api`、`docs/testing`、`README.md` | API、目录、测试和运行证据 |

## 4. 数据表和字段设计

迁移名冻结为 `0059_enterprise_prompt_template_library.sql` 与对应 `.down.sql`。

### 4.1 `prompt_templates`

| 字段 | 约束与用途 |
| --- | --- |
| `prompt_template_id` | `ptpl_*` 主键；另有 `(tenant_id,prompt_template_id)` 唯一候选键 |
| `tenant_id` | 非空，复合 owner 的第一列 |
| `name` | 1..120 字符、UTF-8 不超过 360 bytes、无控制字符；tenant 内 NOCASE 唯一 |
| `description` | 0..1000 字符；只在模板读取响应中返回，不进入事件/日志 |
| `status` | `active/disabled`；disabled 的新调用必须失败 |
| `current_version_id` | 非空；与固定的 `current_version_state` 一起通过延迟复合 FK 指向本模板已发布版本 |
| `current_version_state` | 非空且 `CHECK(current_version_state='published')`；不是业务可写状态，只用于让数据库外键证明 head 绝不指向 building 版本 |
| `revision` | 从 1 开始；元数据、状态和 current version 变化均以 CAS 单调增加 |
| `created_by/updated_by` | 同 tenant 用户复合 FK |
| `created_at/updated_at` | UTC 秒文本，no-op 不更新时间 |

模板不物理删除；重名、跨租户 current version、revision 跳变或不存在 owner 由数据库和服务层共同拒绝。

### 4.2 `prompt_template_versions`

| 字段 | 约束与用途 |
| --- | --- |
| `prompt_template_version_id` | `ptv_*` 主键和 tenant 复合候选键 |
| `tenant_id/prompt_template_id` | 复合 FK 指向同 tenant 模板 |
| `version_number` | 每模板从 1 连续递增，tenant/template/version 唯一 |
| `state` | 事务内 `building`，提交前唯一允许一次转为 `published`；调用只认 published |
| `definition_schema_version` | 固定 `prompt-template-v1` |
| `definition_ciphertext` | 独立 Prompt Template AES-GCM 密文；加密 canonical JSON `{content, variables, change_summary}` |
| `definition_size_bytes` | 加密前 canonical UTF-8 大小，用于资源门禁，不泄露正文 |
| `variable_count` | 0..32，仅供列表和完整性复核 |
| `project_scope` | `all/selected`；selected 必须至少一个版本项目绑定 |
| `recommended_model_count` | 0..8，与版本模型绑定数量一致 |
| `change_summary_hmac` | 64 位小写十六进制分域 keyed HMAC；1..300 字符、UTF-8 不超过 900 bytes 且无控制字符的明文只存在于加密 definition 和授权解密响应中 |
| `created_by/created_at/published_at` | 同 tenant 用户与 UTC 时间 |
| `request_id/trace_id` | 创建或发布请求的稳定链路 ID；同 tenant 的 request_id 唯一 |

`building` 只是单个 `BEGIN IMMEDIATE` 事务中的内部装配状态，不对 API 暴露，也不是产品草稿。
版本封印后 header、密文、计数、绑定和顺序均不可更新/删除。

版本表必须同时提供以下候选键，禁止“同 tenant 不同模板版本”拼接：

- `UNIQUE(tenant_id,prompt_template_version_id)`，用于最小 tenant owner 查询；
- `UNIQUE(tenant_id,prompt_template_id,prompt_template_version_id)`，供模板 head 与绑定引用；
- `UNIQUE(tenant_id,prompt_template_id,prompt_template_version_id,state)`，供模板 head 在数据库层固定引用 published 状态；
- `UNIQUE(tenant_id,prompt_template_id,version_number,prompt_template_version_id)`，供 Invocation 冻结精确版本。

模板 head 外键固定为
`(tenant_id,prompt_template_id,current_version_id,current_version_state) ->
prompt_template_versions(tenant_id,prompt_template_id,prompt_template_version_id,state)`，并使用
`DEFERRABLE INITIALLY DEFERRED` 解决 v1 创建时的循环关系。事务可先插入 building 版本和固定为
published 的模板 head，但提交前必须完成 seal；否则四列延迟外键在 COMMIT 失败，不能只依赖应用或 trigger
证明版本已经 published。

加密定义内的 `content` 最多 32 KiB UTF-8；`variables` 最多 32 项，每项只允许：

- `name`：`^[a-z][a-z0-9_]{0,63}$`，并拒绝 `constructor/prototype/__proto__` 等保留键；
- `label`：1..80 字符；
- `required`：严格布尔；
- `max_chars`：1..8000 的严格整数。

加密定义内的 `change_summary` 必须为 1..300 字符、UTF-8 不超过 900 bytes 且不含 Unicode 控制字符；
`definition_size_bytes` 计算完整 canonical `{content,variables,change_summary}`，不能只统计 Prompt 正文和变量；
完整 canonical definition 的 UTF-8 总量最多为 128 KiB，避免变量标签和 schema 元数据绕过正文上限放大
密文、数据库页和解密内存占用。Control 与 Gateway 必须复用同一个共享常量，不能分别放宽。

占位符语法只允许 `${lower_snake_case}`。服务端使用标准库 `string.Template` 的解析能力，并在渲染前
额外拒绝裸 `$`、`$name`、未知占位符、重复变量、声明但未使用变量、未声明占位符和非 exact-set 值；
不执行表达式、属性访问或函数。变量值总 UTF-8 上限 64 KiB，渲染后 final user Prompt 上限 64 KiB。

Python 解析结果使用不可变 tuple/mapping；Console 构造变量 map 时使用 null-prototype 对象并逐键复制，
不能用不可信对象做 `Object.assign`、扩展运算或 DOM dataset，避免变量名成为原型污染入口。

### 4.2.1 独立模板加密合同

- 新增 `METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET`，Control 与 Gateway 必须显式配置同一稳定根密钥；
  不允许回退 `METAHUB_AUTH_TOKEN_SECRET`、Provider Key secret、Webhook secret 或硬编码 dev 默认值。
- 密文格式冻结为 `ptpl-aesgcm-v1:v1:<nonce_b64>:<ciphertext_b64>`；格式版本与 key version 均参与解析，
  未知版本失败关闭，为后续受控轮换预留边界。
- AES-GCM AAD 是 canonical UTF-8 JSON：
  `["prompt-template-v1",tenant_id,prompt_template_id,prompt_template_version_id,version_number,"v1"]`。
  AAD 由可信 DB owner 生成，不从请求正文接收。
- keyed idempotency fingerprint 使用同一稳定根密钥经独立 domain label
  `metahub/prompt-template/idempotency/v1` 派生的 HMAC key；加密 key 使用不同 domain label，禁止跨用途复用。
- change summary 摘要使用第三个独立 domain label `metahub/prompt-template/change-summary/v1` 派生 HMAC key；
  数据表、Audit 摘要和 Event 均不得保存该管理说明明文或普通无密钥 hash。
- 缺少配置统一 503 `prompt_template.unavailable`（Gateway 映射 `chat.gateway_unavailable`）；格式、AAD、
  认证标签或 definition schema 损坏统一 500 `prompt_template.definition_unavailable`
  （Gateway 映射 `chat.response_invalid`），异常链不得携带密文或密码学细节。
- 必测跨 tenant、同 tenant 跨 template、同 template 跨 version 和修改 version number 的密文互换，
  四类都必须因 AAD 认证失败且不产生 Provider 调用。

### 4.3 `prompt_template_version_projects`

- 字段：`version_project_id`、`tenant_id`、`prompt_template_id`、`prompt_template_version_id`、
  `project_id`、`created_at`。
- 绑定外键固定为
  `(tenant_id,prompt_template_id,prompt_template_version_id) ->` 版本三列候选键，Project 另用
  `(tenant_id,project_id)`；同版本同项目唯一。
- 只能在父版本 `building` 时插入；父版本 published 后禁止增删改。
- `project_scope=all` 时必须零行；`selected` 在 seal 时必须至少一行且所有 Project 当前 active。

### 4.4 `prompt_template_version_models`

- 字段：`version_model_id`、`tenant_id`、`prompt_template_id`、`prompt_template_version_id`、
  `model_alias_id`、`display_order`、`created_at`。
- 版本外键同样固定为 `(tenant_id,prompt_template_id,prompt_template_version_id)` 三列，ModelAlias 另用
  `(tenant_id,model_alias_id)`；同版本 model alias 唯一，`display_order` 从 0 连续，只能在 building 时插入。
- 发布时别名必须属于同 tenant 且 active；发布后绑定不可变。
- 这些绑定表达“推荐顺序”，不替代 Chat 模型权限和 Gateway 路由。调用所选模型不在推荐列表时允许继续，
  但 UI 不自动切换；推荐模型失效也不允许跨 tenant 回退。

### 4.5 `chat_prompt_template_invocations`

| 字段 | 约束与用途 |
| --- | --- |
| `prompt_template_invocation_id` | `ptinv_*` 主键 |
| `tenant_id` | 所有复合关系第一列 |
| `prompt_template_id/version_id/version_number` | 四列 FK 精确冻结同 tenant、同模板、同 version number 的版本 |
| `chat_session_id/chat_message_id` | 精确复合 FK 到 PRD-066 Session/Message owner；每条 Message 最多一个调用 |
| `actor_user_id/actor_auth_session_id` | 调用时登录 owner，均由后端恢复 |
| `gateway_request_id/gateway_trace_id` | 与 Chat message 和 RequestLog 关联的链路 ID |
| `status` | `in_flight/succeeded/failed`，只允许一次合法终态转换 |
| `failure_code` | 仅 failed 可填稳定内部/公开错误码，不保存异常文本 |
| `created_at/completed_at` | UTC 时间 |

表中不保存 Prompt、变量名、变量值、渲染正文、history、Response、Token、Key 或 Endpoint。
创建 Chat message、一次性 grant 和 invocation 必须在同一个 `BEGIN IMMEDIATE` 事务内完成；成功/失败
终态与 Chat message 终态同事务更新。即使模型调用失败，调用过哪个模板版本仍可追溯。

0059 必须为 PRD-066 表补充且在 down 恢复以下候选键：

- `chat_sessions(tenant_id,chat_session_id,owner_user_id,owner_auth_session_id)`；
- `chat_messages(tenant_id,chat_session_id,chat_message_id)`；
- `chat_messages(tenant_id,chat_session_id,chat_message_id,owner_user_id,gateway_request_id,gateway_trace_id)`。

Invocation 对版本使用
`(tenant_id,prompt_template_id,version_number,prompt_template_version_id)` 四列 FK；对 ChatSession 使用
`(tenant_id,chat_session_id,actor_user_id,actor_auth_session_id)` 四列 FK；对 ChatMessage 使用上述六列 FK。
INSERT owner trigger还要逐字段核对 Message/Session 的 owner、request/trace、`in_flight` 状态和 version
published/current；不能依赖多个独立外键拼出一个数据库中从未存在过的组合。

### 4.6 `prompt_template_events`

- 不可变 source event，字段包含 event ID、tenant、template/version ID、version number、event type、
  actor、`publication_audit_log_id`、request/trace、固定安全 payload、created_at 和 dispatch 状态。
- `event_type` 本迁移只允许 `prompt_template.published`。
- payload 只包含 `template_id`、`version_id`、`version_number`、`template_revision`、`project_scope`、
  `project_count`、`recommended_model_count`；不含名称、描述、change summary、正文、变量 schema/值。
- POST v1 同事务写两条 AuditLog：`prompt_template.created` 与
  `prompt_template.version_published`；source event 只绑定后者。后续发布写一条 version_published，PATCH
  写一条 updated。事件用 tenant-qualified FK 绑定 publication AuditLog，并由 owner trigger 精确复核
  action/object/actor/request/trace/time；网络 Webhook 在提交后复用现有 delivery/HMAC。

### 4.7 `prompt_template_operations`

该表保存低频管理写的不可变幂等事实，不保存请求或响应快照：

- 字段：`prompt_template_operation_id`、`tenant_id`、`actor_user_id`、`request_id`、`trace_id`、
  `operation(create/patch/publish)`、`intent_hmac`、`prompt_template_id`、按 operation 受约束的 version ID/number、
  `result_template_revision`、`result_audit_log_id`、`created_at`。
- 表级 CHECK 固定 operation 与版本结果的 exact 关系：`create/publish` 的 version ID 和 version number
  必须同时非空，`patch` 的两列必须同时为空；禁止只填其中一列或让 PATCH 假装产生版本。
- `UNIQUE(tenant_id,request_id)` 阻止同一请求 ID 跨 action/模板复用；tenant-qualified FK 绑定对应
  AuditLog。create Operation 绑定 `prompt_template.created`，patch 绑定 updated，publish 绑定
  version_published；owner trigger 复核 actor、模板、版本、revision、request/trace 和 action/object。
- canonical intent HMAC 覆盖 operation、actor、name/description/status、定义明文、变量 schema、排序后的
  project IDs、有序 recommended model IDs、change summary 和 expected revision；缺失字段也有确定 null 标记。
- 重放先按 tenant+request ID 命中 operation，再以常量时间比较 HMAC、actor 和 action；一致则返回记录的
  version 事实（若有）加当前权威模板投影、`replayed=true`，不新增 Audit/Event；不一致固定 409。
- Operation 只允许 INSERT，禁止 UPDATE/DELETE/REPLACE。它不是敏感 response snapshot，因此模板后续改名、
  启停或发布 v3 后仍可安全解释原操作，而不会伪造“历史响应原样重建”。

### 4.8 AuditLog 扩展与历史保真

新增动作和对象映射：

- `prompt_template.created -> prompt_template`
- `prompt_template.updated -> prompt_template`
- `prompt_template.version_published -> prompt_template_version`

摘要白名单只允许 status、revision、version number、project scope/count、recommended model count、
definition size、variable count 和 `change_summary_hmac`。模板名称只使用 AuditLog 既有 `object_name`，
正文、变量 schema/值、项目/模型完整清单和密文均禁止进入摘要。

0059 的权威基线是 **0054 迁移完成并继续到 0058 的真实终态**，不能退回仅 PRD-058 定义。必须显式复制
`audit_logs` 的 rowid、全部 17 列与 NULL 语义，保留包含 PRD-060 Invitation、PRD-062 Config Transfer
在内的完整 action/object 枚举，并执行双向 EXCEPT、count、sequence、FK 与 integrity guard。

必须逐名/逐 SQL 语义恢复：audit_logs 上 11 个显式索引（包含 invitation/config transfer request unique）、
6 个本地 trigger，以及引用 AuditLog 的 4 个外部 owner trigger：
`trg_audit_exports_owner_insert_prd058`、`trg_audit_log_export_sequences_owner_insert_prd058`、
`trg_invitation_events_owner_insert_prd060`、`trg_role_permission_events_owner_insert_prd059`。
新增模板 request 唯一索引使用 `(tenant_id,request_id,action)` 条件唯一，使 v1 同 request 的 created 与
version_published 两条审计不冲突。任何保真差异必须在删除旧表前原子失败。

恢复并扩展后的 Audit owner/conflict trigger 必须逐 action 复核同 tenant PromptTemplate/TemplateVersion
真实存在、actor 属于同 tenant、changed fields 与摘要白名单精确匹配；不能只放宽 CHECK 枚举后让任意
内部调用伪造模板对象或把密文/正文塞入通用摘要。

## 5. API 设计

所有公开接口先执行 Session 与 permission 依赖，再读取 query/path/body；严格 JSON 使用有界读取、
UTF-8、任意层重复键拒绝和 exact-set 字段。响应均带 `request_id/trace_id` 且 `no-store`。

### 5.1 `POST /prompt-templates`（API-301）

- 权限：`prompt_template.write`。
- 请求：name、description、content、variables、project_scope/project_ids、recommended_model_alias_ids、
  change_summary。
- 行为：单事务创建模板和 v1、校验/加密定义、封印版本、设置 current version、写 AuditLog/source event。
- 响应：首次 201、幂等重放 200；包含当前模板投影、同一 v1 元数据、`event_id`、`replayed`、
  request/trace，不回显密文。
- 幂等：前端为同一 canonical intent 复用稳定 `X-Request-ID`；v1 的 tenant-qualified request ID 与
  HMAC fingerprint 相同则返回同一 version 事实加“当前权威模板投影”，`replayed=true`，不新增审计/事件；
  不同意图复用同 ID 返回 `prompt_template.idempotency_conflict`。不声称重建历史时点的完整响应。

### 5.2 `GET /prompt-templates`（API-302）

- 权限：`prompt_template.read`。
- query：status、search、limit、offset；固定上限 100，未知或重复 query 拒绝。
- 响应：稳定排序的安全列表，不返回 Prompt 正文或完整变量定义。

### 5.2.1 `GET /prompt-templates/options`

- 权限：`prompt_template.read` 或 `prompt_template.write` 任一实时权限；不能要求调用者额外拥有
  `project.read` 或 `routing.read`，也不能借本接口获得对应领域的管理权限。
- 响应：模板表单专用的 tenant-scoped 最小候选目录：
  `project_id/name/status` 与 `model_alias_id/alias/display_name/environment_id/status`。
- 只返回 active 资源和表单绑定所需字段，不返回 Project 应用、Virtual Key、Provider、路由配置、价格、
  Endpoint 或其他管理元数据。该路由必须在 `/{template_id}` 动态路由之前注册。

### 5.3 `GET /prompt-templates/{template_id}` 与版本详情（API-302）

- 权限：`prompt_template.read`。
- 模板详情返回元数据、绑定安全投影和版本历史。
- `GET /prompt-templates/{template_id}/versions/{version_id}` 返回授权用户可见的解密定义；
  解密失败统一 `prompt_template.definition_unavailable`，不返回密文或密码学细节。
- 跨租户与不存在 ID 同形 404。

### 5.4 `PATCH /prompt-templates/{template_id}`（API-303）

- 权限：`prompt_template.write`。
- 请求：`expected_revision`，以及 name/description/status 中至少一个字段。
- 行为：`BEGIN IMMEDIATE` + revision CAS；no-op 不增 revision、不写审计；禁用后新调用立即失败。
- 响应：权威模板、changed、request/trace。
- 网络重试：成功写入同时保存不可变 Operation；同 request ID 和同规范意图返回记录的 operation 事实与
  当前权威模板投影，不同意图返回幂等冲突。重放永不再次执行 PATCH，也不覆盖后续更新。

### 5.5 `POST /prompt-templates/{template_id}/versions`（API-304）

- 权限：`prompt_template.write`。
- 请求：`expected_revision` 加完整新定义和绑定；不支持局部修改历史版本。
- 行为：串行计算下一版本，building 装配、一次封印、更新 current version/revision，写 AuditLog/source event。
- stale revision 或并发发布只有一个成功；同 request ID/HMAC fingerprint 的网络重试返回同一 version
  事实加当前权威 template、`replayed=true`，不新增审计/事件；不同意图复用 ID 返回幂等冲突。

### 5.6 企业 Chat 合同扩展

`GET /chat/options` 在调用者具有 `prompt_template.use` 时新增 `prompt_templates` exact schema：

- `prompt_template_id`、`prompt_template_version_id`、`version_number`、`name`；
- `variables[{name,label,required,max_chars}]`；
- `project_scope`、`applicable_project_ids`；
- `recommended_model_alias_ids`，保持发布时顺序。

该投影不含正文、描述、历史和完整资源目录。没有 use 时返回空数组，不能因此授予模板 read。

`POST /chat/sessions/{chat_session_id}/messages` 可选新增：

```json
{
  "prompt_template_version_id": "ptv_...",
  "template_variables": {"customer_name": "..."}
}
```

- 两字段必须同时出现或同时省略；不选模板时必须完全省略，保持 PRD-066 合同和行为兼容。
- 客户端不提交 template ID；Control 只能从 tenant-qualified version 反查模板/current head，减少身份歧义。
- `SendChatMessageRequest.history` 改为 0..20 项并执行条件约束：普通模式仍必须非空、奇数且以 user 结束；
  模板模式必须为空或为完整的 user/assistant 对（偶数）。Control/Gateway 追加 rendered user 后重新核对
  最大 20 条和 64 KiB 总量，不能用空 history 放宽普通 Chat。
- 携带时 Control 在原 message/grant 事务实时复核 `chat.use + prompt_template.use`、tenant、active/current、
  项目适用性和变量 exact-set，并写 invocation。
- 内部 Gateway wire 只增加变量 map，不信任浏览器提交 tenant、模板正文或 system message；版本身份从
  grant 对应 invocation 恢复并交叉核对。
- Gateway 消费 grant 时再次复核权限、登录 Session、模板 active/current、project scope、版本和资源 owner，
  解密并渲染最终 user message，随后调用既有 `_handle_gateway_request`，让现有 DLP 对其执行与普通 Chat
  相同的 user-message 扫描。
- 内部 wire 只允许两个互斥 exact-set：普通 `{grant,history}` 与模板 `{grant,history,variables}`。
  普通 history 保持“奇数且以 user 结束”；模板 history 必须为空或为完整 user/assistant 对（偶数），
  Gateway 渲染后追加 final user，再执行统一条数/UTF-8 总字节上限。
- Gateway 事务线性化顺序冻结为：`BEGIN IMMEDIATE -> grant/invocation/message/Session/RBAC/template
  current/project 全图复核 -> 解密/validate/render -> grant consumed CAS -> COMMIT -> _handle_gateway_request`。
  consumed commit 是“禁用后允许已在途调用完成”的唯一线性化点。
- Control 准备阶段可向浏览器返回冻结 `prompt_template.*`；Gateway 竞态 owner/撤权/禁用统一
  `chat.execution_invalid`，数据库或密钥缺配置为 `chat.gateway_unavailable`，已认证密文/schema 损坏为
  `chat.response_invalid`，不扩展内部任意模板错误透传白名单。Invocation.failure_code 与最终 Chat error 一致。
- 模板成功响应额外返回当前调用栈中的 `rendered_user`，供浏览器维护下一轮内存 history；该字段 no-store、
  `repr=False` 且不持久化。模板模式 UI 不提交自由 draft，用户输入只来自声明变量；取消模板后恢复普通输入。
  `prompt_template.use` 明确授权当前调用者接收本次 rendered user；它不授予 PAGE-094、原始版本历史或
  定义 GET。若租户不希望某角色看到渲染结果，必须从后端矩阵撤销 use，前端隐藏不是安全边界。
- 禁用事务与调用准备都使用 SQLite write lock 串行；禁用提交后的新调用必定失败。已经成功消费 grant 并
  进入 Provider 的调用允许完成，其终态仍可追踪，不能把网络中的请求追回描述成“未调用”。

## 6. 前端页面和交互设计

### 6.1 PAGE-094

- 导航由后端 `prompt_template.read` 决定，入口名“Prompt 模板”。无权限时无导航、无受保护 DOM、无预取。
- 桌面使用紧凑列表加右侧详情工作区；移动端使用列表/详情切换，不嵌套装饰卡片。
- 顶部提供搜索、状态分段控件和刷新；具有 write 时显示创建按钮，没有 write 时整个页面只读。
- 列表展示名称、状态、当前版本、适用项目数、推荐模型数、更新时间；正文不进入列表 DOM。
- 详情以标签页切换“当前版本”和“版本历史”；历史版本只读，不显示删除或编辑命令。
- 创建与“发布新版本”使用同一受限表单：正文、变量行、项目多选、推荐模型有序选择、change summary。
- 元数据修改和启停使用 `metadata_revision` owner，前端变量名不得简写为 `version`；409 保留本地草稿并
  要求刷新。版本发布另用 `version_number`，两者不能混用。
- 身份、tenant、authorization fingerprint、page generation、request generation 任一变化时 abort 请求并
  清空解密正文、变量草稿、项目/模型选项和迟到响应。
- `renderPromptTemplates` 永不把 Prompt 正文或变量草稿写入 HTML 字符串、attribute 或 dataset；DOM
  就位和每次局部重绘后，只通过 `.value`/`textContent` hydration。禁止正文进入模板字符串、URL、
  Storage、错误对象或埋点。
- hash/导航/注销/401/403/pagehide/tenant/Session/fingerprint 漂移时，先同步对敏感容器执行
  `replaceChildren()` 并清空闭包正文，再 abort/await 网络任务；迟到响应不能重新 hydration。
- PAGE-094 激活期间关闭 15 秒全壳 DOM 替换，只保留不会重绘页面的授权指纹检查和页面内显式刷新；
  创建/发布表单 open、dirty、busy、conflict 或已加载解密正文时 `blocksShellDomReplacement()` 必须为真。
- 409 冲突保留 authoritative base、本地 draft 和 base `metadata_revision`；使用独立 request ID 的精确 GET
  侧载最新值，由用户显式采纳或放弃，不能以全页刷新覆盖草稿。

PAGE-094 owner 冻结为：tenant/user、登录 Session ID/revision、authorization fingerprint、permission
revision/hash、Token 闭包、refresh generation、后端导航 `state=ok`、active page ID，以及精确
`id=PAGE-094/target_section=prompt-templates/visible/enabled`。read owner 使用
`PROMPT_TEMPLATE_READ`；所有写 ticket 另建 `PROMPT_TEMPLATE_WRITE` owner，不能用 read owner 代替。
list、detail、version definition、mutation 和 conflict GET 使用互不取消的 controller/generation lane。

### 6.2 PAGE-093 集成

- 具有 `prompt_template.use` 时显示模板选择；选择后按当前项目过滤 applicable 模板，并渲染变量输入。
- 推荐模型只增加标记和排序提示，永远不修改现有 `selectedModelAliasId`；非推荐模型始终允许调用，
  后端也不得据此返回不适用错误。
- template/version/revision 与 ChatSession owner 冻结；模板变化、降权、清空、注销或新建会话时清除变量值。
- 变量值不得放入 localStorage/sessionStorage、URL、analytics、Console event 或错误详情。
- 达到 PRD-066 history/wire 上限、模板变量无效、发送中或身份变化时发送按钮保持稳定禁用状态。
- Chat 返回 403 时按“授权状态漂移”处理：先清空模板敏感状态并刷新 `/auth/me` 与后端导航，再依据最新
  permission 决定保留普通 Chat 还是回落概览，不能一律断言 `chat.use` 已撤销。

PAGE-093 原 owner 仍只冻结 `chat.use`；仅选中模板后的 send ticket 额外冻结实时
`prompt_template.use`、template/version ID 和 options revision。模板错误状态机固定为：

- `auth.permission_denied`：同步销毁全部正文/变量，刷新认证与导航；若仍有 `chat.use`，以空会话恢复普通 Chat。
- `prompt_template.disabled/version_conflict/not_applicable/not_found`：清空变量，标记 options stale 并重读；
  发送保持禁用，用户必须显式重选模板或显式选择“无模板”，绝不能自动把原草稿降级为无模板发送。
- `prompt_template.variables_invalid`：保留当前模板和可修正变量输入，不进入 uncertain。
- 模板发送阶段任何 5xx 或网络结果未知：沿用 PRD-066 的 `uncertain`，禁止再次发送，必须显式清空/新建会话；
  浏览器不能用错误码猜测一次性 grant 是否已经被 Gateway 消费。
- 不带模板的发送继续只要求 `chat.use`，模板目录故障不能阻断普通 Chat。

### 6.3 可访问性与响应式

- 仓库当前没有图标依赖或 bundler；本条复用现有 `icon-button` 与已使用的 `×/↻/←/→` 熟悉符号，
  每个按钮提供 title/aria-label，不临时引入 CDN、手绘 SVG 或新的构建链。状态切换使用 toggle，
  状态筛选使用 segmented control。
- 所有输入具有 label、错误关联和键盘焦点；变量行删除使用垃圾桶图标而非文字胶囊按钮。
- 1280x720 与 390x844 下无横向溢出、重叠、动态内容推挤或长 ID 撑破容器。

## 7. 错误码设计

| 错误码 | HTTP | 场景 |
| --- | ---: | --- |
| `auth.token_missing/auth.token_invalid` | 401 | 匿名或失效 Session |
| `auth.permission_denied` | 403 | 缺 read/write/use/chat.use，且在正文解析前失败 |
| `prompt_template.not_found` | 404 | 不存在或跨租户模板/版本，同形防枚举 |
| `prompt_template.request_invalid` | 422 | wire、JSON、字段、字符、分页或 exact-set 非法 |
| `prompt_template.definition_invalid` | 422 | 占位符、变量 schema、大小、绑定或资源状态非法 |
| `prompt_template.binding_not_found` | 404 | body 中 Project/Model 不存在或属于另一 tenant，同形防枚举 |
| `prompt_template.variables_invalid` | 422 | Chat 变量缺失、额外、类型或长度非法；不回显变量值 |
| `prompt_template.name_conflict` | 409 | tenant 内名称冲突 |
| `prompt_template.revision_conflict` | 409 | expected revision 过期或并发修改 |
| `prompt_template.idempotency_conflict` | 409 | 相同 request ID 被用于不同创建/发布意图 |
| `prompt_template.disabled` | 409 | 新调用命中 disabled 模板 |
| `prompt_template.version_conflict` | 409 | 调用/发布版本不是当前版本 |
| `prompt_template.not_applicable` | 409 | 当前 Chat Project 不在 selected scope |
| `prompt_template.definition_unavailable` | 500 | 密文损坏、schema 不一致或无法解密 |
| `prompt_template.integrity_failed` | 500 | head、计数、版本连续性、owner 或不可变关系损坏 |
| `prompt_template.unavailable` | 503 | 数据库/加密配置/依赖暂时不可用 |

Control 在调用 Gateway 前的模板准备失败可向浏览器返回本节固定的 `prompt_template.*`；一旦进入 Gateway，
内部接口只返回固定 `chat.*`：晚到 owner/撤权/禁用/版本或适用性变化映射 `chat.execution_invalid`，密钥或
数据库不可用映射 `chat.gateway_unavailable`，已认证密文/schema 损坏映射 `chat.response_invalid`。
Control 继续只透传固定 `chat.*`；DLP、Policy、预算、Provider、`gateway.*` 及其他非 `chat.*` 内部错误均按
PRD-066 折叠为 `chat.gateway_unavailable`，不得新增模板错误透传白名单。所有错误正文只返回目录化信息和
request/trace ID，不返回 SQL、路径、repr、Prompt、变量名/值、密文、绑定目标或另一 tenant 的存在性。

资源错误优先级冻结为：template/version 不存在或跨 tenant 使用 `prompt_template.not_found`；创建/发布 body
引用的 Project/Model 不存在或跨 tenant 使用 `prompt_template.binding_not_found`；同 tenant 资源存在但
inactive/环境不合法属于 `definition_invalid`；Chat 当前 Project 不在版本 selected scope 才是
`not_applicable`。密钥未配置是 503，密文格式/AAD/tag/schema 损坏是 500，二者不得互换。

## 8. 安全、租户、并发与隐私约束

1. tenant、actor、role、permission、Template/Version/Project/Model/Chat owner 均从 Session、grant 和
   tenant-qualified DB 关系恢复；请求不接受 tenant_id、actor_id、system message 或模板正文覆盖。
2. 所有模板路由先鉴权再解析 body；Chat 无模板主流程只要求 chat.use，模板分支必须 Control/Gateway
   双重实时复核 prompt_template.use。
3. 定义 canonical JSON 使用独立 Prompt cipher 与 owner AAD 加密落库；明文只在授权 GET 或一次调用栈中
   存在，并在引用离开后释放，禁止调用 AAD=None 的通用 secret helper。
4. 模板正文和变量值不进入 Pydantic repr、exception chain、logger 参数、AuditLog、Event、Webhook、
   RequestLog、测试快照、Browser 截图或前端持久存储。Browser 只截列表、元数据和已关闭正文编辑器的
   状态；正文行为通过不向会话输出值的 DOM 属性断言验收。
5. list/options 响应最小化；仅 use 用户看不到原始 definition、未调用版本正文、描述、历史和完整资源目录。
   成功调用后 no-store 响应中的本次 `rendered_user` 是唯一例外，它由 `prompt_template.use` 显式授权，
   只在当前浏览器内存 history 中存活，不授予模板管理读取能力。
6. 创建、发布、PATCH、禁用和 Chat 调用准备使用 `BEGIN IMMEDIATE`；版本 CAS、模板状态与 invocation
   创建具有确定串行语义，失败零部分写入。
7. source event 与 AuditLog 和业务变更同事务；网络投递失败不回滚已提交版本，但 delivery 状态可追踪。
8. Gateway 把模板渲染结果追加为 trusted final user message，使现有只扫描 user role 的 DLP 与普通 Chat
   使用同一执行路径；随后继续 Policy/预算/路由/用量/成本治理，不提供跳过治理或直选 Provider 的参数。
9. 应用日志只允许安全 ID、version number、status、计数和固定 error code；动态 canary 扫描必须为 0。
10. 所有连接在成功、权限拒绝、校验失败、取消和异常路径 finally-close；Windows 专项验证数据库可重命名/删除。

## 9. 数据库迁移与降级设计

### 9.1 Up

1. 预检迁移版本、0058 依赖对象、RBAC revision 3、0054@0058 Audit/AuditExport/Invitation/Config Transfer
   终态和未知 staging 对象。
2. 把 Permission catalog 提升到 revision 4，加入三项模板权限；只为 preflight 冻结的 canonical 角色
   自动扩权并更新 matrix hash/revision，自定义矩阵不擅自补权。
3. 保真重建 AuditLog 枚举及全部索引/触发器/子表入向关系。
4. 创建 Template、Version、Project/Model binding、Invocation、Operation 和 source event 表及
   owner/immutable trigger。
5. 为现有和新 tenant 建立 revision 4 权限投影；更新新 tenant 初始化 trigger。
6. 终态 guard 校验 catalog 数量、角色 hash、对象数量、FK、integrity、Audit sequence 和 trigger 集合。

### 9.2 Down

- 在任何 DDL 前拒绝：存在模板、版本、invocation、operation、source event、模板 AuditLog/Webhook delivery、
  自定义模板权限授予/撤销、未知依赖或 FK/integrity 异常。
- canonical 空状态才允许删除业务表、收缩三项权限、恢复 revision 3 matrix/hash、保真恢复旧 AuditLog 枚举。
- 删除仅涉及本迁移对象；已有 PRD-066 Chat、PRD-059 RBAC、PRD-058 AuditExport 和历史证据必须逐列保真。
- fresh 0001->0059、0058->0059、0059 down/up 和有业务事实时 down 原子拒绝都必须有专项测试。

## 10. 测试用例

### 10.1 TC-0133 主流程/集成

1. Admin/Developer 可创建 v1；定义密文落库，API 解密内容正确，数据库和日志无明文 canary。
2. 创建含 0/多变量、all/selected projects、0/多推荐模型的模板，列表/详情/版本顺序稳定。
3. 发布 v2 后 v1 全列和绑定不变，current version/revision 单调，历史可追溯且不可修改/删除。
4. PATCH name/description/status 使用 CAS；no-op 零 revision/Audit/Event，禁用后新调用拒绝，启用恢复。
5. 初始发布产生 created + version_published 两条 AuditLog，后续发布产生一条 version_published；每次发布
   各有一条绑定 publication AuditLog 的 source event 和签名 Webhook，payload 不含敏感字段。
6. PAGE-094 完成创建、查看、发布、搜索、筛选、启停、409 草稿恢复和版本历史。
7. PAGE-093 选择模板、填写变量并发送；Gateway 把内存渲染的 final user message 交给既有 DLP/治理链，
   Provider 调用成功，响应的 rendered_user 只用于当前浏览器内存 history。
8. RequestLog、Usage、Cost、Budget、Policy、DLP、Fallback 仍可按同一 request_id 追踪；Invocation 精确记录 v1/v2。
9. 不带模板的 PRD-066 Chat 主流程和 HTTP wire 完全兼容。

### 10.2 TC-0134 异常/权限/安全

1. 匿名、缺 read/write/use 用户分别得到 401/403；畸形/超限正文不能改变先鉴权优先级。
2. 跨租户 template/version/project/model/chat ID 与真实不存在同形 404/409，另一 tenant 无读写痕迹。
3. stale revision、并发版本发布、重名、非连续版本、错误 current head 均失败且零部分写入。
4. `${}` 非法语法、裸 `$`、未知/重复/缺失变量、额外变量、超长值、非字符串值和渲染超限稳定 422。
5. Jinja 条件/循环、属性访问、函数、嵌套对象、客户端 system role 和 tenant/Provider/Key 注入全部拒绝；
   渲染 user message 的 DLP `allow/log_only/block` 行为与普通 Chat 一致。
6. selected project 不匹配、版本非 current、模板 disabled、模板/Project/Model 在 Control 与 Gateway 间失效时失败关闭。
7. 密文损坏、加密密钥缺失、source event/Audit 失败、数据库 busy/断连均使用统一码；事务和连接无泄漏。
8. Webhook 网络失败不回滚 published 版本，delivery 可追踪且只保存固定错误；重放不复制 Prompt。
9. 动态 canary 扫描 API error、Audit、Event、Webhook、RequestLog、普通日志、Console、Browser 证据命中 0。
10. Viewer 只能 use：无 PAGE-094/定义/历史预取，但可在 Chat 获取最小模板投影并成功调用；失权后立即清理。

### 10.3 迁移与数据库

1. fresh、0058 历史、up/down/re-up、checksum/head/count 连续。
2. AuditLog rowid/NULL/全列双向 EXCEPT、AuditExport sequence/FK/trigger 逐项保真。
3. 版本 header、绑定和 published source event 的 UPDATE/DELETE/REPLACE 均被 DB 阻断。
4. 同形跨 tenant 复合 FK、current head、project/model binding、invocation owner 直接 SQL 负测。
5. 交换跨 tenant/跨 template/跨 version 密文以及篡改 version number，AAD tag 均失败且零 Provider 调用。
6. 有模板、invocation、审计、Webhook、自定义矩阵或未知后续依赖时 down 在首项 DDL 前原子拒绝。
7. 每条成功/异常测试后 `foreign_key_check=0`、`integrity_check=ok`，数据库句柄可释放。

### 10.4 Console 与 Browser

1. Admin、Developer、Viewer 和无权限角色的导航、DOM、预取、按钮与后端结果一致。
2. 1280x720 与 390x844 完成 PAGE-094 创建/发布/启停/历史和 PAGE-093 模板调用。
3. 身份/tenant/fingerprint/page/request generation 变化会 abort；迟到响应不能恢复敏感 DOM 或变量值。
4. 401/403/404/409/422/500/503、空态、加载态和依赖恢复均有确定页面状态。
5. 最长中文名称、变量 label、版本/请求 ID 不重叠、不横向溢出；键盘、焦点、aria/tooltip 完整。
6. Console error、pageerror、blocked request、非 localhost 外部请求和敏感 canary 均为 0。
7. renderer HTML、attribute、dataset、URL、Storage、console 和发布截图均不含正文 canary；正文只由
   hydration 写入，销毁边界先同步清 DOM，再处理 abort/await。
8. list/detail/definition/mutation/conflict 五个异步 lane 互不误取消；403 后普通 Chat 可恢复，模板 stale
   不会降级为无模板发送，非推荐模型可正常调用。

### 10.5 回归与运行验收

- 只执行 PRD-067 专项和直接受影响回归：RBAC、Audit、Webhook、Enterprise Chat、Gateway 治理、
  Console 导航/PAGE-093/PAGE-094；局部失败只定向复测，不反复从头运行全量。
- 专项全绿后执行一次必要受影响组合与仓库规定的最终回归；中文注释门禁必须通过。
- TEMP、TMP、pytest basetemp、运行证据和 Browser 截图全部限定在 `D:/MetaHub/.tmp` 或仓库 `.artifacts`，
  不提权、不使用 `C:/tmp`，不请求沙箱外动作。
- 开发库先在线备份再正式迁移 0059；真实 Gateway/Control/Console、真实受治理 Chat、双视口 Browser、
  权限撤销/恢复、注销和停服静止终态全部验收。

## 11. 中文注释要求

- 新增模块、类、DTO、常量、重要字段和每个函数均写详细中文 docstring 或函数级注释。
- 权限、tenant owner、加密/解密、模板解析、封印、CAS、事务、错误转换、竞态、回退和隐私过滤必须解释
  “为什么”和业务约束，不能只翻译代码。
- 修改后执行推荐 Python：
  `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe scripts/ci/check-comments.py`。
- 当前注释基线已经通过；PRD-067 不得用 ignore 或空泛注释绕过门禁。

## 12. 代码修改计划

1. 完成本文件两轮设计自审并冻结权限、schema、API、Chat wire 和隐私合同。
2. 先实现共享模板 parser/renderer 与纯函数测试，证明语法、保留键、大小和无代码执行边界。
3. 实现 0059 up/down 和迁移专项，先关闭 RBAC/AuditLog/PRD-058 保真风险。
4. 实现共享 RBAC revision 4、独立 Prompt cipher/HMAC、错误目录和 Webhook 事件合同。
5. 实现 Control PromptTemplate models/routes/service、严格 wire、加密、不可变版本、CAS、Audit/Event。
6. 扩展企业 Chat Control/Gateway 双重复核、内存渲染和 invocation 终态。
7. 实现 Console API、PAGE-094 和 PAGE-093 模板选择/变量表单/owner 清理。
8. 补齐主流程、异常、权限、跨租户、并发、隐私、迁移、Runtime 和 Browser 测试。
9. 更新 `docs/api`、`docs/testing/PRD-067-tests.md`、README 和目录说明。
10. 执行专项、一次受影响回归、中文注释、开发库迁移、真实三服务与双视口验收；全部通过后
    才能把 PRD-067 标记完成并从 Excel 识别下一项。

## 13. 设计自审第一轮

状态：**已完成并修订通过**。范围：需求边界、三权限、tenant 关系、不可变版本、敏感 Prompt 和
Chat 竞态。

### 13.1 发现与修正

1. **权限：只给 Admin 写权限与 Excel 的“业务管理员/开发者”主要用户不一致。** 已冻结 Admin、
   Developer 的 read/write/use；Viewer 只继承最小 use，其他职责角色默认无权。实际授权仍逐请求读库。
2. **隔离：单列模板、版本、Project、Model 和 Chat FK 不能证明同 tenant。** 已要求全部关系以
   `(tenant_id,id)` 为候选键/复合 FK，查询也必须 tenant-first；跨租户和不存在同形失败。
3. **不可变：直接插入 published 版本无法在数据库层阻断晚到绑定。** 已复用 PRD-045 的事务内
   building 装配与一次 seal；building 不对 API 可见，published header/绑定均不可改删。
4. **敏感：把正文或变量 schema 存 JSON 明文会扩大数据库和错误采集暴露面。** 已改为独立 Prompt
   AES-GCM cipher，以 tenant/template/version/number AAD 绑定 canonical definition；事件、审计、列表、
   调用事实均只保存安全计数或 keyed HMAC。
5. **语法：手写双花括号替换容易接受表达式或产生歧义。** 已冻结标准库 `string.Template` 解析，
   只放行 `${lower_snake_case}`，拒绝裸 `$`、简写、未知/多余变量和 JS 原型保留键。
6. **竞态：只在 Control 校验模板会让撤权/禁用与 grant 消费之间失效。** 已要求 Control 准备事务和
   Gateway 消费事务双重复核 permission、tenant、active/current、project 和登录 owner；两端都失败关闭。
7. **语义：把推荐模型当白名单会改变路由和成本，超出“推荐模型”范围。** 已明确推荐只用于 UI 建议，
   适用项目才是模板硬约束，Gateway 模型选择仍按 ChatSession 与既有治理链。
8. **兼容：给 `/chat/options` 整体增加双权限会阻断无模板 Viewer Chat。** 已冻结 options 仍只要求
   `chat.use`，缺 `prompt_template.use` 时模板投影为空；无模板 send wire 保持兼容。
9. **前端隐私：现有 Shell 使用 innerHTML，周期重绘可能复制正文或脏变量。** 已要求正文只用 hydration，
   有解密内容/脏草稿时暂停全壳重绘，所有身份/离页/注销路径 abort 并清空。
10. **重试：创建或发布响应丢失时仅靠 CAS 可能产生重名或无法确认。** 已在版本事实冻结 tenant-qualified
    request ID 与 keyed HMAC fingerprint；同意图重放返回同一 version 事实和当前模板投影，不同意图 409。
11. **审计：现有 AuditLog CHECK 不认识模板动作。** 已把 0054@0058 的 17 列/rowid/NULL、11 索引、
    6 本地 trigger、4 外部 owner trigger 和摘要白名单列为 0059 阻断门禁。

第一轮结论：Excel 范围、权限最小化、tenant 复合关系、版本封印、静态加密、内存渲染、双重实时复核、
推荐模型语义、无模板 Chat 兼容、前端隐私、幂等和审计保真均已有可实现合同。可以进入第二轮迁移、
API/Gateway 兼容和测试范围自审；编码门禁仍关闭。

## 14. 设计自审第二轮

状态：**已完成并修订通过**。范围：迁移/down guard、AuditLog 保真、CAS/幂等、Gateway 兼容、
前端 owner 和测试范围。

### 14.1 发现与修正

1. **迁移：只扩 Audit CHECK 会丢失 owner 与摘要防注入。** 已要求 17 列/rowid/NULL/sequence/FK 保真，
   并扩展 owner/conflict trigger 对模板对象、actor、action 和字段白名单的同租户精确复核。
2. **Down：直接删除三权限会破坏自定义矩阵和历史事实。** 已冻结全业务事实、模板 Audit/Webhook、
   自定义授权和未知依赖的前置 guard；只有 canonical 空状态可收缩 revision 4 -> 3。
3. **身份：浏览器同时提交 template ID/version ID 会制造双 owner 歧义。** 已收敛为可选
   `prompt_template_version_id + template_variables` exact pair，模板和 current head只能由后端反查。
4. **候选目录：PAGE-094 若调用 `/projects`、`/model-aliases` 会误要求广域权限。** 已增加模板域最小
   `/prompt-templates/options`，只投影绑定需要的 active ID/名称/环境，不授予 Project/路由管理权限。
5. **幂等：初次创建、版本发布和 PATCH 响应丢失时不能盲目重试。** 已增加 tenant 内 request ID 唯一的
   不可变 Operation、keyed HMAC intent、同一 operation/version + 当前 template 重放；不同意图冲突，
   重放不覆盖后续写入也不新增审计/事件。
6. **Gateway：现有 Control 只透传 `chat.*`，模板域错误不能任意穿透。** 已把晚到 owner/撤权/禁用/版本
   变化固定映射为 `chat.execution_invalid`，密钥/数据库问题映射 `chat.gateway_unavailable`，已认证定义损坏
   映射 `chat.response_invalid`；其余 DLP/Policy/预算/Provider 等非 `chat.*` 错误继续按 PRD-066 折叠，
   不建立模板错误白名单，且 5xx 在页面统一进入 uncertain，禁止二次计费重放。
7. **状态机：模板禁用、版本 stale、变量 422、权限撤销和 5xx 没有唯一 UI 语义。** 已冻结清理、
   options stale、可修正输入保留、普通 Chat 恢复和 uncertain/清会话路径，禁止静默无模板发送。
8. **Owner：单个 page generation 不能覆盖读、详情、解密和写操作。** 已冻结 Session/revision、permission
   revision/hash、导航 id/target、Token 和 refresh generation，并拆分五个 controller/generation lane。
9. **DOM：详情和历史会显示解密正文，单纯“列表不显示”不足。** 已要求 renderer 永不接收正文字符串，
   每次渲染后 hydration；所有身份/离页边界先同步 `replaceChildren`，Browser 证据发布前脱敏。
10. **刷新：15 秒全壳替换会丢正文、焦点和草稿。** 已在 PAGE-094 激活期关闭全壳替换，只保留授权
    指纹轮询和显式局部刷新，并在 open/dirty/busy/conflict/definition 状态强制阻断 DOM replacement。
11. **推荐语义：即使称“默认建议”仍可能自动改模型。** 已明确只显示标记/排序提示，永不改
    `selectedModelAliasId`，后端不以推荐列表拒绝调用。
12. **工具链：仓库没有图标库。** 已冻结复用既有 icon-button 与熟悉字符，不引入 CDN、手绘 SVG 或
    无法由当前静态 ESM 服务加载的新依赖。
13. **测试：反复全量会放大局部修复成本。** 已限定先跑 PRD-067 专项和直接受影响组合，局部失败定向
    复测；专项全绿后只执行一次必要最终回归、迁移、真实 Runtime 和双视口 Browser。

第二轮结论：迁移可逆边界、Audit 保真、写入幂等、Chat/Gateway wire 与错误兼容、候选目录最小权限、
前端 owner/状态机/敏感 DOM 和测试范围均已有确定合同，未发现剩余编码前阻断项。

### 14.2 后端交叉审查补充修订

状态：**已完成修订并通过终审**。

1. **P0：system role 绕过现有 DLP。** 仓库 DLP 只扫描 user 消息；已把模板 v1 冻结为本轮 final user
   message。模板 history 为空/偶数，Gateway 追加渲染 user 后再走相同 DLP/治理链；成功响应仅在 no-store
   当前栈返回 rendered_user 供浏览器内存 history，永不持久化。
2. **P0：通用 AES-GCM 无 AAD 且密钥域错误。** 已禁止复用通用 helper，新增独立显式配置、版本化密文、
   分域 KDF/HMAC，以及绑定 schema/tenant/template/version/number/key-version 的 AAD；四类密文交换列为负测。
3. **P0：泛化 tenant FK 可在同 tenant 拼接错误事实。** 已写死 version 三/四列候选键、包含固定
   `current_version_state='published'` 的 head 延迟四列 FK、binding 三列 FK，以及 Session/Message
   新候选键和 Invocation 六列 owner 关系/trigger；building head 无法通过 COMMIT。
4. **P1：Audit 基线过旧。** 已改为 0054@0058 终态，保真完整枚举、11 索引、6 本地 trigger、4 外部
   owner trigger；模板分支只能在最新 owner/conflict 语义上增量扩展。
5. **P1：发布事件与审计可漂移。** 已冻结 v1 两条 Audit、后续发布一条 Audit，source event 通过
   publication_audit_log_id 复合 FK 和 trigger 精确绑定 action/object/actor/request/trace/time。
6. **P1：Gateway 线性化与错误映射不确定。** 已冻结全图复核、render、grant CAS、commit、Provider 的
   顺序；commit 为允许在途完成的线性化点，Gateway 只返回固定 chat.*，Invocation 同步终结。
7. **P1：RBAC guard 不可执行。** 已写入 revision 3/4 的 51/54、134/141 和三 canonical 角色前后 hash；
   Finance/Security/Ops 不变，自定义矩阵不补权。
8. **P1：幂等结果无法重建历史响应。** 已增加不可变 Operation，并改为同一 operation/version 事实 +
   当前模板投影 + replayed，不声称返回历史时点 DTO；HMAC canonical 覆盖操作、正文/元数据、排序项目、
   有序模型、change summary 和 CAS。
9. **错误：跨租户绑定、inactive 资源和密文失败语义冲突。** 已分别冻结 404 binding_not_found、
   422 definition_invalid、409 not_applicable、500 definition_unavailable 和 503 unavailable。

补充修订完成后由独立后端审查再次逐项核对；终审输入快照 SHA256
`AA24625A19811BEFFFFFA48EF30B123888E1A93060CB6C15D2FF8E2BE743AA36` 对应的终审结论为无剩余编码前阻断项。

## 15. 实现与验收结论

PRD-067 已按本设计完成并冻结以下交付边界：

1. 0059 已应用到开发库，RBAC catalog 为 revision 4；PromptTemplate、不可变 Version、Project/Model
   binding、Invocation、Operation、Audit 和 source event 均具备 tenant-qualified owner 与数据库门禁。
2. Control API 对 read/write/use 逐请求执行后端权限；模板定义采用独立 owner-bound AES-GCM，所有接口
   使用统一错误码、`request_id`、`trace_id` 和 `Cache-Control: no-store`。
3. PAGE-094 已实现创建、读取定义、版本发布、启停和历史；PAGE-093 已实现模板选择、变量输入和经
   Control/Gateway 双重复核后的完整治理调用，Prompt、变量和 Response 不进入普通日志或业务表。
4. 自动化最终记录包含 Python 受影响组合 `73 passed`、Console 直接相关 `38 passed`、移动布局修复
   定向 `10 passed` 和 lint `188 files`；中文注释门禁最终为 `comment coverage ok`。
5. Runtime 权威证据为
   `D:/MetaHub/.artifacts/prd067-runtime/run-20260802T110138Z-c00b97cac3/runtime-evidence.json`，
   SHA-256 为 `AFE1FDF4710AB35DB7E82F0C967E94E482E2A4FC21B3B4270038C3BFE4F1D49A`。
6. 应用内 Browser 已在 `1280x720/390x844` 验证 PAGE-094 与 PAGE-093；三张最终截图和 SHA-256
   记录在 `docs/testing/PRD-067-tests.md`。移动端刷新按钮 `40x40`、新建按钮 `286x38` 同行，
   25 个列表项裁切数和页面横向溢出均为 0，Browser error/warning 为 0。
7. 开发库为 `59/0059`，幂等重跑无新应用；`integrity_check=ok`、外键违规 0，验收 Session、
   ChatSession、issued grant 和一次性 Provider Key active 数均为 0，三产品服务 HTTP `200/200/200`。

详细命令、主流程、异常/权限/租户结果、迁移终态、应用内截图和资源清理统一记录在
`docs/testing/PRD-067-tests.md`。Excel 总表、M2/M3 交付表和验收清单确认的下一项为
`PRD-068 - 行业/场景策略模板库`；总体 108 条目标仍在进行。
