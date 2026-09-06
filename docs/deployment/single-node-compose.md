# 单机 Docker Compose 部署

本文对应 `PRD-028 - 单机Docker Compose部署`，用于研发和试点客户在单台服务器上启动 MetaHub MVP。

## 范围

- 默认部署 profile：`local-sqlite`。
- 默认启动服务包括 `gateway`、`control-api`、`control-ingress`、`status-page`、`capacity-runner`、
  `mcp-scheduler-worker`、`mcp-sandbox`、`batch-worker`、`console`。
- 可选基础设施 profile：`compose-full`，额外启动 `redis`、`postgres`、`minio`、bucket 初始化和独立
  `backup-worker`。
- 应用包含显式 `sqlite://` 本地/测试后端和 `postgresql://` 连接池适配器；不会在 PostgreSQL 故障时回退
  SQLite。原生迁移支持空库 baseline 0001..0139 和后续独立 PostgreSQL delta；未完成 bootstrap、
  源码摘要漂移和缺失原生 bundle 均失败关闭。默认 Compose 仍使用 SQLite。生产 PostgreSQL、Redis
  和对象存储必须启用 TLS，迁移验证不能代替全产品业务验收。
- 包含 Control Nginx 入口与退休规则，不包含 Kubernetes Helm、高可用、TLS 终止、域名和生产密钥托管。

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
3. 构建应用、隔离 Worker、`control-ingress` 和 Console 镜像。
4. 启动 Compose 服务。
5. 在 `control-api` 容器内执行数据库迁移和 seed。
6. 轮询 Gateway、Control API、MCP Sandbox 和 Console 健康检查。
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

`stop.ps1` 默认保留 volume，因此重启后 SQLite、可选 PostgreSQL 和 MinIO 数据不会被删除。Redis 关闭
RDB/AOF 且不挂载 volume，只承载可重建缓存、限流与短租约。

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

## Control 入口与回滚

宿主机的 `METAHUB_CONTROL_PORT`（默认 8001）现在转发到 Nginx 的 8080，而不是直接发布 Control。
后端 8001 仅供 Compose 内部服务访问。入口以非 root、只读根文件系统、独立临时目录运行，不挂载
应用密钥或数据库，不记录请求 URL、Cookie 和请求头。

`infra/ingress/retired-console-routes.conf` 从 PRD-147 的固定清单生成，执行
`python scripts/deploy/generate_control_ingress.py --check` 可检测漂移。退休规则覆盖所有 HTTP 方法、
URL 解码、重复斜杠及尾斜杠；请求改写到普通未知路径，由后端生成标准 404，入口统一加 `no-store`。
`/branding/public` 和新分域路由不被退休规则拦截。`/openapi.json`、`/docs`、`/redoc` 只允许内部访问，
避免旧版本从 API 文档重新披露退休操作。

入口镜像固定为 `control-ingress:prd147-v1`，不使用应用的 `METAHUB_IMAGE_TAG`。升级或回滚只切换
应用镜像，保留当前入口与退休规则；不得恢复旧 Compose 文件中的 Control 端口发布。外部 TLS/LB
必须指向入口端口，不得指向后端服务地址。直接本地开发启动器不构成公开发布入口。

## 发布证据公钥信任

For PRD-138 release-candidate evidence, configure the Control-only public trust
mount described in [Launch evidence trust](launch-evidence-trust.md). The default
Compose installation does not provision signing trust or private signing keys.

## MCP Sandbox

PRD-083 后，生产 MCP 网络执行拆分为独立 `mcp-scheduler-worker` 与 `mcp-sandbox`：

- Control API 固定关闭嵌入式 MCP Scheduler，且不接收 Sandbox URL、internal token、envelope、permit 或
  response secret。
- Scheduler 挂载 SQLite/日志卷并持有 ciphertext keyring、Sandbox URL/token、envelope signing secret 和
  response current/previous key；它不持 permit secret，也不接收部署 ACL CIDR/port。
- Sandbox 使用专用最小镜像，不挂载数据库、日志或宿主目录，不接收 Control/Gateway 配置；容器以非 root、
  read-only root filesystem、`cap_drop: ALL`、`no-new-privileges` 运行。
- Sandbox 没有宿主机端口，只连接内部 control network 与独立 egress network。Scheduler 只连接 control
  network，不能直接连接第三方 MCP Endpoint。
- Endpoint/Bearer 只在 Sandbox 内存中解密；Uvicorn access log 关闭，permit 为一次性且不进入数据库或日志。

部署脚本首次生成 `.env.compose` 时会分别生成 MCP ciphertext、HMAC、Sandbox internal、envelope、permit 和
response secret。`.env.compose.example` 只保留占位符。`METAHUB_MCP_SANDBOX_PRIVATE_CIDRS` 只接受运维显式
配置的精确 IPv4 `/32` 或 IPv6 `/128` 私网例外；默认为空。数据库、缓存、文件协议和容器控制端口仍由永久
deny port 集合拒绝，不能被 tenant policy 放开。

## 本地模型 Endpoint 网络配置

PRD-084 的 `local_endpoint` Provider 仍由 Control/Gateway 进程访问，不经过 MCP Sandbox。部署前必须由
运维在 `.env.compose` 或等价运行配置中明确设置网络范围；租户 API 不能提交 CIDR、端口、resolved IP 或
超时/大小上限。

- `METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS` 默认为空，只接受规范且不重叠的企业私网 CIDR。公网地址不会因
  此变量自动获准；本地 Endpoint 必须命中配置范围。
- `METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS` 默认为 `80,443,8000,8080,8443,11434`。
- `METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK=false`；只允许在隔离 dev/test 验收中临时启用，prod 配置为
  true 会使服务启动失败。
- connect/overall deadline 默认 `5s/120s`；非流式/流式响应上限默认 `16 MiB/64 MiB`。
- metadata、link-local、multicast、unspecified 永久拒绝。每次请求执行 DNS 全地址 ACL 并固定单一已批准
  IP，同时保留原 Host/TLS SNI，禁用 proxy、redirect、HTTP/2 和跨请求 keep-alive。

Control 和 Gateway 必须使用相同的 local Endpoint 网络变量。配置非法时 readiness 失败关闭；不得通过
Provider API、配置导入、Compose tenant 环境变量或前端表单放宽部署边界。完整 API 和错误合同见
`docs/api/local-model-endpoints.md`。

## Compose Full Profile

```powershell
.\scripts\deploy\install.ps1 -Profile compose-full
```

该 profile 会额外启动 Redis 7、PostgreSQL 16、MinIO、bucket 初始化和独立 Backup Worker。模板仍以显式
SQLite 保证默认本地 profile 可运行。原生迁移的执行器与 baseline 已提供，首次执行要求空 `public`
schema；已存在的部分 bootstrap 不会被自动覆盖。每次迁移都验证 0139 源码清单与 bootstrap 终态，
后续原生 delta 的 DDL 和版本登记在同一事务中提交，delta 不得自带事务边界。基线不允许原地回滚，
应从已复核备份恢复到隔离数据库；后续 delta 支持 owner-specific down。切换应用数据库之前仍须
完成对应业务、兼容与故障恢复验收。生产环境的 URL 必须包含
`sslmode=require|verify-ca|verify-full`，Redis 使用 `rediss://`，对象存储使用 HTTPS。

Backup Worker 要求完整的对象存储和 KMS 配置。`METAHUB_KMS_PROVIDER=test` 只允许 dev/test；生产必须使用
独立 AWS KMS key。PostgreSQL 备份还必须设置与源库身份不同的 `METAHUB_BACKUP_RESTORE_DATABASE_URL`，
用于 `ready` 前的隔离恢复验证。worker 不提供恢复生产库的 HTTP 或自动化入口。

## 安全约束

- `infra/compose/.env.compose` 已加入 `.gitignore`，不得提交。
- `.env.compose.example` 只能包含 `replace-with-generated-*` 占位符；安装器会独立生成数据库、MinIO 和本地
  测试 KMS 值。
- `deployment.completed` 事件只记录 profile、服务 URL 和 `request_id`，不记录管理员密码、Provider Key、Virtual Key、Token Secret、数据库密码或 MinIO Secret。
- Provider Key 和 Virtual Key 明文仍只允许在首次创建/本地 env 中出现，不得进入日志。
- MCP Endpoint、Bearer、permit、Sandbox internal/envelope/response secret 不得进入部署事件、容器日志、
  health response 或验收证据。
- 默认端口绑定到 `127.0.0.1`；Control 只能经附带的入口代理访问，公网 TLS 终止仍由部署环境提供。

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
