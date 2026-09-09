# PRD-099 测试记录

## 2026-09-09 原生真实 Usage 投影增量

`test_prd099_postgresql_projection.py` 在完整原生迁移后复用六项计量合同，通过真实 Workspace
登录、订阅创建及原样 Usage 触发器进入投影。平台目录鉴权仍使用受控上下文，不等于真实 Platform MFA。
`prd147-feedback-native-native-billing-batch-r1-9b5ab028733443038834b5672eb9cfca.xml`
中本项六项均通过：历史 Usage 时间、权益左右边界、新周期失败回滚、冻结账单的业务及真实 SQL 故障
回滚和重试。联合批次其他模块后续发生产品竞态及基础设施失败，不宣称整批通过。
`native-billing-sqlite-regression-r1.xml` 共 7 通过，其中本项同合同六项通过。
运行库只读 `native-billing-runtime-readonly-r1.json` 与 `native-billing-post-restore-readonly-r1.json`
均为三个 HTTP 200、quick_check=ok、外键违规 0，历史缺失权益、失败 outbox 与 checkpoint=0 未改写。
`native-billing-pre-restore-runtime-r1.json` 记录服务退出及后台 dropped=1323、backend_failure=307；
恢复入口不关闭这些历史失败，不构成 Summary SLA、稳定性或 72 小时验收。
`native-billing-current-readiness-r1.json` 再次确认三个入口 HTTP 200、quick_check=ok、外键违规 0，
失败和 pending outbox 各一条、checkpoint=0。`native-billing-current-runtime-r1.json` 为 failed：
四样本/12 HTTP 均 200，但当前进程已有 accepted=315、dropped=252、backend_failure=76、queued=63。
`native-billing-scheduler-stacks-r1.json` 是当前控制进程 30 秒非阻塞栈诊断，4768 样本、一次采样错误；
不包含局部变量，不证明失租原因或业务修复。工作簿只读复核，未修改或导出原表。

联合终验 `prd147-feedback-native-native-billing-batch-r2-1c36ae3da1ec46499dfc85144417aafb.xml`
为 52 passed / 0 failed / 0 errors / 0 skipped，1451.642 秒；本项真实投影 6/6，相邻原生
schema/空投影 2/2。六项与已有 SQLite 合同完全相同，包含原生真实 SQL 失败后的回滚及幂等恢复。
隔离 PostgreSQL 正常停止；原生迁移只影响新的 PostgreSQL 部署，未重启或改写当前 SQLite 试用库。
独立审计为 `prd-099-native-billing-sensitive-audit-r1.json`，精确凭据扫描为
`prd099-native-billing-key-audit-r1.json`。本批测试/静态检查通过不关闭运行库缺失权益、历史 failed
outbox、调度租约和 Summary 失败，不代表 G6/G8、公开 GA、全部目标或 72 小时已完成。

状态：实现、自动化门禁、真实浏览器验收和最终候选审计已完成。权威下一项为
`PRD-100 - 生产基础设施`。`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB | migration `3 passed` | 0090->0091、fresh install、pristine down/up、非 pristine 回退阻断、索引、触发器、完整性与 FK |
| API | platform billing `1 passed` | 真实 Workspace 登录与订阅、Usage outbox、exactly-once、周期关闭、迟到 Usage、CSV、Platform 调整 CAS/幂等与凭据负矩阵 |
| WEB | PRD-094..099 affected `35 passed` | Workspace/Platform Cookie 边界、CSV Blob 清理、响应 allowlist、角色裁剪、调整 CSRF/幂等/CAS、响应式 DOM |
| Browser | Identity/Platform `390x844`、`1280x900`、`1440x900` 通过 | 真实 Cookie/Session/TOTP、账单摘要、筛选、调整表单、响应式布局与敏感输入清理 |
| Static | Console lint `321 files`、Ruff、compileall、`git diff --check` 与候选敏感扫描通过 | PRD-099 当前变更集与 PRD-094..098 页面兼容 |

## 迁移与领域结论

- 0091 独立创建 meter definitions/readings、billing cycles、quota allocations、summaries、adjustments 与
  outbox/checkpoint，不复用 PRD-051 FinOps 或 PRD-079 quota ledger。
- 迁移 checkpoint 固定到上线时 `usage_export_ledger_sequences` 最大序列，不回填上线前历史 Usage；之后由
  Usage INSERT trigger 追加独立 outbox。worker 使用 canonical sequence 与 source 唯一键保证 exactly-once。
- 每条 Usage 只投影四个整数数量和 owner hash，不复制 `raw_usage`、Prompt、Response 或 Secret。请求 meter
  使用套餐月请求权益；Token meter 明确为 informational，缺失价格不解释为免费。
- open 周期读取 live aggregate；closed summary 不可变。迟到 Usage 追加 `late_usage` adjustment 并标记
  `reconciliation_required`。手工调整要求平台权限、summary hash CAS 与幂等键。
- pristine 数据可回退并重新 apply；存在 billing/readings/outbox 事实时 down 原子失败。fresh 与升级库均通过
  完整性和 FK 检查。

## 授权与客户端结论

- Workspace finance 和 Platform billing 是两个独立 Cookie/Session 解释域，混合 Bearer、Workspace、
  Platform 或 Support 凭据在业务查询前失败关闭。
- Workspace Console 提供周期、meter、配额、超额和 CSV；仅 owner/finance_admin 有 DOM。Platform Billing
  提供状态筛选、主体绑定游标、reconciliation 列表和 adjustment 表单；调整权限撤销后零 DOM。
- CSV 客户端在读取 Blob 前验证冻结 Content-Type、文件名、`no-store` 与 `nosniff`，点击完成后立即删除
  临时节点并 revoke Blob URL。平台调整正文只从已加载摘要派生 owner、cycle、meter 和 CAS hash。

## 真实浏览器验收

- 使用隔离 SQLite 数据库完成真实 GlobalSession、WorkspaceSession、PlatformSession/TOTP 流程；Workspace
  账单摘要显示 4 个 meter，Platform Billing 显示 1 个 `reconciliation_required` 周期并可按该状态筛选。
- 390x844、1280x900、1440x900 六个页面/视口组合的 root 横向溢出均为 0，未发现未受控元素越界或
  交互控件重叠；Identity 与 Platform console error/warning 均为 0。
- Identity 的 3 个密码输入均为空；Platform 验证成功后 TOTP 输入已从 DOM 移除。密码、TOTP、Cookie、
  Session 与 Secret 均未进入截图、测试记录或提交候选。
- 截图保存在 `.tmp/prd099-browser-evidence/`：
  - `identity-390.png` `4d7c3ba7aa5faea230e30be390fc89c7b1dddd102fcd7ac3445a02257c2758d1`
  - `identity-1280.png` `21a569eca41ab0326bb72a87361b9197c79d26e6b291bd8874a2a1ad32151206`
  - `identity-1440.png` `ca35b80322234c66c389d7b71f993f8f4b357320377181f53c97132b36d83a57`
  - `platform-billing-390.png` `b9a7be7c0ff7153998e02b2fe307021173fcae5442a8e653c71c41011cfb272b`
  - `platform-billing-1280.png` `c35146ee5354343b1ba955e453145d58685349a145bf27d517543c9456f1fb1e`
  - `platform-billing-1440.png` `f3904c7db8443037a11f4565c5ca890e82eaeea3e2f89f2b980dc14adb55e538`

## 定向回归说明

- PRD-051/079/080 组合回归未全绿：历史 fixture 仍把最新 migration 硬编码为 `0073`/`0083`，在当前
  `0091` 仓库会于 setup 失败。
- 其余 4 个失败已用独立 basetemp 单独复现：旧 quota outbox 投递预期、旧 webhook mock 缺少
  `payload_format`，以及 2 个 Windows spool 重试/目录可见性预期。失败所在模块和测试不在 PRD-099
  候选 diff 中；PRD-099 的 DB/API/Console 专项均通过，未用这些历史失败替代或放宽专项门禁。

## 2026-09-09 事实时间与投影原子性修复

以下为本次增量结果，前文为历史验收，不代表当前商业链路或原生计量已全量通过。
证据目录：`.artifacts/g6-security-20260907/`。

- `prd099-projection-red-r1.xml`：5 failed / 1 passed，56.975 秒。复现延迟入队错失历史权益、借用
  入队时权益接受非法时间、失败时保留新 cycle/allocation 和部分迟到 Reading/adjustment。
- `prd099-projection-green-r1.xml`：6 passed，62.451 秒；原始红测保留。
- 数据库错误最终用例改为第二条迟到调整后执行真实缺列 SQL，检查外层 rollback、claim 复原、
  重试成功且恰好八条 Reading/四条 adjustment，冻结 summary 完整不变。
- `prd147-feedback-native-prd099-projection-r1-d624e232680c4f5b9eb91282950f4ac8.xml`：6 errors，
  尚未进入计量断言。失败用例源保留于 `.tmp/test_prd099_postgresql_projection_pending.py`，不计为通过。
- `prd147-feedback-native-prd098-099-schema-r2-8b656eee2a3743c5b0dbe5668d2e1836.xml`：4 passed，
  PRD-099 仅占两项：八个必需表的缺失/view/temp 边界，以及空 Usage 投影不改 checkpoint。
  其余两项为 PRD-098。原生完整投影仍未通过，不注入 Usage 或绕过触发器建立假前置条件。
- 原生前置缺口：订阅订单隐含 rowid；Usage 导出序列表主键无分配器，其 append trigger 又省略该列。
  需要独立序列/迁移治理，不能以随机 ID、物理地址或跳过检查点替代。
- `prd099-projection-predeploy-r1.json`：运行库套餐、订阅和权益均为零；outbox 一条 failed、一条
  pending，checkpoint=0。失败原因 `platform_metering.owner_invalid`。quick_check=ok，FK 违规为零。
- `prd098-099-predeploy-runtime-r1.json`：上线前已有 accepted=126、dropped=63、backend_failure=63、
  shutdown_timeout=45，采样 failed；三个 HTTP 均为 200。此证据不因代码发布重启而被清除或改写。

本批不改历史 Usage、失败 outbox、价格或订阅依据；Summary SLA、后台队列稳定性和 72 小时验收仍未完成。
`prd098-099-batch-r1.xml`：19 passed，365.039 秒，包含相邻套餐流程、完整关账/迟到调整/幂等流程、
六项投影故障回归和 11 项 schema 测试。定向 Ruff、compileall、diff check 通过。

本地发布：Control API 更新为 PID=2172，Gateway/Console 未重启。
`prd099-projection-postdeploy-r1.json` 与 predeploy 的 14 组账本计数及 SHA-256 完全一致，
checkpoint 仍为 0，FK 违规为零，quick_check=ok，三个服务 readiness 均 200。
`prd098-099-live-feedback-r1.json` 仅该回放报告 passed：8 次同反馈重放、16 次日志/过滤器查询及
8 次 Summary 全部 200，反馈事实摘要不变。POST 仍有秒级延迟，本记录不认定性能 SLA 通过。

CUA 浏览器首次刷新报 `feedback.unavailable`，request_id=`req_81608988d95348fbb33469de35ade9fc`；
再次刷新恢复 1 条 Kimi K2.6 Test、5/5、100% 采纳，截图目视非空且可滚动。该次浏览器失败与脚本
成功分别保留，不能宣称 Summary 间歇故障已修复，也没有重做移动端验收。
`prd098-099-postdeploy-runtime-r1.json` 只记录新进程短采样；上线前的后台计数失败不因此关闭。
独立敏感审计：`prd-099-projection-sensitive-audit-r1.json`、`prd099-projection-key-audit-r1.json`。

## 2026-09-09 空轮询只读准入与本地发布

投影与关账先在自有只读事务检查工作，释放游标/快照后才申请写锁。写事务重新检查全部事实，
调用者已有事务时拒绝操作但不提交、回滚或关闭调用者的连接。设计与两轮自审见第 11 节。
以下证据均位于 `.artifacts/g6-security-20260907/`，各轮独立记录，不合并为一次通过结果。

- `prd099-idle-admission-red-r1.xml`：5 passed / 9 failed，真实独立 SQLite 写锁复现无工作仍争锁。
- `prd099-idle-admission-green-r1.xml`：14 passed，覆盖空、失败、序号缺口、缺失 outbox、状态冲突、
  无到期周期、已有业务事务、并发状态/检查点/时间变动以及 schema 不可用。
- `prd099-idle-admission-batch-r1.xml`：23 passed / 2 failed，269.298 秒。包含完整账单 API 成功；
  两项新增集成测试准备数据时违反 checkpoint 的序号/事实 CHECK，尚未到达事务保留断言。
- 测试改为更新合法 `updated_at`，仍检查预期错误码、未提交数据可见、自有事务存续及 rollback
  恢复全部事实。`prd099-idle-admission-projection-r2.xml`：10 passed，78.889 秒，含原六项真实
  订阅/权益/迟到投影合同及四项并发、事务保留增量。
- `prd147-feedback-native-prd099-idle-admission-r1-46b2e43e9b8d4a28b48bd3d22cb65a9b.xml`：
  原生 PostgreSQL 同十项合同 10 passed，267.667 秒，隔离实例正常停止且无 postmaster.pid。
- 四个受影响 Python 文件 Ruff、compileall 及 `git diff --check` 通过。受保护基线与复用迁移
  输入的 SHA256 均保持原值，未重新迁移或编辑运行数据库。

本地 Control API 从已核验 PID 19220 更新为 PID 312，Gateway/Console 保持运行。
`prd099-idle-admission-predeploy-r1.json` 与 `postdeploy-r1.json` 的 13 组事实计数/hash 相同，
quick_check=ok、外键违规 0、三个 readiness 均为 200。历史 failed/pending 各一条、checkpoint=0
继续保留。缺失历史权益仍需独立的对账依据，不能跳过失败记录或虚构套餐价格。

`prd099-idle-admission-feedback-r1.json` 的八次已有反馈回放与十六次日志/过滤器读取均 200，
反馈事实摘要不变；Summary 为七次 200、一次 503，整份报告仍为 failed，无付费模型调用。
发布前四样本/十二次健康检查均 200，但旧进程已有 63 dropped、19 backend failure。
发布后 `prd099-idle-admission-runtime-r1.json` 十二样本/三十六次健康检查均 200，63 队列，
仍有 3 dropped，backend failure/shutdown timeout 为 0，报告仍为 failed。不以重启清零或
本项空轮询修复宣称调度稳定、Summary SLA、全目标、G6/G8 或 72 小时验收通过。

独立审计记录：`PRD-099-idle-admission-sensitive-audit-20260909.md`。
