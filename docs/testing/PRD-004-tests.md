# PRD-004 测试说明

## 覆盖范围

主流程：

- 普通请求自动生成或透传 `request_id` / `trace_id`。
- 所有响应头返回 `X-Request-ID` 和 `X-Trace-ID`。
- `GET /internal/health` 返回 `status`、`service_name`、`environment`、`request_id`、`trace_id`。
- 错误码注册表包含 HTTP 状态、owner、retryable、message、suggestion。
- 控制台错误页预览展示 `code/request_id/trace_id/suggestion`。

异常/安全：

- 未知路径返回 `infra.not_found`，包含 `request_id` 和 `trace_id`。
- 未捕获异常返回 `infra.unhandled`，不泄露堆栈和 Provider Key。
- 结构化日志保留 `request_id`、`trace_id`，同时脱敏 Provider Key、Authorization、Token。
- 并发请求之间 `request_id` / `trace_id` 不串。
- 调试台入口仅为禁用预留状态，不查询未授权业务数据。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

当前验证结果：

- 后端 pytest：`22 passed`
- 控制台 Node test：`6 passed`
- 控制台 lint：通过
