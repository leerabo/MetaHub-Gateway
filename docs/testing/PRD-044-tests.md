# PRD-044 策略引擎 DSL 最小版测试记录

测试日期：2026-07-17

当前状态：设计、两轮自审、实现、专项与全量回归、0033 运行库迁移、真实 API、三服务、日志/数据库敏感扫描和桌面/中间宽度/移动浏览器验收全部通过，PRD-044 已完成。

运行环境：Windows PowerShell；Python 使用 Codex 内置运行时；Node.js 为 `v24.13.0`。pytest 同时设置工作区 `TEMP/TMP`，每次运行使用唯一 `--basetemp`，避免系统临时目录权限影响结论。运行库为 `D:/MetaHub/.data/metahub-dev-prd031.db`。

## Excel 验收映射

- `TC-0087`：六个变量 `tenant/env/project/model/data_class/budget_remaining`、五类动作 `allow/deny/mask/log_only/route_to`、非法表达式保存拒绝、命中解释和超时自动 deny + 告警标志，均由 DSL 单元、Policy API、真实 API 与浏览器验收覆盖。
- `TC-0088`：自定义脚本、函数、属性访问、复杂递归与外部调用均被词法/文法边界阻断；优先级、异常、权限、租户、统一错误、request_id 和敏感信息不泄露由专项故障注入、全量回归和最终运行扫描证明。
- 页面清单 `PAGE-062/PAGE-063`：策略管理页和策略编辑器只对当前稳定 RBAC 中的 admin 展示；developer/viewer 越权直达时导航、DOM 和 Policy 请求均为 0。
- API 清单 `API-206` 至 `API-212`：落地为 `GET/POST /policies`、`PATCH /policies/{policy_id}` 与 `POST /policies/validate`；请求不接受 tenant_id、服务端 ID、version、AST、hash 或 actor。
- 数据对象 `OBJ-061` 至 `OBJ-063` 与事件 `EVT-049/EVT-050`：落地为 Policy、单 PolicyRule、单 PolicyAction，以及 `policy.created`、`policy.validation_failed` 安全摘要事件。

## 架构约束映射

- 架构 `5.1` 要求控制面承载低频管理、数据面承载高频模型流量。本 PRD 只实现控制面校验/持久化和 shared 纯求值，不让 Gateway 每次请求查询策略表。
- 架构 `5.3` 与附录 `A.3` 要求配置先校验、后保存、再编译并通过版本化快照影响数据面。PRD-044 交付确定性编译和 Policy version；快照发布、缓存、回滚与请求 `snapshot_version` 仍由 PRD-045 实现。
- 架构 `7.2` 把更复杂 DSL、灰度和用户/部门路由放到后续阶段。本期只允许白名单变量、有限比较/布尔组合和单动作，禁止函数、属性、脚本、网络、文件和外部调用。
- 架构 `11.4` 要求敏感命中只保存规则 ID/摘要，`15.3` 要求业务表有 tenant_id 且管理 API 执行后端租户鉴权。Policy 事件、审计、Webhook 和日志只保存 ID/hash/reason/request_id/trace_id 等安全元数据。
- `active` 只表示下一快照候选，不代表策略已在线发布；`mask` 只返回决策，不修改 Prompt/Response；`route_to` 只返回已验证目标，不在 PRD-044 发起真实路由。

## 专项自动化

### DSL 与运行配置

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
$env:TEMP=(Resolve-Path '.tmp').Path
$env:TMP=$env:TEMP
$base = Join-Path $env:TEMP ('pytest-prd044-dsl-'+[guid]::NewGuid())
python -m pytest -q `
  control-plane-api/tests/unit/test_policy_dsl.py `
  gateway-data-plane/tests/unit/test_runtime_config.py `
  --basetemp $base
```

结果：`102 passed`。覆盖 canonical AST/hash、Unicode/Decimal、六变量、所有操作符、布尔优先级与短路、五动作规范、注入阻断、资源上限、缺失上下文、多策略优先级、`log_only` 继续、默认 allow、解释截断、结果完整性和确定性超时 fail-closed；配置默认 5 ms 且只允许 1-100 ms。

### Policy API、审计与 Webhook

```powershell
python -m pytest -q `
  control-plane-api/tests/integration/test_policies.py `
  --basetemp $base

python -m pytest -q `
  control-plane-api/tests/integration/test_audit_logs.py `
  control-plane-api/tests/integration/test_webhooks.py `
  --basetemp $base
```

- Policy API：`17/17 passed`，耗时 `121.5s`。覆盖 admin validate/create/list/PATCH/ETag、非法 DSL 与结构错误分层、developer/viewer/匿名无业务副作用、跨租户 Policy 和 route target、锁内非法目标回滚后独立失败事件、失败事件 commit 失败、created/audit 事务回滚、两路 Webhook 独立失败、超时 deny + 单事件幂等。
- Audit + Webhook：`14/14 passed`，耗时 `110.5s`。覆盖 `policy.created/policy.updated` 安全审计摘要、两类可订阅 Policy 事件、HMAC、投递隔离、递归敏感字段拒绝和既有事件回归。

### 0033 迁移

```powershell
python -m pytest -q `
  control-plane-api/tests/integration/test_db_migrations.py `
  --basetemp $base
```

结果：`18 passed`。覆盖空库迁移、重复 migrate、0032 历史升级、0033 up/down/up、Policy/Rule/Action/Event 归属、version 精确加一、active route target、事件不可变与部分唯一约束、audit 历史保留、`integrity_check` 和 `foreign_key_check`。

## 全量回归与静态门禁

- Python Control API：`238/238 passed`，耗时 `843.9s`。
- Python Gateway：`86/86 passed`，耗时 `240.1s`。
- Python 示例：`38/38 passed`，耗时 `3.5s`。
- Python 合计：`362/362 passed`。
- Node 示例：`33/33 passed`，Node 测试框架耗时 `3146.6064ms`；示例 5 个源码和 6 个测试文件逐一 `node --check`，`11/11` 通过。
- Console Policy 专项：`11/11 passed`。
- Console 全量：`183/183 passed`，耗时 `4254.4ms`。
- Console lint：`console lint ok (97 files)`。
- PRD-044 相关 Python 文件 Ruff 检查通过，目标模块 `compileall` 通过；不把局部结果表述为全仓 style Ruff 通过。
- `python scripts/ci/check-comments.py` 输出 `comment coverage ok`，最终中文注释门禁通过。
- `node --check scripts/acceptance/prd044-browser-acceptance.mjs` 退出码 0；PRD-044 Console 相关 4 个 JS/MJS 文件逐一语法检查通过。

### 回归中发现与修正

- 第一次 Gateway 全量回归有 1 项失败，原因是 shared FastAPI 工厂把 Policy 所需 ETag 暴露头全局应用到 Gateway CORS，扩大了无关服务的响应合同。实现已改为 `create_service_app` 接受可选额外 CORS 请求/暴露头，只由 Control API 启用 `If-Match/ETag`，并新增 Control CORS 定向测试。
- 修正后双服务 CORS 定向回归 `2/2 passed`，随后重新执行最终 Gateway `86/86` 与 Control API `238/238` 全量回归。本文记录的是修正后的最终统计，不沿用第一次中间轮结果。

## 运行库迁移与完整性

- 迁移前备份为 `artifacts/prd044/runtime-pre0033.db`，大小 `2,846,720` bytes。
- 运行库共有 33 条 applied migration，最高版本为 `0033`。
- `PRAGMA integrity_check` 返回 `ok`；`PRAGMA foreign_key_check` 返回 0 行。
- `policies`、`policy_rules`、`policy_actions`、`policy_events` 四张表存在；Policy 相关触发器共 17 个。
- Policy/Rule/Action 父子归属与事件关联孤儿数均为 0。
- 最终运行库有 6 条 Policy、6 条 Rule、6 条 Action；12 条 PolicyEvent，其中 `policy.created` 6 条、`policy.validation_failed` 6 条；Policy 管理审计共 8 条。

## 真实 API 验收

- admin 合法 validate 返回 `valid=true`、可解释 comparison 和 `decision=deny`；六变量由 DSL 专项逐项覆盖，五类动作均有真实 API 创建证据。
- 五类动作均通过真实 API 创建成功；真实 API 的缺失 route target，以及专项中的跨租户/disabled target，均被固定错误拒绝，且错误不回显正文或目标存在性。
- 创建返回 `ETag: "1"`，PATCH 使用正确 `If-Match` 后返回 `ETag: "2"`；stale 与缺失 If-Match 均返回 HTTP 409 `policy.version_conflict`，原策略不被覆盖。
- 未知 query 返回 `policy.query_invalid`；developer/viewer 四个入口均返回 403 `auth.permission_denied`，匿名返回 401 `auth.token_missing`。
- 成功与错误响应 body/header 均携带 request_id/trace_id；Policy 响应使用 `Cache-Control: no-store`，CORS 允许 `If-Match` 并暴露 ETag。
- 本轮真实 API 后日志与数据库安全扫描均为 0 命中，没有 DSL、description、context、Provider Key、Authorization、Token、Prompt 或 Response 泄露。
- 运行库当前没有订阅 `policy.created/policy.validation_failed` 的 Endpoint，因此真实 Webhook delivery 为 0；专项自动化已经覆盖订阅、HMAC、幂等和两路独立失败，0 delivery 不表示跳过分发逻辑。

## 浏览器验收

使用系统 Chrome 与 `scripts/acceptance/prd044-browser-acceptance.mjs` 对真实 Console 验收：

- admin 完成 route_to 策略校验、创建、更新至 v2、非法表达式拒绝、动作筛选与重置；本轮实际 Policy 网络请求共 7 个。
- 浏览器脚本第一次使用 180 字符字符串时，DSL 按设计返回 `string_too_long`，证明字符串上限真实生效；脚本随后改用合法的 127 字符无空格值完成布局压力验收，而不是放宽生产约束。
- 1280x720、900x800、390x844 三档的 `innerWidth/clientWidth/root scrollWidth/body scrollWidth` 均与视口宽度一致；Policy 根节点、编辑器、校验区、筛选、列表无越界 offender，按钮文字 clipping 为 0。
- developer/viewer 都先真实请求 `#policies`，随后 hash 被清空并回退 overview；Policy 导航数、Policy DOM 数和后台 Policy 请求数均为 0。
- admin/developer/viewer 的 browser console error、page error 和 HTTP >=400 计数均为 0。
- 截图 `artifacts/prd044/browser-desktop-1280x720.png` 为 1280x720、62,121 bytes；`artifacts/prd044/browser-mobile-390x844.png` 为 390x844、32,153 bytes。两张图片均非空，并以灰色 mask 遮罩邮箱、租户和 Session 身份区域。

## 服务、日志与敏感扫描

- Gateway `http://localhost:8000/health`、Control API `http://localhost:8001/health`、Console `http://localhost:5173/` 最终均返回 HTTP 200。
- 最终扫描 6 个运行日志文件：Control API、Gateway、Console 各自 stdout/stderr；`level=ERROR`、Traceback 和 HTTP 5xx 命中均为 0，敏感 sentinel 命中为 0。
- 数据库敏感扫描检查 295 个文本值、共 47,257 个字符，DSL/description/context、Provider Key、Authorization、Token、Prompt、Response 等模式命中为 0。

## 已知边界

- 当前稳定角色只有 `admin/developer/viewer`，策略读写和校验为 admin-only；独立安全/财务角色与权限矩阵不在 PRD-044 提前实现。
- 本 PRD 不发布快照、不挂载 Gateway、不改变真实模型调用；这是已验收的范围边界，PRD-045 才负责快照版本、发布/回滚和数据面消费。
- `/policies/validate` 只提供单草稿的可选示例求值，不是 PRD-046 的完整 Dry Run 工作台，也不接受 Prompt。
- `mask` 当前只形成可解释决策，真实脱敏继续由 DLP 执行；`route_to` 只返回目标，真实路由由后续快照链路执行。

## 当前结论

PRD-044 的设计与两轮自审、受限 DSL、0033 迁移、控制面 API、租户/RBAC/事务/事件/Webhook、安全与前端实现、专项与 362 条 Python 全量回归、33 条 Node 示例、183 条 Console 全量回归、运行库完整性、真实 API、三服务、日志/数据库敏感扫描和三档浏览器验收全部通过。`active` 未发布和 Gateway 未挂载属于 PRD-044 的明确边界，不是未完成项；PRD-044 可以正式标记完成，PRD-045 尚未开始。
