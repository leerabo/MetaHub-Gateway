# PRD-054 请求解释中心测试记录

测试日期：2026-07-28

当前状态：设计、两轮编码前自审、实现、专项测试、一次根级全量回归、定向失败闭环、
真实 Runtime、真实 Browser 和停服静止终态均已完成，PRD-054 已达到可运行、可验收状态。
这只表示 PRD-054 完成，不代表全部 108 条 PRD 已完成。

运行环境：Windows PowerShell；Python 使用
`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
pytest 的 `TEMP`、`TMP` 和 `--basetemp` 全部限定在 `D:/MetaHub/.tmp`，没有使用
`C:/tmp`、沙箱外目录或提权方案。

## 1. 验收口径

- Excel 对应项：`PRD-054 - Request Explainability Center`，页面为 `PAGE-079`。
- admin/developer 可以解释当前租户请求；viewer、匿名和跨租户请求必须由后端同形拒绝。
- 响应固定为十阶段、十一区块，区分 `complete/partial`、缺失和截断；不得读取当前配置
  回算历史事实。
- API 成功与错误均保留统一错误码、`request_id/trace_id`，解释读取必须先持久化不可变访问
  事实和 `request.explanation_viewed` source outbox。
- Provider Key、Session Token、Prompt、Response、Webhook URL/payload、原始上游错误和 DLP
  命中文本不得进入 API、DOM、URL、Storage、日志或验收证据。
- 0044 必须证明 tenant owner、append-only、状态机、lease/CAS、REPLACE/rowid 和 down guard。
- Browser 必须覆盖 1280x720 与 390x844 的 complete、partial、error，并证明 viewer 无 DOM/
  预取、截图非空、无布局溢出和 Session 清理。

完整冻结口径见 [PRD-054 技术设计](../prd/PRD-054-request-explainability-center.md) 和
[请求解释中心 API](../api/request-explanations.md)。

## 2. 设计与两轮编码前自审

设计文档先于编码落盘，两轮编码前自审均已写入设计文档并关闭 P0/P1：

- 第一轮聚焦单快照证据、历史事实边界、十阶段/十一区块、完整性语义、截断上限、金额精度、
  tenant owner、不可变访问事实和 source outbox 事务边界。
- 第二轮聚焦后端 RBAC 优先级、跨租户同形拒绝、严格原始 wire、Webhook 最小披露、lease/CAS、
  Console 一次性内存 handoff、迟到响应 owner、双视口和隐私门禁。

第二轮结束时只表示允许编码，不等于实现或运行验收通过。实现完成后的两轮交叉审查、修正
和最终完成结论现已随直接证据一并补入设计文档。

## 3. 已取得的直接测试结果

| 范围 | 直接结果 | 主要覆盖 |
| --- | --- | --- |
| Shared / Control / 0044 / 边界 / 导航聚焦组合 | `69 passed` | 错误与事件合同、迁移、DTO、RBAC、租户、完整/partial、截断、精度、故障关闭和导航 |
| 第十次过期 outbox lease 定点复验 | `1 passed` | `processing + attempts=10` 直接终结 failed，不发起第 11 次网络调用 |
| Console PRD-054 与相邻回归 | `32 passed` | PAGE-079、RequestLog handoff、角色 owner、迟到响应、响应式布局 |
| Console lint | `139 files passed` | 当前前端源码和 Browser 验收器静态门禁 |
| Runtime 验收器合同 | `8 passed` | 路径/数据库边界、角色、DTO、outbox、清理及 fixture 字段合同 |
| Browser 验收器合同 | `9 passed` | Runtime 前置、双视口六状态、网络/DOM/Storage/截图/Session 门禁 |
| 中文注释门禁 | `comment coverage ok` | `scripts/ci/check-comments.py` 全仓通过 |
| 根级 Python 全量回归 | `1285 tests = 1227 passed + 58 failed` | 只执行一次；58 个失败全部属于新增 0044 后的历史迁移栈顶快照，`test_db_migrations.py` 52 个、`test_prd053_db_migrations.py` 6 个 |
| 原失败 node 定向闭环 | `58/58 passed` | 只重跑根级 JUnit 中的原失败 node ID，没有重跑整套全量 |

上述结果按实际执行批次分别记录，不把它们相加伪装成一次新的全量通过。最终 outbox 修正后
只重跑了直接受影响的第十次 lease 节点；根级回归发现历史迁移快照未先回退 0044 后，也只
修正并重跑了原失败 node，没有因局部问题重启全量。

## 4. 真实 Runtime

权威批次：`artifacts/prd054/20260728T040841`。

[Runtime 证据](../../artifacts/prd054/20260728T040841/runtime-acceptance.json) 已直接取得
`status=passed`，证明：

- 隔离运行库共 44 条迁移、最高 0044，关键表、索引、触发器、integrity 和外键检查通过。
- Gateway、Control API、Console 三服务 HTTP 200。
- admin/developer 成功，viewer 403，匿名 401；权限在畸形业务输入前执行。
- 跨租户与不存在均为 `request_explanation.not_found` 404；非法 query/path 为统一 422。
- complete/partial 都返回固定十阶段、十一区块并通过隐私白名单。
- 三次成功读取形成三条不可变访问事实，失败请求不留成功事实；source outbox 持久化、
  event ID 唯一且未向通用 Audit 重复复制。
- admin/developer/viewer 三个验收 Session 全部 revoked。

同一批次的 [Browser 证据](../../artifacts/prd054/20260728T040841/browser-acceptance.json)
也为 `status=passed`：1280x720 与 390x844 分别覆盖 complete、partial、error，共六张非空
正式 PNG；布局违规、浏览器错误、viewer 预取和敏感信息命中均为 0。

Runtime JSON 的最终 SHA-256、数据库 checkpoint/sidecar 和日志隐私结果均在停服静止终态后
登记，避免对仍在写入的文件提前计算无效哈希。

## 5. 最终运行与静止验收

- [x] 同一权威批次的真实 Browser 已覆盖两档视口和 complete/partial/error 六张正式 PNG，
  角色、网络、DOM、Storage、隐私和 Session 清理全部通过。
- [x] 当前源码只执行一次根级 Python 全量；58 个迁移快照失败按原 node ID 定向闭环为
  `58/58 passed`，没有第二次运行整套全量。
- [x] 服务停止后 5173/8000/8001 均无监听；运行库为 44 条迁移、最高 0044，checkpoint
  `[0,0,0]`、integrity `ok`、外键违规 0，批次新增 7 个 Session 全部 revoked，WAL/SHM/
  journal sidecar 和 spool pending/processing/quarantine 均为 0。
- [x] 六份正式日志共 214472 bytes；Traceback、error level、意外 5xx、credential shape、
  管理员密码和四类私有 fixture 明文命中均为 0。
- [x] 全程只在 `D:/MetaHub/.tmp` 删除 pytest SymbolicLink 节点，不使用 `C:/tmp`、不提权、
  不递归删除目标；最终汇总覆盖 7 个实际清理阶段，588 个链接节点删除、588 个目标保留、
  缺失目标和已记录 basetemp 剩余 reparse point 均为 0。
- [x] [database-final.json](../../artifacts/prd054/20260728T040841/database-final.json)、
  [logs-final.json](../../artifacts/prd054/20260728T040841/logs-final.json)、
  [pytest-link-cleanup.json](../../artifacts/prd054/20260728T040841/pytest-link-cleanup.json) 和
  [final-audit.json](../../artifacts/prd054/20260728T040841/final-audit.json) 已生成并登记稳定 SHA-256。

## 6. 当前结论

PRD-054 的设计、实现、测试、文档、数据库迁移、真实 Runtime/Browser 和静止终态验收均已
通过，现标记为完成。全部 108 条 PRD 仍在进行中；按 Excel 里程碑和优先级重新核对后的
下一候选项为 `PRD-055 - Trace查询与过滤增强`，开始前仍须再次检查当前完成标记。
