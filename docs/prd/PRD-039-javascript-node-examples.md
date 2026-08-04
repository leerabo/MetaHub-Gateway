# PRD-039 - JavaScript/Node 示例项目

状态：已完成设计、两轮独立自审、编码、自动化测试与运行态验收。

## 需求理解

PRD-039 属于 `M0.5-W3/4`、`P0` 的 SDK JS 能力，目标是让前端或 Node 开发者在服务端 Node.js 环境中，以 OpenAI-compatible 方式调用 MetaHub Gateway，并能运行非流式和流式 Chat Completions 示例。

Excel 权威需求要求：

- 仓库交付路径为 `examples/js`，提供可以安装和运行的 Node 示例项目。
- 使用官方 OpenAI JavaScript SDK 的兼容调用方式。
- 提供环境变量、非流式调用、流式输出和安全错误处理。
- 控制台“开发者门户下载区”提供受保护下载入口。
- 下载动作记录带 `tenant_id` 的 `sdk.example_downloaded` 事件。
- 验收重点为 Node 示例可运行、SSE 流式内容可逐段输出。
- 明确不推荐浏览器直接连接 Gateway 或在浏览器中持有 Virtual Key。

架构文档要求 JavaScript/TypeScript 与 Python SDK 优先，保持 OpenAI-compatible 使用习惯，并补充超时、重试、`trace_id`/`request_id` 和错误解释。PRD-039 仍属于示例项目，不建设完整私有 SDK，不新增 MetaHub 协议，也不提前实现预算 SDK、Debug Center 客户端或浏览器短期令牌。

官方 `openai-node` 支持非 EOL Node.js、`baseURL`、`timeout`、`maxRetries`、Chat Completions 异步流迭代，以及通过 `.withResponse()` 取得流式请求的 `request_id`。本项目在 2026-07-15 以 npm registry 的当前版本 `6.47.0` 完成锁定和验证，运行基线采用 Node.js 22 LTS 及以上；实现只使用公开接口，参考：[openai-node 官方仓库](https://github.com/openai/openai-node)。

## 涉及模块

- `examples/js/`
  - 新增独立 Node.js ESM 示例项目、环境变量模板、README、非流式/流式入口和内置测试。
- `control-plane-api/src/metahub_control/developer_portal/`
  - 新增 JavaScript 示例元数据和受控 ZIP 下载接口。
  - 把 Python 专用归档安全逻辑收敛为内部 `SDKArtifactSpec`，Python 与 JavaScript 共用路径、大小和事件约束。
- `console-web/src/features/python-sdk-examples/`
  - 扩展现有 SDK 下载页为 Python/JavaScript 分段视图，并增加浏览器直连安全提示。
- `console-web/src/lib/pythonSdkExamples.js`
  - 扩展 JavaScript 元数据读取与 ZIP 下载客户端，继续复用统一 `AuthApiError`。
- `control-plane-api/tests/integration/`
  - 新增 PRD-039 API、RBAC、租户隔离、ZIP 和事件集成测试。
- `console-web/tests/unit/`
  - 新增 JavaScript SDK 页面、接口、下载、转义和安全提示测试。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-039-tests.md`
  - 更新交付范围、目录和测试证据。

本 PRD 不修改 Gateway 热路径。现有 `/v1/chat/completions` 已支持 `stream=true` 的 SSE 透传、失败日志、用量和成本追踪，Node 示例继续经过同一 Virtual Key、路由、日志和 Usage Ledger 链路。

## 数据表/字段设计

### 数据库变更结论

不新增迁移。

PRD-038 的 `0028_python_sdk_examples.sql` 已把 `developer_portal_events.event_type` 约束扩展为：

```sql
CHECK (event_type IN ('developer.quickstart_viewed', 'sdk.example_downloaded'))
```

JavaScript 下载复用同一事件类型，通过 `object_id` 与 payload 的 `language` 区分制品，不需要为语言重复建表或新增事件类型。

### `SDKExample`

Excel 中的 `SDKExample` 在本 PRD 仍是控制面动态生成的 DTO，不单独落表。示例源码和版本由 Git 与 `package-lock.json` 管理，避免数据库副本与仓库代码漂移。

### `JavaScriptSDKExampleResponse`

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 固定 `javascript-openai-example` |
| `tenant_id` | string | 只取认证上下文，不接受请求参数 |
| `language` | literal | 固定 `javascript` |
| `title` | string | JavaScript/Node 示例标题 |
| `description` | string | OpenAI-compatible、流式和安全边界说明 |
| `package_name` | string | 固定 npm package name |
| `version` | string | 固定制品版本 `0.1.0` |
| `runtime` | string | 固定 `Node.js >= 22` |
| `repository_path` | string | 固定 `examples/js` |
| `download_url` | string | 固定下载接口 |
| `gateway_base_url` | string | 公开的 Gateway `/v1` 地址 |
| `chat_model_alias` | string | 当前租户推荐 Chat 模型别名 |
| `environment_variables` | array | 固定环境变量说明，不含真实值 |
| `commands` | array | 安装、非流式和流式运行命令 |
| `files` | array | ZIP 固定白名单 |
| `capabilities` | array | chat、stream、timeout、retries、request_id |
| `browser_direct_supported` | boolean | 固定 `false` |
| `security_notice` | string | 禁止浏览器持有长期 Virtual Key 的中文提示 |
| `request_id` | string | 控制面元数据请求标识 |
| `trace_id` | string | 控制面链路标识 |

### `sdk.example_downloaded`

沿用 `developer_portal_events` 字段：

- `tenant_id`、`user_id`、`session_id`：来自认证上下文。
- `object_id`：固定 `javascript-openai-example`。
- `path`：固定 `/#sdk-examples`。
- `request_id`、`trace_id`：来自统一中间件。
- `payload_json`：仅包含：

```json
{
  "language": "javascript",
  "artifact_version": "0.1.0",
  "file_count": 9,
  "example_id": "javascript-openai-example"
}
```

事件不得包含源码、Prompt、模型输出、环境变量值、Virtual Key、Provider Key、Authorization 或浏览器信息。

## API 设计

### `GET /developer/sdk-examples/javascript`

用途：读取当前认证租户的 JavaScript/Node 示例元数据。

权限：`admin`、`developer`；复用 `require_developer_portal_reader`。`viewer` 返回后端 403，未登录返回 401。

请求：无 query/body，不允许传 `tenant_id`、角色、源码目录、文件名或 Key。

成功响应：HTTP 200，响应体为 `JavaScriptSDKExampleResponse`，统一响应头携带 `X-Request-ID` 与 `X-Trace-ID`。

服务流程：

1. 校验 JavaScript 固定白名单文件真实存在且没有符号链接逃逸。
2. 从认证上下文读取 `tenant_id`。
3. 仅查询当前租户的 active Chat 模型别名；没有匹配时返回公开占位 `default-chat`。
4. 返回固定运行时、环境变量、命令、文件清单和浏览器安全提示。

### `GET /developer/sdk-examples/javascript/download`

用途：从仓库固定白名单构建 JavaScript 示例 ZIP。

权限和请求边界与元数据接口相同。

成功响应：

- HTTP 200。
- `Content-Type: application/zip`。
- `Content-Disposition: attachment; filename="metahub-javascript-examples-0.1.0.zip"`。
- `Cache-Control: no-store`。
- `X-Content-Type-Options: nosniff`。
- 统一 `X-Request-ID`、`X-Trace-ID`。

处理顺序：先完成路径、大小和 ZIP 构建，再写下载事件，最后返回响应。归档安全错误 fail-closed；低频产品分析事件写入失败 fail-open，并记录不含密钥的结构化错误日志。

### Node 示例公开配置

环境变量：

| 名称 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `METAHUB_BASE_URL` | 否 | `http://localhost:8000/v1` | Gateway OpenAI-compatible 地址 |
| `METAHUB_VIRTUAL_KEY` | 是 | 无 | 只存在 Node 服务端进程环境 |
| `METAHUB_CHAT_MODEL` | 否 | 当前租户推荐别名 | Chat 模型别名 |
| `METAHUB_TIMEOUT_SECONDS` | 否 | `30` | 转换为 SDK 毫秒超时 |
| `METAHUB_MAX_RETRIES` | 否 | `2` | SDK 有限自动重试次数 |

固定命令共四步，先创建只存在于本机的 `.env`，再运行示例：

```text
npm ci
Copy-Item .env.example .env
node --env-file=.env src/chat.js --prompt "用一句话介绍企业级 AI Gateway"
node --env-file=.env src/stream.js --prompt "流式介绍企业级 AI Gateway"
```

README 同时给出 Bash 的 `cp .env.example .env`；复制后必须填写 `METAHUB_VIRTUAL_KEY`，并按页面元数据确认 Chat 模型别名。`.env` 永远不进入 ZIP、Git、日志或页面状态。

## Node 示例设计

### 项目结构

```text
examples/js/
├── .env.example
├── README.md
├── package.json
├── package-lock.json
├── src/
│   ├── chat.js
│   ├── client.js
│   ├── config.js
│   ├── errors.js
│   └── stream.js
└── tests/
    ├── chat.test.mjs
    ├── client.test.mjs
    ├── config.test.mjs
    ├── errors.test.mjs
    ├── project-files.test.mjs
    └── stream.test.mjs
```

下载 ZIP 只包含 `.env.example`、README、package 文件和五个 `src` 文件，不包含测试、`.env`、`node_modules`、npm 缓存、日志或本机路径。

### 依赖与运行时

- ESM：`"type": "module"`。
- Node：`>=22.0.0`，使用 Node 原生 `--env-file` 与 `node:test`。
- 官方客户端：`openai` 固定为经项目验证的 `6.47.0`，并提交 `package-lock.json`。
- 不引入浏览器 bundle、前端构建器、dotenv、日志 SDK 或 MetaHub 私有客户端。

### 客户端构建

```javascript
new OpenAI({
  apiKey: config.virtualKey,
  baseURL: config.baseUrl,
  timeout: config.timeoutSeconds * 1000,
  maxRetries: config.maxRetries,
  logLevel: "off",
});
```

实现不设置 `dangerouslyAllowBrowser`。如果模块被错误加载到存在 `window/document` 的浏览器环境，客户端工厂在读取 Key 或发起请求前返回固定 `client.browser_runtime_forbidden`。

### 非流式示例

- 调用 `client.chat.completions.create({ model, messages, stream: false })`。
- stdout 只输出模型文本和响应 `_request_id`。
- 空 choices/content 返回固定 `client.empty_response`，不打印完整响应对象。

### 流式示例

- 调用 `client.chat.completions.create({ model, messages, stream: true }).withResponse()`。
- 从 `{ data: stream, request_id }` 保存请求标识。
- 使用 `for await ... of` 安全读取 `chunk?.choices?.[0]?.delta?.content`，仅非空字符串写入 stdout；空 choices、usage、工具调用或拒绝块不能触发异常。
- 正常结束补换行并输出 `request_id`；无文本分片返回 `client.empty_stream`。
- 流迭代中途失败时，优先使用异常 `request_id`，否则使用握手阶段保存的 `request_id`。
- SDK 的 `timeout` 只可靠覆盖建连和响应头阶段；示例额外用公开请求选项的 `AbortController.signal` 施加总流式时限，并在 `finally` 清理定时器。
- `maxRetries` 不会重放已经输出部分内容的流；握手完成后的断流直接返回安全错误，避免重复输出和重复计费。
- 不缓存完整响应，不把 Prompt、Key 或原始 SSE 对象写入日志。

### 安全错误格式

固定 stderr 格式：

```text
error_code=<safe_code> status=<http_status|-> request_id=<safe_id|-> retryable=<true|false>
```

- OpenAI `APIError`：安全读取 `status`；请求标识按 `error.requestID`、兼容字段 `error.request_id`、`error.headers.get("x-request-id")`、流握手 `request_id` 的顺序提取，并统一限制为单行安全标识。
- 当前 Gateway 统一错误体使用顶层 `code`，官方 SDK 不保证把它映射为 `APIError.code`；只有 SDK 公开字段存在且符合标识白名单时才保留，否则固定输出 `client.api_error`，绝不解析异常 message。
- `APIConnectionTimeoutError`：`client.timeout`，可重试。
- `APIConnectionError`：`client.connection_failed`，可重试。
- 本地配置错误：`client.missing_virtual_key`、`client.invalid_timeout`、`client.invalid_retries`。
- 浏览器运行时：`client.browser_runtime_forbidden`，不可重试。
- 未知异常：`client.unexpected_error`，不打印 `message`、`stack`、`cause` 或对象序列化结果。

## 前端页面/交互设计

入口继续使用“开发者 > SDK 示例”，不增加重复导航项。

### 页面结构

- 顶部使用 Python / JavaScript 分段控制，首次默认 Python；模块级 `activeSdkLanguage` 在 15 秒刷新和下载反馈后保留选择，登出时重置。
- 两种语言的元数据独立加载和显示错误；JavaScript 失败不应让 Python 制品消失，反之亦然。
- JavaScript 面板展示版本、Node 运行时、base URL、Chat 模型、环境变量、四步安装/配置/运行命令、能力和 ZIP 文件清单。
- 在 JavaScript 下载按钮附近显示醒目但克制的安全提示：“仅用于 Node.js 服务端；不要在浏览器代码中保存或发送长期 Virtual Key”。
- 下载按钮使用下载图标和文本；运行命令继续复用复制按钮。
- 下载成功显示文件名与响应 `request_id`；失败显示统一中文消息与 `request_id`。

### 交互与响应式

- 分段控制使用唯一的语言前缀 ID、`role=tablist/tab/tabpanel`、`aria-controls`、`aria-labelledby`、`aria-selected`、roving `tabindex` 和 `hidden`；支持左右方向键、Home/End、Enter/Space。
- 两种元数据按 Python 后 JavaScript 顺序读取：任一 401 立即统一清理会话并停止下一请求，因此不存在双 401 竞态；非 401 错误只写入对应语言状态。
- 下载仍通过带 Bearer Token 的 fetch 获取 Blob，使用一次性对象 URL，点击后立即 revoke。
- 文件名仅接受安全 ASCII `.zip` 基名，拒绝路径分隔符与响应头注入。
- 1280x720 和 390x844 下不得出现横向溢出；移动端分段控制可换行，摘要和两列内容改为单列，长命令在代码区内滚动而不撑开页面。
- 页面不提供 Virtual Key 输入框、浏览器 fetch 代码、`dangerouslyAllowBrowser` 开关或“复制 Key 到前端”动作。

## 错误码设计

控制面复用现有统一错误码，无需新增注册项：

| 错误码 | HTTP | 场景 | 处理建议 |
| --- | --- | --- | --- |
| `auth.unauthorized` | 401 | 未登录或会话失效 | 重新登录 |
| `auth.permission_denied` | 403 | viewer 访问元数据或下载 | 使用 admin/developer |
| `developer.sdk_example_unavailable` | 503 | 元数据依赖、源码根或固定文件不可用 | 检查 SDK 示例制品与模型目录 |
| `developer.sdk_example_archive_failed` | 500 | 路径、大小、读取或压缩失败 | 检查服务端 SDK 示例制品 |
| `developer.sdk_example_event_invalid` | 422 | 内部事件 payload 不符合白名单 | 修复服务端事件构造 |

现有错误码的公开 message/suggestion 从 Python 专属文案改为语言无关的 “SDK example artifact”，HTTP code 和状态保持不变。Node 示例本地 `client.*` 代码不会注册为控制面 HTTP 错误；它们是 CLI 安全错误标识。Gateway `request_id` 由官方客户端异常公开字段映射，Gateway 原始 code 无公开字段时安全退化为 `client.api_error`。

## 安全与权限约束

- 元数据和下载接口后端强制 `admin/developer`，前端隐藏不作为授权依据。
- `tenant_id` 只取 `AuthContext`；模型别名查询、下载事件均带当前租户条件。
- Virtual Key 只从 Node 进程环境读取，不出现在命令参数、URL、页面状态、ZIP、事件或日志。
- 禁止浏览器直连：不提供浏览器示例，不启用 `dangerouslyAllowBrowser`，源码包含运行时保护，页面给出明确安全提示。
- 官方 SDK 日志固定关闭，避免 debug 日志输出请求头或请求正文。
- ZIP 接口无路径参数；根目录、每级父目录和叶子成员均通过 `lstat` 拒绝任何符号链接，再做真实路径归属和普通文件校验。
- 读取前先以 `stat().st_size` 检查单文件 512 KiB 和总计 2 MiB 上限，随后执行最多“上限 + 1”字节的受限读取并复核长度，避免先把大文件完整读入内存。
- ZIP 内路径使用固定根目录和 POSIX 相对路径，不包含绝对路径、`..`、符号链接目标或系统文件。
- 事件只记录短标量白名单，并强制 language 为 python/javascript、版本格式合法、file_count 为拒绝 bool 的正整数、example_id 与当前制品规范一致。
- ZIP 构建完成后的事件调用边界捕获所有 `Exception` 并 fail-open；记录器内部负责数据库回滚，安全日志只含 tenant/user/request/trace 和固定错误码。
- 流式请求继续经过 Gateway 的 Virtual Key、模型路由、DLP、调用日志、Usage Ledger 和成本追踪；示例不绕过治理链路。
- 失败请求由 Gateway 进入 request/error log；Node CLI 只展示安全 code/status/request_id，不展示 Provider Key 或原始上游错误。

## 测试用例

### Node 项目主流程

- 配置正确解析 base URL、Virtual Key、模型、秒级超时和重试，并转换客户端毫秒超时；超时限定 `0.1–300` 秒，重试限定整数 `0–10`，拒绝空值、NaN、Infinity、负数和小数重试。
- 客户端使用 `baseURL/apiKey/timeout/maxRetries/logLevel=off`，不设置 `dangerouslyAllowBrowser`。
- 非流式 fake client 返回文本与 `_request_id`，CLI 输出二者。
- 流式 fake client 通过 `.withResponse()` 返回异步迭代器，多个 delta 按顺序逐段写入并输出握手 `request_id`。
- CLI 采用可注入的 `main({ argv, environ, clientFactory, stdout, stderr })`、`runChat` 和 `runStream`；仅直接执行入口时设置 `process.exitCode`，导入测试不会自动发请求。
- `package.json`、lockfile、README、`.env.example`、CLI 命令和实际源码入口一致。
- 安装锁定依赖后，Node 22+ 可执行 `npm test`、chat 和 stream 入口。

### Node 异常与安全

- 缺少 Key、超时/重试非法、未知参数、缺少 Prompt 返回固定本地错误。
- API 状态错误保留安全 Gateway code、status 与 `request_id`。
- 连接失败、超时和未知异常不输出异常 message/stack/cause。
- 流在输出若干 delta 后失败，先补换行再输出错误；错误行保留握手 request_id，已输出文本不被重复打印。
- 收到一个 delta 后永久停滞时总流式时限会 abort，清理定时器并返回 `client.stream_timeout`。
- 在浏览器模拟环境调用客户端工厂得到 `client.browser_runtime_forbidden`，不会构造官方客户端。
- 测试把 Virtual Key、Authorization、Prompt、响应正文放入异常对象，stdout/stderr 均不得包含这些值。
- 仓库与 ZIP 不包含 `.env`、`node_modules`、Provider Key、本机路径或浏览器直连开关。

### 控制面 API

- admin/developer 查询 JavaScript 元数据返回 200，tenant/model 只属于当前租户。
- admin/developer 下载得到有效 ZIP、固定文件名、九个受控成员和安全响应头。
- 下载后写入 `sdk.example_downloaded`，language 为 javascript，字段与事件白名单完全一致。
- viewer 返回 403，未登录返回 401，响应包含统一 code 和 `request_id`。
- 第二租户不能读取第一租户模型别名或事件。
- 缺文件、白名单路径逃逸、叶子链接到根内敏感文件、父目录符号链接、符号链接逃逸和超限文件 fail-closed 且不泄露绝对路径。
- 事件校验、连接、INSERT 或额外 RuntimeError 均不阻断已生成 ZIP，但产生不含密钥的结构化错误日志。
- Python 路由、DTO 字段、命令 ID、ZIP 文件名/根目录、九个成员和事件 payload 完全不变；制品规范在调用时读取模块常量，保留现有 monkeypatch 注入点，PRD-038 测试原样通过。
- 使用真实 `openai@6.47.0` 和本地 HTTP/SSE 服务验证 `.withResponse()`、`requestID/request_id`、状态错误、空 choices、中途断流和总流式超时，不以 fake client 单测替代真实协议测试。

### 前端

- Python/JavaScript 元数据请求都携带 Bearer 与 `X-Request-ID`，不发送 `tenant_id`。
- 分段控制正确处理点击、方向键、Home/End、Enter/Space、tab/tabpanel 关系和 `aria-selected`，刷新后保留当前语言，不会把一个语言错误扩散到另一个语言。
- JavaScript 元数据中的 HTML、命令、路径和安全提示全部转义。
- 页面明确出现 Node 服务端与禁止浏览器长期 Key 提示，不出现 Key 输入框。
- JavaScript 下载 Blob 使用安全文件名、释放对象 URL并显示响应 `request_id`。
- 401 清理登录态；403/5xx 保留统一错误和 `request_id`。
- 浏览器在 1280x720 与 390x844 下验证 `scrollWidth <= clientWidth`；tab、下载按钮、安全提示矩形位于容器内，长命令只允许在自身 `pre` 内滚动。

### 回归门禁

- `scripts/ci/check.ps1` 显式执行 `examples/js` 的 `npm ci --ignore-scripts` 与 `npm test`；全量 Python pytest、控制台 Node test、前端 lint 和中文注释门禁通过。
- 新增 JavaScript 定义、函数、类和关键流式/权限/路径/错误分支有具体中文注释。
- 本地真实 Gateway + fake OpenAI-compatible 上游完成一次非流式和一次流式 Node 调用；数据库 request log 与 usage/cost 记录可关联 request_id。

## 代码修改计划

1. 新增 `examples/js`、固定官方依赖和 Node 内置测试，先证明非流式、流式、总时限、安全错误与浏览器保护。
2. 在控制面新增 JavaScript DTO、元数据和下载路由，把 Python 归档安全逻辑收敛为调用时构造的双语言受控制品规范，并强化任意符号链接与预读取大小保护。
3. 复用 `sdk.example_downloaded` 事件，扩展事件记录参数并补 JavaScript/回归集成测试；不新增迁移。
4. 把 SDK 下载页扩展为 Python/JavaScript 分段视图，增加独立状态、通用下载绑定和浏览器 Key 安全提示。
5. 补前端 API、转义、tabs、下载、401/403 和响应式测试。
6. 更新根 README、目录说明与 PRD-039 测试记录。
7. 扩展 CI 运行 `examples/js` 的锁定安装和测试，再运行专项测试、全量 CI、lint、中文注释门禁和依赖锁校验。
8. 启动或重启本地服务，浏览器完成桌面/移动端下载区验收，并用真实 Node 示例验证 Gateway SSE、调用日志、用量和成本链路。

## 设计自审 1：控制面、安全与回归

第一轮独立审查发现：`SDKCommand` 不支持 stream；现有归档允许指向根内文件的符号链接且先读后限流；事件 fail-open 只捕获部分异常；冻结制品规范会破坏 Python 测试注入点；错误文案仍写死 Python；事件只校验字段名而未校验值。

设计已修正为：

- 扩展命令 ID，JavaScript 实际 DTO 构造进入测试。
- 共享归档器拒绝根、父目录和叶子任意符号链接，并在受限读取前后执行大小校验。
- ZIP 完成后的事件边界捕获所有事件异常；数据库事务仍在记录器内回滚。
- Python 规范在调用时从原模块常量构造，保留现有 monkeypatch、接口、ZIP 和事件契约。
- 统一错误消息改为语言无关，事件值按制品规范强校验。

结论：无需新增迁移；RBAC、tenant_id、固定白名单和事件表复用方向成立。第一轮问题全部进入设计与测试计划。

## 设计自审 2：Node 流式、错误与前端交互

第二轮独立审查发现：openai-node 异常实际可能使用 `requestID`；现有 Gateway 顶层 code 不保证进入 `APIError.code`；Node 20 已 EOL；SDK timeout 不覆盖响应头后的永久停流；首次运行缺少 `.env` 创建；空 choices、中途断流、真实 SDK 集成、tab 持久化、401 竞态和完整键盘模式设计不足。

设计已修正为：

- 锁定并真实验证 `openai@6.47.0`，运行时基线提升到 Node 22+。
- 安全提取 `requestID/request_id/header/handshake`，Gateway code 不可见时退化为 `client.api_error`。
- SSE 使用额外 AbortController 总时限，明确部分输出后不重试。
- 增加 `.env` 准备步骤、严格数值边界、可注入 CLI 入口和真实 HTTP/SSE 测试。
- 双语言元数据顺序加载以消除双 401 竞态，模块状态保留 active tab，并补全 WAI-ARIA 键盘交互和语言专属反馈/文件名兜底。

结论：Node 示例的公开 SDK 契约、流式失败边界、浏览器 Key 安全和前端交互已形成可执行设计。两轮审查通过，可以开始编码。

## 实现结果

- 新增 `examples/js` 独立 ESM 项目，锁定 `openai@6.47.0` 与 Node.js 22+，交付非流式 Chat、SSE 流式 Chat、总时限、有限重试、固定安全错误和浏览器运行时阻断。
- 新增 JavaScript 元数据与 ZIP 下载接口；归档器复用 Python 契约并增加根、父目录、叶子符号链接拒绝、读取前后大小与文件身份校验。
- `sdk.example_downloaded` 继续复用既有租户事件表，不新增迁移；事件失败在 ZIP 完成后 fail-open，事件值按语言、版本、文件数和示例 ID 严格校验。
- 控制台 SDK 页面扩展为完整 Python/JavaScript tabs，支持 ARIA 键盘交互、语言状态保持、独立错误态、安全下载文件名和 Node 服务端 Key 安全提示。
- SDK 模型推荐会把已明确 `unhealthy` 的 Provider 排在健康或未知状态 Provider 之后，避免下载后默认配置指向已知故障路由。
- CI 已纳入 JavaScript 示例锁定安装、33 项测试、控制台测试、lint 与中文注释门禁。

## 测试与验收结果

- 最终 `scripts/ci/check.ps1` 退出码为 0：Python `222 passed`，JavaScript 示例 `33 passed`，控制台 `140 passed`，lint 覆盖 89 个文件，中文注释门禁通过。
- 控制面与回归专项 `18 passed`，Ruff 通过；admin/developer、viewer 403、匿名 401、租户隔离、ZIP 路径/符号链接/大小限制和事件降级均已覆盖。
- Node 非流式调用成功：`req_c8ea8805ac8c44c7b5943a8f3cb5be71`；流式调用成功：`req_c113d84ab95047a1842c4ea416d79853`。
- 两条调用日志和用量账本均记录 `prd037-runtime`、输入 4 tokens、输出 3 tokens、总计 7 tokens、成本 0 CNY；流式请求状态为 `completed`。
- 控制台下载反馈与数据库事件使用 `console_javascript_sdk_1784086237296`，payload 为 JavaScript、版本 0.1.0、9 个受控文件。
- 浏览器桌面主流程通过；390x844 下命中移动断点、无横向溢出，JavaScript 面板、下载按钮和安全提示均位于 28-348px 内容边界内。

完整命令和验收证据见 `docs/testing/PRD-039-tests.md`。
