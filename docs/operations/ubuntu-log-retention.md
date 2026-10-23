# Ubuntu 试用环境的日志保留

Compose 的所有服务使用 Docker `journald` 驱动，标签为 `metahub/容器名`。没有把运行时环境变量附加到日志元数据。安装脚本将 `infra/production/journald-retention.conf` 安装到 `/etc/systemd/journald.conf.d/zz-metahub-retention.conf`，排序位于 Ubuntu 自带的 `syslog.conf` 后面，防止默认日志转发覆盖关闭设置。

磁盘日志预算为 256 MiB，内存日志预算为 64 MiB；保留期限为 7 天，每小时轮转，磁盘至少保留 1 GiB 空闲。大小限制和时间限制同时生效，空间紧张时可能提前清理。清理以归档日志文件为单位，当前文件和最后一个轮转区间有小时级边界。该 drop-in 管理整个 Ubuntu 系统日志，包括 Docker、SSH 和系统服务；已有集中日志策略的服务器应合并这些设置并检查最终有效值。

查看单个服务的日志：

```bash
sudo docker compose --env-file /opt/metahub-state/compose.env \
  -f /opt/metahub/infra/production/compose.yml logs --tail 100 control-api
sudo journalctl CONTAINER_NAME=metahub-trial-control-api-1 --since '1 hour ago'
sudo journalctl --disk-usage
sudo systemd-analyze cat-config systemd/journald.conf
```

日志配额是每台服务器共用的上限，不是数据库或备份的配额。日志不会代替数据库中的调用、账务和预算审计记录。生产入口关闭 HTTP 访问日志，PostgreSQL 禁止输出语句和失败行，启动失败只记录异常类型；仍需以正式运行的脱敏检查结果验收。

配置已加入；真实 Ubuntu 的配置加载、容器日志写入和轮转验收结果须查看部署验收记录，不能把配置文件本身当作已验证。

依据：[Docker journald 驱动](https://docs.docker.com/engine/logging/drivers/journald/) 和 [systemd journald.conf](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)。
