# PRD-035 测试记录

PRD：`PRD-035 - Provider健康探测与状态展示`

## 覆盖范围

- 迁移：`0025_provider_health.sql` 新字段、Key 级状态、日志表、索引、租户归属触发器、旧匿名健康状态重置和确定性 down 回滚。
- 探测：OpenAI-compatible `/models` 流式状态探测、3xx/401/403/429/5xx/超时/连接错误固定分类、无正文下载和无原始异常回显。
- 状态机：手工失败立即 unhealthy、周期连续失败阈值、成功恢复 healthy、inconclusive 不刷新健康证据、generation + lease CAS 丢弃迟到结果。
- KeyPool：逐凭据认证，一把坏 Key 不放大为 Provider 故障；全 429 仅冷却；数据面过滤新鲜坏 Key，过期结论 Fail Open。
- 路由：直接模型新鲜 unhealthy 返回 `gateway.provider_unhealthy` 并写失败请求日志；模型别名在请求前选择健康 fallback；不包含 PRD-036 请求失败后重试。
- 权限与租户：admin/developer 可读和手工探测，viewer 后端 403；跨租户 Provider 统一 404；probe 请求禁止 URL、Header、Key 和 tenant_id。
- 脱敏：Provider Key 明文/密文、Authorization、上游正文和异常 repr 不进入 API、探测日志、Webhook 或应用日志。
- 前端：健康摘要、列表、筛选、单行探测、Dashboard 独立降级、HTML 转义、viewer 不渲染、请求竞态 generation/AbortController 和移动布局。
- 中文注释：新增 Python、JavaScript、SQL、CSS 和测试定义，以及关键判断、跳转、异常、权限、租户和脱敏分支均通过强制门禁。

## 执行命令

```powershell
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests -q
python -m pytest gateway-data-plane\tests -q
node --test console-web\tests\unit\*.test.mjs
node console-web\scripts\lint.mjs
python -m compileall -q shared-sdk\python control-plane-api\src gateway-data-plane\src
python scripts\ci\check-comments.py
```

## 当前结果

- PRD-035 聚焦控制面测试：迁移、失败恢复、日志、权限和严格请求体通过。
- 控制面全量测试：82 passed；随后新增 SSRF 纯函数边界测试并聚焦通过，当前共 83 项。
- 数据面全量测试：68 passed；新增别名避障、直接 503、过期 Fail Open 和坏 Key 过滤用例通过。
- 前端单元测试：117 passed；lint：`console lint ok (84 files)`。
- Python 编译：通过。
- 中文注释门禁：`comment coverage ok`。
- 本地迁移：开发库成功应用 0025；控制面、数据面和控制台标准端口分别返回 200。
- 运行态：周期调度把未在 SSRF 私网 allowlist 内的本地 `localhost:11434` Endpoint 标记为 `endpoint_blocked`，健康页显示固定错误分类且未泄露正文或 Key。
- 视觉复验：1440、1100、720、390 宽度均正确显示 Provider 健康导航、五类摘要、筛选和状态行；DOM 检查无页面/行横向溢出，浏览器控制台无错误，截图人工检查无文字重叠或遮挡。
- 安全清理：视觉验收临时管理员 Session、账号和页面事件已删除，临时浏览器依赖与含 Token 的会话文件已移除。
