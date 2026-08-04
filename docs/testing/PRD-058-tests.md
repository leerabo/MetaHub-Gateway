# PRD-058 审计日志不可删除与导出测试记录

测试日期：2026-07-31

状态：**已完成**。设计、两轮编码前自审、实现、实现后两轮复核、数据库迁移、专项与相邻回归、
真实 Runtime、Browser、停服静止终态及正式 artifact 均已通过。本文只标记 PRD-058 完成；全部
108 条 PRD 仍在进行，`all_108_prds_complete=false`。

权威需求：`PRD-058 | M1-企业POC | P1 | 管理操作审计增强 | 审计日志不可删除与导出`。
设计与公开合同分别见 `docs/prd/PRD-058-audit-log-immutable-export.md` 和
`docs/api/audit-exports.md`。

## 1. 验收口径

- `TC-0115`：管理员修改 Provider、Key、预算、DLP、策略等关键对象均有同事务审计；Application
  跨项目造成的 Virtual Key 间接更新也逐条留痕。导出结果可用不可变 `AuditExport`、自身
  AuditLog、稳定快照、行数、字节数和 SHA-256 复核。
- `TC-0116`：主流程、异常、权限和跨租户路径返回统一错误码及一致的
  `request_id/trace_id`；Provider Key、Virtual Key 明文、Token、Prompt、Response、Cookie、
  Secret 和 DLP 原始 pattern 不进入公开响应、CSV、Webhook 或普通日志。
- `GET /audit-logs/export` 在 PRD-059 前固定为后端 admin-only；tenant、actor 和角色只从认证
  Session 恢复，前端隐藏按钮不能替代后端权限。
- CSV 固定 UTF-8 BOM、CRLF、18 列、10,000 行和 16 MiB 上限，并执行 Unicode 控制字符归一、
  公式注入防护、逐行字节门禁和存量 AuditLog 严格解码。
- `AuditExport`、`audit.exported` AuditLog 和 `audit_export_events` source 在同一事务提交；
  网络投递只发生在提交后，且仅有效 source lease owner 能恢复 pending Webhook intent。
- PAGE-084 仅由管理员显式点击触发导出；15 秒自动刷新只请求列表。离页、注销、身份、筛选或
  owner 变化必须 abort/revoke，developer/viewer 不得预取受保护 API。

## 2. 设计与实现后复核

两轮编码前自审已在设计文档第 12、13 节完成，关闭了 CSV 注入、稳定水位、复合 tenant FK、
actor 作用域幂等、事务顺序、source lease、前端 owner、down guard、连接释放及 Key 批量更新旁路。

实现后又完成两轮交叉复核，证据为
`.tmp/prd058/artifacts/20260731T124500-r14/post-implementation-review.json`：

1. 安全、权限与数据一致性复核：确认独立 admin 守卫、tenant-qualified 查询/关系/CAS、同事务
   导出事实、统一错误与 `request_id/trace_id`、pending-resume 身份绑定及敏感数据零泄漏。
2. 兼容性、前端生命周期与验收证据复核：确认 0049 历史保真和 down guard、PAGE-084 显式导出、
   object URL 单次销毁、双视口三角色、根级测试如实分段记录、正式 artifact 与链接清理边界。

两轮结果均为 `passed`，无未关闭阻断项。

## 3. 自动化测试结果

### 3.1 当前代码直接回归

| 范围 | 结果 | 说明 |
| --- | ---: | --- |
| 受影响 Python 最终回归 | `39 passed` | 审计导出、Webhook pending-resume、Application/Project 及相邻合同 |
| Console PRD-058 专项 | `15 passed` | PAGE-084 请求、下载、owner、RBAC、响应式和安全反馈 |
| Runtime 验收器合同 | `9 passed` | 端口、静止终态、链接守卫、敏感扫描及证据合同 |
| 中文注释门禁 | `passed` | `scripts/ci/check-comments.py` |
| Ruff 致命规则 | `passed` | 按仓库策略忽略 E501，其余致命规则为零 |

Browser 宿主合同首轮真实结果为 `9 passed / 2 fixture failures`。修复测试夹具后只重跑两个失败
节点，结果 `2/2 passed`；没有把夹具修复描述为首次全绿。

### 3.2 代码冻结前后的直接结果

| 范围 | 结果 | 主要覆盖 |
| --- | ---: | --- |
| PRD-058 冻结专项 | `10/10 passed` | CSV、幂等、快照、公式防护、大小门禁、RBAC、租户、坏存量行、source lease/CAS |
| Shared SDK 全量 | `443/443 passed` | 共享错误、i18n、Webhook 事件和既有 Shared 合同 |
| 受影响 Control 回归 | `17/17 passed` | Audit、Console 导航、Webhook 和 PRD-057 相邻合同 |
| 0049 迁移专项 | `6/6 passed` | 历史回填、up/down/up、owner、不可变、复合 FK、source 状态机和 down guard |
| 历史迁移冻结回归 | `5/5 passed` | 全栈迁移及 PRD-053/054/056/057 历史边界 |
| Application/Project | `4/4 passed` | 跨项目逐 Key 审计、失败回滚、权限和租户隔离 |
| Node 示例全量 | `33/33 passed` | OpenAI-compatible 示例兼容 |
| Console 全量 | `470/470 passed` | PAGE-084 及全部既有控制台回归 |
| Console lint | `151 files passed` | JavaScript/CSS 静态质量 |

上述历史 pytest cache 的 nodeids 在链接节点清理后未重新打开，也未进入 r14 正式 artifact，明确不参与
通过判定。最终结论只使用已持久化 JUnit、命令日志、Runtime、Browser 和测试记录，未把不可复核的缓存
包装成通过证据。

## 4. 根级 Python 历史

根级收集共 `1536` 个唯一节点。必须如实保留以下边界：

- 初始执行没有留下完整 JUnit，且后续产品代码发生变化，因此不接受为一次完整结果。
- 后段续跑实际为 `372 tests = 371 passed / 1 failed`。唯一失败是
  `test_database_readiness_binds_0046_checksum_and_builtin_catalog` 的 PRD-056 旧迁移头断言；对应节点
  已定向 `1/1 passed`。
- 失败窗口实际为 `15 tests = 14 passed / 1 failed`。唯一失败是
  `test_empty_0045_migration_round_trip_preserves_schema_integrity` 的 PRD-055 旧迁移头断言；对应节点
  已定向 `1/1 passed`。
- 产品修复后只执行受影响专项，没有重跑根级全量。本记录不声称“一次根级全量全绿”。

归档 JUnit SHA-256：`root-resume.xml` 为
`e3f2994d586993f89abcb1d18e600873d3d60f579b544c6c22839c14320159b7`，
`root-failure-window.xml` 为
`b59cf83cdacacae2f169ae0a5310cfc586e8575f704cec5a379a780fc9a66fcf`。

## 5. pending-resume 缺陷闭环

首轮真实 Runtime 在 source worker 恢复两个既有 pending Webhook delivery 时停止推进，诊断库显示
source 已领取并退避，但 delivery 仍为 `pending/attempt_count=0`。该批次未生成最终
`runtime-acceptance.json`，不计为通过。

根因是恢复路径没有从不可变 `audit_exports` 恢复并绑定原始 operation `request_id`，导致恢复调用
使用了错误身份，无法安全复用首次已冻结的 pending intent。修复后：

- 从 `audit_exports` 恢复原始 request、trace、actor、event、timestamp 和 payload；
- 出网前逐字段校验 tenant、event、export、actor、trace、timestamp 和 payload；
- 任一身份或内容错配都在网络调用前失败关闭；
- 重新确认 source claim/token/lease 的当前 owner，迟到 worker 不能发送或终结。

定向 pending-resume 回归、受影响 Python `39 passed` 和最终真实 Runtime 均通过。

## 6. 数据库迁移验收

开发库 `D:/MetaHub/.data/metahub-dev-prd031.db` 已用正式迁移器从 0048 升级到 `49/0049`。

| 项目 | 结果 |
| --- | --- |
| 迁移前在线备份 | `.tmp/prd058/dev-migration-20260731T0820-r1/pre-0049.db` |
| 备份 SHA-256 | `b12e7c5d1586987bd8b7a9a58ca59142926cf0acde46e39349f75450c9d8276b` |
| 0049 文件/checksum | `634b789f3b93c315faed66d68ebd65e25b8cafea6b2ce2221a354994adab73b0` |
| 完整性 / 外键 | `integrity_check=ok` / `0` 条违规 |
| 稳定序列映射 | `115` 条 AuditLog 对 `115` 条 sequence，双向缺失均为 `0` |
| PRD-058 trigger | `19` 个关键 trigger 全部存在 |
| 不可变探针 | AuditLog/AuditExport 的 UPDATE、DELETE 均按预期拒绝，事务回滚无残留 |
| checkpoint / sidecar | `(0,0,0)`；`-wal/-shm/-journal` 均不存在 |

迁移证据 SHA-256：
`55b222d13e1c9c1bc462a1a1527e54b2451d173962811aff760c354a779d317d`。

## 7. 真实 Runtime 验收

权威批次：`D:/MetaHub/.tmp/prd058/runtime-final-20260731T0900-r4`，`status=passed`。

- Gateway、Control API、Console 健康检查均为 HTTP 200。
- admin 导出 HTTP 200；CSV 为 1 行、1187 字节，内容 SHA-256 为
  `a5dd7ab533a110b193ec7d73de4b6777e363c2687a73829e18392fecdfdbd414`；同 operation 重放得到
  相同 export ID 和相同字节。
- developer/viewer 为 `auth.permission_denied`，匿名为 `auth.token_missing`，非法筛选为
  `audit_export.filter_invalid`；响应 Header/Body 的 request ID 合同一致。
- 两租户各自导出 1 行，跨租户命中为 0；AuditLog、AuditExport 和 source 的更新/删除门禁通过。
- `audit.exported` Webhook HMAC 正确，固定八字段 payload，真实接收 2 次；全部 source 为
  `dispatched`，pending/processing 和 active claim 均为 0，两个 delivery 均成功。
- 7 个动态扫描文件中已知敏感明文命中 0；停服后 8000/8001/5173/18058 均无监听，Session 为 0，
  SQLite sidecar 为 0。

Runtime 证据 SHA-256：
`1a4f582cb64aeebe9186c00be13f90f720887c6467c2efe085aff56c369d5b04`；
原始 Runtime DB SHA-256：
`726b3358effcb4cb21bc674763bd73cdd4695bb49c46fe0483905d4408801a4e`。

## 8. Browser 验收

权威批次：`D:/MetaHub/.tmp/prd058/browser-final-20260731T1240-r13`，`status=passed`；
运行数据库为 `D:/MetaHub/.tmp/prd058/browser-database-20260731T1240-r13/runtime.db`。

- 1280x720、390x844 两档均覆盖 admin、developer、viewer；8 张截图都是真实 PNG，magic、尺寸、
  非空像素和 SHA-256 已复核。
- admin 显式导出请求恰为 1，自动刷新和页面装载没有附加导出；请求不携带 tenant、limit、offset
  等禁止参数，响应来自真实 Control API，未拦截或伪造。
- Blob 下载生命周期为 create/click/revoke 各 1 次，临时 anchor 已移除，活动 object URL 为 0。
- developer/viewer 手写 `#audit-logs` 后导航、DOM、列表/导出预取均为 0，并回退 overview。
- 两视口横向溢出、重叠、Console error、Page error、blocked request 和敏感命中均为 0。
- 最终库为 4 个导出、4 个 source、3 个成功 delivery；Session、claim、非终态 source 和失败 delivery
  均为 0，Webhook HMAC 校验通过，49 条迁移的 head 为 0049，完整性 `ok`、外键违规 0。

Browser 证据 SHA-256：
`1e49acd8f6792770bda42ab18fd011d098f73e6c5ffb0161070f97c47739b85f`；
停服证据 SHA-256：
`268e66d405439a18b96105d1eb23043ef9b512d7827427d126e8643100941b8b`；
静态收口 SHA-256：
`1c6bbff3653b1f5c1d042d3839bf19ce39264661fe36c1bfc6f99e15038c05ef`；
Browser DB SHA-256：
`7722ea96aae275c28462bfc844488ebb925c88ef272703c237edf338ba5201ee`。

## 9. 链接节点清理

按用户批准，只处理以下四个 `D:/MetaHub/.tmp` 目录：

- `pytest-prd058-webhook-focused-r1`
- `pytest-prd058-prd055-roundtrip-r2`
- `pytest-prd058-root-resume-r1`
- `pytest-prd058-prd056-readiness-r2`

仅删除其中 `128` 个 ReparsePoint 链接节点；`128` 个目标目录及目标摘要全部保留，四个目录内剩余
链接为 `0`，其他 `.tmp` 目录未触碰。未提权、未使用 `C:/tmp`，也未删除数据库、pytest cache、
JUnit 或其他测试证据。

证据：`.tmp/prd058/artifacts/20260731T124500-r14/link-cleanup-evidence.json`；SHA-256：
`d93ba2b8007e345250f634407977cf9a434e5c81f3ad8a9eb5006f39e358ac8e`。

## 10. 正式 artifact 与最终审计

权威目录：`.tmp/prd058/artifacts/20260731T124500-r14`。

| 项目 | 结果 |
| --- | --- |
| 实际文件 / 目录 | `58` / `6` |
| manifest 登记 | `56/56`；另有 manifest 及其摘要两个控制文件 |
| 路径、字节、SHA-256 | 56 项逐项一致，无重复、越界、缺失或未登记载荷 |
| JSON | `12/12` 可解析 |
| ReparsePoint / SQLite sidecar / DB 文件 | `0 / 0 / 0` |
| 已知敏感明文扫描 | 55 个业务文件命中 `0`；控制文件不参与业务载荷扫描 |
| manifest SHA-256 | `2f2a1af548292437acc45a95619bb89c2c4e49377680aa435b707379c1c3f8ae` |
| final audit | `status=passed` |
| final audit SHA-256 | `a555fa14edefc7540de4c9af9b4f4f126e932af5b83c70ea021995ff1024fef4` |

原始 Runtime/Browser SQLite 数据库包含测试 DLP 规则正文，因此没有复制进正式 artifact；正式记录
只登记冻结 SHA-256，原库继续保留在 `D:/MetaHub/.tmp/prd058`。第一次归档因链接证据文件名不匹配
失败关闭，未删除文件；部分构建目录完整移至
`.tmp/prd058/artifact-build-failed-20260731T091000-r1`。

## 11. 最终结论

- [x] 设计文档先于编码落盘，两轮编码前自审完成。
- [x] 后端租户隔离、admin-only 权限、统一错误码和 request/trace ID 通过。
- [x] 0049、不可变事实、稳定快照、同事务审计/source 和 pending-resume 通过。
- [x] 主流程、异常、权限、跨租户、隐私、回滚、并发和前端生命周期均有直接测试。
- [x] 开发库迁移、真实 Runtime、Browser、停服静止终态、链接清理和正式 artifact 通过。
- [x] 实现后两轮复核无未关闭阻断项。

PRD-058 已达到可运行、可验收、可复核条件，可以标记完成。按 Excel `01_PRD总表` 与
`02_M0-M1核心开发` 第 60 行重新识别，下一条未完成项是
`PRD-059 - 细粒度角色权限矩阵`（`M1-企业POC / P1 / 核心层 / RBAC增强`）。
总体目标继续进行，`all_108_prds_complete=false`。
