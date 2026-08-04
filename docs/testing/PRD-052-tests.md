# PRD-052 租户/项目/Key 三级预算测试记录

测试日期：2026-07-27

当前状态：已完成。设计与两轮编码前自审、实现、0042、聚焦测试、真实 Runtime/Browser、日志隐私、Session 清理、三服务停服和 SQLite 静止终态审计均已通过。权威批次为 `artifacts/prd052/20260727T112603`，终态证据为 [final-audit.json](../../artifacts/prd052/20260727T112603/final-audit.json)。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。pytest 的 `TEMP/TMP` 均限定为 `D:/MetaHub/.tmp`，每批使用独立 `--basetemp`；本轮没有使用 `C:/tmp`、沙箱外目录或提权测试方案。

## 1. 验收口径

- Excel `TC-0103`、`TC-0104`：租户、Project、Virtual Key 三层预算同时适用，任一 `block` 规则耗尽都必须在 Provider 前拒绝；Project A 耗尽不得影响 Project B，租户层耗尽则影响同一 Environment 下该租户的全部 Project/Key。
- `POST /budgets` 是 create-only；同 Environment、同 scope 重复创建返回 `409 budget.rule_conflict`，不能暗中覆盖旧规则。`GET /budgets`、`GET /budget-status`、`PATCH /budgets` 继续保持兼容。
- admin 可读写，developer 只读，viewer 不可读写；权限必须由后端执行，前端隐藏入口不是授权依据。
- Project、Application、Virtual Key、BudgetRule、预算事件和正式 FinOps 分录均按 `tenant_id + environment_id` 校验归属；跨租户对象与不存在对象保持同形错误，不泄露存在性。
- 已入账消费只读取正式 `finops_ledger_entries` 中 `usage_charge + debit + posted` 的归一化 CNY；未知、未定价或不可归一化金额必须 fail closed。
- API 成功和错误均保留统一 `request_id/trace_id`；预算读取响应保持 `Cache-Control: no-store`。
- `gateway.budget_exhausted` 必须返回 HTTP 402，并证明 Provider attempt 为 0；用量、成本、预算判定、失败请求及 `budget.scope_exceeded` 事件可追踪。
- Provider Key、Virtual Key 明文、Authorization、Token、密码、Prompt、Response 和 Endpoint 凭据不得写入日志、DOM、URL、Storage、截图或验收 JSON。

完整数据表、API、页面、错误码、安全边界和代码计划见 [PRD-052 设计文档](../prd/PRD-052-tenant-project-key-three-level-budget.md)。

## 2. 设计与两轮编码前自审

设计文档先于实现落盘，并完成两轮自审后才进入编码。

第一轮自审聚焦需求边界、FinOps 金额来源、层级冲突、事务并发与兼容性，修正了以下问题：

- 将预算消费从旧 `usage_ledger.cost_cny` 冻结为正式 Usage Charge 账本口径，金额比较统一使用整数微元。
- 区分页面容量语义的 `limiting_rule` 与单次请求阻断主因 `governing_rule`，冻结 `tenant > project > application > virtual_key` 的稳定主因顺序。
- 明确多层规则为累计 AND 门禁，子层 `alert_only` 不能解除父层 `block`。
- 补齐 reservation、提额恢复、事件首次投递重试、scope ID 算法、Application 兼容父链与 Policy 剩余额度复用边界。
- 将 `budget.scope_exceeded` 定义为可持久化、可去重且提交后投递的业务事实，不在事务提交前产生外部副作用。

第二轮自审聚焦 RBAC、租户/Environment/Project/Key 归属、严格 wire、错误码、隐私和连接生命周期，修正了以下问题：

- POST/PATCH body 与 GET query 拒绝未知字段、重复字段、客户端 `tenant_id/actor` 和服务端派生字段；RBAC 在正文业务解析前执行。
- Control 读取使用显式只读事务，写路径使用 `BEGIN IMMEDIATE`，所有公开服务在 `finally` 关闭连接。
- 0042 同时校验 INSERT 与归属字段 UPDATE，数据库 trigger 作为服务层 tenant-qualified 查询后的第二道防线。
- Audit、Webhook、结构化成功日志只允许在事务提交后产生；回滚路径不得留下虚假成功事实。
- 未知 CNY、SQLite 整数聚合风险、sticky BudgetBlock、409 scope mismatch 和正式 Usage 接管 reservation 的边界均被写入冻结合同。
- 范围复核确认未引入部门预算、FinanceAdmin、Redis、多节点 CAS 或任意优先级 DSL，未删除既有 Application scope。

两轮自审结论均记录在设计文档第 14 节；第二轮结束时无未关闭 P0/P1 设计问题。

## 3. 证据分级与测试策略

本记录严格区分三类证据：

1. **本轮直接通过输出**：当前源码实际执行后得到的 pytest/Node/lint/编译/注释结果，可用于当前变更闭环。
2. **权威运行制品**：`artifacts/prd052/20260727T112603` 中写一次的 `runtime-evidence.json`、`browser-evidence.json`、六张最终 PNG 和 `final-audit.json`。
3. **缓存或诊断佐证**：`.tmp` 下 pytest basetemp、SQL 诊断库、浏览器诊断脚本、失败批次和修复前截图，只用于定位测试节点或保留失败历史，**不作为新的通过计数，也不与直接结果相加**。

本轮遵循聚焦闭环：正式 Usage/价格证据修复只复测对应单点与 Runtime 合同；移动端 CSS 两括号修复只复测响应式生命周期合同并重新执行两档 Browser 验收。没有因为局部问题重跑无关的 Python、Node 或 Console 全量套件，也没有为补数字虚构“全量通过”计数。

## 4. 本轮直接测试结果

| 范围 | 结果 | 证据与关键覆盖 |
| --- | --- | --- |
| 正式 Usage 单点 | `1 passed` | 当前源码直接执行；在真实迁移临时库插入 Usage、价格快照和正式 `usage_charge` 投影，验证 `finops_legacy_backfill` 与 `legacy-unversioned-v1` owner 合同 |
| Environment Alias 单点 | `1 passed` | 当前源码直接执行；冻结内部模型 ID 与外层 Alias 名称不能混用，防止价格 owner 错配 |
| PRD-052 Runtime 验收器合同 | `14/14 passed` | 当前源码直接执行；覆盖回环地址、批次/数据库绑定、严格响应、角色 Session 清理、Alias、正式 Usage、readiness、隐私投影和 write-once |
| Policy Dry-Run 相邻回归 | `5/5 passed` | JUnit：`.tmp/prd052-regression-policy-dry-run-final.xml`，记录 `tests=5`、`failures=0`、`errors=0`、`skipped=0`、`time=38.310s` |
| 预算页面响应式生命周期 | `5/5 passed` | `console-web/tests/unit/prd052BudgetLifecycle.test.mjs`；覆盖异步 owner、失效 callback、Key/Environment 联动、409+request_id 保留、CSS 括号平衡与单列转换 |
| Console lint | `129 files passed` | 当前 Console 源码 lint 通过 |
| Python 语法检查 | 退出码 0 | 修改后的 Runtime 验收器与合同测试 `py_compile` 通过 |
| 中文注释门禁 | `comment coverage ok` | `scripts/ci/check-comments.py` 当前源码通过 |

除明确保存的 Policy Dry-Run JUnit 外，其余项目是本轮终端直接通过输出；对应 `.tmp` basetemp 只用于 pytest 隔离，不把目录存在误写成 JUnit 或额外一轮执行。由于没有为每个早期开发批次保留统一机器可读总表，本记录不合成一个无法审计的“PRD-052 Python 总数”。

## 5. 主流程、异常流程、权限与租户隔离

### 5.1 主流程

- admin 创建 tenant、project、virtual_key 三层规则；Control 返回稳定 `BudgetScope`、规则 ID 和调用链 ID。
- `GET /budget-status` 从可信 Key 归属恢复 `tenant -> project -> application -> virtual_key` 兼容链；页面目标三层金额都来自同一条正式 Usage，不重复计费。
- 正式 Usage 与 FinOps `posted/debit/usage_charge` 一对一投影；租户、Project、Key 的 `ledger/reserved/effective` 均验证为 `1/0/1 CNY`。
- Project 预算为 `0.5 CNY`，在 `1 CNY` 正式消费下显示 `exhausted + block`，并形成可追踪的 BudgetBlock、evaluation 与 `budget.scope_exceeded` 事实。

### 5.2 异常流程

- 同 scope 重复 `POST /budgets` 返回 HTTP 409，`budget.rule_conflict` 与 `request_id` 保留，原规则未被覆盖。
- Project 预算耗尽时 Gateway 返回 HTTP 402、`gateway.budget_exhausted`；阻断主因为 Project 规则，Provider attempt 精确为 0。
- 正式账本金来源、价格 evidence owner、模型 Alias/内部模型 ID 和迁移 readiness 均有 fail-closed 合同；本轮发现的 owner mismatch 没有通过放宽数据库 trigger 绕过，而是修正验收数据的 source/version 组合。
- Policy Dry-Run 验证预算阻断仍以 HTTP 200 表达 `would_error`、不创建 reservation；缺少预算上下文、未知消费和 DLP 先阻断路径均保持原合同。

### 5.3 权限与租户隔离

- Runtime 真实验证 admin 写入；developer/viewer 的 POST 与 PATCH 均由后端返回 403，不能依赖前端按钮隐藏。
- 代码级预算与迁移专项覆盖 tenant-qualified Rule/Project/Application/Key 查询、跨租户/跨 Environment 同形拒绝、父规则与子事实 owner 一致性，以及恶意 INSERT/UPDATE 被 0042 trigger 拒绝。
- Runtime 资源链验证 Environment、Project、Application、Virtual Key 与三条预算规则属于同一 tenant/environment；客户端没有提交或覆盖 `tenant_id`。
- 成功和业务错误均校验响应 Header/正文 `request_id` 一致；预算 API 读取校验 `Cache-Control: no-store`。

早期专项的 isolated basetemp 可证明相应节点曾被执行，但 basetemp 本身不保存可审计 pass/fail 汇总。因此本节只记录已核对的合同覆盖，不把目录数量换算成通过数；最终通过计数以第 4 节直接输出和第 6、7 节写一次 JSON 为准。

## 6. 0042 与真实 Runtime

权威批次：[artifacts/prd052/20260727T112603](../../artifacts/prd052/20260727T112603/)。权威结果：[runtime-evidence.json](../../artifacts/prd052/20260727T112603/runtime-evidence.json)，`schema_version=1`、`status=passed`。

- Runtime 库共 42 条 applied migration，最高版本 `0042`；`PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 违规为 0。
- 0042 readiness 验证不仅检查迁移版本，还检查 `environments`、`projects`、`price_snapshots`、预算表和价格 owner trigger；未迁移或对象不完整的数据库拒绝运行且不被修改。
- Runtime 创建的 Environment 使用公开 API，Project/Application/Virtual Key owner chain 全部通过；默认模型路由和 Environment 均为 active。
- 三条新预算规则创建成功；重复创建 409、PATCH、聚合状态、三层摘要、可信作用链、Application 兼容链和正式账本/预留拆分均通过。
- 正式 Usage ID 为 `usg_prd052_accept_252f345ba5de`，对应 FinOps Entry 为 `fle_6bb2f43c987449f79d9e1d4817ae235d`；证据只保存不透明 ID 和安全投影，不保存模型正文或凭据。
- Gateway 真实请求返回 HTTP 402；错误码 `gateway.budget_exhausted`，request/trace 均与该次验收绑定，Project rule governed 为 true，`provider_attempt=0`。
- admin、developer、viewer 三类 Runtime Session 全部注销；聚合 `session_revoked=true`。
- 运行库中 `2026-07-27T03:30:00Z` 之后共创建 7 个验收 Session，终态为 `7 created / 7 revoked / 0 active / 7 revoked_at`。
- 运行库和开发库终态均为 42 条 applied migration、最高 `0042`、`integrity_check=ok`、外键违规 0；运行库 `PRAGMA wal_checkpoint(truncate)=[0,0,0]`，且 `runtime.db-wal`、`runtime.db-shm`、`runtime.db-journal` 均不存在。
- Runtime 隐私门禁 `credential_material_absent`、`environment_only_credentials`、`model_content_absent`、`safe_projection_verified` 均为 true。

`artifacts/prd052/20260727T095706` 是早期失败/诊断批次；`.tmp/prd052-sql-diagnosis-20260727T110647-137e87fe.db` 用于精确定位 `price_snapshot_owner_mismatch`。二者均不是权威 Runtime 成功证据。根因为验收器使用 `source=finops_legacy_backfill` 时错误搭配 `price_schema_version=prd052-runtime-v1`；0041 冻结合同要求版本 `legacy-unversioned-v1`。修复后正式 Usage 单点、14 条 Runtime 合同及上述真实 Runtime 依次通过。

## 7. 真实 Browser 与移动端修复

权威结果：[browser-evidence.json](../../artifacts/prd052/20260727T112603/browser-evidence.json)，`schema_version=1`、`status=passed`。

- 1280x720 与 390x844 两档真实视口分别验收预算概览、作用链和管理员表单，共 6 张 PNG；正式截图 `6/6` 通过，SHA-256/bytes mismatch 均为 0，并全部校验精确尺寸和非空像素。
- 六组布局的页面级横向溢出、控件裁切、内容裁切和重叠均为 0；三个锚点在两档视口都与视口相交。
- 页面实际显示 4 条规则；聚合状态为“已耗尽 / 阻断”，限制层级为 Runtime Project，作用链为 Tenant → Project → Application → Virtual Key。
- Project 行显示 `1 / 0 / 1 CNY`，额度为 `0.5 / 0 CNY`；管理员创建表单、三层 scope 选项、模式控件及 Key/Environment 联动正常，服务端拥有字段未出现在表单。
- Budget rules/status API 均返回 HTTP 200、`request_id` 和 `Cache-Control: no-store`；`blocked_request_count=0`、`browser_error_count=0`。
- 最终截图已对身份和 Key 选择器应用隐私遮罩；DOM、URL、Storage 扫描通过。登出 HTTP 200，浏览器认证状态清空，Browser Session revoked 且浏览器关闭。

最终 PNG：

- `screenshots/budget-overview-desktop-1280x720.png`
- `screenshots/budget-overview-mobile-390x844.png`
- `screenshots/budget-scope-chain-desktop-1280x720.png`
- `screenshots/budget-scope-chain-mobile-390x844.png`
- `screenshots/budget-admin-form-desktop-1280x720.png`
- `screenshots/budget-admin-form-mobile-390x844.png`

移动端首次检查发现页面横向溢出。根因不是预算数据或浏览器沙箱，而是 `@media (max-width: 1400px)` 中 `.budget-rule-row:not(.budget-rule-head) > [data-label]::before` 声明块之后漏了两个右花括号：一个应关闭选择器声明块，一个应关闭媒体查询。后续 CSS 被错误吞入未闭合上下文，390px 单列规则不能稳定生效。修复仅补回这两个结构括号，并在 `prd052BudgetLifecycle.test.mjs` 增加全文件花括号数量相等和移动端 `grid-template-columns: 1fr` 合同；随后 5/5 生命周期测试和两档真实 Browser 全部通过。

`screenshots-pre-fix-overflow/` 与 `.tmp/prd052-browser-diagnostic.mjs` 只保留根因定位历史，不是最终六张截图，也不得用来证明布局通过。

## 8. 日志与隐私审计

- Runtime JSON 不包含密码、Authorization、Provider Key、Virtual Key 明文、Prompt 或 Response；只保留 tenant-qualified 不透明资源 ID、金额、布尔门禁和调用链 ID。
- Browser JSON 的 `environment_only_credentials`、`runtime_projection_safe`、`screenshot_identity_and_key_selectors_masked`、`dom_url_storage_scanned`、`safe_projection_verified` 均为 true。
- 页面网络证据没有被安全策略阻断的外部请求，也没有浏览器错误；最终截图不显示完整 Key、Token、Prompt 或 Response。
- 服务启动日志中的配置**字段名**（例如 token/secret 配置项名）不等于敏感值泄漏，审计必须按明文口令、Bearer/Authorization 值、Key/Token 值和 Prompt/Response 正文判断，不能按单词命中误报。
- 停服后终态扫描覆盖 6 份日志、共 5,286,382 bytes；ERROR、Traceback、unhandled exception、HTTP 5xx、Bearer 值、JWT 值、完整 Key 和模型正文命中均为 0。
- 仅 8 行包含 token/secret 配置项名称，其状态严格为 `defaulted`、`masked` 或 `missing`，没有凭据值；终态证据确认 `credential_values_absent=true`、`model_content_absent=true`。

## 9. 停服与静止终态审计

权威结果：[final-audit.json](../../artifacts/prd052/20260727T112603/final-audit.json)，`schema_version=1`、`status=passed`、`completed_at=2026-07-27T06:33:03Z`。停服只针对已经核对端口和进程身份的三个 PID，没有提权或扩大到无关进程。

- [x] Console：node PID 19448、端口 5173；`Stop-Process` 后进程停止且 listener closed。
- [x] Gateway：python PID 16188、端口 8000；`Stop-Process` 后进程停止且 listener closed。
- [x] Control API：python PID 16132、端口 8001；`Stop-Process` 后进程停止且 listener closed。
- [x] 三端口合计 `LISTENING_PORTS=0`，三个服务 exec session 均已退出。
- [x] Runtime 与 `.data/metahub-dev-prd031.db` 均为 42 条迁移、最高 `0042`、integrity `ok`、外键违规 0。
- [x] 验收窗口 Session 为 `7 created / 7 revoked / 0 active / 7 revoked_at`。
- [x] `PRAGMA wal_checkpoint(truncate)=[0,0,0]`；批次数据库目录只保留 `runtime.db`，无 WAL/SHM/journal sidecar。
- [x] 六份日志终态隐私与错误扫描通过；6 files、5,286,382 bytes，敏感值、模型正文、Traceback、ERROR 和 5xx 均为 0。
- [x] Runtime/Browser SHA-256 已写入 `final-audit.json`；正式截图 6 张、hash mismatch 0、layout failure 0，诊断截图明确排除。

## 10. 最终结论

PRD-052 已完成设计、两轮编码前自审、实现、0042、聚焦测试、当前源码语法/注释门禁以及真实 Runtime/Browser 验收。权威批次证明三层预算、正式 FinOps 消费、后端 RBAC、租户归属链、409/402、Provider 前阻断、双视口页面和登出清理均符合冻结合同。

停服后的端口、数据库、Session、sidecar、日志隐私和证据哈希终态也已全部通过，PRD-052 当前没有已知 P0/P1 缺陷，可以标记为“已完成”。本结论不表示全部 108 条 PRD 已完成；按 Excel 优先级和顺序核对后的下一条未完成项是 `PRD-053 成本异常检测基础`。
