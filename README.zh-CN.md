# MetaHub —— 面向组织的 AI 网关

MetaHub 首先解决的是组织治理问题：给团队一个受控的网关 KEY，同时让管理员集中管理供应商凭据、应用权限、预算和可解释的多模型路由。

它不是只转发请求的个人中转站。每次调用都会先经过组织层级、应用 Virtual KEY、请求的模型别名、路由策略、预算和审计规则，再访问上游模型。

[English README](README.md) · [中文文档](docs/README.zh-CN.md) · [安全策略](SECURITY.md)

## 一个 KEY 可以调用多条模型路由

应用只需要一个 MetaHub Virtual KEY 和一个网关地址，就可以调用组织开放的多个稳定模型别名：

```text
一个应用 KEY
  ├─ team-chat  → 快速模型 → 主模型 → 备用模型
  ├─ team-code  → 编程模型 → 备用模型
  └─ team-fast  → 当前符合条件的最低成本模型 → 备用模型
```

应用永远拿不到 OpenAI、Anthropic、Gemini、Grok 或其他供应商的真实 KEY。管理员可以在别名后面更换供应商、模型版本、价格和回退顺序，而不需要修改所有客户端。

路由支持固定顺序、可用性优先、成本优先、质量 + 成本。成本优先会根据当前仍符合条件的价格重新比较，不会永久记住创建路由时最便宜的模型。每次选择都会记录候选模型、最终模型、回退尝试、预算结果和选择原因。

请先阅读[组织级网关模型](docs/organization-gateway.zh-CN.md)，再把 MetaHub 当作客户端网关接入。

## 核心能力

- 组织、团队、项目、应用、成员和角色权限管理。
- 一个 KEY 调用多个模型路由：应用使用组织定义的别名，别名背后可连接多个厂商和模型，支持优先级、故障切换、健康检查、成本优先和质量成本路由。
- 供应商治理：组织统一保存 OpenAI、Anthropic、Gemini、Grok 等供应商凭据，客户端只使用模型别名。
- 面向用户、项目和应用的虚拟 API KEY。
- 组织、项目、应用和人员预算，支持告警、用量统计和超额控制。
- 请求日志、Token 用量、成本估算、路由解释和审计记录。
- Ubuntu + Docker Compose 自托管部署，支持 PostgreSQL、备份、升级和回滚。

生产环境请使用版本化 GitHub Release，不要在服务器上直接拉取 `main`，也不要把数据库、密钥、SMTP 凭据或 `.env` 文件提交到仓库。

## 文档

请从 [英文主文档](README.md) 开始；完整中文使用说明见 [中文文档](docs/README.zh-CN.md)，重点阅读[组织级网关模型](docs/organization-gateway.zh-CN.md)和[SaaS 平台管理员](docs/saas-administrator.zh-CN.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。
