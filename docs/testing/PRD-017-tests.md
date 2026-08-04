# PRD-017 Token Usage 与成本估算测试记录

## 覆盖范围

- 主流程：非流式模型调用写入 `usage_ledger`，记录 `input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`、`cost_cny`、`cost_usd`、`estimated`、`usage_source` 和 `price_snapshot_id`。
- 成本：`price_snapshots` 保存入账时模型价格、币种和 `price_schema_version`；M0 不做 CNY/USD 汇率折算。
- 控制面查询：`GET /usage/summary` 支持按天、模型、Virtual Key 聚合；`GET /usage/records` 支持明细分页和估算筛选。
- 安全：viewer 后端拒绝；查询不接受 `tenant_id`；控制面响应不返回 `raw_usage`、Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。
- 异常：非法日期、非法 `estimated`、非法分页返回统一 `usage.date_invalid` 或 `usage.query_invalid`，响应携带 request_id。
- 前端：用量面板展示总览、按天/模型/Key 聚合、明细分页、估算标记和双币种成本。
- 注释：Python/JavaScript/SQL/CSS 文件和关键分支通过中文注释门禁。

## 自动化测试

已运行：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m pytest control-plane-api\tests\integration\test_usage.py control-plane-api\tests\integration\test_request_logs.py gateway-data-plane\tests\unit\test_provider_adapters.py gateway-data-plane\tests\integration\test_request_log_minimal_loop.py -q
```

结果：`13 passed`。

```powershell
& $py -m pytest -q
```

结果：后端全量 pytest 通过。

```powershell
$node='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test console-web\tests\unit\*.test.mjs
& $node console-web\scripts\lint.mjs
```

结果：控制台 Node 测试 `55 pass`，`console lint ok (44 files)`。

```powershell
& $py scripts\ci\check-comments.py
```

结果：`comment coverage ok`。

## 全量验收

已运行项目统一 CI：

```powershell
$py='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:METAHUB_PYTHON=$py
powershell -ExecutionPolicy Bypass -File scripts\ci\check.ps1
```

结果：
- 中文注释门禁：`comment coverage ok`。
- 后端全量 pytest：`75 passed in 218.55s`。
- 控制台 Node 测试：`55 pass`。
- 控制台 lint：`console lint ok (44 files)`。
