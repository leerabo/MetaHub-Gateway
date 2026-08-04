# PRD-019 - 5分钟快速接入页

状态：已完成实现、自动化测试和文档更新。

## 需求理解

PRD-019 属于 M0-W2/P0 的开发者门户能力。目标不是建设完整文档站，而是在登录后的控制台里提供一个“5 分钟完成第一次非流式 chat 调用”的快速开始页，让开发者能直接复制 Python、JavaScript 和 curl 示例，并把本租户当前可用的 Gateway base_url、推荐模型别名和安全的 Virtual Key 占位说明自动填入。

本 PRD 必须满足：

- 页面入口为“开发者门户-快速开始”，需要纳入 PRD-018 的统一控制台骨架和角色化导航。
- 后端提供 `GET /developer/quickstart-data`，按当前登录用户的 `tenant_id` 生成快速开始数据。
- 示例覆盖 Python、JavaScript 和 curl，并统一使用 OpenAI-compatible `/v1/chat/completions` 非流式调用。
- 示例自动填入 `base_url` 和推荐 `model`，Virtual Key 只能以环境变量或占位符出现，不允许后端返回明文 Key。
- 页面展示常见错误和排查入口，至少覆盖鉴权、模型、请求体和上游不可用几类 M0 常见问题。
- 触发 `developer.quickstart_viewed` 事件，后续可接入审计或 Webhook。
- 新开发者按页面步骤能在 5 分钟内完成一次非流式 chat 调用。

边界：

- M0 不做完整文档站搜索、全文索引、文档版本管理、SDK 自动安装器、在线 Notebook 或多语言大而全示例库。
- OpenRouter 和硅基流动只作为对标或迁移兼容对象，不作为默认模型厂家或示例中的默认上游。
- 默认上游仍是模型厂商、云端模型 Endpoint 或客户本地 OpenAI-compatible Endpoint。
- 快速开始页不保存 Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。

架构约束：

- 控制面只承载低频管理和开发者门户数据，不进入模型调用热路径。
- 开发者门户 P0 需要提供 quick start、API reference、SDK examples、error code center 的闭环，本 PRD 只实现 quick start 最小闭环。
- 所有业务数据默认带 `tenant_id`，查询和事件写入都只能使用认证上下文恢复租户。
- 控制台隐藏入口只是 UX，后端必须做 admin/developer 权限校验。

## 涉及模块

- `control-plane-api/src/metahub_control/developer_portal/`
  - 新增开发者门户后端模块，承载 Quickstart DTO、数据生成和事件写入。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增或复用 admin/developer 读权限依赖，viewer 不能读取快速开始数据。
- `control-plane-api/src/metahub_control/console/service.py`
  - 在导航定义里新增 `quickstart` 入口，Admin/Developer 可见，Viewer 不可见。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增开发者门户错误码。
- `infra/migrations`
  - 新增开发者门户事件表迁移，记录 `developer.quickstart_viewed`。
- `console-web/src/lib/developerQuickstart.js`
  - 新增快速开始数据 API 客户端，不发送 `tenant_id`。
- `console-web/src/features/developer-quickstart/`
  - 新增快速开始页渲染函数，负责步骤、代码示例、常见错误和复制按钮。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 接入 `quickstart` active navigation 内容区。
- `console-web/src/app/main.js`
  - 登录后加载 quickstart 数据，绑定复制按钮，保留已有调试台临时 Virtual Key 输入逻辑。
- `console-web/src/styles/main.css`
  - 增加快速开始布局、代码块、步骤状态和错误排查列表样式。
- `tests`
  - 新增后端集成测试和前端 Node 单元测试。

## 数据表/字段设计

### `developer_portal_events`

用于记录开发者门户最小事件。PRD-019 只写入 `developer.quickstart_viewed`，后续 PRD 可扩展 API 文档、SDK 示例或错误码中心事件。

- `event_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `user_id TEXT NOT NULL`
- `session_id TEXT`
- `event_type TEXT NOT NULL CHECK (event_type IN ('developer.quickstart_viewed'))`
- `object_id TEXT NOT NULL`
- `path TEXT NOT NULL`
- `request_id TEXT NOT NULL`
- `trace_id TEXT`
- `payload_json TEXT NOT NULL DEFAULT '{}'`
- `created_at TEXT NOT NULL`
- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (user_id) REFERENCES users(user_id)`
- `FOREIGN KEY (session_id) REFERENCES sessions(session_id)`

索引：

- `idx_developer_portal_events_tenant_time(tenant_id, created_at DESC)`
- `idx_developer_portal_events_user_time(tenant_id, user_id, created_at DESC)`
- `idx_developer_portal_events_type_time(tenant_id, event_type, created_at DESC)`

说明：

- `object_id` 对本 PRD 固定为 `quickstart`。
- `payload_json` 只允许安全元数据，例如 `model_alias`、`has_active_virtual_key`、`example_ids`，不得包含 Virtual Key 明文、Provider Key、Authorization、Prompt、Response、messages 或 input。
- 事件写入失败不应阻塞 quickstart 页面主响应，但必须写结构化错误日志，避免开发者门户不可用。

### `SDKExample`

后端生成的 DTO，不落表。

- `id`
- `tenant_id`
- `language`
- `title`
- `description`
- `install_command`
- `code`
- `copy_label`
- `base_url`
- `model_alias`
- `uses_env_var`

说明：

- `code` 中的 Virtual Key 一律使用环境变量或 `<METAHUB_VIRTUAL_KEY>` 占位。
- `tenant_id` 来自认证上下文，用于证明示例针对当前租户生成；不接受前端传入。

### `QuickstartStep`

后端生成的 DTO，不落表。

- `id`
- `tenant_id`
- `order`
- `title`
- `description`
- `status`
- `action_label`
- `navigation_item_id`
- `target_hash`

`status` 取值：

- `ready`
- `action_required`
- `missing`

### `QuickstartCommonError`

后端生成的 DTO，不落表。

- `code`
- `title`
- `cause`
- `fix`
- `link_target`

建议覆盖：

- `gateway.auth_missing`
- `gateway.auth_invalid`
- `gateway.model_missing`
- `gateway.messages_missing`
- `gateway.model_not_found`
- `gateway.upstream_unavailable`
- `gateway.upstream_error`

## API设计

### `GET /developer/quickstart-data`

用途：返回当前租户的快速开始数据，包括 Gateway base_url、推荐模型别名、Virtual Key 安全占位、步骤、SDK 示例和常见错误。

权限：当前用户必须为 `admin` 或 `developer`。`viewer` 返回 `auth.permission_denied`。

请求：

- Header：`Authorization: Bearer <control_plane_token>`
- 不接收 `tenant_id`、`user_id`、`role` 或明文 Virtual Key。

推荐模型别名选择：

1. 优先选择当前运行环境对应的 active `model_aliases.environment`。
2. 若不存在，则选择当前租户第一个 active alias。
3. 若仍不存在，返回占位 `default-chat`，同时步骤状态标记模型别名 `action_required`。

Virtual Key 占位选择：

1. 若当前租户存在 active Virtual Key，返回 `has_active_virtual_key=true` 和安全 `key_prefix`。
2. 示例代码仍使用环境变量 `METAHUB_VIRTUAL_KEY`，不返回明文 Key。
3. 若不存在 active Virtual Key，返回 `has_active_virtual_key=false`，步骤引导到 `#virtual-keys`。

响应示例：

```json
{
  "tenant_id": "tenant_default",
  "gateway_base_url": "http://localhost:8000/v1",
  "model_alias": "default-chat",
  "virtual_key_placeholder": "METAHUB_VIRTUAL_KEY",
  "has_active_virtual_key": true,
  "virtual_key_prefix": "mhv_dev_abcd",
  "steps": [
    {
      "id": "copy-key",
      "tenant_id": "tenant_default",
      "order": 1,
      "title": "准备 Virtual Key",
      "description": "使用创建时复制的一次性明文 Key，或新建一把 dev Key。",
      "status": "ready",
      "action_label": "查看 Virtual Key",
      "navigation_item_id": "virtual-keys",
      "target_hash": "#virtual-keys"
    }
  ],
  "examples": [
    {
      "id": "python-openai",
      "tenant_id": "tenant_default",
      "language": "python",
      "title": "Python",
      "description": "使用 OpenAI Python SDK 发起一次非流式 chat 调用。",
      "install_command": "pip install openai",
      "code": "from openai import OpenAI\n...",
      "copy_label": "复制 Python",
      "base_url": "http://localhost:8000/v1",
      "model_alias": "default-chat",
      "uses_env_var": true
    }
  ],
  "common_errors": [
    {
      "code": "gateway.auth_invalid",
      "title": "Virtual Key 无效",
      "cause": "Key 被禁用、复制不完整或来自其他租户。",
      "fix": "重新创建 dev Key，并只把明文放入本地环境变量。",
      "link_target": "#virtual-keys"
    }
  ],
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

## 前端页面/交互设计

### 导航入口

- 在侧边栏“开发者”分组新增“快速开始”入口，目标 section 为 `quickstart`。
- Admin/Developer 可见，Viewer 不可见。
- 概览页下一步引导中，如果没有调用日志，优先引导到快速开始页。

### 页面布局

- 顶部摘要区展示：
  - Gateway base_url。
  - 推荐模型别名。
  - Virtual Key 状态：有 active Key 时显示脱敏前缀；无 Key 时显示创建引导。
- 步骤区展示：
  - 准备 Virtual Key。
  - 选择模型别名。
  - 安装 SDK 或准备 curl。
  - 复制示例并执行。
  - 使用 request_id 到调用日志排查。
- 示例区使用 tabs 或分段按钮切换 Python、JavaScript、curl；每个示例有复制按钮。
- 常见错误区展示错误码、原因、修复建议和跳转入口。

### 复制按钮

- 复制按钮只复制代码块或安装命令。
- 复制内容中不能包含真实 Virtual Key。
- Clipboard 不可用时显示本地提示，不把代码写入日志或事件 payload。

### 与调试台关系

- 快速开始页用于复制代码到本地执行。
- `debug-console` 仍是临时输入 Virtual Key 的在线调试入口。
- 两者共享 Gateway base_url 和模型别名推荐，但都不持久保存 Virtual Key 明文。

## 错误码设计

新增：

- `developer.quickstart_unavailable`
  - HTTP 503。
  - 用于 quickstart 依赖数据无法读取且不能降级生成示例时。
- `developer.quickstart_event_invalid`
  - HTTP 422。
  - 用于内部事件 payload 超出白名单；客户端通常不会直接看到。

复用：

- `auth.token_missing`
- `auth.token_invalid`
- `auth.permission_denied`
- `infra.validation_failed`
- `infra.unhandled`
- Gateway 常见错误码：`gateway.auth_missing`、`gateway.auth_invalid`、`gateway.model_missing`、`gateway.messages_missing`、`gateway.model_not_found`、`gateway.upstream_unavailable`、`gateway.upstream_error`

所有 API 响应继续由统一中间件携带 `request_id` 和 `trace_id`。

## 安全与权限约束

- `/developer/quickstart-data` 只允许 admin/developer 访问，viewer 后端拒绝。
- API 不接受前端传入的 `tenant_id`，所有查询都从认证上下文恢复当前租户。
- 示例代码不得包含真实 Virtual Key、Provider Key、Authorization、Prompt、Response 或 messages 历史内容。
- Virtual Key 只以 `METAHUB_VIRTUAL_KEY` 环境变量或 `<METAHUB_VIRTUAL_KEY>` 占位展示；如果后端发现 active Key，只返回 `key_prefix`。
- 事件 payload 只记录安全元数据，不记录示例代码全文、用户输入内容或密钥。
- 前端复制按钮不写入服务端事件 payload；最多在本地 UI 显示复制结果。
- Quickstart 的模型别名来自当前租户 `model_aliases`，不得跨租户读取模型或 Provider 信息。
- 失败请求最终应可通过 Gateway 调用日志或前端错误提示定位；快速开始页本身不吞掉统一错误码。

## 测试用例

### 主流程

- Admin 调用 `GET /developer/quickstart-data`：
  - 返回 200。
  - `tenant_id` 来自登录上下文。
  - `gateway_base_url` 来自 `METAHUB_GATEWAY_API_BASE_URL` 并带 `/v1`。
  - 返回 Python、JavaScript、curl 三类示例。
  - 示例包含推荐 `model_alias` 和 `METAHUB_VIRTUAL_KEY` 占位。
  - 写入 `developer.quickstart_viewed` 事件。
- Developer 调用同一接口：
  - 返回 200。
  - 若存在 active Virtual Key，返回 `has_active_virtual_key=true` 和 `virtual_key_prefix`。
  - 不返回明文 Key、`key_hash` 或 Provider Key。
- 前端渲染：
  - 展示步骤、base_url、模型别名、Key 状态、三个示例和常见错误。
  - 复制按钮的 `data-copy-code` 不包含真实 Key。
  - 通过 PRD-018 shell 的 `quickstart` active item 渲染。

### 异常/权限/安全

- Viewer 调用 `GET /developer/quickstart-data` 返回 `auth.permission_denied`。
- 无 active Virtual Key：
  - API 不报错，步骤状态为 `action_required`，引导到 `#virtual-keys`。
- 无 active ModelAlias：
  - API 不报错，使用 `default-chat` 占位，步骤状态为 `action_required`，引导到 `#model-aliases`。
- 前端或测试构造的响应中包含 HTML 特殊字符：
  - 页面必须正确转义，不能产生 XSS。
- 事件 payload 白名单：
  - 不允许写入 Token、Authorization、Provider Key、Virtual Key、Prompt、Response、messages、input。
- 代码示例可运行性：
  - Python 示例语法可由 `ast.parse` 通过。
  - JavaScript 示例可由 Node parser 或轻量语法检查通过。
  - curl 示例包含正确的 `POST /v1/chat/completions`、Authorization header 占位和 JSON body。

## 代码修改计划

1. 新增 `0012_developer_quickstart.sql/down.sql`。
2. 新增 `developer_portal` 后端模块：
   - `models.py`
   - `service.py`
   - `routes.py`
   - `__init__.py`
3. 在 `app.py` 注册 developer portal router。
4. 在 `auth/dependencies.py` 增加 `require_developer_portal_reader`，admin/developer 可读。
5. 在错误码表增加 `developer.quickstart_unavailable` 和 `developer.quickstart_event_invalid`。
6. 在控制台导航中新增 `quickstart` 入口。
7. 新增前端 API 客户端 `developerQuickstart.js`。
8. 新增前端渲染模块 `renderDeveloperQuickstart.js`。
9. 调整 `renderDeveloperStart.js` 和 `main.js`：
   - 加载 quickstart state。
   - active item 为 `quickstart` 时渲染快速开始页。
   - 绑定复制按钮。
10. 更新 CSS。
11. 补测试：
   - 后端 API/事件/权限集成测试。
   - 前端 quickstart 客户端、渲染和复制按钮单元测试。
   - 示例代码静态可运行性测试。
12. 更新 README、目录边界和测试文档。

## 实现结果

- 新增 `developer_portal_events` 租户级事件表，迁移版本推进到 `0012_developer_quickstart`。
- 新增 `developer_portal` 后端模块，提供 `GET /developer/quickstart-data`。
- 新增 `require_developer_portal_reader` 后端权限依赖，admin/developer 可读，viewer 后端拒绝。
- 快速开始响应生成 Gateway `/v1` base_url、推荐模型别名、Virtual Key 环境变量占位、脱敏 Key 前缀、步骤、Python/JavaScript/curl 示例和常见错误说明。
- 后端写入 `developer.quickstart_viewed` 事件，payload 仅包含模型别名、是否已有 active Key、示例 ID 等安全元数据。
- 控制台导航新增“快速开始”入口，并在统一 shell 的 active item 中渲染快速开始页。
- 前端新增快速开始 API client、渲染模块、复制按钮和响应式样式；复制内容从代码块读取，不把代码或 Key 占位塞进 data 属性。
- 所有新增 Python/JavaScript/SQL/CSS 文件和关键权限、租户、脱敏、事件分支均通过中文注释门禁。

## 测试结果

- 后端集成测试覆盖 quickstart 主流程、viewer 权限拒绝、缺 Key/缺别名降级引导、示例不泄露明文 Key、事件按租户写入和迁移/回滚。
- 前端 Node 测试覆盖 quickstart API client 不传 `tenant_id`、统一错误映射、页面转义、Key 占位和 shell active item 渲染。
- 注释门禁 `scripts/ci/check-comments.py` 已通过。
- 完整命令和结果记录见 `docs/testing/PRD-019-tests.md`。

## 自审 1：需求完整性

- 已覆盖 Python、JavaScript、curl 示例。
- 已覆盖自动填入 base_url、Virtual Key 占位和模型别名。
- 已覆盖常见错误与排查入口。
- 已覆盖 `GET /developer/quickstart-data`、`SDKExample`、`QuickstartStep` 和 `developer.quickstart_viewed`。
- 已明确 M0 不做完整文档站搜索。

结论：需求范围完整，未把文档站、搜索或复杂 SDK 能力提前塞入 M0。

## 自审 2：安全、可维护性与风险

- 最大风险是“自动填入 Virtual Key”被误实现为回显明文 Key；设计已限定只能返回占位符和脱敏前缀。
- 快速开始示例依赖模型别名和 Virtual Key 现状；缺失时不报错，而是给出 action_required 步骤，避免空租户无法打开页面。
- 后端统一生成示例，避免前端散落 base_url、model alias 和错误码逻辑。
- 事件单独落开发者门户事件表，避免污染 PRD-018 的 `console_page_events` 约束。
- 示例代码和错误说明需要测试覆盖，防止后续改动让“5 分钟接入”变成不可复制的展示文本。

结论：可以进入编码，但实现时必须把“密钥不回显”和“示例可复制运行”作为验收硬门槛。
