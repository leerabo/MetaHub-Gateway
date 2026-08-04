# PRD-045 策略快照版本化与发布测试记录

测试日期：2026-07-18

当前状态：已完成。设计、两轮独立自审、实现、专项与全量回归、运行库 0034、真实三服务/API、敏感扫描和桌面/中间宽度/移动浏览器验收均已通过；本记录按自动化、运行 API 和真实浏览器三层保存证据，不用单层结果替代其他层。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。pytest 设置工作区 `TEMP/TMP` 并为每次运行使用唯一 `--basetemp`。目标运行库为 `D:/MetaHub/.data/metahub-dev-prd031.db`；最终运行库为 34 条迁移、最高 `0034`。

## 证据口径

- 最终总数只记录最后一次完整通过结果；同一测试在专项和全量中重复执行时不相加。
- 首轮失败只作为修复依据，不计入最终通过；修复后已重新执行受影响专项和完整分组。
- 临时库、运行库、真实 API 和真实浏览器分别给出证据，不能互相替代。
- `artifacts/prd045/` 保留调试失败批次；“日志零错误/零敏感命中”仅指下文明确标识的最终成功批次。

## Excel 验收映射

- `TC-0089`：通过。真实 API 批次完成发布、Gateway 拉取/请求、日志与 Usage 版本固定及单调回滚；真实浏览器批次完成 Console 发布/回滚和调用日志版本筛选/详情。
- `TC-0090`：通过。自动化覆盖一致性、LKG/no-head/stale/route_to 等边界；真实 API 覆盖内部身份、CAS、幂等、RBAC、Webhook 和敏感扫描；真实浏览器覆盖前后端权限与三档页面。
- M1 边界：本 PRD 只快照 PRD-044 Policy DSL，不实现复杂灰度、双跑、实例 ACK、跨地域一致性、MCP 主链路、硬件盒子、信创或复杂数据出境。

## 架构约束映射

- 控制面把同一 tenant 的 active Policy 重编为不可变快照；Gateway 通过独立内部 Token 按 tenant 拉取，不在每个模型请求中查询控制面或策略表。
- `v1=A -> v2=B -> rollback(v1)` 必须生成 `v3=A`。`content_hash` 证明执行内容一致，`artifact_hash` 覆盖版本/provenance；head 不倒退，历史 header/items/events 不可改写。
- Gateway 使用 per-tenant ETag、singleflight、monotonic freshness 和不可变 lease。后台 swap 不能改变已经进入非流式、Fallback 或 SSE 会话的版本。
- 冷启动无可信同步失败关闭；`public_service` 在普通 max-age 后阻断；`internal_only` 只允许在有硬上限的 emergency LKG 内继续，并保留鉴权、DLP、限流和预算。
- 所有已认证模型请求先写 `(tenant_id, request_id)` claim，再写 request log/Usage；Prompt、Response、DSL、AST、Provider Key、Virtual Key、Authorization、内部 Token 和原始上游错误不得进入日志、审计或 Webhook。

## 复现准备

```powershell
Set-Location D:\MetaHub
$python = 'C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH = 'shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src'
$env:TEMP = (Resolve-Path '.tmp').Path
$env:TMP = $env:TEMP
```

每条 pytest 命令运行前都重新生成 basetemp：

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-' + [guid]::NewGuid())
```

## 已确证专项自动化

### Shared 快照、运行配置与错误合同

```powershell
& $python -m pytest -q `
  gateway-data-plane/tests/unit/test_policy_snapshot.py `
  gateway-data-plane/tests/unit/test_runtime_config.py `
  --basetemp $base
```

结果：`47 passed`。

覆盖严格 wire 往返、固定双 hash 向量、Unicode/Decimal canonical、单调回滚 provenance、持久编译事实重核、未知/重复字段与超大 payload 拒绝、frozen 对象、统一错误/诊断/Webhook 白名单，以及轮询/LKG 时间关系、生产同步开关、内部 Token 和 Control URL 启动门禁。

### PRD-044 DSL 回归

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-dsl-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/unit/test_policy_dsl.py `
  --basetemp $base
```

结果：`93 passed`。证明 PRD-045 复用的唯一 DSL 编译/纯求值入口没有破坏 PRD-044 白名单变量、动作、确定性顺序、注入阻断、缺失上下文和超时 fail-closed 合同。

### Webhook 与配置摘要回归

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-shared-regression-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_webhooks.py `
  control-plane-api/tests/integration/test_config_summary.py `
  --basetemp $base
```

结果：`11 passed`。覆盖新增事件可订阅、安全信封/HMAC/敏感键拒绝，以及新增快照配置进入脱敏配置摘要。Webhook 文件之后又在 Control 定向回归中单独执行 `8 passed`；该重复结果不重复累计。

### Control 快照、Policy、审计、导航与 Webhook

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-control-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_policy_snapshots.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd045-policy-regression-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_policies.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd045-audit-navigation-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_audit_logs.py `
  control-plane-api/tests/integration/test_console_navigation.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd045-webhooks-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_webhooks.py `
  --basetemp $base
```

已确证结果：

- Policy 快照 API：`5 passed`。覆盖 publish/history/rollback/candidate drift、head+candidate 双 CAS、request_id 幂等、无变化冲突、admin-only、跨租户、未知字段、内部 Token、wire ETag/304、tenant 状态、mask/data_class/route target 安全和事务/Webhook 故障边界。
- 既有 Policy 回归：`17 passed`，证明候选 Policy 的 admin/RBAC、If-Match、事务、事件和 DSL 行为未回退。
- 审计 + 控制台导航：`8 passed`，覆盖新增审计动作/对象安全摘要和 admin-only `policy-snapshots` 导航。
- Webhook 定向回归：`8 passed`，覆盖新增 `policy_snapshot.published` 后既有 Endpoint、签名、重试、重放与 Secret 轮换合同。

### 0034 迁移

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-migrations-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_db_migrations.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd045-migrations-focused-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_db_migrations.py `
  -k 'migrate_empty_database or policy_snapshot' `
  --basetemp $base
```

已确证结果：全迁移文件 `21/21 passed`，耗时 `107.4s`；PRD-045/空库定向 `4 passed`。覆盖快照封印/不可变、tenant-qualified 外键、claim 与日志一致性、跨租户相同 request_id、Usage/价格快照复合关联、0033 -> 0034 -> down -> up 历史保真、Webhook/audit 清理，以及 down 遇跨租户重复 request_id 时安全中止。

另有新鲜临时库证据：共 `34` 条 applied migration，最高版本 `0034`，`PRAGMA integrity_check` 为 `ok`，`PRAGMA foreign_key_check` 为 `0` 行。该结果不替代运行库验收。

### 0034 复合请求关联受影响业务回归

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-composite-request-regression-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_budgets.py `
  control-plane-api/tests/integration/test_dashboard.py `
  control-plane-api/tests/integration/test_projects_applications.py `
  control-plane-api/tests/integration/test_usage.py `
  control-plane-api/tests/integration/test_usage_export.py `
  control-plane-api/tests/integration/test_model_prices.py `
  --basetemp $base
```

结果：`15/15 passed`，耗时 `104s`。该组合定向证明 0034 的 request claim、`(tenant_id, request_id)` 日志身份和 Usage/price 复合关联没有破坏预算聚合、Dashboard、Project/Application 归属、Usage 查询/导出及模型价格主流程；它仍不是 Control API 全量回归的替代品。

### Gateway 缓存、LKG 与运行时求值

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-gateway-cache-' + [guid]::NewGuid())
& $python -m pytest -q `
  gateway-data-plane/tests/unit/test_policy_snapshot_cache.py `
  --basetemp $base
```

最终结果：快照与缓存组合 `22 passed`。覆盖首次 singleflight、同 tenant frozen lease、损坏 payload 不保存 ETag、同版重试、monotonic LKG/emergency/hard expiry、public stale 阻断、无 head 与冷启动区分、304 强 ETag、waiter 取消不取消共享刷新、deny 日志版本冻结、预算上下文缺失和 `route_to` 安全摘要。

### Request Log 与 Debug Console claim

```powershell
$base = Join-Path $env:TEMP ('pytest-prd045-request-logs-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_request_logs.py `
  --basetemp $base

$base = Join-Path $env:TEMP ('pytest-prd045-debug-claim-' + [guid]::NewGuid())
& $python -m pytest -q `
  control-plane-api/tests/integration/test_debug_console.py::test_debug_console_reused_request_id_does_not_overwrite_first_result `
  --basetemp $base
```

已确证结果：Request Log `3 passed`；Debug Console 重用 request_id 专项 `1 passed`。覆盖 `snapshot_version` tenant-scoped 过滤、非法版本、详情安全摘要、viewer 拒绝，以及调试请求固定 disabled claim、重复 request_id 不覆盖首个日志/Usage/claim。

## 最终自动化门禁

- Python 根级全量回归：`434 passed`。该结果覆盖 shared、Control、Gateway、Python 示例及既有跨模块合同；首轮 Gateway 夹具失败修正后已重跑完整分组。
- 快照/缓存专项：`22 passed`；迁移全文件：`21 passed`。两项均包含 PRD-045 新增边界，不能与根级全量重复相加。
- Node 示例：`33 passed`；Console 全量：`194 passed`；Console lint：`100 files passed`。
- `scripts/ci/check-comments.py` 中文注释门禁通过；PRD-045 相关 Python `compileall`、MJS/JS `node --check` 和目标 Ruff 检查通过。全仓仍有既有 E501，因此没有把目标 Ruff 结果扩大为“全仓 Ruff 通过”。
- 最终浏览器脚本 SHA-256：`24E1BE5E6D6C876C3434E0EE84EEEFC663CEC8EA5AB08FF9DBB97FB6863A0190`；脚本对过滤响应、DOM 行数/request_id/版本和详情响应建立关联等待，避免旧 DOM 可见造成假阳性。

## 运行库与迁移验收

- 迁移前备份：`artifacts/prd045/metahub-dev-prd031-pre-0034-20260717T224326.db`，`3,133,440 bytes`，SHA-256 `C14086A2BC6697B9A2E3D08B8C687795DD257D52B07B86274E1A799DE64E1C6D`。原运行库没有执行破坏性 down 往返。
- 当前验收脚本成功证据库：`artifacts/prd045/runtime-current-0034-20260718T121643.db`，`3,362,816 bytes`，SHA-256 `F7A6F7947F8FC6ED0574B264D5FAF90B794AFF53062E7C90B025380E5E59BBF0`。该库由上述 0033 备份重新复制后只应用当前 0034，再运行当前验收脚本；不复用旧成功库。
- 当前运行库共 `34` 条 migration，最高 `0034`；`PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 为 `0` 行。迁移测试另完成 `0033 -> 0034 -> down -> up`，逐字段验证日志、Usage、价格、审计、Webhook、索引与触发器保真。
- 最终浏览器批次结束后，default tenant head 为 `v10`，`operation=rollback`、`previous=v9`、`rollback_source=v8`。v10 与 v8 的 `content_hash` 相同、4 条快照项逐字段差异为 0；`artifact_hash` 因版本和 provenance 不同而保持不同，符合单调回滚合同。
- 批次 `047d42c599` 的临时 Policy 为 `disabled/v2`；admin/developer/viewer 各 1 个用户均 disabled，5 个临时用户 Session 和 1 个 bootstrap Session 均 revoked。全库 active 临时用户、Policy、Session、bootstrap Session 均为 0；原 mask Policy 为 active，正式本地 admin 仍 active。

## 真实 API 与三服务验收

最终 API 批次：`20260718T121929`。当前 `scripts/acceptance/prd045-runtime-acceptance.py` 的 SHA-256 为 `8DDBF560662C18012672FF0C081BAFBC397B07187DF4F68497CE60EEC6B49CE6`；该批次使用此文件启动真实 Gateway、Control API、Console 和本地假上游，完成以下验证：

- 三个服务健康入口均为 HTTP 200；发布 v1、发布 v2、回滚 v1 内容生成 v3，v3 内容 hash 等于 v1、artifact hash 不同，head 单调递增。
- Gateway 在轮询窗口内消费新版本；allow、deny、回滚后新请求、request log、claim 与 Usage 使用同一实际版本。策略阻断在 Provider 前完成，上游调用数为 0。
- 内部 Token 覆盖 401/403/200/304；错误 CAS、重复 request_id 异 fingerprint 返回 409；developer/viewer 的 current/history/publish/rollback 共 8 个请求均为 403，匿名 current 为 401。跨租户同 ID、防枚举和 tenant-qualified 关联由上文自动化集成/迁移测试覆盖，不把自动化证据误写成该真实 API 批次执行过的请求。所有实际错误响应保持统一 code、request_id 和 trace_id。
- `policy_snapshot.published` 的 3 个有效 Webhook 事实、管理审计、请求日志和 Usage 均已核对；payload 只包含安全摘要。
- 真实 API 批次共 10 个日志文件、`186,070 bytes`。精确扫描确认管理员密码、Virtual Key、`sk-prd045-local-`、Prompt sentinel、96 位内部 Token、Authorization、Bearer、Traceback、真实 `level=ERROR` 和 5xx 命中均为 0；出现的 `token`/`prompt` 文本只属于已脱敏配置键、统一错误码和敏感扫描计数字段，不含对应值。验收 JSON 另确认所有运行期秘密在公开响应和数据库中的命中均为 0。
- 主验收完成 v1 allow、v2 deny、回滚 v1 形成 v3；cleanup 无错误并发布恢复快照 v4。最终证据库 head 为 v4，34/0034、`integrity_check=ok`、外键违规 0；2 个临时用户均 disabled、临时 Session active 数为 0，验收 Policy、Provider Key 和 Webhook 均已失活。

no-head、`route_to`、public stale、internal emergency、hard expiry、Fallback/SSE 中途刷新等故障边界由上文自动化完成，不把这些自动化结果误写成真实三服务逐项注入。结果未知精确重试与历史分页由 Control/Console 集成和单元测试覆盖，浏览器批次不重复伪造网络中断。

## 真实浏览器验收

最终成功批次：`20260718T111530`，使用真实 Chrome、运行库和三服务执行 `scripts/acceptance/prd045-browser-acceptance.mjs`。

- admin 从基线 v8 通过 Console 发布 v9，再回滚 v8 内容形成 v10；页面准确显示回滚后候选漂移，发布/回滚均要求原因和后端 CAS。
- 调用日志以 `snapshot_version=2` 查询，响应中所有行版本均为 2；DOM 行数、request_id 顺序和版本与响应完全一致。详情 API 与非空详情 DOM 同时关联目标 request_id/v2，且未出现 DSL、Prompt、Response 或 Authorization。
- developer/viewer 手写 `#policy-snapshots` 均回退到 overview；两者的导航、策略 DOM 和快照请求数均为 0。后端 current/history/publish/rollback 共 `8` 个唯一权限请求全部返回 403，证明前端隐藏不替代后端授权。
- 浏览器 `console_error`、`pageerror` 和非预期 HTTP 错误均为 0；临时资源和 Session 清理全部成功。
- 三档布局的 `scrollWidth/bodyScrollWidth` 均等于视口宽度，越界元素和裁切按钮均为 0，策略主面板均在视口内。截图已经遮罩身份区域并经目视检查，无重叠、裁切或身份泄露：

| 截图 | 尺寸 | bytes | SHA-256 |
| --- | ---: | ---: | --- |
| `artifacts/prd045/browser-desktop-1280x720.png` | 1280x720 | 82,848 | `7D217D55F35C0E8807E44CF5FA48B8E73D4F2E80C4AF1711879E512D1FA78931` |
| `artifacts/prd045/browser-intermediate-900x800.png` | 900x800 | 82,927 | `BA822F7F048ABE1844F9CE45D09CF02DF6D8FC0AE02818A5A0C60A6638ECF746` |
| `artifacts/prd045/browser-mobile-390x844.png` | 390x844 | 39,309 | `AF0EDB9FA25F86C2FC405104318D167FB4E1760A62CF6CE4193B6455C3A9BB06` |

成功批次的 8 个 `.log` 文件共约 360 KB；密码、临时密码、Authorization、Bearer、access token、Provider secret/key、DSL、Prompt/Response、Traceback、真实 `level=ERROR` 和 5xx 的扫描命中均为 0。日志中的 403 只来自上述预期权限断言。目录内较早失败批次保留用于复盘，不属于该零命中结论。

## 当前结论

PRD-045 的设计、两轮自审、实现、注释、租户隔离、后端 RBAC、统一错误/request_id、0034、专项与全量回归、真实 API/三服务、敏感扫描和三档浏览器验收均已通过。`TC-0089`、`TC-0090` 完整满足，PRD-045 已加入 README 已完成清单；下一条按 Excel 优先级进入 PRD-046 设计阶段。
