# PRD-001 测试说明

## 覆盖范围

主流程：

- `gateway-data-plane` `/health` 返回 200、`status=ok`、`request_id` 和 `X-Request-ID`。
- `gateway-data-plane` `/version` 返回服务版本、启动时间和 `request_id`。
- `control-plane-api` `/health` 返回同样契约。
- 控制台能读取服务状态，并在自身可用时展示 `Console Web` 为运行中。

异常/安全：

- 未知路径返回统一错误码 `infra.not_found`，不返回框架默认错误。
- 非法 `METAHUB_ENV` 会触发 `infra.config_invalid`。
- `provider_key`、`Authorization`、`access_token` 等敏感字段会被替换为 `***MASKED***`。
- 后端不可用时，控制台状态读取降级为 `error`，页面不白屏。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

CI 检查不依赖数据库、Redis 或 Provider Endpoint。
