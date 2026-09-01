# PRD-142 测试记录

状态：已完成。0134、Platform/Channel/Deployment 三个品牌 owner 域、Host 公共品牌 LKG、
私有 Gateway 品牌发布与回滚、四组 Excel API、旧 Workspace Branding 退役链均已交付。
本条保留独立设计、聚焦自动化、真实浏览器证据、两轮实施自审、敏感审计和独立提交；
下一权威项为 `PRD-143 - Platform Provider Adapter 工程台与错误规则分域`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0134 Migration | pristine 0133 -> 0134、current head、down/re-up、旧 Branding inventory/asset/event/outbox 保真、权限切换、活动 lease 阻断和 guarded down |
| Platform Brand | draft/update、资产绑定/移除、域名申请、release、public LKG、幂等重放、CAS、step-up、Origin/CSRF 与动态撤权 |
| Channel Brand | Channel scope、合同绑定、资产/域名/release、Partner fallback、跨 Channel 失败关闭和全局身份 Cookie 共存 |
| Deployment Brand | Workspace owner、publish/ack/LKG/rollback、desired/applied generation 分离、active Deployment 门禁和错 Workspace 隔离 |
| Public/Asset | Host 范围公共投影、ETag/no-store、PNG signature/尺寸/体积/安全扫描、无 owner hint、无域名 challenge 或注册材料外泄 |
| Console/Browser | strict client、Platform/Channel/Deployment controller 隔离、Identity 先加载公共品牌、旧 Workspace Branding 运行链归零、390/1280/1440 响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Backend/API/Migration | `21/21 passed`，失败/错误/跳过均为 `0`，耗时 `1877.18s`；JUnit：`.artifacts/prd142-tests/backend-focused-junit.xml` |
| 受影响 Console | `78/78 passed`；PRD-142 client/controller/renderer、Identity、Channels、Private Gateway、权限与既有 Branding/PRD-087/124 回归均通过 |
| Console lint | `console lint ok (440 files)` |
| Python static | Ruff 全规则、compileall 均通过 |
| Repository static | `git diff --check` 通过 |
| 浏览器 | `8/8` 正式检查通过；11 张正式 PNG + 1 张补充 full-page PNG；Platform、Channel、Deployment 与公共 Identity 均无页面级横向溢出、品牌控件裁切或告警 |
| Excel/路线图 | PRD-142 为“已完成”，PRD-143 保持“未启动”；B5 保留两个 `false` 总体标志并推进到 PRD-143；12 个工作表全部渲染、公式错误 `0`；SHA-256 `1726174aada706dd91b9294e6f8c6e5d552eac93c187f50bffedba0ca033680e` |
| 路线图校验器 | `245 checks passed`；`next_prd=PRD-143` |
| 浏览器证据 | `.artifacts/prd142-browser/browser-acceptance.json`；SHA-256 `afad9dc6ee7811e1c0a81c442c0186880c0ebf0f9db7812ecd87c835403e47a7` |
| 自动化摘要 | `.artifacts/prd142-tests/test-summary.json`；SHA-256 `e92e2ce08092374f96e606a1ff8d90d96e2eb3f7e2d0e02af7e12235bbac6b0d` |

测试账号、密码、TOTP、Cookie 和 session token 只存在于隔离本地运行时和当前浏览器会话。
渠道测试伙伴通过真实 UI 创建；隔离 SQLite 平台会话快照争用通过刷新会话活动时间完成验收，
没有为测试绕过修改生产代码，也没有把凭据或注册材料写入源码、Storage、URL、截图或证据。

## 浏览器结论

- Platform Branding 展示独立 released revision、公共 Host 投影和五类受控命令，不读取或写入
  Workspace/tenant branding owner。
- Channel Partner 在真实 UI 创建后只展示自身 ChannelBrand；跨 Partner 状态不复用，页面没有 alert。
- active 私有 Gateway Deployment 展示独立 DeploymentBrand 和四类命令；pending Deployment 只显示
  “注册后可用”门禁，不发起失败的品牌读取。
- 公共 Identity 页面在身份 controller mount 前应用 Host 范围产品名、标题、说明、颜色、Logo 与 favicon；
  凭据输入为空，公共 DTO 不含 owner、challenge、Cookie 或内部 revision 事实。
- `390 / 1280 / 1440` 关键视口均无 document 级水平溢出、文本遮挡、控件裁切或品牌表单越界。
  `platform-branding-1440.png` 是补充 full-page 捕获；正式桌面证据使用 viewport 版本。

## 两轮实施自审

### 第一轮：迁移、owner 与发布唯一事实

- 复核 0134 expand/backfill/fence/down guard，将旧 Workspace Branding inventory 显式迁移到
  PlatformBrand；ChannelBrand 与 DeploymentBrand 不从 tenant 推断 owner。活动旧 outbox lease 会阻断
  cutover，品牌事实存在时 down 失败关闭，fresh/current/down/re-up 均由独立测试覆盖。
- 复核三种 owner 的权限、Cookie、CSRF、step-up、`If-Match` 与幂等边界。全局身份 Cookie 与 Platform
  Session 合法共存；Workspace/Support/Bearer 等错域凭据失败关闭，动态撤权不会沿用旧快照。
- 复核 asset/domain/release 事实和公共 LKG：只接受有界 PNG，安全扫描未通过不能发布；公共响应只按 Host
  解析已发布 generation，Channel 合同轮换、Deployment desired/applied/LKG 互不冒充。

第一轮复核后无未关闭 P0/P1。

### 第二轮：前端隔离、退役链与真实布局

- 复核四条固定 Cookie API、strict DTO、Platform/Channel/Deployment controller 的 owner/epoch/abort/
  idempotency 生命周期；Channel 切换不复用前一 owner 数据，Deployment 只能读取当前可见且 active 的目标。
- 旧 Workspace `branding` 页面、loader、tenant query、blob URL 和设置 CSS 已从活动运行链移除；共享 RBAC、
  Console 导航、DeveloperStart 与历史测试同步冻结，退役 ID 只用于阻止旧偏好复活。
- 真实浏览器分别复验 Platform、Channel、Deployment 和公共 Identity。Channel 测试伙伴通过生产 UI 路径
  创建；隔离库会话快照争用只刷新 `last_seen_at` 后重试，没有加入生产绕过。所有关键视口和截图复验通过。

第二轮复核后无未关闭 P0/P1。

## 敏感审计与禁止文件

- 正式 JSON/PNG/JUnit 对 password、Authorization、Cookie、Token、TOTP、domain challenge、注册材料、
  私钥和真实账号标识的审计无实际敏感值；截图只包含品牌运营展示数据。
- 测试中的 session、CSRF、step-up、密码和 secret 均由隔离运行时动态生成，或为不可用固定 fixture；
  不进入提交、日志、浏览器 Storage、URL 或正式 artifact。
- `.env`、`.tmp`、`.logs`、数据库、日志、Cookie jar、PEM/KEY 等禁止路径不进入提交；`.tmp` 是可整体
  删除的 pytest、浏览器和工作簿临时目录，正式证据只在 `.artifacts/prd142-*`。
- 未跟踪的 PRD-101..105 用户文档与本条无关，不暂存。提交只使用显式路径，不使用 `git add .`。

## 批次门禁

本条按 S7 节奏执行 0134、受影响 API/Console、静态、真实浏览器、路线图和敏感审计，没有重复执行完整
G7。PRD-143..147 继续共享 Platform owner、权限、迁移和前端组件；PRD-147 独立提交后统一执行数据库
矩阵、跨域集成/FLOW、三浏览器三缩放、百万事件性能、长稳、安全、兼容、旧入口四层归零和必要回归。
PRD-142 专项通过不能替代 G7，也不能宣称公开 GA；当前 G7 状态仍为 `pending`。
