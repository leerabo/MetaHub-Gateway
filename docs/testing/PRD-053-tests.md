# PRD-053 成本异常检测基础测试记录

测试日期：2026-07-27

当前状态：设计、两轮编码前自审、实现、分层测试、真实 Runtime/Browser 与停服静止终态验收
均已完成，PRD-053 可标记完成。本记录严格区分一次根级全量、原失败定向闭环和权威运行证据，
不把未直接执行的组合改写为通过，也不代表全部 108 条 PRD 已完成。

运行环境：Windows PowerShell；Python 使用
`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
pytest 临时目录均限定在 `D:/MetaHub/.tmp`，每批使用独立 `--basetemp`；长路径复现后的定向重跑使用短 `sNNNNN` 目录。
测试没有使用 `C:/tmp`、沙箱外目录或提权方案。

## 1. 验收口径

- Excel `TC-0105`、`TC-0106`：模拟成本异常必须生成可解释告警；误报可 dismiss，重复扫描和
  并发 worker 不得生成重复事实，临时限流必须在 Provider 前生效并可释放/过期恢复。
- 检测只读取正式 FinOps `posted + debit + usage_charge` 和可信 CNY 证据；unknown、未定价、
  非 canonical 金额或 owner 不完整必须形成 partial/failed，可见但不能伪装成无异常。
- 5 分钟窗口、过去 24 小时固定 288 桶基线、5 倍门限、最低 10,000 micros、非工作时段和
  30 分钟滚动冷却均使用整数/明确时区合同，不引入机器学习判定。
- GET/PATCH 均为 admin-only 后端权限；匿名 401，developer/viewer 403。tenant、actor 和时间只
  从 Session/服务器恢复，跨租户资源同形拒绝。
- API 必须执行严格 query/header/JSON 校验，成功和错误均保留统一 `request_id/trace_id`，读取
  响应使用 `Cache-Control: no-store`。
- 临时限流只适用于 Virtual Key anomaly，只能逐维收紧永久规则与其他有效覆盖；限流 429 必须
  发生在 Provider 前并证明 Provider attempt 为 0。
- `cost.anomaly_detected` 只包含精确 20 字段安全 payload，使用稳定 event ID、持久 source
  outbox 和 at-least-once pending resume，不泄露 Key、Token、Prompt、Response 或远端正文。

完整设计与公开接口合同见 [PRD-053 设计文档](../prd/PRD-053-cost-anomaly-detection-foundation.md)
和 [成本异常检测 API](../api/cost-anomalies.md)。

## 2. 设计与两轮编码前自审

设计文档先于编码落盘，两轮自审结论已写入设计文档。第一轮聚焦检测口径、正式账本、迟到
分录、冷却去重、事务和状态机，冻结了固定 288 桶、watermark、run lease/CAS、partial/failed
和 source outbox。第二轮聚焦 RBAC、租户/Environment/Key 归属、严格 wire、临时覆盖收紧、
Webhook 恢复、隐私和 Windows 连接生命周期。

编码前已修正的关键边界包括：

- 基线为包含零消费桶的连续 24 小时，不按活跃桶改变分母；基线为零时 spike 不触发。
- 迟到正式分录按原业务时间和新 ledger sequence 重评，但已有 anomaly 证据不可改写。
- 精确窗口唯一键与 30 分钟滚动冷却分离；冷却使用业务 `window_start`，不使用处理时刻。
- 租户级与逐 Key 完整性分层，坏 Key 不能污染其他可信 Key，也不能让整次运行伪装成功。
- 临时限流由 admin 显式动作创建，只能收紧并保持独立覆盖；到期/释放不改写永久规则。
- 检测事务不出网；Webhook 恢复只复用数据库内已持久正文和 attempt 状态。

第二轮结束时设计文档无未关闭 P0/P1 问题；这只表示允许编码，不等于实现和运行验收通过。

## 3. 证据分级与环境约束

最终记录区分三类证据：

1. **直接测试结果**：当前源码在本机实际执行并得到退出码 0 或 JUnit 零失败的测试。
2. **权威运行证据**：只采用 `artifacts/prd053/20260727T183515` 中 `status=passed` 的 Runtime、Browser 与静止终态 JSON。
3. **诊断证据**：失败启动日志和 `screenshots-diagnostic-attempt-*` 仅用于解释修复过程，不计入正式通过数量。

所有 pytest `TEMP`、`TMP` 和 `--basetemp` 均位于 `D:/MetaHub/.tmp`。Windows 长路径复现后，
spool 单点改用短 `D:/MetaHub/.tmp/sNNNNN` basetemp；未使用 `C:/tmp`、沙箱外目录或提权。
最终只删除 `.tmp` 内 483 个 SymbolicLink/Junction 节点，未使用递归删除，483 个目标目录全部保留。

## 4. 最终直接测试结果

| 范围 | 结果 | 直接覆盖 |
| --- | --- | --- |
| Shared 成本异常合同 | `104/104 passed` | 六项有界配置、14 个统一错误码、中英文标签、精确 20 字段事件/金额/隐私合同 |
| Webhook pending resume | `6/6 passed` | 持久 intent 恢复、attempt 递增、Secret CAS、disabled Endpoint、订阅快照 |
| 既有 Webhook 相邻回归 | `8/8 passed` | 默认不恢复 pending 的旧创建、投递和重试行为保持兼容 |
| Browser 验收器合同 | `15/15 passed` | Runtime 前置、双视口、五状态、RBAC、统一错误、截图/hash、Session 清理 |
| Node 示例 | `33/33 passed` | OpenAI-compatible 示例及错误处理保持兼容 |
| Console | `401/401 passed` | PAGE-077/078、状态、owner、动作、布局及既有控制台回归 |
| Console lint | `134 files passed` | 当前 Console 源码和验收脚本静态门禁 |
| Python 根级全量 | `1178 passed, 30 failed in 3798.69s` | 全量已完整执行一次；30 个失败均为环境路径或历史测试合同，不虚写为全量零失败 |
| 失败闭环定向复验 | `34/34 passed` | 3 个非迁移节点、1 个 PRD-047 roundtrip、PRD-050/051/052 三个完整迁移文件 30 个实例 |
| 中文注释门禁 | `comment coverage ok` | `scripts/ci/check-comments.py` 全仓通过 |
| Runtime / Browser | `status=passed / status=passed` | 真实三服务、Webhook、429/恢复、三角色、双视口和 10 张正式 PNG |

主要专项文件：

- `shared-sdk/python/tests/test_prd053_cost_anomaly_shared_contracts.py`
- `control-plane-api/tests/integration/test_prd053_{db_migrations,cost_anomalies,webhook_resume}.py`
- `control-plane-api/tests/unit/test_prd053_cost_anomaly_detector.py`
- `gateway-data-plane/tests/unit/test_prd053_temporary_rate_limit_overlay.py`
- `console-web/tests/unit/costAnomalies.test.mjs`
- `scripts/acceptance/tests/test_prd053_runtime_acceptance.py`
- `scripts/acceptance/tests/test_prd053_browser_acceptance.mjs`

## 5. 主流程、异常、权限与租户隔离

### 5.1 数据库、检测器与调度

- 0043 正向/反向、重复 apply、0042 往返、恶意 INSERT/UPDATE、owner/immutable/CAS、down guard 和 Audit 保真均由迁移专项及 Runtime 数据库验证。
- 检测器覆盖固定 5 分钟窗口、连续 24 小时 288 桶基线、租户/Key spike、非工作时段、门限等值不触发、基线为零、迟到 ledger watermark、冷却去重和并发 lease 失权。
- unknown、REAL、负值、越界和 owner 不完整进入 partial/failed；坏 Key 不污染可信 Key。detector 执行 2 次并形成 3 条 anomaly，终态为 2 open、1 mitigated。
- source outbox 覆盖 claim、退避、最多 10 次、稳定 event ID、部分成功恢复和 pending intent 续投。

### 5.2 Control API 与权限

- `GET /cost-anomalies` 覆盖筛选、分页、签名 cursor、summary、`Cache-Control: no-store` 和统一 `request_id/trace_id`。
- `PATCH /cost-anomalies/{id}/action` 覆盖 dismiss、acknowledge、apply temporary limit、release、24 小时窗口、严格状态转换、幂等 replay/conflict 和 Audit 同事务。
- 匿名返回 401；developer/viewer 由后端返回 403；admin 成功。畸形 query/header/body 不抢在 RBAC 前暴露合同。
- tenant/actor 只从 Session 恢复；跨租户 anomaly、Environment、Key 同形拒绝，失败事务不残留 action、override、Audit 或成功事件。

### 5.3 Gateway、Webhook 与 Console

- Gateway 对永久规则和所有有效临时覆盖逐维取最小值；真实覆盖下在 Provider 前返回统一 429，Provider attempt 为 0，release 后恢复 HTTP 200。
- `cost.anomaly_detected` 使用精确 20 字段安全 payload、稳定 event ID 和 HMAC；Provider Key、Virtual Key、Session Token、Prompt、Response 均不进入证据。
- PAGE-077/078 覆盖列表、告警卡、详情、筛选、cursor、动作对话框、幂等恢复和迟到响应 owner。
- developer/viewer 无导航、无受保护 DOM、无目标 API 预取；admin 动作可见，服务端字段不能由表单覆盖。

## 6. 全量回归与失败闭环

Python 根级全量只执行一次，原始结果为：

```text
1178 passed, 30 failed in 3798.69s (1:03:18)
```

没有因局部问题重新运行一小时全量。30 个失败按根因定向闭环：

| 数量 | 根因 | 修复与复验 |
| ---: | --- | --- |
| 1 | Windows 长 basetemp 使 spool processing claim 路径过长 | 使用短 `.tmp/sNNNNN` basetemp，原节点通过；生产 spool 逻辑未放宽 |
| 1 | cancellation 夹具缺少 PRD-052 新增 `scope_events` | 只补测试夹具，原节点通过 |
| 1 | Dry-Run 仍断言旧 `USAGE_LEDGER` 查询 | 更新为正式 `FINOPS_LEDGER_ENTRIES`，原节点通过 |
| 1 | PRD-047 application scope 引用悬空 owner | 创建同租户真实 Project/Application，roundtrip 通过 |
| 26 | PRD-050/051/052 历史迁移专项把新增 0043 当作自身栈顶 | 模块级迁移快照分别冻结到 0041/0042，三个完整文件 `30/30` 通过 |

定向 JUnit 均在权威批次内：

- `targeted-nonmigration-junit.xml`：`3/3`，SHA-256 `71c5c71acbf447cbc0111b6cc2ae15108ae1c32ec76946f49357d7c3d261fa65`。
- `targeted-prd047-junit.xml`：`1/1`，SHA-256 `741d3aada91c7350f9b012593117b0ea227d9efad299eb8f1600894b78846f08`。
- `targeted-migrations-junit.xml`：`30/30`，SHA-256 `310bd51d956b790c1f48f5db9df6954eb6f6c4fdfda2f78ada8a7121ff228e45`。

因此可以表述为“原 30 个失败全部闭环、受影响范围 34/34 通过”，不能表述为“1208 passed”。

## 7. 最终验收清单

- [x] 设计文档先于编码落盘，两轮自审无未关闭 P0/P1。
- [x] 0043、检测器、Control API、Gateway overlay、Webhook、Console 主流程及异常流程通过。
- [x] 后端 RBAC、跨租户/跨 Environment/Key owner、统一错误码和 `request_id/trace_id` 通过。
- [x] 全仓中文注释门禁、Node、Console、lint 和 Python 全量/定向失败闭环完成。
- [x] 独立 Runtime 库为 43 条迁移、最高 0043；原开发库保持 42 条、最高 0042，未被误升级。
- [x] 真实 Gateway、Control API、Console、Webhook receiver、admin/developer/viewer 和 429/恢复通过。
- [x] 1280x720 与 390x844、五种页面状态、10 张 PNG、布局/网络/隐私门禁通过。
- [x] 44 个本批次 Session 全部 revoked，active 为 0。
- [x] 三服务停止，5173/8000/8001 无监听；两库 checkpoint `[0,0,0]`、integrity `ok`、外键违规 0、sidecar 全部不存在。
- [x] `.tmp` 链接节点清理为 483 deleted / 483 targets preserved / 0 remaining ReparsePoint；未递归删除、未提权、未使用 `C:/tmp`。

## 8. Runtime、Browser 与静止终态

权威批次：[artifacts/prd053/20260727T183515](../../artifacts/prd053/20260727T183515)。

- [Runtime 证据](../../artifacts/prd053/20260727T183515/runtime-acceptance.json)：`status=passed`，SHA-256 `63661f71f2380bb6fff8e140ebe12bc45a37d3555310d3a2c9ed43865aae6959`。真实链路生成 3 条 anomaly、2 次 run，Webhook 20 字段/HMAC/隐私通过，Gateway 先 429 后 release 恢复 200。
- [Browser 证据](../../artifacts/prd053/20260727T183515/browser-evidence.json)：`status=passed`，SHA-256 `e612b779d4de1b5eac6c0bd2aac1b58b0ae1511024a0b227df02fc977aedfd74`。两档视口、五状态、10 张正式 PNG 共 521227 bytes，缺失/hash mismatch/空白均为 0；blocked request、browser/page/console error 均为 0。
- `screenshots-diagnostic-attempt-01..06` 共 6 个目录、40 个文件、2273922 bytes，只是失败诊断历史，明确排除在正式截图之外。
- [数据库终态](../../artifacts/prd053/20260727T183515/database-final.json)：Runtime 43/0043，最终 DB SHA-256 `890b725328075155e6cf57a4cae653b090e839b9979989d600dd3f475406eb26`；开发库 42/0042。两库 checkpoint、integrity、FK 和 sidecar 门禁全部通过；spool pending/processing/quarantine 均为 0。
- [日志终态](../../artifacts/prd053/20260727T183515/logs-final.json)：正式 Control/Gateway 日志无 Traceback、无未处理异常、无 5xx、无敏感明文；Gateway 的 4 条 429 日志为预期阻断证据。`console.log` 为 quiet dev server 的 0-byte 正式日志。三个 stderr 仅记录重复启动的 `EADDRINUSE`/Uvicorn formatter 失败，作为诊断证据单列排除。
- [pytest 链接清理](../../artifacts/prd053/20260727T183515/pytest-link-cleanup.json)：`status=passed`，SHA-256 `821b261b99c9888913bd0964710f6444d9d744b1d69149ad41993db8da95498b`，目标目录和测试证据全部保留。
- [最终审计](../../artifacts/prd053/20260727T183515/final-audit.json)：`status=passed`，SHA-256 `2bbbcc6eba473dd86a1667cbcca4e68a782ea367ca368e0970477d5304ee7303`。

## 9. 最终结论

PRD-053 的设计、两轮自审、实现、专项测试、一次根级全量、原失败定向闭环、0043 独立运行库迁移、
真实 Runtime/Browser、中文注释和静止终态均已完成，可标记为已完成。

该结论只适用于 PRD-053；全部 108 条 PRD 仍在进行。Excel 核对后的下一条未完成项为
`PRD-054 - Request Explainability Center`。
