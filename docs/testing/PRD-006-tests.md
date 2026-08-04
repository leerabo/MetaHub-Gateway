# PRD-006 测试说明

## 覆盖范围

主流程：

- 迁移 `0002_admin_sessions` 创建 `sessions` 表并扩展 `users` 登录安全字段。
- seed 管理员可通过 `POST /auth/login` 登录。
- `GET /auth/me` 携带 Bearer Token 返回用户、租户和 Session。
- `POST /auth/logout` 撤销 Session。
- 控制台登录页提交成功后进入控制台并展示个人菜单。

异常/权限/安全：

- 错误密码返回 `auth.login_invalid` 并累计失败次数。
- 连续失败达到阈值后返回 `auth.login_locked`。
- 缺少 Token 返回 `auth.token_missing`。
- 伪造 Token 返回 `auth.token_invalid`。
- 过期 Token 返回 `auth.token_expired`。
- 已登出 Token 返回 `auth.session_revoked`。
- 非 admin 角色访问后端管理员依赖返回 `auth.permission_denied`。
- 明文密码、Bearer Token、`password_hash`、`token_hash` 不出现在 API 响应或明文落库字段中。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

当前验证结果：

- 后端 pytest：`30 passed`
- 控制台 Node test：`12 passed`
- 控制台 lint：通过
- 真实 HTTP：登录、`/auth/me`、Token 过期、登出、Session 撤销、缺失 Token 均按预期返回。
- 浏览器：Chrome 打开控制台后可登录、查看个人菜单、登出回登录页，页面无 error。
