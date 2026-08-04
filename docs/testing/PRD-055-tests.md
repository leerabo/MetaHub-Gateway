# PRD-055 Trace查询与过滤增强测试记录

测试日期：2026-07-28 至 2026-07-29

当前状态：已完成。设计、两轮编码前自审、实现、两轮实现后自审、0045 正反迁移、
专项测试、一次 Python 根级全量、唯一失败 node 定向闭环、Node/Console/lint/中文注释门禁、
真实 Runtime、真实 Browser 及停服静止终态审计均已完成，PRD-055 可以标记完成。
这只表示 PRD-055 完成，不代表全部 108 条 PRD 已完成；总体目标仍在进行中。

运行环境：Windows PowerShell；Python 使用
`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
pytest 的 `TEMP`、`TMP` 和所有 `--basetemp` 只允许位于 `D:/MetaHub/.tmp`；
不使用 `C:/tmp`、不提权。清理只能删除 `.tmp` 内 pytest 链接节点，必须保留链接目标、
普通目录、补丁和测试证据。

## 1. 验收口径

- Excel 权威项：`PRD-055 - Trace查询与过滤增强`，`M1-企业POC / P1`。
- 页面/API/对象/事件：`PAGE-080`、`GET /request-logs?filters=`、
  `SavedFilter/RequestLog`、`log.query_executed`。
- `TC-0109`：高级筛选、稳定排序、有界分页、保存筛选复用及解释中心跳转；万级日志分页
  响应可接受且筛选准确。
- `TC-0110`：不引入全文搜索引擎；覆盖查询性能、权限、统一错误码、敏感信息不泄露和
  状态可追踪。
- admin/developer 可以使用 PAGE-080；anonymous 401、viewer 403，且后端 RBAC 先于
  filters、path 和 JSON 业务校验。
- 所有查询以可信 Session tenant 为第一归属条件；SavedFilter 还必须绑定当前 owner，
  同租户其他用户和 admin 也不能读取个人筛选。
- 成功与失败均保留统一 operation `request_id/trace_id`。Prompt、Response、Provider Key、
  Virtual Key 明文、Token、Authorization、原始异常和筛选值不得进入日志、事件、URL 或 Storage。

完整口径见 [PRD-055 技术设计](../prd/PRD-055-trace-query-enhancement.md) 和
[调用日志高级查询 API](../api/request-log-query.md)。

## 2. 设计与自审

- 设计文档先于编码落盘，两轮编码前自审关闭了兼容输入、稳定分页、tenant/owner、RBAC
  优先级、source outbox、隐私和可运行性能阈值等 P0/P1。
- 实现后第一轮复核查询 SQL、0045、事务和 outbox，最终采用
  `WITH matched AS MATERIALIZED` + `COUNT(*) OVER()`，并把 SavedFilter 唯一键统一为
  trim + NFKC + casefold。
- 实现后第二轮复核权限、Console 生命周期和 Browser 真实性，修正 PAGE-080 周期刷新竞态
  及闭合 details 后代的可见性误判。
- 两轮实现后自审结论只证明实现范围内无未关闭 P0/P1，不替代最终一次全量与静止终态。

## 3. 测试资产与覆盖

| 范围 | 主要文件 | 覆盖 |
| --- | --- | --- |
| Shared | `shared-sdk/python/tests/test_prd055_trace_query_contracts.py` | 错误码、i18n、Webhook exact payload/信封、敏感键门禁 |
| Control core | `control-plane-api/tests/unit/test_prd055_trace_query_core.py` | filter codec、规范化、排序、分页、阶段推导、SQL 合同 |
| Control API | `control-plane-api/tests/integration/test_prd055_trace_query_backend.py` | 三种查询来源、RBAC、租户、owner、SavedFilter CRUD、故障关闭 |
| QueryEvent | `control-plane-api/tests/integration/test_prd055_query_event_outbox.py` | 不可变事实、lease/CAS、恢复、次数上限、隐私 |
| 迁移 | `control-plane-api/tests/integration/test_db_migrations.py` | 0045 up/down、表/索引/触发器、owner、状态机和 down guard |
| 隐私 | `control-plane-api/tests/unit/test_prd055_access_log_privacy.py` | access log 不记录 query、筛选值和敏感正文 |
| Runtime 合同 | `test_prd055_runtime_acceptance_contract.py`、`scripts/acceptance/tests/test_prd055_runtime_acceptance.py` | 批次路径、数据库、角色、性能、事件和清理证据合同 |
| Console | `console-web/tests/unit/requestLogs.test.mjs` | PAGE-080 状态、SavedFilter、owner、迟到响应、响应式布局 |
| Browser 合同 | `console-web/tests/unit/prd055BrowserAcceptance.test.mjs` | Runtime 前置、双视口八状态、三角色、网络、截图和清理 |

Browser 验收器最后一次定向合同复验为 `11/11 passed`，覆盖周期刷新竞态、闭合 details
可见性和布局 anchor 修正。其他聚焦批次不与最终计数相加，最终冻结源码上的直接结果如下：

| 范围 | 直接结果 | 证据与说明 |
| --- | --- | --- |
| Python 根级全量 | `1364 = 1363 passed / 1 failed / 0 errors / 0 skipped`，`4489.857s` | [全量 JUnit](../../artifacts/prd055/20260728T134456/python-full-junit.xml)；完整执行一次，不虚写为全量零失败 |
| 原失败 node 定向闭环 | `1/1 passed`，`10.044s` | [定向 JUnit](../../artifacts/prd055/20260728T134456/python-failed-node-junit.xml)；仅重跑原失败 node，没有重跑整套全量 |
| Node 示例全量 | `33/33 passed` | `logs/node-examples-full.stdout.log`，失败、跳过、取消和 todo 均为 0 |
| Console 全量 | `433/433 passed` | `logs/console-full.stdout.log`，失败、跳过、取消和 todo 均为 0 |
| Console lint | `140 files passed` | `logs/console-lint.stdout.log` |
| 中文注释门禁 | `passed`，`comment coverage ok` | `logs/comments-check.stdout.log` |

Python 全量唯一失败为 `test_published_deny_claims_version_and_never_calls_provider`。该测试误用
生产默认的 `5ms` 策略求值超时，使断言在高负载全量环境中得到
`gateway.policy_evaluation_timeout`，而不是预期的 `gateway.policy_denied`；这不是 PRD-055
生产逻辑回归。修正仅限测试配置，把该用例的策略超时设置为 `100ms`，随后原失败 node
`1/1 passed`。最终状态记为 `passed_with_targeted_closure`，不得表述为重新取得一次
`1364/1364` 全量通过。

## 4. 真实 Runtime

权威批次：`artifacts/prd055/20260728T134456`。

[Runtime 证据](../../artifacts/prd055/20260728T134456/runtime-acceptance.json) 为
`status=passed`，SHA-256：

`8CC8F6268A11DBFD2B634A0CEE647C2E03049C280969B6D5441DCB6593014143`

直接证明：

- 隔离运行库已应用并校验 0045，所需表、索引、触发器、checksum、integrity 和外键检查通过。
- Gateway、Control API、Console 三服务健康检查均为 HTTP 200。
- 100000 条性能 fixture 分布于两个 tenant；同 request ID 跨租户仍隔离。
- 20 次正式样本排除 warmup，P95 `491 ms`、最大 `510 ms`，满足 P95 500 ms、
  单次 1500 ms 的本地阈值；该结果不冒充生产容量承诺。
- `EXPLAIN QUERY PLAN` 使用 trace/provider/latency tenant-first 索引，拒绝全表扫描。
- inline v1、legacy 兼容、混合来源拒绝、排序白名单和有界分页均通过。
- admin/developer 成功；anonymous 401、viewer 403，权限在畸形 filters 解析前执行。
- SavedFilter 创建、列出、应用、更新、版本冲突、软删除、同租户 owner 和跨租户隔离通过。
- 26 次成功查询形成不可变 QueryEvent，筛选值不进入事件，source outbox 已派发且尝试有界。
- Runtime 创建的验收 Session 均已 revoked。

## 5. 真实 Browser

[Browser 证据](../../artifacts/prd055/20260728T134456/browser-acceptance.json) 为
`status=passed`，SHA-256：

`967DB05A0CA8ED74D561D85798A93545DCF0132CF9D2B6CAA6E4A8196E608754`

直接证明：

- 1280x720 与 390x844 均覆盖 success、empty、真实统一 422、SavedFilter，共 8 张正式 PNG。
- 每个状态横向溢出、控件裁切、内容裁切和重叠均为 0；桌面为 compact-grid，移动端为
  single-column。
- 两档视口均验证 SavedFilter 创建、选择零查询、显式应用、服务端权威条件、第二页、
  version 2 更新、解释 handoff 和软删除。
- admin/developer 可查询；viewer 无导航、手写 hash 被清理、无受保护 DOM，
  RequestLog prefetch 为 0。
- RequestLog GET 28 次、SavedFilter 请求 34 次、Explain GET 2 次；blocked request 为 0。
- Browser error、Console error 均为 0；2 个预期 422 只作为预期 HTTP Console 事件登记。
- request ID 持久化命中、密码暴露命中均为 0；三角色退出后 DOM、Storage 和 request ID 清空。
- 两个 Browser 临时用户均为 disabled，所有匹配 Session 均为 revoked。
- 内置 Browser 页面环境不暴露 Resource Timing，因此没有虚构资源时序结论；零预取结论
  由实际 RequestLog 网络计数、DOM/导航、后端 403、Session 状态和源码合同共同证明。

正式截图及 SHA-256：

| 状态 | 文件 | SHA-256 |
| --- | --- | --- |
| desktop success | `success-desktop-1280x720.png` | `2431E0424F88CEDDD206F97ED491A685842924258607F1A8836D0E98D37547D9` |
| desktop empty | `empty-desktop-1280x720.png` | `194F7D0141B49C96523B85C809A5416F71626A703942B9D95DAE0A8FF0B04EF2` |
| desktop error | `error-desktop-1280x720.png` | `90618599F120113A2C3D8E3B04EEF7588FF4A4FD55A6BE0CEF2050BA2CAA0DCE` |
| desktop SavedFilter | `saved-filter-desktop-1280x720.png` | `6B5D0044B1AA2B2EC106F6380CEEA20B7FE773C99F5A9C205C435E40AA2AEDA9` |
| mobile success | `success-mobile-390x844.png` | `5914146FD7BA839E246A2A2208863802F0B6123765C8D742353C04FFF2E1DBBE` |
| mobile empty | `empty-mobile-390x844.png` | `C5CAFEE37A863797D770EDE8B9287D87ADE18BE08EBC6390A2F9A0BFAD2EDEED` |
| mobile error | `error-mobile-390x844.png` | `04783B7A80F5CF7E80AA89A4C598B25CDBC964540434B6DC5F8F8E0BE8A3ACFF` |
| mobile SavedFilter | `saved-filter-mobile-390x844.png` | `5809CBFAC0C701A875ABAE7CAA95A7084F3DF57A8CA26AB13F39F67E5637AC4C` |

## 6. 最终一次全量与静止终态

### 6.1 全量失败闭环

- Python 根级全量只执行一次，真实分布为 `1363 passed / 1 failed`；唯一失败按原 node ID
  定向闭环为 `1/1 passed`，没有因局部测试超时配置问题重新执行 1364 项全量。
- Node 示例 `33/33`、Console `433/433`、Console lint `140 files` 和中文注释门禁均通过。
- 全量与定向 JUnit、stdout/stderr 均保存在权威批次中；失败批次没有被删除或
  改写，能够审计原始失败、修正边界和定向通过结果。

### 6.2 数据库、事件与身份终态

[数据库终态](../../artifacts/prd055/20260728T134456/database-final.json) 为 `status=passed`：

- 共 `45` 条迁移，最高版本 `0045`，无 non-applied 迁移；0045 loader checksum 与权威文件一致。
- `wal_checkpoint(TRUNCATE)=[0,0,0]`、`integrity_check=ok`、外键违规 `0`；停服关闭连接后
  `-wal/-shm/-journal` 均不存在，5173/8000/8001 均无监听。
- SavedFilter 终态为 `14 deleted / 0 active`，非法生命周期 `0`。
- QueryEvent 共 `258 dispatched`，`attempts_min=attempts_max=1`，无无效 actor/session；
  Request Explain outbox 另有 `4 dispatched`。
- PRD-055 Session 为 `16 revoked / 0 active`；Browser 临时用户为
  `8 developer:disabled + 8 viewer:disabled`，即 `16 disabled / 0 non-disabled`。
- spool 的 pending/processing/quarantine 均为 `0`。

### 6.3 日志隐私与 pytest 链接清理

[日志终态](../../artifacts/prd055/20260728T134456/logs-final.json) 对 `44` 个正式或历史诊断日志
逐文件扫描；密码、Bearer/MetaHub Token、Provider Key、Prompt/Response JSON、生成密码和
私有 fixture 的敏感命中均为 `0`。

[pytest 链接清理摘要](../../artifacts/prd055/20260728T134456/pytest-link-cleanup.json) 与
[逐节点明细](../../artifacts/prd055/20260728T134456/pytest-link-cleanup-details.json) 证明：

- 仅删除 `502` 个链接节点，其中 pytest `current` 链接 `498`、fixture 链接 `4`。
- `502/502` 解析目标均保留，missing target `0`；普通目录删除 `0`。
- 未使用递归删除、`C:/tmp` 或提权；剩余可见 ReparsePoint 为 `0`。
- 补丁、普通目录、目标目录和测试证据均未作为清理对象。

### 6.4 权威哈希与总审计

- Runtime：`8CC8F6268A11DBFD2B634A0CEE647C2E03049C280969B6D5441DCB6593014143`。
- Browser：`967DB05A0CA8ED74D561D85798A93545DCF0132CF9D2B6CAA6E4A8196E608754`。
- [最终审计](../../artifacts/prd055/20260728T134456/final-audit.json)：
  `55F5EAA68D07E04F396C8E5728C6597AEB89117058990A7706E5904E5C9097DE`，
  `status=passed`，并明确 `all_108_prds_complete=false`。

## 7. 当前结论

PRD-055 的设计、两轮编码前自审、实现、两轮实现后自审、0045、专项测试、一次 Python
根级全量及唯一失败定向闭环、Node/Console/lint/中文注释、真实 Runtime、真实 Browser、
租户与权限、统一错误和 request_id、数据库迁移、隐私、资源清理及停服静止终态均已通过，
PRD-055 可以标记完成。

该完成结论只适用于 PRD-055；`all_108_prds_complete=false`，全部 108 条 PRD 的总体目标仍在
进行中。后续 PRD 仍须按 Excel 优先级逐条执行完整设计、自审、实现、测试和运行验收流程。
