# PRD-002 测试说明

## 覆盖范围

主流程：

- 配置优先级为 `默认值 < .env < METAHUB_CONFIG_FILE < 环境变量`。
- `dev/test/prod` 环境名被校验，非法值返回 `infra.config_invalid`。
- Provider Base URL 可以从配置读取并进入配置摘要。
- `GET /internal/config/summary` 返回 `request_id`、`X-Request-ID` 和脱敏后的配置项。
- 控制台能读取配置摘要，并在后端不可用时显示降级状态。

异常/权限/安全：

- Provider Key、数据库 URL、Redis URL、内部 token 只显示 `***MASKED***` 或 `missing`。
- 配置摘要响应和日志不包含 Provider Key 明文。
- `prod` 环境访问内部配置摘要时，缺少 `X-Internal-Token` 返回 `auth.internal_token_required`。
- `prod` 环境 token 错误时返回 `auth.internal_token_invalid`。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

当前验证结果：

- 后端 pytest：`13 passed`
- 控制台 Node test：`4 passed`
- 控制台 lint：通过
