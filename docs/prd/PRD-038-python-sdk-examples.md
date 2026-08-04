# PRD-038 - Python SDK 与示例项目

状态：实现完成，已完成两轮设计自审、自动化测试与运行态验收。

## 需求理解

PRD-038 属于 `M0.5-W3/4`、`P0` 的 SDK Python 能力。目标是让中国境内企业开发者基于现有 OpenAI-compatible Gateway，在仓库内获得一个可以安装、配置和直接执行的 Python 示例项目，并能从控制台开发者门户下载同一份受控示例包。

Excel 权威需求要求：

- 示例可配置 `base_url`、Virtual Key、超时和重试次数。
- 示例必须覆盖非流式 Chat Completions 与 Embeddings。
- 错误处理必须展示统一错误码和 `request_id`，不能只打印 Python 堆栈。
- 仓库交付路径为 `examples/python`，并提供独立 README。
- 页面入口为“开发者门户下载区”。
- 下载动作记录 `sdk.example_downloaded` 事件。
- 明确不做“完整大 SDK”，不创造 MetaHub 私有调用协议。

架构文档要求 SDK 保持 OpenAI-compatible 使用习惯，并补充超时、重试、链路标识和错误解释。结合 Excel 的 MVP 边界，本 PRD 采用官方 `openai` Python 客户端作为传输与协议实现，只提供示例项目级的配置、调用和安全错误格式化，不发布自研 `MetaHubClient` SDK。

官方 `openai-python` 当前公开契约支持 `OpenAI(base_url=..., timeout=..., max_retries=...)`；失败请求可从 `APIStatusError.request_id` 取得响应头中的请求标识。实现只依赖这些公开接口，不使用以下划线开头的内部模块。参考：[openai-python 官方仓库](https://github.com/openai/openai-python)。

本 PRD 不改变 Gateway 热路径。现有 `/v1/chat/completions` 和 `/v1/embeddings` 已由 PRD-009 提供，调用日志、用量与成本追踪继续由 Gateway 负责。

## 涉及模块

- `examples/python/`
  - 新增独立 Python 示例项目、环境变量模板、README、Chat/Embeddings 入口和单元测试。
- `control-plane-api/src/metahub_control/developer_portal/`
  - 扩展 Python 示例元数据查询、受控 ZIP 构建和下载事件记录。
- `control-plane-api/src/metahub_control/console/service.py`
  - 增加 `sdk-examples` 导航定义，允许 admin/developer 进入。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加示例包不可用、归档失败和事件元数据非法错误码。
- `infra/migrations/0028_python_sdk_examples.*.sql`
  - 扩展 `developer_portal_events.event_type` 约束，使其接受 `sdk.example_downloaded`。
- `console-web/src/lib/pythonSdkExamples.js`
  - 查询 Python 示例元数据并携带登录 Token 下载 ZIP，不发送 `tenant_id`。
- `console-web/src/features/python-sdk-examples/`
  - 渲染 Python 示例下载区、配置项、运行命令、文件清单和下载反馈。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 在统一控制台壳中调度 `sdk-examples` 页面。
- `console-web/src/app/main.js`
  - 仅在进入 `sdk-examples` 时加载元数据，并绑定下载动作。
- `console-web/src/styles/main.css`
  - 增加紧凑的开发者下载区和响应式样式。
- `tests` 与开发文档
  - 增加示例项目、后端 API、数据库迁移和前端交互测试，并更新 README/测试记录。

## 数据表/字段设计

### `SDKExample`

`SDKExample` 仍是控制面按源码清单生成的 DTO，不单独落表。示例源码由 Git 管理，避免把同一份代码同时存入数据库导致版本漂移。

建议响应字段：

- `id: str`：固定为 `python-openai-example`。
- `tenant_id: str`：来自认证上下文，仅用于证明响应租户边界。
- `language: "python"`。
- `title: str`。
- `description: str`。
- `package_name: str`：示例包名。
- `version: str`：示例制品版本。
- `repository_path: str`：固定安全路径 `examples/python`。
- `download_url: str`：控制面受保护下载接口。
- `gateway_base_url: str`：当前运行配置对应的 Gateway `/v1` 地址。
- `chat_model_alias: str`：当前租户推荐 Chat 模型别名或安全占位。
- `embedding_model_alias: str`：当前租户推荐 Embeddings 模型别名或安全占位。
- `environment_variables: list[SDKEnvironmentVariable]`：变量名、是否必填、默认值说明；Virtual Key 不返回默认值。
- `commands: list[SDKCommand]`：安装、Chat、Embeddings 命令。
- `files: list[str]`：下载归档允许出现的受控相对路径。
- `capabilities: list[str]`：`chat`、`embeddings`、`timeout`、`retries`、`request_id_error_handling`。
- `request_id: str`、`trace_id: str`：页面数据查询链路标识。

### `developer_portal_events` 变更

SQLite 现有 `event_type` CHECK 仅允许 `developer.quickstart_viewed`。迁移 `0028` 采用建新表、复制、替换的方式扩展为：

```sql
CHECK (event_type IN ('developer.quickstart_viewed', 'sdk.example_downloaded'))
```

`sdk.example_downloaded` 事件字段：

- `event_id`：`dpe_` 前缀随机 ID。
- `tenant_id`：认证上下文租户。
- `user_id`、`session_id`：当前登录主体。
- `event_type`：固定 `sdk.example_downloaded`。
- `object_id`：固定 `python-openai-example`。
- `path`：固定 `/#sdk-examples`。
- `request_id`、`trace_id`：下载请求链路标识。
- `payload_json`：只允许 `language`、`artifact_version`、`file_count`、`example_id`。
- `created_at`：UTC 时间。

回滚时只保留 `developer.quickstart_viewed` 历史行，并删除 PRD-038 新增事件，因为旧 CHECK 无法保存新事件类型。该数据损失必须在 down migration 注释中明确。

不新增 SDK 下载记录表。低频行为审计复用开发者门户事件表即可，避免重复账本。

## API设计

### `GET /developer/sdk-examples/python`

用途：返回当前租户 Python 示例项目的下载元数据、当前 Gateway base URL、推荐 Chat/Embeddings 模型别名、环境变量和运行命令。

权限：仅 `admin`、`developer`。`viewer` 必须由后端返回 `auth.permission_denied`。

请求约束：

- Header：`Authorization: Bearer <control_plane_token>`。
- 不接受 `tenant_id`、Virtual Key、Provider Key 或任意源码路径参数。
- 示例目录和文件清单由后端常量定义，客户端不能指定文件。

响应示例：

```json
{
  "id": "python-openai-example",
  "tenant_id": "tenant_default",
  "language": "python",
  "title": "Python OpenAI-compatible 示例",
  "package_name": "metahub-gateway-examples",
  "version": "0.1.0",
  "repository_path": "examples/python",
  "download_url": "/developer/sdk-examples/python/download",
  "gateway_base_url": "http://localhost:8000/v1",
  "chat_model_alias": "default-chat",
  "embedding_model_alias": "default-embedding",
  "environment_variables": [],
  "commands": [],
  "files": [],
  "capabilities": ["chat", "embeddings", "timeout", "retries", "request_id_error_handling"],
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

模型推荐规则：

1. 仅查询当前 `tenant_id` 的 active 模型别名。
2. Chat 优先选择能力包含 `chat=true` 的模型，再回退到当前租户 active 别名，最后使用 `default-chat` 占位。
3. Embeddings 优先选择能力包含 `embeddings=true` 的模型；不存在时使用 `default-embedding` 占位，并在页面提示需要先配置可用别名。

### `GET /developer/sdk-examples/python/download`

用途：从仓库受控清单构建 ZIP 并下载，成功后记录 `sdk.example_downloaded`。

成功响应：

- HTTP 200。
- `Content-Type: application/zip`。
- `Content-Disposition: attachment; filename="metahub-python-examples-0.1.0.zip"`。
- 统一中间件继续附带 `X-Request-ID`、`X-Trace-ID`。

安全实现：

- ZIP 只读取服务端常量清单中的文件，不接收路径参数，不递归打包目录。
- 每个源文件解析后的绝对路径必须仍位于 `examples/python` 下。
- 包中只包含 `.env.example` 占位，不包含 `.env`、数据库、日志、缓存、Token 或 Key。
- 归档文件缺失或越界时，在发送二进制响应前抛统一 API 错误。
- 下载事件写入失败不阻断已生成制品，但写结构化安全日志并保留请求标识。

### 示例项目公开入口

安装后提供：

```powershell
python -m metahub_gateway_examples.chat --prompt "用一句话介绍企业级 AI Gateway"
python -m metahub_gateway_examples.embeddings --input "企业级 AI Gateway"
```

配置变量：

- `METAHUB_BASE_URL`：默认 `http://localhost:8000/v1`。
- `METAHUB_VIRTUAL_KEY`：必填，不提供默认值，不打印。
- `METAHUB_CHAT_MODEL`：默认 `default-chat`。
- `METAHUB_EMBEDDING_MODEL`：默认 `default-embedding`。
- `METAHUB_TIMEOUT_SECONDS`：默认 30，限制为合理正数范围。
- `METAHUB_MAX_RETRIES`：默认 2，限制为非负小整数。

示例调用官方 `OpenAI` 客户端；成功时打印结果和响应 `_request_id`，失败时只打印归一错误类别、公开错误码、HTTP 状态、`request_id` 和是否可重试，不打印 Virtual Key、Authorization、请求正文、完整异常或上游原始响应。

## 前端页面/交互设计

### 导航与加载

- 在“开发者”分组新增 `sdk-examples` 导航项，标题为“SDK 示例”。
- Admin/Developer 可见，Viewer 不可见；后端接口仍执行同样的 RBAC。
- 只有当前 active item 为 `sdk-examples` 时才请求元数据，避免每 15 秒刷新时产生无关负载。

### 页面结构

- 顶部显示“Python OpenAI-compatible 示例”、版本和当前 Gateway base URL。
- 配置区显示环境变量名、必填状态和安全说明；Virtual Key 只显示变量名。
- 命令区显示安装、Chat、Embeddings 三条可复制命令。
- 能力区明确覆盖 Chat、Embeddings、超时、重试、错误 `request_id`。
- 文件清单用于让开发者下载前知道制品内容。
- 主操作为带下载图标的“下载 Python 示例”按钮；下载期间禁用，避免重复事件和重复 ZIP 构建。

### 下载交互

1. 前端使用登录 Token 发起 `fetch`，不能使用裸 `<a>` 绕过认证头。
2. 成功后把 Blob 转为临时对象 URL，触发浏览器下载并立即释放 URL。
3. 文件名优先从安全解析后的 `Content-Disposition` 获取，非法值回退为固定文件名。
4. 成功反馈显示文件名和响应头 `request_id`。
5. 失败时读取统一 JSON 错误体，展示错误消息与 `request_id`，不展示堆栈或响应原文。

响应式布局保持单列可读；下载按钮、命令和长路径不得造成 390px 移动端横向溢出。

## 错误码设计

新增：

- `developer.sdk_example_unavailable`
  - HTTP 503，可重试。
  - 示例源码目录、元数据或租户模型信息无法读取。
- `developer.sdk_example_archive_failed`
  - HTTP 500，可重试。
  - 受控归档构建失败；响应不包含文件系统路径或原始异常。
- `developer.sdk_example_event_invalid`
  - HTTP 422，不可重试。
  - 服务内部尝试写入非白名单下载事件元数据。

复用：

- `auth.token_missing`
- `auth.token_invalid`
- `auth.permission_denied`
- `infra.unhandled`

所有 JSON 错误继续由统一异常中间件返回 `code`、`message`、`request_id`、`trace_id`、`suggestion` 和 `diagnosis`。二进制成功响应通过响应头暴露链路标识。

示例 CLI 不伪造服务端错误码：有 Gateway JSON 错误体时提取其公开 `code`；连接失败、超时或本地配置错误使用示例侧固定类别，并明确 `request_id=-` 表示请求未获得服务端响应。

## 安全与权限约束

- 元数据与下载接口仅允许 admin/developer，不能只靠控制台隐藏按钮。
- `tenant_id` 只来自 `AuthContext`，前端和下载 URL 不携带租户参数。
- ZIP 构建使用固定文件清单和目录归属检查，拒绝目录遍历、符号链接越界和任意文件读取。
- `.env.example` 只能包含占位符；测试扫描整个归档，禁止出现种子 Virtual Key、Provider Key、Authorization 值和本机数据库路径。
- 示例配置校验报错不得包含 `METAHUB_VIRTUAL_KEY` 的值。
- 示例错误处理不得打印 `repr(exception)`、完整响应体、请求正文或客户端配置对象。
- Provider Key 不进入示例项目、API、ZIP、日志、事件或前端状态。
- 下载事件 payload 使用事件类型对应的白名单；不能因复用 PRD-019 逻辑而扩大 quickstart 事件白名单。
- 示例调用后的用量、成本和失败请求仍由 Gateway 的 `request_logs`、`usage_ledger` 和错误日志追踪，本 PRD 不新增旁路调用。
- 下载事件属于低频控制面审计；其写入失败不得让可用制品变成下载失败，但必须记录固定错误码和 request_id。

## 测试用例

### 示例项目主流程

- 从环境变量解析 base URL、Virtual Key、Chat/Embeddings 模型、超时和重试次数。
- 构建官方 `OpenAI` 客户端时准确传入 `base_url`、`api_key`、`timeout`、`max_retries`。
- 使用假客户端执行 Chat，断言请求为非流式并输出文本和成功响应 `request_id`。
- 使用假客户端执行 Embeddings，断言 input 透传并输出向量维度和成功响应 `request_id`。
- 两个 `python -m` 入口可在注入假官方客户端后完整运行并返回退出码 0。

### 示例项目异常与安全

- 缺少 `METAHUB_VIRTUAL_KEY`：启动前失败，固定提示变量名，不输出其他环境值。
- 超时为非数字、负数或过大；重试为负数或过大：返回固定本地配置错误。
- 模拟带 `request_id` 的 APIStatusError：输出 Gateway 错误码和 `request_id`。
- 模拟超时/连接失败：输出可重试状态且 `request_id=-`。
- 异常对象中故意放入 Virtual Key、Authorization 和 Prompt：最终 stderr 不得包含这些内容。
- README 命令、`.env.example`、`pyproject.toml` 与实际模块路径一致。

### 控制面 API 主流程

- Developer 查询元数据返回 200，`tenant_id` 来自登录上下文，包含 Chat/Embeddings、超时、重试和错误处理能力。
- 元数据推荐模型只来自当前租户；缺少 embeddings 模型时返回安全占位和配置提示。
- Developer 下载返回有效 ZIP、固定文件名、统一请求头和受控文件清单。
- 下载后数据库写入一条 `sdk.example_downloaded`，tenant/user/session/request/trace 均正确。
- ZIP 每个成员都位于固定根目录，内容不包含真实密钥或本机路径。

### 权限与异常

- Viewer 查询元数据和下载均返回 `auth.permission_denied`。
- 无 Token 返回统一 401 和 `request_id`。
- 临时移除受控源文件：元数据或下载返回 `developer.sdk_example_unavailable`/`developer.sdk_example_archive_failed`，不泄露绝对路径。
- 事件 payload 注入非白名单键、复杂对象或超长字符串时返回内部校验错误且不落库。
- 两个租户分别查询，不能读到对方模型别名或事件。

### 前端

- API 客户端不发送 `tenant_id`，查询和下载都携带 Bearer Token 与 `X-Request-ID`。
- 页面转义标题、命令、文件名和模型别名，恶意 HTML 不能进入 DOM。
- 下载成功生成 Blob、解析安全文件名、释放对象 URL，并显示响应 `request_id`。
- 统一 JSON 错误映射保留 `request_id`；401 触发既有登录态失效流程。
- Viewer 的导航响应不包含 `sdk-examples`；直接调用后端仍被拒绝。
- 1440x900 与 390x844 运行态检查无横向溢出、遮挡或按钮文字截断。

### 回归门禁

- 数据库从 `0027` 升到 `0028`，保留已有 `developer.quickstart_viewed` 行。
- `0028` 回滚后移除 SDK 下载事件并恢复旧 CHECK，再次升级仍成功。
- 现有 Chat、Embeddings、快速开始、控制台导航和统一错误测试继续通过。
- `scripts/ci/check-comments.py` 通过；所有新增 Python/JavaScript 定义均有具体中文注释，关键权限、租户、密钥、错误和跳转分支有邻近中文说明。

## 代码修改计划

1. 新增 `examples/python` 独立示例项目和测试，先证明 Chat、Embeddings、配置、重试、超时与 request_id 错误展示。
2. 新增 `0028_python_sdk_examples.sql/down.sql`，扩展开发者门户事件约束并补迁移/回滚测试。
3. 扩展开发者门户 models/service/routes，提供元数据和受控 ZIP 下载。
4. 增加统一错误码与后端集成测试，覆盖 RBAC、租户隔离、归档安全和事件。
5. 增加控制台 `sdk-examples` 导航、API client、页面渲染、下载交互和前端测试。
6. 更新 README、目录说明和 PRD-038 测试记录。
7. 运行专项测试、全量 pytest、控制台测试/lint、中文注释门禁和数据库迁移回滚。
8. 启动现有服务，使用浏览器完成桌面/移动端下载区验收，并核对下载事件和 ZIP 内容。

## 设计自审 1：需求完整性与边界

逐项对照 Excel：

- `base_url`、Virtual Key、超时、重试均有环境变量与配置校验设计。
- Chat 与 Embeddings 各有独立可执行入口和测试。
- 错误处理明确从官方异常公开属性和 Gateway JSON 错误体提取 `request_id`，不只打印异常。
- `examples/python`、README、开发者下载区和 `sdk.example_downloaded` 均有对应交付物。
- 没有新建私有协议或完整 SDK；官方客户端承担 HTTP、序列化和重试实现。
- Gateway 热路径、用量和成本账本不被旁路。

发现并修正的边界问题：架构附录提到 SDK 还应包含流式、预算/Trace 示例，但 PRD-038 验收只要求 Chat 与 Embeddings，且 PRD-039 专门负责 JavaScript 流式示例。本条仅保留成功/失败 request_id 和日志排查说明，不把流式、预算 API 或 Debug Center 深链提前塞入。

结论：设计覆盖 PRD-038 全部显式要求，并保持 M0.5 范围。

## 设计自审 2：安全、可运行性与回滚风险

重点检查了四类风险：

1. 密钥泄露：示例错误处理不打印原始异常，ZIP 使用白名单，事件 payload 使用事件类型独立白名单，前端不保存 Virtual Key。
2. 任意文件下载：接口没有路径参数，源文件必须位于固定目录且逐项校验，不使用递归压缩。
3. “能展示但不能运行”：示例包有独立 `pyproject.toml`、模块入口、环境变量模板和可执行测试；控制台下载的是同一源码，而不是另一份字符串模板。
4. SQLite CHECK 变更：升级采用重建表保留旧事件；回滚显式过滤新事件后恢复旧约束，避免把非法事件复制进旧表导致回滚中断。

发现并修正的实现风险：下载事件若在 ZIP 构建前写入，会把失败下载计为成功；设计调整为先完整构建并校验归档，再记录事件，最后返回响应。事件写入失败采用 fail-open 仅针对低频产品分析，但会留下结构化错误日志；归档安全校验失败必须 fail-closed。

结论：可以进入编码。编码阶段必须把“下载制品与仓库源码同源”“错误输出不含原始异常”“后端 RBAC”作为硬门槛。

## 实现结果

- 在 `examples/python` 交付独立可安装示例项目，使用官方 `openai` 客户端完成非流式 Chat Completions 与 Embeddings 调用，并支持 `base_url`、Virtual Key、超时和有限重试配置。
- 示例错误处理统一输出稳定错误码与 `request_id`；任何异常分支均不回显原始异常、请求正文、Prompt、输入文本或 Virtual Key。
- 控制面新增 `GET /developer/sdk-examples/python` 与 `GET /developer/sdk-examples/python/download`，两条接口均从认证上下文确定 `tenant_id`，并在后端执行 admin/developer 权限校验。
- 下载接口只打包九个固定白名单文件，限制单文件与归档总大小，拒绝路径逃逸和符号链接；响应使用 `no-store` 与 `nosniff` 安全头。
- 数据库迁移 `0028_python_sdk_examples` 扩展开发者门户事件约束；下载成功后记录 `sdk.example_downloaded`，事件只包含语言、制品版本、示例 ID 和文件数。
- 控制台新增“SDK 示例”入口，展示租户内推荐模型、环境变量、运行命令、能力与制品清单；下载成功或失败均展示响应 `request_id`。
- README、目录说明和独立测试记录已同步更新，新增 Python/JavaScript 定义和关键权限、安全、错误分支均补充中文注释。

## 测试与验收结果

- 全量 CI：后端与 Python 示例共 `212 passed`；控制台共 `131 passed`；前端 lint 与中文注释门禁通过。
- 示例项目专项测试：`38 passed`，覆盖配置、Chat、Embeddings、CLI、超时/连接/API 异常和敏感信息防泄漏。
- 控制面专项与回归测试：SDK API、快速开始、导航和示例测试共 `46 passed`；数据库迁移与回滚测试 `9 passed`。
- 数据库运行态从 `0027` 升级到 `0028` 成功；浏览器下载后实际写入 `sdk.example_downloaded`，请求标识为 `console_python_sdk_1784080983459`，事件未包含密钥或示例内容。
- 浏览器验收：桌面端页面内容、命令和下载交互正常；390x844 临时视口下页面无横向溢出，侧栏与主内容正确纵向堆叠，面板和按钮未越界。
- 本地服务验收地址：控制台 `http://localhost:5173/#sdk-examples`，控制面 `http://localhost:8001`，Gateway `http://localhost:8000`。

详细命令与证据见 `docs/testing/PRD-038-tests.md`。
