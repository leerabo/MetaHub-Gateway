# MetaHub SaaS 增量开发路线图（PRD-090 起）

状态：**PRD-090..124 已完成，当前下一项为 PRD-125**

基线日期：2026-08-20

当前详细范围：PRD-090 至 PRD-148。该范围只是当前规划视窗，不限定需求总数或最终编号；后续从当前末尾连续追加。

## 1. 权威性与旧计划处理

- `企业级AI_Gateway_PRD开发总清单_给CodexClaude.xlsx` 继续保存 PRD-001 至 PRD-089 的已完成事实。
- 旧总清单中的 PRD-090 至 PRD-108 全部标记为历史废弃、禁止开发，不删除原文件，以便审计。
- PRD-090 起以根目录 `企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 和本文为唯一开发依据；两者冲突时，以 Excel 的 `01_PRD总表` 为准。
- 新计划不锁定永久总数。新增范围只能从当前末尾继续编号并记录变更，不能复活或覆盖旧 PRD-090..108。
- 当前状态为 `saas_incremental_roadmap_complete=false`；兼容旧口径继续保持 `all_108_prds_complete=false`。

## 2. 产品形态与硬边界

MetaHub 的目标形态包括个人 SaaS、企业 SaaS和云管私有 Gateway。平台团队负责 SaaS 总管理、运维、安全、财务和受控客户支持；个人和企业自助注册全局账号，再创建或加入 Workspace。登录不要求租户标识，账号通过 Membership 关联一个或多个 Workspace。

私有 Gateway 由客户部署数据面，云端管理设备身份、期望状态、版本、健康、授权和允许上传的遥测。本地 Provider Key/Token 默认只保存在客户环境。完全断网环境通过签名包异步导入导出，不能声称由云端实时管理。

平台角色与租户角色是两个独立命名空间。平台运营人员不会自动成为任何 Workspace 成员；支持访问必须经过客户授权、双人审批、限定时间和字段裁剪。所有租户业务数据默认携带 `tenant_id`/`workspace_id`，权限由后端逐请求校验，API 统一返回错误码和 `request_id`。

平台IAM分别承载管理、运维、安全、财务、客服、审核、合规、隐私、发布、Provider工程、渠道、客户成功和增长角色。企业Workspace的owner/admin只能添加本企业职能用户并在委派上限内分配开发、安全、财务、法务、审计和只读角色。导航由后端返回，但页面API仍逐请求鉴权。渠道、客户成功、增长、平台品牌、Provider工程、套餐目录、商业账务裁决和平台SRE只属于SaaS平台；企业Gateway保留BYOK、模型/路由、项目/Key、额度预算、用量/内部财务、员工权限、安全策略、日志审计、开发工具和私有部署。

| 产品域 | 必须承载的能力 | 主要角色与授权边界 |
| --- | --- | --- |
| 全局账号壳层 | 登录、注册、验证、账号安全、设备会话、全局偏好、Workspace选择 | 账号本人；GlobalSession不直接授予平台或企业业务权限 |
| SaaS平台IAM与支持 | 平台员工、平台角色、JIT SupportGrant、break-glass、平台审计 | 仅`platform_*`；企业owner/admin不能创建或授予平台角色 |
| SaaS租户与客户运营 | Workspace目录、生命周期、客户成功、健康、续费、工单和SLA | `platform_ops/platform_customer_success/platform_support`按最小字段；无SupportGrant不读企业业务正文 |
| SaaS渠道与增长 | 渠道伙伴、代理商、商机/客户归属、佣金摘要、获客漏斗、活动、实验和转化归因 | `platform_channel/platform_growth`；所有企业角色拒绝导航、DOM、预取和API |
| SaaS品牌与Provider工程 | SaaS主品牌、渠道白标、官方Adapter目录、合同CI、内置错误目录和发布候选 | `platform_owner/platform_channel/platform_provider_engineer/platform_security/release_manager`；企业只看本地Deployment外观或BYOK兼容摘要 |
| SaaS商业与SRE | 套餐目录、订阅权益、支付退款、发票、争议裁决、平台收入成本、基础设施、Cell/Fleet、事故、容量和发布 | `platform_billing/platform_ops/release_manager`按职责分离；平台运维不获得Provider工程维护或最终发布权 |
| 企业组织治理 | 企业资料、员工、成员、角色、权限、Owner转移和企业审批 | `owner/admin`在本Workspace委派上限内添加职能用户；后端逐请求校验Membership revision |
| 企业模型接入 | BYOK、官方/云Provider、本地OpenAI-compatible Endpoint、Provider健康、模型、别名、路由、Fallback和评测 | `owner/admin/developer`按读写职责；Secret只在企业/客户执行域 |
| 企业调用与FinOps | Project/Application、Virtual Key、额度、预算、用量、BYOK成本价、内部财务、企业账单摘要与争议申请 | `finance_admin`管理财务事实；技术角色只看获授权技术用量；平台商业裁决与企业内部账分域 |
| 企业安全与可观测 | 策略、DLP、会话/输出安全、Webhook、调用日志、错误诊断、安全事件和审计 | `security_admin/auditor/legal_admin`按字段和动作裁剪；隐藏页面不替代API权限 |
| 企业开发与数据工具 | Prompt、文件、文件安全、企业异步批处理、MCP、SDK/API文档、数据生命周期和私有Deployment | `owner/admin/developer/security_admin`按能力授权；文件正文、Prompt/Response和Secret不进入日志或平台支持证据 |

底层队列、通知、健康探测、品牌历史表或账务对账可以复用受审运行时，但“复用运行时”不等于共享工作台、API、Session、数据owner或角色。平台任务与企业批任务、平台品牌与本地Deployment外观、官方Provider工程与企业BYOK配置必须保持独立导航和后端授权。

现有tenant Console的64项导航已在Excel `11_现有导航处置`逐项分类，并绑定当前API/表/Scheduler证据、目标角色、承接PRD和切换门禁。归属计数固定为：45项企业Gateway保留，12项拆为独立平台/企业投影，5项完全迁入SaaS平台，1项迁到GlobalAccount壳层，1项退出登录后产品UI。任何后续实现不得只删除菜单，必须同时处理DOM、预取、直达API、数据owner、事件、Scheduler、审计、兼容和回滚。

完全迁入SaaS平台的是`growth-analytics`、`customer-health`、`partner-portal`、`adapter-specs`和`adapter-contract-runs`。`competitor-comparison`不属于平台运营工作台，必须退出登录后产品UI；`language-settings`属于账号本人而不是任何企业或平台角色。`overview`、`admin-assistant`、`approvals`、`branding`、`provider-health`、`error-maps`、`model-prices`、`plans`、`billing-disputes`、`policy-templates`、`system-status`和`config-summary`必须拆成独立主体、API、缓存、DTO和数据投影，不能继续共用tenant页面。

Provider Key、Token、Prompt、Response 和客户业务正文不得明文进入日志、事件、支持包或验收证据。成功、失败、取消、重试、用量和成本必须可追踪。默认上游仍为智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务及客户本地 OpenAI-compatible Endpoint；OpenRouter、硅基流动只作为竞品和迁移兼容对象。

## 3. 阶段路线

| 阶段 | PRD范围 | 目标 | 退出门禁 |
| --- | --- | --- | --- |
| S0 | PRD-090 | 冻结 SaaS、云边、身份、权限、迁移和安全架构 | 两轮设计自审关闭 P0/P1；旧 090..108 无活动开发引用 |
| S1 | PRD-091..094 | 全局账号、注册、Workspace、Membership 与安全切换 | 登录无租户标识；旧账号无损迁移；跨 Workspace 泄漏为 0 |
| S2 | PRD-095..105 | 平台运营、激活、生产基线与商业服务 | 平台/租户角色分离；从试用、计量到支付、退款、开票可对账 |
| S3 | PRD-106..113 | 任务、渐进发布、配置、SRE、风控、容灾与容量 | 任务可恢复；发布可回退；100+租户压力与多区域灾备达标 |
| S4 | PRD-114..119 | 企业身份、数据治理、机器身份和 Provider 目录 | SSO/SCIM/MFA 生命周期闭环；驻留与机器权限后端生效 |
| S5 | PRD-120..130 | 云管私有 Gateway 完整生命周期 | mTLS、LKG、本地 Secret、安装、升级、授权、恢复和断网包通过 |
| S6 | PRD-131..138 | 迁移、客户运营、隐私合规、安全和发布候选 | 四波试点；11 类 QA；72 小时长稳；生成RC但不公开GA |
| S7 | PRD-139..147 | 平台运营分域与企业Gateway重组 | 64项导航唯一处置；平台运营迁出；企业AI治理保留；历史事实保真 |
| S8 | PRD-148 | SaaS正式发布 | 全域迁移终验；72小时长稳；最终Go/No-Go会签通过 |

## 4. PRD 总表

| PRD | 阶段 | 优先级 | 需求 |
| --- | --- | --- | --- |
| PRD-090 | S0 | P0 | SaaS总体架构与兼容迁移基线 |
| PRD-091 | S1 | P0 | 全局用户账号与认证凭据 |
| PRD-092 | S1 | P0 | 个人与企业用户自助注册验证 |
| PRD-093 | S1 | P0 | 个人空间与企业Workspace创建 |
| PRD-094 | S1 | P0 | 成员关系、空间切换与邀请升级 |
| PRD-095 | S2 | P0 | SaaS平台运营角色与受控支持访问 |
| PRD-096 | S2 | P0 | 平台租户目录与生命周期运营台 |
| PRD-097 | S2 | P0 | 个人与企业初始化向导重构 |
| PRD-098 | S2 | P1 | SaaS套餐、试用、订阅与Entitlement |
| PRD-099 | S2 | P1 | 平台级用量计量、配额结算与账单摘要 |
| PRD-100 | S2 | P0 | PostgreSQL、Redis、对象存储与KMS生产底座 |
| PRD-101 | S2 | P0 | 多租户云Gateway池与区域路由 |
| PRD-102 | S2 | P0 | 企业认证、合同主体与开票资料 |
| PRD-103 | S2 | P0 | 订阅订单、支付、催缴与退款 |
| PRD-104 | S2 | P1 | 发票、税务资料与资金对账 |
| PRD-105 | S2 | P1 | 通知中心、消息模板与联系偏好 |
| PRD-106 | S3 | P0 | 分布式任务队列、Scheduler与Worker生产运行时 |
| PRD-107 | S3 | P0 | 功能开关、租户分群与渐进发布 |
| PRD-108 | S3 | P0 | 租户配置分发、缓存一致性与失效 |
| PRD-109 | S3 | P0 | SaaS可观测性、SLO与错误预算 |
| PRD-110 | S3 | P0 | 注册、API与资源反滥用及风控 |
| PRD-111 | S3 | P0 | 多区域高可用、备份与灾难恢复 |
| PRD-112 | S3 | P1 | 公共状态页、事故与维护沟通 |
| PRD-113 | S3 | P1 | 容量、性能、混沌与单位经济性 |
| PRD-114 | S4 | P0 | 企业域名、OIDC/SAML SSO与登录发现 |
| PRD-115 | S4 | P1 | SCIM目录同步与成员生命周期 |
| PRD-116 | S4 | P0 | MFA、条件访问、设备与会话安全 |
| PRD-117 | S4 | P0 | 数据驻留、保留策略与法律保全 |
| PRD-118 | S4 | P1 | Service Account、OAuth客户端与受限控制面API |
| PRD-119 | S4 | P1 | 官方Provider连接目录与区域合规接入 |
| PRD-120 | S5 | P0 | Edge Agent注册、设备身份与mTLS |
| PRD-121 | S5 | P0 | 云边期望状态、签名快照与离线LKG |
| PRD-122 | S5 | P0 | 私有网关遥测、用量与失败事实同步 |
| PRD-123 | S5 | P0 | 私有网关本地Secret托管与轮换 |
| PRD-124 | S5 | P0 | 云管私有Gateway安装包、预检与接入向导 |
| PRD-125 | S5 | P1 | 私有部署验证与脱敏支持包 |
| PRD-126 | S5 | P1 | Fleet资产、健康与受控远程诊断 |
| PRD-127 | S5 | P0 | Fleet分批升级、回滚与维护窗口 |
| PRD-128 | S5 | P1 | 私有Gateway License、容量与离线宽限 |
| PRD-129 | S5 | P0 | 私有Gateway备份、恢复与灾备演练 |
| PRD-130 | S5 | P1 | 隔离环境签名包异步管理与冲突合并 |
| PRD-131 | S6 | P0 | 旧租户、直连与私有部署迁移切换向导 |
| PRD-132 | S6 | P1 | 客户工单、SLA与安全诊断协作 |
| PRD-133 | S6 | P1 | 公共控制面API、SDK版本与废弃策略 |
| PRD-134 | S6 | P0 | 全局账号隐私、同意与数据主体请求 |
| PRD-135 | S6 | P0 | 等保、个保与企业审计证据中心 |
| PRD-136 | S6 | P0 | SBOM、制品签名、漏洞与发布证明 |
| PRD-137 | S6 | P0 | 租户隔离持续验证与安全攻防 |
| PRD-138 | S6 | P0 | SaaS发布候选、核心迁移波次与全产品门禁 |
| PRD-139 | S7 | P1 | 渠道伙伴、代理商与客户归属管理 |
| PRD-140 | S7 | P1 | SaaS平台客户管理、健康与续费协同 |
| PRD-141 | S7 | P1 | SaaS平台增长漏斗、活动与转化归因 |
| PRD-142 | S7 | P1 | 平台品牌、渠道白标与私有Gateway外观分域 |
| PRD-143 | S7 | P1 | Provider Adapter目录、合同测试与错误规则分域 |
| PRD-144 | S7 | P0 | 平台套餐账务与企业用量财务边界重构 |
| PRD-145 | S7 | P0 | 平台与Workspace总览、审批、助手、状态和设置分域 |
| PRD-146 | S7 | P1 | 企业Gateway角色导航、任务首页与高级入口重组 |
| PRD-147 | S7 | P0 | 存量控制台全面分域迁移与旧入口归零 |
| PRD-148 | S8 | P0 | SaaS公开发布、迁移终验与全产品最终门禁 |

## 5. 执行规则

1. PRD-090 架构基线至 PRD-124 云管私有 Gateway 安装包、预检与接入向导已完成；S5 已建立 0112..0116、设备证书信任、非自批签名期望状态、Agent 原子应用/持久 LKG、严格遥测白名单、本地 append-only spool、连续 ACK、Usage 幂等对账、本地信封加密/原子轮换、云端零明文引用投影、不可变安装制品、纯读预检和可恢复安装状态机，权威下一项为 PRD-125；G5 在 PRD-130 独立提交后统一复核。
2. 每条 PRD 先落盘详细设计，完成两轮编码前自审并关闭 P0/P1，再进入实现。
3. 开发阶段只运行受影响专项测试；完成门禁再运行必要回归，禁止用重复全量测试代替缺失的专项证据。
4. 每条 PRD 都必须覆盖 WB、API、BB、GR、PERF、WEB、UI、UCD、COMP、SEC、FLOW，并完成真实 API/浏览器/迁移或部署验收。
5. 数据变更采用 expand-migrate-contract；从空库执行升级，验证版本、`integrity_check=ok`、外键违规 0，并具备明确回滚边界。
6. 每条 PRD 完成后执行敏感信息、禁止文件、租户隔离和静止终态审计，更新 Excel、README、`docs/prd`、`docs/testing`，再提交并推送 `origin/main`。
7. 新增需求只能在当前末尾继续编号；改变既有范围必须形成版本化变更记录，不能静默改写已启动或已完成 PRD。

## 6. Excel 内容

工作簿包含变更说明、PRD 总表、阶段路线、核心数据对象、API 与事件、角色权限矩阵、11 类测试与验收、旧计划废弃映射、里程碑门禁、风险与决策、角色页面矩阵和64项现有导航处置，共12个Sheet。每条 PRD 在总表中列出范围、边界、页面、API、数据对象、事件、权限、验收、依赖、迁移/回滚、安全、测试、SLO、工作量、团队和交付证据。
