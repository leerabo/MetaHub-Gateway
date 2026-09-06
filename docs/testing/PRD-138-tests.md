# PRD-138 测试记录

状态：已完成。0130、20 个固定发布门禁、签名证据、六类独立会签、四波执行、回滚独立复验、RC 评审和
Platform Launch Gates Console 已交付；本条保留独立设计、聚焦测试、真实浏览器验证、两轮自审、敏感审计和提交。
下一权威项为 `PRD-139 - 渠道伙伴、代理商与客户归属管理`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0130 Migration | `0129 -> 0130 -> 0129 -> 0130`、fresh/current head、重复执行、20 policy、11 权限、角色矩阵、candidate/artifact/evidence/decision/wave/rollback/review/event/outbox/action/scheduler、复合 owner、不可变约束和业务事实 down guard |
| Launch Governance API | 精确四路由闭集、Cookie/CSRF/Origin/step-up/CAS/幂等、签名 verifier、canonical candidate/evidence、20 gate、72..96 小时长稳、六账号会签、最终会签动态复核、四波顺序/观察、RC-only 评审、回滚前一波与独立复验、scheduler/outbox |
| Console | strict DTO、gate/evidence/decision/wave/rollback owner、canonical artifact-set digest、确定 step-up owner、Abort/ticket/revision、动态撤权、签名/TOTP 清理、无敏感持久化和响应式布局 |
| 真实浏览器 | 本地管理员凭据仅在进程内使用；Desktop 1280x900、Mobile 390x844；20/20 gate 渲染、导航独立横向滚动、移动 gate 表堆叠、无可见 alert、无 browser log、无敏感字段 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-138 Migration | `4/4 passed`；0130 fresh/往返、复合 owner 与业务事实 down guard |
| PRD-138 API/服务 | `7/7 passed`；复用 request connection、四路由/缺 verifier 失败关闭、20 签名证据与六账号会签、最终签名动态复核、波次撤权、四波 RC-only、回滚执行者/申请人/复验者分离 |
| PRD-138 Console | `5/5 passed`；固定路由/header/canonical digest、strict DTO/owner/GA 拒绝、裁剪渲染、step-up/幂等/撤权/确定 owner 和禁止持久化 |
| Console 关联回归 | PRD-095/136/137/138 `24/24 passed` |
| JavaScript/static | Console lint `console lint ok (414 files)` |
| Python static/format | PRD-138 后端、受影响权限服务、运行时错误目录和聚焦测试 Ruff 全规则、compileall 通过 |
| 真实浏览器 | Desktop/Mobile 均通过；20/20 policy、无 overlap、无 alert/log、无敏感输入残留；验证后 viewport 和页面已恢复 |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核门禁真实性、角色独立、owner 和 step-up 绑定。修正最终签名只按数量进入 ready、回滚复验未排除
  既有 executor、Console 多 Workspace 输入顺序与后端 owner 不一致三项问题；分别增加事务内全量动态复核、
  action 事实独立性检查和排序 owner 回归。固定 verifier 与 digest-only envelope 保持失败关闭。修正后无未关闭 P0/P1。
- 第二轮复核 CAS/幂等、证据过期、波次顺序、rollback/review 终态、scheduler 接管和前端敏感生命周期。
  start/resume/complete/review 均恢复当前证据与签署权限；pause/rollback 为风险降低动作但仍需当前执行权限和 step-up；
  所有执行路径只到 `release_candidate` 或失败/回滚终态。签名、TOTP 和 step-up 在 finally/表单清理中归零。
  修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 历史专项的 pytest basetemp 和截图缓存可按需重建，但不能据此整体删除 `C:\MetaHub\.tmp`。
  2026-09-07 起该目录还承载连续观测冻结源码、启动器、运行日志及可复用基础设施和数据库；清理必须逐项
  核对进程引用与恢复用途。正式测试结论和证据索引保存在本文、设计文档、`.artifacts` 和 Git 中。
- 本地管理员账号、密码和 TOTP 仅用于 localhost 真实浏览器验证，不打印、不写入文档、日志、DOM、Storage、
  URL 或提交；测试完成后输入值已清空。
- 审计不得输出 Token、Cookie、client secret、step-up grant、evidence signature、私钥、Provider Key、客户正文、
  原始证据、对象 URL、内部 endpoint 或本地凭据。服务只保存固定 code、owner、digest、计数、时间和签名摘要。
- Console DTO/DOM/Storage 不投影 tenant_id、原始 artifact URI/log、Authorization、测试正文或公开 GA 状态；
  Desktop/Mobile DOM 与日志审计命中为 0。
- 未跟踪的 PRD-101..105 用户文档不进入 PRD-138 暂存集；最终暂存只包含本条实现、测试、文档和路线图事实。

## 批次门禁

本条独立提交前运行 0130、API/服务、Console、关联回归、静态检查和真实 Chromium Desktop/Mobile 专项。
S6 批次在本条独立提交后统一运行 `0123 -> 0130` 全数据库矩阵、集成、FLOW、三浏览器三缩放、性能、安全、
代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、四波试点、灾备、支付/Edge 演练和 72 小时长稳。
即使 G6 全部通过，也只能形成 `release_candidate`；公开 GA 仍由 PRD-139..148 后续范围承接。

## 2026-09-07 G6 验签接线补充

复核发现正式应用没有安装 `launch_evidence_verifier`。上面的历史 API 用例注入了恒真 verifier，
因此只能证明 DTO、权限和状态机合同，不能证明正式密码学验签可用。现已补入部署方只读公钥信任、
用途隔离 Ed25519、17 字段 canonical envelope 共享计算和正式 app 接线。配置和轮换合同见
`docs/deployment/launch-evidence-trust.md`，签名格式见 `docs/api/launch-evidence.md`。

- 加密单元合同：34/34 通过，包括 owner/policy/result/time 绑定、用途隔离、公钥失效/移除/替换、
  错 gate、重复/未知/超限信任、非 canonical 编码、缺配置关闭、配置摘要脱敏和子进程正式 app 接线。
- 受影响 API/服务：10/10 通过。新增三项真实 Ed25519 签名用例分别拒绝错误签名、移除的公钥和损坏
  信任文件；失败不改变 candidate/evidence/event/outbox/action 事实，修复后同一请求成功且只存摘要。
  新用例独立构造外部签名 envelope，没有调用服务的摘要 helper 代替签名端合同验证。
- 合并运行：44 passed，263.51 秒；证据 `.artifacts/g6-verifier-20260907/api-crypto-r1-junit.xml`。
  首次默认 pytest 临时目录被 Windows ACL 拒绝，44 项在初始化阶段出错，记录保留在
  `api-crypto-junit.xml`；改用全新工作区 basetemp 后重跑，没有修改系统权限或覆盖旧失败记录。
- 受影响 6 个 Python 文件 Ruff 全规则通过；`git diff --check` 通过。本次不改 Console 合同或迁移，
  没有重复跑全产品回归，也没有把历史浏览器、性能或 G7 结果改称本次测试结果。
- 两轮实施复核：第一轮确认每个签名字段均被绑定、失败无治理写入、缺信任不能回退替身；第二轮确认
  生产接线、信任轮换重读、私有配置投影、签名只存摘要和幂等重放边界。移除公钥不追溯撤销已入库事实，
  追溯撤销需替换候选并重新收集证据和会签。
- 敏感审计范围为本批 11 个实现/测试/文档文件和本批 JUnit；私钥只在测试进程内生成使用。
  采用高可信密钥/云凭据/带密码数据库 URL 模式扫描，并人工复核请求/响应/持久化边界；索引保存在
  `.artifacts/g6-verifier-20260907/source-sensitive-audit.json`，不将模式扫描视为全产品安全验收。

这些 API 用例仍替换登录与 step-up，状态机用例仍使用合成 73 小时时间和测试账号；它们不是实际
72 小时业务运行、六名独立责任人的会签或真实四波试点。本次新实现也不在正在运行的 `e6bbce1`
冻结版本中。G6 未通过，PRD-148 未启动，发布治理原生 PostgreSQL 业务覆盖仍待补齐。
