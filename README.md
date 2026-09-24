# MetaHub AI Gateway

2026-09-24 成员撤权与角色化后台续验：当前源码快照为 `outputs/prd148-source-current-20260924-member-key.json`，成员/使用者 9/9、Virtual Key 6/6、Gateway 员工跨协议精确回归 1/1、Console 角色与 KEY 回归 287/287；前端 lint 842 files、build 295 assets 通过，前后端生产资源摘要为 `be5564c28378fe2ab5e2d73c5c7bcd8efab194d4c87444c5010abb0bf2d2d87f`。移除成员会立即停用个人及管理员分配 KEY、关联使用者和活动 Workspace session，旧组织后台入口统一进入按角色过滤的 Console。宽 Gateway 集成因迁移夹具长时间无终态而中断，本地调度器仍有 lease/reconciliation/SQLite 警告；这些不构成 GA，PRD-148 保持未完成。详见 `outputs/prd148-member-key-current-20260924.json`。

2026-09-24 当前中文首发交付语义修正：组织管理员与 SaaS 平台管理员继续严格分域。组织管理员在“后台成员”中邀请、搜索、停用成员；不再使用管理员代发的“模型使用者与 KEY”员工目录。后台成员登录后自己创建、复制、轮换和撤销个人 MetaHub KEY，只能看到自己的 KEY 与个人用量；组织管理员在组织 KEY 页面管理组织级 KEY。模型服务页保留一个默认收起的“自定义接入（中转站 / OpenAI 兼容地址）”入口，品牌目录不是唯一选择。当前代码验证：前端全量 2239/2239，`npm run build` 294 assets，`npm run lint` 840 files；后端 Virtual Key、最新迁移链和生产资源摘要专项通过。生产资源摘要为 `c0ea8953b9c5bf12974dba01b0398a274ff276c707a1165a519ec04cafc7690a`。这些是当前源码与隔离测试证据，不等同于真实商业权益、生产 Partner/Device、发布回滚、对账或 GA；PRD-148 继续未完成。

2026-09-23 管理员接入闭环与控制台布局修正：管理员完成度只计算 Provider、模型/路由和员工 KEY，KEY 发放完成后即可交付，不等待员工第一次调用；首次调用仍由服务端记录为真实使用事实。员工 KEY 对话框提供“删除”操作，调用现有 DELETE 软禁用接口使凭据立即失效，并折叠保留历史计量/审计记录。控制台根容器改为使用可用视口宽度（桌面最大 1800px，窄屏自适应），避免内容长期挤在 1120px 中央区域。前端 lint 844 files、生产构建 296 assets 通过；`production-surface-manifest.json` 已同步当前资源摘要。PRD-148 仍未完成。

2026-09-23 账号初始化收敛后的 S1 双视口复验：旧脚本曾直接点击默认折叠区内的隐藏步骤，连续两次在 12 秒操作上限内超时；页面、API、资源和控制台没有异常。脚本现先断言“查看全部初始化步骤”默认关闭，并在每次选择隐藏步骤前执行可见的展开操作，不放宽任何操作、业务或截图门槛。当前源码在 `1280x900` 与 `390x844` 完成 18 个注册、组织、邀请、Workspace 会话、Provider 视图、员工个人 KEY、停用、初始化确认与 SDK 下载步骤，以及 16 个初始化深链和 32 个布局视图；页面错误、资源错误、外部请求、溢出和小输入字体问题均为 0。证据为 `.artifacts/s1-browser-uft5otua/acceptance.json`、`outputs/prd148-source-current-20260923-onboarding-browser-s3.json`，源码 SHA256 `c081131c0f5f4a2bda93ee5e80d145344f2138a138326c7f07ec7489e262eba4`。该验证只使用隔离 SQLite、seeded Provider catalog 和本地 feedback 契约，不证明 KIMI、付费权益、商业计量或 GA，PRD-148 保持未完成。

2026-09-22 当前源码五域负向矩阵复跑：Platform 38/38、Workspace 44/44、Partner/Support 16 通过并保留 1 个已退役 PRD-087 路由 skipped、Device 14/14；四个分片退出码均为 0，运行期间源码保持不变。证据为 `outputs/prd148-negative-current-20260922-zh/status.json`，绑定当前源码 SHA256 `008f081939eb89c7e45e1ab57db93be544b3889e9103eacc283684160b5be364`。这仍是隔离集成夹具矩阵，不关闭真实注册到付费/计量/撤权链路、企业治理/私有 Gateway、Partner/Device 生产数据、发布复核/回滚、财务隐私安全迁移对账或 HEAD/origin；按用户决定不执行 72 小时稳定性观察和六方会签，PRD-148 保持未完成。

2026-09-22 当前源码 GA Launch 状态机集成复跑 14/14 通过，证据为 `outputs/prd148-ga-launch-current-20260922-r2.json`。Gateway 协议回归中保留一项零费率迁移夹具成本断言漂移，不能写成产品失败或通过；真实 Gateway、付费额度和计量对账仍需业务链路证据。`prd148_complete` 继续为 `false`。

2026-09-22 迁移基线核查：在 `.tmp`、`.artifacts` 的数据库/压缩包和可达 Git 历史中没有找到 S1 要求的 `062ae8e3...f4c2a`，只找到旧基线 `d1634e8b...9fb03`。证据为 `outputs/prd148-baseline-search-20260922.json`；不替换基线、不制造匹配哈希，真实注册到撤权证据保持未闭合。

2026-09-22 中文三角色批量走查：隔离 Chromium 以 `zh-CN` 执行管理员 47 个、开发者 60 个、Viewer/员工 60 个授权入口（Viewer 使用 390x844 移动视口），页面、请求、控制台、布局、裁切、交互重叠和授权跳转均为 0 异常；随后对 `error-maps`、`language-settings` 做修复后定向复验，均通过。审计发现并修复高级工具导航分组直接显示 `advanced` 的中文文案问题，修改了导航分组映射、双语文案和契约测试。lint 841 files 通过，build 295 assets 通过并完成资源摘要对账。证据为 `outputs/prd148-human-audit-current-20260922-zh-admin.json`，当前源码 SHA256 `008f081939eb89c7e45e1ab57db93be544b3889e9103eacc283684160b5be364`，生产资源摘要 `ec08b64a4bf6c24ceae741e54500168d1f8fe4c77b23f1b2e46bcbda45f3fb88`。

2026-09-22 发布前剩余验收（中文首发优先）：前端/角色分区/GA Launch 等隔离回归已完成；当前仍有 6 个发布阻塞检查未闭合：真实注册到员工 KEY、Gateway 调用、计量/额度、撤权的完整链路；真实付费权益与额度；企业治理/私有 Gateway；Partner/Device 生产数据路径；当前源码绑定的五域负向矩阵重跑；HEAD 与 origin 对账。最新 finance 重跑在注册页提交按钮等待阶段超时，并记录 `gatewayScene.js` 资源请求失败，证据为 `.artifacts/s1-browser-rtrfvuij/browser.json`，不计为通过。当前源码快照为 `.tmp/current-source-snapshot-20260922.json`（SHA256 `0f708d8d67e22e6a7bdc3c965093c571f8bcf950b039670fb6d5655eb38335de`）。按用户决定，本轮不执行多语言扩展、72 小时稳定性观察或六方会签；这些项目不计为通过，也不阻止中文首发前的上述核心检查。

2026-09-22 当前源码 S1 真实浏览器重放：注册、创建组织、Provider/模型入口、成员邀请和个人 KEY/轮换停用前置动作均实际完成；在租户 onboarding 截图等待字体阶段超时，API 无 5xx、页面错误或待加载资源，因此仍不能宣称完整注册到撤权/付费链路通过。证据为 `.artifacts/s1-browser-r86v1rw6/acceptance.json`；脚本要求的 `062ae8e3...` 固定迁移基线仍缺失，使用的 `d1634e8b...` 仅是旧脚本基线副本。

同一基线的 finance 分支两次在业务断言前被运行器阻塞：一次为身份静态资源请求失败，另一次为 quickstart shell 未挂载并记录 Windows access violation。证据为 `.artifacts/s1-browser-hmc0dc5u/browser.json` 和 `.artifacts/s1-browser-n3md83li/browser.json`；财务、额度和商业计量仍没有当前真实流程通过证据。

2026-09-22 当前源码追加复验：历史曾报异常的 `environments`、`providers`、`provider-health`、`policy-snapshots` 已按三角色重跑 12 个页面目标，页面、请求、响应、控制台、布局、交互和授权异常均为 0；控制面 GA Launch 集成 14/14 通过（0 failures、0 errors、0 skipped）。证据分别为 `.artifacts/product-qa/20260922-targeted-historical-failures/prd148-role-audit-chromium-scale-1.json` 和 `outputs/prd148-ga-launch-current-20260922.xml`。这些仍是隔离 seeded/集成合同证据，真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、发布回滚、财务/隐私/安全/迁移对账和 HEAD/origin 未闭合，PRD-148 保持未完成。

2026-09-22 OpenRouter 对标修改已收口：管理员默认入口为四个工作域，首屏只保留当前下一步；财务工作台先显示例外状态；员工交付固定为一个统一 Gateway 地址和一把个人 MetaHub KEY；空态、权限矩阵、英文静态与异步面板已完成当前源码回归；管理员工作台已去除卡中卡。当前证据为 `outputs/prd148-openrouter-card-band-current-20260922.json`，源码 SHA256 `ceb9be5281b8bcf3419f12cc0714540d4bec1232c029cbb34ab30fff2547394b`，前端全量 2229/2229、lint 841 files、build 295 assets/277 modules 通过。六方会签与 scheduler-enabled 连续 259200 秒稳定性验证按用户指示不执行，不构成通过证据；真实注册到付费、生产 Partner/Device、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-22 管理员分域与中文导航修复：SaaS 平台运营管理员只使用 Platform session，Workspace 组织管理员、Developer 和员工只使用 Workspace session；平台迁移、平台账务和发布门禁证据不与组织 Provider、模型、路由、员工 KEY、预算或私有 Gateway 混用。中文高级工具导航过渡标题已统一将内部 `advanced` 映射为“高级工具”，内部 route/group code 与 RBAC 不变。当前源码证据为 `outputs/prd148-source-current-20260922-advanced-group-localization-final.json`，SHA256 `a365a6cc351f6fad4f9b1a4c2319ac2b007343ea72aa948fcfd1e63319d36469`；前端全量 2233/2233、lint 841 files、build 295 assets/277 modules、控制面摘要契约定向测试均通过。`prd148_complete=false`、`ga_approval=false` 继续保持。

2026-09-21 OpenRouter 对标第十二批完成当前发布资源对账：前端全量单测 2224/2224、`npm run lint` 838 files、顺序 `npm run build` 294 assets/276 modules 通过；前后端 production manifest 已统一到资源摘要 `2383e3599edfec3a658080fdb2408df9828ca5132fc841cdf341ada611a436c7`。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-release-reconciled.json`，SHA256 `31c274c380b5f587d8d8a271e79bbc6431a6fe2372f3cba669f4addd0997b15d`。此前构建摘要漂移已按发布资源对账问题修正；真实注册到付费、企业治理/私有 Gateway、Partner/Device 真实数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第十一批完成状态优先空态收敛：Partner 无归属 Workspace、Platform 无数据域、私有 Gateway 无部署时均给出原因和下一步；角色权限矩阵增加按职责分组查看提示，降低 643 项权限一次性扫描负担。前端全量单测 2223/2223、`npm run lint` 838 files、生产构建 294 assets/276 modules 通过；当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-empty-states.json`，SHA256 `5bf1230c779fd79b26349be5320f6258634ea922d09ca7f049696798b79348af`，生产资源摘要 `5fd93fb48d28691c622e48cdf9e572aed1f44b6b5723f69749ff2bd2fd08ac6c`。浏览器分区复验本轮在数据库准备阶段再次挂起，已保留运行目录和环境阻塞事实；真实注册到付费、企业治理/私有 Gateway、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第十批已完成角色化首屏的语言一致性：`en-US` 管理员的四个工作域、唯一下一步、完成态、折叠步骤和低频治理任务不再混入中文；英文员工接入页的统一 Gateway、个人 MetaHub KEY、协议和客户端说明也统一为英文。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-i18n.json`，SHA256 `b1306d4de931297ba04bc579d720d2e721e0d03530bc13e1c6d2225fcdfdfceb`，生产资源摘要 `456d31dee44283f5b0175ba5f37e5cecae5c8e9da12912fda12332a6bb965dc3`；`humanWalkthrough` 49/49、前端全量 2223/2223、lint 838 files、build 294 assets/276 modules 通过。完整差距和边界见 [OpenRouter 体验差距分析](docs/operations/OpenRouter-UX-gap-analysis-20260920.md) 与 [PRD-148 OpenRouter 人类走查](docs/testing/PRD-148-openrouter-human-audit-20260921.md)。这些仍是当前源码和隔离回归证据，真实注册到付费、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第八批完成状态卡的最后一处交互缺口：管理员和财务状态卡在域已完成时保留后端授权的“查看配置”入口，不再停在不可点击的状态文字；员工 viewer 当前源码分区走查 60/60 页通过（batch1-6），无请求失败、响应错误、控制台错误或布局问题。新增前端单测后全量为 2,221/2,221，`npm run lint` 838 files、`npm run build` 294 assets/276 modules 通过。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-status-card-review-r2.json`，SHA256 `4c724b6602cdd7fe34de487db5b261330cf93c261caeec3355592380423bb937`，生产资源摘要 `604e687220b841bc717f807d6cc76eb7efcc90c8c77d0932fb655f2445097e54`。这些仍是当前源码和隔离 seeded 证据；真实注册到付费、六方会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第七批已完成连续接入向导的最后一层收敛：管理员首页在四域状态卡下显示 Provider → 模型 → 员工 KEY → 首次调用进度条；被阻塞或处理中且没有动作的工作域明确显示“等待前置条件/处理中”，不再误报“已就绪”。前端全量单测 2,220/2,220、`npm run lint` 838 files、`npm run build` 294 assets 通过。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-progress.json`，SHA256 `e0064c47ed721be544d9aaf89644f0d292ba1ee3ec58cbd93662590659174408`，生产资源摘要 `7bc3004734f98685f61b6b03676c158b48f751b0f3ac89adc50bcefbc0f63a4e`。真实商业链路、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第六批已补齐空态：管理员在没有任务事实时仍显示四个“待确认”工作域，财务管理员仍显示“财务与风险”工作台，已完成域显示“已完成”。受影响单测 81/81、lint 838 files、生产构建 294 assets 通过；当前源码 SHA256 为 `afa034615dbe88d9d55f67d9687174ec36079d07910b12f16e1180af8b56db29`，生产资源摘要为 `dfeca42ba38d9a841e47c7a182858062200d84d1031602b9b3f01b5dcdc731a5`。真实商业链路、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第四批：page-view 遥测改为 deferred read 预检加短写事务，降低任务首页与导航偏好并发时的 SQLite 写锁争用；page-view 集成回归 7/7，隔离 Chromium Workspace FLOW 3/3，均无 503、请求失败或控制台错误。源码快照为 `outputs/prd148-source-current-20260921-openrouter-pageview-fix.json`，SHA256 `1ee644602250309fb74c18f158deb9b1c6180a92671189e554f0e08c0bdab111`。这只证明当前隔离环境的修复有效，真实商业链路、付费权益、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标正向域复验：隔离 seeded 当前迁移头走查完成 Device 创建（201、刷新后可见）和 Partner workspace attribution（200），两页布局均无溢出、裁切或交互重叠；此前两次严格选择器失败已保留并归类为测试夹具问题。证据见 `.artifacts/product-qa/20260921-positive-domains-r3/prd148-positive-domains-chromium-scale-1.json`，当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-final.json`，SHA256 `2344f9f626d82cf4ffed1210ed7c431389fb39a4e09c241637b523557f97c5dd`。该批不替代真实商业、六方会签、回滚、对账、正式 259200 秒稳定性或 HEAD/origin，PRD-148 继续未完成。

2026-09-21 OpenRouter 对标第三批已落地：财务管理员默认任务页先显示“财务与风险”状态卡，原始财务任务收进明细；管理员四域卡缺少后端事实时显示“待确认/等待状态”，不再误报“已就绪”。前端全量单测 2,216/2,216、`npm run lint` 838 files、`npm run build` 294 assets/276 modules 通过。当前源码快照为 `outputs/prd148-source-current-20260921-finance-workbench.json`，SHA256 `e808e0df3da4222cda2165dd173020e9712e0a7421691fab231a2cd2c86fb080`，生产资源摘要为 `0925b588ff39e58c571b8c71aa87b99d0480dead1c683f837064f65f95641de4`。这只完成 OpenRouter 体验建议的当前源码修改，PRD-148 真实商业、Partner/Device 正向数据、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未完成。

2026-09-21 OpenRouter 对标集中复验：平台运营三视口通过；Support 管理员/开发者完成创建、回复、升级、解决、评价、重开和刷新持久化，Viewer 写入正确拒绝；Device 仍是空部署态，Partner 仅有无成员 403 边界。首次 Workspace FLOW-01 的任务首页并发写锁 503 已修复，后端导航 6/6、Workspace FLOW-01/02/03 3/3、前端单测 2,214/2,214、lint 838 files、build 294 assets 通过。当前源码 SHA256 为 `6ef4d4e845c33970938171047f03bb2148282fda59e4329545f4dd53ff25e9ba`，证据见 `docs/testing/PRD-148-openrouter-human-audit-20260921.md`。该批仍是隔离 seeded QA，PRD-148 继续未完成。
当前源码全路由角色审计随后复验通过：admin/developer/viewer 全部授权路由无页面、请求、控制台或布局问题，证据为 `.artifacts/product-qa/20260921-role-audit-r2/prd148-role-audit-chromium-scale-1.json`。该结果不替代真实 Partner/Device 正向数据、商业权益、会签、回滚、对账、正式稳定性或 HEAD/origin 证据。

2026-09-21 OpenRouter 对标落地第二批：员工（当前 `viewer` 成员角色）默认进入“我的接入”，只看到统一 Gateway、个人 MetaHub KEY 和客户端交付规则；`owner`/`admin` 任务首页增加“接入与成员、模型与网关、用量与风险、治理与运维”四域状态卡。授权深链和后端权限契约保留。当前源码快照为 `outputs/prd148-source-current-20260921-ux-final.json`，生产资源摘要为 `507cb0d8922e592149dca76d686f67e4988fb112123740c0e9face7475bf93ee`；受影响单测 77/77、前端全量单测 2,214/2,214、lint 838 files、build 294 assets/276 modules 通过。PRD-148 仍未完成，真实商业与正式 GA 门禁未闭合。对标分析见 `docs/operations/OpenRouter-UX-gap-analysis-20260920.md`。

2026-09-21 交互审计器后续复验：修复侧栏手风琴偏好 PATCH 与后续详情/打开动作之间的 teardown 竞速。当前源码聚焦 developer 六页（models、model-aliases、model-quality-scores、experiments、projects、virtual-keys）6/6 通过，0 请求失败、0 响应错误、0 控制台错误和 0 布局问题；证据为 `.artifacts/product-qa/20260921-interaction-developer-settle-fix3/prd148-human-interaction-chromium-scale-1.json`，源码 SHA256 为 `ee904c92faed340fd4508280e8932b70c5d976a89b8b442c067ef1eb3190b3a0`。修复后的三角色 180 页全量批次在 1200 秒门槛内超时且没有终态 JSON，仍不能替代完整交互门禁。单测 2211/2211、build 294 assets、lint 838 files 通过；真实注册到付费、企业治理/私有 Gateway、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。权威索引为 `outputs/prd148-current-evidence-index-followup-20260921-r2.json`。

角色分区复验随后完成：admin 60/60、developer 60/60、viewer 60/60，共 180/180 页；每组 0 请求失败、0 响应错误、0 控制台错误、0 布局/标签/授权问题。证据目录见同一权威索引的 `partitioned_three_role_interaction`。统一 180 页串行批次的 1200 秒超时仍保留，不能把分区结果改写成统一批次终态。

2026-09-21 当前源码浏览器复验：生产构建 294 assets/276 modules、lint 838 files、前端单测 2,211/2,211 通过；当前源码 SHA256 为 `369d9050a49e81d176fbd5a440e4d53a903ca21af3d40f02d148efec6c5dee82`（2,693 个受测文件），生产资源摘要为 `69ac6068f2bc9c1fb4415db527049197d3ca1a17c846ea5cc02c596298f99bce`。完整 Web 矩阵 `.artifacts/product-qa/20260921-021856` 为 10/11，Chromium 1.25x 原始矩阵失败仍保留；独立重跑通过但不能覆盖原始失败。Viewer 交互复验 `.artifacts/product-qa/20260921-interaction-ux-classified` 4/4 通过；当前源码完整交互批次 `.artifacts/product-qa/20260921-interaction-after-batch` 超时并记录导航偏好 PATCH 422，聚焦管理员批次通过，开发者批次另有页面关闭时 PATCH `net::ERR_ABORTED`，均不能替代完整当前源码交互门禁。权威增量索引为 `outputs/prd148-current-evidence-index-followup-20260921.json`。真实注册到付费、企业治理/私有 Gateway、Partner/Device 数据路径、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-20 OpenRouter 体验对标后的导航收敛批次：MetaHub 按成员职责收敛默认侧栏，并将未完成当前任务的入口放入“更多控制”，保留全部授权深链和后端契约。前端全量单测 2,209/2,209、lint 838 files、生产构建 294 assets 通过，当前资源摘要为 `8a0dad0777857e6e375be96df0e8c1dd0a9935c268e8635bc6f23429274fb798`。当前源码封存为 `outputs/prd148-source-current-20260920-r18.json`，SHA256 为 `f9b7f49499af1072ae1888b2719659c4632ceee7d7b4e4059254d37f4ca22696`。修复前交互走查发现角色权限页保存条遮挡两个复选框，修复后 Chromium 三角色 180/180 页通过，证据为 `.artifacts/product-qa/20260920-interaction-navigation-r9/prd148-human-interaction-chromium-scale-1.json`；当前默认侧栏为管理员 12、开发者 11、查看者 4，后置入口为 20、7、3，完整授权入口仍为 47、27、7，见 `.artifacts/product-qa/20260920-role-audit-navigation-r10/prd148-role-audit-chromium-scale-1.json`。这批是 seeded/local 浏览器证据；真实注册到付费、企业治理/私有 Gateway、Partner/Device 数据路径、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-20 page-view 写入串行化修复：Workspace 快速切换时的遥测 writer 现在按顺序提交，并保留当前会话与取消闸门。受影响单测 17/17、lint 838 files、生产构建 294 assets 通过；资源摘要为 `ab7194634d27e6cbbe51caf72346f04ce5b677d98b0743e2083769179614ad53`，当前源码 SHA256 为 `dcfcd93cab153698df52cafcb5f47455000b985d943dcc115efd16ee7edaa0e7`（2691 个受测文件）。这只关闭了串行写入的局部回归，真实 S1 截图/page-view 尾部、五域当前快照、商业/治理/会签/回滚/对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合；PRD-148 保持未完成，Excel `01_PRD总表!B60` 继续为“未启动”。

前端全量回归随后为 2,202/2,202，证据 `outputs/prd148-frontend-full-regression-current-20260920-r9.json`。

2026-09-20 当前源码人类式角色批次：admin、developer、viewer 各 60 个 Workspace 路由，共 180/180 页通过；三角色真实业务旅程 FLOW-01/02/03、Platform 运营迁移/MFA 走查、公共入口四视口走查均通过。证据汇总为 `outputs/prd148-human-role-batch-current-20260920-r3.json`，绑定源码 `dcfcd93cab153698df52cafcb5f47455000b985d943dcc115efd16ee7edaa0e7`。Partner 专用 Chromium 验收在页面操作前因旧 PRD-087 harness 要求只应用 0080、当前迁移器实际继续应用 73 个后续迁移而失败；该项按迁移基线漂移记录，未宣称 Partner 通过。商业、真实注册到付费、企业治理/私有 Gateway、会签、回滚、对账、正式稳定性和 HEAD/origin 仍未闭合。

Partner/Support 当前源码聚焦回归：前端 21/21、后端 10/10 通过；Partner 浏览器页面证据仍等待迁移基线修正，未用这些契约测试替代真实页面验收。

权威 Excel 已同步本批走查记录，`01_PRD总表!B60=未启动`，当前工作簿 SHA256 为 `b278257ab680b379d74f4897aba75be05f4be60974c8bd79d8a7a4534a4524a3`；输出副本为 `outputs/prd148-workbook-update-current-20260920-r2/output.xlsx`。

2026-09-20 资源读取锁边界批次：当前源码 SHA256 `6afa45fceb7e36b27bdaa28c79894b83cb239e1b146d197bf8196b5b94ce552b`（2691 个受测文件）上，项目/环境回归 6/6、导航与 page-visit 回归 14/14 通过；`GET /projects`、`GET /environments` 的 Workspace 只读认证使用 `touch=False`，不再更新 last-seen。真实 S1 尾部截图/page-view 失败、五域源码漂移、商业与 GA 门禁仍开放，Excel `01_PRD总表!B60` 继续为“未启动”。

认证 deadline 与 Workspace 资源写入回归 11/11 通过。完整 S1 重放本轮未启动，因为验收脚本要求的受保护计量基线缺失（需要 `METAHUB_TEST_METER_BASELINE` 且摘要为 `d1634e8b...`）；该结果只记为测试基线证据不足，不作为产品通过或失败。

2026-09-20 PRD-148 当前验收仍未完成。当前源码 SHA256 为 `6713a21a301d9b7cfb526df7dcf5b53e98dfd79a82d869ef08a14c86d17c0392`（2691 个受测文件）；最近完整三角色全路由人类式交互审计共 180/180 页通过。员工 KEY 名称可见性、连续发放面板和撤权档案可见性已修复，前端全量回归 2,201/2,201、lint 838 files，当前 294 assets 生产构建资源摘要为 `fc980ad1de3e46ebb7fe326205ee7c102f9c59c091c22dc08c355485f0c91c7c`。真实 S1 重放已到员工 KEY、Gateway、计量路径和撤权后的 Gateway 401，但账户引导导航截图仍超时并伴随延迟 page-view/字体请求，不能宣称完整真实链路。隔离真实管理员旅程还复现了间歇性 `/workspace/navigation/page-views` 503，以及导航偏好在初始手风琴操作中重复发送 revision 0 的 PATCH；两者均保留为开放真实问题，详见权威索引。本机试用没有付费权益；真实付费/治理/私有 Gateway、六方会签、发布回滚、财务隐私安全迁移对账、正式 259200 秒稳定性和 HEAD/origin 一致仍未闭合；Excel `01_PRD总表!B60` 保持“未启动”。权威索引：`outputs/prd148-current-evidence-index-final-20260916.json`。

当前源码三角色人类式交互清查已重新完成：admin、developer、viewer 各 60 个 Workspace 路由，共 180/180 页；0 页面失败、0 请求/响应错误、0 溢出、0 交互重叠、0 未标记控件。证据为 `outputs/prd148-human-interaction-current-20260920.json` 和 `.artifacts/prd148-human-interaction-current-20260920-r1/prd148-human-interaction-chromium-scale-1.json`，绑定当前源码 `0091a551460cacf86d0a6db922e08c8dd040608c091bb4d719102c3e22df7232`。这是非破坏性页面与可逆控件清查，不关闭真实注册/邀请/Provider/员工 KEY/Gateway 全链路、商业权益、六方会签、发布回滚、财务隐私安全迁移对账、正式 259200 秒稳定性或 HEAD/origin 门禁。

五域终态批次 `outputs/prd148-negative-current-final-20260916/status.json` 的 Platform 38/38、Workspace 44/44、Partner/Support 17（含 1 skipped）、Device 14/14 均无失败或错误，但起止源码 SHA256 从 `604cab2a...` 变为 `82168788...`，`source_unchanged=false`，因此只作为源码漂移记录，不能宣称当前源码或 GA 通过。

2026-09-19 导航会话冲突修复：真实本机浏览器复现的过期导航偏好 `412 identity.revision_conflict` 不再删除全局或 Workspace Cookie；冲突后 `/identity/me` 与 `/workspace/navigation` 均保持 200。PRD-146 定向 CAS 回归和 PRD-091 登出 Cookie 回归通过，证据见 `outputs/prd148-navigation-cookie-regression-20260919.json` 与 [导航会话回归记录](docs/testing/PRD-148-navigation-session-regression-20260919.md)。修复使此前审计中的后续 401 问题关闭，但本次源码变更使旧五域/Gateway 制品需要重新绑定；PRD-148 仍未完成。

2026-09-19 当前源码重新走查：隔离 Chromium 角色页面审计 admin/developer/viewer 共 180 页通过，三角色业务旅程 FLOW-01/02/03 通过，均绑定源码 `7537aee46d2e90b358ebd7db349cb66f9d0baf1b394d8495156596021690f136`。真实注册→组织创建尝试中，注册、组织创建和会话切换 API 成功，但隔离静态服务出现多个 `/src` 模块请求失败，Quickstart 未加载并记录 Windows 访问冲突；该批归为验收夹具/运行环境失败，不能宣称 Provider、员工 KEY 或 Gateway 通过。证据见 `outputs/prd148-current-evidence-index-final-20260916.json` 与 `.artifacts/s1-browser-hw00nz0y/acceptance.json`，PRD-148 继续未完成。
2026-09-19 当前源码本机真实试用链路已形成脱敏证据：登录、组织空间、Provider/模型/默认路由读取、员工消费者、个人 KEY、真实 Gateway、usage summary/records、预算状态、撤权及撤权后 Gateway 401 均已验证；证据为 `outputs/prd148-real-current-flow-20260919.json`。`quota-buckets` 的工作区 Cookie 请求返回 401，当前租户没有付费套餐/权益且该接口要求 Bearer 会话，因此额度与商业证据仍不足。平台 P95 单项在相同 `<50ms` 门槛下当前源码 1/1 通过，历史超时仍保留为运行波动证据；见 `outputs/prd148-platform-p95-current-20260919.json`。这些证据不构成生产 GA，PRD-148 仍未完成。
2026-09-19 当前源码五域负向矩阵 r2 已完成且起止快照一致：Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个退役入口 skipped、Device 14/14，证据为 `outputs/prd148-negative-current-20260919-r2/status.json`。首次 Windows 参数拆分与重复 Workspace 运行器问题均保留并单独分类，不计为产品失败；五域结果也不关闭真实商业、会签、回滚、对账、正式 259200 秒稳定性或 HEAD/origin 门禁。
当前源码前端 `npm run lint`（838 files）与 `npm run build`（294 assets，276 modules）通过，当前源码快照为 `15f1147f7e9135d7a1e2da4fafdf534ebbdef42c74eb07ed075cb55d01ce30c6`（2690 个受测文件）；该静态 QA 不改变 PRD-148 未完成状态。

Chromium 1× 登出失败已在当前源码隔离环境重放：三角色旅程整体通过，原始 15000ms `waitForURL` 超时未复现。原始失败仍保留为真实历史失败，不能据此降级为夹具问题，也不能关闭浏览器或 GA 门禁；重放证据为 `outputs/prd148-browser-failure-rerun-20260919/browser-real-chromium-scale-1.json`，索引已记录该分类。

2026-09-19 三角色人类式全路由交互走查已完成：管理员、开发者、查看者各遍历 60 个 Workspace 路由，共 180/180 页；可逆侧栏、筛选、标签、详情、关闭和刷新控件共记录 421 次交互，0 页面/API/控制台/布局/标签问题。该批 QA 源码快照含审计器，为 `15f1147f7e9135d7a1e2da4fafdf534ebbdef42c74eb07ed075cb55d01ce30c6`（2690 文件），证据见 `outputs/prd148-human-interaction-current-20260919/evidence-manifest.json`。破坏性业务动作仍由专门流程验收；真实商业、发布、回滚、对账、正式稳定性和 HEAD/origin 门禁仍未闭合。

MetaHub 是面向中国境内企业用户的企业级 AI Gateway + 治理控制面。它提供统一模型调用入口、控制面治理、成本与用量追踪、日志和调试能力。

重要边界：

- OpenRouter 和硅基流动是对标平台、竞品平台和迁移兼容对象，不是默认模型厂家。
- 默认上游是模型厂商或云端模型 Endpoint，例如智谱、通义、文心、火山、混元、DeepSeek 官方或云上服务，以及客户本地 OpenAI-compatible Endpoint。
- MVP 不做硬件盒子、不做 MCP 主链路、不做信创、不做复杂数据出境；这些能力放到 M2/M3 或可选模块。

## SaaS 增量路线图

2026-09-18 Shell 与 Workspace 任务导航本轮走查修复：隔离英文偏好管理员时，组织管理、账户操作、返回空间、任务进度、连接状态、高级工具和侧栏折叠按钮等全局文案此前仍有中文硬编码；现已统一接入 `zh-CN`/`en-US` 字典，并沿主渲染链传递活动 locale。受影响前端单测 2200/2200、`npm run lint`（838 files）和 `npm run build`（294 assets）通过。当前源码快照为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `b567e0133e5516f3c886384c50dd249b7a9aa22cf040347554e0b7cebc4e12f4`；r9 隔离 Chromium 角色审计已完成 admin/developer/viewer 各 60 页，共 180 页，0 页面/API/浏览器错误、0 溢出、0 裁切、0 交互重叠，且英文样本显示 `Collapse navigation`/`Model providers`，证据见 `outputs/prd148-role-audit-current-20260918-r9/evidence-manifest.json`。本批关闭 `OBS-UI-003`，不改变真实商业、会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 未闭合状态。

同一当前源码上的五域负向矩阵 r4 已通过：Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个明确退役入口 skipped、Device 14/14；证据为 `outputs/prd148-negative-current-20260918-r4/status.json`。首次共享 pytest 临时目录权限错误通过按域独立 basetemp 重跑解决，归类为测试夹具问题；该批仍不构成真实商业、会签、回滚、对账、正式 259200 秒或 HEAD/origin 证据。

2026-09-18 当前源码角色与业务旅程复验：隔离 Chromium 以独立数据库完成 admin/developer/viewer 各 60 个源路由，共 180 页；0 页面/API/浏览器错误、0 横向溢出、0 控件裁切、0 交互重叠。当前源码三角色业务旅程 FLOW-01/02/03 也通过，覆盖登录、角色操作、键盘导航、表单校验、恢复、移动抽屉和登出；匿名首页、登录、注册策略、验证码关闭策略和密码恢复入口在 1440、1280、390、320 视口通过。证据分别为 `outputs/prd148-role-audit-current-20260918-r6/evidence-manifest.json`、`outputs/prd148-human-journey-current-20260918-r3/evidence-manifest.json`、`outputs/prd148-public-audit-current-20260918-r2/evidence-manifest.json`，均绑定当前源码 SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`。这是隔离脚本证据；admin/viewer 的 hydration 登出专门场景未执行，不能替代真实商业、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒观察或 HEAD/origin 一致性。

2026-09-18 请求解释/反馈体验问题修复：英文请求解释的状态、阶段、区块、事实字段和查询控件已统一使用活动语言字典；反馈报表日期占位符改为服务端权威查询窗口，窗口未加载时使用中性 ISO 格式提示。受影响单测 20/20、`npm run lint`（838 files）和 `npm run build`（294 assets）通过，源码起止快照一致为 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`，证据见 `outputs/prd148-request-ui-fix-current-20260918/evidence-manifest.json`。该批只关闭两个已确认 UI 发现，不构成真实付费、企业治理、六方会签、发布回滚、对账或正式 259200 秒稳定性证据；Chromium 登出超时与 PRD-148 其余门禁继续保留。

2026-09-18 资源摘要同步后的当前快照：PAGE-116 Workspace Cookie 本地模型页修复及本轮 Provider 证据范围、订阅空态改进已同步前端生产资源摘要 `df518fe132342130280c753f957a42ed8e90a43a21284b8d34936b08170de022`；`npm run lint` 838 files、`npm run build` 294 assets 通过。当前源码 SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`，域页面自动化审计 69/69（59 桌面、10 移动）见 `outputs/prd148-domain-ui-audit-current-20260918-r5/report.json`。本轮角色走查脚本未形成终态报告，不能算角色复验通过；这不构成原生 computer-use、真实商业链或 GA 证据。Chromium 登出超时、真实付费/治理/私有 Gateway、会签/回滚/对账、正式 72 小时和 HEAD/origin 仍未闭合。

2026-09-18 人类式业务走查批次：集中复核管理员、开发者、Viewer 的页面契约、Provider 状态、订阅/争议、员工 KEY、Webhook、请求解释和 FinOps 空态后，修复了“健康状态但无真实调用样本/探测成功率低”缺乏证据范围提示，以及无订阅时缺少开通路径说明的问题。受影响单测 24/24，lint 838 files，build 294 assets；证据记录见 [PRD-148 人类式走查](docs/testing/PRD-148-human-audit-20260918.md) 和 `outputs/prd148-human-audit-current-20260918.json`。这批证据不关闭真实商业、会签、回滚、对账、正式稳定性或 HEAD/origin 门禁。

2026-09-18 当前源码角色页面复验：隔离 Chromium 使用独立数据库完成 admin/developer/viewer 三角色共 180 个页面和声明路由检查，0 页面/API/浏览器错误、0 横向溢出或交互重叠；迁移 153、最新 0155、完整性检查通过。源码起止快照一致为 `dc503279acc4febf040a46b28b4a771a8d5a509ab53469541e4e12749be2d4d1`，证据见 [当前角色审计](outputs/prd148-role-audit-current-20260918-r3/evidence-manifest.json)。这只是当前源码绑定的隔离页面回归，不构成外部身份、真实付费/企业治理、六方会签、正式稳定性或 GA 证据；五域矩阵源码漂移、Chromium 登出失败、调度/计量告警、正式 259200 秒观察及 HEAD/origin 一致性仍未闭合，PRD-148 保持未完成。

权威 Excel 已同步该批说明，`01_PRD总表!B60` 仍为“未启动”，当前工作簿 SHA256 为 `972bb53b63bf9170e74c546aacc8aa9bc71cdd581332379f3c93cd0c678747d6`；权威变更记录在 `00_变更说明!B5`，证据索引见 `outputs/prd148-current-evidence-index-final-20260916.json`。

2026-09-18 验收状态核对：当前源码 SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`。此前顺序五域矩阵 r2 绑定较早源码 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`，已由当前源码一致的 r3 批次补充；Chromium 1x 登出 15000ms 超时继续按真实失败保留。真实商业、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性及 HEAD/origin 一致性仍未闭合，PRD-148 保持未完成。

2026-09-18 反馈归因与计量修复复验：真实本机 KIMI 调用返回 200（38 tokens），路由尝试收口为 `succeeded`、usage ledger 为 `recorded`，反馈 POST 返回 201、汇总显示 1 条样本；停用临时 KEY 后再次调用返回 401。此前生产试验因连接池事务包装对象被质量统计类型校验拒绝，导致 attempt/计量悬挂；现已允许受控连接所有权 facade 并增加回滚覆盖，连接所有权单测 4/4、路由与反馈回归 14/14、Ruff 通过。证据仍是本机 scheduler-enabled 试验，不能替代正式 GA；五域矩阵有源码漂移、Chromium 登出 15000ms 失败、真实商业/会签/回滚对账/正式 72 小时与 HEAD/origin 仍未闭合。当前源码 `9849bd36f8d10ebc59278b1a12da888f60512355190c223d2c99ba7d8dcd93eb`，PRD-148 未完成。见 [反馈归因修复证据](outputs/prd148-feedback-attribution-fix-final-20260917/evidence-manifest.json)。

同一当前源码批次复查 PRD-140 客户成功 outbox 接管，完整迁移链 0155 上单项 1/1 通过，原先记录的日期接管失败已关闭；证据见 [outbox 当前复验](outputs/prd148-prd140-outbox-current-20260918/evidence-manifest.json)。这不改变 PRD-148 的全局未完成状态。

2026-09-17 反馈认证性能批次：将 RBAC/Workspace 的全元数据扫描改为事务内按表名查询，保留全部三次认证与 250ms 门槛。基础回归 20/20、反馈/Workspace 集成 26/26、协议与默认模型 26/26、SQLite/原生 PostgreSQL 目录边界通过；普通 scheduler-enabled 本机服务后续反馈 24/24 返回 200（约 99–186ms），Owner/Developer 桌面与手机操作复核通过。这是短样本，未关闭历史 503 和全部调度争用；计量对账告警仍在。当前源码 `7ca09119f905a01690a4438821b9bbfa6c5c4dba4fd8091e7ee0639c2399b83e`，PRD-148 未完成，正式 72 小时未启动。见 [本批证据](outputs/prd148-feedback-live-profile-20260917/summary.md)。

2026-09-17 可观测性批次：请求解释已补齐中英文本、输入旁校验和移动端图标操作；反馈日期提示取自实际响应窗口。受影响前端 97/97、全前端 2197/2197、生产摘要契约 1/1、lint 838 files、build 294 assets 通过；本机同组织 Owner/Developer 的 Playwright 操作复核通过，未覆盖 Viewer。后续 scheduler-enabled 服务诊断中，真实 Owner 的 8 次反馈读取全部 503；外层页面混合语言、全部当前 QA 与真实 GA 门禁仍未闭合，PRD-148 保持未完成，正式 72 小时未启动。当前源码 `3e95d3ff373d2ddeaf30f9dea288664caf5a7164bbf6a81a30c2cc686b85db62`。见 [本批分类记录](outputs/prd148-observability-fix-20260917/summary.md)。

2026-09-17 角色页面复核：Playwright 隔离回归 admin/developer/viewer 共 180 个页面检查通过；本批发生测试夹具源码变更，已封存为有漂移证据，不能认证最终源码。随后 GA Launch/协议/默认模型/跨协议身份计费停用 41/41 通过，源码前后一致。Webhook 布局误报与 PRD-140 日期夹具已定位，接管单项修正后 1/1 通过。调度器争用、完整浏览器矩阵、计量历史数据缺口及真实 GA 门禁仍未闭合，PRD-148 保持未完成，正式 72 小时未启动。见 [本批分类记录](outputs/prd148-role-review-20260917.md)。

2026-09-17 平台全导航与会话边界批次：平台 39 个入口集中走查无横向溢出；admin/developer/viewer
隔离浏览器回归 36 个目标页全部通过，0 API/浏览器错误。平台客户成功和隐私队列现在只接受与
PlatformSession 完全匹配的父 GlobalSession，私有 Gateway 的离线/待接管状态不再显示为正常接管，
生产资源摘要同步为 `45212ba70208e23a8835a28871107750d62f7ab7697e76b1d2665905f9f851a2`。
PRD-140 outbox 接管断言、真实商业链、六方会签、发布回滚/对账和正式 72 小时稳定性仍未闭合，
PRD-148 保持未完成。详见 [本批证据](outputs/prd148-platform-session-boundary-20260917/report.json)。

2026-09-17 平台运营批次：先巡检 40 个原生浏览器入口，再合并修复平台导航、手机菜单、首次加载和跨页状态。受影响前端 230/230、lint 838 files、build 294 assets 通过；原生确认组织目录首次显示 13 条、侧栏末尾可点击、手机选页收起。客户成功凭据冲突、组织搜索/详情等 503 仍是真实失败，全角色与业务验收未完成。当前源码摘要 `fe85860e6cc1ccba5495b90a9ceb5c3e17fe97eabb797f3354e317b3870bb054`，PRD-148 保持未完成。见 [平台批次证据与未通过项](outputs/prd148-native-platform-fix-20260917/summary.md)。

2026-09-17 成员与 KEY 批次：原生内置浏览器已验证管理员邀请、独立开发者接受并登录、改为只读角色后的菜单与旧 KEY 地址边界；合并修正了应用 KEY 表单与创建/轮换归属提示。当前源码受影响前端 399/399、lint 838 files、build 294 assets 通过，原生应用 KEY 创建→轮换→复制→停用及桌面/移动布局已复核。当前源码摘要 `788ed041e750dcd19be154cbfe1c6edcdad045c5edf104898cf333dd92e241bb`。间歇性加载失败、调度器告警及其余全域验收仍未闭合，PRD-148 保持未完成。见 [本批证据](outputs/prd148-native-member-key-fix-20260917/summary.md)。

2026-09-17 浏览器控制复核：原生 Codex 内置浏览器已实际通过页面读取、点击“API 文档”和截图；Computer Use 组件可加载并枚举应用。统一 CUA 入口仍报 `unsupported Codex auth method: apikey`，该入口尚未修复，不能据此宣称内置浏览器整体不可用。清理清单没有删除浏览器组件。迁移页配置加载失败仍待定位，PRD-148 保持未完成。见 [浏览器控制诊断](outputs/browser-control-diagnostic-20260917/summary.md)；下方旧记录保留其历史时间范围。

2026-09-16 业务域复查：Computer Use 运行时已恢复初始化和应用枚举，但 Edge 既有页面的 URL 无法被策略可靠识别，原生页面操作因此未执行；Playwright 证据仍单独标注。DLP 已完成当前 Workspace Cookie/CSRF/事务撤权和本机 UI 创建停用回归；Developer/反馈接口在 Control 重载后恢复 200，Webhook 仍待修复。GA 14/14、Gateway 27/27、Platform P95 单项 1/1 通过，反馈专项最终 16/16 通过。真实商业、六方会签、回滚对账、正式 72 小时稳定性与 HEAD/origin 仍未闭合，PRD-148 保持未完成。当前源码摘要见 `outputs/prd148-current-source-followup2-20260916.json`。

2026-09-16 最新：PRD-148 保持未完成。两端生产资源摘要、293 assets 构建、GA Launch 14/14 已复核；完整浏览器矩阵出现登出导航超时，五域/浏览器批次仍在执行且有源码摘要变更，不能作为最终一致快照。formal11 已失败，未启动合格的新 72 小时观察。真实商业、六方会签、发布回滚对账与 HEAD/origin 仍缺证据。见 [最新验收记录](docs/testing/PRD-148-current.md) 和 `outputs/prd148-current-evidence-index-final-20260916.json`。
2026-09-18 当前域审计：PAGE-116 Workspace Cookie 会话与本地模型空态已修复；当前源码 Playwright 页面审计 69/69（59 桌面、10 移动）通过，证据为 `outputs/prd148-domain-ui-audit-current-20260918-r4/report.json`。该自动化审计不等同于原生 computer-use 或 GA。五域批次仍因源码起止漂移不能作当前一致快照，真实商业链、六方会签、发布后复核/回滚、财务隐私安全迁移对账、正式 72 小时稳定性与 HEAD/origin 仍未闭合，PRD-148 保持未完成。
PRD-095 并发、游标和 warm session P95 单项本轮隔离重跑 1/1 通过（`outputs/prd148-platform-p95-current-20260918.xml`）；此前超过 50ms 的真实失败仍保留，门槛未放宽，单项通过不构成五域或 GA 证明。
2026-09-18 顺序当前源码五域复跑：Platform 38/38、Workspace 45/45、Device 14/14，Partner/Support 16/16 通过并保留 1 个明确退役入口 skipped。Partner 测试替身已补齐当前平台读事务的 `csrf_token`/`read_only` 参数；证据目录为 `outputs/prd148-negative-current-20260918-r2`，绑定源码 SHA256 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`。该批次解决了已确认的测试夹具问题，但不关闭真实商业、会签、发布回滚、对账、正式 72 小时或 HEAD/origin 门禁，PRD-148 仍未完成。
2026-09-16 当前源码复跑：Platform 38/38、Workspace 62/62、Partner/Support 16 通过加 1 个退役入口 skipped、Device 14/14；反馈专项 16/16。最新源码摘要为 `6348c7766544b91625140779cc147a37cc0ca9b340b3b7e1d42fa667d37a71be`。数据库 deadline 修复已通过受影响回归。真实付费/企业治理/私有 Gateway、六方会签、发布回滚对账、scheduler-enabled 72 小时、Excel 与 HEAD/origin 一致性仍未具备，PRD-148 继续未完成。
正式观察 `outputs/continuous-stability-20260916-formal-prd148-r3/status.json` 已因 Gateway 4 次 read timeout 在 309.67 秒失败，长稳 credit=0；不能用作 72 小时证据。

- PRD-001 至 PRD-089 的已完成事实保持不变。旧总清单中的 PRD-090 至 PRD-108 已废弃并禁止开发，只保留历史审计。
- 新路线图从 PRD-090 重新开始，不限定需求总数或最终编号。当前已细化 PRD-090 至 PRD-148；后续需求从当前末尾连续追加。
- PRD-090 起的唯一权威计划是 [SaaS 增量开发计划 Excel](企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx) 和
  [SaaS 路线图说明](docs/planning/MetaHub-SaaS-PRD-090-onward.md)，冲突时以 Excel `01_PRD总表` 为准。
- 新 `PRD-090` 至 `PRD-147 - 存量控制台全面分域迁移与旧入口归零` 已有交付记录，G7 分域专项通过；
  G6 正在补正式接线、原生业务和真实运行证据，详见 [G6 补充状态](docs/testing/G6-remediation-status.md)。
  `PRD-148 - SaaS公开发布、迁移终验与全产品最终门禁` 正在收尾和验收，尚未标记完成。
  2026-09-15 复核：修复前源码五域已选负向集合为 112 passed 加 1 个退役入口 skipped，Gateway 27/27；完整隔离 QA 为 15/15 任务、浏览器 27/27、loopback 30/30、压力 600/600，但 97 项审计仅 95/97，PERF-08/10 缺合格长稳。新增修复了 0155 down 被正式迁移入口拒绝的问题，GA 集成回归 14/14；副本往返保留 1094 表、543694 行。新源码 `9904e348…cebfe` 的完整自动化 QA 已通过 15/15 任务，但审计仍为 95/97；五域/Gateway 继续重跑。只读审计发现本机入口仍为 expanded/generation 0，缺分域迁移、审批和对账事实；64 条队列契约不证明迁移完成。正式 72 小时、真实付费计量、企业治理/私有 Gateway、真实会签/发布回滚/对账及 HEAD/origin 一致性仍未闭合。PRD-148 保持未完成，详见 [PRD-148 当前验收记录](docs/testing/PRD-148-current.md) 和 `outputs/prd148-current-evidence-index-20260915.json`。
  本地试用库已升级至 0155，账号和组织等既有数据已逐行核对保留；
  [本地试用交付记录](docs/testing/PRD-148-local-trial-release-20260912.md) 区分已交付功能与待完成的正式验收。
  本机人类路径走查记录见 [PRD-148 人类路径走查](docs/testing/PRD-148-human-path-20260915.md)，只覆盖本地试用身份和脱敏测试凭据，不构成正式 GA 证据。
  2026-09-15 当前源码矩阵复跑：Platform 38/38、Workspace 62/62、Partner/Support 16 通过加 1 个退役入口 skipped、Device 14/14、GA/Gateway 40/40；当前 All QA 5/5、PERF smoke/pressure 3/3、Chromium admin/developer/viewer 真实角色旅程通过。最新索引见 `outputs/prd148-current-evidence-index-final-20260915.json`。外部付费、企业治理/私有 Gateway、六方会签、真实回滚、对账和 scheduler-enabled 72 小时观察仍缺证据，PRD-148 继续未完成。
- 新计划状态为 `saas_incremental_roadmap_complete=false`；兼容旧口径继续保持 `all_108_prds_complete=false`。

## 当前状态

当前已完成：

- `PRD-001 - 仓库工程骨架与模块边界`
- `PRD-002 - 运行配置与环境隔离基础`
- `PRD-003 - 数据库迁移与种子数据`
- `PRD-004 - 统一日志、TraceID 与错误码`
- `PRD-005 - 服务健康检查与版本信息`
- `PRD-006 - 管理员登录与 Session`
- `PRD-007 - 单租户与基础多租户模型`
- `PRD-008 - 基础 RBAC 角色`
- `PRD-009 - OpenAI-Compatible 接口基础`
- `PRD-010 - Virtual Key 创建、鉴权与禁用`
- `PRD-011 - 模型 Endpoint Provider 注册`
- `PRD-012 - 模型目录与内部标准模型ID`
- `PRD-013 - Provider Adapter 抽象接口`
- `PRD-014 - Provider KeyPool基础管理`
- `PRD-015 - 模型别名与最小路由`
- `PRD-016 - Request Log最小闭环`
- `PRD-017 - Token Usage与成本估算`
- `PRD-018 - 控制台骨架与导航`
- `PRD-019 - 5分钟快速接入页`
- `PRD-020 - 内置API调试控制台`
- `PRD-021 - 智能错误提示基础版`
- `PRD-022 - Virtual Key级RPM/TPM限流`
- `PRD-023 - Key/租户日预算与告警`
- `PRD-024 - 关键词/正则敏感词拦截`
- `PRD-025 - 基础事件订阅与推送`
- `PRD-026 - MVP总览Dashboard`
- `PRD-027 - i18n基础框架`
- `PRD-028 - 单机Docker Compose部署`
- `PRD-029 - Project与Application模型`
- `PRD-030 - dev/test/prod环境隔离`
- `PRD-031 - 价格表与生效时间`
- `PRD-032 - Provider 计费适配规范 MVP`
- `PRD-033 - 预算耗尽阻断与恢复`
- `PRD-034 - 管理操作审计日志`
- `PRD-035 - Provider健康探测与状态展示`
- `PRD-036 - 非流式请求备用模型Fallback`
- `PRD-037 - SSE流式代理与超时看门狗`
- `PRD-038 - Python SDK与示例项目`
- `PRD-039 - JavaScript/Node示例项目`
- `PRD-040 - 从直连模型厂商迁移指南`
- `PRD-041 - 竞品定位与能力映射`
- `PRD-042 - Webhook事件过滤与重放基础`
- `PRD-043 - 用量CSV导出`
- `PRD-044 - 策略引擎DSL最小版`
- `PRD-045 - 策略快照版本化与发布`
- `PRD-046 - 策略模拟与单请求预演`
- `PRD-047 - 成本优先/可用性优先路由`
- `PRD-048 - 效果-成本路由基础`
- `PRD-049 - 调用反馈与采纳率记录`
- `PRD-050 - Prompt Cache 感知粘性路由`
- `PRD-051 - FinOps Ledger 正式账本`
- `PRD-052 - 租户/项目/Key三级预算`
- `PRD-053 - 成本异常检测基础`
- `PRD-054 - Request Explainability Center`
- `PRD-055 - Trace查询与过滤增强`
- `PRD-056 - Provider错误归一化规则库`
- `PRD-057 - Webhook死信与持久重放`
- `PRD-058 - 审计日志不可删除与导出`
- `PRD-059 - 细粒度角色权限矩阵`
- `PRD-060 - 用户邀请与加入流程`
- `PRD-061 - 内部Provider状态页`
- `PRD-062 - 配置导入导出CLI`
- `PRD-063 - 国内模型Endpoint适配规范`
- `PRD-064 - Provider Adapter契约测试框架`
- `PRD-065 - 多角色首页与导航`
- `PRD-066 - 企业Chat演示入口`
- `PRD-067 - 企业Prompt模板库`
- `PRD-068 - 行业/场景策略模板库`
- `PRD-069 - 轻量敏感意图分类`
- `PRD-070 - 多轮会话风险引擎`
- `PRD-071 - 输出侧安全检测与流式滑窗`
- `PRD-072 - 文件上传与文本解析基础`
- `PRD-073 - 隐藏 Prompt Injection 检测`
- `PRD-074 - 批量任务 Scheduler 与 Worker`
- `PRD-075 - 模型 A/B 测试与质量评估`
- `PRD-076 - Response Cache 可选能力`
- `PRD-077 - 只读管理员 AI 助手`
- `PRD-078 - 移动端/企微钉钉审批入口`
- `PRD-079 - 套餐、试用与额度类型`
- `PRD-080 - 账单争议与调整流程`
- `PRD-081 - SIEM/安全事件格式`
- `PRD-082 - MCP 工具注册与权限`
- `PRD-083 - 第三方 MCP 沙箱网络 ACL`
- `PRD-084 - 本地模型 Endpoint 接入`
- `PRD-085 - 产品使用埋点与激活漏斗`
- `PRD-086 - 客户健康评分`
- `PRD-087 - 渠道伙伴与白标门户`
- `PRD-088 - 客户/渠道白标配置`
- `PRD-089 - 租户数据导出与注销流程`

最近进展：

- S7/G7 补充验证完成：PostgreSQL 17.11 原生业务合同 `20/20`、新版 baseline/native delta
  `22/22`、Windows 迁移业务 `14/14`、Linux Console `1286/1286` 通过。新增真实 PostgreSQL
  运行测试修复了 seed/权限/对账的 SQLite 专用查询、JSON 规范化和四张间接 owner 表的漏计。
  Linux 修正复验 `47/47` 与最终 SQLite 增量 `12/12`、Windows IAM `3/3`、敏感审计均通过。
  G7 专项目标通过不代表 G6 的 72 小时缺口已补齐，不声明 GA。详见
  [S7/G7 报告](docs/testing/S7-G7-report.md) 和
  [基础设施补充验证](docs/testing/G7-infrastructure-remediation.md)。
- `PRD-140 - SaaS平台客户管理、健康与续费协同` 已完成：0132、客户运营安全投影、客户目录、可解释
  健康、成功计划、跟进任务与批量审批、续费管道、QBR 摘要、五个固定 Platform API 和独立 Customer
  Success 工作台均已交付；旧企业 `customer-health` 导航、DOM、预取和生产路由已卸载。Migration `8/8`、
  受影响 Backend/SDK `64/64`、Console `41/41`、lint `429 files`、Ruff/compile 和三档 Browser 验收通过。
  详见 [PRD-140 设计](docs/prd/PRD-140-saas-platform-customer-success-renewal.md) 和
  [PRD-140 测试记录](docs/testing/PRD-140-tests.md)。下一权威项为 PRD-141，完整 G7 留到 PRD-147 后统一执行。
- `PRD-139 - 渠道伙伴、代理商与客户归属管理` 已完成：0131、渠道伙伴/成员/ReferralCode/商机、独立
  Workspace 归属审批、CommissionRule snapshot、ChannelStatement、legacy PRD-087 回填与 fence、六个固定
  Platform/Partner 路由、Platform Channels 工作台和独立 Partner 壳层均已交付。Migration/API `7/7`、
  Console `5/5`、lint `423 files`、Ruff/compile 和 Desktop/Mobile Browser 通过；Workspace 不再自动加载
  `/partners/*`。详见 [PRD-139 设计](docs/prd/PRD-139-channel-partner-attribution-management.md) 和
  [PRD-139 测试记录](docs/testing/PRD-139-tests.md)。下一权威项为 PRD-140，完整 G7 留到 PRD-147 后统一执行。
- `PRD-138 - SaaS发布候选、核心迁移波次与全产品门禁` 已完成：0130、20 个固定签名 gate、六账号
  Go/No-Go 会签、动态撤权、内部/个人/企业/云管私有四波观察、唯一前波回滚与独立复验、持久 scheduler/
  event/outbox，以及 Platform Launch Gates 工作台均已交付。Migration `4/4`、后端 API/服务 `7/7`、Console
  `5/5`、关联回归 `24/24`、lint `414 files`、Ruff 和真实 Desktop/Mobile Browser 通过。详见
  [PRD-138 设计](docs/prd/PRD-138-saas-release-candidate-launch-gates.md) 和
  [PRD-138 测试记录](docs/testing/PRD-138-tests.md)。后续 G6 报告仅支持当时可用环境的
  release candidate：Windows/SQLite 和四小时长稳已验证，未完成真实 72 小时验收，不能称为完整 G6。
- `PRD-137 - 租户隔离持续验证与安全攻防` 已完成：0129、签名版本化 profile、10 个固定 owner-registry
  场景、隔离 runner lease/fencing、缺失或撤权失败关闭、不可变 finding、独立 remediation 复验、Workspace
  owner/security_admin 双批与 Platform review 风险接受，以及 Platform Security Assurance 工作台均已交付。
  Migration `4/4`、后端 API/服务 `7/7`、Console `5/5`、lint `411 files` 和 Ruff 通过。详见
  [PRD-137 设计](docs/prd/PRD-137-tenant-isolation-continuous-security-validation.md) 和
  [PRD-137 测试记录](docs/testing/PRD-137-tests.md)。下一权威项为 PRD-138；完整 G6 留到其独立提交后。
- `PRD-136 - SBOM、制品签名、漏洞与发布证明` 已完成：0128、CycloneDX/SPDX SBOM、SLSA 式
  provenance、用途隔离 Ed25519 attestation、固定 feed/scanner 漏洞代际、默认高危/关键发布阻断、
  Workspace 时限例外双人审批、PatchRelease/回滚/N/N-1 兼容门禁，以及 Developer/Platform 安全报告
  与客户端验签均已交付。后端迁移/API `5/5`、Console 与 PRD-133 回归 `11/11`、lint `409 files`
  和 Ruff 通过。详见 [PRD-136 设计](docs/prd/PRD-136-sbom-artifact-signing-vulnerability-provenance.md)
  和 [PRD-136 测试记录](docs/testing/PRD-136-tests.md)。下一权威项为 PRD-137；完整 G6 留到 PRD-138 后。
- `PRD-135 - 合规控制与证据中心` 已完成：0127、版本化控制/法规映射、受信 machine evidence、
  签名 envelope/replay guard、例外非自批、整改闭环、Workspace 报告、持久 scheduler fencing、Platform
  控制视图和 Identity 报告视图均已交付。后端迁移/API `6/6`、Console 与必要回归 `44/44`、既有
  Machine Identity 功能回归 `5/5`、lint `407 files` 和 Ruff 通过；历史 PRD-118 本机 P95 性能残差保留到
  G6，不降低阈值。详见 [PRD-135 设计](docs/prd/PRD-135-compliance-evidence-center.md) 和
  [PRD-135 测试记录](docs/testing/PRD-135-tests.md)。下一权威项为 PRD-136；完整 G6 留到 PRD-138 后。
- `PRD-134 - 全局账号隐私、同意与数据主体请求` 已完成：0126、Global+Workspace 双会话本人鉴权、
  ConsentRecord/偏好 CAS、七 owner DSR、Legal Hold/retention 决定、AES-GCM IdentityExport、确定性 HMAC
  一次性回执、持久 scheduler fencing、Identity 隐私中心和 Platform 最小队列均已交付。API `3/3`、迁移
  `3/3`、Console `7/7`、关联回归 `69/69`、lint `404 files` 和 Ruff 通过。详见
  [PRD-134 设计](docs/prd/PRD-134-global-account-privacy-consent-data-subject-request.md) 和
  [PRD-134 测试记录](docs/testing/PRD-134-tests.md)。下一权威项为 PRD-135；完整 G6 留到 PRD-138 后。
- `PRD-133 - 公共Control API与SDK版本弃用治理` 已完成：0125、显式 public registry、canonical
  compatibility diff、Python/JavaScript 可复现 SDK、Ed25519/KMS 签名与浏览器下载验签、双人发布、
  90 天废弃与持久 sunset gate、Developer API Docs 和 Platform Releases 工作台均已交付。Shared `5/5`、
  API `1/1`、迁移 `3/3`、Console 专项 `6/6`、关联回归 `47/47`、lint `401 files` 和 Ruff 通过。详见
  [PRD-133 设计](docs/prd/PRD-133-public-control-api-sdk-version-deprecation.md)、
  [API 合同](docs/api/developer-contracts.md) 和 [PRD-133 测试记录](docs/testing/PRD-133-tests.md)。下一权威项为
  PRD-134；完整 G6 留到 PRD-138 后。
- `PRD-132 - 客户工单、SLA与安全诊断协作` 已完成：0124、加密客户消息与内部备注、附件隔离扫描、
  持久 SLA clock/升级、满意度、Workspace/Platform 双域 API 和 Console 均已交付。后端聚焦 `6/6`、
  Console `5/5`、相邻导航 `3/3`、Ruff 与 lint `398 files` 通过。详见
  [PRD-132 设计](docs/prd/PRD-132-customer-ticket-sla-secure-diagnostics.md) 和
  [PRD-132 测试记录](docs/testing/PRD-132-tests.md)。下一权威项为 PRD-133；完整 G6 留到 PRD-138 后。
- `PRD-131 - 旧租户、直连与私有部署迁移切换向导` 已完成：0123、固定 owner-qualified 来源适配器、
  只读 assessment/差异摘要、版本化 plan/run、Workspace/Support JIT 权限、持久 checkpoint、可恢复
  cutover/rollback 和 Migration Center 均已交付。后端聚焦 `6/6`、Console `5/5`、导航/权限回归
  `21/21`、Ruff 与 lint `393 files` 通过。详见
  [PRD-131 设计](docs/prd/PRD-131-legacy-tenant-direct-private-migration-cutover.md) 和
  [PRD-131 测试记录](docs/testing/PRD-131-tests.md)。下一权威项为 PRD-132；完整 G6 留到 PRD-138 后。
- `PRD-130 - 隔离环境签名包异步管理与冲突合并` 已完成：0122、绑定 owner/direction/version vector 的
  Ed25519 manifest、逐 entry AEAD/hash/Merkle、严格介质验证与原子 apply、Workspace step-up 请求、Platform
  非自批批准/拒绝、显式 cloud/local conflict resolution、`metahub bundle verify/apply` 和双域 Console 均已
  交付。Python 聚焦 `17/17`、Console `7/7`、lint `388 files` 通过；双域 `1280x900`/`390x844` 浏览器验收
  无页面溢出、控件交叠、禁止字段命中或浏览器错误。详见
  [PRD-130 设计](docs/prd/PRD-130-isolated-environment-signed-bundle-conflict-reconciliation.md) 和
  [PRD-130 测试记录](docs/testing/PRD-130-tests.md)。下一权威项为 PRD-131；PRD-130 独立提交后统一执行 G5。
- `PRD-129 - 私有Gateway备份、恢复与灾备演练` 已完成：0121、用途分域签名 manifest、逐块 AEAD/Merkle、
  客户恢复密钥包裹、本地原子恢复 journal、替换节点 KEK 重包裹、持久云报告 outbox、Control/Agent API、
  非自批隔离演练和 Workspace/Platform 双域 Console 均已交付。Shared/Gateway `22/22`、Control/迁移/导航
  `5/5`、Runtime config `43/43`、Console `4/4` 通过；双域双视口浏览器验收无页面溢出、控件交叠、
  禁止字段或日志错误。详见 [PRD-129 设计](docs/prd/PRD-129-private-gateway-backup-restore-dr-drill.md) 和
  [PRD-129 测试记录](docs/testing/PRD-129-tests.md)。下一权威项为 PRD-130；G5 在 PRD-130 后统一执行。
- `PRD-128 - 私有Gateway License、容量与离线宽限` 已完成：0120、用途分域 Ed25519 SignedEntitlement、
  单调 generation/LKG/可信时钟、原子容量 lease、7 天离线宽限、明确吊销、Control/Agent API、在线同步、
  `metahub license import` 和 Workspace/Platform 双域 Console 均已交付。Migration `2/2`、Control API `1/1`、
  Shared/CLI/Gateway `14/14`、导航 `4/4`、Console 专项 `4/4` 及必要回归 `45/45` 通过。详见
  [PRD-128 设计](docs/prd/PRD-128-private-gateway-license-capacity-offline-grace.md) 和
  [PRD-128 测试记录](docs/testing/PRD-128-tests.md)。下一权威项为 PRD-129；G5 在 PRD-130 后统一执行。
- `PRD-127 - Fleet分批升级、回滚与维护窗口` 已完成：0119、双人审批签名 release、确定性 cohort、
  客户维护窗口/冻结、兼容/容量/备份预检、失败自动暂停、可恢复 Agent lease/start/report、LKG 与单节点 rollback、
  Workspace/Platform 双域 Console 均已交付。Control `5/5`、Shared `7/7`、Gateway `2/2`、Console `4/4` 及
  PRD-120..127 Console 必要回归 `41/41` 通过；两页 390x844 浏览器验收无页面溢出或日志错误。详见
  [PRD-127 设计](docs/prd/PRD-127-fleet-staged-upgrade-rollback-maintenance-window.md) 和
  [PRD-127 测试记录](docs/testing/PRD-127-tests.md)。下一权威项为 PRD-128；G5 在 PRD-130 后统一执行。
- `PRD-126 - Fleet资产、健康与受控远程诊断` 已完成：0118、不可变健康快照/head、字段裁剪 Fleet 索引、
  1 万节点稳定 keyset、Workspace/Platform 双域权限、客户短时授权、Ed25519 签名命令、无 shell Agent handler、
  lease/取消/超时/重启恢复和双域 Console 均已交付。Python 专项 `15/15`、相邻回归 `151/151`、Console
  `37/37` 通过；真实三浏览器、长稳、性能和安全全门禁仍在 PRD-130 后统一执行。详见
  [PRD-126 设计](docs/prd/PRD-126-fleet-assets-health-controlled-diagnostics.md) 和
  [PRD-126 测试记录](docs/testing/PRD-126-tests.md)。下一权威项为 PRD-127。
- `PRD-125 - 私有部署验证与脱敏支持包` 已完成：0117、Gateway 编译期八项验证 runner、失败项重试与
  DeliveryAcceptance step-up、本地固定 collector/递归敏感扫描/5 分钟清理、AES-GCM 上传、Workspace 裁剪摘要、
  双域 SupportGrant 下载审计和 `ready -> expired -> deleted` TTL reaper 均已交付。Python `33/33`、Console
  `5/5` 及 PRD-120..124 必要回归 `24/24` 通过；完整批次浏览器/性能/安全门禁仍在 PRD-130 后统一执行。
  详见 [PRD-125 设计](docs/prd/PRD-125-private-deployment-validation-redacted-support-bundle.md) 和
  [PRD-125 测试记录](docs/testing/PRD-125-tests.md)。下一权威项为 PRD-126。
- `PRD-124 - 云管私有Gateway安装包、预检与接入向导` 已完成：0116、不可变签名制品、全 artifact/SBOM/hash
  校验、纯读预检、持久 step journal/hash-chain audit、崩溃恢复/补偿、默认保留卸载、双人发布、Workspace
  installer API/Console 和本地 CLI 均已交付。专项 `11/11 + 5/5 + 3/3 + 3/3` 通过；真实 API 校验五个
  artifact 的长度/SHA-256/no-store，Chromium 390/1280/1440 完成失败预检到 active 的恢复闭环且无页面级
  溢出或服务错误。详见 [PRD-124 设计](docs/prd/PRD-124-private-gateway-installer-preflight-onboarding.md)
  和 [PRD-124 测试记录](docs/testing/PRD-124-tests.md)。下一权威项为 PRD-125；G5 在 PRD-130 独立提交后统一执行。
- `PRD-123 - 私有网关本地Secret托管与轮换` 已完成：0115、本地 schema v2、DPAPI/客户 KMS 信封加密、
  prepare-test-activate-drain-retire 原子轮换、固定 Provider 测试、Agent 引用同步、Control 零明文投影和
  Workspace Console 均已交付。详见 [PRD-123 设计](docs/prd/PRD-123-private-gateway-local-secret-custody-rotation.md)
  和 [PRD-123 测试记录](docs/testing/PRD-123-tests.md)。
- `PRD-122 - 私有网关遥测、用量与失败事实同步` 已完成：0114、严格四流遥测 DTO、三档 policy、
  append-only 本地 spool、gzip 断点续传、连续 ACK/缺口恢复、Usage 幂等对账、固定失败事实、Workspace
  水位/策略和 Platform Fleet 安全投影均已交付。Shared/本地 spool、Control API、迁移、Console、静态检查、
  真实开发数据和 1280px/551px Browser 均形成独立证据；平台投影不含 tenant/Workspace、model/provider、
  token/cost 或 payload，浏览器无页面级横向溢出。详见
  [PRD-122 设计](docs/prd/PRD-122-private-gateway-telemetry-usage-failure-sync.md) 和
  [PRD-122 测试记录](docs/testing/PRD-122-tests.md)。下一权威项为 PRD-123；G5 在 PRD-130 独立提交后统一执行。
- `PRD-121 - 云边期望状态、签名快照与离线LKG` 已完成：0113、不可变 Desired State、非自批高危审批、
  RFC 8785 canonical envelope、Ed25519 签名快照、独立 trust manifest、Agent 原子双槽/持久高水位/LKG、
  单调 ACK、递增 generation 回滚、Workspace 期望/实际/LKG 和 Platform Fleet 安全投影均已交付。
  Migration `4/4`、Shared + Gateway `15/15`、Control API `1/1`、PRD-120 后端回归 `3/3`、Console
  PRD-120/121 `12/12` 通过，Console lint `371 files` 通过；真实流程证明 Owner 不能自批、第二管理员审批、
  Agent mTLS Session 拉取并 ACK 后 `期望 1 / 实际 1 / LKG 1` 且 `converged/applied`，1280px 与 536px
  Workspace/Platform 均无页面级横向溢出或浏览器错误。详见
  [PRD-121 设计](docs/prd/PRD-121-edge-desired-state-signed-snapshot-offline-lkg.md) 和
  [PRD-121 测试记录](docs/testing/PRD-121-tests.md)。下一权威项为 PRD-122；G5 在 PRD-130 独立提交后统一执行。
- `PRD-120 - Edge Agent注册、设备身份与mTLS` 已完成：0112、一次性注册、Ed25519 CSR/设备证书、mTLS
  transport 证明、证书轮换、45 秒 EdgeSession、吊销、Workspace 私有 Gateway 和 Platform Fleet 均已
  交付。Migration/API/共享合同/导航/Console 专项 `19/19` 通过，Console lint `370 files` 通过；真实
  TLS 1.3 双向证书握手 40 次 p95 `31.67ms`，实际 Browser 验证四条 Deployment、一次性值清理、平台
  DTO 脱敏、离页清理和 536px 无页面级横向溢出。详见
  [PRD-120 设计](docs/prd/PRD-120-edge-agent-device-identity-mtls.md) 和
  [PRD-120 测试记录](docs/testing/PRD-120-tests.md)。下一权威项为 PRD-121；G5 在 PRD-130 独立提交后统一执行。
- `PRD-116 - MFA、条件访问、设备与会话安全` 已完成：0108、多因子 TOTP/WebAuthn、恢复码、受信设备、
  Global/Workspace Session 撤销、条件访问与一次性 step-up 均已交付。最终 Migration `4/4`、Identity/API
  `2/2`、Shared SDK `1/1`、Console 专项 `11/11`、Console 全量 `1093/1093` 和 lint `358 files` 均通过。
  真实受控恢复验证 24 小时冷静期拒绝、48 小时窗口内双证明、旧身份安全状态原子撤销、新 `recovery`
  Session 轮换和 Workspace 跨刷新保持清空；三视口均无溢出、越界、重叠、broken image 或浏览器错误。
  详见 [PRD-116 设计](docs/prd/PRD-116-mfa-conditional-access-device-session-security.md) 和
  [PRD-116 测试记录](docs/testing/PRD-116-tests.md)。其历史下一项已由 PRD-117..119 完成交付；
  S4 G4 仍在 PRD-119 独立提交后统一执行。
- `PRD-115 - SCIM目录同步与成员生命周期` 已完成：0107、Workspace 所有权绑定的 SCIM Bearer、
  Users/Groups、受限 filter/PATCH、token 轮换、role mapping、Membership 投影、Owner 保护、冲突 CAS、
  事件/outbox 与 Identity Console 均已交付。最终 Migration `5/5`、API `1/1`、Shared SDK `3/3`、
  Gateway CORS 回归 `1/1`、Enterprise Identity Console `10/10`、Console 全量 `1082/1082`，lint、Ruff、
  差异检查和敏感扫描均通过。
  本地真实认证链路完成登录、Workspace 切换、token 轮换、weak ETag Group PATCH 和冲突生成；真实 Browser
  完成 `keep_local` 恢复并确认 Membership 不变。`1440x900`、`1280x720`、`390x844` 三视口均为零横向
  溢出、零越界、零重叠、零裁切和零 Console error。详见
  [PRD-115 设计](docs/prd/PRD-115-scim-directory-membership-lifecycle.md) 和
  [PRD-115 测试记录](docs/testing/PRD-115-tests.md)。S4 G4 保留到 PRD-119 独立提交后执行，
  `saas_incremental_roadmap_complete=false`。
- `PRD-114 - 企业域名、OIDC/SAML SSO与登录发现` 已完成：0106、全局唯一 VerifiedDomain、OIDC/SAML
  connection/certificate/policy、登录发现与回调、单次 state/nonce/replay、强制 SSO 与 break-glass 恢复、
  企业身份事件/outbox 和 Identity Console 均已交付。最终 Migration `4/4`、API `2/2`、协议 `6/6`、
  Shared SDK `4/4`、Console 专项 `7/7`、Console 全量 `1079/1079`，PRD-091/094/095 邻接回归、Ruff、
  compileall、lint、差异检查和敏感扫描均通过。真实 Browser 验证 Owner 管理、auditor 只读、developer 隐藏、
  提交后敏感输入清空和 DOM 零敏感值；`1280/440/390` 三视口乘三档缩放的九组测量均为零横向溢出、零重叠、
  零裁切和零稳定期位移。详见 [PRD-114 设计](docs/prd/PRD-114-enterprise-domain-sso-login-discovery.md)
  和 [PRD-114 测试记录](docs/testing/PRD-114-tests.md)。S4 G4 保留到 PRD-119 独立提交后执行，
  `saas_incremental_roadmap_complete=false`。
- `PRD-105 - 通知中心` 已完成：0097、加密通知 source/outbox、偏好 ETag CAS、不可变模板版本、异步投递 worker、
  Workspace 通知收件箱与 Platform 受控模板编辑器均已交付。最终 Migration/API `3/3`、PRD-098/102/103/104
  邻接回归 `4/4`、Console focused `3/3`、Console 全量 `1033/1033`、lint `333 files`；本次变更范围 Ruff、
  compileall、敏感值扫描和差异检查均通过。真实浏览器在 `390x844`、`1280x900`、`1440x900` 三档验证
  Identity 与 Platform 页面零页面级横向溢出、零控件重叠、零裁切和零控制台错误。详见
  [PRD-105 设计](docs/prd/PRD-105-notification-center.md)、[通知中心 API](docs/api/notification-center.md) 和
  [PRD-105 测试记录](docs/testing/PRD-105-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-104 - 发票、税务产物与资金对账` 已完成：0096、不可变结算单与税额快照、PDF/CSV 发票产物、
  Header-only 单次下载、订单/支付/Meter/结算单/发票五源冻结对账、账务与合规双域审批冲红、provider worker、
  Workspace 发票区和 Platform 合规复核视图均已交付。最终 PRD-104 Migration/API `4/4`、Console focused
  `27/27`、Console 全量 `1030/1030`、lint `331 files`，Ruff 与 compileall 均通过；真实 Edge 在
  `390x844`、`1280x900`、`1440x900` 三档验证 Identity 发票页与 Platform 合规页零页面级横向溢出、零控件
  重叠和零裁切。详见 [PRD-104 设计](docs/prd/PRD-104-invoices-tax-reconciliation.md)、
  [发票与对账 API](docs/api/invoice-reconciliation.md) 和
  [PRD-104 测试记录](docs/testing/PRD-104-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-088 - 客户/渠道白标配置` 已完成：0081、RBAC revision 23、tenant-qualified BrandingConfig、
  安全 PNG Logo、域名预留、不可变 `branding.updated` 审计、EVT-100/source outbox、实时后端权限校验、
  Branding 受保护/公共 API、PAGE-120、登录页与认证后 Console shell 均已交付。公共投影对非法和不存在 tenant
  slug 返回同形默认品牌，不能枚举租户；Logo、登录文案和域名原文不进入日志、审计、事件或验收证据。
  PRD-088 Branding 集成 `4/4`、Migration `9/9`、Console Branding `9/9`、Console 全量 `935/935`，Ruff 与
  lint 均通过。真实 HTTP 验证 admin/viewer、动态撤权、统一错误/request_id 和 no-store；三视口 Browser 验证
  管理员保存、viewer 只读、撤权清理、恢复权限及零横向溢出。真实库为 `81/0081`、`integrity_check=ok`、
  外键违规 0，EVT-100 worker 最终无 pending，敏感哨兵命中 0。详见
  [PRD-088 设计](docs/prd/PRD-088-tenant-white-label-branding.md)、
  [Branding API](docs/api/branding.md) 和
  [PRD-088 测试记录](docs/testing/PRD-088-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-087 - 渠道伙伴与白标门户` 已完成：0080、RBAC revision 22、tenant-qualified Partner、
  PartnerCustomer、CommissionRecord、两阶段分润游标、EVT-099/source outbox、伙伴安全操作链、六组后端权限、
  `GET/POST/PATCH /partners*`、CSV/交付包和 PAGE-119 均已交付。客户用量明确包含 Project 映射前历史，分润只从
  映射生效后的 FinOps ledger sequence 物化；不同币种不换算，estimated 与 accrued/not_applicable 状态保留。
  后端逐请求校验 tenant/RBAC/owner，跨租户与不存在同形 404，API 全路径统一 `no-store, private`、错误码与
  request_id；Provider Key、Token、Prompt、Response、Endpoint、客户展示名和品牌正文不进入日志、事件或正式证据。
  最终 PRD-087 Python 联合专项 `53/53`、Shared 相邻 `81/81`、Control 相邻 `17/17`、PAGE-119 Console
  `18/18`、Console 全量 `926/926`、lint `286 files` 均通过。真实 Chromium 批次
  `.artifacts/prd087-browser/page119-final-20260817-r17/` 从空 SQLite 完成 `80/0080` 往返并验证两个 tenant、
  动态撤权、迟到响应、未授权零预取、CSV/交付、19 个业务请求/响应、四张 390/1280/1440 PNG、零 Console/Page
  error、零敏感明文和 owned 资源清理。详见
  [PRD-087 设计](docs/prd/PRD-087-channel-partner-white-label-portal.md)、
  [伙伴门户 API](docs/api/partner-portal.md) 和
  [PRD-087 测试记录](docs/testing/PRD-087-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-086 - 客户健康评分` 已完成：0079、RBAC revision 21、tenant-qualified append-only
  CustomerHealthScore、每小时两阶段 Scheduler、changed-only `customer.health_changed` EVT-098/source outbox、
  纯读 `GET /customer-health` 和 PAGE-118 均已交付。v1 固定六类信号，其中工单源明确 unavailable，
  `effective_weight_bps=9000` 并采用整数最大余数重归一；后端逐请求校验 tenant/RBAC，API 全路径统一
  `no-store, private` 与 request_id。专项结果为 Shared `36/36`、Control `7/7`、Migration `12/12`、Console
  `14/14`；最终相邻 Python `92/92`、Console 受影响回归 `61/61`、lint `279 files`，Ruff 精确改动范围与
  compileall 均通过。真实运行覆盖空库 `79/0079` 往返、outbox sent、API 200/401/403/422/503、纯读计数不变和三租户
  隔离；Browser 在 1280/1440/390 三种视口生成 healthy、high-risk、no-samples、tenant-isolation 四张 PNG，
  均已逐图复核无重叠、无横向溢出和无 Console error。详见
  [PRD-086 设计](docs/prd/PRD-086-customer-health-score.md)、
  [客户健康 API](docs/api/customer-health.md) 和
  [PRD-086 测试记录](docs/testing/PRD-086-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-085 - 产品使用埋点与激活漏斗` 已完成：0078、RBAC revision 20、tenant-qualified
  ProductEvent、`product_event.recorded` EVT-097/source outbox、`GET /product-analytics/funnel` 和
  PAGE-117 均已交付。七阶段来源由受支持的注册、Provider、Virtual Key、首次成功调用、预算、邀请与
  RequestLog 查询事实原子投影；后端逐请求校验 tenant/RBAC，失败请求、Usage 与成本继续由既有链路追踪，
  Provider Key、Token、Prompt、Response 不进入产品事件、日志或正式证据。最终 PRD-085 Python 联合专项
  `104/104`、Shared 相邻文件 `9/9`、Control 相邻回归 `18/18`、Gateway 相邻完整文件 `11/11`、Console
  `894/894`、lint `274 files`、Browser driver `9/9`、PRD-085 定向中文注释门禁通过。权威 Runtime JSON 为
  `.artifacts/prd085-runtime/prd085-runtime-20260817T055917Z-f41d66e4.json`，SHA-256 为
  `9BC26BCD9DC04A18B9628BE5A689FE9492EE2E4ED99F6A623DFFE36397EE61D6`；权威 Browser 批次为
  `.artifacts/prd085-browser/prd085-browser-20260817T073524Z-final-r2/`，JSON/manifest SHA-256
  分别为 `2BA160C3B7351FF1B9771AC1D2BD1BBA9BCFB06B90E94CAA3463484B92A881F9`、
  `BC51513B7ED3B20AF628121C1D49ADCC7A5FEDB6D4A04156875D113591BE49A1`；九次真实漏斗 GET 覆盖
  200、动态 403、当前 401 和迟到 401，主次租户以 `2/7`、`1/7` 明确区分，四张 PNG 已逐图复核。
  详见 [PRD-085 设计](docs/prd/PRD-085-product-analytics-activation-funnel.md)、
  [产品分析 API](docs/api/product-analytics.md) 和
  [PRD-085 测试记录](docs/testing/PRD-085-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-084 - 本地模型 Endpoint 接入` 已完成：0077、RBAC revision 19、Adapter v2 `local_endpoint`、
  tenant-qualified Local Endpoint aggregate、Control 创建/列表/探测 API、Gateway 私网 ACL 与 provider-call
  fence、`local_model.called` EVT-096/source outbox/Webhook、PAGE-116 和部署/API 文档均已交付。后端逐请求
  校验 tenant/RBAC，DLP 与预算阻断保持上游调用 0；成功、失败、取消、usage、成本、attempt 和事件可追踪，
  Endpoint URL、Token、Prompt、Response 与上游异常正文不进入日志或正式证据。最终联合专项 `56/56`、
  持久层/网络/取消专项 `20/20`、Shared PRD-063/064/084 `116/116`、Gateway PRD-063/064 `74/74`、
  Adapter v2 `10 specs / 103 cases`、Control PRD-063/064 `12/12`、Console `876/876`、lint `269 files`、
  Runtime 合同 `8/8`、Browser harness `7/7` 与 Browser driver `6/6` 通过。权威 Runtime JSON 为
  `.artifacts/prd084-runtime/20260816T183036Z-c60eb988.json`，SHA-256 为
  `F226D7665714C6AF335C72AE64E33392976337CE7A1E8850319384C8FFDBFDDC`；权威 Browser 批次
  `.artifacts/prd084-browser/final-20260817-01/` 的 JSON/manifest SHA-256 分别为
  `F1C6EAF61B496956326CD56974058A9250FAF8AF20C23816F1F9EFF9A949507E`、
  `9A20078068521D2C3E1AC33C838E7EDBBA5060F112617C5BABBA28FC66290F13`，四张 PNG 已逐图复核。
  详见 [PRD-084 设计](docs/prd/PRD-084-local-model-endpoint-access.md)、
  [本地模型 Endpoint API](docs/api/local-model-endpoints.md) 和
  [PRD-084 测试记录](docs/testing/PRD-084-tests.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-083 - 第三方 MCP 沙箱网络 ACL` 已完成：0076、Network Policy revision、DNS 全地址 ACL、HTTPS
  pinned transport、独立 Scheduler/Sandbox、短时 envelope/permit、durable call intent、熔断、EVT-095
  source outbox、PAGE-115 与三角色 Compose 边界均已交付。后端实时校验 tenant/RBAC/Grant/Schema/Policy，
  固定错误携带 request_id，成功、阻断、超时、用量和成本状态可追踪；Endpoint、Token、参数、结果、IP/CIDR
  与第三方异常正文不进入日志或证据。最终 Shared `9/9`、Worker `28/28`、Scheduler `20/20`、0076/Compose
  `17/17`、相邻 MCP `35/35`、Console `33/33`、Runtime/Browser Python 合同 `62/62` 和 Browser driver
  `6/6` 通过。权威 Runtime `prd083-final-20260816-ac` 从空 SQLite 完成
  `76/0076 -> 76/0076 -> 75/0075 -> 76/0076`，四阶段 integrity/FK 通过，JSON SHA-256 为
  `C5FFB99B15FF2156D26408B08C4F8ACD2C4EC07610A06799DDDFE471B23D0047`；权威 Browser
  `prd083-final-20260816-f` 四张 PNG 已逐图复核，JSON/manifest SHA-256 分别为
  `30685F52892AE64838CCA4C32C1CAA044B974D311D8E0425016BFFDC8C61AE94`、
  `0C59FEE846B45B54461DC00D7C86C2591F47EBF9B87248CB3850128D80830044`。固定端口、owned process、Sandbox
  session/connection/task、SQLite sidecar 和正式 work/staging 最终归零。详见
  [PRD-083 设计](docs/prd/PRD-083-third-party-mcp-sandbox-network-acl.md)、
  [MCP 工具 API](docs/api/mcp-tools.md)、[PRD-083 测试记录](docs/testing/PRD-083-tests.md)、
  [Runtime](.artifacts/prd083-runtime/prd083-final-20260816-ac) 和
  [Browser](.artifacts/prd083-browser/prd083-final-20260816-f)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-082 - MCP 工具注册与权限` 已完成：Shared 发布 RBAC revision 17、MCP exact contracts、
  Draft 2020-12 Schema profile、分域加密摘要、参数 DLP、返回脱敏和 `mcp.tool_called` 安全事件；0075
  新增 tenant-qualified Server、Credential、Operation、SyncJob、Tool、Grant、Invocation、Attempt、
  Approval 绑定、event/outbox 与 scheduler 事实。Control 提供 Server 注册/刷新/停用/启用、Tool Schema
  与 policy、异步 invoke/result、后端实时 RBAC、非自审高危审批、公平 lease、失败/结果未知恢复和全路径
  no-store；PAGE-114 提供 Servers/Tools/Invocations 三个 tab、原生注册与 Schema Dialog、安全调用摘要和
  双视口布局。生产 executor 仍固定 `disabled`，正式 Runtime 只使用 test-only literal-loopback 官方 SDK
  adapter，不把 PRD-083 的沙箱/网络 ACL 能力冒充为已交付。最终 PRD-082 Python 专项合集 `146/146`，
  其中 Scheduler `30/30`、Runtime contracts `36/36`、Browser harness `8/8`；Console 全量 `846/846`、
  Browser driver contracts `6/6`、lint `261 files`，Ruff/compile 通过；这些分组存在重叠，不相加冒充
  一次根级全量。权威 Runtime 从空 SQLite
  完成 `75/0075 -> 75/0075 -> 74/0074 -> 75/0075`，四阶段均 `integrity_check=ok`、外键违规 0；
  DLP block、返回脱敏、高危审批、已知失败、结果未知、失败请求、用量/成本和停服恢复均可追踪，固定端口、
  owned process 与 SQLite sidecar 最终为 0。权威 Browser `prd082-page114-20260816-r8` 在
  `1280x720`/`390x844` 覆盖三 tab、Server disable/enable、Schema Dialog 和移动注册 Dialog，四张截图
  已目视复核；外部请求、禁止 query、Console/Page error、全局横向 overflow 和敏感明文命中均为 0。
  正式生命周期验收确认会话过期时权限矩阵失败关闭，动态撤权后 PAGE-114、导航和 root 状态立即清除并
  回到概览，viewer 无入口且直达 `#mcp-tools` 仍不渲染页面；端口、进程、数据库、sidecar 和临时凭据
  最终全部清零。四张正式 PNG 已由主 Agent 逐张目视复核。
  详见 [PRD-082 设计](docs/prd/PRD-082-mcp-tool-registration-permissions.md)、
  [MCP 工具 API](docs/api/mcp-tools.md)、[PRD-082 测试记录](docs/testing/PRD-082-tests.md)、
  [Runtime](.artifacts/prd082-runtime/prd082-final-runtime-7) 和
  [Browser](.artifacts/prd082-browser/prd082-page114-20260816-r8)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-081 - SIEM/安全事件格式` 已完成：Shared 提供 exact `SecurityEventPayload`、JSON/CEF
  deterministic wire 与 HMAC 实际字节合同；0074 新增 tenant-qualified 安全 source、5 秒聚合、成员、
  outbox、Endpoint 分钟限流和 Webhook format/wire 冻结。Control 以公平 lease scheduler 从 DLP、会话风险和
  Provider Key 异常生成 `security.event_pushed`，支持失败关闭、重启恢复、死信/replay 与 Secret 轮换；
  Gateway 只提交安全 source，不在热路径直连 SIEM。PAGE-113 已增加 JSON/CEF 模式、1..600 每分钟上限和
  冻结投递摘要，后端 `webhook.manage` 仍是唯一授权边界。最终 Shared/Control `34/34`、Gateway `36/36`、
  Runtime/Browser 合同 `9/9`、Node Browser 合同 `5/5`、Console 全量 `822/822`，Console lint
  `254 files`；新增 Python 文件全规则 Ruff 通过。权威 Runtime 从空 SQLite 完成
  `74/0074 -> 74/0074 -> 73/0073 -> 74/0074`，四阶段均 `integrity_check=ok`、外键违规 0，三类真实
  安全 source、聚合、限流、JSON/CEF、HMAC、失败追踪和 Control 重启恢复均通过。权威 Browser 在
  `1280x720` 与 `390x844` 实际创建两个 Endpoint 并完成两次投递，外部请求、Console/Page error、布局
  溢出、重叠、接收端验签错误和敏感明文命中均为 0；4 张截图已人工复核。详见
  [PRD-081 设计](docs/prd/PRD-081-siem-security-event-format.md)、
  [SIEM Webhook API](docs/api/webhook-siem.md)、[PRD-081 测试记录](docs/testing/PRD-081-tests.md)、
  [Runtime](.artifacts/prd081-runtime/final-20260816t) 和
  [Browser](.artifacts/prd081-browser/final-20260816q)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-080 - 账单争议与调整流程` 已完成：Shared 发布 RBAC revision 16、统一错误和
  `billing_dispute.created` exact 事件；0073 新增 tenant-qualified dispute、item、settlement hold、
  transition、Adjustment link、event/outbox 与公平 scheduler 事实。Control 提供严格列表/详情/创建/CAS
  审核、后端实时 RBAC、自审阻断、原子 Audit/FinOps reversal 和 Webhook 恢复；PAGE-112 提供 request/time
  两种申请、审核、部分批准/拒绝、原生 Dialog 与完整 owner 清理。PRD-080 Python 专项 `58 passed`，
  Console 全量 `815/815`，lint `253 files`，PRD-078/079 当前 0073 兼容回归 `30 passed`。权威 Runtime
  从空 SQLite 完成 `73/0073 -> 73/0073 -> 72/0072 -> 73/0073`，四阶段均
  `integrity_check=ok`、外键违规 0，并验证真实 Gateway charge、失败追踪、部分冲正和 Control 崩溃恢复。
  最终安全复核还锁定零金额 scope 的固定 404、服务端链路 ID、Audit `User-Agent` 脱敏、未知异常固定 503/
  全路径 no-store，以及多 item 合法汇总金额。权威 Browser `prd080-browser-final-20260815-05` 在
  `1280x720` 与 `390x844` 覆盖完整 PAGE-112 工作流、动态撤权、迟到响应和登出清理，4 张截图人工复核通过，
  外部请求、Console/Page error、布局溢出、重叠和敏感明文命中均为 0。详见
  [PRD-080 设计](docs/prd/PRD-080-billing-dispute-adjustment-workflow.md)、
  [账单争议 API](docs/api/billing-disputes.md)、[PRD-080 测试记录](docs/testing/PRD-080-tests.md)、
  [Runtime](.artifacts/prd080-runtime/prd080-runtime-final-20260815-05) 和
  [Browser](.artifacts/prd080-browser/prd080-browser-final-20260815-05)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-079 - 套餐、试用与额度类型` 已完成：Shared 发布 RBAC revision 15、四类额度、
  结算分摊、统一错误和 `quota.exhausted`/`trial.expired` 安全事件合同；0072 新增全量
  tenant-qualified Plan、Bucket、Adjustment、Reservation、Settlement、Allocation 与 source/outbox
  事实。Control 提供 Plan CAS、四桶查询/调整、试用到期 scheduler 和 Webhook 恢复；Gateway 在
  Provider 前按 `trial/free/gift/paid` 原子预留，并在唯一 Usage/FinOps charge 事务内按真实成本结算，
  失败、未知结果、spool、Batch、A/B 和 Response Cache 均保留可追踪 owner。PAGE-110/111 提供套餐
  与额度管理，后端实时 RBAC 是唯一授权边界。PRD-079 专项 `73 passed`，Console 全量 `792/792`，
  Browser 合同 `9/9`，OpenAI-compatible `28 passed`，FinOps/Webhook `26 passed`，受影响兼容回归
  `29 passed`。权威 Runtime 从空 SQLite 完成
  `72/0072 -> 72/0072 -> 71/0071 -> 72/0072`，四阶段均 `integrity_check=ok`、外键违规 0；权威
  Browser `main-browser-final-20260814-i` 在 `1280x720` 与 `390x844` 覆盖 Plan 保存、四类调整、
  报表拆分、Dialog busy/Escape、撤权、迟到响应和登出清理，4 张截图人工复核通过，外部请求、
  Console/Page error、页面级溢出、重叠和敏感明文命中均为 0。详见
  [PRD-079 设计](docs/prd/PRD-079-plan-trial-quota-types.md)、
  [套餐与额度 API](docs/api/plan-billing.md)、[PRD-079 测试记录](docs/testing/PRD-079-tests.md)、
  [Runtime](.artifacts/prd079-runtime/run-agent4-b72e1d) 和
  [Browser](.artifacts/prd079-browser/main-browser-final-20260814-i)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-078 - 移动端/企微钉钉审批入口` 已完成：Shared 提供独立可轮换审批 HMAC 合同、RBAC
  revision 14、统一错误和 `approval.created/timeout` exact payload；0071 新增全量 tenant-qualified
  ApprovalTask、Decision、step-up、writeback、scheduler lease 与 source/outbox 事实，并以原子 guard 保护
  正反迁移。Control 提供有界内部创建、列表/详情、后端密码二次验证、approve/reject、目标域双重实时 RBAC、
  数据库时钟 timeout、公平租约及事件/Redis 回写恢复；PAGE-108/109 提供桌面审批中心与窄屏单任务卡片，
  外部企微/钉钉/飞书仅接收既有签名 Webhook 通知，不能成为授权根。最终 Python 专项 `97 passed`，
  Console/登录 owner/健康状态 `26/26 passed`，Node Browser 合同 `46/46 passed`。权威 Runtime
  `prd078-runtime-20260814-08` 从空 SQLite 完成
  `71/0071 -> 71/0071 -> 70/0070 -> 71/0071`，四阶段均 `integrity_check=ok`、外键违规 0；
  权威 Browser `prd078-browser-final-20260814-36` 在 `1280x720` 与 `390x844` 覆盖标准/高风险审批、
  step-up、忙碌态、撤权、owner 漂移、迟到响应和登出清理，3 张截图人工复核通过，外部请求、运行异常、
  布局溢出、重叠和敏感明文命中均为 0。详见
  [PRD-078 设计](docs/prd/PRD-078-mobile-enterprise-approval-entry.md)、
  [审批 API](docs/api/approvals.md)、[PRD-078 测试记录](docs/testing/PRD-078-tests.md)、
  [Runtime](.artifacts/prd078-runtime/prd078-runtime-20260814-08) 和
  [Browser](.artifacts/prd078-browser/prd078-browser-final-20260814-36)。总体目标仍为
  `all_108_prds_complete=false`。
- `PRD-077 - 只读管理员 AI 助手` 已完成：Shared 提供有界自然语言 parser、
  只读动作闸门、typed claim/source 图、RBAC revision 13、统一错误和 `assistant.queried` exact payload；
  0070 新增 tenant-qualified AssistantQuery、AssistantAudit、event source/outbox 和原子 down guard；Control
  提供四类确定性只读查询、来源域后端 RBAC、动态撤权、双长期 SQLite WAL/FULL 连接、绝对 deadline、
  原子 `last_seen`/审计与 lifespan 恢复；PAGE-107 使用 owner/generation/Abort 隔离问题和回答内存。
  权威 Runtime 从空 SQLite 完成 `70/0070 -> 70/0070 -> 69/0069 -> 70/0070`，四阶段
  `integrity_check=ok`、外键违规 0；20 个并发 attempt 为 9 个 200 与 11 个受控 503，并留下 20 组唯一审计图，
  停服恢复后 outbox 清空，明文命中与 SQLite sidecar 均为 0。权威 Browser 实际验证 PAGE-107 的权限、
  动态撤权、来源导航、防篡改、迟到与登出清理；`1280x720`/`390x844` 无横向溢出、文本溢出或控件重叠，
  外部请求、私密网络字段及明文命中均为 0。最终证据和分组测试记录见
  [PRD-077 测试记录](docs/testing/PRD-077-tests.md)，各分组存在重叠，不相加冒充一次根级全量。详见
  [PRD-077 设计](docs/prd/PRD-077-read-only-admin-ai-assistant.md) 和
  [管理员 AI 助手 API](docs/api/admin-assistant.md)。总体目标仍为 `all_108_prds_complete=false`。
- `PRD-076 - Response Cache 可选能力` 已完成：Shared 加密/合同、0069/RBAC revision 12、
  tenant-qualified Policy/Key Generation/Entry/Fill/Tag/Request Fact/Hit/Invalidation/Event/Outbox、
  Gateway 精确资格判定与单写者填充、Control CAS API 与恢复调度、Usage/FinOps/预算/RequestLog 投影、
  PAGE-106、维护 CLI 和部署配置均已落地。最终专项为 Shared `39 passed`、Gateway `29 passed`、
  0069 `5 passed`、Control `9 passed`、维护 `11 passed`、Runtime 合同 `35 passed`、Browser 合同
  `20 passed`；Console 全量 `734 passed`、lint `230 files passed`。这些分组存在包含关系，不相加冒充
  一次根级全量。
  `prd076-final-20260811-06` 从空 SQLite 完成 fresh/repeat/down/re-up：`69/0069 -> 68/0068 -> 69/0069`，
  各阶段均 `integrity_check=ok`、FK 0；真实回环服务覆盖 miss/fill/hit、并发单填充、TTL、隔离、收费、
  预算阻断、失效、Control 停机恢复、commit-unknown、failure/usage spool 和 down guard，最终 pending、
  quarantine 与 sidecar 均为 0，148 个文件的敏感明文命中为 0。PAGE-106 Browser 批次
  `prd076-browser-final-20260811-24` 在 `1280x720`、`390x844` 覆盖空/配置/编辑/冲突/失效/撤权和
  RequestLog hit；9 张截图非空，布局溢出、重叠、外部请求、异常 Console、运行文件明文命中均为 0，
  10 项 manifest SHA-256 全匹配。详见
  [PRD-076 设计](docs/prd/PRD-076-response-cache-optional-capability.md)、
  [Response Cache API](docs/api/response-cache.md)、[PRD-076 测试记录](docs/testing/PRD-076-tests.md)、
  [Runtime](.artifacts/prd076-runtime/prd076-final-20260811-06) 和
  [Browser](.artifacts/prd076-browser/prd076-browser-final-20260811-24)。
- `PRD-075 - 模型 A/B 测试与质量评估` 已完成：Shared Experiment 合同、0068/RBAC revision 11、
  tenant-qualified Experiment/Assignment/Execution/Outcome/Settlement/Result、Control API 与
  Scheduler/Reaper、Gateway 真实流量精确分桶、`experiment.completed` outbox、PAGE-105 和部署配置均已落地。
  最终受影响回归为 Shared/Provider `180 passed`、Gateway `204 passed`、Control/0068 `120 passed`、
  Runtime 验收器 `21 passed`；Console 全量 `723 passed`、lint `225 files passed`。这些分组可能覆盖相邻合同，
  不把它们相加冒充一次根级全量。
  `prd075-final-20260810T011951Z` 从空库完成 fresh/repeat/down/re-up，四阶段均 `integrity_check=ok`、FK 0；
  三个实验全部 completed，A/B 各四次调用，失败/用量/成本可追踪，Control 停机后恢复 finalizer 与 Webhook，
  outbox pending 0，145 个受控文件的随机明文 canary 命中 0。PAGE-105 Browser 批次
  `prd075-final-20260809T234829Z` 在 `1280x720`、`390x844` 验证两张冻结结果卡、UI 点击创建、独立 API
  幂等/冲突合同及动态撤权后的 DOM/state 清理与后端 403；11 张截图非空，13 项 manifest SHA-256 全匹配。
  详见
  [PRD-075 设计](docs/prd/PRD-075-model-ab-testing-quality-evaluation.md)、
  [Experiment API](docs/api/experiments.md)、[PRD-075 测试记录](docs/testing/PRD-075-tests.md) 和
  [Runtime](.artifacts/prd075-runtime/prd075-final-20260810T011951Z)、
  [Browser](.artifacts/prd075-browser/prd075-final-20260809T234829Z)。
- `PRD-074 - 批量任务 Scheduler 与 Worker` 已完成：Shared Batch 合同与 canonical 价格 wire、
  0067/RBAC revision 10、tenant-qualified BatchJob/BatchTask/Attempt、Control API、独立 Scheduler/Worker、
  短事务 lease/heartbeat/Reaper、预算 hold/allocation、Gateway 一次性 grant 与实时优先 gate、加密结果、
  RequestLog/Usage/FinOps、`batch_job.created/completed` source-outbox、PAGE-104 和 Compose 均已落地。
  实现后复核修正了同秒公平轮转、已知失败的 Reaper 语义、无 Attempt 取消/截止、Worker 能力边界、
  Webhook pending 的 `source_request_id` 恢复、统一错误体解析，以及 Control/Gateway 单价精度漂移；
  Shared 现统一输出固定 12 位单价，真实 Control route plan 已直接通过 Gateway strict validator。
  正式 Runtime 在空 SQLite 上通过 `67/0067`、`integrity_check=ok`、外键违规 0、实时 busy 不触达
  Provider、过期 lease 恢复、取消、撤权、成本/用量/失败追踪、Control 停机 outbox 恢复和全部自建进程
  清理；23 个数据库/对象/日志文件中的 Provider Key、Token、Prompt、文件正文和 Response 明文命中为 0。
  PAGE-104 Chromium `1280x720`、`390x844` 列表和移动创建弹窗均无横向溢出、重叠或文本溢出，
  结果关闭清理、迟到响应隔离和未授权入口裁剪通过。详见
  [PRD-074 设计](docs/prd/PRD-074-batch-scheduler-worker.md)、
  [Batch API](docs/api/batch-jobs.md)、[PRD-074 测试记录](docs/testing/PRD-074-tests.md)、
  [Runtime](.artifacts/prd074-runtime/prd074_20260808T105000Z_runtime_final2) 和
  [Browser](.artifacts/prd074-browser/prd074_20260808T094300Z_page104)。
- `PRD-073 - 隐藏 Prompt Injection 检测`的设计、两轮编码前自审、Shared TXT/DOCX/PDF 检测、
  固定版本离线 OCR、Control 扫描/报告/CSV/模型准入、PAGE-103、0065 安全事实与 RBAC revision 9、
  后端 tenant/双权限/统一错误和 7 字段安全 Webhook 均已实现。实现后安全终审关闭 PDF
  `ToUnicode/CMap` 语义绕过和 finding 饱和后丢失 Prompt/coverage 两项问题；最终交叉审查又以追加
  0066 关闭 source 已提交、delivery intent 尚未创建时的崩溃丢失窗口。0066 trigger 在扫描同一事务
  写 tenant-qualified pending outbox，并回填既有 source；Control lifespan 启动即恢复，路由只 wake
  scheduler。worker 使用 90 秒 lease；每轮先在独立短 `BEGIN IMMEDIATE` 事务中真实 claim 一条到期
  source，提交 `processing`、attempt、token、owner 和 lease。随后当前且未过期的 owner 在另一个独立
  事务中，以 tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 复核并补齐 0065 遗留的部分
  delivery、持久化 `delivery_snapshot_frozen_at` 并续租；即使订阅集合为空也冻结，之后新增 Endpoint
  不追溯旧 source。每次真实网络尝试前仍以同一组完整 CAS 再续租。lifespan 以 `threading.Event` 协作停服并等待同步 worker 完成收尾，不通过取消
  `to_thread` 遗留写库线程。稳定 `fsev_` event ID 提供 at-least-once，失败按 5/30/300 秒退避且
  最多 10 次。source 终态与剩余 pending delivery/死信同事务，Webhook attempt 使用状态、序号和
  Secret 版本 CAS 阻断迟到覆盖；坏 source 必须先完成真实 claim，再由当前 owner 交叉验证并以完整 CAS
  固定隔离，随后继续处理后续 tenant 事件，不让损坏队首形成
  全局阻塞。
  0066 迁移与聚焦回归、20 个 Runtime 合同节点和新的 `66/0066` 真实 Runtime 均已通过。正式验收
  真实停止 Control，在停服窗口提交 source/outbox，再由新 Control lifespan scheduler 以 attempt 1
  恢复为 `dispatched`；验收器主动 drain 为 false、临时 claim 已清空、隐私命中为 0。权威 Runtime
  为 [prd073_20260806T062221Z_b2632ac89f57fc3a](.artifacts/prd073-runtime/prd073_20260806T062221Z_b2632ac89f57fc3a)，
  JSON SHA-256 为 `E2EFF29227A4246AE8FBB86B5B3C973C6E5212A1568AA32D9ABFB05B0DAE34FD`。
  0066 不改 PAGE-103，既有双视口 Browser 证据继续有效；2026-08-06 又在实际隔离库上复验桌面、
  移动、幂等扫描、零横向溢出、零页面错误和登出清理。详见
  [PRD-073 设计](docs/prd/PRD-073-hidden-prompt-injection-detection.md)、
  [文件安全 API](docs/api/file-security.md)、
  [PRD-073 测试记录](docs/testing/PRD-073-tests.md)、
  [有效 Browser](.artifacts/prd073-browser/prd073_20260805T045800Z_4f3ee3d6aa3f233b)。
- `PRD-072 - 文件上传与文本解析基础`：设计与两轮自审、Shared 文件合同、Control 文件 API、
  PAGE-101/102、0064、RBAC revision 8、加密 Blob、TXT/DOCX/PDF 抽取、DLP 预扫描、Token 估算、
  后端租户/权限/统一错误和安全 Webhook 均已完成。专项结果为 Shared `25 passed`、Control 与导航
  `15 passed`（文件 API 13、导航 2）、迁移 `6 passed`、Console 最终聚焦 `18 passed`、Runtime 验收器
  `6 passed`（含管理员密码只从环境注入并读取后移除）、Console lint `209 files passed`，中文注释、语法和
  日志隔离门禁通过。正式 Runtime 为 `status=passed`：三格式 ready、DLP 403、权限 403、跨租户 404、
  `64/0064`、数据库完整性/外键、认证密文对象和敏感明文零命中均通过；Windows UTF-16LE 日志另以
  新的多编码 canary 规则实时复扫，三份日志命中为 0。Browser 在 `1280x720` 与 `390x844` 完成列表、
  筛选、DLP/病毒/Token、SecurityAdmin 只读降级和注销清理，页面无横向溢出且正文、对象键、hash、
  规则内容没有进入 DOM。详见
  [PRD-072 设计](docs/prd/PRD-072-file-upload-text-extraction.md)、
  [文件 API](docs/api/files.md)、
  [PRD-072 测试记录](docs/testing/PRD-072-tests.md) 和
  [正式制品](.artifacts/prd072-runtime/prd072_20260804T212908Z_922f8ab2ff200912)。
- `PRD-071 - 输出侧安全检测与流式滑窗`：设计与两轮自审、Shared/Gateway/Control/Console、0063、
  DLP `scan_scope`、PAGE-099/PAGE-100、FinOps 争议投影、后端租户/RBAC/统一错误和 Webhook 均已完成。
  相关自动化为 Shared/Gateway `60 passed`、0063 `9 passed`、Control 相邻 `47 passed`、Console 相邻
  `59 passed`、最终页面直接回归 `9 passed`、Runtime 验收器合同 `7 passed`、lint `203 files passed`，
  `py_compile` 与中文注释门禁通过。正式 Runtime 为 `status=passed`：clean 200、非流阻断 403、扫描不可用
  503、跨 chunk 流阻断唯一 SSE error、401/403/404、账单/事件 owner、`63/0063`、完整性/外键、隐私和
  一次性资源清理均通过。应用内 Browser `1280x720` 与 `390x844` 无覆盖/横向溢出，Viewer 前后端权限和
  DOM 隐私通过。详见
  [PRD-071 设计](docs/prd/PRD-071-output-safety-streaming-window.md)、
  [输出安全 API](docs/api/output-safety.md)、
  [PRD-071 测试记录](docs/testing/PRD-071-tests.md) 和
  [正式制品](.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64)。
- `PRD-070 - 多轮会话风险引擎`：设计与两轮自审、0062、RBAC revision 7、Redis 原子累计/持久
  evidence、current/previous HMAC 轮换 verifier、Control 处置、PAGE-098 代码、租户/权限/统一错误、
  真实三服务/API/RESP 和非浏览器验收均已通过。聚焦 Python `86 passed`，HMAC 相关回归 `35 passed`，
  数据库文件系统故障节点另行 `1 passed`，PAGE-098 `11 passed`，Console lint `201 files`，定向 Ruff 与
  中文注释门禁通过；开发库为 `62/0062`，运行证据 `integrity_check=ok`、外键违规 0、敏感明文命中 0。
  应用内 Browser `1280x720` 与 `390x844` 已验证默认/已处置筛选、详情、2 个冻结 overlay、5 条动作
  时间线和零横向溢出；桌面筛选栏溢出已修复并复验。并行 Edge extension 超时文件只作为非权威诊断，
  不覆盖页面验收结论。详见
  [PRD-070 设计](docs/prd/PRD-070-multi-turn-session-risk-engine.md)、
  [会话风险 API](docs/api/session-risks.md) 和
  [PRD-070 测试记录](docs/testing/PRD-070-tests.md)。

历史交接定位（已关闭）：

- Excel 的 `01_PRD总表` 第 90 行、`03_M2-M3扩展交付` 第 26 行、PAGE-121、API-402..406、
  OBJ-122/123、EVT-101/102 与 TC-0177/0178 已和 README、`docs/prd`、`docs/testing` 的 PRD-088 完成状态
  交叉确认。PRD-088 之后的下一条权威未完成项为
  `PRD-089 | M2-企业增强 | P2 | 扩展层 | 数据导出删除 | 租户数据导出与注销流程`，不是仅按编号推断。
  该历史登记已由下方 PRD-089 完成项关闭。旧总清单的 PRD-090..108 已整体废弃；新路线图的
  `PRD-090 - SaaS总体架构与兼容迁移基线` 至 `PRD-097 - 个人与企业初始化向导重构` 已完成；当前
  权威未完成项为 `PRD-098 - SaaS套餐、试用、订阅与Entitlement`，`saas_incremental_roadmap_complete=false`。
- PRD-068 已完成：设计与两轮自审、0060、租户/权限/错误、Shared/Gateway/Control/Console 专项、
  Snapshot v12 真实 apply、数据库完整性和三服务运行均通过。最终 PAGE-095 聚焦回归为 `16 passed`；
  应用内 Browser `1280x720` 与 `390x844` 验证了五模板、已应用后的 incompatible 防重复、预算模板
  ready-with-warning、完整确认摘要、Snapshot v12、无页面级横向溢出、零页面错误和注销清理。验收中
  修正了 Candidate CAS 应匹配 `before_candidate_hash` 以及移动确认框长 ID 撑宽问题；中文注释门禁通过。
  证据见 [PRD-068 设计](docs/prd/PRD-068-industry-scenario-policy-template-library.md)、
  [策略模板 API](docs/api/policy-templates.md) 和
  [PRD-068 测试记录](docs/testing/PRD-068-tests.md)。
- PRD-069 已完成：本地分类器、0061、RBAC revision 6、`POST /security/classify-intent`、Gateway
  DLP 后/限流前语义闸门、Usage spool v3、RequestLog 七状态以及 PAGE-096/PAGE-097 均通过。唯一一次
  根级 Python 全量为 `2146 passed / 4 failed`，四项历史迁移/RBAC 合同漂移原 node 定向 `4/4 passed`，
  未重跑全量；新增性能/Webhook/shutdown 门禁 `4/4 passed`。20k 中文 strong/light P95 为
  `6.7792/8.1162ms`，三服务最终 `200/200/200`，开发库 `61/0061` 且完整性/外键通过，Browser 已显示
  非空登录页，39 个运行日志文件敏感命中 0。
  合同与当前证据见 [PRD-069 设计](docs/prd/PRD-069-lightweight-sensitive-intent-classification.md)、
  [轻量敏感意图 API](docs/api/intent-security.md) 和
  [PRD-069 测试记录](docs/testing/PRD-069-tests.md)。
- PRD-067 已完成：设计与两轮自审、后端交叉终审、0059、真实 Provider/Gateway 治理调用、PAGE-094
  模板管理、PAGE-093 模板调用、租户/权限/异常、应用内 Browser 双视口和资源清理均通过。最终 Python
  受影响组合 `73 passed`、Console 相关 `38 passed`、移动布局定向 `10 passed`、lint `188 files`，
  中文注释门禁通过；两次真实模板 Chat 均为 18 tokens，开发库 `59/0059` 且完整性通过，测试
  Session/Chat/Grant/临时 Key 均归零。设计、API、测试和 Runtime 证据见
  [PRD-067 设计](docs/prd/PRD-067-enterprise-prompt-template-library.md)、
  [Prompt 模板 API](docs/api/prompt-templates.md)、
  [PRD-067 测试记录](docs/testing/PRD-067-tests.md) 与
  `.artifacts/prd067-runtime/run-20260802T110138Z-c00b97cac3/runtime-evidence.json`；
  `all_108_prds_complete=false`。

PRD-062 已完成：Control API 提供 `POST /config/export`、`POST /config/import/validate`、
`POST /config/import`，0054 配置迁移事实与 `config.transfer.read/write`，以及标准库客户端
`scripts/gatewayctl.ps1` / `python -m metahub_shared.gatewayctl.cli`。当前合同使用 2 MiB、深度 8、
200000 节点和 5000 总行资源门禁；manifest hash 排除仅用于溯源的 `exported_at`；active Virtual Key
在 apply 前必须预留私有密钥文件。设计、HTTP 合同和 CLI 用法见
[PRD-062 设计](docs/prd/PRD-062-config-import-export-cli.md)、
[配置迁移 API](docs/api/config-transfer.md) 和
[gatewayctl 使用说明](docs/development/gatewayctl-config-transfer.md)。API `6 passed`、CLI
`33 passed`、Audit `13 passed`、迁移 `3 passed`、直接回归 `21 passed`、中文注释门禁与两套 0054 实迁移
均通过；真实 source/target CLI、权限、租户和 5 Token Gateway 调用见
[PRD-062 测试记录](docs/testing/PRD-062-tests.md)。

PRD-060 已落地一次性 Token 邀请、tenant-qualified Invitation、实时后端 `user.manage`、同事务
AuditLog/source event、固定接受审计脱敏、0051/0052 和 PAGE-087。迁移与后端专项、Console 全量、
开发库升级、三服务 Runtime、Microsoft Edge 1280x720/390x844 真实页面及隐私门禁均已通过。
设计、接口和最终测试记录见 [PRD-060 设计](docs/prd/PRD-060-user-invitation-join-flow.md)、
[用户邀请 API](docs/api/invitations.md) 和
[PRD-060 测试记录](docs/testing/PRD-060-tests.md)。

已具备：

- 数据面：`gateway-data-plane`，提供 `/health`、`/internal/health`、`/version`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/models`、`/v1/embeddings`、`POST /v1/adapter-contract-probes/identity`、`POST /feedback`。Responses 和 Anthropic Messages 是复用同一 Virtual Key、路由、预算、DLP、限流、账本与流式治理链路的协议门面；客户端只需网关地址和个人 KEY，工具专有高级能力仍按客户端版本单独验收。其余路由与审计约束保持不变。
- 控制面 API：`control-plane-api`，提供 `/health`、`/internal/health`、`/version`、`/error-codes`、`GET/POST /error-maps`、`PATCH /error-maps/{error_map_id}`、`GET /internal/error-maps/{tenant_id}/current`、`/internal/config/summary`、`/auth/login`、`/auth/logout`、`/auth/me`、`/tenant/current`、`/user/preferences`、`/users`、`/environments`、`/projects`、`/applications`、`/virtual-keys`、`/virtual-keys/{id}/limits`、`/providers`、`/providers/{id}/keys`、`/providers/{id}/probe`、`/provider-health`、`/provider-keys/{id}`、`/models`、`/model-prices`、`/model-aliases`、`/model-aliases/{id}/fallback`、`/model-aliases/{id}/routing-mode`、`/model-aliases/{id}/cache-affinity`、`/model-quality-scores`、`/policies`、`/policies/validate`、`/policies/dry-run`、`/policies/{policy_id}`、`/policy-snapshots/current`、`/policy-snapshots`、`/policy-snapshots/publish`、`/policy-snapshots/{id}/rollback`、`/internal/policy-snapshots/{tenant_id}/current`、`POST /security/classify-intent`、`/request-logs`、`GET /request-explain/{source_request_id}`、`/audit-logs`、`GET /audit-logs/export`、`/finops/ledger`、`/finops/adjustments`、`/usage/summary`、`/usage/records`、`/usage/export.csv`、`/usage/exports/{job_id}`、`/usage/exports/{job_id}/download`、`/budgets`、`/budget-status`、`GET /cost-anomalies`、`PATCH /cost-anomalies/{id}/action`、`/dlp/rules`、`/webhooks`、`/webhooks/{id}/test`、`/webhooks/{id}/deliveries`、`/webhooks/{id}/replay`、`/webhooks/{id}/rotate-secret`、`/dashboard/overview`、`/console/navigation`、`/console/preferences`、`/console/events`、`/developer/quickstart-data`、`/developer/sdk-examples/python`、`/developer/sdk-examples/python/download`、`/developer/sdk-examples/javascript`、`/developer/sdk-examples/javascript/download`、`/debug/chat-completions`、`POST /feedback`、`GET /feedback/summary`。`GET /model-aliases` 返回当前 CacheAffinity、RoutingPolicy、休眠质量配置和候选安全摘要；`quality_cost` PATCH 使用完整五字段配置、锁内 CAS 和同 request_id 持久幂等。`GET/PATCH /model-quality-scores` 分离统计版本与人工反馈版本，读权限为 admin/developer、写权限仅 admin，tenant_id 只从 Session 恢复。PRD-049 的反馈创建与汇总继续从 Session 恢复 tenant/actor，admin/developer 可用、viewer 后端拒绝，跨租户或跨 Key 源调用同形防枚举。PRD-051 的 FinOps 读取和调整均为 admin-only 后端权限，tenant_id 只从 Session 恢复，跨租户目标同形 404，统一错误携带 request_id/trace_id。PRD-053 的成本异常 GET/PATCH 同样固定为 admin-only，并只从 Session 恢复 tenant/actor；该合同已通过专项、跨租户与真实运行验收。PRD-056 的规则读权限为 admin/developer、写权限仅 admin，viewer 后端拒绝；tenant/actor 只从 Session 恢复，版本/head/operation/Source Event/Audit 同事务，内部快照只接受 Gateway service token。PRD-058 的审计导出固定 admin-only，tenant/actor 只从 Session 恢复；CSV、不可变 AuditExport、`audit.exported` AuditLog 与持久 source 在同一事务形成复核链。上述已完成合同均已通过对应 RBAC、租户和运行验证。
  PRD-064 已注册 `POST /internal/tenants/{tenant_id}/adapter-contract-runs`、`GET /adapter-contract-runs` 和
  `GET /adapter-contract-runs/{run_id}`；内部上报与 Session 读取分离鉴权，所有租户归属、实时
  `provider.health.read`、统一错误、request_id/trace_id 和 no-store 均由后端执行；真实 HTTP 的
  admin/developer、viewer 403、匿名 401、跨租户 404 和 Outbox 恢复已通过。
  PRD-075 已注册 `POST /experiments`、`GET /experiments`、`GET /experiments/{experiment_id}` 和
  `GET /experiments/{experiment_id}/results`；接口只从 Session 恢复 tenant/actor，并以后端实时 RBAC、
  tenant-qualified owner、统一错误与 request_id/trace_id 执行隔离，公开响应不包含请求级正文或 Provider 凭据。
- 健康检查：`/health/live` 不依赖外部资源，`/health/ready` 检查数据库等依赖。
- 控制台：`console-web`，提供统一主框架、角色化导航、Environment 设置、项目与应用、Virtual Key 环境绑定、模型别名、PAGE-076 预算中心（`tenant/project/virtual_key` 三级，Application 仅作用链兼容）、环境用量/日志、admin-only 用量 CSV 导出与异步任务轮询、管理操作审计、Provider 与 KeyPool、Provider 健康状态、模型目录、DLP、admin-only 策略管理/编辑器、快照发布/上一版回滚历史及策略模拟器、Webhook、快速开始、API 调试台和错误诊断等治理页面；PRD-048 已把 PAGE-068 扩展为四种模式和五字段质量配置，并新增 PAGE-069 模型评分页。admin 可维护人工评分，developer 只读，viewer 无 DOM/预取；RequestLog 展示请求时冻结的公式、版本、证据、分量和总分，不用当前 Score 回算历史。PRD-049 新增 PAGE-070 日志详情反馈与 PAGE-071 反馈报表，admin/developer 可提交或读取，viewer 无 DOM/预取；异步 owner、迟到响应和筛选后 DOM 收敛均有门禁。PRD-050 在 2026-07-24 的变更前全量基线为 Console `354/354`、lint `118 files`；当前 Browser 验收器局部修复只执行定向回归，最终 45 张真实 Chrome PNG 覆盖三角色与 1280/900/390 三档视口。 PRD-051 新增 PAGE-074 正式账本与 PAGE-075 用量详情；调整/冲正仅 admin 可见且后端再次校验，双视口真实 Browser 已验证布局、焦点、草稿销毁和隐私。PRD-056 新增 PAGE-081 错误码管理，并在 PAGE-082 既有日志详情展示冻结 attempt/session Fact；admin 可编辑、developer 只读、viewer 无导航/DOM/预取，409、焦点、注销和双视口均已真实验收。
  PRD-063 的 PAGE-090 展示冻结 Adapter 规范目录；PRD-064 的 PAGE-091 已实现契约运行摘要、筛选、分页和
  case 详情，并依赖后端导航 owner/实时权限。PAGE-091 最终专项 `15/15`，1280x720 与 390x844
  真实 Edge 的 403/503、恢复、空态、分页、详情、注销、非空截图和隐私门禁均已通过。
- 共享契约：`shared-sdk`，提供运行时基础契约、配置摘要、数据库迁移/seed 工具、认证安全工具、错误码注册表、错误诊断规则、i18n 基础字典、受限 Policy DSL 编译/纯求值合同、`policy_preview` 无副作用预演合同，以及 tenant-bound content hash、完整 artifact hash 和严格策略快照 wire 合同；PRD-048 在 PRD-047 两阶段排序上新增 `quality_cost_v1` 的整数/Decimal 质量、成本、延迟分量、冷启动/门槛矩阵和稳定原因码，Gateway 与 Dry-Run 复用同一纯函数。PRD-049 新增共享 Feedback contracts/validation/eligibility/storage/wire/webhook 包，Control 与 Gateway 复用同一规范化、幂等、租户归属、不可变写入和安全事件合同。PRD-056 新增 Provider 错误 builtin catalog、tenant overlay 编译、强 ETag/wire、有界事实提取、不可覆盖 guard、确定性 matcher、统一错误推导和 repr-safe 脱敏证据合同。
- 脚本与文档：`scripts`、`docs`、`infra`。PRD-047/048 接口合同见 [RoutingPolicy 与模型质量 API](docs/api/routing-policies.md)，PRD-048 证据见 [PRD-048 测试记录](docs/testing/PRD-048-tests.md)；PRD-049 的 [设计文档](docs/prd/PRD-049-request-feedback-adoption-records.md)、[Feedback API](docs/api/request-feedback.md) 和 [测试记录](docs/testing/PRD-049-tests.md) 分别作为设计、公开合同与最终验收的权威来源。PRD-058 的 [设计](docs/prd/PRD-058-audit-log-immutable-export.md)、[审计日志不可变导出 API](docs/api/audit-exports.md) 和 [测试记录](docs/testing/PRD-058-tests.md) 已登记最终合同与直接验收证据。
  PRD-064 的 [设计](docs/prd/PRD-064-provider-adapter-contract-test-framework.md)、
  [契约运行 API](docs/api/provider-adapter-contract-runs.md) 和
  [测试记录](docs/testing/PRD-064-tests.md) 分别登记编码前/实现后自审、稳定接口、最终证据和
  未伪写为全量零失败的分层回归边界。
- PRD-050 增量：Gateway 已在当前合格候选内实现 tenant/Virtual Key/data_class/endpoint revision/Prompt fingerprint 隔离的 pending/warm 端点与 Provider Key 亲和；Control 提供 admin-only `PATCH /model-aliases/{id}/cache-affinity` 和 admin/developer 读取，RequestLog 返回安全 Selection 摘要；PAGE-072/073 已通过三角色三视口真实 Chrome 验收。
- PRD-051 已完成：Gateway 将每条成功 Usage 同事务投影为 append-only 正式分录，主库暂不可用时写入本机 HMAC/fsync spool 并幂等恢复；Control 提供 admin-only `GET /finops/ledger` 与 `POST /finops/adjustments`，支持五类调整、冲正、签名游标、审计和统一错误；Console 提供 PAGE-074/075。Python 根级全量首次 `958 = 890 passed / 68 failed`，随后仅按受影响节点以迁移 `46/46`、Control `13/13`、Gateway `10/10` 闭环；Node `33/33`、Console `375/375`、lint `125 files`。真实 Runtime、双视口 6 张 Browser 截图及停服终态 19 项门禁均通过，权威 Browser 批次为 `artifacts/prd051/20260727T043500-browser-success-02`。
- PRD-052 已完成：预算按 `tenant/project/virtual_key` 三级累计 AND 生效，`application` 只保留在可信作用链中的历史兼容能力；正式消费统一取 `finops_ledger_entries` 中 `posted usage_charge/debit`，并叠加 outstanding reservation。Control 新增 admin-only、create-only 的 `POST /budgets`，重复规则稳定返回 409；读写均以后端 RBAC、Session 租户和 Environment/Project/Key 归属校验为准。Gateway 在 Project 预算耗尽时于 Provider 前返回带 `request_id` 的统一 402，且不产生 Provider attempt；Console PAGE-076 已通过 1280x720 与 390x844 两档真实 Browser 验收。迁移共 42 条、最高 0042，数据库 `integrity_check=ok` 且外键违规为 0；中文注释门禁、聚焦专项与相邻回归均通过。设计、接口、测试与验收证据分别见 [PRD-052 设计](docs/prd/PRD-052-tenant-project-key-three-level-budget.md)、[三级预算 API](docs/api/three-level-budgets.md)、[PRD-052 测试记录](docs/testing/PRD-052-tests.md)；历史 Runtime、Browser 和停服终态审计保留在本机 `artifacts/prd052/20260727T112603/`，不随源码仓库分发。该状态只代表 PRD-052 完成，不代表全部 108 条 PRD 已完成。
- PRD-053 已完成：0043、确定性成本异常检测、admin-only GET/PATCH、租户隔离、Webhook、Gateway 临时限流和 PAGE-077/078 已落地。Runtime 与 Browser 证据均为 `status=passed`；Browser 合同 `15/15`，双视口五状态正式截图 10 张；Node `33/33`、Console `401/401`、lint `134 files`。Python 根级全量实际结果为 `1178 passed / 30 failed`，30 个环境或旧合同失败随后以受影响范围 `34/34` 全部闭环，未虚写为全量零失败；中文注释和静止终态通过。设计、接口和测试见 [PRD-053 设计](docs/prd/PRD-053-cost-anomaly-detection-foundation.md)、[成本异常 API](docs/api/cost-anomalies.md) 与 [测试记录](docs/testing/PRD-053-tests.md)；历史 Runtime、Browser 和最终审计保留在本机 `artifacts/prd053/20260727T183515/`，不随源码仓库分发。该状态不代表全部 108 条 PRD 已完成。
- PRD-054 已完成：Control 新增 GET /request-explain/{source_request_id}，后端从
  Session 恢复 tenant/actor 并只允许 admin/developer；跨租户与不存在同形 404，成功
  读取原子保存不可变访问事实和 request.explanation_viewed source outbox。0044 新增
  request_explanations、request_webhook_delivery_links 及 tenant owner/不可变/lease
  门禁；Console PAGE-079 提供十阶段、十一区块解释页，并从 Request Logs 使用身份绑定
  的一次性内存 handoff 跳转，viewer 无 DOM/预取。公开合同见
  [请求解释中心 API](docs/api/request-explanations.md)，冻结设计见
  [PRD-054 设计](docs/prd/PRD-054-request-explainability-center.md)，直接结果见
  [PRD-054 测试记录](docs/testing/PRD-054-tests.md)。专项、0044 实迁移、真实
  Runtime/Browser、隐私与停服静止终态均已通过；该状态不代表全部 108 条 PRD 已完成。
- PRD-055 已完成：Control 的 `GET /request-logs` 支持 inline v1、个人
  SavedFilter 和 legacy 三种互斥来源、组合筛选、白名单排序及有界分页，并提供
  `GET/POST/PATCH/DELETE /request-logs/saved-filters`。后端只允许 admin/developer，
  tenant/owner 来自 Session，viewer 真实 403；查询使用 materialized CTE + window count，
  每次成功查询写不可变 `log.query_executed` source outbox。Console PAGE-080 提供高级查询、
  SavedFilter 显式应用和 PAGE-079 一次性内存跳转，筛选和源 request ID 不进入 URL/Storage。
  0045、十万行双租户 Runtime 和双视口八状态 Browser 均已直接通过；Runtime P95 为
  `491 ms`、最大 `510 ms`，Browser blocked/error/viewer prefetch 均为 0。冻结设计见
  [PRD-055 设计](docs/prd/PRD-055-trace-query-enhancement.md)，公开合同见
  [调用日志高级查询 API](docs/api/request-log-query.md)，测试记录见
  [PRD-055 测试记录](docs/testing/PRD-055-tests.md)；历史 Runtime、Browser 和最终审计保留在本机
  `artifacts/prd055/20260728T134456/`，不随源码仓库分发。Python 根级全量
  仅执行一次，真实结果为 `1364 = 1363 passed / 1 failed`；唯一失败是测试误用生产
  `5 ms` 策略超时，测试级改为 `100 ms` 后原 node `1/1 passed`，生产逻辑未改，
  也未重跑整套全量。Node `33/33`、Console `433/433`、lint `140 files` 和中文注释门禁
  均通过；运行库共 `45` 条迁移、最高 `0045`。Runtime、Browser、最终审计 SHA-256 分别为
  `8CC8F6268A11DBFD2B634A0CEE647C2E03049C280969B6D5441DCB6593014143`、
  `967DB05A0CA8ED74D561D85798A93545DCF0132CF9D2B6CAA6E4A8196E608754`、
  `55F5EAA68D07E04F396C8E5728C6597AEB89117058990A7706E5904E5C9097DE`；仅删除 `502`
  个链接节点并保留 `502` 个目标，端口、sidecar、spool、Session 和日志隐私终态均通过。

- PRD-056 已完成：Shared 规则库、Control CRUD/内部快照/Source Outbox、Gateway 请求期单次 lease/cache/Fact、PAGE-081/082 和 0046 已落地。Python 根级收集 `1472` 个唯一 node；原进程在前 433 个通过后中止，随后以无重叠切片覆盖全部余下 node，并对 4、1、52、21 个局部失败集合逐项定向闭环，未虚写为一次全绿。Node `33/33`；Console 一次全量 `451 = 450 passed / 1 failed`，旧测试切片修正后对应文件 `10/10 passed`；lint `147 files`，中文注释门禁通过。Runtime、Browser、最终审计 SHA-256 分别为 `72FC7B860E987BED60F5F441EA4465A069937DB091C5E6C45F789EBAA0EFB245`、`4CC280D0878F66DB191180D6D52F8BAA65B7C4DE727EC5B73202A9CE9D0BB380`、`1BAC341FD4FDEEA2ED4CDB32508F667D8CFB171952D0748F0D3AA458BA0B224B`。运行库 `46/0046`，Session/端口/sidecar/spool/ReparsePoint 均归零；仅删除 584 个链接节点并保留 584 个目标。设计、API 和测试见 [PRD-056 设计](docs/prd/PRD-056-provider-error-normalization-rule-library.md)、[Provider 错误归一规则 API](docs/api/provider-error-maps.md) 与 [测试记录](docs/testing/PRD-056-tests.md)；历史权威证据保留在本机 `artifacts/prd056/20260730T022600/`。该状态不代表全部 108 条 PRD 已完成。
- PRD-057 已完成：Control 新增 `GET /webhook-dead-letters`、
  `POST /webhook-deliveries/{webhook_delivery_id}/replay` 和
  `POST /webhook-dead-letters/{dead_letter_event_id}/archive`，以独立 DeadLetterEvent、
  30 天保留、持久 replay intent、lease/CAS worker 和同事务 Audit 提供 at-least-once 恢复；
  tenant/actor 只从 Session 恢复，admin/developer 可用，viewer 后端拒绝，公开 DTO 不含正文、
  URL、Secret、幂等键或 lease。专项 `24/24`；唯一一次根级 Python 全量为
  `1496 = 1407 passed / 89 failed`，没有重跑整套，原失败节点随后 `89/89` 精确定向闭环；Node
  `33/33`、Console `462/462`、lint `151 files` 和中文注释门禁通过。隔离运行库为 `48/0048`，
  `integrity_check=ok`、外键违规 0、PRD-057 trigger 9。真实 Runtime、三角色/匿名与第二租户、
  HMAC/DTO 去敏以及 1280x720/390x844 Browser 五图均通过；停服后端口、Session、spool、lease、
  pending 和应用运行库 sidecar 为 0。链接清理只在 `D:/MetaHub/.tmp` 删除 540+159 个已确认节点并
  保留全部目标；22 个既有 ACL 目录未提权绕过，结论限定为授权可见范围。权威证据见
  [Webhook 死信 API](docs/api/webhook-dead-letters.md)、
  [PRD-057 测试记录](docs/testing/PRD-057-tests.md) 和
  最终审计保留在本机 `artifacts/prd057/20260731T005000/final-audit.json`，其 SHA-256 为
  `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b`。该阶段后续 PRD-058
  也已完成；`all_108_prds_complete=false`。

- PRD-058 已完成：0049 新增稳定 AuditLog 导出序列、不可变 `audit_exports` 和可恢复
  `audit_export_events`；Control 提供 admin-only `GET /audit-logs/export`，固定 UTF-8 BOM/CRLF
  18 列、10,000 行/16 MiB 上限、公式注入防护及 tenant+actor+request_id 幂等。导出文件、
  `audit.exported` AuditLog 和持久 source 在同一事务形成复核链；PAGE-084 仅允许显式点击，
  自动刷新不触发导出，离页/注销/换身份会销毁 owner 和 Blob。首轮 Runtime 暴露的 pending-resume
  身份绑定缺口已修复，专项、相邻回归、0049 实迁移、真实 Runtime、双视口 Browser、停服静止
  终态及两轮实现后复核均通过。正式 artifact 为 `.tmp/prd058/artifacts/20260731T124500-r14`，共 58 个文件，
  manifest `56/56`，final audit 为 `status=passed`、SHA-256
  `a555fa14edefc7540de4c9af9b4f4f126e932af5b83c70ea021995ff1024fef4`。设计、公开合同和测试见
  [PRD-058 设计](docs/prd/PRD-058-audit-log-immutable-export.md) 和
  [审计导出 API](docs/api/audit-exports.md)、
  [PRD-058 测试记录](docs/testing/PRD-058-tests.md)。其后 PRD-059 也已完成；
  `all_108_prds_complete=false`。

- PRD-059 已完成：0050 新增六个固定角色、48 个租户 Permission、表驱动 RolePermission、角色
  revision/hash、不可变权限 source event 和新租户初始化；认证逐请求从当前租户数据库加载权限，
  业务路由按稳定 permission code 在后端授权。Control 提供 admin-only 的完整矩阵 GET 和 revision
  CAS PATCH，Admin 不可变且职责上限不能被客户端突破；Console PAGE-085 提供桌面矩阵和移动列表。
  迁移 `6/6`、事件 guard `1/1`、认证/权限/路由 `13/13`、Console 受影响 `116/116`、lint
  `156 files` 和中文注释门禁通过；Ruff 未安装，未虚写为通过。开发库为 `50/0050`，完整性 `ok`、
  外键违规 0。真实 Edge 验收覆盖登录、6/48 矩阵、Admin 锁定、草稿放弃、隐私、注销和
  1280x720/390x844 两张非空 PNG；停服后三端口、WAL/SHM 和 pending 权限事件均为 0。权威
  Browser 证据位于 `.tmp/prd059-edge-acceptance-20260731b`。其后 PRD-060 也已完成；
  `all_108_prds_complete=false`。

- PRD-060 已完成：0051 新增 tenant-qualified Invitation、一次性 Token 哈希、严格状态机、
  不可变 source event 和 AuditLog 扩展；0052 在不改写已应用 0051 checksum 的前提下，把接受审计
  邮箱统一为固定脱敏标记。创建接口逐请求执行后端 `user.manage`，公开接受只从 Token 恢复租户、
  邮箱和角色；两个入口均忽略客户端链路 Header，密码、Token、邮件正文不进入日志、审计、事件或
  Browser 证据。Console PAGE-087 提供邀请弹窗、私有链接/邮件文案和 fragment 公开接受页。
  迁移 `23/23`、Invitation/路径/Shared `26/26`、Console `491/491`、lint `160 files` 和中文注释
  门禁通过。开发库为 `52/0052`，完整性 `ok`、外键违规 0；三服务 Runtime 主流程/权限/重放通过，
  Edge 双视口 5 张非空截图无溢出、外部请求和 Token 留存。权威证据见
  [PRD-060 测试记录](docs/testing/PRD-060-tests.md)。其后 PRD-061 也已完成；
  `all_108_prds_complete=false`。

- PRD-061 已完成：Control 新增后端双权限 `GET /status/providers`，从 Session 恢复 tenant 并在同一
  只读快照聚合 Provider、非流式调用、流式首块和完整探测事务；返回六项调用计数、三类独立 P95、
  万分比成功率、最近五条安全故障与维护备注，统一错误和 request/trace ID 完整。0053 增加维护备注、
  四个聚合索引和 `status.viewed`；PAGE-088 复用 Provider Health 导航并提供筛选、立即探测、15 秒刷新、
  owner/generation 迟到响应隔离和 1280x720/390x844 响应式页面。运行收口修复同 request/trace、同健康
  generation 的独立探测误合并，Provider 状态专项最终 `8 passed`；迁移 `8 passed`、直接回归 `12 passed`、
  Console 直接影响 `44 passed`、验收器合同 `10 passed`、lint `164 files`、中文注释门禁通过。
  开发库为 `53/0053`，完整性 `ok`、外键违规 0；真实非流式、SSE、探测、最新源码 HTTP 和 Edge 双视口
  最终批次均通过。设计、API、测试与浏览器证据见
  [PRD-061 设计](docs/prd/PRD-061-internal-provider-status-page.md)、
  [Provider 状态 API](docs/api/provider-status.md)、
  [PRD-061 测试记录](docs/testing/PRD-061-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd061-browser/run-20260801T040500-final/browser-acceptance.json`。
  其后 PRD-062 也已完成；`all_108_prds_complete=false`。

- PRD-062 已完成：Control 已注册配置导出、预检和原子导入三个专用接口，并以后端
  `config.transfer.read/write`、Session tenant、统一错误与 request/trace ID 执行授权和隔离；0054 建立
  不可变导入/导出事实并把权限目录提升到 revision 2。`gatewayctl` 已提供严格 JSON、本地与服务端双重资源
  门禁、同 origin 网络约束、原子导出和 active Virtual Key 的 apply 前私有文件预留。导出事实不保存正文，
  重复 request ID 返回显式冲突；导入 replay 的 HTTP 报告使用本次 trace，而 Webhook 补投保留首次提交 trace。
  最终 API `6 passed`、CLI `33 passed`、Audit `13 passed`、迁移 `3 passed`、直接回归 `21 passed`；两套
  独立数据库为 `54/0054`、checksum 一致、完整性 `ok`、外键违规 0。真实 CLI import/export/dry-run/apply/
  replay、Viewer 403、跨租户 404、Gateway 失败追踪和 5 Token 成功用量均通过。权威记录见
  [PRD-062 测试记录](docs/testing/PRD-062-tests.md)。其后 PRD-063 也已完成；
  `all_108_prds_complete=false`。

- PRD-063 已完成：Shared 发布不可变九类 Adapter spec，冻结目录为 `adapter-spec-v1`、`9 specs`、
  `13633 bytes`、SHA-256 `9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870`；
  智谱、通义、文心、火山、混元和 DeepSeek 属于默认模型厂家/云服务，客户 Endpoint 与迁移入口分离，
  OpenRouter/硅基流动不进入默认厂商集合。Gateway 在联网前校验 spec/auth/version/hash，并按 spec 处理
  stream usage；Control 提供 `GET /adapter-specs` 和内部激活接口，以 Session tenant、实时
  `provider.health.read`、服务 Token、幂等、统一错误和 request/trace 执行隔离。0055 保存 tenant-scoped
  activation，PAGE-090 提供三分类九类目录。最终 Shared/Gateway `86 passed`、Control `7 passed`、0055
  `3 passed`、Provider/Health `14 passed`、Webhook `8 passed`、Console `32 passed`、浏览器验收器
  `8 passed`，中文注释门禁通过；开发库为 `55/0055`、完整性 `ok`、外键违规 0。真实 DeepSeek 非流式、
  通义流式、429、权限、租户、用量/成本/失败追踪和 Edge `1280x720/390x844` 均通过。设计、API 与证据见
  [PRD-063 设计](docs/prd/PRD-063-domestic-model-endpoint-adapter-specification.md)、
  [Provider Adapter 规范 API](docs/api/provider-adapter-specs.md)、
  [PRD-063 测试记录](docs/testing/PRD-063-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd063-browser/run-20260801T100000-final/browser-acceptance.json`。其后
  PRD-064 也已完成；`all_108_prds_complete=false`。

- PRD-064 已完成：Gateway 为九类冻结 Adapter spec 提供 exact-set mock 合并门禁、两类受控真实 probe、
  canonical 报告和 CI 非零阻断；Control 提供 tenant-scoped 内部上报、列表/详情、实时
  `provider.health.read` 与 `adapter.contract_failed` 可恢复 Outbox；0056 保存不可变 run/case/event，
  PAGE-091 提供摘要、筛选、真实 cursor 分页、详情及 403/503 安全状态投影。最终 mock `92/92`、Gateway
  `41/41`、受影响 ASGI `4/4`、Control `5/5`、PAGE-091 `15/15`、浏览器合同 `10/10`、Node `33/33`、
  lint `175 files` 和中文注释门禁均通过；开发库为 `56/0056`、完整性 `ok`、外键违规 0，三服务
  `200/200/200`。真实 Control RBAC/双租户/Outbox、Gateway probe 和 Edge `1280x720/390x844` 均通过。
  Python 根级一小时超时及 Console 首次单失败均按测试记录分层闭环，不虚写为一次全量零失败；5 个越过
  用户目录边界的 PRD-050 旧静态证据测试未运行、未计为通过。权威边界见
  [PRD-064 设计](docs/prd/PRD-064-provider-adapter-contract-test-framework.md)、
  [契约运行 API](docs/api/provider-adapter-contract-runs.md) 和
  [PRD-064 测试记录](docs/testing/PRD-064-tests.md)。其后 PRD-065 也已完成；
  `all_108_prds_complete=false`。

- PRD-065 已完成：Control 新增 tenant-scoped `RoleDashboardConfig`、五类视图、六角色实时 permission
  交集、六类有界聚合卡片和 `GET /dashboard/role-view`；0057 完成配置、新租户初始化和
  `dashboard.role_viewed` 事件扩展；PAGE-092 提供后端下发范围内的内存视图切换、迟到响应淘汰及
  401/403/503 安全状态。最终后端 `3/3`、迁移 `19/19`、受影响 Python `17/17`、PAGE-092 `14/14`、
  旧 Dashboard `6/6`、Provider Health 受影响组合 `14/14`、浏览器合同 `6/6`、lint `178 files` 和中文
  注释门禁均通过。开发库为 `57/0057`、完整性 `ok`、外键违规 0，三服务 `200/200/200`；六角色真实
  HTTP、第二租户、实时撤权/恢复、事件防伪和 Edge `1280x720/390x844` 均通过，浏览器与数据库敏感
  扫描为 0。设计、稳定接口、测试和最终浏览器证据见
  [PRD-065 设计](docs/prd/PRD-065-multi-role-home-navigation.md)、
  [角色首页 API](docs/api/role-dashboard.md)、
  [PRD-065 测试记录](docs/testing/PRD-065-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd065-browser/run-20260801T225656-CST/browser-evidence.json`。当时识别的
  下一项 PRD-066 和其后的 PRD-067 现已完成；当前按 Excel 进入
  `PRD-068 | M2-企业增强 | P2 | 策略模板库 | 行业/场景策略模板库`；`all_108_prds_complete=false`。

### PRD-089 租户数据导出与注销（已实现）

PRD-089 adds tenant-qualified `POST /tenant/export`, `GET /tenant/exports/{job_id}` and protected
ZIP download, plus `POST /tenant/deletion-request` and a cancellable lifecycle worker. Export
artifacts use the independent tenant-data AES-GCM key domain and contain only fixed safe
projections for tenant metadata, Usage/Cost totals, failure summaries and audit aggregates.
Deletion freezes the tenant, verifies the signed schema/trigger manifest, archives bounded
retention facts, removes tenant-owned rows atomically, invalidates the cache generation and
leaves a tombstone plus lifecycle event/outbox facts. Every route derives `tenant_id` from the
authenticated session, re-checks live RBAC, rejects unknown body/query fields, returns
`request_id`/`trace_id`, and applies `no-store, private` boundaries. The migration is `0082`;
fresh SQLite verification is `integrity_check=ok` with zero foreign-key violations. PAGE-121
is exposed only with `tenant_data.read`. See [PRD-089 design](docs/prd/PRD-089-tenant-data-export-deletion.md)
and [PRD-089 tests](docs/testing/PRD-089-tests.md). The legacy PRD-090..108 plan is retired. The
new authoritative roadmap starts at `PRD-090 - SaaS总体架构与兼容迁移基线`; PRD-090 through
PRD-093 are complete, and the next unfinished item is `PRD-094 - 成员关系、空间切换与邀请升级`.
`saas_incremental_roadmap_complete=false`, and the compatibility flag remains
`all_108_prds_complete=false`.

### PRD-090 SaaS总体架构与兼容迁移基线（已完成）

PRD-090 freezes three standard product forms (personal SaaS, enterprise SaaS and cloud-managed
private Gateway), with fully isolated operation treated as an asynchronous signed-package
capability rather than a fourth real-time mode. GlobalAccount and WorkspaceMembership are
separate; platform, workspace, support and device identities use disjoint trust domains;
Workspace-to-tenant binding is immutable; platform support requires customer/platform approval.
The migration plan preserves existing tenant IDs and uses expand-migrate-contract without adding
0083. Fresh SQLite verification applied 82 migrations through 0082 with `integrity_check=ok` and
zero foreign-key violations. See [PRD-090 design](docs/prd/PRD-090-saas-architecture-migration-baseline.md),
[target architecture](docs/architecture/saas-target-architecture.md),
[threat model](docs/security/saas-threat-model.md) and
[PRD-090 tests](docs/testing/PRD-090-tests.md).

The role/page baseline separates the SaaS operator console from every enterprise Workspace.
Platform IAM owns operations, security, billing, support, verification, compliance, privacy,
release, Provider engineering, channel, customer-success and growth roles. Enterprise owner/admin users may add their
own functional members, but channel management, customer management and growth analytics remain
platform-only and never appear in enterprise navigation, DOM prefetch or APIs.

PRD-090 also freezes a source-checked disposition for all current tenant Console navigation
items. Platform branding, Provider Adapter engineering/CI, plan catalog, commercial billing
decisions and platform SRE join channel/customer/growth in the SaaS operator domain. The enterprise
Gateway keeps role-scoped BYOK, models/routing, projects/keys, quotas/budgets, usage/internal finance,
members, security, logs/audit, developer tools and private deployments. PRD-138 is a release
candidate gate; public SaaS GA is PRD-148 after PRD-139..147 complete the domain migration.

### PRD-091 全局用户账号与认证凭据（已完成）

PRD-091 adds tenant-free `GlobalAccount`, Argon2id password credentials, opaque Secure/HttpOnly
browser sessions, CSRF-bound device management and atomic identity events/outbox. New
`/identity/*` routes never grant Workspace or platform permissions and reject legacy Bearer
credentials; existing `/auth/*` routes continue to authorize the enterprise Console independently.
Migration 0083 backfills one account per legacy user without merging equal emails across tenants,
isolates ambiguous identities and preserves password hashes. The standalone account-security page
is `/identity.html`; public registration was deliberately left to PRD-092 and is described below. See
[PRD-091 design](docs/prd/PRD-091-global-account-and-credentials.md),
[Global Identity API](docs/api/global-identity.md) and
[PRD-091 tests](docs/testing/PRD-091-tests.md).

### PRD-092 个人与企业用户自助注册验证（已完成）

PRD-092 adds tenant-free registration, email verification, generation-based resend and password
recovery to Global Identity. Public requests use persistent multi-dimensional abuse buckets before
Argon2id, strict Origin/Fetch Metadata/JSON boundaries and a controlled delivery adapter. Six-digit
codes use an independent HMAC root; unverified normalized email is stored only as an HMAC plus an
owner-bound AES-256-GCM envelope under a separate PII key domain. Verification creates or restores
only a GlobalAccount and GlobalSession. Invitation continuation writes a
`verified_pending_membership` binding and does not create a Workspace, Membership, tenant User or
platform role. Migration `0084` supports pristine down and atomically blocks rollback after any
security fact. See [PRD-092 design](docs/prd/PRD-092-self-service-registration-and-verification.md),
[Global Identity API](docs/api/global-identity.md) and
[PRD-092 tests](docs/testing/PRD-092-tests.md).

### PRD-093 个人空间与企业Workspace创建（已完成）

PRD-093 adds personal and organization Workspace creation for verified GlobalAccounts. Migration
`0085` establishes the immutable one-to-one Workspace-Tenant binding, minimal active owner
Membership, Region/profile/provisioning/environment-origin facts, durable events/outbox and a
one-shot proof for linking the legacy admin User to the creating account. Existing tenants are
backfilled deterministically; pristine rollback is supported and any new Workspace fact blocks
schema down atomically.

`POST/GET/PATCH /workspaces` enforce strict JSON, GlobalSession, Origin/CSRF and CAS boundaries.
Creation binds a hashed idempotency key to the canonical body and atomically creates Tenant, RBAC,
legacy User, owner, default dev Environment and Workspace facts without fake Provider, Key, Model,
Project, Usage or cost data. The Identity page can create and list safe Workspace summaries while
keeping drafts and idempotency material in memory only. This PRD deliberately does not issue a
WorkspaceSession or expose general Membership management; those remain in PRD-094. See
[PRD-093 design](docs/prd/PRD-093-workspace-creation.md),
[Global Identity API](docs/api/global-identity.md) and
[PRD-093 tests](docs/testing/PRD-093-tests.md).

### PRD-094 成员关系、空间切换与邀请升级（已完成）

PRD-094 adds database-authoritative Membership, independent WorkspaceSession cookies, Workspace
switching, role/status management, MembershipInvitation upgrade, atomic Owner transfer and
Workspace-aware Console navigation. Migration `0086` backfills all legacy account links without
replacing the immutable Workspace creator, preserves the PRD-060 N/N-1 invitation path, and blocks
schema down after any new authorization fact.

The seven Workspace/Membership routes use strict Global or Workspace credential interpretation,
Origin/CSRF, revision CAS, delegation ceilings and same-shape cross-Workspace failures. Every
Workspace request re-checks the account, Workspace, binding, Membership and legacy permission
revisions; a committed role/status change, Owner transfer or GlobalSession revocation invalidates
the next request. The Identity page provides a compact searchable Workspace selector, member and
invitation management, and Owner transfer without putting tokens or identity internals in DOM or
browser Storage. BroadcastChannel carries only an anonymous invalidation generation, and stale
requests are aborted and fenced. See [PRD-094 design](docs/prd/PRD-094-membership-and-workspace-switching.md),
[Workspace Access API](docs/api/workspace-access.md) and
[PRD-094 tests](docs/testing/PRD-094-tests.md). Automated gates and real-browser login, keyboard,
390/1280/1440 viewport, access-log privacy and interaction performance evidence are complete. The
next delivery is recorded in the PRD-095 section below.

### PRD-095 平台运营角色与受控支持访问（已完成）

PRD-095 adds a Platform IAM domain that does not reuse Workspace Membership or legacy tenant RBAC.
Migration `0087` adds encrypted TOTP factors, one-time owner bootstrap, 13 fixed platform roles,
database-authoritative PlatformSession, dual-domain SupportGrant, short SupportSession,
break-glass review, immutable audit and outbox facts. Platform, Support, Workspace and Global
credentials remain separate, and every privileged request revalidates the underlying revisions.

The standalone `/platform.html` workbench provides backend-projected navigation, Operator
management, controlled support, diagnostics, break-glass and role-scoped audit. Identity security
adds TOTP enrollment and only shows the platform entry after the backend returns an active Operator
projection. Secrets and one-time codes stay in the current input lifecycle and never enter DOM
snapshots or browser Storage. See [PRD-095 design](docs/prd/PRD-095-platform-iam-controlled-support.md),
[Platform Access API](docs/api/platform-access.md) and
[PRD-095 tests](docs/testing/PRD-095-tests.md). Migration, concurrency, warm performance, signed
cursor, security, full Console and real-browser 390/1280/1440 gates are complete; the authoritative
next delivery is recorded in the PRD-096 section below.

### PRD-096 平台租户目录与生命周期运营台（已完成）

PRD-096 adds migration `0088` and a role-trimmed Platform Workspace directory without granting any
Workspace Membership. The directory exposes only operational, Region, lifecycle, bounded count,
health, plan/deployment placeholder and contact-status projections. Exact ID/slug search and HMAC
keyset cursors are bound to the PlatformSession, Operator revision, role set and normalized filters.

Platform ops can suspend or restore a Workspace through revision CAS, CSRF, explicit confirmation
and idempotency. Security and customer-success roles have disjoint fixed risk-tag allowlists, while
Owner notifications persist only controlled account references and template payloads. Suspend
revokes active WorkspaceSession facts and keeps N-1 code fail-closed; restore cannot revive deleted
or conflicting data. The workbench now provides a dense directory, filters, full-width detail,
health and event timelines, lifecycle confirmation, risk operations and notification state with
generation-fenced reloads. See [PRD-096 design](docs/prd/PRD-096-platform-workspace-directory-operations.md),
[Platform Access API](docs/api/platform-access.md) and [PRD-096 tests](docs/testing/PRD-096-tests.md).
Migration, role projection, cursor, health corruption, concurrency, Console and compatibility gates
are complete; the authoritative next delivery is recorded in the PRD-097 section below.

### PRD-097 个人与企业初始化向导重构（已完成）

PRD-097 adds migration `0089` and replaces active-row onboarding guesses with an eight-step,
backend-derived Workspace projection. System-created environments, legacy seed resources,
placeholder credentials and orphan success requests do not count as completion. Activation requires
user-confirmed profile/environment facts plus a verified Provider, Model, Alias, Project,
Application, Virtual Key and real Gateway success with Usage/ledger evidence.

The Identity Workspace page now provides a compact initialization center with progress, selectable
steps, safe diagnostics, role-trimmed actions and configuration links. Mutations use WorkspaceSession,
Origin, workspace CSRF, revision CAS and idempotency; developer access cannot mutate organization
profile state. Evidence loss returns an activated Workspace to incomplete while preserving immutable
history. See [PRD-097 design](docs/prd/PRD-097-workspace-onboarding-rebuild.md),
[Workspace Onboarding API](docs/api/workspace-onboarding.md) and
[PRD-097 tests](docs/testing/PRD-097-tests.md). Migration, strict transport, resource-chain,
compatibility, Console and real-browser 390/1280/1440 gates are complete; the authoritative next item
is PRD-098.

## 目录结构

```text
gateway-data-plane/   数据面热路径：API Listener、鉴权、限流、预算、路由、Provider Adapter、Usage
control-plane-api/    控制面低频管理 API：租户、用户、Provider、模型、Virtual Key、账单、Webhook、审计
console-web/          企业管理员和开发者控制台
shared-sdk/           OpenAPI、JSON Schema、Python/TypeScript SDK 与共享契约
examples/python/      基于官方 openai 客户端的 Chat、Embeddings、超时、重试与错误处理示例
examples/js/          基于官方 openai Node 客户端的非流式/流式 Chat、安全错误与浏览器 Key 防护示例
scripts/              本地开发、CI、数据库、发布、预检脚本
infra/                Docker、Compose、迁移、种子数据等基础设施文件
docs/                 PRD 设计、架构、API、ADR、开发、Runbook、安全、测试文档
```

更详细说明见 [目录边界说明](docs/development/directory-map.md)；代码注释要求见 [代码中文注释规范](docs/development/code-commenting-standard.md)。

## 本地启动

准备 Python 3.12 和 Node.js 20+。Console 不依赖第三方 npm 包；Python 运行依赖使用容器和本地共享的约束文件。首次在 Windows PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\infra\docker\requirements.runtime.txt

if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

启动前先初始化 SQLite 和本地管理员。密码和 Token Secret 只注入当前运行环境，不写入日志：

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
$env:METAHUB_SEED_ADMIN_PASSWORD="change-me-locally"
$env:METAHUB_AUTH_TOKEN_SECRET="change-me-auth-token-secret"
.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1

# 可选：指定 Python 路径
$env:METAHUB_PYTHON=(Resolve-Path .\.venv\Scripts\python.exe).Path

# 一键启动三个真实服务
.\scripts\dev\start-all.ps1
```

四个进程分别承担模型调用数据面、管理控制面、公共状态读服务和 Web Console，不是演示模式。一键启动后访问：

- Gateway Health: <http://localhost:8000/health>
- Control API Health: <http://localhost:8001/health>
- Status API: <http://localhost:8002/public/status>
- Console Web: <http://localhost:5173>

本地开发可使用 `http://localhost:5173` 或 `http://127.0.0.1:5173` 打开 Console。若未显式设置
`METAHUB_ALLOWED_ORIGINS`，`start-control-api.ps1` 仅为这两个 loopback 来源提供默认 CORS
白名单；部署环境必须显式配置实际 Console 来源。

同一局域网内的受限技术试用可用专用启动器指定一个精确的 Console 来源：

```powershell
.\scripts\dev\start-lan-trial.ps1 -ConsoleOrigin "http://172.16.100.2:5173"
```

该命令只向 Control API 的 CORS 白名单加入该一个来源，并保留本机两个 loopback 来源。客户设备必须在同一可达网络，且应先由另一台设备验证 Console 登录和 Gateway 调用；`172.16.x.x` 等私网地址不是公网交付地址，也不构成付费服务或 GA 发布。

若 Windows 网络配置为 Public，需由管理员在同一台服务器上为当前网段创建受限入站规则：

```powershell
.\scripts\dev\enable-lan-trial-firewall.ps1 -RemoteCidr "172.16.100.0/24"
```

规则只允许该网段访问 TCP `5173`（Console）、`8001`（Control）、`8000`（Gateway）和 `8002`（状态页）。客户必须在另一台同网段设备上通过 `http://172.16.100.2:5173/` 登录，并以 `http://172.16.100.2:8000/v1` 加个人 MetaHub KEY 完成一次调用后，才可对外宣称该受限试用可用。

停止时执行：

```powershell
.\scripts\dev\stop-all.ps1
```

停止脚本只会停止本次 `start-all.ps1` 记录的启动器及其 Python/Node 子进程，不会扫描或终止机器上的其他 Python/Node 应用。

也可以分别启动：

```powershell
.\scripts\dev\start-gateway.ps1
.\scripts\dev\start-control-api.ps1
.\scripts\dev\start-console.ps1
```

本地三服务启动器默认关闭只用于后台恢复的可选 Control scheduler，以降低空数据库时的轮询和内存开销；真实 API、SQLite、Session、后端 RBAC 和当前页面数据读取全部保持开启。直接启动或生产配置的 scheduler 默认为开启；需在本地验收恢复任务时，可在启动前显式设置 `$env:METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED="true"`。

## 单机 Docker Compose 部署

PRD-028 后，研发和试点客户可以使用 Compose 在单台服务器启动 MVP：

```powershell
.\scripts\deploy\install.ps1
```

默认 profile 为 `local-sqlite`，使用显式 SQLite volume 保存本地数据。PRD-100 后，`compose-full` 还会启动
PostgreSQL 16、无持久化 Redis、MinIO、bucket 初始化和独立 Backup Worker；生产适配器使用有界 psycopg
连接池、S3-compatible 对象存储和 KMS 信封加密，基础设施故障不会回退到 SQLite。原生迁移提供
0001..0139 的可复现 PostgreSQL baseline 和后续独立 native delta，校验源码摘要、对象与迁移历史；
未完成的 bootstrap 失败关闭。Compose 默认仍为 SQLite，原生迁移通过不等于全产品 PostgreSQL
业务验收或生产发布已放行。基线不支持原地 down，恢复必须使用隔离数据库和已复核备份。

Control 的宿主 8001 端口由独立 Nginx `control-ingress:prd147-v1` 提供，后端 `control-api:8001`
不再发布宿主端口。旧 Console 路径和 API 文档在入口返回普通 no-store 404；`/branding/public` 与
新分域 API 保持可用。应用 N/N-1 切换及回滚不得降低入口退休规则版本，也不得绕过入口发布后端端口。

常用命令：

```powershell
.\scripts\deploy\install.ps1 -DryRun
.\scripts\deploy\start.ps1
.\scripts\deploy\status.ps1
.\scripts\deploy\smoke.ps1
.\scripts\deploy\stop.ps1
```

`infra/compose/.env.compose` 由 install 脚本生成并已加入 `.gitignore`。首次生成时 CLI 只在 stdout 返回一次默认管理员密码；部署事件和日志不会记录管理员密码、Provider Key、Virtual Key、Token Secret、数据库密码或 MinIO Secret。详细说明见 [单机 Docker Compose 部署](docs/deployment/single-node-compose.md)。
## 测试与检查

```powershell
.\scripts\ci\check.ps1
```

产品级验收按 WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW 一次成组执行：

```powershell
.\scripts\quality\run-product-qa.ps1 -Group All -RunBrowserMatrix `
  -RunLivePerformance -RunPressure -PressureRequests 120 -PressureConcurrency 8 `
  -PressureDurationSeconds 0
```

当前最终批次为 `15/15` 任务和 `97/97` 计划项通过；三浏览器三缩放的 36 个数据页切换样本为
`484-1675 ms`，详见 [产品级 QA 最终报告](docs/testing/product-qa/REPORT-20260820.md)。

该命令会运行：

- 后端 pytest：覆盖 `/health`、`/version`、统一错误码、request_id、配置异常、敏感日志脱敏、Project/Application 归属、日志/用量过滤和预算 scope。
- 配置中心测试：覆盖 `.env/配置文件/环境变量` 优先级、Provider Base URL 读取、敏感配置脱敏、prod 内部 token 权限。
- 可观测测试：覆盖 trace_id 透传、`X-Trace-ID`、`/internal/health`、未捕获异常归一、并发请求 trace 不串。
- 健康检查测试：覆盖 `/health/live` 不依赖 DB、`/health/ready` DB 成功/失败、version 构建信息、敏感信息不泄露。
- OpenAI-compatible 测试：覆盖 chat JSON、chat SSE、embeddings、Virtual Key 鉴权、参数校验、上游错误、request_logs 和 usage_ledger。
- Virtual Key 测试：覆盖创建、一次性明文展示、哈希存储、禁用、禁用后鉴权失败、`last_used_at`、viewer 权限拒绝。
- Provider 测试：覆盖创建至少 2 个 Provider、连接测试成功/失败、禁用、developer/viewer 权限拒绝、base_url/type/auth_type 校验、重复 code 和前端 admin-only 面板；Provider 创建/更新运行日志不记录 URL 或 hash，持久审计只保存完整规范 URL 的 SHA-256 变化摘要。
- 模型目录测试：覆盖新增/编辑/禁用模型、developer 只读、viewer 拒绝、内部模型 ID 到 `provider_model_id` 映射、价格字段和前端权限视图。
- Provider Adapter 测试：覆盖 Adapter 请求构造、OpenAI-compatible 和通用 HTTP 工厂选择、usage 归一化、上游错误归一化和日志不泄漏 Provider Key。
- Provider KeyPool 测试：覆盖 Key 加密落库、列表不回显明文/密文、禁用 Key 不参与路由、数据库 Key 优先选择、上游 429 后临时摘除、developer/viewer 权限拒绝和前端 KeyPool 面板；环境 Key 只允许非生产 active 默认租户中 code 和数据库/冻结 Route Endpoint 均匹配运维基准的 active Provider 使用，生产或其他租户不得复用进程级 Secret。
- 模型别名测试：覆盖 `ModelAlias` 创建/读取/禁用、developer 只读、viewer 拒绝、别名路由到默认模型、默认模型禁用后 fallback、禁用别名/无可用模型的可解释错误和 `routing_decisions` 落库。
- 成本/可用性路由自动化：覆盖 shared 两阶段确定性排序、严格 ModelAlias wire/CAS/幂等/RBAC/租户、Gateway Health/KeyPool/价格最小依赖矩阵、Decision/Candidate 原子事实、Dry-Run parity、调用日志强类型候选、CNY Budget/极端价格、主备未结算分项、Provider 环境 Key 所有权/Endpoint 绑定、Provider URL 日志/审计安全和 Browser owner-safe cleanup。PRD-047 最终通过 Python `574`、Node 示例 `33`、Console `228`、Console lint `106 files`、0036 运行库、真实三服务/Webhook/Usage/隐私清理和三档 Chrome 验收，证据见 [PRD-047 测试记录](docs/testing/PRD-047-tests.md)。
- 调用反馈与采纳率测试：覆盖 Control Session 与 Gateway Virtual Key 双入口、租户/RBAC/防枚举、严格输入与幂等冲突、不可变 Feedback/Event/Aggregate、RequestLog/ModelQuality 投影、`feedback.created`、0038/0039 正反迁移和冲突守卫、PAGE-070/071、Runtime/Browser/隐私/清理。最终证据为 Python `783`、Console `287/287`、39 条迁移/最高 0039 和 15 张浏览器 PNG，详见 [PRD-049 测试记录](docs/testing/PRD-049-tests.md)。
- 调用日志测试：覆盖 `/request-logs` 列表/详情、request_id/status/model/provider/error_code 过滤、分页、viewer 后端拒绝、失败请求落库、token/成本回写和敏感字段不回显。
- 用量与成本测试：覆盖 `/usage/summary`、`/usage/records`、同步/异步 CSV 导出、固定 42 列、UTF-8 BOM/CRLF、Decimal 金额、公式防护、只增快照水位、lease/重试/过期、admin-only 后端权限、跨租户隔离、0032 正反迁移、真实 usage 与估算 usage 区分、价格快照和非法筛选/大数据边界。
- 预算告警测试：覆盖 `/budgets`、`/budget-status`、租户级/Virtual Key 级日预算、80% 告警、100% 阻断或仅告警、重复告警去重、预算命中日志、viewer/developer 权限和用量概览摘要。
- 基础 DLP 测试：覆盖 `/dlp/rules` admin-only 管理、正则安全校验、`block/log_only/allow` 数据面行为、只扫描 user messages、调用日志 DLP 摘要、日志详情哈希展示和敏感明文不泄露。
- 策略引擎测试：覆盖六个白名单变量、五类动作、DSL 注入阻断、优先级与可解释求值、超时 fail-closed、admin-only 后端权限、跨租户与 route_to 目标隔离、If-Match 并发版本、事务/事件/Webhook 故障、0033 正反迁移和控制台异步状态安全。
- 策略快照测试：专项覆盖双 CAS 发布、单调新版本回滚、租户/RBAC、内部 Token/ETag、不可变制品双 hash、按租户 singleflight/304、LKG/emergency LKG/硬过期、请求 claim、`deny/route_to`、复合 request_id、调用日志版本筛选和 0034 正反迁移；PRD-045 全量回归、运行库、真实 API 和浏览器验收状态以 [PRD-045 测试记录](docs/testing/PRD-045-tests.md) 为准。
- 策略预演测试：专项覆盖 admin-only 后端授权、tenant-qualified 上下文、严格有界 JSON、Policy/路由/DLP/预算只读短路、稳定 request_id 三事实、Webhook、安全摘要、零 Provider/Usage 成本、0035 往返迁移、结果未知补偿和 Console Prompt 生命周期；完整自动化、真实 API、运行库和浏览器证据以 [PRD-046 测试记录](docs/testing/PRD-046-tests.md) 为准。
- Webhook 测试：覆盖 Endpoint 注册、最近 100 条记录、事件/状态筛选、一次性 `signing_secret`、新旧 HMAC 验签、3 次失败重试、串行/并发重放幂等、不同键再次重放、Secret 轮换、跨租户 404、viewer/匿名拒绝、递归敏感信封拒绝、0031 正反迁移和前端响应式交互。
- Dashboard 测试：覆盖 `/dashboard/overview` 当前租户今日请求、成功率、错误率、P95、成本、Top 模型、Top Virtual Key、最近错误、空状态、viewer 只读访问、未登录统一错误和 `dashboard.viewed` 事件。
- i18n 基础框架测试：覆盖 `/user/preferences` 默认中文、切换英文、`user.locale_changed` 事件、非法 locale、夹带 `tenant_id` 拒绝、未登录统一错误、`GET /error-codes?locale=` 本地化 label、语言设置页渲染和前端 fallback。
- 控制台骨架测试：覆盖 `/console/navigation`、`/console/preferences`、`/console/events`、角色化导航、非法入口、偏好恢复、敏感事件 payload 拒绝和 `tenant_id` 不由前端传入。
- 开发者快速开始测试：覆盖 `/developer/quickstart-data`、Python/JavaScript/curl 示例、Key 环境变量占位、模型别名推荐、viewer 后端拒绝和 `developer.quickstart_viewed` 事件。
- Python SDK 示例测试：覆盖 Chat、Embeddings、环境变量、超时/重试、request_id、安全 ZIP、租户/RBAC、下载事件和控制台交互。
- JavaScript/Node 示例测试：覆盖真实官方客户端非流式/流式调用、SSE 总时限、安全错误、浏览器 Key 防护、双语言下载页、租户/RBAC 和 `sdk.example_downloaded` 事件。
- 直连迁移指南测试：覆盖可信 Gateway URL、Environment 与模型别名选择、Python/Node 前后代码、错误差异、治理安全回滚、租户/RBAC、访问事件去重和直连到 MetaHub 的真实调用闭环。
- 竞品定位测试：覆盖中立文案、三角色帮助页、官方来源、Provider 分类防误用、`help.competitor_viewed` 租户事件、0030 往返迁移、权限过滤和响应式 tabs。
- 内置 API 调试台测试：覆盖 `/debug/chat-completions` 成功调试、viewer 后端拒绝、非法参数/敏感字段拒绝、上游失败归因、`request_logs`/`usage_ledger`/`debug_console_events` 写入和前端响应渲染。
- 智能错误提示测试：覆盖 `ErrorDiagnosisRule` 静态规则、`GET /error-codes`、统一错误响应 `diagnosis`、Provider 401/429/402/404/timeout/参数错误归一、调用日志详情诊断、调试台/日志详情诊断卡片 HTML 转义和敏感信息不泄露。
- Virtual Key 限流测试：覆盖 `PATCH /virtual-keys/{id}/limits`、RPM/TPM/并发超限、不同 Key 隔离、Redis 不可用 fail closed、失败 request_logs、前端限制表单和 HTML 转义。
- 管理审计测试：覆盖 Provider/两类 Key/预算/DLP 管理变更、同事务回滚、admin-only 查询、跨租户隔离、不可变记录、动作和 JSON 约束、IP/User-Agent 清洗、摘要脱敏、最小 Webhook 事件和控制台响应式交互。
- Provider 健康测试：覆盖 0025 迁移、手工/周期领取、失败转红与恢复转绿、逐 Key 认证状态、固定错误分类、租户/RBAC、探测日志、Webhook 状态转换、数据面预请求避障、过期 Fail Open、Dashboard 摘要和响应式健康页。
- 非流式 Fallback 测试：覆盖 0026 正向/回滚迁移、策略幂等版本、admin 写/developer 读/viewer 拒绝、主模型 5xx 后备用成功、流式禁止切换、最多两次 attempt、主备预算总和、最终模型用量关联、失败用量 unavailable、事件脱敏和响应式策略编辑器；PRD-047 已锁定原始 `reserved_cost_cny` 不改写，且主失败备成功仅把主估算保留在 `outstanding_cost_cny`。
- SSE 流式代理测试：覆盖 0027 正向/回滚迁移、StreamSession 租户与状态约束、正常多块透传、首块/块间超时、ASGI 客户端断开、上游读异常、幂等关闭、部分 Usage、`stream.timeout` 脱敏事件、调试台增量预览和主动停止。
- 控制台 Node test：覆盖服务状态读取、控制台主框架渲染、导航 active 状态、HTML 转义和后端不可用时的页面降级。
- 控制台轻量 lint：检查基础格式。
- 部署包测试：覆盖 Compose 文件结构、DeploymentProfile、env 占位符、dry-run、volume 删除保护、env secret 生成脱敏和 Dockerfile 入口。

## 数据库迁移与种子数据

当前本地可运行实现使用标准库 SQLite，便于在没有 PostgreSQL 驱动的机器上验证迁移框架、表约束、seed 幂等和敏感值不落库。生产 PostgreSQL 接入会沿用同一 CLI 边界。

本地示例：

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
$env:METAHUB_SEED_ADMIN_PASSWORD="change-me-locally"
$env:METAHUB_SEED_PROVIDER_API_KEY="replace-with-provider-key"
$env:METAHUB_SEED_VIRTUAL_KEY="<set-local-test-virtual-key>"

.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```

已创建的 MVP 表包括：

- `tenants`
- `users`
- `virtual_keys`
- `providers`
- `provider_credentials`
- `models`
- `model_quality_scores`（0037，PRD-048）
- `model_quality_score_events`（0037，PRD-048）
- `request_feedbacks`（0038，PRD-049）
- `request_feedback_events`（0038，PRD-049）
- `model_feedback_statistics`（0038，PRD-049）
- `model_aliases`
- `model_alias_routing_policies`（0036 创建；0037 扩展质量配置）
- `routing_decisions`
- `routing_candidates`（0036 创建；0037 扩展冻结质量事实）
- `routing_policy_events`（0036 创建；0037 增加 v2 配置事件）
- `request_logs`
- `usage_ledger`
- `price_snapshots`
- `finops_ledger_entries`
- `finops_adjustments`
- `budget_rules`
- `budget_alert_events`
- `budget_evaluations`
- `budget_reservations`
- `budget_blocks`
- `budget_block_events`
- `budget_scope_events`
- `dlp_rules`
- `dlp_matches`
- `webhook_endpoints`
- `webhook_deliveries`
- `webhook_delivery_attempts`
- `provider_health_probe_logs`
- `stream_sessions`
- `user_preferences`
- `user_preference_events`
- `console_page_events`
- `developer_portal_events`
- `debug_console_events`
- `policies`
- `policy_rules`
- `policy_actions`
- `policy_events`
- `policy_explanations`
- `policy_snapshots`
- `policy_snapshot_items`
- `policy_snapshot_heads`
- `policy_snapshot_events`
- `gateway_request_claims`
- `provider_adapter_catalog_activations`（0055，PRD-063）
- `adapter_contract_runs`（0056，PRD-064）
- `adapter_contract_cases`（0056，PRD-064）
- `adapter_contract_event_outbox`（0056，PRD-064）
- `sessions`
- `roles`
- `schema_migrations`
- `seed_runs`

0039 不新增业务表；它以六个 `BEFORE INSERT/UPDATE` 冲突守卫补齐 Feedback、Event、已反馈 RequestLog 与最终 RoutingAttempt 的 `INSERT/UPDATE OR REPLACE` 防改写边界，并保留 0038 的回滚事实门禁。

Provider Key、Virtual Key、管理员密码不会明文落库；seed CLI 只输出可安全展示的 prefix/fingerprint 信息。

## 管理员登录与 Session

本地登录依赖数据库迁移和 seed 管理员。首次使用控制台前先运行：

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
$env:METAHUB_SEED_ADMIN_PASSWORD="change-me-locally"
$env:METAHUB_AUTH_TOKEN_SECRET="change-me-auth-token-secret"

.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```

默认 seed 管理员：

- 租户：`default`
- 邮箱：`admin@metahub.local`
- 密码：来自 `METAHUB_SEED_ADMIN_PASSWORD`

首次登录后从左侧“总览”进入初始化向导，按业务依赖顺序完成：企业资料、Environment、Provider 和 Provider Key、模型、模型别名、Project/Application、Virtual Key，最后执行首次调用。Provider Key 由企业管理员在“配置 > Provider”中录入，只在提交请求中出现，界面、日志和验收证据不会回显明文。在配置可用的真实 Provider Endpoint/Key 前，控制台的治理功能可正常使用，但 Gateway 不会伪造上游模型响应。

认证接口：

- `POST /auth/login`：使用本地账号登录，返回 Bearer Token 和 Session。
- `GET /auth/me`：校验 `Authorization: Bearer <token>` 并返回当前用户、租户和 Session。
- `POST /auth/logout`：撤销当前 Session。

安全约束：

- 数据库只保存 `token_hash`，不保存明文 Bearer Token。
- 错误密码会累计失败次数，达到阈值后临时锁定账号。
- 缺失、伪造、过期、已登出的 Token 都返回统一错误结构和 `request_id`/`trace_id`。
- 后续管理 API 应复用后端 `require_current_admin`，不能只靠控制台隐藏按钮。

## 当前租户模型

M0 使用 seed 默认租户 `default`，并提供当前租户接口。租户接口只使用登录 Token 恢复出的 `tenant_id`，不接受前端传入其他租户 ID 来选择目标。

租户接口：

- `GET /tenant/current`：读取当前登录用户所属租户。
- `PATCH /tenant/current`：管理员更新当前租户的 `name` 和 `service_mode`。

安全约束：

- 普通接口没有跨租户查询、切换或更新入口。
- 请求体中的 `tenant_id`、`slug`、`status` 不允许作为更新字段。
- 非 admin 角色更新租户时，后端返回 `auth.permission_denied`。
- 所有 MVP 业务表必须包含 `tenant_id`，`schema_migrations` 作为基础设施表例外。

## 基础 RBAC 角色

M0 固定支持三种角色：`admin`、`developer`、`viewer`。角色目录存放在当前租户的 `roles` 表中，控制面用户表继续使用 `users.role` 保存当前角色。

用户管理接口：

- `GET /users`：管理员读取当前租户用户列表和角色选项。
- `POST /users`：管理员创建当前租户本地用户，初始密码只保存哈希。
- `PATCH /users/{id}/role`：管理员修改当前租户用户角色。

安全约束：

- `/users` 接口全部使用后端 `require_current_admin`，developer/viewer 直接调用会返回 `auth.permission_denied`。
- 用户创建和角色修改只能作用于当前认证上下文里的 `tenant_id`，请求体不能指定其他租户。
- API 响应不返回 `password_hash`、明文初始密码、Session 或 Token。
- Developer 不能新增 Provider；Viewer 不能创建 Virtual Key。后续业务接口必须复用后端角色依赖，而不是只靠控制台隐藏入口。
- 最后一个 active admin 不能被降级，admin 也不能把自己降级为非 admin。

## Virtual Key 管理

M0 支持 admin/developer 在控制面创建和管理当前租户 Virtual Key。业务系统使用 Virtual Key 调用 Gateway，避免直接暴露模型厂家 Provider Key。

Virtual Key 接口：

- `GET /virtual-keys`：列出当前租户 Key，支持 `status`、`environment` 过滤。
- `POST /virtual-keys`：创建 Key，响应只在本次返回 `plain_value`。
- `PATCH /virtual-keys/{id}`：更新名称、环境、备注或状态。
- `DELETE /virtual-keys/{id}`：软禁用 Key，不物理删除历史对象。

安全约束：

- 创建响应以外不再返回明文 Key。
- 数据库只保存 `key_hash`、`key_prefix` 和元数据。
- `key_hash` 不返回给前端；`key_prefix` 只用于排障展示。
- 禁用后的 Key 调用数据面返回 `gateway.auth_invalid`。
- 数据面成功鉴权后更新 `last_used_at`，便于管理员清理长期未使用 Key。
- viewer 后端直接调用 `/virtual-keys` 会返回 `auth.permission_denied`。

## Virtual Key 级基础限流

PRD-022 后，管理员和开发者可以为当前租户的每把 Virtual Key 设置 RPM、TPM 和并发限制。规则保存在 `virtual_key_rate_limit_rules`，每条业务数据带 `tenant_id`；运行时计数器使用 Redis Key，Key 名只包含 `tenant_id` 和 `virtual_key_id`，不包含明文 Virtual Key、Prompt、Response 或 Provider Key。

限流接口：

- `PATCH /virtual-keys/{id}/limits`：更新该 Key 的 RPM、TPM 和并发限制，`null` 表示清除对应限制。

限流行为：

- 未配置规则或规则未启用时，请求按原数据面链路继续执行。
- 达到 RPM、TPM 或并发限制时，数据面返回 429 `gateway.rate_limit_exceeded`，不会继续调用上游 Provider。
- 启用限流规则但 Redis 计数不可用时，数据面 fail closed，返回 503 `gateway.rate_limit_unavailable`。
- 超限和 Redis 不可用请求都会尽量写入 `request_logs`，并记录安全结构化事件 `rate_limit.exceeded` 或 `rate_limit.unavailable`。
- TPM 使用请求体长度做准入估算；真实 Token 和成本仍由 `usage_ledger` 在上游响应后记录。

## 租户、项目与 Key 日预算

PRD-023 建立租户和 Virtual Key 日预算，PRD-052 在不删除既有 Application 兼容作用域的前提下，将正式能力扩展为 `tenant/project/virtual_key` 三级累计 AND 门禁。预算按 Asia/Shanghai 业务日计算；已入账消费只读取 `finops_ledger_entries` 的 `posted usage_charge/debit`，再叠加尚未结算的 reservation，不能继续直接汇总 `usage_ledger.cost_cny`。达到 80% 生成 `budget.threshold_reached`，首次达到 100% 还会形成可去重的 `budget.scope_exceeded` 事实；规则可配置为仅告警或在 Provider 前阻断。

预算接口：

- `GET /budgets`：admin/developer 读取当前租户预算规则和今日状态，不接受前端传入 `tenant_id`。
- `POST /budgets`：admin 以 create-only 语义创建租户、Project 或 Virtual Key 规则；同一作用域重复创建稳定返回 409。
- `PATCH /budgets`：admin 更新或停用既有规则，并保留旧客户端兼容入口。
- `PATCH /budgets/{id}/mode`：admin 切换 `block/alert_only`，并按规则状态事务化处理恢复。
- `GET /budget-status`：admin/developer 按可信 Environment/Project/Application/Virtual Key 归属链查询聚合状态。

预算行为：

- 数据面在限流之后、上游调用之前执行预算检查；阻断时返回 402 `gateway.budget_exhausted`，不会继续调用上游 Provider。
- 预算规则、告警、评估、reservation、block 和 scope event 均带 `tenant_id`；控制面只按认证 Session 查询当前租户，并在后端校验 Environment/Project/Application/Key 归属。
- 告警事件按规则、业务日和阈值去重，避免同一预算重复刷屏。
- 预算命中摘要写入 `request_logs`，日志详情可查看 `budget_evaluations`，但不记录 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key。
- 控制台提供“预算告警”页面；Token 用量概览中也展示今日预算状态摘要，便于把成本趋势和预算阈值放在一起查看。

## 基础 DLP

PRD-024 后，管理员可以为当前租户配置关键词或安全正则 DLP 规则。M0 只扫描 Chat Completions 请求中的 `role=user` 文本，不扫描 system/assistant/tool 消息、文件、图片、Embeddings input 或模型响应。

DLP 接口：
- `GET /dlp/rules`：admin 读取当前租户规则，支持按状态、类型和动作过滤。
- `POST /dlp/rules`：admin 创建关键词或正则规则，不接受前端传入 `tenant_id`。
- `PATCH /dlp/rules/{rule_id}`：admin 更新规则名称、内容、动作、状态、优先级和描述。

DLP 行为：
- 数据面在鉴权和路由之后、限流和预算之前执行 DLP；`block` 命中返回 403 `gateway.dlp_blocked`，不调用上游 Provider。
- `log_only` 和 `allow` 命中后继续调用上游，并写入 `dlp_matches` 与 `request_logs` DLP 摘要。
- `dlp_matches` 只保存规则 ID、动作、消息索引、命中文本哈希和长度，不保存 Prompt、messages 原文、命中文本、Authorization、Virtual Key 明文或 Provider Key。
- 控制台提供“DLP 规则”页面；调用日志详情显示 DLP 决策和命中哈希，便于排障但不泄露敏感输入。
## Webhook 基础事件订阅

PRD-025 后，管理员和开发者可以为当前租户注册企业系统 Webhook Endpoint，订阅预算、DLP、调用失败等治理事件类型，并通过测试事件验证接收端签名和可达性。M0 完成 Endpoint 注册、事件类型订阅、HMAC-SHA256 签名、最多 3 次重试和发送记录；生产治理事件的具体触发点会在后续相关 PRD 按同一事件信封接入。

Webhook 接口：

- `GET /webhooks`：admin/developer 读取当前租户 Endpoint 和最近发送记录，不接受前端传入 `tenant_id`。
- `POST /webhooks`：admin/developer 创建 Endpoint，响应只返回一次 `signing_secret`。
- `POST /webhooks/{id}/test`：向当前租户指定 Endpoint 发送 `webhook.test` 事件，并返回 delivery 与 attempts 摘要。
- `GET /webhooks/{id}/deliveries`：按事件类型和状态读取指定 Endpoint 最近最多 100 条记录；响应不包含事件正文或重放幂等键。
- `POST /webhooks/{id}/replay`：仅允许重放 failed delivery；请求体只提交来源 delivery ID 和 8-128 位幂等键。
- `POST /webhooks/{id}/rotate-secret`：原子轮换当前 Endpoint Secret，新明文只在本次响应中出现一次。

Webhook 行为：

- `webhook_endpoints`、`webhook_deliveries` 和 `webhook_delivery_attempts` 均带 `tenant_id`。
- 数据库只保存签名密钥密文、前缀和指纹；列表、发送记录和日志不返回 `signing_secret` 明文。
- 投递 payload 固定包含 `event_id`、`event_type`、`timestamp`、`tenant_id`、`trace_id` 和安全 `payload`，不包含 Prompt、Response、Authorization、Virtual Key 明文、Provider Key 或 DLP 命中文本。
- HTTP 发送使用 `X-MetaHub-Signature: t=<unix_seconds>,v1=<hmac>`，签名输入为 `timestamp.body`，接收端可用 shared-sdk 的验签函数复用校验逻辑。
- 测试发送失败不会抹掉记录；系统保存最终 delivery 状态和每次 attempt 的 HTTP 状态、耗时、错误码与安全摘要。
- original delivery 按原事件 ID 幂等；replay delivery 按当前租户、Endpoint、来源 delivery 和幂等键唯一。同键重复调用回读同一记录且不再次出网，不同键允许新的人工重放。
- replay intent 与管理审计先在短事务中提交，再执行有界 HTTP 发送；Endpoint 必须保持 active，不能通过重放绕过停用状态。
- 每条 delivery 保存创建时的 `secret_version`；轮换事务同时写新密文、版本、操作者和 `webhook_endpoint.secret_rotated` 审计，事务失败时旧 Secret 继续有效。
- `webhook.replayed` 是安全内部事件，不进入 Endpoint 可订阅目录；审计和日志只保存 ID、事件类型、状态和版本，不保存 URL、正文、签名、Secret 或幂等键。
- 控制台提供详情筛选、刷新、失败重放和轮换确认；一次性 Secret 只留在当前页面内存，完整重载、登出或用户/租户/Session 变化时清除，不进入 localStorage/sessionStorage。

PRD-042 不包含 replay intent 已提交后发生进程崩溃的接管恢复，也不包含 `dead_letter`、对象存储、归档或过期调度。相同幂等键当前提供 at-most-once 出网语义；持久恢复与死信处理由 PRD-057 实现。

## MVP 总览 Dashboard

PRD-026 后，控制台概览页改为当前租户的 MVP Dashboard。它按 Asia/Shanghai 今日窗口从 `request_logs` 和 `usage_ledger` 实时聚合今日请求数、成功率、错误率、P95 延迟、今日成本、总 Token、Top 模型、Top Virtual Key 和最近错误；空租户显示接入引导，不展示空表格。

Dashboard 接口：

- `GET /dashboard/overview`：读取当前登录租户的今日聚合摘要，不接受 `tenant_id`、日期或跨租户筛选参数。

Dashboard 行为：

- 指标事实来源是 `request_logs` 与 `usage_ledger`，`DashboardMetric` 只是响应 DTO，不新增缓存表。
- `console_page_events.event_type` 支持 `dashboard.viewed`，用于审计概览页访问；事件 payload 仍走白名单，不保存 Token、Authorization、Prompt、Response 或密钥。
- API 允许已认证的 admin/developer/viewer 读取安全聚合摘要；调用日志详情和用量明细仍由各自后端权限依赖限制。
- Top Virtual Key 只返回 `virtual_key_id`、名称和 `key_prefix`，不返回 Virtual Key 明文、哈希、密文或 Provider Key。
- 最近错误只展示 `request_id`、错误码、模型、Provider、延迟、时间和安全诊断标题；不会读取或回显上游原始响应、Prompt 或 messages。
- Dashboard 查询索引在 `0018_dashboard_overview.sql` 中维护，查询必须始终带认证上下文中的 `tenant_id` 和今日时间窗口。

## i18n 基础框架

PRD-027 后，控制台具备用户级语言偏好框架。默认语言为 `zh-CN`，当前支持 `zh-CN` 和 `en-US`；新增语言设置页、控制台骨架新增文案和错误码展示 label 通过 i18n 字典渲染。历史页面不会在 M0 一次性全量翻译，后续 PRD 修改到的页面必须把新增文案接入 key。

i18n 接口：

- `GET /user/preferences`：读取当前登录用户的语言偏好和可选语言，不接受 `tenant_id`、`user_id` 或角色参数。
- `PATCH /user/preferences`：只允许提交 `{ "locale": "zh-CN" | "en-US" }`，不允许夹带 `tenant_id`、`user_id`、Token 或业务上下文。
- `GET /error-codes?locale=zh-CN`：错误码目录在保持英文稳定 `code/message/suggestion` 的同时返回展示层 `label`。

i18n 行为：

- `user_preferences.locale` 保存当前用户语言偏好；`user_preference_events` 只记录稳定事件 `user.locale_changed`、old/new locale 和链路 ID。
- 前端 `createI18n()` 按当前 locale、默认中文、key 的顺序 fallback；未知 locale 回退到中文。
- 日志、Webhook event_type、错误码和数据库 code 字段继续使用稳定英文 code，不把中文展示文案写入内部字段。

## Provider 管理

M0 支持管理员注册当前租户的模型 Endpoint Provider，包括智谱、通义、文心、火山、混元、DeepSeek 官方或云上 Endpoint，以及客户本地 OpenAI-compatible Endpoint。OpenRouter 和硅基流动只作为对标平台、竞品平台或迁移兼容分类，不作为默认模型厂商类别。

Provider 管理接口：
- `GET /providers`：管理员读取当前租户 Provider 列表，支持 `status` 和 `provider_type` 过滤。
- `POST /providers`：管理员创建 Provider，字段包括 `provider_code`、`name`、`provider_type`、`base_url`、`auth_type`、`description`。
- `PATCH /providers/{id}`：管理员更新 Provider 元数据或启用/禁用 Provider。
- `POST /providers/{id}/test`：管理员测试 Endpoint 可达性，返回成功/失败原因并更新健康状态。
- `GET /providers/{id}/keys`：管理员读取该 Provider 的 KeyPool 元数据，不返回明文或密文。
- `POST /providers/{id}/keys`：管理员新增 Provider Key，明文只出现在请求体，落库前使用 AES-GCM 加密。
- `PATCH /provider-keys/{id}`：管理员更新 Key 名称、权重或启用/禁用状态。

安全约束：
- `/providers` 全部复用后端 `require_current_admin`，developer/viewer 直接调用会返回 `auth.permission_denied`。
- Provider 注册 API 不接收 Provider Key 明文；Provider Key 由 KeyPool API 管理。
- Provider 和 KeyPool 列表只返回 Endpoint/Key 元数据，不返回 `plain_value`、`encrypted_key`、密钥明文或上游认证头。
- 连接测试失败会写入 Provider 健康字段和结构化日志事件 `provider.health_checked`，但不记录密钥。
- Provider 创建/更新的运行日志不记录 Endpoint 或 URL hash；持久审计只保存规范化完整 URL 的 `base_url_hash` 和非敏感字段。host/path 可包含客户内网拓扑或 Token，不得以“只删 query”作为脱敏。

## Provider KeyPool

PRD-014 后，管理员可以为同一个 Provider 配置多个上游 API Key。数据面调用上游时优先从当前租户、当前 Provider 的健康 Key 中选择一个；禁用 Key 和未过期的 `rate_limited` Key 不参与路由。上游返回 429 或明确限流错误时，网关会把本次使用的 Key 临时标记为 `rate_limited` 并记录 `provider_key.rate_limited` 事件。

KeyPool 接口：
- `GET /providers/{id}/keys`：返回 `provider_key_id`、`key_prefix`、`key_fingerprint`、`weight`、`status`、最近使用和最近错误元数据。
- `POST /providers/{id}/keys`：请求字段为 `name`、`plain_value`、`weight`；响应不回显 `plain_value`。
- `PATCH /provider-keys/{id}`：支持 `name`、`weight`、`status=active|disabled`。

安全约束：
- Provider Key 落库格式为 `aesgcm-v1:<nonce>:<ciphertext>`，生产环境必须显式配置 `METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET`。
- 响应、日志、`request_logs`、`usage_ledger` 都不记录 Provider Key 明文或 Authorization header。
- 已存在的 seed 占位 Key 不可解密，不参与真实 KeyPool 选择。不存在托管 Key 时的环境 fallback 只是受控本地兼容路径：必须是非生产 active 默认租户、active Provider，且 Provider code 和数据库/冻结 Route Endpoint 与运维基准精确匹配。生产或其他租户必须配置自己的托管 Key。

## 模型目录

M0 支持当前租户维护统一 ModelCatalog。对外调用方使用 `internal_model_id` 作为 OpenAI-compatible 请求体里的 `model`；数据面再解析到 Provider 的 `provider_model_id` 并转发给上游。数据库中的 `models.model_id` 保持为技术主键，用于日志和外键关联。

模型目录接口：
- `GET /models`：admin/developer 读取当前租户模型目录，支持 `status`、`provider_id` 和 `capability` 过滤。
- `POST /models`：admin 创建模型，字段包括 `internal_model_id`、`provider_id`、`provider_model_id`、能力标签、上下文长度、输入/输出价格、币种和备注。
- `PATCH /models/{id}`：admin 更新 Provider 绑定、上游模型 ID、能力、价格或启用/禁用状态。

安全约束：
- 所有模型目录数据按认证上下文中的 `tenant_id` 查询和更新，不接收请求体传入的租户 ID。
- developer 只读，viewer 后端拒绝；控制台隐藏入口只是体验优化。
- 响应不返回 Provider Key、Virtual Key、Authorization 或任何上游认证材料。
- 禁用模型后数据面无法继续通过该内部模型 ID 路由，返回 `gateway.model_not_found`。

## 模型别名与最小路由

PRD-015 后，调用方可以在 OpenAI-compatible 请求体的 `model` 字段传入稳定别名，例如 `company-default`。数据面先使用当前 Virtual Key 的 `tenant_id` 和 `environment` 解析 `model_aliases`；如果没有同名别名，再兼容 PRD-012 的内部模型 ID 直连路径。别名命中后，默认模型可用则选默认模型；默认模型禁用或 Provider 不可用时，尝试管理员配置的备用模型。M0 不做 DSL、质量路由、灰度路由或流式中途切换。

模型别名接口：
- `GET /model-aliases`：admin/developer 读取当前租户别名，支持 `status` 和 `environment` 过滤。
- `POST /model-aliases`：admin 创建别名，字段包括 `alias`、`environment`、`default_model_id`、`fallback_model_id`、`description`。
- `PATCH /model-aliases/{id}`：admin 更新别名、环境、默认模型、备用模型、备注或启用/禁用状态。

路由追踪：
- 每次别名解析成功会写入 `routing_decisions`，记录 `request_id`、`trace_id`、请求别名、真实模型、Provider 和 `decision_reason`。
- 别名禁用返回 `gateway.model_alias_disabled`；默认/备用模型均不可用返回 `gateway.model_disabled`；失败请求仍写入 `request_logs`。
- `routing_decisions`、`request_logs` 和 `usage_ledger` 不记录 prompt/input/messages、Virtual Key 明文、Provider Key 或 Authorization。

## Request Log 最小闭环

PRD-016 后，网关每次鉴权成功后的调用都会尽量写入 `request_logs`：成功请求记录 `request_id`、`tenant_id`、`virtual_key_id`、`endpoint`、请求模型、解析后的内部模型、Provider、状态、延迟、token 和成本估算；失败请求记录统一错误码，便于用 `request_id` 串起控制台、日志和数据库排障。PRD-045 的 0034 把日志身份升级为 `(tenant_id, request_id)` 复合主键；已认证模型请求在读取 JSON 前先创建不可变 `gateway_request_claims`，冻结 `trace_id`、Virtual Key、实际 `snapshot_version` 和 LKG 状态，后续非流式、Fallback、流式终态和 Usage 不能改写该版本。`usage_ledger` 继续保存用量账本，并通过同租户复合外键关联日志、把 token 与成本摘要回写到 `request_logs`。

调用日志控制面接口：
- `GET /request-logs`：admin/developer 查询当前租户调用日志，支持 `request_id`、`status`、`model`、`provider_code`、`error_code`、`environment_id`、`project_id`、`application_id`、`snapshot_version`、`limit`、`offset`。
- `GET /request-logs/{request_id}`：admin/developer 查询当前租户单条日志详情，返回路由决策摘要和用量摘要。

安全约束：
- `/request-logs` 全部使用后端 `require_request_log_reader`，viewer 直接返回 `auth.permission_denied`。
- 查询条件不接受 `tenant_id`，所有日志按登录上下文中的 `tenant_id` 过滤。
- `snapshot_version` 只能是正整数；列表和详情只返回快照版本、`current/lkg/emergency_lkg/expired` 等状态，以及 Policy ID/动作/reason/目标模型 ID 的安全摘要，不返回 DSL、AST 或上下文实际值。
- 控制台只展示 `key_prefix`，不展示 Virtual Key 明文、Provider Key、Authorization、Prompt、Response、messages 或 input。
- `request.logged`、`request_log.write_failed` 和 `usage_ledger.write_failed` 事件只记录安全元数据，写库失败不会反向中断网关热路径。

## Token Usage 与成本估算

PRD-017 后，`usage_ledger` 作为 UsageRecord 物理表保存请求级 Token 账本：`input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`、`cost_amount`、`cost_cny`、`cost_usd`、`currency`、`estimated`、`usage_source`、`price_snapshot_id` 和模型/Provider/Virtual Key 维度。`price_snapshots` 保存每次入账时使用的模型价格、币种和 `price_schema_version`，避免后续模型目录价格变更后历史成本不可解释。

用量控制面接口：

- `GET /usage/summary`：admin/developer 查询当前租户用量概览，支持 `start_date`、`end_date`、`model_id`、`virtual_key_id`、`provider_id`、`estimated`、`request_id`、`trace_id`，返回总量、按天、按模型和按 Virtual Key 聚合。
- `GET /usage/records`：admin/developer 分页查询当前租户 UsageRecord，支持同样筛选条件以及 `limit`、`offset`。
- `GET /usage/export.csv`：仅 admin 按当前规范筛选创建同步或异步 CSV 导出；小结果直接返回文件，大结果返回可查询的 `UsageExportJob`。
- `GET /usage/exports/{job_id}`：仅 admin 查询当前租户导出任务状态，不返回 CSV、lease 或存储路径。
- `GET /usage/exports/{job_id}/download`：仅 admin 下载当前租户已完成且未过期的完整制品，下载前复验长度和 SHA-256。

成本与估算口径：

- 上游返回 usage 时，Adapter 归一化 `input_tokens`、`output_tokens`、`total_tokens` 和 `cache_tokens`，`estimated=false`、`usage_source=upstream`。
- 上游缺少 usage 时，数据面只按请求/响应文本长度做粗略估算，不保存文本原文，`estimated=true`、`usage_source=estimated`。
- M0 不做复杂汇率回溯；人民币成本写 `cost_cny`，美元成本写 `cost_usd`，`cost_amount + currency` 保留原币种金额。

安全约束：

- `/usage/summary` 和 `/usage/records` 使用后端 `require_usage_reader`，viewer 直接返回 `auth.permission_denied`。
- CSV 导出从属接口使用更严格的后端 `require_usage_exporter`，当前仅 admin 可用；developer 的 Usage 只读权限不会隐式获得批量账本导出。
- 查询条件不接受 `tenant_id`，所有用量按登录上下文中的 `tenant_id` 过滤。
- 控制面 API 和控制台不返回 `raw_usage`，只展示规范化用量、脱敏 Key 前缀、价格快照 ID 和成本字段。
- `usage.recorded` 事件只记录 request_id、tenant_id、模型、Provider、Token、成本和价格快照，不记录 Provider Key、Authorization、Prompt 或 Response。

PRD-043 使用 `usage_export_ledger_sequences.usage_export_sequence` 的 AUTOINCREMENT 映射形成稳定账本水位；`UsageExportJob.snapshot_ledger_sequence` 只保存在任务内部，不进入 API、审计、事件或日志。0032 后 Usage Ledger 采用追加事实语义，已入账记录不能 UPDATE/DELETE；更正必须写新的调整事实。MVP 制品使用有界 SQLite BLOB 和单机 scheduler，不包含对象存储或分布式任务队列。

## 策略引擎 DSL 最小版

PRD-044 已完成设计、两轮自审、实现、专项/全量回归、0033 运行库迁移、真实 API、三服务、敏感扫描和三档浏览器验收。当前交付边界如下：

- `GET /policies`、`POST /policies`、`PATCH /policies/{policy_id}` 和 `POST /policies/validate` 均使用后端 `require_policy_admin`，只从登录上下文读取 `tenant_id`；developer、viewer 和匿名请求不能依赖前端隐藏绕过权限。
- 最小 DSL 只接受 `tenant/env/project/model/data_class/budget_remaining` 六个变量、有限比较与布尔组合，以及 `allow/deny/mask/log_only/route_to` 五类动作；不执行 `eval/exec`，也不支持函数、属性访问、递归、脚本或外部调用。
- `route_to` 只能引用当前租户 active `model_id`；不存在、禁用和跨租户目标统一拒绝。PATCH 强制使用强 `If-Match`，并在事务内权威重读版本，避免并发管理员静默覆盖。
- 策略、单规则、单动作和事件由 0033 迁移持久化，创建/更新审计与业务写入保持事务一致；`policy.created` 和 `policy.validation_failed` 只携带安全摘要，不保存 DSL 正文、Prompt、Response、Token 或 Provider Key。
- shared 求值器按 `priority ASC, policy_id ASC` 确定性执行；超过 `METAHUB_POLICY_EVALUATION_TIMEOUT_MS` 时固定 fail closed 为 `deny` 并要求告警。该配置默认 5 ms，允许范围 1-100 ms。
- `active` 当前只表示“下一快照候选”，不是已发布状态。本 PRD 不把策略挂入 Gateway 热路径；策略快照、发布/回滚和数据面消费由 PRD-045 交付，完整 Dry Run 页面由 PRD-046 交付。

详细设计见 [PRD-044 设计文档](docs/prd/PRD-044-policy-engine-minimal-dsl.md)，完整验收证据见 [PRD-044 测试记录](docs/testing/PRD-044-tests.md)。

## 策略快照版本化与发布

PRD-045 已完成。实现把 PRD-044 的 active Policy 候选显式发布为 tenant 内不可变版本，并把同一版本贯穿 Gateway 请求、调用日志与 Usage：

- `GET /policy-snapshots/current` 返回当前 head、active 候选 hash/数量及 `ETag`、`X-Policy-Candidate-ETag`；`GET /policy-snapshots?limit=&offset=` 返回当前租户历史。公共接口均为 admin-only、`Cache-Control: no-store`，不接受客户端 `tenant_id`。
- `POST /policy-snapshots/publish` 同时要求强 `If-Match` 和 `If-Policy-Candidate-Match`，在事务锁内重编全部 active Policy 后执行 head/candidate 双 CAS；`POST /policy-snapshots/{id}/rollback` 只允许复制当前上一版内容，并创建新的更高版本。例如 `v1=A -> v2=B -> rollback(v1)` 生成 `v3=A`，不会把 head 倒退而产生 ABA。
- 每次发布/回滚原子写入 header、items、head、持久事件和管理审计，提交后复用 Webhook 投递 `policy_snapshot.published`。事件和公共历史仅含版本、hash、Policy 数量、操作及安全 ID，不含 DSL、AST、Prompt、Response、Provider Key、Virtual Key、Authorization 或内部 Token。
- `GET /internal/policy-snapshots/{tenant_id}/current` 只接受独立 `X-Internal-Token` 服务身份，按 tenant 返回严格 wire 与强 ETag；304 仅刷新该 tenant 的新鲜度。未知/disabled tenant 与 active tenant 的“已确认无 head”使用不同合同。
- Gateway 在 Virtual Key 鉴权和 Environment 校验后、读取 JSON 前取得不可变 lease 并创建 `gateway_request_claims`。每个 tenant 首次请求 singleflight 拉取，随后 scheduler 仅轮询已知 tenant；合法新版本原子替换缓存，损坏、降版、同版异 hash、超大响应或网络失败保留 LKG。
- `internal_only` 在普通 max-age 后可进入有硬上限的 `emergency_lkg`，并继续执行鉴权、DLP、限流和预算；超过硬上限失败关闭。`public_service` 在普通 max-age 后即阻断；冷启动从未成功同步且没有 LKG 一律阻断。阻断请求仍先冻结 version/status claim，再写统一错误和 request_id。
- 请求只执行 lease 内 Policy。`deny`、求值超时、缺失可信上下文和未知动作在 Provider 前失败关闭；`route_to` 不继承原别名 Fallback，必须重新验证目标模型的 tenant、Environment、active Provider、健康和能力。Policy `allow/log_only` 不能绕过既有 DLP、限流、预算或 Provider Guard。
- 控制台为 admin 提供“策略发布”入口，展示当前版本、候选漂移、hash 摘要和历史；发布/上一版回滚必须填写原因并复用同一 request_id 处理结果未知的重试。developer/viewer 不加载页面，但真实授权仍由后端执行。调用日志列表/详情展示实际版本、缓存状态和安全决策摘要。
- `scripts/acceptance/prd045-runtime-acceptance.py` 执行真实三服务 v1/v2/v3、Gateway、内部 Token、Webhook、Usage 与敏感扫描；`scripts/acceptance/prd045-browser-acceptance.mjs` 使用一次性角色账号执行真实 Console 发布/回滚、后端 RBAC、调用日志和三档布局，并在异常路径按 tenant + 精确资源账本回收 Session、用户和临时 Policy。证据只写入工作区 `artifacts/prd045/`，密码和 Token 仅驻留验收进程内存。

0034 新增 `policy_snapshots`、`policy_snapshot_items`、`policy_snapshot_heads`、`policy_snapshot_events` 和 `gateway_request_claims`，把 `request_logs` 的身份升级为 `(tenant_id, request_id)`，并让 `usage_ledger`、`price_snapshots` 通过同租户复合外键关联请求。已发布快照、快照项、事件和请求 claim 由数据库触发器保护为不可变；down migration 遇到跨租户重复 request_id 时会安全中止，不会静默丢日志或错绑 Usage。

本地 Gateway 同步配置已经列入 `.env.example`：

```dotenv
METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED=true
METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS=2
METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS=2
METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS=300
METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS=3600
METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES=4194304
```

Gateway 和 Control API 必须配置相同的高熵 `METAHUB_INTERNAL_CONFIG_TOKEN`，Gateway 还必须使用 `METAHUB_CONTROL_API_BASE_URL` 指向 Control API。生产环境或 `public_service` 模式禁止关闭同步；生产同步 URL 必须使用 HTTPS，dev/test 的 HTTP 仅允许回环地址。普通 max-age 不得小于轮询间隔，emergency max-age 不得小于普通 max-age。配置摘要只显示脱敏值，日志不得输出 Token 或完整内部响应。

详细设计见 [PRD-045 设计文档](docs/prd/PRD-045-policy-snapshot-versioning-publishing.md)；专项、全量、运行库、真实 API、敏感扫描和浏览器完整证据见 [PRD-045 测试记录](docs/testing/PRD-045-tests.md)。最终结果为 Python `434 passed`、Node 示例 `33 passed`、Console `194 passed`、Console lint `100 files passed`，运行库 34 条迁移/最高 0034，三服务和三档浏览器验收通过。

## 策略模拟与单请求预演

PRD-046 已完成。管理员可在策略上线前用一条当前构造的上下文预演 active Policy 候选、路由、基础 DLP 和预算，不读取历史 Prompt，也不调用真实 Provider：

- `POST /policies/dry-run` 使用后端 `require_policy_admin`，只从登录态恢复 `tenant_id`，不接受客户端 tenant。正文以 256 KiB 上限流式读取，拒绝重复 JSON key、NaN/Infinity、非法 UTF-8、未知字段、非 object、深层递归和超限输入；actor 30 RPM、tenant 120 RPM 的有界进程内限额只保护本地 CPU。
- 模拟以 query-only 一致性连接读取当前租户 Environment、Project、Model、active Policy、路由、DLP、Usage、reservation 和预算事实，不调用 Adapter/KeyPool/客户 Endpoint，不创建 Gateway claim、request log、Usage、路由、DLP 或预算运行事实。deny、route unavailable、DLP block 和 budget block 都以 HTTP 200 返回 what-if 结论；协议错误继续使用统一错误码、request_id 和 trace_id。
- 完成结果在第二个写事务原子写入 `policy_explanations`、`policy_events` 和 `audit_logs` 三类安全事实；提交后才投递 `policy.dry_run_executed` Webhook。重复 `tenant + actor + request_id` 会先恢复并核对旧三事实、重派稳定事件，再返回 409，避免结果未知重试重复写事实。
- Prompt 仅在请求内存和浏览器短生命周期状态中使用，不持久化、不计算 hash、不写日志/事件/审计/Webhook，也不在响应回显；响应只给出长度和 `prompt_redacted=true`。`runtime_compatible` 只说明 PRD-045 发布兼容性，`gateway_parity="partial"`、`coverage_complete=false` 明确本期不模拟 Virtual Key/Application 预算、RPM/TPM/并发、KeyPool/credential 或真实 Provider 调用。
- admin-only `policy-simulator` 页面展示兼容性、决策树、规则、路由、DLP 和预算结果。提交 settle、离页、登出、身份/角色变化和 Abort 都清理 Prompt 与旧结果；developer/viewer 即使直达 hash 也回退 overview，后端请求仍返回 403。
- 0035 新增 tenant-bound、不可变的 `policy_explanations`，扩展 Policy event/audit 精确合同与部分唯一索引；down migration 删除 PRD-046 事实后重建 0034 表、索引和触发器，并逐字段保留 0034 历史。

最终已通过 PRD-046 专项、Policy/Webhook/Dry-Run/迁移回归、Python 根级全量 `459 passed`、Node 示例 `33 passed`、Console `200 passed`、Console lint `103 files passed`、中文注释、compile/node-check/目标 Ruff，以及批次 `20260718T161021` 的真实 Chrome 1280x720、900x800、390x844 验收。真实 API 最终批次 `20260718T101126` 使用当前 SHA-256 `171803A28ECD05DCE0189635025E4EAED35EB9724FD6D2374492096C5D7692FE` 脚本通过三服务、主/异常/RBAC、租户、五类零成本结果、Webhook、重复 request_id、隐私和结果未知资源补偿；Provider 回环调用为 0，cleanup `errors=[]`、`active_intent_residue=0`。服务停止后的 0035 证据库为 35 条迁移、完整性 `ok`、外键违规 0，临时资源/Session 活动残留和十份最终日志敏感命中均为 0。

详细设计见 [PRD-046 设计文档](docs/prd/PRD-046-policy-dry-run-single-request-preview.md)，完整分层证据见 [PRD-046 测试记录](docs/testing/PRD-046-tests.md)。

## 成本优先与可用性优先路由（已完成）

PRD-047 已完成两轮设计自审、实现期安全复审、主体代码、专项/全量、0036 迁移、真实三服务/API/Webhook/Usage、停止后数据库与日志复核和真实 Chrome 三档验收。当前实现边界如下：

- 每个正常 ModelAlias 持久化一条版本化 RoutingPolicy；存量和新建 Alias 默认为 `fixed v1`。候选仍只来自 Alias 的 default/fallback，不新增任意候选管理 API，也不新增 Project/User/Department override。
- `fixed` 保留现有主备稳定顺序且不读 KeyPool/价格；`availability_first` 在健康与 KeyPool 过滤后优先新鲜 healthy；`cost_first` 只有两条候选均合格时才读取同一 `as_of` 价格和估算 Token，以 15 位 Decimal 成本排序，正常缺价或混币使用稳定原因码回退 fixed 顺序。
- active CNY Budget 只接受带版本的 CNY 合法价格，缺失、非 CNY、越界或损坏价格统一返回 `gateway.budget_unavailable`。主备准入仍按最坏总额写入不可改写的 `reserved_cost_cny`；`outstanding_cost_cny` 只保留终态后仍未结算的上游分项，即主成功/Provider 前本地失败为 0、主失败备成功为主估算、双上游失败为主备总额。
- 进程级 Provider Key 只能在非生产 active 默认租户、active Provider、code 和数据库/冻结 Route Endpoint 均匹配运维基准时作为兼容回退；生产或其他租户必须使用自己的托管 Key。
- `GET /model-aliases` 为 admin/developer 返回当前 Policy 和配置候选安全摘要；admin-only `PATCH /model-aliases/{id}/routing-mode` 使用认证后 8 KiB 严格 JSON、精确字段/类型、锁内 CAS、同 request_id 持久幂等、同事务 audit/event 和跨租户 404。协议与重试合同见 [ModelAlias RoutingPolicy API](docs/api/routing-policies.md)。
- Gateway 在同一冻结事务读取 mode 所需的最小 Health/KeyPool/价格/Fallback 事实并原子写 `routing_decisions`、`routing_candidates`；动态模式无法形成可追踪事实时在 Provider 前失败关闭。调用日志详情返回 Policy ID/版本/mode、成本比较时点与 Token 估算，以及最多两条强类型候选事实。
- 0036 migration 新增 `model_alias_routing_policies`、`routing_policy_events`、`routing_candidates` 和 `budget_reservations.outstanding_cost_cny`，扩展并重建 `routing_decisions` 和 `audit_logs`，以 tenant-qualified 复合外键、owner/version/identity/immutable 触发器约束 Policy/Alias/Model/Provider/Decision/Candidate 图；PRD-030 遗留 NULL Environment Key 只按同租户 code 安全兼容，Policy-bound Decision 使用 request 部分唯一索引而不删除 0035 重复历史。历史行与链路字段保真的唯一安全例外是不可逆删除旧 Provider Audit 摘要中的 `base_url`；down 不恢复 URL，但保留已安全化的其他历史。
- Console 新增 `routing-policies` 页面：admin 编辑，developer 只读，viewer 不渲染页面且不预取；保存状态绑定 Session、tenant、user、role、Alias、Policy ID/版本、草稿代次和 request ID，拒绝迟到或身份漂移响应。
- 最终批次 `artifacts/prd047/20260719T145100` 通过真实三服务/API/Webhook/Usage、隐私与 owner-safe cleanup，以及 1280x720、900x800、390x844 Chrome。停止后数据库 SHA-256 为 `daf0acfc9d88aa79681cbcbc83d763738ee280f23059abca0d78e1f9e9972039`，36 条迁移/最高 0036、完整性 `ok`、外键违规 0、WAL/SHM 与端口监听均为 0；2 条保留的 enabled RoutingPolicy 只绑定 disabled Alias，可调用计数为 0。

详细设计与自审记录见 [PRD-047 设计文档](docs/prd/PRD-047-cost-first-availability-first-routing.md)，完整自动化、迁移、Runtime、Browser、日志、清理和停止后证据见 [PRD-047 测试记录](docs/testing/PRD-047-tests.md)。

## 效果-成本路由基础（已完成）

PRD-048 已完成两轮设计自审、实现后安全复审、代码实现、0037 正反迁移、专项/全量回归、真实三服务/API/Webhook、幂等清理、停止后数据库与日志复核，以及真实 Chrome 三角色三档验收。当前实现边界如下：

- RoutingPolicy 新增 `quality_cost` 和质量/成本/延迟三权重、质量门槛、最小样本数五字段配置。算法先做通用资格和质量门槛，再处理 cold/ready 证据，只在正权重确实需要时读取延迟、Token 和价格；全部计算使用整数 bps 和 Decimal，原因码、同分与缺失依赖回退稳定。
- `GET/PATCH /model-quality-scores` 以 tenant-qualified Model 为对象，admin/developer 可读、仅 admin 可写；统计版本和人工反馈版本独立，写入使用锁内 CAS、tenant+actor+request_id 幂等恢复、同事务 Audit/Event 和提交后 Webhook。viewer 与跨租户在后端拒绝，API 不接受客户端 tenant_id。
- Gateway 只把真实上游 timeout、429、5xx 和明确 transport 终态计入质量；认证、配额、模型/请求错误、本地协议/处理错误和客户端断开不污染统计。质量资格与预算可能计费判断独立，Attempt/Stream 第一次终态和 Score 增量共用事务，重复或清理异常不能双计或跳过租约释放。
- 动态 Decision/Candidate 冻结 `quality_cost_v1`、Policy/config、Score ID、统计/反馈版本、证据、计数、三分量和总分；Dry-Run 复用同一纯函数且无 Provider/Usage/统计副作用，RequestLog 只读不可变事实，禁止用当前 Score 回算历史。
- 0037 创建 Score/Event 并重建 Policy/Decision/Candidate/Event/Audit 硬合同；历史回填使用精确 Attempt/Stream 分类、规范 UTC/整数微秒延迟和 64-bit preflight。down 只有完整自动统计可重建且没有质量业务事实时才允许，其他情况在结构修改前以固定错误原子阻断。
- Console PAGE-068 支持四种路由模式和五字段配置，PAGE-069 支持 admin 人工评分、developer 只读、viewer 零 DOM/预取；调用日志展示冻结质量解释。最终 21 张真实 Chrome PNG 覆盖 1280x720、900x800、390x844，均非空、无横向溢出、外部请求、未知浏览器错误或敏感命中。
- 最终批次 `artifacts/prd048/20260719T200349` 通过 Python `680/680`、Python 示例 `38/38`、Node 示例 `33/33`、Console `261/261`、Console lint `110 files`、Ruff/compileall/中文注释，以及真实 Runtime/Browser/日志/清理门禁。停止后数据库 SHA-256 为 `39eaf358f537aa4c24fa2b061a821e47fb1238b5a6dd651174e182ee3be66ce3`，37 条迁移/最高 0037、完整性 `ok`、外键违规 0，WAL/SHM 和三端口监听均为 0。

详细设计和三轮复审见 [PRD-048 设计文档](docs/prd/PRD-048-quality-cost-routing-foundation.md)，完整证据见 [PRD-048 测试记录](docs/testing/PRD-048-tests.md)。

## 调用反馈与采纳率记录（已完成）

PRD-049 已完成设计与多轮自审、实现、安全加固、0038/0039 正反迁移、专项/全量回归、真实三服务、Runtime、Browser、日志与停止后数据库复核。当前实现边界如下：

- Control API 与 Gateway 都提供 `POST /feedback`；前者从 Session 恢复当前 tenant 和用户 actor，后者从 Virtual Key 恢复 tenant/key actor。两个入口共用严格 payload、标签规范化、意图指纹、幂等和事务合同，Gateway 不提供 Summary 查询。
- admin/developer 可在 Control 创建反馈和读取汇总，viewer 在读取正文前由后端拒绝；Virtual Key 只能评价同租户且由同一 Key 产生的成功调用。不存在、跨租户和跨 Key 的源 request_id 统一返回防枚举 404，客户端不能提交 tenant、actor、Model 或 Provider 归属。
- 每个 `tenant_id + source_request_id` 只能形成一条不可变 Feedback；评分、采纳、固定白名单标签、最终 Model/Provider 和 actor 快照与持久事件同事务写入。接口不提供 PATCH/DELETE，数据库同时阻断 UPDATE、DELETE 与命中唯一键的 REPLACE 旁路。
- `GET /feedback/summary` 仅位于 Control API，按当前租户、可选模型和 UTC 闭开窗口返回总计、五档评分、标签和模型分页；并发门禁、绝对 deadline 和租户/时间索引避免低频聚合拖住 ASGI 或模型热路径。
- RequestLog 列表/详情增加 `has_feedback`、不可变反馈和可反馈资格；ModelQuality 增加独立逐请求满意度与采纳率字段，不修改 PRD-048 自动质量样本、人工评分或 `quality_cost_v1` 历史解释。
- `feedback.created` 使用首次持久事件 ID 在提交后投递 Webhook；投递失败不能回滚已提交 Feedback，重放不产生第二条业务事实。
- 0038 创建 `request_feedbacks`、`request_feedback_events`、`model_feedback_statistics` 及租户复合外键、不可变/聚合/事件和安全回滚约束；0039 在不修改 0038 checksum 的前提下增加六项 INSERT/UPDATE 冲突守卫，关闭 REPLACE 改写 Feedback/Event/父 RequestLog/最终 Attempt 的路径。
- Console PAGE-070 提供日志详情提交与已记录只读态，PAGE-071 提供租户反馈报表；admin/developer 可见，viewer 无 DOM/预取。筛选响应只有在四项指标、唯一模型行和全部标签 DOM 收敛后才进入截图验收。
- 最终批次 `artifacts/prd049/20260720T181908` 通过 Python `783`、Console `287/287`、Console lint `114 files`、39 条迁移/最高 0039、真实 Runtime 与 15 张三视口 Browser PNG 验收；这只证明 PRD-049 完成，不代表全部 108 条 PRD 已完成。

详细设计与自审见 [PRD-049 设计文档](docs/prd/PRD-049-request-feedback-adoption-records.md)，公开接口见 [Feedback API](docs/api/request-feedback.md)，专项、全量、迁移和真实运行证据见 [PRD-049 测试记录](docs/testing/PRD-049-tests.md)。

## Prompt Cache 感知粘性路由（已完成）

PRD-050 已完成两轮编码前自审、实现、0040、当前源码相关测试、真实 Runtime/Browser、日志隐私、清理和停止后数据库终审。当前实现边界如下：

- CacheAffinity 配置以 ModelAlias 为对象，enabled 时要求服务端可信 data_class；Control API 从 Session 恢复 tenant/actor，admin 可写、admin/developer 可读、viewer 后端拒绝，跨租户与不存在 Alias 同形。
- Gateway 只对有界长 system/developer 前缀计算独立 HMAC 指纹；tenant、Virtual Key、data_class、endpoint revision 和 Prompt 任一变化都不会复用。它不缓存或返回 Prompt/Response，也不实现语义缓存。
- 动态 RoutingPolicy 只在同一健康/质量资格层级内应用 pending/warm 端点偏好；fixed 保持主备硬顺序。KeyPool 在已选 Endpoint 内复用或重绑 Credential，target/lease generation 防止迟到终态污染新绑定。
- Selection 在 Provider 前提交，非流式 succeeded attempt 或流式 completed Session 才确认 warm；失败、timeout、watchdog、disconnect、DLP/限流/预算/权限阻断不会伪造预计命中。
- PAGE-072 为 admin 编辑、developer 只读、viewer 无受限 DOM/预取；PAGE-073 分开展示 estimated hit 和 Provider 实报缓存相关 Token。前端权限表现不替代后端 RBAC。
- 0040 新增五张业务表并扩展 Candidate reason/Audit 约束；当前 loader checksum 为 `dc4289d44091e231f2c288398c58ab8eccc075fd7bcbbc869f2b7b7c8257f090`。
- 权威批次 `artifacts/prd050/20260725T005725` 的 Runtime 与 Browser 均为 `passed`；Browser 形成 45 张真实 Chrome PNG（admin/developer/viewer = 30/12/3），三档视口各 15 张，SHA 全唯一、非空/隐私遮罩与 45 条布局合同全部通过。
- 当前源码相关验证为 Browser 合同 4/4、CacheAffinity + Dashboard 15/15、静态 verifier 旧 SHA 修复后的组合 21/21、脚本语法和中文注释通过。2026-07-24 的 Python 921/921、Console 354/354、Browser 单测 71/71、Node 示例 33/33、lint 118 files 明确只作为变更前基线，本轮未重跑无关全量。
- 六份服务日志 5xx/ERROR/CRITICAL 和敏感命中均为 0；停止后数据库 40/0040、integrity ok、FK 0、运行态残留 0、WAL/SHM/journal 0，最终 SHA-256 为 `5b895e79fc11dda8c77d1a9131bebc407ae9e68acc6102bf2883d742a25238c7`。
- 本次收口全部位于 `D:/MetaHub`，没有使用 `C:/tmp`、提权、CIM 或端口 owner 扫描。图片查看器 setup refresh 失败已如实记录，真实 Chrome/PNG 像素/布局自动化证据作为视觉通过依据。

详细设计与两轮自审见 [PRD-050 设计文档](docs/prd/PRD-050-prompt-cache-aware-sticky-routing.md)，公开合同见 [CacheAffinity API](docs/api/cache-affinity.md)，测试范围和最终证据见 [PRD-050 测试记录](docs/testing/PRD-050-tests.md)。

## 控制台骨架与导航

PRD-018 后，控制台登录成功后进入统一主框架。后端生成 `NavigationItem` 列表，前端只消费可见入口并渲染顶部租户/用户信息、分组侧边栏、当前内容区和空状态下一步引导。

控制台骨架接口：

- `GET /console/navigation`：返回当前角色可见导航项和 active item，不接受前端传入 `tenant_id`。
- `GET /console/preferences`：读取当前 `tenant_id + user_id` 的控制台偏好。
- `PATCH /console/preferences`：保存 active navigation 和侧边栏折叠状态，目标入口必须对当前角色可见。
- `POST /console/events`：写入 `console.page_viewed`，事件只允许低风险白名单 payload。

安全约束：

- Admin 可见配置入口；Developer 可见 Key、日志、用量、API 文档和调试台；Viewer 不展示受限管理入口。
- 前端隐藏导航只是 UX，Provider、用户、模型、Key、日志和用量 API 仍由各自后端依赖做 RBAC。
- `user_preferences` 和 `console_page_events` 默认带 `tenant_id`，写入租户、用户和 session 信息都来自认证上下文。
- 控制台事件不记录 Token、Authorization、Provider Key、Virtual Key 明文、Prompt、Response、messages 或 input。

## 开发者快速开始

PRD-019 后，控制台提供“快速开始”入口，面向 admin/developer 返回当前租户可复制执行的 Python、JavaScript 和 curl 非流式 chat 调用示例。

快速开始接口：

- `GET /developer/quickstart-data`：返回 Gateway `/v1` base_url、推荐模型别名、Virtual Key 环境变量占位、步骤、代码示例和常见错误说明。

安全约束：

- 接口只允许 admin/developer 访问，viewer 后端返回 `auth.permission_denied`。
- 响应不返回 Virtual Key 明文、`key_hash`、Provider Key 或 Authorization，只返回 `METAHUB_VIRTUAL_KEY` 占位和可安全展示的 Key 前缀。
- `developer_portal_events` 默认带 `tenant_id`，`developer.quickstart_viewed` 只记录模型别名、是否已有 active Key 和示例 ID 等安全元数据。
- 示例代码统一使用 `METAHUB_VIRTUAL_KEY` 环境变量，不把 Key 写进源码或页面事件。

## 内置 API 调试台

PRD-020 后，控制台提供内置 API 调试台。开发者或管理员可以选择当前租户 active Virtual Key 和 active 模型别名，编辑 OpenAI-compatible `messages`，发起一次非流式 Chat Completions 调试请求，并查看响应、request_id、trace_id、延迟、usage、成本和错误建议。

调试台控制面接口：

- `POST /debug/chat-completions`：admin/developer 发起低频调试请求；viewer 后端返回 `auth.permission_denied`。

安全与追踪约束：

- 调试接口不接收 `tenant_id`、Virtual Key 明文、Provider Key、Authorization 或其他密钥字段。
- 后端只按登录态和当前租户 active `virtual_key_id` 恢复 `environment/key_prefix` 等元数据，不读取或返回 `key_hash`。
- 成功请求写入 `request_logs`、`usage_ledger` 和 `debug_console_events`，失败请求在具备有效 Virtual Key 上下文时写入失败日志和调试事件。
- `debug_console_events` 只保存安全元数据，不保存 Prompt、messages content、完整 response、Virtual Key 明文、Provider Key 或 Authorization。
- 成功响应返回 `log_detail_url`，控制台可跳转到调用日志详情继续排障。

## 智能错误提示基础版

PRD-021 后，统一错误响应、错误码目录、调用日志详情和调试台错误卡片都会带安全的 `diagnosis` 字段。诊断规则用于把 401/429/5xx 等失败归因为可操作原因，例如上游 Key 失效、Provider 限流、模型不存在、余额不足、网关超时和参数错误。

核心能力：

- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py` 保存静态 `ErrorDiagnosisRule`，M0 不落库、不做租户自定义规则。
- `GET /error-codes` 返回错误码元数据和诊断信息，供文档、调试台和运维排障使用。
- Provider Adapter 将上游 401/403、429、402/quota、404/model、timeout、400/422 映射到细分 `gateway.upstream_*` 错误码。
- `GET /request-logs/{request_id}` 在详情中返回 `error_diagnosis`，控制台日志详情展示原因、建议、动作、级别和可重试标记。
- 调试台错误卡片展示同一套诊断 DTO，并对所有字段做 HTML 转义。

安全边界：

- 诊断只使用错误码、HTTP 状态、Provider error type 和受控关键词，不返回上游原始 body、headers、Authorization、Provider Key、Virtual Key 明文、Prompt、messages 或 Response。
- `error.diagnosed` 是结构化日志事件，只包含 `request_id`、`trace_id`、`error_code`、`diagnosis_rule_id`、分类和 retryable 等安全元数据。
- M0 不做自动修复、不触发 Key 自动轮换、不做 LLM RCA。

## OpenAI-Compatible 网关接口

M0 数据面提供基础 OpenAI-compatible 入口。开发者可以把 OpenAI SDK 的 `base_url` 指向 Gateway `/v1`，并用 Virtual Key 作为 Bearer Token。

本地 seed Virtual Key 默认来自 `METAHUB_SEED_VIRTUAL_KEY`，未设置时为：

- `<set-local-test-virtual-key>`

本地运行示例前，把一次性明文 Key 放入环境变量：

```powershell
$env:METAHUB_VIRTUAL_KEY="<set-local-test-virtual-key>"
```

Provider Endpoint 来自 seed provider 或环境变量：

```powershell
$env:METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL="http://localhost:11434/v1"
$env:METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY="replace-with-provider-key"
```

Python SDK 示例：

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key=os.environ["METAHUB_VIRTUAL_KEY"],
)

response = client.chat.completions.create(
    model="default-chat",
    messages=[{"role": "user", "content": "你好"}],
)
```

API：

- `POST /v1/chat/completions`：支持基础 JSON 请求和 `stream=true` SSE 透传。
- `POST /v1/embeddings`：支持基础 `input` 请求。

安全与追踪约束：

- 数据面只用 Virtual Key 恢复 `tenant_id`，不信任请求体中的租户字段。
- Provider Key 只进入上游请求头，不写入日志、响应、request_logs 或 usage_ledger。
- `request.received` 不记录 prompt/input/message 明文。
- 成功请求写 `request_logs` 和 `usage_ledger`；鉴权成功后的失败请求尽量写 `request_logs`。

## Provider Adapter

PRD-013 后，数据面通过内部 `ProviderAdapter` 合同调用上游，主流程不直接硬编码某个厂商的 HTTP 细节。当前实现：

- `OpenAICompatibleAdapter`：默认 OpenAI-compatible JSON/SSE Adapter。
- `GenericHttpConfigAdapter`：通用 HTTP 配置 Adapter，作为后续自定义 Endpoint 的扩展点。
- `adapter_for_route(route)`：按 `providers.provider_type` 选择 Adapter。
- usage 统一归一为 `input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`；上游缺 usage 时会产生带 `estimated=true` 的本地估算用量。
- 上游错误统一归一为可操作错误码：`gateway.upstream_auth_failed`、`gateway.upstream_rate_limited`、`gateway.upstream_quota_exceeded`、`gateway.upstream_model_not_found`、`gateway.upstream_timeout`、`gateway.upstream_request_invalid`、`gateway.upstream_error` 或 `gateway.upstream_unavailable`，并写结构化事件 `provider_adapter.error`。

Provider Adapter 不暴露新的公开 API，不记录 Authorization、Provider Key、prompt、input 或 message 明文。后续新增厂商优先新增 Adapter，不改 `/v1` 主流程。

## API 契约

### GET /error-codes

共享只读元数据接口，Gateway 与 Control Plane 都由 `create_service_app` 暴露。响应包含统一错误码、HTTP 状态、retryable、owner、suggestion、`diagnosis` 和展示层 `label`，用于 API 文档、调试台和运维排障。可选 `locale=zh-CN|en-US` 只影响 `label`，不改变稳定英文 `code/message/suggestion`。

该接口不读取租户业务数据，不接收 `tenant_id`，也不返回 Provider Key、Virtual Key、Authorization、Prompt 或 Response。

### POST /v1/chat/completions

数据面 OpenAI-compatible 接口，必须传入 `Authorization: Bearer <virtual_key>`。

请求示例：

```json
{
  "model": "default-chat",
  "messages": [
    {
      "role": "user",
      "content": "你好"
    }
  ],
  "stream": false
}
```

响应为上游 OpenAI-compatible JSON；`stream=true` 时返回 `text/event-stream`。参数校验失败返回 `gateway.model_missing`、`gateway.messages_missing` 等统一错误码。

### POST /v1/embeddings

数据面 OpenAI-compatible 接口，必须传入 `Authorization: Bearer <virtual_key>`。

请求示例：

```json
{
  "model": "default-embedding",
  "input": "hello"
}
```

响应为上游 OpenAI-compatible JSON。Embeddings M0 不支持 `stream=true`，出现时返回 `gateway.stream_not_supported`。

### POST /debug/chat-completions

控制面内置 API 调试接口，必须传入登录态 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。该接口用于低频控制台调试，不要求用户粘贴 Virtual Key 明文。

请求示例：

```json
{
  "virtual_key_id": "vk_...",
  "model_alias": "default-chat",
  "messages": [
    {
      "role": "user",
      "content": "你好，MetaHub"
    }
  ],
  "temperature": 0.7,
  "max_tokens": 512
}
```

成功响应包含 `debug_request_id`、`request_id`、`trace_id`、`latency_ms`、`response_json`、`usage`、`cost` 和 `log_detail_url`。失败响应使用统一错误结构，包含 `code`、`message`、`request_id`、`trace_id` 和 `suggestion`。请求体如果包含 `tenant_id`、`authorization`、`provider_key`、`virtual_key`、`token`、`secret`、`password` 或 `key_hash` 等字段会被拒绝。

### GET /virtual-keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

返回示例：

```json
{
  "virtual_keys": [
    {
      "virtual_key_id": "vk_...",
      "tenant_id": "944f...",
      "name": "local dev key",
      "key_prefix": "mhv_dev_xxx",
      "environment": "dev",
      "description": "本地开发调试",
      "status": "active",
      "last_used_at": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /virtual-keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

请求示例：

```json
{
  "name": "local dev key",
  "environment": "dev",
  "description": "本地开发调试"
}
```

响应只在创建时包含一次性明文：

```json
{
  "virtual_key": {
    "virtual_key_id": "vk_...",
    "key_prefix": "mhv_dev_xxx",
    "environment": "dev",
    "status": "active"
  },
  "plain_value": "mhv_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### PATCH /virtual-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

请求示例：

```json
{
  "name": "prod app key",
  "environment": "prod",
  "description": "生产业务系统",
  "status": "disabled"
}
```

### DELETE /virtual-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。该接口执行软禁用，返回禁用后的 Key 元数据，不返回明文。

### GET /providers

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

返回示例：

```json
{
  "providers": [
    {
      "provider_id": "provider_...",
      "tenant_id": "944f...",
      "provider_code": "deepseek_official",
      "name": "DeepSeek 官方 Endpoint",
      "provider_type": "deepseek",
      "base_url": "https://api.deepseek.com/v1",
      "auth_type": "bearer",
      "status": "active",
      "description": "生产候选 Provider",
      "health_status": "unknown",
      "last_tested_at": null,
      "last_test_error": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /providers

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 Provider Key 明文字段。

请求示例：

```json
{
  "provider_code": "deepseek_official",
  "name": "DeepSeek 官方 Endpoint",
  "provider_type": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "auth_type": "bearer",
  "description": "生产候选 Provider"
}
```

### PATCH /providers/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。目标 Provider 必须属于当前租户。

请求示例：

```json
{
  "status": "disabled"
}
```

### POST /providers/{id}/test

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。接口执行最小连接测试，并返回成功或失败原因。

请求示例：

```json
{
  "timeout_seconds": 5
}
```

响应示例：

```json
{
  "provider": {
    "provider_id": "provider_...",
    "provider_code": "deepseek_official",
    "health_status": "healthy"
  },
  "test_result": {
    "status": "success",
    "http_status": 200,
    "latency_ms": 42,
    "message": "Endpoint is reachable.",
    "checked_at": "2026-07-08T10:05:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /providers/{id}/keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
返回示例：
```json
{
  "provider_keys": [
    {
      "provider_key_id": "provider_key_...",
      "tenant_id": "944f...",
      "provider_id": "provider_...",
      "name": "prod key 1",
      "key_prefix": "sk-prod-abc",
      "key_fingerprint": "6ac1...",
      "weight": 100,
      "status": "active",
      "last_used_at": null,
      "last_error_at": null,
      "last_error_code": null,
      "last_error_message": null,
      "rate_limited_until": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /providers/{id}/keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
请求示例：
```json
{
  "name": "prod key 1",
  "plain_value": "sk-...",
  "weight": 100
}
```

响应只返回 Key 元数据，不返回 `plain_value` 或 `encrypted_key`。

### PATCH /provider-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
请求示例：
```json
{
  "status": "disabled"
}
```

### GET /models

认证接口，必须传入 `Authorization: Bearer <token>`，当前用户角色必须为 `admin` 或 `developer`。支持 `status`、`provider_id`、`capability` 查询参数。

返回示例：

```json
{
  "models": [
    {
      "model_id": "model_...",
      "tenant_id": "944f...",
      "internal_model_id": "deepseek-chat",
      "provider_id": "provider_...",
      "provider_code": "deepseek",
      "provider_name": "DeepSeek 官方 Endpoint",
      "provider_model_id": "deepseek-chat",
      "display_name": "DeepSeek Chat",
      "capabilities": {"chat": true, "stream": true},
      "context_window": 64000,
      "input_price_per_1k": 0.001,
      "output_price_per_1k": 0.002,
      "currency": "CNY",
      "price_schema_version": "manual-202607",
      "status": "active",
      "description": "生产候选模型",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /models

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 `tenant_id`。

请求示例：

```json
{
  "internal_model_id": "deepseek-chat",
  "provider_id": "provider_...",
  "provider_model_id": "deepseek-chat",
  "display_name": "DeepSeek Chat",
  "capabilities": {"chat": true, "stream": true, "embeddings": false},
  "context_window": 64000,
  "input_price_per_1k": 0.001,
  "output_price_per_1k": 0.002,
  "currency": "CNY",
  "price_schema_version": "manual-202607",
  "description": "生产候选模型"
}
```

### PATCH /models/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。路径 `{id}` 是技术主键 `models.model_id`，M0 不支持直接重命名 `internal_model_id`。

请求示例：

```json
{
  "provider_model_id": "deepseek-chat-v2",
  "context_window": 128000,
  "output_price_per_1k": 0.003,
  "status": "disabled"
}
```

### GET /model-aliases

认证接口，必须传入 `Authorization: Bearer <token>`，当前用户角色必须为 `admin` 或 `developer`。支持 `status`、`environment` 查询参数。

返回示例：

```json
{
  "model_aliases": [
    {
      "model_alias_id": "alias_...",
      "tenant_id": "944f...",
      "alias": "company-default",
      "environment": "dev",
      "default_model_id": "model_...",
      "default_internal_model_id": "deepseek-chat",
      "default_display_name": "DeepSeek Chat",
      "default_provider_code": "deepseek",
      "default_provider_name": "DeepSeek 官方 Endpoint",
      "fallback_model_id": "model_...",
      "fallback_internal_model_id": "qwen-plus",
      "fallback_display_name": "Qwen Plus",
      "fallback_provider_code": "qwen",
      "fallback_provider_name": "通义千问 Endpoint",
      "status": "active",
      "description": "默认聊天别名",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /model-aliases

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 `tenant_id`。

请求示例：

```json
{
  "alias": "company-default",
  "environment": "dev",
  "default_model_id": "model_default",
  "fallback_model_id": "model_fallback",
  "description": "默认聊天别名"
}
```

### PATCH /model-aliases/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "status": "disabled"
}
```

### GET /health

返回示例：

```json
{
  "status": "ok",
  "service_name": "gateway-data-plane",
  "environment": "dev",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /internal/health

内部轻量健康检查，M0 不依赖数据库、Redis 或 Provider：

```json
{
  "status": "ok",
  "service_name": "control-plane-api",
  "environment": "dev",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /health/live

Liveness probe，只证明进程可响应，不访问数据库、Redis 或 Provider。

```json
{
  "service_name": "gateway-data-plane",
  "status": "ok",
  "live": true,
  "ready": false,
  "checks": [],
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /health/ready

Readiness probe。数据库配置存在时执行 ping，失败时 `ready=false` 并返回 HTTP 503；Redis 和 Provider 在 M0 为可选检查。

```json
{
  "service_name": "control-plane-api",
  "status": "fail",
  "live": true,
  "ready": false,
  "checks": [
    {
      "name": "database",
      "status": "fail",
      "required": true,
      "latency_ms": 1,
      "message": "database ping failed: DbError"
    }
  ],
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /version

返回示例：

```json
{
  "service_name": "gateway-data-plane",
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_..."
}
```

### GET /internal/config/summary

控制面内部接口，用于“系统设置-环境信息”只读页。`dev/test` 可直接访问，`prod` 必须传入 `X-Internal-Token`。

返回示例：

```json
{
  "service_name": "control-plane-api",
  "environment": "dev",
  "loaded_at": "2026-07-07T09:00:00Z",
  "items": [
    {
      "key": "METAHUB_PROVIDER_DEEPSEEK_API_KEY",
      "source": "env",
      "status": "masked",
      "value_preview": "***MASKED***"
    }
  ],
  "warnings": [],
  "request_id": "req_..."
}
```

### GET /tenant/current

认证接口，必须传入 `Authorization: Bearer <token>`。

返回示例：

```json
{
  "tenant": {
    "tenant_id": "944f...",
    "slug": "default",
    "name": "MetaHub Default Tenant",
    "service_mode": "internal_only",
    "status": "active",
    "created_at": "2026-07-07T10:00:00Z",
    "updated_at": "2026-07-07T10:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### PATCH /tenant/current

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "name": "MetaHub Demo Tenant",
  "service_mode": "internal_only"
}
```

响应同 `GET /tenant/current`。

### GET /users

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

返回示例：

```json
{
  "users": [
    {
      "user_id": "usr_...",
      "tenant_id": "944f...",
      "email": "dev@example.com",
      "display_name": "Dev User",
      "role": "developer",
      "status": "active",
      "created_at": "2026-07-07T10:00:00Z",
      "updated_at": "2026-07-07T10:00:00Z",
      "last_login_at": null
    }
  ],
  "roles": [
    {
      "code": "admin",
      "name": "管理员",
      "description": "管理当前租户配置、用户、Provider 和 Key。"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /users

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "email": "dev@example.com",
  "display_name": "Dev User",
  "password": "LocalDev!2026",
  "role": "developer"
}
```

响应返回创建后的用户对象，不返回密码或密码哈希。

### PATCH /users/{id}/role

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "role": "viewer"
}
```

目标用户必须属于当前租户；跨租户 user_id 统一返回 `user.not_found`。

所有响应都会返回 `X-Request-ID` 和 `X-Trace-ID`。错误响应统一为：

```json
{
  "code": "infra.not_found",
  "message": "Requested resource was not found.",
  "request_id": "req_...",
  "trace_id": "trc_...",
  "suggestion": "Check the API path and method."
}
```

## PRD 开发流程

后续必须按 Excel 中的 `PRD_ID` 逐条推进，优先顺序为 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3`。

每条 PRD 必须：

1. 阅读 Excel 中对应 PRD 行以及相关页面/API/数据对象/验收用例。
2. 先输出设计文档到 `docs/prd/`，包含需求理解、模块、表设计、API、前端、错误码、安全、测试、代码计划。
3. 编码时保留详细中文注释：每个定义/函数有中文说明，关键权限、租户、脱敏、路由、成本和失败降级分支有注释。
4. 补主流程测试和异常/权限/安全测试。
5. 完成后更新 README 或 `docs/development/` 文档。

## 安全红线

- Provider Key、Virtual Key、Authorization、Token、Secret 不允许明文落日志。
- 管理员密码和 Bearer Token 不允许明文落库或落日志。
- 后续所有业务数据默认带 `tenant_id`。
- 控制台隐藏按钮不等于权限，所有管理 API 必须在后端做权限校验。
- 用量与成本必须可追踪，失败请求必须进入调用日志或错误日志。

## Environment 隔离边界

PRD-030 将 `dev/test/prod` 建模为租户级 Environment：

- `GET /environments` 对 admin/developer 开放；创建和启停仅 admin 可用，越权请求由后端拒绝。
- Virtual Key、ModelAlias 和 BudgetRule 新写路径必须绑定同租户 `environment_id`；环境代码只作为兼容快照。
- 同一别名可在不同 Environment 指向不同模型，同一环境内仍保持唯一；预算规则按环境和 scope 分别配置。
- Gateway 环境只来自 Virtual Key 鉴权上下文，不接受请求体或 Header 覆盖；禁用环境在 Provider 调用前阻断，失败仍写入带环境的调用日志。
- 调试台后端比较所选 Environment 与 Key 绑定，生产 Key 在测试环境中使用会返回 `debug.environment_mismatch`。
- 路由决策、调用日志、用量账本和预算评估保存环境归因；`environment.created` 只记录安全元数据。

M0.5 不实现多地域环境、部署集群映射、环境审批流或跨环境自动发布。

## 模型价格与生效时间边界

PRD-031 建立有效期价格计划与不可变 Usage 快照：

- `POST /model-prices` 仅 admin 可用，登记模型输入/输出 Token 每千单价、CNY/USD 币种、版本和带时区生效时间。
- `GET /model-prices` 对 admin/developer 开放，按服务器时刻动态展示历史、当前和待生效计划；viewer 由后端拒绝。
- Gateway 只按可信 `tenant_id + model_id + 服务器 UTC 时间` 选择最近已生效计划，客户端不能覆盖价格计划或生效时刻。
- 每条 Usage 创建独立 `price_snapshots`，复制实际单价、版本、`price_plan_id` 和 `effective_from`；新价格不会修改旧成本或旧快照。
- 没有价格计划的存量模型继续使用模型目录兼容价格，避免破坏 PRD-017 已有调用链路。
- `price.updated` 事件和 Webhook 只包含模型、版本、币种和生效时间，不包含任何 Key、Authorization、Prompt 或 Response。

M0.5 不自动抓取厂商价格、不做汇率换算、历史账单重算、价格审批或批量导入。

## Provider 计费归一化边界

PRD-032 为 Provider Adapter 建立统一文本用量契约：

- Adapter 将 OpenAI、通用/Anthropic 与 Gemini usage 字段归一为输入、输出、总量和缓存 Token，并保留估算来源与稳定警告代码。
- Provider 自报成本只用于厂商对账，PricePlan 计算的内部成本仍是预算与 Usage Ledger 的权威口径；USD 或未知币种不做隐式人民币换算。
- raw usage 只提取 usage 子对象，并在 Adapter 写入前、账本入口和控制面返回前执行脱敏与体积限制；Provider Key、Authorization、Prompt 和 Response 不得进入数据库、日志或 API。
- `GET /usage/records` 继续由后端限制 admin/developer；控制台用量详情展示安全 normalized/raw 双口径，viewer 不能通过手写请求绕过权限。
- 非流式成功调用记录 `usage.normalized` 安全结构化事件，保留 request_id、trace_id 和 tenant_id 追踪，不在数据面热路径同步派发 Webhook。

M0.5 只处理文本 Token，不处理图片、语音、视频、复杂阶梯计费、自动抓价或无快照汇率换算。

## 预算耗尽阻断与恢复边界

PRD-033 在 PRD-023 日预算 Guard 基础上补齐可审计的阻断生命周期：

- 当租户、Project、Application 或 Virtual Key 的 block 规则预计达到 100% 时，Gateway 在 Provider 调用前返回 `gateway.budget_exhausted`，首次命中创建唯一 active BudgetBlock。
- 当日 active block 是硬阻断事实，后续较小请求不能绕过；每次失败仍保留 request_id、trace_id、规则、金额和原因摘要。
- Gateway 在 Provider 前以原子事务为每条预算规则创建成本 reservation，并把未结算预留计入余额，避免并发请求集体突破额度。无 Fallback 的历史记录继续按 NULL `outstanding_cost_cny` 兼容旧语义；新运行状态明确后，Usage 只替代已结算分项，还可能被上游计费的主/备失败分项继续由 `outstanding_cost_cny` 保守计入 spent。
- `PATCH /budgets/{id}/mode` 仅 admin 可切换 `block/alert_only`；提额继续使用 `/budgets`，两条路径都按登录态 tenant_id 校验并事务化释放 block。
- 提额、切换仅告警、停用规则或进入新业务日会写 `budget.unblocked` 事件；下一请求直接读取权威数据库并立即恢复，无需重启。
- 控制台显示阻断时间和生命周期 ID，developer 只读、viewer 隐藏；前端权限呈现不能替代后端 RBAC。
- BudgetBlock 与事件不保存 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key；PricePlan/PriceSnapshot 负责形成 Usage 成本证据，预算权威消费只取正式 `finops_ledger_entries` 的 `posted usage_charge/debit` 并叠加 outstanding reservation。
- 流式调用会解析 Provider usage chunk；缺失或中断时仅按字符计数写估算 Usage。账本暂时失败时 reservation 继续保守占用，避免成本从预算口径消失。

M0.5 不实现提额审批、自动低价模型降级、复杂预算快照分发或同步阻塞数据面热路径的 Webhook 投递。

## 非流式备用模型 Fallback 边界

PRD-036 在模型别名最小路由和 Provider 健康预检之后增加请求期一次性容灾：

- `PATCH /model-aliases/{id}/fallback` 仅 admin 可写；developer 可从别名列表读取策略，viewer 由后端拒绝。目标模型必须属于当前租户、处于 active 且不能与主模型相同。
- 只有非流式请求的受控 `timeout`、HTTP 429 和 HTTP 5xx 可触发备用模型；DNS、TLS、本地参数、鉴权、预算、DLP 等错误不会重试，流式请求绝不在输出中途切换模型。
- 每个非流式请求最多两次上游调用。每次网络调用前必须先创建 `routing_attempts.started` 审计记录；第二次闸门失败时禁止调用 Provider，避免产生无法追踪的流量和重试风暴。
- `request_logs` 保存是否触发、attempt 数量和最终 attempt；成功 Usage 关联最终 attempt 和最终模型。上游失败且没有 usage 时记录 `unavailable`，不伪造 0 Token 或 0 成本。
- 预算准入按主模型与备用模型最坏成本之和预留；流式请求仅预留初始路由。主请求可能已被 Provider 计费，因此不能只按最终成功模型预留；主失败备成功时保留主估算为 outstanding，双上游失败保留主备总额，Provider 前本地失败则为 0。
- `route.fallback_triggered` 使用稳定事件 ID 投递到有界后台队列，Webhook 最多重试三次；模型请求热路径不等待接收端 HTTP，事件和日志不包含 Provider Key、Authorization、Prompt 或 Response。
- 控制台模型别名页为 admin 提供策略编辑器，developer 只读；调用日志详情按顺序展示主/备 attempt、触发原因、最终模型和用量可用状态。移动端展开表单进入文档流，保存按钮可完整滚动访问。

M0.5 不实现三层以上候选链、流式中途切换、跨地域重放、任意表达式策略或自动选择最低价模型。

## Python SDK 示例边界

PRD-038 在现有 OpenAI-compatible API 和 5 分钟快速开始页之上提供可安装、可下载的 Python 示例项目：

- 示例源码位于 `examples/python`，使用官方 `openai` Python 客户端，不创建 MetaHub 私有协议或完整大 SDK。
- `METAHUB_BASE_URL`、`METAHUB_VIRTUAL_KEY`、Chat/Embeddings 模型、超时和有限重试全部通过环境变量配置；Virtual Key 没有默认值且不会被打印。
- `python -m metahub_gateway_examples.chat` 和 `python -m metahub_gateway_examples.embeddings` 分别验证 Chat 与 Embeddings；成功和失败都展示可用于调用日志排查的 `request_id`。
- `GET /developer/sdk-examples/python` 返回当前租户的安全元数据；`GET /developer/sdk-examples/python/download` 只打包固定白名单文件，并记录带 `tenant_id` 的 `sdk.example_downloaded` 事件。
- 控制台“SDK 示例”页仅对 admin/developer 展示，后端对元数据和下载接口再次执行 RBAC；viewer 手写请求也会被拒绝。
- ZIP、事件、日志和页面不包含 Provider Key、Virtual Key 明文、Authorization、Prompt、Response、数据库路径或任意服务端文件。
- 示例调用仍经过 Gateway，因此成功用量与成本进入 `usage_ledger`，失败请求进入 `request_logs` 或结构化错误日志。

本 PRD 不做流式 Python 示例、预算专用客户端、自动配置生成器、PyPI 正式发布或自研 SDK 生命周期；这些能力按后续 PRD 独立演进。

## JavaScript/Node 示例边界

PRD-039 在 Python 制品基础上补充 Node.js 服务端示例，不把长期 Virtual Key 带入浏览器：

- 示例源码位于 `examples/js`，运行基线为 Node.js 22+，使用锁定的官方 `openai` 客户端和 OpenAI-compatible Chat Completions。
- `.env.example` 只提供公开占位；开发者在本机创建 `.env` 并填写 Virtual Key，真实 `.env`、Key、Prompt 和模型输出不会进入 Git、ZIP、事件或日志。
- `src/chat.js` 验证非流式调用；`src/stream.js` 使用异步迭代逐段输出 SSE，并通过额外总时限避免响应头后永久停滞。
- CLI 安全处理 `requestID`、`request_id`、响应头和流握手 request_id；未知异常只输出固定错误码，不打印 message、stack、Authorization 或请求正文。
- `GET /developer/sdk-examples/javascript` 返回当前租户元数据；下载接口只打包固定白名单，并复用带 `tenant_id` 的 `sdk.example_downloaded` 事件。
- 控制台 SDK 页面使用 Python/JavaScript 分段视图；后端对两种语言的元数据和下载接口都执行 admin/developer RBAC。
- 示例明确拒绝浏览器运行，不设置 `dangerouslyAllowBrowser`，页面不提供浏览器直连代码或 Virtual Key 输入框。
- Node 调用仍经过 Gateway 的路由、DLP、调用日志、Usage Ledger 和成本追踪，不旁路企业治理链路。

本 PRD 不发布完整 npm SDK、不支持浏览器长期凭据、不增加私有协议，也不实现预算/Trace 专用客户端或 Debug Center 自动跳转。

## 从直连模型厂商迁移指南边界

PRD-040 在开发者控制台提供从模型厂商直连迁移到 MetaHub 的可执行指南：

- 首版只保证服务端 Python 和 Node.js 使用 OpenAI Chat Completions 的非流式、流式示例，不承诺厂商专有 SDK、Responses API、Assistants API 或浏览器直连。
- 迁移只替换完整的 `base_url/api_key/model` 配置三元组；`model` 必须使用当前租户、当前 Environment 下可用的 MetaHub 模型别名。
- Gateway `/v1` 地址来自后端运行配置并经过 URL 规范化；页面不使用硬编码生产地址，不允许复制带凭据、query、fragment 或非法路径的 URL。
- `migration.guide_viewed` 复用 `POST /console/events`，租户、用户和会话身份来自后端认证上下文；事件不保存 Provider Key、Virtual Key、Prompt、源码或响应正文。
- 回滚只适用于配置、兼容性或 MetaHub 技术故障，并要求审批、等价治理、凭据隔离和完整配置原子切换；预算、DLP、限流或安全策略阻断不得自动回直连。
- 页面不读取客户仓库、不扫描代码、不自动修改项目，也不进入 Gateway 数据面热路径。

详细设计见 [PRD-040 设计文档](docs/prd/PRD-040-direct-provider-migration-guide.md)，测试记录见 [PRD-040 测试记录](docs/testing/PRD-040-tests.md)。

## 竞品定位与能力映射边界

PRD-041 在帮助中心提供可追溯的中立定位和当前实现映射：

- OpenRouter 和硅基流动是对标、竞品和迁移兼容对象，不是模型厂商，也不是 MetaHub 默认上游。
- 页面按 2026-07-15 的官方资料说明公开关注点，不声称竞品缺少治理、审计、私有部署或成本能力。
- MetaHub 映射入口只来自后端按当前角色下发的可见导航，前端静态页面不能恢复被 RBAC 隐藏的入口。
- `help.competitor_viewed` 复用控制台事件 API，tenant/user/session 来自认证上下文；失败不阻断阅读且同一次访问不重试。
- Provider 类型继续使用模型厂商、云 Endpoint 或通用 `migration_compatible` 白名单，不增加竞品品牌枚举或默认路由。

## FinOps Ledger 正式账本边界

PRD-051 已完成在既有 Usage Ledger 之上增加正式、不可变的财务分录层：

- 每条 Usage 只生成一条 `usage_charge` 正式分录，并绑定当前租户、来源 `request_id/trace_id`、价格快照、价格版本和证据状态；历史仅有金额的记录明确标为 `legacy_amount_only`，不得用当前价格回算。
- Gateway 主库写入失败时把冻结的安全 Usage 与价格证据同步写入本机 HMAC/fsync spool；恢复按 tenant + Usage ID 验真和幂等提交，只为本次新建分录投递一次 `ledger.entry_created`。
- `GET /finops/ledger` 和 `POST /finops/adjustments` 只允许 admin，tenant 和 actor 仅从 Session 恢复；developer/viewer 手写请求也由后端拒绝，跨租户目标与不存在目标保持同形 404。
- 人工调整支持赠送、试用、手工借记、手工贷记和冲正；金额使用六位小数字符串，冲正累计不得超过原分录，调整、分录和管理审计在同一事务提交。
- PAGE-074/075 提供筛选、签名游标分页、汇总、详情、调整和冲正；浏览器不保存幂等键、operation ID、Provider Key、Token、Prompt、Response 或自由文本原因到 URL/Storage/验收制品。
- 0041 负责历史回填、运行时自动投影、复合租户所有权、append-only/REPLACE 防护和有业务事实时的回滚拒绝；SQLite 仍是当前本地验收基线。

设计与两轮自审见 [PRD-051 设计文档](docs/prd/PRD-051-finops-ledger.md)，公开合同见 [FinOps Ledger API](docs/api/finops-ledger.md)，最终测试记录见 [PRD-051 测试记录](docs/testing/PRD-051-tests.md)；权威 Browser 与停服终态证据位于 `artifacts/prd051/20260727T043500-browser-success-02`。

### 2026-09-14 当前 PRD-148 复核

2026-09-14 复核：formal/formal2/formal3/formal4-isolated 观察均已因 Control 超时或 SQLite 调度器争用失败，不能引用；共享 SQLite 连接现按库一次启用 WAL，新的 scheduler-enabled 120 秒诊断 704 请求、0 错误，但它不是 72 小时证据。PRD-095 session P95 仍有 61.6ms 的真实门槛失败，未放宽。最终源码五域矩阵 Platform 38/38、Workspace 44/44、Partner/Support 16 通过加 1 个退役入口跳过、Device 14/14；Product QA 与 Chromium FLOW 批次均通过。GA 生命周期 13/13、Gateway 协议/默认模型 26/26、跨协议员工身份与计量 1/1、成本回归 1/1、损坏价格防护 4/4 均有当前制品。已完成一条本机真实注册、组织、KIMI Provider/模型/默认路由、员工个人 KEY、Gateway 调用和撤权链路；但该组织没有套餐、订阅和权益快照，实际用量落账以 IntegrityError 延迟，付费/额度证据不足。六方会签、发布后复核、回滚及财务/隐私/安全/迁移对账仍缺真实证据，PRD-148 保持未完成。详见 [当前验收记录](docs/testing/PRD-148-current.md)。

### 2026-09-16 当前复核

PRD-148 继续保持“未完成”。当前生产 surface 摘要已更新并通过 293 assets 构建；隔离浏览器 QA 默认关闭人机挑战只用于本地测试夹具，生产策略不变。前端完整单测 2141/2141、GA Launch 14/14、Gateway 协议/默认路由 24/24 通过。公开入口审计通过，匿名登录拒绝、注册策略可读、页面布局无溢出；关闭挑战的复跑正在收尾。

此前五域结果仍按旧快照保留，源码夹具变更后必须重新绑定当前快照；Platform 38/38、Workspace 62/62、Device 14/14、Partner/Support 16 通过加 1 个退役入口 skipped 不能直接升级为本轮完整门禁。正式 formal11 以 observation_source_changed 失败（6057 秒、0 errors，要求 259200 秒），不构成 GA。真实付费/企业治理/私有 Gateway、六方会签、发布后复核、回滚、财务/隐私/安全/迁移对账、全部 QA 当前快照、Partner/Support 17/17、72 小时 scheduler-enabled 观察以及 HEAD/origin 一致性仍缺证据。当前权威索引将继续保持 `prd148_complete=false`。
2026-09-16 多角色走查补充：当前隔离 Chromium 角色审计（admin/developer/viewer）、公开入口四尺寸和 Platform 运营旅程均通过，视觉检查确认私有 Gateway/订阅空态与接入说明；Codex In-app Browser 已可用，底层 CUA API-key 会话限制单独记录。Gateway 全量组合重跑因多个失败标记且未形成终态 JUnit，按未完成记录；此前正式 72 小时观察失败、真实商业/会签/回滚/对账和 HEAD/origin 一致性仍未闭合，PRD-148 继续未完成。证据见 `outputs/prd148-ui-audit-current-20260916/summary.json`、`outputs/prd148-gateway-protocol-rerun-20260916/summary.json` 和 `outputs/prd148-roadmap-current-20260916/validation.json`。

2026-09-16 本地服务复核：`127.0.0.1:5173` 的账号页曾因控制面仅允许 `localhost:5173` 而显示无法连接；本地开发进程已重启为同时允许两个 loopback 来源，CORS 预检通过，未登录 `/identity/me` 正确返回 401，Chromium 登录表单无页面错误。当前角色审计证据见 `outputs/prd148-role-audit-current-live-20260916/prd148-role-audit-chromium-scale-1.json`；这仍是隔离本地回归，PRD-148 的真实商业、会签、发布回滚、72 小时稳定性和 HEAD/origin 门禁未完成。
路线图当前复核：主工作簿 `01_PRD总表!B60=未启动`，校验器 `251 checks passed`、`next_prd=PRD-148`；状态修正仅恢复路线图前缀契约，不代表 PRD-148 完成。当前索引为 `outputs/prd148-current-evidence-index-followup3-20260916.json`。

2026-09-17 内置浏览器治理批次：真实本地组织 owner 登录后，集中检查并调整角色权限、成本异常、
策略发布三页的术语和信息负担。受影响前端 395/395、Workspace 治理集成 1/1、lint/build 通过；
桌面与手机六次页面观察已有当前源码绑定。开发者中心导航回退、手机导航收起和直接 URL 导航后
登录状态丢失仍待处理；独立多角色完整功能走查与 GA 门槛未闭合，PRD-148 保持未完成。
范围与分类见 [治理批次记录](outputs/prd148-native-governance-20260917/summary.md)。

2026-09-17 导航批次：集中修复开发者中心展开回退、刷新覆盖手动分组、手机选页不收起导航，
并在内置浏览器复核原操作路径。受影响前端 181/181、后端导航 7/7、lint/build 通过。
内置浏览器 `reload()` 丢失 Cookie 仍未定因；API 文档与接入页模型/地址不一致、迁移页地址
不可用、访问事件 422 已合并记录，尚未修复。独立多身份完整验收及 GA 门槛仍未完成。
详见 [导航与会话批次记录](outputs/prd148-native-navigation-20260917/summary.md)。

2026-09-20 导航与 page-visit 集成回归使用独立工作区临时目录重跑为 14/14（`outputs/prd148-nav-page-current-20260920-r6.xml`）；默认 Windows pytest 临时目录拒绝访问按测试夹具/环境问题保留。真实浏览器 page-view 争用和 S1 截图超时仍未关闭，PRD-148 保持未完成。

2026-09-18 当前源码请求解释回归：17/17 通过，证据为 `outputs/prd148-current-request-explanation-regression-20260918.json`；仅覆盖隔离单元回归，PRD-148 仍未完成。
当前源码导航、Console 入口、账号/会话生命周期、迁移指南和开发者快速开始回归 118/118 通过，证据为 `outputs/prd148-current-navigation-regression-20260918.json`；仅覆盖隔离单元回归，PRD-148 仍未完成。
当前源码 Chromium 角色审计：admin、developer、viewer 各 60 页，共 180 页通过，证据为 `.artifacts/prd148-role-audit-current-20260918-r4/evidence-manifest.json`；仅覆盖隔离页面/权限/布局审计，不是原生 computer-use、真实商业验收或 GA，PRD-148 仍未完成。
当前源码域级页面审计覆盖 59 个桌面页和 10 个移动页，共 69/69 通过，证据为 `outputs/prd148-domain-ui-audit-current-20260918-r5/evidence-manifest.json`；仅覆盖只读页面/布局审计，不是完整操作链或 GA，PRD-148 仍未完成。
此前 0143 源码快照的 GA Launch 生命周期回归 14/14、Gateway 协议/默认模型/跨协议员工身份计量回归 38/38 已保留为历史证据；当前 199b 源码快照的权威 r3 结果见下文，均为隔离测试，不是实际商业或 GA 证明，PRD-148 仍未完成。
当前源码三角色 Chromium 业务旅程 FLOW-01/02/03 均通过，证据为 `.artifacts/prd148-human-journey-current-20260918-r2/evidence-manifest.json`；包含 Cookie 登录、角色操作、导航、校验、恢复和登出，但 hydration 登出只执行 developer 分支，且不构成真实商业或 GA 证明，PRD-148 仍未完成。

当前源码五域负向矩阵 r3 已完成且起止快照一致：Platform 38/38、Workspace 44/44、Partner/Support 16 passed + 1 个 PRD-087 退役路由 skipped、Device 14/14。当前源码 GA Launch 14/14；Gateway 协议、流式、默认模型和跨协议员工身份计费回归 27/27。证据分别为 `outputs/prd148-negative-current-20260918-r3/status.json`、`outputs/prd148-ga-launch-current-20260918-r3/evidence-manifest.json`、`outputs/prd148-gateway-current-20260918-r3/evidence-manifest.json`，均绑定源码 SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`。这些是隔离自动化证据，不关闭 Chromium 登出失败、真实付费/企业治理/私有 Gateway、六方会签、发布回滚、财务隐私安全迁移对账、正式 259200 秒稳定性或 HEAD/origin 门禁；PRD-148 仍未完成。

### 2026-09-18 Feedback empty-state batch

Feedback summaries now show “暂无数据”/“No data” when there are no samples, while real zero ratings and zero adoption remain visible when samples exist. The current batch is source-bound to 2688 files with SHA256 `dc503279acc4febf040a46b28b4a771a8d5a509ab53469541e4e12749be2d4d1`; request-feedback tests 9/9, lint 838 files, and the 294-asset production build passed. The frontend and control-plane production manifests now agree on asset digest `aa58650f6c0b3c7badaaa4c69f5f05cd8717f54bec32ab3187c10894518f318b`. Evidence: `outputs/prd148-feedback-ui-empty-state-20260918/evidence-manifest.json`.

PRD-148 remains incomplete. The drifted five-domain artifacts, Chromium logout timeout, open scheduler/metering failures, real paid and enterprise flows, six-party signoff, release review and rollback, reconciliation, formal 259200-second observation, and HEAD/origin consistency still require current evidence.

### 2026-09-19 page-view writer admission follow-up

The current source now bounds Workspace page-view telemetry writer admission to two seconds so a stale navigation beacon cannot hold SQLite logout or session navigation behind a long writer lock. Normal uncontended page-view writes retain the existing contract. The frontend lifecycle regression passed 2,200/2,200 and the isolated current-source Workspace page-visit integration passed 7/7 using a dedicated basetemp; the earlier Windows pytest temporary-directory `PermissionError` remains a fixture issue. Evidence: `outputs/prd148-page-visit-write-deadline-20260919.json`.

The real Chromium organization/invitation journey with two delayed `/workspace/navigation/page-views` requests remains retained as an open product failure until a production-build browser replay proves the fix. This change does not close paid entitlement, six-party signoff, release review/rollback, finance/privacy/security/migration reconciliation, scheduler-enabled 259200-second stability, or HEAD/origin consistency. PRD-148 remains incomplete.

The post-change production-build replay (`.artifacts/s1-browser-pdmjjb3s`) did not close that failure: it reached the retired-task acceptance-contract drift and still recorded delayed page-view requests (up to about 50 seconds). Evidence is summarized in `outputs/prd148-page-visit-write-deadline-browser-rerun-20260919.json`; the runtime issue remains open.

A second production-build replay (`.artifacts/s1-browser-7dz4v8if`) completed the previously failing invited-account subflow: invitation acceptance, Workspace session establishment, logout back to `/`, and guarded redirect to the login page all passed. The full flow still failed later while loading the owner account workspaces section, with two page-view requests pending for about 40 seconds. This is partial progress, not a complete business-flow or GA pass. Evidence: `outputs/prd148-page-visit-write-deadline-browser-rerun2-20260919.json`.
### 2026-09-17 Platform availability batch

Fixed SQLite snapshot-to-write contention for Platform session reads by re-authenticating under an immediate transaction when a session touch or authority refresh is required. The isolated concurrent regression passed 9/9, and the scheduler-enabled browser rechecked status, residency, GA gates and Workspace operations. Customer Success and privacy mixed-cookie rejection remains an explicit PRD-140 failure boundary; scheduler reconciliation warnings and the required 72-hour observation remain open. PRD-148 is not complete. Evidence: `outputs/prd148-platform-availability-diagnostic-20260917/findings.md`.

### 2026-09-18 Current-source r5/r4 verification

The current source snapshot is `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf` with source SHA256
`b567e0133e5516f3c886384c50dd249b7a9aa22cf040347554e0b7cebc4e12f4`. The unchanged-source
five-domain negative matrix passed Platform 38/38, Workspace 45/45, Partner/Support 16 passed
plus one explicitly retired PRD-087 route skipped, and Device 14/14. Current-source GA Launch
passed 14/14 and Gateway protocol/default-model/cross-protocol accounting passed 27/27.
Evidence: `outputs/prd148-negative-current-20260918-r5/`,
`outputs/prd148-ga-launch-current-20260918-r4/`, and
`outputs/prd148-gateway-current-20260918-r4/`. The initial Windows pytest temporary-directory
permission error remains classified as a fixture issue and was resolved for the isolated rerun.

These are isolated automated and synthetic-upstream results. The Chromium 1x logout timeout is
retained as a real failure; the local KIMI registration-to-revocation path is trial-only and has
no paid entitlement or settlement evidence. Six-party signoff, post-release review/rollback,
finance/privacy/security/migration reconciliation, scheduler-enabled 259200-second stability,
and HEAD/origin consistency remain open. PRD-148 remains incomplete.

2026-09-19 page-visit follow-up: current-source PRD-147 page-visit regression is 7/7 with no product
assertion failures (`outputs/prd148-page-visits-current-20260919.xml`). The real Chromium organization
journey still has two `/workspace/navigation/page-views` requests waiting about 40 seconds after the
invited account signs out; this remains an open runtime concurrency failure and the threshold was not
relaxed. A separate finance journey failure is an acceptance-script contract drift because it seeks
the retired `workspace.settings.profile` task entry; it is tracked as a fixture/contract issue. The
current evidence index remains `prd148_complete=false`.
2026-09-19 page-view 写锁修复后的第三次生产构建重放已记录：注册、组织、试用成员/Provider、账号入口及一个 page-view 200 完成；隔离 Uvicorn 随后发生 Windows access violation，浏览器上下文在导航历史阶段关闭，不能宣称完整业务链通过。page-view 集成回归 7/7，事务全路径沿用两秒截止时间。证据为 `outputs/prd148-page-visit-write-deadline-browser-rerun3-20260919.json`；PRD-148 仍未完成。

2026-09-19 员工流程后续复验：修复了员工创建成功后目录详情被折叠的问题，并将 CSV 导入文本框固定为 16px；受影响前端单测 43/43、lint 838 files 和生产构建 294 assets 通过，当前资源摘要为 `65ed5c9c8de0740b18823abd1b25c959c9b4ae4e63f42da519d01d5ff676914f`，源码 SHA256 为 `6a8396c904034006d48c792c29cbb10187f797c1a9b1d89edd4a694783199720`（2690 files）。真实浏览器复验 `.artifacts/s1-browser-69efzdeh` 已完成两次员工创建并越过原表单可见性失败，但在 CSS 修复前记录了 13.3333px 文本框门槛失败；`.artifacts/s1-browser-a58pqciu` 的账号偏好页等待超时，`.artifacts/s1-browser-3oik8m1s` 的试用成员/Provider 阶段等待 Console 超时，且运行期间仍有延迟 page-view 请求。没有清洁完整业务链通过证据，PRD-148 仍未完成。索引已更新为 `outputs/prd148-current-evidence-index-final-20260916.json`。

2026-09-20 真实员工链路复验：修复了员工 KEY 名称字段默认不可见、创建第一把 KEY 后发放面板关闭、以及员工档案折叠导致撤权控件不可操作的问题；前端全量单测 2,201/2,201、lint 838 files、生产构建 294 assets 通过，当前生产资源摘要为 `0c05a2c0972de0505e07a7a3e2e12157a844d5bec9bff8a585f383c1874dc15b`，当前源码 SHA256 为 `cf68165aabf91d3dc23485588022f406af514c50886f05f945a06227aa819eac`（2691 files）。新的真实 S1 重放 `.artifacts/s1-browser-tv64otey` 已完成注册、创建组织、Provider/订阅、邀请成员、两名员工、两把个人 KEY、Gateway 旧 KEY 失效、员工撤权和撤权后 Gateway 401；随后在账户引导导航截图阶段超时，仍伴随延迟 page-view/字体请求，不能宣称完整真实链路通过。之前失败 `.artifacts/s1-browser-hx1h_hh3`、`.artifacts/s1-browser-vb5jyu54` 保留为真实失败证据。PRD-148 继续保持未完成。

2026-09-20 当前源码分批人类式走查：admin、developer、viewer 覆盖全部 60 个 Workspace 路由，共 180/180 页通过；0 页面失败、HTTP 5xx、请求失败、控制台错误、溢出、裁切、交互重叠或无标签控件。证据目录见 `outputs/prd148-current-evidence-index-final-20260916.json` 的 `current_20260920_human_interaction_partitioned`。一次三进程并行运行在 page-view 写锁竞争下出现 503，单独重跑通过，已按运行器/并发事实保留，未升级为全量失败。

本轮为 page-view 遥测客户端增加 2500ms AbortController 上限，前端单测 2,201/2,201、lint 838 files、生产构建 294 assets 通过；当前源码 SHA256 为 `6713a21a301d9b7cfb526df7dcf5b53e98dfd79a82d869ef08a14c86d17c0392`，资源摘要为 `fc980ad1de3e46ebb7fe326205ee7c102f9c59c091c22dc08c355485f0c91c7c`。真实 S1 `.artifacts/s1-browser-gdpwx6px` 仍在 tenant 截图阶段超时并有 6 个 page-view 请求 pending，因此该修复不构成完整注册到撤权链路或 GA 通过；PRD-148 继续未完成。

同批次当前源码的人类式审计覆盖 admin/developer/viewer 共 180 页，平台运营三视口走查和 WB/API/BB/GR/SEC/PERF/WEB/UI/UCD/COMP/FLOW 全批次均通过；证据为 `.artifacts/product-qa/prd148-human-interaction-chromium-scale-1.json`、`.artifacts/product-qa/prd147-platform-chromium-scale-1.json` 和 `.artifacts/product-qa/20260919-231439/evidence-manifest.json`。这些是隔离 QA 证据，不能替代真实付费、发布会签、回滚、对账、72 小时稳定性或 HEAD/origin 一致性。

严格角色导航审计另有当前源码报告 `.artifacts/product-qa/prd148-role-audit-chromium-scale-1.json`：admin/developer/viewer 共 180 页、47 个声明导航入口，权限回退、请求、布局和交互检查全部通过。该报告不关闭真实商业链路、发布回滚、对账、scheduler-enabled 259200 秒观察或 HEAD/origin 门禁。

2026-09-20 资源摘要重绑定：当前源码快照为 2691 个受测文件，SHA256 `4038c1faeea4ac1ae09c2eb55124ff1a2899302f6e9f541d33bf75f5ee59839a`。生产 surface 校验、`npm run build`（294 assets）和 `npm run lint`（838 files）通过，实际资源摘要为 `f6fee31bb171fee0e319fc134689bd5bd2ac75f80bfd36ebb754a6812b62230c`。证据为 `outputs/prd148-source-current-20260920.json` 与 `outputs/prd148-production-build-current-20260920.json`。

同一源码的新 S1 真实浏览器复验 `.artifacts/s1-browser-z1pzaatf` 已完成注册、创建组织、Provider/订阅、邀请成员、两名员工、两把个人 KEY、Gateway 调用、旧 KEY 失效和员工撤权后的 Gateway 401；在账户引导 `tenant` 页面截图时 `page.screenshot` 12 秒超时，仍有 6 个 page-view 请求未收敛（最长约 110 秒）。该批次是部分成功的真实失败，不能作为完整业务链或 GA 通过。详见 `outputs/prd148-real-s1-rerun-20260920.json`；PRD-148 保持未完成。

2026-09-20 当前 Gateway/GA 复验：GA Launch 集成 14/14，协议门面/流式/默认模型单元 26/26；跨协议员工身份与计量集成在本机超过历史可控时限且没有终态 JUnit，已按未完成记录于 `outputs/prd148-gateway-integration-attempt-20260920.json`。单元结果见 `outputs/prd148-gateway-unit-current-20260920.xml`，不替代跨协议集成、真实 Provider 或商业 GA 证据。当前源码快照 SHA256 `4a929e115f245d46716e932364d68522431939f93c0644916b84fcdd63706b7b`；PRD-148 继续未完成。

2026-09-20 Partner 当前页面边界复验：隔离 Chromium 390x844 审计在无 ChannelMembership 账号下连续两次收到 `403 partner.membership_required`，Partner 页面显示访问已撤销，且无溢出、裁切、交互重叠或应用控制台错误。证据为 `.artifacts/product-qa/20260920-partner-r6/prd148-partner-audit-chromium-scale-1.json`。旧 PRD-087 浏览器验收仍受 0080→0155 迁移基线漂移阻断；该页面边界证据不代表有成员数据投影、付费结算、发布会签、回滚、对账、正式 259200 秒稳定性或 GA 通过，PRD-148 保持未完成。

### 2026-09-20 当前源码验收更新

**最新复核（优先于下方历史批次结论）**：有数据走查发现 Support/Device 业务页仅有外壳，Partner 入口缺少正式跨域切换。Support 已接入正式 Workspace 会话并通过管理员/开发者创建、回复、升级、解决、评价、重开、刷新持久化及查看者写入 403；当前 r6 已验证三角色 handoff 200 及随后预期 `403 partner.membership_required`，但 Device 仍无真实 Deployment 数据，完整交互/全量 QA 未闭合。旧 human-r4 的 `passed=true` 漏算断言，不能作为完整交互通过证据；Support/Device 后端 24/24 也不能替代真实浏览器工作流。见 [合并问题与验证范围](docs/testing/PRD-148-populated-domain-audit-20260920.md) 和 `outputs/prd148-current-evidence-index-final-20260916.json`。PRD-148 继续未完成，正式 72 小时观察未启动。

2026-09-20 Partner 跨域入口已增加显式服务端 handoff：Workspace 账户菜单调用 `/identity/partner-handoffs`，服务端校验同一 GlobalSession、撤销 WorkspaceSession 并清理 Workspace cookies，Partner API 仍拒绝混合凭据。前端 Workspace 授权回归 12/12；真实三角色入口复验、成员投影和商业验收仍未完成。

当前源码快照 `outputs/prd148-source-current-20260920-r3.json` 为 2691 个文件、SHA256 `6713a21a301d9b7cfb526df7dcf5b53e98dfd79a82d869ef08a14c86d17c0392`；生产构建为 294 assets，资源摘要 `fc980ad1de3e46ebb7fe326205ee7c102f9c59c091c22dc08c355485f0c91c7c`。admin/developer/viewer 分批 Workspace 走查 180/180 页通过，page-visit 集成 7/7 通过。

真实 S1 重放 `.artifacts/s1-browser-_dluz246` 已完成注册、创建组织、Provider/订阅、邀请成员、两名员工、个人 KEY、Gateway 调用、轮换、撤权及撤权后 401；在 `1280.onboarding-navigation.tenant` 截图阶段等待字体超时，6 个 page-view 请求仍 pending，故完整真实链路不通过。五域矩阵虽然有 113/113 通过，但起止源码漂移，不能绑定当前源码。真实付费/额度、企业治理/私有 Gateway、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致仍未闭合，PRD-148 保持未完成。证据索引为 `outputs/prd148-current-evidence-index-final-20260916.json`。
2026-09-21 OpenRouter 对标后的三角色人类式复验：owner/admin、developer、viewer/employee 各 60 个 Workspace 路由均通过，0 请求/响应错误、0 控制台错误、0 布局问题、0 交互失败。合并审计记录见 [PRD-148 OpenRouter 人类走查](docs/testing/PRD-148-openrouter-human-audit-20260921.md) 和 `outputs/prd148-openrouter-human-audit-20260921.json`。运行器固定文件名导致后一次角色覆盖原始 JSON，已在证据中明确，不把覆盖文件当作三份独立原始证据。该批仍是隔离 seeded QA；真实注册到付费、企业治理/私有 Gateway、Partner/Device 数据路径、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-21 OpenRouter 对标第五批：管理员默认侧栏已从五组收敛为四个工作域（Workspace、Gateway、Finance、Governance）；任务首页仍是首登落点，员工仍只获得统一 Gateway 地址和个人 MetaHub KEY，所有授权深链保持可用。导航/壳层/产品 QA 契约 122/122、lint 838 files、生产构建 294 assets/276 modules 通过；当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-four-domains.json`，SHA256 `e02776d1aed45aa4d2b588d7de9342de152e99cdaebbe969d2f71e222b8b8ed7`，资源摘要 `23a54e3a40411c44732c5cb6c051cf1857e116c6d99784165bf6363c70011db4`。本批隔离 Chromium 运行器在数据库准备阶段挂起，未形成浏览器结论；真实商业/付费、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。
2026-09-21 OpenRouter 对标第九批：管理员接入首页改为首屏只显示一个当前可执行的下一步，完整 Provider → 模型 → 员工 KEY → 首次调用步骤移入“查看全部接入步骤”折叠区；已完成步骤仍可查看配置，阻塞和等待状态仍按后端事实显示。前端全量单测 2221/2221、lint 838 files、生产构建 294 assets/276 modules 通过；当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-next-step.json`，SHA256 `9ed6b8f3292234e47baa21163ca33660a25b894e65df43e5b1d013b1621c8189`，资源摘要 `128e057623923ff796ac68582b3dce416d17e5e67068d633f097034091c72099`。这只完成 OpenRouter 信息架构收敛，不构成真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 的完成证据；PRD-148 继续未完成。

2026-09-21 OpenRouter 对标第十三批：`en-US` 的角色权限矩阵、Webhook 设置和 Partner 切换失败提示已统一 locale；已知角色 code 即使服务端返回中文名称也使用英文投影，Webhook 的 CEF/JSON、事件白名单、一次性密钥和 RBAC 契约保持不变。定向 permissions/Webhook 25/25、前端全量 2226/2226、lint 838 files、build 294 assets/276 modules 通过；构建先发现旧资源摘要漂移，更新前后端 manifest 后对账通过。当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-role-webhook-i18n-final.json`，SHA256 `fab0506baa4c5e4c9b1c26671b22f62370a4d9549665017de8142134cb75e71f`，资源摘要 `9bda09855a4020b9b40981c305f60818b6354cf9af383120112161348bc0b685`。本批仍缺新的英文高级页面浏览器终态、真实商业链路、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin；PRD-148 保持未完成。
2026-09-22 OpenRouter 对标第十四批：旧 Provider、模型、预算、治理、审计、可观测性、支持和开发者中心渲染器的英文静态文案已在授权内容汇聚层统一投影。投影仅对 `en-US` 的登记静态词汇生效，中文默认、用户/Provider/模型数据、API 字段、错误码、RBAC 和员工统一 Gateway/个人 KEY 合同保持不变。静态投影 2/2、权限/Webhook 定向 27/27、前端全量 2228/2228、lint 841 files、build 295 assets/277 modules 通过；前后端生产资源摘要统一为 `1cceebf6bf335d5d9fab7967b56dbe3019c07b92d7792b5193a89a6e95896bcb`。当前源码快照为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，2696 个文件，SHA256 `f735cd8a766775b8bfba39234cf1d1cbbb9384d8e02259c3b2b91d0441550d75`。随后 admin 英文隔离 Chromium 分区 10/10 通过且 textSample CJK 字符为 0；该证据不替代完整高级页面、真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin，PRD-148 保持未完成。

2026-09-22 OpenRouter 对标第十五批：异步 hydration 路径已统一接入英文静态文案投影；当前源码分区 Chromium 复验为 admin 57/57、developer 57/57、viewer 60/60，全部无请求、响应、控制台、布局、动作或授权问题。developer/viewer 使用 `zh-CN`，中文 textSample 属预期 locale；admin 三批 `en-US` CJK 样本为 0。当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，2696 个文件，SHA256 `32c39158491e80b47ae38ca0cacb93b728b3948509fa51eaedce053b2bee4c1f`；前后端生产资源摘要为 `9112943188353389eec9f1807c3c7de7680e7b30073fb84be01c923e39f83e1e`。前端全量 2228/2228、lint 841 files、build 295 assets/277 modules 和摘要契约通过。分区 seeded 证据不替代统一 180 页运行器、真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin；PRD-148 继续未完成。

2026-09-22 全角色与有数据域复验：平台运营三视口通过；admin/developer/viewer 授权审计按角色分片各 60/60，通过且无请求、响应、控制台、布局或授权问题。Support admin/developer 创建、回复、升级、解决、评价、重开和刷新持久化通过，viewer 写入 403；Device 创建 201 且刷新可见；Partner handoff 200、归属投影 200。正向脚本已将 License 未签发 404、域切换 401 和导航卸载取消单独分类，意外网络/控制台问题为 0。三角色统一授权批次的 300 秒超时仍保留为运行器容量/环境问题。证据目录为 `.artifacts/product-qa/20260922-{platform-operator-current,role-audit-admin,role-audit-developer,role-audit-viewer,populated-domains-current,positive-domains-classified}`。这些是隔离 seeded 证据，真实设备交付、企业治理/私有 Gateway、商业额度、六方会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-22 OpenRouter 对标第十七批：修复 Support 和 Private Gateway 异步 hydration 绕过英文静态文案投影的问题；新增异步面板断言 1/1，前端全量 2229/2229，lint 841 files，build 295 assets/277 modules。当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `b1f008bbbc5b02f28138fb53b669cc2fdcc4e61434a6943b933fafc2b1f5ff5e`；前后端生产资源摘要为 `5f44508358f909fd368442bf806f45d1f1157ff9af68e35ca5536485fd638e99`。构建首次发现旧摘要漂移，已同步 manifest 后通过，按发布资源对账问题保留。该批只证明当前源码和隔离回归，真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-22 异步面板三角色复验：Support 与 Private Gateway 按 admin、developer、viewer/employee 各复验 2 页，全部通过，0 请求/响应/控制台/布局/交互/授权问题。证据目录为 `.artifacts/product-qa/20260922-post-i18n-{admin-r2,developer,viewer}`。第一次 route 参数错误在浏览器启动前失败，已保留为测试夹具问题。该批仍是隔离 seeded 证据，不替代真实注册到付费、企业治理/私有 Gateway、生产 Partner/Device、六方会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin。

S1 当前真实链路仍未重新启动：历史批次在 `1280.onboarding-navigation.tenant` 截图等待字体阶段失败；脚本要求的迁移基线 SHA256 `062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a` 在 `.tmp` 中缺失，现存候选不匹配。该项按迁移基线缺失/证据不足保留，不放宽校验，也不把注册到撤权的部分成功写成完整通过。

2026-09-22 OpenRouter 对标第十八批：管理员工作台去掉四个域卡外层的大边框，改为无框内容带，保留四个可操作域卡和全部授权/深链，减少“卡中卡”的视觉重量。前端全量回归 2229/2229、lint 841 files、build 295 assets/277 modules 和前后端摘要契约通过；当前源码快照为 `outputs/prd148-source-current-20260922-openrouter-card-band.json`，生产资源摘要 `72d8c6314c1b3cb2621b336b27762f1220ed1b72902658beddcd7669e36b26b3`。此前 Gateway 402、Quota 402 和流式成本断言均确认是零费率迁移夹具漂移，已改为显式记录当前边界；当前源码绑定为 `outputs/prd148-source-current-20260922-gateway-fixture-reconciled-r4.json`，SHA256 `ceb9be5281b8bcf3419f12cc0714540d4bec1232c029cbb34ab30fff2547394b`。OpenAI 兼容 30 个用例由串行前段、三个分片和三个边界终态聚合，证据索引见 OpenRouter 差距文档；真实商业链路、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 继续未完成。

2026-09-22 OpenRouter 对标第二十批：预算页英文静态文案、壳层 More controls 和表单占位符完成收口；中文默认界面与组织/环境等用户数据保持不变。前端全量 2230/2230、lint 841 files、build 295 assets/277 modules，资源摘要 `d5accfb3e166e8f4c3f021263a2c25e40a1a5f09db31af87759c7b76e1e0aeee`；当前源码证据 `outputs/prd148-source-current-20260922-openrouter-budget-locale.json`，SHA256 `0b9a8c6f127d9fb2ae7af1aca7e0b9e97a4d2ec255e99bd25cdd0a2505cd27ea`。管理员预算/Webhook/权限/FinOps 分片 4/4 通过，证据 `.artifacts/product-qa/20260922-targeted-admin-role-audit-r4/prd148-role-audit-chromium-scale-1.json`。这仍是隔离 seeded UX 证据；真实商业链路、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 未闭合，PRD-148 保持未完成。

2026-09-22 OpenRouter 对标第二十一批：修复 Webhook 事件选择器滚动区与保存按钮的真实几何重叠。admin/developer 当前分片均为 0 overlap、0 clipping、0 overflow、0 请求/响应/控制台错误；旧失败证据保留，当前源码证据为 `outputs/prd148-source-current-20260922-webhook-layout-fix.json`，SHA256 `6d9d675782de61e5248ed328c31311e4675a710cd9c405fd6272669932e7d40a`，资源摘要 `3a97495ab2596d0b8cdd15b5251a0ebabc93727a794219d4390e8051b5727a22`。该批仍是隔离角色 UX 验收，不代表真实商业或 GA 完成。

2026-09-22 平台运营与组织管理员分域复核：SaaS 平台运营使用独立 Platform session，隔离 Chromium 的控制台迁移路径在 1440x900、1280x720、390x844 三视口通过，迁移状态 2/2 为 200，0 请求失败、0 控制台错误；证据为 `.artifacts/product-qa/20260922-platform-r5/prd147-platform-chromium-scale-1.json`。Workspace 组织管理员、developer 和 viewer/employee 继续使用独立 Workspace session，相关页面和关键交互证据不与平台运营证据合并。证据索引为 `outputs/prd148-human-audit-batch-current-20260922.json`，详细说明见 [平台运营与组织管理员分域复核](docs/testing/PRD-148-human-audit-20260922-platform-separation.md)。用户明确不执行六方会签和 scheduler-enabled 259200 秒观察，两项不计证据；真实商业链路、生产 Partner/Device、发布复核/回滚、对账和 HEAD/origin 仍未闭合，PRD-148 保持未完成。
2026-09-22 Support/Private Gateway locale 收口：英文静态投影中的混合中文短语已通过 `localizeConsoleStaticHtml` 统一修复。当前源码证据为 `outputs/prd148-source-current-20260922-support-locale-final.json`，SHA256 `d0c39d15adf763d2ceea482cbd2ee11f4b9a4515d9cc8c429ad2c3a25a678cee`；前端全量 2234/2234、lint 841 files、build 295 assets/277 modules，前后端生产资源摘要统一为 `be93bbfd03b244a16c7483c7caf8f428ec9f59733efdea074e87a81004884069`。SaaS 平台运营管理员仍只使用 Platform session，Workspace 组织管理员、Developer 和员工仍只使用 Workspace session；本批没有把两类证据合并，也没有把 locale 源码回归写成新的完整浏览器 GA 证明。真实商业链路、发布复核/回滚、对账和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

2026-09-23 多身份走查：当前源码按四个分片完成 Workspace admin、developer、viewer/employee 各 60 个注册路由，共 180 个角色与路由组合；页面、请求、响应、控制台、溢出、裁切、交互重叠、未结算响应和禁止内容均为 0。SaaS Platform session 的平台运营迁移路径与三视口独立通过；Support 生命周期、viewer 写入拒绝、Device viewer 拒绝与 Partner handoff 也按合同通过。冷启动 Control 服务超过旧 35 秒就绪窗口被归为运行器问题，运行器现在使用可配置、默认 90 秒的服务就绪等待，业务与浏览器断言未放宽。证据为 `outputs/prd148-human-audit-current-20260923.json` 与 [多身份走查记录](docs/testing/PRD-148-human-audit-20260923.md)。Platform 运营证据与 Workspace 组织证据严格分开；这仍是隔离 seeded 回归，PRD-148 未完成。

2026-09-23 客户技术试用交付：可安排明早在本机或另一台已实际验证连通的同网设备上进行受控中文技术试用。试用使用健康的 `default` Workspace 路由；组织管理员配置 Provider、模型和默认路由，员工只接收一个统一 Gateway 地址和一把个人 MetaHub KEY。Private Gateway 页面已把低频“运行与交付”和“维护与治理”操作折叠，核心客户任务三角色交互 24/24 通过；当前源码绑定为 `outputs/prd148-source-current-20260923-private-gateway-layering-r2.json`，2698 文件，SHA256 `d3e2954e9e51746973f0d974889de2f97f69eba364f11b9adcdc81d26e6594b5`。本地数据库的订阅和权益快照均为 0，计量 outbox 仍有 4 条 pending 和 1 条 `platform_metering.owner_invalid`，所以不得承诺付费额度、结算、财务对账、SLA、公网/TLS 或 GA。交付步骤与证据边界见 [客户技术试用交接](docs/testing/PRD-148-customer-trial-handoff-20260923.md)；PRD-148 保持未完成。

2026-09-23 Private Gateway 安装器二次收敛：截图走查确认部署选择、步骤、下载与六项机器要求同时平铺，进一步增加首次配置的扫描负担。现将低频环境规格收进默认闭合的“环境要求”，保留部署、版本、规格、架构、进度和签名下载的直接可见性；安装 API、权限、下载和验证流程未变。定向单测 12/12、lint 844 files、生产构建 296 assets、Control manifest 对账 8/8，以及隔离 Device/Partner Chromium 正向流程均通过；后者还确认环境要求可展开和收起，Device 创建 201、一次性材料和刷新持久化继续有效，布局与意外浏览器错误均为 0。当前源码快照为 `outputs/prd148-source-current-20260923-installer-density-r3.json`，SHA256 `107409ad5ec76a7bb2a9d2c236fbbe53eb2ad08f9f77c350150db9ede28145ee`，资源摘要 `dbf35088bbe1dd22faa4813e89679c03d30ac9629bcb3e5e3417dd533bf4e224`。本批仅是隔离 seeded UI 证据，不改变试用的商业与 GA 边界；PRD-148 保持未完成。

 2026-09-23 当前源码 S1 浏览器重放：隔离 SQLite 上的注册、组织、邀请成员、Workspace 会话、Provider 渐进配置视图、员工个人 KEY 创建/轮换和员工停用均执行成功，且 15 个已保存页面无溢出或小输入字体问题。随后在 `1280.onboarding-navigation.tenant` 截图捕获阶段超时；页面已完成、字体已加载、没有待处理资源或请求，但完整流程仍为失败，不能把部分成功改写为 S1 通过。没有修改超时或放宽断言，当前按未解决浏览器捕获失败保留。它只使用本地 feedback 契约和 seeded Provider catalog，不是 KIMI、商业额度、付费权益或 GA 证据。索引为 `outputs/prd148-s1-browser-current-20260923.json`；PRD-148 保持未完成。  2026-09-23 S1 双视口复核随后在相同业务与截图阈值下通过：1280x900 和 390x844 共 18 个注册、组织、邀请、Workspace 会话、Provider 视图、员工个人 KEY、停用、onboarding 和 SDK 下载步骤，以及 32 个布局视图，页面错误、资源错误、外部请求、溢出和小输入字体问题均为 0。终态为 `.artifacts/s1-browser-fdoyqs67/acceptance.json`，摘要为 `outputs/prd148-s1-browser-current-20260923-r2.json`，绑定源码 SHA256 `3051b71e8d625881976665897280fe3f1e9ea01af8f91272d978b7f2e99515d6`。历史截图超时仍保留为历史失败；本流仍只使用 seeded Provider catalog 和本地 feedback 契约，不证明 KIMI、付费权益、商业计量或 GA，PRD-148 保持未完成。
