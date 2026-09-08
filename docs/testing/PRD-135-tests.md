# PRD-135 测试记录

状态：已完成。版本化合规控制与法规映射、受信机器证据、例外与整改、Workspace 报告、持久 freshness
调度，以及 Platform/Workspace 双域 Console 已交付；本条保留独立设计、聚焦测试、敏感审计和提交。
下一权威项为 `PRD-136 - SBOM、制品签名、漏洞与发布证明`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0127 Migration | `0126 -> 0127 -> 0126 -> 0127`、fresh/current head、重复执行、控制/映射/证据/例外/整改/报告/事件/outbox/action/replay/scheduler、动态 Workspace/Platform 权限、machine scope/audit catalog、复合 owner、不变约束、tenant deletion manifest、业务事实 down guard、完整性和外键 |
| Control API | Platform Cookie/CSRF/Origin 与 Machine Bearer 分域、严格 body/query/header、签名 evidence envelope、schema/digest/owner/freshness、幂等/replay、控制 outcome、报告 high-watermark、跨 Workspace 同形 404、例外非自批和动态撤权 |
| Machine chain | 真实 service account -> OAuth client -> client credentials -> scoped token -> machine authorization -> evidence receipt 链；`compliance_collector` 只能取得 `compliance.collect`，旧 provider/observability 路由保持轻量授权探针 |
| Scheduler | 持久 lease token/fencing generation、过期租约接管、失败控制整改、证据即将过期、例外批准事件、幂等 outbox 投递和恢复 |
| Console | Platform 控制/映射/outcome/freshness/来源/例外/整改与 Workspace 过滤；Identity Workspace 报告/差距/stale/证据有效性/历史；strict DTO、owner/epoch/Abort、权限漂移、响应式表格和“非外部认证”负文案 |
| 必要回归 | Identity、发票对账、PRD-118 Machine Identity、PRD-134 Privacy Center，以及既有机器身份五项功能链路 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-135 Migration/API | `6/6 passed`；0127 fresh/往返/down guard 与真实 OAuth/machine evidence、控制报告、例外、整改、事件和 scheduler 链路 |
| PRD-135 Console 与必要回归 | Identity/发票对账/Machine Identity/Privacy/Compliance 联合 `44/44 passed`，其中 PRD-135 Console `5/5 passed` |
| PRD-118 功能回归 | 排除独立性能阈值用例后 `5/5 passed`；旧 provider/observability scope、幂等、动态撤权、并发 CAS 保持通过 |
| JavaScript/static | 改动脚本 `node --check` 11/11；Console lint `console lint ok (407 files)` |
| Python static/format | PRD-135 后端、权限扩展和聚焦测试 Ruff 全规则通过；实际改动 Python 文件 formatter check 通过 |
| Roadmap | 生成器自校验与 validator/聚焦测试通过；Excel PRD-135=`已完成`、PRD-136=`未启动` |
| Diff hygiene | `git diff --check` 通过（仅既有 `identity.css` CRLF 归一化提示） |

## 两轮实施自审

- 第一轮复核 owner、证据来源、字段投影和合规措辞。0127 将控制/映射版本、证据、例外、整改、报告、
  事件、replay 与 scheduler 全部纳入复合 owner 或全局不可变边界，同时扩展 machine audit 闭集和 tenant
  deletion manifest。API/Console 只投影摘要、digest、有效期、outcome 和整改引用，不投影正文、对象地址、
  本地路径或采集器密钥；控制通过不渲染成认证通过。修正后无未关闭 P0/P1。
- 第二轮复核 replay、幂等、例外非自批、freshness 与热路径兼容。签名 envelope 绑定 route/owner/control/
  collector/artifact/time/outcome，重复 nonce 和变体 payload 失败关闭；scheduler 使用持久 lease/fencing，
  失败控制、过期证据和例外事件可幂等恢复。复核发现所有机器路由读取完整扩展权限会改变既有热路径，
  已限定为只有 `compliance.collect` 使用完整快照，provider/observability 保持既有轻量探针。修正后无未关闭 P0/P1。

## 性能残差

历史 PRD-118 性能用例要求 OAuth token issue P95 `< 300ms`。本机 Windows 环境的合并运行约 `598ms`，
隔离复跑约 `735ms`，因此该用例仍按原阈值失败；未降低阈值，也未将其写成通过。新增 migration-head 查询
单独执行 10,000 次约 `1.38s`，约 `0.14ms/次`，不能解释上述 I/O 主导的残差。完整性能 G6 仍在
PRD-138 独立提交后统一执行并形成正式批次结论。

## 敏感审计与临时证据

- 正式结论保存在本文和 Git，原始测试结果按运行独立保留。`.tmp` 还包含冻结源码、基础设施和持久
  VM 磁盘，不能整体删除；只可单独核验已停止、无依赖的可重建测试目录后处理。
- 审计只输出规则命中计数，不输出密码、Token、Cookie、client secret、TOTP、step-up grant、evidence
  receipt、签名值、私钥、Provider Key、客户正文、对象 URL 或本地管理员凭据；本地测试凭据只在进程内使用，
  不打印或持久化。
- evidence API 禁止路径、URL、命令和自由 JSON；Console 不读取 Storage，不把证据正文、receipt 或认证
  覆盖写入 DOM。未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终暂存集只包含 PRD-135 实现、测试、文档和路线图文件；生产凭据字面量、Bearer 值、私钥块、JWT/
  Provider Key 形态、URL 携带 secret、本地管理员密码、本机用户路径、凭据文件/`.env` 和受保护
  PRD-101..105 文档命中均为 `0`。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要 Identity/Platform/Workspace 回归和静态检查。`0123 -> 0130`
全数据库矩阵、集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、
SQLite/PostgreSQL、N/N-1、四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。
G6 只能形成 `release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生修复复验

同 PRD-134/136/137 合批。真实 OAuth 流程定位到 PostgreSQL 保留字 `grant` 别名，报告生成定位到
SQLite `rowid` 水位。分别改为 `token_grant` 与 owner 限定的 `SUM(revision)`；记录不可删除、
版本单调递增约束不变，批准和过期都触发水位变化。时间比较也按入库毫秒精度处理。

- PostgreSQL：`g6-security-c693a109613341cbb02892f3d7ae60da-junit.xml` 中本条 3/3 通过，包含
  真实 OAuth/machine collector、证据重放与 owner 边界、双人例外和调度。首轮三项失败保存在
  `g6-security-09c8ecef524c4521b168228d57388585-junit.xml`，未抹去或改为通过。
- SQLite：`sqlite-r2-junit.xml` 中本条 3/3 通过。
- 水位/时间：`timestamp-watermark-r1-junit.xml` 中本条 7/7，最终
  `receipt-config-unit-r2-junit.xml` 再覆盖；Console `console-unit-junit.xml` 中本条 5/5。

证据位于 `.artifacts/g6-security-20260907/`，独立敏感审计为 `prd-135-sensitive-audit.json`。
两轮复核见本条 PRD 第 8 节。机器认证是真实应用 OAuth；合规 evidence 与审批角色仍为测试场景，
不构成外部合规认证或独立责任人批准。既有 PRD-118 性能残差本轮未复测、不改原阈值。

## 2026-09-08 完整调度下的异常清理

真实完整本地运行暴露 scheduler commit fence 失效后的 SQLite 持锁。独立数据库副本的
诊断记录将持续事务定位到 _release_lease；原函数异常分支跳过连接关闭。本条增加 finally
归还句柄及异常 rollback，保持 best-effort 行为，不绕过外层 lease fence。

原始 JUnit 位于 `.artifacts/g6-security-20260907/`：

- `prd135-cleanup-red-r1.xml`：4 项，2 通过、2 失败，复现拒绝提交和 rollback 失败后未关闭。
- `prd135-cleanup-r2.xml`：11/11，含上述 4 个清理用例与 7 个既有时间/水位回归。
- `prd135-cleanup-api-r1.xml`：3/3，原有 SQLite 证据、审批、报告与调度业务回归；使用该测试
  自建完整迁移库，不冒充原生 PostgreSQL、真实浏览器或长期稳定性验收。

清理测试在真实 SQLite 事务中使用生产 commit guard 拒绝提交，检查原事实不变、句柄关闭及
第二连接即时获得写锁。连接建立失败仍正常返回，rollback 抛错也继续关闭。独立敏感审计
为 `prd-135-scheduler-cleanup-sensitive-audit.json`，范围仅本条四个暂存文件与上述三份报告。
后续发现的旧用量导出持锁属于公共提交拒绝边界，由 PRD-147 独立处理，不混入本条完成范围。
