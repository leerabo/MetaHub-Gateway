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

- 正式结论只保存在本文、设计文档和 Git；pytest basetemp、SQLite 副本、代理脚本、截图缓存和过程输出均为
  可重现临时证据，`C:\MetaHub\.tmp` 可整体删除，不作为正式交付事实。
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
