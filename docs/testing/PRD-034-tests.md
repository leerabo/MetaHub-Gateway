# PRD-034 测试记录

PRD：`PRD-034 - 管理操作审计日志`

## 覆盖范围

- 迁移：`0024_audit_logs.sql` 空库应用、重复幂等、down 回滚、租户/操作者归属、动作白名单、JSON 结构和不可变触发器。
- 管理审计：Provider、Provider Key、Virtual Key、Virtual Key 限流、预算规则/模式和 DLP 规则创建或修改均在原业务事务内写审计。
- 原子性：审计插入或事务提交失败时业务变更同步回滚，不能出现只有业务或只有审计的半成功状态。
- 查询：`GET /audit-logs` 支持动作、对象、操作者、request_id、UTC 时间范围和分页筛选，排序稳定且只返回认证租户数据。
- 权限：仅 admin 可查询；developer/viewer 返回后端 403；控制台隐藏入口之外仍由后端执行 RBAC；没有删除 API 或删除控件。
- 脱敏：Provider Key、Virtual Key 明文/哈希、DLP 原始 pattern、自由描述、请求正文和响应正文不进入审计摘要或事件。
- 来源信息：记录直接对端合法 IP 和清洗、UTF-8 字节截断后的 User-Agent，不解析 `X-Forwarded-For`。
- 事件：`audit.recorded` 仅携带审计 ID、动作、对象和变化字段；提交后尽力投递，投递失败不反转已提交业务。
- 前端：筛选、重置、分页、展开前后摘要、复制 request_id、错误恢复、并发刷新隔离、HTML 转义和响应式布局。
- 中文注释：新增 Python、JavaScript、SQL 和测试定义及关键判断、异常、权限、租户、脱敏、事务分支通过强制门禁。

## 执行命令

```powershell
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_audit_logs.py control-plane-api\tests\integration\test_providers.py control-plane-api\tests\integration\test_virtual_keys.py control-plane-api\tests\integration\test_budgets.py control-plane-api\tests\integration\test_dlp_rules.py control-plane-api\tests\integration\test_db_migrations.py -q
python -m pytest control-plane-api\tests\unit control-plane-api\tests\integration -q
python -m pytest gateway-data-plane\tests\unit gateway-data-plane\tests\integration -q
node --test console-web\tests\unit\*.test.mjs
node console-web\scripts\lint.mjs
python -m compileall -q shared-sdk\python control-plane-api\src gateway-data-plane\src
python scripts\ci\check-comments.py
```

## 最终执行结果

- PRD-034 聚焦后端测试：23 passed。
- 控制面全量测试：80 passed。
- 数据面全量测试：65 passed。
- 前端单元测试：115 passed；lint：`console lint ok (81 files)`。
- Python 源码编译：通过。
- 中文注释门禁：`comment coverage ok`。
- 本地迁移：开发库成功应用 0024，创建审计表、索引、归属和不可变触发器。
- 本地运行：管理员将预算模式从 `alert_only` 切换为 `block` 后恢复，最终业务状态正确，并产生 2 条 `budget_rule.updated` 审计；查询响应和审计行均携带 request_id。
- 视觉复验：1440、1100、720、390 宽度下表格/卡片切换、筛选布局、完整长 ID、展开摘要和移动字段标签均可读，无横向溢出或文字重叠。
- 交互复验：错误时间范围后仍可重置并恢复 2/2 记录；单页上一页/下一页正确禁用；未发现删除入口或新的浏览器控制台异常。
- 本地访问：使用标准入口 `http://localhost:5173` 登录正常；`127.0.0.1` 不在当前默认 CORS 允许来源中，不作为本地控制台入口。
