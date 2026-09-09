# PRD-099 用量缓存边界测试

日期：2026-09-09。仅覆盖用量缓存修复，组织会话和使用者归集的整批证据另见 S2 批次记录。

- `shared-sdk/python/tests/test_workspace_usage_cache_boundary.py`：31 passed，0 failures，0 errors，2.75 秒。报告 `.artifacts/g6-security-20260907/workspace-usage-cache-boundary-20260909.xml`。
- 五个路径分别覆盖 200、401、403、404、422 和最外层 500；包括已有 public 缓存指令被覆盖、未知用量路径 404 禁缓存和近似前缀不受影响。
- 本地 Control 加载修复后，独立 HTTP 脚本通过 77 项检查。报告 `.artifacts/g6-security-20260907/workspace-usage-local-http-20260909095523.json`。用量读取、空账本 CSV、重放、非法筛选、无 CSRF、匿名和员工 KEY 均验证状态及 no-store。
- 初次 HTTP 验收发现缺少响应头，修复后第二次在脚本错误的 400 断言失败；正式合同为 `usage.export_filter_invalid` 422。修正断言后最终整段通过，未修改产品错误码。
- 本次不迁移数据库，不调用上游模型，不操作用户浏览器会话。测试账号会话退出，所建测试使用者和 KEY 停用，审计事实保留。

本提交仅包含共享缓存分支、针对性测试及独立记录，不包含尚未提交的组织/使用者/用量功能。没有以本次通过声明 Excel 全部完成。
