# PRD-148 多角色人类式走查问题矩阵（2026-09-21）

本矩阵在本批修改前冻结。范围覆盖 SaaS 平台运营、Workspace owner/admin、developer、viewer/employee，以及 Partner、Support、Device、Finance、Governance 和高级工具入口。走查使用当前源码的隔离 Chromium/Playwright 运行器；不把它写作原生 computer-use，也不把 seeded 数据写作真实商业或 GA 证据。

## 当前证据基线

| 范围 | 当前结果 | 证据边界 |
| --- | --- | --- |
| admin/developer/viewer Workspace 路由 | 180 个页面分区报告通过；请求、响应、控制台、溢出、裁切、交互重叠、标签问题均为 0 | 隔离 seeded 身份；不替代真实组织和付费链路 |
| 角色全路由授权审计 | admin 47、developer 27、viewer 1 个授权路由；深链拒绝后回到角色可访问首页 | 当前迁移头；不等于真实企业 RBAC 验收 |
| 高密度控件统计 | `role-permissions` 643 个控件；`webhooks` 87 个；`internal-finops` 48 个 | 以可见控件和页面结构为准，属于 UX 负荷证据 |
| Partner/Device | seeded Partner attribution 和 Device 注册 201 已通过 | disposable seeded；生产数据路径仍缺 |
| 浏览器运行器 | 一次数据库准备阶段无日志挂起，未生成终态 | 测试运行器/环境问题，不能计为浏览器通过或产品失败 |

2026-09-22 当前补充：管理员预算、Webhook、角色权限和内部 FinOps 四页分片已在当前源码重跑并通过，证据 `.artifacts/product-qa/20260922-targeted-admin-role-audit-r4/prd148-role-audit-chromium-scale-1.json`。预算英文静态混排已归类为真实展示问题并修复；环境名、组织名等用户数据不参与通用翻译。多语言收口不改变中文首发优先级，也不替代真实注册、付费、Partner/Device 生产数据或 GA 门禁。

Webhook 几何问题已修复：历史报告中的事件复选框/保存按钮重叠来自滚动窗高度不是完整行高；当前 admin/developer 复验见 `.artifacts/product-qa/20260922-webhook-overlap-{admin,developer}`，两个角色均为 0 overlap、0 clipping、0 overflow。历史问题保留用于审计链，不再作为当前未修复产品失败。

2026-09-22 历史异常页面复验：`environments`、`providers`、`provider-health`、`policy-snapshots` 按 admin/developer/viewer 分角色重跑，共 12 个页面目标通过；请求失败、响应错误、控制台错误、溢出、裁切、交互重叠和授权异常均为 0。证据为 `.artifacts/product-qa/20260922-targeted-historical-failures/prd148-role-audit-chromium-scale-1.json`。这批结果只说明历史异常未在当前隔离 seeded 源码上复现，不替代真实注册、商业计量或生产数据验收。

同一当前源码上的控制面 GA Launch 集成批次 14/14 通过（0 failures、0 errors、0 skipped），覆盖签名证据导入、独立回滚、outbox 重试、迁移保护和稳定性合同边界；JUnit 证据为 `outputs/prd148-ga-launch-current-20260922.xml`。该批次验证控制面合同，不等于真实注册到付费或生产 GA。

当前源码 S1 真实浏览器重放使用历史脚本要求的 `d1634e8b...` 基线副本，实际完成注册、创建组织、Provider/模型入口、成员邀请及个人 KEY/轮换停用前置动作；在 `1280.onboarding-navigation.tenant` 的截图等待字体阶段超时，API 无 5xx、页面错误和待加载资源。证据为 `.artifacts/s1-browser-r86v1rw6/acceptance.json`。脚本要求的另一条固定基线 `062ae8e3...` 仍不存在，因此本次不能作为完整当前注册到撤权或付费链路通过。

同一基线的 finance 分支两次均未进入计量断言：`.artifacts/s1-browser-hmc0dc5u/browser.json` 记录身份静态资源请求失败，`.artifacts/s1-browser-n3md83li/browser.json` 记录 quickstart shell 未挂载和 Windows access violation。两份都归类为测试运行器/环境阻塞，不能作为财务或额度通过证据，也不能据此判定产品逻辑失败。

## 合并问题

| ID | 域/角色 | 观察 | 分类 | 本批处理 |
| --- | --- | --- | --- | --- |
| UX-ROLE-001 | Workspace admin · 角色权限 | 643 个可见控件同时展示六角色矩阵；已有分组提示，但默认仍要求用户先理解全矩阵 | 真实产品 UX 问题 | 默认改为当前角色视图，保留显式“对比全部角色”模式；授权和 PATCH 契约不变 |
| UX-WEBHOOK-001 | Workspace admin/developer · Webhook | 事件订阅复选框随创建表单全部展开，事件类型增长会直接放大首屏表单高度和扫描成本 | 真实产品 UX 问题 | 订阅事件改为默认折叠的可搜索区域，显示已选数量；CEF/JSON 约束和原有输入选择器不变 |
| UX-PLATFORM-001 | SaaS 平台运营 | 平台导航必须保留运营、安全、账务、发布和供应商能力，但总览首屏更适合先看状态/待办而非模块名词 | 真实 UX 改进项 | 本批不删除导航；记录为后续状态优先工作台改造，不伪造缺失的风险事实 |
| EVD-LOCALE-001 | admin 高级页面 | 部分报告 textSample 出现中英文混排；报告未绑定 locale，无法区分默认中文界面与英文回归 | 证据不足 | 增加 locale 绑定的专项回归后再决定是否改页面文案 |
| UX-ACCESS-001 | viewer/employee 深链 | 无权限深链会回到“我的接入”，并显示入口不可用提示；当前请求和布局均通过 | 当前行为可解释，仍需真实用户确认 | 保留现状；不扩大员工默认菜单，不把回退当成权限失败 |
| EVD-DATA-001 | Partner/Support/Device/Finance | 当前正向页面主要来自 seeded 或空态；真实成员归属、设备交付、订阅/计量和支持数据未闭合 | 证据不足 | 继续保留开放门禁，不以局部页面通过宣称完成 |
| FIXTURE-001 | 全角色浏览器分区 | 数据库准备阶段出现父子 Python 无日志挂起 | 测试夹具/环境问题 | 保留运行目录和失败事实；不修改产品门槛或覆盖历史产物 |

## 修改边界

本批只处理 UX-ROLE-001 和 UX-WEBHOOK-001。两项修改都只改变默认信息密度和可见顺序，不改变后端 RBAC、租户边界、权限上限、Webhook 事件白名单、签名密钥一次性展示、CEF 约束或写入接口。其余项目继续作为开放证据或后续批次，不在局部通过后提前关闭。

## 复验要求

- 权限矩阵：默认视图的控件数量下降；角色切换、分类筛选、草稿、CAS 冲突、保存和移动端列表仍可用；显式对比模式仍可查看全部角色。
- Webhook：创建表单默认只显示摘要和已选数量；展开后搜索、CEF/JSON 切换、事件选择和提交字段保持不变；已有端点详情和投递筛选不受影响。
- 运行 `permissions`、`webhooks`、相关 human walkthrough 单测；再运行前端全量测试、lint 和 build。
- 更新 OpenRouter 差距分析、走查记录和当前证据索引，并保持 `prd148_complete=false`。

## 第十三批补充：高级页面语言问题

本批复核确认 `EVD-LOCALE-001` 中的混排并非只有证据绑定问题：`en-US` 的角色权限矩阵和 Webhook 页面存在真实中文硬编码。处理结果如下：

| ID | 域/角色 | 观察 | 分类 | 处理结果 |
| --- | --- | --- | --- | --- |
| UX-LOCALE-002 | Workspace admin · PAGE-085 | 服务端返回中文角色名时，英文矩阵仍可能在保存栏和 aria-label 显示中文 | 真实产品 UX 问题 | 已按已知 role code 做英文投影，并补充中文 DTO 的英文回归断言 |
| UX-LOCALE-003 | Workspace admin/developer · Webhook | 标题、事件、状态和错误文案在英文页面混入中文 | 真实产品 UX 问题 | 已统一 locale 投影，CEF/JSON、事件白名单、一次性密钥和 RBAC 契约不变 |
| UX-LOCALE-004 | Shell · Partner | Partner 切换失败消息固定为中文 | 真实产品 UX 问题 | 已接入 `shell.partner_handoff_failed` 中英文键 |

当前源码与验证：`outputs/prd148-source-current-20260921-openrouter-role-webhook-i18n-final.json`，SHA256 `fab0506baa4c5e4c9b1c26671b22f62370a4d9549665017de8142134cb75e71f`；定向 25/25、前端全量 2226/2226、lint 838 files、build 294 assets/276 modules。构建先发现并修复资源摘要漂移，后端 manifest 对账测试通过。

本批没有新的浏览器终态，英文高级页面仍需新的隔离浏览器证据；真实商业链路、Partner/Device 生产数据、会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 继续保持证据不足，`prd148_complete` 不得变为 `true`。

## 第十四批补充：旧页面英文静态文案

分区走查确认 `EVD-LOCALE-001` 的范围还包括 Provider、模型、预算、成本异常、治理、审计、可观测性、支持和开发者中心旧渲染器。新增的静态文案投影只在 `en-US` 授权内容出口运行，登记词汇之外的用户数据和稳定协议字段不参与替换。定向投影 2/2、权限/Webhook 27/27、全量前端 2228/2228、lint 841 files、build 295 assets/277 modules 通过；随后 admin 英文隔离 Chromium 分区 10/10 通过，textSample CJK 字符为 0，证据为 `.artifacts/product-qa/20260922-openrouter-en-admin-static-i18n-r2/prd148-human-interaction-chromium-scale-1.json`。

该修复不改变导航、RBAC、租户隔离、员工接入合同或后端 API。新的英文高级页面 Chromium 终态尚未生成，真实商业链路、六方会签、回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍未闭合。

## 第十五批补充：当前源码角色分区证据

异步 hydration 路径和发布资源摘要同步后，当前源码重新完成分区 Chromium 走查：admin 57/57、developer 57/57、viewer/employee 60/60，全部无请求失败、响应错误、控制台错误、布局、动作或授权问题。admin 的三批 `en-US` 页面 CJK 样本为 0；developer/viewer 为预期 `zh-CN`，中文不属于英文混排失败。证据目录为 `.artifacts/product-qa/20260922-{admin,developer,viewer}-i18n-*`，当前源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `32c39158491e80b47ae38ca0cacb93b728b3948509fa51eaedce053b2bee4c1f`。

本批确认的是当前源码和隔离 seeded 角色分区回归；统一 180 页运行器、真实注册到付费、企业治理/私有 Gateway、Partner/Device 生产数据、六方会签、发布复核/回滚、财务/隐私/安全/迁移对账、正式 259200 秒稳定性和 HEAD/origin 仍是开放门禁。`prd148_complete` 不得变为 `true`。

## 第十六批补充：平台运营与有数据域分类

| 范围 | 当前结果 | 分类 |
| --- | --- | --- |
| SaaS 平台运营 | 3 个视口通过，状态读数 2/2，布局和控制台检查为 0 问题 | 已通过，隔离 seeded |
| Workspace admin/developer/viewer | 分角色各 60 个路由通过；三角色统一 300 秒批次无终态 | 分片通过；统一批次为运行器容量/环境问题 |
| Support | admin/developer 全部写入动作和刷新持久化通过，viewer 写入 403 | 已通过，隔离 seeded |
| Device | 创建注册 201、一次性材料可见、刷新后部署可见 | 已通过，隔离 seeded；生产交付仍缺 |
| Partner | 显式 handoff 200、归属投影 200、布局无问题 | 已通过，隔离 seeded；生产成员归属仍缺 |
| 正向脚本网络事件 | License 未签发 404、域切换 401、卸载取消已明确分类，意外网络/控制台错误为 0 | 测试边界已修正，不把预期事件写成失败 |

## 第十七批补充：异步面板英文混排修复

| ID | 域/角色 | 观察 | 分类 | 处理结果 |
| --- | --- | --- | --- | --- |
| UX-LOCALE-005 | Workspace admin · Support | 异步刷新后的状态与正文绕过统一英文投影 | 真实产品 UX 问题 | Support hydration 输出接入 `localizeConsoleStaticHtml`，保留中文默认和动态数据 |
| UX-LOCALE-006 | Workspace admin · Private Gateway | 异步刷新后的部署页混入中文静态标签 | 真实产品 UX 问题 | Private Gateway hydration 输出接入同一投影，权限、部署数据和一次性材料契约不变 |

当前验证：异步面板断言 1/1、前端全量 2229/2229、lint 841 files、build 295 assets/277 modules；源码证据为 `outputs/prd148-source-current-20260922-openrouter-static-i18n.json`，SHA256 `b1f008bbbc5b02f28138fb53b669cc2fdcc4e61434a6943b933fafc2b1f5ff5e`。资源摘要漂移已同步前后端 manifest 并记录为发布对账问题。真实注册到付费、企业治理/私有 Gateway、生产 Partner/Device 数据、会签、回滚、对账、正式 259200 秒观察和 HEAD/origin 仍为开放门禁。

三角色异步面板复验随后完成：admin、developer、viewer 各 2 页均通过，0 响应错误、0 请求失败、0 控制台错误、0 布局/动作/授权问题。证据目录分别为 `.artifacts/product-qa/20260922-post-i18n-{admin-r2,developer,viewer}`。第一次参数错误运行目录 `.tmp/product-qa-browser/20260921-195926-ca54818e` 保留并分类为测试夹具问题。

## 当前收口记录（2026-09-22）

OpenRouter 对标修改的当前状态已经归并为：

| 项目 | 当前状态 | 证据 |
| --- | --- | --- |
| 默认入口和四个管理员工作域 | 已落地 | `console-web/src/features/workspace-navigation/renderWorkspaceNavigation.js`、分角色审计目录 |
| 财务例外优先和诚实空态 | 已落地 | `humanWalkthrough.test.mjs` 的 finance/empty-status 契约 |
| 员工统一 Gateway + 个人 KEY | 已落地 | `humanWalkthrough.test.mjs`、当前员工分区报告 |
| 英文静态/异步面板一致性 | 已落地 | `.artifacts/product-qa/20260922-post-i18n-{admin-r2,developer,viewer}` |
| 外层卡中卡移除 | 已落地 | `outputs/prd148-openrouter-card-band-current-20260922.json` |

六方会签和 scheduler-enabled 连续 259200 秒稳定性验证按用户明确指示不执行，不能作为通过证据。真实注册到付费、生产 Partner/Device 数据、发布复核/回滚、财务/隐私/安全/迁移对账和 HEAD/origin 仍归类为证据不足或未完成，`prd148_complete` 不得变为 `true`。

## 2026-09-23 当前源码全量分片复验

| 范围 | 结果 | 分类 |
| --- | --- | --- |
| Workspace admin/developer/viewer | 4 个分片覆盖各 60 个注册路由，共 180 个角色与路由组合；页面、请求、响应、控制台、溢出、裁切、交互重叠、未结算响应和禁止内容均为 0 | 已通过，隔离 seeded |
| SaaS 平台运营 | Platform session、迁移状态 2/2、三视口通过，0 请求/控制台/布局问题 | 已通过，隔离 seeded；不与 Workspace 证据混用 |
| Support/Device/Partner | Support 生命周期、viewer 写入 403、Device viewer 403 回退、Partner handoff 200 和 membership 403 均符合合同 | 已通过，隔离 seeded |
| 冷启动服务就绪 | 固定 35 秒在完整 SQLite 迁移/seed 后提前超时，浏览器尚未启动 | 测试夹具/运行器容量问题；默认就绪等待改为可配置的 90 秒 |

本轮没有新增产品 UX、权限、布局或逻辑问题，因此没有触发新的页面实现修改。证据见 [2026-09-23 人类式走查记录](PRD-148-human-audit-20260923.md) 与 `outputs/prd148-human-audit-current-20260923.json`。历史登出与 page-view 并发失败、真实商业和生产门禁仍按既有分类保留，`prd148_complete=false`。
