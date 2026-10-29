# 快速开始

MetaHub 包含组织控制面和接收 OpenAI 兼容请求的数据面。典型部署使用 Docker Compose、PostgreSQL 和独立的私有状态目录。

准备 Ubuntu、域名、HTTPS、备份和模型供应商凭据，下载并校验签名 Release，创建私有状态目录，执行安装脚本，再从交互终端运行 `scripts/deploy/register-saas-admin.sh` 创建 SaaS 平台管理员。随后配置供应商、模型别名、候选路由、项目、应用和虚拟 API KEY；同一个应用 KEY 可以调用多个模型别名，最后发送一条测试请求。

不要把生产凭据放进 Issue、日志、截图或公开配置文件。
