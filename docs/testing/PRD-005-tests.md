# PRD-005 测试说明

## 覆盖范围

主流程：

- `/health/live` 在没有数据库、Redis、Provider 时仍返回 200、`live=true`。
- `/health/ready` 在 SQLite 数据库 ping 成功时返回 200、`ready=true`。
- `/version` 返回版本、commit、build_time、started_at、uptime、request_id、trace_id。
- 控制台系统状态小组件展示 live、ready、version、commit、trace_id 和依赖状态。

异常/安全：

- 数据库配置存在但不可用时 `/health/ready` 返回 503、`ready=false`。
- 数据库故障时 `/health/live` 仍成功。
- health 响应不包含数据库密码、Redis 密码、Provider Key。
- ready=false 是可解释状态，控制台不会白屏。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

当前验证结果：

- 后端 pytest：`26 passed`
- 控制台 Node test：`8 passed`
- 控制台 lint：通过
