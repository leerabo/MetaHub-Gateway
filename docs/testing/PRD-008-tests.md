# PRD-008 测试说明

## 覆盖范围

主流程：

- seed 默认租户 `default` 后写入 active `admin/developer/viewer` 三种固定角色。
- admin 可通过 `GET /users` 读取当前租户用户列表和角色选项。
- admin 可通过 `POST /users` 创建当前租户用户，初始密码只保存 PBKDF2 哈希。
- 被创建用户可登录，`GET /auth/me` 返回其当前角色。
- admin 可通过 `PATCH /users/{id}/role` 修改当前租户用户角色。
- 控制台 admin 登录后展示用户管理面板、创建用户表单和角色选择。

异常/权限/安全：

- 非 admin 调用 `/users` 返回 `auth.permission_denied`。
- Developer 新增 Provider 被后端 `require_current_admin` 拦截。
- Viewer 创建 Virtual Key 被后端 `require_virtual_key_writer` 拦截。
- 非法角色返回 `user.role_invalid`。
- 当前租户重复邮箱返回 `user.email_exists`。
- 跨租户 `user_id` 改角色返回 `user.not_found`，其他租户用户不被修改。
- 最后一个 active admin 不能被降级，返回 `user.last_admin_required`。
- admin 不能把自己降级为非 admin，返回 `user.self_role_change_denied`。
- 用户管理 API 响应不包含 `password_hash` 或明文初始密码。
- developer/viewer 控制台不渲染用户管理入口，但后端权限仍独立校验。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

专项命令：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest control-plane-api/tests/integration/test_basic_rbac_users.py -q

cd D:\MetaHub\console-web
npm test
npm run lint
```

当前验证结果：

- 干净测试环境下运行 `.\scripts\ci\check.ps1` 通过：后端 pytest `38 passed`，控制台 Node test `21 pass`，console lint `ok`。
- 后端 PRD-008 专项集成测试 `4 passed`。
- 真实 HTTP 验证覆盖登录、角色目录、用户列表、创建用户、developer 登录、角色更新、非 admin 拒绝、非法角色、跨租户 user_id 拒绝和敏感值不泄露。
- 真实浏览器验证覆盖 admin 可见用户管理入口、developer 不渲染用户管理入口、页面控制台无 error/warning。
