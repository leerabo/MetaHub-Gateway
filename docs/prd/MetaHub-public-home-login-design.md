# MetaHub 公开主页与登录体验

日期：2026-09-09。范围：公开产品入口、账号访问页的视觉及控制台入口兼容。

## 设计

- `/` 与 `/index.html` 展示公开主页；新增 `/console.html` 保留现有控制台。旧根路径业务 hash 迁移到控制台，查询参数及 hash 原样保留。
- 主页以 MetaHub 为唯一 H1，黑白冷灰搭配少量翠绿。无边框 Three.js 网关场景表现多路接入、统一出口；不展示虚构实时数据或可用率。
- 八项能力：BYOK、统一 Key、策略路由、敏感数据防护、精细化财务、边缘 Agent、团队权限、可观测性。边缘 Agent 不等同于通用智能体编排。
- 登录、注册、找回密码共用品牌视觉；现有 Cookie、Workspace、MFA、风险挑战、Host 品牌覆盖和账号安全控制器保持不变。已登录账号提供明确的控制台入口。
- 动画可暂停，尊重 prefers-reduced-motion，页面隐藏时停帧，WebGL 不可用时保留静态视觉。核心文案和登录链接不依赖 JavaScript 或 WebGL。
- 本地提供固定版本 Three.js 与 Lucide 图标资产，不增加运行时 CDN、追踪脚本或模型请求。

## 第一轮设计复核：产品与路由

已核对现有 virtual-keys、routing-policies、dlp、finops-ledger、budgets、observability、private-gateways/edge-operations 和 Workspace 身份模块。公开文案限定为这些模块的能力范围，不承诺未验收的性能、认证或 72 小时稳定性。公开锚点与业务 hash 分开；保留邀请等历史深链接。

## 第二轮设计复核：安全与交付

公开主页无鉴权 API 调用，无凭据存储。身份页复用现有控制器，不引入可控 redirect URL。新增资源必须纳入生产资产白名单与哈希契约；手机、桌面、键盘、动画暂停和登录入口需要实际浏览器验证。此前后台可靠性文档与用户文件保持原样。
