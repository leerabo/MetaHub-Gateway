# S7 平台运营增量与控制台分域批次设计

状态：设计冻结，等待 G6 通过后进入连续编码。权威范围来自
`企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 的 PRD-139..147、G7 和
“现有导航处置”64 项清单。当前数据库基线为 0130；本批次完成后基线为 0139。

## 1. 批次目标与实施顺序

本批次把存量 tenant-scoped 伙伴、客户健康、增长、品牌、Provider 工程和商业账务事实迁到
明确的平台、Workspace、伙伴或 Deployment 域，并最终重组企业 Gateway 工作区。固定顺序为
`139 -> 140 -> 141 -> 142 -> 143 -> 144 -> 145 -> 146 -> 147`：先建立六个目标业务域，
再隔离共享页面和会话，随后生成 Workspace 导航合同，最后统一回填、切换、tombstone 和接管
Scheduler。G6 失败时只允许完成设计，不开始 S7 migration 或业务编码。

每条 PRD 保留独立设计、migration、受影响单元/API 冒烟、测试记录、敏感审计和 Git 提交。
开发中不重复运行全套测试；PRD-147 独立提交后统一执行 G7 数据库、集成、FLOW、真实浏览器、
性能、安全、兼容和必要回归。任何 G7 必需证据缺失、跳过、失败或含敏感明文都会阻断 PRD-148。

## 2. 不可跨越的域与事实边界

- `PlatformSession` 只恢复平台 operator、当前 revision、固定平台角色和权限；它不隐式创建
  Membership，也不读取客户 Prompt、Response、附件正文、内部备注或 Provider Secret。
- 伙伴使用 `GlobalSession + ChannelMembership` 恢复 `/partner` 白名单，不使用
  `WorkspaceSession`，不能枚举未归属 Workspace，也不能进入企业控制面或 `/platform`。
- `WorkspaceSession` 只访问当前 `workspace_id + tenant_id` owner 的企业事实；平台角色不能委派为
  `platform_*`，`service_account` 没有浏览器导航。
- `SupportSession` 不继承 Platform 或 Workspace 导航。客户正文访问仍要求 PRD-095 的实时
  SupportGrant、字段 scope、TTL、双审批和 grant revision。
- 平台客户、增长、Provider 和账务投影只保存精确白名单字段、不可逆摘要、计数和解释因子；
  不复制客户业务正文。来源事实只有一个 writable owner，迁移后旧行只作为历史或 tombstone。
- 所有关联客户的事实使用不可变 `workspace_id + tenant_id` 复合外键；全局账号、平台制品、
  Deployment 和伙伴组织维持各自 owner，不用一个泛化 `owner_id` 混合。
- 迁移采用 expand/migrate/cutover/contract。兼容读可以回滚，双写不得无限保留；rollback 是有
  lease、checkpoint、补偿和审计的恢复 job，不能复活旧权限、旧 DOM、旧预取或旧 API。

## 3. 迁移、核心对象与 down guard

| PRD | Migration | 目标事实与迁移来源 | down guard |
| --- | --- | --- | --- |
| 139 | 0131 | ChannelPartner/Membership、ReferralCode、RegisteredLead、WorkspaceAttribution/Change、CommissionRule snapshot、ChannelStatement；迁移 PRD-087 partners/commission/outbox | 任一伙伴、成员、商机、归属、规则、结算、映射、event/outbox 或游标存在即拒绝 |
| 140 | 0132 | CustomerAccountProjection/Contact、HealthSnapshot、SuccessPlan/Task、RenewalOpportunity、QbrSummary；迁移 PRD-086 score/scheduler 水位 | 任一客户投影、联系人、健康、计划、任务、续费、QBR、event/outbox 或游标存在即拒绝 |
| 141 | 0133 | AcquisitionSource、Funnel/Cohort snapshot、Campaign、Experiment、ConversionAttribution、GrowthExport；消费既有 ProductEvent/outbox | 任一聚合、活动、实验、归因、导出、消费水位、event/outbox 或游标存在即拒绝 |
| 142 | 0134 | PlatformBrand、ChannelBrand、DeploymentBrand、VerifiedDomain、BrandAsset、BrandMigrationCursor；迁移 PRD-088 branding/event/outbox | 任一品牌、域名、资产、验证、LKG、event/outbox 或迁移水位存在即拒绝 |
| 143 | 0135 | ProviderAdapterRelease、ContractRunProjection、BuiltinErrorCatalog、WorkspaceErrorOverride、CompatibilityProjection；迁移 0046/0055/0056 事实 | 任一目录、release、run 投影、错误规则、覆盖、兼容投影、event/outbox 或水位存在即拒绝 |
| 144 | 0136 | PlatformPlanCatalog、WorkspaceSubscriptionProjection、PlatformInvoice、BillingDisputeCase、ProviderCostRate、InternalFinOpsLedger、Reconciliation；迁移 0072/0073 和既有价格/账本 | 任一价格版本、订阅、发票、争议、成本率、账本、对账、event/outbox 或游标存在即拒绝 |
| 145 | 0137 | Platform/WorkspaceDashboardProjection、DomainApprovalQueue、AssistantQueryProjection、AccountPreference、DeploymentDiagnosticProjection | 任一投影、审批、查询摘要、偏好、诊断、event/outbox 或刷新水位存在即拒绝 |
| 146 | 0138 | WorkspaceNavigationProjection、RoleTaskHome、NavigationPreference、AdvancedToolEntitlement、NavigationContractSnapshot | 任一导航合同、任务、偏好、entitlement、快照、event/outbox 或 epoch 水位存在即拒绝 |
| 147 | 0139 | ConsoleDomainMigration、BackfillCursor、LegacyRouteTombstone、NavigationContractSnapshot、DomainReconciliation、SchedulerRegistry/Lease | 任一切换、回填、tombstone、对账、scheduler lease/fence、水位、event/outbox 或审计事实存在即拒绝 |

所有 migration 保持稳定 ID、UTC、严格 CHECK、revision/generation、复合 owner FK、不可变事件触发器
和 source/outbox 同事务。逐条验证 fresh/current-head/逐级 up/down guard/重放；G7 再验证
`0130 -> 0139` 往返、逐表行数/hash、FK、outbox、审计和 Scheduler 水位。

## 4. API、权限与共享实现合同

- PRD-139：`/platform/channel-*` 由 `platform_channel` 管理，`platform_billing` 只复核结算，
  `platform_owner` 只审批高风险归属变更；`/partner/workspaces` 只返回当前 ChannelMembership
  归属。推荐码、商机和审批都使用幂等键/CAS；归属变更双审批且申请人与审批人不同。
- PRD-140：`/platform/customer-*` 只返回安全客户目录、可解释健康因子和续费状态；
  `platform_customer_success` 管理计划，支持和财务分别看到裁剪字段，企业和伙伴身份全部拒绝。
- PRD-141：`/platform/growth/*` 只消费版本化、同意有效的聚合事件；实验有退出/停止规则，
  高基数和 PII 导出要求 step-up、审批、短 TTL、no-store 和逐次审计。
- PRD-142：平台、伙伴、Deployment 三种品牌 owner 独立。域名全局唯一并重新验证；资产先进入
  私有 staging，完成大小/类型/hash/恶意内容检查后原子发布；Edge 离线只消费签名 LKG。
- PRD-143：Adapter 发布、合同运行和内置错误目录仅由平台工程域写入；Workspace developer 只能
  管理本 Workspace error override。Runner secret、内部 schema、源码和构建路径不进入企业 DTO。
- PRD-144：平台商业账、Workspace 订阅/发票、企业内部 Provider 成本账分开授权但可双边对账；
  金额使用整数最小单位、显式币种/税率/价格版本。企业不能管理套餐目录或裁决争议。
- PRD-145：同名 Platform/Workspace 页面使用独立 route、DTO、client、store、AbortController 和
  navigation epoch；GlobalSession 只读账号偏好。助手来源逐对象鉴权，查询正文不持久化。
- PRD-146：`GET /workspace/navigation` 是后端权限投影，绑定 Membership revision、合同版本和
  entitlement；前端隐藏不是权限边界。单组展开、直接 URL、偏好和任务首页都不能扩大权限。
- PRD-147：切换只在来源/目标行数、hash、FK、outbox、审计、scheduler 水位和负载矩阵全部匹配
  时 CAS 推进。旧路由从外部 router 卸载并统一返回普通 404；tombstone 只作为内部迁移事实，
  不形成可调用兼容 API。回滚仅恢复内部兼容读。

`platform_access.service._authority` 在增加 S7 权限表前改为遍历固定的
`_PERMISSION_EXTENSION_TABLES` 白名单并用固定 SQL 模板加载，不扫描或拼接任意 schema 表名。
0131..0139 只向该 tuple 增加已迁移表；权限指纹覆盖角色、全部 extension permission 和 revision，
每次请求重新恢复，撤权后下一请求立即失败。

所有写 API 拒绝 Bearer、WorkspaceSession/SupportSession 错域 Cookie、重复 Cookie、query 凭据、
未知字段和非预期 body，并要求 exact Origin/CSRF、一次性 step-up、`Idempotency-Key` 和 quoted
`If-Match`。幂等记录绑定 operator、action、owner 和 canonical request digest；同键异体冲突。

## 5. 64 项导航处置与源码差异

源码当前有 66 个 `NAVIGATION_DEFINITIONS`，Excel 64 项全部存在；后续合法新增项为
`migration-operations` 和 `support-tickets`，两项都保留在 Workspace。以下 D01..D22 是 Excel
原文处置的唯一编码，不允许同一 navigation_id 出现第二种结论：

| Code | Excel 处置 |
| --- | --- |
| D01 | 平台/企业拆分 |
| D02 | 迁出企业 |
| D03 | 平台/私有部署拆分 |
| D04 | 企业保留并改名 |
| D05 | 企业保留 |
| D06 | 企业保留并归组 |
| D07 | 平台/企业拆分并降级 |
| D08 | 企业保留；平台另建计量 |
| D09 | 企业保留；平台账本拆分 |
| D10 | 企业保留并入高级配置 |
| D11 | 平台/企业拆分并改名 |
| D12 | 企业保留；平台发放拆分 |
| D13 | 企业保留；平台另建商业异常 |
| D14 | 企业保留并归高级工具 |
| D15 | 企业保留并降级 |
| D16 | 企业保留；平台审计拆分 |
| D17 | 企业保留并入日志详情 |
| D18 | 企业保留并任务化 |
| D19 | 企业保留并入开发者中心 |
| D20 | 迁出产品控制台 |
| D21 | 企业保留并入详情 |
| D22 | 迁到全局账号壳层 |

| navigation_id | 处置 | 承接 PRD |
| --- | --- | --- |
| overview | D01 | 094/095/145/146 |
| admin-assistant | D01 | 095/145 |
| approvals | D01 | 095/145 |
| growth-analytics | D02 | 141/147 |
| customer-health | D02 | 140/147 |
| partner-portal | D02 | 139/147 |
| branding | D03 | 142/147 |
| tenant | D04 | 093/094/096/146 |
| users | D04 | 091/094/115/146 |
| role-permissions | D05 | 094/115/146 |
| environments | D06 | 101/108/146 |
| providers | D05 | 100/119/123/146 |
| local-models | D06 | 119/120/123/146 |
| private-gateways | D06 | 120..130/146 |
| adapter-specs | D02 | 143/147 |
| adapter-contract-runs | D02 | 143/147 |
| provider-health | D01 | 109/112/119/143/145 |
| error-maps | D07 | 143/147 |
| models | D05 | 119/146 |
| model-aliases | D05 | 101/108/146 |
| routing-policies | D05 | 101/108/146 |
| model-quality-scores | D05 | 109/119/146 |
| experiments | D05 | 101/106/146 |
| projects | D05 | 094/146 |
| virtual-keys | D05 | 100/118/146 |
| usage | D08 | 099/122/144/146 |
| finops-ledger | D09 | 099/104/144/146 |
| model-prices | D01 | 098/119/144/147 |
| response-cache | D10 | 101/108/117/146 |
| plans | D11 | 098/144/147 |
| quota-buckets | D12 | 098/099/144/146 |
| billing-disputes | D01 | 104/132/144/147 |
| budgets | D05 | 099/106/146 |
| cost-anomalies | D13 | 099/106/109/144/146 |
| policies | D05 | 108/117/146 |
| policy-simulator | D14 | 108/146 |
| policy-snapshots | D05 | 108/121/146 |
| policy-templates | D01 | 117/119/143/146 |
| dlp-rules | D05 | 108/117/146 |
| intent-security | D15 | 110/146 |
| session-risks | D05 | 116/137/146 |
| output-safety | D05 | 108/109/117/146 |
| webhooks | D05 | 105/133/146 |
| webhook-dead-letters | D15 | 106/146 |
| audit-logs | D16 | 095/117/135/145/146 |
| request-logs | D05 | 109/117/146 |
| request-explanations | D17 | 108/109/146 |
| feedback-summary | D05 | 109/140/146 |
| quickstart | D18 | 097/146 |
| sdk-examples | D19 | 133/146 |
| migration-guide | D19 | 131/133/146 |
| api-docs | D19 | 133/146 |
| competitor-comparison | D20 | 141/146/147 |
| enterprise-chat | D05 | 116/117/146 |
| prompt-templates | D05 | 117/146 |
| files | D05 | 117/122/146 |
| file-security | D21 | 117/122/137/146 |
| batch-jobs | D05 | 106/113/146 |
| mcp-tools | D19 | 110/117/118/146 |
| tenant-data-lifecycle | D04 | 117/134/146 |
| debug-console | D15 | 110/133/146 |
| system-status | D01 | 109/112/145 |
| language-settings | D22 | 091/105/145 |
| config-summary | D07 | 109/120/125/145/147 |

当前 Workspace exclusion 错误隐藏 `providers`、`local-models`、`provider-health`、`error-maps` 和
`billing-disputes`，PRD-146 必须恢复这些经过裁剪的企业入口。最终只从企业导航合同中删除
`growth-analytics`、`customer-health`、`partner-portal`、`adapter-specs`、
`adapter-contract-runs` 和 `competitor-comparison`；删除同时覆盖 import、route switch、DOM、
prefetch、直接 hash、迟到响应和旧 API，不只是侧栏文本。

## 6. Scheduler 注册表、leader lease 与 fencing

Excel PRD-147 写有 23 个后台循环，但当前 `app.py` 在
`control_background_schedulers_enabled` 下注册 39 个 legacy scheduler，此外还有始终注册的 status
publication 和按 gateway placement 注册的 snapshot scheduler。实现以源码事实为准：39 个受开关
管理的上下文全部进入固定注册表，两个外围上下文也通过同一 supervisor 获得唯一执行权，不能在
多 Web 副本中各自启动无限循环。

0139 为每个 scheduler code 持久化 owner domain、enabled、lease owner、lease deadline、fencing
generation/token digest、last started/completed/error、业务 watermark 和 revision。启动时 supervisor
只装配注册表中允许的 context；每轮工作前原子获取/续租并传递 fencing generation，状态写入、
event/outbox 和 watermark 更新都验证同一 fence。失租实例停止提交；接管实例从持久水位继续。
关闭按逆序释放 lease；崩溃依赖 deadline 接管，不清空队列或重置游标。

G7 至少用两个 app replica 证明：每个 scheduler code 同时只有一个 leader；过期 leader 的迟到提交
被 fence；接管不重复 event/outbox；39+2 注册项无遗漏；禁用项不启动；现有 platform job worker
lease、各业务 scheduler lease 和审计水位均保真。

## 7. 前端分域与交互合同

平台运营页面继续使用安静、紧凑的工作台布局，按角色返回 Platform navigation；企业工作区以
职责任务和 Gateway 日常操作为主，不显示增长、客户成功、渠道、平台 Provider CI 或商业裁决。
伙伴只有独立 `/partner` 壳层。页面共享视觉组件，但不共享鉴权 client、DTO、store 或缓存。

固定状态包括 loading、empty、partial、stale、permission revoked、conflict、dependency unavailable
和 recovery in progress。按钮只在服务返回 action capability 时启用；高风险写操作展示审批、revision
和恢复入口。390/1280/1440、DPR 1/1.25/1.5、键盘/焦点/读屏、无重叠/裁剪/横向溢出和直接 URL
都在 G7 真实浏览器验证。路由切换 p75 < 1.5 秒，每页只加载当前数据。

## 8. 两轮设计自审

### 第一轮：事实 owner、身份与迁移完整性

- P0：若为平台页面复制 partners、customer health 或 ProductEvent，会保留两套 writable facts。
  修正为目标域保存迁移后的唯一事实或只读投影；旧 ID 显式映射，切换后旧表只读/tombstone。
- P0：让伙伴复用 WorkspaceSession 会把客户 Membership 能力带入伙伴入口。修正为
  GlobalSession + ChannelMembership，固定 `/partner` 白名单，企业和 Platform API 全部拒绝。
- P0：平台客户目录若包含联系人正文、工单正文或请求内容，会形成批量越权读取面。修正为精确
  安全字段、不可逆摘要和 owner-qualified reference；详细诊断仍走 SupportGrant。
- P0：只改侧栏会让旧 hash、DOM、prefetch 和 API 继续可达。修正为 PRD-147 内部 tombstone
  事实、外部 router 卸载和静态源码/运行时双审计，企业旧入口四层同时归零。
- P1：当前 Workspace exclusion 隐藏了五个 Excel 要求保留/拆分的入口。修正为 PRD-146 恢复
  `providers/local-models/provider-health/error-maps/billing-disputes` 的企业裁剪合同。
- P1：直接复制现有 permission loader 的条件分支会扩大漏表和 SQL 拼接风险。修正为先抽取固定
  allowlist tuple 迭代，再逐 migration 增加 S7 表，并用权限指纹验证动态撤权。

第一轮修正后无未关闭 P0/P1。

### 第二轮：并发、Scheduler、回滚与证据真实性

- P0：按 Excel 过期的“23 个循环”实现会漏掉当前 39 个受开关 scheduler。修正为源码注册表
  39 项全覆盖，并让 status/snapshot 两个外围 context 使用同一 leader/fence 机制。
- P0：只给 supervisor 一个进程内锁无法阻止多 Web replica 重复消费。修正为数据库持久 lease、
  generation/token fence、逐次提交校验和可接管 watermark；失租旧 worker 的迟到写失败关闭。
- P0：rollback 若重新启用旧 route 或 dual-write，会恢复已撤销的跨域能力。修正为 rollback 只
  切内部兼容读并执行补偿 job，永不重新挂载旧权限、route、DOM、prefetch 或 API。
- P0：只比行数无法发现 owner、金额或投影漂移。修正为逐表 canonical hash、FK、outbox、审计、
  scheduler 水位和抽样复算共同匹配，任何差异阻断 cutover。
- P1：现有产品 QA 固定 0130 和企业 admin/developer/viewer 浏览器矩阵，不能证明 G7。修正为完成
  S7 后把基线推进到 0139，并新增 Platform、Partner、Support、旧入口归零、64 导航和 39+2
  Scheduler lease/fence 证据；旧 97 用例只作为必要回归，不能单独宣称 G7。
- P1：在 PRD 开发中重复全套测试会污染批次节奏和长稳。修正为逐 PRD 只跑受影响单元/API
  冒烟；PRD-147 后一次性跑统一数据库、集成、FLOW、浏览器、性能、安全、兼容和回归。

第二轮修正后无未关闭 P0/P1。

## 9. 独立交付与 G7 退出证据

每条 PRD 依次提交：`docs/prd/PRD-<id>-*.md`、013x up/down、模块代码、受影响测试、
`docs/testing/PRD-<id>-tests.md` 和敏感审计结果。提交不得混入其他 PRD 或用户未跟踪文件。

G7 必须统一证明：0130..0139 fresh/逐级/往返/down guard；64 项 Excel 导航和 2 项后增导航均有
唯一处置；平台运营/品牌/Provider 工程/商业账务分域；所有历史行数/hash/outbox/Scheduler/审计
水位保真；企业旧入口、DOM、预取和 API 为零；Platform/Workspace/Partner/Support/Device 五域
负载矩阵失败关闭；39+2 scheduler 唯一 leader 和 fencing；三浏览器三缩放真实流程；各 Excel
SLO；敏感与禁止文件审计为零。通过后才允许开始 PRD-148 和 G8/GA 终验。
