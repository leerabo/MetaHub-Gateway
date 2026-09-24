# PRD-148 当前源码人类式交互审计复验（2026-09-19）

## 2026-09-24 成员移除与角色化后台续验

本轮当前源码快照为 `outputs/prd148-source-current-20260924-member-key.json`，纳入 2703 个源码文件，SHA256 `0ff3be1ea56fee196b21ce6b20ff00f6c144429034956833a426e59aa0182d4f`。成员/使用者回归 9/9、Virtual Key 回归 6/6、Gateway 员工跨协议身份计量与停用精确回归 1/1、Console 角色与 KEY 回归 287/287、调度器生产资源摘要契约回归通过。前端 lint 为 842 files，build 为 295 assets，前后端资源摘要统一为 `be5564c28378fe2ab5e2d73c5c7bcd8efab194d4c87444c5010abb0bf2d2d87f`。

移除 Workspace 成员现在会在同一事务停用其个人 KEY、管理员分配 KEY、历史使用者关联和活动 Workspace session；目录与正常 KEY 列表不再显示停用对象，Gateway 鉴权立即拒绝旧凭据。身份页的旧组织 hash 入口统一跳转到按角色过滤的 `/console.html`，不再维护第二套组织后台。证据与分类见 `outputs/prd148-member-key-current-20260924.json`。

Gateway 宽集成重跑在完整迁移夹具阶段长时间未形成 JUnit 终态，已安全中断，归类为迁移夹具性能/证据不足，不计为产品通过或失败。Ruff 仍有既有 E501 长行，compileall 与 F821 检查通过。本地四个服务健康接口返回 200，但调度器日志仍有 `lease_lost`、`reconciliation_required` 和 SQLite 慢操作告警；这些只证明本地可启动，不构成 GA。按用户决定不执行 72 小时稳定性观察和六方会签，PRD-148 保持未完成。

## 2026-09-21 OpenRouter 对标第三批复验

本批补齐差距分析中的例外驱动财务入口：`finance_admin` 任务首页先显示“财务与风险”状态卡，原始财务任务收进可展开明细；管理员四域状态卡对缺失事实显示“待确认/等待状态”，不把空数据标为已就绪。当前源码快照为 `outputs/prd148-source-current-20260921-finance-workbench.json`，SHA256 `e808e0df3da4222cda2165dd173020e9712e0a7421691fab231a2cd2c86fb080`，生产资源摘要 `0925b588ff39e58c571b8c71aa87b99d0480dead1c683f837064f65f95641de4`。

前端全量单测 2,216/2,216、`npm run lint` 838 files、`npm run build` 294 assets/276 modules 通过。该批只证明当前源码的角色化首屏与状态表达；真实商业权益、Partner/Device 正向数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒观察及 HEAD/origin 仍缺证据，PRD-148 保持未完成。

## 2026-09-20 OpenRouter 对标后的导航收敛批次

针对 OpenRouter 截图中“短导航、单任务页面、复杂能力后置”的差距，本批在当前源码实现了角色感知的默认侧栏投影和“更多控制”折叠区。所有授权路由、深链、后端导航契约和审计范围保持不变；仅改变默认暴露顺序。受影响导航/控制台单元测试 105/105、权限矩阵与布局测试 48/48、前端全量单测 2,209/2,209、`npm run lint` 和 `npm run build`（294 assets）通过，生产资源摘要为 `005640641484230b0f6b10762f766651352fb12e644f4e6b95a05db4e2cfab3d`。

本批当前源码封存为 `outputs/prd148-source-current-20260920-r18.json`：revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2,693 个受测文件、SHA256 `f9b7f49499af1072ae1888b2719659c4632ceee7d7b4e4059254d37f4ca22696`。

当前 Chromium 角色审计证据为 `.artifacts/product-qa/20260920-role-audit-navigation-r10/prd148-role-audit-chromium-scale-1.json`：180/180 路由页面通过，管理员默认侧栏 12 项、开发者 11 项、查看者 4 项；后置入口分别为 20、7、3，全部授权入口仍为 47、27、7。该批为 seeded/local 浏览器回归，不能替代真实身份、商业权益、Partner/Device 数据路径、会签、回滚、对账、正式 259200 秒稳定性或 GA。

交互审计 r6 使用 360 秒运行时上限未完成，终态是脚本超时；该夹具问题保留在 `.artifacts/product-qa/20260920-interaction-navigation-r6/`，不能记为通过。r7 在修复前完成 180 页并发现管理员角色权限页保存条覆盖两个复选框；该真实 UI 失败保留在 `.artifacts/product-qa/20260920-interaction-navigation-r7/`。修复后 r9 使用 900 秒上限完成 180/180 页，0 失败、0 请求/响应错误、0 控制台错误、0 溢出、0 裁切、0 交互重叠、0 标签问题，证据为 `.artifacts/product-qa/20260920-interaction-navigation-r9/prd148-human-interaction-chromium-scale-1.json`。这仍是 seeded/local 页面回归，不关闭 PRD-148 的真实商业与 GA 门禁。

## 2026-09-20 Workspace page-view 写入串行化复验

`console-web/src/lib/workspacePageVisitTracker.js` 现在将跨页面 page-view writer 串行化，同时保留当前 Workspace 所有权与 AbortController 检查，避免旧页面服务端 SQLite 写入尚未结束时与新页面 writer 竞争。`prd147WorkspacePageVisits.test.mjs` 17/17 通过，Console lint 838 files、生产构建 294 assets 通过；当前源码快照为 `outputs/prd148-source-current-20260920-r9.json`，SHA256 `dcfcd93cab153698df52cafcb5f47455000b985d943dcc115efd16ee7edaa0e7`，资源摘要为 `ab7194634d27e6cbbe51caf72346f04ce5b677d98b0743e2083769179614ad53`。

该修复只覆盖客户端 writer 排队与局部回归，尚未证明真实 S1 截图/page-view 尾部已闭合；五域批次仍有起止源码漂移，真实商业/治理、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒稳定性及 HEAD/origin 仍缺当前证据。PRD-148 保持未完成，Excel `01_PRD总表!B60` 继续为“未启动”。

随后执行前端全量单测 2,202/2,202 通过（`outputs/prd148-frontend-full-regression-current-20260920-r9.json`），未改变上述 GA 证据边界。

## 2026-09-20 多角色与运营域批量走查

当前源码绑定 `outputs/prd148-source-current-20260920-r10.json`（SHA256 `dcfcd93cab153698df52cafcb5f47455000b985d943dcc115efd16ee7edaa0e7`）。admin、developer、viewer 各走查 60 条 Workspace 路由，共 180/180 页通过，0 页面失败、请求/响应错误、控制台错误、横向溢出、控件裁切、交互重叠或未标记控件；三角色 FLOW-01/02/03 真实业务旅程、Platform 运营 MFA/迁移页三视口、公共入口四视口也通过。完整事实见 `outputs/prd148-human-role-batch-current-20260920-r3.json`。

Partner 专用 Chromium 验收在页面操作前失败于 `migration.fresh:result`。对应迁移回归为 15 tests、1 passed、14 errors：旧 PRD-087 harness 固定要求应用到 0080，而当前迁移器在 0080 后继续应用 73 个迁移。这是迁移基线漂移，不能当作 Partner UI 通过或产品功能失败；Partner 当前源码页面证据仍缺失，下一批需先修正/重建与当前迁移基线一致的验收夹具，再统一复跑 Partner/Support/Device 页面和动作。

Partner/Support 聚焦回归已在同一源码快照完成：前端 `partnerPortal`、PRD-139、PRD-132、PRD-125 共 21/21，后端 PRD-125/132/139 共 10/10。它们只证明协议、权限和状态边界，不能替代 Partner 页面真实操作或商业 GA 证据。

权威 Excel 已更新本批事实并保持 `01_PRD总表!B60=未启动`；工作簿 SHA256 为 `b278257ab680b379d74f4897aba75be05f4be60974c8bd79d8a7a4534a4524a3`，输出副本为 `outputs/prd148-workbook-update-current-20260920-r2/output.xlsx`。

PRD-148 仍未完成。当前三角色全路由审计已在源码起止一致的快照上完成：管理员、开发者、查看者各 60 条路由，共 180/180 页；0 页面失败、0 HTTP 5xx、0 请求失败、0 控制台错误、0 横向溢出、0 控件裁切、0 交互重叠、0 无标签控件。证据为 `.artifacts/prd148-human-interaction-current-20260919-r4/evidence-manifest.json` 和 `.artifacts/prd148-human-interaction-current-20260919-r4/prd148-human-interaction-chromium-scale-1.json`，源码 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `5b7da462fb81947f3a5beae93cc889889cbf881adcbda3225db764e776ed76dd`、2690 个文件，`source_unchanged=true`。

## 2026-09-20 当前源码三角色人类式交互复验

当前源码重新运行了 admin、developer、viewer 三种用户身份的全部 Workspace 路由，每个角色 60 页，共 180 页。报告显示 0 页面失败、0 请求/响应错误、0 溢出、0 交互重叠、0 未标记控件；证据为 `outputs/prd148-human-interaction-current-20260920.json` 及其原始浏览器报告 `.artifacts/prd148-human-interaction-current-20260920-r1/prd148-human-interaction-chromium-scale-1.json`。该批绑定 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `0091a551460cacf86d0a6db922e08c8dd040608c091bb4d719102c3e22df7232`、2690 个受测文件。

这是当前源码的非破坏性路由和可逆控件清查，不能替代真实注册到付费、邀请接受、Provider/模型/默认路由、员工个人 KEY、Gateway、计量额度与撤权流程，也不能替代企业治理/私有 Gateway、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 一致性证据。当前批次没有发现可合并的 UI 产品问题；真实组织浏览器的隔离 Uvicorn 崩溃和延迟 page-view 请求仍按独立失败保留。

该批是非破坏性隔离浏览器审计，不能替代真实注册到付费、企业治理/私有 Gateway、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒零 P0/P1 稳定性或 HEAD/origin 一致性。五域 `outputs/prd148-negative-current-final-20260916/status.json` 虽然各域测试终态无失败，但 `source_unchanged=false`，因此保留为漂移证据，不升级为当前源码 GA。Excel `01_PRD总表!B60` 继续保持“未启动”，权威索引已关联 `current_20260919_human_interaction_audit_r4`。

# PRD-148 当前源码 page-visit 复验（2026-09-19）

PRD-148 仍未完成。当前受测源码为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2690 个文件、SHA256 `0091a551460cacf86d0a6db922e08c8dd040608c091bb4d719102c3e22df7232`。

- 后端 Workspace page-visit 集成回归 7/7 通过，JUnit 为 `outputs/prd148-page-visits-current-20260919-rerun.xml`；测试使用独立 Windows basetemp，未放宽锁等待或权限门槛。
- GA Launch 集成回归当前重跑为 14/14（`outputs/prd148-ga-launch-current-20260919.xml`）。协议/默认模型/跨协议身份计量批次本轮运行器未形成终态 JUnit，历史 27/27 结果保留但未升级为当前批次通过。
- 真实组织浏览器链路已完成注册、创建组织、订阅空态、Provider 目录、邀请和受邀者会话检查，但没有形成通过证据。`.artifacts/s1-browser-wg98wuns/browser.json` 保留了两个未完成的 `/workspace/navigation/page-views` 请求；该结果继续按真实运行失败处理，不能作为 GA。
- 另一次结果 `.artifacts/s1-browser-03f619ly/browser.json` 证明登出后确实到达带 `reason=session-unavailable&request_id=...` 的登录页；旧夹具只匹配无查询参数，已修正 `scripts/acceptance/trial-member-provider-browser.mjs`。`.artifacts/s1-browser-es00wwno/browser.json` 是浏览器上下文提前关闭，未计通过。
- `outputs/prd148-browser-failure-20260916.json` 中 Chromium 1x 登出 15000ms 未回首页的真实失败继续保留，未放宽门槛或降级为夹具问题。

当前 page-view 路由将 Cookie/CSRF/Origin/必需 Cookie 边界校验与事务内 Workspace/session/permission 复核分开，避免每个遥测 beacon 额外开启完整认证事务。当前源码边界与集成回归共 19/19 通过，JUnit 为 `outputs/prd148-page-visits-current-20260920.xml`；这只证明接口契约和事件循环边界，不替代真实浏览器流程。真实生产构建重放 `.artifacts/s1-browser-wevaspnu` 仍在试用成员/Provider 阶段因隔离 Uvicorn Windows access violation 失败，并保留两个延迟 page-view 请求，未升级为通过。

当前五域快照、真实注册到付费和员工撤权、企业治理/私有 Gateway、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒零 P0/P1 稳定性和 HEAD/origin 一致仍未闭合。权威索引 `outputs/prd148-current-evidence-index-final-20260916.json` 已更新；Excel `01_PRD总表!B60` 保持“未启动”，本次工作簿 SHA256 为 `cae9a58f9db18bb5db1761a1d538042d972af14ca3314f6b6ed2f9a8c51f491f`。

已封存的五域 r5 结果为 Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个退役入口 skipped、Device 14/14，但其 source hash 为 `b567e013...`，在本次源码变更后不再作为当前完整快照；未启动新的五域重跑，也未将旧结果升级为当前 GA 证据。

## PRD-148 当前源码角色页面复验（2026-09-18）

## 2026-09-18 Shell 与 Workspace locale 修复

英文偏好管理员的 Shell、Workspace 任务首页、连接状态、高级工具和侧栏折叠按钮此前混有中文硬编码；本批新增 locale-aware 字典并从主渲染链传递 `i18n`。受影响前端单测 2200/2200、Console lint 838 files、生产 surface 校验和 build 294 assets 通过。当前源码 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `b567e0133e5516f3c886384c50dd249b7a9aa22cf040347554e0b7cebc4e12f4`。r9 隔离 Chromium 角色审计已完成 admin/developer/viewer 各 60 页，共 180 页，0 页面/API/浏览器错误、0 溢出、0 裁切、0 交互重叠；英文样本显示 `Collapse navigation`/`Model providers`，证据为 `outputs/prd148-role-audit-current-20260918-r9/evidence-manifest.json`。本批对应 `OBS-UI-003`，PRD-148 仍未完成。

## 2026-09-18 当前角色、业务旅程与公共入口复验

当前源码隔离 Chromium 角色审计 r6 通过 admin/developer/viewer 各 60 个源路由，共 180 页；页面授权回退、请求/API、浏览器控制台、横向溢出、控件裁切和交互重叠均为 0。三角色业务旅程 r3 的 FLOW-01/02/03 通过，覆盖 Cookie 登录、角色操作、键盘导航、原生校验、恢复、移动抽屉和登出；其中仅 developer 执行 hydration 登出场景，admin/viewer 的该专门场景未执行。公共入口 r2 在 1440x900、1280x720、390x844、320x568 通过首页、登录错误、注册策略、验证码关闭策略和密码恢复布局检查，0 控件裁切、0 横向溢出、0 控制台/HTTP 错误。

证据目录为 `outputs/prd148-role-audit-current-20260918-r6`、`outputs/prd148-human-journey-current-20260918-r3`、`outputs/prd148-public-audit-current-20260918-r2`，三个 manifest 均绑定 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2688 文件、SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`。这些结果是隔离脚本与测试数据库证据，不等同原生 computer-use、真实注册到付费、企业治理/私有 Gateway、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账或正式 259200 秒稳定性；PRD-148 仍未完成。

## 2026-09-18 请求解释与反馈体验修复

集中走查确认的两个 P2 体验问题已在当前源码修复：请求解释 renderer 的状态、阶段、区块、事实字段和查询控件均通过 `en-US`/`zh-CN` 活动字典渲染；反馈报表日期输入使用服务端返回的 `from_time`/`to_time` 作为占位符，等待权威窗口时只显示 `YYYY-MM-DDTHH:mm:ssZ`，不再嵌入过期日历值。受影响单测 20/20、Console lint 838 files、生产 surface 校验和 build 294 assets 均通过。

证据目录为 `outputs/prd148-request-ui-fix-current-20260918`，封存清单确认源码起止一致：revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2688 文件、SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`。这只关闭 `OBS-UI-001/002`，不关闭 Chromium 1x 登出失败、真实付费/企业治理、六方会签、发布回滚、财务/隐私/安全/迁移对账或 scheduler-enabled 259200 秒观察；PRD-148 仍未完成。

## 2026-09-18 当前源码五域与协议复验

在 Shell/Workspace locale 修复后的当前源码快照上，五域负向矩阵 r4 起止快照一致：Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个明确退役入口 skipped、Device 14/14。证据为 `outputs/prd148-negative-current-20260918-r4/status.json`、同目录四个 XML 和 `evidence-manifest.json`，全部绑定 SHA256 `5b549d942d31da370eeb730c482448373e291987daa3ab416b4d479805e3c5a0`。首次共享 pytest 临时目录权限错误通过按域独立 `--basetemp` 重跑解决，归类为测试夹具问题；Platform P95 门槛保持原值并在完整矩阵中通过。该批不关闭真实商业、会签、发布/回滚、对账、正式 259200 秒稳定性或 HEAD/origin 门禁。

当前源码 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2688 个源码文件、SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac` 下，五域负向矩阵 r3 起止快照一致：Platform 38/38、Workspace 44/44、Partner/Support 16 passed 加 1 个 PRD-087 退役路由 skipped、Device 14/14。状态与 JUnit 证据为 `outputs/prd148-negative-current-20260918-r3/status.json` 及同目录四个 XML。

同一快照上的 PRD-148 GA Launch 集成回归为 14/14，协议/流式/默认模型/跨协议员工身份计费回归为 27/27。证据目录分别为 `outputs/prd148-ga-launch-current-20260918-r3` 与 `outputs/prd148-gateway-current-20260918-r3`，两个 evidence manifest 都确认 source unchanged。它们仍是隔离测试与合成上游，不能代替真实付费、企业治理/私有 Gateway、发布签核/回滚、财务隐私安全迁移对账或正式稳定性观察。

权威 Excel 已追加本批说明，`01_PRD总表!B60=未启动`，当前工作簿 SHA256 为 `972bb53b63bf9170e74c546aacc8aa9bc71cdd581332379f3c93cd0c678747d6`。Chromium 1x 登出 15000ms 未回首页的真实失败继续保留；六方会签、发布后复核、259200 秒 scheduler-enabled 观察和 HEAD/origin 一致仍未闭合，PRD-148 保持未完成。

## 2026-09-18 资源摘要同步后的当前快照

PAGE-116 修复后，前端与控制面生产资源摘要已同步为 `aa58650f6c0b3c7badaaa4c69f5f05cd8717f54bec32ab3187c10894518f318b`，`npm run lint`（838 files）与 `npm run build`（294 assets）通过。控制面已按该摘要重启，新的当前源码快照为 2688 个文件、SHA256 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`。

同步后的域页面审计仍为 69/69（59 桌面、10 移动），证据目录为 `outputs/prd148-domain-ui-audit-current-20260918-r5`。权威工作簿 `01_PRD总表!B60` 仍为“未启动”，当前工作簿 SHA256 为 `e59c8726cc704b9dcb3f6213a668b663464a57e69dce42d166f94f36d2534b4e`。这只证明当前源码下的自动化浏览器页面契约，不证明原生 computer-use、真实付费链、六方会签、发布回滚、对账或 GA。PRD-148 继续未完成。

## 2026-09-18 当前域页面审计

当前源码绑定的 Playwright 域页面审计已完成 69/69：59 个桌面页面与 10 个移动页面均无加载态、HTTP 错误、请求失败、页面异常、横向溢出、控件裁切或交互重叠。PAGE-116 本地模型页已按 Workspace Cookie 会话读取真实 `/local-endpoints`，空列表主体和创建入口可见。

证据：`outputs/prd148-domain-ui-audit-current-20260918-r5/report.json`。该产物明确为自动化浏览器审计，`nativeComputerUse=false`，不构成真实商业流程、六方会签、回滚或 GA 证据。五域批次 `outputs/prd148-negative-current-final-20260916/status.json` 仍因 source_start/source_finish 漂移而不能作为当前一致快照；Chromium 登出后 15000ms 未回首页的失败继续保留在 `outputs/prd148-browser-failure-20260916.json`。

本次 PAGE-116 修复包括 Workspace 导航权限快照投影、Cookie-only owner 校验及无 Bearer Token 的列表请求；受影响前端单测 44/44 通过，后端导航集成测试通过。当前源码摘要以 `outputs/prd148-current-evidence-index-final-20260916.json` 为准，PRD-148 仍未完成。

PRD-095 并发、游标和 warm session P95 单项在本轮隔离重跑通过 1/1（`outputs/prd148-platform-p95-current-20260918.xml`）；此前 50ms 门槛以上的真实失败证据仍保留，门槛没有放宽，单项重跑也不替代五域当前快照或 72 小时稳定性。
2026-09-18 顺序当前源码五域复跑结果：Platform 38/38、Workspace 45/45、Partner/Support 16 通过加 1 个明确退役入口 skipped、Device 14/14；Partner/Support 的 503 用例在独立数据库、无并发条件下仍复现，定位为测试替身未接受当前平台读事务传入的 `csrf_token` 与 `read_only` 参数。补齐替身后完整 17 项顺序复跑为 16 passed、1 skipped（`outputs/prd148-negative-current-20260918-r2/partner-support-seq.xml`）。顺序批次证据见 `outputs/prd148-negative-current-20260918-r2`，绑定源码 SHA256 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`；五域入口已封存，但该证据不关闭真实商业、会签、发布/回滚、财务隐私安全迁移对账、72 小时或 HEAD/origin 门禁。
当前源码的 GA Launch 集成复跑为 14/14（`outputs/prd148-current-ga-launch-20260918-r2.xml`），Gateway 协议/流式/默认模型单元集合为 26/26（`outputs/prd148-gateway-unit-current-20260918.xml`）。一次包含 Gateway 集成的宽集合运行因多个失败标记被中断，未形成通过终态，不能用单元集合替代全量协议、真实 Provider 或商业验收。

五域顺序批次使用隔离数据库，在测试前后封存源码快照。`source_start` 与 `source_finish` 均为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2688 个文件、SHA256 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`，`source_unchanged=true`。

- 已通过：admin、developer、viewer 三种职责共 180 个页面/声明路由，页面/API/浏览器错误 0，横向溢出 0，裁切和交互重叠 0；隔离数据库迁移 153、最新 0155、完整性检查通过。
- 证据范围：这是当前源码绑定的隔离角色页面回归，只证明页面授权回退和布局契约；不证明外部身份、真实注册到付费、企业治理/私有 Gateway、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式稳定性或 GA。
- 未闭合：五域矩阵起止源码漂移、Chromium 登出 15000ms 失败、历史反馈 503/调度争用、计量投影和对账告警、真实商业链、正式 scheduler-enabled 259200 秒观察及 HEAD/origin 一致性。PRD-148 保持未完成。

证据目录：`outputs/prd148-role-audit-current-20260918-r3`；封存清单为 `evidence-manifest.json`，运行报告为 `prd148-role-audit-chromium-scale-1.json`。

权威工作簿已同步本批说明，`01_PRD总表!B60` 仍为“未启动”，SHA256 `7b004f26ecdf29505b1508b9c6356864f8f878e693e08a6d9ac3287b59f00154`。

---

# 2026-09-18 反馈空数据展示修复批次

本批只处理反馈汇总在无样本时的展示语义：平均评分和采纳率显示“暂无数据”，有样本时仍显示真实的 `0`、`0%` 等结果；中英文文案同步，避免把空集合误报成低评分。当前源码为 2688 文件，SHA256 `dc503279acc4febf040a46b28b4a771a8d5a509ab53469541e4e12749be2d4d1`。

- 已通过：反馈相关单测 9/9；Console lint 838 files；生产 surface 校验及 production build 294 assets。生产资源摘要已同步到 `aa58650f6c0b3c7badaaa4c69f5f05cd8717f54bec32ab3187c10894518f318b`，前端和控制面 manifest 一致。
- 真实失败：旧五域矩阵仍有 source_start/source_finish 漂移；Chromium 登出 15000ms 未回到首页；历史反馈 503/调度争用、计量投影/对账告警和旧悬挂账本仍未闭合。
- 测试夹具问题：历史系统临时目录权限错误和 schema trace 过滤问题继续保留在原始产物中；本批没有用夹具结果覆盖这些失败。
- 迁移基线漂移：本批未改迁移，0155 完整链和 WorkspaceSessionContext 修复保留。
- 证据不足：本批不是 PRD-090..147 全量映射、五域当前认证、十一类 QA、真实付费/企业治理/私有 Gateway、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账或正式 259200 秒稳定性观察。

证据目录：`outputs/prd148-feedback-ui-empty-state-20260918`，其中 `evidence-manifest.json` 绑定本批源码和测试输出。PRD-148 继续保持未完成，Excel `01_PRD总表!B60` 仍为“未启动”。

---

# PRD-148 反馈认证目录性能批次（2026-09-17）

本节优先于下方历史状态。PRD-148 未完成；当前源码为 2688 文件，SHA256 `7ca09119f905a01690a4438821b9bbfa6c5c4dba4fd8091e7ee0639c2399b83e`。先在实际 scheduler-enabled Control 中收集分段证据，再集中修改 shared schema helper 和 Workspace 扩展目录读取；未删除任何认证或放宽 250ms 限制。

- 已通过：基础回归 20/20、反馈/Workspace 集成 26/26、Gateway 协议/默认模型 26/26、Ruff；SQLite 跨连接快照与原生 PostgreSQL schema/分区/临时表/大小写/回滚检查通过。普通运行配置后续真实 Owner 顺序反馈读取 24/24 为 200（98.723–185.603ms）；同组织 Owner 13、Developer 12 个 Playwright 操作完成，桌面和手机无 5xx/请求失败，非法日期 422、解释查询/反馈重置 200，Owner 语言已恢复 zh-CN。当前浏览器产物封存源码一致。
- 真实失败：修复前 8/8 反馈 503 保留；同服务阶段诊断 24 次另有 5 次 503。确认多次全元数据扫描是耗时来源：五表查询 10.105→0.148ms、47 表查询 11.766→2.230ms（同只读快照对照中位数）。这些对照与短时后测不关闭全部 503/调度争用，当前计量投影/对账告警仍在。
- 测试夹具问题：初轮 24 个集成 setup 错误来自系统 pytest 临时目录访问拒绝，改用新的项目内目录后全批通过；SQLite 内部 PRAGMA trace 被旧断言误计为外部 SQL，修正后仍约束单次 SQL 调用。原生 PostgreSQL 启动器的 Windows pipe 继承等待已修正并重跑通过，隔离实例已停止。原始产物保留。
- 迁移基线漂移：本批无迁移变更，保留既有 0155 完整链和 WorkspaceSessionContext 修复。
- 证据不足：本批只覆盖指定认证/反馈/协议和两角色页面；未复验完整五域/十一类 QA、所有职责的业务动作、真实付费/治理/私有 Gateway、会签/发布复核/回滚/对账及 72 小时。HEAD/origin 仍不一致。空反馈图表将无样本显示为 0/5 和 0% 的可理解性问题进入下一次体验问题汇总，本批未顺手改界面。

证据目录 `outputs/prd148-feedback-live-profile-20260917`，固定浏览器目录 `outputs/prd148-observability-actions-schema-after-20260917`。[本批分类记录](../../outputs/prd148-feedback-live-profile-20260917/summary.md)说明源码范围。临时同服务 profiling 已移除，Control PID 1640 正常 scheduler-enabled 本机试验中；活动 `control-schema-runtime.log` 不作为不可变目录封存。Excel 只更新说明 B5，251 项校验通过，状态仍为“未启动”；未开始正式观察，未提交或推送。

---

# PRD-148 可观测性体验批次（2026-09-17）

本节优先于历史进度。PRD-148 未完成，Excel `01_PRD总表!B60` 仍为“未启动”。当前源码 SHA256 `3e95d3ff373d2ddeaf30f9dea288664caf5a7164bbf6a81a30c2cc686b85db62`，2688 个文件。先收集请求解释与反馈页面问题，再集中修改可观测性领域。

- 已通过：受影响前端 97/97、全前端 2197/2197、生产摘要契约 1/1；lint 838 files、build 294 assets；Excel 251 项检查。两端生产摘要均为 `6384ef773887e8a647514bd18bd16bca221c57d7f095b95c9704e864d548e402`。本机同一组织 Owner 13、Developer 12 个操作步骤完成，现有请求解释 200、无效 ID 在输入旁提示且不发查询、反馈窗口提示/422/重置 200、桌面和手机布局通过。Owner 的英文解释页面检查后已恢复 zh-CN 偏好。这些是 Playwright 脚本证据，未覆盖 Viewer 或全浏览器。
- 真实失败：修改前 Owner 手机反馈 GET 503，Developer 重置及手机 GET 503。后续有限样本未再出现 5xx，不关闭故障；成功请求仍曾达到 245ms，接近 endpoint-wide 250ms 硬门槛。后台计量投影/对账告警仍在，未提高门槛、跳账或补造权益。英文外层导航和账号区域仍存在混合语言。
- 测试夹具问题：早期脚本未等待异步页面、把受邀 Developer 误称 Viewer，已改成显式选组织并验证实际角色。五个结构断言对既有 `sessionChanged` 守卫变量不兼容，已修正预期，未改动产品会话守卫。原始失败保留。
- 迁移基线漂移：本批未改动迁移或其夹具，既有 0155 与 WorkspaceSessionContext 修复保留。
- 证据不足：旧五域与全浏览器批次已终止，有源码漂移及原始登出失败，不能认证当前源码。PRD-090..147 当前完整映射、十一类 QA、所有角色业务动作、真实商业/治理/私有 Gateway、六方会签/发布复核/回滚/对账、正式 72 小时以及 HEAD/origin 一致性仍未闭合。

本批固定浏览器产物见 `outputs/prd148-observability-actions-after-20260917/evidence-manifest.json`；活动日志 `outputs/prd148-observability-fix-20260917/control-runtime.log` 不纳入固定目录封存。Control 已重载生产摘要并保持 scheduler 开启，仅作本机试验。HEAD `0b3fd65f…` 与实时 origin/main `fbf4c946…` 不一致，未提交或推送。[分类记录](../../outputs/prd148-observability-fix-20260917/summary.md)与主证据索引已关联本批。Excel 仅更新说明 B5，未提升完成状态。

后续反馈分段诊断（12:21 UTC）：当前 scheduler-enabled 服务的新真实 Owner 会话读取 8/8 返回 `feedback.unavailable` 503，客户端 227–257ms，服务器 request ID 已对应。独立进程 ASGI 诊断前两次冷连接 503，后三次 200；热样本三次权限快照重建累计 128–140ms，冷连接 PRAGMA 曾耗时 279ms。阶段为包含时间，不能相加；独立进程未运行 lifespan，不能证明正式服务已恢复或确定其根因。只注销本诊断新建会话，未调用 Provider。详情 `outputs/prd148-feedback-phases-20260917.json`。当前服务仍存在真实失败，前述有限浏览器通过保留其样本范围。

---

# PRD-148 角色页面与夹具复核（2026-09-17）

本节优先于下方历史进度。PRD-148 仍未完成，权威 Excel 状态保持“未启动”。

- 已通过：Playwright 隔离角色页面检查 180/180；PRD-140 outbox 接管单项 1/1；Excel 251 项校验通过。页面批次期间修正了测试文件，源码从 `faf13af0…c4bdd8` 变为 `1317ff40…afc0da`，封存明确 `source_unchanged=false`，不算最终源码认证。
- 后续一致源码回归：GA Launch 14、协议/默认模型 26、跨协议员工身份/计费/停用 1，共 41/41，0 failures/errors/skipped；源码 `1317ff40…afc0da` 前后一致。见 `outputs/prd148-ga-protocol-review-20260917/evidence-manifest.json`；隔离库/合成上游不代替真实商业验收。受影响 Node 夹具回归 4/4，PRD-140 测试文件 Ruff 通过。
- 真实失败：此前 scheduler-enabled 争用/503/Gateway 超时、旧 Chromium 登出超时仍未闭合。只读计量审计确认首条失败为 `platform_metering.owner_invalid`，对应历史订阅/权益为 0，检查点为 0；未跳账或补造权益。
- 夹具问题：Webhook 重叠检测漏算滚动裁剪、权限预期漏算账号动作/高级工具、文档标签误报凭据、outbox 固定日期过期均有诊断及修正；原始失败保留，未放宽性能或租约断言。
- 迁移基线漂移：已有 0155 完整链与 WorkspaceSessionContext 修复保留，本批未更改迁移基线。
- 证据不足：180 页不代表业务动作或全部视口。旧五域与完整浏览器批次均已结束且有源码漂移；全部当前 QA、真实商业/治理/私有 Gateway、六方会签/发布复核/回滚/对账、正式 72 小时及 HEAD/origin 一致性均未闭合。未启动新长稳。

登出补查：已有 before/after 产物复现了异步加载替换鼠标手势中按钮的机制，修复后保留按钮且登出 200 返回首页，原始失败保留。既有 postcopy 旅程仅 developer 执行加载中登出场景，admin/viewer 的该字段 false 表示未执行，并非该场景失败；不扩大为全角色覆盖。当前完整矩阵仍待重新认证。

本地试验服务已在隔离回归结束后恢复（5173/8001/8000，Control scheduler 开启）。Playwright 确认登录表单可见、账号服务连接错误未出现；只证明入口连接，不是已认证业务或正式观察，证据在 `outputs/prd148-local-runtime-restored-20260917`。

本批[分类记录](../../outputs/prd148-role-review-20260917.md)保留截图、夹具定位、体验问题和后续边界。未继续尝试 computer-use；本批页面证据均为脚本。Excel 只更新说明 B5，PRD-148 状态未提升。HEAD 与实时 origin/main 仍分别为 `0b3fd65f…` / `fbf4c946…`。

---

# PRD-148 平台运营批次（2026-09-17）

先以现有平台管理员通过原生内置浏览器巡检 40 个导航入口，再集中修改导航和基础页加载。
首轮含加载中快照，不计为 40 项流程通过。当前源码 2685 文件，摘要
`fe85860e6cc1ccba5495b90a9ceb5c3e17fe97eabb797f3354e317b3870bb054`；PRD-148 与全角色验收仍未完成。

- 已通过：受影响前端 230/230，lint 838 files，build 294 assets；原生组织目录首次加载 13 条，操作员、基础设施、Gateway、审计、通知模板读取本机数据；桌面末尾导航可点击且标题保留在首屏，手机菜单展开、末尾选页及自动收起通过。
- 真实失败：客户成功 HTTP 400 凭据冲突，隐私页也报冲突；状态事件、数据驻留、GA 门禁、组织详情与会话读取曾返回 503；修复后手机组织搜索两次 503，后端请求记录已对应。运营助手依赖不可用、scheduler 计量待对账与争用仍未闭合。
- 夹具与兼容问题：三项旧文案/滚动断言更新；统一回归发现两项页面选择返回值兼容回归，修正实现后 230 项全通过。早期加载中快照不算通过，原生鼠标无法命中旧侧栏尾部时使用键盘继续观察，未冒充点击成功。
- 迁移基线：本批未修改迁移或迁移夹具，既有修复保留。
- 证据不足：页面业务动作、独立多角色、全 QA/五域当前最终源码、真实商业与员工 Gateway、会签/发布/回滚/对账和正式 72 小时均未完成。页面正文仍有混合语言；此前全套测试不能当作新源码全量验收。

证据：[平台修复批次](../../outputs/prd148-native-platform-fix-20260917/summary.md)，
[修改前问题清单](../../outputs/prd148-native-platform-20260917/findings-before-fix.md)。
生产摘要两份源码均为 `945f451ecf3c77cad38cf06aec4524b7dae0fc17682f30aec416dabea38a2685`；Control 已重载为 PID 3700，scheduler 开启，原生总览刷新恢复数据（后端 200、94ms），但启动后仍有 5453ms 数据库慢调用，不能认为可用性已修复。迁移 manifest 接口完整一致性尚未复核。
Excel 未升级状态，未启动新长稳，未 fetch/commit/push；HEAD 仍领先缓存 origin/main 135 个提交。

---

# PRD-148 成员与 KEY 批次（2026-09-17）

当前源码为 2685 文件，SHA256 `788ed041e750dcd19be154cbfe1c6edcdad045c5edf104898cf333dd92e241bb`，HEAD `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`。PRD-148 与全角色 Computer Use 走查仍未完成。

- 已通过：原生管理员邀请→独立开发者接受并登录→管理员改为只读→重新登录后角色首页和旧 KEY 路由回退。KEY 批次按合并发现修改，当前受影响前端 399/399、lint 838 files、build 294 assets 通过；原生应用 KEY 创建、轮换、真实复制、停用与 1440×900 / 390×844 布局复核通过。测试创建的两把应用 KEY 均已停用。
- 真实观测失败：原生页面仍间歇性 `Failed to fetch`，首次列表加载和轮换后的刷新均出现，读取重试后恢复；不据此判定根因或宣称修复。后台 scheduler 仍有数据库争用与计量待对账告警。
- 工具/夹具问题：原生 `fill("")` 未清空旧值，改用键盘清空后通过必填拦截；视口切换后的即时截图有残帧，稳定截图另存。当前 399 项未出现夹具失败。
- 迁移基线：本批未改迁移或迁移夹具。上一开发者批次的 page-visit 基线修复及单项通过独立封存。
- 证据不足：平台运营独立身份、所有页面与职责、存活会话即时撤权、个人员工真实 Gateway/计量全流程、全 QA、会签/发布/回滚/对账与正式 72 小时仍未完成。账号栏目切换的加载闪动待后续批量处理。

当前证据：[成员与 KEY 修复批次](../../outputs/prd148-native-member-key-fix-20260917/summary.md)。修改前的[成员角色观察](../../outputs/prd148-native-members-20260917/findings-before-fix.md)及[上一开发者批次](../../outputs/prd148-native-developer-20260917/summary.md)有独立源码封存，不合并冒充当前全量验收。前后端生产摘要同步为 `a07f12febc95b46eca2db9c907ea3c2330ef733918dfbe2d586bdaaff947f2f1`。运行 PID 为 Control 16600、Gateway 2512、前端 7412；scheduler 开启，但未启动正式稳定性观察。Excel 本批未修改；HEAD/origin 未同步、未提交或推送。

---

# PRD-148 浏览器控制复核（2026-09-17）

本节更正下方历史记录中的浏览器可用性描述，不更新整体验收结论。PRD-148 仍未完成。

- 已通过：Computer Use 加载和应用枚举；原生 Codex 内置浏览器读取当前登录页面、实际点击“API 文档”、加载配置示例并截图。已恢复默认视口。
- 工具问题：统一 CUA 入口仍拒绝当前 `apikey` 认证方式，未修复该入口；原生入口可用于继续操作。清理清单的删除路径全部位于工作区，没有删除浏览器组件。
- 真实观测失败：迁移页仍显示配置加载失败；此前间歇性 `Failed to fetch` 的产品或传输归因尚未确定。
- 测试夹具及迁移基线：本次诊断未新增测试，也未修改夹具或迁移。
- 证据不足：未证明统一入口的确切根因、其他版本的兼容情况、全角色业务流程或正式 72 小时稳定性。浏览器基本操作通过不能计作 GA。

详见 [浏览器控制诊断](../../outputs/browser-control-diagnostic-20260917/summary.md)。

---

# PRD-148 最新进度（2026-09-16）

PRD-148 仍未完成，权威 Excel `01_PRD总表!B60` 保持“未完成”。当前源码 SHA256 为 `6348c7766544b91625140779cc147a37cc0ca9b340b3b7e1d42fa667d37a71be`（2677 个源码文件，HEAD `0b3fd65f…`）。Computer Use 已恢复初始化和应用枚举，但 Edge 既有页面 URL 无法被策略可靠识别，原生页面操作未执行；Playwright 证据单独标注。反馈专项最终 16/16、GA Launch 14/14、Platform P95 1/1 通过。证据索引为 `outputs/prd148-current-evidence-index-followup2-20260916.json`。

本轮按业务域完成一次合并审计后修复 DLP：Workspace Cookie/CSRF、跨组织同形隔离、事务内撤权和本机 UI 创建→停用→刷新保留均通过；影响前端回归 479/479，DLP 后端事务回归 1/1。当前 Control 重载后的本机只读接口中 DLP、Developer API/Changelog/Schema/SDK、反馈摘要均为 200；Webhook 仍为 `auth.token_missing`，需继续修复。GA Launch 14/14、Gateway 协议 27/27、Platform 并发/P95 单项 1/1 通过，均为自动化或本机证据，不能代替 GA。

原生 computer-use 初始化失败，错误为 `failed to write kernel assets` / `os error 3`；本轮使用 Playwright 脚本并明确标注，不能冒充原生桌面验收。正式 scheduler-enabled 连续 259200 秒观察尚未启动；历史 formal11 及短时健康观察计入 0 秒。真实订阅/权益/结算账本、企业治理/私有 Gateway、六方会签、发布后复核、真实回滚、财务/隐私/安全/迁移对账和 HEAD/origin 一致性仍缺证据。五域矩阵历史批次有源码漂移，需在当前最终快照继续重跑。工作树保留用户改动，未 reset、checkout 或广泛删除。

---

# PRD-148 最新进度（2026-09-16）

PRD-148 仍未完成，权威 Excel `01_PRD总表!B60` 保持“未启动”。最新结构化索引为 [prd148-current-evidence-index-final-20260916.json](../../outputs/prd148-current-evidence-index-final-20260916.json)。下方旧日期记录保留为历史，不能替代当前验收。

当前源码 SHA256 为 `60f18aa294d505f9e3ef8285e3673963bdb9d39fb6cab29ba878b8fb9a5a6222`（2676 文件，HEAD `0b3fd65f…`）。前端与控制面生产资源摘要均已同步为 `08f10629e3a937d8e354c95d4041874b8419a1de6d0784abe76027b9c1d4ca5a`，manifest 合同单项与 293 assets 构建通过。

- 已通过的本轮测试执行结果：Platform 38/38（P95 仍要求 <50ms）；Workspace 主集 44/44 加扩展 18/18；Gateway 协议/默认模型 26/26；跨协议员工身份/计费/停用 1/1；摘要同步后的 GA Launch 14/14。前端完整单测 2141/2141。公开入口四尺寸审计通过，自动化隔离库默认关闭人机挑战；本机 dev 注册策略当前也为 human_verification=0。上述自动化不证明真实商业或 GA。
- 真实观测失败：完整浏览器矩阵 Chromium 1× 登出后 15000ms 未返回首页，原因待定位，不能先归为夹具或放宽门槛；见 `outputs/prd148-browser-failure-20260916.json`。formal11 因 observation_source_changed 失败，约 6057 秒、34005 请求、0 errors，GA 计入 0 秒。
- 夹具问题：系统 Python 没有 pytest；默认 pytest 临时目录权限拒绝及缺失 basetemp 父目录；更换项目虚拟环境和独立目录后 GA 14/14。Windows PowerShell QA 运行器在子进程退出后卡住，已保留 `20260916-011634/runner-status.json` 并用当前 PowerShell 重跑。dashboard projection 由真实 worker 预构建仍只属隔离 QA 准备。
- 迁移基线漂移：既有完整链 0155 与 WorkspaceSessionContext 夹具修正保留，本轮未放宽迁移断言。
- 证据不足：PRD-090..147 的逐项当前验收、Partner/Support 退役 skipped 处置、全部 11 组最终 QA、真实订阅/权益/结算账本、企业治理/私有 Gateway、真实六方会签、发布后复核、回滚、财务/隐私/安全/迁移对账、72 小时及 HEAD/origin 一致性均未闭合。本机只读审计确认发布/会签/回滚/复核记录为 0，订阅与权益为 0。

五域主批 `outputs/prd148-negative-current-final-20260916` 的 Partner/Support、Device，以及全浏览器 QA `.artifacts/product-qa/20260916-012939` 仍在运行。批次开始源码为 `604cab2a…4bbf86`，期间同步了控制面摘要，因此即使执行结果通过也不能作为最终一致快照。已设置本任务每小时续查终态和失败，续查机制不是 MetaHub 正式长稳。新正式 259200 秒观察尚未启动。

远端实时核对：origin/main 为 `fbf4c946b403077742e1b2d3c08a573e135308f7`，HEAD 领先 135 个提交；工作树保留已有改动，未提交或推送本批。

---

# PRD-148 当前验收记录

## 2026-09-15 当前源码追加复核

PRD-148 仍为**未完成**。当前源码快照为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、2674 个源码文件、SHA-256 `4a8f9efbf0319204d49280fd9398a761dc340be2e222fb5bfa0aef74700b08c2`。

- 当前来源一致的 Product QA All 批次 `.artifacts/product-qa/20260915-213534/` 为 5/5：WB/API/BB/GR/SEC、隔离 PERF、WEB/UI/UCD/COMP/FLOW、lint 和 Chromium 真实角色旅程均通过。另有 `.artifacts/product-qa/20260915-214141/` 的 PERF 隔离、loopback smoke、600 请求/12 并发压力 3/3 通过；这些批次不等于 72 小时业务稳定性。
- 当前 Chromium 角色证据覆盖 admin、developer、viewer，视口 1280×720、1440×900、390×844；0 非预期 console error、0 request failure、0 横向溢出，见 `.artifacts/product-qa/20260915-213534/browser-real-chromium-scale-1.json`。
- GA 生命周期重跑 `outputs/prd148-rerun-20260915/ga-launch-rerun2.xml` 为 14/14；协议门面、流式、默认路由和模型别名相关回归 `outputs/prd148-rerun-20260915/protocol-defaults-rerun2.xml` 为 24/24。两次首次直接运行的临时目录 WinError 5 仅为测试夹具问题，已用仓库内唯一 basetemp 重跑通过。
- scheduler-enabled formal11 当前仍为 `running`，最新状态约 4226 秒、0 errors，要求 259200 秒；健康采样范围也明确声明不构成业务稳定性、事故分级或独立批准证据，不能计作 GA。
- 真实本机试用链路仍只证明注册→组织→KIMI Provider/模型/默认路由→员工 KEY→Gateway→撤权；当前没有已发布套餐、订阅、有效权益快照或结算用量，外部付费/额度、企业治理/私有 Gateway、六方会签、真实发布回滚和财务/隐私/安全/迁移对账仍缺证据。

本次追加证据索引为 `outputs/prd148-current-evidence-index-final-20260915.json`。Excel 中 `01_PRD总表!B60` 继续保持“未启动”。

2026-09-15 性能复核：完整 Platform 集合曾以 60.45ms P95 失败，随后单项复核为 54.18ms，确认不是一次性波动。对平台 edge 权限目录发现结果做同一授权检查内缓存后，单项测试通过 `<50ms` 门槛，证据为 `outputs/prd148-platform-p95-cache-20260915.xml` 及 `outputs/prd148-platform-p95-rerun-current-20260915.json`。完整五域矩阵尚未因该源码变化重跑。迁移基线候选已应用，受影响回归仍在运行。

## 2026-09-15 0155 回滚修复后的当前状态

PRD-148 **未完成**。本节优先于下方历史记录。当前 HEAD 仍为
`0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，2674 个受测源码文件摘要为
`9904e348b329974e1d53cf40cb7706af337c1bf9c97e4f5c360ee6ee000cebfe`。

- 已通过：修复前源码 `da72cabf…571e7a` 的完整隔离 Product QA
  `.artifacts/product-qa/20260915-122328/` 为 15/15 任务，九组浏览器共
  27/27 角色旅程、loopback 30/30、压力 600/600。97 项审计为 **95/97**，
  PERF-08、PERF-10 因没有合格长稳证据未通过。审计明确拒绝 formal10，
  见 `outputs/prd148-case-audit-pre-review-fix-20260915.json`。这不是 GA 完成。
- 真实失败及修复：本机一致备份在正式迁移入口执行 0155 down 时被拒绝，
  原因是缺少入口要求的 `PRAGMA legacy_alter_table = ON`。修复仅补充 down
  的受信事务声明。失败保留在 `outputs/prd148-current-local-migration-20260915.json`。
  修复候选的副本往返保留 1094 表、543694 行，完整性正常、外键违规 0，
  见 `outputs/prd148-review-migration-candidate-20260915.json`。
  当前仓库源码随后同样通过正式入口往返，前后摘要一致，结果封存于
  `outputs/prd148-current-local-migration-fixed-20260915.json`。它是本机副本迁移
  证据，`real_ga_rollback=false`，不代替实际发布回滚或独立签署的对账。
- 已通过：GA 集成回归 `outputs/prd148-ga-review-rollback-20260915.xml` 为
  **14/14**，覆盖正式迁移入口的空事实往返、单次复盘逐行保留、双次复盘由
  数据保护条件拒绝 down，且保留版本和事实。模拟身份、签署及上游仅用于合同测试。
- 新源码完整自动化 QA 已通过：`.artifacts/product-qa/20260915-135730/` 为
  15/15 任务，来源摘要未变化。97 项审计仍为 **95/97**，PERF-08、PERF-10
  无合格长稳证据，见 `outputs/prd148-case-audit-after-review-fix-20260915.json`。
  五域已选负向集合、Gateway 及商业合同仍由既有 runner 顺序验证。
  `outputs/prd148-validation-after-review-fix-20260915/status.json` 记录终态；
  运行中不算通过。旧五域矩阵为 112 passed + 1 retired skipped，已转为上一源码证据。
- 测试夹具问题：PRD-132 身份上下文及既有夹具修复保留。原 GA 集成文件有
  18 条 Ruff E501 长行提示，本次没有新增。没有将退役 Partner skip 计为通过。
- 迁移基线漂移：静态核对另发现 10 处完整链测试仍断言 0122/0139，尚未执行
  不能写作产品失败。详见 `outputs/prd148-migration-baseline-review-20260915.json`。
  明确使用历史迁移子集的测试及 native dispatch 替身不作批量替换。
- 证据不足：前置 58 个 PRD 的 158 个集成文件、614 个测试函数已清点，现有选定
  回归不足以证明每个要求完成，见 `outputs/prd148-predecessor-inventory-20260915.json`。
  本机套餐仍未发布，订阅/entitlement 为 0；真实付费/计量/额度、企业治理/私有
  Gateway、独立六方会签、发布复核、实际回滚及对账仍缺证据。
- 本机迁移事实不足：只读审计 `outputs/prd148-console-migration-audit-20260915.json`
  显示 Workspace/Platform 分域迁移行均为 0，入口为 `expanded`、generation 0，
  旧入口写入围栏为 0。分域对账、契约发布、审批、安全复核、backfill checkpoint
  和旧入口退役记录均为 0。37 个 scheduler 登记及 64 条队列契约是目录事实，
  不代表实际迁移、切换或签署验收完成；10 个导航快照也不替代这些运行事实。
- 正式连续 72 小时仍未启动合格观察。`.env` 配置 scheduler enabled，但配置值
  不等于运行事实；健康接口观察均不计入 GA。真实 origin/main 仍为
  `fbf4c946b403077742e1b2d3c08a573e135308f7`，本地领先 135 个提交，未推送。

Excel 只向 `01_PRD总表!Y60` 追加当前说明，B60 保持“未启动”，所有其他单元格、
sheet 及 package part 不变，验证见
`outputs/prd148-workbook-preservation-review-fix-20260915.json`。

## 2026-09-15 final-source verification

PRD-148 remains **未完成**. The final current-source snapshot is revision
`0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`, 2674 source files, SHA-256
`da72cabf5344e1c06e96d5d582b3cd184b36293485388036760d96ab3b571e7a`.

- The five-domain negative matrix is bound to this snapshot: Platform 38/38,
  Workspace 44/44, Partner/Support 16 passed plus one retired-route skip, and
  Device 14/14. Current artifacts are
  `outputs/prd148-negative-platform-current-final-20260915.xml`,
  `outputs/prd148-negative-workspace-current-final2-20260915.xml`,
  `outputs/prd148-negative-partner-support-current-final2-20260915.xml`, and
  `outputs/prd148-negative-device-current-final2-20260915.xml`.
- Gateway protocol/default-model/cross-protocol identity and accounting
  contracts are 27/27 in `outputs/prd148-gateway-protocol-current-final-20260915.xml`.
  PRD-098 catalog/subscription and PRD-103 payment-commerce contract tests are
  2/2 in `outputs/prd148-commercial-contract-current-20260915.xml`; they use
  isolated mockpay fixtures and do not prove an external paid activation.
- The final-source Product QA batch `.artifacts/product-qa/20260915-100455/`
  completed 4/4 contract/isolated runner tasks with source unchanged. It omitted
  the real browser matrix, live/pressure runs and long stability, so it does not
  close all 11 QA groups or the 97-case audit. The full 15-task run is in progress
  at `.artifacts/product-qa/20260915-122328/` with `-RunBrowserMatrix
  -RunLivePerformance -RunPressure`.
- The scheduler-enabled formal6 observation is failed, not running: 750.716
  seconds, 4183 requests, and three Control `read_timeout` errors. It contributes
  zero to the required 259200 seconds.
- The local unfamiliar-user trial remains a trial-only record. It proves
  registration, organization, KIMI Provider/model/default route, employee
  consumer, personal key, Gateway call, revocation and post-revocation 401, but
  the organization still has no subscription/entitlement or settled usage
  ledger. A current read-only check found one unpublished `trial-local` draft
  plan, zero subscriptions/entitlements and one existing Platform operator.
  The real commercial audit remains blocked by
  `platform_metering.owner_invalid`.
- No current evidence proves external payment, enterprise governance/private
  Gateway, six-party signoff, real release/rollback/post-review, finance/privacy/
  security/migration reconciliation, or a scheduler-enabled 72-hour run. HEAD is
  still 135 commits ahead of cached `origin/main`; PRD-148 stays incomplete.

The structured index for this snapshot is
`outputs/prd148-current-evidence-index-20260915.json`.

The scheduler-enabled formal9 attempt recorded 372.56 seconds and 2115 requests
with zero errors before its observer process disappeared without a terminal
result. `outputs/continuous-stability-20260915-formal9/status-reconciliation.json`
records that interruption and it contributes zero toward GA. A fresh formal10
observation was started from the current source snapshot. It was deliberately
stopped after 689.22 seconds and 3864 requests with zero errors because the full
QA preconditions were unmet and the sampler covers anonymous health endpoints,
not authenticated business flows or incident severity. Its disposition is
`outputs/continuous-stability-20260915-formal10/status-reconciliation.json`.
No eligible formal observation is running. The 259200-second business stability
and zero-P0/P1 requirements remain unproven. Earlier formal7 and
formal8 startup attempts were rejected before observation because of runner
configuration mistakes; their errors are retained and are not product evidence.

## 2026-09-15 current-source follow-up

PRD-148 remains **未完成**. A real security defect was found in the PRD-145 approval cursor decoder: non-canonical Base64url aliases could preserve the decoded signature bytes. The decoder now enforces canonical re-encoding, and `outputs/prd148-prd145-cursor-fix-20260915.xml` passes the deterministic platform/workspace regression and the original approval-queue integration case.

The post-fix Workspace negative matrix is current and green: `outputs/prd148-negative-workspace-postcursorfix-20260915.xml` is 44/44. The Platform matrix was rerun after both cursor fixes and is 38/38 in `outputs/prd148-negative-platform-current-final-20260915.xml`; the Partner/Support (16 passed + 1 retired skip) and Device (14/14) artifacts still need to be rebound to the final source snapshot before the five-domain matrix can be called complete. The PRD-148 GA lifecycle rerun is 13/13 in `outputs/prd148-ga-launch-postcursorfix-20260915.xml`.

The same canonical Base64url boundary was also fixed in `control-plane-api/src/metahub_control/platform_customer_success/service.py` after PRD-140's customer-account cursor test exposed the alias. The targeted PRD-140, PRD-145, and hardening regressions pass. The final source snapshot for this follow-up is `outputs/prd148-current-source-final2-20260915.json`.

The formal scheduler-enabled observation `outputs/continuous-stability-20260914-formal6/status.json` failed after 750.716 seconds with three Control `read_timeout` errors. It has zero value toward the required 259,200 seconds. The real commercial audit remains blocked: no SaaS plan, subscription, entitlement, paid activation, or usage ledger settlement exists; the observed metering failure is `platform_metering.owner_invalid`. The combined Gateway rerun was interrupted after a fixture stall and is not a pass. Current structured evidence is indexed in `outputs/prd148-current-evidence-index-20260915.json`.

核对日期：2026-09-14。PRD-148 **未完成**，Excel `01_PRD总表!B60` 保持“未启动”。依据同表 `Q60`、`V60` 和 G8 门禁，任何局部回归、模拟流程、匿名 HTTP 或短时观察均不能代替 GA 验收。

## 已通过

- 路线图结构校验：251 checks，`next_prd=PRD-148`。该校验不证明 PRD-090..147 的所有当前业务流程和历史 G6 长稳已经完成。
- SQLite 连接生命周期、提交权限围栏、可信遥测绑定、遥测持久化、PRD-147 调度器回归：83/83，`outputs/prd148-sqlite-telemetry-regression-rerun-20260914.xml`。其中新增用例验证 schema 解析期间不持有写锁、初始化失败释放连接、20 次遥测批次复用一个连接且持久化计数和水位完整。
- 连接池修复后的受影响回归 `outputs/prd148-pool-state-regression-20260914.xml` 为 88/88，源码摘要 `f2a72b4e801d08cd94789467fa80d505e550909d8e234908595e203accde21bd`。新增验证覆盖 20 次借还仅编译查询一次、复用查询仍读取当前数据、四种被修改的 SQLite 安全开关均恢复默认值。本批修改文件 Ruff 通过。
- `outputs/prd148-ga-current3-20260914.xml`：GA 生命周期 13/13 通过；`outputs/prd148-gateway-unit-current-20260914.xml` 为 Gateway 协议/默认模型 26/26，`outputs/prd148-gateway-integration-current-20260914.xml` 为跨协议身份/计量/停用 1/1。`outputs/prd148-gateway-one2-20260914.xml` 的成本回归 1/1、`outputs/prd148-gateway-broken3-20260914.xml` 的损坏价格防护 4/4。它们是隔离数据库/测试身份的工程证据，不是实际 GA 业务事实。
- 最终源码五域矩阵 `outputs/prd148-negative-current2-20260914/status.json` 完成且 `source_unchanged=true`：Platform 38/38、Workspace 44/44、Partner/Support 16 passed + 1 个退役入口 skipped、Device 14/14。
- 最终源码 Product QA `.artifacts/product-qa/20260914-194336/` 的 WB/API/BB/GR/SEC、PERF、WEB/UI/UCD/COMP/FLOW 合同、Console lint、loopback PERF smoke 全部任务通过；`.artifacts/product-qa/20260914-195005/` 的真实 Chromium FLOW 也通过。

## 真实失败及修复

- 旧正式观察 `outputs/continuous-stability-20260913-formal/status.json` 已失败：565.31 秒、9 个窗口、3084 次请求、3 次 Control read_timeout。`outputs/continuous-stability-20260914-formal2/status.json` 也已在 251.12 秒、1389 次请求、4 次 Control read_timeout 后失败；两次观察均不能计入正式 72 小时。
- scheduler-enabled 诊断 `outputs/prd148-runtime-check-20260914/status.json` 已结束：616.98 秒、1191 次请求、1182 次 200、9 次 ReadTimeout。终态后已核对命令行并停止原 Control PID 10776。诊断后段与受影响单测重叠，早于单测的超时仍可独立复现；不将这段负载作为正式运行基准。
- 栈采样显示遥测线程在持有 SQLite 写锁后执行首次 schema 读取，验证码及后台队列在等待同一写锁。只读成本测量 `outputs/prd148-sqlite-schema-cost-20260914.json`：冷 schema 解析约 361–519 ms，约分配 10.8 MB；热业务读取不足 0.1 ms。
- `shared-sdk/python/metahub_shared/db/connection.py` 在连接交付前解析 schema，并关闭预热游标，避免冷解析占用业务写锁；初始化异常关闭连接。`shared-sdk/python/metahub_shared/observability/runtime.py` 的批次持久化复用有界连接池。后台调度器继续开启，SQLite 超时和 Platform 性能门槛没有放宽。
- 修复后的实际运行复测仍需终态证据；83 项单测不证明运行争用或内存问题已经全部消失。
- PRD-095 压力矩阵曾为 P95=60.53 ms；在权限查询按当前角色直接读取扩展表的修复后，当前 Platform 集合 `outputs/prd148-negative-platform-current2-20260914.xml` 已 38/38 通过，原 `<50 ms` 门槛保留。其余四域仍需同一最终源码快照封存。

## 测试夹具问题

- 首轮 `outputs/prd148-sqlite-telemetry-regression-20260914.xml` 为 26 passed、57 errors，均因系统 pytest 临时目录 WinError 5 而未能建立夹具。改用仓库内唯一临时目录后 83/83 通过，保留原失败记录。
- 已有 PRD-132 附件重认证夹具修复保留：返回 WorkspaceSessionContext。
- Partner 旧入口的 1 项 skipped 属退役接口，不记为通过；Channel API 的撤权、双人审批和域隔离替代用例另列证据。

## 迁移基线漂移

- 已有集成夹具从 0122/0134/0139 更新到完整 0155 链的修改保留。
- `outputs/prd148-metering-audit-20260914.json`：本地试用库计量水位为 0，第一条用量失败为 `platform_metering.owner_invalid`。所有权绑定为 1，但订阅数和用量发生时有效权益快照数均为 0；另有 1 条 pending。它需要迁移/财务来源核对，不能通过跳过水位、补造合同或直接删除历史用量消除。

## 证据不足

- 旧五域集合 `outputs/prd148-negative-current-20260914/status.json` 记录 Platform 37/38、Workspace 44/44、Device 14/14、Partner/Support 16 passed 加 1 个退役入口 skipped。最终源码快照的五域重跑已启动，Platform 当前 38/38；在四域完成前不能宣称矩阵整体通过。
- WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW 尚无本批完整、来源一致的最终通过记录。

## 2026-09-14 current-run additions

- Platform session concurrency/warm-performance gate is now green at 1/1 in
  `outputs/prd148-platform-authority-rerun-20260914.xml`; the `<50 ms` P95
  threshold was retained.
- A user-authorized local trial completed registration, organization creation,
  KIMI Provider/model/default route setup, employee consumer and personal key
  creation, Gateway call, key revocation, and post-revocation rejection. The
  redacted record is `outputs/prd148-real-local-flow-20260914.json`.
- The same organization has no SaaS catalog plan, subscription, entitlement,
  or FinOps ledger. Gateway request logging succeeds, while usage-ledger
  posting is deferred with `IntegrityError`; this is recorded as a real
  commercial/migration baseline blocker in
  `outputs/prd148-real-commercial-audit-20260914.json` and is not counted as
  metering or paid-flow acceptance.
- The prior five-domain rerun in
  `outputs/prd148-negative-current-20260914/status.json` used the pre-fix
  source and recorded Platform 37/38. The final-source matrix
  `outputs/prd148-negative-current2-20260914/status.json` is source-unchanged:
  Platform 38/38, Workspace 44/44, Partner/Support 16 passed plus one retired
  route skipped, and Device 14/14. The retired-route skip remains an evidence
  limitation and is not counted as a full 17/17 pass.
- Product QA final-source batch `.artifacts/product-qa/20260914-194336/` passed
  WB/API/BB/GR/SEC, isolated PERF, WEB/UI/UCD/COMP/FLOW contracts, Console
  lint, and loopback PERF smoke. The real Chromium flow batch
  `.artifacts/product-qa/20260914-195005/` passed FLOW-real-chromium-scale-1.
  These are QA batch evidence only and do not close the real deployment,
  commercial, signoff, or 72-hour gates.
- Current-source Gateway protocol/default-model unit tests passed in
  `outputs/prd148-gateway-unit-current-20260914.xml`; the isolated cross-protocol
  identity/accounting integration test passed in
  `outputs/prd148-gateway-integration-current-20260914.xml`. The combined run
  was terminated after a >30 minute fixture stall and is retained as a fixture
  issue, not a pass.
- The bounded KIMI credential audit passed with AES-GCM storage and no exact
  credential/master-secret findings in the scanned local evidence scope:
  `outputs/prd148-kimi-security-audit-20260914.json`.
- The scheduler-enabled observation at
  `outputs/continuous-stability-20260914-formal2/status.json` terminated as
  failed after 251.12 seconds and 1,389 requests because Control had four
  read timeouts. A fresh formal observation must start only after current QA
  and matrix evidence are sealed; no short observation counts as 72-hour GA.
- After health probes stopped acquiring pooled database handles, the
  scheduler-enabled five-minute diagnostic
  `outputs/continuous-stability-20260914-diagnostic3/status.json` completed
  with 1,703 requests and zero errors. It is remediation evidence only.
- `outputs/continuous-stability-20260914-formal3/status.json` is failed after
  120.35 seconds and 632 requests with 7 errors (six Control read timeouts and
  one Gateway connection reset). Control logs show SQLite scheduler registry
  release/renew contention and metering reconciliation warnings; it cannot
  count as GA.
- `outputs/continuous-stability-20260914-formal4-isolated/status.json` also
  failed after 371.93 seconds and 2,040 requests with 7 Control read timeouts.
  The failure reproduced against a database copy, so the scheduler-enabled
  SQLite contention is an implementation/runtime blocker rather than only
  current business-library backlog. No formal 72-hour observation is running.
- Shared SQLite connections now enable WAL once per database (with schema warm-up
  retained) so scheduler-enabled readers do not wait behind rollback-journal
  writers. A fresh 120-second scheduler-enabled diagnostic recorded 704 requests
  and 0 errors in `outputs/continuous-stability-20260914-wal-diagnostic/status.json`.
  This is remediation evidence only. The PRD-095 isolated warm-session benchmark
  still measured P95 61.6ms against the unchanged <50ms gate in
  `outputs/prd148-platform-p95-wal3-20260914.xml`; that result remains a real
  performance failure and is not waived.
- 真实注册、邮箱验证、组织、Provider、模型、默认路由、员工个人 KEY、真实上游调用、计量/额度、撤权，以及付费、企业治理、私有 Gateway 的完整业务事实仍不足。
- 六方真实独立会签、支持交接、实际发布/回滚回执、发布后复核、财务/隐私/安全/迁移最终对账仍不足。测试中的身份、时钟、上游和审批替身仅证明程序行为。
- 本批尚未完成正式 259200 秒观察。formal3 与 formal4-isolated 均已失败；既有 ga10/ga11/ga12、formal/formal2、scheduler-disabled 和所有短时匿名 HTTP 记录均不构成 GA 长稳或 0 个 P0/P1 的事实。下一次正式观察必须在修复并重新验证 scheduler-enabled 运行时后启动。
- `outputs/prd148-real-evidence-audit-20260914.json` 只读核对：本地为 dev 配置，没有发布 sink；候选、决策、GA 证据、发布、回滚、复盘与投递记录均为 0，不能据此发起真实会签或宣称发布已执行。
- HEAD 为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，当前本地领先缓存的 origin/main 135 个提交，工作树包含此前改动。本批没有提交或推送，README/Excel/docs/HEAD/origin 的最终一致性未满足。

员工交付约定继续保持：一个 MetaHub 网关地址和一把个人 MetaHub KEY；管理员配置默认路由后通常不需要员工填模型。环境变量属于客户端保存配置的方式。不同协议路径使用同一 KEY，具体 Agent 客户端版本须独立验收。

结构化索引：`outputs/prd148-current-evidence-index-20260914.json`。2026-09-13 索引作为历史保留，其中正式观察的 running 字段已由实际 failed 终态推翻。

## 2026-09-15 current-source matrix and browser rerun

本轮源码快照为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，源码闭集 2674 个文件，摘要 `4a8f9efbf0319204d49280fd9398a761dc340be2e222fb5bfa0aef74700b08c2`。

- Platform 负向矩阵 `outputs/prd148-negative-platform-current-final2-20260915.xml`：38/38 通过。PRD-095 session P95 在缓存修复后的单项复跑通过 `<50ms`；缓存以数据库文件变更签名失效，未放宽门槛。
- Workspace 当前集合 `outputs/prd148-negative-workspace-current-exact2-20260915.xml`：62/62 通过，覆盖既有 44 条集合及当前源码扩展用例。
- Partner/Support `outputs/prd148-negative-partner-support-current-exact-20260915.xml`：17 条，16 通过，1 条退役 Partner 路由显式 skipped；不将 skipped 计为通过。
- Device `outputs/prd148-negative-device-current-exact-20260915.xml`：14/14 通过；此前覆盖的 18/18 子集也通过。
- GA/Gateway 集成 `outputs/prd148-ga-gateway-current-final-20260915.xml`：40/40 通过。
- Platform 浏览器旅程 `outputs/prd148-platform-browser-rerun3-20260915/prd147-platform-chromium-scale-1.json`：登录、平台迁移状态、1440/1280/390 三视口通过，0 控制台错误、0 请求失败、0 横向溢出。首次超时属于夹具只等待 identity 页登出控件，当前登录约定会自动进入 console 页，夹具已兼容两种登出控件。

这些结果只证明当前源码的自动化/浏览器回归，不证明外部付费、企业治理、私有 Gateway、六方会签、真实发布回滚或 72 小时 scheduler-enabled 稳定性。PRD-148 继续保持“未完成”。结构化索引见 `outputs/prd148-current-evidence-index-after-cache-20260915.json`。

### 正式观察启动状态（2026-09-15）

QA 回归后已按 scheduler-enabled 正式配置启动新的观察 `outputs/continuous-stability-20260915-formal11/status.json`。当前状态为 `running`，最近状态文件记录约 372 秒、0 错误；要求为 259200 秒且 0 个 P0/P1。运行中不计为通过，进程 PID 6780 仍在运行，不能替代 72 小时终态证据。

## 2026-09-16 当前复核（PRD-148 仍未完成）

本轮保留 `PRD-148=未启动`。生产资源摘要已按当前源码更新并通过 `console-web` 的 production surface 校验与构建（293 assets）。隔离浏览器 QA 默认关闭人机挑战，数据库夹具返回 `registration_human_verification_enabled=false`；这是自动化夹具设置，不是生产环境安全策略变更。

当前已确认的回归结果：前端完整单测 2141/2141；GA Launch 14/14；Gateway 协议/默认路由 24/24；此前源码绑定的 Platform 38/38、Workspace 62/62、Device 14/14、Partner/Support 16 passed + 1 retired skipped 仍保留，但本轮夹具源码变更后需要重新绑定五域矩阵才能作为当前快照证据。公开入口审计 `product-qa-public-audit-20260916-final` 通过，匿名登录 401、注册策略 200、页面无错误和布局溢出；最终关闭挑战的复跑正在收尾。

正式长稳不能计入：`outputs/continuous-stability-20260915-formal11/status.json` 以 `observation_source_changed` 失败，6057 秒、34005 requests、0 errors，要求 259200 秒；它既不是 72 小时证据，也不是业务流程证据。旧 formal/ga10/ga11/ga12、scheduler-disabled、健康接口和短时观察继续不计入 GA。

仍未闭合的硬门槛：真实付费计划/订阅/权益和结算用量账本；企业治理和私有 Gateway；真实六方会签、发布后复核、回滚和财务/隐私/安全/迁移对账；WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW 的最终全量当前快照；Partner/Support 退役路由的完整 17/17；scheduler-enabled 连续 259200 秒且 0 P0/P1；HEAD 与 origin/main 一致。真实本地注册→组织→Provider→模型→路由→员工→个人 KEY→Gateway→计量/额度→撤权只证明本地试用链路，不能宣称 GA。

### 2026-09-16 最终源码绑定矩阵复跑

最终四域负向矩阵已在同一源码快照 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、摘要
`dca5b62349450beb0da5cd55812cc5e1b5532c1eb76d6ffb935b9f487a829984` 下完成，起止摘要一致：

- Platform：38/38，通过 `outputs/prd148-negative-current-rerun4-20260916/platform.xml`。
- Workspace：44/44，通过 `outputs/prd148-negative-current-rerun4-20260916/workspace.xml`。
- Partner/Support：16 通过、1 个已退役 Partner 路由显式 skipped；不把 skipped 计为通过。
- Device：14/14，通过 `outputs/prd148-negative-current-rerun4-20260916/device.xml`。
- 证据封存：`outputs/prd148-negative-current-rerun4-20260916/evidence-manifest.json`，`source_unchanged=true`。

反馈汇总当前回归为 11/11，认证 deadline 单项与共享汇总资源测试通过；数据库连接层把 deadline 传播到 SQLite connect/busy timeout，认证 deadline 连接跳过冷 schema 预解析，短锁等待快速失败。受影响源码改动后，先前矩阵均视为过期，以上 rerun4 才是当前矩阵证据。

这些结果仍不构成 PRD-148 完成。真实付费套餐/订阅/权益和结算账本、企业治理与私有 Gateway、六方会签、发布后复核与回滚、财务/隐私/安全/迁移对账、全部 QA 最终快照、scheduler-enabled 连续 259200 秒稳定性，以及 README/Excel/docs/HEAD/origin 一致性仍未全部具备当前可追溯证据。

Excel 状态已同步为 `01_PRD总表!B60=未启动`，工作簿输出为 `outputs/prd148-workbook-update-followup3-20260916/output.xlsx`；ZIP 完整性校验通过。HEAD 与 origin/main 仍有 135 个提交差异，因此一致性门禁继续未通过。

新的 scheduler-enabled 正式观察 `outputs/continuous-stability-20260916-formal-prd148-r3/status.json` 已失败：309.67 秒、1700 请求、4 个 Gateway `read_timeout`，要求 259200 秒；可用性失败不计入任何长稳 credit，需修复 Gateway 稳定性后重新启动。
## 2026-09-16 后续多角色走查（当前）

- Codex In-app Browser 的 browser-desktop 运行时已可用并完成本地标签复核；底层 `cua.getState()` 在当前 API-key 会话仍返回 `unsupported Codex auth method: apikey`，因此该工具链限制单独记录，不与产品页面证据混淆。
- 当前隔离浏览器角色审计已通过 admin、developer、viewer 三身份，覆盖权限导航、API 文档、Provider、模型、路由、员工 KEY、用量、订阅、私有 Gateway、策略和支持页面；公开入口四尺寸审计通过；Platform SaaS 运营浏览器旅程在 1440x900、1280x720、390x844 通过。汇总见 [UI/角色/Platform 审计](../../outputs/prd148-ui-audit-current-20260916/summary.json)。这些是本地隔离 seeded runtime 证据，不是外部身份、商业验收或 GA 会签。
- 视觉走查确认无数据时私有 Gateway 和 Workspace 订阅页显示明确空态；API 文档统一网关地址、协议路径和员工 KEY 规则可见。旧批次 API 文档 401 与当前隔离审计不一致，归入陈旧运行时证据，不作为当前源码失败结论。
- 精确的当前 Gateway 协议/默认路由/跨协议身份计费选择已终态通过 27/27（`outputs/prd148-gateway-27-now-20260916.xml`，0 failures、0 errors、0 skipped）。另一个包含大量慢速集成的宽集合运行超过 40 分钟并出现多个失败标记，未形成终态 JUnit，已中断并按“未完成”记录于 [Gateway 重跑记录](../../outputs/prd148-gateway-protocol-rerun-20260916/summary.json)；不能用 27/27 精确选择覆盖宽集合或宣称全量通过。
- Platform 性能单项当前重跑 1/1 通过，未放宽原有 P95 `<50ms` 门槛；GA Launch 当前重跑 14/14 通过。五域矩阵仍为 Platform 38/38、Workspace 44/44、Partner/Support 16 passed + 1 retired skipped、Device 14/14。
- 路线图校验在恢复 PRD-148 的正确“未启动”状态后返回 `status=passed`（251 checks，`next_prd=PRD-148`），见 [当前路线图校验](../../outputs/prd148-roadmap-current-20260916/validation.json)。正式稳定性观察仍只有 309.67 秒且 4 次 Gateway `read_timeout`，72 小时计入 0 秒；PRD-148 继续未完成。

## 2026-09-16 本地运行与浏览器连接复核

- 当前源码快照仍为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，源码摘要
  `6348c7766544b91625140779cc147a37cc0ca9b340b3b7e1d42fa667d37a71be`，文件数 2677。
- 用户打开的 `http://127.0.0.1:5173/identity.html` 曾显示“暂时无法连接账号服务”。根因是本地服务默认只允许
  `http://localhost:5173`，浏览器请求来自 `127.0.0.1:5173`，导致浏览器将正常未登录 401 视为跨域网络失败。仅重启本地开发服务并显式允许两个 loopback 来源后，`OPTIONS /identity/me` 返回 200（含允许来源和 credentials），未登录 `/identity/me` 返回 401 `identity.session_required`，实际浏览器脚本显示登录表单且无页面错误。该修复不改变生产注册/人机校验策略。
- 使用独立本地数据库、当前源码和 Chromium 重新执行角色审计：admin、developer、viewer 各 12 个目标页面均通过；0 控制台错误、0 请求失败、0 响应错误、0 横向溢出和交互重叠。证据为
  `outputs/prd148-role-audit-current-live-20260916/prd148-role-audit-chromium-scale-1.json`，范围仍是 seeded 本地浏览器回归，不是外部身份、商业验收或 GA 会签。
- Platform P95 单项以仓库 `.venv` 和独立 basetemp 重跑为 1/1 通过，原 `<50ms` 门槛未放宽；系统 Python 的 `tzdata` 缺失和临时目录权限错误仅归类为测试夹具环境问题。证据为 `outputs/prd148-platform-p95-single-current-20260916.xml`。
- 当前证据索引为 `outputs/prd148-current-evidence-index-followup3-20260916.json`。Gateway 宽集合仍在无 JUnit 终态前中断，付费/权益/结算账本、企业治理/私有 Gateway、六方会签、发布后复核/回滚、财务隐私安全迁移对账和 scheduler-enabled 259200 秒稳定性仍未证明；PRD-148 保持未完成。

## 2026-09-20 page-view 争用与导航偏好复核

- 五域终态文件 `outputs/prd148-negative-current-final-20260916/status.json` 已结束：Platform 38/38、Workspace 44/44、Partner/Support 17（含 1 skipped）、Device 14/14；但起止源码摘要从 `604cab2a...` 漂移到 `82168788...`，不能绑定当前源码或 GA。
- 隔离真实管理员旅程的三次运行记录了 `POST /workspace/navigation/page-views` 返回 503 `workspace_session.unavailable`，发生在 `/projects` 与 `/environments` 并行活动时，延迟约 2.1–2.2 秒；另一次隔离运行通过。该间歇性争用按真实运行失败保留，不归类为夹具问题。证据目录见 `.tmp/product-qa-browser/20260919-185522-2a7b14d1`、`.tmp/product-qa-browser/20260919-190002-8b6d55c5`、`.tmp/product-qa-browser/20260919-190504-bc837e76` 和 `.tmp/product-qa-browser/20260919-190940-10a4280a`。
- 同一旅程观察到导航偏好 PATCH 对 `gateway`、`finance` 和侧栏折叠重复携带 `If-Match: "0"`；该 UI 请求行为与前述 page-view 争用仍未闭合，不能以一次通过重跑消除。真实 S1 仍在撤权后 Gateway 401 之后因租户截图/延迟 page-view 尾部失败。
- 默认 pytest 临时根目录在 Windows 返回访问拒绝，已用工作区独立 basetemp `.tmp/pytest-prd148-nav-current-20260920` 重跑；该项单独归类为测试夹具/环境问题，不作为产品通过或失败。权威索引已追加上述分类，PRD-148 继续保持未完成。
- Gateway 宽集合拆分重跑 `test_openai_compatible_api.py` 与 `test_model_alias_routing.py` 均在长时间运行中出现失败标记，随后为回收资源安全中断，未生成 JUnit 终态；该结果作为真实失败/证据不足记录，不能覆盖 27/27 精确回归，也不能作为通过。
- 通过 Codex In-app Browser 的 browser-desktop 运行时直接刷新用户标签并读取 DOM，刷新后登录表单可见且账号服务告警消失；底层 `cua.getState()` 仍因 Codex API-key 会话返回 `unsupported Codex auth method: apikey`，该工具链限制与产品页面问题分开记录。

## 2026-09-16 Webhook 业务域批次复核

先完成 59 个桌面页面和 10 个手机页面的集中审计，再处理 Webhook 设置与死信管理。
本轮使用 Playwright 自动化，原生内部浏览器控制再次返回 `unsupported Codex auth method: apikey`；
computer-use 技能可以列出桌面应用，但不能据此声称已完成内部浏览器操作。

- 已通过：受影响前端回归 158/158、lint 834 files、生产构建 293 assets；本地已注册 Workspace owner
  的创建/搜索/格式选择/轮换/隐藏/刷新/返回操作 22/22；1440、1280、390 三尺寸布局检查通过。
  生产资源摘要同步为 `0186f4d13822ed961b3ed0107927dfcbcaf7b5eebe984bad613a87453076681a`。
- 真实失败与修复：修复 Workspace Webhook 认证与页面所有权；修复配置文件已提供密钥、代码却只读进程
  环境变量造成的创建 503；修复桌面表单超出内容区、Endpoint 操作裁切、手机事件选择区高达 3247px。
  67 项事件提供中文名称与搜索，复选框恢复 18px，列表限制为 280px。
- 真实失败仍存：角色权限、成本异常、策略发布在桌面与手机均持续加载。原六页复核为 6/12 通过，
  其余六项失败。反馈汇总本次 2/2 可用，但此前 503 未证明根治，继续列为间歇可用性问题。
- 夹具问题：密钥隐藏断言未等待异步刷新，以及 CEF 标签定位错误均已修正。旧几何检查漏检父容器裁切，
  不能作为 UI/UCD 完整通过证据；已补充本域检查并人工查看完整截图。
- 迁移基线：本批未发现新的基线漂移，既有 0155 修正保留。
- 证据不足：后端配置回归 3/3 是最终前端布局修改之前的受影响测试，不能冒充最终全量源码 QA；
  Webhook 事务内撤权、完整死信与多角色操作、Endpoint 启停/编辑流程仍需继续验收。
  测试只创建停用 Endpoint，没有对外发送 Webhook。真实商业流程、六方会签、发布回滚、对账、全部 QA、
  连续 72 小时及 HEAD/origin 一致性仍未满足，PRD-148 保持未完成。

本批索引：[Webhook 批次记录](../../outputs/prd148-webhook-final-20260916/summary.md)。
运行日志与封存证据分开保存。HEAD 仍领先本地缓存的 origin/main 135 个提交，未提交或推送。

## 2026-09-16 内置浏览器控制恢复复核

- 已通过：直接调用 `cua.createBrowserTab("iab", ...)` 成功连接 Codex 内置浏览器，
  实际点击“创建账号”进入注册页，再点击“返回登录”回到登录页；没有提交注册或登录。
- 工具失败：重置后的 `cua.getState()` 仍报 `unsupported Codex auth method: apikey`。
  失败范围是浏览器枚举，不能再据此声称内置浏览器整体不可用。直接选择的标签可继续复用。
- 夹具问题与迁移漂移：本次工具复核不涉及；没有修改应用安装、认证方式或安全设置。
  工作区清理记录没有删除浏览器工具、Codex 认证文件或插件，未发现清理导致此错误的证据。
- 证据不足：本次仅验证工具连接和页面切换，不证明真实注册/登录、完整业务流程或 GA。
  PRD-148 保持未完成。详细过程见
  [浏览器控制诊断](../../outputs/prd148-browser-control-diagnostic-20260916.md)。

## 2026-09-17 内置浏览器治理批次

使用已恢复的 Codex 内置浏览器，通过真实登录表单进入现有本地企业组织，集中走查角色权限、
成本异常和策略发布后统一修改展示文案。角色/权限分组已中文化，移除正常界面中的页面编号和
完整矩阵 hash，发布状态改用业务语言，同时保留权限门禁、版本约束和网关同步边界。

- 已通过：三个页面桌面/手机共六次加载和页面横向溢出检查；角色切换、财务分组筛选、
  权限草稿与放弃、发布原因门禁。受影响前端 395/395；lint 835 files；build 293 assets；
  Workspace 治理集成 1/1，模板回滚撤权/审计原子性单项通过。均为限定范围，不是全 QA。
- 真实失败：开发者中心偏好被后端主导航分组校验拒绝，导致展开后回退；刷新覆盖手动展开、
  手机选页后导航仍展开尚待导航批次处理。直接 URL 导航两次回到登录页，产品会话与工具行为
  归因未完成，不能宣称登录持久化已通过。
- 夹具问题：财务管理员并非默认只读；撤权会使旧 Workspace 会话失效；回滚故障注入须指向
  新事务 helper；畸形策略正文应断言领域合同 422。以上已修正并保留撤权测试。
- 工具问题：内置浏览器 `fill("")` 没有清空已有文本；读取值确认后使用 Backspace 完成清空。
- 迁移基线：本批未发现新漂移，既有修正保留。
- 证据不足：真实异常处置、独立 SaaS 运营/员工身份、完整业务流程与全部 GA 门槛仍未完成。

生产资源摘要为 `58e7c7605163a39d8ed5bf63f0a425de9bfe02fa6d3d29dc6bd3ddb2568cd816`。
详见 [治理批次范围和证据](../../outputs/prd148-native-governance-20260917/summary.md)。
README 仅记录此批次，Excel 状态不升级；PRD-148 继续未完成，未提交或推送本批。

## 2026-09-17 导航与会话批次

先整理导航问题，再统一修复后端开发者分组偏好、前端异步刷新覆盖与手机选页收起行为。
当前原生浏览器身份仍是本机真实注册的组织 owner，不把角色编辑标签冒充其他身份登录。

- 已通过：开发者中心展开保存、筛选刷新保留手动分组、390x844 手机选页收起并加载内容、
  账号设置与空间链接往返。受影响前端 181/181、导航集成 7/7、Ruff、lint 836 files、
  build 293 assets。独立 Chromium 两个 loopback 域名的登录/刷新/跳转通过，单独归档。
- 真实失败：内置浏览器 `reload()` 后缺少会话 Cookie，尚未归因；开发者 API 文档使用
  `default-chat` 而接入页为 `default`，地址展示不一致；迁移页可信地址不可用、复制禁用；
  页面访问事件持续 422。以上继续按开发者域合并处理，scheduler 争用也仍未解决。
- 夹具问题：前端隔离 VM 未声明新增侧栏状态，修正后全项通过；会话对照首次用错账号页
  “退出登录”按钮定位，修正并保留首轮失败结果。原生 Control+R 没有产生新请求，不计刷新通过。
- 迁移基线：本批未发现新漂移，完整迁移链校验与既有修正保留。
- 证据不足：本批不是完整多身份操作、全功能/全 QA、真实商业/私有网关交付、六方会签、
  发布回滚或 72 小时稳定性。Excel 状态不升级，HEAD 比本地缓存 origin/main 领先 135，
  未 fetch/commit/push，PRD-148 继续未完成。

资源摘要：`5e675e5cede7b77fe8de5252d46adbca475a1badff584d3473f47092ef6568ce`。
证据与后续问题：[导航批次](../../outputs/prd148-native-navigation-20260917/summary.md)、
[开发者域问题集](../../outputs/prd148-native-navigation-20260917/developer-domain-backlog.md)。

## 2026-09-17 平台可用性与凭据边界批次

集中修复 Platform 读取在 SQLite 调度器写入竞争下把会话 `last_seen_at` 更新失败映射为
503 的问题。读取先在快照中鉴权；需要更新时间或发现权限变化时回滚快照、取得写事务并重新鉴权，
因此撤权不会继续使用旧授权结果。状态投影直接使用写事务以容纳只读审计。

- 已通过：真实并发提交回归 9/9（认证、Session、Workspace 目录/详情/健康、驻留证据、GA 门禁、
  状态和撤权竞争）；Ruff；scheduler-enabled 重启后的状态、驻留、GA 门禁、Workspace 目录/筛选/详情。
- 真实失败：客户成功仍返回 `platform.credential_conflict`，隐私队列仍返回
  `privacy.credential_conflict`；这是 PRD-140 要求的混合凭据拒绝，当前 Cookie 生命周期尚未完成。
  scheduler 仍报告 `platform_metering.reconciliation_required`、任务租约丢失和数据库慢操作，不能宣称稳定性修复。
- 测试夹具问题：系统 pytest 临时目录权限导致一次重跑 9/9 setup errors；工作区隔离目录的首次完整回归
  已得到 9/9 通过，失败记录保留，不混计。
- 迁移基线：本批未发现新漂移；已有 0155 完整链保持不变。
- 证据不足：五域完整负向矩阵、全部 QA、真实多身份商业链、六方会签、发布/回滚/对账和 72 小时
  scheduler-enabled 观察仍未完成，PRD-148 继续未完成。

证据：[平台可用性批次](../../outputs/prd148-platform-availability-diagnostic-20260917/findings.md)。

## 2026-09-17 平台全导航、角色回归与会话边界批次

本批先集中走查平台 39 个导航入口，再用隔离的 Chromium 真实浏览器旅程覆盖 admin、
developer、viewer 三种企业网关角色。平台入口在 428px 采样宽度下无横向溢出；角色回归覆盖
36 个目标页，0 个 API 失败、0 个请求失败、0 个控制台错误、0 个裁切或交互重叠；隔离数据库
迁移到 0155，完整性和外键检查通过，registration human verification 在该夹具中关闭。

本批修复了平台会话建立后仍保留父 GlobalSession Cookie 导致客户成功和平台隐私队列被误判为
混合凭据的问题：现在只允许与 PlatformSession 建立时完全匹配的父会话，其余不匹配、重复或
畸形凭据继续 fail closed。私有 Gateway 表格也不再把离线或待接管 Deployment 展示为“正常接管”，
常见平台状态和基础设施类型改用业务中文标签。

- 已通过：`prd148-role-audit` admin/developer/viewer 三角色浏览器回归；PRD-095 平台 P95 单项；
  PRD-134 隐私专项；PRD-140 身份查询边界；前端私有 Gateway、平台访问单元测试；Ruff、lint 838 files、
  production build 294 assets；PRD-147 平台浏览器旅程。
- 真实失败：PRD-140 `test_due_event_and_outbox_takeover` 重跑仍在 outbox 接管断言失败，待独立修复，
  与本批会话边界改动无直接关系；scheduler-enabled 计量对账告警和 72 小时观察仍未闭合。
- 测试夹具问题：系统 pytest 默认临时目录被 Windows 权限拒绝；改用仓库内隔离 `--basetemp` 后继续执行，
  相关 setup error 保留，不计为产品通过。
- 迁移基线：本批使用完整迁移链 0155，未发现新的基线漂移；生产资源摘要因新增展示文案已同步为
  `45212ba70208e23a8835a28871107750d62f7ab7697e76b1d2665905f9f851a2`。
- 证据不足：真实注册到付费、六方会签、发布回滚、财务/隐私/安全/迁移对账，以及正式 scheduler
  配置连续 72 小时 0 P0/P1 观察仍没有当前可追溯证据；PRD-148 保持未完成。

证据：[平台 Gateway 导航走查](../../outputs/prd148-native-platform-gateway-20260917/)、
[三角色浏览器回归](../../outputs/prd148-role-audit-live-20260917/)、
[平台旅程回归](../../outputs/prd148-platform-journey-after-sessionfix-20260917/)。

## 2026-09-17 多角色集中走查与平台文案批次（当前源码）

本批在现有工作树上先完成问题合并，再集中修改平台运营界面：供应商目录、通知模板、组织详情的用户可见标签统一为业务中文；保留自动化契约所需的不可见状态/兼容标识；通知状态按实际状态投影显示。生产资源摘要更新为 `45212ba70208e23a8835a28871107750d62f7ab7697e76b1d2665905f9f851a2`。

- 已通过：三角色隔离 Chromium 走查（admin/developer/viewer，36 个目标页）`outputs/prd148-role-audit-postcopy-20260917/prd148-role-audit-chromium-scale-1.json`；公开入口走查 `outputs/prd148-public-audit-postcopy-20260917/prd148-public-audit-chromium-scale-1.json`；Platform 运营旅程三视口 `outputs/prd148-platform-journey-postcopy-20260917/prd147-platform-chromium-scale-1.json`；Workspace 人类路径三角色 `outputs/prd148-workspace-journey-postcopy-20260917/browser-real-chromium-scale-1.json`；相关目录/供应商单测 11/11；lint 838 files；build 294 assets。
- 真实失败：本批未新增产品 HTTP 失败。Workspace 人类路径报告中 admin/viewer 的 `logout_during_hydration_passed=false` 被脚本整体判定为通过，需后续单独解释该可选场景，不能扩大为所有真实用户流程通过。
- 测试夹具问题：浏览器 QA 使用隔离数据库并关闭人机挑战、关闭后台 scheduler；投影由 worker 预构建。这些是夹具事实，不构成真实商业或 GA 证据。
- 迁移基线：隔离库使用完整迁移链 0155，完整性正常、外键违规 0；本批未改迁移。
- 证据不足：真实注册到付费、六方会签、发布回滚、财务/隐私/安全/迁移对账、scheduler-enabled 连续 72 小时 0 P0/P1 和 HEAD/origin 一致仍未闭合；PRD-148 保持未完成。

原生桌面 CUA 仍因当前 Codex 自定义 API-key Provider 拒绝 `apikey` 而不可用；内置浏览器控制和 Playwright 可用，本批证据明确标注为隔离 Chromium/内置浏览器，不冒充原生桌面 CUA。

## 2026-09-18 当前请求解释回归

在当前源码快照（2688 文件，SHA256 `0143b226e96aac094667d32d03e2fe3aef590369c1cdb99aafc262f1e65d9384`）上重跑请求解释页面状态、渲染、本地化、身份绑定和旧响应丢弃回归，17/17 通过。证据：`outputs/prd148-current-request-explanation-regression-20260918.json`。该单元回归不关闭五域矩阵、真实商业链、发布控制或正式 72 小时门禁。

同一源码快照上的导航、Console 入口、账号/会话生命周期、迁移指南和开发者快速开始回归 118/118 通过。证据：`outputs/prd148-current-navigation-regression-20260918.json`。该单元回归不关闭五域矩阵、真实商业链、发布控制或正式 72 小时门禁。

同一源码快照上的当前 Chromium 角色审计已完成：admin、developer、viewer 各 60 个注册 Workspace 页面，共 180 页通过；页面授权回退、API/请求/控制台错误、横向溢出、控件裁切和交互重叠均为 0。证据：`.artifacts/prd148-role-audit-current-20260918-r4/evidence-manifest.json`。这是隔离浏览器页面证据，不是原生 computer-use、真实商业验收或 GA 证据。

同一源码快照上的域级页面审计覆盖 59 个桌面页和 10 个移动页，共 69/69 通过；页面请求、错误、路由回退、加载占位、溢出、裁切和交互重叠均无异常。证据：`outputs/prd148-domain-ui-audit-current-20260918-r5/evidence-manifest.json`。这是只读页面/布局审计，不等同于完整操作链或 GA。

当前源码 PRD-148 GA Launch 生命周期集成回归 14/14 通过，Gateway 协议/流式/默认模型/跨协议员工身份计量回归 38/38 通过。证据分别为 `outputs/prd148-current-ga-launch-20260918.xml` 和 `outputs/prd148-current-gateway-protocol-20260918.xml`。两套均为隔离库与合成上游测试，不证明真实发布、商业订阅、六方会签或正式稳定性。

当前源码三角色 Chromium 业务旅程（FLOW-01 admin、FLOW-02 developer、FLOW-03 viewer）均通过，包含 Cookie 登录、角色操作、导航、表单校验、服务恢复和登出；请求失败、控制台错误、溢出、裁切和交互重叠均为 0。证据：`.artifacts/prd148-human-journey-current-20260918-r2/evidence-manifest.json`。现有旅程合同只在 developer 分支执行 hydration 登出场景，不能扩展为三角色均覆盖，也不等同于真实商业或 GA。
## 2026-09-18 反馈归因与计量修复复验

本批先复现真实链路再集中修复：真实本机 KIMI 请求 `req_4fcd96b151034725a01f478f6437bc18` 返回 200，38 tokens/费用写入 usage ledger，唯一 routing attempt 为 `succeeded` 且 `usage_status=recorded`；Owner 提交反馈返回 201，汇总为 1 条、5/5、100%，停用临时 KEY 后调用返回 401。此前请求 `req_d34ea7667fff44ecb264260f2b4b0d22` 暴露真实失败：上游用量已归一化但连接池包装对象未通过质量统计的原生连接类型校验，attempt 停留 `started`，账本进入 spool；该记录保留，未补造或篡改历史数据。

- 已通过：连接所有权与回滚单测 4/4；路由尝试与反馈集成回归 14/14；Ruff；真实 KIMI 路由、计量、反馈、撤权链路；桌面/手机反馈布局无溢出、裁切或交互重叠。
- 真实失败：旧五域矩阵虽然各集合测试通过，但 source_start/source_finish 不一致，不能认证当前源码；Chromium 登出仍未在 15000ms 内回到首页；历史反馈 503/调度争用和旧悬挂账本仍需正式对账。
- 测试夹具问题：先前系统 pytest 临时目录权限错误和 schema trace 过滤问题均保留原始产物；本批新增连接池 facade 回归覆盖真实运行时边界。
- 迁移基线：继续使用完整迁移链 0155，未修改迁移基线。
- 证据不足：PRD-090..147 全量当前源码映射、全部十一类 QA、真实付费/企业治理/私有 Gateway、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 scheduler-enabled 259200 秒和 HEAD/origin 一致仍未闭合。PRD-148 保持未完成，正式稳定性计数仍为 0。

证据：[修复批次封存目录](../../outputs/prd148-feedback-attribution-fix-final-20260917/)、[真实调用与计量审计](../../outputs/prd148-feedback-fresh-call-after-20260917/attribution.json)。当前源码 SHA256 `9849bd36f8d10ebc59278b1a12da888f60512355190c223d2c99ba7d8dcd93eb`，HEAD `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf` 与实时 `origin/main` 仍不一致。

PRD-140 客户成功 outbox 接管已在同一当前源码和完整迁移链 0155 上重新验证为 1/1 通过，封存 `outputs/prd148-prd140-outbox-current-20260918/evidence-manifest.json`；旧失败产物仍保留为历史记录，不再作为当前失败。其他五域源码漂移、Chromium 登出超时、商业/会签/回滚对账、正式 72 小时和 HEAD/origin 仍未闭合。

## 2026-09-18 当前证据索引与权威工作簿核对

当前源码快照为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`（2688 files）。顺序五域矩阵 `outputs/prd148-negative-current-20260918-r2` 绑定 SHA256 `872f1f625358542e5d967810f5b993756596c3fc30398f62c34b9094aa632235`，Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个退役入口 skipped、Device 14/14；因源码漂移，这批不能作为当前完整源码证据。角色走查本轮未生成终态报告，分类为 `incomplete_no_report`，不计为通过；Chromium 1x 登出 15000ms 未回首页的真实失败继续保留。权威 Excel 已同步当前记录，`01_PRD总表!B60=未启动`，工作簿 SHA256 `515488a7c5764a2116e1dd3beb57285fa5f8dee34ef899f54a13f6d013b27bd6`。真实商业链、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致仍未闭合，PRD-148 保持未完成。证据索引已更新为 `outputs/prd148-current-evidence-index-final-20260916.json`。

## 2026-09-18 当前源码 r5/r4 复验

当前源码快照为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `b567e0133e5516f3c886384c50dd249b7a9aa22cf040347554e0b7cebc4e12f4`（2688 files）。在同一未漂移快照上，五域负向矩阵 r5 通过 Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个明确退役入口 skipped、Device 14/14；证据为 `outputs/prd148-negative-current-20260918-r5/status.json` 和 `evidence-manifest.json`。初次共享 Windows pytest 临时目录权限错误保留为夹具问题，独立 basetemp 复跑未出现产品断言失败。

同一快照上的 PRD-148 GA Launch 生命周期集成回归 r4 为 14/14，Gateway 协议、流式、默认模型和跨协议员工身份/计量回归 r4 为 27/27；证据分别为 `outputs/prd148-ga-launch-current-20260918-r4/evidence-manifest.json` 和 `outputs/prd148-gateway-current-20260918-r4/evidence-manifest.json`。两者仍是隔离数据库与合成上游测试，不构成真实商业、六方会签、发布回滚或正式稳定性证明。

Chromium 1x 登出 15000ms 未回首页仍按真实失败保留；本机 KIMI 试用链仅证明注册、组织、Provider/模型/路由、员工 KEY、调用、计量和撤权的本地试用，不含付费套餐、结算权益、生产部署。六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒观察和 HEAD/origin 一致仍未闭合，PRD-148 保持未完成。

## 2026-09-19 页面访问并发复核

当前源码快照（2688 files，SHA256 `b567e0133e5516f3c886384c50dd249b7a9aa22cf040347554e0b7cebc4e12f4`）上的
PRD-147 Workspace page-visit 集成回归为 7/7，通过且无失败或错误，证据为
`outputs/prd148-page-visits-current-20260919.xml`。该回归使用隔离迁移夹具，不能覆盖真实服务调度争用。

真实 Chromium 组织/邀请走查仍保留一个产品失败：邀请账户退出后有两个
`/workspace/navigation/page-views` 请求分别等待约 40,023ms 和 38,458ms，完整旅程超时；证据为
`.artifacts/s1-browser-h5ma4pu8/acceptance.json`、`browser.json` 和保留的压缩数据库。当前分类为运行时并发写锁或迟到访问事件，未放宽门槛、未归类为夹具。

财务走查的另一失败是验收脚本契约漂移：旧脚本硬编码已退役的
`workspace.settings.profile` 任务入口，而当前任务首页只提供 Provider、模型、员工 KEY、首次调用四项；证据为
`.artifacts/s1-browser-wk4cg2of/acceptance.json`。这项归类为测试/验收契约问题，不能作为产品通过。

本批不改变五域矩阵、商业付费链、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式
259200 秒稳定性观察或 HEAD/origin 门禁；PRD-148 继续未完成。汇总证据为
`outputs/prd148-human-full-20260919/page-visit-regression.json`。

## 2026-09-19 当前源码五域矩阵复跑

在当前源码 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256
`bdb3204a376fc7f6859f8f163c8b8bbe68787371abac2ccf667558f2954bb838`（2689 files）上，五域负向矩阵
`outputs/prd148-negative-current-20260919-r1` 起止快照一致：Platform 38/38、Workspace 45/45、
Partner/Support 16 passed 加 1 个明确退役入口 skipped、Device 14/14。每个域使用独立
`--basetemp`；此前共享 Windows pytest 临时目录权限问题作为夹具问题保留，当前批次无产品断言失败。
该证据只证明当前源码负向矩阵，不关闭真实付费链、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、
scheduler-enabled 259200 秒观察或 HEAD/origin 门禁，PRD-148 继续未完成。证据索引已更新为
`outputs/prd148-current-evidence-index-final-20260916.json`。

同一当前源码上再次启动 Gateway 协议、默认模型和跨协议计量 27 项批次；运行超过既有可控时限仍未生成终态
JUnit，已停止已知 pytest 工作进程并记录为 `outputs/prd148-gateway-current-20260919-r1` 的
`interrupted_no_terminal_junit` 测试运行器/环境失败。没有把历史 27/27 升级为当前源码通过证据。

## 2026-09-19 当前源码真实本地试用链路与 P95 复核

在当前源码快照 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256
`7537aee46d2e90b358ebd7db349cb66f9d0baf1b394d8495156596021690f136` 上，使用本机已授权试验账号完成登录、组织空间切换、Provider/模型/默认路由读取、员工消费者创建、个人 Virtual Key 创建、真实 Gateway 调用、usage summary/records 与预算状态读取、Virtual Key 撤权，以及撤权后 Gateway 401。明文 KEY、密码、Cookie、请求正文和响应正文均未写入证据；脱敏记录见 `outputs/prd148-real-current-flow-20260919.json`。

`GET /quota-buckets` 使用工作区 Cookie 返回 `401 auth.token_missing`；代码契约要求该接口使用 Bearer 会话，且本地组织没有付费套餐或权益快照。因此这项被记录为商业/额度证据不足，不被误报为产品通过，也不构成付费、结算或 GA 证据。

## 2026-09-20 资源摘要与 S1 真实重放

本轮重新计算当前源码快照为 2691 个受测文件，SHA256 `4038c1faeea4ac1ae09c2eb55124ff1a2899302f6e9f541d33bf75f5ee59839a`。前端 production surface 校验、`npm run build`（294 assets）和 `npm run lint`（838 files）均通过；两个生产 manifest 已同步到资源摘要 `f6fee31bb171fee0e319fc134689bd5bd2ac75f80bfd36ebb754a6812b62230c`。证据为 `outputs/prd148-source-current-20260920.json` 和 `outputs/prd148-production-build-current-20260920.json`。

同一快照上的真实 S1 浏览器流程 `.artifacts/s1-browser-z1pzaatf` 已越过注册、组织、Provider/订阅、邀请、两名员工、两把个人 KEY、Gateway 调用、KEY 轮换、撤权和撤权后 Gateway 401。流程在 `1280.onboarding-navigation.tenant` 的截图阶段失败：`page.screenshot` 等待字体 12 秒超时，仍有 6 个 `/workspace/navigation/page-views` 请求 pending，最长约 110 秒。该结果保留为真实部分失败，不归类为夹具通过，也不宣称完整注册到撤权链路或 GA；证据为 `outputs/prd148-real-s1-rerun-20260920.json`。付费权益、企业治理/私有 Gateway、六方会签、发布回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致仍未闭合，PRD-148 保持未完成。

同一源码快照上，`test_platform_concurrency_cursor_scope_and_warm_performance` 重跑为 JUnit 1/1、0 failures、0 errors，原有 `<50ms` 门槛未修改。此前 P95 超时仍保留为历史真实运行波动，当前单项证据见 `outputs/prd148-platform-p95-current-20260919.xml` 和 `outputs/prd148-platform-p95-current-20260919.json`。

当前源码 Gateway 27 项批次仍没有终态 JUnit；Chromium 1x 登出 15 秒失败、隔离注册静态服务失败、真实邀请页访问事件并发失败、付费套餐/权益、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒观察以及 HEAD/origin 一致性仍未闭合。PRD-148 保持未完成。

## 2026-09-19 当前源码五域负向矩阵 r2

导航 Cookie 修复后的当前源码快照 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256
`7537aee46d2e90b358ebd7db349cb66f9d0baf1b394d8495156596021690f136`（2689 files）上，五域负向矩阵起止快照一致：Platform 38/38、Workspace 45/45、Partner/Support 16 passed 加 1 个明确退役入口 skipped、Device 14/14。权威状态与逐文件 SHA-256 见 `outputs/prd148-negative-current-20260919-r2/status.json` 和 `evidence-manifest.json`。

首轮并行启动时，Windows `Start-Process` 将一个包含空格的 Partner 参数化测试名拆成了错误文件名；该错误保留在 `partner_support.err`，随后按测试文件在隔离 basetemp 中重跑并得到上述终态。Workspace 首轮进程延迟写出 JUnit，重复补跑在原 JUnit 出现后停止；这两项均为运行器事实，不计作产品失败。

矩阵只覆盖当前源码负向授权与隔离迁移夹具，不关闭真实注册到付费/额度、六方会签、发布后复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒 0 P0/P1 观察或 HEAD/origin 一致性；PRD-148 仍未完成。

同一源码快照上的 `npm run lint` 通过 838 files，`npm run build` 通过 294 assets（production surface 276 modules / 294 shipped sources）。构建没有改变 QA source snapshot；静态前端结果不等同于真实浏览器、商业或 GA 通过。

## 2026-09-19 三角色人类式全路由交互走查

在当前 QA 源码快照（revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `15f1147f7e9135d7a1e2da4fafdf534ebbdef42c74eb07ed075cb55d01ce30c6`、2690 文件，包含本轮审计器）上，隔离 Chromium 以管理员、开发者、查看者三种身份遍历全部 60 个 Workspace 源路由，共 180 页。每页执行登录后导航、页面加载、布局测量、敏感文案扫描，并点击侧栏分组、筛选/标签、详情/关闭、刷新等可逆控件；不执行创建、删除、审批、发布、撤权或发送等破坏性动作。

- 已通过：三个角色各 60/60 页；0 页面失败、0 HTTP 5xx、0 请求失败、0 控制台错误、0 横向溢出、0 控件裁切、0 交互重叠、0 真正的无标签表单字段；共记录 421 次可逆交互动作。汇总证据为 `outputs/prd148-human-interaction-current-20260919/evidence-manifest.json`，角色原始报告位于该目录列出的三个 artifact。
- 测试夹具问题：首版审计器把被 `<label>` 包裹的表单控件误计为无标签，`inputsWithoutLabel` 实际为 0；修正判定为只统计无可访问名称的按钮/链接后，三角色终态均通过。首次静态端口导航读取导致错误响应形状、首轮 900 秒运行器超时均保留在历史临时目录，不计产品失败。
- 真实失败与证据不足不变：Chromium 登出 15 秒失败、邀请页访问事件并发失败、真实付费/额度权益、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性以及 HEAD/origin 一致仍未闭合；本批不是原生 computer-use、生产 GA 或商业验收证据，PRD-148 保持未完成。

## 2026-09-19 Chromium 登出重放

在当前源码快照（revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `7537aee46d2e90b358ebd7db349cb66f9d0baf1b394d8495156596021690f136`、2689 文件）上，仅重放 Chromium 1× 真实浏览器旅程。管理员、开发者、查看者三条旅程均完成，0 请求失败、0 控制台错误、0 布局问题；原始 15000ms 登出 `waitForURL` 超时本次未复现。证据为 `outputs/prd148-browser-failure-rerun-20260919/browser-real-chromium-scale-1.json`。

原始失败 `outputs/prd148-browser-failure-20260916.json` 继续保留为真实历史失败，当前重放不能把它定性为夹具问题，也不能关闭浏览器、商业或 GA 门禁。该重放使用隔离数据库与自动化 Chromium，不是原生 computer-use、生产部署、商业验收或正式稳定性证据；PRD-148 继续未完成。

## 2026-09-19 page-view 写锁门禁修复

针对真实组织/邀请旅程中两个 `/workspace/navigation/page-views` 请求在登出边界等待约 40 秒的问题，`control-plane-api/src/metahub_control/workspace_navigation/service.py` 将 page-view 遥测的 SQLite writer admission 总等待限制为 2 秒。无争用写入的鉴权、幂等、审计和响应契约不变；超时仍按遥测失败处理，不能阻塞认证边界。

- 当前源码绑定：revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，SHA256 `b120d70387ea568e4e8f88754e2edc963bc74c4292718d3878b992df0413c39d`，2690 个受测文件。
- 已通过：前端生命周期回归 2200/2200；Workspace page-visit 集成回归 7/7；Python 编译检查通过。集成回归使用独立 `.artifacts/pytest-page-visits-current-2`，避免系统临时目录权限问题。
- 真实失败保留：`.artifacts/s1-browser-h5ma4pu/acceptance.json` 中的两个延迟 page-view 请求尚未用生产构建浏览器重放验证修复，因此仍是开放的真实失败，不放宽门槛。
- 证据不足保持：付费权益、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致仍未闭合；PRD-148 未完成。

证据：`outputs/prd148-page-visit-write-deadline-20260919.json`。

生产构建重放 `.artifacts/s1-browser-pdmjjb3s` 已执行 294 资源校验，注册、组织、Provider/模型、邀请、登录和账号入口均完成；随后在 `navigation-history` 阶段命中旧脚本的退役 `workspace.settings.profile` 任务入口，归类为验收契约漂移。该次报告仍有多个 `/workspace/navigation/page-views` 请求 pending（约 17--50 秒），因此两秒 writer admission 调整尚未得到清洁浏览器重放证明，原真实运行时失败继续开放。摘要证据：`outputs/prd148-page-visit-write-deadline-browser-rerun-20260919.json`。

修正任务入口夹具后，第二次生产构建重放 `.artifacts/s1-browser-7dz4v8if` 已完成注册、组织、Provider/模型、邀请接受、邀请用户 Workspace 会话和登出回首页；`logoutHomeAndModernLogin=true`，此前邀请用户登出失败在该关键子流程中未复现。完整流程在 Owner 账号后续工作区页面等待时失败，且仍有两个 page-view 请求约 39--42 秒 pending；因此不能把该次结果写成完整业务链或 GA 通过。摘要证据：`outputs/prd148-page-visit-write-deadline-browser-rerun2-20260919.json`。

在将 page-view 的连接建立、`BEGIN IMMEDIATE`、鉴权、幂等写入、最终权限复核和提交统一放入两秒截止时间后，隔离 page-visit 集成回归仍为 7/7。随后使用验收器要求的完整基线和 294 个生产资源进行第三次重放：注册、组织、试用成员/Provider 和账号入口完成，page-view 已收到 200 且失败记录时没有 page-view 请求 pending；但隔离 Uvicorn 服务在导航历史阶段发生 Windows access violation，三个静态模块请求失败，浏览器上下文关闭。该批次是测试运行器/Windows 服务崩溃，不能升级为完整浏览器通过，也不能把原始 page-view 失败直接定性为夹具。证据为 `outputs/prd148-page-visit-write-deadline-browser-rerun3-20260919.json` 与 `.artifacts/s1-browser-m7_21hqe`；PRD-148 继续未完成。

随后把员工创建后的添加面板保持打开，修复了完整流程中第二名员工表单在刷新后被折叠、无法输入的产品状态问题。受影响前端回归 2200/2200，生产资源 294 个、资源摘要 `2b6c9819cc0890853f735930c76eb28979b42b0ff64c0d31bce86e8d7dfea35f` 已重新绑定；证据为 `outputs/prd148-production-build-current-20260919.json`。再次生产重放在注册阶段被同一隔离 Uvicorn Windows access violation 中断，未形成业务流程通过，证据为 `outputs/prd148-page-visit-write-deadline-browser-rerun4-20260919.json` 与 `.artifacts/s1-browser-_dplipcv`。

员工表单修复后的当前源码三角色交互审计已重新完成：管理员、开发者、查看者各遍历 60 条路由，共 180/180 页；0 页面失败、0 HTTP 5xx、0 请求失败、0 控制台错误、0 横向溢出、0 控件裁切、0 交互重叠、0 真正无标签控件。起止源码一致为 `bb777466416def1ca9a215c5309bb0c5ddd68fdfd3ec3c82a22cb3e548da5d58`（2690 文件），证据为 `.artifacts/prd148-human-interaction-current-20260919-r3/evidence-manifest.json`。该批仍是非破坏性隔离浏览器审计，不关闭真实商业、发布回滚、对账、正式 259200 秒稳定性或 HEAD/origin 门禁。

## 2026-09-19 员工流程后续复验

当前源码在员工页复验中修正了两个真实前端问题：员工目录渲染现在把创建后保持展开的状态传入 `<details>`，第二名员工可以继续输入；CSV 批量导入文本框明确使用 16px 字体，满足浏览器可访问性门槛。受影响的 `workspaceConsumers` 与 `humanWalkthrough` 单测共 43/43 通过，lint 838 files 通过，生产构建 294 assets 通过（资源摘要 `65ed5c9c8de0740b18823abd1b25c959c9b4ae4e63f42da519d01d5ff676914f`）；当前源快照为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `6a8396c904034006d48c792c29cbb10187f797c1a9b1d89edd4a694783199720`、2690 文件。

真实组织浏览器复验仍未形成完整通过：`.artifacts/s1-browser-69efzdeh/browser.json` 已完成两个员工 POST 并越过原来的第二员工表单失败，但在修复前记录了 CSV 文本框 13.3333px 的旧布局失败；`.artifacts/s1-browser-a58pqciu/browser.json` 在账号偏好表单出现前超时；`.artifacts/s1-browser-3oik8m1s/browser.json` 在试用成员/Provider 阶段等待 Console 时超时。相关运行均有延迟 `/workspace/navigation/page-views` 请求，仍按真实运行失败保留，不能归类为夹具或 GA 证据。下一次清洁重放尚未通过，因此 PRD-148 保持未完成。
## 2026-09-20 真实员工链路复验

本轮真实 S1 重放使用隔离控制面进程，避免 Windows Uvicorn 线程 access violation 污染浏览器证据。当前源码修复并验证了三个真实 UI 问题：员工 KEY 名称字段默认不可见；第一把 KEY 创建后发放面板关闭，第二名员工无法继续；员工档案折叠后撤权复选框不可操作。全量前端单测 2,201/2,201、lint 838 files、生产构建 294 assets 通过。

最新真实 artifact 为 `.artifacts/s1-browser-tv64otey`。它已完成注册、创建组织、Provider/订阅、邀请接受、两名员工、两把个人 KEY、真实 Gateway 调用、旧 KEY 失效、员工撤权及撤权后 Gateway 401。失败发生在后续账户引导导航的截图阶段（`page.screenshot` 12 秒超时），并保留延迟 page-view/字体请求；该结果是未完成的真实浏览器复验，不计为完整链路通过。修复前失败 `.artifacts/s1-browser-hx1h_hh3` 与 `.artifacts/s1-browser-vb5jyu54` 继续保留。

## 2026-09-20 当前源码全批次角色与 QA

当前源码三角色人类式交互审计为 180/180 页通过，平台运营走查在 1440x900、1280x720、390x844 三视口通过，均无请求失败、控制台错误、溢出或裁切。WB/API/BB/GR/SEC 后端批次 8/8、PERF 隔离批次 9/9、WEB/UI/UCD/COMP/FLOW 合同批次和 Console lint 均通过；全批次 evidence manifest 起止源码一致，绑定 SHA256 `cf68165aabf91d3dc23485588022f406af514c50886f05f945a06227aa819eac`。

该批完成的是当前源码的隔离角色、布局、契约和性能验证；仍不证明真实付费权益、外部 Provider、企业治理/私有 Gateway、六方会签、发布回滚、财务/隐私/安全/迁移对账或 scheduler-enabled 259200 秒稳定性。真实 S1 流程的截图/page-view 尾部失败继续按开放真实失败保留。

严格角色导航审计的独立报告 `.artifacts/product-qa/prd148-role-audit-chromium-scale-1.json` 绑定同一当前源码快照：管理员、开发者、查看者各覆盖 60 页，共 180 页；47 个声明导航入口均通过权限回退检查，0 页面失败、0 请求/响应错误、0 控制台错误、0 溢出、0 裁切控件、0 交互重叠。该报告只覆盖当前源码的角色导航与权限边界，不能替代破坏性业务流程、付费商业验收或 GA 证据；索引已加入 `current_20260920_strict_role_audit`，PRD-148 仍未完成。

### 2026-09-20 分批角色走查与 page-view 超时

当前源码按 admin、developer、viewer 分角色、每批 20 路由遍历全部 60 个 Workspace 路由，共 180/180 页通过；布局、可逆交互、标签、HTTP 响应和控制台检查均为零问题。三进程并行的一次 viewer 批次在 `/workspace/navigation/page-views` 写锁竞争下出现 503，单独重跑通过；该运行器事实保留在 `current_20260920_human_interaction_partitioned`，不作为当前产品全量失败或通过的替代证据。

`console-web/src/lib/workspaceNavigation.js` 现在为 page-view 遥测 fetch 增加 2500ms 客户端 AbortController 上限，并保留调用方取消语义。2,201/2,201 前端单测、838 文件 lint、294 asset production build 通过；源码快照 `outputs/prd148-source-current-20260920-r3.json`，构建证据 `outputs/prd148-production-build-current-20260920-r3.json`。真实 S1 重放 `.artifacts/s1-browser-gdpwx6px` 仍在 tenant 截图阶段失败，6 个 page-view 请求仍未收敛；完整真实链路、商业/治理、会签、回滚、对账、259200 秒稳定性和 HEAD/origin 均未闭合，PRD-148 保持未完成。

## 2026-09-20 当前源码重放后续

在 page-view 客户端 2500ms 超时修复之后，当前源码真实 S1 重放 `.artifacts/s1-browser-_dluz246` 仍在 `1280.onboarding-navigation.tenant` 截图阶段失败：字体已加载但 `page.screenshot` 12 秒超时，6 个 `/workspace/navigation/page-views` 请求仍 pending，最长约 86.8 秒。注册、创建组织、Provider/订阅、邀请员工、两名员工、个人 KEY、Gateway 调用、KEY 轮换、员工撤权及撤权后 401 已完成；这仍是部分成功的真实失败，不能宣称完整注册到撤权链路、商业 GA 或稳定性通过。

权威 Excel 已记录当前源码、180/180 分批角色走查、page-visit 7/7、S1 失败和 `PRD-148 未启动` 状态。五域矩阵 113/113 的起止源码漂移、真实付费/额度、企业治理/私有 Gateway、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒观察和 HEAD/origin 一致性仍开放。

## 2026-09-20 当前 Gateway/GA 复验

当前源码上的 GA Launch 集成回归为 14/14（`outputs/prd148-ga-launch-current-20260920.xml`，0 failures/errors/skipped）。协议门面、流式转换、默认模型单元回归为 26/26（`outputs/prd148-gateway-unit-current-20260920.xml`）。同一命令中的跨协议员工身份与计量集成用例在本机运行超过历史可控时限且未生成终态 JUnit，已停止并记录在 `outputs/prd148-gateway-integration-attempt-20260920.json`；没有把单元通过升级为跨协议集成通过，也没有把该未完成运行归类为产品通过或夹具通过。当前源码快照为 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256 `4a929e115f245d46716e932364d68522431939f93c0644916b84fcdd63706b7b`、2691 个受测文件；本轮 XML 未单独封存起止快照，证据范围按隔离测试结果记录。

因此协议/默认模型/跨协议身份计费的当前完整证据仍未闭合；真实 Provider、付费与额度、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性和 HEAD/origin 一致性继续开放，PRD-148 保持未完成。

page-view 事务边界回归随后以工作区独立 basetemp 重跑为 7/7（`outputs/prd148-page-visits-current-20260920-r4.xml`）。一次将首次 authority 校验移到 `BEGIN IMMEDIATE` 外的优化实验使 7 项全部返回 503，已回退且未保留在源码；该实验失败记录在索引的 `current_20260920_page_visit_lock_optimization_experiment`，不能把 7/7 契约通过扩大为并发浏览器通过。

## 2026-09-20 资源读取锁边界批次

重新计算当前源码快照为 `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、SHA256
`6afa45fceb7e36b27bdaa28c79894b83cb239e1b146d197bf8196b5b94ce552b`、2691 个受测文件。为降低真实浏览器中 `/projects`、`/environments` 读取与 page-view 写事务的 SQLite 争用，Workspace 认证依赖新增显式 `touch` 参数；两个只读列表路由使用 `touch=False`，不会更新 `last_seen`。项目/环境集成回归 6/6，导航与 page-visit 集成回归 14/14，均 0 failure/error，证据为 `outputs/prd148-resource-routes-current-20260920.xml` 与 `outputs/prd148-page-visits-nav-current-20260920-r5.xml`。

这只验证了资源读取锁边界和既有契约，尚未关闭真实 S1 截图/page-view 尾部失败、间歇性浏览器争用或导航偏好 422；五域矩阵的旧终态起止源码漂移，跨协议当前集成无终态 JUnit，付费权益、六方会签、发布回滚、对账、正式 259200 秒稳定性与 HEAD/origin 仍缺当前证据。权威索引为 `outputs/prd148-current-evidence-index-final-20260916.json`，PRD-148 保持未完成。

同一批次的认证 deadline 与 Workspace 资源写入回归为 11/11（`outputs/prd148-auth-resource-current-20260920.xml`），覆盖撤权、CSRF、资源写入回滚和竞争撤权。完整 S1 重放未能启动：验收脚本要求的受保护计量基线路径/摘要（`METAHUB_TEST_METER_BASELINE`，SHA256 `d1634e8b...`）当前缺失，失败发生在浏览器和服务启动前，分类为测试基线证据不足，不产生产品通过/失败结论。

2026-09-20 导航与 page-visit 集成回归在工作区独立 pytest 临时目录重跑为 14/14（`outputs/prd148-nav-page-current-20260920-r6.xml`，0 failures/errors）。首次默认 Windows pytest 临时目录拒绝访问仍按测试夹具/环境问题记录；该回归只证明隔离契约，不能关闭真实浏览器 page-view 争用、S1 截图超时或商业/发布/稳定性门禁。

2026-09-20 Partner 当前页面边界复验：隔离 Chromium 390x844 审计在无 ChannelMembership 账号下连续两次收到 `403 partner.membership_required`，Partner 页面显示访问已撤销，且无溢出、裁切、交互重叠或应用控制台错误。证据为 `.artifacts/product-qa/20260920-partner-r6/prd148-partner-audit-chromium-scale-1.json`。旧 PRD-087 浏览器验收仍受 0080→0155 迁移基线漂移阻断；该页面边界证据不代表有成员数据投影、付费结算、发布会签、回滚、对账、正式 259200 秒稳定性或 GA 通过，PRD-148 保持未完成。
## 2026-09-21 OpenRouter 对标后集中走查复验

本轮先合并 admin/developer/viewer 三角色 180 页导航与布局走查，再补做平台运营三视口、Support/Device/Partner 域和 Workspace FLOW-01/02/03。平台运营通过；Support 管理员/开发者完成创建、回复、升级、解决、评价、重开和刷新持久化，Viewer 写入 403；Device 仍为空部署态，Partner 仅完成无 ChannelMembership 的 `partner.membership_required` 403 边界，因此两者正向真实数据路径仍缺证据。

首次 Workspace FLOW-01 在并行 page-view/导航偏好写入时复现 `/workspace/getting-started` 503 `workspace_session.unavailable`，按当前源码真实并发失败处理。任务首页路由已增加仅针对该短暂可用性错误的两次短退避重试；后端 PRD-146 导航集成在仓库独立 basetemp 下 6/6 通过，Workspace FLOW-01/02/03 复验 3/3 通过。浏览器脚本原先禁止出现“Provider KEY”，与员工交付规则冲突，已修正为禁止实际密钥格式，保留说明文本。

证据：`.artifacts/product-qa/20260921-workspace-flow-r3/browser-real-chromium-scale-1.json`、`.artifacts/product-qa/20260921-populated-domains-r1/prd148-populated-domains-chromium-scale-1.json`、`.artifacts/product-qa/20260921-platform-ops-r1/prd147-platform-chromium-scale-1.json`、`outputs/prd148-openrouter-human-audit-20260921.json`。当前源码快照为 `outputs/prd148-source-current-20260921-human-audit-final2.json`，SHA256 `6ef4d4e845c33970938171047f03bb2148282fda59e4329545f4dd53ff25e9ba`。本轮仍是隔离 seeded QA，不关闭真实付费、企业治理/私有 Gateway、Partner/Device 正向数据、六方会签、发布回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性或 HEAD/origin 门禁。

随后在同一源码上复验全路由角色审计：admin、developer、viewer 覆盖全部授权 Workspace 路由，0 页面失败、0 请求/响应错误、0 控制台错误、0 溢出、0 裁切和 0 交互重叠；证据为 `.artifacts/product-qa/20260921-role-audit-r2/prd148-role-audit-chromium-scale-1.json`。该批仍是隔离 seeded QA，不能替代真实商业和 GA 证据。

2026-09-21 正向域复验随后完成：`.artifacts/product-qa/20260921-positive-domains-r3/prd148-positive-domains-chromium-scale-1.json` 记录 Device `POST /edge/registrations` 201、一次性材料与刷新后部署可见，以及 Partner `/partner/workspaces` 200 attribution 投影；两页布局检查通过。前两次严格选择器失败已保留为测试夹具问题，修复后的当前源码快照为 `outputs/prd148-source-current-20260921-openrouter-final.json`，SHA256 `2344f9f626d82cf4ffed1210ed7c431389fb39a4e09c241637b523557f97c5dd`。该证据仍不替代真实商业数据或 PRD-148 GA，状态保持未完成。

## 2026-09-21 OpenRouter 对标 page-view 争用修复

走查中真实复现的 `/workspace/navigation/page-views` 503 已在当前源码修复：page-view 先在 deferred read 事务中完成身份、权限、路由和幂等预检，新增事件才进入短 `BEGIN IMMEDIATE` 事务并再次复核最终权限；重复请求不申请写锁，撤权和冲突仍保持原错误语义。`test_prd147_workspace_page_visits.py` 在仓库本地 basetemp 下 7/7 通过，隔离 Chromium Workspace FLOW 3/3 通过，page-view 请求无 503、请求失败或控制台错误。证据分别为 `outputs/prd148-source-current-20260921-openrouter-pageview-fix.json` 和 `.artifacts/product-qa/20260921-openrouter-pageview-rerun/browser-real-chromium-scale-1.json`。默认 Windows pytest 临时目录权限错误继续按测试夹具问题记录；真实商业/GA 门禁仍未完成。
