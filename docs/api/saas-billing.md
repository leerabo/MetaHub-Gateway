# SaaS Billing API

PRD-098 建立 MetaHub 平台拥有的 SaaS 商品目录、Workspace 订阅、试用与不可变权益快照。本 API 与
PRD-079 的 tenant-owned BYOK `/plans`、`/quota-buckets` 完全分离；客户端不得把两类套餐互换或合并。

## 凭据域

- `GET /catalog/plans` 为公开安全投影，不解释 Workspace、Platform、Support 或 Legacy Bearer 身份。
- `/subscriptions*` 只解释 active WorkspaceSession，且成员职责必须为 `owner` 或 `finance_admin`。请求不得
  携带 Authorization、PlatformSession 或 SupportSession。
- `/platform/plans*` 只解释 PlatformSession。GET 要求 `platform.catalog.read`，POST 要求
  `platform.catalog.write`；WorkspaceSession、SupportSession 与 Bearer 均不能替代。
- Workspace 与 Platform 写请求必须来自允许的唯一 Origin，并分别使用当前域 CSRF Cookie 与
  `X-CSRF-Token`。认证响应均为 `Cache-Control: no-store, private`。

## 接口

| 方法 | 路径 | 请求边界 | 结果 |
| --- | --- | --- | --- |
| GET | `/catalog/plans` | 无 query；公开读取 | active catalog 的 published 版本与安全权益定义 |
| GET | `/subscriptions/current` | Workspace finance；无 query | 当前订阅、试用、权益快照与最近订单；无订阅时各事实为空 |
| POST | `/subscriptions` | Workspace CSRF、`Idempotency-Key`、严格 JSON | 创建订阅意图；免费/试用可激活，付费保持 pending |
| POST | `/subscriptions/current/change-plan` | Workspace CSRF、`If-Match`、`Idempotency-Key`、严格 JSON | 登记升级或下周期降级，不删除资源 |
| GET | `/platform/plans` | Platform catalog read；无 query | draft/published/retired 全版本目录 |
| POST | `/platform/plans` | Platform catalog write、Platform CSRF、严格 JSON | 追加一个 draft 或 published 版本 |

`If-Match` 必须是带引号的正整数，例如 `"3"`。`Idempotency-Key` 为 16..128 字符，首字符为字母或数字，
其余字符只允许字母、数字、`.`、`_`、`:`、`-`。相同 key 与相同规范意图返回原结果；相同 key 配不同
意图返回冲突。

## Workspace 请求

创建订阅正文固定为：

```json
{
  "plan_version_id": "spv_00000000000000000000000000000000",
  "start_trial": true
}
```

变更套餐正文只包含 `plan_version_id`。客户端不能提交 workspace、tenant、价格、币种、权益、状态、周期、
订单或支付字段。计划版本必须来自当前公开目录；服务端在同一事务中锁定 published 版本并复制规范金额与
权益事实。

免费版本生成 `not_required` 订单；有可用试用策略时订阅进入 `trialing` 并生成 Trial 与
EntitlementSnapshot；其他付费意图保持 `pending/pending_payment`，直到 PRD-103 的支付成功事实推进。
套餐变更保留当前权益，写入 `pending_plan_version_id/pending_effective_at`。现有资源不会被删除或停用。

试用到期由固定轮询、单轮最多 50 项的后台 scheduler 与请求守卫共用同一个幂等状态机：旧 Trial 标记
expired，旧权益按原到期时间结束。若存在 active catalog 的 published 免费版本，服务端按
`display_order/plan_code` 选择确定性回落目标，并追加 `not_required` 订单、新权益快照和
`subscription.trial_expired` 事件；若没有免费版本，订阅进入 expired，不能继续使用付费权益。请求守卫
保留为兜底，因此 scheduler 延迟或暂时不可用不会延长已到期权益。

## Platform 请求

创建目录版本正文固定包含：

```json
{
  "plan_code": "team",
  "display_name": "Team",
  "display_order": 20,
  "billing_period": "monthly",
  "currency": "CNY",
  "unit_price": "99.00",
  "trial_days": 14,
  "entitlements": {
    "gateway_requests_per_month": 10000,
    "workspace_seats": 5,
    "model_aliases": 10,
    "retention_days": 30,
    "priority_support": true
  },
  "publish": true
}
```

金额必须是两位小数的十进制字符串；数据库保存币种小单位整数。published PlanVersion 不允许更新或删除，
后续改价或改权益必须追加新版本。权益字段为固定 allowlist，未知能力或越界整数会失败关闭。

## 响应与错误

目录响应返回 `plans/request_id/trace_id`。每个 catalog 包含稳定 plan code、状态、顺序、revision 和版本；
每个版本包含周期、币种、价格、试用天数、权益定义与 `entitlements_sha256`。

订阅响应返回可空的 `subscription/trial/entitlement/latest_order`、`idempotent_replay`、`request_id` 和
`trace_id`。服务端不会返回 tenant、账号、Cookie、Session、支付材料或自由文本合同。

- `saas_catalog.request_invalid/not_found/conflict/unavailable`：目录请求、状态或运行依赖不合法。
- `subscription.request_invalid/not_found/conflict/revision_conflict/idempotency_conflict/unavailable`：订阅
  意图、CAS、幂等或运行状态不合法。
- `workspace_session.*`、`platform.*`：凭据域、CSRF、Origin、Session 或权限失败。

错误仅返回固定安全文案和关联 ID，不暴露 SQL、表名、HMAC、Cookie、TOTP、支付资料或客户正文。
