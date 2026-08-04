# PRD-023 测试记录 - Key/租户日预算与告警

## 覆盖范围

- 控制面预算 API：`GET /budgets`、`PATCH /budgets`、`GET /budget-status`。
- 数据面预算热路径：80% 告警、100% 阻断、仅告警放行、告警事件去重和失败请求日志。
- 调用日志：列表/详情返回预算命中摘要，详情包含 `budget_evaluations`。
- 控制台：预算告警页、用量概览预算摘要、开发者只读、viewer 隐藏。
- 迁移：`0015_budget_rules.sql` 和 down 迁移。
- 注释：全仓 `.py/.js/.mjs/.sql/.css` 中文注释门禁。

## 已执行命令

```powershell
python -m pytest control-plane-api\tests\integration\test_db_migrations.py -q
python -m pytest control-plane-api\tests\integration\test_budgets.py -q
python -m pytest gateway-data-plane\tests\unit\test_budget_service.py -q
python -m pytest gateway-data-plane\tests\integration\test_budget_guard.py -q
python -m pytest control-plane-api\tests\integration\test_request_logs.py -q
python -m pytest control-plane-api\tests\integration\test_budgets.py control-plane-api\tests\integration\test_request_logs.py control-plane-api\tests\integration\test_console_navigation.py control-plane-api\tests\integration\test_db_migrations.py -q
python -m pytest gateway-data-plane\tests\unit\test_budget_service.py gateway-data-plane\tests\integration\test_budget_guard.py -q
node --test console-web\tests\unit\budgets.test.mjs console-web\tests\unit\requestLogs.test.mjs
node --test console-web\tests\unit\usage.test.mjs console-web\tests\unit\budgets.test.mjs
node --test console-web\tests\unit\*.test.mjs
node console-web\scripts\lint.mjs
python scripts\ci\check-comments.py
python -m compileall shared-sdk\python control-plane-api\src gateway-data-plane\src
python -m pytest -q
```

## 当前结果

- 迁移测试：通过，`0015` 预算迁移和回滚断言已覆盖。
- 控制面预算聚焦测试：通过，覆盖 admin 写入、developer 只读、viewer 拒绝和异常参数。
- 数据面预算测试：通过，覆盖阻断前不调用上游、80% 告警去重、业务日边界和日志上下文。
- 调用日志测试：通过，覆盖预算字段和预算评估详情。
- 控制台测试：通过，覆盖预算页面、用量概览摘要和日志详情预算卡片。
- 控制台 lint：通过。
- 中文注释门禁：通过。
- Python 编译检查：通过。
- 后端全量 pytest：通过，100 个测试全部通过。
- 前端全量 Node test：通过，74 个测试全部通过。
