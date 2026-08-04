# PRD-026 测试记录 - MVP总览Dashboard

## 覆盖范围

- 控制面 Dashboard API：`GET /dashboard/overview`。
- 指标聚合：今日请求数、成功率、错误率、P95 延迟、今日成本、总 Token、Top 模型、Top Virtual Key 和最近错误。
- 数据来源：指标来自当前租户 `request_logs` 和 `usage_ledger`，不新增 Dashboard 缓存表。
- 权限与租户边界：未登录返回统一认证错误；admin/developer/viewer 可读取安全聚合摘要；其他租户数据不进入指标。
- 安全展示：Top Virtual Key 只展示名称和 `key_prefix`；最近错误不展示 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key。
- 控制台事件：`console_page_events.event_type` 支持 `dashboard.viewed`，payload 仍走白名单过滤。
- 控制台：概览页渲染 Dashboard、空状态接入引导、错误面板、HTML 转义和 viewer 日志详情跳转隐藏。
- 迁移：`0018_dashboard_overview.sql` 和 down 迁移、Dashboard 查询索引、`dashboard.viewed` CHECK 约束。
- 注释：全仓 `.py/.js/.mjs/.sql/.css` 中文注释门禁。

## 已执行命令

```powershell
python -m compileall shared-sdk\python\metahub_shared\runtime control-plane-api\src\metahub_control control-plane-api\tests\integration\test_dashboard.py control-plane-api\tests\integration\test_db_migrations.py
python scripts\ci\check-comments.py
python -m pytest control-plane-api\tests\integration\test_dashboard.py control-plane-api\tests\integration\test_db_migrations.py -q --basetemp D:\MetaHub\.tmp\pytest-prd026-<guid> -p no:cacheprovider
node --test console-web\tests\unit\dashboard.test.mjs
node --test console-web\tests\unit\*.mjs
node scripts\lint.mjs  # 在 console-web 目录执行
python -m pytest -q --basetemp D:\MetaHub\.tmp\pytest-full-<guid> -p no:cacheprovider
```

## 当前结果

- 中文注释门禁：通过，覆盖新增 Dashboard 后端、前端、测试和迁移文件。
- 后端 PRD-026 聚焦测试：通过，覆盖真实日志/usage 聚合、空状态、viewer 只读、未登录认证错误、租户隔离、`dashboard.viewed` 事件和 0018 回滚链。
- 前端 PRD-026 聚焦测试：通过，覆盖 Dashboard API client、统一错误映射、KPI/排行/最近错误渲染、HTML 转义、空状态和 viewer 日志跳转隐藏。
- 后端全量 pytest：通过。Windows 下使用唯一 `--basetemp`，避免旧临时目录锁导致 pytest 启动前清理失败。
- 前端全量 Node test：通过，90 个测试全部通过。
- 控制台 lint：通过；曾发现 Dashboard 前端文件缺尾部换行，已修复并重跑。
