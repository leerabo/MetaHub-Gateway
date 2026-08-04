# MetaHub Gateway JavaScript/Node 示例

这个独立 ESM 项目使用官方 `openai@6.47.0` 的公开接口访问 MetaHub Gateway，不引入 MetaHub 私有客户端或协议。运行环境要求 **Node.js 22 或更高版本**。

示例包含非流式 Chat Completions、通过 `.withResponse()` 获取握手 `request_id` 的 SSE 流式输出、有限自动重试、`AbortController` 流式总时限，以及不会泄露密钥、Prompt 或原始响应的安全错误格式。

## 安全边界

本项目只允许在 Node.js 服务端进程运行。**禁止让浏览器直接连接 Gateway，也不要在浏览器代码、页面状态、URL、日志或打包产物中保存长期 Virtual Key。** 源码未设置 `dangerouslyAllowBrowser`，客户端工厂还会在检测到 `window` 或 `document` 时拒绝构造客户端。

`.env` 仅供本机使用，不应提交到 Git、加入下载 ZIP 或写入日志。示例固定关闭官方 SDK 日志，也不会输出 Authorization、Virtual Key、Prompt、完整响应或原始 SSE 对象。

## 安装与配置

在 `examples/js` 目录执行：

```powershell
npm ci
Copy-Item .env.example .env
```

Linux 或 macOS：

```bash
npm ci
cp .env.example .env
```

复制后必须在本机 `.env` 中填写 `METAHUB_VIRTUAL_KEY`，并根据开发者门户显示的租户元数据确认 `METAHUB_CHAT_MODEL`。

| 变量 | 必填 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `METAHUB_BASE_URL` | 否 | `http://localhost:8000/v1` | 仅允许带主机的 `http`/`https` 地址，禁止 URL 内嵌凭据 |
| `METAHUB_VIRTUAL_KEY` | 是 | 无 | 只存在于 Node.js 服务端进程环境 |
| `METAHUB_CHAT_MODEL` | 否 | `default-chat` | 当前租户已启用的 Chat 模型别名 |
| `METAHUB_TIMEOUT_SECONDS` | 否 | `30` | `0.1` 到 `300` 秒；同时限制完整流式生命周期 |
| `METAHUB_MAX_RETRIES` | 否 | `2` | `0` 到 `10` 的整数 |

## 运行

非流式调用：

```powershell
node --env-file=.env src/chat.js --prompt "用一句话介绍企业级 AI Gateway"
```

流式调用：

```powershell
node --env-file=.env src/stream.js --prompt "流式介绍企业级 AI Gateway"
```

非流式入口显式传递 `stream: false`，成功时只输出模型文本和响应 `_request_id`。流式入口显式传递 `stream: true`，通过 `.withResponse()` 保存握手 `request_id`，只逐段输出非空文本；空 choices、usage、工具调用或拒绝块会被安全忽略。

`METAHUB_TIMEOUT_SECONDS` 不只是 SDK 建连超时。流式入口额外把 `AbortController.signal` 作为公开请求选项传给 SDK，因此收到响应头后永久停滞的 SSE 也会在总时限到达时中止。已经输出的片段不会被重放或自动重试。

## 错误与排查

失败只写入 `stderr`，格式固定为：

```text
error_code=<safe_code> status=<http_status|-> request_id=<safe_id|-> retryable=<true|false>
```

- `request_id` 有值表示请求已到达 Gateway，可交给平台管理员定位调用日志。
- `client.timeout`、`client.stream_timeout` 和 `client.connection_failed` 通常可重试，但业务层仍须设置重试上限。
- `client.api_error` 表示 Gateway 没有通过 SDK 公开字段提供合规错误码；示例不会解析异常消息或原始响应正文。
- `client.browser_runtime_forbidden` 表示代码被错误加载到浏览器环境，应改为由可信 Node.js 服务端调用。

## 测试

```powershell
npm ci
npm test
```

测试同时使用可注入 fake client 和真实 `openai@6.47.0`，并启动仅监听本机回环地址的临时 HTTP/SSE 服务，验证 `.withResponse()`、`requestID`/`request_id`/响应头/握手回退、状态错误、空响应、中途断流与总时限中止。
