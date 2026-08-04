# PRD-049 调用反馈与采纳率记录测试记录

测试日期：2026-07-20

当前状态：已完成。需求设计、编码前两轮自审、实现后安全复审、第四至第七轮封账加固复审、代码实现、0038/0039 正反迁移、专项与根级全量回归、真实三服务/API/Webhook、真实 Chrome 三角色三视口、资源清理、停止后数据库与日志复核均已通过。唯一权威成功批次为 `D:/MetaHub/artifacts/prd049/20260720T181908`；旧批次和 `artifacts/prd049/diagnostics/` 下的反例或失败尝试只用于复盘，不作为完成证据。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`；pytest 的 `TEMP/TMP` 固定为 `D:/MetaHub/.tmp`，`PYTHONPATH=shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src`。迁移与真实运行使用 SQLite Backup API 创建的独立验收库，不直接在源开发库执行破坏性往返。

## 1. 验收口径

- Excel 验收要求：可以对成功调用打分；评分进入模型质量统计；跨租户、跨 Virtual Key 或无权限的 `request_id` 不可反馈。
- Control API 的 admin/developer 与 Gateway active Virtual Key 使用同一不可变 Feedback 合同；viewer 由后端拒绝，前端隐藏入口不替代权限校验。
- 所有 API 响应包含统一错误码、`request_id` 和 `trace_id`；不存在的租户/Key 范围统一同形 404，不能形成枚举侧信道。
- Feedback、Event、Aggregate、RequestLog 最终归因和 final Attempt 必须保持 tenant-qualified 图一致；0039 必须阻断 SQLite 默认 `recursive_triggers=0` 下的 INSERT/UPDATE OR REPLACE 覆盖。
- Provider Key、Virtual Key 明文、Token、密码、Prompt、Response、原始上游异常和自由文本不得进入数据库、日志、事件、DOM、URL、Storage 或验收制品。
- 自动化、迁移、Runtime、Browser、人工视觉复核、日志审计和停止后静止数据库是相互独立的完成证据，缺一项不得标记完成。

完整需求、表/API/页面/错误码、安全边界和七轮复审见 [PRD-049 设计文档](../prd/PRD-049-request-feedback-adoption-records.md)；公开 wire、405/CORS 和 Webhook 合同见 [调用反馈与采纳率 API](../api/request-feedback.md)。

## 2. 设计与封账复审

- 编码前第一轮关闭 Gateway 报表热路径、actor 可信恢复、跨租户防枚举、精确 Webhook payload、幂等指纹和 Console owner 生命周期问题。
- 编码前第二轮补齐双向 deferred FK 的 tenant-qualified 候选键、迁移事务顺序、聚合反查验真、raw `X-Request-ID`、单 SQLite snapshot、父事实保护和并发错误重分类。
- 实现后第三轮关闭链路凭证过滤、Summary 绝对 deadline、后端 RBAC、500 CORS/禁缓存和 Browser owner-safe cleanup 问题。
- 第四、第五轮实证 `INSERT OR REPLACE` 在默认非递归 trigger 下可覆盖 Feedback/Event/父事实，并确认必须追加 0039、统一 405 和迁移连接关闭。
- 第六、第七轮继续发现 `UPDATE OR REPLACE` 可从未引用源行删除受保护目标，最终把 0039 扩为六个 trigger、十三个冲突分支，并以 `rowid` 自排除保持生产 upsert 和 Usage/Token/成本补账兼容。
- PAGE-071 最终修复 `waitForResponse` 返回后仍可能读取旧 DOM 的竞态：验收器等待响应数据收敛到当前页面再断言；反向单测固定证明旧 Summary DOM 不得被误判为成功。

## 3. 自动化与静态门禁

| 范围 | 最终结果 |
| --- | --- |
| Python 根级全量 | `783/783 passed`，退出码 0 |
| PRD-049 迁移专项 | `33/33 passed` |
| 全迁移文件 | `56/56 passed` |
| Runtime 验收器单测 | `9/9 passed` |
| JavaScript/Node 示例 | `33/33 passed` |
| Console 修复后全量 | `287/287 passed` |
| Console lint | `114 files passed` |
| Browser 验收器专项 | `16/16 passed`，包含 PAGE-071 旧 DOM 反例 |
| Python compileall | 退出码 0 |
| Ruff | `All checks passed!` |
| 中文注释 | `comment coverage ok` |

最终 manifest 登记 18 个封账 exitcode，非零数量为 0；所有非服务 stderr 均为空。根级 Python 已包含 Shared、Gateway、Control、Python 示例和验收器测试，专项数字只用于证明关键迁移与验收脚本边界，不与根级总数重复累计。

## 4. 0038/0039 迁移证据

- 源开发库为 38 条迁移、最高 `0038`，主文件 SHA-256 为 `da5d30ce95197fe9a2c10b0dc391ae6bc5e6224b3ce892292a40b760fa57c563`。
- SQLite Backup API 创建的初始验收库 SHA-256 为 `70926643f74b945dbf11cb312bfcf550a665182500e56cd5194253434ea89d36`。
- 首次升级只应用 `0039`；随后连续两次迁移均返回 `applied=[]`，39 条全部 skipped。
- 0039 checksum 为 `4f94e14f916aded4c142c2098fa5e9ee34b79e84879d8aff720d59c54fd6335e`；最高迁移名称为 `request_feedback_immutability_hardening`。
- 六个持久守卫完整存在：Feedback/Event INSERT 冲突、RequestLog INSERT/UPDATE 冲突目标、RoutingAttempt INSERT/UPDATE 冲突目标。
- 33 个专项覆盖十三个 REPLACE 分支、默认/开启递归 trigger、合法 RequestLog upsert、普通 UPDATE/补账、up/down 前置与完整性失败原子回滚、六类业务事实 down guard 和迁移连接关闭。
- 空事实可按 `0039 -> 0038 -> 0037` 逐版本回滚并重放；存在 Feedback、Event、Aggregate、`feedback.created` 订阅、delivery 或关联 attempt 时，0039 首次 down 即固定失败且数据、trigger 与迁移状态原样。

## 5. 真实 Runtime 验收

最终 `runtime-state.json` 与 `runtime.stdout.json` 语义一致，`status=passed`，stderr 为 0。Gateway、Control API 和 Console 在验收前均为 HTTP 200。

- Session 与 Virtual Key 两条主流程各形成一条 Feedback、Event 和 Webhook delivery；数据库计数均为 2。
- Summary 精确为 `feedback_count=2`、`average_rating_milli=4500`、`adoption_rate_bps=5000`；ModelQuality 扩展一致，PRD-048 Provider 终态统计保持隔离。
- admin/developer Summary 可读；viewer 的 Feedback/Summary 均由后端拒绝；未知调用、跨 tenant 和跨 Key 同形隐藏。
- Session 同意图 replay、异意图冲突、RequestLog 资格锁定、最终归因与反馈联表均通过。
- Control/Gateway 不支持方法均返回 `infra.method_not_allowed` 和正确 `Allow`；Gateway Summary 继续是统一 404。
- `feedback.created` HMAC、精确 payload、持久 event ID 和敏感字段门禁全部通过。
- Runtime 扫描 6 份日志，数据库与日志敏感命中均为 0；8 项临时凭证资源完成精确清理并验证。

## 6. 真实 Chrome 与人工视觉验收

最终结果为 `artifacts/prd049/20260720T181908/browser/browser-result.json`，与根目录 `browser.stdout.json` 字节一致；两者 SHA-256 均为 `3743a40603457cd303b66be8dd0959abf6945efee9e5396afaabe4f70a23a2e9`，Browser stderr 为 0。

- admin、developer 各验证 PAGE-070/PAGE-071 的 1280x720、900x800、390x844 三档，共 12 张；viewer 两个受限 hash 均回退 overview，共 3 张。
- 15 张 PNG 均非空、应用隐私遮罩且 SHA 各不相同；每个页面/视口的横向溢出、不可达控件和重叠计数均为 0。
- admin/developer 可见不可变 Feedback 和 Summary 2/4500/5000；viewer 对目标 API 的 request、response、prefetch 均为 0。
- 后端探针再次证明 admin/developer 可读、viewer 403；三个角色均真实 logout 一次且认证 Storage 清空。
- Browser error、外部请求、DOM/Storage 敏感命中均为 0；cleanup 验证通过，Feedback 事实不变，FK 与 integrity 仍通过。
- 人工逐段复核 15 张原始长截图，空白图、横向溢出、控件裁切、非连贯重叠和文字遮挡五类发现均为 0。

## 7. 停服、静止数据库与日志终验

三服务停止后执行 `wal_checkpoint(TRUNCATE)`，结果为 `(0, 0, 0)`：

- 最终库 `metahub-prd049.db` 大小 3,911,680 bytes，SHA-256 为 `80578e85c35b8548aaca2d4103ec0d416d5ef6f022566ccb31b85e621ab28d9f`。
- 39 条迁移、最高 `0039`、checksum 匹配、六个加固 trigger、`integrity_check=ok`、FK 违规 0，WAL/SHM 均不存在。
- 两轮 Runtime 保留四条不可变 Feedback、四条 Event、Aggregate `feedback_count` 合计 4、四条 `feedback.created` delivery；没有通过清理业务事实来伪造空库。
- 临时 Browser 用户和 active Session 均为 0；8000、8001、5173 监听均为 0。
- 数据库精确 Secret、通用敏感内容和未分类 Webhook Secret 命中均为 0。

最终日志审计结果：

- 结构化 JSON 909 行、Uvicorn access 809 行、生命周期 8 行。
- 26 行 warning 全部对应受控 4xx/RBAC/异常用例；结构化和 access 5xx 均为 0。
- 应用 ERROR、未知浏览器错误、非服务 stderr、完整凭证和通用敏感命中均为 0。
- 10 个 JSON 文件均可解析，Browser 双 JSON 字节一致，Runtime 双 JSON 语义一致；59 个文本文件完成敏感扫描。
- 最终 manifest 为 `status=passed`，Runtime、Browser、visual、log audit 和 database audit 五项均为 `passed`。

## 8. 诊断与作废证据

- `artifacts/prd049/20260720T112345` 完成过 0038 阶段功能验收，但封账终审随后实证 REPLACE 不可变旁路、框架默认 405 和迁移连接关闭缺口，不能作为最终批次。
- `artifacts/prd049/diagnostics/20260720T112345/replace-repro-before-fix.db` 只用于保存修复前数据库反例。
- `artifacts/prd049/diagnostics/20260720T181908/` 保存 Browser 捕获方式和 PAGE-071 旧 DOM 竞态的失败尝试。最终修复后重新执行 16 个验收器单测和完整 Browser，只有根目录成功结果进入最终 manifest。
- 诊断制品不得与最终批次数字相加，也不得用失败截图替代根目录 15 张权威 PNG。

## 9. 已知边界

- Gateway 只提供 `POST /feedback`，不提供 Summary；聚合报表仅在 Control API。
- 一次成功源调用只允许一条不可变反馈，不提供 PATCH/DELETE 或自由文本评论。
- 逐请求反馈不改写 PRD-048 的 Provider 终态样本、人工先验或 `quality_cost_v1` 决策。
- Gateway 提交后事件队列仍是 best-effort；持久 outbox、进程崩溃接管和 dead-letter 属于 PRD-057。
- 当前数据库运行基线仍为 SQLite；PostgreSQL 接入需要独立迁移方言和并发语义验收。
- OpenRouter、硅基流动仍是竞品与迁移兼容对象，不是默认上游 Provider。

## 10. 结论

PRD-049 的设计、自审、实现、租户隔离、后端权限、统一错误/request_id、不可变反馈与统计、0038/0039 迁移、专项/全量、真实三服务、Webhook、Browser、视觉复核、隐私和静止终态全部通过，可以标记完成。

按 Excel `01_PRD总表` 重新识别出的下一条未完成项是 `PRD-050 - Prompt Cache 感知粘性路由`（M1-企业POC / P1）。总体 108 条 PRD 目标仍在进行中，不因 PRD-049 完成而宣称全部完成。