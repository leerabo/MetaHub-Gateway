# PRD-010 测试说明

## 覆盖范围

主流程：

- admin 可通过 `POST /virtual-keys` 创建 Virtual Key。
- 创建响应只返回一次 `plain_value`，列表和后续响应不返回明文。
- 数据库只保存 `key_hash` 和 `key_prefix`，不保存明文 Key。
- `GET /virtual-keys` 返回当前租户 Key 列表，包含名称、前缀、环境、状态、最近使用时间。
- 新创建 Key 可作为 `Authorization: Bearer <virtual_key>` 调用 Gateway `/v1/chat/completions`。
- 数据面成功鉴权后更新 `virtual_keys.last_used_at`。
- `DELETE /virtual-keys/{id}` 软禁用 Key。
- 禁用后的 Key 调用数据面返回 `gateway.auth_invalid`。
- 控制台 admin/developer 可见 Virtual Key 面板、创建表单、一次性明文展示和启用/禁用按钮。

异常/权限/安全：

- viewer 调用 `/virtual-keys` 返回 `auth.permission_denied`。
- 非法 environment 返回 `virtual_key.environment_invalid`。
- 空 PATCH 返回 `virtual_key.update_empty`。
- 当前租户找不到目标 Key 返回 `virtual_key.not_found`。
- 并发使用同一 Key 调用 Gateway 不报错，`last_used_at` 有值。
- 控制台不向请求体传 `tenant_id`。
- 控制台不为 viewer 渲染 Virtual Key 面板。
- 响应、数据库、request_logs、usage_ledger 不包含明文 Key。

## 运行命令

```powershell
.\scripts\ci\check.ps1
```

专项命令：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest control-plane-api/tests/integration/test_virtual_keys.py -q

cd D:\MetaHub\console-web
npm test
npm run lint
```

当前验证结果：

- 后端 PRD-010 专项集成测试通过。
- 控制台 Node test 通过：`31 pass`。
- 全量 `.\scripts\ci\check.ps1` 通过：后端 pytest `47 passed`，控制台 Node test `31 pass`，console lint `ok (29 files)`。
- 真实 HTTP 验证通过：控制面创建临时 Key、列表隐藏明文和 `key_hash`、新 Key 调用 Gateway 成功、`last_used_at` 更新、禁用后调用返回 `gateway.auth_invalid`。
- 真实浏览器验证通过：控制台 seed admin 登录后展示 Virtual Key 面板；页面创建 Key 后展示一次性明文；禁用按钮可用且状态变为 disabled；浏览器控制台无 warn/error。
- 验收中发现并修复 CORS 缺口：共享 FastAPI CORS 已允许 `DELETE`，并新增预检测试覆盖控制台 DELETE 管理接口。
