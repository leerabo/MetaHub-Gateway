# PRD-139 测试记录

状态：已完成。0131、渠道唯一事实、伙伴成员与推荐码、商机、Workspace 归属审批、规则快照、可对账
statement、legacy PRD-087 迁移、Platform Channels 和独立 Partner 页面均已交付。本条保留独立设计、
聚焦测试、浏览器证据、两轮实施自审、敏感审计和提交；下一权威项为
`PRD-140 - SaaS平台客户管理、健康与续费协同`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0131 Migration | fresh/current head、`0130 -> 0131 -> 0130 -> 0131`、legacy 六表回填、逐 source mapping/hash、CNY/USD rule 与 statement、复合 owner/FK、旧表 write guard、scheduler fence 和业务事实 down guard |
| Channel API | 六个固定路由、匿名/错域/混合凭据、未知字段、Origin/CSRF、step-up、quoted `If-Match`、幂等同体/异体、动态撤权、lead 去重、初始单批、替换双批、最终批准复核、statement 裁剪与错误 4xx 合同 |
| Partner 隔离 | GlobalSession + 当前 ChannelMembership、owner 过滤后分页/total、finance statement totals、撤权失败关闭、无 WorkspaceSession/PlatformSession/SupportSession、无 commission rule/pending attribution 泄漏 |
| Console | 六路由 strict DTO、Platform capability 裁剪、billing statement-only、敏感 intent SHA-256、一次性 step-up、稳定重试幂等键、quoted CAS、Abort/epoch/revision、撤权清理、Workspace 旧 Partner loader 不再被调用 |
| 浏览器 | 实际 `platform.html#channels` MFA 边界、实际 `partner.html` Session 边界；生产 renderer/CSS 的 Channels 与 Partner 成功态；1440x900/390x844、审批表单边界、无水平溢出、无 console error、非空截图 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-139 Migration + API | `7/7 passed`；命令：`python -m pytest test_prd139_db_migrations.py test_prd139_channel_management.py -q --basetemp=.tmp/pytest-prd139-final-20260831a` |
| PRD-139 Console | `5/5 passed`；六路由/DTO、Partner client 隔离、capability、审批响应式布局、敏感生命周期与撤权 |
| JavaScript/static | Console lint `console lint ok (423 files)`；`node --check` 与 `git diff --check` 通过 |
| Python static | 受影响后端、权限服务、共享错误目录和两个 PRD-139 测试文件 Ruff 全规则、compileall 通过 |
| 浏览器 | 10 张正式截图；configured viewport 为 Desktop `1440x900`、Mobile `390x844`；console error `0`、水平溢出 `0`、期望正文缺失 `0`、所有图像非空 |
| 审批布局 | Desktop wrapper/table `1072.67px`、decision cell `407.60px`、form `387.60px`；Mobile cell `346.67px`、单列表单 `228.67px`；两端 `form_inside_cell=true` |
| 浏览器证据 | `.artifacts/prd139-browser/prd139-final-20260831-01/browser-acceptance.json`，SHA-256 `1756d0163da3b2a254740cc506d2a1d5183a36fcca2b8347c2ee2af2db0048cf` |

浏览器实际路由只验证身份边界，没有输入或保存本地凭据；授权成功态由运行中的 Console 直接导入生产
`renderChannelManagement`、`renderPartnerPage` 和正式 CSS，以纯虚构数据完成响应式视觉检查。会话、权限、
撤权和真实业务 API 已由同一实现的 7 个集成场景覆盖，不用静态 HTML 代替后端授权结论。截图后临时 fixture
已删除，manifest 对 10 个 PNG 的 bytes/SHA-256 复核 mismatch 为 `0`。

## 两轮实施自审

### 第一轮：路由、owner、迁移与审批

- 复核六路由闭集、Platform/Partner 凭据域、partner/target Workspace 双 owner、legacy source mapping、
  金额/rule snapshot 和 scheduler fence。修正 expected channel error 未登记导致验证异常落为 500、测试 app
  未共享动态 Platform auth、billing 导航错误依赖 directory read 三项；错误现为预期 4xx，billing 只读
  statement，所有最终批准均在事务内重验 operator permission/revision。
- 复核归属时间区间和替换语义。替换 `starts_at` 必须严格晚于当前 active 开始时间，原归属只在最终批准
  时原子结束于新开始时间；旧 approval/generation、重复 lead 和异体幂等重放均失败关闭。

第一轮修正后无未关闭 P0/P1。

### 第二轮：前端隔离、敏感生命周期与布局

- 复核 Platform/Partner/Workspace 三个入口。Partner entry 只导入 `partnerChannel.js`，响应拒绝 Platform
  字段；Workspace 仅保留 PRD-087 历史源码，批量刷新和活动导航 dispatcher 均不再调用旧 loader，
  `/partners/*` 外部 router 已卸载，物理源码/DOM 删除留给 PRD-147。
- 复核一次性 ReferralCode、联系人、TOTP、step-up grant、幂等 intent 和迟到响应。敏感 command 只保留
  SHA-256 fingerprint，Transient retry 复用同一 key，成功/失败/离页/撤权/destroy 均 abort 并清理。
- 真实截图发现审批 decision cell 在桌面固定表格中不足，导致 reason/submit 横向裁切；已把最后一列固定为
  38%、移除 form 最小宽度并在 Mobile 恢复 100% 单列。DOM 测量、5/5 回归和重新截图均通过。

第二轮修正后无未关闭 P0/P1。

## 敏感审计与禁止文件

- 受影响文件对静态密码值、私钥 PEM、`sk-*` API Key、Bearer 令牌和静态会话/Token Secret 扫描命中均为
  `0`。本条新增运行/测试代码中的唯一 `admin@metahub.local` 命中是 migration 测试的公开种子邮箱，不包含
  密码、Cookie、Session 或授权值；测试密码在进程内随机生成，不落盘。
- 测试中的 referral/contact 都使用显式 synthetic/invalid 值；运行时代码只保存 referral lookup digest、
  masked suffix 与联系人 envelope ciphertext/digest，事件、错误、DOM、Storage 和正式证据不保存明文。
- Token、Cookie、CSRF、step-up grant、TOTP、Global/Platform/Workspace/Support Session、Provider Secret、
  客户正文、合同正文、内部 endpoint、原始 ledger 与本地凭据均未写入文档、截图 manifest 或 Git 变更。
- 当前变更路径审计为 `33`，`.env`、`.tmp`、`.logs`、数据库、日志、PEM/KEY 等禁止文件为 `0`。
  `.tmp` 是可整体删除的 pytest/fixture 临时目录；正式浏览器证据只在 `.artifacts/prd139-browser/`。
- 未跟踪的 PRD-101..105 用户文档与本条无关，不进入 PRD-139 暂存集；提交使用显式文件清单。

## 批次门禁

本条按 S7 节奏只运行 0131、受影响 API/Console、静态、真实浏览器和敏感审计，没有重复执行 G6 或全套
产品回归。PRD-140..147 继续共享 S7 owner、权限、Platform/Partner 组件和迁移策略；PRD-147 独立提交后
一次性执行 G7 数据库、集成、FLOW、三浏览器三缩放、性能、安全、兼容、旧入口四层归零和必要回归。
