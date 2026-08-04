# PRD-007 测试说明

## 覆盖范围

主流程：

- seed 默认租户 `default` 存在，状态为 `active`。
- 登录管理员可通过 `GET /tenant/current` 读取当前租户。
- 管理员可通过 `PATCH /tenant/current` 更新租户名称和 `service_mode`。
- 控制台登录后展示租户信息面板，并可提交保存。

异常/权限/安全：

- 未登录访问当前租户返回 `auth.token_missing`。
- 非 admin 角色更新当前租户返回 `auth.permission_denied`。
- PATCH 空请求返回 `tenant.update_empty`。
- 请求体携带 `tenant_id` 的跨租户写入尝试被拒绝。
- 数据库存在第二租户时，默认管理员只能读写自己的 `tenant_id`。
- MVP 业务表均包含 `tenant_id`；`schema_migrations` 为基础设施表例外。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

## 当前验证结果

- 干净测试环境下运行 `.\scripts\ci\check.ps1` 通过：后端 pytest `34 passed`，控制台 Node test `16 pass`，console lint `ok`。
- 后端专项测试覆盖 `GET /tenant/current`、`PATCH /tenant/current`、未登录、非 admin、空 PATCH、跨租户请求体拒绝、MVP 表 `tenant_id` 审计。
- 控制台专项测试覆盖租户 API client、Bearer Token、PATCH 不携带 `tenant_id`、结构化错误和租户信息面板渲染。
- 真实 HTTP 验证覆盖登录、读取当前租户、更新当前租户、跨租户请求体拒绝、未登录统一错误码，并在结束后恢复默认租户名。
- 真实浏览器验证覆盖登录后租户信息面板展示、保存租户名称、页面重新渲染读回 `MetaHub Browser Tenant`、控制台无 error/warning。
