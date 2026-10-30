# Ubuntu 部署

这是 MetaHub 支持的自托管方式，适用于 Ubuntu 22.04+ / 24.04+、x86_64 或 arm64。发布包带版本号和签名；数据库卷、密钥、备份和运行状态都放在发布目录之外。

## 用户直接执行的安装命令

Release 发布后，把下面整段复制到 Ubuntu SSH 会话。先替换 `VERSION` 和域名。命令下载版本化 Release 包，不会克隆 `main` 分支。

```bash
set -e
REPOSITORY='leerabo/MetaHub-Gateway'
VERSION='2.0.0'
DOMAIN='gateway.example.com'
BUNDLE="metahub-${VERSION}-source.tar.gz"
RELEASE_ROOT="/opt/metahub-releases/${VERSION}"
STATE='/opt/metahub-state'
URL="https://github.com/${REPOSITORY}/releases/download/v${VERSION}/${BUNDLE}"

# 只安装下载和校验 Release 所需的基础工具。
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg openssl python3
sudo install -d -m 0755 /opt/metahub-releases
if sudo test -e "$STATE"; then
  echo "${STATE} 已存在；已有部署请使用升级流程。" >&2
  exit 1
fi
cd /tmp
curl -fL -o "$BUNDLE" "$URL"
curl -fL -o "${BUNDLE}.sha256" "${URL}.sha256"
sha256sum -c "${BUNDLE}.sha256"
sudo mkdir -p "$RELEASE_ROOT"
sudo tar -xzf "$BUNDLE" -C "$RELEASE_ROOT"
sudo bash "$RELEASE_ROOT/scripts/deploy/install-ubuntu-dependencies.sh"
sudo python3 "$RELEASE_ROOT/scripts/deploy/prepare-production.py" --state "$STATE" --domain "$DOMAIN"
sudo bash "$RELEASE_ROOT/scripts/deploy/metahubctl.sh" install "$STATE"
sudo bash "$RELEASE_ROOT/scripts/deploy/register-saas-admin.sh" "$STATE"
sudo bash "$RELEASE_ROOT/scripts/deploy/metahubctl.sh" status "$STATE"
```

Release 页面还会提供 `metahub-release.json` 和 `metahub-release.sig`。解压前必须使用运维端保存的 Ed25519 公钥验证签名；上面的 SHA-256 校验是额外的完整性检查，不能替代签名验证。签名无法验证时必须停止安装。

## 主机准备

```bash
sudo install -d -m 0755 /opt/metahub-releases
```

`prepare-production.py` 会创建私有状态目录，并且拒绝覆盖已有状态；如果 `/opt/metahub-state` 已存在，请使用升级流程。

发布包必须同时包含 `release-manifest.json`、生产 Compose 文件、镜像锁定文件、迁移和部署脚本。下载后先验证 SHA-256 与 Ed25519 签名；不要在生产服务器执行 `git pull`，也不要用未固定版本的分支压缩包替换 Release。

`register-saas-admin.sh` 必须在交互式终端执行，只能在空数据库上运行一次，用于创建 SaaS 平台管理员；它不会创建组织成员或应用 KEY。登录后再登记供应商、模型别名、候选路由、项目、应用、预算和 Virtual KEY。完整说明见 [SaaS 平台管理员](../saas-administrator.zh-CN.md)。

常用运维命令：

```bash
sudo scripts/deploy/metahubctl.sh backup /opt/metahub-state
sudo scripts/deploy/metahubctl.sh restore-check /opt/metahub-state /opt/metahub-state/backups/<prefix>
sudo scripts/deploy/metahubctl.sh restart /opt/metahub-state
```

升级前保留旧版本和已验证备份。迁移是向前的，不自动降级；升级失败时按恢复手册使用新的状态目录恢复。公网只开放 SSH/HTTPS，不要暴露 PostgreSQL、Redis 或内部 API 端口。

## 安装后的验收

打开 `https://<你的域名>/identity.html?view=login`，使用 SaaS 管理员登录，然后配置供应商、模型别名、候选路由、项目、应用、预算和 Virtual KEY。邀请用户前，至少发送一条测试请求，确认请求记录、路由解释、Token 用量、成本和预算台账都正确。

安装命令默认只在回环地址启动服务。完成 DNS、证书和主机名检查后，再接入现有 HTTPS 反向代理。如果根域名已经由其他服务使用，请使用 `gateway.example.com` 这类独立子域名，不要覆盖原有站点配置。
