# PRD-141 测试记录

状态：已完成。0133、SaaS 平台增长漏斗/同期群/活动/实验/归因、受控导出、Platform Growth
工作台和旧 Workspace Product Analytics 退役链均已交付。本条保留独立设计、聚焦测试、浏览器证据、
两轮实施自审、敏感审计和提交；下一权威项为 `PRD-142 - SaaS 平台品牌与白标体系`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0133 Migration | fresh/current head、down/re-up、0132 -> 0133、旧 ProductEvent/outbox 保真、eligibility/attribution/snapshot/cursor/export/event/outbox/scheduler 事实 guard、权限矩阵精确收缩和 FK/integrity |
| Platform Growth API | 四角色字段矩阵、错域/混合 Cookie、Origin/CSRF、strict query/body、cursor 绑定、CAS/幂等、campaign/experiment/export 状态机、动态撤权、ordinary 404 和安全错误合同 |
| Aggregate/Export | 五阶段折叠、同期群、suppression、单位经济、渠道/客户成功裁剪、artifact hash/schema/receipt、恢复水位和事件 exact envelope |
| 退役兼容 | 活动应用不挂载 `/product-analytics/funnel`；PRD-085 0080 历史 fixture 只保留 ProductEvent/outbox 重试、死信和敏感 schema 回归；共享 RBAC/Customer Health 目标同步收缩 |
| Console/Browser | strict client、store/controller、owner/epoch/abort、角色裁剪、五 tab、Workspace 旧入口归零、桌面/移动布局、WebAuthn/TOTP 互斥和退役深链回退 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| 0133 Migration/RBAC 组合 | `52/52 passed`；0133 migration `5`、Customer Health DTO/只读服务 `2`、共享 PRD-085 合同 `45` |
| Platform Growth API | `7/7 passed`；四角色、聚合、活动/实验、导出、scheduler/event、退役路由和真实 Cookie 链 |
| Product Analytics/Customer Health 回归 | `40/40 passed`；活动 404 + PRD-085 outbox/schema `4`、共享 PRD-086 合同 `36` |
| Customer Health API | `7/7 passed`；DTO 目标、只读响应、RBAC、scheduler/outbox 与恢复状态机 |
| 受影响 Console | `25/25 passed`；PRD-086/085/141 client、normalizer、store/controller、renderer、旧运行入口与 CSS 合同 |
| Console/Workspace 权限与切换回归 | `12/12 passed`；完整扩展权限快照、混合凭据与退出清理、warm switch p95 `<300ms`、100 并发全 `200` 且仅一个 active session |
| Console 扩展权限单元合同 | `6/6 passed`；当前 18 个扩展权限族完整集合可用、未知权限失败关闭，PRD-127..130/132 既有合同保持 |
| 既有扩展权限篡改回归 | `2/2 passed`；Provider Catalog 与 Edge 权限矩阵在当前 0133 head 下仍动态复核并失败关闭 |
| Excel/路线图 | PRD-141 为“已完成”，B5 保留两个 `false` 总体标志并推进到 PRD-142；12 个工作表视觉通过、公式错误 `0`；SHA-256 `821930888c906bc78e985a36abc42a2e92625a324f709ffa52e2a57098be9bb9` |
| 路线图校验器 | `239 checks passed`；`next_prd=PRD-142` |
| JavaScript/static | Console lint `console lint ok (433 files)`；`git diff --check` 通过 |
| Python static | PRD-141、退役链和受影响 PRD-085/086 业务/测试 Python Ruff 全规则通过 |
| 浏览器 | 7 项正式检查通过；5 张正式 PNG + 1 张补充 full-page PNG，桌面/移动图像均非空，文档级水平溢出为 0 |
| 浏览器证据 | `.artifacts/prd141-browser/browser-acceptance.json`；SHA-256 `d25a2789a6007a5918b3ffbd3d27dfa835b7b011f5460544aebbfd1f234b5efd` |

浏览器账号、密码和 TOTP secret 均由隔离运行时生成，仅通过 `127.0.0.1` loopback broker 注入当前测试会话；
不写入源码、Storage、URL、截图、manifest 或测试记录。新运行时应用到 0133 后，Workspace 管理员登录成功，
没有 `rbac.matrix_integrity_failed`。

## 浏览器结论

- Platform Owner 可访问 Funnels/Cohorts/Campaigns/Experiments/Attribution 五个 tab；桌面和 375px
  移动端表格只在自身容器横向滚动，不扩大 document。
- Customer Success 只显示 Funnels/Cohorts，Source、Channel、单位经济和导出控件 DOM 命中为 0。
- Channel 只显示 Attribution，View 固定为 `Channel summary`；敏感归因字段不进入响应投影或 DOM。
- WebAuthn 模式隐藏、禁用并清空 TOTP 输入；切换认证方式不保留上一个因子的明文。
- 已登录 Workspace 访问 `#growth-analytics` 后 hash 被清空并回到 Overview；此阶段 API 实时日志没有
  `/product-analytics/funnel` 请求。随后独立直连该路径返回 `404`，同时 `/health` 返回 `200`。
- `owner-funnels-desktop.png` 是 full-page 分块捕获，固定元素有重复，仅作补充证据；正式桌面证据使用
  `owner-funnels-desktop-viewport.png`。

## 两轮实施自审

### 第一轮：迁移、权限与唯一事实

- 复核 0133 expand/backfill/fence/down guard、ProductEvent/outbox 唯一事实、四角色字段矩阵、GrowthExport
  一次性 receipt 和活动应用路由。真实 Workspace 登录发现迁移已移除 `product_analytics.read`，共享 RBAC
  目录仍保留该权限，导致矩阵完整性失败；现已同步 catalog、默认角色哈希、历史合同和迁移断言。
- 退役 `growth-analytics` 导航后复核 Customer Health 建议动作，修正 service 与 strict DTO 白名单不一致；
  `restore_gateway_activity` 现在只允许具备 `request_log.read` 时投影到 `request-logs`，旧目标被 DTO 拒绝。
- PRD-085 旧测试不再把当前 head 假定为 0080；历史 outbox fixture 显式止于 0080，正向旧 API 场景退出
  活动测试集合，当前应用 404 与历史事实恢复同时保留证据。

第一轮修正后无未关闭 P0/P1。

### 第二轮：认证共存、前端隔离与真实布局

- 真实 Platform 登录发现全局身份 Cookie 与平台 Cookie 正常共存时被 Growth 路由误判为混合凭据；现只拒绝
  Workspace/Support/Bearer 等错域凭据，并增加浏览器 Cookie 链回归。
- 真实移动端发现 900px 表格扩大 document；现由表格容器承担横向滚动。Channel 桌面端
  `Channel summary` 被筛选网格截断，现已调整 Attribution grid，并在 1280/375 复验。
- 复核 Workspace active import/router/DOM/prefetch、DeveloperStart、导航偏好和 Customer Health action；旧
  Product Analytics 运行链为 0，旧 `EVENT_TYPE_BY_NAVIGATION` 映射同步移除；excluded ID 只用于阻止历史
  偏好恢复，不会复活页面或 API。
- 额外 Console/Workspace 回归发现完整 WorkspaceSession 扩展权限快照超过 Console 手写白名单，且 18 个
  不可变权限族被逐表重复校验，导致合法导航失败、切换 p95 超标和 SQLite 并发锁等待。现由同一权威集合
  接受 Console 扩展权限，并将 schema、migration 与全矩阵读取合并为三次批量查询；完整逐族比对和未知权限
  失败关闭语义保持不变，原 3 个失败与完整 `12/12` 回归均通过。

第二轮修正后无未关闭 P0/P1。

## 敏感审计与禁止文件

- 私钥 PEM、`sk-*`、JWT、静态 password/secret/token/API key 的实际命中为 `0`；本条测试密码和密钥均由
  `secrets` 在进程内生成。
- 两个 Bearer 字符串命中仅为 Console 单元测试的 `fixture-session-token` 固定占位符，不是可用凭据；正式
  浏览器 manifest 的敏感词命中仅为检查 ID `webauthn_totp_exclusion`，没有 TOTP 值。
- 正式 JSON/PNG 对 password、Authorization、Cookie、测试邮箱和 TOTP 值的字节扫描无实际敏感值；截图只
  显示角色/展示名和聚合运营数据，不显示账号邮箱。
- `.env`、`.tmp`、`.logs`、数据库、日志、PEM/KEY 等禁止路径不进入提交；未跟踪的 PRD-101..105 用户文档
  与本条无关，不暂存。提交只使用显式路径，不使用 `git add .`。

## 批次门禁

本条按 S7 节奏执行 0133、受影响 API/Console、静态、真实浏览器、路线图和敏感审计，没有重复执行完整
G7。PRD-142..147 继续共享 Platform owner、权限、迁移与前端组件；PRD-147 独立提交后统一执行数据库
矩阵、跨域集成/FLOW、三浏览器三缩放、百万事件性能、长稳、安全、兼容、旧入口四层归零和必要回归。
PRD-141 专项通过不能替代 G7，也不能宣称公开 GA。
