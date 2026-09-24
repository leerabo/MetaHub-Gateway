# 本地试用入口与操作

打开 `http://localhost:5173/`，从首页登录。普通组织管理员的使用顺序如下：

1. 注册账号并创建组织；已有组织的账号直接进入组织空间。
2. 添加模型服务，在厂家列表选择服务商，保存自己的 API KEY 后测试连接。
3. 配置可用模型。支持 OpenAI 兼容模型目录的服务可读取模型列表后选择；不提供目录的服务可以手工填写厂家模型名称。
4. 配置调用模型名称和路由，再添加员工档案并为每人创建独立 MetaHub KEY。
5. 在“接入信息”页展开员工使用的工具，复制该工具的网关地址；将刚为该员工发放的一次性 MetaHub KEY 单独交付。支持设置界面的工具填写 Base URL 和 API Key；Codex、Claude Code CLI 可复制各自的配置示例。工具要求模型名称时填写页面显示的模型入口，或使用该工具提供的模型列表功能。员工无需注册或登录 MetaHub。

组织成员邀请用于需要登录管理后台的成员；普通员工发放 KEY 使用“员工与 KEY”，不需要发送邀请。组织管理员管理厂家凭据和员工额度；注册策略、邮件验证和人机验证开关由 SaaS 平台管理员管理。

`localhost` 地址适用于当前电脑。其他电脑无法用自己的 localhost 访问这里；面向员工跨电脑使用时，需要部署可访问的网关域名。

当前公开数据面提供模型发现（`GET /v1/models`）、Chat Completions（`/v1/chat/completions`）、OpenAI Responses（`/v1/responses`）、Anthropic Messages（`/v1/messages`）和 Embeddings（`/v1/embeddings`）入口。它们共用员工个人 KEY；服务提供模型列表不代表每个客户端都会自动读取，部分客户端仍要求手工选择模型。2026-09-13 已重启 Gateway 加载根地址兼容别名，根路径与 `/v1/*` 共十个接口均通过实际匿名鉴权检查。根路径此前返回 404 的旧实例已被替换。验证记录见 `outputs/prd148-runtime-tool-release-20260913.json`。

2026-09-12 更新：当前 [Codex 官方配置](https://developers.openai.com/codex/config-reference/) 的自定义 Provider 使用 `wire_api = "responses"`，KEY 由配置中的 `env_key` 指定的变量提供；Claude Code CLI 可将地址和 KEY 配置在个人目录的 `.claude/settings.json`。这是两种客户端自身的配置方式。MetaHub 已提供 `/v1/responses` 与 `/v1/messages` 兼容入口，各工具复用一个网关服务和个人 KEY。Cursor、WorkBuddy、DeepSeek Harness 需核实具体版本是否开放自定义接口及其协议；协议可调用不代表编辑文件、补全、内置工具等 Agent 功能全部通过联调。

员工交付的是同一个网关服务和个人 KEY。在接入页选择工具后，直接复制该工具要求的地址格式：OpenAI 兼容工具与 Codex 使用 `http://localhost:8000/v1`，Claude Code 使用 `http://localhost:8000`，由客户端追加 `/v1/messages`。这是同一个服务的路径差异，个人 KEY 保持相同；不要让员工自行推测后缀或把首页网址当作 API 地址。ChatGPT 网页聊天及只允许官方账号登录的工具不能通过这份配置接入。

产品目标是员工跨工具复用同一网关服务和个人 KEY。员工接入通常只需要两项：网关地址和管理员发放的个人 MetaHub KEY；管理员已配置默认路由时，模型名称不需要员工填写，只有工具强制要求时才选择页面给出的模型入口。工具的请求协议、工具调用回传和流式事件由网关适配，模型权限与额度继续绑定员工身份。员工无需分发厂家密钥、注册 MetaHub 或逐项填写环境变量；只有在某个客户端自身只接受配置文件/环境变量时，才使用该客户端的配置模板保存同样的地址和 KEY。不存在能导入所有工具的通用 JSON 格式。接入页的“复制接入说明”是可阅读的说明，不是工具配置文件，也不含 KEY。

## 本机服务

| 服务 | 地址 | 启动入口 |
| --- | --- | --- |
| 前端 | `http://localhost:5173/` | `scripts/dev/start-console.ps1` |
| 控制面 | `http://localhost:8001/health` | `scripts/dev/start-control-api.ps1` |
| Gateway | `http://localhost:8000/health` | `scripts/dev/start-gateway.ps1` |

已启动的服务不要重复启动。启动脚本读取本机 `.env`，日志在 `.logs`；不要把 `.env`、数据库、备份或含密钥的截图提交到仓库。前端生产制品通过 `console-web` 下的 `npm run build` 生成。

当前 0155 数据库升级记录、备份位置和测试范围见 `docs/testing/PRD-148-local-trial-release-20260912.md`。故障时先保留现场和当前数据库，再在备份副本验证恢复；当前版本没有执行过需要恢复用户数据的故障操作。

## 当前试用限制

2026-09-15 核对：本机 `.env` 已配置 `METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED=true`。配置值不代表服务正在运行；本次核对时 5173、8000、8001 均无监听，完整 QA 使用独立临时端口与数据库。此前关闭调度器的诊断记录保留在 `docs/testing/PRD-148-runtime-delivery-20260913.md`，不作为当前正式运行证据。

依赖后台任务的计量投影、异步通知、周期结算及定时治理仍须实际验收。本地只有一份未发布套餐草稿，尚无订阅或 entitlement；既有计量投影报错 `platform_metering.owner_invalid` 仍未闭合。formal9 已中断，formal10 已主动停止，二者均为短时健康接口观察，GA 计入为 0 秒。当前没有合格的连续 72 小时、0 P0/P1 观察，详见 `docs/testing/PRD-148-current.md`。

2026-09-15 本机迁移只读审计显示入口仍为 `expanded`、generation 0、旧入口写入围栏未启用；Workspace/Platform 分域迁移、对账、审批、安全复核、契约发布和旧入口退役记录均为空。37 个 scheduler 登记和 64 条队列契约仅证明目录已安装，不能当作分域切换或迁移终验。证据见 `outputs/prd148-console-migration-audit-20260915.json`。
