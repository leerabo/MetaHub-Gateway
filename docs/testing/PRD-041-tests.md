# PRD-041 竞品定位与能力映射测试记录

测试日期：2026-07-16

## 测试范围

- 帮助中心“竞品对比”对 admin、developer、viewer 的后端导航可见性和匿名拒绝。
- OpenRouter、硅基流动与 MetaHub 的中立定位、当前能力映射、官方来源和售前禁用表述。
- Provider 分类不出现 `openrouter`、`siliconflow`、`silicon_flow`，显式桥接只使用通用 `migration_compatible`。
- `help.competitor_viewed` 的事件、导航、路径、hash、来源、视口配对，以及 tenant/user/session/request_id 追踪。
- `0030_competitor_help_event` 正向、回滚、历史保留、索引恢复和 `0030 -> 0029` 连续回滚顺序。
- 三标签 ARIA 键盘交互、后端可见导航过滤、HTML 转义、第三方链接隔离和桌面/移动布局。
- 通用页面访问状态机对并发渲染、15 秒刷新、离开重入和 Session 变化的去重语义。

## 自动化测试

### 控制面与数据库专项

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
python -m pytest -q `
  control-plane-api/tests/integration/test_competitor_positioning.py `
  control-plane-api/tests/integration/test_providers.py `
  control-plane-api/tests/integration/test_db_migrations.py
```

结果：`16 passed`。覆盖三角色导航、匿名 401、viewer 事件主流程、事件伪造/敏感值拒绝、竞品 Provider 类型拒绝、0030 往返迁移和旧迁移回归。

### 前端专项

```powershell
npm --prefix console-web test
node --test `
  console-web/tests/unit/competitorComparison.test.mjs `
  console-web/tests/unit/migrationGuide.test.mjs
npm --prefix console-web run lint
```

结果：控制台全量 `156 passed`，其中 PRD-040/041 专项 `16 passed`；lint `console lint ok (94 files)`。覆盖中立文案、官方来源、权限过滤、ARIA tabs、事件去重、Provider 下拉边界、通用事件字段和 tracker 重构回归。

### 静态与注释检查

```powershell
ruff check control-plane-api/tests/integration/test_competitor_positioning.py
python scripts/ci/check-comments.py
```

结果：Ruff `All checks passed!`，中文注释门禁 `comment coverage ok`。

### 全量 CI

```powershell
$tempRoot = (Resolve-Path '.tmp').Path
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
powershell -ExecutionPolicy Bypass -File scripts/ci/check.ps1
```

结果：退出码 0，总耗时 `762.5s`；Python 全仓 `231 passed in 742.46s`，Node 示例 `33 passed`，控制台 lint 94 文件和中文注释门禁通过。首次使用系统 Temp 时因沙箱拒绝 pytest 临时目录而在 fixture setup 失败，改用工作区临时目录后同一全量脚本通过；该调整不改变测试选择或应用配置。

## 运行态验收

- `0030` 已幂等应用到 `D:/MetaHub/.data/metahub-dev-prd031.db`；运行库共 30 条 applied migration，最高版本 `0030`。
- Gateway `http://localhost:8000/health`、Control API `http://localhost:8001/health`、Console `http://localhost:5173/` 均返回 HTTP 200。
- 管理员通过 `/#competitor-comparison` 登录后，帮助中心导航、版本 `1.0.0`、核验日期、三平台定位和角色可见映射均正常渲染。
- 首次 hash 进入写入一条 `source=direct_hash`；等待 15 秒自动刷新没有重复写入；浏览器完整回归期间三次侧栏重入分别写入独立 `source=sidebar` 访问。事件均绑定租户 `944f3e6d-8b66-584e-b095-7cac890a5f87`、认证用户、规范路径和 request_id。
- 浏览器验收发现通用导航事件遗漏 `viewport_class`，已补齐并复验：竞品映射跳转 `#api-docs` 成功，事件返回 200，页面不再显示 `console.event_invalid`。

## 浏览器验收

- 1280x720：`documentScrollWidth=documentClientWidth=1265`，竞品根宽 854；表格只在自身 814/820 宽度内局部滚动，无页面横向溢出或文字重叠。
- 390x844：`documentScrollWidth=documentClientWidth=375`，竞品根宽 358；能力表容器宽 318、内容宽 760，横向滚动被限制在表格容器，三个标签均为 107px 且文字不溢出。
- `End` 键从“能力映射”切换并聚焦“售前规范”；15 秒刷新继续保留当前标签；所有时刻只有一个 selected tab 和一个可见 tabpanel。
- 四个官方链接均为固定 HTTPS，并携带 `target=_blank`、`rel=noopener noreferrer`；竞品页 DOM 不包含第三方 script、image、iframe 或 stylesheet 运行时资源。

## 当前结论

PRD-041 的主流程、异常/权限、租户隔离、事件追踪、Provider 分类、0030 正反迁移、全量回归和桌面/移动交互均通过。实现没有把竞品注册为模型厂商或默认上游，也没有新增竞品业务 API、运行时抓取、攻击性文案或第三方资源依赖。
