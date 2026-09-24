# 员工跨工具接入核查与页面修订

日期：2026-09-12。本记录对应接入体验与兼容协议修订，不代表具体第三方客户端已通过完整 Agent 验收，也不改变 PRD-148 正式验收状态。

运行勘误：此前只完成了源码与单元测试，旧 Gateway 进程尚未加载新增协议，真实 `/v1/responses`、`/v1/messages` 返回 404。2026-09-12 15:04 UTC 已重启并完成 9 项匿名 HTTP 检查，结果见 `outputs/prd148-protocol-runtime-20260912.json`。现在入口可用且进入统一鉴权；该结果不等同于真实员工调用成功或所有客户端 Agent 功能验收通过。

## 产品约定

员工应复用一个 MetaHub 网关服务和一把个人 KEY，在支持自定义模型接口的工具中工作，无需注册 MetaHub 或领取厂家 KEY。模型名称仅在客户端要求时提供；客户端配置文件和环境变量属于各工具的安装方式，不应成为 MetaHub 所有员工的必填表单。KEY 继续关联员工档案、权限、限额和用量。

页面首屏因此固定交付三项信息：一个网关服务地址、一把员工个人 KEY、一个按需填写的模型名称。OpenAI 兼容客户端通常把网关地址填入 Base URL；Anthropic/Claude Code 使用同一网关服务的 Messages 路径。CLI 工具若没有 API Key 输入框，才按该工具自己的设置或环境变量保存同一把 KEY；这不是 MetaHub 要求员工额外配置多组凭据。

本轮进一步统一“员工接入”的心智模型：员工只记住一个 MetaHub 网关服务地址和一把个人 KEY。Gateway 同时保留 `/v1/*` 正式协议入口，并提供根地址的兼容别名；因此会把 `/chat/completions`、`/responses`、`/messages`、`/models` 或 `/embeddings` 追加到服务地址的客户端，也能复用同一地址和同一把 KEY。页面不再把 `/v1` 与根地址并列成两套凭据，也不再把环境变量展示成员工必填项。

同一网关服务不意味着所有客户端都接受同一种配置文件或相同路径后缀。客户端需要的协议转换、模型映射、工具调用与流式事件由网关及工具配置指引承担；未开放自定义接口的工具不能承诺可接入。

## 查明的问题

- Gateway 现在提供统一的 `/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 与 `/v1/embeddings` 入口；它们共用同一套员工 KEY、路由、预算、DLP、限速和账本链路。
- 原页面让 Codex 切换 Chat Completions，但 [当前 Codex 官方配置](https://developers.openai.com/codex/config-reference/) 明确 `model_providers.<id>.wire_api` 只支持 `responses`。已获取并阅读该官方页面，不采用搜索摘要推断。
- [Claude Code 官方网关协议](https://code.claude.com/docs/en/llm-gateway-protocol) 列出 Anthropic Messages 的 `/v1/messages` 及可选 `/v1/messages/count_tokens`；不能以 OpenAI Chat Completions 入口代替。
- 原页面把 Cursor、WorkBuddy、DeepSeek Harness 整体标为“当前支持”，没有对应版本的 Agent 验收。文本请求成功不能证明工具执行、多轮结果回传、补全等功能可用。
- 未配置聊天路由时，原页面却提示省略模型即可使用默认路由，掩盖了真实配置缺口。

## 已修改

- 地址、KEY、按需模型名称放在工具说明前面；删除容易被误认成通用导入文件的 JSON 配置模板。
- 加入可复制、可展开查看的员工接入说明；明确个人 KEY 另行交付，说明中没有真实凭据。
- 六类工具说明默认折叠，用原生 details 支持键盘展开；明确区分接口支持、客户端待验证和原生协议缺失。SDK 和环境变量保留在高级折叠区域。
- 没有模型路由时提示管理员尚未配置；元数据加载中不出现带占位地址的复制项。
- 复制被浏览器拒绝时提供就地错误反馈，保留手工复制说明的入口。
- 已更正本地操作说明，并给历史旅程记录追加勘误，避免旧记录继续误导接入。

## 验证与范围

- 接入页、KEY、员工、模型路由、Python/JavaScript SDK、快速开始定向前端回归 67/67 通过。
- 第一次相关测试为 52/53，其中失败项是模型路由列表已使用“可选模型入口”，测试仍期望旧“员工使用名称”；核对现有页面后同步该断言，未为通过测试改回旧文案。
- Console lint 通过，833 文件；生产入口检查通过，275 模块、293 资源。
- 运行中 Gateway `/health` HTTP 200；只读检查 OpenAPI 确认上述协议缺口。未调用收费模型、未访问员工密钥或改写用户数据。
- 前后端资源摘要同步为 `a8e9cbb2c8f745cab86c781fac239d880f56f26fb71cdf96cbbf54093cd47c6c`，精确清单一致性测试 1/1 通过；生产构建通过，293 资源。
- 本地 `http://localhost:5173/` 的接入页模块、KEY 页模块、主应用模块及控制台样式均返回 HTTP 200，四项内容与当前源码一致。前端已可刷新使用；本轮未重启控制面或数据面。
- 本轮按用户要求未使用 computer use，不声称第三方工具真人或 Agent 实测通过。

## 尚待实现的兼容能力

Responses 和 Anthropic Messages 入口现在复用现有 KEY 身份、模型路由、DLP、预算、限速与账本处理。当前验收覆盖普通响应、流式文本、工具调用/结果回传的协议转换；Responses 专有内置工具、Anthropic 特有扩展和不同客户端的高级 Agent 行为仍需按客户端版本单独验证，不能把协议入口等同于所有 Agent 功能均可用。

各工具必须记录具体版本、配置入口、适用功能及真实调用证据。未完成上述工作前，本次页面交付不能被计为“所有工具可用”，也不能被作为 PRD-148 全部完成的证据。

## 工具配置补充设计自审

第一轮（员工任务）：首屏只呈现网关、个人 KEY 和按需模型。员工展开自己的工具后，应能直接复制该工具的地址并查看配置位置；不要求员工判断 Chat/Responses/Messages 协议。SDK 环境变量继续折叠。不能虚构 Codex 或 Claude Code 有统一的地址/KEY 登录表单，不能将 ChatGPT 官网聊天等同于 Codex 自定义 Provider。

第二轮（地址与凭据）：Claude Code 官方连接说明明确客户端在 Base URL 后追加 `/v1/messages`，所以必须仅移除权威 OpenAI Base URL 的末尾 `/v1`，保留反向代理前缀。Codex 保留 `/v1`，使用 `wire_api = "responses"`。配置示例只含管理员提供的地址、模型及占位凭据；不读取员工 KEY、不写入浏览器存储。缺少地址或路由时不生成可复制配置。文案区分协议实现与客户端实测。

本次获取并阅读官方来源：

- https://developers.openai.com/codex/config-reference/
- https://developers.openai.com/codex/config-advanced/
- https://code.claude.com/docs/en/llm-gateway-connect

流式修复第一轮自审：旧适配器在替换 `response.body_iterator` 后，生成器又读取这个属性，实际成为自迭代；既有四项协议单元测试均未迭代 SSE，不能证明流式可用。Responses 工具事件还缺少 item added/done 与完整完成对象；Anthropic 总是 end_turn、零用量。必须以消费完整流和工具结果下一轮回传验证，保留实际失败证据。

流式修复第二轮自审：保存原始迭代器，沿用既有 Gateway 响应对象和 finally 清理，使用现有有界 SSE framer 处理跨字节 UTF-8。工具调用 ID、参数、文本、用量和结束原因不得在转换中丢失；上游错误/中断不得生成成功终态。此修复只覆盖兼容子集，Responses 专有工具及真实客户端仍单独验收。

## 本轮修复与实际证据

- 页面按工具给出可复制地址，Claude Code 自动移除末尾 `/v1` 并保留反向代理前缀；Codex 与 Claude Code CLI 分别提供自己的配置示例，KEY 不进入页面状态或说明。修正了虚构通用配置界面及“所有桌面工具均不用环境变量”的表述。
- 原流式适配器自迭代故障已由实际读流复现：`outputs/tool-streams-before-20260912.xml`。修复抽入 `gateway-data-plane/src/metahub_gateway/api/protocol_streams.py`，保留原 Gateway 响应包装、断连与清理链路。Responses 现在提供完整 item、参数、文本与完成事件；Anthropic 保留工具身份、结束原因及用量。中文跨字节、错误、截断、达到输出上限和首事件断连均有回归。
- `outputs/tool-access-frontend-20260912.xml`：2141/2141，通过，无失败、错误或跳过。Console lint 和生产构建通过，293 个资源，摘要 `258fe996f1d1a000408d6f2390d2054f34cb5ca4db8b29a9d94dfaf31c22d7e3`。
- `outputs/tool-gateway-regression-20260912.xml`：70/70，通过，219.34 秒，覆盖协议、默认模型、原有流式安全原子性、断连/持久化及流式用量集成。在补充首事件断连检查后，`outputs/tool-protocol-final-20260912.xml` 18/18 通过，包含 17 项当前协议测试与 1 项精确前后端资源清单核对。
- OpenAI SDK 实际消费转换后的 SSE，并回传工具执行结果；该测试使用 httpx MockTransport 和合成响应，不是远程模型或 Codex 客户端实测。Claude Code 使用协议事件与工具结果回传测试，不冒充客户端联调。
- 已重启本地 Gateway（PID 7536）与 Control API（PID 9608），前端继续由原本地服务提供。`outputs/tool-access-runtime-20260912.json` 的 7 项只读 HTTP 检查通过：三个服务健康、两项页面资源字节一致、两个协议到达鉴权。匿名 401 不计为员工模型调用成功。
- 本轮 5 个修改/结果文件的常见凭据标记扫描为 0；未读写真实员工 KEY、未调用收费模型。没有使用 computer use。
- 当前源码摘要为 `9d84c08f7958d7381626946e3760e8a1f43c712d229a48e336baf368aea5973d`，文件数 2671。批次完整 QA 已启动于 `.artifacts/product-qa/20260912-233002`，结束前不计为通过。PRD-148 正式门禁仍未满足。
- 当前版本曾运行连续观察 `outputs/continuous-stability-20260912w`，2026-09-12 15:33:27 UTC 启动。它在浏览器 QA 资源竞争期间第 7 个窗口出现 3 次 Control API `read_timeout` 后按规则终止；首 6 个窗口无错误，失败现场保留，不累计为 72 小时。QA 结束后必须新建观察目录重新开始，`ga_approval=false` 不代表正式发布通过。
