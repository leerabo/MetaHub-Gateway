# 故障排查

先查看组件健康页和请求日志。Ubuntu 上可执行：

```bash
sudo scripts/deploy/metahubctl.sh status /opt/metahub-state
docker compose --env-file /opt/metahub-state/compose.env \
  -f /opt/metahub-releases/<version>/infra/production/compose.yml ps
```

依次检查 HTTPS/Caddy 与后端就绪状态、厂商凭据和模型可用性、路由回退、预算账本、SMTP 主机/端口/TLS/应用密码，以及 PostgreSQL 健康度和磁盘空间。数据库故障时不要删除卷，使用加密备份和 `restore-check`。分享诊断时只保留版本、请求 ID、组件、时间和健康结果，脱敏凭据、Token、邮箱、请求内容和客户数据。
