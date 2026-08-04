# PRD-040 从直连模型厂商迁移指南测试记录

测试日期：2026-07-15

## 测试范围

- 控制台迁移指南的 base URL、Provider Key、模型别名、错误差异和回滚五类必选内容。
- Gateway 公网地址规范化、Environment 与模型别名可用状态、租户隔离和后端 RBAC。
- `migration.guide_viewed` 访问事件的规范路径、单次 visit 去重、敏感值拒绝和数据库异常降级。
- `0029_migration_guide_event` 正向、回滚、往返迁移，以及迁移执行器失败回滚。
- Python/Node 直连与 MetaHub 两套非流式/流式 Demo，调用日志、用量成本和允许场景回滚。
- 桌面与移动端布局、键盘 tabs、四个独立复制按钮、Clipboard 失败反馈和中文注释覆盖。

## 自动化测试

### 控制面与数据库专项

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
python -m pytest `
  control-plane-api/tests/integration/test_migration_guide.py `
  control-plane-api/tests/integration/test_console_navigation.py `
  control-plane-api/tests/integration/test_dashboard.py `
  control-plane-api/tests/integration/test_model_aliases.py `
  control-plane-api/tests/integration/test_db_migrations.py -q
```

结果：`23 passed in 127.4s`。覆盖 admin/developer 主流程、viewer/匿名拒绝、事件配对、租户归属、敏感值拒绝、可信 Gateway URL、alias 状态、Dashboard 事件回归和迁移失败原子性。

### 前端专项

```powershell
npm --prefix console-web test
npm --prefix console-web run lint
```

结果：控制台 `148 passed`，lint `console lint ok (91 files)`。覆盖内容完整性、HTML 转义、显式 Environment/alias 选择、ARIA tabs、复制反馈、事件去重和 viewer 防护。

### 静态与注释检查

```powershell
ruff check --ignore E501 <本 PRD 修改的 Python 文件>
python scripts/ci/check-comments.py
```

结果：Ruff `All checks passed!`，中文注释门禁 `comment coverage ok`。

### 全量 CI

```powershell
powershell -ExecutionPolicy Bypass -File scripts/ci/check.ps1
```

结果：退出码 0，总耗时 `775.3s`。Python 全仓 `228 passed in 755.67s`，Node 示例 `33 passed`，控制台 lint 91 文件和中文注释门禁通过；控制台 148 项已在同一代码状态下单独通过。

## 运行态验收

- Gateway `http://localhost:8000`、Control API `http://localhost:8001`、控制台 `http://localhost:5173/#migration-guide` 和本地 Provider `http://127.0.0.1:11434/v1` 均运行可用；运行库迁移为 29 条，最高版本 `0029`。
- Python 官方客户端和 Node 官方客户端均使用同一个本地 Provider 完成直连非流式/流式调用，输出分别为 `MetaHub chat ok` 和 `MetaHub stream ok`。
- 只把 base URL 改为 Gateway、Provider Key 改为临时 Virtual Key、model 改为 `prd037-runtime` 后，两种语言的非流式/流式请求均成功。
- Node request_id 为 `req_c0ca7b017f354eaa8a07fe18eb8217b8`、`req_b41062d235ae4742a28c7affcd80182c`；Python request_id 为 `req_prd040_python_metahub_nonstream`、`req_prd040_python_metahub_stream`。
- 四条 request log 均为 success，解析模型 `prd037-runtime`、Provider `prd037_runtime`；四条 usage ledger 均为输入 4、输出 3、总计 7 tokens、0 CNY，流式状态为 completed。
- 两个验收 Virtual Key 完成后均已禁用，Key 明文未输出、未写文档；按完整直连配置回滚后，Node 非流式与流式再次成功。
- 首次直接 hash 进入产生一条 `source=direct_hash`，离开后侧栏重入产生一条 `source=sidebar`；15 秒刷新和配置选择没有重复事件。两条事件均带认证租户、用户、request_id 和规范路径 `/#migration-guide`。
- 浏览器显式选择“开发环境 / dev”与 `prd037-runtime` 后，四个复制按钮均显示“已复制”；ArrowLeft/ArrowRight 正确切换 Python/Node tabs。
- 390x844 下 `innerWidth=390`、根节点 `scrollWidth=clientWidth=375`；1280x720 下 `innerWidth=1280`、根节点 `scrollWidth=clientWidth=1265`，无文本重叠或横向溢出。

## 验收结论

PRD-040 的主流程、异常/权限、租户隔离、事件追踪、迁移原子性、调用日志、用量成本、真实迁移/回滚和桌面/移动交互均通过。实现没有新增迁移业务 API、自动扫描仓库、上传源码或绕过企业治理链路。
