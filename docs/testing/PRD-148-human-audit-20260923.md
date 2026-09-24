# PRD-148 多身份人类式走查（2026-09-23）

本批先集中运行所有 Workspace 注册路由、Platform 运营入口和有数据域流程，再按结果集中处理问题。走查使用隔离 Chromium/Playwright，不是原生 computer-use，也不将 disposable seeded 数据描述为真实商业或 GA 验收。

## 已通过

- Workspace 组织侧：admin、developer、viewer/employee 各覆盖 60 个注册路由，共 180 个角色与路由组合。页面失败、请求/响应错误、控制台错误、横向溢出、控件裁切、交互重叠、未完成响应和禁止内容均为 0。四个分片证据位于 `.artifacts/product-qa/20260923-role-chunk-{01,02,03,04}/`。
- SaaS 平台运营：Platform session、控制台迁移和状态读取 2/2 在 1440x900、1280x720、390x844 通过，0 请求失败、0 控制台错误、0 溢出和 0 裁切。证据为 `.artifacts/product-qa/20260923-platform-operator-current/`。
- 有数据域：admin/developer 的 Support 创建、回复、升级、解决、评价、重开和刷新持久化通过；viewer 写入是预期 `403 support.permission_denied`。Device 的 viewer API 是预期 `403 workspace_session.permission_denied` 并回到员工可访问入口；Partner handoff 为 200，未建立 Partner membership 的归属入口为预期 403。证据为 `.artifacts/product-qa/20260923-populated-domains-current/`。

Platform 运营管理员仅使用 Platform session。组织管理员、Developer 和员工仅使用 Workspace session。组织 Provider、模型、路由、员工 KEY、预算、Support、Device、Partner 和私有 Gateway 的结果不被计入平台运营通过，反之亦然。

## 合并问题与处理

发现的唯一新增问题是运行器冷启动误超时。完整迁移和 seed 后，Control 应用在 Windows 冷启动时会超过此前固定的 35 秒服务就绪等待；该失败发生在浏览器启动前。运行器现改用 `METAHUB_QA_SERVICE_STARTUP_TIMEOUT_SECONDS`，默认 90 秒。该值只作用于隔离服务就绪，不改变页面、业务、RBAC、性能或浏览器断言。

此前 Chromium 登出 15 秒未回首页和 Workspace page-view 并发问题继续保留为真实失败，没有被本轮页面审计覆盖或降级。

## 本地账号入口复核

用户使用 `http://127.0.0.1:5173/identity.html` 时曾看到“暂时无法连接账号服务”。本轮在真实本地三服务进程中复现：Control 健康检查和账号页均为 `200`，但 `127.0.0.1:5173` 的跨域预检被默认只允许 `localhost:5173` 的配置拒绝。浏览器因此把匿名会话检查误认为网络故障。

`scripts/dev/start-control-api.ps1` 现在只在调用者没有显式设置 `METAHUB_ALLOWED_ORIGINS` 时，同时允许两个 loopback Console 来源；显式配置不被覆盖。重启 `scripts/dev/start-all.ps1` 后，两个来源的 `OPTIONS /identity/me` 均为 `200`，每个来源均收到自身的允许来源和 credentials 头；匿名 `GET /identity/me` 正确返回 `401 identity.session_required`。

仓库受控 Chromium 直接打开 `127.0.0.1:5173/identity.html` 后，登录表单可见，账号服务告警不可见。浏览器为预期匿名 `401` 记录了一条资源消息，但没有应用异常或请求传输失败。账号入口单测 `29/29` 和启动脚本 PowerShell 解析均通过。完整可追溯证据见 `outputs/prd148-local-console-loopback-20260923.json`。

这只解决本机开发地址的可用性，不能替代真实账号登录、Workspace 组织会话、SaaS Platform 会话或商业验收。Platform 运营管理员仍只使用 Platform session；组织管理员、Developer 和员工仍只使用 Workspace session。

## 公开页面与状态服务复核

集中公开走查发现 `status.html` 在桌面和手机均停留在“正在获取服务状态…”。根因是 `start-all.ps1` 只启动 Gateway、Control 和 Console，没有启动状态页固定依赖的 `127.0.0.1:8002` 读服务。新增 `start-status.ps1` 后，`start-all` 负责端口冲突检查、进程记录和启动，`stop-all` 使用同一受控进程树回收；本地状态服务仍只签发公开状态投影，不持有 Console 凭据或改变 Gateway KMS。

同一批还修复了找回密码深链：页内按钮此前可以打开找回表单，但 `?view=reset` 刷新后回到登录页。现在 URL 同步和页面装配都识别 `reset`，不触发登录、注册、组织或 Platform 操作。

四个本地服务均返回 `200`，公开状态 API 返回 generation `7` 的正常状态投影。账号入口和公开状态单测 `36/36` 通过；Chromium 在 `1440x900`、`1280x720`、`390x844` 访问首页、登录、注册、找回、隐私和状态页共 18 项，通过且无页面失败、应用异常、请求失败、5xx 或横向溢出。证据为 `outputs/prd148-public-runtime-current-20260923.json`。

账号入口源码造成发布资源摘要更新后，前端 lint 通过 `841` 个文件，生产构建通过 `295` 个资产；前后端摘要均为 `9c8516b2cf1a3332e64d7a87057841a5298d4744bb0c00c4a3db2d44a6b8a987`，控制面 manifest 对账专项 `43/43` 通过。首次并行 lint/build 的 `dist` 清理竞争，以及首次 pytest 访问系统临时目录的拒绝访问，均保留在该 JSON 的 `fixture_records`，后续顺序和工作区临时目录重跑才是有效结果。

本批未提交账号注册、登录、找回、组织或平台变更。因此它不证明 Platform 运营、Workspace 企业网关运营、付费权益或 GA；两类已登录身份证据继续严格分开。

## Platform 计量恢复与试用范围复核

Platform-only 计量恢复套件最初有两项失败，均由测试仍将完整迁移链的 head 硬编码为 `0149` 引起。`0149` 是计量恢复表本身的迁移，不是当前 SQLite 链的 head；测试实际得到 `db.migration_version_mismatch`。修复后的夹具先按当前完整链回滚 `0155` 到 `0150`，再验证 `0149` 在已有恢复动作时拒绝回滚；空库路径则完成 `0149` down/up 并重放原完整后缀。它没有放宽迁移或回滚断言。

`outputs/prd148-platform-meter-recovery-final-20260923.xml` 现在为 17/17 通过、0 failures、0 errors。范围仅限 Platform session 的 MFA、角色变更、CSRF、审计事务、恢复幂等性和迁移回滚；其中 Workspace session 和 Support session 被显式拒绝，不能代替组织管理员、员工或商业流程的验证。

本机当前 Gateway、Control 与状态服务健康，控制台、Gateway、Control 对本机局域网地址 `172.16.100.2` 的自请求均为 200。这个结果不证明另一台机器或公网可访问。当前健康的 `default` 组织路由指向 `kimi_real_prd148`，但另一个组织有活动 `company-default` 路由指向不健康 DeepSeek Provider，客户试用不得选择该组织。现有本地库还有 4 条待投影计量和 1 条 `platform_metering.owner_invalid` 失败；订阅和权益快照均为 0，因此本次只能作为受限技术试用，不能承诺付费权益、财务结算或 GA。详见 `outputs/prd148-trial-readiness-20260923.json`。

## Workspace 登出与访问事件重跑

历史 Chromium 失败 `outputs/prd148-browser-failure-20260916.json` 保留：当时登出后 15 秒内没有回到首页。当前源码在新的隔离 SQLite/WAL、Control、Gateway、Console 和 Chromium 环境完成三条 Workspace session journey：组织管理员 `1280x720`、Developer `1440x900`、员工 `390x844` 均通过。三条均为 0 请求失败、0 控制台错误、0 水平溢出、0 控件裁切和 0 交互重叠；Developer 的水合中点击登出也通过。浏览器报告在 `.artifacts/product-qa/20260923-logout-pagevisits-recheck/browser-real-chromium-scale-1.json`，摘要为 `outputs/prd148-workspace-browser-current-20260923.json`。

本轮隔离 Control 日志的 `/workspace/navigation/page-views` 写入成功，未出现 `workspace_session.unavailable`。它只表明当前 Workspace session 路由变更与登出回归不再复现旧问题；不外推为 scheduler-enabled 生产 SQLite 争用已经消失，也不能替代 SaaS Platform session 验收。

## 当前源码 Platform 复核

独立 Chromium Platform journey 在当前源码重新完成 MFA enrollment、Platform bootstrap、清除 Workspace session/CSRF cookie、Platform session 建立、控制台迁移状态刷新和三视口检查。迁移状态读取 `2/2` 都为 `200`；`1440x900`、`1280x720`、`390x844` 视口均为 0 请求失败、0 控制台错误、0 横向溢出、0 控件裁切和 0 退役运行时面板。证据为 `.artifacts/product-qa/20260923-platform-session-rerun/prd147-platform-chromium-scale-1.json` 与 `outputs/prd148-platform-browser-current-20260923.json`。该结果只属于 SaaS Platform operator 的 Platform session，不能计入组织管理员或员工的 Workspace 通过项。

## 未闭合边界

本批不证明真实付费权益与额度、生产企业治理/私有 Gateway、生产 Partner/Device 数据、发布复核/回滚、财务/隐私/安全/迁移对账或 HEAD/origin 一致。六方会签和连续 259200 秒稳定性观察按用户指示不执行，也不计为通过。`prd148_complete=false`、`ga_approval=false`。

## 当前源码角色重跑

面向受限技术试用的交付前，当前源码又在独立 SQLite、Control、Gateway、Console 与 Chromium 中执行全量 Workspace 路由审计。admin、developer、viewer/employee 各完成 60 个注册路由，合计 180 个角色与路由组合；三种角色的页面失败、4xx/5xx 响应、请求失败、控制台错误、横向溢出、控件裁切、交互重叠和未完成响应均为 0。已授权导航入口分别为 47、27、1，符合各自 RBAC 视图。

终态报告为 `.artifacts/product-qa/20260923-trial-handoff-role-audit/prd148-role-audit-chromium-scale-1.json`。源码绑定为 `outputs/prd148-source-current-20260923-trial-handoff.json`，2698 个文件、SHA-256 `b6855b993c3e7de2ddfb18d06781b0a6b030b9c481d42c553db4cb96d16d392d`，运行期间未漂移。本结果只建立当前页面、授权与布局基线；不证明真实注册到付费、公网/TLS、商业权益、外部设备连通性、GA 或生产稳定性。

同一源码的 SaaS Platform operator 复核使用独立 Platform session。MFA bootstrap、Workspace cookie 清理、Platform 会话建立、迁移状态读取 2/2 与 1440x900、1280x720、390x844 三视口均通过；请求失败和控制台错误均为 0，且没有横向溢出、控件裁切或退役运行时面板。终态报告为 `.artifacts/product-qa/20260923-trial-handoff-platform-audit/prd147-platform-chromium-scale-1.json`。该结论只覆盖 SaaS 运营身份，不能计入组织管理员、Developer 或员工的 Workspace 证据。

Device、Partner 和 Support 在当前源码上也完成正向数据路径及三角色边界复核。admin 的 Device 部署创建返回 201，一次性注册材料可见，关闭面板后刷新仍可见部署；Partner 活跃成员投影返回 200，两个页面均无溢出、裁切或交互重叠。admin/developer 的 Support 流程完成创建、回复、升级、解决、评价、重开与刷新持久化；viewer 创建被正确拒绝为 403。三角色均能完成显式 Partner handoff（200），随后由于没有 Partner membership 而接收预期 `403 partner.membership_required`；viewer Device API 也被正确拒绝为 `403 workspace_session.permission_denied`。终态报告分别为 `.artifacts/product-qa/20260923-trial-handoff-positive-domains/prd148-positive-domains-chromium-scale-1.json` 和 `.artifacts/product-qa/20260923-trial-handoff-populated-domains/prd148-populated-domains-chromium-scale-1.json`。

以上仅为隔离 seeded 数据走查，并非客户实例的真实设备、Partner 成员或商业流程。预期的启动会话 401、未签发 edge license 404 和导航卸载产生的 `ERR_ABORTED` 已由审计明确单列，均未计入意外浏览器错误。

公开入口当前源码审计覆盖 1440x900、1280x720、390x844 和 320x568。首页、登录、注册策略及找回入口均无横向溢出或控件裁切；错误密码登录正确返回 401 并显示错误信息；隔离环境中的注册策略明确关闭人机验证，未出现“准备中”残留、服务端 5xx 或控制台错误。终态报告为 `.artifacts/product-qa/20260923-trial-handoff-public-audit/prd148-public-audit-chromium-scale-1.json`。

Gateway 契约批次以当前源码完成 41/41：PRD-148 launch 状态机 14 项、协议门面与流式处理、默认模型解析、以及跨工具协议下员工个人 KEY 的身份、计量和停用回归均通过。跨协议集成单例耗时 203.163 秒，批次总耗时 587.274 秒，已产生完整 JUnit 终态 `outputs/prd148-trial-handoff-gateway-20260923.xml`。其中 launch 状态机仅验证隔离的签名、迁移、回滚和稳定性门禁逻辑；它不代表真实六方会签、用户已拒绝执行的 72 小时观察，或任何生产 GA 批准。

## Private Gateway Complexity Reduction

本批截图审视确认 Private Gateway 管理员页的功能、权限和几何均正确，但首次部署后的单一详情把安装、运行健康、交付、License、离线交换、备份、外观、升级、遥测、凭据、支持包和事件时间线全部平铺，超过组织管理员一次操作所需的信息量。Support 与移动员工页没有出现同类问题，因此修改集中在该页面，不扩展到其他域。

部署详情现在保留安装接入、部署身份、健康摘要和创建入口作为首屏。低频操作分为两个默认闭合的原生 details 组：“运行与交付”容纳健康诊断、期望状态、交付验证和最近事件；“维护与治理”容纳授权、备份、升级、遥测、本地凭据、支持及吊销。所有既有 data 属性、危险操作、API 请求、RBAC 判断和敏感字段保护保持不变，低权限离线交换视图不会显示 Secret 术语或数据。

初始实现曾在维护组说明中出现英文 `Secret`，触发了既有低权限敏感字段不可见断言；已改为中性的“本地凭据”，并以同一套件复验。另发现 LAN origin helper 在无浏览器 location 的模块导入中返回空 Control 地址，导致安装器生命周期单测在装配前失败；该路径已恢复 `http://localhost:8001` 默认值并新增回归断言。

Private Gateway 定向单测与 origin 单测 62/62 通过，lint 844 files 通过，生产构建 296 assets 通过，资源摘要同步为 `b480ed6d8dfc0aec1cd778a684dd8b3f229e896ef2ca5d5a4bcfda6685f24a29`，Control 前后端 manifest 对账 43/43 通过。新的隔离 Chromium 正向流程继续通过：Device 创建为 201、一次性材料可见且刷新后部署仍可见；Partner 成员投影为 200；无意外响应、请求、控制台、溢出、裁切或交互重叠。新截图为 `.artifacts/product-qa/20260923-private-gateway-layering/positive-domain-screenshots/admin-device-positive.png`。

随后补充了浏览器级操作验证：两个高级组在首次渲染时均为闭合，管理员点击后可展开，再次点击能返回紧凑首屏；Device 创建、一次性材料、刷新持久化和 Partner 投影仍通过，意外浏览器、请求和布局错误均为 0。终态为 `.artifacts/product-qa/20260923-private-gateway-layering-r2/prd148-positive-domains-chromium-scale-1.json`。

源码绑定为 `outputs/prd148-source-current-20260923-private-gateway-layering-r2.json`，2698 个文件、SHA-256 `d3e2954e9e51746973f0d974889de2f97f69eba364f11b9adcdc81d26e6594b5`。本批是当前源码的 UI 与回归证据，不证明公网、付费权益、结算、正式发布或 GA。

后续截图复核发现，安装器仍将四个当前选择项、六项环境要求、安装进度和下载动作平铺在同一个首屏。二次收敛只将 CPU、内存、磁盘、Runtime、端口和代理 / CA 放进默认闭合的“环境要求”原生明细；部署、版本、规格、架构、步骤和签名下载仍保持直接可见。没有改动安装 API、RBAC、一次性材料、下载、预检或验证动作。

当前源码的定向 Private Gateway 单测 12/12、lint 844 files、生产构建 296 assets 和 Control manifest 对账 8/8 通过。新的隔离 Chromium 正向域复验确认“环境要求”默认闭合、可展开并再次闭合；Device 创建为 201、一次性材料可见、刷新持久化和 Partner 投影 200 继续通过，意外请求、响应、控制台、溢出、裁切和交互重叠均为 0。终态为 `.artifacts/product-qa/20260923-installer-density-r3/prd148-positive-domains-chromium-scale-1.json`。资源摘要为 `dbf35088bbe1dd22faa4813e89679c03d30ac9629bcb3e5e3417dd533bf4e224`，源码绑定为 `outputs/prd148-source-current-20260923-installer-density-r3.json`，2698 个文件、SHA-256 `107409ad5ec76a7bb2a9d2c236fbbe53eb2ad08f9f77c350150db9ede28145ee`。这是后续 UI 变更的独立证据，不回写此前全路由或 Gateway 批次的源码绑定。

## Core Customer Task Interaction Partition

当前源码还以 admin、developer、viewer/employee 三种 Workspace 身份运行核心客户任务的可逆交互分区。每个身份覆盖快速开始、Provider、模型、默认路由、员工个人 KEY、预算、接入示例和 Private Gateway 共 8 个路由，合计 24 个角色与路由组合。审计执行真实登录、导航、筛选/刷新、折叠与弹层等可逆点击，不创建、批准、删除或撤销资源。

24/24 通过，页面失败、响应错误、请求失败、控制台错误、横向溢出、控件裁切、交互重叠、无标签按钮和无标签表单字段均为 0。终态为 `.artifacts/product-qa/20260923-core-task-interaction/prd148-human-interaction-chromium-scale-1.json`。这补强了高频客户任务的当前源码交互证据；它是聚焦分区，不替代已有的 180 路由静态角色审计，也不证明真实商业、外网或 GA。

## S1 Browser Recheck

早期运行曾在 `1280.onboarding-navigation.tenant` 的截图捕获中于 12 秒后超时。该失败保留在 `outputs/prd148-s1-browser-current-20260923.json`，不回写为夹具问题，也没有放宽业务或截图阈值。

此前在源码 `3051b…` 上的双视口重跑已通过。之后账号初始化页改为默认只显示当前步骤、将全部八步收进关闭的原生明细。该界面变更使旧 S1 脚本直接点击隐藏步骤，先后两次在 12 秒操作上限内正确失败；页面、API、资源与控制台均没有异常。脚本现先断言明细默认关闭，在每次选择隐藏步骤前执行可见的展开操作，再继续原有确认、撤销与深链断言，没有放宽任何门槛。

更新后的当前源码完成双视口重跑：`1280x900` 和 `390x844` 的注册、组织创建、邀请成员、Workspace 会话与重新进入、Provider 渐进配置视图、员工个人 KEY 的创建/轮换/停用、组织资料持久化、onboarding 确认与 16 个深链目标、两种 SDK 下载均通过。18 个浏览器步骤与 32 个已保存布局视图均无横向溢出或小输入字体问题；页面错误、资源错误和外部请求均为 0。终态为 `.artifacts/s1-browser-uft5otua/acceptance.json`，源码绑定为 `outputs/prd148-source-current-20260923-onboarding-browser-s3.json`（SHA-256 `c081131c0f5f4a2bda93ee5e80d145344f2138a138326c7f07ec7489e262eba4`）。

该流只调用本地 feedback 契约并使用 seeded Provider catalog；它不配置真实 Provider 凭据、不创建模型或默认路由、不调用 KIMI、不证明付费权益、额度、对账、公网或 GA。

## Deferred Mobile UX Observation

`OBS-UX-ACC-001`：当前 390px 账户设置截图没有溢出、裁切、重叠或不可操作控件，但账户侧栏的低频安全、设备、语言和组织治理入口仍会在初始化内容前形成较长的纵向扫描区。Workspace 控制台的同尺寸治理导航已采用折叠入口；账户设置尚未采用同一模式。该项不影响组织管理员在控制台配置 Provider、路由、员工 KEY 或进行 Gateway 试用，也不是权限或业务逻辑失败。它保留为后续与其他低频账户治理视图一起评审的复杂度观察，避免在客户试用前作零散导航调整。

`OBS-UX-NAV-002`：Workspace 会话的控制台用户菜单无条件显示“Partner 门户”。当前 admin、developer 与 viewer/employee 都可以执行 handoff（HTTP 200），但没有 Partner membership 时都会在目标域收到预期 `403 partner.membership_required`。这不是越权或后端失败，但普通组织用户在主操作区看到一个当前不可用的低频入口，增加扫描负担并制造错误预期。它与 `OBS-UX-ACC-001` 合并为后续低频跨域导航批次：需在保留有 membership 的 Partner 入口前提下，评审条件显示或移入次级导航；客户试用前不作零散调整。
