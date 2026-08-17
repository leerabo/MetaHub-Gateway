# PRD-085 产品使用埋点与激活漏斗设计

状态：已完成；编码前两轮与实现后两轮自审的 P0/P1 均已关闭，正式 Runtime/Browser 验收通过。

日期：2026-08-17

## 1. 权威范围与交付边界

### 1.1 交叉确认

本设计以工作簿、README、PRD-084 设计/测试记录和系统架构文档的交叉结果为准，不按编号猜测：

- `01_PRD总表!A86:R86` 与 `03_M2-M3扩展交付!A22:R22`：
  `PRD-085 | M2-企业增强 | P2 | 扩展层 | 用户行为埋点 | 产品使用埋点与激活漏斗`。
- `04_页面清单`：`PAGE-117 增长分析页`，主要用户为产品/客户成功。
- `05_API接口清单!API-389..391` 是工作簿对 method、path segment 的机械拆分，共同表示唯一公开接口
  `GET /product-analytics/funnel`，不是三个 API。
- `06_数据对象`：`OBJ-116 ProductEvent`，业务对象必须包含 `tenant_id`。
- `07_Webhook事件`：`EVT-097 product_event.recorded`。
- `08_验收用例`：`TC-0169/0170`，覆盖七项行为、漏斗准确性、租户权限、异常与隐私。
- `12_开发状态`、README、`docs/prd` 和 `docs/testing` 共同确认 PRD-084 已完成，PRD-085 是下一条尚未实现项。

系统架构文档没有预先定义 ProductEvent、漏斗口径或该 API，因此本设计补齐缺失合同；通用架构约束继续生效：
管理分析位于控制面，业务表租户限定，后端执行权限检查，复杂报表不得进入数据面热路径，普通日志和事件不得保存敏感明文。

### 1.2 本次交付

- 服务端记录以下七类已提交业务事实：注册、创建 Provider、创建 Virtual Key、首次成功调用、配置预算、邀请同事、查看日志。
- 新增租户限定的不可变 `ProductEvent` 事实和可恢复 `product_event.recorded` source outbox。
- 新增只读 `GET /product-analytics/funnel` 和独立后端权限 `product_analytics.read`。
- 新增 PAGE-117 增长分析页，展示当前租户七阶段漏斗、完成度、事件数和首次/最近发生时间。
- 对历史权威事实做确定性回填，使升级后漏斗不从零开始；历史回填不伪装成实时 Webhook。
- 迁移版本为 `0078_product_analytics_activation_funnel`，当前总体状态保持
  `all_108_prds_complete=false`。

### 1.3 明确不做

- 不提供浏览器或客户任意提交事件的 `POST` API；调用方不能伪造 ProductEvent、actor 或 tenant。
- 不采集匿名访客、页面点击、Prompt、Response、模型消息、请求/响应正文、Provider Key、Virtual Key、Token、
  Authorization、Endpoint、原始 usage 或自由文本 metadata。
- 不新增跨租户产品后台。现有身份模型没有平台超级管理员；创建无审计的跨租户旁路会违反架构约束。
- 不修改默认上游边界；OpenRouter、硅基流动继续只是竞品与迁移兼容对象。
- 不把失败模型请求认定为“首次调用”激活成功；失败仍由现有 RequestLog、Usage、成本与 routing attempt 链路追踪。
- 不新增通用 BI 查询语言、任意时间聚合或 ProductEvent 导出/删除 API。

## 2. 产品口径

### 2.1 统计单位

PRD 的七项动作组成企业租户首次接入生命周期，因此漏斗单位固定为“当前认证租户”，不是匿名访客、单个 Project 或
单个最终用户。仓库当前没有 tenant signup API，“注册”因此固定为租户成功建立并取得 `tenant_id` 的一次性事实，不能用
管理员创建用户或邀请接受冒充新的租户注册。`actor_user_id` 只用于追溯其余动作的租户内操作者；租户注册和 Gateway
首次调用没有可信控制台用户时允许为空。

`GET /product-analytics/funnel` 永远从认证上下文恢复唯一 `tenant_id`，不接受 `tenant_id` query/body/header。
不同租户必须使用各自 Session 查询；这样既满足按租户查看，也从接口形状上消除 IDOR。未来若引入平台超级管理员，必须在
独立 PRD 中增加跨租户授权、审计与查询合同。

### 2.2 七阶段冻结定义

| 顺序 | event_type | 完成条件 | 权威来源 | 失败/重复语义 |
| --- | --- | --- | --- | --- |
| 1 | `tenant.registered` | 当前支持的 tenant 开通流程完整提交并取得稳定 `tenant_id` | `seed_runs.status=applied`；0078 对既有 tenant 做一次兼容回填 | 每 tenant 恰好一条；默认 seed 是开通方式而非用户行为，不外发 Webhook |
| 2 | `provider.created` | Provider 创建事务及 AuditLog 成功提交 | `audit_logs.action=provider.created`；或 `config.imported` 且安全摘要 `providers_created_count>0` | 每个来源一条；重试复用来源唯一键 |
| 3 | `virtual_key.created` | Virtual Key 创建事务及 AuditLog 成功提交 | `audit_logs.action=virtual_key.created`；或 `config.imported` 且安全摘要 `virtual_keys_created_count>0` | 不保存或外发 Key 明文、hash、prefix |
| 4 | `gateway.first_call_succeeded` | 当前租户第一条终态 `request_logs.status=success` | `request_logs` | 每租户最多一条；失败请求不推进但继续由现有链路追踪 |
| 5 | `budget.configured` | Budget 创建或更新事务及 AuditLog 成功提交 | `audit_logs.action=budget_rule.created|budget_rule.updated` | 每个 AuditLog 一条；漏斗只取首次 |
| 6 | `teammate.invited` | Invitation 创建及 `user.invited` AuditLog 成功提交 | `audit_logs.action=user.invited` | 邀请发出即完成，不等待接受；不保存邮箱或 Token |
| 7 | `request_logs.viewed` | 后端成功执行日志查询并原子写入查询事实 | `request_log_query_events` | 每个成功查询一条；不保存筛选值或日志正文 |

本项的“注册”定义为取得 `tenant_id` 且当前受支持开通事务完整提交的 tenant 一次性事实。仓库没有 signup 或 tenant create
API，当前唯一受支持开通入口是 `apply_seed_data`；它在 tenant、管理员及初始化资源都写入成功后，才在同一事务插入
`seed_runs.status=applied`。因此 0078 不监听任意 `tenants INSERT`，而从这个持久完成标记派生注册事实：默认 seed 正式
视为部署租户开通，但固定写成 `capture_mode=backfill`，参与漏斗且不创建 outbox，绝不把部署初始化外发成实时用户行为。
升级前已经存在的 tenant 无论是否保留 seed run，均从 `tenants` 兼容回填一次，同样不外发。未来新增正式租户创建 API 时，
必须在其开通事务内增加明确的持久 provisioning fact 后再接入本事件；不得退回监听所有 tenant INSERT、首个用户、管理员创建
用户或邀请接受。这样 source 边界由 `seed_runs` schema 标记，而不是依赖 seed 的调用顺序、slug 或硬编码 tenant ID。

“创建 Key”按现有产品术语固定为 Virtual Key，而非 Provider Credential。seed 自动创建的 Provider/Virtual Key 没有对应
业务 AuditLog，不得据资源存在性推断激活，避免所有新租户被默认标记为已完成第 2/3 阶段。

### 2.3 漏斗算法

- 时间范围固定为全历史，时间显示与计算固定 UTC；API 返回 `window.kind=all_time` 与 `timezone=UTC`，避免隐含浏览器时区。
- 每阶段返回当前租户 `event_count`、`reached`、`first_occurred_at`、`last_occurred_at`。
- `cumulative_reached` 表示从第 1 阶段到当前阶段都至少有一条事实。动作允许乱序；缺口后来补齐时，后续已发生阶段可进入累计漏斗，
  不因历史先后顺序永久丢失激活资格。
- `reached_tenant_count` 和 `cumulative_tenant_count` 只能为 `0|1`；`cohort_tenant_count` 固定为当前可信 tenant 的 `1`。
- 第一阶段转化率为 `cumulative_tenant_count/cohort_tenant_count`；后续阶段分母为上一阶段
  `cumulative_tenant_count`。分母为 0 时返回 `null`，不得猜成 0% 或 100%。传输值固定为整数 basis points
  `0..10000`，前端只负责显示，避免浮点和多端舍入漂移。
- `completed_stage_count` 统计独立已完成阶段，`cumulative_stage_count` 统计连续漏斗深度，
  `completion_bps=round_half_up(completed_stage_count/7*10000)`；七阶段全部完成时 `activated=true`。
- ProductEvent 记录每次去重后的权威行为，而不是只保存七个聚合里程碑；因此 Provider、Key、预算、邀请和日志查看的
  `event_count` 可大于 1，`first_occurred_at` 与 `last_occurred_at` 有独立意义。注册与首次成功调用是一次性阶段，分别由
  `UNIQUE(tenant_id,event_type)` partial index 限制为每 tenant 一条。同一来源重试不得增加 `event_count`，阶段完成只取存在性。

固定七阶段查询只扫描当前租户索引并聚合七个枚举，不属于架构文档所述复杂离线报表，不需要创建异步报表 Job。

## 3. 事件捕获架构

### 3.1 数据库派生而非散落埋点

七项事件全部从已经提交业务成功与租户归属的权威表派生。0078 安装数据库 trigger，在来源写事务内原子插入
`product_events`；来源事务回滚时 ProductEvent 与 outbox 一并回滚。

该方案覆盖 Control API、Gateway、配置聚合入口和后续同库受控入口，不依赖前端按钮、日志文本或提交后 best-effort 回调。
`UNIQUE(tenant_id,source_kind,source_id,event_type)` 防止来源重放双计数；所有 trigger 都使用
`INSERT ... SELECT ... WHERE NOT EXISTS` 明确跳过已存在的同一来源，不使用会吞掉 owner、FK 或 CHECK 错误的
`INSERT OR IGNORE`。SQLite 单写者串行化与唯一索引共同处理竞争。注册和首次成功调用另有
`UNIQUE(tenant_id,event_type)` partial index，确保重复 seed run 或并发成功请求只选出一个一次性事实。

实时 trigger：

- `AFTER INSERT ON seed_runs WHEN status='applied'` -> `tenant.registered`，固定 `capture_mode=backfill`；该事实与整个 seed
  事务一同提交或回滚，不创建实时 outbox；不安装通用 `tenants INSERT` 注册 trigger；
- `AFTER INSERT ON audit_logs` -> 映射 Provider、Virtual Key、Budget、Invitation 四类事件；
  `config.imported` 只读取既有白名单审计摘要中的 `providers_created_count` 与
  `virtual_keys_created_count`，按大于零分别形成一个里程碑来源，不复制导入正文；
- `AFTER INSERT ON request_logs WHEN status='success'` 与
  `AFTER UPDATE OF status ON request_logs WHEN OLD.status!='success' AND NEW.status='success'`
  -> 尝试插入 `gateway.first_call_succeeded`；
- `AFTER INSERT ON request_log_query_events` -> `request_logs.viewed`。

trigger 只复制 tenant、actor、来源 ID、固定 event_type、request/trace 和时间；绝不读取正文、Credential、Endpoint、
邮箱、筛选值、usage JSON 或自由 metadata。

### 3.2 历史回填

0078 在安装实时 trigger 之前按同一映射回填既有 `tenants`、`audit_logs`、成功 `request_logs` 和
`request_log_query_events`：

- 每个既有 tenant 从最早的 applied `seed_runs`（若存在）或 tenant 自身创建事实回填唯一注册事件；迁移后新 seed tenant
  只在 `seed_runs.status=applied` 后形成同样的非外发注册事实；
- 普通事件保留所有去重后的历史来源；
- 首次成功调用只选择每租户按 `(created_at,request_id)` 排序的第一条成功 RequestLog；
- 回填行固定 `capture_mode=backfill`，实时行固定 `capture_mode=live`；
- 回填行参与 API 指标，但不创建 outbox，避免升级后向客户 Endpoint 突发重放历史行为；
- 迁移中的时间、actor、request/trace 全部来自权威列；不存在的值保持 `NULL`，不伪造链路 ID。

### 3.3 ProductEvent

`product_events` 字段：

- `product_event_id`：`pev_` 加
  `substr(metahub_sha256(json_array(tenant_id,event_type,source_kind,source_id)),1,32)`；同一权威来源在 down/re-up 后保持稳定；
- `tenant_id`：必填，复合 owner 索引首列；
- `event_type`：冻结七值枚举；
- `actor_user_id`：可空；非空时必须属于同 tenant；
- `source_kind`：`tenant|seed_run|audit_log|request_log|request_log_query`；
- `source_id`：稳定来源标识，只用于内部幂等和排障，不由漏斗 API 或 Webhook 返回；
- `operation_request_id`、`operation_trace_id`：可空、安全链路 ID；
- `capture_mode`：`backfill|live`；
- `status`：本版固定 `recorded`；
- `occurred_at`、`created_at`、`updated_at`：UTC；本版三者创建后不可变。

表按 `(tenant_id,event_type,occurred_at,product_event_id)` 查询，按来源建立唯一索引，并由 trigger 禁止 UPDATE、DELETE、
`INSERT OR REPLACE`/rowid 冲突及跨租户 actor。ProductEvent 不存 JSON metadata，从结构上关闭 Prompt 等任意字段流入路径。

### 3.4 `product_event.recorded` source outbox

实时 ProductEvent INSERT 触发同事务创建一条 `product_event_outbox`；outbox ID 固定为 `pevo_` 加
`substr(metahub_sha256(product_event_id),1,32)`，回填行不创建。outbox 保存
`tenant_id/product_event_id`、`pending|leased|sent|failed`、attempt、lease owner/token/deadline、next attempt、
固定错误码与时间，以及有界 `subscription_snapshot_json` 和 `snapshot_frozen_at`；不复制 payload。快照字段必须满足
`NULL/NULL` 或“合法、排序稳定、去重后的原始 `webhook_delivery_id` 数组 + 非空冻结时间”的成对约束，空数组表示已经冻结但
当时没有订阅者，与尚未冻结的 `NULL` 明确区分。

Control Plane 安装可关闭的后台 scheduler：

1. 每次只按时间领取一条可运行或 lease 已过期的 source；租户轮转沿用既有 source outbox 公平策略。第一次有效 claim
   必须在同一数据库事务内冻结订阅快照并持久化，即使快照为空也写 `[]` 和 `snapshot_frozen_at`；后续 lease/restart 只复用
   已冻结快照，冻结后新增或删除订阅都不改变该 source 的目标集合。
2. 从不可变 ProductEvent 重建固定 payload 和稳定 event ID `evt_pev_<32hex>`，不信任 outbox JSON。
3. 使用现有 Webhook dispatcher 严格按已持久化快照恢复或创建 pending delivery，不重新查询当前订阅；event ID 稳定，
   Endpoint/event 唯一键防重复外发。
4. 每次网络尝试前续租；成功 CAS 为 `sent`，可重试失败按固定退避回到 `pending`，十次后为 `failed`。
5. 启动扫描、队列拥塞、commit 后进程退出和 lease 到期均可恢复；Webhook 失败不回滚已提交业务事实。

Webhook payload exact-set：

```json
{
  "schema_version": "1.0",
  "product_event_id": "pev_...",
  "event_type": "provider.created",
  "capture_mode": "live",
  "occurred_at": "2026-08-17T00:00:00Z"
}
```

信封 `tenant_id`、`actor_id`、`object_id=product_event_id`、`trace_id` 必须与 source 一致。payload 不包含 `source_id`、
request_id、对象名称、邮箱、URL、Prompt、Response、Key、Token、筛选值或任意业务正文。Shared webhook catalog 新增
exact validator，并把 `product_event.recorded` 加入可订阅集合。

## 4. 权限与租户隔离

0078 将 RBAC catalog 升级到 revision 20，新增：

- `product_analytics.read`：读取当前租户产品激活漏斗，risk=`read`。

默认授予不可变 `admin` 和 `ops_admin`；不授予 developer、viewer、finance_admin、security_admin。仓库当前没有
`product_manager/customer_success` 角色，不能为了页面名称把指标扩给所有只读用户；后续角色扩展须走权限矩阵 PRD。

迁移只扩展仍精确匹配 revision-19 默认矩阵的 admin/ops_admin；租户自定义角色不被静默改写。Shared catalog、模板、
租户 permission 行、role grants、matrix hash、新租户初始化 trigger 和 Console authorization fingerprint 必须同步升级。

路由依赖 `require_product_analytics_reader` 在解析任何业务 query 前实时认证 Session 并检查该 permission。服务查询的每个
SELECT 都显式以 `context.user.tenant_id` 为首条件。前端导航和 capability 只改善体验，不能替代后端检查；动态撤权后的下一请求
立即 403，页面同步清除旧租户 DOM 和内存。

## 5. 公开 API

### 5.1 `GET /product-analytics/funnel`

请求无业务 query/body；后端要求 `product_analytics.read`。成功响应设置 `Cache-Control: no-store, private`：

```json
{
  "tenant_id": "tenant_...",
  "window": {"kind": "all_time", "timezone": "UTC"},
  "cohort_tenant_count": 1,
  "stages": [
    {
      "order": 1,
      "event_type": "tenant.registered",
      "event_count": 1,
      "reached": true,
      "cumulative_reached": true,
      "reached_tenant_count": 1,
      "cumulative_tenant_count": 1,
      "conversion_from_previous_bps": 10000,
      "first_occurred_at": "2026-08-17T00:00:00Z",
      "last_occurred_at": "2026-08-17T00:00:00Z"
    }
  ],
  "summary": {
    "completed_stage_count": 1,
    "cumulative_stage_count": 1,
    "total_stage_count": 7,
    "completion_bps": 1429,
    "activated": false
  },
  "generated_at": "2026-08-17T00:01:00Z",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

响应必须恰好返回七个按冻结顺序排列的 stage。API 只返回稳定英文 `event_type`，不返回展示 `label`；中英文名称由
Console i18n 根据 exact event type 映射。时间为 RFC 3339 UTC 或 `null`；计数使用非负整数；basis points 为
`0..10000` 或规定的 `null`。API 不返回 ProductEvent 列表、actor、来源 ID 或任意业务对象字段。

### 5.2 错误与缓存边界

- 缺失/失效 Session 复用 `auth.*` 401；缺权复用 `auth.permission_denied` 403。
- 任意 query string 固定返回 `product_analytics.request_invalid` 422；接口不能静默忽略客户端 tenant、时间窗或未知参数。
- 数据库读取、枚举漂移、计数不变量或时间解析失败统一 `product_analytics.unavailable` 503。
- FastAPI 校验与未知 query 使用统一 422，响应始终携带 `request_id`/`trace_id`。
- 认证失败、权限失败、校验失败和成功均由应用最外层 middleware 覆盖 `no-store, private`，防止代理缓存上一租户漏斗。
- 日志只写固定事件名、tenant/actor、request/trace、阶段计数和固定错误码，不写 ProductEvent source、对象字段或正文。

## 6. PAGE-117 增长分析页

新增后端导航项 `growth-analytics`，group=`总览`，target=`growth-analytics`，要求
`product_analytics.read`。Console i18n 提供中英文导航标签；页面主体沿用现有安静、密集的运维界面，不引入营销 Hero 或
第三方图表运行时。

页面结构：

- 顶部紧凑标题、UTC/全历史口径和图标刷新按钮；
- 一条固定七段漏斗，使用有稳定尺寸的 step grid 展示完成、缺口和累计中断状态；移动端改为纵向，不缩放字号；
- 摘要区展示完成阶段数、连续深度、完成率与是否激活；
- 明细表展示阶段、状态、事件数、首次/最近时间和相邻转化率；
- loading、仅注册初始态、错误态与权限撤销态完整；错误显示统一 code/request_id，不显示底层异常。

新增 `productAnalytics.js` 对成功响应执行 exact-set、七阶段 exact-order、tenant owner、计数、时间、百分比和累计不变量校验。
页面 state 只保存脱敏聚合；read ticket 绑定 tenant、user、session、permission revision、authorization fingerprint、
navigation 和 generation。离页、登出、401/403、tenant/Session/权限变化、pagehide 时 Abort 并清空 state 与 DOM；迟到响应不能
回写新 owner。页面无表单、Storage、URL query、下载或 ProductEvent 明细。

访问 PAGE-117 仍只发送现有 `console.page_viewed` 外壳事件，payload 仅含 source/hash/viewport；漏斗计数、ProductEvent、
actor 和时间不得复制进 Console page event。

## 7. 迁移 0078

Up 必须在 `BEGIN IMMEDIATE` 中：

1. fail closed 核对最新 applied version=`0077`、RBAC revision-19 exact catalog/role hash、新旧临时对象不存在。
2. 创建 `product_events`、`product_event_outbox`、索引、owner/状态/不可变 trigger。
3. 在实时来源 trigger 安装前按 2.2 映射回填历史事实；验证每类数量、来源唯一性、注册/首次调用每 tenant 最多一条，
   seed Provider/Key 不形成假事件，且 outbox 为 0。
4. 创建 ProductEvent -> outbox trigger 与五个来源 trigger（`seed_runs`、`audit_logs`、`request_logs` INSERT、
   `request_logs` 状态 UPDATE、`request_log_query_events`）。
5. RBAC catalog 升 revision 20，增加 `product_analytics.read`，保真重建 immutable template/permission 结构，
   仅扩展默认 admin/ops_admin，恢复全部约束和新 tenant 初始化 trigger。
6. final guard 核对表、列、索引、trigger、FK、枚举、outbox、RBAC exact hash、历史 ProductEvent 与来源计数。
7. 提交；运行器随后原子登记 0078 checksum。

Down 使用 expected-version rollback 模板。部署编排必须先关闭 ProductEvent scheduler；SQL 随即以 `BEGIN IMMEDIATE`
阻止新的 source 写入，并在首个持久 DDL 前完成全部门禁：

1. owner guard 确认 latest applied=`0078` 且 ProductEvent/RBAC schema 未漂移；
2. 若存在任何 `capture_mode=live` ProductEvent、任意 source outbox（包括 pending/leased/sent/failed）、订阅了
   `product_event.recorded` 的 Endpoint、该类型的 Webhook delivery/attempt/dead-letter，
   立即失败关闭；不得以“已备份”为由静默删除外部可见事实；
3. 只有可由 0077 权威表确定性重建、从未外发的 `capture_mode=backfill` 行允许删除，因此
   `0077 seeded -> 0078 -> down -> re-up` 可以执行并得到相同事实；
4. 删除来源 trigger、outbox trigger、索引和新表；只撤销 0078 自动增加且仍未自定义的 grants，恢复 revision-19 catalog、
   permissions、role matrix 与 tenant initialize trigger；
5. final guard 确认不存在 PRD-085 订阅/投递残留且 0077 exact schema 已恢复，再由迁移运行器在同事务把 0078 标为
   rolled_back。

Up、down、re-up 和空库 78/0078 均须验证 `integrity_check=ok`、`foreign_key_check=[]`。不得修改任何已应用 migration。

## 8. 失败、并发与隐私

- 业务动作与 ProductEvent/outbox 同事务；任何 source 校验或 event INSERT 失败都必须让业务事务失败关闭，不能提交无埋点成功。
- first-call partial unique index处理并发；trigger 只通过显式 existence predicate 跳过已核验的同 tenant 首次事实，
  其他 owner/schema 错误必须中止来源事务。
- ProductEvent 读取不参与 Gateway 请求；数据面仅承担已有 RequestLog 事务内固定小行写入，Webhook 与聚合均在控制面异步/只读完成。
- outbox lease 使用 token+owner+deadline CAS；取消、停服和 lease 丢失不得把未知投递标为成功。
- ProductEvent、outbox、Webhook、API、Console、测试证据与结构化日志都必须通过敏感键扫描；禁止字段和值包括
  Provider Key、Virtual Key、Token、Prompt、Response、Authorization、Cookie、Endpoint、邮箱、消息、输入输出正文和筛选值。
- 用量、成本和所有失败请求继续以现有 RequestLog、Usage/FinOps Ledger、routing attempt 和错误码为权威；ProductEvent 不复制账务数据，
  也不把失败调用变成激活成功。

## 9. 测试计划

### 9.1 Shared

- RBAC revision 20、permission exact catalog、六角色授权上限和默认 grant。
- `product_event.recorded` exact payload/envelope、稳定 event ID、可订阅目录、敏感键和额外字段拒绝。

### 9.2 Migration

- 空库 1..78、0077 seeded -> 0078、up/down/re-up、checksum、latest/count、integrity/FK；Down 覆盖纯 backfill 成功，
  live source/outbox、订阅、delivery/attempt/dead-letter 任一存在均失败且数据库逐值不变。
- 七类历史回填 exact count、seed 只形成非外发 tenant 注册且不推进 Provider/Key、重复 seed run 不重复注册、任意 tenant
  INSERT 不冒充注册、首次成功选择、失败 RequestLog 排除、
  无历史 outbox。
- live trigger 原子性、来源重试、并发首次调用、cross-tenant actor、不可变/rowid/replace、outbox state machine。
- RBAC 默认矩阵扩展、自定义矩阵保留、新 tenant 初始化、down 恢复 0077 exact hash。

### 9.3 Control API 与 outbox

- 七类实际业务动作产生 ProductEvent；失败动作不产生；乱序后漏斗按定义收敛。
- 当前 tenant 隔离、另一 tenant 登录独立结果、无权限 403、动态撤权、坏 Session、数据库故障和统一 request_id。
- exact 七阶段、仅注册阶段/全激活、重复事件、相同时间、百分比 null/舍入、时间 UTC 与不变量失败关闭。
- outbox claim/retry/final failure、lease expiry、协作停服、重启恢复、空订阅快照、冻结后增删订阅、冻结后崩溃恢复、
  stable event、delivery 幂等和敏感 payload 拒绝。

### 9.4 Gateway

- 第一条成功非流/流调用各能产生唯一首次事件；失败、预算/DLP/权限阻断不推进；后续成功不产生第二个首次事件。
- RequestLog、Usage/成本和失败追踪合同不回归，Gateway 响应不等待 Webhook。

### 9.5 Console 与浏览器

- API client exact-set/tenant/order/invariant/error boundary。
- 导航只对后端授权可见；加载、七阶段、乱序缺口、激活、仅注册初始态、错误态、刷新和 401/403 清理。
- owner/generation/Abort、离页、换租户、动态撤权、迟到 200/401 与 pagehide。
- 桌面和移动 viewport 无重叠、横向溢出或文本遮挡；浏览器真实栈验证 API、页面事件、网络请求和无敏感证据。

### 9.6 最终门禁与实际运行

- 开发阶段只运行 PRD-085 与受影响的 auth/console/request-log/provider/key/budget/invitation/webhook/Gateway 专项。
- 完成门禁再运行必要 Console、Control、Gateway、Shared 回归，ruff（按仓库规则处理既有 E501）、compileall 和 diff check。
- 从空 SQLite 实际迁移到 78/0078，执行 seed、integrity/FK；启动 Control、Gateway、Console，以受支持 seed 开通两个租户，
  在授权/未授权用户下实际完成七类动作（同时覆盖配置导入计数映射）、查询漏斗、恢复 outbox，并保存不含敏感正文的
  JSON/截图/manifest/SHA-256。
- 最后执行敏感信息、禁止文件、Git 状态和迁移证据审计，更新 README、目录说明、测试记录和 Excel，再提交推送 `origin/main`。

## 10. 计划修改面

- `infra/migrations/0078_product_analytics_activation_funnel.sql` 与 `.down.sql`
- `shared-sdk/python/metahub_shared/rbac.py`
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`、`runtime/errors.py`
- `shared-sdk/python/metahub_shared/db/seed.py` 的开通完成合同测试（不改写或复制 seed 敏感正文）
- `control-plane-api/src/metahub_control/product_analytics/`
- `control-plane-api/src/metahub_control/auth/`、`app.py`、`console/service.py`
- `console-web/src/lib/productAnalytics.js`
- `console-web/src/features/product-analytics/`
- `console-web/src/app/main.js`、`src/i18n/messages.js`、`src/styles/main.css`
- 对应 Shared/Control/Gateway/Console/migration/acceptance 专项测试
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-085-tests.md` 与工作簿状态

## 11. 编码前自审

### 11.1 第一轮：需求、准确性与事务边界

发现并修正：

1. P1：原草案使用浮点百分比，后端、JSON 和浏览器可能出现舍入漂移。已改为后端权威整数 basis points，并冻结
   `round_half_up` 规则。
2. P1：原草案提到 `INSERT OR IGNORE`，可能把 FK、CHECK、owner 漂移误当幂等。已改为显式 existence predicate，
   唯一索引只承担竞争终检，其他错误失败关闭。
3. P1：只映射 `provider.created`/`virtual_key.created` AuditLog 会漏掉配置导入真实创建。已增加
   `config.imported` 安全计数映射，仍不读取导入正文或资源名称。
4. P1：声明“无业务 query”但未定义未知 query 行为会造成 tenant/filter 参数被框架静默忽略。已冻结任意 query 为
   `product_analytics.request_invalid` 422。
5. P2：首次调用“尝试还是成功”存在需求歧义。按激活语义固定为第一条成功终态；失败调用不推进但继续由现有用量、
   成本和失败链路追踪。

结论：第一轮 P0=0，P1=0（4 项已关闭），P2=0（1 项已裁决并写入合同）。继续第二轮审查。

### 11.2 第二轮：来源真实性、恢复与公开合同

发现并修正：

1. P1：原草案监听所有 `tenants INSERT`，会把直接 SQL 或未完成初始化误判为注册；迁移后默认 seed 还会产生 live
   Webhook。已改为当前受支持开通入口的持久 `seed_runs.status=applied` 完成事实，固定 `capture_mode=backfill` 且不建
   outbox；0078 只对既有 tenant 兼容回填。未来开通 API 必须提供显式 provisioning fact，禁止退回无来源区分的 tenant trigger。
2. P1：原 outbox 没有持久订阅快照字段，无法区分“尚未冻结”和“已冻结为空”，重启后可能把旧事件发送给后来新增的
   Endpoint。已增加 `subscription_snapshot_json/snapshot_frozen_at` 成对约束，并冻结空快照、租约恢复和增删订阅语义。
3. P1：原 Down 允许备份后删除事件，但未阻断 live 事实、source outbox 和 Webhook 外部依赖。已改为首个 DDL 前完整
   fail-closed guard；只允许删除可确定重建且从未外发的 backfill 行，使 seeded up/down/re-up 同时可执行。
4. P1：原文对 ProductEvent 是“每次去重行为”还是“每租户阶段唯一里程碑”表述不一致，会让 `event_count` 和 Webhook
   数量不可验证。已冻结为每次权威行为一条，仅注册与首次成功调用是一阶段一条；来源重放仍由确定性 ID/唯一键去重。
5. P2：API 示例含中文 `label`，会把展示本地化耦合到后端稳定合同。已移除 label，Console 仅按 exact event type 做 i18n。
6. P2：合法 tenant 不存在“完全空事件”状态；已把测试改为仅注册阶段，并保留损坏/不变量输入的 503 失败关闭测试。

结论：第二轮 P0=0，P1=0（4 项已关闭），P2=0（2 项已关闭）。本文设计冻结，允许开始编码；后续实现若改变来源、
租户边界、事件计数、快照或回滚合同，必须先回到设计复审。

## 12. 实现后自审与修正

### 12.1 第一轮：持久层、来源与回归边界

发现并修正：

1. P1：SQLite 混合 `Z` 与 `+00:00` 时间文本若直接排序，首次/最近时间会偏离真实 UTC 顺序。查询改为按
   `julianday()` 排序并以稳定 ID 终决，新增混合格式回归。
2. P1：迁移专项缺少 dead-letter down guard 与部分 live 来源断言。补齐 subscription、delivery、attempt、
   dead-letter、RBAC drift 和 live fact 的事务前置阻断，确认失败不产生部分 mutation。
3. P1：PRD-074 权限数量断言硬编码旧 catalog，PRD-085 revision 20 会制造伪回归。测试改为验证增量权限和角色边界，
   不再把全局数量当业务合同。
4. P1：Gateway SQLite 测试代理未转发 `in_transaction`，无法覆盖 Usage 部分写入回滚。代理补齐真实连接状态并通过
   完整相邻文件回归。

结论：第一轮 P0=0，P1=0（4 项已关闭），无遗留高风险项。

### 12.2 第二轮：真实运行、浏览器竞态与证据

发现并修正：

1. P1：Runtime 对 Node 路径、启动锁、配置导入映射、限流隔离、RBAC 暂态重试和 WAL/SHM 清理约束不足。
   全部改为仓库内固定运行时、owner-safe 锁、有界重试及终态 cleanup，并增加来源 provenance 合同。
2. P1：Browser 动态撤权时，暂停响应可先由 `Fetch.requestPaused` 到达但不再触发
   `Network.responseReceived`，导致真实 200 少计。驱动改为在 Fetch 响应阶段记录，并以 Network request ID 去重，
   同时兼容 CDP header 数组和 Network header 对象；单元合同锁定双事件只计一次。
3. P1：Browser 隐私与失败坐标需要防止把私密值或异常正文写入进程输出。固定值扫描、无响应正文捕获、细粒度安全
   坐标和 owner 目录回滚均已补齐。
4. P1：Chromium 151 即使关闭 component update 仍可能启动 `OsUpdateHandlerWin`，使终止态子进程锁住隔离
   Profile；headless 验收现显式禁用该非业务更新处理器，driver 清理改为先删子项、最后删 owner marker，并由
   Node 合同锁定失败可重试边界。首个清理失败批次不作为权威证据，修正后的 r2 批次完整通过。
5. P1：r2 只以真实 Browser 覆盖三次 200，尚未实际执行 PAGE-117 当前 401/403 与迟到 401 分支。最终驱动增加
   受 owner 保护的权限恢复和 Session 失效协调动作，冻结九次 query-free GET 状态序列
   `200,200,200,403,200,401,200,401,200`，并证明迟到 401 不会清除已登录的第二租户。
6. P2：Harness 的失败清理原先可能在首个目录删除异常后跳过其他目录和 HTTP socket。`abort()` 改为进程、数据库、
   三个 owner 目录和 server 分项 best-effort；故障注入合同证明清理异常不会覆盖原始验收失败坐标。
7. P2：Browser 来源证明未绑定认证、Session、RBAC、导航和统一错误实现，且两个租户同为 `1/7` 时旧 DOM 不具
   区分力。来源清单扩展到 24 个关键文件；主租户新增无敏感字段的真实 Provider 审计来源形成 `2/7`，次租户保持
   `1/7`，最终事实同时要求精确切换与两个租户各自的 revision 20，禁止用全表 `MIN` 掩盖单租户漂移。
8. P1：Chromium 151 在 feature 已禁用时仍可能偶发启动 `os_update_handler.exe`。最终驱动为每次运行在仓库 `.tmp`
   构造 owner-qualified Chrome 硬链接树，明确排除更新器和可写 `debug.log`，不改原始 `.tools`；故障注入与正式批次
   均证明 Browser、driver profile、隔离运行树、端口和 sidecar 完整清理。

结论：第二轮 P0=0，P1=0（8 项已关闭）。正式 Runtime 与 Browser 均通过，四张 PNG 已逐图复核，无未关闭 P0/P1。

## 13. 完成证据与下一项

- Runtime：`.artifacts/prd085-runtime/prd085-runtime-20260817T055917Z-f41d66e4.json`，SHA-256
  `9bc26bcd9dc04a18b9628be5a689fe9492ee2e4ed99f6a623dffe36397ee61d6`。
- Browser：`.artifacts/prd085-browser/prd085-browser-20260817T073524Z-final-r2/`；JSON/manifest
  SHA-256 为 `2ba160c3b7351ff1b9771ac1d2bd1bba9bcfb06b90e94caa3463484b92a881f9`、
  `bc51513b7ed3b20af628121c1d49adcc7a5fedb6d4a04156875d113591be49a1`。
- 完整门禁、逐文件 bytes/hash、隐私与资源审计见 `docs/testing/PRD-085-tests.md`。

Excel 总表第 87 行、M2/M3 第 23 行、PAGE-118、API-392/393、OBJ-117、EVT-098 与 TC-0171/0172 共同确认
下一条权威未完成项为 `PRD-086 | M2-企业增强 | P2 | 扩展层 | 客户健康 | 客户健康评分`。本次不实现 PRD-086，
`all_108_prds_complete=false`。
