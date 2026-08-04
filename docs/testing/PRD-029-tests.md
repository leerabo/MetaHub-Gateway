# PRD-029 测试记录

PRD：`PRD-029 - Project与Application模型`

## 覆盖范围

- 数据迁移：`0020_projects_applications.sql` 创建 `projects`、`applications`、`project_events`，扩展 Virtual Key、调用日志、用量账本和预算 scope；down migration 能回滚项目/应用表和新增列。
- 控制面主流程：admin 创建 Project/Application，绑定 Virtual Key，日志按项目过滤，用量按项目/应用聚合，项目预算状态按账本归因统计。
- 权限与异常：viewer 被后端拒绝读取项目，developer 被后端拒绝创建项目，非法项目编码返回统一错误码，应用与项目不匹配的 Key 绑定返回 `application.project_mismatch`。
- 数据面归因：鉴权上下文带出 `project_id`、`application_id`，request log 与 usage ledger 写入请求发生时的归属快照，预算准入加载 project/application 规则。
- 控制台：新增“项目与应用”导航与面板，Virtual Key 创建表单提交 `project_id/application_id`，调用日志和用量筛选提交项目/应用参数，预算表单支持 project/application scope。
- 中文注释门禁：新增 Python、JS、SQL 定义、函数和关键权限/错误/归属分支均通过 `scripts/ci/check-comments.py`。

## 已执行命令

```powershell
npm test
npm run lint
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_projects_applications.py control-plane-api\tests\integration\test_db_migrations.py control-plane-api\tests\integration\test_console_navigation.py -q --basetemp '.tmp\pytest-prd029-final' -p no:cacheprovider
python -m pytest control-plane-api\tests -q --basetemp '.tmp\pytest-control-prd029' -p no:cacheprovider
python -m pytest gateway-data-plane\tests -q --basetemp '.tmp\pytest-gateway-prd029' -p no:cacheprovider
python scripts\ci\check-comments.py
```

结果：

- 前端 Node 单测：99 passed。
- 前端 lint：`console lint ok (72 files)`。
- PRD-029 focused 后端集成：9 passed。
- 控制面全量 pytest：通过。
- 数据面全量 pytest：通过。
- 中文注释门禁：`comment coverage ok`。
