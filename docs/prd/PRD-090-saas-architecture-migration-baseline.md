# PRD-090 SaaS总体架构与兼容迁移基线

状态：**已完成**（2026-08-20）
总体状态：`saas_incremental_roadmap_complete=false`；兼容旧状态 `all_108_prds_complete=false`。

## 1. 权威需求与范围

本设计逐字段核对 `企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 的 `01_PRD总表`、`02_阶段路线图`、`07_旧计划废弃映射`、`08_里程碑门禁` 与 `09_风险与决策`。PRD-001..089 已完成事实保持不变；旧总清单 PRD-090..108 全部废弃、禁止开发。

本条冻结个人 SaaS、企业 SaaS、云管私有 Gateway 三种标准产品形态，并冻结完全隔离环境的签名包异步管理能力。完全隔离不是第四种实时管理形态。本文定义全局身份、Workspace、平台域/租户域、云边信任、数据驻留、Secret、迁移波次、兼容期、目标拓扑、威胁模型和统一 SLO。

本条不实现登录、注册、Workspace、平台运营台或 Edge Agent，不创建 API、页面或迁移，不修改业务数据库。Excel 中“迁移往返、真实 API/浏览器”属于每条 PRD 的通用证据模板；PRD-090 的精确替代证据是：现有 0082 基线只读核验、架构合同验证器、文档链接校验和旧计划废弃扫描。

## 2. 现状证据

| 当前对象/合同 | 现状 | 证据 | PRD-090结论 |
| --- | --- | --- | --- |
| `tenants` | `tenant_id` 是业务隔离根；slug 唯一；`internal_only/public_service` | `infra/migrations/0001_core_mvp_tables.sql` | 保留并在兼容期投影为 organization Workspace |
| `users` | 用户记录携带 `tenant_id`；邮箱仅租户内唯一；密码哈希随租户用户保存；`role`无数据库CHECK/FK | 0001、auth service | PRD-091 扩展为GlobalAccount+Membership；旧表保留兼容投影并补角色一致性guard |
| `sessions` | Session固定`tenant_id+user_id`；Token claim带`tid`；数据库只有两个独立FK，没有`(tenant_id,user_id)`复合FK | 0002、`auth/tokens.py` | 保留撤销事实；PRD-091/094新增两级Session并补owner一致性guard |
| 登录请求 | `tenant_slug` 可省略，但服务端回落 default tenant；邮箱查询仍需 tenant | `auth/models.py`、`auth/service.py` | 兼容保留；PRD-094 完成后删除登录页租户字段 |
| 租户 RBAC | 六个租户角色和细粒度 permission；逐请求从数据库复核 | PRD-059、migration 0050 | 保留为 Workspace RBAC，不复用给平台员工 |
| `invitations` | token hash、单次使用、到期与租户owner已存在；接受时固定创建新`users`行 | 0051、invitation service | 迁移为MembershipInvitation；已有GlobalAccount验证后直接增加Membership |
| 租户生命周期 | `tenant_data_states`另有`active/deletion_requested/deleted`状态 | 0082、PRD-089 | Workspace授权同时复核该状态，不能只看`tenants.status` |
| 业务对象 | 实库258表中250表携带`tenant_id`；95表引用users、9表引用sessions、139表引用tenants、5表引用roles | 0001..0082、空库迁移清单 | 根表不得换ID或直接替换；采用兼容投影与expand-migrate-contract |
| 数据面身份 | Virtual Key 恢复 tenant；调用/Usage/失败带租户事实 | Gateway 与既有测试 | 保留；未来只把 tenant 映射来源升级为 Workspace |
| 私有部署 | Compose 和本地 Endpoint 已有；尚无云边设备身份/期望状态 | PRD-028/084 | 保留本地运行能力；云管协议从 PRD-120 开始新增 |

当前最高迁移是 0082。本条不创建 0083；PRD-091 若需要数据库变化必须从 0083 开始。

## 3. 架构决策记录

### ADR-090-01：全局账号与Workspace分离

决定：认证主体使用 GlobalAccount；业务授权使用 WorkspaceMembership。一个账号可以拥有个人 Workspace 并加入多个企业 Workspace。登录只识别账号，登录后在单一 Workspace 时直达，在多个 Workspace 时选择或恢复最近一次允许空间。

拒绝方案：继续以 `tenant_slug+email` 作为账号主键。该方案无法支持一个人加入多个企业，会复制密码、MFA和会话撤销事实。

### ADR-090-02：平台IAM与租户IAM分域

决定：`platform_*` 角色和租户角色使用不同存储、Token audience、路由前缀和审计域。平台role id固定为`platform_owner`、`platform_ops`、`platform_security`、`platform_billing`、`platform_support`、`platform_verification`、`platform_compliance`、`platform_privacy`和`release_manager`；它们不自动成为任何Workspace成员。支持访问只能生成限时`SupportGrant`，且不允许读取Provider Key、Token、Prompt、Response或本地Secret。

拒绝方案：按`admin@metahub.local`或任意存量邮箱自动创建`PlatformOperator`。旧账号默认继续只是其Workspace的租户admin；按邮箱静默提升会让平台管理与客户业务管理无法证明隔离。

### ADR-090-03：兼容期内Workspace与Tenant一一映射

决定：现有 `tenant_id` 不重写。每个现有 tenant 回填一个 organization Workspace，映射保持一一且不可变；新增个人/企业空间同样获得 tenant-compatible owner ID 或明确映射。业务表继续使用 `tenant_id`，避免一次迁移全部 100+ 表。

拒绝方案：一次性把所有表改为 `workspace_id`。爆炸半径过大，回滚不可执行。

### ADR-090-04：控制面、数据面和云边解耦

决定：控制面发布版本化签名快照；Gateway 按 tenant/workspace 使用本地快照和 LKG。Edge Agent 只发起出站 mTLS，云端不能入站打开任意远程 Shell。

拒绝方案：Gateway 每次请求同步查询控制面或云端直接登录客户节点。这会扩大延迟、故障域和支持越权面。

### ADR-090-05：Secret随执行位置托管

决定：云 Gateway Secret 进入租户独立 KMS 信封域；私有 Gateway Secret 只在客户环境。云端只保存引用、版本和健康摘要。

### ADR-090-06：驻留是硬约束

决定：Workspace 创建时绑定 Region；存储、备份、Gateway placement 和容灾都必须验证驻留。没有合规目标时失败关闭，不能为了可用性静默跨区。

### ADR-090-07：生产SaaS渐进迁移

决定：使用 expand-migrate-contract、双写、对账水位、分波切换和功能开关。兼容窗口至少覆盖 N/N-1；每波都可回到旧读路径，但不能撤销已发生的安全撤权、支付、账本或审计事实。

### ADR-090-08：存量账号只在身份已证明时合并

决定：同一规范邮箱跨tenant可能代表不同自然人，也可能拥有不同password hash、状态或显示资料。未经邮箱重新验证和管理员确认不得合并；冲突进入人工队列，证明完成前使用隔离shadow account，disabled/revoked事实按更严格并集处理。兼容旧读路径必须查询新Account/Membership、`tenant_data_states`和撤销tombstone的deny overlay。

拒绝方案：任选一个password hash或active状态形成GlobalAccount。该方案可劫持其他Workspace并在回滚时复活已撤权账号。

### ADR-090-09：角色决定页面投影，后端决定最终授权

决定：平台角色和企业职能角色分别维护页面/动作矩阵。平台人员由Platform IAM添加；企业owner/admin邀请企业成员并在委派上限内分配`admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer`，`service_account`走机器身份流程，owner转移单独二次验证。后端返回导航投影，但直达URL、API、预取、迟到响应和动态撤权都必须再次鉴权。

渠道管理、客户管理和增长分析只在SaaS平台域，由`platform_channel/platform_customer_success/platform_growth`访问。任何企业角色，包括owner，均不获得这些导航、DOM、预取或API能力。

拒绝方案：前端仅隐藏菜单，或让企业admin创建`platform_*`角色。前者可被直达URL/API绕过，后者会打穿平台域与租户域。

## 4. 目标数据对象

| 对象 | 所有者域 | tenant规则 | 关键约束 |
| --- | --- | --- | --- |
| GlobalAccount | 平台身份 | 不带tenant | 规范邮箱/手机唯一；状态机；不可从账号直接读业务 |
| AccountCredential | 平台身份 | 不带tenant | 密码/MFA/IdP凭据分域；只存hash/公钥/密文 |
| GlobalSession | 平台身份 | 不带tenant | 撤销、设备和风险；不承载租户角色 |
| Workspace | 租户根 | 一一映射tenant | `personal/organization`；Region、状态、Owner完整 |
| WorkspaceMembership | 租户身份 | 必带workspace/tenant | account+workspace唯一；revision单调；最后Owner保护 |
| WorkspaceSession | 租户身份 | 必带workspace/tenant | 绑定GlobalSession+Membership revision；切换轮换 |
| PlatformOperator | 平台IAM | 不带tenant | 员工身份、强MFA、生命周期与客户账号隔离 |
| PlatformRoleBinding | 平台IAM | 不带tenant | 独立权限目录；不能引用租户角色 |
| SupportGrant | 跨域受控桥 | 必带workspace/tenant | 申请人不得审批；两名不同审批者且客户/平台各一；原因、时限、字段范围、撤销 |
| EdgeDeployment | 租户边缘 | 必带workspace/tenant | 设备身份、Region、版本、状态和证书owner |
| DesiredState | 租户边缘 | 必带workspace/tenant | generation、签名、audience、过期和LKG |

## 5. 内部合同草案

这些路径只冻结语义，不在 PRD-090 实现：

| 合同 | 主体 | 成功语义 | 失败边界 |
| --- | --- | --- | --- |
| `POST /identity/login` | 未认证账号 | 返回GlobalSession，不返回租户权限 | 防账号枚举；统一code/request_id |
| `GET /identity/workspaces` | GlobalSession | 返回允许的Workspace安全摘要 | 不返回其他空间或敏感资料 |
| `POST /identity/workspace-switches` | GlobalSession+Membership | 轮换WorkspaceSession | 动态撤权、陈旧revision失败关闭 |
| `/platform/*` | PlatformSession | 仅平台IAM | 租户Token固定403，不自动补Membership |
| `/edge/*` | Device mTLS | 仅自身Deployment合同 | audience/证书/签名/owner任一异常即拒绝 |
| `GET /console/navigation`（目标语义） | WorkspaceSession | 仅返回当前企业角色允许的Workspace页面，不含渠道/客户管理/增长分析 | 返回值不替代页面API的后端权限校验 |
| `GET /platform/navigation`（目标语义） | PlatformSession | 按平台角色返回运营页面；渠道、客户、增长分别隔离 | WorkspaceSession和SupportSession固定拒绝 |

所有 API 继续使用统一错误结构与 `request_id`；所有写操作必须幂等或使用CAS。Global、Workspace、Platform、Support和Device五类主体使用不同issuer、audience和密钥域。Cookie采用`__Host-`、Secure、HttpOnly且不设置Domain，写请求校验Origin+CSRF；Bearer和Cookie并存时固定拒绝，不选择权限更高者。

## 6. 迁移矩阵

| 现有能力 | 决策 | 目标 | 实施PRD | 回滚 |
| --- | --- | --- | --- | --- |
| `tenants` | 扩展 | organization Workspace+不可变`WorkspaceTenantBinding` | 093 | 保留旧tenant读写和物理ID；不允许重绑 |
| 250张tenant业务表 | 保留 | 继续以tenant为物理owner，由服务端绑定到Workspace | 091..137 | 不批量改列/ID；逐模块双读对账 |
| `users` | 迁移 | GlobalAccount+Membership，旧user兼容投影 | 091/094 | 切回旧登录；保留新账号、冲突队列和撤权事实 |
| `sessions`/Token `tid` | 迁移 | GlobalSession+WorkspaceSession；新Session ID不复用旧语义 | 091/094 | 旧Session只在N/N-1读取；不复活已撤销Session |
| `users.role` | 废弃授权来源 | 受约束镜像Membership.role_code | 091/094 | 兼容读取仍复核Membership revision和deny overlay |
| `invitations` | 迁移 | MembershipInvitation；已有账号验证后加Membership | 092/094 | 保留token hash、单次、到期和接受审计 |
| `tenant_slug`登录/UI字段 | 废弃 | 账号登录+Workspace选择 | 091/092/094 | N/N-1开关恢复旧入口，但仍复核新deny overlay |
| 租户RBAC及`/permissions` | 保留 | Workspace RBAC | 094 | 继续读取现有角色矩阵；后端逐请求复核 |
| `/auth/me`、`/auth/logout` | 迁移 | Global/Workspace Session生命周期 | 091/094 | 兼容响应版本化；多凭据并存固定拒绝 |
| `/tenant/current` | 扩展 | 当前Workspace兼容别名；read/write权限分离 | 093/094 | N/N-1保留路径，owner只从Session绑定恢复 |
| `/users` | 迁移 | Membership管理 | 094 | 保留旧user投影直至所有95张FK消费者迁完 |
| `/invitations` | 迁移 | Membership邀请 | 092/094 | 旧路由版本化代理到新状态机 |
| 平台管理员 | 新增 | 独立PlatformOperator | 095 | 关闭平台路由；bootstrap只用于恢复 |
| default seed资源 | 迁移 | `legacy_seed`，不计激活完成 | 097 | 不删除客户真实资源 |
| SQLite | 保留开发 | PostgreSQL生产、SQLite本地测试 | 100 | 数据校验后切回旧库/版本 |
| 单Gateway | 扩展 | Region/Cell池 | 101 | 单Cell placement开关 |
| 本地Endpoint | 保留 | 私有Gateway本地执行 | 119/120..130 | 本地管理模式继续可用 |
| 旧PRD-090..108 | 废弃 | 新SaaS路线图 | 090 | 仅可撤销路线图版本，绝不自动复活旧范围 |

### 6.1 现有63项导航处置

`control-plane-api/src/metahub_control/console/service.py` 的 `NAVIGATION_DEFINITIONS` 是现状清单，共63项。Excel `11_现有导航处置` 逐项登记现有分组、页面、处置、企业/平台角色、API/表/Scheduler证据、承接PRD和切换边界；验证器直接解析源码并要求63项一一对应，不能靠人工编号或只迁三个已知页面。

| 处置 | navigation_id | 目标 |
| --- | --- | --- |
| 迁出企业或租户壳层 | `growth-analytics`、`customer-health`、`partner-portal`、`adapter-specs`、`adapter-contract-runs`、`competitor-comparison`、`language-settings` | 前三项进入平台增长/客户成功/渠道域；Adapter工程进入平台Provider工程台；竞品内容退出登录后产品UI；语言进入GlobalAccount壳层 |
| 平台/企业拆分 | `overview`、`admin-assistant`、`approvals`、`branding`、`provider-health`、`error-maps`、`model-prices`、`plans`、`billing-disputes`、`policy-templates`、`system-status`、`config-summary` | 同名能力使用不同主体、API、缓存和数据投影；企业只保留本Workspace治理、BYOK成本、订阅摘要、争议申请和Deployment诊断 |
| 企业保留但改名、归组、合并或降级 | `tenant`、`users`、`environments`、`local-models`、`response-cache`、`policy-simulator`、`intent-security`、`webhook-dead-letters`、`request-explanations`、`quickstart`、`sdk-examples`、`migration-guide`、`api-docs`、`file-security`、`mcp-tools`、`tenant-data-lifecycle`、`debug-console` | 分别进入企业资料/成员、模型接入、日志/文件详情、安全高级工具、真实向导、开发者中心和数据隐私；不再平铺全部菜单 |
| 企业AI治理原位保留 | 其余27项 | BYOK、模型/路由、项目/Key、用量/内部FinOps、额度/预算、策略/DLP、安全、日志审计、Prompt/文件/批处理等继续以tenant为owner并按职能角色显示 |

高风险迁移不能只改菜单：

| 现有实现事实 | 目标处置 | 保真要求 |
| --- | --- | --- |
| `/product-analytics/funnel`；`product_events/product_event_outbox`及触发器 | PRD-141平台增长，PRD-147切换 | 行数/hash/outbox水位和漏斗口径可复算；企业旧API固定拒绝 |
| `/customer-health`；`customer_health_scheduler_tenants/scores/score_outbox`及Scheduler | PRD-140平台客户成功，PRD-147切换 | 评分、Scheduler lease/outbox/审计水位保真；企业不见平台客户目录 |
| `/partners/*`；`partners/partner_customers/commission_records/outbox`及Scheduler | PRD-139平台渠道与独立伙伴入口，PRD-147切换 | 归属和佣金可对账；伙伴不能枚举非归属客户 |
| `/branding*`；`branding_configs/events/event_outbox`及Scheduler | PRD-142拆成平台品牌、渠道白标和Deployment外观 | 企业SaaS不能改平台登录页；域名/资源安全与事件水位保真 |
| `/adapter-specs`、`/adapter-contract-runs`、`/error-maps`；0055/0056/0046表与outbox | PRD-143平台Provider工程台；企业仅兼容摘要和受限覆盖 | CI case/Runner/内置目录不暴露；Workspace覆盖继续owner隔离 |
| `/plans`、`/billing-disputes`、`/model-prices`和企业FinOps/额度/预算 | PRD-144拆分平台商业账与企业内部成本账 | 套餐发布/争议裁决仅平台；企业只提交争议并管理BYOK成本、额度预算；金额版本可复算 |
| `/dashboard/*`、`/admin-assistant/query`、`/approvals/*`、`/internal/config/summary` | PRD-145拆分平台/Workspace共享页面 | 两域Session、来源、缓存、预取和迟到响应不互通；普通UI无研发配置 |
| Control `app.py`统一开关下安装的23个后台循环 | PRD-106建立生产Worker基线；PRD-143/144/147按业务域迁移 | 每个循环有唯一执行身份、queue owner、lease和水位；Web多副本不得重复承担无界Worker |

PRD-146按上述矩阵重组企业Gateway导航，默认折叠、单组展开并按角色显示任务首页；PRD-147统一执行expand/dual-read/backfill/cutover/contract和旧入口归零。任何历史表都不得因“迁出企业UI”被直接删除或重写tenant_id。

## 7. 迁移波次与退出条件

1. **W0 架构与清单**：PRD-090只冻结目标和迁移清单，不映射任何账号、不创建平台权限。退出：旧计划退役、0082基线和两轮自审通过。
2. **W1 身份与Workspace铺底**：PRD-091..094回填Workspace、已证明的Account和Membership并双写会话。同邮箱异hash/状态进入冲突队列，未经重新验证不暴露其他Workspace。退出：逐租户用户/角色/邀请/撤销Session/`tenant_data_states`水位一致，核心owner guard通过。
3. **W1.5 平台IAM bootstrap**：PRD-095通过显式一次性ceremony建立首个`platform_owner`；生产无默认邮箱，强MFA，双人见证或离线恢复密钥，全程审计并在成功后关闭入口。旧`admin@metahub.local`默认仍只是Workspace admin。退出：平台/租户路由互斥、bootstrap不可重复。
4. **W2 个人试点**：自助注册、个人Workspace、试用和真实调用。退出：无占位Provider被误判完成。
5. **W3 企业SaaS试点**：企业Owner、邀请、账单、Cell placement。退出：跨Workspace测试为0，账本可对账。
6. **W4 私有Gateway试点**：mTLS、DesiredState、LKG、本地Secret和遥测水位。退出：云端敏感明文0，断网/升级/回滚通过。
7. **W5 控制台分域迁移**：PRD-139..147按63项处置矩阵迁移平台运营、品牌、Provider工程、商业账务和共享页面，再重组企业角色导航。退出：历史事实保真，企业旧平台入口、DOM、预取和直达API归零。
8. **W6 正式发布**：PRD-148在PRD-138发布候选和W5全部通过后执行公开Go/No-Go。退出：72小时长稳、灾备、财务、隐私、安全和全产品门禁通过。

每波记录输入水位、schema版本、行数/hash、授权revision、回退开关、Owner和观察窗口。禁止跨波跳过P0门禁。

## 8. 故障、回滚与兼容

- 身份双写失败：不签发新Session；旧登录事实保持，不产生半Membership。
- 账号身份冲突：不自动选择hash/status或合并Membership；保留隔离shadow account并进入人工队列。
- Workspace切换失败：旧WorkspaceSession保持或明确失效，不把目标tenant写入前端Storage后继续请求。
- 兼容读回退：只切换读取实现，仍查询新deny overlay、`tenant_data_states`和撤销tombstone；不得复活被停用身份。
- 配置发布失败：Gateway保持未过期LKG并报告陈旧；不部分应用。
- 平台IAM不可用：`/platform`失败关闭；租户Gateway不受平台Console故障牵连。
- Edge断网：本地LKG继续服务并持久化允许字段水位；云端显示stale而不是伪造healthy。
- 路线图回滚：撤销`saas-architecture/v1`权威标记并恢复评审，不修改数据库、既有文档完成事实或旧Excel历史。

## 9. 性能与测试门禁

统一SLO见 `docs/architecture/saas-target-architecture.md`。PRD-090专项必须验证：

1. 新Excel从 PRD-090 起按当前登记范围连续、标题唯一、依赖无前向/自环、完成状态前缀连续；不设置最终编号或需求总数上限。
2. 旧Excel PRD-090..108与新Excel废弃映射逐条一致，状态全为“已废弃-禁止开发”。
3. 平台角色和租户角色code不重叠；所有PRD的范围、边界、权限、验收、依赖、迁移、安全、测试和SLO非空。
4. 默认Provider和竞品边界存在；两个总体完成标记保持false。
5. ADR、目标架构、威胁模型、迁移矩阵、63项导航处置、回滚和SLO链接完整。
6. 新增制品中真实Key、Token、密码、私钥、Prompt或Response明文命中为0。
7. 当前0082只读证据仍为`integrity_check=ok`、外键违规0；不创建0083。

## 10. 两轮编码前自审

### 第一轮：范围与架构一致性

结论：**通过，P0/P1已关闭**。独立现状审查发现旧Excel仍指向旧PRD-090、平台Owner自动映射、跨tenant同邮箱误合并、根表FK爆炸半径、`sessions`复合owner缺口、`users.role`无数据库约束、邀请和核心身份API遗漏、注销状态未进入授权判定、只迁渠道/客户/增长会遗漏白标/Provider工程/商业账务/共享页面，以及PRD-138先GA再迁移的时序冲突。修正为：旧计划活动状态显式退役；三种标准形态加一项离线异步能力；生产平台Owner只允许PRD-095显式ceremony；账号冲突失败关闭；258表/关键FK和身份API全部进入迁移矩阵；源码63项导航逐项处置；PRD-138改为发布候选，PRD-139..147完成分域，PRD-148才允许公开GA；回滚保留deny overlay。没有修改PRD-001..089完成事实，也没有新增迁移。

### 第二轮：安全、失败与可验证性

结论：**通过，P0/P1已关闭**。独立安全审查发现SupportGrant审批主体不精确、快照签名拼接不唯一、离线root撤销被夸大、撤权SLO冲突、Cookie/CSRF与混合凭据、Secret AAD及驻留owner矩阵缺口。修正为：申请人不得审批且客户/平台各一名不同审批者；Ed25519签带domain separator和protocol version的完整RFC 8785 envelope；trust-manifest TTL与双人离线recovery bundle；Workspace/Platform `<2s`和在线Device `<60s`分开；五类主体互斥；`__Host-` Cookie、Origin+CSRF和多凭据拒绝；Secret AAD六元绑定；SQL/cache/queue/outbox/object/telemetry/证据owner与data-class x location矩阵写入架构和威胁模型。验证器以失败关闭规则检查这些合同，不把章节标题存在当作通过。

## 11. 交付清单

- `docs/architecture/saas-target-architecture.md`
- `docs/security/saas-threat-model.md`
- `scripts/planning/validate_saas_roadmap.py`
- `docs/testing/PRD-090-tests.md`
- 更新后的 Excel、README 和目录说明

只有两轮自审问题关闭、专项验证全部通过、Excel状态更新并完成安全审计后，PRD-090才能标记完成；PRD-091在此前不得实现。
