# PRD-148 人工路径交互复验（2026-09-21）

本次使用隔离的 Playwright 浏览器脚本复验 Viewer 在 `private-gateways`、`webhooks`、`support-tickets` 和 `budgets` 四个入口的可见性、回退、键盘可操作性、布局和请求生命周期。该批不是原生 computer-use，也不代表真实商业验收。

产物：`.artifacts/product-qa/20260921-interaction-ux-classified/prd148-human-interaction-chromium-scale-1.json`。

结果为通过：4/4 目标页符合 Viewer 的授权边界；未授权入口回退到可访问页面，Support 页面保持可访问；控制台错误、服务端响应错误、硬请求失败、溢出、交互重叠和未标注控件均为 0。

其中 3 个 `POST /workspace/navigation/page-views [net::ERR_ABORTED]` 被记录在 `expected_request_cancellations`。它们发生在回退页发生下一次导航时，访问状态机主动取消陈旧的低风险 page-view 写入；对应服务端日志已返回 200。审计脚本保留这些取消记录，但只将其他 `requestfailed` 计为硬失败。

相关状态机单测和 Web 契约测试共 23 项通过。该结果只覆盖当前隔离 seeded QA 账号和浏览器交互，不闭合真实注册到付费、企业治理、六方会签、回滚、对账、72 小时稳定性或 HEAD/origin 一致性门槛。

当前源码复验补充：`.artifacts/product-qa/20260921-interaction-after-batch` 在完整路由遍历中超时，临时运行日志记录多次 `PATCH /identity/preferences/navigation` 返回 422 `identity.request_invalid`；该批没有最终 JSON，保留为真实运行失败/证据不足。聚焦管理员三路由批次 `.artifacts/product-qa/20260921-interaction-diagnostic/prd148-human-interaction-chromium-scale-1.json` 通过；开发者聚焦批次 `.artifacts/product-qa/20260921-interaction-diagnostic-developer2/prd148-human-interaction-chromium-scale-1.json` 记录一次页面关闭时仍在途的导航偏好 PATCH 被 Chromium 标记为 `net::ERR_ABORTED`，并保留为当前运行失败。当前源码摘要为 `369d9050a49e81d176fbd5a440e4d53a903ca21af3d40f02d148efec6c5dee82`，不能用上述聚焦批次替代完整交互门禁。
