# PRD-074 批量任务 Scheduler 与 Worker 测试记录

状态：**已完成并通过最终验收（2026-08-08）**。设计、两轮编码前自审、实现后两轮复核、0067、
Shared/Control/Gateway/Scheduler/Worker、预算与用量成本、Webhook source-outbox、PAGE-104、真实 Runtime、
Browser、隐私与禁止文件审计均已闭环。该结论只表示 PRD-074 完成；全部 108 条总体目标仍在进行，
`all_108_prds_complete=false`。

## 1. 接手基线与权威范围

- `main` 在接手时经 fetch 和 `--ff-only` 更新到 `b41854976f3a423f355d38440f29b3320b1f5424`，
  `HEAD == origin/main`；交接基线 `475efd0e95849b84b5bf6623ca3de19d00c07e89` 是其直接祖先，提交主题为
  `feat: complete PRD-073 file security outbox recovery`。开始 PRD-074 前工作树干净。
- 空 SQLite 基线实际应用 66 条迁移，最高 `0066`，`integrity_check=ok`、外键违规 0；PRD-073 已完成，
  没有重复实现。
- 已结构化解析 Excel 七个工作表，并与 README、架构文档、`docs/prd`、`docs/testing` 和目录说明交叉。
  PAGE-104、三个 Batch API、BatchJob/BatchTask、`batch_job.created/completed`、TC-0147/0148 均指向
  PRD-074，不是仅按编号推断。

## 2. 最终自动化门禁

所有 Python 测试都使用仓库内 `C:\MetaHub\.venv`，并把 `TEMP/TMP`、唯一 `--basetemp` 和 JUnit 结果
限定在 `C:\MetaHub\.tmp`。开发阶段只跑受影响专项；实现收口后才执行一次必要组合，不把不同代码时点的
结果相加冒充一次全量。

| 验证项 | 最终结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared Batch 合同 | `9 passed` | DTO、加密/HMAC、owner 绑定、canonical 12 位价格、RBAC revision 10 |
| Gateway Batch unit/integration | `23 passed` | grant、实时优先 gate、治理链、预算/限流、Usage spool、结果密文与错误 |
| Control API + Scheduler | `7 passed` | 创建/读取/取消/结果、tenant/RBAC、统一错误、导航与 scheduler wake |
| Worker + outbox | `23 passed` | 公平调度、lease/heartbeat/Reaper、恢复、取消/撤权、预算、Webhook source/outbox |
| 0067 迁移 | `5 passed` | fresh/repeat/down、owner/状态/不可变约束、RBAC 与外部事实 down guard |
| Webhook 相邻 guard | `3 passed` | source lease、cooperative stop 和失败关闭顺序 |
| 后端最终组合 | `70 passed / 0 failed / 0 errors / 0 skipped` | 上述六组 JUnit 汇总，耗时 `227.818s` |
| Runtime 验收器合同 | `16 passed` | 仓库路径、固定回环、隐私、进程清理、迁移和成功/坏终态 owner 链 |
| Console 全量 | `708 passed / 0 failed` | PAGE-104 strict DTO、owner/generation/Abort、权限、移动布局、DOM 隐私及历史回归 |
| Console lint | `220 files passed` | Console JavaScript 静态语法与仓库规则 |
| Python Ruff | `All checks passed` | PRD-074 新模块、专项测试和 Runtime 验收器；共享旧模块执行 `F,I` 门禁 |

Webhook pending-resume 修正后，PRD-074 outbox 专项为 `6 passed`，相邻 PRD-053 resume 回归为
`10 passed`。Control/Gateway 价格 wire 修正后，共享价格合同为 `4 passed`；真实 Control route plan 已直接
通过 Gateway validator。最终组合已经覆盖这些节点，单独计数仅记录修复闭环，不与 70 项重复相加。

唯一非失败输出是 FastAPI TestClient 依赖链的 Starlette 弃用警告；它不改变本次行为或门禁结论，后续依赖
升级时处理。

## 3. 迁移与数据约束

- `0067_batch_scheduler_worker.sql` 从空库实际执行到 `67/0067`，重复 apply 保持 schema 稳定；down 在空事实
  场景成功，存在 allocation、event/outbox 或外部交付事实时失败关闭。
- 最终数据库为 RBAC catalog revision 10，包含 64 个 permission templates、173 条 canonical 角色模板关系；
  只扩展精确默认角色，自定义权限矩阵不被静默授权。
- BatchJob、BatchTask、BatchAttempt、grant、budget hold/allocation、scheduler cursor、event/outbox 均带
  `tenant_id`，复合外键和 trigger 冻结 owner、状态、租约、金额与不可变事实。
- 最终真实 Runtime 为 `67/0067`、`integrity_check=ok`、外键违规 0；Job 终态为 completed 2、failed 1、
  cancelled 1。

## 4. 业务、权限与恢复闭环

- 创建、列表、详情、任务、取消和结果读取全部由后端按实时 Session tenant 与 `batch.read/write` 校验；
  文件派生任务同时复核 `file.read`。跨 tenant 与不存在资源同形，统一错误携带 `request_id/trace_id`。
- Scheduler 以优先级、到期时间和 tenant cursor 公平 claim；同秒任务使用 dispatch revision 稳定轮转。
  Worker 使用短事务 lease/heartbeat，Provider 调用期间不持有数据库事务。
- 取消、deadline、撤权、过期 lease、已知失败、unknown 和已持久结果恢复均使用完整 owner/CAS 条件；
  prepared attempt 可恢复，无法证明 Provider 未调用时不重复调用。
- Job hold、attempt allocation、grant 与 Usage/FinOps 分别保存事实；成功 owner 链要求 RequestLog、
  succeeded/recorded RoutingAttempt、Usage 和 FinOps trace 一致，posted cost 非零且不与实时 reservation 双计。
- Worker 不持有 Provider 或 Webhook Secret。Control lifespan scheduler 从同 tenant 不可变 event 恢复
  `source_request_id`，并严格比对 event/type/job/actor/trace/timestamp/canonical payload 后投递。

## 5. 正式 Runtime 验收

最终脱敏证据：
[runtime-acceptance.json](../../.artifacts/prd074-runtime/prd074_20260808T105000Z_runtime_final2/runtime-acceptance.json)。

SHA-256：`3A3ACD7D4B306202E450FF6CA75B75EF6F805761CB8A364E096E5A637E9AD7CD`。

该证据是在 owner 链验收补强后重新完整执行得到，不复用旧运行目录：

| 验收项 | 最终事实 |
| --- | --- |
| 固定回环运行 | Gateway 18740、Control 18741、Provider stub 18742、Webhook stub 18743；无外部网络 |
| 实时优先 | 实时请求 200 且从不等待 Batch；busy 时 attempt=0、grant 未消费并轮换 2 次 |
| Worker 恢复 | 过期 lease 被 Reaper 恢复，同一 prepared attempt 完成，Provider 重复调用 0 |
| 取消与撤权 | queued cancel 为 200 且 Provider 调用 0；撤权后调用 0、attempt 0、固定失败码 |
| 账务 owner 链 | Attempt/RequestLog/Routing/Usage/FinOps/Allocation 完整，成本非零，路由为 succeeded/recorded |
| Outbox 恢复 | Control 停服时 source 已提交且未投递；重启 lifespan 自主恢复，active drain=false |
| Webhook | created 4、completed 4；签名失败、非法载荷和禁止字段命中均为 0 |
| 数据库 | `67/0067`、revision 10、`integrity_check=ok`、外键违规 0 |
| 隐私 | 扫描 23 个数据库/对象/日志文件，其中服务日志 14 个；三编码明文命中 0 |
| 清理 | Gateway、两次 Control、四类 Worker 均由 owner 停止；四个固定端口最终监听数 0 |

Provider Key、Token、Prompt、文件正文和 assistant Response 只存在于受控内存或认证密文中，不进入日志、
公开 JSON、Webhook body 或提交制品。证据递归拒绝 secret-shaped key，并保持
`all_108_prds_complete=false`。

## 6. PAGE-104 Browser 验收

脱敏证据：
[browser-acceptance.json](../../.artifacts/prd074-browser/prd074_20260808T094300Z_page104/browser-acceptance.json)。

Browser JSON SHA-256：`A0B1EB8435E689A5BF222289B12B2B09A84FD49E9B587F5388A8A75D18579B38`。

| 视口/场景 | 最终事实 |
| --- | --- |
| Chromium | `151.0.7922.76`，仓库内 executable，固定回环静态 harness |
| `1280x720` 列表 | 横向溢出 0、重叠 0、文本溢出 0，截图非空 |
| `390x844` 列表 | 横向溢出 0、重叠 0、文本溢出 0，截图非空 |
| `390x844` 创建 | 弹窗横向溢出 0、重叠 0、文本溢出 0，截图非空 |
| 生命周期 | 结果仅显式读取一次；先清理再 abort；迟到响应隔离；未授权结果按钮 0 |
| 隐私 | Provider Key、Token、Prompt、Provider Response 和文件正文 DOM 命中均为 0 |

三张 manifest 引用截图的 SHA-256 分别为：桌面
`EDF2776F5CDC8A4AB3612B42648CF9D534000CAB5BE1A225610079AB61786981`、移动列表
`E9403B941D6429BB90947D0056DDF6D7DD63AA20D1F9A9738D0F41B4AD85D599`、移动创建
`9E12AE193F9B81B753264F53C177EBE36BC9E4FF7AFBC74F242722D7222A5B48`。

## 7. 最终审计与下一项

- Git 只白名单最终 Runtime 的 JSON/SHA 和 Browser manifest 引用的 JSON、三张截图、SHA；数据库、WAL/SHM、
  服务日志、浏览器 profile、临时身份和重复截图均不进入提交。
- 源码与待提交制品执行敏感值、凭据形态、禁止文件、临时文件、数据库和大文件审计；未发现 Provider Key、
  Token、Prompt、Response 明文或不允许提交的运行文件。
- Excel 总表、M2/M3 交付、PAGE-105、API、数据对象、Webhook 和验收用例共同确认，下一条权威未完成项为
  `PRD-075 | M2-企业增强 | P2 | 模型评测 | 模型 A/B 测试与质量评估`，对应 PAGE-105、
  `POST /experiments`、`GET /experiments/{id}/results`、ModelExperiment、ExperimentResult、
  `experiment.completed` 和 TC-0149/0150；范围不包含自动训练。

PRD-074 达到可迁移、可运行、可恢复、可审计和可验收终态；总体状态保持
`all_108_prds_complete=false`。
