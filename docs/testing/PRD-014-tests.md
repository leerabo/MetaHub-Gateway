# PRD-014 Provider KeyPool 基础管理测试说明

## 覆盖范围

- 控制面 KeyPool API：`GET /providers/{id}/keys`、`POST /providers/{id}/keys`、`PATCH /provider-keys/{id}`。
- 数据面 KeyPool：数据库 Key 解密选择、禁用/限流状态跳过、环境变量 fallback、上游 429 临时摘除。
- 前端控制台：Provider 面板内嵌 KeyPool 列表、创建表单和启停表单。
- 安全约束：Provider Key 明文不回显、不明文落库、不进入 request_logs/usage_ledger/日志。

## 自动化测试

```powershell
$env:METAHUB_PYTHON="C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $env:METAHUB_PYTHON -m pytest `
  control-plane-api/tests/integration/test_db_migrations.py `
  control-plane-api/tests/integration/test_provider_keys.py `
  gateway-data-plane/tests/unit/test_provider_keypool.py `
  gateway-data-plane/tests/unit/test_provider_adapters.py `
  gateway-data-plane/tests/integration/test_openai_compatible_api.py

C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test console-web/tests/unit/providers.test.mjs
```

## 断言要点

- admin 可创建、读取和禁用 Provider Key；developer/viewer 调用 KeyPool API 返回 `auth.permission_denied`。
- `plain_value` 只出现在创建请求体，响应中不包含 `plain_value` 或 `encrypted_key`。
- 数据库 `provider_credentials.encrypted_key` 使用 `aesgcm-v1:` 格式，dump 中不包含明文 Key。
- 重复 Key 返回 `provider_key.duplicate`；非法明文、权重、状态和空 PATCH 返回对应 `provider_key.*` 错误码。
- 数据面优先选择数据库 Key，跳过 disabled Key；当存在可管理 Key 但都不可用时返回 `provider_key.unavailable`。
- 上游 429 返回统一 `gateway.upstream_error`，同时把本次使用的数据库 Key 标记为 `rate_limited` 并写入最近错误字段。
- 控制台 Provider 面板渲染 KeyPool 表单和 Key 元数据，不渲染明文 Provider Key。
