# PRD-027 测试记录 - i18n基础框架

## 覆盖范围

- 控制面用户偏好 API：`GET /user/preferences`、`PATCH /user/preferences`。
- 语言偏好数据：`user_preferences.locale` 默认中文、切换英文、重复切换幂等。
- 语言变更事件：`user_preference_events.event_type='user.locale_changed'`，只记录 old/new locale、`request_id`、`trace_id` 和安全 payload。
- 权限与租户边界：接口必须登录；租户和用户只来自 `AuthContext`；请求体夹带 `tenant_id` 会被拒绝。
- 错误码展示：`GET /error-codes?locale=` 返回本地化 `label`，但 `code/message/suggestion` 保持英文稳定契约。
- 控制台前端：i18n fallback、语言设置页渲染、`/user/preferences` client、shell 中 `language-settings` active item、HTML 转义和旧导航 fallback。
- 迁移：`0019_i18n_user_preferences.sql` 新增 `locale` 和事件表；down 迁移移除事件表并重建旧版 `user_preferences`。
- 注释：新增 Python、JS、SQL、CSS 文件继续通过中文注释门禁。

## 已执行命令

```powershell
python -m compileall shared-sdk\python\metahub_shared\runtime control-plane-api\src\metahub_control\user_preferences control-plane-api\tests\integration\test_user_preferences.py
python scripts\ci\check-comments.py
python -m pytest control-plane-api\tests\integration\test_user_preferences.py control-plane-api\tests\integration\test_db_migrations.py gateway-data-plane\tests\unit\test_error_codes.py -q --basetemp D:\MetaHub\.tmp\pytest-prd027-<guid> -p no:cacheprovider
node --test console-web\tests\unit\userPreferences.test.mjs console-web\tests\unit\consoleShell.test.mjs console-web\tests\unit\dashboard.test.mjs
node scripts\lint.mjs
```

## 当前结果

- 中文注释门禁：通过。
- Python 编译：通过。
- 后端 focused 测试：通过，覆盖默认语言、切换语言、事件写入、非法 locale、非法 payload、未登录、错误码本地化 label 和 0019 回滚。
- 前端 focused 测试：通过，覆盖 i18n fallback、用户偏好 API client、语言设置页渲染、shell 英文展示和旧 Dashboard/Console Shell 回归。
- 控制台 lint：通过。

## 风险记录

- M0 不做历史页面全文翻译；后续 PRD 修改到某个页面时，新增文案必须接入 i18n key。
- M0 不新增 `i18n_messages` 持久表；租户级文案覆盖、在线热更新和翻译审批放到后续阶段。
- 语言偏好不是权限字段；任何业务权限仍必须由后端 RBAC 依赖校验。
