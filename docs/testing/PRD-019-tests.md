# PRD-019 5分钟快速接入页测试记录

## 覆盖范围

- 主流程：`GET /developer/quickstart-data` 返回当前租户 Gateway `/v1` base_url、推荐模型别名、Key 环境变量占位、步骤、Python/JavaScript/curl 示例和常见错误。
- 权限：admin/developer 可读取快速开始数据，viewer 后端返回 `auth.permission_denied`。
- 安全：响应不返回 Virtual Key 明文、`key_hash`、Provider Key 或 Authorization；示例只使用 `METAHUB_VIRTUAL_KEY`。
- 降级：缺 active Virtual Key 或 active ModelAlias 时，接口仍返回 200，并用 `action_required` 步骤引导到对应入口。
- 事件：`developer.quickstart_viewed` 写入 `developer_portal_events`，记录 `tenant_id`、`user_id`、`session_id`、`request_id` 和安全 payload。
- 前端：快速开始页展示配置摘要、步骤、示例、常见错误和复制按钮，所有后端文本均 HTML 转义。
- 注释：PRD-019 新增 Python、JavaScript、SQL、CSS 代码均通过中文注释门禁。

## 自动化测试

已运行：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py scripts\ci\check-comments.py
```

结果：`comment coverage ok`。

已运行：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m pytest control-plane-api\tests\integration\test_developer_quickstart.py control-plane-api\tests\integration\test_console_navigation.py control-plane-api\tests\integration\test_db_migrations.py -q
```

结果：`9 passed`。

已运行：

```powershell
$node='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test console-web\tests\unit\*.test.mjs
& $node console-web\scripts\lint.mjs
```

结果：控制台 Node 单元测试 `64 pass`，`console lint ok (50 files)`。

## 全量验收

最终全量 CI 命令：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:METAHUB_PYTHON=$py
powershell -ExecutionPolicy Bypass -File scripts\ci\check.ps1
```

结果：

- 中文注释门禁：`comment coverage ok`。
- 后端全量 pytest：`79 passed in 231.40s`。
- 控制台 Node 测试：`64 pass`。
- 控制台 lint：`console lint ok (50 files)`。
