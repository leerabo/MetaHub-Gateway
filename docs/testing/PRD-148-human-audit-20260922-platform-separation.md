# PRD-148 平台运营与组织管理员分域复核（2026-09-22）

本批专门复核 SaaS 平台运营管理员与企业 Workspace 组织管理员的身份域和页面边界。两者不是同一类管理员：平台运营使用 Platform session，组织管理员使用 Workspace session；平台运营页面的供应商、平台账务、发布门禁和控制台迁移不属于组织管理员页面，组织的 Provider、模型、路由、员工 KEY、预算、Webhook 和私有 Gateway 也不属于 SaaS 平台运营证据。

## SaaS 平台运营管理员

隔离 Chromium 旅程通过：先完成平台 bootstrap，再清除 Workspace session/CSRF cookie，建立 Platform session，进入控制台迁移页并刷新状态。迁移状态读取 2/2 为 200，1440x900、1280x720、390x844 三个视口均无横向溢出、控件裁切、控制台错误或请求失败。证据为 `.artifacts/product-qa/20260922-platform-r5/prd147-platform-chromium-scale-1.json`。

首次失败的原因是测试上下文同时保留 Workspace cookie，迟到的 Workspace page-view 遥测触发 `workspace_session.credential_conflict`；这不是平台页面失败，但说明走查必须保持两个凭据域隔离。修正后的脚本为 `tests/product_qa/web/prd147PlatformJourney.mjs`。初始未认证 `/platform/session` 的 401 是平台登录门禁探测，已单独排除，不当作平台业务请求错误。

## Workspace 组织侧

Workspace organization admin、developer 和 viewer/employee 使用独立 Workspace session 走查。当前角色授权、Provider/模型/路由、员工 KEY、预算、Webhook、Support、Device、Partner 和私有 Gateway 证据分别保存在既有角色分区和有数据域目录中。本批不把这些结果合并为 SaaS 平台运营通过。

## 结果分类

- 已通过：平台迁移浏览器路径、Workspace 分角色页面与关键交互、当前隔离五域负向集合。
- 真实失败：历史 Chromium 登出 15 秒未回首页；历史 Workspace page-view 并发失败继续保留。
- 测试夹具/运行器问题：混用两个会话域的旧平台脚本已修正；统一角色运行器容量超时继续单列。
- 迁移基线漂移：要求的 `062ae8e3...f4c2a` 基线缺失。
- 证据不足：真实注册到付费/额度、企业治理与私有 Gateway 生产路径、Partner/Device 生产数据、发布复核/回滚、财务/隐私/安全/迁移对账、HEAD/origin 一致。

用户已明确不执行六方会签和 scheduler-enabled 连续 259200 秒观察，因此两项记录为“未执行，不计证据”。PRD-148 保持未完成，`ga_approval=false`。
