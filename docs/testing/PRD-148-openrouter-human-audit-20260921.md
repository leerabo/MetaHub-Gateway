# PRD-148 OpenRouter UX 批量走查记录

日期：2026-09-21（批次复验）

本记录合并本轮按管理员、开发者、员工/查看者三个角色运行的本地隔离浏览器走查，以及平台运营、Support、Device、Partner 和完整 Workspace 业务旅程。走查用于验证 OpenRouter 差距分析中已落地的默认导航和任务首页修改，不替代真实注册、付费权益、外部身份、正式 Gateway、六方会签或 PRD-148 GA 证据。

## 结果

| 角色 | 视口 | 路由数 | 结果 | 请求/响应错误 | 控制台错误 | 布局/交互问题 |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| owner/admin | 1440x900 | 60 | 通过 | 0 | 0 | 0 |
| developer | 1280x720 | 60 | 通过 | 0 | 0 | 0 |
| viewer/employee | 390x844 | 60 | 通过 | 0 | 0 | 0 |

审计运行：`scripts/quality/product_qa_browser_runtime.py`，旅程 `prd148-human-interaction`，产物根目录 `.artifacts/product-qa/20260921-current-human-audit-r1`。运行器使用隔离 SQLite 和 seeded 身份，固定报告文件名会被后一次角色运行覆盖；因此本表保留本轮实际读取到的三次终态摘要，当前目录中的 JSON 是最后一次 viewer 终态，不能解释为三份独立原始报告。

## 已确认的体验修改

- owner/admin 首次进入任务首页，并按“接入与成员、模型与网关、用量与风险、治理与运维”显示状态域。
- viewer/employee 默认只获得“我的接入”任务入口，页面说明一个统一 Gateway 地址和一把个人 MetaHub KEY。
- 协议路径属于同一网关；模型名只在客户端强制要求时填写；员工不需要填写 MetaHub 环境变量、Provider KEY 或第二把 KEY。
- 授权深链、后端 RBAC 和高级工具契约保留；角色默认投影只改变首层认知负担。

## 分类结论

- 已通过：三角色本地 seeded 浏览器页面、导航切换、授权回退、表单标签、布局溢出和控制台/请求错误检查。
- 真实失败：本轮未发现新的产品真实失败。
- 后续业务走查发现：隔离 Workspace FLOW-01 在任务首页与 page-view/导航偏好并行写入时复现 `/workspace/getting-started` 503（`workspace_session.unavailable`）；同批日志显示首次读取 200，锁竞争期间再次读取失败。该项按当前源码真实并发失败处理，已进入集中修复批次。
- Support 已完成三角色页面与动作走查；Device 只有空部署状态，Partner 只有无成员权限边界，正向设备/Partner 业务数据仍是证据缺口。
- 测试夹具问题：运行器固定 artifact 文件名导致角色报告覆盖；这是证据保存问题，不能把覆盖后的文件当作三份独立证据。
- 迁移基线漂移：本轮未修改后端迁移或历史基线。
- 证据不足：真实注册到付费权益、真实 Partner/Device 数据、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致性仍未闭合。

## 当前状态

本轮 OpenRouter UX 结构修改本身未发现新的布局或交互缺陷；但 Workspace 任务首页并发 503 已进入集中修复批次。Support/Device/Partner 结果只证明当前隔离身份边界和支持工作流，不能替代真实设备数据和 Partner 成员验收。PRD-148 继续保持未完成。

## 2026-09-22 预算与壳层复验

针对预算页英文静态文案混排，当前源码已修复完整短语投影和壳层延迟菜单 key。管理员 `budgets`、`webhooks`、`role-permissions`、`internal-finops` 四个页面分片 4/4 通过：请求失败、响应错误、控制台错误、布局溢出、控件裁切、交互重叠和授权问题均为 0。证据为 `.artifacts/product-qa/20260922-targeted-admin-role-audit-r4/prd148-role-audit-chromium-scale-1.json`。

截图中的组织/环境/Project 名称属于 seeded 用户数据，保留其中文显示；静态表单标签、预算状态和空态已统一。该批不是原生 computer-use，也不是真实商业链路或 PRD-148 GA 证据。

## 2026-09-22 Webhook 几何问题复验

历史角色审计在 admin 和 developer 的 Webhook 页面发现事件选择器底部复选框与保存按钮重叠。当前 CSS 已将滚动区改为完整行高；两角色单页复验均为 `interactive_overlap_count=0`、`clipped_control_count=0`、`overflowPx=0`，且请求、响应和控制台错误均为 0。该项已从真实产品问题转为已修复问题，旧失败截图和报告继续保留。

## 第三批状态驱动复验

针对差距分析中“财务从模块列表改为例外驱动”和“状态卡不能伪造就绪”的建议，当前源码已完成集中修改：

- `finance_admin` 首屏先显示“财务与风险”状态卡，订阅、用量、预算和内部 FinOps 的明细收进可展开任务列表。
- owner/admin 四域卡在后端未返回对应任务事实时显示“待确认/等待状态”，不再将空集合计算为“已就绪”。
- 新增单测 2 项，随前端全量回归 2,216/2,216 通过；lint 838 files、build 294 assets/276 modules 通过。
- 当前源码证据为 `outputs/prd148-source-current-20260921-finance-workbench.json`，SHA256 `e808e0df3da4222cda2165dd173020e9712e0a7421691fab231a2cd2c86fb080`，生产资源摘要 `0925b588ff39e58c571b8c71aa87b99d0480dead1c683f837064f65f95641de4`。

这仍是角色化首屏的源码和隔离回归证据，不是付费、Partner/Device 正向数据或 PRD-148 GA 证据。

## 集中修复后的复验

- 后端导航集成测试使用仓库内独立 basetemp 后 6/6 通过；默认 Windows pytest 临时目录权限错误保留为测试夹具问题。
- `workspace` FLOW-01/02/03 在 `.artifacts/product-qa/20260921-workspace-flow-r3/browser-real-chromium-scale-1.json` 中 3/3 通过。此前 FLOW-01 的 `/workspace/getting-started` 503 已通过有限短退避重试修复；新的走查断言误把产品必须展示的“Provider KEY 不需要填写”当作泄漏，已改为检查实际密钥格式，归类为夹具断言漂移。
- 当前修复批次源码快照：`outputs/prd148-source-current-20260921-human-audit-final2.json`，SHA256 `6ef4d4e845c33970938171047f03bb2148282fda59e4329545f4dd53ff25e9ba`，文件数 2693，生产资源摘要 `507cb0d8922e592149dca76d686f67e4988fb112123740c0e9face7475bf93ee`。
- 当前源码全路由角色审计复验：`.artifacts/product-qa/20260921-role-audit-r2/prd148-role-audit-chromium-scale-1.json`。admin、developer、viewer 覆盖全部授权 Workspace 路由，0 页面失败、0 请求/响应错误、0 控制台错误、0 溢出、0 裁切、0 交互重叠；该结果仍是隔离 seeded 身份证据。

## Partner/Device 正向路径复验

新增隔离 seeded 的 Partner/Device 正向走查，产物为 `.artifacts/product-qa/20260921-positive-domains-r3/prd148-positive-domains-chromium-scale-1.json`：

- Device 通过当前“创建部署”表单完成 `POST /edge/registrations`，返回 201；一次性注册材料可见，刷新后 `QA Browser Device` 仍显示。
- Partner 通过 `GET /partner/workspaces` 返回 200，显示 `QA Browser Partner` 和 Workspace attribution 投影。
- Device 与 Partner 页面均为 0 横向溢出、0 控件裁切、0 交互重叠。
- 前两次失败已保留并归类为测试夹具问题：第一次严格选择器命中两个 `<main>`，第二次把 Partner 的 `main#partner-app` 错当成控制台 `.console-main`。修复 `tests/product_qa/web/prd148PositiveDomainsAudit.mjs` 后才得到第三次终态。
- 该批是 disposable seeded、当前迁移头（0155）证据；清理 Workspace Cookie 后出现的 401 和尾部 page-view 请求失败被保留在报告中，不能当作真实商业或 GA 通过。

本批当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-final.json`，SHA256 `2344f9f626d82cf4ffed1210ed7c431389fb39a4e09c241637b523557f97c5dd`，文件数 2694。PRD-148 仍保持未完成。

## 第十三批：高级页面语言回归

在前一批英文任务首页复验后，源码检查发现两个高密度高级页面仍会混入中文：PAGE-085 角色权限矩阵的角色/权限动态文案，以及 Webhook 设置页的表单、事件、状态和错误文案。该问题分类为真实产品 UX 问题。

- PAGE-085 已知角色 code 现在使用英文投影，即使服务端 DTO 的角色 `name` 已是中文；保存栏、权限描述、风险标签、锁定帮助和 aria-label 使用同一 locale。
- Webhook 页面全部可见文案使用 locale 投影；CEF/JSON 约束、事件白名单、一次性 signing secret 和后端 Workspace 权限不变。
- Partner 门户切换失败消息使用当前 locale。

当前源码证据：`outputs/prd148-source-current-20260921-openrouter-role-webhook-i18n-final.json`，SHA256 `fab0506baa4c5e4c9b1c26671b22f62370a4d9549665017de8142134cb75e71f`，文件数 2694；生产资源摘要 `9bda09855a4020b9b40981c305f60818b6354cf9af383120112161348bc0b685`。定向 permissions/Webhook 回归 25/25、前端全量 2226/2226、lint 838 files、build 294 assets/276 modules，后端 manifest 对账测试通过。构建首次拒绝旧资源摘要并在同步前后端 manifest 后通过，按发布资源对账漂移记录，不归类为运行时产品失败。

分类结论：已通过的是源码 locale 契约、定向回归、全量前端回归和资源摘要对账；真实失败为本批没有新的产品运行时失败；测试夹具问题和迁移基线漂移没有新增；证据不足仍包括英文高级页面新的隔离 Chromium 终态、真实注册到付费、Partner/Device 生产数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin。PRD-148 继续保持未完成。

## 2026-09-21 page-view 争用修复

针对走查中真实复现的 `/workspace/navigation/page-views` 503，当前源码将 page-view 写入拆为两个边界：先在 deferred read 事务中完成 Workspace/session、权限、路由和幂等预检；只有需要新增事实时才申请短 `BEGIN IMMEDIATE`，再次完成最终权限复核后写入。重复请求在预检阶段直接结束，不申请写锁；最终复核失败或幂等冲突仍回滚并保留原错误语义。

- 当前源码快照：`outputs/prd148-source-current-20260921-openrouter-pageview-fix.json`，SHA256 `1ee644602250309fb74c18f158deb9b1c6180a92671189e554f0e08c0bdab111`，文件数 2694。
- page-view 集成回归：7/7 通过，使用仓库本地 basetemp `.pytest-openrouter-pagevisits-20260921-r1`；默认 Windows pytest 临时目录拒绝访问继续按测试夹具问题保留。
- 随后的隔离 Chromium Workspace FLOW 重放为 3/3，page-view 请求无 503、请求失败或控制台错误，证据为 `.artifacts/product-qa/20260921-openrouter-pageview-rerun/browser-real-chromium-scale-1.json`。旧的 503 运行仍保留为历史真实失败；这次 seeded 重放只证明当前修复在隔离环境有效，不替代真实商业链路或 GA 证据。

## 2026-09-21 第五批：四域首层复验

管理员默认侧栏从 `Home + Workspace + Gateway + Finance + Governance` 五组收敛为四个工作域：`Workspace`、`Gateway`、`Finance`、`Governance`。任务首页仍是 owner/admin 首次进入的默认路由，员工仍只获得“我的接入”；所有 `workspace.home.*` 和管理深链继续保留在后端授权投影中。

导航/壳层/产品 QA 契约回归为 122/122，lint 838 files，生产构建 294 assets/276 modules。当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-four-domains.json`，SHA256 `e02776d1aed45aa4d2b588d7de9342de152e99cdaebbe969d2f71e222b8b8ed7`，生产资源摘要为 `23a54e3a40411c44732c5cb6c051cf1857e116c6d99784165bf6363c70011db4`。

本批隔离 Chromium 角色审计在数据库准备阶段出现父子 Python 进程无日志挂起，没有生成浏览器报告；该运行器阻塞已保留为测试夹具/环境问题，不能宣称浏览器通过，也不改变此前已完成的 seeded 角色报告。真实商业链路、付费权益、会签、回滚、对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 2026-09-21 第六批空态回归

当前源码补齐了无任务事实时的状态优先边界：管理员仍显示四个“待确认”工作域，财务管理员仍显示“财务与风险”工作台，已完成域显示“已完成”。相关单测 81/81、lint 838 files、生产构建 294 assets 通过。源码 SHA256 为 `afa034615dbe88d9d55f67d9687174ec36079d07910b12f16e1180af8b56db29`，生产资源摘要为 `dfeca42ba38d9a841e47c7a182858062200d84d1031602b9b3f01b5dcdc731a5`。这项回归只证明当前源码的信息架构空态，不替代真实注册、商业权益或 PRD-148 GA 证据。

## 2026-09-21 第七批连续接入向导回归

本批完成差距分析中“用向导替代跨页配置”和“状态优先”的剩余界面细节：

- 管理员的 Provider → 模型 → 员工 KEY → 首次调用路径现在显示可访问的完成进度条，进度只按后端返回的 `completed` 事实计算，不把缺失任务或空态推断为已完成。
- 四个工作域的状态优先级固定为：待确认、需要关注、等待前置条件、处理中、待完成、已完成。被阻塞且没有可执行动作的域会明确显示“等待前置条件”，不会显示“已就绪”。
- 深链、后端 RBAC、员工统一 Gateway/个人 KEY 交付规则未改变；该批只调整默认任务首页的表达和状态。

当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-progress.json`，SHA256 `e0064c47ed721be544d9aaf89644f0d292ba1ee3ec58cbd93662590659174408`，文件数 2694。前端全量单测 2220/2220、`npm run lint`（838 files）和 `npm run build`（294 assets）通过，生产资源摘要为 `7bc3004734f98685f61b6b03676c158b48f751b0f3ac89adc50bcefbc0f63a4e`。

本批仍是当前源码和隔离 UI 契约证据，不关闭真实注册到付费、企业治理/私有 Gateway、Partner/Device 正向数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 门禁；PRD-148 继续保持未完成。

## 2026-09-21 第八批状态卡复验

状态卡的已完成域现在保留后端任务事实中的授权复查路由，显示“查看配置”；没有可用任务或路由时仍显示等待/完成状态，不猜测入口。新增契约测试覆盖已完成域的 Provider 复查动作。

viewer/employee 当前源码分区批次 5 和 6 已结束：共 20 个页面，20/20 通过；与 batch1-4 合计 60/60。请求失败、响应错误、控制台错误、溢出、裁切和交互重叠均为 0。证据：

- `.artifacts/product-qa/20260921-current-human-viewer-batch5/prd148-human-interaction-chromium-scale-1.json`
- `.artifacts/product-qa/20260921-current-human-viewer-batch6/prd148-human-interaction-chromium-scale-1.json`

当前源码证据为 `outputs/prd148-source-current-20260921-openrouter-status-card-review-r2.json`，SHA256 `4c724b6602cdd7fe34de487db5b261330cf93c261caeec3355592380423bb937`；生产资源摘要为 `604e687220b841bc717f807d6cc76eb7efcc90c8c77d0932fb655f2445097e54`。前端全量单测 2,221/2,221、lint 838 files、build 294 assets/276 modules 通过。

该批仍是当前源码和隔离 seeded 角色证据，不替代真实商业链路、付费权益、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性或 HEAD/origin 一致性；PRD-148 保持未完成。

## 2026-09-21 第九批首屏下一步复验

管理员接入首页已从默认展开完整四步改为首屏只显示第一个可执行的下一步，完整步骤移入“查看全部接入步骤”折叠区。折叠区保留已完成步骤的“查看配置”、阻塞/处理中事实和所有原授权路由；没有下一步时显示“组织接入流程已完成”，不推断后端状态。

当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-next-step.json`，SHA256 `9ed6b8f3292234e47baa21163ca33660a25b894e65df43e5b1d013b1621c8189`，生产资源摘要为 `128e057623923ff796ac68582b3dce416d17e5e67068d633f097034091c72099`。前端全量单测 `2221/2221`、lint 838 files、build 294 assets/276 modules 通过。

本批是信息架构和隔离源码回归，不是完整浏览器原生走查，也不替代真实商业链路、付费权益、Partner/Device 生产数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 门禁；PRD-148 保持未完成。

## 2026-09-21 第十批：英文角色文案复验

走查归并出一个真实的展示问题：`en-US` 管理员的工作台同时出现英文状态标签和中文工作域标题，员工接入页也有中文交付规则。当前源码已将这些新增/收敛后的文案接入 `messages.js` 的中英文键，中文默认界面保持原有文案。

- 英文管理员：四个工作域、任务摘要、完成计数、唯一下一步、完成态和步骤折叠标题均为英文。
- 英文员工：统一 Gateway 地址、个人 MetaHub KEY、协议说明、客户端限制、复制动作和接入状态均为英文；仍只传达一把个人 KEY 和一个网关地址。
- 回归证据：`console-web/tests/unit/humanWalkthrough.test.mjs` 49/49；`npm run lint` 838 files；`npm run build` 294 assets/276 modules。
- 当前源码：`outputs/prd148-source-current-20260921-openrouter-i18n.json`，SHA256 `b1306d4de931297ba04bc579d720d2e721e0d03530bc13e1c6d2225fcdfdfceb`，生产资源摘要 `456d31dee44283f5b0175ba5f37e5cecae5c8e9da12912fda12332a6bb965dc3`。

这是 i18n 和首屏表达回归，不是原生 computer-use、真实商业验收或 PRD-148 GA 证据。真实注册到付费、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 2026-09-21 第十一批：空态和权限矩阵收敛

本批将走查归并出的两类信息架构问题落到当前源码：无数据页面现在说明缺少的业务前置条件并给出下一步，角色权限页先提示按职责分组查看，避免把空白和 643 项控制同时交给用户。受影响表面为 Partner 归属 Workspace、Platform 数据域、私有 Gateway 部署列表和 PAGE-085 角色权限矩阵。

全量前端单测 2223/2223、`npm run lint` 838 files、`npm run build` 294 assets/276 modules 通过。当前源码绑定为 `outputs/prd148-source-current-20260921-openrouter-empty-states.json`，SHA256 `5bf1230c779fd79b26349be5320f6258634ea922d09ca7f049696798b79348af`，生产资源摘要 `5fd93fb48d28691c622e48cdf9e572aed1f44b6b5723f69749ff2bd2fd08ac6c`。

随后尝试的当前源码 Chromium 分区走查在数据库准备阶段无日志挂起，未生成浏览器终态；`.tmp/product-qa-browser/20260921-134839-26ba5c5b`、父子进程终止事实和历史失败均保留。该批是源码契约回归，不能替代原生 computer-use、真实 Partner/Device 数据、注册到付费、会签、回滚、对账、正式 259200 秒稳定性或 HEAD/origin 门禁；PRD-148 继续未完成。

## 2026-09-21 第十二批：当前源码回归与发布摘要对账

当前源码全量前端测试实际为 2224/2224，lint 为 838 files。构建首次发现第十一批冻结的生产摘要已经过期，随后同步前后端 manifest 并顺序重跑：294 assets、276 modules 通过，资源摘要为 `2383e3599edfec3a658080fdb2408df9828ca5132fc841cdf341ada611a436c7`。后端摘要契约测试 `test_frontend_release_digest_exactly_matches_production_manifest` 也通过。

本批源码绑定为 `outputs/prd148-source-current-20260921-openrouter-release-reconciled.json`，SHA256 `31c274c380b5f587d8d8a271e79bbc6431a6fe2372f3cba669f4addd0997b15d`，文件数 2694。上一批 `openrouter-final.json` 历史快照保持不变。初始构建失败归类为发布资源对账漂移，修正后已通过；不把它写成产品运行时失败，也不把本批验证扩大为真实商业或 GA 证据。真实注册到付费、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 2026-09-22 第十四批：旧页面英文静态文案收口

源码复核确认英文混排范围已经超出 PAGE-085 和 Webhook，覆盖 Provider、模型、预算、成本异常、治理、审计、可观测性、支持和开发者中心旧渲染器。新增 `console-web/src/i18n/localizeConsoleStaticHtml.js`，在授权内容汇聚层只对 `en-US` 投影登记的静态词汇；中文默认、用户/Provider/模型数据、API 字段和错误码保持原值。

验证结果：静态投影定向 2/2，权限/Webhook 定向回归 27/27，前端全量 2228/2228，lint 841 files，build 295 assets/277 modules；后端 `test_frontend_release_digest_exactly_matches_production_manifest` 使用仓库 `.venv` 通过。生产资源摘要为 `1cceebf6bf335d5d9fab7967b56dbe3019c07b92d7792b5193a89a6e95896bcb`，源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`。

随后隔离 Chromium admin 英文分区覆盖 10 个页面，10/10 通过，0 请求/响应/控制台/布局/交互问题，textSample CJK 字符为 0；证据为 `.artifacts/product-qa/20260922-openrouter-en-admin-static-i18n-r2/prd148-human-interaction-chromium-scale-1.json`。该证据只覆盖本批页面和 seeded 身份，真实注册到付费、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍是证据不足，PRD-148 保持未完成。

## 第十五批：当前源码全角色分区复验

为验证异步 hydration 文案投影和资源摘要同步后的当前源码，重新运行了分区 Chromium/Playwright 走查。该运行器不是原生 computer-use，身份和数据仍是隔离 seeded QA。

| 角色 | 当前页面数 | 结果 | 证据 |
| --- | ---: | --- | --- |
| admin | 57 | 57/57，通过；请求、响应、控制台、布局、动作和授权问题均为 0；`en-US` CJK 样本为 0 | `.artifacts/product-qa/20260922-admin-i18n-batch1`、`batch2`、`batch3-r3` |
| developer | 57 | 57/57，通过；请求、响应、控制台、布局、动作和授权问题均为 0 | `.artifacts/product-qa/20260922-developer-i18n-batch1`、`batch2`、`batch3` |
| viewer/employee | 60 | 60/60，通过；请求、响应、控制台、布局、动作和授权问题均为 0 | `.artifacts/product-qa/20260922-viewer-i18n-batch1` 至 `batch6` |

当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `32c39158491e80b47ae38ca0cacb93b728b3948509fa51eaedce053b2bee4c1f`，文件数 2696。`npm test` 为 2228/2228，`npm run lint` 为 841 files，`npm run build` 为 295 assets/277 modules；前后端生产资源摘要统一为 `9112943188353389eec9f1807c3c7de7680e7b30073fb84be01c923e39f83e1e`。构建首次拒绝旧摘要后完成同步，归类为发布资源对账漂移，修正后通过。

developer/viewer 的中文 textSample 来自预期 `zh-CN` 身份；不把它们误报为英文混排。当前分区通过仍不替代统一 180 页交互门禁、真实注册到付费和企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性或 HEAD/origin。PRD-148 继续未完成。

## 第十六批：平台运营、授权分片与有数据域

当前角色审计扩展到平台运营和有数据业务域。平台运营三视口通过；admin、developer、viewer 各 60 个 Workspace 路由分片通过，三角色统一批次因 300 秒总时限超时且没有终态 JSON，作为运行器容量问题保留。Support 的 admin/developer 写入链和 viewer 403 边界通过；Device 创建 201、刷新后可见；Partner handoff 200、归属页 200，布局无溢出或交互重叠。

正向 Device/Partner 脚本中的 401（切换域前清理 Workspace 会话）、License 尚未签发的 404 和导航卸载 `ERR_ABORTED` 已在 QA 报告中单独标记为预期事件；意外响应、请求失败和控制台错误为 0。证据：`.artifacts/product-qa/20260922-platform-operator-current`、`.artifacts/product-qa/20260922-role-audit-{admin,developer,viewer}`、`.artifacts/product-qa/20260922-populated-domains-current`、`.artifacts/product-qa/20260922-positive-domains-classified`。

本批仍不等于生产企业治理、真实设备交付、商业额度或 GA；Partner/Device 生产数据、六方会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 继续开放。

## 第十七批：异步 Support/Private Gateway 文案复验

走查中发现 Support 与 Private Gateway 的异步 hydration 直接写入旧渲染器 HTML，英文管理员页面会出现中英文混排。修复后两个出口均经过 `localizeConsoleStaticHtml`，只投影 `en-US` 的登记静态词汇，不翻译用户数据或协议字段。

验证：新增异步面板断言 1/1；前端全量 2229/2229；lint 841 files；build 295 assets/277 modules；前后端生产资源摘要 `5f44508358f909fd368442bf806f45d1f1157ff9af68e35ca5536485fd638e99`。当前源码证据 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `b1f008bbbc5b02f28138fb53b669cc2fdcc4e61434a6943b933fafc2b1f5ff5e`。

构建首次遇到旧摘要漂移，已同步 manifest 后通过，分类为发布资源对账漂移。该验证仍只覆盖当前源码和隔离 seeded 回归；真实商业链路、六方会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 第十八批：异步面板三角色复验

第十七批修复后，按三种 Workspace 身份分别重放 Support 与 Private Gateway 两页：

| 身份 | 页面 | 结果 | 证据 |
| --- | ---: | --- | --- |
| admin | 2 | 通过；请求、响应、控制台、布局、交互和授权问题均为 0 | `.artifacts/product-qa/20260922-post-i18n-admin-r2` |
| developer | 2 | 通过；请求、响应、控制台、布局、交互和授权问题均为 0 | `.artifacts/product-qa/20260922-post-i18n-developer` |
| viewer/employee | 2 | 通过；请求、响应、控制台、布局、交互和授权问题均为 0 | `.artifacts/product-qa/20260922-post-i18n-viewer` |

第一次重放因将 route code 误传给要求 source id 的脚本参数，在浏览器启动前以 `interaction_audit_target_invalid` 终止，已保留为测试夹具参数错误，不计为产品失败。三角色复验仍是隔离 seeded 数据，只证明本次异步文案修复在三种身份下没有引入可见回归。

## 第十九批：OpenRouter 对标修改收口

本批将差距分析中的已落地内容按当前源码重新归并，避免把历史批次的局部证据误读为整体 GA：

- 管理员工作台保留四个职责域，去除外层卡中卡；每个域只呈现状态、完成计数和一个授权动作。
- 管理员接入流首屏只显示当前可执行下一步，Provider、模型、员工 KEY 和首次调用的完整步骤通过显式展开查看。
- 财务管理员先看到订阅、用量、预算和成本风险的例外状态，任务明细后置；空事实保持“等待状态”，不推断“已就绪”。
- 员工默认入口继续只交付一个统一 Gateway 地址和一把个人 MetaHub KEY；协议路径、客户端限制和模型名说明不扩展为额外凭据或服务。
- Partner、Platform、Private Gateway 空态和权限矩阵已提供下一步/分组提示；英文静态和异步面板经过同一 locale 投影。

当前源码绑定证据为 `outputs/prd148-openrouter-card-band-current-20260922.json` 与 `outputs/prd148-source-current-20260922-gateway-fixture-reconciled-r4.json`，源码 SHA256 `ceb9be5281b8bcf3419f12cc0714540d4bec1232c029cbb34ab30fff2547394b`。对应前端全量回归 2229/2229、lint 841 files、build 295 assets/277 modules；这些是源码和隔离 seeded 证据。

六方会签和 scheduler-enabled 连续 259200 秒稳定性验证按用户明确指示不执行，不能写作通过或 GA 证据。真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 仍未闭合，OpenRouter 体验修改已收口，PRD-148 继续保持未完成。
