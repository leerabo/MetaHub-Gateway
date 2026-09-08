# PRD-098 SaaS 套餐目录、试用、订阅与权益

状态：实现完成，测试通过
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-090、PRD-093、PRD-095、PRD-097、PRD-079

## 1. 目标与边界

本条建立平台拥有的 SaaS 套餐目录、不可变套餐版本、Workspace 订阅、试用和权益快照。它不把
PRD-079 的 tenant-owned BYOK Plan/QuotaBucket 改名复用：后者仍负责 Workspace 内部成本与额度，
本条负责 MetaHub 向客户出售的产品能力、席位和平台调用额度。降级只改变未来权益，不能删除、停用或
改写 Workspace 资源；超出新权益的既有资源进入只读超额状态，由后续显式处置。

本条不接支付渠道、不生成发票、不扣款或退款。创建订阅只生成待履约 OrderRecord；免费套餐和试用可按
冻结规则直接激活，付费订阅由 PRD-103 的支付成功事实激活。

## 2. 所有权与凭据

- `GET /catalog/plans` 是无登录或 GlobalSession 可读的公开安全投影，只返回 active published 版本。
- `/subscriptions*` 只解释 WorkspaceSession，要求 active owner 或 `finance_admin`；workspace_id 从 Session
  恢复，正文不得提交 tenant、Workspace、价格或权益。
- `/platform/plans*` 只解释 PlatformSession，要求 `platform.catalog.read/write`；Workspace Cookie、Legacy
  Bearer、Virtual Key 与 SupportSession 均不能替代。
- Platform operator 只能管理目录，不能代客户创建订阅；SupportSession 不能读商业合同。

## 3. 0090 数据模型

- `saas_plan_catalogs`：稳定 plan code、可见状态、展示顺序和 revision；不保存当前价格捷径。
- `saas_plan_versions`：只追加版本，保存币种、周期、价格微单位、试用策略、席位与能力定义 JSON 的规范
  hash；published 后不可 UPDATE/DELETE。
- `workspace_subscriptions`：workspace/tenant 复合 owner、plan version、状态、周期边界、CAS revision；每个
  Workspace 最多一个 current subscription。
- `subscription_trials`：workspace、subscription、trial policy version、开始/结束时间和唯一 cycle；只追加。
- `entitlement_snapshots`：订阅状态转换时生成的不可变权益，绑定 plan version、来源订单和生效区间。
- `subscription_order_records`：幂等订阅意图，保存规范金额/币种/plan version，不保存支付凭据。

所有金额使用币种定义的小单位整数；不得跨币种求和。权益 JSON 使用固定 schema、排序键和 SHA-256，
服务端按 allowlist 解析，未知能力失败关闭。外键同时绑定 workspace_id 与 tenant_id。

## 4. 状态与事务

订阅状态固定为 `pending/active/trialing/past_due/canceled/expired`。创建时在一个事务内锁定 published
PlanVersion、写 OrderRecord 与 Subscription；免费/试用同时写 EntitlementSnapshot，付费保持 pending。
相同 Idempotency-Key 和规范意图重放原结果，不同意图 409。

`change-plan` 要求 If-Match。升级可立即生成新订单；降级只登记下周期生效的 pending change，当前快照
保持到 period end。任何转换都只追加新 snapshot，不修改历史版本。试用到期由请求守卫与 scheduler 幂等
收敛；无付费成功事实时回落免费版本。支付、退款和 dunning 只通过后续受约束内部命令推进状态。

## 5. HTTP 合同

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `GET /catalog/plans` | public/global | published 目录安全投影 |
| `POST /subscriptions` | Workspace owner/finance | 幂等创建订阅意图 |
| `GET /subscriptions/current` | Workspace finance | 当前订阅与当前权益 |
| `POST /subscriptions/current/change-plan` | Workspace owner/finance | CAS 升降级，不删资源 |
| `GET /platform/plans` | platform catalog/billing | 全版本目录 |
| `POST /platform/plans` | platform catalog write | 创建 draft 或发布不可变版本 |

响应均返回 request_id/trace_id；认证响应 `no-store, private`。严格 JSON 拒绝重复键、未知字段、客户端
owner、JSON number 金额和超限结构。平台写要求 Origin、CSRF、If-Match/Idempotency-Key 和不可变审计。

## 6. Console 与性能

Workspace Console 提供套餐比较、当前订阅、试用倒计时、计划变更和权益明细；Platform Console 提供紧凑
目录版本表和发布面板。390/1280/1440 无横向溢出。目录 warm p95 `<200 ms`，当前订阅 `<250 ms`，写事务
`<500 ms`；日志与证据不含合同自由文本、Cookie、TOTP、Secret 或支付材料。

## 7. 专项测试

覆盖 0089->0090/fresh/down/up、版本不可变、跨 Workspace owner、公开目录裁剪、三类凭据负矩阵、试用
并发到期、升降级 CAS/幂等、降级不删资源、权益 hash、付费未支付不激活、动态撤权、Console 三视口和
敏感 canary 0 命中。

## 8. 第一轮自审：领域与生命周期

结论：通过，P0/P1 已修正。

- P0：复用 PRD-079 `plans` 会把客户 BYOK 额度与平台商品混为一个授权 owner。修正为独立 `saas_*`
  平台目录和订阅表，只以明确映射读取 Workspace owner。
- P0：降级时删除超额资源会造成不可逆客户数据损失。修正为未来 snapshot 生效和只读超额标记。
- P0：把 draft 版本原地改价会改写历史订单。修正为 published PlanVersion 永久不可变。
- P1：付费订阅在无支付事实时直接 active 会形成免费履约。修正为 pending，后续只接受 PRD-103 内部命令。

## 9. 第二轮自审：权限、金额与恢复

结论：通过，P0/P1 已修正。

- P0：同一路由接受 PlatformSession 与 WorkspaceSession 会跨域提权。修正为路径与 Cookie 域严格分离。
- P0：按 JSON 浮点保存价格会产生对账漂移。修正为原币种小单位整数和规范十进制文本。
- P1：试用只靠 scheduler 到期会留下权益窗口。修正为读写守卫与 scheduler 共用幂等状态机。
- P1：回滚 0090 若存在订阅事实会丢失商业记录。修正为仅 pristine down，运行时回退保留 deny/read overlay。

## 10. 2026-09-09 跨数据库表结构检查补充设计

PRD-099 原生回归在创建套餐时复现 `relation sqlite_schema does not exist`，尚未进入计量。
本批将 SaaS 七表检查交给已有 `tables_exist`，与计量八表检查一起适配，不增加 schema 缓存或改权限。
第一轮自审：必须保留七个必需表、缺表错误码和调用者事务；不能靠数据库名猜测表是否存在。
第二轮自审：仅当前 schema 的真实表满足要求，view、temp 或其他 schema 不可替代；数据库故障不能
返回成功。使用 SQLite 逐表缺失/回滚测试、原生当前 schema 故障和套餐创建/公开查询验证。

订阅读取另依赖 SQLite 隐含 `rowid`，原生表未提供相应序列。该排序涉及同时间订单的真实先后，
不能随意替换成随机 ID 或物理 tuple 地址。本次不声称原生完整订阅流程通过，也不通过注入已订阅
前置数据绕过该缺口。原生验收限定套餐创建/查询和 schema 检查；完整订阅留待独立迁移修复。
