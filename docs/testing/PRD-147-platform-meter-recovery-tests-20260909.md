# PRD-147 平台计量恢复页面测试

`.artifacts/g6-security-20260907/meter-recovery-frontend-r1-20260909.xml`：22 项通过，包括 7 项恢复专项、原 PRD-099 账单客户端/渲染及 PRD-095 平台身份回归。

专项覆盖严格 payload、敏感额外字段拒绝、未知/不一致状态、CSRF/幂等/无 Bearer、权限裁剪、非默认归档、旧 DOM 目标、重复提交、网络不确定失败、离页迟到响应及旧 Session 覆盖新身份的回归。

真实浏览器加载当前生产源码和 CSS，但 API 使用隔离 synthetic DTO。1280px 桌面和 390px iframe 的页面 overflow=false、formClipped=false、minLabelPx=14、action为空、confirmed=false。不是认证浏览器 E2E。截图：

- `.artifacts/g6-security-20260907/meter-recovery-desktop-20260909.png`
- `.artifacts/g6-security-20260907/meter-recovery-mobile-top-20260909.png`
- `.artifacts/g6-security-20260907/meter-recovery-mobile-form-20260909.png`

生产图遍历验证通过：271 modules / 289 shipped sources。仅验证源码图，不证明当前运行的 dist 已包含此次改动。
