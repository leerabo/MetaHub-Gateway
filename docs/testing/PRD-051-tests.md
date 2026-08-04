# PRD-051 FinOps Ledger 正式账本测试记录

测试日期：2026-07-27

当前状态：已完成。设计与两轮编码前自审、0041、实现、分层测试、开发库迁移、真实 Runtime/Browser、日志隐私、Session/spool 清理及停服终态审计均已通过。权威 Browser 批次为 `artifacts/prd051/20260727T043500-browser-success-02`。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。所有 pytest 的 `TEMP/TMP` 和独立 `--basetemp` 均位于 `D:/MetaHub/.tmp`；没有使用 `C:/tmp`、沙箱外目录或提权。

## 1. 验收口径

- Excel `TC-0101`：每笔 Usage 成本可追溯到来源 `request_id`、价格快照和价格版本；调整保存原因与可信操作者审计。
- Excel `TC-0102`：覆盖跨租户、无权限、重复冲正、金额精度、幂等、异常输入、数据库/spool 故障、统一错误码和安全日志。
- 所有正式账本、调整、Usage、快照、审计和事件事实都必须 tenant-qualified；客户端不能提交或覆盖 tenant、actor、币种及派生归因。
- Control 的读取和调整均为 admin-only 后端权限；developer/viewer 的前端零 DOM 只是附加证据，不能替代 API 403。
- API 成功与错误携带 `request_id/trace_id`，FinOps 响应为 `Cache-Control: no-store`；跨租户目标与不存在目标保持同形 404。
- Provider Key、Virtual Key、Authorization、Token、密码、Prompt、Response、Endpoint、调整原因和幂等键原文不得进入日志、Webhook 或验收 JSON。
- 0041、主库直写、持久 spool 恢复、Control 调整、Console 状态、全量回归和真实运行是独立证据，任一缺失都不能标记完成。

完整数据表、API、页面、错误码、安全边界和两轮自审见 [PRD-051 设计文档](../prd/PRD-051-finops-ledger.md)；公开 wire、RBAC、游标、幂等和事件合同见 [FinOps Ledger API](../api/finops-ledger.md)。

## 2. 设计与实现审查

- 第一轮编码前自审修正 SQLite 金额转换、无版本快照、Audit 枚举保真、随机 ID、签名快照游标和 Webhook best-effort 边界。
- 第二轮编码前自审补齐主库失败后的本机持久 spool、原币权威金额、全链路多币种、snapshot currency、游标筛选绑定和 Audit 外部依赖扫描。
- 最终短复核进一步冻结 Adjustment 的币种继承、spool 域隔离 HMAC、服务账号 owner、原子 claim/租期恢复、quarantine、数据库 owner 复核和仅新建分录入队。
- 实现收口补齐 Control、Gateway、Shared、Console、SQL 和测试代码的中文业务约束注释；未为注释修改扩大测试范围。

## 3. 合并专项结果

权威专项批次：`artifacts/prd051/20260727T015822/targeted/`。

| 范围 | 结果 | 关键覆盖 |
| --- | --- | --- |
| Python PRD-051 合并专项 | `39/39 passed`，failures/errors/skipped 均为 0 | 0041、Control FinOps、导航、Gateway spool/事件、Shared 配置/错误/Webhook、Runtime 验收器合同 |
| 0041 迁移聚合 | `9/9 passed`，包含参数化回滚门禁 | 历史回填、自动投影、tenant owner、append-only/REPLACE、业务事实 down 拒绝、失败原子性 |
| Control FinOps | `5/5 passed` | 五类调整、金额精度、幂等、部分/全部冲正、汇总、RBAC、严格 wire、游标、跨租户 404、Audit 回滚、Webhook/OpenAPI/CORS/no-store |
| Control 导航 | `2/2 passed` | admin 入口及 developer/viewer 裁剪 |
| Gateway FinOps | `7/7 passed` | 主库直写、快照后故障、spool exactly-once、篡改隔离、双失败 503、事件循环线程边界、lifespan 周期恢复 |
| Shared 合同 | `9/9 passed`，覆盖 5 项核心合同 | spool 配置边界与遮罩、统一可重试 503、中英文标签、reversal/usage 事件证据状态 |
| Runtime 验收器合同 | `7/7 passed` | 回环地址、证据目录、环境身份、request_id/no-store、安全 JSON、write-once |
| Console FinOps 三文件 | `19/19 passed` | API 白名单、金额字符串、统一错误、PAGE-074/075、admin-only、异步 owner、游标状态、响应式 CSS |
| Browser 脚本静态检查 | 退出码 0 | `node --check`，import 不触发网络或 Chrome |

Python 原始 JUnit：`artifacts/prd051/20260727T015822/targeted/python-junit.xml`，记录 `tests=39`、`failures=0`、`errors=0`、`skipped=0`、`time=116.139s`。Console 专项只执行一次，终端结果为 19 tests、19 pass、0 fail；没有为了补制品重复执行。

## 4. 已验证的安全与一致性合同

- Usage 与正式 `usage_charge` 分录一对一；分录绑定 source request/trace、PriceSnapshot、schema version、evidence status 和可信归因。
- 历史只有金额的事实使用 `ledger_only + legacy_amount_only`，页面和事件不得把它伪装成零单价可重算证据。
- 主库事务失败时 spool 保存失败当时的冻结价格证据；重放不读取当前价格，数据库已存在同一事实时只清理 claim，不重复投递事件。
- spool 拒绝 UNC、symlink/reparse、非当前 owner、超限、重复键、HMAC 或文件名身份异常；正文与文件名使用不同业务域。
- 调整、正式分录和 Audit 同事务；Audit 失败时三表均回滚。幂等只存 SHA-256，不保存 Header 原文；同键异载荷返回 `finops.idempotency_conflict`。
- 冲正只查询当前租户目标，累计金额不得超过原分录；目标为冲正、零金额或跨租户时按冻结错误合同失败。
- `ledger.entry_created` payload 不包含原因、幂等键、Prompt、Response、Token 或 Key；Gateway 只为本次新建且已提交分录入队一次。
- Console 不把 opaque cursor、幂等键、operation ID 或自由文本草稿写入 render snapshot、URL 或 Storage；身份、租户、导航和刷新 generation 变化会使迟到响应失效。

## 5. 最终回归与运行验收

### 5.1 全量一次与聚焦闭环

| 范围 | 首次结果 | 后续闭环 |
| --- | --- | --- |
| Python 根级全量 | `958` 项：`890 passed / 68 failed / 0 errors / 0 skipped` | 不重复全量；按原失败节点复测迁移 `46/46`、Control/旧合同 `13/13`、Gateway `10/10` |
| JavaScript/Node 示例 | `33/33 passed` | 无需复测 |
| Console 全量 | `375/375 passed` | 交互修复后只跑 FinOps 相关 `10/10` |
| Console lint | `125 files passed` | 无新增 lint 问题 |
| 中文注释门禁 | 首轮发现新增代码注释缺口并保留失败日志 | 修订后两次 `comment coverage ok`；最终闭环再次通过 |

Python 全量 JUnit 为 `artifacts/prd051/20260727T015822/full/python-junit.xml`。这次全量如实保留为失败批次，不能写成 `958/958 passed`；68 项在共享迁移历史、Control 旧合同与 Gateway 相邻参数三个受影响层内完成定向闭环，没有因 Browser 焦点或 fixture spool 小修重新消耗整套回归。

文档冻结后的最终聚焦复核为：中文注释 `comment coverage ok`、Browser 合同 `7/7`、FinOps 前端 `10/10`、受 fixture/spool 修复影响的 Python 节点 `2/2`。机器可读结果分别位于权威批次 `final/browser-contract.tap`、`final/console-finops.tap` 和 `final/python-focused-junit.xml`。

### 5.2 开发库迁移

- 迁移前冻结库：`0040`、40 条 applied migration、Usage 18、RequestLog 22、integrity `ok`、外键违规 0。
- 通过 SQLite Backup API 生成 `artifacts/prd051/20260727T015822/database/metahub-dev-before-0041.db`。备份与源库逻辑事实一致；SQLite 页面布局可能不同，因此不声称二进制 SHA-256 相同。
- 首次 migrate 只应用 `0041`；第二次 applied 为空。迁移后为 41 条 applied migration、Usage 18、Ledger 18、Adjustment 0、坏投影 0。
- 18 条 `usage_charge` 中 `priced=3`、`legacy_amount_only=15`；loader checksum 为 `094a73d10b0dd8e39602578206feb5285ef515dc4ec3fdebc73faf2292b1b7e5`，integrity `ok`、外键违规 0。

### 5.3 真实 Runtime

- Gateway、Control API、Console 启动后健康检查均为 HTTP 200；Runtime 证据为 `artifacts/prd051/20260727T015822/runtime-evidence.json`，`status=passed`。
- 管理员读取初始 18 条账本；创建调整返回 `201`，同键同载荷重放 `200` 且资源相同，同键异载荷 `409 finops.idempotency_conflict`。
- 全额冲正返回 `201` 且方向相反；再次超额冲正返回 `409 finops.reversal_exceeds_remaining`。所有响应均验证 `request_id/trace_id` 与 `Cache-Control: no-store`。
- 登出返回 `200` 且 Session revoked。运行库终态为 Usage 18、Ledger 20（18 usage_charge、1 manual_credit、1 reversal）、Adjustment 2、FinOps Audit 2、坏投影 0、integrity `ok`、外键违规 0。
- Browser 权威批次复用上述已通过 Runtime JSON，两个文件 SHA-256 均为 `a07932b95b0f5d2ac22fca81b1d7be9aec67281f9432485602cfca87440d95c9`；没有把复制作成“再次执行 Runtime”。

### 5.4 真实 Browser

- 首次真实页面批次 `artifacts/prd051/20260727T015822` 在 `browser.execute/unexpected_failure` 退出 1，已保留 PAGE-074/075 的 4 张真实截图。
- 批次 `artifacts/prd051/20260727T043500-browser-success` 的两次启动前失败分别为 `config.identity/admin_password_missing` 与 `browser.launch/playwright_unavailable`，均退出 1、未登录、未创建 Session。
- 权威成功批次为 `artifacts/prd051/20260727T043500-browser-success-02`：`browser-evidence.json status=passed`，Browser 退出 0。
- 1280x720 与 390x844 各覆盖 PAGE-074、PAGE-075、Adjustment Dialog，共 6 张 PNG；实际尺寸、长度、SHA-256 与证据逐项一致且像素非空。
- 6 组布局的横向溢出、控件裁切、重叠均为 0；`blocked_request_count=0`、`browser_error_count=0`。初始焦点、Esc 关闭、触发器焦点恢复、草稿销毁与重新打开为空均通过。
- 四项隐私门禁均为 true；登出后 Session revoked、Browser closed。原失败证据均保留且未被覆盖。

### 5.5 停服终态审计

终态证据：`artifacts/prd051/20260727T043500-browser-success-02/final/final-audit.json`，`status=passed`，19 项 gate 全部为 true：

- 0041 精确对象为 2 张 FinOps 表、19 个新增索引、18 个新增触发器加 5 个保真 Audit 触发器，Audit 合同 1；迁移 checksum 与当前 SQL 一致。
- 开发库与运行库的 Usage 投影、价格证据、Adjustment owner、Audit owner、空 tenant、integrity 和外键检查全部为 0 异常。
- Runtime 证据四个资源 ID 与 sequence 19/20、2 micros、相反方向、2 条 Audit 精确关联，错误关联为 0。
- 相对迁移前冻结库新增 3 个验收 Session，终态为 `3 created / 0 active / 3 revoked / 3 revoked_at`；全库 Session 为 `120 active / 27 revoked`。
- 默认与 Runtime spool 的 pending/processing/quarantine 均为 0；服务日志中密码、Authorization/Bearer、Provider Key、Virtual Key、Prompt、Response 与 Adjustment reason 命中均为 0。
- Gateway PID 2652、Control PID 24560、Console PID 3068 及端口 8000/8001/5173 均已停止；开发库与运行库 WAL/SHM/journal 均不存在。

### 5.6 最终检查清单

- [x] 设计及两轮编码前自审完成。
- [x] 主流程、异常流程、权限流程、租户隔离和统一错误通过。
- [x] 0041、重复 migrate、checksum、integrity、外键和投影通过。
- [x] Node、Console、lint、中文注释及受影响 Python/Browser 合同通过。
- [x] 真实 Runtime、双视口 Browser、日志隐私和 Session/spool 清理通过。
- [x] 停服终态审计、README、目录说明、设计验收清单及 final manifest 完成。

## 6. 最终结论

PRD-051 的设计、两轮自审、实现、测试、迁移、文档和实际运行验收已全部通过，当前没有已知 P0/P1 缺陷，可以标记为“已完成”。本结论不表示全部 108 条 PRD 已完成；按 Excel 优先级和顺序核对后的下一条未完成项是 `PRD-052 租户/项目/Key三级预算`。
