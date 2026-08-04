# PRD-038 Python SDK 与示例项目测试记录

测试日期：2026-07-15

## 测试范围

- Python 示例项目的配置、Chat、Embeddings、CLI、超时、重试与安全错误输出。
- 控制面元数据与 ZIP 下载接口的租户隔离、RBAC、统一错误和事件记录。
- `0028_python_sdk_examples` 数据库升级、回滚、重新升级与旧事件保留。
- 控制台 SDK 示例页的安全渲染、鉴权请求、Blob 下载、文件名校验和 `request_id` 反馈。
- README、目录说明、中文注释覆盖和现有功能回归。

## 自动化测试

### 全量 CI

```powershell
$env:PYTHONPATH='shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src'
& scripts/ci/check.ps1
```

结果：

- 后端与 Python 示例：`212 passed in 774.27s`。
- 控制台单元测试：`131 passed`。
- 控制台 lint：`console lint ok (88 files)`。
- 中文注释门禁：`comment coverage ok`。

### Python 示例专项测试

```powershell
python -m pytest examples/python/tests -q
```

结果：`38 passed`。主流程覆盖 Chat 与 Embeddings；异常流程覆盖缺失 Virtual Key、非法超时/重试参数、API 状态错误、连接失败、超时和敏感信息不回显。

### 控制面与回归测试

```powershell
python -m pytest `
  examples/python/tests `
  control-plane-api/tests/integration/test_python_sdk_examples.py `
  control-plane-api/tests/integration/test_developer_quickstart.py `
  control-plane-api/tests/integration/test_console_navigation.py -q
```

结果：`46 passed`。覆盖 admin/developer 主流程、viewer 403、未登录 401、租户模型隔离、固定 ZIP 文件清单、路径逃逸拒绝、事件写入失败降级和下载响应安全头。

### 数据库迁移测试

```powershell
python -m pytest control-plane-api/tests/integration/test_db_migrations.py -q
```

结果：`9 passed`。验证从 `0027` 升级到 `0028` 时保留 `developer.quickstart_viewed`；回滚时移除新事件并恢复旧 CHECK；再次升级成功。

### 静态检查

```powershell
python -m ruff check examples/python control-plane-api/tests/integration/test_python_sdk_examples.py
python scripts/ci/check-comments.py
```

结果：Ruff 与中文注释门禁均通过。

## 运行态验收

- Gateway `http://localhost:8000/health` 返回 200。
- Control API `http://localhost:8001/health` 返回 200，并已应用数据库版本 `0028`。
- 控制台 `http://localhost:5173/#sdk-examples` 使用本地管理员真实登录后，正确展示 base URL、Chat/Embeddings 模型、六个环境变量、三条运行命令、能力列表与九个制品文件。
- 点击“下载 Python 示例”后显示 `已下载 metahub-python-examples.zip` 和 `request_id: console_python_sdk_1784080983459`。
- 数据库存在对应 `sdk.example_downloaded` 事件，`tenant_id` 正确；payload 仅包含 `artifact_version`、`example_id`、`file_count` 和 `language`。
- 桌面默认视口无横向溢出；390x844 临时视口下 `scrollWidth=clientWidth=375`，侧栏底部不超过主内容顶部，SDK 面板和下载按钮均位于应用边界内。

## 验收结论

PRD-038 的主流程、异常流程、权限校验、租户隔离、安全归档、事件追踪和浏览器交互均通过。实现未引入完整私有 SDK，也未改变 Gateway 调用、用量和成本追踪链路。
