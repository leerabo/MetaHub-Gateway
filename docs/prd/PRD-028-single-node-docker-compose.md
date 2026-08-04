# PRD-028 - 单机Docker Compose部署

状态：已完成编码、测试和文档更新。

## 需求理解

PRD-028 属于 M0-W2/P0 的“部署包 MVP”能力，目标是让研发和试点客户能在一台服务器上用一组脚本启动 MetaHub MVP，并能完成迁移、seed、健康检查和持久化验证。

Excel 约束：
- 主要用户：交付/研发。
- 需求范围：提供 docker-compose，包含 API、console、Postgres、Redis、MinIO 可选；初始化脚本、默认管理员、环境变量模板。
- 明确不做：不做 Kubernetes Helm，不做高可用。
- 页面/入口：部署文档。
- API/动作：CLI install/start/stop。
- 数据对象：DeploymentProfile。
- 事件：deployment.completed。
- 验收：全新服务器可按文档 30 分钟内启动；健康检查全绿；重启数据不丢。
- 测试重点：冷启动、端口冲突、数据卷。

架构约束补充：
- MVP 基础设施层允许 PostgreSQL 单实例、Redis 单实例、本地磁盘或 MinIO 存储日志。
- 交付形态允许 Docker Compose；Helm/HA、Redis Sentinel、托管数据库和复杂 Preflight 放到后续。
- 当前代码运行时的 `connect_database()` 明确只支持 `sqlite://`，暂未接入 PostgreSQL 驱动。因此本 PRD 的可运行默认 profile 使用持久化 SQLite 文件；Compose 仍提供 PostgreSQL 服务和 env/profile 入口，但不把应用默认切到 `postgresql://`，避免交付包启动即失败。若评审要求本 PRD 必须让应用实际使用 PostgreSQL，则需同步扩展 DB driver、迁移方言和 seed SQL，这会显著扩大 PRD-028 范围。

## 涉及模块

- `infra/compose/`
  - 新增 `docker-compose.yml`、`.env.compose.example`、`profiles/` 和 README。
  - 定义 `gateway`、`control-api`、`console`、`redis`、`postgres`、`minio` 服务和持久化 volume。
- `infra/docker/`
  - 新增 Python API 镜像 Dockerfile 和 Console Web 镜像 Dockerfile。
  - API 镜像复用同一基础镜像，通过启动命令区分 gateway/control-plane。
- `scripts/deploy/`
  - 新增 `install.ps1`、`start.ps1`、`stop.ps1`、`status.ps1`、`smoke.ps1`。
  - 所有脚本默认读取 `infra/compose/.env.compose`，不把密钥打印到日志。
- `scripts/preflight/`
  - 新增或复用端口、Docker、磁盘目录和 env 校验逻辑。
- `shared-sdk/python/metahub_shared/runtime/`
  - 不新增业务 API；可增加部署 profile 解析的轻量 DTO/验证函数，供脚本测试复用。
- `docs/deployment/`
  - 新增单机部署文档和故障排查。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-028-tests.md`
  - 更新部署入口、运行方式和测试记录。

## 数据表/字段设计

本 PRD 不新增业务数据库表，也不改动现有多租户业务 schema。

### DeploymentProfile

`DeploymentProfile` 是部署配置对象，不进入业务库。建议以 `infra/compose/profiles/*.json` 或脚本内部 DTO 表达。

字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `profile_id` | string | `local-sqlite`、`compose-full` 等稳定 ID。 |
| `project_name` | string | Docker Compose project name，默认 `metahub`。 |
| `database_mode` | enum | `sqlite` 或 `postgresql`；M0 默认 `sqlite`。 |
| `services` | object | 各服务启用状态、端口和健康检查目标。 |
| `volumes` | object | SQLite、Postgres、Redis、MinIO、日志目录持久化位置。 |
| `env_file` | string | 实际部署环境变量文件路径。 |
| `created_at` | datetime | profile 生成时间，仅用于本地部署记录。 |

### 部署事件

`deployment.completed` 不进入 Webhook 主链路。M0 只写本地 JSONL：`.logs/deploy/deployment-events.jsonl`。

事件字段：
- `event_type='deployment.completed'`
- `deployment_id`
- `profile_id`
- `status`
- `started_at`
- `completed_at`
- `services`
- `request_id`

事件 payload 不保存 Provider Key、Virtual Key、管理员密码、数据库密码或 token secret。

## API设计

本 PRD 不新增 HTTP API，只新增 CLI。

### `scripts/deploy/install.ps1`

用途：首次安装，生成 `.env.compose`、创建数据目录、构建镜像、启动基础服务、执行迁移和 seed、运行健康检查。

参数：
- `-Profile local-sqlite|compose-full`，默认 `local-sqlite`。
- `-EnvFile infra/compose/.env.compose`。
- `-ProjectName metahub`。
- `-SkipBuild`，用于本地已有镜像时跳过 build。

主流程：
1. 检查 Docker 和 Docker Compose。
2. 检查端口冲突。
3. 生成或校验 `.env.compose`。
4. 创建 `.data/compose` 和 `.logs/deploy`。
5. `docker compose up -d`。
6. 在 API 容器内执行迁移和 seed。
7. 轮询 `/health/live`、`/health/ready`、`/version`。
8. 写入 `deployment.completed` 本地事件。

### `scripts/deploy/start.ps1`

用途：启动已有部署，不重置数据，不覆盖 env。

### `scripts/deploy/stop.ps1`

用途：停止容器但保留 volume。可选 `-RemoveVolumes` 必须二次确认，默认不可用在自动脚本中。

### `scripts/deploy/status.ps1`

用途：输出服务健康、端口、容器状态和关键 URL。

### `scripts/deploy/smoke.ps1`

用途：执行部署后 smoke：Gateway health、Control health、Console HTTP、登录默认管理员、读取 `/dashboard/overview`。

## 前端页面/交互设计

PRD-028 不新增控制台业务页面。

部署文档需要提供：
- 一条命令首次安装。
- 启动、停止、状态、日志查看命令。
- 默认管理员账号位置和修改方式。
- 健康检查 URL。
- 重启验证：停止/启动后数据库文件或 volume 不丢。
- 常见失败：端口占用、Docker 未启动、env 缺失、健康检查失败、磁盘权限。

Console 容器行为：
- 生产预览模式启动静态控制台或 Node dev server 的容器化版本。
- `METAHUB_CONTROL_API_BASE_URL`、`METAHUB_GATEWAY_API_BASE_URL` 通过 env 注入。
- 不把管理员密码或 Provider Key 打包进前端构建产物。

## 错误码设计

CLI 错误码沿用英文稳定 code，输出结构包含 `code/message/request_id/suggestion`。

| 错误码 | 场景 | 建议 |
| --- | --- | --- |
| `deployment.docker_missing` | Docker 或 Compose 不存在 | 安装 Docker Desktop/Engine 并重试。 |
| `deployment.port_conflict` | 端口被占用 | 修改 `.env.compose` 端口或释放占用进程。 |
| `deployment.profile_invalid` | DeploymentProfile 或 env 缺失/非法 | 按 `.env.compose.example` 修复。 |
| `deployment.compose_failed` | `docker compose` 启动失败 | 查看 `docker compose logs`。 |
| `deployment.init_failed` | 迁移或 seed 失败 | 检查数据库 URL、volume 权限和 seed env。 |
| `deployment.health_failed` | 健康检查未全绿 | 查看 Control/Gateway 日志和 `/health/ready`。 |

后续如要把部署状态暴露到控制台，再新增 HTTP API；M0 不需要。

## 安全与权限约束

- `.env.compose.example` 只能放占位值，不提交真实密钥。
- `install.ps1` 生成的 `.env.compose` 必须在 `.gitignore` 中排除。
- 默认管理员密码如果自动生成，只能打印一次并写入本地 env；不得写入部署事件、容器日志或 README。
- Provider Key、Virtual Key、Authorization、Token Secret、MinIO Secret、数据库密码不得明文进入 JSONL 事件。
- Compose 默认绑定 `127.0.0.1` 或局域网试点端口；公网暴露、TLS、域名、反向代理放后续生产部署 PRD。
- `stop.ps1` 默认保留 volume；删除 volume 必须显式参数和确认提示。
- MinIO 在 M0 是可选 profile；启用时也必须使用本地生成的 access/secret，不使用硬编码默认密码。

## 测试用例

后端/脚本测试：
1. 主流程：解析 `DeploymentProfile` 和 `.env.compose.example` 成功，默认 profile 为 `local-sqlite`。
2. 主流程：生成 `docker compose config` 所需服务名、端口、volume 和 healthcheck。
3. 主流程：`install.ps1 -WhatIf` 或 dry-run 输出将执行步骤，不打印密钥。
4. 异常：Docker 不存在时返回 `deployment.docker_missing`。
5. 异常：端口被占用时返回 `deployment.port_conflict`，并指出端口和服务。
6. 异常：缺少必填 env 时返回 `deployment.profile_invalid`。
7. 持久化：stop/start 不删除 SQLite/Postgres/Redis/MinIO volume。
8. 健康：smoke 脚本可轮询 Gateway、Control 和 Console；失败返回 `deployment.health_failed`。

可选本机 smoke：
- 在安装了 Docker 的机器上运行 `scripts/deploy/install.ps1`。
- 执行 `scripts/deploy/smoke.ps1`。
- `scripts/deploy/stop.ps1` 后再 `start.ps1`，确认 `/auth/login` 和 `/dashboard/overview` 仍可用。

前端测试：
- 不新增 UI 单测；只在部署 smoke 中验证 Console 首页可访问。

文档测试：
- README 和部署文档命令必须与脚本文件名一致。
- `.env.compose.example` 不包含真实密钥或默认弱密码。

## 代码修改计划

1. 新增 `infra/compose/docker-compose.yml`，定义 API、Console、Redis、Postgres、MinIO profile 和持久化 volume。
2. 新增 `infra/compose/.env.compose.example`，包含端口、服务 URL、数据库模式、seed 管理员、Redis、MinIO 和构建元数据。
3. 新增 `infra/docker/python-service.Dockerfile`，复用 Python 代码和 shared-sdk，支持 gateway/control 两种启动命令。
4. 新增 `infra/docker/console.Dockerfile`，构建并启动控制台容器。
5. 新增 `scripts/deploy/install.ps1`、`start.ps1`、`stop.ps1`、`status.ps1`、`smoke.ps1`。
6. 新增部署 profile 解析/校验辅助代码和测试，必要时放在 `scripts/deploy/lib/`。
7. 新增 `docs/deployment/single-node-compose.md` 和 `docs/testing/PRD-028-tests.md`。
8. 更新 README、目录文档和 `.gitignore`。
9. 补充单元/集成测试：profile 校验、端口冲突 dry-run、compose 文件结构、密钥不输出。
10. 若本机 Docker 可用，执行可选 smoke；若不可用，记录跳过原因并保证静态/脚本测试通过。

## 两轮设计自审结论

第一轮自审：默认应用数据库不直接切 PostgreSQL。当前运行时只支持 `sqlite://`，如果本 PRD 强行把应用指向 `postgresql://`，部署包会在健康检查阶段失败。PRD-028 先交付可运行的一条命令单机包，并预置 PostgreSQL 服务和 profile 入口；PostgreSQL 驱动和迁移方言需要作为明确的后续任务或评审追加范围。

第二轮自审：`deployment.completed` 不接入 Webhook 主链路。M0 部署事件只写本地 JSONL，避免为了部署脚本引入生产事件总线、Webhook 触发和外部网络依赖；后续生产运维平台需要订阅部署事件时，再复用 Webhook 事件信封。

## 编码后自审与验证记录

### 实现摘要

- 新增 `infra/compose/docker-compose.yml`，默认启动 `gateway`、`control-api`、`console`，并通过 `compose-full` profile 预留 Redis、PostgreSQL、MinIO。
- 新增 `infra/compose/.env.compose.example` 和 `profiles/*.json`，明确 `local-sqlite` 是当前可运行默认 profile。
- 新增 `infra/docker/python-service.Dockerfile`、`infra/docker/console.Dockerfile` 和 `requirements.runtime.txt`，复用现有 Python/Node 服务入口。
- 新增 `scripts/deploy/install.ps1`、`start.ps1`、`stop.ps1`、`status.ps1`、`smoke.ps1` 和公共模块 `MetaHub.Deploy.psm1`，CLI 输出统一包含 `request_id`。
- 新增 `docs/deployment/single-node-compose.md`、`docs/testing/PRD-028-tests.md`，并更新 README、目录边界说明和 `.gitignore`。

### 编码后第一轮自审

默认数据库继续使用 SQLite 是正确的交付选择。当前 `connect_database()` 只支持 `sqlite://`，如果 Compose 默认把应用切到 PostgreSQL，会让 PRD-028 的“一条命令启动”验收直接失败。PostgreSQL 服务和 env 入口已经作为 `compose-full` 基础设施预留，后续需要通过独立 PRD 接入驱动和迁移方言。

### 编码后第二轮自审

CLI 的安全输出边界已收敛：dry-run 不创建 `.env.compose`；首次安装只允许 stdout 返回一次管理员密码；`deployment.completed` 事件不包含任何 secret；`stop.ps1` 删除 volume 必须二次确认。测试覆盖了这些边界，避免后续改脚本时破坏安全约束。

### 已执行验证

```powershell
python -m compileall control-plane-api\tests\integration\test_deployment_compose.py
python -m pytest control-plane-api\tests\integration\test_deployment_compose.py -q --basetemp <unique> -p no:cacheprovider
python scripts\ci\check-comments.py
```

结果：

- PRD-028 focused 测试：7 passed。
- 中文注释门禁：`comment coverage ok`。
- 全量后端 pytest：通过。
- 前端 Node 单测：95 passed。
- 前端 lint：`console lint ok (69 files)`。

本机缺少 `docker` 命令，真实 Docker daemon smoke 未执行；部署文档已列出服务器验收命令，交付环境具备 Docker 后应补跑 `install.ps1`、`smoke.ps1`、`stop.ps1`、`start.ps1`、`smoke.ps1` 验证冷启动和重启数据不丢。