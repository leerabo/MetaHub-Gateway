# PRD-024 测试记录 - 关键词/正则敏感词拦截

## 覆盖范围

- 控制面 DLP API：`GET /dlp/rules`、`POST /dlp/rules`、`PATCH /dlp/rules/{rule_id}`。
- 数据面 DLP 热路径：关键词/正则规则、`block` 阻断、`log_only` 放行、只扫描 `role=user` 文本、命中摘要落库。
- 调用日志：列表/详情返回 DLP 摘要，详情包含 `dlp_matches`，不返回命中文本明文。
- 控制台：DLP 规则页、admin-only 导航、日志详情 DLP 卡片、HTML 转义和敏感明文不泄露。
- 迁移：`0016_dlp_rules.sql` 和 down 迁移。
- 注释：全仓 `.py/.js/.mjs/.sql/.css` 中文注释门禁。

## 已执行命令

```powershell
python -m compileall shared-sdk\python\metahub_shared\runtime control-plane-api\src\metahub_control gateway-data-plane\src\metahub_gateway
python scripts\ci\check-comments.py
python -m pytest -q control-plane-api\tests\integration\test_dlp_rules.py control-plane-api\tests\integration\test_request_logs.py control-plane-api\tests\integration\test_console_navigation.py control-plane-api\tests\integration\test_db_migrations.py gateway-data-plane\tests\integration\test_dlp_guard.py gateway-data-plane\tests\unit\test_dlp_service.py
node --test console-web\tests\unit\dlp.test.mjs console-web\tests\unit\requestLogs.test.mjs console-web\tests\unit\budgets.test.mjs console-web\tests\unit\consoleShell.test.mjs
```

## 当前结果

- Python 编译检查：通过。
- 中文注释门禁：通过。
- 后端 PRD-024 聚焦测试：通过，16 个测试全部通过。
- 前端 PRD-024 聚焦测试：通过，21 个测试全部通过。
- 控制面权限测试：通过，developer/viewer 直接调用 DLP 管理 API 会被后端拒绝。
- 敏感信息测试：通过，DLP 命中记录、调用日志详情和前端渲染只展示哈希、长度、规则 ID 和动作，不展示命中文本明文。
