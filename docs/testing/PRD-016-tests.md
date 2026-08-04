# PRD-016 Request Log 最小闭环测试记录

## 覆盖范围

- 主流程：成功 Chat Completions 调用写入 `request_logs`、`usage_ledger`，并回写 endpoint、请求模型、解析模型、Provider、latency、token、成本估算和价格版本。
- 异常流程：鉴权成功后的请求体验证失败写入失败日志，保留 `request_model`、`endpoint` 和统一 `error_code`。
- 写库容错：日志库写入失败时输出 `request_log.write_failed` warning，不把异常抛回网关热路径。
- 控制面查询：`GET /request-logs` 支持 request_id/status/model/provider_code/error_code 过滤和分页，`GET /request-logs/{request_id}` 返回详情、路由决策和用量摘要。
- 权限：admin/developer 可读调用日志，viewer 后端返回 `auth.permission_denied`。
- 安全：响应不回显 raw usage 中的敏感样本，不返回 Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。
- 前端：调用日志 client、列表、详情、过滤、角色隐藏、HTML 转义和敏感字段不展示。
- 注释：PRD-016 新增核心代码已补中文注释，关键判断覆盖租户隔离、权限、脱敏和写库失败降级。

## 已运行命令

```powershell
node --test console-web/tests/unit/requestLogs.test.mjs
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; & $env:METAHUB_PYTHON -m pytest control-plane-api/tests/integration/test_request_logs.py gateway-data-plane/tests/integration/test_request_log_minimal_loop.py control-plane-api/tests/integration/test_db_migrations.py -q
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'; .\scripts\ci\check.ps1
```

## 验收结果

- 前端新增单测：5 passed。
- 后端 PRD-016 专项与迁移测试：10 passed。
- 完整 CI：后端 pytest 72 passed，前端 Node test 51 passed，console lint 41 files ok。
- 浏览器验收：本地控制台以 admin 登录后，调用日志列表和详情均显示 `req_prd016_live_console`；列表展示 `/v1/chat/completions`、`default-chat`、`local_openai`、`10 tokens / 0 CNY`，详情展示路由决策、输入/输出/总 token、价格版本 `seed-v1`。页面未出现测试 Prompt `prd016 private live prompt`、完整 Virtual Key `mhv_dev_seed_virtual_key` 或 Provider Key `sk-local-prd016-provider`。
