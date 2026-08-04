# PRD-039 JavaScript/Node 示例项目测试记录

测试日期：2026-07-15

## 测试范围

- Node.js ESM 示例的配置、官方 OpenAI 客户端、非流式 Chat、SSE 流式输出、总时限、有限重试与安全错误。
- 控制面 JavaScript 元数据和 ZIP 下载接口的租户隔离、RBAC、统一错误、安全归档与事件记录。
- Python 示例契约回归、健康 Provider 优先推荐和失败事件 fail-open。
- 控制台双语言 tabs、键盘交互、独立状态、Blob 下载、响应式布局与浏览器 Key 安全边界。
- README、目录说明、中文注释覆盖和全仓历史功能回归。

## 自动化测试

### 全量 CI

```powershell
powershell -ExecutionPolicy Bypass -File scripts/ci/check.ps1
```

最终退出码为 0，结果如下：

- Python 全量测试：`222 passed in 689.13s`。
- JavaScript/Node 示例：`33 passed`。
- 控制台单元测试：`140 passed`。
- 控制台 lint：`console lint ok (89 files)`。
- 中文注释门禁：`comment coverage ok`。

### 控制面与回归专项

```powershell
python -m pytest `
  control-plane-api/tests/integration/test_javascript_sdk_examples.py `
  control-plane-api/tests/integration/test_python_sdk_examples.py `
  control-plane-api/tests/integration/test_developer_quickstart.py `
  control-plane-api/tests/integration/test_console_navigation.py -q
```

结果：`18 passed`。覆盖 admin/developer 主流程、viewer 403、匿名 401、租户模型隔离、健康 Provider 优先级、固定 ZIP 清单、路径逃逸、任意符号链接、文件超限、事件值校验和事件异常降级。

### Node 与前端专项

```powershell
npm --prefix examples/js ci --ignore-scripts
npm --prefix examples/js test
npm --prefix console-web test
npm --prefix console-web run lint
```

结果：Node `33 passed`，控制台 `140 passed`，lint 通过。Node 测试使用真实 `openai@6.47.0` 访问仅监听本机回环地址的 HTTP/SSE 服务，覆盖 `_request_id`、握手 request ID、状态错误、中途断流、空流和响应头后的总时限中止。

### 静态与注释检查

```powershell
ruff check --ignore E501 `
  control-plane-api/src/metahub_control/developer_portal/service.py `
  control-plane-api/src/metahub_control/developer_portal/models.py `
  control-plane-api/src/metahub_control/developer_portal/routes.py `
  control-plane-api/tests/integration/test_javascript_sdk_examples.py
python scripts/ci/check-comments.py
```

结果：Ruff 与中文注释门禁均通过。

## 运行态验收

- Gateway `http://localhost:8000`、Control API `http://localhost:8001`、控制台 `http://localhost:5173/#sdk-examples` 均已启动。
- JavaScript 元数据返回 Node.js 22+、`prd037-runtime`、5 个环境变量、4 条命令、5 项能力和 9 个受控制品文件。
- 非流式 Node 示例输出 `MetaHub chat ok`，退出码 0，`request_id=req_c8ea8805ac8c44c7b5943a8f3cb5be71`。
- 流式 Node 示例逐段输出 `MetaHub stream ok`，退出码 0，`request_id=req_c113d84ab95047a1842c4ea416d79853`。
- 两条请求日志均为 success；用量账本均记录输入 4、输出 3、总计 7 tokens、成本 0 CNY，流式状态为 completed。
- 点击“下载 JavaScript 示例”后显示文件名与 `request_id=console_javascript_sdk_1784086237296`；数据库存在同 ID 的 `sdk.example_downloaded`，payload 仅含版本、示例 ID、文件数和语言。
- 控制台明确提示只允许 Node.js 服务端持有长期 Virtual Key，不提供浏览器 Key 输入或浏览器直连建议。
- 390x844 视口下 `innerWidth=390`、`scrollWidth=clientWidth=375`，命中 `max-width: 720px`；JavaScript 面板、下载按钮和安全提示矩形均为 `left=28`、`right=348`，无横向溢出。

## 验收结论

PRD-039 的主流程、流式输出、异常处理、权限校验、租户隔离、安全归档、事件追踪、调用日志、用量成本和桌面/移动端交互均通过。实现未推荐浏览器直连 Key，也未建设完整私有 SDK。
