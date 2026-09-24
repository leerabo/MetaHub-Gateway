# PRD-148 中文管理员人类式走查（2026-09-22）

本批使用隔离 seeded 数据库和 Chromium 1440x900，以中文管理员身份走查当前 Workspace 授权入口。检查页面进入、授权边界、请求与响应、控制台错误、横向溢出、控件裁切、交互重叠以及可逆控件操作。

## 结果

- 管理员授权入口：47/47 通过。
- 开发者授权入口：60/60 通过。
- Viewer/员工授权入口：60/60 通过（390x844 移动视口）。
- 请求失败、响应错误、控制台错误、横向溢出、控件裁切、交互重叠：均为 0。
- 修复后定向复验：`error-maps`、`language-settings` 2/2 通过。
- 单元全量回归：2232/2232 通过。

## 集中问题与修复

发现高级工具导航分组直接显示后端枚举 `advanced`，中文用户看到英文内部值。已在 `renderConsoleShell.js` 增加分组映射，并在 `messages.js` 增加 `zh-CN` 的“高级工具”和 `en-US` 的“Advanced tools”；新增 `consoleShell.test.mjs` 契约测试。

## 证据

- 审计索引：`outputs/prd148-human-audit-current-20260922-zh-admin.json`
- 中文管理员全量：`.artifacts/product-qa/20260922-zh-admin-role-audit/prd148-role-audit-chromium-scale-1.json`
- 定向复验：`.artifacts/product-qa/20260922-zh-admin-advanced-rerun/prd148-role-audit-chromium-scale-1.json`
- 开发者分区：`.artifacts/product-qa/20260922-zh-developer-role-audit/prd148-role-audit-chromium-scale-1.json`
- Viewer/员工分区：`.artifacts/product-qa/20260922-zh-viewer-role-audit/prd148-role-audit-chromium-scale-1.json`
- 当前源码快照：`.tmp/current-source-snapshot-20260922-zh-audit.json`

构建对账：`npm run lint` 通过（841 files）；`npm run build` 通过（295 assets），当前生产资源摘要为 `ec08b64a4bf6c24ceae741e54500168d1f8fe4c77b23f1b2e46bcbda45f3fb88`。最终源码快照为 `.tmp/current-source-snapshot-20260922-final.json`，SHA256 `008f081939eb89c7e45e1ab57db93be544b3889e9103eacc283684160b5be364`。

这批是隔离浏览器产品审计，不替代真实注册到付费、生产 Partner/Device、企业治理/私有 Gateway、当前源码五域矩阵、财务对账或 HEAD/origin 对账；PRD-148 仍保持未完成。
