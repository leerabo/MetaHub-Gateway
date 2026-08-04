# PRD-013 测试说明 - Provider Adapter抽象接口

## 覆盖范围

- `ProviderAdapter` 内部合同：`build_request`、`send_non_stream`、`send_stream`、`parse_usage`、`normalize_error`。
- `OpenAICompatibleAdapter`：请求构造、Provider Key header、模型 ID 替换、非流式响应、usage 归一化、错误归一化。
- `GenericHttpConfigAdapter`：通用 HTTP Adapter 工厂选择。
- 数据面主流程：现有 `/v1/chat/completions`、`/v1/embeddings` 集成测试继续通过。

## 建议命令

```powershell
$env:METAHUB_PYTHON="C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $env:METAHUB_PYTHON -m pytest gateway-data-plane/tests/unit/test_provider_adapters.py gateway-data-plane/tests/integration/test_openai_compatible_api.py -q
.\scripts\ci\check.ps1
```

## 主流程用例

- `test_openai_adapter_build_request_replaces_model_and_adds_bearer`
  - 验证内部模型 ID 被替换为 `provider_model_id/upstream_model_name`。
  - 验证 Provider Key 只进入 Authorization header。
- `test_adapter_parse_usage_supports_openai_and_generic_fields`
  - 验证 OpenAI usage 字段和通用 `input_tokens/output_tokens` 字段都能归一。
- `test_adapter_factory_selects_generic_for_custom_provider`
  - 验证自定义 Provider 走通用 HTTP Adapter。
- `test_openai_adapter_send_non_stream_returns_normalized_response`
  - 验证非流式发送返回 `NormalizedResponse` 和 `NormalizedUsage`。

## 异常与安全用例

- `test_adapter_normalizes_upstream_errors_without_logging_secrets`
  - 上游 429 返回 `gateway.upstream_error`。
  - 结构化事件为 `provider_adapter.error`。
  - 日志中不出现 Provider Key 或 Authorization。

## 回归要求

- `gateway-data-plane/tests/integration/test_openai_compatible_api.py` 必须继续通过。
- Chat JSON、Chat SSE、Embeddings、上游 5xx、不可达 Provider、request_logs 和 usage_ledger 行为不得回退。
