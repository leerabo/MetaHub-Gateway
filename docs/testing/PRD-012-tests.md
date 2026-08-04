# PRD-012 测试说明 - 模型目录与内部标准模型ID

## 覆盖范围

- 控制面 `GET /models`、`POST /models`、`PATCH /models/{id}`。
- `ModelCatalog` 字段：`internal_model_id`、`provider_model_id`、能力标签、上下文长度、输入/输出价格、币种和状态。
- 后端权限：admin 可创建/编辑，developer 只读，viewer 拒绝。
- 数据面映射：OpenAI-compatible 请求体 `model` 使用内部标准模型 ID，Gateway 转发到上游时替换为 `provider_model_id`。
- 控制台：admin 可见创建/启停入口，developer 只读，viewer 不展示模型目录。

## 建议命令

```powershell
$env:METAHUB_PYTHON="C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $env:METAHUB_PYTHON -m pytest control-plane-api/tests/integration/test_models.py gateway-data-plane/tests/integration/test_openai_compatible_api.py -q
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --test console-web/tests/unit/models.test.mjs
.\scripts\ci\check.ps1
```

## 主流程用例

- `test_model_catalog_create_list_update_and_disable_flow`
  - admin 创建模型目录记录。
  - developer 读取模型目录。
  - admin 更新 `provider_model_id`、上下文长度、输出价格并禁用模型。
  - 验证 `upstream_model_name` 与 `provider_model_id` 同步。
- `test_internal_model_id_routes_to_provider_model_id_and_respects_disabled_status`
  - 请求 `model=prd012-internal-chat`。
  - 上游收到 `provider-real-chat-model`。
  - 禁用模型后数据面返回 `gateway.model_not_found`。
- `models.test.mjs`
  - 前端 client 不提交 `tenant_id`。
  - admin/developer/viewer 三类控制台视图符合后端权限边界。

## 异常与权限用例

- developer 调用 `POST /models` 返回 `auth.permission_denied`。
- viewer 调用 `GET /models` 返回 `auth.permission_denied`。
- 重复 `internal_model_id` 返回 `model.internal_id_exists`。
- 非当前租户或不存在的 Provider 返回 `model.provider_invalid`。
- 非法能力标签、负价格、负上下文长度分别返回 `model.capabilities_invalid`、`model.price_invalid`、`model.context_window_invalid`。
- 空 PATCH 返回 `model.update_empty`。
- 不存在的模型 ID 返回 `model.not_found`。

## 安全检查

- 所有模型目录 SQL 都使用认证上下文中的 `tenant_id`。
- 响应不包含 Provider Key、Virtual Key 明文、`key_hash` 或 Authorization 头。
- Provider Key 仍只在数据面调用上游时从运行时环境变量读取，不进入模型目录 API。
