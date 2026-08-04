# PRD-011 测试记录 - 模型 Endpoint Provider 注册

## 覆盖范围

- 主流程：管理员创建至少两个 Provider，列表仅返回当前租户 Provider 元数据，连接测试成功后写入 `health_status='healthy'`。
- 失败流程：连接测试遇到 401/403 时返回失败原因，写入 `health_status='unhealthy'` 和 `last_test_error`。
- 权限流程：developer/viewer 直接访问 `/providers` 后端接口返回 `auth.permission_denied`。
- 字段校验：非法 `base_url`、非法 `provider_type`、非法 `auth_type`、重复 `provider_code`、空 PATCH、未知 Provider 均返回统一 `provider.*` 错误码。
- 安全约束：Provider API 不接收 Provider Key 明文字段，列表响应不包含 `key_hash`、`plain_value` 或 `sk-` 风格密钥。
- 迁移约束：`0005_provider_metadata.sql` 可应用、可幂等跳过、可单步回滚；回滚 0005 不影响 0004 的 Virtual Key 字段。
- 控制台：Provider 面板仅 admin 可见，支持创建、测试连接、启停动作，错误信息和不可信字段经过 HTML 转义。

## 自动化测试

```powershell
& 'C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest control-plane-api/tests/integration/test_providers.py control-plane-api/tests/integration/test_db_migrations.py -q
npm --prefix console-web test -- providers.test.mjs
```

当前结果：

- 后端定向测试：`7 passed`
- 前端定向测试：`36 passed`
- 全量检查：后端 `49 passed`，前端 `36 passed`，console lint `32 files ok`

## 真实服务验收

本地服务：

- Console Web：`http://localhost:5173`
- Gateway Data Plane：`http://localhost:8000`
- Control Plane API：`http://localhost:8001`
- Fake OpenAI-compatible upstream：`http://127.0.0.1:18080/v1`

验收结果：

- HTTP 主流程：管理员创建成功 Provider 和失败 Provider；成功 Provider 连接测试返回 `success` 且 `health_status='healthy'`；失败 Provider 返回失败原因并写入 `health_status='unhealthy'`；禁用 Provider 后返回 `status='disabled'` 和非空 `disabled_at`。
- HTTP 权限：developer 直接调用 `GET /providers` 返回 403 `auth.permission_denied`。
- 浏览器主流程：admin 登录后可见 Provider 管理面板；页面创建 `prd011_browser_*` Provider 后列表数量增加；点击“测试”后显示 `Endpoint is reachable.`，行状态变为 `healthy`；点击“禁用”后行状态变为 `disabled`，操作按钮变为“启用”。
- 浏览器安全：页面 console warn/error 日志为空；Provider 面板没有任何 Provider Key 明文字段。

## 验收要点

- 所有 `/providers` 错误响应都包含统一 `code/message/request_id/trace_id/suggestion`。
- 所有 Provider 业务数据均写入当前认证上下文的 `tenant_id`。
- Provider Key 不进入 Provider 注册 API、响应、数据库 Provider 元数据或日志 payload。
- Provider 连接测试失败不会抛出未处理异常，而是返回结构化 `test_result` 并更新健康字段。
- 控制台隐藏入口只作为体验优化，真实权限由后端 `require_current_admin` 强制执行。
