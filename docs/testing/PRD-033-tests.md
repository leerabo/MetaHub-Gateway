# PRD-033 测试记录

PRD：`PRD-033 - 预算耗尽阻断与恢复`

## 覆盖范围

- 迁移：0023 空库应用、重复幂等、tenant_id 字段、部分唯一索引及单步 down 回滚。
- 硬阻断：预计成本达到 100% 时在 Provider 前返回 402，并写失败调用日志、预算评估和 active BudgetBlock。
- 一致性：模型价格降低后的小请求仍由 active block 拒绝，重复请求不重复创建 block 或 blocked 事件。
- 并发一致性：准入事务串行读取并写 reservation，两个并发请求不能共同突破额度；Usage 入账后预留不重复计费。
- 账本降级：Provider 成功但 Usage 写入失败时，reservation 继续保守占用并阻断下一请求；Provider 前失败会释放预留。
- 流式计量：最终 usage chunk、无 usage 估算、客户端断连和上游流中异常均写 Usage Ledger，正文和 Key 不持久化。
- 恢复：切换 `alert_only`、提高额度、停用规则和业务日切换均释放 active block 并写 unblocked 事件。
- 恢复时效：同一 Gateway 进程内提额释放后，下一请求立即调用 Provider，无需重启或等待缓存。
- 权限隔离：developer 模式写入返回 403；其他租户 rule ID 返回 404；非法 mode 返回统一 422。
- 安全：生命周期、事件、日志和 API DTO 只包含安全 ID、金额、原因及 request_id/trace_id。
- 数据库归属：跨 tenant/Environment/rule 的 block、event、reservation 插入由触发器拒绝。
- 前端：active block 提示、admin 模式选择、developer 只读、viewer 隐藏和客户端安全路径编码。
- 中文注释：新增 Python、JavaScript、SQL 定义与关键权限、租户、金额、阻断、恢复分支通过强制门禁。

## 最终执行结果

```powershell
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_budgets.py gateway-data-plane\tests\integration\test_budget_guard.py control-plane-api\tests\integration\test_db_migrations.py -q -p no:cacheprovider
python -m pytest gateway-data-plane\tests\unit\test_error_codes.py control-plane-api\tests\integration\test_webhooks.py -q -p no:cacheprovider
npm run lint
npm test
python scripts\ci\check-comments.py
```

- PRD-033 合并聚焦测试（预算、流式、迁移、控制面）：25 passed。
- 错误码与 Webhook 聚焦测试：4 passed。
- 前端 lint：`console lint ok (78 files)`。
- 前端单元测试：108 passed。
- 中文注释门禁：`comment coverage ok`。
- 控制面全量测试：74 passed。
- 数据面全量测试：65 passed（28 unit + 37 integration）。
- Python 源码编译：通过。
- 本地迁移：开发库成功应用 0023，`budget_blocks/budget_block_events` 均存在。
- 本地运行：Gateway/Control API 健康检查为 `ok`，控制台返回 HTTP 200；真实 admin 登录、预算创建和 `PATCH /budgets/{id}/mode` 成功，响应带 request_id 且 active_block 正确为空。
- 视觉复验：1440x1000 与 390x844 视口下中文名称、规则 ID、维度、模式选择器和移动字段标签完整可读，无横向溢出或元素重叠。

说明：数据面继续拆分 unit/integration 执行，规避当前 Windows 环境下全部目录单进程组合的历史超时；两组共 65 项全部通过。
