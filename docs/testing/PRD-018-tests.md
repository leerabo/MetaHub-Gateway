# PRD-018 控制台骨架与导航测试记录

## 覆盖范围

- 主流程：Admin、Developer、Viewer 登录后获取各自可见导航项，默认激活 `overview`，Developer 保存偏好后重新读取能恢复 active item。
- 权限与异常：非法导航项返回 `console.navigation_invalid`，非法偏好返回 `console.preference_invalid`，事件目标不匹配或敏感 payload 返回 `console.event_invalid`。
- 数据追踪：`console.page_viewed` 写入 `console_page_events`，记录 `tenant_id`、`user_id`、`session_id`、`request_id` 和 `trace_id`。
- 前端体验：控制台主框架渲染顶部租户/用户信息、分组侧边栏、active 状态、空状态引导和只渲染当前入口的内容区。
- 安全：前端客户端不发送 `tenant_id`，事件 payload 不允许 Token、Authorization、Provider Key、Virtual Key、Prompt、Response 等敏感字段。
- 注释：PRD-018 新增 Python、JavaScript、SQL、CSS 代码均通过中文注释门禁。

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
& $py -m pytest control-plane-api\tests\integration\test_console_navigation.py control-plane-api\tests\integration\test_db_migrations.py -q
```

结果：`7 passed`，覆盖 Console API 与迁移链路。

已运行：

```powershell
$node='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test console-web\tests\unit\*.test.mjs
& $node console-web\scripts\lint.mjs
```

结果：控制台 Node 单元测试 `60 pass`，`console lint ok (47 files)`。

## 全量验收

最终全量 CI 命令：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:METAHUB_PYTHON=$py
powershell -ExecutionPolicy Bypass -File scripts\ci\check.ps1
```

结果：

- 中文注释门禁：`comment coverage ok`。
- 后端全量 pytest：`77 passed in 224.46s`。
- 控制台 Node 测试：`60 pass`。
- 控制台 lint：`console lint ok (47 files)`。
