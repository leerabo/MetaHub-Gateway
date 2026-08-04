# PRD-046 策略模拟与单请求预演测试记录

测试日期：2026-07-18

当前状态：已完成。设计、两轮自审、实现、专项自动化、Python 根级全量、0035 迁移、Node/Console/静态门禁、真实三服务/API、结果未知补偿、敏感扫描和真实 Chrome 三档验收均已通过；本记录按自动化、迁移、运行 API、停止后数据库和真实浏览器分层保存证据，不用单层结果替代其他层。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。pytest 使用工作区 `D:/MetaHub/.tmp` 作为 `TEMP/TMP`，每次运行使用唯一 `--basetemp`。当前开发库为 `D:/MetaHub/.data/metahub-dev-prd031.db`，运行验收只允许使用受控副本，不在正式开发库上执行破坏性 down/up 往返。

## 证据口径

- 通过统计只记录最后一次明确退出码为 0 的结果；专项与全量覆盖重复时不相加。
- 自动化、迁移、真实 API 和真实浏览器是四类独立证据，不能互相替代。
- `artifacts/prd046/` 可保留失败或诊断批次；只有本文明确指定的成功批次可用于结论。
- runtime 脚本、成功输出、空 stderr、停止后数据库和浏览器截图均记录完整 SHA-256；诊断批次和中间 hash 不作为最终证据。
- 首轮失败或调度抖动只作为修复/复跑依据，不计入最终通过；最终统计只引用当前代码和最后一次完整通过结果。

## Excel 验收映射

- `TC-0091`：通过。自动化、真实 API 和真实浏览器共同覆盖 Prompt、模型、Project、数据等级输入，以及 Policy 决策树、命中规则、动作、路由、DLP 和预算输出；Provider 回环调用、Usage 和成本事实均为 0。
- `TC-0092`：通过。自动化与真实 API 覆盖匿名/坏 Token、developer/viewer、跨租户、防枚举、异常输入、依赖故障、并发/重复 request_id、结果未知补偿和敏感字段；真实浏览器覆盖 developer/viewer 直达回退与后端 403。
- M1 边界：只模拟当前构造的一次请求，不回放历史流量，不调用真实 Provider，不实现 Virtual Key/Application 预算、RPM/TPM/并发、KeyPool/credential 或完整 Gateway 等价模拟。

## 架构约束映射

- `POST /policies/dry-run` 由后端 `require_policy_admin` 授权，只从 Session 恢复 `tenant_id`，请求体不接受 tenant。
- 正文以 256 KiB 上限流式读取；重复 key、NaN/Infinity、未知字段、非法 UTF-8、非 object、深层递归和超限输入失败关闭为统一错误码，并继续携带 request_id/trace_id。
- Policy、路由、DLP、Usage、reservation 和预算在同一 tenant-qualified query-only 连接中读取；模拟不调用 Provider Adapter、KeyPool 或客户 Endpoint。
- 完成后第二连接原子写入 `policy_explanations`、`policy_events`、`audit_logs` 三类安全事实，提交后才投递稳定 `policy.dry_run_executed` Webhook。
- Prompt 不持久化、不计算 hash、不写日志/事件/审计/Webhook、不在响应回显；响应只保留长度和 `prompt_redacted=true`。
- `coverage_complete=false` 与 `gateway_parity=partial` 明确未覆盖 Virtual Key/Application、限流、credential 和真实 Provider；不得把 what-if 描述成线上精确等价。

## 复现准备

```powershell
Set-Location D:\MetaHub
$python = 'C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = 'shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src'
$env:TEMP = (Resolve-Path '.tmp').Path
$env:TMP = $env:TEMP
$base = Join-Path $env:TEMP ('pytest-prd046-' + [guid]::NewGuid())
```

## 已通过专项自动化

### Policy Preview 与 Dry-Run 主专项

```powershell
& $python -m pytest -q `
  control-plane-api/tests/unit/test_policy_preview.py `
  control-plane-api/tests/integration/test_policy_dry_run.py `
  --basetemp $base
```

结果：`20/20 passed`，由 shared 纯预演 `4` 项和 Control Dry-Run 集成 `16` 项组成。覆盖确定性 Policy/路由/DLP/预算短路、严格 DTO、admin-only、tenant 隔离、防枚举、request_id 三事实、并发恢复、Webhook、依赖/事务故障、零 Provider 调用和零运行成本事实。

### Preview 与 API 合同交叉复跑

```powershell
$base = Join-Path $env:TEMP ('pytest-prd046-contract-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/unit/test_policy_preview.py `
  control-plane-api/tests/unit/test_policy_dry_run_contracts.py `
  --basetemp $base
```

结果：`7/7 passed`。其中 preview 的 `4` 项与上一组重复，只作为交叉复跑，不重复累计。合同专项覆盖路由注册、统一错误/诊断、Webhook 白名单、正文上限和敏感字段约束。

### 既有 Policy 与 Webhook 回归

```powershell
$base = Join-Path $env:TEMP ('pytest-prd046-policy-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_policies.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd046-webhook-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_webhooks.py `
  --basetemp $base
```

结果：既有 Policy `17/17 passed`；Webhook `8/8 passed`。证明新增 Dry-Run 没有破坏 PRD-044 Policy CRUD/校验/事务/RBAC，也没有破坏 Endpoint、HMAC、重试、重放和 Secret 轮换合同。

## 0035 迁移验证

```powershell
$base = Join-Path $env:TEMP ('pytest-prd046-migrations-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_db_migrations.py `
  --basetemp $base
```

结果：`23/23 passed`。覆盖空库应用至 0035、0034 -> 0035 -> down -> up、`policy_explanations` tenant/actor 外键和不可变触发器、三事实部分唯一索引、Policy event/audit/Webhook 合同扩展，以及 down migration 对非 PRD-046 Policy、审计、Webhook、请求日志、Usage、索引和触发器的逐字段保真。

0035 的正式边界：

- 新增 `policy_explanations`，只保存已完成预演的安全摘要，不保存 Prompt、Prompt hash、DSL/AST、比较实际值、预算余额、Provider URL、Key 或 Token。
- `policy_events` 和 `audit_logs` 增加 `policy.dry_run_executed` 精确白名单，三类事实共享 `tenant + actor + request_id` exactly-once 键。
- explanation 禁止 UPDATE/DELETE；down migration 删除 PRD-046 新事实并恢复 0034 旧结构，不伪造转换后的历史。

## 全量与静态门禁

- Python 根级全量：收集 `459` 项，最终无并发第二轮 `459/459 passed`，退出码 0。首轮仅有 1 项 50 ms 策略求值调度超时；该项单独复跑及随后完整第二轮均通过，首轮不计入最终统计。
- Node 示例：`33 passed`。
- Console 全量：`200 passed`。
- Console lint：`103 files passed`。
- `scripts/ci/check-comments.py`：`comment coverage ok`。
- PRD-046 相关 Python compile、JavaScript/MJS `node --check` 和目标 Ruff：通过。

上述结果证明代码级回归和中文注释基线已关闭，但不能替代真实服务/API 与浏览器运行验收。

## 真实浏览器验收

最终成功批次：`20260718T161021`，使用真实 Chrome、Control API、Console 和受控 0035 数据库运行 `scripts/acceptance/prd046-browser-acceptance.mjs`。

- admin 真正提交一次 Dry-Run，HTTP 200，响应有 request_id/trace_id 与 `Cache-Control: no-store`；页面显示 allow、5 个树节点、1 条检查 Policy、partial parity、route selected、DLP clean、budget within limit，并把焦点恢复到结果标题。
- Prompt 在请求 settle 前已经从 textarea 和前端 payload 引用清除；响应、DOM、URL、form/storage、console 和截图均不包含 sentinel。
- 零成本断言核对 14 个运行事实/状态集合无变化，`providerCalled=false`；只新增 1 条 explanation、1 条 Policy event、1 条 audit，安全摘要精确且数据库敏感命中为 0。
- developer/viewer 手写 `#policy-simulator` 均回退 overview；无导航、无模拟器 DOM、无 Dry-Run 请求。两者后端唯一权限请求均为 403，证明前端隐藏不替代后端授权。
- 清理确认临时 Policy/Project/用户/Session 均已处置，5 条原 Policy 状态恢复，`failures=[]`；身份区域已遮罩。
- 三档根布局均无横向溢出、关键元素越界、文本裁切或同级重叠；表单列数为 `2/1/1`：

| 截图 | 尺寸 | bytes | SHA-256 |
| --- | ---: | ---: | --- |
| `artifacts/prd046/browser-desktop-1280x720.png` | 1280x2866 | 207,038 | `CC18E30B6D55F7A6FC55DDEB2E1130AF1E572FCB26ABB8451A0ED709FA56F055` |
| `artifacts/prd046/browser-intermediate-900x800.png` | 900x3195 | 204,396 | `B06A2A62D56E378C4515E531C398E322D406A3BBF8C6669F0B797FBF0D79AAF0` |
| `artifacts/prd046/browser-mobile-390x844.png` | 390x6840 | 238,507 | `F2C008D7101229B75722E1D1422916A804E9A50FA6E798AEF30D1BF31485F7BE` |

## 真实 API 与三服务验收

最终成功批次：`20260718T101126`。当前 `scripts/acceptance/prd046-runtime-acceptance.py` 的 SHA-256 为 `171803A28ECD05DCE0189635025E4EAED35EB9724FD6D2374492096C5D7692FE`，该批次使用此文件连接受控 Gateway、Control API、Console、SQLite 和回环 Webhook/Provider 探针。

| 制品 | bytes | SHA-256 |
| --- | ---: | --- |
| `artifacts/prd046/runtime-acceptance-20260718T101126.out.log` | 29,525 | `5CDBE004C08C003C07A214096A260F9229E4337FEA82DE7484F14556B78921DF` |
| `artifacts/prd046/runtime-acceptance-20260718T101126.err.log` | 0 | `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855` |

- 进程退出码 0，JSON `status=passed`；Gateway、Control、Console 均为 HTTP 200。运行库报告最高 0035、`integrity_check=ok`、外键违规 0。
- allow、deny、route unavailable、DLP block、budget block 和 Webhook 失败不改主响应六类阶段均为 HTTP 200，request_id/trace_id/no-store 完整；严格非法正文返回 422 `policy.dry_run_payload_invalid` 且不回显 Prompt。
- 匿名/developer/viewer 分别为 401/403/403，错误码为 `auth.token_missing`/`auth.permission_denied`；响应不含 Prompt。跨租户与防枚举由专项集成测试补足，未把自动化请求伪写成该运行批次。
- 回环 Provider `call_count=0`；各业务阶段的 Gateway claim/log/Usage/路由/DLP/预算事实均零增量，explanation/event/audit 各精确新增 1 行且三者 request/trace/object/decision 一致。
- Webhook 成功路径收到 2 个签名有效事件；失败 Endpoint 完成 3 次尝试且 Dry-Run 仍为 200。重复 request_id 返回 409 `policy.dry_run_request_id_reused`，复用持久 event_id 重派两类事件，无重复网络发送。
- cleanup `errors=[]`、intent 精确回查 11 个资源、`active_intent_residue=0`、`final_reconcile_rows=0`。管理员登录 intent 解析 1 条、正常 logout 成功；2 个临时用户、2 个 Endpoint、Project、Provider、Model、Alias、DLP、Budget、Policy 均已禁用，原 5 个 Policy 和 1 个 Budget 恢复。
- 结果未知路径使用有限 mutation 超时、前后 settle、唯一 User-Agent Session intent、`BEGIN IMMEDIATE` 认证围栏、captured/resolved ID fail-closed coalesce 和最终数据库收敛；独立复审及迟到提交、User ID 冲突、登录响应丢失故障注入均通过。
- 运行期隐私扫描覆盖 5 个指定制品；2 个 Prompt 及 hash、8 个已知敏感值、Authorization/Bearer、Provider/Virtual Key、治理正文、Traceback、ERROR 和 HTTP 5xx 命中全部为 0。

## 运行库与停止后验收

- 最终证据库：`artifacts/prd046/runtime-final-current-0035-20260718T101025.db`，`3,620,864 bytes`，SHA-256 `2EEB0094DC31B30464982E1689FB2E554BB6F5825BCB89C0F812647BB2C8BDF1`。
- 服务停止后执行 WAL checkpoint，WAL 为 0；`schema_migrations` 共 35 条，最高 `0035 policy_dry_run`，`integrity_check=ok`，外键违规 0。
- 11 个本轮临时资源全部 disabled；1 个管理员 Session 与 2 个角色 Session 均 revoked，活动临时资源/Session 为 0。原有九类治理状态与验收前快照差异为 0，原 5 个 Policy 和 1 个 Budget 保持 active。
- 数据库对 Prompt 前缀、临时密码的明文命中均为 0。Gateway、Control、Console 已停止，8000、8001、5173 均无监听。
- 停止后对最终 10 份服务/验收日志再次扫描，已知敏感值、Authorization/Bearer、密码/Token、Provider/Virtual Key、Prompt sentinel、治理正文、Traceback、ERROR 和 HTTP 5xx 十类命中全部为 0。

## 已知边界

- 本期不读取历史 Prompt/Response 或回放 request_logs，不创建真实 Gateway 调用。
- `runtime_compatible` 仅表示 active candidate 是否符合 PRD-045 发布器/执行器合同；不是完整线上等价保证。
- `coverage_complete=false`：不模拟 Virtual Key/Application 预算、RPM/TPM/并发、KeyPool/credential、真实 Provider 健康调用或客户 Endpoint。
- 进程内 30 RPM/actor、120 RPM/tenant 只用于单实例 CPU 防护，不宣称替代未来分布式限流。
- Webhook 持久 outbox、进程崩溃接管和 dead-letter 仍属于 PRD-057。

## 当前结论

设计、两轮自审、实现、专项/全量、0035、真实三服务/API、结果未知补偿、停止后数据库与日志复扫、真实浏览器和文档均已通过。PRD-046 可以标记完成；下一条 PRD-047 尚未开始设计或实现。
