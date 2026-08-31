# PRD-140 测试记录

状态：已完成。0132、SaaS 平台客户目录、版本化健康、成功计划、客户任务、续费与 QBR 协同、
Platform Customer Success 工作台以及旧企业 `customer-health` 入口切换均已交付。本条保留独立设计、
聚焦测试、浏览器证据、两轮实施自审、敏感审计和提交；下一权威项为
`PRD-141 - SaaS 平台增长、漏斗与活动归因`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0132 Migration | fresh/current head、`0131 -> 0132 -> 0131 -> 0132`、PRD-086 四表与 PRD-096 Workspace 投影回填、逐行 hash/watermark、复合 owner/FK、旧写 guard、scheduler fence 和业务事实 down guard |
| Customer Success API | 五个固定路由、四角色字段矩阵、匿名/错域/混合凭据、strict query/body、Cookie/Origin/CSRF、quoted CAS、幂等、状态机、批量独立审批、动态撤权、同形 404 与固定错误合同 |
| 健康与恢复 | v1 兼容、v2 六因子整数复算、source stale/future/failure、Membership revision 漂移、事件去重、savepoint 恢复、任务/续费/QBR 重启幂等和 supersede |
| Console | strict DTO、五路由 Cookie client、独立 store/controller、owner/epoch/request ticket、迟到响应、联系人显式生命周期、Plan/Task/Batch 命令、角色裁剪和旧入口归零 |
| 浏览器 | 真实 `platform.html#customer-success` MFA 门禁；生产 renderer/CSS 的 Health/Task/Renewal/QBR 与 Support 裁剪；1440/1280/390、无页面横向溢出、无 Console error、非空截图 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-139/140 Migration | `8/8 passed`；3 个 0131 历史兼容场景与 5 个 0132 场景均到 `100%`、退出码 `0` |
| 受影响 Backend/SDK | `64/64 passed`；PRD-086 `7`、PRD-096 `5`、PRD-132 `3`、PRD-139 `4`、PRD-140 `9`、共享健康合同 `36` |
| PRD-096 随机凭据复验 | `5/5 passed`；静态测试密码和 recovery secret 改为进程内随机生成后复跑 |
| 受影响 Console | `41/41 passed`；PRD-086/095/096/139/140 client、store/controller、renderer、active entrypoint 与 CSS 合同 |
| Excel/路线图 | B5 保留两个 `false` 标志并推进到 PRD-141，PRD-140 状态为已完成；12 个工作表视觉通过、公式错误 `0`、目标样式不变；路线图校验 `235` 项通过，聚焦测试 `5/5 passed` |
| JavaScript/static | Console lint `console lint ok (429 files)`；`git diff --check` 通过 |
| Python static | 业务/测试 Python Ruff 全规则通过；三份规划脚本 `F/E9/I`、compileall 与运行校验通过（不重排其既有长行） |
| 浏览器 | 12 张正式 PNG；configured viewport 为 `1440x900`、`1280x720`、`390x844`；Console error/warning `0`、页面水平溢出 `0`、所有图像非空 |
| 浏览器证据 | `.artifacts/prd140-browser/prd140-final-20260901-01/browser-acceptance.json`，SHA-256 `86455804a0fbd9471428e1435d4b431d5022b195b74e421be7d1aaedbe148cfc` |

浏览器没有输入或保存本地凭据。真实 Platform 路由验证独立 MFA 边界；授权成功态由运行中的 Console
直接导入生产 `renderPlatformCustomerSuccess` 和正式 `platform.css`，只使用虚构 ID 与运营数据。会话、
权限、动态撤权、跨域拒绝和真实业务 API 由同一实现的 64 个后端/SDK 场景覆盖，不用静态页面替代后端
授权结论。正式清单逐 PNG 记录 bytes、canvas 尺寸与 SHA-256。

## 浏览器布局结论

- 1440 Health 显示 5 个 tabs 与 6 个因子；Task 同时展示单任务和批量任务表单；DOM、页面与可见控件无
  横向越界，资源标签没有外部 origin。
- 首轮真实截图发现 Renewal/QBR 在 1440 的详情列约 `611px` 时仍保留 `780px` 最小宽度，金额/生成时间列
  需要内部横向滚动。修正后 1440 Renewal 为 `611/611px`，1280 Renewal/QBR 为 `522/522px`，wrapper
  overflow 均为 `false`，最后一列和所有 QBR cells 均在容器内。
- 390 Renewal 转为纵向字段行，table/container 为 `347/347px`，cells outside 为 `0`；Task 两张表单均为
  `347/347px`，可见控件越界为 `0`。Platform 横向导航和五 tab 使用各自受控滚动，不扩大 document。
- Support 在桌面和移动端都只有 Health/Renewal；联系人按钮、Plan/Task/Batch form、客户名称、金额和邮箱
  DOM 命中均为 `0`。

## 两轮实施自审

### 第一轮：迁移、owner、状态机与恢复

- 复核 0132 expand/backfill/fence/down guard、五路由闭集、PlatformSession 四角色矩阵、客户/Workspace
  复合 owner、health head/snapshot 单调和 legacy PRD-086 保真。修正生产路由表仍可能暴露旧
  `/customer-health`、Platform Workspace 仍读取旧可写 health projection、历史 0130 RBAC fixture 受当前
  catalog 漂移影响三项；现为普通统一 404、只读 0132 head/snapshot、历史 fixture 自包含并恢复当前常量。
- 复核联系人 KMS/source revision、批量双人审批、任务 due generation、renewal/QBR 重启恢复和事件唯一性。
  修正联系人 source failure 审计回滚、Membership revision 漂移清空、scheduler 单客户 savepoint、续费风险
  事件去重和 QBR supersede 场景，失败均不提交半成品或敏感读取假记录。

第一轮修正后无未关闭 P0/P1。

### 第二轮：前端隔离、敏感生命周期与真实布局

- 复核 Platform/Workspace 入口所有权。active `main.js` 不再导入、路由、DOM 或预取企业
  `customer-health`；Platform shell 只加载独立 customer-success model，历史模块仅保留到 PRD-147 清理。
- 复核 Cookie-only client、strict DTO、联系人的显式读取/关闭、离页 abort、owner/revision/epoch、迟到
  200/401/403/409/503、幂等重试和一次性 step-up；联系人、verification code、grant 和 manifest 不进入
  Storage、URL、history、analytics 或长期 state。
- 真实浏览器发现并修复 Renewal/QBR 桌面固定最小宽度导致的内部裁剪，新增 CSS 合同测试并重跑
  41 个受影响 Console 场景；1440/1280/390 和 Support 裁剪复验全部通过。

第二轮修正后无未关闭 P0/P1。

## 敏感审计与禁止文件

- PRD-140 最终提交路径 `42` 个；私钥 PEM、`sk-*`、Bearer literal、静态密码、静态 recovery/token/client
  secret 的意外命中为 `0`，`.env`、`.tmp`、`.logs`、数据库、日志、PEM/KEY 等禁止路径为 `0`。
- 全文件静态赋值规则另命中 README 既有且不在本次 diff 的 5 个显式占位符：`change-me-*`、
  `replace-with-*` 和 `<set-...>`；它们不是可用凭据。本次新增 diff 与其余 41 个路径命中为 `0`。
- 两个 Cookie pattern 命中仅位于 PRD-096/139 集成测试的动态 Header 拼装，值来自运行时 Cookie jar 或
  synthetic token，不是静态凭据。PRD-096 历史静态测试密码与 recovery secret 已改为 `secrets` 运行时生成。
- 正式证据共 13 个文件（12 PNG + 1 JSON），对私钥、API Key、Bearer、password、Cookie、seed 管理员邮箱、
  synthetic 联系人邮箱/手机号的字节扫描命中为 `0`；浏览器验收没有输入凭据。
- 目录响应不返回联系人；单客户 contact 失败/漂移响应不含 `example.invalid` canary；平台 audit/event/outbox
  只保留固定 purpose、ID、枚举、计数和 digest，不保留 email、phone、异常字符串或请求正文。
- 未跟踪的 PRD-101..105 用户文档与本条无关，不进入 PRD-140 暂存集；提交使用显式文件清单。

## 批次门禁

本条按 S7 节奏只运行 0132、受影响 API/Console、静态、真实浏览器和敏感审计，没有重复执行完整 G7。
PRD-141..147 继续共享 S7 owner、权限、Platform 组件和迁移策略；PRD-147 独立提交后一次性执行 G7
数据库矩阵、跨域集成/FLOW、三浏览器三缩放、10 万目录性能、长稳、安全、兼容、旧入口四层归零和必要
回归。PRD-140 专项通过不能替代 G7，也不能宣称公开 GA。
