# PRD-147 平台计量提交一致性验证

## 失败基线

2026-09-09 对修复前 HEAD `428da69` 中 platform/main.js、platformPageState.js、renderPlatformPage.js 的 Git blob 分别执行 `node --input-type=module --check`，三项全部 SyntaxError。运行工作树不含这些错误，因此旧工作树测试不能作为提交版本通过的证据。

## 修复后

- 带上下文暂存：三个文件分别保留 2、21、3 个修复 hunk，排除 2、6、1 个注册策略 hunk；复查未暂存差异仅剩注册政策，未改变真实源码或用户数据。
- 暂存文件语法检查通过。临时代码树 `5b800a57a9c5e08223e7760601f525b200457abe` 导出到 `.tmp/s1-meter-index-20260909/console-web`，未从工作树复制产品模块。
- 在上述独立目录运行 platformMeterRecovery、prd099PlatformBilling、prd095PlatformAccess、prd135ComplianceCenter：26 passed，0 failures，0 errors。结果：`.artifacts/g6-security-20260907/meter-index-alignment-20260909.xml`。
- 新增 `scripts/quality/verify-staged-javascript.mjs` 专门读取 Git index。其 3 项测试覆盖坏 index/好工作树、好 index/坏工作树、空 index、CommonJS 模式、含空格路径和非 JS 文件排除。结果：`.artifacts/g6-security-20260907/staged-js-guard-20260909.xml`，3 passed。

没有改动后端 API、数据库、运行服务或付费模型配置，不为此重复全套 SaaS 测试。该检查证明语法，不替代接口、浏览器或构建依赖闭包；本地集成资产包含其它未提交增量，仍不能称为此独立提交可复现的生产包。
