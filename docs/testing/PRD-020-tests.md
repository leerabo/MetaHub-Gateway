# PRD-020 内置 API 调试控制台测试记录

## 覆盖范围

- 主流程：`POST /debug/chat-completions` 使用当前登录用户、当前租户 active Virtual Key 和 active 模型别名发起一次非流式 Chat Completions 调试请求，返回 `debug_request_id`、`request_id`、`trace_id`、延迟、响应、usage、cost 和日志详情跳转地址。
- 调用追踪：成功请求写入 `request_logs`、`usage_ledger` 和 `debug_console_events`，可按 `request_id` 跳转调用日志详情。
- 权限：admin/developer 可调用调试接口，viewer 直接调用后端返回 `auth.permission_denied`。
- 参数校验：空 messages、非法 role、content 过长、`stream=true`、未知额外字段和敏感字段返回统一错误结构。
- 上游失败：fake upstream 失败时归一为 `gateway.upstream_error`，并写入失败 request_log 与 `debug.request_sent` failed 事件。
- 安全：请求体不接受前端传入 `tenant_id`、Virtual Key 明文、Provider Key、Authorization、token、secret、password 或 `key_hash`；事件表和日志不保存 Prompt、messages content、完整 response、Provider Key 或 Virtual Key 明文。
- 前端：控制台调试台只选择 Virtual Key 元数据和模型别名，不展示明文 Key；成功展示响应、usage、成本和日志详情入口；错误态展示统一错误码、request_id、trace_id 和 suggestion；模型输出与错误文本均 HTML 转义。
- 注释：PRD-020 新增 Python、JavaScript、SQL、CSS 代码均通过中文注释门禁，定义、函数和关键安全分支保留中文说明。

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
& $py -m pytest control-plane-api\tests\integration\test_debug_console.py control-plane-api\tests\integration\test_db_migrations.py -q
```

结果：后端 PRD-020 专项与迁移链路 8 个测试全部通过，命令正常退出。

已运行：

```powershell
$node='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test console-web\tests\unit\*.test.mjs
& $node console-web\scripts\lint.mjs
```

结果：控制台 Node 单元测试 `68 pass`，`console lint ok (53 files)`。

## 全量验收

本轮统一 CI 曾因 424 秒工具超时未返回中间输出，因此拆分为等价门禁命令重新验证：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m pytest -q
& $py -m pytest --collect-only -q
```

结果：

- 后端全量 pytest：收集 82 个测试，运行日志达到 `[100%]`，命令正常退出，错误日志为空。
- 中文注释门禁：`comment coverage ok`。
- 控制台 Node 测试：`68 pass`。
- 控制台 lint：`console lint ok (53 files)`。

## 验收结论

PRD-020 满足主流程、异常、权限、安全与追踪验收要求。调试接口具备统一错误码、request_id/trace_id、tenant_id 隔离、Provider Key/Virtual Key 脱敏、用量成本追踪和失败请求记录能力；控制台隐藏入口只是体验层，后端权限依赖已覆盖 viewer 直连场景。
