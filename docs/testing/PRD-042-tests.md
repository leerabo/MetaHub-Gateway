# PRD-042 Webhook 事件过滤与重放基础测试记录

测试日期：2026-07-17

运行环境：Windows PowerShell；Python 使用 Codex 内置运行时；运行库为 `D:/MetaHub/.data/metahub-dev-prd031.db`。pytest 使用工作区 `.tmp` 下的唯一 `--basetemp`，避免系统临时目录权限影响测试结论。

## Excel 验收映射

- `TC-0083`：真实失败 delivery 完成手工重放；相同幂等键不重复出网，不同键可创建另一条 replay；Secret 轮换前旧 HMAC 有效，轮换后新 HMAC 有效且旧 HMAC 对新投递失效。
- `TC-0084`：事件/状态筛选和最近 100 条上限、统一错误码、request_id/trace_id、租户隔离、admin/developer 与 viewer/匿名权限、审计、敏感信息不泄露和状态追踪均有自动化与运行态证据。

## 自动化测试

### Webhook 与迁移专项

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
$base = Join-Path (Resolve-Path '.tmp').Path ('pytest-'+[guid]::NewGuid())
python -m pytest -q control-plane-api/tests/integration/test_webhooks.py --basetemp $base
```

结果：`8 passed`。覆盖主流程、最近 100 条稳定排序、事件/状态筛选、串行/并发同键幂等、不同键再次重放、Secret 新旧验签、shared runtime 轮换竞态、匿名/viewer/跨租户、非法筛选/幂等键、disabled/success 拒绝、递归敏感或不一致历史信封零出网。

`control-plane-api/tests/integration/test_db_migrations.py` 在完整 Control API 回归中通过，覆盖 0031 从真实 0030 历史升级、original/replay 两组唯一约束、Endpoint/source/actor 租户所有权触发器、attempt 外键、回滚删除 replay 并保留 original，以及 `integrity_check`/`foreign_key_check`。

### Python 全量

收集结果：55 个测试文件、239 条测试。统一命令为：

```powershell
$env:PYTHONPATH="shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src"
$env:TEMP="D:/MetaHub/.tmp"
$env:TMP="D:/MetaHub/.tmp"
$base = Join-Path (Resolve-Path '.tmp').Path ('pytest-final-'+[guid]::NewGuid())
python -m pytest -q --basetemp $base
```

同一收集清单的拆分回归结果为 `239/239 passed`：Control API `120/120`、Gateway `81/81`、Python 示例 `38/38`。每组使用相同 `PYTHONPATH`、工作区 TEMP/TMP 和独立 basetemp；无断言失败。

统一单进程命令持续 `904.1s` 后达到当前工具执行上限，进程被终止且未产生单一 pytest 摘要；未把超时记作代码通过。随后按上述三组拆分，同一 `--collect-only` 清单的 239 条全部取得退出码 0，因此全量测试覆盖没有缺项。该现象是本机单进程反复创建完整 SQLite 迁移库的执行时长问题，不是断言失败。

### Node、Console 与静态门禁

```powershell
npm --prefix examples/js test
npm --prefix console-web test
npm --prefix console-web run lint
python scripts/ci/check-comments.py
```

结果：Node 示例 `33/33 passed`；最终 Console `159/159 passed`；lint `console lint ok (94 files)`；中文注释门禁 `comment coverage ok`。前端新增回归还固定了两项浏览器发现：定时刷新不得抢占正在进行的交互刷新，身份切换必须同时清除一次性 Secret、错误、反馈和旧 request_id。

## 运行库迁移与完整性

- 迁移命令把 `0031_webhook_replay_secret_rotation` 应用到运行库；共 31 条 applied migration，最高版本 `0031`。
- 迁移前使用停服状态下的 SQLite 文件备份保存 `D:/MetaHub/.data/metahub-dev-prd031.before-0031.db`。
- `PRAGMA integrity_check` 返回 `ok`，`PRAGMA foreign_key_check` 为空；Endpoint 存在 Secret 版本字段，delivery 存在 original/replay 两组部分唯一索引。
- 最终验收 Endpoint Secret 版本为 4，所有 replay 均已离开 `pending`；管理审计包含 `webhook_delivery.replayed` 和 `webhook_endpoint.secret_rotated`，摘要只含 ID、事件、状态、时间和版本。

## 真实 API 验收

本地内存接收器前三次返回 500，之后返回 204；只在测试进程内短暂保存验签所需信息，未写接收正文、签名或 Secret 文件，验收后已停止并删除。

1. admin 创建 Endpoint `wh_cb0b404db3074546985a3131495c756e`，创建响应包含 request_id `req_35659753de1142568d91d616b769096a`，Secret 版本为 1。
2. `POST /test` 完成 3 次 500 尝试并得到 failed 来源 `whd_4735455d0c2e40bf96a11661ce28d318`，request_id 为 `req_4c50704713544eb59e8b71eed898dc3b`。
3. developer 首次以键 A 重放得到 success replay `whd_6dcce617e24f4531be26036d5571818c`；相同键第二次返回 `replayed=false` 和同一 delivery，接收器计数不增加；键 B 产生另一条 replay `whd_9ddc3d39ea1046e1bbffc5e597325664`。
4. 首轮运行接收器总请求数为 6：来源失败 3 次、两次不同键 replay 各 1 次、轮换后 test 1 次。同键重复请求没有额外出网。
5. 固定原始 body 与签名时间戳验证：旧 Secret 对轮换前记录有效；新 Secret 对轮换后记录有效；旧 Secret 对轮换后记录失败。列表、详情和审计均不包含两版 Secret 或幂等键。
6. 所有成功和错误响应体的 request_id/trace_id 与 `X-Request-ID`/`X-Trace-ID` 响应头一致。日志目录对 `whsec_`、验收幂等键、管理员密码、签名头和 `payload_json` 的匹配计数均为 0。

## 权限、租户与异常

- admin 和 developer 的详情/重放管理调用成功；viewer 对详情、重放、轮换均为 403 `auth.permission_denied`；匿名三条路径均为 401 `auth.token_missing`。
- 专用第二租户 Endpoint `wh_prd042_cross_c591173b26` 由默认租户调用详情、重放和轮换，三条路径均为 404 `webhook.endpoint_not_found`，request_id 分别为 `req_dcf7009bfa174b899fc03c2329039229`、`req_693c825ade564eaa838dd64602b8fa26`、`req_989c1edea7324c4494eb88ba7709018b`；接收器计数保持不变。
- success delivery 重放返回 409 `webhook.delivery_not_replayable`；非法状态/limit 返回 422 `webhook.delivery_filter_invalid`；短幂等键返回 422 `webhook.idempotency_key_invalid`；请求体夹带 tenant_id 返回 422 `infra.validation_failed`。
- 自动化另覆盖 disabled Endpoint、跨 Endpoint delivery、损坏/非对象/递归敏感历史信封、审计失败回滚、数据库约束失败和并发同键零重复发送。

## 浏览器验收

使用系统 Chrome 和 Playwright 对实际 `http://localhost:5173/#webhooks` 验收，截图只在一次性 Secret 已清除后生成：

- 1280x720：`innerWidth=clientWidth=scrollWidth=1280`；检查 42 个可见控件/行，无越界或按钮文本截断；78 个响应式字段的 `::before` 标签全部存在。
- 390x844：`innerWidth=clientWidth=scrollWidth=390`；同样检查 42 个可见控件/行，无根节点横向溢出、重叠或按钮截断；78 个字段标签全部存在。
- 页面按 `webhook.test + failed` 筛选后点击重放，HTTP 200，反馈显示与响应一致的 request_id `console_webhooks_1784249458707`；轮换 HTTP 200，版本升至 4，显示一次性 Secret 提示。
- Secret 未进入 localStorage/sessionStorage；登出后 DOM 立即清除，重登录后 Secret、旧反馈和旧 request_id 均未恢复。
- viewer 的 Webhook 导航、面板和直接 hash 面板计数均为 0；网络中 `GET /console/navigation` 422 和 `GET /environments` 403 是后端对 viewer 非法目标/权限的预期拒绝，不作为页面隐藏替代后端权限的证据。
- 截图：`artifacts/prd-042-browser/desktop-1280x720.png`、`artifacts/prd-042-browser/mobile-390x844.png`。

## 已知边界

PRD-042 在短事务提交 replay intent 与审计后才出网。若进程恰在此后、发送前崩溃，记录可能停留在 `pending`；同一幂等键只回读已有 intent，不接管重发，因此当前是单幂等键 at-most-once 语义。持久恢复扫描、`dead_letter`、对象存储、归档和过期策略属于 PRD-057（TC-0113/TC-0114），不在本条完成范围内。

## 当前结论

PRD-042 的设计与两轮自审、0031 正反迁移、后端主流程/异常/权限/租户/并发/安全测试、前端交互、全量拆分回归、运行库迁移、真实 HMAC/重放 API 和桌面/移动页面验收均已通过。实现未引入对象存储死信桶、MCP、硬件、信创或复杂数据出境能力，也未把 OpenRouter/硅基流动作为默认上游。
