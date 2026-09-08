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

## 2026-09-08 TLS 重复初始化修复

每次固定 DNS 探测只创建一个标准验证上下文，默认池初始化和目标池共用它；不同探测
仍独立创建上下文和连接池，不引入全局缓存。证书、主机名、SNI、代理与重定向策略不变。
两轮编码前自审见本条设计末节。

- `polling-init-red-r1.xml`：TLS 构造计数反证失败；15 个 SQLite fixture 因系统临时目录
  权限错误未执行。r2 保留测试错误码断言误用造成的失败。r3 修正后 2 failed / 17 passed，
  失败仅为 11 次结构查询和两次探测 4 个上下文。
- `polling-init-green-r1.xml`：21 passed，包括本条 4 个新测试、现有 SSRF/DNS 边界与
  PRD-136 结构检查。真实本地 TLS 验证成功、错误主机名、不可信证书三个分支均通过。
- `polling-init-batch-r1.xml`：SQLite 同批 34 passed，516.64 秒；本条新 TLS 4 项、
  原有健康 API/恢复/权限/SSRF/DNS 4 项通过。无新增付费模型调用。
- Ruff：新增测试全规则通过；Provider service 原有 29 个 E501 与 HEAD 对照未变，
  其他规则通过；`git diff --check` 通过。未声称整个旧文件全规则通过。

已部署至本地 Control API。`prd147-polling-init-runtime-r1.json` 短时 6 个样本、18 次
健康检查通过；不替代此前队列故障或长期验收。`prd147-polling-init-live-r1.json` 中
反馈回放 8/8、日志和筛选 16/16 返回 200，但 Summary 2/8 成功、6/8 返回 503，组合
验收失败。浏览器日志有 2 条既有 Kimi 记录及“已反馈”，汇总页同样 unavailable。
数据核验仍为 2 request / 3 explanation / 1 feedback / 1 event，quick_check=ok、无 FK 违规。

证据根目录为 `.artifacts/g6-security-20260907/`；独立审计
`prd-035-polling-init-sensitive-audit-r1.json`。这两个重复开销修复不构成 Summary SLA、
完整业务目标、G6/G7 或 72 小时稳定性通过。
