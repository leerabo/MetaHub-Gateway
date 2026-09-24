# PRD-148 人类式交互复验后续（2026-09-21）

本轮继续使用隔离 Playwright 浏览器脚本，明确不是原生 computer-use。修复了交互审计器在侧栏手风琴点击后立即执行详情/打开动作造成的导航偏好 PATCH teardown 竞速：审计器现在先捕获 PATCH request，再等待对应 response，保留真实 requestfailed。

当前源码绑定：`outputs/prd148-source-current-20260921-final4.json`，SHA256 `ee904c92faed340fd4508280e8932b70c5d976a89b8b442c067ef1eb3190b3a0`，2693 个文件；生产资源摘要 `872180c966e4d921f99c5ba74fed5be9eb9023442d4305cc51fbe5570c0cf262`。

验证结果：developer 的 `models, model-aliases, model-quality-scores, experiments, projects, virtual-keys` 聚焦批次 `.artifacts/product-qa/20260921-interaction-developer-settle-fix3` 为 6/6，通过页面、授权、请求、响应、控制台、布局和标签检查；导航偏好 PATCH 失败数为 0。此前完整三角色 180 页批次 `.artifacts/product-qa/20260921-interaction-full-after-fix2` 的 6 个 PATCH `net::ERR_ABORTED` 已由该聚焦批次复现并关闭。

统一三角色 180 页批次在 1200 秒门槛内超时，运行器没有生成终态 JSON，记录为运行时批次超时，不能单独作为完整门禁证据。随后按角色/分区重跑形成当前终态：管理员 60/60（`.artifacts/product-qa/20260921-interaction-admin-partition-r4`）、开发者 6 组共 60/60（`.artifacts/product-qa/20260921-interaction-developer-group1` 至 `group6`）、查看者 6 组共 60/60（`.artifacts/product-qa/20260921-interaction-viewer-group1-r4` 至 `group6-r4`）；每组 0 请求失败、0 响应错误、0 控制台错误、0 布局/标签/授权问题。旧的 422 `identity.request_invalid` 和页面 teardown abort 产物继续保留，未被改写。

受影响单测与静态检查：前端全量单测 2211/2211、`npm run build` 294 assets、`npm run lint` 838 files 通过。PRD-148 仍未完成；真实注册到付费、企业治理/私有 Gateway、Partner/Device 数据路径、六方会签、发布后复核、回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性观察以及 HEAD/origin 一致性仍缺当前可追溯证据。
