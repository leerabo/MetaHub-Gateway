# PRD-022 测试记录 - Virtual Key级RPM/TPM限流

## 覆盖范围

- 控制面主流程：`PATCH /virtual-keys/{id}/limits` 支持 admin/developer 设置 RPM、TPM 和并发限制。
- 控制面异常/权限：viewer 后端拒绝、非法限制值返回 `virtual_key.limit_invalid`、空请求返回 `virtual_key.limit_update_empty`。
- 数据面 RPM：同一 Virtual Key 达到限制后返回 429 `gateway.rate_limit_exceeded`。
- 数据面隔离：Key A 超限不影响 Key B。
- 数据面 TPM：估算 token 超限时在上游调用前拦截，避免 Provider 成本。
- 数据面并发：并发限制拒绝同时在途请求，并在请求完成后释放槽位。
- Redis 不可用：启用限流规则但计数器不可用时 fail closed，返回 503 `gateway.rate_limit_unavailable`。
- 可观测性：超限和 Redis 不可用请求写入失败 `request_logs`。
- 前端：Virtual Key 限制设置表单、client 请求体、错误包装和 viewer 隐藏入口。
- 安全：响应、日志和 Redis Key 不包含明文 Virtual Key、Provider Key、Authorization、Prompt 或 Response。
- 注释门禁：新增/修改代码满足定义、函数、关键判断、异常和返回路径的中文注释要求。

## 已执行命令

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\ci\check-comments.py
```

结果：`comment coverage ok`。

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest control-plane-api\tests\integration\test_virtual_keys.py::test_virtual_key_limit_settings_flow_and_permissions gateway-data-plane\tests\unit\test_rate_limits.py gateway-data-plane\tests\integration\test_virtual_key_rate_limits.py -q
```

结果：`9 passed`。

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest control-plane-api\tests\integration\test_db_migrations.py -q
```

结果：`5 passed`。

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe --test console-web\tests\unit\*.test.mjs
```

结果：`69 passed`。

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe console-web\scripts\lint.mjs
```

结果：`console lint ok (53 files)`。

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pytest -q
```

结果：后端全量 pytest 通过，输出进度到 `100%`。
