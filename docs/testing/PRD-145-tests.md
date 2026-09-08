# PRD-145 测试记录

状态：单 PRD 聚焦测试通过。S7 批次 G7 依照开发计划在 PRD-147 完成后统一执行。

## 专项范围

| 测试组 | 覆盖 | 结果 |
| --- | --- | --- |
| Control API / 状态机 | Platform/Workspace 总览与助手隔离、审批短 cursor、GlobalAccount preference CAS/outbox、部署摘要/scheduler fence、revision 回退、poison outbox | 6/6 通过 |
| 0137 migration | fresh matrix、pristine roundtrip、legacy locale backfill、fact-loss rollback guard | 2/2 通过 |
| Console PRD-145 单测 | Cookie Session/域 CSRF、严格 DTO、store/AbortController/cursor 隔离、迟到/回退响应、偏好幂等与活动 dispatcher | 6/6 通过 |
| 受影响前端回归 | 角色首页、旧助手、旧审批、语言偏好及 PRD-145 新组件 | 57/57 通过 |
| 静态检查 | console lint 472 文件；Python Ruff 聚焦路径 | 通过 |
| 敏感审计 | 高置信密钥/JWT、Bearer、浏览器持久化，只报文件名扫描 | 零命中 |

受影响前端回归的 57 项包含 6 项 PRD-145 新单测，因此本条不重复计数的聚焦测试总数为 65。

## 自动化结果

后端：

    pytest control-plane-api/tests/integration/test_prd145_console_experience.py
    6 passed

    pytest control-plane-api/tests/integration/test_prd145_db_migrations.py
    2 passed

    ruff check <PRD-145 focused paths>
    All checks passed

API 测试覆盖：

- Platform 与 Workspace DTO、权限、projection generation 和 credential isolation。
- 聚合审批只读、分页 cursor owner 绑定及来源 revision 回退拒绝。
- GlobalAccount preference ETag/CAS、幂等 replay、event/outbox。
- deployment diagnostic 白名单与 scheduler lease fence。
- poison outbox 的持久失败状态。

迁移测试覆盖全新数据库和 legacy preference backfill；存在不可逆新事实时 down migration 明确拒绝。

前端：

    node --test tests/unit/prd145ConsoleExperience.test.mjs
    6 passed

    node --test dashboard + assistant + approvals + preferences + role-dashboard + prd145
    57 passed

    node scripts/lint.mjs
    console lint ok (472 files)

专项断言包括：

- 三套 client 均为 credentials include、cache no-store，不发送 Authorization。
- Platform/Workspace/GlobalAccount POST/PATCH 使用各自 CSRF Cookie。
- preference PATCH 使用 If-Match 和稳定幂等键，5xx 重试复用 intent。
- 所有公开 normalizer 拒绝 extra field。
- Platform/Workspace cursor 和受保护 intent 不进入 snapshot。
- 迟到响应、navigation epoch/revision 回退和 fingerprint 漂移不能覆盖新 owner。
- Workspace 活动 dispatcher 不调用旧 overview、assistant 或 approval loader。
- renderer 对新权限执行二次 fail-closed 交集。

## 测试过程修正

首次最终 pytest 命令的 --basetemp 父目录不存在，测试在 fixture 初始化前退出；创建可再生父目录后，
使用可追踪 session 重跑并取得 6/6 与 2/2 明确通过结果。该问题不涉及产品代码。

Ruff 首次发现 Workspace permission import 排序，机械修复后聚焦路径全部通过。

历史 UI 回归最初有 6 项仍断言旧组件标记。迁移为新组件断言后，其中一项发现真实问题：缺少新 Workspace
审批权限时仍可渲染空筛选器。实现已增加 renderer 权限交集；相同门禁覆盖总览、助手、系统状态和部署摘要。

## 两轮实施自审

### 第一轮：owner、并发与事实

- 三个 store 的 owner key 字段互不兼容，无法跨域接受 ticket。
- cursor、幂等键和问题正文只在 operation/DOM 内存中存在。
- navigation epoch、permission revision 与 fingerprint 只能单调推进。
- preference revision 回退、projection revision 回退均清理 busy 并进入 conflict。
- migration roundtrip 和 fact-loss guard 均有真实 SQLite 覆盖。

### 第二轮：活动调用、权限与体验

- performRefresh 到活动 dispatcher 之前不存在旧页面 loader 调用。
- dispatcher 五个新入口只加载当前页面 controller。
- hash/pagehide/logout/Session 失效会 abort 并清空受保护状态。
- Workspace 新页面 renderer 不单独信任导航字符串，必须通过权限交集。
- 英文语言迁移面板使用英文账号设置入口；旧租户级保存按钮不再出现。

## 敏感审计与批次门禁

- 对 PRD-145 migration、Control、Console、测试路径执行高置信 private key、云密钥、OpenAI/GitHub key 和
  JWT 模式的只报文件名扫描：零命中。
- 新 client/store/controller 执行 Authorization/Bearer 扫描：零命中。
- 新 client/store/controller/renderer 执行 localStorage、sessionStorage、IndexedDB、Cache API 和
  console 输出扫描：零命中。
- 测试只使用固定长度占位 Cookie、保留域名和临时 SQLite；正式证据不保存密码、TOTP、Cookie、Token、
  Provider Key、真实 Endpoint 或客户正文。
- .tmp 为可再生测试材料；正式证据只进入 .artifacts、docs/testing 和权威 Excel。
- 本提交必须使用显式路径暂存，不使用 git add .，不纳入未跟踪 PRD-101..105 文档和无关 outputs/。
- 数据库、跨域集成/FLOW、三浏览器三缩放、性能、长稳、安全与必要回归在 PRD-147 后统一执行 G7。

## 权威计划回写

- 权威 Excel 的 01_PRD总表!B57 已从“未启动”更新为“已完成”；PRD-146 保持“未启动”。
- 权威文件和 outputs/prd145-roadmap-closeout 副本均由 artifact-tool 重新导入验证：12 个 sheet，
  公式错误扫描 0 命中。
- B56:B58 的 styleId、字体、边框、换行与对齐保持一致；仅 B57 单元格值发生变化。
- 12 张工作表顶部和 PRD-145 聚焦范围均成功渲染；13 张 PNG 非空，采样颜色数为 7..24。
- 本地图像查看接口无响应，computer-use 状态读取连续两次超时，因此不声称完成了人工视觉审图；
  自动渲染、像素非空、尺寸与样式一致性检查通过。S7 G7 将再次执行真实浏览器和人工页面复核。

## 2026-09-07 Workspace Cookie 消费端补修

此前“完成”描述不证明真实 SaaS Cookie 登录能够进入主 Console。PRD-137 console-entry-r5
已记录桌面/移动两个入口失败；本次先修复本条五视图对旧 auth_context 的依赖，主入口启动仍归
PRD-146 继续修改。本次没有更改 Excel、服务端授权、DTO、迁移或 Platform store。

概览、系统状态、助手、审批、部署摘要现在只消费真实 shell 的 GlobalSession 公共句柄、撤销
代次/有效期、Workspace/role、权限与成员修订、授权指纹及导航 contract/epoch。拒绝旧内部
tenant/account/session owner，不把公开 Workspace ID 填进旧身份字段。四个客户端绑定
workspaceControlApiUrl，Cookie/CSRF 继续由既有客户端读取，并拒绝重定向及保留正文读取取消。

当前授权失败/冲突会清空事实、草稿、cursor 并要求重新验证。切换视图清空全部上一页私有状态，
旧 UI 事件不能再次设置草稿或审批翻页。失效后只渲染固定提示和刷新入口，刷新重新加载真实
会话与导航，不以失效 owner 重试业务 API。跨页通知先 leave，再执行主刷新。

在 console-web 使用 bundled Node 执行四个受影响文件：

```text
node --test --test-reporter=junit --test-reporter-destination=../.artifacts/g6-security-20260907/prd145-cookie-owner-r3-junit.xml tests/unit/prd145WorkspaceCookieOwner.test.mjs tests/unit/prd145ConsoleExperience.test.mjs tests/unit/prd146WorkspaceNavigation.test.mjs tests/unit/mainRefreshOwnership.test.mjs
node scripts/lint.mjs
node scripts/verify-production-surface.mjs
```

原始报告均位于 `.artifacts/g6-security-20260907/`。r1 为 72/72；r2 增加同步渲染期间重入
会话的退出回调防护，73/73；最终 r3 为 73/73，无失败、错误或跳过。覆盖五视图实际 controller
分派、19 类无效会话/导航组合、13 个 owner 维度变化、旧成功/401/403/409、首响应授权不匹配、
业务 epoch 回退、当前撤权清理、过期会话、旧视图事件、四个 Cookie 客户端正文取消，以及主
应用 origin/失效/恢复接线。既有两条 Workspace store 测试改用公开会话 owner，不放宽校验。

lint 为 766 files，production surface 为 259 modules / 273 shipped sources。没有运行或宣称
全套 QA、浏览器入口成功或 G6/G7/G8 通过。独立设计和两轮自审见 domain 设计的 Cookie 补充节。
独立审计 `prd-145-cookie-owner-sensitive-audit.json` 检查 9 个精确暂存源码/测试/文档、三份
报告的 UTF-8、凭据模式、SHA-256 和受保护基线。只暂存本条文件，不混入 PRD-137 测试夹具、
用户未跟踪文档、outputs 或机器证据。

## 2026-09-07 读取审计 WAL 冲突修复

真实 Cookie 浏览器 r7 的桌面 overview 为 503，移动端通过；该事实不足以独立确定数据库原因。
本次对八个生产 service 入口建立确定性双连接交错，未修复版八项全部失败，其异常上下文均为
`SQLITE_BUSY_SNAPSHOT`。因此确认这组读后写审计事务存在真实缺陷，不把它当成偶发夹具超时。
设计及两轮编码前自审见本条 domain 设计的“Console 读取与审计事务补充设计”。

Platform/Workspace overview、assistant、approvals、Identity preferences 和 Workspace deployment
summary 均改为读取授权之前 `BEGIN IMMEDIATE`。不拆分审计事务、不吞错误、不自动重试命令。
PostgreSQL adapter 已将该命令翻译为 BEGIN；本增量未运行 PostgreSQL 集成或性能门禁，不据此
声称跨数据库并发或 SLO 已验证。既有 preference PATCH、DTO、权限与 Cookie 合同不变。

原始报告均独立保留在 `.artifacts/g6-security-20260907/`：

| 报告 | 结果 | 实际覆盖 |
| --- | --- | --- |
| `prd145-read-audit-red-r1-junit.xml` | 8/8 失败 | 八个入口均确定性复现 SQLITE_BUSY_SNAPSHOT |
| `prd145-read-audit-green-r1-junit.xml` | 14/14 通过 | 八个并发回归及本模块六项现有 API/投影测试 |
| `prd145-read-audit-atomic-r1-junit.xml` | 2/2 通过 | Platform/Workspace 助手审计失败一起回滚，恢复后查询和审计各一条 |

运行使用独立 `--basetemp`，只从受保护原基线复制，迁移复制库至当前 0142，未写原库。
红版选取 `-k reads_serialize`，绿色执行本模块，新增原子性测试选取 `-k audit_failure`。
报告使用 `--tb=short`；绿色和原子性运行指定 `-o junit_family=xunit1`，避免原红版 xunit2
property 兼容警告。数据库错误仅记录固定类别，不输出 SQL 参数、密码、Cookie 或查询正文。

双连接测试使用真实 WAL、实际生产 service 函数、独立 scheduler-state 写事务及真实审计表。
第一连接取读快照后第二连接尝试提交；修复后第二连接被 writer 预留阻止，请求提交后可以继续，
请求审计恰好一条。身份对象沿用本模块替身以隔离事务行为，不作为真实认证/撤权的证明。
PRD-137 的真实注册、登录、生产 scheduler、Console DOM 与退出验证保留独立记录和提交。

实施自审一：确认八个入口都存在持久写入，修改覆盖同类事务，未扩大到无关服务；审计失败仍
映射既有 unavailable 并回滚，助手查询投影不会残留。实施自审二：独立连接真实冲突由红版
反证、绿版证明解除，竞争 writer 可恢复；原始失败报告没有覆盖，API fixture 0142 漂移已更正。

Ruff 与 `git diff --check` 通过。独立敏感审计
`prd-145-read-audit-transactions-sensitive-audit.json` 校验四个精确暂存源码/测试/文档、三份原始
报告的严格 UTF-8、高置信凭据模式、SHA-256、暂存字节和原基线哈希。模式扫描不等于独立安全
会签。该提交不含 PRD-146 主入口、PRD-137 夹具、用户未跟踪 PRD-101..105 或 outputs。
Excel 保持只读，G6/G7/G8 未通过，PRD-148 不启动。

## 2026-09-08 发布文案与维护节奏

十八类平台/Workspace 指标补齐实际含义的中文标签、记录状态和修订号，未知机器码使用
受控兜底。空间成员、探测、用量均保持真实记录数，不变成任务完成数、健康率或费用。
完整 reconciliation 检查保留，成功后 next_attempt_at 为五分钟；业务工作仍为一至两秒，
失败重试、CAS、租约期限与提交 guard 不变。

`.artifacts/g6-security-20260907/` 中 labels-red-r1 两项失败；labels-r2 9/9，cadence-r2
38/38（其中四项维护/业务节奏及完整检查，34 项调度回归），console-regression-r3 全前端
1815/1815。报告完整文件名分别为 `prd145-labels-red-r1.xml`、`prd145-labels-r2.xml`、
`prd145-cadence-r2.xml`、`prd145-console-regression-r3.xml`。
cadence-red-r1 为一失败三通过，原始失败展开了隔离测试随机 lease token，原件保留但不列入
可发布无明文证据；后续断言只展开布尔结果。该临时 token 不能用于本地业务库或外部服务。

Console lint 778 files、surface 262 modules / 276 sources、build 276 assets 通过。
两端发布摘要 `9dc5f75d3611ab71241d7cac98802f9b754f1a53d9dcf19e08146e2bb30512d6`。
构建首轮按预期拒绝旧摘要，更新实际构建字节摘要后通过，没有跳过发布校验。
真实本地管理员 Cookie 页面已查看桌面与 390x844 移动概览，成员 2、部署 6 和空记录标签
显示正确，无文案重叠，移动 drawer 默认关闭；临时 viewport 已 reset。该手工检查没有生成
独立浏览器 JUnit，不冒充完整浏览器业务验收。入口为 `index.html#workspace.home.overview`，
错误 hash 的既有回退提示在正常 reload 后清除。

实际完整后台仍有租约抖动，正在独立核对连接池的冷 schema 重解析；不能把本项绿色测试
当作整版可用发布。用户最新指示优先于上述旧门禁顺序：先交付后做 72 小时长稳，长稳未开始。
独立审计入口 `prd-145-release-overview-sensitive-audit.json`，不含用户未跟踪文档或 outputs。

## 2026-09-08 助手引用与现行导航

真实 Cookie 页面返回六条部署记录和零条探测记录，但两条引用均显示未识别事项，并指向旧
hash。来源名称改用 DTO 的 source_type，Workspace 通过既有导航 registry 映射 source ID；
deployment-summary 兼容到 config-summary，未映射来源只显示文字，Platform 路径不变。

`prd145-citation-red-r1.xml` 为 2 failed / 2 passed；`prd145-citation-r2.xml` 为 11/11，
覆盖八类引用、平台路径、未知引用与输入不变。全前端 `prd145-citation-regression-r3.xml`
1817/1817，lint 778 files、surface 262 modules / 276 sources、build 276 assets 通过。
实际资产摘要更新为 `2ccb9af17e2cb46dccd785f6d4160d62cede9bc13040ad0ea77980569c15fd0f`；
首次 build 按预期拒绝旧摘要后，两端同步再构建通过。真实点击与视觉检查仍独立记录，
不能用此次前端回归覆盖后台运行中后来出现的一次租约丢失。
