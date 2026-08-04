# PRD-009 测试说明

## 覆盖范围

主流程：

- OpenAI-compatible `POST /v1/chat/completions` 非流式请求可使用 seed Virtual Key 调用。
- Chat 请求按当前租户 `model_alias` 路由到上游 `upstream_model_name`。
- 透传 `temperature` 等 OpenAI-compatible 扩展参数。
- Provider Adapter 从 `METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY` 读取 Provider Key，并只发送到上游请求头。
- `stream=true` 返回 `text/event-stream`，SSE chunk 原样透传。
- `POST /v1/embeddings` 支持基础 `input` 请求并返回 OpenAI-compatible JSON。
- 成功请求写入 `request_logs` 和 `usage_ledger`，记录 `tenant_id`、`virtual_key_id`、`model_id`、`provider_id`、token usage 和零成本占位。
- 控制台展示 API 文档、Python/JS SDK 示例和最小 OpenAI 调试台。

异常/权限/安全：

- 缺少 `Authorization` 返回 `gateway.auth_missing`。
- 无效 Virtual Key 返回 `gateway.auth_invalid`。
- 缺少 `model` 返回 `gateway.model_missing`。
- Chat 缺少 `messages` 返回 `gateway.messages_missing`。
- Embeddings 缺少 `input` 返回 `gateway.input_missing`。
- 上游非 2xx 返回 `gateway.upstream_error`。
- Provider URL 不可用或无效返回 `gateway.upstream_unavailable`。
- 鉴权成功后的失败请求尽量写入 `request_logs`，包含统一 `error_code`。
- `request_logs`、`usage_ledger` 不保存 prompt/input 明文、Virtual Key 明文或 Provider Key 明文。
- 控制台调试台不把 Virtual Key 写入 sessionStorage/localStorage。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

专项命令：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest gateway-data-plane/tests/integration/test_openai_compatible_api.py -q

cd D:\MetaHub\console-web
npm test
npm run lint
```

当前验证结果：

- 后端 PRD-009 专项集成测试通过：`6 passed`。
- 控制台 Node test 通过：`26 pass`。
- 全量 `.\scripts\ci\check.ps1` 通过：后端 pytest `43 passed`，控制台 Node test `26 pass`，console lint `ok (26 files)`。
- 真实 HTTP 验证通过：本地 fake OpenAI-compatible upstream + Gateway 完成 chat JSON、chat SSE、embeddings、缺失 model、无效 Virtual Key，并核对 `request_logs` 与 `usage_ledger`。
- 真实浏览器验证通过：控制台 seed admin 登录后可见 API 文档和 OpenAI 调试台；页面调试台调用 `default-chat` 返回 `live pong`；浏览器控制台无 warn/error。
