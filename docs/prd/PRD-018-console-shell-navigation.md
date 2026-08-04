# PRD-018 - 控制台骨架与导航

状态：已完成实现、自动化测试和文档更新。

## 需求理解

PRD-018 属于 M0-W2/P0 的控制台基础能力。目标不是新增业务管理功能，而是把已经完成的 Provider、模型、Virtual Key、调用日志、用量、API 文档等入口收束进一个可持续扩展的控制台主框架，让不同角色登录后能快速找到自己可用的治理入口。

本 PRD 必须满足：
- 登录后进入统一控制台主框架，包含侧边栏、顶部租户/用户信息、主内容区和空状态引导。
- Admin 能看到配置入口，包括租户、用户、Provider/KeyPool、模型目录、模型别名等。
- Developer 能看到 Key、调用日志、用量、API 文档和调试入口；模型/别名保持只读视角。
- Viewer 不展示受限管理入口；但这只是体验优化，后端业务 API 仍必须继续做 RBAC。
- 导航入口覆盖当前 M0 核心功能，并为后续 M0.5/M1 模块保留稳定注册方式。
- 记录最小 `console.page_viewed` 事件，便于后续审计和产品使用分析。

边界：
- M0 不做复杂角色首页、个性化工作台、移动端响应式深度适配、菜单权限 DSL、前端权限策略编辑器。
- 不引入前端框架迁移；继续沿用当前原生 ESM + Node test 的轻量控制台。
- 不把 OpenRouter/硅基流动作为默认上游配置入口，只保留架构边界说明。

架构约束摘录：
- 控制面提供低频管理能力，不承载高频模型流量。
- 控制台服务企业管理员和开发者，核心对象必须按租户隔离。
- 后端权限校验不能被前端隐藏按钮替代。
- Trace、Usage、Budget、Webhook 等查询数据均按租户隔离。
- 控制台、错误提示、SDK 文档需要面向企业用户保持清晰的开发者体验闭环。

## 涉及模块

- `control-plane-api/src/metahub_control/console/`
  - 新增控制台导航、用户偏好和页面访问事件 API。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 复用现有登录态校验；本 PRD 不新增复杂角色体系。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增控制台参数错误码。
- `infra/migrations`
  - 新增 `user_preferences` 和 `console_page_events` 的最小迁移。
- `console-web/src/lib/consoleShell.js`
  - 新增控制台导航、偏好和页面访问事件客户端。
- `console-web/src/features/console-shell/`
  - 新增主框架渲染、导航模型渲染、空状态引导和入口注册。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 从单页堆叠改为被主框架调度的内容区渲染，复用已有业务面板。
- `console-web/src/app/main.js`
  - 接入导航状态、hash 路由、偏好保存和 `console.page_viewed` 事件上报。
- `console-web/src/styles/main.css`
  - 增加侧边栏、内容区、空状态、活跃导航、角色徽标样式。
- `tests`
  - 补后端集成测试和前端 Node 单元测试。

## 数据表/字段设计

### `user_preferences`

用于保存当前租户内用户的最小控制台偏好。所有记录带 `tenant_id`，并绑定 `user_id`。

- `preference_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `user_id TEXT NOT NULL`
- `active_navigation_item_id TEXT NOT NULL DEFAULT 'overview'`
- `sidebar_collapsed INTEGER NOT NULL DEFAULT 0 CHECK (sidebar_collapsed IN (0, 1))`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `UNIQUE(tenant_id, user_id)`

索引：
- `idx_user_preferences_tenant_user(tenant_id, user_id)`

说明：
- `active_navigation_item_id` 必须是后端允许该角色访问的导航项，否则回退到 `overview`。
- M0 不存储任意 JSON 偏好，避免前端把敏感上下文或大对象塞进数据库。

### `console_page_events`

用于记录最小页面访问事件，后续 Webhook/审计模块可按需消费。

- `event_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `user_id TEXT NOT NULL`
- `session_id TEXT`
- `event_type TEXT NOT NULL DEFAULT 'console.page_viewed'`
- `navigation_item_id TEXT NOT NULL`
- `target_section TEXT NOT NULL`
- `path TEXT NOT NULL`
- `request_id TEXT NOT NULL`
- `trace_id TEXT`
- `payload_json TEXT NOT NULL DEFAULT '{}'`
- `created_at TEXT NOT NULL`

索引：
- `idx_console_page_events_tenant_time(tenant_id, created_at DESC)`
- `idx_console_page_events_user_time(tenant_id, user_id, created_at DESC)`
- `idx_console_page_events_item_time(tenant_id, navigation_item_id, created_at DESC)`

说明：
- `payload_json` 只允许安全白名单字段，例如 `source`、`hash`、`viewport_class`；不得包含 Token、Provider Key、Virtual Key 明文、Prompt、Response。
- 本 PRD 不提供事件列表查询页面，只写入可追踪事件。

### `NavigationItem`

`NavigationItem` 是后端生成的 DTO，不落表。字段：

- `id`
- `label`
- `group`
- `target_section`
- `required_roles`
- `visible`
- `disabled`
- `description`
- `empty_state_hint`
- `order`

## API 设计

### `GET /console/navigation`

用途：返回当前登录用户可见的导航项、默认选中项和空状态引导。

权限：已登录用户均可调用。后端按 `context.user.role` 计算可见项；不接收 `tenant_id`。

响应示例：

```json
{
  "navigation_items": [
    {
      "id": "providers",
      "label": "Provider 与 KeyPool",
      "group": "配置",
      "target_section": "providers",
      "required_roles": ["admin"],
      "visible": true,
      "disabled": false,
      "description": "注册上游 Endpoint 并管理 Provider KeyPool。",
      "empty_state_hint": "先注册一个模型厂商或客户本地 OpenAI-compatible Endpoint。",
      "order": 30
    }
  ],
  "active_navigation_item_id": "overview",
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

### `GET /console/preferences`

用途：读取当前登录用户的控制台偏好。

权限：已登录用户均可调用；只读取当前 `tenant_id + user_id`。

响应字段：
- `preference`
- `request_id`
- `trace_id`

### `PATCH /console/preferences`

用途：保存当前用户的控制台偏好。

权限：已登录用户均可调用；只写当前 `tenant_id + user_id`。

请求：

```json
{
  "active_navigation_item_id": "usage",
  "sidebar_collapsed": false
}
```

约束：
- `active_navigation_item_id` 必须存在且对当前角色可见。
- 不接收 `tenant_id`、`user_id`、任意 JSON 扩展字段。

### `POST /console/events`

用途：写入最小控制台页面访问事件。

权限：已登录用户均可调用；后端只使用认证上下文写入 `tenant_id/user_id/session_id`。

请求：

```json
{
  "event_type": "console.page_viewed",
  "navigation_item_id": "request-logs",
  "target_section": "request-logs",
  "path": "/#request-logs",
  "payload": {
    "source": "sidebar"
  }
}
```

响应：

```json
{
  "accepted": true,
  "event_id": "evt_x",
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

## 前端页面/交互设计

### 主框架

- 顶部栏：品牌、当前租户、登录用户、角色、Session 到期时间、退出按钮。
- 左侧栏：按组展示导航项，包含“概览”“配置”“调用与成本”“开发者”“系统”。
- 主内容区：只渲染当前 active item 对应面板，避免当前页面继续把所有面板堆在一个长页面里。
- 快捷状态：概览页展示本地服务状态、MVP 边界、下一步清单。

### 角色视图

- Admin：
  - 概览、租户、用户、Provider/KeyPool、模型目录、模型别名、Virtual Key、用量、调用日志、API 文档、调试台、系统状态、环境信息。
- Developer：
  - 概览、模型目录只读、模型别名只读、Virtual Key、用量、调用日志、API 文档、调试台、系统状态。
- Viewer：
  - 概览、API 文档、系统状态；受限入口不渲染。

说明：前端角色视图只是 UX 入口控制。所有已存在业务 API 继续使用后端依赖校验，例如 Provider 仍只允许 admin，Virtual Key/Usage/Request Log 仍由各自 API 控制权限。

### 空状态引导

- Provider 为空：提示先注册 Endpoint，再添加 KeyPool。
- 模型目录为空：提示先绑定 Provider，再创建内部模型 ID。
- Virtual Key 为空：提示先创建 dev Key，再使用 API 文档调试。
- 用量/日志为空：提示先通过调试台或业务系统发起一次请求。
- 权限不足：不展示受限按钮；若直接访问 hash，则回退到概览并展示“当前角色不可访问该入口”。

### 路由与偏好

- 使用 `location.hash` 表示当前导航项，例如 `/#usage`。
- 首次进入优先级：合法 hash > 后端偏好 > `overview`。
- 切换导航时保存偏好，并发送 `console.page_viewed`。
- 后端不可用时，控制台仍可显示本地降级概览和登录失效提示。

## 错误码设计

新增：

- `console.navigation_invalid`
  - HTTP 422。
  - 用于不存在、禁用或当前角色不可见的导航项。
- `console.preference_invalid`
  - HTTP 422。
  - 用于偏好字段非法、类型错误或包含未允许字段。
- `console.event_invalid`
  - HTTP 422。
  - 用于事件类型非法、导航项非法或 payload 超出白名单。

复用：

- `auth.token_missing`
- `auth.token_invalid`
- `auth.permission_denied`
- `infra.unhandled`

所有响应继续由共享 FastAPI 中间件携带 `request_id` 和 `trace_id`。

## 安全与权限约束

- `/console/*` 不接收客户端传入的 `tenant_id` 或 `user_id`。
- `user_preferences` 和 `console_page_events` 必须带 `tenant_id`。
- 后端生成导航项时只使用认证上下文中的角色，不信任前端传入的角色。
- 前端隐藏导航项不能替代业务 API 的后端 RBAC；PRD-018 不降低任何既有权限依赖。
- `console.page_viewed` payload 只保留安全白名单字段，不记录 Token、Provider Key、Virtual Key 明文、Prompt、Response、Authorization。
- 页面和测试继续验证 HTML 转义，避免租户名、用户 display_name、错误消息导致 XSS。
- UserPreference 只保存 UI 偏好，不保存业务查询条件中的 Key、Prompt、Response 或明文密钥。

## 测试用例

### 主流程

- Admin 调用 `/console/navigation`：
  - 可见 Provider、用户管理、模型、Key、用量、日志、API 文档和系统入口。
  - 默认 active item 为 `overview` 或已保存合法偏好。
- Developer 调用 `/console/navigation`：
  - 可见 Virtual Key、用量、日志、API 文档、模型只读入口。
  - 不可见 Provider 创建、用户管理等 admin 配置入口。
- 控制台渲染：
  - 侧边栏显示当前角色可见入口。
  - 顶部栏显示租户、用户、角色、Session 信息。
  - 切换导航只渲染当前内容区。
- 空状态：
  - Provider/Key/日志/用量为空时展示下一步引导。
- 偏好：
  - 切换到 `usage` 后保存偏好；重新加载时默认选中 `usage`。
- 事件：
  - 切换页面写入 `console.page_viewed`，记录 tenant/user/session/request_id/trace_id。

### 异常/权限

- Viewer 访问：
  - 后端导航不返回受限管理入口。
  - 前端不渲染受限入口。
- 非法导航项：
  - `PATCH /console/preferences` 返回 `console.navigation_invalid`。
- 非法事件：
  - `POST /console/events` 传入非 `console.page_viewed` 返回 `console.event_invalid`。
- 跨租户防护：
  - 请求体包含 `tenant_id` 时被忽略或拒绝，不能写入其他租户偏好。
- 敏感信息：
  - 事件 payload 中的 `token`、`authorization`、`provider_key`、`prompt` 等字段被拒绝。
- 业务 API 权限：
  - 即使前端没有入口，直接调用 Provider/User 等 API 仍由现有后端测试验证权限拒绝。

## 代码修改计划

1. 新增 `0011_console_shell_navigation.sql/down.sql`。
2. 新增控制面 `console` 模块：
   - `models.py`
   - `service.py`
   - `routes.py`
   - `__init__.py`
   - app 注册路由。
3. 新增错误码：
   - `console.navigation_invalid`
   - `console.preference_invalid`
   - `console.event_invalid`
4. 新增前端客户端 `consoleShell.js`。
5. 新增前端主框架组件：
   - `renderConsoleShell`
   - `renderSidebarNavigation`
   - `renderOverviewPanel`
   - `renderEmptyGuidance`
6. 调整 `main.js`：
   - 加载导航和偏好。
   - 解析 hash。
   - 切换导航时保存偏好并上报事件。
7. 调整 `renderDeveloperStart.js`：
   - 由单页堆叠改为按 active item 渲染内容区。
   - 保留所有现有业务面板，减少重复实现。
8. 更新 CSS，新增主框架、侧边栏和空状态样式。
9. 补测试：
   - 后端迁移测试。
   - 后端 Console API 集成测试。
   - 前端导航/权限/空状态/事件客户端单元测试。
10. 更新 README、目录边界和测试文档。

## 实现结果

- 新增 `user_preferences` 和 `console_page_events` 两张租户级业务表，迁移版本推进到 `0011_console_shell_navigation`。
- 新增控制面 `console` 后端模块，提供 `/console/navigation`、`/console/preferences` 和 `/console/events`，所有写入均从认证上下文恢复 `tenant_id/user_id/session_id`。
- 后端按角色生成 `NavigationItem`，Admin 可见配置入口，Developer 可见 Key/日志/用量/API 文档/调试台，Viewer 只保留概览、API 文档和系统状态。
- 新增 `console.navigation_invalid`、`console.preference_invalid`、`console.event_invalid` 统一错误码，响应继续携带 `request_id` 和 `trace_id`。
- 前端新增控制台主框架与侧边栏导航，登录后按后端返回的导航项渲染内容区，hash 和用户偏好只作为体验状态，不能绕过后端 RBAC。
- `console.page_viewed` 事件只允许白名单 payload，明确拒绝 Token、Authorization、Provider Key、Virtual Key、Prompt、Response 等敏感字段。
- 所有新增 Python/JavaScript/SQL/CSS 文件和关键权限、租户、脱敏、事件分支均通过中文注释门禁。

## 测试结果

- 后端定向集成测试覆盖 Admin/Developer/Viewer 导航、偏好恢复、非法导航、非法事件、敏感 payload 拒绝和迁移/回滚。
- 前端 Node 测试覆盖 Console Shell 客户端请求、错误映射、导航渲染、HTML 转义和 active item 内容渲染。
- 注释门禁 `scripts/ci/check-comments.py` 已通过，确保新增代码满足中文注释要求。
- 完整命令和结果记录见 `docs/testing/PRD-018-tests.md`。

## 自审 1：需求完整性

- 已覆盖 Excel 中的基础布局、侧边栏、顶部租户/用户信息、权限控制、空状态引导和 M0 核心入口。
- 已覆盖 Admin 与 Developer 的验收差异。
- 已覆盖 `NavigationItem`、`UserPreference` 和 `console.page_viewed`。
- 未把复杂角色首页、移动端、菜单 DSL 或 M1/M2 工作台提前塞进 M0。

结论：需求范围完整，且未越界。

## 自审 2：安全、可维护性与风险

- 后端仍是权限最终判定点；PRD-018 只改善导航体验，不降低业务 API RBAC。
- 所有持久化数据都带 `tenant_id`，查询和写入都来自认证上下文。
- 页面访问事件采用白名单 payload，避免把密钥、Prompt、Response 或 Authorization 写入数据库。
- 导航项由后端统一生成，前端只消费，不在多个组件里散落角色判断。
- 当前控制台已有大量面板，本 PRD 的主要风险是重构入口时破坏现有事件绑定；测试需要覆盖现有 Provider/Key/日志/用量入口仍可渲染和操作。

结论：可以进入编码，但必须先以现有面板复用为主，避免大规模视觉重写。
