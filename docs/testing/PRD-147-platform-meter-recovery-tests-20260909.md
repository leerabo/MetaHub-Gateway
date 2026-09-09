# PRD-147 平台计量恢复页面测试

`.artifacts/g6-security-20260907/meter-recovery-frontend-r1-20260909.xml`：22 项通过，包括 7 项恢复专项、原 PRD-099 账单客户端/渲染及 PRD-095 平台身份回归。

专项覆盖严格 payload、敏感额外字段拒绝、未知/不一致状态、CSRF/幂等/无 Bearer、权限裁剪、非默认归档、旧 DOM 目标、重复提交、网络不确定失败、离页迟到响应及旧 Session 覆盖新身份的回归。

真实浏览器加载当前生产源码和 CSS，但 API 使用隔离 synthetic DTO。1280px 桌面和 390px iframe 的页面 overflow=false、formClipped=false、minLabelPx=14、action为空、confirmed=false。不是认证浏览器 E2E。截图：

- `.artifacts/g6-security-20260907/meter-recovery-desktop-20260909.png`
- `.artifacts/g6-security-20260907/meter-recovery-mobile-top-20260909.png`
- `.artifacts/g6-security-20260907/meter-recovery-mobile-form-20260909.png`

生产图遍历验证通过：271 modules / 289 shipped sources。仅验证源码图，不证明当前运行的 dist 已包含此次改动。

## 后续运行发布验收

- `meter-manifest-r1-20260909.xml`：前后端 manifest 完整一致性测试 1 passed。
- `meter-release-http-20260909.json`：HTTP 实际读取 289 项资源，逐项长度和 SHA256 与 dist 记录完全一致；资产集摘要 `676a0e8b92f173faf796639fedf3887937483a81cec9dd5579b918cf9f8d3cea`。
- 首次运行验收发现 5173 提供源码目录，`/production-assets.json` 返回 404；之后切换为 `console-web/dist`，新前端 PID 22020，保留原访问地址。Control PID 21572，Gateway/Status 未重启。
- 实际浏览器平台入口显示 TOTP 验证门禁，没有替换用户会话或读取用户 MFA 秘密。这只证明门禁渲染，不是认证浏览器恢复操作验收。真实 MFA API 操作证据归 PRD-099。
- 本次只更新部署记录；当前生产 manifest 包含此前未提交注册隐私页等业务域的汇总资产变化，需随依赖域提交整理，不将混合 manifest 冒充本 PRD 独立代码提交。
