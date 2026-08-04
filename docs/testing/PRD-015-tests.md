# PRD-015 模型别名与最小路由测试说明

## 覆盖范围

- 控制面 ModelAlias API：`GET /model-aliases`、`POST /model-aliases`、`PATCH /model-aliases/{id}`。
- 数据面最小路由：按 Virtual Key 环境解析稳定别名、默认模型选择、默认模型禁用后的备用模型选择、直接内部模型 ID 兼容路径。
- 路由追踪：成功和失败请求写入 `routing_decisions`，失败请求写入 `request_logs`。
- 前端控制台：模型别名列表、创建表单、启停表单、admin/developer/viewer 权限视图。
- 安全约束：不在 `routing_decisions`、`request_logs`、`usage_ledger` 中记录 prompt/input/messages、Virtual Key 明文、Provider Key 或 Authorization。

## 自动化测试

```powershell
$env:METAHUB_PYTHON="C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $env:METAHUB_PYTHON -m pytest `
  control-plane-api/tests/integration/test_db_migrations.py `
  control-plane-api/tests/integration/test_model_aliases.py `
  gateway-data-plane/tests/integration/test_model_alias_routing.py `
  gateway-data-plane/tests/integration/test_openai_compatible_api.py

C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test console-web/tests/unit/modelAliases.test.mjs
```

## 断言要点

- admin 可以创建、读取和禁用模型别名；developer 可以读取但不能创建；viewer 直接调用后端返回 `auth.permission_denied`。
- 同租户同环境重复别名返回 `model_alias.duplicate`。
- 非法别名、非法环境、无效默认/备用模型、空 PATCH 返回对应 `model_alias.*` 错误码。
- 调用 `company-default` 时，Gateway 会把上游 payload 中的 `model` 替换成默认模型的 `provider_model_id`。
- 默认模型禁用但备用模型可用时，Gateway 选择备用模型，并记录 `decision_reason=alias.fallback`。
- 别名禁用返回 `gateway.model_alias_disabled`；默认/备用模型均不可用返回 `gateway.model_disabled`。
- `routing_decisions` 记录 `requested_model`、`selected_internal_model_id`、`provider_code`、`decision_status`、`decision_reason` 和失败 `error_code`。
- 敏感内容不进入路由决策、请求日志或用量台账。
