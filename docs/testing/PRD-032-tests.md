# PRD-032 测试记录

PRD：`PRD-032 - Provider 计费适配规范 MVP`

## 覆盖范围

- Provider 方言：OpenAI、通用 input/output、Gemini `usageMetadata` 与缓存 Token 字段归一。
- 缺失与异常：无 usage、只有成本无 Token、负数、非数字、NaN 和总量矛盾均有确定降级语义。
- 成本双口径：Provider CNY/USD 自报成本可追踪，PricePlan 内部成本不被覆盖，USD 不做无汇率快照的隐式换算。
- 安全：raw usage 的 API Key、Authorization、正文、超深对象、超长字符串和超大数组被脱敏或截断；数据库和控制面响应均有防御性脱敏。
- 权限：用量接口继续由后端限制 admin/developer，viewer 越权测试保持覆盖。
- 可观测性：`usage.normalized` 事件包含 request_id/trace_id，不包含 raw usage、密钥或请求响应正文。
- 控制台：归一化详情展示 Provider 成本、警告、normalized usage 和安全 raw usage，动态内容均转义。
- 中文注释：新增定义、函数及 Token、成本、币种、脱敏关键分支均通过仓库注释门禁。

## 已执行命令

```powershell
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest gateway-data-plane\tests\unit\test_provider_adapters.py gateway-data-plane\tests\integration\test_request_log_minimal_loop.py control-plane-api\tests\integration\test_usage.py -q -p no:cacheprovider
python -m pytest control-plane-api\tests -q -p no:cacheprovider
python -m pytest gateway-data-plane\tests\unit -q -p no:cacheprovider
python -m pytest gateway-data-plane\tests\integration -q -p no:cacheprovider
npm run lint
npm test
python -m compileall -q shared-sdk\python control-plane-api\src gateway-data-plane\src
python scripts\ci\check-comments.py
```

阶段性结果：

- 聚焦后端测试：14 passed。
- 前端 lint：`console lint ok (78 files)`。
- 前端单元测试：108 passed。
- Python 源码编译：通过。
- 中文注释门禁：`comment coverage ok`。
- 控制面全量测试：72 passed。
- 数据面全量测试：57 passed（28 unit + 29 integration）。

说明：数据面全部目录单进程组合命令在当前 Windows 执行环境中异常超时；拆分 unit/integration 后 57 项全部通过，且无 pytest 子进程残留。
