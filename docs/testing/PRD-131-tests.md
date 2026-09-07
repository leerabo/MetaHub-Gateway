# PRD-131 测试记录

状态：已完成。旧租户、直连与私有部署迁移 assessment、差异、plan/run、cutover/rollback、Workspace/JIT
权限和 Migration Center 已交付；本条保留独立设计、聚焦测试、敏感审计和提交。下一权威项为
`PRD-132 - 客户工单、SLA与安全诊断协作`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0123 Migration | `0122 -> 0123 -> 0122 -> 0123`、fresh/current head、重复执行、权限/适配器/owner、assessment/plan/run/difference/checkpoint/rollback/head/event/outbox/action/support audit、tenant deletion manifest、不可变与状态触发器、业务事实 down guard、完整性和外键 |
| Control API | Cookie/CSRF/origin、严格 DTO、If-Match、幂等、step-up、owner 同形 404、固定适配器、只读 source 水位、差异裁剪、cutover/rollback、迟到 projection、动态权限和 SupportGrant/JIT scope/审计 |
| Console | Cookie-only client、ETag/no-store/private、exact DTO、owner/generation/Abort、稳定意图幂等键、评估/差异/计划/运行/检查点/切换/回退、MFA 与 grant 清理、离页/切换/登出/权限漂移销毁 |
| 必要回归 | Console refresh ownership、导航目标隔离、权限 revision/fingerprint、权限矩阵 DTO 和动态撤权 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-131 Python 聚焦 | `6/6 passed`；0123 migration 往返/guard 与 assessment/run/cutover/rollback/owner/JIT API，pytest 进程显式返回 `PYTEST_EXIT=0` |
| PRD-131 Console | `5/5 passed`；严格 client/DTO/state/render/binding/cleanup 边界 |
| Console 必要回归 | `21/21 passed`；refresh ownership、导航和权限边界 |
| Python static | PRD-131 变更源码与测试 Ruff 全规则通过 |
| Python format | PRD-131 变更源码与测试 Ruff formatter 检查通过 |
| Console lint | `console lint ok (393 files)` |
| Roadmap | 生成器自校验通过；validator `190` 项通过，下一项为 `PRD-132`；validator 聚焦 `5/5 passed` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 owner、扫描与凭据边界。assessment 只调用代码注册的七个适配器并使用 Workspace/tenant
  owner 条件，API 不接收 SQL、表名、列名、路径、URL 或自由 selector；difference/事件/Console 只投影固定
  code、计数、hash、sequence 和 watermark。Workspace 写请求拒绝 Bearer、Platform/Support Cookie 混用，
  Support 只在有效 JIT grant 的 `difference_summary` scope 读取并生成不可变 access audit。修正后无未关闭 P0/P1。
- 第二轮复核冻结、回退、幂等与迟到响应。run generation、freeze generation、checkpoint、cutover head、
  rollback journal 和事件/outbox 同事务推进；重复命令绑定 actor/action/idempotency/request hash，回退先进入
  `rolling_back` 并清除旧完成时间，再恢复 head、写 rollback checkpoint 和事件。Console 对 owner、权限
  fingerprint、generation 和 Abort owner 逐次复核，敏感 step-up 材料只存在于局部变量并在所有出口清空。
  修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 本条历史专项的 pytest 副本可重建；2026-09-07 起 `.tmp` 还含冻结源码、持久磁盘和可复用基础设施，
  不再允许整体删除。清理须逐项核对进程引用、恢复用途和正式证据索引。
- 暂存审计只输出规则命中计数，不输出密码、Token、Cookie、TOTP、私钥、Provider Key、Prompt/Response、
  客户正文、对象 URL 或本地路径值；未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终 33 个暂存文件审计结果：高置信敏感值 `0`、本地开发管理员凭据 `0`、禁止文件 `0`、
  PRD-101..105 用户文档 `0`；`.tmp` 与 PRD-131 模块缓存删除后均不存在。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要导航权限回归和静态检查。`0122 -> 0130` 全数据库矩阵、
集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、
四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 只能形成
`release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生数据库补充验证

本次与 PRD-132/133 合批修复，独立设计及两轮复核见 PRD-131 第 7 节。修复固定适配器列枚举、
真实账本 ID/序列、当前事务零写水位、Workspace/后台 RBAC 查询，以及新租户初始化收尾。
原生增量 0140 移除 operational-state 对物理 ctid 的错误比较，保留逻辑身份和 revision 保护。

证据目录：`.artifacts/g6-operations-20260907/`。

| 验证 | 实际结果与证据 |
| --- | --- |
| PostgreSQL 17.11 / Windows | `g6-operations-cdadab81c6864cce9db3280ed6c360ca-junit.xml` 中本条模块 8/8 通过；该次全运行 16/16，439.88 秒 |
| 七域业务与权限 | assessment/run/cutover/rollback 全七域、owner 隔离、权限扩大/投影漂移、Support JIT 撤销与 break-glass 拒绝 |
| 数据库保护 | 五张 RBAC 必需表逐一缺失拒绝；故意写入 adapter 被拒且回滚；重复 seed 不恢复已有冲突；0140 往返、事实不变及七类非法更新拒绝 |
| SQLite 业务 | `sqlite-migration-all-domains-junit.xml`，3/3 通过，139.17 秒；全七域使用相同 API 主流程 |
| 聚焦单元 | `seed-init-unit-r2-junit.xml` 17/17；`ledger-adapter-unit-junit.xml` 1/1，含跨租户排除、非连续账本序列和零写 |
| 共享入口回归 | `workspace-schema-r2-junit.xml`，11/11 通过、7 项未选择；覆盖 schema helper 及三项 Workspace 身份/撤权流程，不是 Workspace 全量测试 |
| 相邻原生回归 | 同次 cdadab81 运行中 PRD-138 列表连接和 PRD-147 seed/调度 fencing 各 1 项通过 |
| Console | `console-unit-junit.xml` 中本条 5/5 通过，不作为真实浏览器证据 |
| 静态检查 | 变更源码/测试 Ruff 与 diff 检查通过；seed 文件六处既有 E501 不在本次新增行中，不宣称整文件零债务 |

保留修复前失败：0fe48fbc 运行因 RBAC SQLite-only 查询产生 10 失败、2 通过；d06fa086 运行本条
7 通过、1 项账本适配器失败，已由本次七域验证覆盖。36504f5 运行的 PostgreSQL 崩溃发生在基线
安装，不能算成业务测试失败或通过。失败日志和 JUnit 均保留，未拼接成一次全绿运行。

独立敏感审计记录：`prd-131-sensitive-audit.json`，逐文件 SHA256、凭据高置信规则、禁止文件及
暂存范围检查。受控测试主体、step-up 替身不代表真实独立批准；本次不补写 Linux、实际迁移演练、
三浏览器、性能、安全演练或连续运行结论，也不改变 G6/G8 正式发布门禁。
