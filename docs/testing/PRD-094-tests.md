# PRD-094 测试记录

状态：原批次已完成，以下保留当时验收记录；2026-09-07 的跨页退出补充修复见文末。
原批次下一项为 `PRD-095 - SaaS平台运营角色与受控支持访问`。`saas_incremental_roadmap_complete=false`，
`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| Config/DB/API/BB/GR/SEC/FLOW/PERF | PRD-094 配置、0086 迁移与 Workspace access 完整专项 `21 passed` | 独立根密钥、0085->0086/pristine down/up、九角色回填/委派、七路由、邀请升级、动态撤权、跨 Workspace、凭据负矩阵、100 并发、warm p95 和敏感 canary |
| Batch Gate | PRD-092..094 统一 Python `69 passed`；Identity/Workspace Console focused `29 passed` | 同一业务域的注册、Workspace 创建、Membership/切换配置、迁移、API、并发、性能、前端状态机和隐私合同统一复验 |
| COMP/FLOW | PRD-059/060/091/092/093 集成回归 `50 passed`；导航/config 定向回归通过 | 旧 RBAC/邀请/Bearer、Global Identity、自助注册、Workspace 创建和 N/N-1 migration guard 保持兼容；暖连接不跨请求或测试数据库污染 |
| WEB/UI/UCD/COMP | PRD-094 focused Console `29 passed`；Console lint `308 files` 与 JS syntax 通过 | 精确 wire/DTO、选择器、成员管理、页面内 Owner 转移显式确认、邀请 token 不进 DOM、BroadcastChannel、Abort/generation、Storage 零调用 |
| Access-log privacy | 启动器与入口隐私单元 `6 passed`；真实 410 canary 通过 | 开发启动器固定使用 `metahub_control.main` 且 `access_log=False`；日志只记录参数化 `/invitations/{token}/accept`，不记录原始 token |
| Static | 定向 Ruff、JS syntax、`git diff --check` 通过 | Workspace access 后端/共享配置/迁移/前端语法与格式；仅保留既有 `.env.example` CRLF 提示 |

## 迁移与数据验收

- 0086 从完整 0085 重建 `workspace_memberships`，保留 creator owner，按 legacy role 回填其余 GlobalAccount
  link，并为重复 account/workspace 建立 migration conflict，禁止静默择一。
- 新增 `membership_invitations`、`workspace_sessions`、`owner_transfers`、`membership_events` 与
  `membership_event_outbox`；复合外键证明 workspace/tenant/account/user owner 链。
- pristine down 仅在没有 Session、新邀请消费、成员 role/status 变化、OwnerTransfer、revision 或新增授权事实时
  允许；否则原子失败。回滚恢复 0085 trigger，不复活 Session、角色或邀请事实。
- 0086 兼容 trigger 对新 legacy User 只回填非 owner Membership，不替换 immutable Workspace creator；
  其他 creator 变更继续由数据库拒绝。

## 安全与隐私结论

- WorkspaceSession 使用独立 `METAHUB_WORKSPACE_SESSION_SECRET`、Session/CSRF Cookie 与 issuer/audience
  语义。Global Cookie、Workspace Cookie、Legacy Bearer、Platform/Support/Device 和 service_account
  主体按路由固定解释，混合凭据失败关闭。
- 每请求从数据库复核 account、GlobalSession、Workspace、binding、Membership、workspace/account/
  permission revisions 和矩阵 fingerprint。成员变更、Owner 转移或 GlobalSession 撤销后的下一请求失败。
- 敏感 canary 执行 Global 登录、Workspace 切换和 Membership 邀请后扫描 SQLite 主文件、WAL、SHM 和
  捕获日志；管理员明文密码、两组 Session/CSRF Cookie 和一次性邀请 token 命中均为 0。
- 邀请 token 只在创建响应的当前内存生命周期出现，不进事件/Outbox/数据库普通列/日志/DOM/Storage。

## 浏览器与性能

- 自动化 warm Workspace 切换先预热一次，再采样 20 次完整切换；nearest-rank p95 满足 `<300 ms`，
  终态 21 条 Session 中恰有 1 条 active。
- 首轮完整专项在逐请求新建 SQLite 连接时测得 p95 约 `310 ms` 并按门限失败；实现改为复用既有容量 4
  的 request-scoped 暖连接池后，定向性能与完整 `21 passed` 均通过。门限、样本算法、事务和权威复核
  均未放宽。
- 100 并发切换终态恰有一个 active WorkspaceSession；100 并发成员停用和 Owner 转移均最多一次提交，
  没有最后 owner 缺口或双 owner 中间态。
- 应用内 Browser 使用本地测试凭据完成登录与单 Workspace 自动进入，并创建第二 Workspace；可搜索选择器、
  键盘选择/表单动作、Workspace 切换、成员 role/status 编辑、邀请和 Owner 转移均通过。
- 邀请响应中的一次性 token 未进入 DOM；Owner 转移必须输入当前密码并勾选页面内显式确认。提交成功后，
  原 Owner 的 WorkspaceSession 立即失效，下一请求不能继续访问旧授权上下文。
- 在干净单标签环境采样 8 次切换到首个角色页可交互时延：`773.7/792.0/795.4/795.9/797.7/
  805.8/814.3/816.8 ms`；nearest-rank p75 为 `805.8 ms`，通过 `<1.5s` 门限。性能遥测回调只接收
  `durationMs`，不携带 workspace、tenant、account、session、role 或 token。
- 390、1280、1440 三个视口均无页面横向溢出、文本或控件裁剪、控件越界和交互区域重叠；Console 错误为
  0，验收结束后已恢复视口覆盖。安全截图保存在忽略目录 `.artifacts/prd094-browser/`：
  `workspace-mobile-390.jpg` SHA-256 `fb4d228ead2e549f56a61e8c9913b8fc2eeb450cface6c11b70993a2060b0f23`，
  `workspace-desktop-1280.jpg` SHA-256 `ef5af51def30d4bd9829beff1e670c64580be298712d2f6ab2fc4ba9c4171f4f`。
- 真实 access-log canary 通过仓库开发启动器运行 Control API。失效邀请返回 410，进程输出仅出现参数化
  `/invitations/{token}/accept`，没有 Uvicorn 原始请求行或邀请 token；回归测试阻止启动脚本重新引入
  `python -m uvicorn metahub_control.app:app` 这类绕开安全入口的命令。

## 敏感审计

源码、测试、迁移、文档和生成 Excel 继续执行 Key/Token/密码/私钥模式扫描；测试使用的账号凭据、Cookie、
CSRF 和邀请 token 不写入验收文档或 git。浏览器证据目录只允许安全截图、结构化时延、视口和 hash，不保存
浏览器 profile、SQLite、WAL/SHM、网络原始正文或 Storage dump。

以上为原批次 PRD-094 关闭记录，不代表后续 G6/G8 或全部 SaaS 目标已完成。

## 跨页退出补充修复 (2026-09-07)

PRD-137 的真实 HTTPS/Chromium 竞态测试发现退出事务完成后，另一账号页仍保留受保护 shell。
原因是退出通知只在提交前发出，接收页只复核 Workspace；提交前的有效摘要与随后 401 可能让
页面停留在空间错误态。产品修复归属 PRD-094，第 17 节已先记录设计与两轮自审。

退出和撤销当前设备保留立即清空/广播，并在异步写入 finally 再广播。接收页先清全部受保护状态、
中止旧请求，再从 `/identity/me` 复核 GlobalSession。消息仍只含 type/generation，不传账号或
凭据；写失败也仅触发权威重读，不将消息等同于服务端撤销成功。后台鉴权、迁移、DTO 均不变。

原始证据独立保存在 `.artifacts/g6-security-20260907/`：

- `prd094-cross-tab-red-r1-junit.xml`：旧实现运行新增回归，12 项中 7 通过、5 失败，确认测试
  能检出缺少提交后通知及缺少 GlobalSession 重验的问题。
- `prd094-cross-tab-unit-r1-junit.xml`：最终产品修复的 Identity/PRD-094/PRD-146 导航定向
  47/47。覆盖 logout/revoke-current 成功与失败的前后通知、立即清空、旧 me 中止和迟到结果不复活。
- `browser-r4-junit.xml`：修复前桌面、移动两项均失败，JUnit 用时 215.537 秒。两视口实际切换、
  旧成员响应隔离均已通过，退出 API 200 后主页面未进入登录态。原件不覆盖、不改记为通过。
- `browser-r5-junit.xml`：修复后两项均通过，JUnit 用时 155.275 秒（pytest 终端 155.35 秒）。
  1440x900、390x844 各有 22 个通过检查点，实际旧响应交付后不恢复旧 DOM，并各观测到 2 次
  浏览器原生中止；旧 Workspace 和退出前 Cookie 重放均被服务端拒绝。

已查看两视口退出截图，输入框均遮罩，没有成员或空间受保护界面残留，无横向溢出。
浏览器私有运行目录已移除，克隆基线不变，原始复用基线 SHA-256 仍为
`572173fbf10027815ea1e1ecbdd201d3b7084f6335a86e436a762a68945f3db6`。
截图位于 `.tmp/pytest-prd137-browser-r5/test_real_identity_pages_fence0/logged-out.png`
和同级 `test_real_identity_pages_fence1/logged-out.png`，属于 PRD-137 真实账号页证据。

本次独立审计 `prd-094-cross-tab-logout-sensitive-audit.json` 核对产品状态机、定向测试、设计和
本记录共 4 个精确暂存文件及上述 4 份原始证据。审计模式扫描不等于外部安全会签。
未重复整套数据库/性能/全域浏览器验收；主控制台、评估短许可、固定 Guest 及完整 G6 继续未完成。
