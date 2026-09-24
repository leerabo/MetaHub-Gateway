# PRD-148 Control 稳定性修复记录 · 2026-09-13

## 当前结论（覆盖下方历史运行状态）

ga10、ga11、ga12 均不是当前有效的完整 GA 长稳证据。ga11 的启动父进程停止后，采样子进程仍曾继续运行，本轮已验证并停止子进程 11228；ga12 的采样进程 17188 及启动进程 8240 也已在部署更新前停止，历史采样文件保留。ga12 最后核验为 18 个窗口、6354 次请求、0 错误、约 1129.7 秒。

这些观察使用关闭 Control 后台调度器的本地配置，且只探测首页与健康接口。即使计满 72 小时，也不能据此证明后台计量、任务恢复、财务对账和真实业务流程无 P0/P1。后续应先诊断调度器开启时的争用，在批次代码和部署固定后进行完整验收，避免用关闭后台任务的健康计数替代正式发布门禁。当前交付事实见 `PRD-148-runtime-delivery-20260913.md`。

## 触发证据

`outputs/control-client-comparison-20260913/report.json` 对 Control `/health` 使用 httpx 连接复用、httpx 关闭连接和 urllib 三种客户端各运行约三分钟。三种方式都出现一次约 5 秒超时，说明问题不在采样器连接复用。

对当前本地 SQLite 数据库的只读测量显示：

- `PRAGMA foreign_key_check` 约 1.2 秒。
- `PRAGMA integrity_check` 约 17–26 秒，随后台读写竞争变化。
- `PRAGMA quick_check` 约 9.6 秒。
- 数据库包含 1,095 张业务表。

Control 的 PRD-145 reconciliation worker 原来每轮执行整库 `integrity_check`。这会制造长时间磁盘和 SQLite 读压力，并与业务请求争用。

## 修复

`control-plane-api/src/metahub_control/console_experience/scheduler.py` 现在在每轮保留外键校验，并按稳定排序每次检查 64 张表的 `integrity_check('table')`。进程内游标在下一轮继续，表结构变化时按新的表数安全取模；全部表约 86 分钟轮巡一次。空数据库仍使用整库检查，异常结果继续映射为 `platform.unavailable`。

这项修改没有降低完整性覆盖范围，也没有把检查移除或改成永久只读健康标记。它把一次长 I/O 操作拆成有界维护批次，避免健康接口和业务事务等待整库扫描。

## 验证

- `outputs/control-reconciliation-sharded-20260913c.xml`：PRD-145 调度器 cadence、外键/完整性检查和跨表游标轮换 5/5 通过。
- Control 源码分片检查实测：64 张表一轮 `1359.56 ms`，对同一数据库无写入、无业务数据改动。
- ruff 对 scheduler 源码和受影响单测通过。
- 独立副本上的 scheduler-enabled 短观测（Control `18001`，SQLite backup 副本）连续 12 次 `/health` 均为 HTTP 200，耗时 5–236 ms；但日志仍出现 SQLite busy 的调度器观察告警及平台计量投影告警，因此只记录为诊断证据，不能计为稳定性通过。观测实例已正常 Ctrl+C 退出。
- 原有 `outputs/control-client-comparison-20260913/report.json` 保留为修复前失败证据，不能计入 72 小时稳定性。

历史记录中的 Control 进程曾是修复前进程；在后续部署步骤中已重启并加载修复。部署后仍必须先做短时三客户端观测，确认没有新的超时，再将正式 259200 秒观察作为候选证据。正式 72 小时稳定性、真实注册到付费流程和 PRD-148 六方会签仍未完成。

## 2026-09-13 真实试用库短观测与第二轮修复

- 将 64 表分片部署到实际试用库后，正式观察 `outputs/continuous-stability-20260913-ga` 在第 7 个窗口出现 3 次 Control `read_timeout`；失败窗口的 Console/Gateway 均为 200，Control 101/104 成功，最大等待约 5 秒。该目录已明确失败，不能计入 72 小时。
- 根因是 64 表完整性分片在真实 SQLite 竞争下仍可能持有读压力超过 5 秒健康超时。已将 `INTEGRITY_TABLES_PER_PASS` 降为 8，仍由进程内游标覆盖全部表；受影响 PRD-145 单测 5/5、Ruff 通过。
- Control 已重启加载第二轮修复，健康检查 200；新的正式观察 `outputs/continuous-stability-20260913-ga2` 已启动，绑定源码摘要 `d871c73bb1e35eec72a2b57c4ddc03d8ce954da095c3117b85fe1fe604135209`。完成前不计为 GA 通过。
- `ga2` 在并行 QA 启动前的第 9 个窗口再次出现 3 次超时；调查确认平台计量调度器在 `reconciliation_required` 时每 2 秒重试并写失败，形成 SQLite 写竞争。已加入 15 秒有界失败退避，并新增调度器退避单测 2 项；`ga2` 保留为失败诊断。
- Control 已再次重启加载退避修复。新的正式观察 `outputs/continuous-stability-20260913-ga3` 已启动，源码摘要 `3583c63ea27c1e89232b5e096c4b9673bdccaddb368a81e9e4243e0b0617d9c2`，完成前不计为 GA 通过。
- `ga3` 在第 9 个窗口仍出现 3 次超时，说明计量失败退避不足以解决 registry 写入竞争；失败目录保留为诊断。进一步将 PRD-147 scheduler registry 默认轮询从 1 秒调整到 5 秒（受 15 秒租约三分之一上限约束），PRD-147 scheduler 回归 37 项（含 1 项跳过的 PostgreSQL 条件用例）通过。
- Control 已加载 5 秒 registry 轮询修复，正式观察 `outputs/continuous-stability-20260913-ga4` 当前已通过前 21 个窗口、约 1,328 秒、0 错误，最大观察间隔 7.84 秒；源码摘要和首尾一致性仍待 259,200 秒结束后确认。

## 2026-09-13 ga4 / ga5 观察切换

- `ga4` 在第 24 个窗口出现 3 次 Control `read_timeout`；该窗口与 Backend QA 并行运行，Control 成功率为 99/102，Console/Gateway 无错误。按边界规则，`ga4` 保留为失败诊断，不能计入正式 72 小时观察。
- Backend QA 已退出，未再与正式观察并行运行。`ga5` 首个窗口曾完成 351 次请求、0 错误，但随后接入页文案变更使源码摘要发生变化；`ga5` 已停止并保留为旧版本诊断，不能计入正式观察。
- 以当前源码摘要 `2af939461bba2c5441b70293d377db918d9c944a5e421f9c15b7f155a8fb6c16` 重新启动 `outputs/continuous-stability-20260913-ga6`。`ga6` 当前状态为 `running`，首个窗口 349 次请求、0 错误，最大观察间隔 0.522 秒；目标仍为 259,200 秒。完成前不计为 GA 通过。

## 2026-09-13 Provider 探测连接预算修复

- `ga6` 在第 7 个窗口因 Control 4 次 `read_timeout` 失败（Console/Gateway 无错误）。日志显示 Provider health scheduler 在 DNS 快照包含多个不可达地址时，对每个地址重复使用完整 5 秒连接超时，放大为长时间 httpx 网络阻塞，并进一步造成 scheduler registry / SQLite 争用。
- 已修复 `control-plane-api/src/metahub_control/provider_health/service.py`：`_PinnedNetworkBackend.connect_tcp()` 现在为一次探测建立共享 deadline，多个已验证地址只消耗同一连接预算；新增确定性单测证明 5 秒预算在两个地址间传递为 `5.0` 与 `0.5` 秒，而不是两次完整 5 秒。
- Control 已重启加载该修复，`/health` 返回 200。新的正式观察 `outputs/continuous-stability-20260913-ga7` 已启动，绑定当前源码摘要 `752e75cfdb25ccbcc71697d0934b6be98917ccfd0f22b25b73c7ac959cf0002c`；首个窗口 352 请求、0 错误，完成前不计入 GA。
- 后续复核时 ga7 已完成 7 个窗口、2,462 次请求、0 错误，观察约 436 秒；状态仍为 `running`，距 259,200 秒目标尚远。

## 2026-09-13 ga7 失败与本地 HTTP profile

- ga7 在第 9 个窗口失败：Control 1038 次请求中 3 次 `read_timeout`，Console/Gateway 无错误。Control 日志仍显示 SQLite scheduler registry `release/renew` 约 2–4 秒以及 `platform_metering.reconciliation_required`，说明 Provider 连接预算修复之外仍存在本地后台调度器争用。
- 为本地只读 HTTP 可用性观察建立显式 profile：`METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED=false` 时，结构化 supervisor 不再注册 registry bindings；空 coordinator 也不再写 observation snapshot。新增 3 个单测通过。
- Control 已按该 profile 重启，`/health` 返回 200；新的 ga9 已绑定源码摘要 `9bced0ee3568dec3ab9a718ba8077251c39338f6be76ce43b7a7ea8c04b0280f`。首个窗口 352 请求、0 错误，日志未再出现 scheduler registry 或 billing 争用告警，完成前不计入 GA。
- ga9 后续已完成 3 个窗口、1,056 次请求、0 错误，观察约 183 秒；源码摘要和进程状态仍一致。

## 2026-09-13 supervisor profile correction / ga10

- scheduler registry 回归发现，直接让空 coordinator 不写观察会破坏启用调度器时的取消语义；已收窄实现：只在结构化 supervisor 检测到 `control_background_schedulers_enabled=false` 时完全跳过 coordinator，调度器开启时保留原有 observation worker 行为。
- PRD-147 scheduler registry 全量单测通过；新的源码摘要为 `4cf811d5c3d3a238ca0d70b863a3214e5b046e16c90c5b0d8d34d1f5c159e75a`。
- ga9 因该修订绑定旧摘要而停止，ga10 已启动；首个窗口 355 请求、0 错误，Control 日志无 scheduler registry、billing 或 lease 争用告警。
- ga10 后续已完成 2 个窗口、707 次请求、0 错误，观察约 120 秒；状态仍为 `running`。
- ga10 当前已完成 14 个窗口、4,952 次请求、0 错误，观察约 882 秒；源码摘要和长稳进程仍一致。
- 已测得独立全新 SQLite 迁移 fixture 首次构建耗时 163.9 秒（153 个迁移）；PRD-148 GA 集成回归随后完整通过 12/12，之前的中止是时间不足而非死锁。
- ga10 当前已完成 15 个窗口、5,307 次请求、0 错误，观察约 946 秒；进程仍存活，日志无新增异常。
- ga10 当前已完成 16 个窗口、5,659 次请求、0 错误，观察约 1,009 秒；进程仍存活。
- ga10 当前已完成 17 个窗口、6,011 次请求、0 错误，观察约 1,071 秒；进程仍存活。
- ga10 当前已完成 18 个窗口、6,363 次请求、0 错误，观察约 1,135 秒；进程仍存活。
- ga10 当前已完成 19 个窗口、6,718 次请求、0 错误，观察约 1,198 秒；进程仍存活。
- ga10 当前已完成 20 个窗口、7,073 次请求、0 错误，观察约 1,261 秒；进程仍存活。
- ga10 当前已完成 21 个窗口、7,425 次请求、0 错误，观察约 1,324 秒；进程仍存活。
- ga10 当前已完成 22 个窗口、7,780 次请求、0 错误，观察约 1,387 秒；进程仍存活。
- ga10 当前已完成 23 个窗口、8,135 次请求、0 错误，观察约 1,451 秒；进程仍存活。
- ga10 当前已完成 24 个窗口、8,490 次请求、0 错误，观察约 1,514 秒；进程仍存活。
- ga10 当前已完成 25 个窗口、8,845 次请求、0 错误，观察约 1,578 秒；进程仍存活。
- ga10 当前已完成 26 个窗口、9,197 次请求、0 错误，观察约 1,641 秒；进程仍存活。
- ga10 当前已完成 27 个窗口、9,552 次请求、0 错误，观察约 1,705 秒；进程仍存活。
- ga10 当前已完成 28 个窗口、9,907 次请求、0 错误，观察约 1,768 秒；进程仍存活。

## 2026-09-13 统一员工接入信息与 ga11

- 接入页首屏现在只交付一个可复制的统一 MetaHub 网关网址和一把员工个人 KEY；员工在客户端自己的 API/模型设置中粘贴这两项即可。`/v1`、Messages 等协议路径收进工具详情，明确它们是同一网关的技术格式，不是额外服务或额外 KEY；环境变量仅是 CLI/程序保存凭据的方式。
- 接入相关人类流程回归 53/53 通过，`git diff --check` 通过。
- ga10 因该前端源码修改导致绑定摘要失效，已停止并保留为旧版本观察；新的 `outputs/continuous-stability-20260913-ga11` 已按当前摘要 `df1ff8476608b63a883642ceaed20c68ca263f8df768f6e800a9c61f57c9ef4e` 启动，目标仍为 259200 秒，完成前不计为 GA 通过。

## 2026-09-13 scheduler-enabled 隔离复测

- 在独立 SQLite 副本上重新启用 Control 后台调度器，运行 120 秒健康延迟探针：537 次请求全部 HTTP 200，最大 188 ms，P95 62 ms，超过 500 ms 的请求为 0。
- 该实例随后已停止；副本、脚本和 JSON 结果保留在 `.tmp/prd148-scheduler-repro-20260913.*` 与 `outputs/prd148-scheduler-enabled-health-20260913.json`。
- 这是修复后的短时诊断结果，不能替代正式 72 小时稳定性、真实业务流程或 GA 会签证据。
- 本轮受影响回归：Console 人类接入/PRD-148 流程 45/45 通过；PRD-145/147 调度器单测 30/30 通过；Gateway 默认模型与协议门面 9/9 通过。
