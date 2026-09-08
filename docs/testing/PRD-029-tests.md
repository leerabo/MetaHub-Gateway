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

## 2026-09-08 PostgreSQL 聚合查询修复

SaaS 页面访问的真实前置流程发现 Project 响应读取存在 PostgreSQL GROUP BY 错误。
两轮补充自审后只修改 Project/Application 列表和详情的四处 GROUP BY，加入关联表展示
字段；实体主键、COUNT(DISTINCT)、tenant WHERE/JOIN、过滤、排序和 DTO 不变。

- `prd029-aggregate-r1-junit.xml`：2/2，2.587 秒，运行实际四个服务查询；验证多应用/Key
  交叉 JOIN 去重、可空 owner、列表/详情一致、其他 tenant 行排除和同形 not_found。
  该单元使用精简数据结构与合成读取上下文，只证明 SQL 语义，不作为鉴权证据。
- `g6-page-visits-r7-91dcfa5a0c6d43bca31b59b0069028c5-junit.xml`：保留创建 Project
  后响应组装时的 42803 反例。Project 写入已提交，不能描述为事务回滚。
- `g6-page-visits-r8-abdae42db9b544a0aafa53cf8f92a681-junit.xml`：真实 PostgreSQL
  注册、建双 Workspace、空间切换、两次 Project 和一次 Application 创建返回预期结果；
  后续成员邀请被数据库 guard 拒绝，2 deselected / 1 error / 72.00 秒。这证明两个详情
  查询已在原生 API 执行，不代表邀请、列表原生流程、页面访问或整批 QA 通过。

证据目录为 `.artifacts/g6-security-20260907/`，所有失败报告独立保留。新单元 Ruff 通过；
生产文件保留既有长行，仅改四处 GROUP BY。独立 `029-project-aggregates` 敏感审计检查四个
精确暂存文件和上述三份报告，输出 `prd-029-project-aggregates-sensitive-audit.json`。
未更新 Excel、浏览器/性能/并发等批次门禁，不修改历史迁移或任何业务数据。
