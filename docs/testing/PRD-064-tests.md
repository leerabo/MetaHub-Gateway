# PRD-064 Provider Adapter 契约测试框架测试记录

状态：**已完成并通过分层回归与实际运行验收**。记录日期：2026-08-01。

本记录区分专项证据、实际运行证据和最终回归边界。PRD-064 已满足设计、实现、权限、租户、
统一错误、迁移、测试、浏览器和运行验收要求；该结论只代表 PRD-064，全部 108 条 PRD 仍在进行，
`all_108_prds_complete=false`。

## 1. 环境与证据边界

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- 开发数据库：`D:/MetaHub/.data/metahub-dev-prd031.db`。
- Gateway / Control / Console：`http://localhost:8000`、`http://localhost:8001`、
  `http://localhost:5173`。
- pytest 最终执行必须使用 `-p no:cacheprovider`，并把 `TEMP`、`TMP` 和 `--basetemp` 全部设置到
  `D:/MetaHub/.artifacts/prd064-*`。
- Runtime、probe 配置、浏览器 profile、截图和脱敏证据只允许写入
  `D:/MetaHub/.artifacts/prd064-*`。
- 不访问 `D:/MetaHub/.tmp`，不使用 `C:/tmp`，不提权；局部问题只复测相关节点，最终门禁阶段再统一执行
  必要回归。
- Provider Key、Virtual Key、内部 Token、Endpoint、Prompt、Response、Authorization、完整 Header 和
  上游自由错误正文不得进入报告、日志、数据库、DOM、Storage 或验收 JSON。

可信部署绑定：

| 环境变量 | 用途 | 约束 |
| --- | --- | --- |
| `METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN` | 授权真实 probe 的 Gateway 根 origin | 必须与 probe JSON 中 `gateway_url` 完全同源；发送 Virtual Key 前先做无凭据健康身份预检 |
| `METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN` | 授权报告上报的 Control 根 origin | 必须与 `--control-url` 或 `METAHUB_CONTRACT_CONTROL_URL` 完全同源；发送内部 Token 前先做无凭据健康身份预检 |
| `METAHUB_CONTRACT_TRUSTED_TENANT_ID` | 绑定 probe、报告和 Control path 的可信 tenant | 必须与 probe 配置、Virtual Key 实际 owner 和报告 owner 一致，不能由报告正文自行授权 |
| `METAHUB_CONTRACT_TENANT_ID` | mock merge gate 的报告 tenant | 只影响离线报告 owner，不替代真实 probe 的可信租户证明 |

生产 origin 只允许 HTTPS；本地 HTTP 仅允许 loopback，并仍须显式配置
`allow_loopback_http=true` 或 wrapper 的 `-AllowLoopbackControlHttp`。真实 Key/Token 只从命名环境变量读取，
不能写入 probe JSON 或 CLI 参数。

## 2. 当前实现范围

- Gateway `provider_contracts` 已包含九类显式夹具、exact-set matrix、mock server、merge gate runner、
  canonical 报告、原子 `.artifacts` 写入、真实 Gateway probe、Control upload 和 CLI。
- Gateway OpenAI-compatible 入口已增加鉴权后的契约 tenant 证明；普通请求未携带证明头时保持原合同。
- Control 已注册内部上报、当前租户列表和详情 API，并复用后端 `provider.health.read` 实时权限。
- 0056 up/down migration 已落盘，覆盖 tenant-scoped run/case、失败事件 source/outbox 和 down guard。
- Console PAGE-091 已落地摘要、分段、筛选、分页、详情、权限裁剪、迟到响应淘汰和响应式布局。
- CI wrapper 已落盘，合同失败传播非零退出码；可选 Control 上报不改变原合同失败结果。

以上代码已由第 3、4、5 节登记的自动化与实际运行证据共同验收。

## 3. 已实际核验的阶段性证据

| 范围 | 命令或证据 | 实际结果 | 结论边界 |
| --- | --- | ---: | --- |
| 离线合并门禁报告 | `D:/MetaHub/.artifacts/prd064-wrapper-final/report.json` | `92 passed / 0 failed` | `mode=mock`、`suite_kind=merge_gate`、`status=passed`；只证明该次离线矩阵运行 |
| 报告目录身份 | 同上 | `adapter-spec-v1` / `9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870` | 与 PRD-063 冻结目录一致 |
| 报告文件完整性 | 同上 | SHA-256 `f32c2835d26cf3e938503e12ba5545d91be60b23669d41a0cb7cb8eadc2d3689` | 只用于复核当前阶段性文件，不代表最终权威批次 |
| 网关契约、报告、CLI 与安全边界专项 | PRD-064 Gateway 单元测试文件 | `41 passed / 0 failed` | 覆盖 exact-set、mock 矩阵、原子报告、CLI、可信 origin、服务身份预检、tenant owner 证明、probe/upload、429/5xx 与脱敏；只登记本次定向测试 |
| 网关直接受影响 ASGI 集成回归 | PRD-064 身份证明与普通请求兼容用例 | `4 passed / 0 failed` | 覆盖普通非流式/流式请求兼容、身份端点及错租户在 Provider 前拒绝 |
| Gateway 租户证明头单值加固 | `test_contract_probe_identity_proves_virtual_key_tenant_before_cases` | `1 passed / 0 failed`，`30.84s` | 定向复测证明重复不同值、重复相同值和代理逗号合并值均在 Provider 前按统一鉴权错误拒绝；普通单值证明合同保持不变 |
| 最终安全版离线合并门禁 | `D:/MetaHub/.artifacts/prd064-wrapper-secure-final/report.json` | `92 passed / 0 failed` | `coverage_complete=true`，SHA-256 `8ee6c59f7f88d788ef4619f70df34ba0323e25310e4c850bc74cc807e7fe03d3`；当前网关实现的权威离线报告 |
| 中文注释门禁 | `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe scripts/ci/check-comments.py` | `comment coverage ok` | 迁移幂等和匿名权限断言修改后再次执行通过，中文注释覆盖基线未被破坏 |
| PowerShell 语法检查 | CI wrapper 与 `scripts/ci/check.ps1` | 通过 | 证明两个脚本均可解析；行为和退出码由下一行的定向负例证明 |
| CI 阻断与副作用负例 | Gateway PRD-064 CLI/报告专项（包含在上述 `41 passed`） | 通过 | 按设计以内存注册表删除夹具、增加伪 spec，验证退出码 `2`；另覆盖合同失败 `1`、probe 配置失败 `3`、上报失败 `4`、路径越界拒绝与旧报告保留，全程未修改正式源码 |
| Console PAGE-091 单文件专项 | `node --test console-web/tests/unit/adapterContractRuns.test.mjs` | 最终 `15 passed / 0 failed` | 覆盖 403 销毁旧受保护状态、详情 403 复用完整清理、503 保留已净化历史页，以及恢复到 200；TEMP/TMP 均指向 `D:/MetaHub/.artifacts` |
| PAGE-091 浏览器验收器纯合同 | `node --test console-web/tests/unit/prd064BrowserAcceptance.test.mjs` | `10 passed / 0 failed` | 验证固定 Origin/证据根、列表/详情 DTO owner、真实 Control cursor 分页代理、导航权限、403/503、双视口、隐私、PNG 与注销源码门禁；该测试不启动浏览器 |
| PAGE-091 相关组合专项 | `node --test console-web/tests/unit/adapterContractRuns.test.mjs console-web/tests/unit/prd064BrowserAcceptance.test.mjs` | `22 passed / 0 failed` | 当前页面合同与验收器假设共同通过；不能替代真实 Chrome/Edge 和截图证据 |
| 开发库 0056 实迁移与只读完整性复核 | 备份 `D:/MetaHub/.artifacts/prd064-runtime/20260801T125516/metahub-dev-before-0056.db`；SQLite `mode=ro` + `PRAGMA query_only=ON` | 备份 `55` 条、最高 `0055`；当前 `56` 条、最高 `0056`；两者均 `integrity_check=ok`、`foreign_key_check=[]` | `0056` checksum 为 `ce8e6ebe12ca6c8853e611f7ce5198d2bad5324f12b53819d344d1cae98eba41`；本次复核未写入共享开发库 |
| 三服务健康检查 | Gateway `/health`、Control `/health`、Console `/` | `200 / 200 / 200` | 2026-08-01 对现有运行进程执行无凭据只读检查，未启动或重启服务 |
| 受控真实 Gateway probe | `D:/MetaHub/.artifacts/prd064-runtime/20260801T125516/gateway-probe-acceptance-r4/evidence.json` | 两类 Adapter：非流式 `2/2`、SSE `2/2`；429 与 503 负例均按预期返回失败报告 | 无凭据 `/health` 身份预检通过；成功、失败、usage、routing attempt、stream session 均有 request_id/trace_id 追踪 |
| Probe 隐私与清理 | 同上 | `authorization/gateway_origin/prompt/provider_origin/virtual_key` 命中均为 `0`；`gateway/stub` 已停止，配置与隔离运行库已删除 | 扫描覆盖 probe 报告、CLI 输出及隔离 Gateway 日志；Control/Console/浏览器证据仍需最终总扫描 |
| Outbox 恢复与事务边界专项 | 2 个未重复的 PRD-064 Control 定向用例 | `2 passed / 0 failed` | 覆盖过期 lease 接管、旧 owner CAS、第十次孤儿终结，以及事件 source 写入失败时 run/cases/outbox 整体回滚并返回统一 503 |
| Control API、租户权限与 Webhook 恢复审计节点 | `test_internal_ingest_auth_strict_json_idempotency_and_catalog`、`test_read_permissions_tenant_isolation_and_failed_event_outbox`、`test_contract_failed_webhook_pending_dead_letter_and_replay` | 当前版本 `3 passed / 0 failed`，`91.48s`；补匿名断言后相关用例 `1 passed / 0 failed`，`43.71s` | 覆盖内部鉴权/严格 JSON/幂等、跨租户冲突与 404、Developer 可读、Viewer 403、匿名 `auth.token_missing/401`、列表/详情 SQLite 故障统一 503，以及 source pending、同一 delivery 恢复、最终失败生成 dead letter 和后端权限重放成功；仅为隔离 ASGI/SQLite 自动化证据，不替代第 5 节真实服务验收 |
| 0056 迁移专项 | `test_prd064_db_migrations.py` 2 个用例；新增幂等断言后定向复测首个用例 | `2 passed / 0 failed`；复测 `1 passed / 0 failed` | 覆盖 fresh、重复执行、空事实 down/re-up、owner/不可变、业务事实 down guard、完整性与外键；全部使用 `.artifacts` 隔离库 |
| Control 真实 HTTP/RBAC/租户/Outbox | `D:/MetaHub/.artifacts/prd064-runtime/20260801T125516/control-plane-acceptance-r1/evidence.json` | 通过；SHA-256 `62b779cc56141dff854b0c3db6dff45bf17ffcc486afbf2ad6992579cd6df9ca` | 覆盖 admin/developer、viewer 403、匿名 401、第二租户同形 404、幂等/冲突、失败事件 pending/恢复及隐私零命中 |
| Gateway 实际 probe | `D:/MetaHub/.artifacts/prd064-runtime/20260801T125516/gateway-probe-acceptance-r4/evidence.json` | `4/4`；SHA-256 `cf8fd3b290e83c6b94eb88b15c17f689181ff01ed7faad714b614e35fce3223d` | 两类 Adapter 的非流式、SSE、429/503、Usage、失败追踪和隔离资源清理均通过 |
| 真实 Edge 双视口 | `D:/MetaHub/.artifacts/prd064-browser-runtime/run-20260801T081846Z/runtime-evidence.json` | 通过；SHA-256 `271f5b932b05bb8f63df5a4aceff81a7d0e6c9efe5b5c627c4f58a43c46e06c9` | 1280x720、390x844 两张非空 PNG；分页、详情、空态、403、503、恢复、注销、DOM/URL/Storage/Console 隐私和清理均通过 |
| 登录日志脱敏专项 | 成功与失败登录日志用例 | `2 passed / 0 failed` | 成功不记录邮箱；失败不记录邮箱或攻击者输入，已知账号只允许安全 `actor_id` |
| Node 示例 / Console lint | Node 示例全量；Console lint | `33/33`；`175 files` | 两项均通过，未把 lint 当作浏览器或功能测试替代品 |
| 中文注释最终门禁 | `scripts/ci/check-comments.py` | `comment coverage ok` | SQLite 并发恢复关键分支补充中文业务约束后再次通过 |

PAGE-091 单文件专项已覆盖固定筛选白名单、tenant/敏感字段剥离、详情 owner、畸形成功失败关闭、渲染、
XSS、viewer 无 DOM/预取、分页生命周期、顶部摘要不受筛选/后页污染、身份与 generation 漂移、只读 GET、
`adapter.contract_fixture_unexpected` 合法失败记录以及移动端 CSS。该结果不能代替 Console 全量、真实导航
或浏览器验收。

`scripts/acceptance/prd064-browser-acceptance.mjs` 已把 profile、TEMP/TMP、下载、截图和 JSON 固定到
`.artifacts/prd064-browser/<run-id>` 的全新普通目录，并包含真实表单登录、后端导航 owner、默认列表、摘要、
状态/模式/suite、详情、真实空态、一次性 403/503 投影与恢复、注销、双视口布局、PNG 像素及
Network/DOM/URL/Storage/Console 隐私门禁。分页步骤不会要求向共享库写入 51 条重复事实：页面先完成生产
`limit=50` 真实读取，随后验收器仅在专用步骤把同源 Control 读取缩为 `limit=1`，由后端真实 cursor 驱动
下一页和上一页，响应内容不由浏览器伪造。真实 Edge 已按该合同执行并由第 5 节登记。

## 4. 自动化门禁完成情况

- [x] Gateway merge gate/报告/CLI 全专项：九类 exact-set、缺失/幽灵/重复 fixture、非流式、SSE、401、
  429、5xx、timeout、Usage、原子写入、退出码和报告脱敏。
- [x] Gateway probe/upload 专项：受信 origin 漂移、错误服务身份、Virtual Key owner 租户证明、重复证明头、
  同源最终响应、Control token、probe 429/5xx 和固定错误码。
- [x] Gateway 直接受影响集成回归：普通 OpenAI-compatible 请求不携带专用头时保持兼容；错租户在
  Provider 前拒绝且失败请求可追踪。
- [x] Control API/Outbox 专项：内部 Token、正文上限、重复 JSON/query/header、严格 case 子集、目录
  exact-set、幂等/冲突、跨租户 run ID、实时权限、列表/详情数据库故障 503、事务整体回滚。
- [x] `adapter.contract_failed` 的成功投递、pending、lease 接管、退避、dead-letter 与第十次孤儿 lease
  终结；恢复时保留首次 event/request/trace/payload。
- [x] 0056 migration：fresh、重复执行、0055 -> 0056、空事实 down/re-up、owner/FK/唯一性/计数/状态/
  immutable trigger、业务事实 down guard、`integrity_check=ok` 和 `foreign_key_check=[]`。
- [x] Console PAGE-091 相邻页面、导航、owner、权限、403/503、注销及 lint；真实浏览器见第 5 节。
- [x] `scripts/ci/provider-adapter-contract-test.ps1` 成功路径与故意破坏 fixture 的非零阻断证明；输出路径
  越界、报告原子替换和可选上报失败退出码。
- [x] `scripts/ci/check-comments.py` 中文注释门禁。
- [x] 按最终变更面执行 Python/Node/Console 分层回归；第 6 节如实保留超时、首次失败和排除边界，
  不虚写为一次全量零失败。

## 5. 实际运行验收

- [x] 备份并把开发库实际升级到 0056，登记 migration count/max/checksum、完整性和外键结果。
- [x] Gateway、Control API、Console 最新源码健康检查均为 HTTP 200。
- [x] 本地无日志 stub 下执行 mock 和受控真实 Gateway probe，覆盖至少两种 Adapter family、非流式、SSE、
  429/5xx 和 Usage；确认凭据发送前服务身份预检通过。
- [x] 内部上报后，PAGE-091 当前租户可见；viewer 后端 403、匿名 401、第二租户对同一 run ID 返回同形 404。
- [x] 失败 run 产生一次 `adapter.contract_failed`，并实际验证投递失败后的 pending/dead-letter 恢复链路。
- [x] 1280x720 与 390x844 真实 Edge 验收：摘要、筛选、分页、详情、403/503、空态、注销、无重叠和
  无横向溢出；截图像素非空。
- [x] 扫描 runner/三服务日志、报告、数据库、DOM、URL、Storage 和浏览器证据，敏感 canary 命中为 0。
- [x] 停止 Gateway probe 验收进程，确认隔离 Gateway/stub、probe 配置、运行库和临时账号资源均已清理。

## 6. 最终回归与失败闭环

### 6.1 Python 根级运行事实

- 根级 Python 回归只执行一次，一小时超时前运行到 `1008/1845`，已经出现 12 个失败；该次运行没有
  完成，不能登记为全量通过。
- 12 个失败映射到 4 个文件后只复测这些受影响节点，JUnit
  `D:/MetaHub/.artifacts/prd064-final-regression/20260801T1645/python-failures-junit.xml`
  为 `37 tests / 0 failures / 0 errors`，SHA-256
  `130593bbcc1782176862a71d2eb18969e9a15634d8165e0f2f9d70da6e2952d6`。
- 为覆盖超时后的剩余文件，从第 1009 项所在文件开始执行后缀回归；JUnit
  `python-suffix-junit.xml` 为 `847 tests / 10 failures / 0 errors`，SHA-256
  `3b3671428bc9fb037bf737988c7daa8b8929961aec76ed95e8586b72b7d25211`。不得把它改写成全绿。

后缀 10 个失败按根因闭环：

1. PRD-056 旧测试把全局迁移头冻结为 `0049`，已更新为当前 `0056`，对应 fresh migration 节点
   `1/1` 通过；测试工件路径同时迁移到 `.artifacts`。
2. Feedback 三个真实并发节点曾把 SQLite 写锁竞争映射成 `feedback.unavailable`；现在保持首次
   `250ms` 写锁预算，关闭旧连接后只读轮询并按 operation/source 重分类。replay、
   `feedback.request_conflict`、`feedback.already_exists` 和持续外部锁四节点 `4/4` 通过。
3. `gatewayctl` PowerShell 包装器在沙箱冷启动超过旧 15 秒；测试进程预算改为 60 秒且不改变产品网络
   超时，两条真实入口节点 `2/2` 通过。
4. 其余 5 项是 PRD-050 旧静态证据测试，硬编码读取用户明确禁止访问的 `.tmp` 临时脚本。正式仓库没有
   可替代的可信成功副本，失败制品也不能冒充验收证据，因此本轮不访问、不运行、不计为通过，并按用户
   的目录边界从 PRD-064 回归范围排除。

### 6.2 Console 与其他门禁

- Console 全量首次为 `555 passed / 1 failed`；唯一失败是 PAGE-072 旧源码定位断言，按当前专属重绑
  位置修正后目标文件 `18/18` 通过。没有虚写为新一次 `556/556` 全绿。
- Node 示例 `33/33`；Console lint `175 files`；PAGE-091 最终专项 `15/15`；浏览器合同 `10/10`。
- 登录日志脱敏专项 `2/2`；中文注释最终门禁 `comment coverage ok`。

## 7. 最终结论

PRD-064 的九类 Adapter exact-set、mock 合并门禁、真实 Gateway probe、Control 内部上报、后端 RBAC、
租户隔离、统一错误与 request_id/trace_id、失败事件恢复、0056 迁移、PAGE-091、双视口 Edge、隐私扫描、
资源清理和必要分层回归均已通过。2026-08-01 最终只读复核为迁移 `56/0056`、
`integrity_check=ok`、外键违规 `0`，Gateway/Control/Console 为 `200/200/200`。

因此 PRD-064 可以标记完成。该结论不表示根级 Python 曾取得一次无排除的全绿运行，也不把 5 个被目录
边界排除的 PRD-050 静态证据测试算作通过；全部 108 条 PRD 仍在进行，
`all_108_prds_complete=false`。
