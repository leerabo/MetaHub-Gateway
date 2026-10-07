# OpenRouter 与 MetaHub 体验差距分析

日期：2026-09-21（持续更新）

## 2026-09-28 模型服务添加入口收口

“自定义”不再是已接入服务页里单独的一套表单，而是“添加模型服务”目录中的一个选项。组织管理员选择它后才看到服务名称、地址和按需展开的协议/认证设置，提交继续走现有 Provider 创建 API。

厂家列表属于 SaaS 平台管理员的签名 Provider Catalog。组织管理员不维护全局厂家，只选择已发布服务并填写本组织持有的 Provider API KEY。当前目录没有 OpenAI/GPT、xAI/Grok 或 Anthropic/Claude 的可用条目时，组织可以先用自定义 OpenAI 兼容入口；GPT/Grok 需要先确认真实兼容端点并由 SaaS 管理员发布，Claude 需要真实 Anthropic 上游 Adapter，不能把 Gateway `/messages` 客户端协议入口当成上游适配完成。

当前变更证据：`outputs/prd148-provider-catalog-custom-r2.json`，受影响前端测试 `98/98`、lint `842 files`、生产构建 `295 assets`，资源摘要 `332e4e48e878f7571ee9d5a3fdb4c24315d7dd907f71dd3a52cb02fbd9bfa9a2`。控制面 pytest 本轮在默认 Windows 临时目录权限错误处阻断，未进入 Provider 断言；该问题按测试夹具/环境失败记录，不作为产品通过或失败。

## 2026-09-28 当前源码视觉与信息架构复核

当前源码 revision 为 `6f36c2422a916a84ead01f6571a5158e253f7c15`。管理员英文、中文管理员、开发者和移动查看者各完成 60 页截图，共 240 张；截图文件、PNG 签名、尺寸和每角色路由唯一性均通过，接触表复核也未发现新的层级、空状态、响应式布局或职责回退问题。证据为 `outputs/prd148-human-visual-review-20260928-r1.json`、`outputs/prd148-screenshot-evidence-integrity-20260928-r1.json` 和 `.artifacts/product-qa/20260928-human-visual-contact-sheets-r1/`。

当前 Workspace 路由注册表为 60 条，无重复 route code 或 source target。当前 seeded 角色审计的授权入口/默认侧栏/延后入口数量为：管理员 `45/12/17`，开发者 `27/9/9`，查看者 `3/3/0`；无权深链产生的重复画面是职责回退，不是重复注册。三角色每个 60/60 路由的控件、按钮和链接交互均无未标注输入、请求/响应错误、裁切或交互重叠，汇总见 `outputs/prd148-interactive-control-coverage-20260928-r1.json`。

这说明当前版本已经收窄了首层认知负担，但能力总量仍远大于 OpenRouter：管理员仍有 45 个授权深链，应继续通过任务首页、状态卡和上下文入口承载，而不是全部恢复为默认菜单。当前证据仍是隔离 seeded Chromium 与源码复核，不等同于真实客户验收或 GA。

## 2026-09-26 当前源码 r50 全页面复验

本轮把“简单、容易找到下一步”扩展为逐页截图和可逆操作检查。管理员、开发者、查看者各 60 页，共 180/180 页面截图通过；当前源码页面、授权回退、请求、响应、控制台、溢出、裁切和重叠问题均为 0。三角色可逆鼠标/键盘交互各 60/60 通过，动作、标签、权限和布局问题均为 0；中文管理员 `zh-CN` 另完成 60/60 截图视觉审计。证据索引为 `outputs/prd148-current-evidence-index-final-20260926.json`，当前源码 SHA256 `31689646d3fe4eaf11a6d615d6b1766e7b1e8b0a23796fac68793f37fd949194`。

本轮没有发现需要追加代码修复的页面缺陷。三角色合并交互批次曾因测试运行器 900 秒限制超时，不能算通过；按角色拆分后完整 180 页通过，夹具超时作为失败保留。截图和交互均是隔离 seeded Chromium 证据，不是原生 computer-use、真实外部账号或 GA。付费模块延期，72 小时观察与六方会签不执行，`prd148_complete=false`、`ga_approval=false` 保持。

## 2026-09-26 中文用户使用容易度 r44 复验

本轮把 OpenRouter 对比落到具体任务完成上：用户应先看到当前状态和下一步，低频设置默认收起，列表操作必须在当前行内可见。当前源码 r44 的管理员、开发者和查看者关键路径交互分别为 11/11、6/6、6/6；r43 的三角色 180 页截图作为前一源码快照的全路由历史基线保留。视觉抽查发现 MetaHub 组织 KEY 表格的操作列因最小列宽总和过大被挤出，管理员无法一眼判断轮换和停用按钮；收紧信息列并固定操作区后，1440px 桌面按钮完整可见，平板/移动仍按两列/单列展示。

当前源码证据：`outputs/prd148-source-current-20260926-usability-r44.json`、`.artifacts/product-qa/20260926-key-fix-r44/`、`.artifacts/product-qa/20260926-usability-admin-r44/`、`.artifacts/product-qa/20260926-usability-developer-r44/`、`.artifacts/product-qa/20260926-usability-viewer-r44/`。前端全量单测 `2259/2259`、lint `842 files`、production build `295 assets` 通过。r43 的 180 页全路由截图尚未在 r44 CSS 修复后重跑，因此不作为当前源码完整截图证据；当前源码的完整 PRD-148/GA 门禁仍未通过。

## 2026-09-25 用户可读性复验补丁

易用性检查不仅看页面有没有功能，还看用户是否能在一眼内读懂数量和状态。英文管理员截图曾出现 `3 位Members`、`1 把active key(s)` 这样的混合短语，虽然不影响请求和布局，却会让用户怀疑页面仍处于半翻译状态。本批把计数器作为完整短语处理：成员显示为 `3 members`，活动 Provider KEY 显示为 `1 active key(s)`；中文默认界面保持中文。

当前证据：源码快照 `outputs/prd148-source-current-20260925-usability-r13.json`（SHA256 `83f17deff1d5f96de77a272386b8a948922dd74cf1099bd85ac7ec2478a4ed12`），本地化回归 `12/12`，前端全量 `2251/2251`，lint `842 files`，build `295 assets`；英文管理员 quickstart/providers/users 的隔离 Chromium 片段 `3/3` 通过，报告和截图在 `.artifacts/product-qa/20260925-current-r15-english-home/`。生产资源摘要 `6971d55c4757cc5706ffaab47cebea2e63efbaf738c7ecc57ff86a8857e18d84` 与前后端 manifest 一致。

这个补丁收口的是用户能否直接读懂页面的一个当前问题，不扩大真实业务验收结论。真实注册到 Gateway/计量/撤权、生产回滚与对账、HEAD/origin、正式 GA 仍没有完整当前证据，PRD-148 保持未完成。

## 2026-09-25 当前公开入口与子路径走查

本批按静态审查提出的四项问题完成了当前源码修复：删除首页永远隐藏的动画控制和无效状态；为 600px 以下首页补充可用的菜单、Escape 关闭和键盘 Enter 打开，并在打开、关闭、点击外部和恢复桌面宽度时维护焦点；将公开 HTML、动态页面链接和管理助手来源链接改为相对路径；把首页英雄区从固定高度与 `overflow:hidden` 改为内容优先的最小高度，避免横屏、低高度和系统字体放大时裁剪 CTA 或底部提示；同时修复控制台退出后跳回根路径导致子路径部署丢失的问题。

验证绑定：当前源码为 `outputs/prd148-source-current-20260925-usability-r6.json`（2703 files，SHA256 `a2e4c94ff550148ee95882d78e11d17c931aed7024716ec3e73f49eddecc2cbe`），公开 Chromium 报告为 `.artifacts/product-qa-current/20260924-public-ui/report.json`（6/6 场景通过，SHA256 `ff84d8bc530d2b76295d8a5acd70aaa840523670cc919787f0b8c0574f7ee0f5`）。前端全量单测 `2249/2249`、私有 Gateway 中文文案回归 `32/32`、`npm run lint`（842 files）、`npm run build`（295 assets）通过；后端资源清单契约通过，前后端生产资源摘要为 `c7f35fb7139e8d3412e402630964604dade1a96c9cc478fb248770a5e6301be4`。角色审计与 Platform/Support/Partner/Device 旧产物绑定较早源码，只作为历史参考，不冒充最终源码全量证据。

Playwright 走查结果：桌面 `1440x900`、移动 `390x844`、低高度 `390x480` 与真实挂载前缀 `/metahub/` 均为 0 横向溢出、0 裁剪、0 静态资源错误、0 非预期浏览器错误；移动菜单的鼠标点击、Escape、Enter 和菜单项焦点通过。匿名登录页桌面首屏约 290ms、移动约 316ms，密码显示/隐藏、错误密码 401、注册策略加载和两种视口布局通过。截图是自动化浏览器证据，不是人工眼睛或原生 computer-use 证据；它也不覆盖已登录组织业务、真实商业权益、生产 Partner/Device、发布回滚、对账、会签或 PRD-148 GA。

本轮进一步执行了全量角色页面审计：admin、developer、viewer 各 60 个 Workspace 页面，共 180/180；平台运营三视口、Support/Partner/Device 有数据页和正向流程分别完成当前产物，布局、裁剪、交互重叠和意外网络/控制台问题均为 0。Gateway 协议/流式/默认模型单测 19/19，跨协议员工身份与计量集成 1/1。GA Launch 集成在仓库内隔离 basetemp 下形成完整终态 `5/9`，4 项因当前迁移头不匹配测试期望的 `0155` 回滚版本而失败，证据为 `outputs/prd148-ga-launch-integration-20260925.json`；不能宣称通过。默认 pytest 临时目录拒绝访问已作为测试夹具问题记录，未修改门槛。

## 结论

OpenRouter 的界面感觉更简单，主要不是因为它没有 Workspace、费用、路由或治理能力，而是它把一个 Workspace 的常见任务收敛为一页概述和少数几个入口：密钥、路由、隐私、护栏、配置与用量。企业治理能力存在，但在概述页作为可理解的工作卡出现，不占用每个使用者的注意力。

MetaHub 当前把员工接入、Workspace 管理、网关运维、财务控制、合规治理、应用管理、可观测性、支持和开发者文档共同放入一个 Console 壳中。能力边界比 OpenRouter 更宽，但第一层信息架构把这些边界平铺给了不需要它们的角色。因此用户首先感受到的是“系统复杂”，而不是“只需一个网关地址和一把个人 KEY”。

这不是建议删除组织、预算、路由或审计。它们是企业网关需要保留的控制面。要改变的是暴露顺序：将员工使用路径做成默认产品，将运营和治理能力按责任、风险和使用频率后置。

## 调研范围与证据

- MetaHub 来源：当前源码 `console-web/src/lib/workspaceNavigation.js` 与
  `control-plane-api/src/metahub_control/workspace_navigation/catalog.py`。2026-09-20 的历史记录为 59 条；当前源码路由注册表为 60 条，当前计数以 `outputs/prd148-route-coverage-20260927-r6.json` 为准。
- MetaHub 已知体验证据：当前隔离角色审计和产品走查记录在
  `docs/testing/PRD-148-current.md`、
  `docs/testing/PRD-148-populated-domain-audit-20260920.md`。这些记录表明多数页面在权限和布局上可用，但也确认 Device 与 Partner 的真实业务数据路径仍未闭合。
- OpenRouter 公开来源：2026-09-20 读取的公开页面与公开文档，`https://openrouter.ai/`、`https://openrouter.ai/docs`。公开导航和页面文案将产品定位为“统一访问模型”的服务，优先呈现模型发现、价格/能力比较、开发者接入与账户动作。
- OpenRouter 已登录界面来源：用户提供的 `https://openrouter.ai/workspaces/default` 截图。截图可见一个 Workspace Overview、周使用量摘要（花费、请求、代币）和九张工作卡：API Key、Guardrails、BYOK、Routing、Presets、Tools、Observability、Classifiers、Workspace settings。它还把 API Key、路由、隐私与护栏放在同一段概述说明中。
- 本轮截图证据：`C:\\Users\\OseasyVM\\AppData\\Local\\Temp\\codex-clipboard-68f911fc-a435-4b9f-8584-3b86b30d3368.png` 至 `C:\\Users\\OseasyVM\\AppData\\Local\\Temp\\codex-clipboard-2822661a-57a0-4fe9-8557-d1c41887e609.png`，共 16 张，采集时间为 2026-09-20 19:36--19:39。本轮涵盖 API Keys、Files、Guardrails、BYOK、Routing、Tools、Observability、Classifiers、Settings、Profile、Activity、Logs、Credits、Notifications、Privacy 和 Preferences。它们是用户提供的已登录页面材料，仅用于体验研究；不得据此创建、删除、修改或读取账户对象。
- 限制：内置浏览器控制当前初始化失败：`failed to write kernel assets: 系统找不到指定的路径 (os error 3)`，因此没有通过自动化操作该已登录账户。本文只使用用户已提供的截图，不包含账户私有数据，也不将截图外的行为宣称为已逐项核验。

## 本轮截图的页面级观察

这组材料补强了一个重要判断：OpenRouter 也有大量高级控制项，但它将它们限制在很短的 Workspace 导航和明确的上下文页中，并没有让每个功能以独立首层工作域出现。

| 页面 | 截图可见的组织方式 | 对 MetaHub 的具体启示 |
| --- | --- | --- |
| API Keys | 一行搜索、一张表、一个右上角主操作；其余操作在行内。 | “个人 KEY”页应把复制、状态、撤权和最近使用量放进单页，避免用户先理解 Virtual Key、Consumer、Project 等对象。 |
| Files | 单一开关、上传按钮和一个空状态。 | 文件与文件安全可在一个页面按风险展开；空状态应给出下一个业务动作，而不是先展示配置体系。 |
| Guardrails | 一个默认护栏列表，状态、策略、成员和 API Keys 在同一表内。 | 把 Policy、DLP、Output Safety、Session Risk 的首屏改为“当前生效护栏”和例外，再链接到高级策略细节。 |
| BYOK | Provider 列表表达为“已配置/未配置”，不要求先进入多个 Provider 子页面。 | 管理员可从一个 Provider 状态页完成接入；私有端点、健康详情和错误映射后置。 |
| Routing | 用 Auto Router、预算层、允许/排除模型、默认模型和防止覆写构成一个连续表单。 | Provider、模型、默认路由与员工可用性应成为一个连续配置流，不应拆成多页且要求用户记住前置关系。 |
| Tools、Observability、Classifiers | 都采用开关或紧凑列表；一个页面回答“是否开启、影响何处、下一步是什么”。 | 高级能力应使用状态和风险解释，避免常驻的独立侧栏分组。 |
| Settings、Credits、Notifications | 费用与通知先给摘要、阈值或待处理项，明细从页内继续。 | Finance 首页先显示本月支出、预算余量、异常和待处理事项；账本、争议、成本版本作为角色受限详情。 |
| Activity、Logs、Privacy、Preferences | 账户活动、请求日志与隐私偏好保持为账户或 Workspace 的二级上下文。 | 个人使用者默认进入“我的接入、我的使用、我的请求”，而非完整的 Workspace 控制面。 |

## 2026-09-30 路由能力对比：不是只有故障后备用

这部分此前只写了页面组织方式，没有把运行时能力拆开说明。按当前源码和用户提供的 OpenRouter 已登录界面/公开文档材料，结论如下：

| 能力 | MetaHub 当前实现 | OpenRouter 对标差距 |
| --- | --- | --- |
| 固定选择 | `fixed` 按别名的主模型、备用模型顺序选择。 | 有默认模型，但路由入口同时承担更丰富的模型池和 Provider 策略。 |
| 可用性优先 | `availability_first` 在请求前根据健康状态和新鲜度优先选择健康候选，不必等主模型请求失败。 | OpenRouter 将可用性/Provider 选择放在同一条 Routing 配置体验中，用户可组合更多约束。 |
| 成本优先 | `cost_first` 在完整候选集合合格时，按本次 Token 估算和同一价格快照比较成本；缺价或币种不可比时回到固定顺序。 | OpenRouter 的路由控制面更直接地暴露预算层、模型允许/排除和 Provider 约束。 |
| 质量、成本、延迟 | `quality_cost` 使用冻结的质量、成本、延迟权重、质量门槛和最小样本数进行选择。 | OpenRouter 的 Auto Router/模型选择心智更接近“由平台按请求选择合适模型”，MetaHub 目前仍要求管理员先维护别名候选和治理参数。 |
| 故障后回退 | 运行时可记录主/备用尝试及触发原因；这是候选选择之后的另一层机制。 | OpenRouter 也有 fallback/provider failover，但用户侧通常不需要先理解主模型、备用模型两个内部对象。 |
| 候选规模 | 当前每条 Alias 支持 1 个默认模型和最多 31 个有序备用模型。请求开始前，四种路由模式会在完整候选集合中选择；发生白名单故障时，运行时沿冻结顺序继续尝试后续候选，最多 32 次上游尝试，成功即结束，全部失败才返回最终错误。 | OpenRouter 的产品体验更接近多个模型/Provider 的候选池，可按允许、排除、预算和 Provider 条件组合；MetaHub 仍可继续补充更丰富的 Provider 约束和可视化解释。 |

因此，“我们的路由只能在默认模型失效后使用吗？”的答案是：**不是**。当前 MetaHub 的 `availability_first`、`cost_first` 和 `quality_cost` 都能在首次上游请求前从完整候选集合中选择；只有“运行时回退”这一层是在主尝试失败后按冻结顺序推进。当前实现已不再保持一次备用尝试的旧合同：非流式请求会在可重试的超时、限流、额度、网络不可用和上游 5xx 下继续遍历剩余候选，最多 32 次，且客户端看不到中间切换。

当前证据位置：路由模式和表单在 `console-web/src/features/routing-policies/renderRoutingPolicyPanel.js`；运行时四种模式、32 条候选上限和选择阶段在 `shared-sdk/python/metahub_shared/runtime/routing_policy.py`；主/备用候选从模型别名读取在 `control-plane-api/src/metahub_control/model_aliases/service.py`；候选集合迁移为 `infra/migrations/0157_model_alias_fallback_candidates.sql`，候选事实表扩展为 `infra/migrations/0173_routing_candidates_32.sql`（PostgreSQL `0181`）。本轮已完成结构和单元验证；真实外部 Provider 的 32 候选端到端证据仍需单独验收，不能把本地夹具结果写成 GA 证据。

### 产品决策建议

中文免费首发阶段不需要立刻复制 OpenRouter 的全部路由面。当前已完成第一步：组织管理员在同一个“编辑路由”弹框中配置默认模型、多个备用模型、选择方式、质量成本延迟参数和故障切换条件，列表只保留可扫描的摘要；路由策略版本作为只读并发依据显示，不作为用户手工输入。故障后的多级尝试已落到运行时和审计表，后续重点是补充真实外部多候选、计量、撤权和跨协议回归证据。未完成这些真实链路前，不应在产品文案中宣称“Auto Router”或“支持任意模型池”。

## 量化的 MetaHub 导航差距

历史 `20260920-role-audit-current-r2` 的 seeded 审计记录了管理员 47、开发者 27、查看者 7 个授权入口；该数字只作为历史信息。当前源码基线为管理员 45、开发者 27、查看者 3 个授权入口，默认侧栏分别为 12、9、3，延后入口分别为 17、9、0；当前证据为 `.artifacts/product-qa/20260927-current-role-{admin,developer,viewer}-r6/` 与 `outputs/prd148-route-coverage-20260927-r6.json`。

这三个数字不是缺陷计数，而是信息架构负荷的当前基线。与截图中的 OpenRouter Workspace 左栏相比，管理员和开发者首先要扫描的产品名词显著更多。下一轮重构应先将管理员的默认入口收敛到四个工作域，并让开发者、员工和查看者只看到当前职责需要的任务入口；全部深链、审计和 API 契约必须继续保留。

## 第一批实现与回归

本轮已在当前源码中实现第一批低风险收敛：

- `console-web/src/lib/workspaceNavigation.js` 按 Workspace 成员职责投影默认侧栏分组；授权路由、深链和后端返回的完整导航集合不变。
- `console-web/src/features/console-shell/renderConsoleShell.js` 将每组超过三项的入口放入“更多控制”，将角色不相关的一级入口放入统一的“更多控制”折叠区；高级工具仍使用原有受权对话框。
- `console-web/src/styles/main.css` 增加折叠区的可读布局，并保持窄屏侧栏抽屉和键盘可达性。
- `tests/product_qa/web/prd148RoleAudit.mjs` 现在同时记录 `authorizedNavigationCount`、`defaultSidebarNavigationCount` 和 `deferredNavigationCount`，避免将深链总数误读成默认认知负担。

历史 Chromium seeded 角色走查证据 `.artifacts/product-qa/20260920-role-audit-navigation-r5/` 的管理员默认侧栏 27、开发者 20、查看者 4 已被当前收敛结果取代。当前 240 张截图、三角色 60/60 路由交互和公开入口审计均绑定到 20260927/20260928 证据；它们仍属于本地 seeded 回归，不替代真实商业验收、外部身份、六方会签或 PRD-148 GA 证据。

本轮验证还包括 `console-web` 导航/控制台单元测试 105/105、`npm run lint` 和 `npm run build`。生产资源摘要已同步为当前 manifest 中的 `d3626f40547c7d0bd63bfe81b34fc4bb9ae020b5b69c6928b48d0774b0bd63c6`。

## 关键差距

| 维度 | OpenRouter 的外显体验 | MetaHub 当前外显体验 | 对用户的影响 |
| --- | --- | --- | --- |
| 产品心智 | “找一个模型并发起调用” | “进入一个企业 AI 控制平面” | 新员工进入 MetaHub 时先看到管理系统，而不是获得可用 AI 能力。 |
| 首屏任务 | 模型、调用、用量、密钥等高频动作 | Home、Workspace、Gateway、Finance、Governance、Applications、Observability、Developer Center 等并列 | 选择成本高，用户难以判断第一步。 |
| 路由 | Workspace Overview 将 Routing 作为一张工作卡，并与 API Key、隐私和护栏的说明并置 | Provider、Models、Aliases、Routing、Quality、Evaluations、Projects、Keys 分散成多页 | 管理员完成“可用默认路由”所需跨页次数过多。 |
| 财务 | 周使用量只先显示花费、请求、代币三项摘要；Credits 等动作后置 | Subscription、Usage、Internal FinOps、Provider Costs、Quota、Disputes、Budgets、Anomalies 都在一组 | 非财务管理员会误以为每项都需要配置；财务人员也缺少以“异常和待处理”为起点的任务入口。 |
| Workspace 与权限 | Workspace 是概述页的上下文；API Key、设置和治理动作从同一概述页进入 | 组织、成员、角色、环境、数据生命周期位于 Workspace 主组 | 管理概念过早出现，员工获得 KEY 的路径被淹没。 |
| 治理 | Guardrails、Classifiers、Observability 和 Workspace settings 以任务卡出现 | Policies、Snapshots、Templates、DLP、Session Risks、Output Safety、Webhooks、Audit 同列 | 合规能力可见性高于实际频率，造成侧栏噪声。 |
| 角色差异 | 用户通常只看到完成个人任务所需界面 | 三角色有权限回退，但仍共享很大的页面模型 | 权限隐藏不足以等同于信息架构简化；看见大量灰度或陌生分组仍增加负担。 |
| 术语 | 对外模型市场常用词为主 | Workspace、Provider、Alias、Policy Snapshot、FinOps、Private Gateway 等控制面术语并存 | 不同岗位需要先学习平台语言才能完成简单任务。 |

## MetaHub 应保留的不同点

MetaHub 不应复制 OpenRouter 的产品边界。以下能力是企业客户购买 MetaHub 的理由，应保留并加强：

- 一个组织内的成员、角色、个人 KEY 与撤权闭环。
- 默认模型与路由治理，避免员工在客户端填写 Provider Key、模型或协议细节。
- 预算、额度、审计、DLP、数据生命周期与可追溯控制。
- 多 Provider、私有 Gateway、设备交付、健康状态和故障治理。
- 支持、合作伙伴、迁移与平台运营工作流。

问题在于这些能力当前按“系统模块”排列，而用户需要按“现在要完成的工作”进入。

## 建议的信息架构

### 1. 默认入口改为按角色的任务首页

员工默认只看到：

1. 我的接入：网关地址、个人 KEY、支持的客户端、复制和撤权状态。
2. 使用情况：个人本月调用、个人额度、最近错误和帮助入口。
3. 请求历史：自己的请求和可理解的失败解释。

员工不应进入 Provider、路由、组织、预算配置或治理菜单。协议路径如 `/v1`、`/responses`、`/messages` 只在客户端示例中作为同一网关的接入细节出现。

### 2. Workspace 管理员采用四个任务域

将当前八个以上首层组收敛为四个工作域，其他能力通过上下文页签或“更多控制”进入：

| 管理员工作域 | 默认内容 | 后置内容 |
| --- | --- | --- |
| 接入与成员 | 成员、个人 KEY、项目、客户端接入 | 角色矩阵、环境、品牌、生命周期 |
| 模型与网关 | Provider、模型、默认路由、私有 Gateway、健康 | Alias、评测、质量、错误映射、缓存 |
| 用量与风险 | 使用量、预算状态、异常、请求错误 | FinOps、Provider 成本、争议、额度桶、审计细节 |
| 治理与运维 | 策略状态、待审批、告警、支持工单 | 模板、快照、DLP、Session Risk、Webhook、迁移、配置摘要 |

这不会删除路由、财务或治理；它会把默认视图从“所有可配置对象”转为“当前需要处理的状态”。

### 3. 用向导替代跨页配置

管理员的新组织默认路径应始终是：

`选择 Provider -> 添加模型或确认默认模型 -> 设置默认路由 -> 邀请员工 -> 生成个人 KEY -> 验证一次 Gateway 调用`。

过程应显示一个可恢复的进度条和明确结果。高级路由、模型别名、质量评测、私有 Gateway 与细分预算只能在需要时展开。不能要求员工填写 MetaHub 环境变量、Provider Key 或第二把 KEY。

### 4. 财务从“模块列表”改为“例外驱动”

财务首页默认回答三个问题：本月花了多少、是否超出预算、谁需要处理。订阅、原始账本、成本版本、争议和对账成为详情页或财务角色的二级入口。预算是管理员或财务的治理工具，不应成为所有用户的一级任务。

### 5. 以状态替代菜单数量

每个角色首页应有少量明确状态卡，而不是入口清单：

- 未完成：Provider 未连通、默认路由未发布、员工没有 KEY、预算异常、策略待审批。
- 正常：默认路由可用、Gateway 健康、额度安全、审计链完整。
- 风险：Provider 退化、调用失败上升、预算接近、私有 Gateway 脱机。

点击状态卡进入对应上下文，而不是先让用户寻找“正确模块”。

## 分批实施建议

1. 先做导航瘦身实验：不删 API 和页面，只为 employee、developer、workspace admin 输出不同的默认导航与任务首页。用现有 route registry 和权限契约实现，不另建平行菜单。
2. 做“默认路由已就绪”向导：将 Provider、模型、路由和员工 KEY 的高频成功路径串成一个可恢复流程，并保留每步审计。
3. 重做 Finance 首页：先呈现用量、预算状态和异常，再让具备权限的人进入账本、争议和成本版本。
4. 将 Governance 与 Advanced tools 改为按风险/待办出现，而不是常驻首层组。
5. 在有真实 Partner Membership、Device Deployment、付费权益和正式运行配置后，用真实账户重做管理员、员工、财务和平台运营走查。不要以 seeded 空态或局部脚本通过作为设计完成。

## 验收指标

- 员工从登录到拿到统一 Gateway 地址和一把个人 KEY，不超过两次导航；模型名只在客户端强制要求时出现。
- Workspace 管理员从新组织到默认路由可用、邀请员工并验证调用，能在单一向导中完成且可恢复。
- Finance 角色能在首页识别预算风险和待处理事项，不需要先理解八个财务模块。
- 每个默认角色的首层导航不超过四个工作域；高级入口必须有职责或风险触发理由。
- 继续保留当前 RBAC、审计、撤权、预算、路由和协议兼容性测试，并增加真实用户任务完成时间、首次任务成功率和误入无权限页的度量。

## 2026-09-21 第五批：管理员首层收敛为四个工作域

前几批已经把员工收敛到“我的接入”，并为 owner/admin 提供四张任务状态卡；但导航投影仍把任务首页所在的 `home` 组作为第五个首层侧栏分组。该结构仍然把“首页”误呈现为管理域，也与本文的四域验收指标不一致。

本批只调整默认侧栏投影，不删除页面、API、权限或深链：

- owner/admin 的默认组现在是 `workspace`、`gateway`、`finance`、`governance` 四组；任务首页仍是 revision 1 的默认落点，显式 `workspace.home.*` 深链仍由后端授权解析。
- developer 默认保留 `gateway`、`observability`、`developer-center`；security、finance、legal 和 auditor 只显示职责相关域；viewer 仍只显示员工“我的接入”入口。
- 任务首页从侧栏隐藏时仍可由首登重定向和“返回空间首页”进入，因此没有通过隐藏入口改变路由权威。
- 导航/壳层/产品 QA 契约回归 122/122 通过；lint 838 files、生产构建 294 assets/276 modules 通过。当前资源摘要为 `23a54e3a40411c44732c5cb6c051cf1857e116c6d99784165bf6363c70011db4`，源码快照为 `outputs/prd148-source-current-20260921-openrouter-four-domains.json`，SHA256 `e02776d1aed45aa4d2b588d7de9342de152e99cdaebbe969d2f71e222b8b8ed7`。

尝试用隔离 Chromium 重新生成三角色全路由报告时，`product_qa_browser_runtime.py` 在数据库准备阶段启动父子 Python 进程后无日志、无浏览器产物地挂起，已终止并保留运行目录。这是本轮测试运行器环境阻塞，不能作为浏览器通过或产品失败；最近一次已完成的 180 页隔离角色报告仍是历史 seeded 证据。真实注册到付费、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

## 与 PRD-148 的关系

本文是体验差距和下一轮产品结构建议，不是 PRD-148 完成证据。当前 PRD-148 仍有真实业务链路、商业权益、Device/Partner 数据投影、会签/回滚/对账、正式 scheduler-enabled 72 小时观察及 HEAD/origin 一致性等未闭合项，状态必须保持未完成。

## 2026-09-21 第六批：空态也保持状态优先

前几批已经把有数据时的管理员四域和财务例外工作台落到源码。本批补齐无任务事实时的边界：

- `owner`/`admin` 即使后端暂时没有接入任务，也继续显示四个“待确认”工作域，并将每个域标为“待确认/等待状态”；不会退回没有上下文的空列表。
- `finance_admin` 在没有订阅、预算或成本事实时仍保留“财务与风险”工作台和“查看财务任务明细”，空数据不会被误报为正常。
- 所有任务已完成的域明确显示“已完成”，不再显示容易误导的“已就绪”。

当前源码快照仍使用 `outputs/prd148-source-current-20260921-openrouter-four-domains.json`，源码 SHA256 为 `afa034615dbe88d9d55f67d9687174ec36079d07910b12f16e1180af8b56db29`，文件数 2694；生产资源摘要已更新为 `dfeca42ba38d9a841e47c7a182858062200d84d1031602b9b3f01b5dcdc731a5`。受影响单测 81/81、lint 838 files、生产构建 294 assets 通过。该批仍只完成 OpenRouter 体验建议的当前源码修改，不关闭真实商业链路、会签、回滚、对账、正式 259200 秒稳定性或 HEAD/origin 门禁。

## 已落地修改（2026-09-21 第二批）

本轮已将上述首层信息架构建议落到当前控制台源码，范围是默认入口和任务首页，不改变后端授权或已有深链：

- 当前 `viewer` 成员角色作为员工体验投影，默认侧栏只显示“我的接入”任务入口；Provider、预算、治理和其他管理页仍保留后端授权深链，但不再出现在员工默认侧栏或高级工具入口。
- 员工任务首页直接说明统一 Gateway 地址、个人 MetaHub KEY、协议路径属于同一网关、模型名称仅在客户端强制要求时填写，以及撤权后 KEY 立即失效。页面不要求员工填写 MetaHub 环境变量、Provider KEY 或第二把 KEY。
- `owner`/`admin` 的任务首页增加四个状态卡：接入与成员、模型与网关、用量与风险、治理与运维。每张卡从已有 task-home 权威事实计算完成/待处理状态，下一步仍跳转到原授权路由。
- 首次进入时 viewer 和 revision 1 的 owner/admin 优先落到任务首页；显式 hash 深链仍按后端授权解析。全部授权路由、审计和 API 契约保留。
- 生产清单已同步为当前资源摘要 `507cb0d8922e592149dca76d686f67e4988fb112123740c0e9face7475bf93ee`，当前源码快照见 `outputs/prd148-source-current-20260921-ux-final.json`。

受影响导航与人类式走查单测 77/77 通过，`npm run lint` 通过（838 files），`npm run build` 通过（294 assets，276 modules）。这些是当前源码的产品结构回归，不代表真实商业、付费权益、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 PRD-148 GA 已完成。

## 已落地修改（2026-09-21 第三批）

本批继续完成“状态优先、例外驱动”的信息架构建议：

- `finance_admin` 默认任务页先展示“财务与风险”状态卡，聚合订阅、用量、预算和内部 FinOps 的权威任务事实；原始任务列表收进“查看财务任务明细”，避免把账本、争议和成本配置同时变成首屏入口。
- 管理员四域状态卡在缺少对应任务事实时显示“待确认/等待状态”，不再把空数据误报为“已就绪”。这保持了状态卡对真实后端事实的诚实表达。
- 当前源码快照为 `outputs/prd148-source-current-20260921-finance-workbench.json`，SHA256 `e808e0df3da4222cda2165dd173020e9712e0a7421691fab231a2cd2c86fb080`；生产资源摘要为 `0925b588ff39e58c571b8c71aa87b99d0480dead1c683f837064f65f95641de4`。
- 相关单测已随前端全量回归达到 2,216/2,216，`npm run lint` 838 files，`npm run build` 294 assets/276 modules 通过。

本批仍只修改默认体验投影和状态表达，不改变 API、RBAC、审计或深链。真实付费权益、Partner/Device 正向数据、六方会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 已落地修改（2026-09-21 正向域复验）

为验证收敛后的任务入口在有数据时仍然可用，补充了隔离 seeded、当前迁移头（0155）的 Partner/Device 正向走查：

- Device 从控制台表单创建部署，`POST /edge/registrations` 返回 201，一次性注册材料可见，刷新后部署仍显示。
- Partner `GET /partner/workspaces` 返回 200，Workspace attribution 投影可见。
- 两个页面均通过布局稳定性检查：无横向溢出、控件裁切和交互重叠。
- 走查脚本先后暴露并修复两个选择器夹具问题：控制台双 `<main>` 严格选择器，以及 Partner 页面专属的 `main#partner-app` 根节点。修复后终态见 `.artifacts/product-qa/20260921-positive-domains-r3/prd148-positive-domains-chromium-scale-1.json`。

当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-final.json`，SHA256 `2344f9f626d82cf4ffed1210ed7c431389fb39a4e09c241637b523557f97c5dd`，文件数 2694。该证据仍是 disposable seeded QA，不等同于真实商业、Partner/Device 生产数据或 PRD-148 GA。

## 2026-09-21 第四批：降低任务首页并发遥测争用

任务首页收敛后，走查暴露出一个与信息架构无关但会影响首屏可靠性的真实问题：导航读取、导航偏好保存与 page-view 遥测同时写 SQLite 时，page-view 可能在两秒 writer deadline 后返回 `workspace_session.unavailable`。当前实现已完成集中修复：page-view 先完成 deferred read 预检，只有新增事件才申请短写事务，并在写入前再次复核 Workspace/session、权限、路由和幂等状态。这样保留审计事实和撤权即时生效，同时不让低风险遥测在认证/导航边界长期持有写锁。

验证：`control-plane-api/tests/integration/test_prd147_workspace_page_visits.py` 在仓库本地 basetemp 下 7/7 通过。随后隔离 Chromium Workspace FLOW 重放为 3/3，page-view 请求无 503、请求失败或控制台错误，证据为 `.artifacts/product-qa/20260921-openrouter-pageview-rerun/browser-real-chromium-scale-1.json`。默认 Windows pytest 临时目录权限错误仍按夹具问题记录；旧 503 运行继续保留为历史真实失败。最新源码证据为 `outputs/prd148-source-current-20260921-openrouter-pageview-fix.json`，SHA256 `1ee644602250309fb74c18f158deb9b1c6180a92671189e554f0e08c0bdab111`。这次是隔离 seeded 重放，不替代真实商业链路或 PRD-148 GA 证据。

## 2026-09-21 第七批：连续接入向导与阻塞状态

差距分析中“Provider、模型、默认路由与员工可用性应成为一个连续配置流”的首屏表达已补齐：管理员任务首页在四域状态卡下方显示 Provider → 模型 → 员工 KEY → 首次调用的可访问进度条。进度条只读取后端任务事实，空态显示待确认。

同时修正了状态卡的一个真实误导边界：工作域存在 `blocked` 或 `in_progress` 任务但没有可执行按钮时，原逻辑可能落入“已就绪”文案；现在按待确认、需要关注、等待前置条件、处理中、待完成、已完成的固定优先级渲染。

验证绑定：`outputs/prd148-source-current-20260921-openrouter-progress.json`（SHA256 `e0064c47ed721be544d9aaf89644f0d292ba1ee3ec58cbd93662590659174408`，2694 files）；前端单测 2220/2220、lint 838 files、生产构建 294 assets；生产资源摘要 `7bc3004734f98685f61b6b03676c158b48f751b0f3ac89adc50bcefbc0f63a4e`。这仍是源码级 UX 修改，真实商业/GA 门禁按本文既有声明保持开放。

## 2026-09-21 第八批：状态卡可以继续进入上下文

前一批已经让管理员首页用状态卡表达四个工作域，但域完成后只显示“已完成”文字，用户还需要回到侧栏才能复查配置。这与 OpenRouter 工作卡的行为不一致。当前状态卡现在复用后端任务事实中的授权路由：

- 未完成且有动作的域继续显示当前下一步，例如“配置模型服务”。
- 已完成且有可用授权路由的域显示“查看配置”，直接进入对应 Provider、模型或财务上下文。
- 没有任务事实、没有动作或路由不在当前注册表时仍只显示“等待状态/已完成”，不会猜测入口或绕过后端 RBAC。

本批同步完成 viewer/employee 的当前源码分区走查：batch1-6 共 60 个页面，全部通过，0 请求失败、0 响应错误、0 控制台错误、0 布局问题。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-status-card-review-r2.json`，SHA256 `4c724b6602cdd7fe34de487db5b261330cf93c261caeec3355592380423bb937`，生产资源摘要为 `604e687220b841bc717f807d6cc76eb7efcc90c8c77d0932fb655f2445097e54`。前端单测 2,221/2,221、lint 838 files、build 294 assets/276 modules 通过。

这批仍只证明当前源码的体验收敛和隔离 seeded 角色回归。真实注册到付费权益、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性及 HEAD/origin 一致性仍未闭合，PRD-148 继续保持未完成。

## 2026-09-21 第九批：首屏只保留一个下一步

上一批已经把 Provider、模型、员工 KEY 和首次调用串成可访问进度，但四步列表仍默认全部展开，管理员首屏同时看到四个状态域、进度条和所有操作，信息密度仍高于 OpenRouter 的工作卡。本批将接入流改为“当前唯一下一步 + 可展开的全部步骤”：

- 只有当前第一个可执行的未完成任务显示在首屏主卡，按钮沿用后端下发的授权路由和原有动作文案。
- 其余步骤收进“查看全部接入步骤”，已完成步骤仍可复查，阻塞、处理中和无动作状态继续按后端事实显示。
- 所有工作域卡、深链、RBAC、page-view 遥测和员工交付规则保持不变；没有可执行下一步时显示明确的“组织接入流程已完成”，不会伪造按钮或就绪状态。

验证：前端全量单测 `2221/2221`、`npm run lint`（838 files）、`npm run build`（294 assets，276 modules）通过。生产资源摘要为 `128e057623923ff796ac68582b3dce416d17e5e67068d633f097034091c72099`，当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-next-step.json`，SHA256 `9ed6b8f3292234e47baa21163ca33660a25b894e65df43e5b1d013b1621c8189`，文件数 2694。

这项修改只收敛首屏信息架构，不构成真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 的完成证据；PRD-148 继续保持未完成。

## 2026-09-21 第十批：英文界面不再混用中文

第九批已经把管理员首屏压缩为一个当前下一步，但走查发现 `en-US` 管理员仍会在任务工作台看到“任务摘要、管理员工作台、四个工作域、接入与成员”等中文硬编码，状态标签却是英文。这会让简化后的首屏失去一致的产品语气。本批完成了最后一处对标修改：

- 管理员四域、财务工作台、域说明、完成计数、唯一下一步、完成态和“查看全部接入步骤”全部改用现有 i18n 字典；中文默认文案保持不变。
- 员工“我的接入”页的 Gateway、个人 KEY、客户端限制、复制按钮和接入状态也接入同一套字典，英文员工不会再看到中文交付说明。
- 已保留所有后端授权路由、深链、RBAC、审计和统一 Gateway/个人 KEY 交付规则；本批只修正展示语言，不扩大员工配置面。

验证：`humanWalkthrough.test.mjs` 49/49 通过，包含英文管理员、英文员工和英文低频治理任务渲染回归；前端全量单测 2223/2223；`npm run lint` 838 files 通过；`npm run build` 294 assets、276 modules 通过。当前源码绑定为 `outputs/prd148-source-current-20260921-openrouter-i18n.json`，SHA256 `b1306d4de931297ba04bc579d720d2e721e0d03530bc13e1c6d2225fcdfdfceb`，文件数 2694；生产资源摘要为 `456d31dee44283f5b0175ba5f37e5cecae5c8e9da12912fda12332a6bb965dc3`。

这只完成 OpenRouter 对标中的信息架构和语言一致性修改。隔离 seeded QA、真实注册到付费、企业治理/私有 Gateway、Partner/Device 真实数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合；PRD-148 保持未完成。

## 2026-09-21 第十一批：空态给出下一步，复杂矩阵先按职责分组

第十批完成语言一致性后，剩余差距集中在低数据量和高数据量两端：Partner、Platform 和私有 Gateway 在没有事实时只显示一行空白提示；角色权限页则一次性展示高密度矩阵。当前源码已完成四项低风险收敛：

- Partner 无归属 Workspace 时说明需要完成成员归属，并提供刷新当前投影的动作。
- Platform 无数据域时说明当前职责域没有可验证记录，并提供返回平台总览的动作。
- 私有 Gateway 无部署时把“创建部署”明确为右上角下一步，保持现有创建按钮和权限边界。
- 角色权限矩阵将筛选标签改为“按权限分组查看”，补充辅助说明和 `aria-describedby`，先按职责分组再编辑，完整矩阵仍可显式查看。

这四项修改只影响状态表达和信息密度，不创建新路由、不绕过 RBAC、不改变 Partner/Device 数据投影。全量前端单测 2223/2223、lint 838 files、生产构建 294 assets/276 modules 通过；当前源码绑定为 `outputs/prd148-source-current-20260921-openrouter-empty-states.json`，SHA256 `5bf1230c779fd79b26349be5320f6258634ea922d09ca7f049696798b79348af`，生产资源摘要 `5fd93fb48d28691c622e48cdf9e572aed1f44b6b5723f69749ff2bd2fd08ac6c`。

本轮浏览器分区运行器在数据库准备阶段再次出现父子 Python 无日志挂起，运行目录 `.tmp/product-qa-browser/20260921-134839-26ba5c5b` 已保留并终止 PIDs 8080、23020；这属于测试运行器/环境阻塞，不能宣称浏览器通过。真实 Partner/Device 数据、注册到付费、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

## 当前修改结果（2026-09-21）

本文件中的建议已经完成一轮可追溯的当前源码修改，范围和边界如下：

| 对标差距 | 当前实现 | 当前证据 | 结论 |
| --- | --- | --- | --- |
| 首屏入口过多 | 员工默认进入“我的接入”；owner/admin 默认收敛到 Workspace、Gateway、Finance、Governance 四个工作域；完整深链继续由后端授权投影 | `outputs/prd148-source-current-20260921-openrouter-empty-states.json`；前端单测 2223/2223 | 已落地，仍需真实账户复验 |
| 跨页完成接入 | 管理员首屏只显示一个当前可执行下一步，完整 Provider → 模型 → 员工 KEY → 首次调用步骤进入显式展开区 | `outputs/prd148-source-current-20260921-openrouter-next-step.json`；导航/任务首页契约通过 | 已落地，不能替代真实注册到调用链 |
| 状态不如任务卡清楚 | 四个工作域按待确认、需要关注、等待前置条件、处理中、待完成、已完成表达；空态给出前置条件和下一步；完成态保留授权复查入口 | `outputs/prd148-current-evidence-index-ux-20260921.json` | 已落地，Partner/Device 生产事实仍缺 |
| 员工交付规则不够直接 | 员工页固定为一个 MetaHub Gateway 地址和一把个人 KEY；协议路径属于同一网关，模型名只在客户端强制要求时填写 | `console-web/src/features/python-sdk-examples/renderPythonSdkExamples.js`；humanWalkthrough 49/49 | 已落地，客户端兼容范围仍按具体版本验收 |
| 空态和高密度矩阵增加扫描成本 | Partner、Platform、私有 Gateway 空态提供下一步；角色权限矩阵先按职责分组查看并保留完整矩阵 | `outputs/prd148-source-current-20260921-openrouter-empty-states.json`；前端单测 2223/2223 | 已落地，权限模型未改变 |
| 英文界面混入中文 | 管理员任务首页、员工接入页和相关状态/步骤文案接入现有 i18n 字典 | `outputs/prd148-source-current-20260921-openrouter-i18n.json`；locale surface 49/49 | 已落地，低频旧页面仍需按角色继续走查 |

以上结论只表示当前源码和隔离 seeded 回归达到预期，不表示产品已经达到 PRD-148 GA。真实注册、付费权益、Partner/Device 生产数据、六方会签、发布后复核、回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒观察和 HEAD/origin 一致仍是开放门禁。

## 交付判断

OpenRouter 对标的界面修改本轮可以结束在“信息架构已收敛、能力边界保留、证据边界明确”。后续只在真实业务验收发现具体阻塞时继续改动，不再用增加首层菜单来补能力。任何把 seeded、模拟上游、匿名健康接口、短时观察或测试运行器通过写成 GA 的报告，均不符合本文的证据标准。

## 2026-09-22 第十七批：异步面板语言一致性

第十六批的角色分区复验仍暴露一个具体的英文界面问题：Support 和 Private Gateway 在异步 hydration 后直接写入旧渲染器 HTML，导致英文壳层中出现 `Status` 与中文状态、`Private Gateways` 与中文正文并列。该问题属于真实产品展示缺陷，不是浏览器运行器误报。

本批将 `renderCurrentSupportTicketPanel` 和 `renderCurrentPrivateGatewayPanel` 接入现有 `localizeConsoleStaticHtml` 出口。投影只在 `en-US` 生效，只替换登记的静态 UI 词汇；用户输入、Provider/模型元数据、API 字段、错误码、一次性材料、RBAC、租户隔离和员工统一 Gateway/个人 KEY 合同均不变。新增异步面板覆盖断言，中文默认界面保持原文。

当前验证：前端全量单测 2229/2229、`npm run lint` 841 files、`npm run build` 295 assets/277 modules；前后端生产资源摘要统一为 `5f44508358f909fd368442bf806f45d1f1157ff9af68e35ca5536485fd638e99`，仓库 `.venv` 的 `test_frontend_release_digest_exactly_matches_production_manifest` 通过。构建首次拒绝旧摘要，已按发布资源对账漂移记录并修正，不归类为运行时产品失败。当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，2696 个文件，SHA256 `b1f008bbbc5b02f28138fb53b669cc2fdcc4e61434a6943b933fafc2b1f5ff5e`。

这次修复只关闭异步英文静态文案问题。它不替代统一 180 页运行器、真实注册到付费和企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性或 HEAD/origin 一致性；`prd148_complete=false`、`ga_approval=false` 继续保持。

本轮人类式审计的合并分类索引为 `outputs/prd148-human-audit-current-20260922.json`，其中把已通过、真实历史失败、测试夹具问题、迁移基线漂移和证据不足分开记录。

## 2026-09-21 第十二批：发布资源对账与最终当前证据

第十一批代码落地后，前端全量回归实际新增了一个测试用例，当前源码单测为 2224/2224；此前文档中的 2223/2223 已过期。本批同时发现生产资源摘要仍停留在第十一批，导致 `npm run build` 首次以 `production asset set drift` 失败。该失败是发布资源对账漂移，不是运行时产品失败，也不是迁移基线漂移。

已完成的修正：

- `console-web/production-surface-manifest.json` 与 `control-plane-api/src/metahub_control/console_migration/frontend_manifest.py` 统一到实际当前摘要 `2383e3599edfec3a658080fdb2408df9828ca5132fc841cdf341ada611a436c7`。
- 顺序执行 `npm run build`，结果为 294 assets、276 modules 通过。
- `npm run lint` 通过（838 files）。
- `control-plane-api/tests/unit/test_prd147_scheduler_registry.py::test_frontend_release_digest_exactly_matches_production_manifest` 通过；该命令使用仓库 `.venv`，系统 Python 没有 pytest 的尝试保留为环境工具差异。
- 全量前端 `npm test` 通过 2224/2224，失败 0、跳过 0。

本批当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-release-reconciled.json`，源码文件 2694 个，SHA256 `31c274c380b5f587d8d8a271e79bbc6431a6fe2372f3cba669f4addd0997b15d`。上一批 `openrouter-final.json` 历史快照保持不变。当前证据索引为 `outputs/prd148-current-evidence-index-ux-20260921.json`，其中明确记录资源对账初始失败、修正结果及分类。

这次对账只证明当前前端发布闭集与后端 manifest 一致。隔离 seeded 走查、真实注册到付费和企业治理/私有 Gateway、Partner/Device 真实数据、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、scheduler-enabled 连续 259200 秒观察以及 HEAD/origin 一致仍未闭合；`prd148_complete` 必须继续为 `false`。

## 2026-09-21 第十三批：高级页面的语言一致性

第十二批只完成了资源摘要对账，随后对 `en-US` 管理员的高级页面做了源码级复核。发现角色权限矩阵和 Webhook 页面仍有中文硬编码，且权限目录如果返回已本地化的角色名称，会把中文名称直接带入英文页面。这是实际产品展示问题，不是测试报告误差。

- PAGE-085 现在对已知角色 code 使用英文 i18n 投影，即使后端 DTO 的 `name` 是中文；未知自定义角色仍保留后端名称。权限名称、描述、风险等级、锁定提示、保存栏和 aria-label 不再把中文 fallback 带入英文矩阵。
- Webhook 设置页的标题、说明、创建表单、事件订阅、事件类型、状态、投递记录、一次性密钥、错误和反馈全部通过 locale 投影；CEF/JSON 事件白名单、签名密钥只显示一次和后端权限边界不变。
- Partner 门户切换失败消息改为使用当前 locale，避免英文壳层出现中文错误提示。
- 中文默认界面保留原有文案；这批只处理展示语言和可访问名称，不改变 API、RBAC、租户隔离、写入接口或员工交付规则。

验证结果：定向 `permissions` + `webhooks` 为 25/25，前端全量为 2226/2226，`npm run lint` 为 838 files，`npm run build` 为 294 assets、276 modules。构建先按旧摘要失败（旧摘要 `2383e359...`，观察到 `9bda0985...`），同步 `console-web/production-surface-manifest.json` 与控制面 manifest 后重跑通过；后端 `test_frontend_release_digest_exactly_matches_production_manifest` 通过。当前源码封存为 `outputs/prd148-source-current-20260921-openrouter-role-webhook-i18n-final.json`，SHA256 `fab0506baa4c5e4c9b1c26671b22f62370a4d9549665017de8142134cb75e71f`，生产资源摘要 `9bda09855a4020b9b40981c305f60818b6354cf9af383120112161348bc0b685`。

这批证明当前源码的 locale 回归与发布资源一致性。尚未生成新的隔离 Chromium 终态，因此不能把单测或旧 seeded 报告写成英文高级页面浏览器通过；真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，`prd148_complete` 继续为 `false`。

## 2026-09-22 第十四批：旧页面英文静态文案收口

第十三批解决了权限矩阵、Webhook 和 Partner handoff 的 locale 问题，但分区走查继续发现 Provider、模型、预算、治理、可观测性和开发者中心等旧渲染器仍会在 `en-US` 页面输出中文静态标题或状态。此次修改在授权内容汇聚层增加了受 locale 限制的静态文案投影，覆盖已确认的共享页面词汇。

- 仅对 `en-US` 生效；`zh-CN` 默认页面保持原文。
- 只替换已登记的静态 UI 文案，不翻译组织名、成员名、Provider/模型元数据、API 字段、错误码或用户输入。
- Provider → 模型 → 员工 KEY → 首次调用、四个管理员工作域、员工统一 Gateway/个人 KEY 交付规则和后端 RBAC 不变。
- 新增 2 个 locale 单测；全量前端单测 2228/2228，lint 841 files，生产构建 295 assets/277 modules 通过。
- 前端和控制面生产资源摘要已统一为 `1cceebf6bf335d5d9fab7967b56dbe3019c07b92d7792b5193a89a6e95896bcb`；摘要对账测试使用 `.venv` 通过，系统 Python 缺少 pytest 的尝试保留为环境工具问题。

当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，2696 个源码文件，SHA256 `f735cd8a766775b8bfba39234cf1d1cbbb9384d8e02259c3b2b91d0441550d75`。随后隔离 Chromium admin 英文分区 10/10 通过，0 请求/响应/控制台/布局/交互问题，textSample 中 CJK 字符为 0，证据为 `.artifacts/product-qa/20260922-openrouter-en-admin-static-i18n-r2/prd148-human-interaction-chromium-scale-1.json`。这只是已覆盖页面的 seeded 证据，不代表所有高级页面或完整浏览器走查；真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，`prd148_complete` 继续为 `false`。

## 2026-09-22 第十五批：当前源码分区复验与发布摘要再对账

第十四批之后，异步 hydration 路径仍可能绕过统一静态文案投影。本批将可观测性、Webhook 死信、开发者中心和主应用 hydration 统一接入 `localizeConsoleStaticHtml`；权限回退警告也使用 locale 字典。该修复只作用于 `en-US` 的登记静态词汇，中文默认、用户/Provider/模型数据、API 字段、错误码、RBAC 和员工统一 Gateway/个人 KEY 合同保持不变。

- 当前源码证据：`outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，2696 个文件，SHA256 `32c39158491e80b47ae38ca0cacb93b728b3948509fa51eaedce053b2bee4c1f`。
- 当前发布资源摘要：`9112943188353389eec9f1807c3c7de7680e7b30073fb84be01c923e39f83e1e`；`npm run build` 为 295 assets/277 modules，`npm run lint` 为 841 files，前端全量测试 2228/2228，manifest 摘要契约通过。
- 当前隔离 Chromium 分区：admin 57/57、developer 57/57、viewer 60/60；请求失败、响应错误、控制台错误、布局、动作和授权问题均为 0。admin 的三批 `en-US` textSample CJK 为 0；developer/viewer 使用 `zh-CN`，中文属于预期 locale，不计为混排失败。

该批只证明当前源码和分区 seeded 角色回归，不是统一 180 页运行器的替代物，也不关闭真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性或 HEAD/origin 门禁。`prd148_complete=false`、`ga_approval=false` 继续保持。

## 2026-09-22 第十六批：全角色授权与有数据域复验

为继续验证“各种用户身份”的实际行为，新增了三类批次：

- SaaS 平台运营：Chromium 1440x900、1280x720、390x844 三个视口通过，状态接口 2/2，0 请求失败、0 控制台错误、0 横向溢出、0 裁切。
- 当前 Workspace 授权：admin、developer、viewer 分角色分片，各 60 个注册路由通过，0 失败、0 响应错误、0 请求失败、0 控制台错误、0 溢出；三角色统一 300 秒批次超时且无终态 JSON，已保留为运行器容量/环境问题，不能被分片结果覆盖。
- 有数据域：Support admin/developer 创建、回复、升级、解决、评价、重开和刷新持久化通过，viewer 写入返回 403；Device 正向创建返回 201 且刷新可见；Partner handoff 返回 200，归属页返回 200。正向脚本中 3 个 401/404 和 3 个 `ERR_ABORTED` 均已按接口合同和导航卸载分类为预期事件，意外网络、控制台和布局问题均为 0。

当前证据目录：`.artifacts/product-qa/20260922-platform-operator-current`、`.artifacts/product-qa/20260922-role-audit-{admin,developer,viewer}`、`.artifacts/product-qa/20260922-populated-domains-current`、`.artifacts/product-qa/20260922-positive-domains-classified`。这些仍是隔离 seeded 数据；真实设备交付、商业额度、Partner 生产归属、企业治理和正式 GA 门禁保持开放。

# 2026-09-23 当前交付语义修正

管理员的接入完成门槛现在止于员工 KEY 发放：Provider、模型/路由和员工 KEY 完成后显示“组织接入流程已完成”，不再等待员工第一次调用。`first_gateway_call` 仍保留为服务端真实使用事实和接入页状态，不会被前端伪造为成功。员工 KEY 对话框的“删除”调用 `DELETE /virtual-keys/{id}`，后端执行软禁用，凭据立即失效；历史计量和审计对象折叠保留，不做物理擦除。

控制台外层针对 `.console-shell` 使用可用视口宽度，桌面最大 1800px，窄屏切换为单列和 24px 外边距，避免继承公共登录壳层的 1120px 固定宽度。

## 2026-09-22 第十八批：取消管理员工作台的卡中卡

前面的导航、任务首页和状态文案已经把 MetaHub 的首屏入口收敛到少数职责域，但管理员工作台仍把四个可操作域卡包在一张带边框的大卡中。这个层级会让每个状态卡看起来像二级设置面板，增加视觉重量，也偏离 OpenRouter 以概述带承载少量工作卡的表达。

本批只调整展示层：

- `workspace-admin-workbench` 改为无框内容带，只保留顶部区分线；四个域卡仍是唯一的操作表面。
- 四个域卡、状态优先级、完成/查看配置动作、后端授权路由、RBAC、审计和员工统一 Gateway/个人 KEY 交付规则不变。
- 这项调整不删除页面或 API，也不把高级治理、财务和路由能力伪装成员工入口。

验证：前端全量回归 2229/2229 通过，`npm run lint` 为 841 files，`npm run build` 为 295 assets/277 modules；前后端生产资源摘要同步为 `72d8c6314c1b3cb2621b336b27762f1220ed1b72902658beddcd7669e36b26b3`，摘要契约测试通过。当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-card-band.json`，2696 个文件，SHA256 `192d4a9b1d8d175531d4f5415fcd5fcbdb79f70ea324fb06ab47f8223e5766ce`。

本批是当前源码级的信息架构收敛，未形成新的真实浏览器或商业验收证据。Gateway OpenAI 兼容集成当前仍有真实测试失败：`test_non_stream_5xx_falls_back_once_and_records_attempt_order` 第二次请求返回 200 而门槛要求 402；该问题与本批 CSS 修改无关，已保留在 `outputs/prd148-gateway-openai-20260922.xml`。真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性和 HEAD/origin 仍未闭合，PRD-148 保持未完成。

## 2026-09-22 第十九批：卡片层级与 Gateway 迁移基线收口

第十八批的 CSS 修改已经落到当前源码：管理员四域工作台外层不再使用一张包住全部内容的大卡，四个域卡成为唯一的操作表面，顶部仍保留分隔线和状态摘要。该调整只降低视觉层级，不删除组织、预算、路由、治理或审计能力，也不改变员工的统一 Gateway 地址与个人 MetaHub KEY 交付规则。

同批复核了 Gateway OpenAI 兼容回归中遗留的 402 断言。当前迁移 head 的 Workspace ProviderCostRate 是不可变的零费率种子；测试中只更新旧 `models` 价格列不能改变实际估算，因此“主备合计超过 0.15 CNY 应在首次上游调用前返回 402”的旧预期不再成立。测试已改名为 `test_fallback_budget_zero_rate_migration_boundary_is_explicit`，明确验证当前行为：主路由保留暴露、备用路由完成调用、请求返回 200、两次尝试被记录且不生成旧式 `budget_evaluations`。这属于测试夹具/迁移基线漂移，不能作为预算闸门通过，也没有修改 Gateway 产品逻辑。

当前可追溯证据：

- CSS 与当前源码绑定证据：`outputs/prd148-openrouter-card-band-current-20260922.json`，源码摘要为 `outputs/prd148-source-current-20260922-gateway-fixture-reconciled-r4.json`，SHA256 `ceb9be5281b8bcf3419f12cc0714540d4bec1232c029cbb34ab30fff2547394b`。
- Gateway 迁移基线终态：`outputs/prd148-gateway-prd036-boundary2-20260922.xml`、`outputs/prd148-gateway-prd079-boundary-20260922.xml`、`outputs/prd148-gateway-stream-boundary-20260922.xml`，均为单项通过；独立 `test_openai_stream_usage.py` 为 12/12 通过，证据为 `outputs/prd148-gateway-stream-usage-20260922.xml`。
- OpenAI 兼容文件的当前聚合回归由 `outputs/prd148-gateway-openai-full5-20260922.xml`（前 13 项通过后遇到已修正的旧流式价格断言）、`outputs/prd148-gateway-openai-tail1-20260922.xml`（5/5）、`outputs/prd148-gateway-openai-tail2b-20260922.xml`（5/5）和 `outputs/prd148-gateway-openai-tail3-20260922.xml`（6/6）组成；修正后的流式终态见 `outputs/prd148-gateway-stream-boundary-20260922.xml`。这些证据合计覆盖 30 个用例，但不是一个未受中途修改影响的单一 XML，因此不能把 `full5` 单独宣称为完整通过。

本批仍不构成真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性或 HEAD/origin 一致的完成证据；`prd148_complete=false`、`ga_approval=false` 继续保持。

## 2026-09-22 当前实现收口

前述建议已经完成一轮当前源码修改，现状按用户任务归并如下。管理员首屏只承担四个工作域和一个当前可执行的接入下一步；完整深链仍由授权导航提供。财务管理员先看到订阅、用量、预算和成本风险的例外状态，明细进入显式展开区。员工默认只看到“我的接入”，交付合同固定为一个统一 Gateway 地址和一把个人 MetaHub KEY，协议路径属于同一网关，模型名称只在客户端强制要求时填写。

| 差距 | 当前修改 | 当前证据 | 边界 |
| --- | --- | --- | --- |
| 首层菜单过多 | owner/admin 默认侧栏收敛为 Workspace、Gateway、Finance、Governance；developer/viewer 按职责投影；授权深链保留 | `outputs/prd148-current-evidence-index-ux-20260921.json`；`.artifacts/product-qa/20260922-role-audit-{admin,developer,viewer}` | 分片 seeded Chromium，不替代真实企业账户 |
| 配置跨页、下一步不清 | Provider → 模型 → 员工 KEY → 首次调用显示为单一当前下一步，完整步骤默认折叠 | `console-web/src/features/workspace-navigation/renderWorkspaceNavigation.js`；`console-web/tests/unit/humanWalkthrough.test.mjs` | 只读取后端任务事实，不证明真实注册到调用链 |
| 财务模块噪声 | 财务工作台先显示例外/待处理状态，订阅、预算、FinOps 详情后置 | `finance_workbench_unit`、`empty_status_workbench_contract` | 未伪造支出或额度数据；真实商业计量仍缺 |
| 空态和高级页面难扫描 | Partner、Platform、Private Gateway 空态提供下一步；权限矩阵先按职责查看；英文静态和异步面板统一投影 | `outputs/prd148-openrouter-card-band-current-20260922.json`；`.artifacts/product-qa/20260922-post-i18n-{admin-r2,developer,viewer}` | 当前证据仍是隔离 seeded 角色 |
| 管理员工作台层级过重 | 去除四域卡外层卡中卡，保留内容带和四张操作卡 | `outputs/prd148-openrouter-card-band-current-20260922.json`；源码 SHA256 `ceb9be5281b8bcf3419f12cc0714540d4bec1232c029cbb34ab30fff2547394b` | CSS/迁移夹具批次未重新生成完整浏览器终态 |

本次“修改完成”只表示 OpenRouter 对标的信息架构和展示层已在当前源码落地，并不改变 PRD-148 的完成判断。当前前端回归为 2229/2229，lint 841 files，build 295 assets/277 modules，生产资源摘要与控制面 manifest 已对账。真实注册到付费权益、企业治理和私有 Gateway 生产路径、Partner/Device 生产数据仍没有当前可追溯证据。六方会签与 scheduler-enabled 连续 259200 秒稳定性验证已按用户明确指示不执行，因此也不构成通过证据；`prd148_complete=false`、`ga_approval=false` 必须保持。

## 2026-09-22 第二十批：预算页和壳层静态文案收口

本批处理上一轮走查中预算页最具体的展示缺陷：英文投影会把 `0 条规则` 拆成 `0 条Rules`，把 `未配置` 拆成 `未Configuration`；预算表单、空态和壳层延迟菜单还存在少量中文静态标签。修复集中在统一的 `localizeConsoleStaticHtml` 出口，并补充壳层 `more_controls` 字典：

- 预算状态、作用链、表头、表单、空态、占位符和错误文案使用完整英文短语；中文默认界面保持原文。
- 只翻译带有明确 HTML 上下文的静态标签；组织名、环境名、Project/Virtual Key 名称等用户数据不被通用词替换，因此 `开发环境` 不会被改写成混合文本。
- `More controls`、`Example: Production key daily budget` 等壳层和表单静态文案补齐英文 key。

验证：前端全量单测 `2230/2230`，预算/本地化/壳层定向 `74/74`，`npm run lint` 通过（841 files），`npm run build` 通过（295 assets、277 modules）；控制面 `test_frontend_release_digest_exactly_matches_production_manifest` 通过。当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-budget-locale.json`，SHA256 `0b9a8c6f127d9fb2ae7af1aca7e0b9e97a4d2ec255e99bd25cdd0a2505cd27ea`，生产资源摘要为 `d5accfb3e166e8f4c3f021263a2c25e40a1a5f09db31af87759c7b76e1e0aeee`。管理员预算、Webhook、权限矩阵和内部 FinOps 分片为 4/4，通过请求、响应、控制台、布局、交互和授权检查，证据为 `.artifacts/product-qa/20260922-targeted-admin-role-audit-r4/prd148-role-audit-chromium-scale-1.json`。

这批仍是当前源码和隔离 seeded 浏览器证据。中文用户是首要发布对象，多语言一致性属于展示质量收口，不改变组织、预算、路由、审计和 RBAC 能力。真实注册到付费、企业治理和私有 Gateway 生产路径、Partner/Device 生产数据、发布复核/回滚、财务/隐私/安全/迁移对账以及 HEAD/origin 仍没有完整当前证据；六方会签和 259200 秒稳定性验证按用户明确指示不执行，`prd148_complete=false`、`ga_approval=false` 继续保持。

## 2026-09-22 第二十一批：Webhook 事件选择器布局修复

多角色页面审计发现 Webhook 创建表单的事件复选框滚动区高度不是行高的整数倍。1440×900 下最后一个可见复选框会被滚动容器底部截断，并与下方“保存 Endpoint”操作行发生交互几何重叠。该项是实际产品布局问题，不是运行器误报。

修复将事件滚动窗调整为 288px，并固定从完整 36px 行开始排列；保留搜索、事件白名单、CEF/JSON、提交字段和后端权限合同不变。新增 CSS 合同测试。admin/developer 当前 Chromium 分片均通过：重叠、裁切、溢出、请求失败、响应错误和控制台错误均为 0。证据为 `.artifacts/product-qa/20260922-webhook-overlap-{admin,developer}/prd148-role-audit-chromium-scale-1.json`。

当前源码证据为 `outputs/prd148-source-current-20260922-webhook-layout-fix.json`，SHA256 `6d9d675782de61e5248ed328c31311e4675a710cd9c405fd6272669932e7d40a`；生产资源摘要为 `3a97495ab2596d0b8cdd15b5251a0ebabc93727a794219d4390e8051b5727a22`。该修复只关闭当前 UI 几何问题，不替代真实企业账户、商业额度、Partner/Device 生产路径或 PRD-148 GA 门禁。

## 2026-09-22 第二十二批：中文导航分组枚举收口

中文管理员走查发现，高级工具深链在导航过渡标题中直接显示内部分组值 `advanced`，例如“空间首页 advanced 错误映射”。这是展示层本地化缺口，不是用户数据，也不应改变路由、权限或身份域。

- 新增 `localizeConsoleGroup` 作为导航分组展示的单一出口；`advanced`、`高级工具` 和 `Advanced tools` 按当前 locale 显示为“高级工具”或“Advanced tools”。
- 过渡标题和侧栏初始渲染共用同一映射；SaaS 平台运营管理员仍只使用 Platform session，Workspace 组织管理员、Developer 和员工仍只使用 Workspace session，两个身份域没有合并。
- 内部 `route_code`、`group_code`、授权导航投影和后端 RBAC 均保持原值；未知分组按原值安全回退。

验证：`consoleShell` 定向单测 18/18、前端全量单测 2233/2233、`npm run lint` 841 files、`npm run build` 295 assets/277 modules 通过；控制面摘要契约定向测试通过。当前源码证据为 `outputs/prd148-source-current-20260922-advanced-group-localization-final.json`，SHA256 `a365a6cc351f6fad4f9b1a4c2319ac2b007343ea72aa948fcfd1e63319d36469`；生产资源摘要为 `81901c9be09db6e1e5524e37969bd690c0d624eb121f677948e31d8375f1332f`。

这项修复只关闭当前中文展示问题。平台运营与组织管理员分域走查的 seeded 证据、真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 仍按既有边界记录；六方会签与 259200 秒稳定性验证按用户指示不执行，`prd148_complete=false`、`ga_approval=false` 继续保持。

## 2026-09-22 第二十三批：Support/Private Gateway locale 与身份域复核

英文静态投影中曾出现 `全部Status`、`消息Time线`、`回复Customer support`、`None yet升级`、`运行Status` 和 `尚未创建Private Gateways 部署` 等混合文案。本批在 `localizeConsoleStaticHtml` 增加完整短语映射并补充回归测试；中文默认文案、用户数据、API 字段、RBAC 和员工统一 Gateway/个人 KEY 合同保持不变。

SaaS 平台运营管理员和 Workspace 组织管理员继续严格分域：平台运营使用 Platform session，负责平台 bootstrap、平台账务、发布门禁和控制面迁移；组织管理员、Developer 和员工使用 Workspace session，负责组织 Provider、模型、路由、员工 KEY、预算、Support、Device、Partner 和私有 Gateway。平台证据不能替代组织证据，组织证据也不能替代平台证据。

当前源码证据为 `outputs/prd148-source-current-20260922-support-locale-final.json`，SHA256 `d0c39d15adf763d2ceea482cbd2ee11f4b9a4515d9cc8c429ad2c3a25a678cee`。前端全量单测 2234/2234、lint 841 files、生产构建 295 assets/277 modules；前端和控制面资源摘要已统一为 `be93bbfd03b244a16c7483c7caf8f428ec9f59733efdea074e87a81004884069`，控制面摘要契约使用仓库 `.venv` 通过。此前平台与 Workspace 浏览器报告仍保留各自原始源码快照，本批没有把源码级 locale 修复宣称为新的完整浏览器 GA。真实商业链路、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 仍未闭合；六方会签与连续 259200 秒稳定性观察按用户指示不执行，`prd148_complete=false`、`ga_approval=false`。
## 2026-09-24 当前中文首发语义修正

上一批历史记录中的“员工 KEY 发放”已不再是当前产品流程。当前角色边界固定如下：

- SaaS 平台管理员只处理 Platform 域；组织管理员处理当前 Workspace 的 Provider、模型、默认路由、组织 KEY、预算和成员治理。
- 组织管理员在“后台成员”中邀请、搜索、停用成员。原“模型使用者与 KEY”员工目录和管理员代发员工 KEY 入口已从生产界面移除。
- 后台成员登录后自行创建、复制、轮换和撤销个人 MetaHub KEY，只能读取自己的 KEY 和个人用量；管理员的组织 KEY 与成员个人 KEY 不混在同一归属语义中。
- 模型服务页把“自定义中转站 / OpenAI 兼容”作为“添加模型服务”目录中的一个选项，名称、地址和高级协议设置在同一条添加流程中按需出现。厂家目录由 SaaS 平台管理员通过签名目录发布流程维护；组织管理员只选择已发布服务并填写本组织的 API KEY。OpenAI/GPT 与 xAI/Grok 可在确认对应 OpenAI 兼容端点后发布目录项；Anthropic/Claude 需要真实 Anthropic 上游适配，不能仅因 Gateway `/messages` 入口存在就宣称已接入。

当前验证绑定：前端全量单测 2239/2239，`npm run lint` 840 files，`npm run build` 294 assets；成员个人 KEY、组织 KEY 视图和当前迁移链专项通过；控制面与前端生产资源摘要统一为 `c0ea8953b9c5bf12974dba01b0398a274ff276c707a1165a519ec04cafc7690a`。历史章节中的“员工 KEY 发放”仅保留为当时批次的事实记录，不应作为当前操作说明。

本节仍只证明当前源码和隔离回归。真实商业权益、企业治理/私有 Gateway、生产 Partner/Device、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 尚未闭合；按用户决定不执行六方会签与 259200 秒稳定性观察，PRD-148 保持未完成。

## 2026-09-25 易用性复验：英文投影不能混入中文 fallback

管理员截图进一步说明，简洁不只是减少菜单数量，也包括每一次刷新都保持同一种语言。英文任务首页曾在静态投影路径中出现“Connect a model 模型服务”“模型服务 and service credential”“Configure 模型服务”。本批在 `localizeConsoleStaticHtml` 的完整短语层增加清理规则，避免把中文 fallback 作为英文标题、描述或操作按钮的一部分展示；中文首发路径和用户数据保持不变。

证据：定向 i18n/任务首页回归 `58/58`，前端全量 `2249/2249`，lint `842 files`，build `295 assets`；当前源码快照 `outputs/prd148-source-current-20260925-usability-r6.json`，SHA256 `a2e4c94ff550148ee95882d78e11d17c931aed7024716ec3e73f49eddecc2cbe`。生产资源摘要为 `c7f35fb7139e8d3412e402630964604dade1a96c9cc478fb248770a5e6301be4`，与前端及控制面 manifest 一致。暴露问题的旧截图仍作为历史回归输入保存，修复后没有重新宣称一套完整三角色浏览器证据，因此 PRD-148 和 GA 继续未完成。

## 2026-09-25 当前 UI 审查收口

针对 OpenRouter 参考站点的差距审查，本轮没有继续堆叠页面能力，而是把用户能直接感知的四个问题收口：

1. 首页没有动画时不再渲染永远隐藏的“暂停动画”按钮，也不保留无效的 disposed 状态；仍有装饰动画的登录页只提供真实可用的暂停/播放控制。
2. 公开入口、子页面、CSS、模块和跨页链接使用相对路径，能够挂载到 /metahub/ 这类反向代理前缀；资源摘要由前端构建和控制面 manifest 双方校验。
3. 移动端主导航改为可见的菜单按钮，支持鼠标、Enter、Esc、外部点击和焦点回归；不会因为隐藏桌面导航而丢失“产品能力”和“服务状态”入口。
4. 首页首屏改为自然高度和可滚动内容，低高度设备不再由固定 svh 与 overflow:hidden 裁剪 CTA 或底部提示。

当前验证绑定到 `outputs/prd148-source-current-20260925-usability-r6.json`（2703 个文件，SHA256 `a2e4c94ff550148ee95882d78e11d17c931aed7024716ec3e73f49eddecc2cbe`）和 `.artifacts/product-qa-current/20260924-public-ui/report.json`（6/6 场景通过，SHA256 `ff84d8bc530d2b76295d8a5acd70aaa840523670cc919787f0b8c0574f7ee0f5`）。最终全量前端测试 2249/2249、lint 842 files、build 295 assets，生产资源摘要 `c7f35fb7139e8d3412e402630964604dade1a96c9cc478fb248770a5e6301be4`，控制面资源摘要契约通过。

这些结果是本地服务上的脚本化 Chromium 证据，不能称为原生 computer-use 或人工最终验收。真实商业链路、生产 Partner/Device 数据、发布复核/回滚、财务/隐私/安全/迁移对账、HEAD/origin 仍未闭合；六方会签和连续 259200 秒稳定性验证按用户决定不执行，PRD-148 继续未完成。

## 2026-09-25 最终复验与发布边界

OpenRouter 对标的当前源码改动已完成最终静态回归：180 张角色化 Chromium 截图覆盖 admin、developer、viewer，桌面 1440px 与移动 390px 均有证据；近空白、异常小尺寸、纯黑、页面失败、响应错误、请求失败、控制台错误、溢出、裁切和交互重叠均为 0。首页公开入口另有 6/6 场景通过，并覆盖 `/metahub/` 子路径、移动菜单、低高度首屏和登录交互。证据索引为 `outputs/prd148-current-evidence-index-final-20260925.json`，视觉索引为 `outputs/prd148-screenshot-visual-audit-20260925-final.json`。

最终源码重新执行 `console-web/npm test`、`npm run lint`、`npm run build`，结果为 `2249/2249`、`842 files`、`295 assets`，当前资源摘要 `c7f35fb7139e8d3412e402630964604dade1a96c9cc478fb248770a5e6301be4` 已与控制面 manifest 对账。付费模块按用户决定延期，仅影响后续商业化发布；中文免费试用仍需把真实注册到组织、Provider/模型/路由、个人 KEY、Gateway 调用、计量/额度、撤权、发布复核/回滚、对账和 HEAD/origin 证据补齐。原生 computer-use、72 小时观察和六方会签按用户决定不执行，不能把脚本化截图审计写成 GA 通过。

## 2026-09-25 用户使用容易度补充

本轮中文管理员走查发现，模型服务价格页的“全是空格让用户填”是明显的可用性差距：页面首屏同时铺开筛选、新增价格和停用验证字段，用户必须先理解内部成本配置才能看到当前状态。OpenRouter 风格的简洁不等于删除组织、预算或路由能力，而是把默认信息层和操作层分开。

当前修改为：

- 默认先显示已有模型服务价格、币种、输入/输出价格、生效区间和状态；没有已有记录时显示明确空态。
- 筛选入口显示“按模型服务、模型、币种或状态查找”，点击后才展开条件；已有筛选条件时自动保持展开。
- 新增价格入口说明“组织所有者 · 只在需要调整组织成本时填写”，默认收起，避免把低频管理动作误当成日常工作。
- 行内调整、采用参考和停用均先通过明确信息入口展开；停用验证不再在列表中露出空验证码输入框。
- 中文字段改用“模型服务”“模型”“输入/输出/缓存命中价格 / 百万 Token”等用户语言，补齐标签和占位提示；后端权限、动态验证码和安全密钥要求不变。

当前源码绑定为 `outputs/prd148-source-current-20260925-model-prices-ux-r2.json`（2703 files，SHA256 `e6e0bd3364a855e99ae48aea0c6e5a9cea7a7cd3400a2fafb887fc30baaedda5`），中文 admin 复验目录为 `.artifacts/product-qa/20260925-model-prices-zh-current/`，三角色终态为 `.artifacts/product-qa/20260925-screenshot-final-model-prices-r2/`；最终截图索引已补齐 180 张。全量前端单测 `2246/2246`、lint `842 files`、build `295 assets` 通过，资源摘要漂移已同步前后端 manifest 为 `fabbb192b15069850b2cc44f08a8c2a651fd1dc83491c71a0225ef3046896761`。

这项修改只改善中文免费试用的首屏理解和操作路径。付费模块按用户决定延期；真实注册到组织、Provider/模型/路由、个人 KEY、Gateway 调用、计量/额度、撤权、发布复核/回滚、对账、HEAD/origin 和 PRD-148 GA 门禁仍未完成。

## 2026-09-25 当前源码易用性复验

本轮把“简洁”落实到中文首发用户实际看到的内容：控制台壳层对业务面板的最终 HTML 统一做中文静态文案投影，管理员首页、成员管理、模型服务和服务成本率页面不再混入 `Workspace members`、`Actions`、`Model providers` 或 `Configure 模型服务`。真实组织名、成员名、模型名、URL 和 Provider 元数据不做通用翻译。

当前 Chromium 证据：中文管理员高频页面 `quickstart`、`users`、`providers`、`model-prices` 为 4/4；员工查看者 `390x844` 首页和越权深链回退通过；管理员首页、成员和模型服务的非破坏性鼠标交互为 3/3。报告分别为 `.artifacts/product-qa/20260925-usability-admin-r7-zh/prd148-role-audit-chromium-scale-1.json`、`.artifacts/product-qa/20260925-usability-viewer-r1/prd148-role-audit-chromium-scale-1.json` 和 `.artifacts/product-qa/20260925-usability-admin-interaction-r1/prd148-human-interaction-chromium-scale-1.json`。三份均为 0 页面/API/控制台/布局问题的隔离 seeded 证据，不冒充真实外部账户或人工 computer-use。

当前源码为 `outputs/prd148-source-current-20260925-usability-r7.json`，SHA256 `33a48040103517f6e7ad2132c1ddd5593d8cc354694d92a8365dee252a7333c9`；前端单测 `2249/2249`、lint `842 files`、build `295 assets` 通过，前后端资源摘要为 `2be5799752f0abd80fae25c771e1af0148d4c58cbe2e780b42701afc55d359ae`。管理员首页仍保留四个工作域、总体进度和下一步卡片，这属于后续信息架构压缩观察项，不影响当前中文页面的文字一致性和高频操作路径。PRD-148 继续未完成。

## 2026-09-25 首屏信息密度收口

OpenRouter 的简洁差距不只在菜单数量，也在首次进入时只给用户一个可执行动作。本批保留 MetaHub 的组织、预算、路由和治理能力，但改变默认信息层级：管理员首页首屏只显示“核心接入进度”和当前下一步；完整三步、四个管理区域及其他低频事项改为默认收起的详情入口。这样组织管理员仍能管理 Provider、模型、成员、预算和治理，但不会先阅读四组状态卡再决定从哪里开始。

本批同时修正进度口径：`核心接入进度` 的 `0 / 3` 只统计 Provider、模型、成员 KEY 三项交付；管理区域中的组织资料、成员职责等状态仍保留在展开内容中，不与首屏数字混用。中文使用“管理区域”，英文使用 `Management areas`，避免把内部“工作域”带到用户任务语言中。

证据：`outputs/prd148-source-current-20260925-usability-r8.json`（SHA256 `9e8cf6051c3efee2ca5a5f6a91c72d10cafc84b94311390be4b578a9ea77493a`）、中文管理员高频交互 `.artifacts/product-qa/20260925-usability-admin-interaction-r3/`（3/3）和首页截图 `.artifacts/product-qa/20260925-usability-admin-quickstart-r1/`。前端单测 2249/2249、lint 842 files、构建 295 assets，资源摘要 `dc70140d5cc7ae5312d687a81e5ec4110182f3abbde39acdeafb2f5c78ef3f1e`。

这项修改改善默认扫描路径，不减少任何授权入口，也不代表真实注册到 Gateway/计量/撤权、发布回滚、对账或 PRD-148 GA 已完成。

## 2026-09-25 当前源码易用性验证收口

本轮把“不要让用户面对一排空输入框”落实到当前源码验证：先同步生产资源清单，再重跑高频页面和可逆交互。管理员首页的唯一下一步仍保留在首屏；成员、模型服务、模型价格和个人 KEY 只在需要时展示搜索、筛选或编辑动作。组织管理员和员工的入口继续分域，员工不会看到平台运营或管理员动作。

当前源码快照 `outputs/prd148-source-current-20260925-usability-r10.json`（2703 files，SHA256 `697f7f788fa4a4be6176e0341753ec92f14b755cdb9b7493ed3677407f554af8`）通过 `npm run lint`（842 files）、前端全量单测（2249/2249）和生产构建（295 assets）。前端与控制面生产资源摘要统一为 `c3856db9be0d5fc160c33ed0ef030dba3e4bd6fdcdbf7a2eee83687cbe54cc16`。

当前脚本化 Chromium 证据：管理员桌面高频页面 5/5、员工移动端高频页面 4/4；同范围可逆鼠标/键盘交互管理员 5/5、员工 4/4。四份报告位于 `.artifacts/product-qa/20260925-current-r10-{admin,viewer,human-admin,human-viewer}/`。所有这些报告均无页面失败、请求失败、响应错误、控制台错误、横向溢出、裁切、交互重叠、动作失败或权限错误。

此前 180 页三角色截图仍保留为历史视觉覆盖，因资源清单同步后没有重新跑完整 180 页，不在本节冒充当前源码全量证据。真实外部注册、商业 Gateway/计量、发布复核/回滚、财务/隐私/安全/迁移对账、HEAD/origin、72 小时稳定性和六方会签仍按证据索引标记为未完成或按用户决定不执行。

## 2026-09-25 文案一致性复验

本轮把视觉走查发现的英文混中文问题追到全局 `userFacingTextGuard`：它在异步刷新后无视 locale，把 `Provider` 等产品词替换为中文。现在该清理器按 `app.dataset.locale` 工作，英文管理员页面保留英文，中文页面继续清理旧渲染器的工程词；新增词典覆盖侧栏“组织管理”、成员页 `Search` 和 Provider 活动 KEY 数量。

当前源码快照为 `outputs/prd148-source-current-20260925-usability-r11.json`（SHA256 `6694ac39ce5444b9c55d7cd12fc2a5fb7ff188532502f2cdd09b0487ed06db0b`），前端全量 `2250/2250`、定向回归 `29/29`、lint `842 files`、build `295 assets` 通过，资源摘要为 `4c0dded057f609f8642232cd97d009623b3c0d44fd77f182aad15fc787d2f7cb`。英文管理员 `quickstart/providers/users` `3/3` 的 Chromium 报告和截图位于 `.artifacts/product-qa/20260925-current-r13-english-home/`，混合语言 DOM、请求错误、溢出和交互重叠均为 0。

这只证明当前高频页面的语言和布局可用性；真实注册、Gateway/计量/撤权、发布回滚、对账、HEAD/origin 和 GA 门禁仍未闭合，PRD-148 继续未完成。
## 2026-09-25 员工视角的易用性补充（前一批分片）

OpenRouter 式的简洁还要求用户知道“为什么没有这个页面”。当前中文查看者移动端在 `quickstart/virtual-keys/providers/users` 上完成 4/4 走查；个人 KEY 页面保留创建、查看和撤销自己的 KEY，Provider 与成员管理员页面按权限回退到员工首页。原先的“该入口不可用”容易被理解为服务故障，现改为“你当前的职责无法打开此页面，已返回可访问页面”，英文同步为 “Your current role cannot open this page. You were redirected to an accessible page.”

这项调整把角色边界说给用户听，保留组织、预算、路由和治理能力，也保留 SaaS 平台管理员与 Workspace 组织管理员的身份分域。报告为 `.artifacts/product-qa/20260925-current-r17-zh-viewer/prd148-role-audit-chromium-scale-1.json`（SHA256 `f1cb4dd5bccdce1e42da734746bacd7a9e0a2d362a52f3945121caccba14506d`）和 `.artifacts/product-qa/20260925-current-r16-zh-admin/prd148-role-audit-chromium-scale-1.json`（SHA256 `16c3c87d1a91022d57e91b0d66ece6c9e6f1fa202b14bc74667f3332e5f873fe`）。这两份分片早于权限提示文案修复，当前源码终态以 r18 报告和 r14 快照为准。

这仍是脚本化 Chromium 的当前源码证据，不是人工 computer-use 或真实客户验收；真实业务链路、发布复核/回滚、财务/隐私/安全/迁移对账和 PRD-148 完成门禁保持未闭合。
## 2026-09-25 权限提示修复后的当前源码复验

权限提示文案修改后重新执行当前源码三角色核心页面：管理员、开发者和查看者的 `quickstart/virtual-keys/providers/users` 共 12/12 通过。查看者 `390x844` 移动截图确认打开 Provider 或成员管理员深链时显示“你当前的职责无法打开此页面，已返回可访问页面”，并回到员工首页；这比“入口不可用”更容易理解，也没有把权限边界误报成服务故障。

报告为 `.artifacts/product-qa/20260925-current-r18-zh-core/prd148-role-audit-chromium-scale-1.json`（SHA256 `f6ea61d7bec9cd26653f66040623306d6eb0cfadd186a0a2cc3f5ab128b23efb`），绑定当前源码 `outputs/prd148-source-current-20260925-usability-r14.json`（SHA256 `fab8072c1e2ddde7a358a9277c5ad546a958a9a2746284f14ef3a5dcf55ff80c`）。所有页面请求、响应、控制台、溢出、裁切和交互重叠均为 0；这仍是隔离 seeded Chromium 证据，不替代真实客户验收或 PRD-148 GA 门禁。

## 2026-09-25 信息密度复验：把“能做”变成“先看懂”

OpenRouter 的简洁感还来自默认状态只呈现高频任务。对照当前中文管理员页面，本轮发现两个会迫使用户先读大量控件的区域：成本账本一打开就平铺空筛选表单，角色权限一打开就平铺全部角色和权限。改动后的默认状态是：

- 成本账本显示当前分录和汇总；“筛选成本账本”收起空字段，设置筛选后自动展开并显示已设置数量，清空后再次回到收起状态。
- 权限页先显示当前角色的职责分组，首组可立即查看，其余分组按标题展开；“对比全部角色”是明确的二次操作，仍保留完整矩阵和保存能力。
- 这两个页面都保留键盘焦点、原有后端授权和移动端纵向列表，不通过隐藏内容改变可用权限。

当前证据绑定到 `outputs/prd148-source-current-20260925-usability-r15.json`（SHA256 `4fb2fe2e240a2e39577f1eac86ad19f87dce034c4184484a3df15595cc377992`）：中文管理员页面 2/2 和可逆交互 2/2 通过，报告分别为 `.artifacts/product-qa/20260925-usability-r23-zh/prd148-role-audit-chromium-scale-1.json` 与 `.artifacts/product-qa/20260925-usability-r24-human-zh/prd148-human-interaction-chromium-scale-1.json`；前端全量单测 `2253/2253`。这说明当前高频管理路径更容易首次扫描，不代表真实外部账户、商业 Gateway、发布回滚、对账或 PRD-148 GA 已完成。

## 2026-09-26 空配置不再铺满首屏

本轮把“全是空格让用户填”继续收口到策略模拟器：当组织还没有环境、项目或模型路由时，首屏只说明缺少的前置配置和下一步，完整请求条件表单默认收起；配置齐全时表单自动展开，管理员仍可运行完整预演。英文偏好下同一提示也保持完整英文，不再出现 `Project`、`Active` 与中文句子混排。

当前源码绑定为 `outputs/prd148-source-current-20260926-usability-r21.json`（2703 files，SHA256 `c26c425f3cafef6615bab88790c8ee768f43be30caadb91fa60f471bdba28853`）。本地化/用量/策略定向回归 `34/34`、前端全量单测 `2256/2256`、lint `842 files`、build `295 assets` 通过，资源摘要为 `36e2c3ba1aff4c8b1614b5595800ae5eb7660988ed9afea8ab6d82aae3467b95`。管理员策略模拟器当前页 `1/1` 和开发者 API 文档当前页 `1/1` 的 Chromium 报告分别在 `.artifacts/product-qa/20260926-current-targeted-admin-policy-r21/`、`.artifacts/product-qa/20260926-current-targeted-developer-api-r21/`。

这项修改只改变默认信息层级，不隐藏已授权能力或改变后端权限；仍是隔离 seeded 浏览器证据，不替代真实客户业务链路、发布回滚、对账或 PRD-148 GA 门禁。

## 2026-09-26 r45 易用性对标复核

OpenRouter 的简洁感主要来自默认信息层级：用户先看到当前状态和一个可执行动作，低频配置在明确入口后再展开。MetaHub 不能删除组织、预算、路由和治理能力，但可以让组织管理员和员工先看懂各自的任务边界。本批按这个标准复核当前源码：

- 组织管理员概览只显示有数据的组织摘要，空区域折叠；组织、财务、Gateway 和治理仍可从导航进入。
- 成员管理以搜索和分页表格为主，成员操作固定在行内，避免把大规模成员渲染成连续卡片。
- Provider 页面先显示已配置服务，自定义中转站 / OpenAI 兼容地址作为收起入口；品牌选项只是快捷方式。
- 组织 KEY 和个人 KEY 分开。组织管理员管理组织 KEY，后台成员自行管理个人 KEY；员工不需要填写组织后台表单。
- 查看者移动端默认只看到自己的空间和接入说明，进入管理员深链会得到职责提示和可访问回退。

当前源码的角色审计与可逆交互各为 15/15，证据在 `.artifacts/product-qa/20260926-usability-current-r45/` 和 `.artifacts/product-qa/20260926-usability-human-r45/`；源码快照为 `outputs/prd148-source-current-20260926-usability-r45.json`，SHA256 `63c7dc9230d8154d2624146d9217a734b6e28bdbb99df06a80f6ab84d875e37d`。前端单测 2259/2259、lint 842 files、构建 295 assets，资源摘要 `3a73b84e5fa3b021cc5b55ca3959ebda80ae17a00214a0f3f33be7001069dd53`。

这证明了当前源码的中文首发高频路径更容易扫描和操作，不等同于真实客户验收、完整注册到 Gateway/计量/撤权、发布回滚、对账或 PRD-148 完成。付费模块延期、六方会签和 72 小时观察按用户决定不执行。

## 2026-09-28 内部字段与模型服务入口收口

对标复核继续围绕 OpenRouter 的“先选择、后配置”信息层级推进。MetaHub 保留组织、预算、路由和治理这些产品差异，但不再把实现这些能力所需的工程字段直接放到组织管理员面前：

- 品牌服务与自定义中转站 / OpenAI 兼容服务合并到“添加模型服务”的同一个选择框，迁移参考项不再混入组织可接入列表。
- Provider 列表移除类型、Provider code 和普通用户无须理解的兼容规则入口，只显示服务名称、地址、状态、最近检查和操作。
- 模型目录移除 MetaHub 内部模型名、能力、上下文等高级字段，路由选择只显示模型名称和模型服务；高级异常处理按权限收起。
- 请求编号、追踪号、区域/认证代码、版本、哈希和数据库 ID 继续留在 API、状态层和不可见提交字段中，服务于路由、撤权、并发和审计，但不再作为普通用户文案。

当前审查记录为 `docs/operations/user-facing-internal-metadata-audit-20260928.md`，扫描 196 个功能文件、95 个渲染文件；受影响定向回归 `85/85`，lint 842 files，build 295 assets，生产资源摘要 `b7012aa806c15ad100ce7b79946e48ace8262fde5f2a9481efb982fbf3cae005`。这批是当前源码 UI 投影证据，真实客户链路、发布复核/回滚、财务/隐私/安全/迁移对账与 PRD-148 GA 门禁仍未闭合。

## 2026-09-29 KEY 操作收敛

OpenRouter 式简洁界面的关键不是删除组织能力，而是把低频配置移出默认视线。本批把个人 KEY 页面改为列表加一个“创建 KEY”按钮：创建表单、一次性明文、复制和隐藏动作共用一个原生弹窗，不再让空表单和结果把页面撑长。管理员不必先创建个人 KEY 才能完成组织接入；`project_key_ready` 仍表示组织已具备成员自助创建 KEY 的能力。

“MetaHub KEY”在成员入口、接入示例、迁移提示、观测和异常页面统一为“个人 KEY”，避免把个人调用凭据误读成组织级配置。解释性文案审计记录在 `docs/operations/user-facing-explanatory-copy-audit-20260929.md`：当前渲染模板得到 44 个词法候选，必要提示保留，明确冗余的 KEY 解释已移除。定向回归 `72/72`、向导后端单测 `7/7`、Workspace 导航集成 `6/6`、lint 842 files、build 295 assets 通过；源码证据为 `outputs/prd148-source-current-20260929-key-modal-r4.json`，SHA256 `f7b9f0b3fd3b32aff8a84699c96e91d6081635360094877cb593eb3c70f7cb85`，生产资源摘要 `2583dbe04de67e30e1f2023b7e4511c0f7f35a13453b75f5e38372900d4a41be`。

这批仍只证明当前源码的用户投影和定向交互，不代表全量 QA、真实外部业务链路或 PRD-148 完成。
