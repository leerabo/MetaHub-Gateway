# Ubuntu 部署

这是 MetaHub 支持的自托管方式，适用于 Ubuntu 22.04+ / 24.04+、x86_64 或 arm64。发布包带版本号和签名；数据库卷、密钥、备份和运行状态都放在发布目录之外。

```bash
sudo install -d -m 0755 /opt/metahub-releases
sudo install -d -m 0700 /opt/metahub-state
python3 scripts/deploy/prepare-production.py \
  --state /opt/metahub-state \
  --domain gateway.example.com
sudo scripts/deploy/metahubctl.sh install /opt/metahub-state
sudo scripts/deploy/register-saas-admin.sh /opt/metahub-state
sudo scripts/deploy/metahubctl.sh status /opt/metahub-state
```

发布包必须同时包含 `release-manifest.json`、生产 Compose 文件、镜像锁定文件、迁移和部署脚本。下载后先验证 SHA-256 与 Ed25519 签名；不要在生产服务器执行 `git pull`。

`register-saas-admin.sh` 必须在交互式终端执行，只能在空数据库上运行一次，用于创建 SaaS 平台管理员；它不会创建组织成员或应用 KEY。登录后再登记供应商、模型别名、候选路由、项目、应用、预算和 Virtual KEY。完整说明见 [SaaS 平台管理员](../saas-administrator.zh-CN.md)。

常用运维命令：

```bash
sudo scripts/deploy/metahubctl.sh backup /opt/metahub-state
sudo scripts/deploy/metahubctl.sh restore-check /opt/metahub-state /opt/metahub-state/backups/<prefix>
sudo scripts/deploy/metahubctl.sh restart /opt/metahub-state
```

升级前保留旧版本和已验证备份。迁移是向前的，不自动降级；升级失败时按恢复手册使用新的状态目录恢复。公网只开放 SSH/HTTPS，不要暴露 PostgreSQL、Redis 或内部 API 端口。
