# MetaHub Gateway Python 示例

这个独立项目使用官方 `openai` Python 客户端的公开接口访问 MetaHub Gateway，不引入 MetaHub 私有协议。示例覆盖：

- `base_url` 与 Virtual Key 环境变量配置；
- 请求超时和自动重试；
- 非流式 Chat Completions；
- Embeddings；
- 成功响应与失败响应的 `request_id`；
- 不输出密钥、请求正文、原始响应和 Python 堆栈的安全错误格式。

## 安装

需要 Python 3.10 或更高版本。在本目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

开发与测试安装：

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

## 配置

`.env.example` 是变量清单，不会由示例自动读取。这样可以避免误把本地 `.env` 或真实 Virtual Key 纳入程序日志与分发包。PowerShell 配置示例：

```powershell
$env:METAHUB_BASE_URL = "http://localhost:8000/v1"
$env:METAHUB_VIRTUAL_KEY = "<控制台签发的 Virtual Key>"
$env:METAHUB_CHAT_MODEL = "default-chat"
$env:METAHUB_EMBEDDING_MODEL = "default-embedding"
$env:METAHUB_TIMEOUT_SECONDS = "30"
$env:METAHUB_MAX_RETRIES = "2"
```

Linux 或 macOS：

```bash
export METAHUB_BASE_URL="http://localhost:8000/v1"
export METAHUB_VIRTUAL_KEY="<控制台签发的 Virtual Key>"
export METAHUB_CHAT_MODEL="default-chat"
export METAHUB_EMBEDDING_MODEL="default-embedding"
export METAHUB_TIMEOUT_SECONDS="30"
export METAHUB_MAX_RETRIES="2"
```

配置约束：

| 变量 | 必填 | 默认值 | 约束 |
| --- | --- | --- | --- |
| `METAHUB_BASE_URL` | 否 | `http://localhost:8000/v1` | 仅允许 `http`/`https`，且地址必须包含主机 |
| `METAHUB_VIRTUAL_KEY` | 是 | 无 | 只使用 Virtual Key，不要填 Provider Key |
| `METAHUB_CHAT_MODEL` | 否 | `default-chat` | 当前租户启用的 Chat 模型别名 |
| `METAHUB_EMBEDDING_MODEL` | 否 | `default-embedding` | 当前租户启用的 Embeddings 模型别名 |
| `METAHUB_TIMEOUT_SECONDS` | 否 | `30` | `0.1` 到 `300` 秒 |
| `METAHUB_MAX_RETRIES` | 否 | `2` | `0` 到 `10` |

## 运行 Chat

```powershell
python -m metahub_gateway_examples.chat --prompt "用一句话介绍企业级 AI Gateway"
```

也可以使用安装后命令：

```powershell
metahub-chat-example --prompt "用一句话介绍企业级 AI Gateway"
```

成功时会输出模型文本和响应的 `request_id`。请求显式设置 `stream=False`，不会进入 SSE 流式模式。

## 运行 Embeddings

```powershell
python -m metahub_gateway_examples.embeddings --input "企业级 AI Gateway"
```

也可以使用安装后命令：

```powershell
metahub-embeddings-example --input "企业级 AI Gateway"
```

成功时只输出向量维度和 `request_id`，不会把完整向量写入终端。

## 错误与排查

失败输出固定包含 `category`、`code`、`status`、`request_id` 和 `retryable`。示例不会输出原始异常、Authorization、Virtual Key、Prompt、Embedding 输入或完整 Gateway 响应。

```text
请求失败: category=gateway_api code=auth.token_invalid status=401 request_id=req_x retryable=false
```

- `request_id` 有值：请求已到达 Gateway。把该值交给平台管理员，可在调用日志中定位失败请求。
- `request_id=-`：连接、超时、本地配置或客户端初始化阶段没有获得服务端响应。
- `retryable=true`：通常是超时、连接异常、限流或服务端错误；仍应使用有上限的重试策略。
- `code=client.dependency_missing`：先执行 `python -m pip install -e .` 安装官方客户端。

官方客户端会按 `METAHUB_MAX_RETRIES` 自动重试部分连接错误、超时、限流和服务端错误。业务侧不要再进行无上限重试，以免扩大流量和成本。

