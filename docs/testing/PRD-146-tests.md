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
