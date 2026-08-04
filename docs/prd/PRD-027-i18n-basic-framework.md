# PRD-027 - i18n基础框架

状态：已完成编码、测试和文档更新。

## 需求理解

PRD-027 属于 M0-W2/P0 的“中英文基础”能力，目标不是一次性把所有历史页面翻译完，而是建立后续可持续扩展的 i18n 框架：控制台默认中文，用户可以在“语言设置”里选择中文或英文；新增 UI 文案必须通过字典 key 获取；错误码继续保持内部稳定英文 `code`，展示层可以显示中文解释；日志和业务表不能把中文自由文本当作 code 存储。

Excel 约束：

- 用户角色：所有用户。
- 页面入口：语言设置。
- API/动作：`GET /user/preferences`、`PATCH /user/preferences`。
- 数据对象：`I18nMessage`、`UserPreference`。
- 事件：`user.locale_changed`。
- 验收：新增页面必须通过字典取文案；错误码可显示中文解释；内部日志不存中文自由文本作为 code；测试重点是文案缺失和 fallback 语言。

架构约束补充：

- 内部字段和错误码使用稳定英文 code，展示层再做中文/英文映射。
- `NormalizedError` 以 `error_code`、`provider_error_code`、`retryable`、`user_action` 等稳定字段为核心。
- 控制台优先中文，内部 code 保持英文稳定。
- 所有业务数据默认带 `tenant_id`；用户偏好、用户偏好事件都必须按认证上下文写入当前租户。

## 涉及模块

- `infra/migrations/`
  - 新增 `0019_i18n_user_preferences.sql` 与 down 迁移。
  - 在 `user_preferences` 增加 `locale` 字段。
  - 新增 `user_preference_events` 记录 `user.locale_changed`。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增用户偏好与 i18n 相关错误码。
  - 保持错误响应的 `code/message/suggestion` 英文稳定，不把中文展示文案写成内部 code。
- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
  - 为用户偏好读取/更新失败、locale 非法补充安全诊断。
- `shared-sdk/python/metahub_shared/runtime/i18n.py`
  - 新增后端 i18n 基础模型和错误码中文 label 查询函数，供 `/error-codes` 或后续控制面复用。
- `control-plane-api/src/metahub_control/user_preferences/`
  - 新增 DTO、服务和路由，实现 `GET/PATCH /user/preferences`。
  - 只按 `AuthContext.user.tenant_id/user_id` 读取和更新，不接受请求体传 `tenant_id`。
- `control-plane-api/src/metahub_control/console/`
  - 继续保留 `/console/preferences` 管理导航与侧边栏偏好。
  - 导航新增“语言设置”入口，所有角色可见。
- `console-web/src/i18n/`
  - 新增 `messages.js`、`createI18n()`、`t()`、`errorLabel()`、fallback 检查工具。
  - 默认 `zh-CN`，支持 `en-US`。
- `console-web/src/lib/userPreferences.js`
  - 新增用户偏好 API client。
- `console-web/src/features/language-settings/renderLanguageSettingsPanel.js`
  - 新增语言设置页面，页面所有新增文案必须通过 i18n key 渲染。
- `console-web/src/features/console-shell/renderConsoleShell.js`
  - 将新加的语言设置入口、用户菜单中 locale 展示和通用错误 label 接入 i18n。
- `console-web/src/app/main.js`
  - 登录后加载用户偏好，创建 i18n 上下文，并把 `locale` 传给主渲染树。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-027-tests.md`
  - 记录 i18n 边界、API、迁移和测试结果。

## 数据表/字段设计

### 扩展 `user_preferences`

当前 `user_preferences` 已保存控制台 UI 偏好：`tenant_id`、`user_id`、`active_navigation_item_id`、`sidebar_collapsed`。PRD-027 复用该表，不新增另一张用户偏好主表，避免同一用户偏好分散。

新增字段：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `locale` | TEXT | NOT NULL DEFAULT `'zh-CN'`, CHECK IN `('zh-CN','en-US')` | 当前用户控制台语言偏好，M0 只支持中文和英文。 |

字段语义：

- `locale` 只表示当前租户内当前用户的控制台展示语言。
- 默认 `zh-CN`，满足面向中国境内企业的默认中文要求。
- 不保存浏览器 `Accept-Language` 原始值，避免记录不必要的客户端环境指纹。
- 不影响 `request_logs.error_code`、结构化日志 `error_code` 或 Webhook 事件 `event_type`，这些仍保持英文稳定 code。

### 新增 `user_preference_events`

用于记录 `user.locale_changed`，满足事件追踪和后续 Webhook 触发点复用。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `event_id` | TEXT | PRIMARY KEY | 事件 ID，前缀建议 `upe_`。 |
| `tenant_id` | TEXT | NOT NULL, FK tenants | 当前租户，来自认证上下文。 |
| `user_id` | TEXT | NOT NULL, FK users | 当前用户，来自认证上下文。 |
| `event_type` | TEXT | NOT NULL CHECK `event_type='user.locale_changed'` | 稳定英文事件 code。 |
| `old_locale` | TEXT | NULL | 变更前 locale。 |
| `new_locale` | TEXT | NOT NULL | 变更后 locale。 |
| `request_id` | TEXT | NOT NULL | 统一请求 ID。 |
| `trace_id` | TEXT | NULL | 统一 trace ID。 |
| `payload_json` | TEXT | NOT NULL DEFAULT `'{}'` | 安全 payload，仅保存 locale 元数据，不保存 Token、Prompt 或密钥。 |
| `created_at` | TEXT | NOT NULL | UTC ISO 时间。 |

索引：

- `idx_user_preference_events_tenant_time(tenant_id, created_at DESC)`
- `idx_user_preference_events_user_time(tenant_id, user_id, created_at DESC)`

### 不新增 `i18n_messages` 持久表

`I18nMessage` 在 M0 是代码内静态字典对象，不作为业务表。原因：

- M0 不做租户自定义翻译、文案审批、在线热更新或翻译运营台。
- 静态字典更适合 CI 检查缺失 key 和 fallback 语言。
- 若后续 M2/M3 需要租户级文案覆盖，再新增带 `tenant_id` 的 `i18n_messages` 表。

`I18nMessage` 最小结构：

```text
key: string
locale: 'zh-CN' | 'en-US'
text: string
namespace: 'common' | 'navigation' | 'errors' | 'language_settings' | ...
```

## API设计

### `GET /user/preferences`

用途：读取当前用户的控制台语言偏好和可选语言列表。

认证：`require_current_user`。

查询参数：无；不接受 `tenant_id`、`user_id` 或 `role`。

响应：

```json
{
  "preference": {
    "preference_id": "upr_xxx",
    "tenant_id": "tenant_default",
    "user_id": "user_admin",
    "locale": "zh-CN",
    "available_locales": [
      { "locale": "zh-CN", "label": "简体中文", "native_label": "简体中文", "default": true },
      { "locale": "en-US", "label": "English", "native_label": "English", "default": false }
    ],
    "created_at": "2026-07-10T10:00:00Z",
    "updated_at": "2026-07-10T10:00:00Z"
  },
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

行为：

- 若偏好行不存在，后端创建默认行：`active_navigation_item_id='overview'`、`sidebar_collapsed=0`、`locale='zh-CN'`。
- 创建和读取都按 `tenant_id + user_id` 查询。
- 响应可以包含 `tenant_id/user_id` 供审计和测试断言，但请求不允许传这些字段。

### `PATCH /user/preferences`

用途：更新当前用户语言偏好。

认证：`require_current_user`。

请求：

```json
{
  "locale": "en-US"
}
```

响应：同 `GET /user/preferences`。

行为：

- `locale` 只允许 `zh-CN`、`en-US`。
- locale 未变化时返回当前偏好，不写 `user.locale_changed` 事件，避免重复事件噪音。
- locale 变化时更新 `user_preferences.locale/updated_at`，并写入 `user_preference_events`。
- 不接受额外字段，避免前端把 `tenant_id`、`user_id`、Token 或业务上下文夹带进偏好接口。

### 可选增强：`GET /error-codes?locale=zh-CN`

M0 可在不破坏现有字段的前提下给错误码目录补充展示字段：

- `label`: 当前 locale 下的短中文/英文解释。
- `localized_suggestion`: 当前 locale 下的展示建议。

原有 `code`、`message`、`suggestion` 保持英文稳定，日志继续只记录 `error_code`、`diagnosis_rule_id` 等 code 字段。若实现风险偏高，本 PRD 可先在前端 `errorLabel(code, locale)` 完成展示层映射，后续再扩展 `/error-codes`。

## 前端页面/交互设计

页面：`语言设置`，新增导航项 `language-settings`，所有角色可见。

布局：

- 顶部说明：使用 i18n key 渲染标题、说明和当前语言。
- 语言选择：两个 radio 或 segmented control，选项来自 API `available_locales`。
- 保存按钮：使用 icon + 文案，提交 `PATCH /user/preferences`。
- 保存成功：更新页面内 locale，并重新渲染 shell 与当前页面。
- 保存失败：显示统一错误结构，错误 label 通过 `errorLabel(error.code, locale)` 获取。

交互约束：

- 默认语言为 `zh-CN`，即使用户偏好 API 失败也要 fallback 到中文。
- `t(locale, key)` 查不到当前 locale 时 fallback 到 `zh-CN`；`zh-CN` 也缺失时显示 key，并在测试中可检查缺失。
- 新增语言设置页面和新增共用 shell 文案必须通过 i18n key 渲染。
- 现有历史页面暂不要求一次性全部翻译；后续 PRD 修改到哪个页面，就必须把新增文案接入 key。
- 前端本地 session 可以缓存 `locale`，但最终仍以后端 `GET /user/preferences` 为准。

建议字典结构：

```js
const messages = {
  "zh-CN": {
    "nav.language_settings": "语言设置",
    "language.title": "语言设置",
    "language.current": "当前语言",
    "language.save": "保存语言",
    "error.auth.token_missing.label": "需要重新登录"
  },
  "en-US": {
    "nav.language_settings": "Language",
    "language.title": "Language",
    "language.current": "Current language",
    "language.save": "Save language",
    "error.auth.token_missing.label": "Sign in required"
  }
};
```

## 错误码设计

- `user_preference.locale_invalid`
  - HTTP 422
  - 场景：`PATCH /user/preferences` 传入不支持的 locale。
  - suggestion：使用 `zh-CN` 或 `en-US`。
- `user_preference.payload_invalid`
  - HTTP 422
  - 场景：请求体没有可更新字段或包含额外字段。
  - suggestion：只提交 `{ "locale": "zh-CN" | "en-US" }`。
- `user_preference.unavailable`
  - HTTP 503
  - 场景：数据库不可用、偏好表缺失、事件写入失败。
  - suggestion：检查数据库连接和 0019 迁移。
- `i18n.locale_unsupported`
  - HTTP 422
  - 场景：后续公共 API 接收 locale 参数时不支持；M0 可作为共享错误码预留。

错误响应仍使用统一结构：

- `code` 为英文稳定 code。
- `message/suggestion` 保持英文机器契约。
- 前端通过 i18n 字典显示中文 label，不把中文 label 当作 code 写入日志或数据库。

## 安全与权限约束

- `/user/preferences` 必须登录，租户和用户只来自 `AuthContext`。
- 请求体禁止 `tenant_id`、`user_id`、`role`、`token`、`authorization`、`provider_key`、`virtual_key` 等字段。
- `user_preferences` 与 `user_preference_events` 均带 `tenant_id`，查询条件必须包含当前 `tenant_id`。
- `locale` 不是权限字段，不能被用来判断业务 API 权限。
- 结构化日志只记录 `event_type='user.locale_changed'`、`old_locale`、`new_locale`、`request_id`，不记录本地化展示文本。
- 错误码、事件类型、Webhook event_type、日志 `event` 字段继续使用稳定英文 code。
- 前端所有 i18n 文案输出仍必须 HTML 转义，不能因为字典是本地文件就绕过转义。

## 测试用例

后端集成测试：

1. 主流程：管理员登录后 `GET /user/preferences` 返回默认 `zh-CN`、可选语言和 `request_id/trace_id`。
2. 主流程：`PATCH /user/preferences` 更新为 `en-US`，数据库 `user_preferences.locale` 更新，写入 `user.locale_changed` 事件。
3. 幂等：重复 PATCH 同一 locale 不重复写事件。
4. 权限/异常：未登录返回 `auth.token_missing`；非法 locale 返回 `user_preference.locale_invalid`；额外字段返回统一 422。
5. 租户隔离：另一个租户的偏好和事件不会被当前用户读取或更新。
6. 迁移：0019 迁移新增 `locale` 字段和事件表，down 迁移回滚事件表并重建旧 `user_preferences`。

前端单元测试：

1. `t("en-US", key)` 命中英文；缺失英文时 fallback 到中文；中文也缺失时返回 key。
2. `errorLabel("auth.token_missing", "zh-CN")` 返回中文解释；未知错误码 fallback 到 code。
3. 用户偏好 client 不发送 `tenant_id`，PATCH 只发送 locale。
4. 语言设置页渲染所有选项、当前 locale、保存按钮和统一错误，HTML 转义安全。
5. renderDeveloperStart 或 console shell 在 active item 为 `language-settings` 时渲染语言设置页。

质量门禁：

- 新增 JS/Python/SQL 文件必须通过中文注释门禁。
- 新增语言设置页面不得出现未通过 `t()` 的新增 UI 文案；可通过聚焦单测或轻量脚本扫描 `renderLanguageSettingsPanel.js`。
- 错误码和事件类型测试必须断言存储值是英文 code。

## 代码修改计划

1. 新增迁移 `0019_i18n_user_preferences.sql/.down.sql`，扩展 `user_preferences.locale` 并创建 `user_preference_events`。
2. 在 shared runtime 增加 i18n 基础模块、支持 locale 列表、错误 label 字典和用户偏好错误码。
3. 新增 `metahub_control/user_preferences/models.py`、`service.py`、`routes.py`，注册 `GET/PATCH /user/preferences`。
4. 扩展 app 注册 user preferences router。
5. 更新 console 导航定义，新增 `language-settings`，所有角色可见。
6. 新增 `console-web/src/i18n/messages.js` 和 i18n helper，提供 `t()`、`errorLabel()`、fallback 和缺失 key 收集。
7. 新增 `console-web/src/lib/userPreferences.js`。
8. 新增 `renderLanguageSettingsPanel.js`，并在 `renderDeveloperStart.js` active content 中接入。
9. 更新 `main.js`：登录后读取用户偏好，创建 i18n 上下文，PATCH 成功后重新渲染。
10. 补后端集成测试、迁移测试、前端单元测试、注释门禁和 lint。
11. 更新 README、目录文档和 `docs/testing/PRD-027-tests.md`。

## 两轮设计自审结论

第一轮自审：不新增 `i18n_messages` 表。M0 的翻译内容属于静态产品文案，不是租户业务数据；用代码字典更利于 CI 检查缺失 key，也避免引入文案管理后台、缓存和热更新复杂度。

第二轮自审：`/user/preferences` 与现有 `/console/preferences` 分工明确。前者承载用户级语言偏好，面向所有用户；后者继续承载导航 active item 和侧边栏折叠状态。二者复用同一 `user_preferences` 表，但 API DTO 分开，避免语言设置页面被控制台导航细节耦合。

## 编码后自审结论

第三轮自审：实现阶段保留了设计中的静态 i18n 字典方案，没有引入 `i18n_messages` 持久表。`GET /error-codes?locale=` 仅新增展示层 `label`，错误响应和日志继续使用稳定英文 `code/message/suggestion`。

第四轮自审：`/user/preferences` 已与 `/console/preferences` 解耦。语言偏好接口只接受 `locale`，拒绝夹带 `tenant_id/user_id`；控制台导航偏好仍由 `/console/preferences` 管理，二者复用同一底层表但 DTO 与测试边界分开。

## 已执行验证

```powershell
python scripts\ci\check-comments.py
python -m compileall shared-sdk\python\metahub_shared\runtime control-plane-api\src\metahub_control\user_preferences control-plane-api\tests\integration\test_user_preferences.py
python -m pytest control-plane-api\tests\integration\test_user_preferences.py control-plane-api\tests\integration\test_db_migrations.py gateway-data-plane\tests\unit\test_error_codes.py -q --basetemp D:\MetaHub\.tmp\pytest-prd027-<guid> -p no:cacheprovider
node --test console-web\tests\unit\userPreferences.test.mjs console-web\tests\unit\consoleShell.test.mjs console-web\tests\unit\dashboard.test.mjs
node scripts\lint.mjs
```

结果：上述 focused 后端测试、前端测试、控制台 lint、Python 编译和中文注释门禁均已通过。
