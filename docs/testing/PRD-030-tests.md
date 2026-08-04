# PRD-030 测试记录

PRD：`PRD-030 - dev/test/prod环境隔离`

## 覆盖范围

- 数据迁移：`0021_environment_isolation.sql` 创建 Environment 与事件表，回填历史环境代码，扩展 Key、别名、预算、路由、日志和用量环境字段；down migration 可恢复旧结构。
- 控制面主流程：admin 创建环境，同一别名在 dev/test 指向不同模型，Virtual Key 和预算使用稳定 `environment_id`，developer 可只读查看。
- 数据面隔离：Gateway 只从 Virtual Key 恢复 Environment，按环境解析别名和预算；禁用环境在 Provider 调用前阻断并保留失败日志归因。
- 异常与权限：developer 创建环境、viewer 读取环境均由后端拒绝；生产 Key 在测试调试台中使用返回 `debug.environment_mismatch` 和统一 `request_id`。
- 可观测性：调用日志、路由决策、用量账本、预算事件和评估记录环境；`environment.created` 进入安全事件与 Webhook 白名单。
- 控制台：环境设置页 admin 可创建/启停、developer 只读、viewer 隐藏；Key、别名、预算、调试台使用环境选择，日志与用量支持环境筛选。
- 中文注释：新增 Python、JavaScript、SQL 和测试定义及关键权限、租户、环境、错误分支纳入 `scripts/ci/check-comments.py`。

## 已执行命令

```powershell
npm run lint
npm test
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_environments.py control-plane-api\tests\integration\test_request_logs.py gateway-data-plane\tests\integration\test_budget_guard.py control-plane-api\tests\integration\test_virtual_keys.py control-plane-api\tests\integration\test_db_migrations.py control-plane-api\tests\integration\test_console_navigation.py -q
python -m pytest control-plane-api\tests -q -p no:cacheprovider
python -m pytest gateway-data-plane\tests -q -p no:cacheprovider
python -m compileall -q shared-sdk\python control-plane-api\src gateway-data-plane\src
python scripts\ci\check-comments.py
```

结果：

- 前端 lint：`console lint ok (75 files)`。
- 前端单元测试：103 passed。
- PRD-030 聚焦后端回归：17 passed。
- 控制面全量测试：70 passed。
- 数据面全量测试：55 passed。
- Python 源码编译：通过。
- 中文注释门禁：`comment coverage ok`。
