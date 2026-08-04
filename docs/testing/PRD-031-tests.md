# PRD-031 测试记录

PRD：`PRD-031 - 价格表与生效时间`

## 覆盖范围

- 数据迁移：`0022_model_price_effective_time.sql` 创建 `model_price_plans`、`model_price_events`，扩展 `price_snapshots.price_plan_id/effective_from`；down migration 保留 PRD-017 原快照和 Usage。
- 主流程：admin 创建当前与未来价格，developer 查看价格时间线，状态动态显示 `current/scheduled/historical`。
- 数据面：Gateway 按服务器 UTC 入账时刻选择最近已生效 PricePlan，没有计划时回退模型目录兼容价格。
- 历史不可变：生效前 Usage 使用旧计划，生效后新 Usage 使用新计划；旧成本、版本、计划和快照 ID 均不改变。
- 异常与权限：developer 写入、viewer 读取由后端拒绝；重复生效时刻返回 409；NaN 等非法金额返回 422。
- 安全：`price.updated` 事件和 Webhook 不包含 Provider Key、Virtual Key、Authorization、Prompt 或 Response。
- 控制台：模型价格导航、模型/状态筛选、admin 新增价格、developer 只读和 viewer 隐藏均有前端单测。
- 中文注释：Python、JavaScript、SQL、测试定义和关键租户/权限/金额/错误分支全部纳入注释门禁。

## 已执行命令

```powershell
npm run lint
npm test
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_model_prices.py control-plane-api\tests\integration\test_db_migrations.py control-plane-api\tests\integration\test_console_navigation.py gateway-data-plane\tests\integration\test_request_log_minimal_loop.py gateway-data-plane\tests\integration\test_budget_guard.py -q -p no:cacheprovider
python -m pytest control-plane-api\tests -q -p no:cacheprovider
python -m pytest gateway-data-plane\tests -q -p no:cacheprovider
python -m compileall -q shared-sdk\python control-plane-api\src gateway-data-plane\src
python scripts\ci\check-comments.py
```

结果：

- 前端 lint：`console lint ok (78 files)`。
- 前端单元测试：108 passed。
- PRD-031 聚焦后端回归：14 passed。
- 控制面全量测试：72 passed。
- 数据面全量测试：55 passed。
- Python 源码编译：通过。
- 中文注释门禁：`comment coverage ok`。
- 本地运行验证：0022 全新库迁移与 seed 成功，Gateway/Control API 健康检查为 `ok`，`GET /model-prices` 返回统一 `request_id`。
