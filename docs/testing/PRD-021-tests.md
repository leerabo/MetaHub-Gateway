# PRD-021 测试记录 - 智能错误提示基础版

## 覆盖范围

PRD-021 覆盖统一错误诊断规则、错误码目录、Provider Adapter 上游错误归一、调用日志详情诊断和前端诊断卡片。

## 主流程用例

- `diagnose_error("gateway.upstream_rate_limited")` 返回 `upstream_rate_limited`、可重试标记和可操作建议。
- `GET /error-codes` 返回所有统一错误码，并为典型错误携带 `diagnosis`。
- Provider Adapter 将上游 401/403、429、402/quota、404/model、timeout、400/422 归一为细分 `gateway.upstream_*` 错误码。
- `GET /request-logs/{request_id}` 对失败请求返回 `error_diagnosis`。
- 调试台和调用日志详情渲染诊断原因、建议、操作、级别和可重试标记。

## 异常/权限/安全用例

- 泛化 `gateway.upstream_error` 在带 `insufficient_quota` 等安全 Provider 类型时优先归因为额度不足。
- 普通 `infra.not_found` 不会因 HTTP 404 被误归因为上游模型不存在。
- 诊断输出不包含 `sk-`、Authorization、Prompt、messages、Response 或上游原始 body。
- viewer 访问调用日志详情仍由后端返回 `auth.permission_denied`。
- 前端诊断字段包含 `<script>` 时必须 HTML 转义。

## 已执行验证

```powershell
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pytest gateway-data-plane\tests\unit\test_error_codes.py gateway-data-plane\tests\unit\test_provider_adapters.py gateway-data-plane\tests\integration\test_observability_errors.py control-plane-api\tests\integration\test_request_logs.py -q
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --test console-web\tests\unit\*.test.mjs
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" console-web\scripts\lint.mjs
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" scripts\ci\check-comments.py
& "C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m pytest -q
```

结果：

- PRD-021 后端专项：13 passed。
- 控制台前端单测：69 passed。
- 控制台 lint：passed。
- 中文注释门禁：`comment coverage ok`。
- 后端全量 pytest：passed。
