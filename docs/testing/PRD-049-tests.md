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

## 11. 2026-09-08 完整调度配置下的汇总隔离修复

本次结论独立于上述历史基线。默认线程池被后台工作占满时，空 Summary 也消耗完
250ms；改为按 PRD-075 既有模式使用进程内专用单线程，并逐调用 copy_context。
每 app 有界槽位、截止时间、shield、真正退出后归还槽位与末端授权均保留。
两轮编码前自审见本条设计第 18 节。

证据均位于 `.artifacts/g6-security-20260907/`：

- `prd049-summary-isolation-red-r1.xml`：1 failed / 1 passed。默认池饱和时空 Summary
  超时，独立上下文对照通过，证明无关后台工作可以提前耗尽汇总预算。
- `prd049-summary-isolation-green-r1.xml`：6 passed，覆盖饱和、上下文、取消、执行器
  拒绝、原有串行门禁和超时后槽位恢复。
- `prd049-summary-isolation-units-r2.xml`：35 passed，包含新增真实数据库提交 guard
  拒绝且不落库，以及受影响的连接生命周期回归。
- `prd049-summary-isolation-batch-r1.xml`：9 passed / 11 deselected，216.50 秒，含
  Workspace 创建/幂等/跨 owner/summary、末端新快照撤权和 HTTP timeout 后连接保留。
- `prd147-feedback-native-summary-isolation-r1-b7e9166878594baa8f93c29d4f5ddc18.xml`：
  PostgreSQL 对应 3 项通过，164.351 秒；隔离实例已停止，无 postmaster.pid。
- 生产模块和新增测试 Ruff 全规则、`git diff --check` 通过。

已部署至本地 Control API，Gateway 与 Console 未重启。发布前 PID 8864 的最近记录为
63 accepted / 0 dropped / 0 backend failure；仅是该进程观察，不覆盖更早失败进程。
发布后 `prd049-summary-isolation-runtime-r1.json` 6 个样本、18 次健康检查通过，
63 queues / 0 drop / 0 backend / 0 shutdown timeout，只代表短窗口。

`prd049-summary-isolation-live-r1.json` 和 r2 两轮各 8 次原有反馈幂等回放：16/16 返回
200 且 fact digest 未变，日志/筛选 32/32 返回 200。Summary 每轮 7/8 成功，总计
14/16 成功、2 次 503，组合验收仍为 failed；不能用第二轮覆盖第一轮，也不能宣称
250ms SLA 已通过。成功响应约 156-234ms，HTTP 客户端时间不等于纯查询耗时。

浏览器初次进入仍 unavailable，点击“刷新”后恢复为 1 feedback / 平均 5/5 / 采纳率
100% / Kimi K2.6 Test；当前桌面截图非空、主表单和统计无重叠，未重跑三浏览器或
全部响应式布局。`prd049-summary-isolation-facts-r1.json` 仍为 2 request / 3 explanation /
1 feedback / 1 event，quick_check=ok、FK 违规 0。没有新增付费模型调用。

独立敏感审计为 `prd-049-summary-isolation-sensitive-audit-r1.json`，精确凭据检查为
`prd049-summary-isolation-key-audit-r1.json`。保存原有失败、用户文档及受保护 baseline。
剩余间歇 Summary 超时、历史队列故障、计量 owner 错误、完整目标和 G6/G7/72 小时
仍未通过；线程池隔离的局部正确性不等于这些目标完成。

## 12. 2026-09-09 调度可靠性批次的独立汇总复核

本次未改变 PRD-049 生产代码，继续使用中间件起算的 250ms deadline、实际 Workspace
权限、新快照终态复核和同操作键幂等。PRD-147 的调度观测隔离不能代替本条汇总验收。

`.artifacts/g6-security-20260907/scheduler-summary-diagnostic-feedback-r1.json` 中 8 次
Summary 均 200，随后最终调度版本的 `scheduler-summary-isolation-feedback-r2.json`
为 4 次 200、4 次 503，组合仍失败。两轮各 8 次原反馈重放均返回同一事实，日志和
SavedFilter 均 200，无 401、无新增反馈或模型调用，不用首轮成功覆盖后续失败。

联合 SQLite 证据 `prd147-reliability-batch-sqlite-r1.xml` 为 67 passed / 1 failed，失败
在权限读取故障消除后的 Summary 恢复请求；此前的会话未撤销、Cookie 未清除及事实不变
断言均通过。最终事实 `scheduler-summary-isolation-facts-r2.json` 仍为 2 request /
3 explanation / 1 feedback / 1 event，quick_check=ok、外键违规 0。

浏览器反馈报表可显示 1 条、平均 5/5、采纳率 100%，但仍有不正确的权限导航提示。
该次浏览器成功不证明间歇 503 已修复。后续继续针对真实权限复核与共享数据库竞争定位，
不更改统计口径、放宽预算或引入跨请求权限缓存。完整产品目标及 72 小时验收仍未完成。

原生联合证据 `prd147-feedback-native-scheduler-summary-batch-r1-196b60ab1b2e493f9c574d5dfe8c83b6.xml`
为 22 passed / 1 failed；失败在反馈创建/重放/跨域流程末段的 Summary 503。测试 PostgreSQL
实例正常停止，未以通过的调度锁测试代替本条失败的汇总结果。独立敏感审计记录见
`PRD-049-summary-reverification-sensitive-audit-20260909.md`，本条记录独立提交。

## 13. 2026-09-09 冷启动及权限分阶段诊断

本轮没有 PRD-049 生产变更，没有新增模型请求或反馈事实。独立诊断进程导入真实 app，
使用正常 localhost 登录和 Workspace 切换，读取运行中的数据库但不启动 lifespan；
运行服务仍有后台队列竞争。报告的 request_and_worker_join_ms 包含 worker 归还时间，
并非生产服务器 HTTP SLA。旧失败原件保留。

证据目录为 `.artifacts/g6-security-20260907/`：

- `prd049-local-phases-cold-origin-r1.json`：首请求 503、1091ms；连接阶段在请求开始
  769ms 后才进入，打开连接 2.24ms，空闲连接归还 313ms。后续请求 200、149-155ms。
- `prd049-local-phases-cold-dispatch-r1.json`：首请求 cProfile 记录 633 次 validator
  初始化和 597 次 get_dependant，定位到 FastAPI 惰性路由准备；profiler 开销未扣除。
- `prd049-local-phases-json-prototype-r1.json`：扩展目录 JSON 聚合未形成实质收益，
  不进入生产。原始 r1、r2 及 cold-open-r1 作为阶段定位证据保留。
- `prd049-local-phases-dispatch-prepared-r1.json`：准备 540 条路由后仍四次 503，
  首次 351ms；数据库首次读取仍超过预算。
- `prd049-local-phases-dispatch-and-sqlite-prepared-r1.json`：加连接准备后，带 profiler
  的四次请求仍 503；首请求 90ms 已在权限阶段失败，不能视为快速成功。
- `prd049-local-phases-prepared-no-cprofile-r1.json`：关闭 profiler 的对照为
  200/503/200/503，含 worker 归还的耗时 249/255/210/249ms，完整认证约 194-230ms。
  准备原型不足以修复间歇不可用。

诊断工具 `.tmp/profile_feedback_local_phases.py` 的 SHA-256 为
`e99ff08ad4ae1d8798582b8d14831fad79b044d6d461cce779978b2f23bc5c1d`。
九份报告分别记录自己的结果，不拼成通过批次；最早的 r1 未记录退出状态，不能追认
清理通过，其余报告中 own_session_logout_status 均为 200。

本地回放工具另补自己的会话 finally 注销，未来回放结果要求注销返回 200，异常类型
只记录类别。没有撤销其他会话。权限目录、最新撤权、截止预算与财务历史事实不变；
完整服务、G6/G7/G8 和 72 小时仍未通过。独立审计见本轮 local-phases 审计记录。

清理修正后的 `prd049-local-phases-live-replay-r1.json` 为真实服务对照：8 次原反馈
幂等重放与 16 次日志/筛选读取均 200，Summary 为 7 次 200、1 次 503。失败请求的
HTTP 客户端时间为 11719ms，不能由 250ms 业务预算推断响应已及时发出。事实前后
一致，own_session_logout_status=200，整轮仍为 failed。该真实运行与九份独立阶段
诊断分开，不以成功的短样本覆盖尾延迟问题。

## 14. 2026-09-09 独立写连接暖复用与实际发布

本条与当前运行可靠性批次联合验证，单独记录代码、测试和审计。仅反馈 POST 的原权限
依赖和业务 worker 显式启用独立连接池；没有把写事务并入 HTTP 请求连接，没有缓存权限。
设计第 20 节含两轮实施前自审。已有 GET 预算、Origin/CSRF、幂等及终态授权均不改变。

证据均位于 `.artifacts/g6-security-20260907/`：

- `prd049-replay-phases-local-r1.json`：四次既有反馈重放均 200，耗时
  1756/1317/1279/1240ms。首样本打开四个、后三个样本各打开三个物理连接，零接收者
  派发仍需 363..403ms。该进程未启动 lifespan，不代表真实服务 SLA。
- `prd049-replay-phases-pooled-r1.json`：同样四次恢复同一事实，211/507/490/255ms；
  测量请求内未新开物理连接，派发 1.4..3.2ms，每次仍完整校验权限三次。两次写锁等待
  报 OperationalError 后按既有只读幂等恢复路径成功，不能视为锁问题消失。
- `prd049-replay-pool-red-r1.xml`：四项预期失败，新 scope API 尚不存在。
  `prd049-replay-pool-green-r1.xml`：17 passed，物理复用和既有连接生命周期回归。
- `prd049-replay-pool-control-r1.xml`：26 passed / 2 failed。测试未开启事务即 commit，
  原 guard 正确不执行；加入真实 BEGIN 后 `control-r2.xml` 为 28 passed。
- `prd049-replay-pool-lifecycle-r3.xml`：9 passed，额外验证真实 asyncio 取消后 worker
  保留独占物理连接；另一连接看不到未提交行，worker 退出才回滚归还，旧 facade 失效。
- `prd049-replay-pool-api-r1.xml`：11 setup errors。误从子目录运行导致相对迁移目录
  不存在，并触发已有 DbError 传播异常；保留原件，不为错误启动目录改生产代码。
- `prd049-replay-pool-api-r2.xml`：10 passed / 1 failed。失败库确有一条反馈，但创建
  和默认汇总处于同一秒，按原闭开窗口被上界排除；旧断言依赖慢请求自然跨秒。
- `prd049-replay-pool-api-r3.xml`：11 passed，147.559 秒。显式覆盖上界排除、下界
  纳入、默认 30 天窗口及同窗口跨租户隔离，并保留撤权、冲突、权限读取故障和超时回归。
- `prd147-feedback-native-replay-pool-r1-evidence` 对应 XML 的三项原生 PostgreSQL
  创建/重放/汇总、终态过期和 worker 线程契约通过；实际文件名以该前缀的 UUID 原件为准。
  `prd147-feedback-native-replay-pool-window-r2-10baa42c64de418bb20aa4cea6828418.xml`
  独立重跑更新后的时间边界流程通过。两个隔离 PostgreSQL 实例均记录正常关闭。
- `prd049-replay-pool-regression-r1.xml`：42 passed，复用连接、Control 读取、资源事务
  生命周期和独立 Summary worker 的受影响回归。Ruff 与差异格式检查通过。

仅更新本地 Control：旧 PID 7044（12:47:09）换为 PID 20332（13:49:01）；Gateway
16852、Console 336 未重启。`prd049-replay-pool-deployment-r1.json` 保存生产源码摘要、
旧进程身份和失败计数。发布前仍有 accepted=126、dropped=63、backend_failure=18，
`prd049-replay-pool-runtime-before-r1.json` 为 failed，不能用新进程零计数覆盖。

真实服务 `prd049-replay-pool-live-r1.json`：8 次同操作反馈全部 200、同一事实且
idempotent_replay=true，HTTP 耗时 203..547ms；16 次日志/筛选和 8 次 Summary 均 200，
Summary 141..188ms。自己的测试会话注销 200，反馈事实前后摘要相同，provider_calls=0。
这组有限样本证明本次实际发布可用，不证明全部性能或 72 小时稳定性。

`prd049-replay-pool-facts-before-r1.json` 与 after-r1 的 13 组账务事实计数及摘要逐组
一致，quick_check=ok、外键违规 0，三个服务入口/就绪检查均 200。原 owner_invalid
失败 outbox、pending outbox 和 checkpoint=0 保留，未改写历史数据制造账務完成。

首次发布短观测 `prd049-replay-pool-runtime-r1.json` 为 failed：前段租约仅接管 56 项，
后续恢复 63 项；18 次健康检查成功，错误/丢弃/停止超时计数均为 0。启动阶段和稳态
分开评价，不删掉该轮。独立敏感记录见 `PRD-049-replay-pool-sensitive-audit-20260909.md`。
G6/G7/G8、历史账务问题、独立会签和 72 小时验收仍不通过。
