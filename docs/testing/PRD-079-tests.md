# PRD-079 套餐、试用与额度类型测试记录

状态：**已完成**（2026-08-14）。两轮编码前设计自审、实现、两轮实现后自审与修正、专项和必要回归、
0072 空库迁移往返、真实 Runtime、PAGE-110/111 Browser、文档与提交前安全门禁均已完成。本结论只关闭
PRD-079；`all_108_prds_complete=false`，未启动或混入 PRD-080。

## 1. 权威范围与设计门禁

- Excel `01_PRD总表` 第 80 行、`03_M2-M3扩展交付` 第 16 行、PAGE-110/111、套餐与额度 API、
  Plan/QuotaBucket、`quota.exhausted`/`trial.expired` 与 TC-0157/0158 共同确认本条为
  `PRD-079 | M2-企业增强 | P2 | 扩展层 | 套餐计费 | 套餐、试用与额度类型`。
- 冻结设计见 `docs/prd/PRD-079-plan-trial-quota-types.md`。第 13、14 节保存两轮编码前自审；第 15、16 节
  保存两轮实现后账务、并发、恢复、权限、隐私、范围和兼容复核及修正。
- 每个业务事实均 tenant-qualified。四个公开接口逐请求执行后端 Session/RBAC，写事务内再次验证权限指纹；
  前端导航与按钮只负责体验。成功和错误均使用统一 request_id/trace_id 与稳定错误码。
- 一条 Usage 仍只对应一条 canonical FinOps charge；额度消费通过 Settlement/Allocation 记录
  `trial/free/gift/paid` 分摊，overage/unallocated 独立追踪，不伪装成 paid。
- Provider Key、Token、Authorization、Prompt、Response、调整 reason 和幂等键明文不得进入日志、
  Webhook 或验收证据。默认上游保持智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务
  与客户本地 OpenAI-compatible Endpoint；OpenRouter、硅基流动仍只是竞品与迁移兼容对象。

## 2. 实现后两轮自审结论

第一轮聚焦账务守恒、并发和恢复，关闭了以下问题：Provider 风险不能冒充正式 charge；Fallback actual 与
主路由 exposure 必须分离；真实成本超过预留时不能永久卡住 Usage spool；恢复任务必须同时处理
`active/exposure_pending`；spool v7 必须兼容 v1-v6 并继续约束 Batch/Output Safety；Settlement trigger
必须绑定 canonical 金额；损坏 outbox source 必须隔离失败，不能形成全局队首饥饿。

第二轮聚焦权限、隐私、范围和兼容，确认 adjustment reason 只留在权威数据库且 Audit 仅保存摘要；
scheduler/Gateway 到期不伪造用户 Audit；FinOps DTO/Webhook 同步增加安全分摊摘要；四项权限由依赖层和
事务内实时指纹双重校验；PRD-069/074/075/076 与既有 spool/FinOps 唯一 owner 均保持兼容；本条没有支付、
账单争议、退款或结算暂停实现。两轮复核后无未关闭 P0/P1。

## 3. 已通过的自动化门禁

以下分组使用互不相同或明确列出的测试文件；它们不能相加冒充一次根级全量测试。

| 验证项 | 最终结果 | 主要覆盖 |
| --- | ---: | --- |
| PRD-079 Python 专项 | `73 passed` | Shared 合同、Control 导航/API、0072、Gateway 额度、Runtime 合同 |
| Console 全量 | `792/792 passed` | 全 Console 单元、owner 生命周期、PAGE-110/111、Dialog 与权限投影 |
| Browser 合同 | `9/9 passed` | 固定路径/端口、CDP owner、loopback、事实 exact-set、隐私和清理 |
| OpenAI-compatible 回归 | `28 passed` | Chat/Embedding/Stream、Fallback、Budget、Quota、Policy、Usage 与失败追踪 |
| FinOps/Webhook 回归 | `26 passed` | FinOps 5、Webhook 8、resume 10、dead-letter 3 |
| 直接受影响兼容回归 | `29 passed` | FinOps spool、意图顺序、Batch、A/B、Usage spool、Cache settlement |
| 静态门禁 | 通过 | Console lint `247 files`；新文件完整 Ruff；33 个改动 Python 非 E501 Ruff 与编译 |
| Git whitespace | 通过 | `git diff --check` |

OpenAI-compatible 28 条因本机 x64 Python 仿真较慢，按四个互不重叠的 7 条分片实际执行，四片均通过。
一次并行运行仅因执行器 10 分钟上限中止，没有计入通过记录；随后串行分片得到上表最终结果。

## 4. 迁移与 Runtime final

权威目录：`.artifacts/prd079-runtime/run-agent4-b72e1d/`。

| 阶段 | migration count/version | integrity | 外键违规 |
| --- | --- | --- | ---: |
| fresh | `72/0072` | `ok` | `0` |
| repeat | `72/0072`，applied 0 | `ok` | `0` |
| down | `71/0071` | `ok` | `0` |
| re-up | `72/0072` | `ok` | `0` |

- 从空 SQLite 实际执行，不使用预制数据库；0071 正向升级、默认/自定义 RBAC 保真、业务事实 down guard、
  trigger/owner/append-only 与四阶段完整性均由迁移专项覆盖。
- 真实 Control/Gateway/Provider loopback 均通过。Control 创建 Plan、调整四种额度并证明后端撤权拒绝；
  Gateway 在额度耗尽时返回 402 且不调用 Provider，成功请求形成 1 Usage、1 FinOps、1 Settlement、
  1 Allocation，失败请求保持可追踪。
- Control 重启后 trial scheduler 与 quota outbox 均恢复，最终 `trial.expired` 事件 1；受控端口释放、owner
  进程停止、SQLite sidecar 0、敏感明文命中 0。
- `runtime-acceptance.json` SHA-256：
  `c09609cb3e03941721a4ea4e8c023136b543cec57b8d5efd9aa3ea649ec5ae5d`。

## 5. Browser final

权威目录：`.artifacts/prd079-browser/main-browser-final-20260814-i/`。

- `status=passed`，桌面 `1280x720` 与移动 `390x844` 实际操作 PAGE-110/111；Plan 保存、CAS、四类额度
  调整和报表拆分均走真实页面与后端 API。
- Plan/Quota 原生 Dialog 的初始焦点、正反 Tab wrap、idle Escape 关闭、busy Escape 阻断、按钮 disabled、
  完成后焦点归还和 reason 清理均通过。finance_admin 动态撤销四项 PRD-079 权限后，后端拒绝且迟到响应、
  DOM、导航和登出存储均清理。
- 四张 PNG 已人工复核为非空、内容完整、无控件/文字重叠。桌面额度表仅使用表格自身局部滚动；两个视口
  的页面级横向溢出、文字溢出和重叠计数均为 0。
- 外部请求、非预期 Console error 和 Page error 均为 0；Plan 1、Bucket 4、Adjustment 4，撤权后四项
  PRD-079 权限剩余 0；迁移为 `72/0072`、`integrity_check=ok`、外键违规 0。
- `browser-acceptance.json` SHA-256：
  `82a5e2b63dbc0f4aefc9c38cd5bf74e25663643a278b0aeeda4c2c4fe57e72cd`。
- `sha256-manifest.json` SHA-256：
  `94c6a74fb0a223f07e7a035576d46dc1ef31d3be3c3755bba5e8a59b811705a7`。

`main-browser-final-20260814-h` 在首个登录 DOM 等待处超时，没有发布证据且不计入结果；此前 `-g` 虽通过，
但 Browser Harness 完成 Ruff 纯格式修正后源码哈希变化，因此由 `-i` 重新运行并取代，只有 `-i` 是提交白名单。

## 6. 安全、禁止文件与范围门禁

- Runtime 扫描 8 个受控文件、Browser 分别扫描 work 10 个和 staging 5 个文件，敏感明文命中均为 0。
- 提交白名单不包含数据库、WAL/SHM/journal、日志、Browser profile/cache、失败诊断批次、Provider Key、
  Token、Authorization、Prompt、Response、调整 reason 或幂等键。
- 迁移、Runtime、Browser JSON、文档和 Excel 均保持 `all_108_prds_complete=false`。
- 最终提交只包含 PRD-079 生产代码、测试、迁移、文档和上述两个权威证据目录，不包含 PRD-080 实现。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 81 行、`03_M2-M3扩展交付` 第 17 行、PAGE-112、账单争议 API、
BillingDispute/AdjustmentRecord、`billing_dispute.created` 与 TC-0159/0160 共同确认，下一条权威未完成项为
`PRD-080 | M2-企业增强 | P2 | 扩展层 | 账单争议 | 账单争议与调整流程`。本次只确认，不实现。
