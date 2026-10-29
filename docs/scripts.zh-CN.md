# 运维脚本

安装和维护脚本位于 `scripts/` 与 `infra/production/`：

- `scripts/deploy/prepare-production.py` 准备独立的生产状态目录。
- `scripts/deploy/metahubctl.sh` 负责 Ubuntu 部署的安装、迁移、启动、检查、备份和恢复。
- `scripts/deploy/register-saas-admin.sh` 在首次安装时创建 SaaS 平台管理员，是一次性 `metahubctl.sh first-owner` 流程的明确入口。
- `metahubctl.sh first-owner` 交互式创建首位管理员，并拒绝已有账号或业务数据的数据库。
- `scripts/deploy/github-releases.py` 解析并校验 GitHub Release。
- `package-trial.py` 和 `sign-release.py` 生成并签名发布包。
- `recover-deployment.py` 和 `verify-backup.py` 执行恢复与备份门禁。
- `scripts/acceptance/` 保存运行时和浏览器验收脚本。
- `scripts/ci/` 保存仓库检查脚本。
- `infra/production/compose.yml` 是生产 Compose 定义，`production.env.example` 说明必填变量。

GitHub 源码会保留验收和 CI 工具，方便贡献者验证改动。签名部署包会刻意缩小范围：`package-trial.py` 只打包生产运行源码、部署入口、控制台源码、SDK 示例、迁移、运维文档和 Windows/Ubuntu 部署脚本，不包含测试、缓存、本地状态或贡献者专用的验收工具。

脚本应从已审核的 Release 目录运行；状态、凭据、数据卷和备份必须放在仓库之外。

平台管理员初始化的完整说明见 [SaaS 平台管理员](saas-administrator.zh-CN.md)。它和组织成员、应用 Virtual KEY 属于不同层级。
