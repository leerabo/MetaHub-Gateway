# MetaHub

MetaHub 是面向团队的 AI Gateway 平台：集中管理模型服务、模型路由、项目与应用、成员权限、API KEY、用量、成本和预算。它同时提供组织控制台、AI Gateway 数据面和平台管理工作台，适合在单台 Ubuntu 服务器上以 Docker Compose 方式部署。

当前 GitHub 试用版本为 **2.0.0-trial.48**。这是预发布版本，安装前请先完成目标服务器、域名、HTTPS、SMTP 和备份策略的验收。发布包和签名文件位于 [GitHub Release v2.0.0-trial.48](https://github.com/leerabo/MetaHub/releases/tag/v2.0.0-trial.48)。

项目路线图当前覆盖 **PRD-090 至 PRD-148**。这是持续演进的 SaaS 项目，不限定需求总数或最终编号；路线图和逐项验收记录位于 `docs/prd/`、`docs/architecture/` 与 `docs/testing/`。

## 能做什么

- 组织管理员配置模型服务、模型目录和默认/备用模型路由。
- 按组织、项目、应用和成员管理访问范围、角色与 API KEY。
- 查看请求、用量、成本、预算、告警和审计记录。
- 通过 PostgreSQL 保存生产业务数据，并使用版本化迁移和备份恢复流程。
- 使用签名 GitHub Release 进行版本检查、人工确认和受保护升级。

## Ubuntu 试用部署

生产部署使用已签名的 Release 包，不在服务器上执行 `git pull`，也不把数据库、密钥或 `.env` 文件放进 GitHub。完整步骤见：

- [GitHub Release 更新与恢复](docs/operations/github-release-upgrades.md)
- [单机 Docker Compose 部署](docs/deployment/single-node-compose.md)
- [首次管理员初始化](docs/operations/first-owner-initialization.md)
- [SQLite 到 PostgreSQL 的离线迁移](docs/operations/sqlite-to-postgresql.md)
- [Ubuntu 日志保留](docs/operations/ubuntu-log-retention.md)

首次安装的基本顺序是：在可信环境核对 Release 签名和包内清单，解压到版本目录，运行 `prepare-production.py` 创建独立 state，执行 `metahubctl.sh install STATE`，再在服务器交互终端运行 `metahubctl.sh first-owner STATE` 创建首位管理员。

```bash
sudo scripts/deploy/metahubctl.sh install /opt/metahub-state
sudo scripts/deploy/metahubctl.sh first-owner /opt/metahub-state
```

命令中的 `STATE` 是服务器上的私有状态目录，需按实际部署路径替换。首位管理员密码和动态验证码密钥只在交互终端设置；系统不会启用默认密码或固定验证码 `000000`。

## 数据与升级安全

- 生产数据库使用 PostgreSQL；迁移按版本前进，遇到结构、排序规则、签名或备份核对失败会拒绝继续。
- 升级前会创建加密备份并在独立数据库中恢复核对；升级失败或中断会阻止后续升级，直到完成恢复处理。
- Release 包不包含客户账号、密码、API KEY、数据库、文件卷、SMTP 凭据或备份密钥。
- 不要删除旧版本目录、数据库卷或备份，直到新版本完成健康检查和业务验收。
- 需要从 SQLite 迁移到 PostgreSQL 时，按离线迁移文档执行，不要直接改写历史迁移记录。

当前仓库的升级工具面向试用部署；发布包不能重复替换或降级。私有 GitHub 仓库下载需要服务器上的细粒度 GitHub token，token 只通过 root 私有配置提供，不写入 README、网页或日志。

## 本地开发

前端控制台位于 `console-web/`，使用 Node.js 运行：

```bash
npm --prefix console-web install
npm --prefix console-web run dev
```

常用验证命令：

```bash
npm --prefix console-web test
npm --prefix console-web run lint -- --no-fix
npm --prefix console-web run build
```

后端和部署工具依赖 Python 3.12；测试配置见 [`pyproject.toml`](pyproject.toml)。单机 Compose、迁移、发布和运行维护的权威说明集中在 [`docs/`](docs/) 中。

## 项目结构

- `console-web/`：组织控制台和平台工作台前端。
- `gateway-data-plane/`：模型请求网关和路由执行。
- `control-plane-api/`：组织、权限、预算、审计和管理 API。
- `infra/`：数据库迁移、生产 Compose 和运行时配置模板。
- `scripts/deploy/`：打包、签名、安装、升级、备份和恢复工具。
- `docs/`：部署、运维、架构和测试文档。

## 历史记录

此前根目录 README 混入了大量按日期记录的视觉调整、测试批次和开发过程。原文已完整保存在 [`docs/history/README-development-log-202609-202610.md`](docs/history/README-development-log-202609-202610.md)，用于追溯，不再作为项目首页说明。

## 许可证与反馈

仓库当前用于 MetaHub 试用和部署验收。请通过 GitHub Issues 或项目维护渠道报告问题；提交问题时请去除密码、动态验证码、API KEY、数据库内容和服务器私密配置。
