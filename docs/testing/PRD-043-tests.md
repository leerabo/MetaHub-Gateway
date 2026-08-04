# PRD-043 用量 CSV 导出测试记录

测试日期：2026-07-17

当前状态：设计、两轮自审、实现、专项与全量拆分回归、运行库迁移、真实同步/异步 API 和桌面/中间宽度/移动浏览器验收全部通过，PRD-043 已完成。

运行环境：Windows PowerShell；Python 使用 Codex 内置运行时；运行库为 `D:/MetaHub/.data/metahub-dev-prd031.db`。pytest 使用工作区 `.tmp` 下的唯一 `--basetemp`，避免系统临时目录权限影响结论。

## Excel 验收映射

- `TC-0085`：admin 按日期、项目、Virtual Key、模型及现有 Usage 维度导出固定 42 列 CSV；小结果同步下载，大结果创建异步任务并完成领取、生成、状态查询和授权下载；请求、下载及 `usage.exported` 可追踪。
- `TC-0086`：developer/viewer/匿名、跨租户、未知或非法筛选、超过行数/字节上限、pending/expired/failed、数据库或生成异常均返回统一错误和 request_id，不泄露 CSV、筛选、SQL、Key、Token、Prompt 或 Response。

## 自动化测试

### 导出、Usage、审计与 Webhook 交叉回归

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
$env:TEMP=(Resolve-Path '.tmp').Path
$env:TMP=$env:TEMP
$base = Join-Path $env:TEMP ('pytest-'+[guid]::NewGuid())
python -m pytest -q `
  control-plane-api/tests/integration/test_usage_export.py `
  control-plane-api/tests/integration/test_usage.py `
  control-plane-api/tests/integration/test_audit_logs.py `
  control-plane-api/tests/integration/test_webhooks.py `
  --basetemp $base
```

结果：`20 passed`。覆盖同步/异步闭环、同租户并发 request_id 幂等、筛选指纹冲突、42 列固定 schema、UTF-8 BOM/CRLF、Decimal 金额、公式防护、完整性校验、lease 接管、三次失败收敛、过期清理、审计、最小 `usage.exported` payload、HMAC 和 Webhook 失败不回滚任务。

### 请求详情与迁移回归

```powershell
python -m pytest -q `
  control-plane-api/tests/integration/test_request_logs.py `
  control-plane-api/tests/integration/test_db_migrations.py `
  --basetemp $base
```

结果：`18 passed`。覆盖 0032 正向/回滚/再次升级、历史 Usage 序列回填、只增 `usage_export_sequence`、租户归属、任务状态和不可变约束、audit CHECK 恢复、`integrity_check`/`foreign_key_check`，以及 Usage Ledger 追加事实约束与既有请求详情/路由轨迹兼容。

专项还验证：任务计数后插入与存量记录同秒的新 Usage，其序号高于 `snapshot_ledger_sequence`，worker 最终仍只导出快照内记录；已分配序号的 Usage UPDATE/DELETE 被数据库拒绝，SQLite 隐式 rowid 复用不会改变历史任务边界。

### Python、Node、Console 与静态门禁

- Python 最终拆分回归：`245/245 passed`，其中 Control API `126/126`（`796.78s`）、Gateway `81/81`（`263.02s`）、Python 示例 `38/38`（`0.53s`）。
- Node 示例：`33/33 passed`。
- Console：`172/172 passed`，耗时 `5336.2896ms`。
- Console lint：`console lint ok (94 files)`。
- 浏览器验收脚本 `node --check scripts/acceptance/prd043-browser-acceptance.mjs` 退出码 0。
- 本 PRD 涉及 Python 文件的关键 Ruff 规则检查通过，Python 编译通过；不把该结果表述为全仓 style Ruff 通过。
- `python scripts/ci/check-comments.py` 退出码 0，中文注释门禁输出 `comment coverage ok`。

## 运行库迁移与完整性

- 运行库共 32 条 applied migration，最高版本为 `0032`。
- `PRAGMA integrity_check` 返回 `ok`；`PRAGMA foreign_key_check` 返回 0 行。
- `usage_export_ledger_sequences` 使用 `usage_export_sequence INTEGER PRIMARY KEY AUTOINCREMENT`；历史账本按 `created_at, usage_ledger_id` 回填，新账本由同事务 AFTER INSERT 触发器分配序号。
- `usage_export_jobs` 最终共 24 条：sync completed 23 条、async completed 1 条；pending/processing 共 0 条，error_code 非空任务 0 条。
- `usage_export_events` 共 24 条；`usage_export.requested` 与 `usage_export.downloaded` 审计各 24 条，不保存 CSV、完整筛选或敏感正文。
- Gateway `http://localhost:8000/health`、Control API `http://localhost:8001/health`、Console `http://localhost:5173/` 最终复验均返回 HTTP 200。

## 真实 API 验收

### 同步下载

- Chrome 页面以 admin 身份触发 `GET /usage/export.csv`，HTTP 200。
- 最新任务 `uex_6aaa83da8a21439aba2c977091537fa1` 为 completed/sync，request_id 为 `console_usage_export_create_1784278533406_2cad874fb31e`，row_count 15、download_count 1。
- `Content-Disposition` 为 `attachment; filename="metahub-usage-all-all-6aaa83da.csv"`；实际 CSV 为 8677 bytes、15 条数据行、42 列，页面反馈包含 request_id。
- 文件包含 UTF-8 BOM 和 CRLF，公式防护检查 `formulaSafe=true`；SHA-256 为 `39613ed3ff753a18a864dd5db072df65deb73c315a5bac4531498db9cef15d07`。下载响应同时使用私有 no-store 和 `nosniff`。

### 异步任务

- 真实异步任务 `uex_9a2fd81cf67346b497fd30b8afda0dce` 使用 request_id `req_prd043_runtime_async_c`，完成 `202 -> processing -> completed -> download` 闭环。
- 任务最终 row_count 15、content_size_bytes 8677、download_count 1、attempt_count 1，文件名为 `metahub-usage-all-all-9a2fd81c.csv`。
- 验收结束后运行库 pending/processing 为 0；没有遗留 lease 或无限重试任务。

## 权限、租户与异常

- admin 后端导出和页面下载成功；developer 可以读取 Usage，但后端无批量导出权限且页面无导出按钮；viewer 无 Usage 导航；匿名由认证中间件拒绝。
- 任务状态和下载始终使用 `tenant_id + usage_export_job_id` 查询；跨租户与不存在统一返回 `usage.export_not_found`，不能枚举其他租户任务。
- 同 request_id、同规范筛选回放同一任务；同 request_id、不同筛选返回 `usage.export_filter_invalid`。
- 未知 query、非法日期或布尔值、超过行数/字节上限、未就绪、过期、最终失败和数据库/生成异常均使用固定错误码和响应 request_id。
- CSV、API、审计、事件和结构化日志不包含 Provider Key、Virtual Key 明文/哈希、Authorization、Cookie、Token、Prompt、Response、messages、raw_usage 或 normalized_usage。
- 日志安全扫描覆盖 42 个文件、20,500,142 bytes，真实可用 Secret 模式命中 0。历史 PRD-039 夹具中有 4 个仅 12 字符的显式 dev/test dummy marker，不是生成密钥；因此不把所有 `mhv_` 字样误报为 0，也不把测试标记当成可用秘密。

## 浏览器验收

使用系统 Chrome 和 Playwright 脚本 `scripts/acceptance/prd043-browser-acceptance.mjs` 对实际 Console 验收：

- 1280x720：`innerWidth/clientWidth/scrollWidth/bodyWidth` 均为 1280；关键元素 `offenders/candidates` 均为 0，无页面横向溢出、重叠或按钮文字截断。
- 900x800：上述四项宽度均为 900；首条 Usage 记录 9 个 `data-label` 伪元素存在，表格和记录宽度均为 562，无局部横向滚动。
- 390x844：上述四项宽度均为 390；`offenders/candidates` 均为 0。首条 Usage 记录 9 个 `data-label` 伪元素存在，表格和记录宽度均为 318，无局部横向滚动。
- admin 实际下载 HTTP 200、8677 bytes，15 条数据行、42 列、BOM/CRLF、公式防护、SHA-256、文件名和页面 request_id 反馈全部正确。
- developer 有 Usage 导航和页面，但没有 admin-only 导出命令；viewer 没有 Usage 导航，也不能从竞品能力映射获得 Usage 跳转。viewer 直接访问 `#usage` 时 hash 被清空、active item 回到 overview，Usage DOM 数为 0。
- Console 单元测试覆盖畸形和超长 hash：非空非法值被裁剪且固定回退 overview，不会把越权导航发给后端或恢复旧偏好；viewer 刷新时不预取 Environment，避免隐藏页面仍触发 403。
- admin/developer/viewer 三个角色的 browser console error、page error 和 HTTP >=400 计数均为 0。
- 截图：`artifacts/prd043/browser-desktop-1280x720.png`、`artifacts/prd043/browser-mobile-390x844.png`、`artifacts/prd043/browser-mobile-records-390x844.png`。三张截图均使用灰色 mask 移除邮箱、租户和 Session 身份文本后落盘。

## 已知边界

- MVP 使用有界 SQLite BLOB、单机 scheduler 和 24 小时默认保留期，不包含 MinIO/S3、分布式队列、跨节点对象存储或长期归档。
- Python `asyncio.to_thread` 不能强制取消已经执行的 SQLite/CSV 线程；生命周期会停止下一轮领取并有界等待，正在执行的任务仍依赖 lease 和幂等完成条件收敛。
- 当前角色目录没有 FinanceAdmin，批量导出暂为 admin-only；财务角色由 PRD-059 实现，不能在本 PRD 提前扩展角色合同。
- 0032 后 Usage Ledger 是追加事实；更正必须写入新的调整事实，不能 UPDATE/DELETE 已入账记录。

## 当前结论

PRD-043 的设计与两轮自审、实现、0032 迁移、后端主流程/异常/权限/租户/并发/安全测试、Python/Node/Console 全量拆分回归、真实同步与异步 API、桌面/中间宽度/移动页面、CSV 合同和日志脱敏验收全部通过。运行库无 active/error 导出任务，三服务最终健康复验通过，PRD-043 可以正式标记完成。
