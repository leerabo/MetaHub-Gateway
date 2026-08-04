# PRD-028 测试记录

PRD：`PRD-028 - 单机Docker Compose部署`

## 覆盖范围

- Compose 文件结构：`gateway`、`control-api`、`console` 默认服务，以及 `redis`、`postgres`、`minio` 可选 profile。
- DeploymentProfile：`local-sqlite` 默认可运行，`compose-full` 仅增加基础设施服务，应用数据库模式仍为 SQLite。
- `.env.compose.example`：只允许占位符，不允许真实密码、Provider Key 或固定管理员密码。
- 部署 CLI dry-run：输出 `request_id`、端口和步骤，不创建真实 env，不打印 secret。
- 危险操作保护：`stop.ps1 -RemoveVolumes` 必须带 `-ConfirmRemoveVolumes`。
- env 初始化：替换所有 `replace-with-generated-*` 占位符，只允许 stdout 返回一次性管理员密码，其他 secret 只写入本地 env。
- Dockerfile：复用现有 `metahub_gateway.main`、`metahub_control.main`、`console-web/scripts/dev-server.mjs` 入口。

## 已执行命令

```powershell
python -m compileall control-plane-api\tests\integration\test_deployment_compose.py
python -m pytest control-plane-api\tests\integration\test_deployment_compose.py -q --basetemp <unique> -p no:cacheprovider
python scripts\ci\check-comments.py
```

结果：

- `test_deployment_compose.py`：7 passed。
- 中文注释门禁：`comment coverage ok`。
- 全量后端 pytest：通过。
- 前端 Node 单测：95 passed。
- 前端 lint：`console lint ok (69 files)`。

## 未默认执行的检查

本轮执行 `docker compose version` 时本机缺少 `docker` 命令，因此未执行真实 `docker compose up` 和 `docker compose config`。真实服务器验收时应补跑：

```powershell
.\scripts\deploy\install.ps1
.\scripts\deploy\smoke.ps1
.\scripts\deploy\stop.ps1
.\scripts\deploy\start.ps1
.\scripts\deploy\smoke.ps1
```

该 smoke 用于证明冷启动、健康检查全绿和重启数据不丢。
