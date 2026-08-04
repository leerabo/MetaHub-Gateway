# 单机 Docker Compose 部署

本文对应 `PRD-028 - 单机Docker Compose部署`，用于研发和试点客户在单台服务器上启动 MetaHub MVP。

## 范围

- 默认部署 profile：`local-sqlite`。
- 默认启动服务：`gateway`、`control-api`、`console`。
- 可选基础设施 profile：`compose-full`，额外启动 `redis`、`postgres`、`minio`。
- 当前应用运行时只支持 `sqlite://` 数据库 URL，因此 `compose-full` 中的 PostgreSQL 是后续 PostgreSQL driver/迁移方言接入前的基础设施预留，不会被应用默认使用。
- 不包含 Kubernetes Helm、高可用、TLS、域名、反向代理和生产密钥托管。

## 前置条件

- Docker Desktop 或 Docker Engine。
- Docker Compose v2。
- PowerShell 5.1 或 PowerShell 7。

## 首次安装

```powershell
.\scripts\deploy\install.ps1
```

默认行为：

1. 生成 `infra/compose/.env.compose`。
2. 创建本地持久化目录和部署日志目录。
3. 构建 `gateway`、`control-api`、`console` 镜像。
4. 启动 Compose 服务。
5. 在 `control-api` 容器内执行数据库迁移和 seed。
6. 轮询 Gateway、Control API 和 Console 健康检查。
7. 写入 `.logs/deploy/deployment-events.jsonl` 中的 `deployment.completed` 事件。

首次生成 `.env.compose` 时，CLI 会且只会在 stdout 中返回一次默认管理员密码。该密码不会进入部署事件、容器日志或 README。

## Dry Run

```powershell
.\scripts\deploy\install.ps1 -DryRun
```

Dry run 只输出计划步骤、端口、profile 和 `request_id`，不会创建 `.env.compose`，也不会访问 Docker。

## 启动、停止和状态

```powershell
.\scripts\deploy\start.ps1
.\scripts\deploy\status.ps1
.\scripts\deploy\stop.ps1
```

`stop.ps1` 默认保留 volume，因此重启后 SQLite 数据文件、可选 PostgreSQL/Redis/MinIO 数据卷不会被删除。

删除 volume 必须显式传入两个参数：

```powershell
.\scripts\deploy\stop.ps1 -RemoveVolumes -ConfirmRemoveVolumes
```

## Smoke 检查

```powershell
.\scripts\deploy\smoke.ps1
```

Smoke 检查包含：

- `GET http://localhost:8000/health/live`
- `GET http://localhost:8001/health/live`
- `GET http://localhost:5173/`
- 使用 `.env.compose` 中的默认管理员账号执行登录检查，但不输出密码。

## Compose Full Profile

```powershell
.\scripts\deploy\install.ps1 -Profile compose-full
```

该 profile 会额外启动 Redis、PostgreSQL 和 MinIO。当前应用仍使用 `METAHUB_DATABASE_URL=sqlite:///./.data/compose/metahub.db`，以保证 M0 可运行；PostgreSQL 真正接入需要后续 PRD 增加驱动、SQL 方言和迁移验证。

## 安全约束

- `infra/compose/.env.compose` 已加入 `.gitignore`，不得提交。
- `.env.compose.example` 只能包含 `replace-with-generated-*` 占位符。
- `deployment.completed` 事件只记录 profile、服务 URL 和 `request_id`，不记录管理员密码、Provider Key、Virtual Key、Token Secret、数据库密码或 MinIO Secret。
- Provider Key 和 Virtual Key 明文仍只允许在首次创建/本地 env 中出现，不得进入日志。
- 默认端口绑定到 `127.0.0.1`；公网暴露、TLS 和反向代理不属于本 PRD。

## 错误码

部署 CLI 输出统一结构：`status/code/message/request_id/suggestion`。

| code | 场景 |
| --- | --- |
| `deployment.docker_missing` | Docker CLI 或 Compose 插件不可用。 |
| `deployment.port_conflict` | 端口被占用。 |
| `deployment.profile_invalid` | profile、env 或危险参数组合不合法。 |
| `deployment.compose_failed` | `docker compose` 启动、停止或查询失败。 |
| `deployment.init_failed` | 迁移或 seed 失败。 |
| `deployment.health_failed` | Gateway、Control API、Console 或默认管理员登录 smoke 失败。 |

## 常用 URL

- Gateway: <http://localhost:8000/health/live>
- Control API: <http://localhost:8001/health/live>
- Console: <http://localhost:5173/>
