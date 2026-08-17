# PRD-087 渠道伙伴与白标门户设计

状态：**已完成并验收**（2026-08-17）。两轮编码前自审、实现、迁移、专项、必要回归和真实 PAGE-119
浏览器验收均已闭环；最终证据与下一项依据见 `docs/testing/PRD-087-tests.md`。

总体状态：`all_108_prds_complete=false`。

## 1. 权威需求与结论

本设计交叉核对以下来源，不按编号猜测范围：

- Excel `01_PRD总表!88` 与 `03_M2-M3扩展交付!24`：伙伴登录、客户列表、白标配置、
  用量汇总、分润报表、交付包下载；不做复杂结算支付。
- Excel `04_页面清单!120`：PAGE-119 伙伴门户。
- Excel `05_API接口清单!395:400`：API-394..399 共同编码
  `GET /partners/customers` 与 `GET /partners/commission`。
- Excel `06_数据对象!119:121`：OBJ-118 Partner、OBJ-119 PartnerCustomer、
  OBJ-120 CommissionRecord，全部业务对象默认携带 `tenant_id`。
- Excel `07_Webhook事件!100`：EVT-099 `partner.customer_created`。
- Excel `08_验收用例!174:175`：TC-0173 主流程、TC-0174 隔离/分账/异常。
- README、PRD-086 测试记录和目录说明：PRD-086 已完成，PRD-087 是下一条权威未完成项。
- 架构文档：管理 API 必须后端租户鉴权；Usage、Budget、Webhook 等按租户隔离；
  复杂报表异步处理；控制面不承载高频模型流量。

结论：本次只实现 PRD-087，不修改 Gateway Provider 默认目录，不引入 OpenRouter/硅基流动为
默认上游，不实现硬件适配，也不启动 PRD-088。

## 2. 业务模型与范围

### 2.1 伙伴与客户归属

v1 固定“一条 active Partner 对应一个现有 MetaHub tenant”。伙伴成员继续通过已有
`POST /auth/login` 登录，不新增弱化认证的独立登录栈。伙伴门户权限由当前租户动态 RBAC 矩阵授予，
前端导航只改善体验，不能替代后端校验。

PartnerCustomer 不直接指向其他 tenant，而是把当前伙伴租户内的一个既有 Project 映射为渠道客户。
因此用量、成本、失败请求和分润来源始终满足：

`AuthContext.tenant_id = Partner.tenant_id = PartnerCustomer.tenant_id = Project.tenant_id = Ledger.tenant_id`

该模型不创建跨租户账本查询例外。每个 Project 在当前 tenant 内最多归属一个 PartnerCustomer，
避免同一调用被重复计入多个客户或重复计算分润。

### 2.2 本期交付

- 复用现有 Session 登录与动态 RBAC，交付伙伴专用导航和 PAGE-119。
- 首次创建 Partner 资料；展示并更新白标名称、品牌色和支持信息。
- 从当前 tenant 的既有 Project 创建客户映射，查看、筛选并终止映射。
- 按客户聚合 RequestLog、UsageLedger 和 FinOps 正式账本，展示请求、失败、Token 与成本。
- 异步把客户生效后的正式 usage charge 物化为不可变 CommissionRecord，按币种生成报表。
- `GET /partners/commission` 同时支持 JSON 和 UTF-8 CSV 下载。
- 下载不含密钥的 JSON 交付包。
- 创建客户时持久发布 EVT-099 `partner.customer_created`。

### 2.3 明确非目标

- 不做付款、提现、开票、税务、对账确认、汇率换算或复杂结算工作流。
- 不读取其他 tenant 的客户、Project、RequestLog、Usage 或 FinOps 数据。
- 不上传或代理远程 Logo，不开放自定义域名/DNS/TLS，避免 SSRF、Cookie 泄露和证书生命周期扩散。
- 不复制 Provider Key、Virtual Key、Token、Prompt、Response、上游异常正文或原始 Usage JSON。
- 不在 Gateway 热路径增加伙伴业务查询；CommissionRecord 由 Control 异步物化。

## 3. 权限与登录

Shared RBAC 目录升级到 revision 22，新增六个权限：

| 权限 | 风险 | 作用 |
| --- | --- | --- |
| `partner_portal.read` | read | 读取当前租户 Partner、安全客户聚合和 JSON 分润摘要 |
| `partner_portal.configure` | critical | 首次创建当前租户唯一 Partner；只对 admin 可授权 |
| `partner_customer.manage` | critical | 创建或终止当前 Partner 的 Project 客户映射 |
| `partner_branding.manage` | write | 更新白标名称、颜色和支持信息 |
| `partner_commission.export` | critical | 导出当前 Partner 的分润 CSV |
| `partner_delivery.download` | critical | 下载当前 Partner 的脱敏交付包 |

现有 RBAC template 的数据库合同只表示 `default_granted=1`，不支持“进入角色上限但默认关闭”。因此
`partner_portal.configure` 只进入不可变 admin；其余五项进入 admin 与最小 `viewer` 角色，迁移为两者自动
扩展 exact matrix。伙伴成员使用既有 viewer 账号登录；developer/finance_admin/security_admin/ops_admin 不获得
伙伴业务权限。Partner 未配置时 viewer 仍只能得到同形 404，无法读取 Project、账本或其他租户；每次请求继续
从数据库恢复 live matrix，撤权或角色变化后下一次请求立即失败关闭。

PAGE-119 只有后端导航返回 `partner-portal` 且 `partner_portal.read` 存在时才加载。写按钮还需对应
权限镜像，但后端必须再次校验。Partner 未配置时，普通读取返回同形 `partner.not_configured`；只有具备
`partner_portal.configure` 的用户可看到并调用初始化动作。

## 4. 数据库设计

正向迁移编号为 `0080_channel_partner_white_label_portal.sql`，反向迁移同名 `.down.sql`。

### 4.1 partners（OBJ-118）

| 字段 | 约束与含义 |
| --- | --- |
| `partner_id` | `par_` + 32 hex；全局和 `(tenant_id, partner_id)` 唯一 |
| `tenant_id` | 非空 FK tenants；每租户最多一行 active/disabled Partner |
| `partner_code` | 规范小写业务码，租户内唯一，创建后不可变 |
| `display_name` | 1..120 UTF-8 bytes，不允许控制字符 |
| `portal_name` | 1..80 UTF-8 bytes，PAGE-119 首屏品牌名 |
| `brand_primary_color` | 固定 `#[0-9A-F]{6}`，防止 CSS 注入 |
| `support_text` | 可空，最多 160 UTF-8 bytes；只作纯文本渲染 |
| `default_commission_rate_bps` | v1 服务端固定 1000，不从公开请求读取，不提供修改 API |
| `status` | `active/disabled`；v1 不提供 disable API |
| `revision` | 从 1 开始的整数乐观版本；branding 每次有效更新递增 1 |
| `idempotency_key_sha256/payload_sha256` | tenant 内 Partner 初始化幂等；不保存原始 key |
| `created_by_user_id` | 当前 tenant User 复合 FK |
| `operation_request_id/operation_trace_id` | 最近成功写操作链路，不含 Token 或正文 |
| `created_at/updated_at` | UTC Z 整秒；branding 更新只推进 updated_at |

### 4.2 partner_customers（OBJ-119）

| 字段 | 约束与含义 |
| --- | --- |
| `partner_customer_id` | `pcu_` + 32 hex |
| `tenant_id/partner_id` | 复合 FK Partner，禁止 owner 漂移 |
| `project_id` | 当前 tenant Project 复合 FK；tenant 内唯一映射 |
| `customer_code` | 规范小写业务码，Partner 内唯一，创建后不可变 |
| `display_name` | 1..120 UTF-8 bytes，纯文本展示 |
| `status` | `active/disabled`；只允许 active -> disabled |
| `linked_from_ledger_sequence` | 创建事务内 FinOps 高水位；此前费用不参与分润 |
| `commission_cutoff_sequence` | 终止事务内高水位；active 时必须 NULL |
| `commission_rate_bps` | 从 Partner 固定费率复制，历史不随 branding 更新变化 |
| `revision` | 从 1 开始；display_name 或终止每次有效更新递增 1 |
| `idempotency_key_sha256/payload_sha256` | tenant + Partner 内幂等与冲突判定，不存原始 key |
| `created_by/disabled_by_user_id` | 当前 tenant User 复合 FK |
| `operation_request_id/trace_id` | 最近成功写操作链路；终止另存 disable request/trace |
| `created_at/updated_at/disabled_at` | UTC Z 整秒，状态与时间严格对应 |

### 4.3 commission_records（OBJ-120）

每条记录只对应一个已过账 `finops_ledger_entries` usage charge：

| 字段 | 约束与含义 |
| --- | --- |
| `commission_record_sequence` | AUTOINCREMENT，只增游标 |
| `commission_record_id` | 由 tenant + PartnerCustomer + source entry 确定性派生 |
| `tenant_id/partner_id/partner_customer_id` | 复合 owner FK |
| `source_finops_ledger_entry_id` | 同 tenant usage charge FK；每客户唯一 |
| `source_request_id/source_trace_id` | 可追踪到失败/成功调用链；不复制请求正文 |
| `customer_code_snapshot/display_name_snapshot` | 创建时客户安全快照，终止后报表仍可解释 |
| `project_id` | 来源 Project，必须等于 PartnerCustomer Project |
| `amount_micros/currency` | 正式账本原币整数微元，CNY/USD 不换汇 |
| `commission_rate_bps` | PartnerCustomer 费率快照 |
| `commission_amount_micros` | 整数 half-up；用商/余数公式避免 64 位乘法溢出 |
| `input/output/total/cache_tokens` | 来源账本安全用量摘要 |
| `estimated` | 来源 usage 是否估算 |
| `status` | v1 固定 `accrued`，不表示已支付 |
| `source_created_at/created_at/updated_at` | 来源发生与物化时间；记录创建后不可更新/删除 |

### 4.4 partner_commission_cursors

每个 PartnerCustomer 一条公平物化游标：`tenant_id`、客户/Project owner、`status`、
`last_scanned_ledger_sequence`、generation、lease owner/token/expiry、next_run_at、failure_count、
last_error_code 和时间。claim 使用短 `BEGIN IMMEDIATE` + CAS；扫描与提交分阶段；每批最多 200 条。

创建客户时游标从 `linked_from_ledger_sequence` 开始。终止事务先读取当前 FinOps 高水位并以同一个
`BEGIN IMMEDIATE` CAS 冻结 cutoff；Scheduler 即使已持有旧 active claim，也必须在提交时重读 status/cutoff，
最多处理 `<= cutoff` 的已发生账本，之后才进入 `complete`，`> cutoff` 永不计佣。唯一键和确定性 ID保证
崩溃重试不会重复分润。空轮询先只读，不长期占用 SQLite writer。

### 4.5 partner_portal_operations

PRD-087 不重建现有 `audit_logs`。该表挂接了多个历史 PRD 的强 owner trigger，扩展其 CHECK 会把本条变更
放大到不相关业务链路。改用 tenant-qualified、append-only 的领域操作表记录六类动作：partner created、
branding updated、customer created/disabled、commission exported、delivery downloaded。每行只保存安全
对象 ID、actor、request/trace、revision、row/count/currency count、filter/content/branding hash 和 UTC 时间；
不保存名称、支持文本、CSV/交付包正文或幂等原值。数据库 trigger 按 action 冻结允许字段并禁止更新/删除。

### 4.6 partner_customer_outbox（EVT-099）

客户创建与 source outbox 在同一事务写入。outbox 保存 tenant、PartnerCustomer、稳定 event ID、
原始 actor/request/trace、固定安全 payload、订阅快照、lease/generation、attempt、next retry 与固定错误码。
首次或恢复 worker 都按稳定 event ID at-least-once 投递，最多 10 次；不得信任 outbox JSON 替代权威
PartnerCustomer 回读。

EVT-099 payload exact set 固定为七个字段：`schema_version=1.0`、`partner_id`、
`partner_customer_id`、`customer_code`、`project_id`、`status=active`、
`commission_rate_bps=1000`，不包含客户展示名或 branding。Webhook 恢复入口必须按
`tenant_id + partner_customer_id + event_id` 回读 Customer、Partner 和 outbox 的原始 actor/request/trace，
逐字段重建 canonical event；任一 owner/hash/时间不一致均固定失败，不用 outbox payload 自证。

### 4.7 数据库门禁

- Partner/Customer/Commission/outbox/cursor 全部具备 tenant-qualified 复合 FK 和 owner trigger。
- PartnerCustomer、CommissionRecord 和 source outbox 禁止 `INSERT OR REPLACE`/rowid 替换。
- CommissionRecord 禁止 UPDATE/DELETE；PartnerCustomer 只允许冻结的终止状态机。
- 新 RBAC template、permission 和 role relation 必须与 Shared revision 22 exact hash 一致。
- up 只接受 79/0079 + revision 21；down 只接受 80/0080 + revision 22。
- down 在任何 Partner、Customer、Commission、outbox、非初始 cursor、EVT-099 订阅/delivery 或领域操作事实存在时
  原子阻断；pristine down -> re-up 必须保真。
- down 还必须确认非 admin 角色从未获授六个新权限，admin 仍是迁移自动扩展后的 exact revision/hash；
  任何后续矩阵编辑均阻断，避免回退静默删除管理员已经表达的授权意图。
- fresh/repeat/down/re-up 每阶段均要求 `integrity_check=ok`、外键违规 0、最高版本/文件 `80/0080`。

## 5. API 设计

所有路由拒绝未知查询参数/未知 JSON 字段，JSON 成功响应携带 `request_id/trace_id`，所有响应设置
`Cache-Control: no-store, private`。附件成功响应通过 `X-Request-ID`、`X-Trace-ID`、
`X-Content-Type-Options: nosniff` 提供链路；错误仍走统一 JSON ErrorResponse。

### 5.1 Partner 与白标

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `POST` | `/partners` | `partner_portal.configure` | 创建当前 tenant 唯一 Partner，201；需 Idempotency-Key |
| `GET` | `/partners/profile` | `partner_portal.read` | 当前 Partner 与 branding 安全摘要 |
| `PATCH` | `/partners/branding` | `partner_branding.manage` | 更新 portal_name、brand color、support text；需 If-Match revision |
| `GET` | `/partners/projects` | `partner_customer.manage` | 当前 tenant 可映射且未占用的 active Project 安全列表 |

POST 不接收 tenant_id、状态、费率、actor、request_id 或时间。branding 使用整数 revision 乐观并发；
旧 revision 返回 409，不做 last-write-wins。

### 5.2 客户

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `GET` | `/partners/customers` | `partner_portal.read` | API-394..396；客户列表及窗口内真实用量/失败/成本聚合 |
| `POST` | `/partners/customers` | `partner_customer.manage` | 创建 Project 映射并原子写 EVT-099 source outbox |
| `PATCH` | `/partners/customers/{id}` | `partner_customer.manage` | 只允许改 display_name 或 active -> disabled |

GET 支持 `start_date/end_date/status/q/limit/offset`，日期最多 366 天，默认最近 30 个完整 UTC 日，
limit 1..100。每个客户返回 request_count、success_count、failed_count、input/output/total/cache tokens、
cost_cny_micros、cost_usd_micros、estimated_records 和 materialized commission cursor 状态。

客户“用量汇总”是当前 tenant 内被映射 Project 的历史治理事实，按请求日期窗口读取，可包含建立映射前的
Project 历史，便于伙伴接管既有客户；“分润”只从 `linked_from_ledger_sequence` 之后开始，两者在 API 字段和
页面文案中必须明确区分，禁止把历史用量误算为历史佣金。

跨 tenant、不存在、其他 Partner 或非法 ID 均返回同形 `partner.customer_not_found`。POST 的幂等边界为
`tenant + Partner + Idempotency-Key hash`；同键同 payload 返回原对象且不重复 EVT-099，不同 payload 返回 409。

### 5.3 分润与附件

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `GET` | `/partners/commission` | `partner_portal.read` | API-397..399；JSON 分润汇总与行项目 |
| `GET` | `/partners/commission?format=csv` | `partner_commission.export` | Excel 可导出要求；UTF-8 BOM CSV attachment |
| `GET` | `/partners/delivery-package` | `partner_delivery.download` | 脱敏 JSON attachment 与 manifest SHA-256 |

分润筛选为 `start_date/end_date/customer_id/format/limit/offset`，最多 366 天。JSON/CSV 都按 currency
分别汇总 amount 与 commission，禁止把 CNY/USD 相加。记录明确 `status=accrued` 与
`settlement_status=not_applicable`，防止把报告冒充支付凭证。

CSV 固定列：period_day、customer_code、customer_name、currency、request_count、total_tokens、
amount_micros、commission_rate_bps、commission_amount_micros、estimated_records。所有文本先阻断
CR/LF/NUL，并对 `= + - @` 前缀加单引号，防止表格公式注入；文件名只含稳定 Partner code 和 UTC 日期。
CSV 按“UTC 日 + Customer + currency”聚合，默认最多 10,000 行、8 MiB；超过任一上限在生成任何附件字节和
AuditLog 前返回 `partner.export_too_large`，不做静默截断。CSV 模式拒绝 `limit/offset`，JSON 模式才分页。

交付包固定 `metahub.partner-delivery/v1`，只含 partner code、branding、安全客户代码/名称、
OpenAI-compatible 相对路径、公开文档入口、生成时间、request/trace 和 manifest hash。不得包含数据库路径、
绝对内部 URL、Project ID、Provider/Virtual Key、Endpoint、Token、Prompt、Response、用量明细或分润明细。
manifest hash 只计算不含 `manifest_sha256` 的 canonical `package` 对象；HTTP envelope 再并列返回该 hash，
禁止把 hash 字段放进自身输入。附件写 AuditLog 必须在返回 body 前提交；审计失败时不得发送部分文件。

## 6. PAGE-119 前端交互

页面是工作台，不做营销落地页。桌面使用紧凑的品牌头、客户表、分润表与右侧操作区；390px 下变为单列，
任何表格使用带 data-label 的行重排，禁止横向溢出。

状态与操作：

- `not_configured`：普通用户只见固定空态；configure 用户见 Partner 初始化表单。
- `loading/ready/empty/error`：每个数据区有稳定尺寸，错误只展示 error code/request_id。
- branding 表单使用原生 color input + 文本输入，提交后以 response revision 更新，不从 DOM 猜测状态。
- 客户创建使用可映射 Project select、客户 code/name 输入；成功后刷新列表，旧请求不能覆盖新对象。
- 客户终止使用确认 Dialog；终止后按钮消失，历史用量和 CommissionRecord 仍可读。
- 分润支持 UTC 日期、客户筛选、刷新和 CSV 下载；下载失败不清空已展示数据。
- 交付包通过附件响应下载，不把内容写入 localStorage/sessionStorage/query string/console。
- 附件用 authenticated fetch 获取；先验证 status、Content-Type、长度上限、request headers 和文件名白名单，
  再创建短生命周期 object URL，click 后立即 revoke。未知内容类型或超限响应只显示固定错误。

页面 owner 由 `page + tenant_id + authorization_fingerprint + backend navigation owner` 组成。租户切换、
logout、当前 401/403、动态撤权或导航离开时，先清空 DOM/内存再 abort；迟到 200/401/403/503 必须被丢弃，
旧 401 不能登出新 Session。

## 7. 错误码

| 错误码 | HTTP | 场景 |
| --- | ---: | --- |
| `partner.request_invalid` | 422 | 严格 body/query/header/date/颜色/文本校验失败 |
| `partner.not_configured` | 404 | 当前 tenant 没有可用 Partner |
| `partner.already_configured` | 409 | tenant 已存在 Partner 或创建幂等冲突 |
| `partner.revision_conflict` | 409 | branding/customer 乐观 revision 过期 |
| `partner.project_not_found` | 404 | Project 不存在、跨 tenant、disabled 或已映射 |
| `partner.customer_not_found` | 404 | 客户不存在、跨 tenant/Partner 或非法 ID |
| `partner.customer_conflict` | 409 | customer code/Project/idempotency 冲突 |
| `partner.commission_unavailable` | 503 | cursor/source/整数/owner 完整性异常 |
| `partner.export_too_large` | 413 | CSV 行数或字节超过冻结上限 |
| `partner.export_unavailable` | 503 | CSV/交付包无法安全生成 |
| `partner.event_unavailable` | 503 | EVT-099 source outbox 无法恢复或投递 |
| `partner.unavailable` | 503 | 数据库或内部合同异常的固定兜底 |

认证缺失/失效继续使用 `auth.token_missing/token_invalid/session_revoked/permission_denied`。
任何异常正文、SQL、路径或提交值均不得进入 API、logger 或正式证据。

## 8. 审计、隐私与可追踪性

- 创建 Partner、更新 branding、创建/终止客户、导出分润和下载交付包写 append-only
  `partner_portal_operations`；固定 action 为 `partner.created`、`partner.branding_updated`、
  `partner.customer_created`、`partner.customer_disabled`、`partner.commission_exported`、
  `partner.delivery_downloaded`。迁移不重建挂接大量历史 owner trigger 的通用 AuditLog。
- Operation/Event 只含安全 ID、状态、revision、行数、币种 totals 和 manifest SHA-256；不保存品牌支持文本、
  客户展示名、CSV/包正文或筛选原文。
- portal logger 只允许固定 `event/error_code` 与 request_id/trace_id/tenant_id/actor/object ID。
- Gateway 的成功、失败、Usage、成本仍由 RequestLog/UsageLedger/FinOpsLedger 追踪；伙伴聚合不复制正文。
- 正式 Runtime/Browser 证据使用虚构安全名称，扫描 Provider Key、Token、Prompt、Response、Endpoint、
  Authorization、Cookie、password/secret 等敏感键和值，命中必须为 0。

## 9. 并发、恢复与兼容

- 创建 Partner/Customer 使用幂等 hash + payload hash；SQLite unique race 回读后精确比较。
- branding/customer 更新使用 revision CAS；0 行更新返回 409 或同形 404，禁止先查后写造成越权竞态。
- Scheduler claim、evaluate、commit 均校验 tenant/customer/generation/token/deadline；失租 worker 禁止写回。
- 每次 claim 冻结 `scan_upper_sequence=min(global ledger high-water, customer cutoff)`；查询窗口为
  `(last_scanned, scan_upper]` 内当前 tenant/project 的 usage charge，按 sequence 最多 200 条。满批只推进到
  最后一条已验证 source；不足一批且窗口内已无匹配行时才推进到 scan_upper，使大量其他 Project 流量不会
  卡住游标。坏源行整批失败关闭，不能跳过后继续推进游标。
- Commission JSON/CSV 返回 cursor `materialized_through_sequence`、`source_high_water_sequence`、
  `freshness_status=current/lagging`。读取不主动写游标；页面不得把 lagging 报表展示成最终结算。
- Event outbox 在每次网络前续租并复核 generation；空订阅也必须形成 sent 终态。
- 旧数据库未到 0080 或 RBAC revision 22 时 portal/API/Scheduler 失败关闭，不用 Shared 默认值补表。
- 不修改既有 Auth、Usage、FinOps 与 Config Transfer wire；Delivery package 是独立安全 manifest，
  不冒充含凭据的配置导出。

## 10. 测试与验收计划

### 10.1 Shared/迁移

- RBAC revision 22 exact catalog、admin 六项与 viewer 五项自动扩展、其他四角色不扩权。
- EVT-099 exact payload、敏感键拒绝、签名事件 request/trace/actor/object owner。
- 空库 80/0080、repeat、pristine down/re-up；每阶段 integrity ok、FK 0。
- owner/append-only/CAS/rowid/replace/idempotency/commission 公式与 down guard 对抗测试。

### 10.2 Control API

- existing login -> Partner 初始化 -> branding -> Project 映射 -> EVT-099 -> 客户用量 -> 分润物化 -> JSON/CSV -> 交付包。
- 401、动态 403、未配置 404、非法 422、revision/idempotency 409、数据库损坏 503。
- 三 tenant + 两 PartnerCustomer：只能读当前 tenant/Partner，跨 tenant 和伪造 ID 同形。
- 请求成功/失败、CNY/USD、estimated、零样本、终止 cutoff、并发 claim、失租、崩溃重试和重启恢复。
- CSV 公式注入、CRLF、BOM、Content-Disposition、manifest hash、附件 header 与 no-store。
- Provider Key、Token、Prompt、Response、raw Usage、支持文本/客户名不进入日志/Audit/Event。

### 10.3 Console/Browser

- strict response validator、owner/ticket/generation、动态撤权、租户切换、迟到 200/401/403/503。
- setup、ready、empty、error、branding、客户创建/终止、分润筛选/CSV、交付包下载。
- 1280x720、1440x900、390x844 截图；无重叠/横向溢出/Console error；未授权身份零预取。

### 10.4 实际运行

- 从空 SQLite 迁移到 0080，启动真实 Control/Console 与本地 Webhook receiver。
- 用真实 Session/RBAC 和两个 tenant 执行 API，插入受治理 RequestLog/Usage/FinOps 安全事实，运行 Scheduler。
- 验证 commission exactly-once、EVT-099 sent、CSV/包真实下载、失败 request_id 和租户隔离。
- 关闭 owned 服务；端口、进程、SQLite sidecar、临时目录归零；只保留白名单正式 evidence 并记录 SHA-256。

## 11. 预计代码边界

- `infra/migrations/0080_channel_partner_white_label_portal.sql/.down.sql`
- `shared-sdk/python/metahub_shared/rbac.py`、`runtime/{errors,webhooks}.py` 与专项测试
- `control-plane-api/src/metahub_control/partner_portal/`、auth dependency、app/console/audit/webhook 装配
- `console-web/src/lib/partnerPortal.js`、`src/features/partner-portal/`、main/i18n/CSS/专项测试
- `scripts/acceptance/prd087-{runtime-acceptance.py,browser-harness.py,browser-acceptance.mjs}` 与合同测试
- `docs/api/partner-portal.md`、README、directory-map、testing 记录和 Excel 状态

不修改 Gateway Provider Adapter、默认上游目录、MCP/硬件链路或 PRD-086 评分实现。

## 12. 编码前自审

### 12.1 第一轮自审

状态：**已完成并修正（2026-08-17）**。

1. P1：初稿只写 Partner 唯一约束，却要求 POST 幂等，无法判断同键异 payload。已给 Partner 增加
   idempotency/payload hash，并冻结同键同 payload 回读、同键异 payload 409。
2. P1：branding 与 Customer PATCH 要求 If-Match，但表设计没有 revision。已给两者增加从 1 开始的 revision，
   所有更新使用 tenant/owner/revision 单语句 CAS。
3. P1：初稿称多个角色“可授予”，却没有精确说明权限上限，security_admin 还会不必要看到伙伴业务。
   首轮先冻结 configure 仅 admin、其余五项候选角色默认不授予；随后核对真实 template 表发现该数据库合同只允许
   `default_granted=1`，最终修正为 admin 六项、专用 viewer 五项自动扩展，其他四角色不扩权。
4. P1：初稿终止客户只冻结 cutoff，未处理已经 claim 的旧 worker。已要求终止和高水位同事务冻结，worker
   提交前重读 status/cutoff/generation，旧 claim 也不能写入 cutoff 后记录。
5. P1：初稿把客户用量与分润都写成“建立映射后”，但 RequestLog 没有稳定序号，秒级时间会产生边界歧义。
   已明确用量是 Project 历史视图，分润才使用 FinOps ledger sequence 精确生效，页面必须区分。
6. P1：初稿没有列出审计 action exact set，迁移和安全摘要无法闭合。首轮冻结六个 action；实现前读取真实
   AuditLog 结构后发现重建会连带大量历史 owner trigger，最终改为独立 append-only 领域操作表并要求 down 阻断。
7. P2：初稿 Customer 更新只保存创建/终止链路，display_name 修改不可追踪。已把 operation request/trace
   定义为最近写操作链路，终止同时保留专用 disable 链路。
8. P2：初稿 CommissionRecord 的展示名快照容易被理解成费用发生时名称。实现和 API 必须把它定义为
   “物化时安全快照”；会计关联始终使用不可变 customer_code/source ledger ID，不以名称做归属判断。

### 12.2 第二轮自审

状态：**已完成并修正（2026-08-17）**。

1. P1：初稿交付包把 manifest hash 同时放进 hash 输入，会形成自引用且客户端无法复算。已冻结 hash 仅覆盖
   canonical package 对象，HTTP envelope 并列返回 hash。
2. P1：初稿 CSV 只有分页上限，没有总行/字节上限，可能让控制面被大导出占满内存。已冻结 10,000 行、
   8 MiB、超限 413、零附件字节/零审计，且 CSV 拒绝 JSON 的 limit/offset。
3. P1：初稿游标只说扫描 Project 匹配行；当全局账本持续增长但当前 Project 无行时，cursor 会永久落后。
   已引入 claim-time scan_upper，只有确认窗口内无未处理匹配行才跨过非匹配序号。
4. P1：初稿 down 只检查业务事实，遗漏租户 admin 在升级后给其他角色授予新权限。已要求 non-admin 新权限为 0、
   admin exact 自动扩展矩阵；任何人工矩阵变化都阻断回退。
5. P1：初稿 EVT-099 没有 payload exact set，也未说明 Webhook 恢复如何证明原始 request/actor。已冻结
   `schema_version` 加六个业务字段的七字段安全 payload，并要求 tenant/customer/event 三重回读权威 source
   重建 canonical event。
6. P1：初稿客户终止可能与 scheduler 已冻结的 scan_upper 交错。第一轮已加入 commit-time cutoff 重读；本轮补充
   scan_upper 取 cutoff 最小值，证明旧 worker 也不能物化 cutoff 后费用。
7. P2：初稿附件只要求不进 Storage，未限制恶意 Content-Type、文件名或超大响应。已要求 fetch 后严格校验、
   短生命周期 object URL 和立即 revoke，异常只显示固定 error code/request_id。
8. P2：初稿分润读取没有暴露物化延迟，伙伴可能把滞后数据当最终金额。已增加 source/cursor 高水位与
   current/lagging 状态，并在 API/UI 明示 accrued、not_applicable settlement。
9. P2：初稿审计可能在附件开始发送后失败，留下无法追踪的成功下载。已冻结先完整生成与校验、事务提交 AuditLog、
   最后构造 Response；任何审计失败都不得发送部分 body。
10. P2：白标色只允许六位大写 hex，页面必须把 CSS 变量限制在 PAGE-119 根节点；离开页面或换租户时移除变量，
    防止旧 Partner 品牌影响新 owner 或其他控制台页面。
11. P1：实现前从 0079 空库提取出 `rbac_role_permission_templates.default_granted CHECK(default_granted=1)`，
    证明“可授权但默认关闭”不可表示。设计已改为 admin 六项、viewer 五项 exact 自动扩展；这比引入未设计的新角色
    或篡改整个 RBAC 数据模型更符合现有兼容边界。
12. P1：同一基线显示 AuditLog 存在大量跨 PRD 的强 owner trigger；只为新增 action 重建会扩大回归面并可能丢失
    既有不变量。PRD-087 改用独立 tenant-qualified append-only operation 表，仍完整记录 actor/request/trace/hash，
    不声称进入通用审计查询 API。

两轮自审问题均已写回正文，至此允许开始 PRD-087 编码；PRD-088 仍不得开始。

## 13. 完成状态

- 0080 与 RBAC revision 22 已实现并完成 fresh/repeat/down/re-up；四阶段均为 `integrity_check=ok`、外键违规 0。
- Partner、PartnerCustomer、CommissionRecord、分润游标、EVT-099/source outbox、伙伴安全操作链、API、CSV、
  交付包和 PAGE-119 均按本文 tenant/RBAC/并发/隐私边界交付。
- Python 联合专项 `53/53`、Shared 相邻 `81/81`、Control 相邻 `17/17`、PAGE-119 Console `18/18`、
  Console 全量 `926/926`、lint `286 files` 和真实 Chromium r17 均通过。
- `01_PRD总表` 第 89 行、`03_M2-M3扩展交付` 第 25 行、PAGE-120、API-400/401、OBJ-121、EVT-100、
  TC-0175/0176 共同确认下一条权威未完成项为 PRD-088；本条不实现 PRD-088。

总体状态继续保持 `all_108_prds_complete=false`。
