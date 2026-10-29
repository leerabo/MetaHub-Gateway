# 配置说明

以 `infra/production/production.env.example` 为参考，并用 `prepare-production.py` 生成实际状态。运行时密钥挂载自 `/opt/metahub-state`，不会从 Git 工作目录读取。

在控制台配置模型厂商、价格、别名、路由、项目、应用、预算和虚拟 KEY。启用注册验证、邮箱验证或找回密码时，在平台管理界面配置 SMTP；先保存配置，再执行连接测试和发信测试。465 通常使用 TLS，587 通常使用 STARTTLS，邮箱服务商要求时使用应用专用密码。

公开仓库只包含模板和示例，绝不能提交真实 `.env`、API Key、SMTP 密码、数据库导出、备份加密密钥、客户数据或服务器私钥。
