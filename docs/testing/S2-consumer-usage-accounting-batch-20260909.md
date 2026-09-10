# 使用者用量与组织会话批次验证

日期：2026-09-09。原 Excel 保持只读；本批关联 PRD-094、099、144、147，不代替完整 PRD 或发布门禁。

## 数据库与接口

- SQLite `.tmp/pytest-workspace-usage-0q7xz59m/results.xml`：11 passed，0 failures/errors，104.990 秒。包含 5 个组织会话用例与 6 个既有 Usage/Export 用例，覆盖人员归属、改名/停用/轮换历史、CSRF、导出 worker、所有者隔离、财务角色人员标签裁剪、审计回滚及并发撤权。
- 原生 PostgreSQL `.artifacts/g6-security-20260907/local-workspace-native-16fa696624614a65a586d30e6b73f4bb.xml`：1 passed，111.366 秒。实际创建组织/使用者/KEY，双币种汇总、明细、异步生成与下载完成。不是 SQLite SQL 模拟，不覆盖全库性能。
- 共享缓存边界 31 passed；本地 HTTP 验收中发现后修复，独立提交 `57a334f`，详情见 PRD-099 usage-cache 记录。

## 前端与布局

- `.artifacts/g6-security-20260907/workspace-usage-frontend-final-20260909.xml`：299 testcase，0 failure/error/skipped；包含 Usage 与 resource pages。
- `.artifacts/g6-security-20260907/workspace-usage-authentication-final-20260909.xml`：94 testcase，0 failure/error；资源认证运输层单独运行。合计 393 项，不把第一次命令中不存在的 ResourceTransport 文件名算入覆盖。
- Console lint 812 文件，生产表面 271 模块，构建 289 资产通过；前后端资产集摘要均为 `1acdc0434d619c4c81b98b3d53f18743864598fb084be3c489672f589a6fba31`。
- 真实 renderer/CSS 的隔离夹具在 390x844、1280x900、1440x900 均目视检查。页面无横向溢出，明细桌面允许表内横向滚动；移动摘要每项显示请求/Token/成本标签，14px 标签和数值可读，标题 16px。
- 截图位于 `.artifacts/g6-security-20260907/usage-ui-mobile-390-viewport.png`、`usage-ui-mobile-consumers-390.png`、`usage-ui-desktop-1280.png`、`usage-ui-desktop-1440.png`。全页截图存在拼接重复，`usage-ui-mobile-390-final.png` 不作为最终视觉证据。隔离夹具不是浏览器真实 API 提交证明。
- 临时浏览器视口已复原、标签已关闭，61605 夹具进程已停止；用户原标签和账号状态未改动。

## 本地发布与短性能

仅更新 Control，最终 PID 20392，`/health` 为 200。Gateway 6956、Console 336、Status 6892 保持原进程。没有新增迁移或恢复旧数据库。

`.artifacts/g6-security-20260907/workspace-usage-local-http-20260909095523.json` 保留 77 个检查点：注册服务建立隔离测试账号，随后真实 HTTP 创建组织、确认 Owner、创建无登录使用者/KEY、读取 summary/records、CSV/幂等重放、非法参数及权限拒绝。30 次/端点、空账本 p95：summary 273.105ms，records 237.435ms。该结果不证明大账本或高并发 SLO。

不调用上游模型；测试 KEY 和使用者已停用，GlobalSession 注销。保留测试组织及审计，不物理删除用户或业务事实。早期失败分别为产品缓存头缺失与脚本 400/422 断言不匹配，不抹去其发生事实。

## 独立归属与剩余工作

PRD-094 归属会话和人员身份；PRD-099 归属用量/币种/导出；PRD-144 归属平台商业账与内部费用分域；PRD-147 归属页面运输和生命周期。各自有本批测试/敏感审计文件，共用上述原始报告，不复制或改写运行结果。

仍需完整真实浏览器组织业务流程、使用者到模型路由/调用/账本的端到端链路、规模查询性能、其他财务页接线，以及按依赖完成尚未提交功能的独立提交。完整 Excel/G6/G7/G8 和交付后的 72 小时长稳保持未完成，不等待长稳才交付已验证功能。
