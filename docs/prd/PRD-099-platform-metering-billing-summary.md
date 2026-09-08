# PRD-099 平台计量、配额结算与账单摘要

状态：已完成
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-098、PRD-051、PRD-079、PRD-080

## 1. 目标与边界

本条从不可变 Usage/FinOps 事实异步生成 SaaS 平台计量、周期配额结算和账单摘要。平台 Meter 描述客户向
MetaHub 购买并消费的计费量；PRD-079/051 继续描述 Workspace BYOK 内部额度和成本，两者不能共享 ledger
owner、调整表或权限。计量只读取 Token/请求数/受支持存储等结构化事实，绝不复制 Prompt、Response、
文件正文或 Provider Secret。

## 2. 0091 数据模型

- `platform_meter_definitions`：稳定 code、unit、aggregation、source schema version、effective interval。
- `platform_meter_readings`：只追加、workspace owner、source fact/type/id/hash、quantity integer、occurred_at；
  source 唯一约束保证 exactly-once projection。
- `platform_billing_cycles`：workspace、subscription、周期、状态与 watermark。
- `platform_quota_allocations`：EntitlementSnapshot 到 cycle/meter 的不可变授予量。
- `platform_billing_summaries`：按 meter 的冻结聚合、超额、原币金额和 source watermark/hash。
- `platform_billing_adjustments`：平台账务角色的只追加正负调整，双人批准引用留给 PRD-104。
- `platform_meter_outbox/checkpoints`：有租约的异步投影和恢复水位。

quantity 和金额均为整数；不同 meter unit、币种或 cycle 禁止混加。Reading 不保存任何请求正文，source hash
绑定 tenant/workspace/request/usage owner。迟到事实进入仍 open 的 cycle；已 closed 周期只追加 adjustment，
不重写 summary。

## 3. 处理与一致性

Usage 提交只写既有 canonical fact/outbox，不等待 SaaS 计量。worker 以 source 唯一键写 Reading，并在同一
事务推进 checkpoint。Cycle summary 可重复重建，hash 不同则失败并进入 reconciliation_required。配额只
从当期 EntitlementSnapshot 分配，不从当前套餐反推历史。

缺失/坏 owner、未知 meter、负数量或水位倒退均失败关闭；不能把无价格解释为零费用。调整要求
PlatformSession `platform.billing.adjust`、Idempotency-Key、固定 reason code、原始 summary revision 和
不可变平台审计；平台员工不能改原始 Usage 或 Reading。

## 4. HTTP 合同

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `GET /billing/summary` | Workspace finance | 当前/指定已知周期的安全聚合 |
| `GET /billing/usage` | Workspace finance | meter 粒度列表，不返回调用正文 |
| `GET /platform/billing/summary` | platform_billing | 跨 Workspace 裁剪汇总与游标 |
| `POST /platform/billing/adjustments` | platform billing adjust | 幂等只追加调整 |
| `GET /billing/reconciliation-export` | Workspace finance | 有界 CSV、owner-bound 短时下载 |

Workspace 路由只解释 WorkspaceSession；平台路由只解释 PlatformSession。游标绑定主体、角色 revision、
筛选和方向。CSV 固定列、UTF-8、公式注入转义、`nosniff/no-store`，不含 Prompt/Response、邮箱或 Secret。

## 5. Console、性能与恢复

账单页以紧凑周期表、meter 明细和配额/超额分栏呈现；平台页提供周期状态、差异状态和调整队列。10 万
Workspace 汇总依赖 cycle/status/updated_at 复合索引，limit=100 warm p95 `<500 ms`；单 Workspace
`<300 ms`。0091 仅 pristine down；代码回退停止新投影但保留 Reading/checkpoint，不允许回退后重复收费。

## 6. 专项测试

覆盖 0090->0091/fresh/down/up、Usage 投影 exactly-once、重放/乱序/迟到、周期关闭、权益版本冻结、跨域
ledger 禁混、调整 CAS/幂等/撤权、CSV 注入、凭据负矩阵、10 万 Workspace 查询计划、敏感 canary 和
PRD-051/079/080 回归。

## 7. 第一轮自审：账本与异步一致性

结论：通过，P0/P1 已修正。

- P0：同步计量会让外部商业系统拖慢 Gateway。修正为 canonical Usage 后异步投影。
- P0：复用 FinOps charge/QuotaBucket 会把内部成本当客户账单。修正为独立 MeterReading 与 Allocation。
- P0：worker 重放可能重复计费。修正为 source owner 唯一键、事务 checkpoint 与可重建 summary hash。
- P1：已关账周期接收迟到事实会改写历史。修正为追加 adjustment 和 reconciliation 标记。

## 8. 第二轮自审：隐私、权限与导出

结论：通过，P0/P1 已修正。

- P0：平台汇总若复用 Usage DTO 会暴露模型调用明细。修正为 meter allowlist 投影和角色裁剪。
- P0：Workspace 与 Platform 共用 Bearer 会允许跨域查询。修正为独立 Cookie/Session 解释边界。
- P1：CSV 可形成公式注入或长期下载链接。修正为固定列转义和 tenant-bound 短 TTL 令牌。
- P1：未知价格按 0 结算会漏费。修正为 unavailable/reconciliation_required，禁止静默归零。

## 9. 2026-09-09 计量事实时间与失败原子性补充设计

现场核对：本地试用空间有唯一 Workspace binding，但没有套餐、订阅或权益快照。首条 outbox 已
`owner_invalid`，后续全局水位停止。这不是允许倒填商业合同、把未知价格计为零或跳过失败记录的依据。
本批不修改该历史事实，也不新增模型调用；历史计量恢复仍需独立的有依据补偿合同。

同一投影事务修复两项：

- 查询显式将 canonical Usage 的 `created_at` 命名为 `usage_created_at`；周期、历史权益和 Reading
  `occurred_at` 仅使用该字段。outbox 入队时间和 worker 当前时间不参与历史归属。
- 完成 outbox claim 后建立局部 savepoint。业务投影 ApiError 先撤销本条新增的 cycle、allocation、
  reading、adjustment 及 cycle 状态变化，再提交 failed outbox。数据库异常仍回滚整个外层事务。
  成功路径仍在同一外层事务完成四个 Reading、outbox 和 checkpoint，无新迁移、重试 API 或跳号规则。

第一轮自审（领域与一致性）：时间重名会跨权益边界误归属；已关闭历史周期必须追加 adjustment，
不能进入入队日的新周期。失败后的部分 Reading 不可删除，故须在首次错误提交前原子撤销。
保留 claim 尝试次数和错误码，但不得保留失败投影的业务副作用。结论：按上述边界实现。

第二轮自审（恢复与安全）：仅回滚 savepoint 内本条投影，不能撤销既有历史 summary 或其他成功事实；
savepoint/错误落盘本身失败时交给外层 rollback。断点不前进，后续用量继续 fail closed，不以补造权益
掩盖缺失订阅。测试复用隔离数据库，不改受保护基线或运行库；报告不含请求正文、Secret 或 Cookie。
结论：定向红绿测试后统一执行 SQLite、原生 PostgreSQL 与相邻账单流程回归，独立审计和提交。

原生回归发现套餐及计量检查使用 SQLite catalog；二者改用已有 `tables_exist`，保留七表/八表完整性、
当前 schema 边界和事务归属，不改模型或金额。两轮补充自审确认：不接受 view/temp/其他 schema 替代，
无权限快照缓存，无新迁移。原生订阅还有未闭环的隐含 rowid 排序，Usage 导出序列表的主键缺少分配器。
本次原生验收限定 schema 边界及空投影事务；保留失败全流程与待恢复用例，不注入 Usage 或跳过触发器。
SQLite 六项投影断言已执行，但不等同于原生事务或真实平台登录通过。
