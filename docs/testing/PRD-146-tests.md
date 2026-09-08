# PRD-146 测试记录

状态：单 PRD 聚焦测试与 Chromium smoke 通过。S7 批次 G7 依照开发计划在 PRD-147 完成后统一执行。

## 专项范围

| 测试组 | 覆盖 | 结果 |
| --- | --- | --- |
| Control API / 状态机 | 断连请求 fail-closed、lease fencing、投影/outbox/reconciliation、导航/任务/高级工具、偏好 CAS 与幂等 replay | 6/6 通过 |
| 0138 migration / shared config | fresh matrix、pristine roundtrip、0137 upgrade、fact-loss down guard、scheduler 默认关闭与严格布尔配置 | 4/4 通过 |
| WorkspaceSession 授权合同 | 真实登录/切换、独立 authorization summary、Membership/permission revision 与 fingerprint | 1/1 通过 |
| Console PRD-146 单测 | Cookie Session、严格 DTO、后端 canonical route、ticket/epoch fencing、任务首页、高级工具、移动端与生命周期清理 | 13/13 通过 |
| 受影响前端回归 | 核心 Console/Identity/Workspace 集 72/72；扩展 PRD-097/102/114-118/134/135 权限页共 126/126 | 全部通过 |
| Chromium smoke | admin 1280x720、developer 1440x900、viewer 390x844；真实登录、任务首页、高级工具与键盘流程 | 3/3 通过 |
| 静态检查 | Console lint 477 文件、Python Ruff 聚焦路径、Git diff whitespace | 通过 |
| 敏感审计 | 高置信密钥/JWT、Bearer、浏览器持久化、Console 输出和正式 JSON 凭据字段 | 零命中 |

受影响前端回归的 126 项包含核心 72 项和 13 项 PRD-146 单测，因此不重复计数。Edge、Firefox、性能、长稳、
Windows/Linux、SQLite/PostgreSQL、N/N-1 与全量安全回归保留到 PRD-147 后的 S7 G7。

## 自动化结果

后端：

    pytest control-plane-api/tests/integration/test_prd146_workspace_navigation.py
    6 passed

    pytest control-plane-api/tests/integration/test_prd146_db_migrations.py \
      shared-sdk/python/tests/test_prd146_workspace_navigation_config.py
    4 passed

    pytest control-plane-api/tests/integration/test_prd094_workspace_access.py \
      ::test_workspace_switch_navigation_mixed_credentials_and_logout_cleanup
    1 passed

    ruff check <PRD-146 focused paths>
    All checks passed

API 与状态机覆盖：

- WorkspaceSession 与 GlobalSession 的职责隔离、四 Cookie 共存、独立 authorization summary、CORS 暴露权限指纹，
  以及混合或错误主体失败关闭。
- 64 个 Excel source、4 个 approved extra、5 个恢复项、6 个退休项与编译期 canonical route registry。
- Navigation/RoleTaskHome/AdvancedTool 投影按当前 Membership、permission revision、entitlement 与 owner 求交。
- NavigationPreference 的 GlobalSession、CSRF、quoted ETag、幂等 replay、跨 Workspace 同形 404 和撤权漂移拒绝。
- Advanced tool open 的来源权限、entitlement ETag、唯一 event/outbox、未知结果恢复与 projection stale 拒绝。
- scheduler lease 丢失后的迟到写 rollback、持久 cursor、fence、outbox 与 reconciliation 恢复。
- Starlette `ClientDisconnect` 不逸出为 500，统一映射为 `identity.request_invalid`。

前端：

    node --test tests/unit/prd146WorkspaceNavigation.test.mjs
    13 passed

    node --test consoleShell + mainRefreshOwnership + prd094WorkspaceAccess \
      + identity + developerQuickstart + prd146WorkspaceNavigation
    72 passed

    node --test <上述核心集 + PRD-097/102/114-118/134/135 Identity 权限页>
    126 passed

    node scripts/lint.mjs
    console lint ok (477 files)

专项断言包括：

- client 只使用 Cookie Session、`credentials: include` 和 `cache: no-store`，不发送 Bearer。
- `/workspace/session.authorization` 是 Identity 权限全集的唯一来源；缺失摘要严格拒绝，旧 Navigation 权限字段不能启用操作。
- Session 与 Navigation 的 Membership/permission revision 或 fingerprint 漂移时清空 Workspace 状态、最多重取一次，
  且不会调用 Conditional Access 等受保护来源 API。
- response fingerprint、Membership revision、contract generation 与 navigation epoch 只能由当前 owner 的票据提交。
- hash/direct route 只接受本次后端 allowed set；退休、detail、advanced 与 account route 不从旧静态 import 恢复。
- route 切换只加载当前页面；切换、Workspace invalidation、logout 与 pagehide 会 abort 并清理私有状态。
- 业务组默认折叠且任一时刻最多展开一组；390px drawer 默认关闭，交互目标保持 44px。
- getting-started 使用后端真实任务事实；旧 `quickstart` 静态代码示例和 Gateway 地址不再进入壳层。
- Advanced Tools Dialog 支持 Escape、焦点归还和明确的来源权限二次鉴权。

## 真实 Chromium 证据

正式证据：`.artifacts/prd146/browser-real-chromium-scale-1.json`。

- 三条 journey 全部通过：admin `1280x720`、developer `1440x900`、viewer `390x844`。
- `console_error_count=0`，`request_failure_count=0`，横向溢出、控制裁切与交互重叠均为 0。
- 三条 journey 的键盘导航、任务首页可见性和原生表单校验通过；适用角色的高级工具 Dialog 流程通过。
- 匿名登录前 `/identity/me` 与 `/workspace/session` 的预期 401 单独精确计数，登录后 Console error 仍为 0。
- 数据库应用 138 个 migration，latest `0138`，integrity `ok`，foreign key violation 为 0。
- 浏览器安全诊断只记录 route ID、合同形状与错误类别，不记录请求凭据或业务正文。

## 测试过程修正

- 工作簿首次自动渲染第三张大表时请求了 `1448x30778px` 画布，被 artifact-tool 拒绝；权威文件当时尚未修改。
  后续改为每张工作表有界渲染，并单独渲染 PRD-146 聚焦范围。
- 首次工作簿后置公式扫描实际返回“0 个匹配”，但校验脚本错误读取不存在的 `total` 字段，因此在已完成导出后退出。
  随后的纯只读验证按 NDJSON match 记录计数，源文件和输出副本均确认公式错误为 0。
- 扩大的 71 项前端回归首次发现 `developerQuickstart` 仍断言旧静态向导。PRD-146 已把 quickstart 迁到后端事实驱动的
  Workspace task home；测试改为验证任务 loading、旧代码示例与 Gateway 地址不渲染，随后 71/71 通过。
- 扩展 Identity 回归发现新 Navigation DTO 不再携带权限全集，而页面仍从旧字段启用 Conditional Access、Data
  Governance、Privacy、Compliance 与 Machine Identity。现由 `/workspace/session.authorization` 提供唯一权限事实，切换后
  重新读取 Session，并对 Navigation 三项授权证据做失败关闭比对；核心 72/72、扩展 126/126 通过。

## 两轮实施自审

### 第一轮：owner、权限与持久事实

- NavigationPreference 位于 Identity API，但写事务从 GlobalAccount 重新查询目标 Workspace 的唯一 active human Membership；
  body 中的 Workspace ID 不作为 owner 证明。
- Workspace 投影不持久化成员可见集合；每次读取与当前 Membership permission、entitlement 和 fingerprint 求交。
- Identity 不从 Navigation 或 role 推断权限；Session authorization summary 与 Navigation 的三项证据不一致时不会发出
  受保护来源请求。
- service account、OAuth、Virtual Key、Edge、Platform、Support 与 Bearer 不能借浏览器导航进入 Workspace。
- 0138 migration 绑定复合 owner，固定 64+4 catalog、5 个恢复项、6 个退休项，并对非 pristine down 失败关闭。
- preference/open command 的 CAS、幂等记录、event/outbox 与审计在同一事务中保持一致。

### 第二轮：活动调用、恢复与体验

- route-only dispatcher 不预取未激活页面；late response 不能覆盖更新的 owner、contract 或 epoch。
- 任务完成只来自 PRD-097 和业务 source fact，不提供用户勾选“完成”的伪事实入口。
- AdvancedToolEntitlement 是只读 availability 投影，不是第二套权限授予表；open 后来源业务 API 仍完整鉴权。
- 默认折叠、单组展开、移动端关闭、Dialog 键盘/焦点及 390/1280/1440 smoke 与产品合同一致。
- CORS、Cookie 共存和断连 body 三个真实浏览器/传输缺陷均有后端回归，不只依赖前端 smoke。

两轮自审无未解决 P0/P1；批次级性能、跨浏览器、跨数据库和版本兼容仍由 PRD-147 后的 G7 给出结论。

## 敏感审计与批次门禁

- 对 PRD-146 migration、Control、Console、共享配置、测试、设计文档与正式证据执行只报文件名的 private key、云密钥、
  OpenAI/GitHub key 和 JWT 高置信模式扫描：零命中。
- 新 Workspace navigation client/store/controller 扫描 `Authorization`、Bearer、`localStorage`、`sessionStorage`、IndexedDB、
  Cache API 和 Console 输出：零命中。
- `.artifacts/prd146` 的 JSON 证据扫描 password、Cookie、CSRF、Authorization、Bearer、幂等键、Provider Key、client secret、
  private key、Prompt 与 Response 字段：零命中。
- 本地管理员凭据只在自动化进程内存中输入，未打印、未写文件、未进入截图、日志、fixture 或 Git。
- `.tmp` 是可删除、可重建的测试材料；正式证据只进入 `.artifacts`、本记录和权威 Excel。
- 本提交使用显式路径暂存，不使用 `git add .`，不纳入 PRD-147/S7 文档、PRD-101..105 文档或 `outputs/`。

## 权威计划回写

- 权威 Excel 的 `01_PRD总表!B58` 已从“未启动”更新为“已完成”；PRD-147 保持“未启动”。
- 权威文件和 `outputs/prd146-roadmap-closeout` 副本均由 artifact-tool 重新导入验证：12 个 sheet，公式错误扫描 0 命中。
- `B57:B59` 的计算样式保持一致，`styleId=3`；只修改 `B58` 的值。
- 12 张工作表顶部与 PRD-146 聚焦范围共 13 张 PNG 均非空，聚焦预览已人工查看，无明显覆盖或裁切。
- 正式工作簿验证记录位于 `.artifacts/prd146/workbook-closeout/verification.json`。

## 2026-09-07 主 Console Cookie 导航 owner 补充

设计及两轮实施前自审见 PRD-146 第 13、13.1 节，与 PRD-147 消费端迁移同批推进。
当前主入口的旧 Token 启动守卫仍未修复；本条只交付已接入 main 的导航共享生命周期修正，
不将其记成真实 Cookie 入口或全部可达业务页面迁移完成。

- 控制器使用既有 Cookie 客户端读取 `/identity/me`、`/workspace/session`、`/workspace/navigation`。
  owner 绑定实际 GlobalSession 公开句柄、撤销代次、两类会话到期时间、Workspace、角色和三项
  授权证据，返回账号/Workspace 事实供后续主入口使用；不生成 Legacy Token 或旧内部 ID。
- 新导航读取开始就中止旧导航；当前响应的 permission revision/fingerprint/membership revision
  必须与 WorkspaceSession 一致。同角色新会话、授权变化和新导航 epoch 淘汰旧私有请求。
- 迟到成功/401/403 都不能改写新状态。旧偏好成功不再把新的 ready 状态改成 conflict；当前
  撤权或合同冲突则清空导航、任务、高级工具及活动请求。相同 owner 的临时写失败仍复用原幂等键。
- main 传递当前刷新 owner，在导航返回、高级工具返回及错误分支写状态前均重验。隔离执行实际
  shell loader 的单元验证：旧 401/403/500 不清新会话，旧成功不改 hash/偏好，当前 401 仍清理。

原始 JUnit 独立保留在 `.artifacts/g6-security-20260907/`：

| 记录 | 结果 | 说明 |
| --- | --- | --- |
| `prd146-navigation-owner-red-r1-junit.xml` | 23 项，13 通过、10 失败 | 修复前记录；8 项揭示产品缺口，2 项因新增 deferred 夹具缺少 reject，不能算产品复现 |
| `prd146-navigation-owner-r1-junit.xml` | 23 项，9 通过、14 失败 | 初次实现；夹具误将 navigation epoch/permission/membership 版本混用，并仍缺 reject；保留，不认定产品回归通过 |
| `prd146-navigation-owner-r2-junit.xml` | 23/23 | 修正夹具版本为现有 DTO 的 permission=10、membership=9，并补 reject |
| `prd146-navigation-owner-r3-junit.xml` | 83 项，81 通过、2 失败 | 扩展 owner/epoch 边界通过；两条旧源码结构断言仍硬编码无参数 shell loader |
| `prd146-navigation-owner-r4-junit.xml` | 86/86 | 更新结构断言，新增实际 shell loader 隔离执行测试 |
| `prd146-navigation-owner-r5-junit.xml` | 86/86 | 最终控制器对已失效导航 ticket 的错误也转换为 conflict |

最终命令为 `node --test --test-reporter=junit --test-reporter-destination=<上述 r5 路径>`，输入仅为
`prd146WorkspaceNavigation.test.mjs`、`mainRefreshOwnership.test.mjs`、`consoleShell.test.mjs`、
`prd094WorkspaceAccess.test.mjs`、`prd145ConsoleExperience.test.mjs`、`auth.test.mjs`。
这是受影响的前端单元及相邻回归，不是数据库/API/真实浏览器或完整批次 QA。前端 lint 763 文件、
production surface 258 模块/272 来源和 `git diff --check` 通过。

独立审计 `prd-146-navigation-owner-sensitive-audit.json` 核验本条 7 个精确暂存文件及上述 6 份
原始记录，使用严格 UTF-8、高置信凭据模式及暂存字节校验；不改变或提交数据库、截图、临时材料、
PRD-137 夹具、PRD-147 文档、用户 PRD-101..105 或 outputs。模式扫描不等于独立安全会签。
工作簿本轮只读，已有完成格不作为补充缺口已关闭的证据。G6/G7 全量验收、主 Console 真实入口、
全部资源客户端及跨页 DOM 生命周期仍需继续，PRD-148 不启动。

## 2026-09-07 主 Console Cookie 启动增量

本节接续 13.2 的设计及两轮实施复核，取代上一增量中“主入口守卫仍未修复”的现状描述，
不追改历史测试结论。启动清理旧认证槽位，通过真实 Identity/Workspace/navigation 建立只含
公共 owner 与冻结授权 DTO 的内存 session，不调用旧 auth/me，也不显示第二套租户密码表单。
账号、空间与角色菜单使用当前 shell 的公开 DTO，不构造旧 tenant/user/session 镜像。

刷新和导航回调同时核对代次与内存 session 归属。新 owner 通过共用清理销毁旧页面状态、正文、
草稿、cursor、下载元数据、控制器和流，但保留本次刚验证的导航。退出立即清 DOM，调用真实
Identity Cookie logout，并在 HTTP 前后发送无身份内容的跨标签失效消息。注销中不重复登出，
不因刷新或 peer 消息重新使用仍有效的 Cookie；注销失败不伪称服务器已撤销。

侧栏、分组、导航及偏好回调拒绝过期 shell，旧成功/错误不影响新 owner。当前 401/403/409
触发会话重验与统一清理。匿名或网络故障提供 Identity 入口和重试，不签发 Legacy Token。

原始报告位于 `.artifacts/g6-security-20260907/`，没有覆盖失败原件：

| 报告 | 结果及解释 |
| --- | --- |
| `prd146-cookie-entry-r1-junit.xml` | 8 条旧源码结构断言失败，不能作为新生命周期行为的反证 |
| `prd146-cookie-entry-r2-junit.xml` | 105 项中 1 条新夹具缺少 content/navigation 字段 |
| `prd146-cookie-entry-r3-junit.xml` | 105/105 通过 |
| `prd146-cookie-entry-affected-r1-junit.xml` | 6 条旧清理位置断言失败，随后按共用清理入口修正 |
| `prd146-cookie-entry-r4-junit.xml` | 242/242 通过，包含注销中刷新/广播及旧控件回调 |
| `prd146-cookie-entry-render-r1-junit.xml` | 42/42 通过，覆盖 shell、Console experience、Workspace access、branding |

新 `prd146ConsoleEntry.test.mjs` 读取并隔离执行完整生产 start、performRefresh、清理、退出与
导航函数，Promise 交错由测试控制，不重写一套生命周期模型。覆盖不完整/混合/过期 owner、
权限 DTO 冻结、首个 Cookie 启动、重叠刷新、迟到成功和错误、当前失效、注销 pending、旧完成
回调以及共用状态清理。相邻测试只更正生命周期结构断言，未放宽来源权限或后端鉴权。

前端 lint 768 files、production surface 260 modules / 274 shipped sources 和 diff-check 通过。
后续只读复核确认 r4 为 242 项、render-r1 为 42 项，失败/错误/跳过均为零；不为文档补录重跑
整套 QA。PRD-137 真实桌面入口 r8 的十五项通过另有记录，同轮移动端失败原件保留；该单元增量
不声称全部浏览器、64 项导航、业务客户端、PostgreSQL、性能或 G6/G7/G8 已通过。

独立审计 `prd-146-cookie-entry-final-sensitive-audit.json` 核对本条 17 个精确暂存文件及六份原始报告，
检查严格 UTF-8、高置信凭据模式、SHA-256、暂存字节和受保护原基线。仅提交六个源码、九个
测试和两份设计/测试文档，不纳入 PRD-137 夹具与证据记录、PRD-145 服务修复、用户未跟踪文档
或 outputs。Excel 只读；仍依赖旧 token 的可达业务页及页面翻译键必须继续按域修复。

## 2026-09-08 发布导航与同源 Cookie 收尾

本批对应设计 13.3：补全 Workspace registry 的中英文导航名称，未知文案键不进入标题或
tooltip；桌面收缩栏可见授权入口，移动端显示展开命令。移动抽屉采用独立会话内状态，默认
关闭、重绘保持、身份清理重置，不修改桌面的持久密度偏好。

真实 localhost 管理员模型别名请求复现控制面使用 127.0.0.1 丢失 Host-only Cookie，现统一
控制面 Cookie origin，实际模型别名和模型评分页均能读取数据。无 Cookie 复制、Legacy Token
注入或鉴权替身。两个 hostname 的真实 main 初始化代码均有定向执行测试。

13 条过时源码断言改为校验现有共享 cleanup 和 Workspace bootstrap；新增会话替换/跨页
广播实际执行每个清理函数的测试，以及生产移动 toggle、refresh、cleanup 的执行用例。
未通过源码正则重新引入旧密码表单或逐域重复清理。

本批原始报告目录为 `.artifacts/g6-security-20260907/`：

| 报告 | 结果 |
| --- | --- |
| `prd146-release-console-r1.xml` | 1806 项，1 个剩余旧 pagehide 断言失败，随后修正 |
| `prd146-release-console-r2.xml` | 运行未完成，保留不完整 XML，不计通过；独立 PRD-050 55 项随后通过 |
| `prd146-release-console-r3.xml` | 1812 项，1 个移动默认关闭断言失败，暴露桌面偏好与移动抽屉混用 |
| `prd146-release-console-r4.xml` | 1813/1813，0 失败、取消、跳过 |
| `prd146-release-browser-r1.xml` | 2/2，早期入口/概览/退出；在最终同源及移动修复前，不替代最终结果 |
| `prd146-release-browser-r2.xml` | 移动 1 项通过；桌面 BootstrapError，0 个业务检查点，私有目录已清理且基线不变 |
| `prd146-release-browser-r3.xml` | 只重验桌面，1/1；不能与 r2 拼写为一次全绿 |
| `prd146-release-manifest-r1.xml` | 早期 manifest 专项 1/1；最终 manifest 另经当前 build/verify 校验 |

最终桌面/移动两个独立浏览器运行各验证 15 个入口、刷新、无 Legacy 槽位、无 Bearer、实际
服务端退出、双标签失效、旧 Cookie 拒绝及水平溢出检查点。两张完整概览截图已查看：导航名称
与移动展开命令可见；概览指标仍有英文机器码衍生文案，留在 PRD-145 单独修复，不声明全视觉
验收完成。本机真实移动点击展开及切换模型评分页后仍可操作；业务写流程仍需后续检查。

最终 lint 777 文件、production surface 262 模块/276 来源、生产构建 276 资产通过；两端
asset-set 摘要为 `673c94f0608719f7dd1ef65572e32fa576fe7da6e9cbd435c41c1976cd82b135`。
独立敏感审计 `prd-146-release-navigation-sensitive-audit.json` 对精确暂存文件及上述报告验证
UTF-8、凭据高置信模式、暂存字节与受保护基线。未纳入本地凭据、数据库、用户文档或 outputs。
全目标及发布仍在推进；72 小时检查按用户要求后置，不在此处等待或虚报通过。
