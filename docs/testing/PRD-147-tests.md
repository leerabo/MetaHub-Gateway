# PRD-147 测试记录

状态：实现与单 PRD 聚焦验证通过。PRD-147 独立提交后执行 S7/G7；本记录不以既有产品 QA 代替 G7。

## 专项范围

| 测试组 | 覆盖 | 结果 |
| --- | --- | --- |
| 0139 migration | fresh/current-head、0138 upgrade、roundtrip、重复 apply、fact-loss down guard、FK/完整性 | 通过 |
| Console migration API/worker | strict DTO、四路径 action、Cookie/CSRF/Origin/step-up/CAS/幂等、八域 owner 证据、批次、cutover/rollback、event/outbox | 通过 |
| Scheduler registry | 41 -> 36 mapping、第 37 platform-job、64 queue contract、逐 queue lease/fence/takeover、双 replica 公平并行、child commit guard | 通过 |
| Route/static contract | 46 个旧 route 在 router/OpenAPI 归零，permission retirement，production source graph 与后端 manifest 一致 | 通过 |
| Console | 全量 Node 单测、lint、production surface verifier、production asset-set digest/build | 通过 |
| 真实 Chromium | Platform 身份登录、TOTP、owner bootstrap、PlatformSession、迁移工作台、桌面/移动布局与刷新 | 通过 |
| 敏感审计 | 源码、正式 JSON、日志字段和提交候选文件的凭据/密钥/正文模式 | 零命中 |

最终聚焦命令：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  control-plane-api/tests/integration/test_prd147_console_migration.py `
  control-plane-api/tests/integration/test_prd147_db_migrations.py `
  control-plane-api/tests/integration/test_prd147_retired_routes.py `
  control-plane-api/tests/unit/test_prd147_scheduler_registry.py

.\.venv\Scripts\python.exe -m ruff check <PRD-147 changed Python paths>

Set-Location console-web
npm test
npm run lint
npm run build
```

聚焦 pytest `22/22` 通过，包含原有 API/migration/route/scheduler 合同与新增的 production manifest parity、
八域 owner evidence 和多 replica queue fairness。Console 全量 `1283/1283` 通过；lint 为
`console lint ok (761 files)`，production verifier 为 `258 modules / 272 shipped sources`，build 为 `272 assets`。

## Owner、导航与发布证据

- backfill cursor 不再把 0131..0138 的域级 cursor 全表 count/digest 复制给每个 Workspace。每个证据由冻结域级 cursor、
  `(workspace_id, tenant_id)` active binding 和固定域 owner 事实表共同组成；事实按 owner 过滤后做 versioned HMAC，非法 mapping
  不进入 target facts，因而阻断 reconciliation。
- owner 事实表覆盖 channel/customer-success/growth/brand/provider mapping，commercial-accounting、console-experience 和
  workspace-navigation 的复合 owner heads/events/outbox。Workspace Navigation 的测试 owner 实际观察到 binding、snapshot、
  items 与 head 等 70 条 owner 事实，不再用全域 cursor 行数冒充 Workspace 行数。
- mandatory check 固定为 `navigation_64_plus_4`。它要求唯一 active head/snapshot、`item_count=68`、实际 68 行、
  `source_navigation_id` 68 个唯一值、64 个 `excel` 和 4 个 `extra`；只存在 head 不再算通过。
- `production-surface-manifest.json` 同时冻结 production source graph 和 `production_asset_set_sha256`。build 对 272 个发布 asset
  逐文件计算摘要并对 canonical asset set 复算，漂移时直接失败；后端 `ConsoleContractRelease.frontend_manifest_digest`
  绑定包含该 asset-set digest 的同一 canonical manifest。

## Scheduler 与恢复

- 每副本仍只有一个 bounded coordinator；active child 改为逐 `(scheduler_code, queue_code, partition_code)` 独立获取、续租、
  停止和释放，不再原子占有一个 scheduler 的全部 queues。
- 每轮每 scheduler 最多新增一个 queue child，replica 使用稳定轮转起点；双 replica 测试证明四个 customer-success queue
  partition 全部有 leader、两副本均获得工作且集合互斥。
- child task 继承其单一 queue 的 outer commit guard；原业务 scheduler 的内部 lease 继续负责业务对象 claim。失租、过期、
  takeover 后旧 child 的 DB commit 失败关闭，不共享 token、generation 或 owner。
- event outbox 在 sink 返回后再次验证 outer fence；未知结果不 finalize，接管 replica 可继续派送且不重复形成终态事实。

## 真实 Chromium 证据

正式证据：`.artifacts/prd147/prd147-platform-chromium-scale-1.json`。

- SHA-256：`282085706E175767A2B078EEFC9200C2919DBD817BE4307BAEC8CFE2EBA643E4`。
- 真实 Identity 登录、TOTP enrollment/verification、Platform owner bootstrap 与 PlatformSession 创建通过。
- `platform.html#console-migration` 状态读取和刷新通过；`1280x720`、`390x844` 横向溢出与控件裁切均为 0。
- 旧 DOM、Console error、Page error 和非预期 Network failure 均为 0。
- 自动化凭据与一次性 bootstrap recovery secret 仅存在于进程内存，并纳入 runner 的精确脱敏和日志泄漏扫描。

## 测试过程修正

- 浏览器 runner 首轮用同步 HTTP helper 等待刷新响应，阻塞 Playwright event loop；改为 `waitForResponse` 后稳定完成。
- TOTP 首轮在时间片边界可能重复 counter；改为前一片、当前片、后一片三个严格递增 counter。
- 移动端 920px 固定表格曾撑开页面；表格改为受容器约束，390px 横向溢出归零。
- 实施自审发现后端 frontend manifest 落后于实际 Console JSON；补齐 HTML/style/replacement/retired/output 合同和 canonical parity
  单测，并增加 production asset-set 构建对账。
- 实施自审发现逐 Workspace cursor 实际复制全域 cursor、scheduler lease 粒度过粗、导航检查仍写 64+2；三项均修正并新增
  反向回归。0139 check-type 约束首次未同步新名称，导致 worker job 安全回队；同步 DDL 后三条失败路径全部复验通过。

## 两轮实施自审

第一轮聚焦 owner、权限、状态机和历史事实：复合 owner FK、域级/owner 级证据边界、三方 SoD、Support/Workspace/Partner
负权限、strict request、幂等/CAS、event/outbox/audit 和 rollback 单调性均复核；旧 route/permission/DOM 不可由 rollback 恢复。

第二轮聚焦 scheduler、发布面与恢复：37 个 cluster context、64 queue contract、逐 partition outer fence、内部业务 lease、
N/N-1 route deny、late commit、production source graph、asset set、移动端和浏览器错误面均复核。修正后的聚焦范围无未解决 P0/P1。

## 敏感审计与提交边界

- 正式 JSON 对 password、Cookie、CSRF、Authorization、Bearer、idempotency key、Provider key、client secret、private key、
  Prompt/Response 和一次性 recovery secret 的扫描为零命中。
- `.tmp` 只保存可删除、可重建的本地数据库与 pytest 目录；正式机器证据只放 `.artifacts`，两者都不提交。
- PRD-101..105 未跟踪文档、`outputs/`、本地凭据、SQLite、截图、trace 和 `.artifacts` 不进入 PRD-147 提交。
- G7 在 PRD-147 独立提交后另建批次报告和机器证据；缺失、跳过或失败项不能记为通过，也不能开始 PRD-148。

## 2026-09-07 文件域 Workspace 接线回归

本轮仅修复 file.read/file.write 从 Workspace Cookie 进入实际文件服务，以及两笔写事务内的真实
Workspace 会话重验。设计与两轮自审见本条文件域补充节；不修改全局认证 helper、迁移、默认权限、
前端或文件存储格式。缺口由 PRD-137 真实注册后的文件上传返回 401 复现，不使用旧 Token 绕过。

原始证据位于 `.artifacts/g6-security-20260907/`：

- `resource-process-r1-junit.xml` 首次混合运行共 5 项，4 通过、1 失败。本条两项中上传中真实降权
  通过，入口用例错误地把混合凭据预期设为 401；既有合同实际为 400，修正测试而非放宽产品。
  同文件另有 PRD-137 的 3 项真实 Guest 进程通过，不计入本条独立通过数。
- `prd147-workspace-files-r2-junit.xml`：本条 2/2，26.09 秒。缺失凭据、错误 CSRF/Origin 和混合
  凭据均在 multipart 读取前拒绝；同租户上传/读取正常，另一租户文件 ID 返回统一 404。
  另一用例在真实文件解析线程屏障期间经成员 HTTP 接口降权，终态提交拒绝、已发布密文删除，保留
  processing 失败现场且无 extract；同一旧 AuthContext 不能创建第二条 owner。
- 同用例重新真实切换后，Viewer 按既有 file.write 矩阵可上传，未按角色名称额外禁用能力。
- `prd147-file-legacy-r1-junit.xml`：旧 Bearer 主流程/双租户加密存储与事务撤权定向 2/2，
  135.50 秒，另 11 项未选。旧事务复核函数保留，不将本次定向扩张为文件模块全部回归。

独立提交只含 auth/dependencies、files/service、本条新集成测试及本条设计/测试记录。敏感审计
`prd-147-workspace-files-sensitive-audit.json` 核验上述精确暂存字节、UTF-8、凭据模式和受保护基线。
未提交测试数据库、正文、Cookie、报告或用户既有未跟踪文档。文件安全扫描及其他旧入口仍需所属
范围继续迁移验证，本轮不重新宣称 G7 全部通过，也不解除 G6/G8。

## 2026-09-07 基础资源客户端认证迁移

项目/应用、Environment、Provider/Key、Model、Virtual Key 和 File 六组客户端共 27 项操作
统一接入显式 Workspace/Legacy 请求传输，并贯穿 AbortSignal。Workspace Cookie 写入读取既有
专用 CSRF Cookie；Legacy 强制 omit Cookie。缺少 Token 不自动转为 Cookie，混合凭据、未知域
和调用方凭据 Header 在出网前拒绝。传输强制 no-store、redirect error，无自动重试；文件上传
仍为单 file multipart，响应 DTO 校验不放宽。未修改服务端授权、会话 DTO 或主入口启动逻辑。

开发期间只运行本次新增测试和六组客户端的既有单元测试：

```text
node --test --test-reporter=junit --test-reporter-destination=../.artifacts/g6-security-20260907/prd147-resource-auth-r4-junit.xml tests/unit/prd147ResourceAuthentication.test.mjs tests/unit/projects.test.mjs tests/unit/environments.test.mjs tests/unit/providers.test.mjs tests/unit/models.test.mjs tests/unit/virtualKeys.test.mjs tests/unit/files.test.mjs
node scripts/lint.mjs
node scripts/verify-production-surface.mjs
```

在 console-web 目录使用 bundled Node 执行，证据均保留在 `.artifacts/g6-security-20260907/`：

- `prd147-resource-auth-r1-junit.xml`：36 项，31 通过、5 失败，共用 Headers 小写序列化破坏旧测试
  约定。修正传输保留既有标准 Header 名称，不修改旧合同。
- `prd147-resource-auth-r2-junit.xml`：95 项，91 通过、4 失败。新增 failed FileInfo 夹具把
  match_count 写成 null，已有 DTO 要求整数 0..100；只修正夹具为 0。
- `prd147-resource-auth-r3-junit.xml`：95/95 通过。
- `prd147-resource-auth-body-red-r1-junit.xml`：两项，1 通过、1 失败。新增响应体取消回归在
  第一项资源操作复现旧容错吞掉 AbortError；另一个传输选项覆盖防护测试通过。
- `prd147-resource-auth-r4-junit.xml`：最终 97/97，无失败、错误或跳过。包含 27 操作乘两种
  凭据模式、全部操作混合凭据拒绝、全部操作正文读取取消、文件上传取消、CSRF 非法/重复/错域、
  缺失/非法 Token、未知域/Header 注入，以及请求选项不能覆盖 Cookie/cache/redirect 策略。
- lint：765 files；production surface：259 modules / 273 shipped sources。没有重复全套 QA。

两轮设计复核与实施取消补充见本条设计。独立敏感审计
`prd-147-resource-auth-sensitive-audit.json` 覆盖 10 个精确暂存源码/文档和上述五份原始测试报告，
核验 UTF-8、凭据模式、索引字节及受保护 SQLite 基线 SHA-256。凭据仅为测试内存值，不提交
报告、数据库或 `.tmp`。模式扫描不替代独立安全验收。

此提交交付资源客户端认证能力，不证明主 Console Cookie 启动或全部页面消费端已接通。
真实入口已有 console-entry-r5 两视口失败证据；PRD-146/147 消费端与 owner 迁移继续推进，
G6/G7/G8 未通过，PRD-148 未开始。

## 2026-09-08 基础资源写事务复核

本增量完成 Project/Application、Environment、Provider/Key、Model、Virtual Key 五组共 16 项
创建/更新/禁用的事务接线。两轮编码前自审见设计中的同名连续实施批次。共用
`ResourceWriteTransaction` 在业务读取前开启事务，恢复实际 Workspace 或 Legacy 会话与具体
write 权限，提交前再次复核。五项 owner 字段不允许由不同上下文拼接；未提交退出统一回滚和
关闭连接。已有审计提交 helper 仍从所属服务传入，保留故障注入与既有错误映射。

Virtual Key 随机冲突的重试重新开启事务、恢复会话、读取环境及项目绑定。没有增加业务请求
自动重试。Provider connection-test 的网络与写回暂未迁移，不在这 16 项中；前端消费端仍待
本批后续连续接线，不能把本次后端提交称为业务页或整批完成。

正式证据均在 `.artifacts/g6-security-20260907/`，不同轮次分别保留：

| 报告 | 结果 | 证明范围 |
| --- | --- | --- |
| `prd147-resource-write-red-r1-junit.xml` | 2 项夹具错误 | Environment 仅支持 dev/test/prod，夹具误用自定义代码，未进入产品反例 |
| `prd147-resource-write-red-r2-junit.xml` | 2 项夹具错误 | Provider 编码不支持连字符，修正夹具，不放宽产品校验 |
| `prd147-resource-write-red-r3-junit.xml` | 1 失败、1 通过 | 实际注册与 Cookie 登录后经 Identity logout 撤销；16 项旧上下文全部 unexpected_commit，当前会话对照通过 |
| `prd147-resource-write-green-r1-junit.xml` | 2/2 | 同一反例的 16 项全部 workspace_session.revoked；11 张业务/事件/审计表内容摘要不变；当前会话 16 项正常 |
| `prd147-resource-write-fault-r1-junit.xml` | 2 通过、1 失败 | 提交前模拟时钟过期与注入 commit 故障各覆盖全部 16 项，均回滚且关闭连接；冲突测试的手写撤销 SQL 未满足字段约束 |
| `prd147-resource-write-collision-r2-junit.xml` | 3/3 | 改用既有撤销函数；真实唯一键冲突回滚后，撤销会话拒绝、已禁用环境拒绝、正常重试成功 |
| `prd147-resource-write-http-r1-junit.xml` | 2/2 | 16 条实际 HTTP 正常写入、匿名 401、CSRF 403、混合凭据 400；五字段逐项混接两份真实会话均拒绝 |
| `prd147-resource-write-unit-r1-junit.xml` | 7/7 | 真实 SQLite 的 begin/body/reauth/commit/成功退出、未知权限、无事务提交生命周期 |
| `prd147-resource-write-concurrency-r1-junit.xml` | 1/1 | 实际写线程在读取资源前已持有 SQLite 写事务；独立连接撤销写入被锁阻止；完成后真实 logout 生效，旧上下文拒绝 |
| `prd147-resource-write-legacy-r1-junit.xml` | 8/8 | 五组资源原有主流程、Key 限制、Application 搬迁逐 Key 审计与审计失败整体回滚；1098.412 秒，无失败/错误/跳过 |

新集成文件的模块级迁移夹具复用既有 `migrated_baseline`，只复制受保护基线并对副本 apply 到
当前迁移；账号、组织、成员、会话和初始资源经实际 HTTP 创建，不覆写认证 dependency。
事务期过期只替换 Workspace 认证时钟，提交失败只替换 committer；冲突用内存 Key 生成器造成
实际唯一约束失败，再用既有撤销函数或测试库环境状态变更插入重试间隙。生命周期单测替换
认证方法，仅证明连接/事务控制流，不当作真实身份授权证据。
双连接用例只在业务资源读取处设置线程屏障，不替换认证；竞争连接实际执行既有会话撤销
SQL 并验证锁冲突，释放屏障后经真实 HTTP logout 再次验证旧上下文拒绝。未据此声明 PostgreSQL
或跨进程/跨主机并发撤权已验证。

聚焦运行命令（仓库根目录；每次使用不同 basetemp 和报告文件，旧报告不覆盖）：

```powershell
$env:METAHUB_TEST_MIGRATED_BASELINE = 'C:\MetaHub\.tmp\pytest-g6-security-sqlite-r2\prd134-api-base0\base.sqlite3'
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_prd147_workspace_resource_writes.py -q --tb=short --show-capture=no --basetemp=.tmp/pytest-prd147-resource-write-green-r1 --junitxml=.artifacts/g6-security-20260907/prd147-resource-write-green-r1-junit.xml
.venv\Scripts\python.exe -m pytest control-plane-api/tests/unit/test_prd147_resource_transaction_lifecycle.py -q --tb=short --show-capture=no --basetemp=.tmp/pytest-prd147-resource-write-unit-r1 --junitxml=.artifacts/g6-security-20260907/prd147-resource-write-unit-r1-junit.xml
```

green-r1 运行时文件只有两项；后续 fault-r1、collision-r2、http-r1 分别使用 `-k 'failures or
collision'`、`-k collision`、`-k 'http_resource or owner_cannot'`。collision-r2 使用 xunit1；
其余记录属性沿用 xunit2 并保留 pytest 警告，不声明独立 XSD 验证。
concurrency-r1 单独选择 `-k serializes` 并使用 xunit1；新增三份 Python 文件完整 ruff check
通过，六份受影响生产源码的 F 类静态检查通过，`git diff --check` 通过。没有格式化旧服务全文件。

只对五组 Legacy 资源选择相邻主流程、Virtual Key 限制与 Application 搬迁审计/回滚冒烟，
不执行整套数据库、浏览器、性能或安全 QA。八项最终全部通过；既有夹具每例从空库迁移，
单例 132..142 秒，此耗时不是业务接口延迟或性能验收结论。期间没有重启同一测试运行。

```powershell
$legacyFiles = @(
  'control-plane-api/tests/integration/test_environments.py'
  'control-plane-api/tests/integration/test_providers.py'
  'control-plane-api/tests/integration/test_models.py'
  'control-plane-api/tests/integration/test_virtual_keys.py'
  'control-plane-api/tests/integration/test_projects_applications.py'
)
$selection = 'environment_alias_resolution_and_resource_binding or provider_create_list_test_and_disable_flow or model_catalog_create_list_update_and_disable_flow or virtual_key_create_call_disable_flow or virtual_key_limit_settings_flow_and_permissions or project_application_key_usage_log_and_budget_flow or application_project_move'
.venv\Scripts\python.exe -m pytest @legacyFiles -q --tb=short --show-capture=no -k $selection --basetemp=.tmp/pytest-prd147-resource-write-legacy-r1 --junitxml=.artifacts/g6-security-20260907/prd147-resource-write-legacy-r1-junit.xml
```

敏感审计使用独立 `prd-147-resource-write-transactions-sensitive-audit.json`，验证十项精确暂存
源码/测试/文档、原始报告、严格 UTF-8、凭据模式及受保护基线摘要。测试凭据和 Key 仅驻留
进程内存；不提交数据库、`.artifacts`、`.tmp`、用户 PRD-101..105 文档或 outputs。模式扫描
不是独立安全会签。PostgreSQL 并发撤权锁定、Provider 网络探测、前端 owner 和统一批次 QA
仍未完成，G6/G7/G8 不变，PRD-148 未开始。

## 2026-09-08 Provider 连通性授权与条件写回

继续基础资源与业务页 owner 批次，补齐前次明确排除的 `POST /providers/{id}/test`。
独立两轮编码前自审见设计同名补充，不另开批次 QA。第一笔只读短事务恢复实际会话、核对
provider.write 和 Shared spec，复制配置及健康水位后关闭连接；原有无 Key 探测在事务外执行。
第二笔事务再次鉴权，以配置 generation、健康时间、排期及 lease token/until 原子条件写回，
提交前再次复核，成功提交后才写 health_checked 日志。路由通过线程池执行同步服务，因此
网络等待期间同进程的 logout 和 Membership 降权仍能完成。没有更改全局鉴权、权限默认值、
SSRF/DNS 策略或健康任务的凭据读取合同，也没有新 migration。

正式证据目录：`.artifacts/g6-security-20260907/`。所有历史轮次保留，不拼接为一轮成功：

| 报告 | 结果 | 实际证明范围 |
| --- | --- | --- |
| `prd147-provider-probe-red-r1-junit.xml` | 6 项产品反例失败，63.755 秒 | 实际 logout 后仍探测并提交；Endpoint 修改、改回原值、Provider 禁用和新增 Key 后仍提交旧结果；同秒新结果被覆盖 |
| `prd147-provider-probe-green-r1-junit.xml` | 6/6，66.428 秒 | 对应反例均拒绝，变更后的四张业务/健康日志/审计表摘要不变；连接关闭 |
| `prd147-provider-probe-boundary-r1-junit.xml` | 5 通过、3 失败，87.684 秒 | 提交期过期/故障和三种真实 worker lease 场景通过；三个 HTTP 用例在正确响应后，测试从事件循环跨线程检查 SQLite 连接，触发线程限制，不能据此宣称连接关闭已验证 |
| `prd147-provider-probe-final-r1-junit.xml` | 同轮 15/15，292.318 秒；无失败/错误/跳过 | 修正连接检查线程后，14 项新用例及 1 项原有 Legacy HTTP 主流程全部通过 |

最终轮覆盖：实际 Identity logout 的出网前拒绝；真实 HTTP 网络等待期间 logout/成员降为
viewer 的终态 401；Cookie 正常、匿名 401、CSRF/Origin 403、混合凭据 400、异空间 404；
Endpoint/ABA/启停/Key 变更；同秒内竞争结果；已有租约出网前拒绝、在途领取和 worker 完成后
旧结果拒绝；提交前过期与注入 commit 故障的回滚、连接关闭和不发成功日志。
配置和 Key 变化通过原服务执行，worker 竞争使用真实 `_claim_provider`/`_complete_probe`。
配置代次不变但健康水位变化也必须拒绝，NULL 条件参数显式 CAST 为 TEXT，避免 PostgreSQL
空参数无法推断类型；尚未把这一 SQL 兼容写法当作 PostgreSQL 运行证据。

新增测试使用实际注册、Workspace 与 Cookie 夹具，没有替换认证 dependency。网络函数只在
明确的边界暂停或返回受控 Attempt，不据此证明真实 DNS/上游行为；原有 Legacy 冒烟使用真实
本地 HTTP Server，并沿用它已有的测试专用 URL 策略替换，验证正常、404 fallback、401 摘要
和禁用流程，不是 SSRF 验收。其空库迁移夹具耗时 141.262 秒，不是 API 性能结论。
连接观察包装器转发原始 SQL 和认证，在实际所属线程执行 close 并验证 closed 异常，然后把
关闭事实带回事件循环；不关闭 SQLite 的线程安全检查，不把线程错误当作已关闭。

```powershell
$env:METAHUB_TEST_MIGRATED_BASELINE = 'C:\MetaHub\.tmp\pytest-g6-security-sqlite-r2\prd134-api-base0\base.sqlite3'
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_prd147_provider_probe_transactions.py control-plane-api/tests/integration/test_providers.py -q --tb=short --show-capture=no -o junit_family=xunit1 -k 'probe or provider_create_list_test_and_disable_flow' --basetemp=.tmp/pytest-prd147-provider-probe-final-r1 --junitxml=.artifacts/g6-security-20260907/prd147-provider-probe-final-r1-junit.xml
```

新测试完整 ruff check、两份受影响生产源码 F 类检查与 `git diff --check` 通过。独立敏感审计
报告 `prd-147-provider-probe-transactions-sensitive-audit.json` 检查五项精确暂存文件、四份原始
JUnit、严格 UTF-8、高置信凭据模式及受保护公共基线摘要；模式扫描不等于独立安全会签。
不提交测试数据库、`.tmp`、`.artifacts`、用户 PRD-101..105 文档或 outputs。Excel 仅按表格
技能只读核对，没有修改完成状态；前端 Cookie/owner 消费端、PostgreSQL 并发撤权锁定和批次
完整 QA 仍未完成，G6/G7/G8 不变，PRD-148 未开始，也未重新等待 72 小时。

## 2026-09-08 基础资源页面 Workspace owner 接线

接续同一批次，设计和两轮自审见 PRD-147「基础资源页面消费端补充自审」。本增量覆盖
Project/Application、Environment、Provider/Key（含 Catalog）、Model、Virtual Key、File 的
现有主页面入口。五组普通列表共用 read lane，File 保留私有 cursor/list/upload ticket；
17 个表单/按钮 handler 的 18 个请求分支显式使用 Workspace Cookie。File 上传另有双层
ticket，Catalog 四项客户端复用已有认证传输并保留幂等键。没有修改服务端认证守卫或授权默认值。

页面 owner 使用实际 public session 构造器和后端导航投影，不填充 Legacy tenant/user/session。
同一完整身份和权限集合的刷新复用 session/page 引用；读取代次仍前进。后台先复核授权，
同 owner 有草稿/未决写入时不替换 DOM。hash、侧栏导航、pagehide、换空间和撤权均清理；
新导航裁剪后先清旧资源 DOM，再读新页面。旧节点在 FormData/File 读取前拒绝，迟到成功、
401/403、复制回调、刷新和 finally 不得更新新 owner。一次性 Key 不再写入按钮 dataset，
隐藏操作立即清可见明文，不等待可能因未决操作而延后的刷新。

正式证据仍在 `.artifacts/g6-security-20260907/`，以下各轮独立保留：

| 报告 | 结果 | 说明 |
| --- | --- | --- |
| `prd147-resource-pages-r1-junit.xml` | 324 项，316 通过、8 失败 | 1 项真实缺陷：同 owner enter 替换 page 引用导致在途 Key 结果丢失；7 项旧夹具/请求头断言未适配 |
| `prd147-resource-pages-r2-junit.xml` | 324/324 | 保留同 owner page 引用，更新实际依赖夹具及大小写不敏感的 Header 断言 |
| `prd147-resource-pages-r3-junit.xml` | 340 项，143 通过、197 失败 | 扩展到完整 dispatcher 时，测试提取器把多行参数的 `})` 当作函数结尾，VM 语法错误；不是 197 项产品缺陷 |
| `prd147-resource-pages-r4-junit.xml` | 340/340 | 提取完整生产函数后，六页分发、复制回调、文件旧 DOM 与实际后台刷新通过 |
| `prd147-resource-pages-final-r1-junit.xml` | 359/359，无错误或跳过 | 增补即时隐藏、目录请求标识生成异常收口、旧筛选/tab 和新导航读前清屏；包含相邻迁移与 Virtual Key 单测 |
| `prd147-resource-pages-manifest-r1-junit.xml` | 1/1，无错误或跳过 | Python 后端 manifest 与前端发布 manifest 的 canonical 内容、摘要一致 |
| `prd147-resource-pages-navigation-r1-junit.xml` | 55/55，无错误或跳过 | 相邻 main refresh ownership、PRD-145 Console experience 和 PRD-146 Workspace navigation 单测 |

最终 Node 报告 SHA-256：`699084ab5e4cd2ad89c871e51c25183611cd652b6ebd39afdcd36896f98cf1b0`。
manifest 单测报告 SHA-256：`5826d2971a9633451c3914fb3bf88b80c7d12f8cccf3d2aeca90a8d82b54733e`。

测试执行生产 main 函数、完整 dispatcher、真实绑定 handler 和 File DOM binder。网络、DOM
及 public shell 输入为受控单元夹具；这些证据验证前端调用/回调边界，不能冒充浏览器或真实
后端鉴权结果。传输测试实际执行七组客户端，检查 Cookie/CSRF、Legacy omit、混合凭据拒绝、
重定向/no-store、幂等键和正文读取取消。未再次运行已完成的长耗时 Legacy API 整组回归。

```powershell
$node = 'C:\Users\OseasyVM\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$tests = @(
  'console-web/tests/unit/prd147ResourcePages.test.mjs'
  'console-web/tests/unit/prd147ResourceAuthentication.test.mjs'
  'console-web/tests/unit/files.test.mjs'
  'console-web/tests/unit/prd119ProviderCatalog.test.mjs'
  'console-web/tests/unit/prd146ConsoleEntry.test.mjs'
  'console-web/tests/unit/prd147ConsoleMigration.test.mjs'
  'console-web/tests/unit/virtualKeys.test.mjs'
)
& $node --test --test-reporter=dot --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination=.artifacts/g6-security-20260907/prd147-resource-pages-final-r1-junit.xml @tests
.venv\Scripts\python.exe -m pytest control-plane-api/tests/unit/test_prd147_scheduler_registry.py -q --tb=short --show-capture=no -k frontend_release_digest --junitxml=.artifacts/g6-security-20260907/prd147-resource-pages-manifest-r1-junit.xml
```

静态检查：`console-web/scripts/lint.mjs` 最终通过（773 文件），production surface verifier 通过
（261 模块、275 发布源码），`git diff --check` 通过。首次 production build 明确拒绝旧摘要；
按实际发布字节把前后端 manifest 同步至
`59e37659083c632b5590cce7fd86c40598ec001d927346a39c4bf3edb45f509f`，随后 build 通过
（275 assets）。没有降低 verifier、略过摘要检查或修改历史迁移行。现有 5173 开发服务返回
新 owner 模块 HTTP 200；这不是浏览器流程或视觉验收。

追加导航回归命令沿用上述 Node reporter 参数，目标为
`mainRefreshOwnership.test.mjs`、`prd146WorkspaceNavigation.test.mjs` 和
`prd145ConsoleExperience.test.mjs`，输出独立 navigation-r1 报告，不与 359 项轮次拼接。

最终独立审计报告为 `prd-147-resource-page-owners-sensitive-audit-r2.json`，检查本次 15 项精确暂存
源码/测试/manifest/文档、上述 7 份原始报告、严格 UTF-8、凭据模式和共享基线摘要。首版审计
仍保留，r2 增补导航回归及更新后的测试记录。审计不
替代独立安全会签。保留用户 PRD-101..105 文档和 outputs，不提交数据库、dist 或临时制品。

下一步仍是同批次未闭环项目：Catalog 后端 owner fallback/事务权限复核、其余业务域页面的
真实 Workspace 消费端、PostgreSQL 并发撤权及统一批次 QA。六组基础页面的单元接线完成
不等于全域 SaaS 迁移完成；G6/G7/G8 尚未证明通过，Excel 未改状态，PRD-148 未开始，未重启
72 小时等待。

## 2026-09-08 Catalog Workspace 事务与健康共同提交

本项接续基础资源迁移批次，设计和两轮自审见 PRD-147 对应补充。四个 Workspace Catalog
API 在查询之前和提交之前恢复真实会话；Legacy read 重新校验 Catalog 扩展权限目录。
Workspace owner 精确绑定 session/membership/user/tenant，失效 wss_ 不再回退旧成员映射。
预检将租约领取与网络阶段分开，健康日志、Key 状态、Provider 聚合、Catalog run/event/outbox
在第二笔授权事务中共同提交；失败仅清本次 token/generation 的租约，不推进排期或写业务结果。

已执行的历史轮次位于 `.artifacts/g6-security-20260907/`，不覆盖失败原件：

| 报告 | 结果 | 原因与处置 |
| --- | --- | --- |
| `prd147-catalog-r1-junit.xml` | 2 条记录，1 失败、1 teardown 错误，13.422 秒 | 关闭复用开关仍未释放请求级连接；真实关闭断言失败，测试 runner 收尾随后失败。预检改为不使用请求级 scope |
| `prd147-catalog-r2-junit.xml` | 30 项，25 通过、5 失败，222.417 秒 | 4 个撤销注入未填 revoked_at/reason；1 个实例篡改被不可变 trigger 提前拒绝。改用生产撤销函数；损坏实例测试明确先移除该测试库的 trigger |
| `prd147-catalog-r3-junit.xml` | 46 项，42 通过、4 失败，481.500 秒 | 3 项旧角色夹具只改 users 未同步 Membership，登录已失败；1 项实际目录 p95 为 840.316ms。修正测试角色关系，无网络接口恢复暖连接池，不削弱权限或放宽 300ms 断言 |
| `prd147-catalog-r4-junit.xml` | 50 项，47 通过、3 失败，478.935 秒；无错误/跳过 | Workspace 事务、网络竞争、真实 Key 加解密、共同提交及 300ms 目录查询断言通过。三个 Legacy 夹具试图直接降低唯一 Owner，被 Membership 的转移保护正确拒绝 |
| `prd147-catalog-legacy-r5-junit.xml` | 定向 3/3，153.192 秒；无失败/错误/跳过 | 保留原 Owner，经真实 `/users` 创建 Developer/Viewer 并真实密码登录，验证目录读权限与两种扩展目录损坏拒绝；核对实际 Membership/Legacy 角色映射。时间含空库迁移，不是 API 耗时 |

r5 只改旧测试夹具和记录，不再重复执行 r4 已通过的 47 项，也不把两轮拼接成同轮 50/50。
r4 SHA-256：`e9db8b984c0bb46665d5df96fec9831e92a861f234e38abea8b32362ad1ec86d`。
r5 SHA-256：`6761f7b3d4a00edaea101bd17f1b797e6159dd856be3b3e3f6d501c9c8e37da6`。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:METAHUB_TEST_MIGRATED_BASELINE = 'C:\MetaHub\.tmp\pytest-g6-security-sqlite-r2\prd134-api-base0\base.sqlite3'
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_prd147_catalog_transactions.py control-plane-api/tests/integration/test_prd119_provider_catalog.py control-plane-api/tests/unit/test_prd147_resource_transaction_lifecycle.py control-plane-api/tests/integration/test_prd147_provider_probe_transactions.py::test_probe_respects_health_worker_lease_and_watermark control-plane-api/tests/integration/test_provider_health.py::test_provider_probe_ssrf_policy_blocks_local_and_accepts_public_dns control-plane-api/tests/integration/test_provider_health.py::test_provider_health_pins_validated_dns_address -q --tb=short --show-capture=no --basetemp=.tmp/pytest-prd147-catalog-r4 --junitxml=.artifacts/g6-security-20260907/prd147-catalog-r4-junit.xml
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_prd119_provider_catalog.py -q --tb=short --show-capture=no -k legacy_bearer --basetemp=.tmp/pytest-prd147-catalog-legacy-r5 --junitxml=.artifacts/g6-security-20260907/prd147-catalog-legacy-r5-junit.xml
```

验证覆盖真实 Workspace 注册/验证/登录/空间创建 Cookie，匿名、CSRF、Origin、混合凭据、
跨空间、Viewer、logout/降权、提交前撤销；实例/Endpoint ABA/Key/generation/lease/期限变化；
幂等重放与同键异体冲突；健康、outbox、commit、提交鉴权、网络异常和最终过期的原子回滚。
网络前实际连接关闭，失败只清本操作租约。随机内存 Key 通过真实加解密，受控 HTTP 返回分别
验证成功、认证拒绝与限流的健康/凭据状态、Catalog run/event/outbox 同时提交。

所有 Workspace 授权场景不替换认证 dependency。Platform 发布测试仅在原签名发布合同范围
保留合成 PlatformContext，不能作为平台真实认证证据。网络暂停、时钟和故障注入仅证明明确
边界；SSRF/DNS 相邻两项验证策略和地址固定，不证明真实 DNS 延迟、整个 KeyPool 或上游 SLO。

本次五份 Python 源码/测试完整 Ruff 检查通过；健康服务相对 HEAD 仍是原有 29 项 E501，无新增，
忽略 E501 后其余规则通过。compileall 与 `git diff --check` 通过。独立敏感审计报告
`prd-147-catalog-transactions-sensitive-audit.json` 检查八项精确暂存文件、五份原始报告、严格
UTF-8、高置信凭据模式及公共基线 SHA-256。该扫描不是独立安全会签。

旧 r1/r2/r3 临时 SQLite 已按用户清理请求移入回收站，所有正式报告和 r4 失败现场保留。
不提交 `.tmp`、`.artifacts`、用户 PRD-101..105 文档或 outputs。Excel 按表格技能只读核对，
不改完成状态；PostgreSQL 并发撤权、其余业务页消费端、全 KeyPool/DNS 时限与批次完整 QA
仍待完成。G6/G7/G8 尚未证明通过，PRD-148 未开始，没有重启 72 小时等待。

## 2026-09-08 Provider 观测页面与手工健康事务

继续基础资源迁移批次，设计与两轮自审见 PRD-147「Provider 状态、健康与手工探测消费端
连续批次」。实际反例证明 `/status/providers` 不接受真实 Workspace Cookie，且用户登出后
旧 AuthContext 仍可手工探测并提交。修复状态路由为显式 Workspace/Legacy 双域守卫，保留
provider.health.read/request_log.read 双权限、固定筛选与分页白名单。健康与状态读取在同一
快照内重验会话，返回前再次复核；状态服务两次均检查第二项调用日志权限，GET 不提交业务写入。

手工 `/providers/{id}/probe` 复用 ResourceWriteTransaction，两笔短事务之间不留实际数据库
连接。第一笔领取租约，第二笔重新鉴权后共同提交健康日志、Key 状态和 Provider 聚合，最终
commit 前复核原租约期限。失败只清自己的 token/generation，不覆盖竞争租约。Catalog 与该
入口共用 perform/complete/commit/release 健康函数；周期任务继续使用可信 tenant 的内部入口，
不继承浏览器 Session。补齐内部任务读取结果后的连接关闭，Local Endpoint 仍走专用 API。

前端两个观测客户端、三个操作复用显式认证传输；Workspace 不发 Bearer，Legacy 显式 omit
Cookie，写入只读专用 CSRF Cookie。状态页加入公共资源 owner/read lane/DOM binding：双读
权限、筛选、分页、probe、旧 DOM、换空间、迟到成功/401/403/503 与 finally 统一淘汰；当前
503 保留筛选和分页。敏感 query 字段被固定白名单过滤，响应体读取取消原样向上传递。

正式证据位于 `.artifacts/g6-security-20260907/`，各轮独立保留，不合成同轮结果：

| 报告 | 结果 | 证明范围 |
| --- | --- | --- |
| `prd147-observation-red-r1-junit.xml` | 2 项产品反例失败，21.355 秒 | 真实 Cookie 状态 401；登出后旧上下文探测未被拒绝 |
| `prd147-observation-r1-junit.xml` | 27/27，193.425 秒，无错误/跳过 | 16 项新观测用例、4 项相邻 Catalog 健康共同提交用例、7 项共享事务生命周期单测 |
| `prd147-observation-ui-r1-junit.xml` | 334/334，无失败/错误/跳过 | 实际主页面函数、事件绑定、共享 owner/传输与原有健康/状态渲染及验收脚本单测 |
| `prd147-observation-ui-r2-junit.xml` | 336/336，无失败/错误/跳过 | 增加当前 503 筛选保留、观测 query 白名单及成功 state 不被响应正文覆盖 |
| `prd147-observation-legacy-r1-junit.xml` | 2/2，262.907 秒，无失败/错误/跳过 | 原有真实 Legacy 登录的状态指标/探测分组/维护备注及 HTTP 失败恢复主流程；耗时包括两个空库迁移 |
| `prd147-observation-machine-r1-junit.xml` | 1/1，14.224 秒，无失败/错误/跳过 | 实际用户登出后可信 scheduled 入口仍可领取/提交；未到期拒绝重复网络，三阶段连接关闭 |
| `prd147-observation-manifest-r1-junit.xml` | 1/1，无失败/错误/跳过 | 前后端发布 manifest canonical 内容与摘要一致 |

API 专项报告 SHA-256：`eab8073db5d9964355633a8828f6d73b49b7d2921c6417f4035e40b86fbee9c6`。
前端 r2 报告 SHA-256：`d5911c62398b10e32ab00ccc321dd3feb7a74cde6ff4d4552eb8513fdd12c1cb`。
Legacy 报告 SHA-256：`60219441a104970f185cae7f13641e0220c155a139f0c6057670047d2e08cfc5`。

```powershell
$env:PYTHONIOENCODING = 'utf-8'
$env:METAHUB_TEST_MIGRATED_BASELINE = 'C:\MetaHub\.tmp\pytest-g6-security-sqlite-r2\prd134-api-base0\base.sqlite3'
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_prd147_provider_observation.py control-plane-api/tests/integration/test_prd147_catalog_transactions.py::test_catalog_real_cookie_scope_and_preflight_replay control-plane-api/tests/integration/test_prd147_catalog_transactions.py::test_catalog_keypool_and_health_facts_commit_together control-plane-api/tests/unit/test_prd147_resource_transaction_lifecycle.py -q --tb=short --show-capture=no --basetemp=.tmp/pytest-prd147-observation-r1 --junitxml=.artifacts/g6-security-20260907/prd147-observation-r1-junit.xml
.venv\Scripts\python.exe -m pytest control-plane-api/tests/integration/test_provider_status.py::test_provider_status_metrics_probe_grouping_and_maintenance_note control-plane-api/tests/integration/test_provider_health.py::test_provider_probe_failure_recovery_and_logs -q --tb=short --show-capture=no --basetemp=.tmp/pytest-prd147-observation-legacy-r1 --junitxml=.artifacts/g6-security-20260907/prd147-observation-legacy-r1-junit.xml
$node = 'C:\Users\OseasyVM\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
& $node --test --test-reporter=dot --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination=.artifacts/g6-security-20260907/prd147-observation-ui-r2-junit.xml console-web/tests/unit/prd147ResourcePages.test.mjs console-web/tests/unit/prd147ResourceAuthentication.test.mjs console-web/tests/unit/providerHealth.test.mjs console-web/tests/unit/providerStatus.test.mjs console-web/tests/unit/prd061BrowserAcceptance.test.mjs
```

machine-r1 是补充后的单用例运行，目标为新模块内
`test_scheduled_probe_keeps_machine_authority_and_closes_connections`，独立 basetemp/report；因此
现在重跑整个新模块会多出这一项，不能把原 r1 的 27 项改写为 28 项。manifest 命令仍为
`test_prd147_scheduler_registry.py -k frontend_release_digest`，独立报告。

新测试及六份相关 Python 文件完整 Ruff 检查通过。健康/状态服务分别保留原有 29/7 项 E501，
没有新增告警，状态服务原有 I001 已随 import 修改修正；忽略 E501 后通过。compileall、
console lint（773 文件）、production surface（261 模块、275 发布源码）和 diff-check 通过。
构建先正确拒绝旧源码摘要；同步两端 manifest 至
`e25b33cf35063779ee7551f55fe31077031bbe8b88934958889ba3db9a81ce70` 后 build 通过（275 assets）。
现有 5173 开发服务返回新客户端 HTTP 200；这不是浏览器流程或视觉验收。

独立敏感审计 `prd-147-provider-observation-sensitive-audit.json` 核对本次 17 项精确暂存文件、
七份原始报告、UTF-8、凭据模式与共享基线摘要。模式扫描不等于安全会签。真实 Workspace
认证没有替换；网络/时钟/故障注入仅证明事务边界。Legacy HTTP 主流程使用其既有受控本地
URL 替换，不是 SSRF 验收；前端 VM、DOM 与验收脚本测试也不是实际浏览器证据。

Workspace 页不再尝试旧 `/console/events`，但新域 `status.viewed` 唯一访问事实入口尚未实现，
已在设计中明确登记，不能以功能接线代替这项完成证据。其余业务消费端、PostgreSQL 并发
撤权、全 KeyPool/DNS 时间上限和统一批次 QA 仍待完成。Excel、用户 PRD-101..105 与 outputs
未改；不提交临时数据库、正式运行制品或 dist。G6/G7/G8 尚未证明通过，PRD-148 未开始。

## 2026-09-08 Workspace 页面访问事件实施记录（未提交、未完成验收）

本轮按 PRD-147 设计补充章节完成两轮自审后，增加统一页面访问 DTO/API、真实会话摘要、
原 console_page_events 的可空会话外键及请求摘要，以及 SQLite/PostgreSQL 0143 前向/回滚
迁移。普通页面、首页、Provider 状态和迁移指南共用前端访问生命周期；Workspace 不调用
退休的 /console/events。旧十二列、rowid 和 payload 不重写；新事件使用现有真实成员对应
的 User 与 WorkspaceSession，不制造 Legacy Session 或第二张事件事实表。

### 独立测试轮次

- prd147-page-visits-r1-junit.xml：1 passed / 1 failed，30.88s；测试把缺 Origin 的匿名
  POST 预期为 401，既有导航边界实际首先返回 403。原报告保留。
- prd147-page-visits-r2-junit.xml：4 passed / 1 failed，69.91s；严格 DTO 错误的现有
  workspace_session.request_invalid 为 422，测试原先预期 400。原报告保留。
- prd147-page-visits-r3-junit.xml：4 passed / 1 failed，71.05s；既有
  identity.revision_conflict 为 412，测试原先预期 409。原报告保留。
- prd147-page-visits-contract-r4-junit.xml：上述严格传输/归属/退休路由单项通过，20.15s。
- prd147-page-visits-final-r5-junit.xml：9 passed / 2 failed，128.04s。七项本组测试
  覆盖原表唯一事实、同键异体、严格传输与跨空间、换 Session 后重放、提交前拒绝回滚、真实
  成员降权、真实会话最终过期、历史行/rowid 保真及数据库 owner/不可变 guard；另有 File
  真实 Cookie 冒烟和 PRD-145 投影用例通过。两项 PRD-137 归档回滚相邻测试失败，发现新的
  0143 down SQL 缺少 expected-version 执行器要求的固定 PRAGMA 前后缀。
- 已补齐 0143 down 的受信事务格式，保留 expected_version 和新事实阻止回滚的断言。
  此修正尚未复验，不能把 r5 描述为全部通过。
- prd147-page-visits-ui-r1/r2/r3-junit.xml：三份独立前端命令均退出 0。最后一轮包含本组、
  PRD-094/146 导航、PRD-147 资源页，以及 Provider 状态/迁移指南/旧竞品页面的受影响单元。
  新测试覆盖真实 public owner 形状、生产渲染入口、不同页面唯一访问、微任务前离页取消、
  迟到 401/403/409/412/503 隔离、同 owner 不重试、重新进入和 Cookie/CSRF 正文 abort。
  这是单元/VM 证据，不是真实浏览器验收。
- Console lint 通过（776 files），生产引用面检查通过（262 modules / 276 shipped sources）。
  build 实际计算到新摘要 253f283d00c44354bbecf0975db6995ed70f5c285bf01f01a5b586e2bb2a1203，
  随后已同步两个 manifest；同步后的完整 build 与 manifest 测试尚未重跑。
- 受影响 Workspace navigation 和测试源 Ruff 检查通过；后续仅修正 SQL 前后缀、同步 manifest。

### PostgreSQL 未完成项

隔离实例 r1（281626fe2bc548bfbf2003434672f643）完成原生迁移和 seed 后，三项测试均在实际
HTTP fixture bootstrap 失败，3 errors / 106.90s，尚未执行页面事件断言。r2
（934fb705892a4b648a5f6494dc2af7d9）只诊断第一项，1 error / 2 deselected / 68.67s，已定位
POST /identity/registrations 返回 503 identity.unavailable。尚未定位其数据库或业务根因，
不得用合成 Session 绕过。两个测试命令均已返回 exit 1，实例脚本已执行其 finally；没有把
仍在运行的测试误作终止或因观察超时重启测试。

原报告位于 .artifacts/g6-security-20260907，前缀为 g6-page-visits-r1/r2，失败报告没有覆盖。
新增 native fixture 复用实际注册/建空间的 workspace_http_subjects，仅输出接口、状态及固定
错误码；尚无 PostgreSQL 页面事件、回滚或并发撤权通过证据。

### 继续顺序与边界

本轮后段 exec_command 新命令无法返回结果，PowerShell（含不加载 profile）和 cmd 读取均
未返回；只取消了这些无输出的只读调用，未重启测试。替代 node_repl 启动也报 kernel assets
路径不存在。下一轮先恢复可执行的只读检查，再定位上述 PostgreSQL 注册 503；复验 0143
受信回滚、原生页面事件与相邻归档保护，完成 build/manifest 检查、敏感审计和独立提交。

上述源文件仍未提交，敏感审计尚未执行；不要把准备好的代码或通过的局部测试作为整批 QA。
工作簿本轮只读，G6/G7/G8 与 PRD-148 仍未证明完成；不新增 72 小时等待。后续继续其他
Legacy-only 消费端、完整业务页浏览器、PostgreSQL 并发撤权、KeyPool/DNS 总体时限及批次 QA。

## 2026-09-08 页面访问与原生邀请补充复验

本节补充前节的后续结果，不覆盖历史失败记录。共享 SQLite 迁移基线摘要未变；Excel 本次只读，
没有把局部测试通过写成 G6/G7/G8 完成，也没有新开 72 小时等待。

| 独立报告 | 结果 | 范围 |
| --- | --- | --- |
| `prd147-page-visits-rollback-r6-junit.xml` | 3 passed，47.095s | 原先两项归档失败及页面访问迁移保真复验通过 |
| `prd147-page-visits-logical-r7-junit.xml` | 7 passed，168.311s | 页面访问真实 Cookie、唯一事实、权限与事务、历史行保真 |
| `prd147-page-visits-manifest-r1-junit.xml` | 1 passed，2.885s | 前后端发布清单一致 |
| `prd147-page-visits-ui-r3-junit.xml` | 324 passed，无失败/错误/跳过 | 受影响客户端与生产 handler 单元，仍非浏览器证据 |
| `prd147-alias-unit-r3-junit.xml` | 8 passed，0.254s | 词法别名改名保留权限、事件、JSON、引号与中文文字值 |
| `prd147-page-visits-archive-r8-junit.xml` | 2 passed，22.207s | 归档回滚夹具适配 backend 独立编号后的 SQLite 冒烟 |

上述通过报告均无跳过。最新 production build 再次通过（276 assets），两端发布摘要仍为
`253f283d00c44354bbecf0975db6995ed70f5c285bf01f01a5b586e2bb2a1203`。
前节所述“回滚修正未复验”和“同步清单后 build 未重跑”已由这些后续结果补齐。

原生 HTTP bootstrap 先后查明的身份 schema 能力、Workspace 运营状态初始化和 Project 聚合
兼容性分别由 PRD-091/093/029 的独立设计、测试、审计及提交交付，本节不混记为页面访问实现。
三个提交 `07cea41`、`6809c76`、`cda51cf` 的各四项已提交源码字节再次与各自审计摘要比对通过。

原生 r8 已走通身份、空间、Project/Application，邀请写入因权限字符串被别名替换而拒绝。
修复生成器后，新增 PostgreSQL 0143 前向/回滚修复；页面访问原生迁移顺延 0144，SQLite 保持
0143。不可变 0139 未重生成。七个函数修复共 13 处文字值；四张表的 32 项 trigger 清单先校验
不可变基线或一致原生定义，再转换为原生格式。业务事实行不更新。

- r9 `f6cb19e74ee5435ea03a4f6406c8e918`：1 error，54.73s。预检拒绝旧 SQLite 格式清单，
  据此补齐精确旧清单摘要与完整原生触发器结构的验证；没有取消 drift guard。
- r10 `717a3d7534d444479117a198fe993b38`：9 passed / 1 failed，271.73s。空库修复往返及
  八项 up/down 函数、禁用 trigger、trigger 清单与表集合清单漂移原子拒绝通过。真实 HTTP
  注册、登录、邀请、受邀成员注册和接受已完成；随后测试错误地要求旧 invitation_events 非空。
  实际 Workspace 流程写 membership 事件并保留 Legacy invitations 关联，不写旧邀请事件表。
  断言改为确认真实 invitations 事实存在，不添加模拟事件。此轮尚未证明页面事件请求通过。

补充源代码复核覆盖函数所属 schema、PL/pgSQL、trigger 返回类型及执行配置。旧清单匹配不能
放过相同正文但不同 search_path、strict、leakproof、volatility 或 parallel 设置的函数；新增
function_settings 的 up/down 反例。回滚保留与还原函数一致的原生清单，事实和表结构不变。

### 本组定向验证完成记录

- r11 `2d14b99b06fd4411aff604ccf6531f5f`：13 passed / 1 failed，JUnit 314.423s。
  修复往返、十项漂移反例、真实 Cookie 页面事件唯一事实/幂等和严格传输/归属通过；真实邀请
  事实存在时 0143 回滚原子拒绝通过。最后一项已触发正确 owner guard，但测试仍匹配 SQLite
  错误文本，原生适配器按既有合同返回固定脱敏文本，故该断言失败，原报告保留。
- r12 `af9caf91d46444a6801f9dc4fc86aeb6`：1 passed / 13 deselected，JUnit 79.643s。
  只复验上述受影响的原生历史/回滚用例。保留生产错误脱敏，测试只采集白名单固定错误码和
  SQLSTATE；依次精确核对新事实阻止回滚、跨空间 owner 拒绝、不可变拒绝均为 23514。
  原有逻辑行历史、幂等和外键检查继续执行。不能把 r11/r12 拼成同轮全绿报告。
- `prd147-page-visits-sqlite-r9-junit.xml`：1 passed，22.195s。同一保真用例改为捕获
  IntegrityError 后的 SQLite 定向复验，无失败/错误/跳过，仍验证 SQLite 具体 guard 文本。
- `g6-page-visits-archive-r1-5b553bbec0364f33804c93716fb2d171-junit.xml`：1 passed /
  27 deselected，JUnit 73.050s。原生归档空库往返与隐私清单验证通过，回滚精确执行 0144、
  0143、0142，再重放同一版本序列；没有通过错误版本拒绝冒充归档保护。

受影响 Python 源码、共享夹具与新测试 Ruff 通过，git diff-check 通过。原生 0139 与 HEAD
逐字节一致，SHA-256 为 `11892ef4760a18c9adacb7745e8e01b173e651adf076fb44e18054b7cb934fe0`。
所有本组 PostgreSQL 命令均已返回且隔离实例完成停止。Console 开发服务已恢复，identity.html
返回 HTTP 200；未启动普通开发后端，不能据此宣称可登录或完成浏览器业务流程。

本组独立敏感审计入口为 `prd-147-workspace-page-visits-sensitive-audit.json`，覆盖 28 项
精确暂存源码、16 份独立原始报告、严格 UTF-8、凭据模式与共享测试基线摘要。模式扫描不是
安全会签；本组原生认证证据来自真实 API 经 ASGITransport 调用，不是浏览器/套接字证据。
页面访问的局部实现与上述定向验证已完成，其他消费端、完整业务浏览器、原生并发撤权、
KeyPool/DNS 总体时限和统一批次 QA 仍继续推进。G6/G7/G8 与 Excel 总目标未标记完成。

## 2026-09-08 模型路由与评估批次复验

范围：Alias、Fallback、RoutingPolicy、CacheAffinity、ModelQualityScore、Experiment
列表/创建/详情/结果。设计与两轮自审见本条设计文档，复用真实 Workspace Cookie/CSRF/Origin
和显式 Legacy Bearer，不生成影子 Session。写入、幂等重放、no-op 和统计终态均重新鉴权。
统计保留专用单线程、非阻塞单飞、250ms SQL 预算与 2s 响应边界；认证暖连接经现有池归还，
工作线程独立取得连接，既不跨线程共享请求句柄，也不缓存授权。只读认证仍执行真实会话、SSO、
条件访问和权限校验，仅不持久化拒绝状态。PostgreSQL 使用原生只读快照及剩余语句期限。

客户端改用显式认证传输，生产 handler 绑定公共 Workspace 会话、导航、页面代次和 DOM 归属。
Workspace ID 不充当 tenant ID。CacheAffinity 保留服务端事实 tenant，拒绝混合 tenant 列表、
已知 Alias 的 tenant 替换及迟到写回，保留 CAS、请求键、草稿和原意图重试。

原始报告均在 `.artifacts/g6-security-20260907/`。以下是独立运行，不拼接成一次整轮通过。

| 报告 | 结果 | 范围 |
| --- | --- | --- |
| `prd147-routing-evaluation-r8.xml` | 12 passed | 真实双 Workspace API、读写/重放/无副作用拒绝、统计及连接恢复 |
| `prd147-routing-evaluation-r9.xml` | 1 passed | 真实统计快照后会话过期，终态返回原始 401，事实不变、query_only 复位 |
| `prd147-evaluation-legacy-r1.xml` | 1 passed | 真实账号密码经 /auth/login，显式 Bearer 读写/结果/退出；撤销后拒绝，Cookie 不受影响 |
| `prd147-statistics-regression-r1.xml` | 4 passed | PRD-075 期限、单飞、工作线程和连接故障回归 |
| `g6-evaluation-r2-ce439146ec1b49278cfbc046ca5cd655-junit.xml` | 2 passed | 原生 PostgreSQL 真实业务与跨空间拒绝、十类终态过期回滚及连接关闭 |
| `prd147-evaluation-frontend-r5.xml` | 416 passed | 受影响传输、页面状态及 handler 单元 |
| `prd147-console-regression-r1.xml` | 1791 passed / 13 failed | 全前端 1804 项；新路由评估 handler 的 44 项全通过，整体未通过 |
| `prd147-head-source-control-r2.xml` | 107 passed / 13 failed | 仅将 main.js 源码读取替换为 HEAD 的对照，复现同样 13 项失败，不是完整 HEAD 回归 |
| `prd147-evaluation-manifest-r1.xml` | 7 passed | 实为生命周期测试，文件名不代表执行了 manifest 测试 |
| `prd147-frontend-manifest-r2.xml` | 1 passed | 实际前后端发布清单测试 |
| `prd147-browser-r1.xml` | 2 passed | 桌面/移动真实注册、登录并进入 Console 概览，无 Legacy token；不是业务页浏览器验收 |

失败追踪：red-r1 为两项预期认证失败；r2 导入错误已修正；r3 九项通过；r4 测试模型
context window 为零导致两项失败，改用真实模型 API 设置；r5/r6/r7 保留统计 503 及诊断。
冷 SQLite 首次 schema 解析 281--297ms 超过原预算，以上连接生命周期修复后 r8 通过。
frontend-r6 的测试错误地读取 listState 而非公开 state，已修正并由全前端运行覆盖。
原生 evaluation-r1 两项 setup error 来自既有审计水位分配缺陷，独立由 PRD-058 提交
`076920f` 修复，不归为 PRD-147 的原生审计交付。错误 selector 的零执行报告不计通过。

13 项前端失败分布在审计、成本异常、企业聊天、环境、FinOps、邀请、预算、浏览器验收、
批处理、Edge installer、请求解释、请求日志和 webhook dead letters；主要为旧 main.js
生命周期/启动源码断言。保留原始失败，下一批逐项检查生产行为后修复，不删除测试或改写成全绿。

发布表面验证通过：262 modules / 276 shipped sources；生产构建 276 assets，Console lint
777 files 通过。两端摘要为 `28523b6cfc83e87e929cb0c38b63540933f8b475ee2265694cd59e7cd318a624`。
Python E4/E7/E9/F/I 检查和 diff-check 通过；完整 Ruff 仍有 E501，未声明全量 lint 通过。
浏览器截图已查看，仍有 overview/nav 翻译键、中英文混合和移动侧栏空白，未宣称视觉 QA 通过。
两个私有目录已清理、子进程归零，受保护 SQLite 基线摘要不变；所有原生实例正常停止。

本批独立敏感审计为 `prd-147-routing-evaluation-sensitive-audit.json`，覆盖精确暂存字节、
原始报告、严格 UTF-8 和高置信凭据模式；不替代独立安全会签。用户明确调整为先交付可用版本、
后做 72 小时长稳。长稳保持未完成但不前置阻止交付，其他未验证业务与发布状态不冒充已完成。

## 2026-09-08 完整调度运行修复

移除开发启动脚本的隐式 scheduler=false，示例配置明确开启完整后台和导航投影。提交 guard
拒绝时立即回滚，失败时尽力关闭，保留原异常；仅 guard 工作器独占复用既有暖连接池。
协调器优先续租、在安装之间检查期限、先续租其他任务再并发回收失效 context；数据库操作
使用单个独立线程及线程内短 scope，避免默认业务线程池排队阻塞协调。

真实业务循环红测确认 Console 的 15 个工作项各被重复领取 12 次，商业计费的 10 项各重复
7 次。八个多队列域现以不可变 task-local lease 映射各自工作项，SQL 领取先过滤队列；
平台任务使用自身 proof，不借用 app.state 中其他分区的主租约。原 manifest/DDL/权限不变。

原始报告目录仍为 `.artifacts/g6-security-20260907/`，各轮独立，不能合并为一次通过：

| 报告 | 结果 | 范围 |
| --- | --- | --- |
| `prd147-release-profile-r3.xml` | 5 passed | 配置默认值、显式禁用与启动覆盖 |
| `prd147-fence-red-r1.xml` | 7 failed | 原 guard 拒绝保留事务的真实反例 |
| `prd147-fence-r5.xml` | 35 passed | 拒绝回滚、暖连接、续租与回收顺序 |
| `g6-fence-r1-f3e4cb42a00a462f9a63fee70c16c160-junit.xml` | 3 passed | 原生 PostgreSQL method/SQL/context 拒绝后第二连接可立即取得行锁 |
| `prd147-queue-red-r1.xml` | 2 failed | 生产 Console/计费循环重复领取反例 |
| `prd147-queue-r3.xml` | 54 passed | 八域映射、SQL 范围、线程继承、独立执行线程、双副本及提交 fence |
| `prd147-queue-api-r2.xml` | 1 passed / 2 setup errors | Console 真实业务 API 与内层 CAS 通过；导航旧 fixture 仍断言迁移 0139，当前为 0143 |

queue-api-r1 因旧 fixture 重复执行全量迁移并已出现两个 setup error 而中断，未生成完整 XML，
不计通过。复用受保护基线的 Console 定向 API 已通过，未改写过时导航 fixture 的失败。
所有测试进程已结束，原生实例在前轮正常停止。临时私有复制库撤销 lease 仅作诊断，不算长稳。

实际完整配置在队列绑定后可启动 63 个 context，并恢复真实账号页业务读取；随后仍发生
续租丢失，诊断发现默认线程池排队及 admin-assistant 回收后初始化失败。独立协调线程已补齐，
管理助手全库 WAL 收束与子系统生命周期不匹配仍需独立修复；未声明完整 profile、G6/G8 或
全目标完成。72 小时保持发布后任务，不能作为当前功能修复与交付的等待理由。

## 2026-09-08 有界暖连接容量

每数据库空闲句柄由 4 调整为 16，数据库 LRU 仍最多 4 个，溢出连接独立打开并在归还时关闭。
私有 60 秒对照的物理打开数分别为 106 与 25，属于诊断而非发布验收。新增请求/guard worker
三波十六路独占复用、旧 wrapper 拒绝、溢出关闭、跨数据库淘汰及完整关闭检查。

`prd147-pool-red-r1.xml` 为 2 failed / 2 passed，两个失败分别证实请求和 worker 的四句柄池
不能保留完整波次；`prd147-pool-r2.xml` 为 62/62，含生命周期、guard、配置、八域调度、
子系统 WAL 回收和完整性维护节拍。未更改租约 TTL、写锁等待、队列数量或后台开关。

首个真实新实例已取得 63 个队列，启动期累计 5 次 drop、3 次 backend failure，随后观测未增长；
不能记成零失败启动。管理助手 outbox 暖句柄临时表冲突已独立归属 PRD-077 修复。
本轮是发布修复增量，不认定完整可用发布、G6/G8 或长稳完成。

## 2026-09-08 有界批量续租

`211a5bb` 暖容量和 `f7e7486` outbox 临时表修复后，完整实例仍增长到 148 次 drop、
21 次 backend failure；此前零错误短观察不能覆盖后续失败。采样和私有争用实验支持减少
逐队列写锁申请，本次在最早 proof 到半程时一次事务续租全部活跃组，每组独立 savepoint。
公共多队列 helper 共用 CAS，保持组内原子性；数据库故障回滚整批，不发布未提交 proof。

原始报告均在 `.artifacts/g6-security-20260907/`：

- `prd147-batch-red-r1.xml`：9 failed / 1 passed，包含新接口缺失、旧串行续租及改接点的测试失败。
- `prd147-batch-r2.xml`：错误测试路径，零执行，不计通过。
- `prd147-batch-r3.xml`：43/43；`r4`：62/62；`r5`：63/63，增加整批数据库异常回滚。
- `g6-batch-renewal-1e8e03dfd1ff431ab46537acb6322b0b-junit.xml`：2 passed / 1 failed，
  失败是夹具直接停用受保护 registry 被完整性约束拒绝，未绕过该保护。
- `g6-batch-renewal-r2-248c5bb1b39245deb550bcbaf1d92052-junit.xml`：原生三项通过。
- `g6-batch-clock-red-a6026127dcff47cebf3d4772482a49d3-junit.xml`：1 failed，真实持锁超过
  三秒后，事务开始时间使过期 proof 仍续租成功。改为先取得行锁，再读取数据库墙钟。
- `g6-batch-final-679d691b95154b779a93cca3a99210b7-junit.xml`：原生四项全部通过，
  包含失效组隔离、savepoint 原子性、有效组 fence 和锁等待期间过期拒绝。原生实例正常停止。
- `prd147-batch-runtime-short-r1.json`：36 样本、108 HTTP 检查全部通过，63 队列，
  drop/backend failure/shutdown timeout 均为零，观测持续刷新。版本是 `889900c` 加批量续租，
  在最后 PostgreSQL 墙钟增量之前；不是最终版本全业务或 72 小时证据。

真实浏览器：重启后 Cookie 会话恢复，概览和 Provider 状态读取成功；继续点击成员管理则
触发旧 `/users` Bearer 请求并退出控制台。该业务入口缺口继续修复，不能据短健康检查宣称
完整可用发布。唯一配置的本地模型端点仍不可用，未关闭 loopback/SSRF 保护或伪造探测成功。

## 2026-09-08 Workspace 成员入口交付增量

主 Console 成员列表改用既有 Membership Cookie API，保留分页、后端 can_manage、CSRF 和
If-Match revision。列表和更新进入已有 owner/代次/取消生命周期；旧 DOM、旧响应、跨空间
迟到结果、重复提交和无变化提交均不能产生有效写入。邀请与所有权管理复用账号页流程。

原始报告位于 `.artifacts/g6-security-20260907/`，各轮范围分开记录：

| 报告 | 结果 | 范围 |
| --- | --- | --- |
| `prd147-members-red-r1.xml`、`prd147-members-r2.xml` | 测试语法失败 | 新测试的正则结束括号未转义，不是产品红测 |
| `prd147-members-r3.xml` | 292 passed | 成员迁移及相关客户端、生命周期合同 |
| `prd147-members-frontend-r4.xml` | 1830 passed | 无变化按钮增量前的完整前端 |
| `prd147-members-r5.xml` | passed | 无变化提交及主入口生命周期定向复验 |
| `prd147-members-frontend-final-r6.xml` | 无完整产物，不计通过 | 无界并发运行有子进程停在启动阶段；中止后用有限并发重跑 |
| `prd147-members-frontend-final-r7.xml` | 1831 passed | 四并发、每文件 60 秒上界的完整前端；最后空分页增量之前 |
| `prd147-members-final-r8.xml` | 299 passed | 最终成员、资源页面、legacy users 和 Workspace Access 定向回归，含空分页恢复 |
| `prd147-members-api-r1.xml` | 3 passed / 7 deselected | 隔离真实 Membership API，角色授权、变更失效及 Cookie/Origin/CSRF |
| `prd147-batch-members-final-r6.xml` | 63 passed | 调度、事务、最终 PostgreSQL 墙钟代码和此前摘要合同 |
| `prd147-members-manifest-r8.xml` | 2 passed / 22 deselected | 最终两端前端摘要合同 |

最终生产构建 276 assets、生产表面 262 modules / 276 sources、Console lint 779 files 通过。
两端最终摘要为 `82d2feca2907d00a3ab6b99e6053a9fa6480378b7e965e94d348b6ec6edae165`。
源码更新后旧摘要导致的构建拒绝保留为正常漂移门禁，不删除校验。

真实桌面浏览器已验证 Cookie 恢复、两名现有成员显示、默认保存禁用、改字段启用及恢复原值
再次禁用；没有提交现有成员权限变更。邀请链接实际进入账号页，原成员、邀请及所有权流程可见。
桌面截图已检查。移动 viewport API 两次设置后实际 DOM 仍为 1265px，不能记作 390px 通过；
临时标签已关闭、视口覆盖已重置。真实移动验收与完整业务验收仍待完成，不以单元测试冒充。

最终三服务已重启，原日志保存在 `.logs/archive-members-release-20260908-r1/`；短运行结果
单独记录，不替代完整业务或 72 小时长稳。用户要求的先交付后长稳顺序保持不变。

`prd147-members-runtime-short-r2.json` 的 18 样本 / 54 次 HTTP 全部返回 200，drop、backend
failure 与 shutdown timeout 均为零。首个样本仍在启动收敛阶段，为 62 队列，随后 17 样本
均为 63；整份严格判定仍为 failed，未重写或以筛掉首个样本的方式冒充完整运行通过。
最终重启后的真实浏览器再次恢复原 Cookie 会话和成员列表。独立敏感审计覆盖精确暂存内容、
上述成功及失败原件与受保护基线，不构成完整 G6/G8 或正式 GA 放行。

## 2026-09-08 Kimi 测试服务接入与凭据配置修复

用户提供一次性测试凭据后，官方 `https://api.moonshot.cn/v1` 模型列表与 `kimi-k2.6`
生成均成功。本地真实 Cookie 管理流程已创建 `kimi_test` Provider、`kimi-test` 模型和开发
环境的 `kimi-test-chat` 别名，没有修改原有本地模型或创建新的 Virtual Key。

真实保存首次遇到 `provider_key.encryption_failed`：`.env` 被 AppConfig 解析，但 Provider
加解密仅查进程环境。新增禁止 repr/序列化的配置字段，显式传递到 Provider 创建、本地模型
凭据、健康探测、普通 KeyPool、亲和选择/恢复/资格，以及初始化向导。保留 AES-GCM 格式与
既有环境变量兼容，不轮换主密钥，不写明文，不禁用权限、健康或网络安全规则。

- `kimi-key-config-red-r1.xml`：两项预期失败，缺少 Provider 加密配置字段。
- `kimi-key-config-r2.xml`：3 passed，Control/Gateway 文件配置、环境优先和配置脱敏；真实
  Workspace 创建后 Gateway 使用配置主密钥解密，错误主密钥拒绝，响应与日志不回显合成秘密。
- `kimi-key-config-r3.xml`：最终 3 passed，补充真实密文的亲和 Key 选择与 warm 资格校验；
  复用受保护基线的私有副本，原始基线摘要不变。
- `kimi-key-regression-r1.xml`：3 passed / 2 failed，普通/亲和解密失败与
  亲和恢复边界通过；两个 Local Endpoint 测试停于旧 fixture 的最新迁移固定为 0089 断言。
  改为检查必需迁移 0089 已包含，不退回旧 schema，不省略后续迁移。
- `kimi-local-endpoint-r2.xml`：补跑 2 passed，真实 Local Endpoint 创建、权限、通用入口
  绕过拒绝与创建重放在密钥校验/加密之前返回均通过。原失败报告未改写。
- `kimi-onboarding-r1.xml`：1 passed / 1 deselected，真实初始化链激活及证据丢失重新阻断。
- `kimi-gateway-compatibility-r1.json`：仅模型列表成功，当时探针偏好的旧模型均不存在，未调用生成。
- `kimi-gateway-compatibility-r2.json`：官方生成 200，11 输入 / 5 输出 / 16 tokens。
- `kimi-metahub-call-r1.json`：本地真实 Gateway、既有开发 Virtual Key、别名与托管 KeyPool
  路径生成 200，11 输入 / 5 输出 / 16 tokens；Provider healthy，真实请求与用量持久化成功。
- `kimi-metahub-stream-r1.json`：同一路径 SSE 200，结束标记与非空内容已验证，11 输入 /
  4 输出 / 15 tokens。真实生成合计 47 tokens，不自动重复消费测试额度。

两服务已重启加载修复，旧日志保留在 `.logs/archive-kimi-key-config-20260908-r1/`。真实
测试凭据仅通过本地 Provider 管理 API 加密入库；没有写入源码、`.env`、探针或报告。模型
本地价格标记 `test-unpriced-20260908`，未配置真实价目，界面零价不代表上游免费或正式账单。
前端本轮没有代码变更；目录/别名页曾出现刷新错误，重新加载后可完成配置，这一间歇问题
仍需单独定位。此次是可用 AI 调用交付，不代表完整目标、全部页面、GA 或 72 小时已完成。

最终受影响用例共 9 个独立场景通过（不重复累计 r2/r3 的相同用例）。新测试和向导文件完整
Ruff 通过；历史文件保留原有 E501 行宽问题，其余规则通过，修改文件 compileall 与 diff
检查通过。源码与暂存字节、严格 UTF-8、凭据模式和受保护基线由独立审计脚本核验；另对
本轮日志、探针和证据执行真实 Key/主密钥精确字节扫描，报告只保存结果，不保存秘密。

## 2026-09-08 Workspace 调用日志、保存筛选与审计导出

本批使用真实 Workspace Cookie 和显式 Legacy Bearer，QueryEvent 保存所属认证域的真实
Session 外键。个人 SavedFilter、审计读取、带 Origin/CSRF 的 CSV GET、幂等重放及最终提交
均重新鉴权。SQLite 0144 与原生 PostgreSQL 0146 是独立增量迁移，不改历史基线。

证据位于 `.artifacts/g6-security-20260907/`，原失败报告保留，不覆盖或累计重复执行：

| 报告 | 结果 | 实际覆盖 |
| --- | --- | --- |
| `prd147-logs-r6.xml` | 6 passed | SQLite 真实双域 API、筛选 CRUD、查询详情、CSV/CSRF 重放、两个真实 outbox、跨 owner、9 类终态过期拒绝、往返保真和结构漂移拒绝 |
| `prd147-logs-native-r5.xml` | 6 passed | 同六场景在真实 PostgreSQL 执行，含原生触发器、约束、隐私 manifest 保真 |
| `prd147-logs-unit-r2.xml` | 56 passed | 本批相关 Python 单元；显式私有 basetemp |
| `prd147-log-pages-r3.xml` | 431 passed | 日志及相关资源页生命周期、客户端合同和 owner 竞态 |
| `prd147-log-pages-refresh-red.xml` | failed | 保存筛选 mutation 被同 owner 的后台授权刷新错误淘汰的原始反例 |
| `prd147-log-browser-red-r1.xml` | 1 failed / 11 passed | Workspace 会话缺失被错误映射为连接失败；真实客户端与 memory 组合用例通过，不能据此排除真实浏览器间歇传输失败 |
| `prd147-log-browser-r2.xml` | 12 passed | 认证提示修复后的定向复验 |
| `prd147-logs-frontend-final-r4.xml` | 1844 passed | 最终全部前端单元，四并发、每文件 60 秒上界；临时诊断代码已删除 |
| `prd147-logs-manifest-r4.xml` | 2 passed / 22 deselected | 最终前后端生产摘要合同 |

最初 Python 单元运行因系统 Temp 权限失败；使用 `.tmp/pytest-prd147-logs-unit-r2` 后通过。
资源页回归最初的 272 个 VM harness 错误是新增清理依赖未接入夹具，补齐后 431 项通过。
早期 SQLite/原生迁移和数据库兼容失败的 r1..r5、native-diag 与 PostgreSQL 日志均继续保留。
最终 lint 780 files、生产表面 262 modules / 276 sources、构建 276 assets 通过；最终两端
资产摘要为 `d7a9a49ab79e9f1638afe95bf1f74e7f5e7b2ef90a1c9bde93757df8c52040b2`。

本地升级报告 `prd147-logs-local-upgrade.json`：先 SQLite backup API 备份到
`.data/backups/log-release-20260908-r1/before.sqlite3`，私有副本演练成功后升级
`.data/metahub-dev-prd117.sqlite3` 至 0144。quick_check、外键及 11 张历史表内容/rowid 摘要
均不变。原生隔离测试实例已正常停止，升级未重复 apply，备份不提交。

真实浏览器恢复既有账号 Cookie，账号页重新建立 Workspace 会话，无需输入新密码或调整
权限。调用列表显示已有 Kimi JSON/SSE 两条真实请求，详情分别保留 16/15 tokens 和冻结
路由、价格、风险事实；不发送新的 Provider 请求。创建个人筛选 `PRD147 local log acceptance`
返回 201，刷新后仍可选择并按服务器 SavedFilter ID 应用。该测试筛选暂保留。

真实审计 CSV 导出返回 200，生成新 `audit.exported` 事实；浏览器 download 事件等待超时，
随后独立检查 `C:/Users/OseasyVM/Downloads/metahub-audit-20260908-0f2ab861.csv` 已落盘，
2038 bytes、2 行，SHA-256 与页面一致：
`16a32f76ddff2c2e13ab6574f619bcf322a3666e16d776a9141048ef59a4e1ef`。
此检查证明下载完成，不把自动化事件超时认定为产品导出失败。CSV 包含本地审计数据，不提交。

视口测量曾确认 1280x900 以及 390x844；移动审计页 scrollWidth=390、可见按钮没有横向越界，
截图 `prd147-audit-mobile-r1.png` 已检查。内嵌浏览器桌面截图受宿主裁剪，fullPage 拼接出现
重复区域，因此不将该拼接图作为桌面无重叠证明。全部页面/全部移动流程仍不能据此认定通过。

未解决项：真实浏览器在日志列表或 SavedFilter 并行刷新时两次出现 TypeError，响应未被
浏览器接纳，即使对应服务日志存在 200；重载恢复后查询和筛选应用成功，尚未证明根因已修复。
临时诊断仅包含异常类型及源码位置，最终已删除，没有记录原始异常正文或凭据。
后台仍出现 platform_metering.reconciliation_required，旧运行还有 lease_lost；不能宣称
全部后台健康。原日志分别保留在 `.logs/archive-log-release-20260908-r1/` 和
`.logs/archive-log-browser-20260908-r2/`。反馈、请求解释等相邻旧入口及完整 PRD-148/G6/G8
仍未完成；此批不代表全目标、GA 或 72 小时已通过，先交付后长稳的用户顺序保持不变。

最终重启前的七份日志/PID 文件另存于 `.logs/archive-log-final-20260908-r3/`，不覆盖前两次
归档。重新核验本项目监听进程后，`prd147-logs-runtime-short-r1.json` 在 11:16:27 至
11:17:17 UTC 完成六个样本：18 次 HTTP 均为 200，每个样本均为 63 队列，drop、backend
failure、shutdown timeout 为零，协调器观测时间保持新鲜，严格判定 passed。这是约 50 秒
的有限观测，不替代后台业务逐项验收、性能结论或 72 小时长稳。最后重启后真实浏览器重新
加载审计页，原 Cookie 会话恢复并显示三条审计事实，没有再次导出或调用付费模型。

四份新增迁移仅按仓库规则规范为 LF，真实迁移加载器复核 0144 规范摘要仍与已应用数据库
一致，没有重复迁移或改变 SQL 内容。最终独立脚本报告为
`prd147-logs-runtime-sensitive-r2.json` 与 `prd-147-workspace-logs-sensitive-audit.json`：
分别核验真实测试 Key/主密钥精确字节，以及本批 30 个暂存文件的路径/字节、严格 UTF-8、
高置信凭据模式、成功与失败证据及受保护基线。扫描结果无命中，不等于完整安全验收。

## 2026-09-08 请求反馈与解释批次

范围：Workspace/显式 Legacy 反馈写入、250ms 总预算汇总、日志内联请求解释、真实双域
访问事实及提交时字段权限裁剪。解释不得重复调用 Provider，不得恢复原始正文或密钥。
工作簿只读核对 `11_现有导航处置!A48:J49`，没有修改工作簿或以本批替代全部导航迁移。

### 定向与回归

所有原始报告均在 `.artifacts/g6-security-20260907/`，不同轮次分开记录：

| 报告 | 结果 | 范围 |
| --- | --- | --- |
| `prd147-explanation-r3.xml` | 10 passed | SQLite 双域真实 HTTP、Origin/CSRF、混合凭据、同形跨租户 404、Session 外键/XOR、终态过期和快照后撤权、访问 outbox、迁移往返/漂移拒绝、最后权限快照投影、反馈四场景 |
| `prd147-feedback-native-explanation-r2-347f8e0adf3641c1aa60498dbf599f6c.xml` | 12 passed | 原生 PostgreSQL 反馈与解释、250ms 汇总 HTTP、锁/statement 预算取消恢复、双域事实和迁移 |
| `prd147-feedback-native-explanation-final-r3-203c9db712bb4694b58c4ff849e0cc7c.xml` | 18 passed | 原生反馈、解释和既有日志回归，含迁移/down 守卫与预算合同 |
| `prd147-explanation-log-regression-r1.xml` | 6 passed | SQLite 已交付日志/导出回归；旧迁移测试先逐一执行后继迁移的正式 down，再验证目标迁移，未绕过回滚守卫 |
| `prd147-explanation-legacy-r1.xml` | 3 passed | Legacy API/RBAC/隐私、微秒源时间保真、缺表/锁/连接释放 |
| `prd147-feedback-explanation-units-r1.xml` | 110 passed | 共享 DB 预算、反馈、事务生命周期、解释合同与字段裁剪 |
| `prd147-feedback-explanation-web-full-r2.xml` | 1855 passed | 全部前端单元，包括真实生产 handler、重复点击、旧 DOM、迟到 200/401、同 owner 刷新、清空草稿、Cookie/CSRF 与混合凭据拒绝 |
| `prd147-feedback-explanation-manifest-r1.xml` | 1 passed / 23 deselected | 前后端发布清单及摘要一致 |
| `prd147-feedback-regression-r1.xml` | 9 passed / 2 failed | 两处旧测试错误地用 dataclasses.replace 复制 Pydantic 配置，尚未进入业务 |
| `prd147-feedback-regression-r2.xml` | 2 passed / 4 deselected | 改用 model_copy 后重验上述两个真实并发/副作用场景 |
| `prd147-feedback-worker-red-r1.xml` | 1 failed / 4 deselected | 真实 HTTP 证明旧反馈写入仍在事件循环线程执行 |
| `prd147-feedback-worker-r2.xml` | 5 passed | 反馈整体移入既有工作线程模式后，SQLite 双域 API、幂等、撤权、汇总预算及线程合同 |
| `prd147-feedback-native-worker-r4-c3a9c55f40bf47e3ae18a399ce6ffae7.xml` | 7 passed | 最终工作线程版本的原生 PostgreSQL 反馈回归；隔离实例正常停止 |

原失败证据保留：SQLite 撤权测试最初在仍持有读锁时提交另一连接写入，得到 database locked。
修正后在读取事务回滚与第二次 begin 之间提交撤权，断言已经结束旧事务，验证实际终态窗口。
前端旧断言要求独立页面 handoff，已改为内联动作，继续检查 ID 不进入 URL/Storage。
字段测试曾发现不存在的 webhook.read，改用路由实际已有的 webhook.manage 后通过；未加假权限。

### 发布准备

隔离目录使用正式构建脚本构建 276 个资产；第一次遗漏 partner.html 的失败副本保留，补齐
清单中的 HTML 后构建通过，没有让失败构建清空当前 dist。前端 lint/生产来源检查通过。
发布摘要为 `cfcefcae1cb4e249db79d500aa5640661902c7ae9849680d8fbba51b1f5afc34`。
`prd147-feedback-explanation-local-upgrade-r1.json` 记录 SQLite backup API 备份和 0145
副本升级演练，15 张业务表历史 rowid 与全部既有列的摘要保持相等，quick_check 与 FK 检查通过。

本批没有新增付费模型调用，沿用之前验证的 Kimi 本地加密配置。PRD-148、G6/G8、完整
业务域与 72 小时长稳仍不能据此认定完成，用户要求的先交付再长稳顺序不变。

### 本地发布与真实反馈

0145 已对 `.data/metahub-dev-prd117.sqlite3` 实际应用；升级前另存
`.data/backups/feedback-explanation-release-20260908-r1/before-apply.sqlite3`，15 张历史业务表
既有列及 rowid 摘要一致。旧 dist 可恢复地保存在 `.tmp/prd147-feedback-explanation-dist-before-r1`。
Console 已明确以 `METAHUB_CONSOLE_ROOT=C:/MetaHub/console-web/dist` 启动，在线
`production-assets.json` 与发布摘要一致，不再把源码服务器当作构建发布验证。

真实 Cookie 浏览器首次打开与手动刷新解释，各产生一条独立访问事实，关联真实 Workspace
Session 并完成事件投递；清空后输入和结果消失。源请求的路由、用量和十阶段决策链可见，
历史 Provider 响应证据标为未记录，未伪造完整证据。

反馈首次及同操作重试均为 503，移入工作线程后仍出现 503。临时诊断只记录类型、SQLite
错误码和源码位置，确认 `SQLITE_BUSY` 发生于 `BEGIN IMMEDIATE`，没有半条反馈。隔离副本
写入成功；`prd147-feedback-lock-probe-r1.json` 的冷/暖连接各四次锁探针全部取得并回滚锁，
未改业务数据。这些诊断尚不能证明实际锁持有者或根因已消除，未放宽 250ms 写锁预算。

诊断运行的同操作重试返回 201，保存唯一反馈 `rfb_7a9f12463a214025b73d51a95fb705f2`。
恢复正式 `start-control-api.ps1` 后，真实页面显示已记录反馈 5/5、已采纳；汇总显示一条样本、
平均 5/5、采纳率 100%，正式服务日志中的 `/feedback/summary` 为 200 / 158ms。
成功写入发生在诊断运行中，不冒充正常完整 profile 下稳定写入已验证。诊断 launcher 已退出，
原诊断文件与失败截图保留，没有进入正式启动脚本或受版本控制源码。

桌面 1280x900 汇总与移动 390x844 反馈页已经截图检查；页面没有横向溢出，可见控件没有
越出视口。截图分别为 `prd147-feedback-summary-desktop-r1.png`、
`prd147-feedback-mobile-saved-r1.png`，失败原件 `prd147-feedback-mobile-failed-r1.png` 保留。
返回日志时仍遇到读取失败，重载后恢复；此前间歇 TypeError 未证明修复，不以服务端 200
替代浏览器成功。后台 `platform_metering.reconciliation_required` 也仍存在。

短观测 r1 六样本/18 HTTP 通过；正常启动后的 r2 首样本仍为 62 队列，之后收敛到 63，
严格报告保留 failed，未删掉首样本。各轮均不代表完整后台业务或 72 小时稳定性。

正常服务收敛后的 `prd147-feedback-explanation-runtime-short-r3.json` 六样本/18 HTTP 全部
通过；各样本 63 队列、drop/backend failure/shutdown timeout 均为零，观测持续刷新。
`prd147-feedback-explanation-facts-r1.json` 与 r2 在解释重开后相隔超过一分钟，均为两条
Gateway 请求、三条解释事实、一条反馈及一个反馈事件；后台刷新没有增加解释访问次数，
quick_check 正常、外键违规为零。解释桌面/移动截图已检查，临时 viewport 已调用 reset。

最终扫描使用新报告 `prd-147-feedback-explanation-sensitive-audit-r2.json` 与
`prd147-feedback-explanation-key-audit-r2.json`，覆盖最终暂存字节、成功/失败证据、诊断
辅助脚本、日志及真实测试 Key/主密钥的精确字节，受保护基线保持原摘要。扫描不是完整安全
签署；本批提交不把真实写锁争用、日志间歇失败、PRD-148 或整体目标标记完成。

## 2026-09-08 反馈超时与会话生命周期修复

正式 profile 重放已存在的 operation，不调用 Provider、不新建反馈样本。r1 辅助脚本将
workspace_id 误当 tenant_id，在 Workspace 选择前退出，业务事实未变化；r2 改用真实绑定表，
七次反馈重放成功，但 Summary 最后一次返回 workspace_session.revoked 并清除 Cookie，
下一步辅助脚本因缺失 Cookie 停止。只读数据库确认该测试 Session 被写为 permission_changed。

根因一：_permission_snapshot 的任意 ApiError 都被解释为权限变化，原生 SQL 中断也会导致
401 和永久撤销。现在依赖不可用只拒绝本次请求并回滚；真实权限拒绝、矩阵破坏、版本或指纹
变化仍保持撤销。Summary 的认证和事务回调把读取故障映射为 feedback.unavailable。

根因二：HTTP deadline 可先于 shield worker 退出，原请求 scope 会提前回收仍被 worker 使用的
连接。现在鉴权完成后只归还空闲句柄，worker 独立借用同一有界池并负责最终归还，活动事务
不被提交、回滚或转交。250ms 预算、并发槽及终态新快照复核均保留。

### 定向验证与失败保留

报告位于 `.artifacts/g6-security-20260907/`，轮次不合并冒充一次验收：

| 报告 | 结果 | 说明 |
| --- | --- | --- |
| `prd147-permission-read-red-r1.xml` | 3 failed | 首次认证、读取事务、终态复核均以真实 SQLite VM 中断复现错误 401 |
| `prd147-summary-worker-red-r1.xml` | 1 failed | 真实 HTTP 超时后 worker 连接已被提前关闭 |
| `prd147-permission-worker-green-r1.xml` | 6 passed / 5 failed | 独立 worker 重复解析冷 schema 导致原预算失败，未放宽预算 |
| `prd147-db-stage-r1.xml` | 12 passed / 1 failed | 新测试错误地跨序退出同一 ContextVar 栈，污染后续用例；改为独立 Context |
| `prd147-db-stage-r2.xml` | 13 passed | 空闲阶段归还、旧 facade 失效、worker 独立生命周期、活动事务保留及原池上限 |
| `prd147-permission-worker-green-r2.xml` | 11 passed | SQLite 反馈完整定向集，包括原生中断后的同 Cookie 恢复、真实撤权与超时 worker |
| `prd147-permission-worker-units-r1.xml` | 64 passed / 3 failed | 旧连接替身缺少 in_transaction；旧启动断言仍要求关闭后台任务 |
| `prd147-permission-worker-units-r2.xml` | 67 passed | 修正测试合同后，DB 生命周期/预算、共享反馈、控制面清理和事务授权均通过 |
| `prd147-feedback-native-permission-worker-r1-9d31cfd7c121437b8c441b3c5052aa95.xml` | 11 passed / 2 failed | 独立 worker 初版的原生汇总预算失败 |
| `prd147-feedback-native-permission-worker-r2-4bec45dd47314e7ea46b0b171c56be3a.xml` | 12 passed / 1 failed | 新生命周期及故障恢复通过，首条真实汇总仍为 503；不标记原生整轮通过 |

### 本地发布事实

只重启控制服务，仍使用正式 `start-control-api.ps1`；前端 dist 和 Gateway 未重启，没有数据库
迁移或恢复旧会话。新日志为 `.logs/control-feedback-reliability-r1.*.log`，原日志未覆盖。
真实浏览器重启期间出现连接失败，服务就绪后重新验证恢复原 Session 和两条 Kimi 日志。
Summary 首次 503 后保持登录，手动刷新恢复 1 条样本、5/5、100% 采纳；截图
`prd147-feedback-reliability-recovered-r1.png` 已检查，不以重试成功隐去首次失败。

`prd147-normal-feedback-replay-r3.json` 记录八次正式服务幂等重放全部 200；两条请求、三条解释、
一条反馈、一个反馈事件以及反馈/事件/聚合摘要前后一致。八次 Summary 为 2 次 200、6 次
503，因此组合报告仍为 failed；没有再次出现 401，测试 Session 保持 active。
本轮不证明首次反馈写锁竞争、日志 TypeError 或全部后台业务已解决，也不代表 PRD-148、
G6/G8、全业务域、GA 或 72 小时已完成。先交付再长稳的顺序保持不变。

### 预算诊断与查询合并

`prd147-feedback-native-http-profile-r1-3d4f2402c9af4f15897fbf0024e6cfcb.xml` 仅为诊断：
首样本在真实 250ms 时限返回 503，鉴权 125ms、首个权限快照 78ms、汇总已执行 125ms。
后两个样本存在超时线程的交错回调，不作为逐请求精确归因；诊断测试本身通过不代表 API 通过。
确认多次权限复核反复执行相同的五表存在性检查后，合并为同一事务中的单查询，不缓存权限。

`prd147-permission-worker-units-r3.xml` 为 74 passed / 2 failed：最初基于无参 pragma_table_list
的批量检查错误地包含 sqlite_schema，且 SQLite trace 会额外记录内部 PRAGMA。改为显式
main.sqlite_schema 的真实 table 集合后，`prd147-permission-worker-units-r4.xml` 为 76 passed，
覆盖单次查询、大小写、View/TEMP/附加 schema、虚拟/影子表、事务内 DDL 与非法名称。
两轮原件保留，缺表和权限矩阵破坏仍拒绝访问。

`prd147-feedback-reliability-runtime-r1.json` 在补丁首轮发布后完成六样本/18 HTTP，63 队列、
drop/backend failure/shutdown timeout 均为零；仍不是后台所有业务成功或 72 小时证明。

最终查询合并版的 `prd147-permission-worker-final-r3.xml` 为 5 passed / 6 deselected；
`prd147-feedback-native-permission-worker-r3-80e57e973133402d8bf8aef96804e8ee.xml` 为 6 passed，
覆盖原生此前失败的创建/幂等/跨域与汇总流程、三阶段数据库故障后 Cookie 恢复、超时 worker
以及 current_schema 真实表/视图/临时表/DDL 合同。该轮是定向重验，不把前两轮失败改写。

共享 RBAC 查询优化在控制服务和 Gateway 通过正式启动脚本一起加载；新日志分别为
`.logs/control-feedback-reliability-r2.*.log`、`.logs/gateway-feedback-reliability-r2.*.log`。
前端 dist 未修改，未新增模型调用或恢复已撤销的历史会话。

`prd147-normal-feedback-replay-r4.json` 在最终双服务版本下八次重放均 200，同一反馈身份和
反馈/事件/聚合摘要不变；Summary 为 7 次 200、1 次 503，组合报告保留 failed，没有 401。
浏览器最终重载和随后刷新汇总仍连续 503，返回日志也出现旧的读取失败，分别保留
`prd147-feedback-reliability-final-failed-r1.png` 与
`prd147-feedback-reliability-final-logs-failed-r1.png`。HTTP 重放成功不代表浏览器问题已解决。

`prd147-feedback-reliability-facts-final-r2.json` 确认两条请求、三条解释、一条反馈/事件，
quick_check 正常、外键违规为零。`prd147-feedback-reliability-runtime-r2.json` 六样本/18 HTTP
均 200，但协调器为 accepted=126、dropped=63、backend_failure=17、queued=63，因此严格
判定 failed。旧进程观测也存在非零累计值；没有清零、删历史或为得到零计数再次重启。
协调器失败归因、原始首次反馈写锁与浏览器间歇故障继续作为未完成项，而非本批修复成果。

## 2026-09-08 Workspace 认证写锁准入批次

工作簿路线图只读复核，原表未导出或修改。继续先交付可用补丁，再独立验证长稳；不把
PRD-148、G0..G8、全域业务或 72 小时验证标记完成。补丁仅调整独立 Workspace 认证入口，
近期会话只读验证，需要触达或持久化撤销时先结束旧快照，再进入写事务完整重新鉴权。
同一 deadline、原始权限目录完整性检查、条件访问、Cookie 和调用方事务合同不变。

本批证据位于 `.artifacts/g6-security-20260907/`：

| 报告 | 结果 | 说明 |
| --- | --- | --- |
| `prd147-auth-admission-red-r1.xml` | 4 failed | 旧实现争抢独立写锁、没有只读阶段、不能在阶段之间重新验证 |
| `prd147-auth-admission-green-r1.xml` | 14 passed / 1 failed | SQLite 反馈回归通过；并发撤权夹具使用 DELETE 模式，写方无法在旧读事务期间提交 |
| `prd147-auth-admission-green-r2.xml` | 3 passed / 1 failed | 仅预热并发连接不能消除 DELETE 模式的读写阻塞，失败保留 |
| `prd147-auth-admission-green-r3.xml` | 4 passed | 明确以运行库同款 WAL 验证并发提交；近期会话、触达重验、阶段间撤权与旧拒绝不直接持久化均通过 |

原来的反馈认证写锁预算用例增加已到期触达条件，仍验证写锁等待计入 60ms 测试预算并返回
feedback.unavailable，不再错误要求每次近期只读认证也争抢写锁。第二阶段发生异常时不能
返回第一阶段的 context。原生 PostgreSQL 定向验收与正式服务验证尚待下节记录。

`prd147-contention-runtime-before-r1.json` 三次采样的九次 HTTP 均为 200，但已有累计
accepted=126、dropped=63、backend_failure=17，因此报告为 failed；后续只读检查观察到
189/126/36，旧进程不能当作健康。运行库使用 WAL，meter outbox 一条 failed，具体为
platform_metering.owner_invalid，另一条 pending；未跳过 checkpoint 或改写账务事实。

直接对比确认 localhost 健康请求约 2.1 秒，而 127.0.0.1 为 15-16ms，说明这部分客户端
连接回退耗时不能归因于服务端数据库处理。浏览器仍复现汇总 503 和保存筛选加载失败。
`prd147-auth-admission-facts-before-r1.json` 确认两条请求、三条解释、一条反馈及一个事件，
quick_check=ok、外键违规为零；本轮未消耗 Kimi 调用额度。

### 原生验证与本地加载结果

`prd147-feedback-native-auth-admission-r1-16fcc42f3ffa43a88334dfa2220c4b5d.xml` 为
9 passed / 1 failed。三项新快照准入测试、真实权限丢失撤销、首次与终态权限读取故障恢复、
创建/重放/跨域/汇总流程和原生锁预算通过；读取阶段故障用例 `[2]` 的随后恢复 HTTP 请求
仍返回 feedback.unavailable。该轮保持失败，不自动重试或增加时限；隔离 PostgreSQL 已停止，
其 postmaster.pid 已确认不存在。`prd147-auth-admission-units-r1.xml` 为 37 passed，覆盖
连接生命周期、控制面清理和资源事务生命周期。Ruff 和 git diff --check 通过。

本地 SQLite 补丁通过正式 start-control-api.ps1 加载，控制服务 PID 为 11300，日志为
`.logs/control-auth-admission-r1.*.log`。Gateway 和前端未重启，也未迁移或修改已有业务数据。
停止旧进程前的 `prd147-contention-runtime-predeploy-r1.json` 记录累计 252 次接纳、189 次
丢弃、51 次后台失败，报告为 failed。新进程短时 `prd147-contention-runtime-after-r1.json`
六次采样/18 HTTP 均成功，63 队列、无新增 drop/backend failure/shutdown timeout；只能证明
该短窗口，不证明旧故障根因已消失，也不算 72 小时验证。

`prd147-auth-admission-live-replay-r1.json` 八次同一操作键的反馈恢复全部 200，始终是同一
反馈且事实摘要前后一致。日志与 SavedFilter API 均 200，Summary 则为 1 次 200、7 次 503，
因此组合报告仍为 failed，没有 401。浏览器汇总继续 503，回到日志后重载可显示两条 Kimi
请求、保存筛选和已反馈标志，已检查 DOM 与实际截图；不把重载恢复当作传输故障已修复。

`prd147-auth-admission-facts-after-r1.json` 与 before 一样为两条请求、三条解释、一条反馈和
一个事件，quick_check 正常、无外键违规。权限读取预算、浏览器间歇错误、账务 owner_invalid
以及协调器旧失败归因仍是未完成项。所有事实保留，未新增收费模型调用。

独立敏感审计入口为 `prd-147-auth-admission-sensitive-audit-r1.json`，核对六项暂存源码/文档、
本批原始证据的严格 UTF-8、敏感模式和受保护基线；精确 Kimi 凭据/主密钥扫描另记为
`prd147-auth-admission-key-audit-r1.json`，不把密钥放入源码、测试报告或聊天回复。

### 提交前的第二阶段锁预算修正

代码复核发现两阶段复用一个 SQLite deadline scope 时，busy_timeout 仍是第一阶段开始时
的剩余值。修正为每阶段分别进入既有 helper，传入同一个绝对 deadline，第二次写锁准入
按已消耗的时间重新计算；不调整任何产品超时配置。

`prd147-auth-admission-budget-red-r1.xml` 以实际持锁复现，160ms 总测试预算下耗时约
297ms。`prd147-auth-admission-final-r1.xml` 四项集成通过，单项墙钟测试仍因 250ms 耗时
超过 210ms 断言而失败，不能宣称端点 SLA 已达标。最终预算传播测试改为同时观察 SQLite
实际接收的 PRAGMA：600ms 测试预算先消耗 300ms，写锁预算必须比初始值少至少 250ms，
另保留宽松墙钟上界检查调度迟延。此处验证配置传播，不以测试墙钟容差替代产品 250ms。
`prd147-auth-admission-budget-final-r2.xml` 一项通过，原失败报告保留。

最终敏感审计另存 `prd-147-auth-admission-sensitive-audit-r2.json`，增加独立锁预算单元测试，
共七项源码/文档；精确凭据扫描另存 `prd147-auth-admission-key-audit-r2.json`，不覆盖初轮。

最终 `prd147-feedback-native-auth-admission-r2-0310744295e44b05aea4d2976380ce3e.xml`
四项通过，覆盖两阶段触达、阶段间撤权、旧快照拒绝重判和 PostgreSQL 锁预算；测试库已退出。
该定向重验不覆盖、更不推翻 r1 的汇总恢复失败。

最终版本经原启动脚本加载至控制服务 PID 10100，日志为 `.logs/control-auth-admission-r2.*.log`。
`prd147-contention-runtime-predeploy-r2.json` 的健康采样与重启窗口重叠，控制健康请求超时、
观测过期，保持 failed，不能作为独立部署前健康结论。正式服务
`prd147-auth-admission-live-replay-r2.json` 八次反馈重放、日志和 SavedFilter 读取均 200，
反馈摘要及两条请求/三条解释/一条反馈/一个事件保持不变；汇总三次 200、五次 503，组合
报告仍为 failed，无误撤销、无新增模型调用。最终短时 runtime-after-r2 仅证明所采窗口，
不作为累计队列故障或 72 小时长稳的完成依据。

## 2026-09-09 调度观测隔离与汇总联合复核

本批保留调度租期、续租阈值、业务提交 fence 和 Summary 的 250ms 绝对预算。新增固定
操作/阶段/整数 SQLite 错误码诊断，不记录异常正文或凭据；观测使用独立单线程，最多一个
在途快照，统计按采样时刻冻结，关闭等待实际线程退出并按顺序写入零活动终态。

证据均在 `.artifacts/g6-security-20260907/`，失败不被后续通过覆盖：

- `prd147-scheduler-diagnostics-r2.xml`：33 项通过，含真实 BEGIN 锁冲突、写入触发器失败、
  延迟外键提交失败、取消传播、线程排队耗时和日志敏感哨兵。
- `prd147-observation-isolation-r1.xml`：36 项通过；r2 为 37 项通过，新增采样时间不被
  完成时间替代。真实 SQLite 租约跨原到期点保持同 generation，慢观测不积压，终态为零。
- `prd147-feedback-native-scheduler-observation-isolation-r1-6ec7a00a0fae414299d80f397be2c715.xml`：
  原生 PostgreSQL 真实观测行锁、持续续租和最终零队列 1 项通过，实例已停止。
- `prd147-reliability-batch-sqlite-r1.xml`：68 项中 67 通过、1 失败；失败为
  `test_feedback_permission_read_failure_preserves_session[1]` 的故障消除后 Summary 仍 503。
  会话保留与不可变事实断言已通过，不能把恢复请求失败改为通过或增加预算。
- `scheduler-summary-diagnostic-feedback-r1.json`：8 次原反馈重放、日志、筛选和 Summary 均
  200，反馈事实摘要一致。诊断进程后续 `scheduler-summary-pre-isolation-r1.json` 仍记录
  accepted=189、dropped=126、backend_failure=30，报告 failed。
- `scheduler-summary-isolation-runtime-r1.json`：24 次采样、72 次健康 HTTP 全部通过，
  63 队列且零丢失/后台失败，只证明该短窗口。该版本随后补充观测采样时间的语义修正。
- 最终版本 `scheduler-summary-isolation-runtime-r2.json`：12 次采样、36 次健康 HTTP，
  后续 dropped=63、backend_failure=18，报告 failed；实际续租耗时捕获到 8,828ms。
- `scheduler-summary-isolation-feedback-r2.json`：8 次反馈重放均 200 且同一反馈，日志和
  SavedFilter 均 200；Summary 4 次 200、4 次 503，组合 failed，历史事实摘要不变。
- `scheduler-summary-isolation-facts-r2.json`：仍为 2 请求、3 解释、1 反馈和 1 事件，
  quick_check=ok、外键违规 0，无新增模型调用。

浏览器直接打开 `/console.html#feedback-summary` 后可显示 1 条反馈、平均 5/5、采纳率
100%，但页面仍有“当前角色不可访问该入口，已回到概览”的不正确提示，作为未完成问题保留。
本轮未改变页面代码，也没有将浏览器一次成功认定为 Summary SLA 已通过。

最终本地 Control PID 1948，日志 `.logs/control-api-scheduler-isolation-r2.*.log`，Gateway
和主页服务未重启。30 秒非阻塞采样共 986 样本、1 次采样错误，原始
`scheduler-isolation-contention-r2.speedscope.json` 及分析 r3 保留。console-experience、
commercial-accounting 等后台 claim 和观测仍等待 BEGIN IMMEDIATE；采样未捕获租约实际
失去的时刻，不能据此证明某单一工作者持锁导致全部故障。分析 r2 包含工作区标准库，r3
已改用模块路径过滤，不将初版分析用于根因结论。

本次隔离仅修复非权威观测拖延续租的执行依赖。共享写锁竞争、Summary 间歇 503、导航提示、
历史计量 owner 错误及完整 G6/G8/72 小时证据仍未完成，完整目标保持进行中。

原生联合结果 `prd147-feedback-native-scheduler-summary-batch-r1-196b60ab1b2e493f9c574d5dfe8c83b6.xml`
为 23 项中 22 通过、1 失败，耗时 782.917 秒。最终采样时间/观测行锁隔离、原生租约 fence、
分组续租原子性和等待期间到期拒绝均通过；失败为反馈创建/重放/跨域流程末段的 Summary 503。
两个本轮原生实例已正常停止，postmaster.pid 均不存在。生产模块与新增原生测试 Ruff 全规则、
三项 Python 文件 compileall、git diff --check 通过；单元文件保留既有长行，仅忽略 E501 后
其余 Ruff 规则通过，不宣称该历史文件全规则无误。独立敏感审计见同目录专门记录及其 JSON。

## 2026-09-09 控制台空轮询只读准入

控制台和导航领取增加不持有写锁的资格预检。读取游标和本次读事务在 BEGIN IMMEDIATE
之前结束；写事务内仍重新取时间、读取资格并执行原 token/generation 条件更新。
入口已有调用者事务时失败关闭，不回滚该事务。没有改变商业调度、租期或 Summary 250ms。

证据均在 `.artifacts/g6-security-20260907/`：

- `prd147-idle-claim-red-r1.xml`：22 项中 12 通过、10 失败。真实 SQLite 持锁使 8 个
  空轮询失败，2 个用例证实旧代码会回滚调用者事务。未删失败证据。
- `prd147-idle-claim-green-r1.xml`：22 项全部通过。覆盖不存在/禁用/冷却/占用、独立
  写锁、并发禁用/延期/领取/删除、新时间/generation、精确到期边界及读取失败。
- `prd147-idle-claim-batch-r1.xml`：81 项中 60 通过、21 个夹具错误，356.348 秒。
  新测试与原 37 项协调器测试均通过。21 个 API 夹具仍要求历史迁移版本，尚未进入业务断言。
- PRD-145/146 独立修正迁移夹具后，`prd147-idle-claim-api-r2.xml` 22 项全部通过，
  76.362 秒。完整业务断言保留，8 条既有 record_property/xunit2 警告未抹去。
- `prd147-feedback-native-idle-claim-r1-edac1ec5b3c94dca974ca838de40ad2b.xml`：2 项
  PostgreSQL 实际已迁移库测试通过，149.859 秒。验证隐式读事务已结束、并发禁用重读拒绝、
  成功领取与后续空轮询、原调用者事务不被回滚。隔离实例已退出，postmaster.pid 不存在。
- `prd147-idle-claim-predeploy-r1.json`：旧进程 accepted=252、dropped=189、后台失败 48，
  状态 failed，保留旧累计事实，不以进程重启清零替代稳定性证明。
- `prd147-idle-claim-feedback-r1.json`：新进程 8 次已有反馈回放均 200 且同一反馈，
  日志与 SavedFilter 均 200；Summary 5 次 200、3 次 503，整体 failed。仍为 2 请求、
  3 解释、1 反馈、1 事件，反馈事实哈希前后一致，付费模型调用数为零。
- `prd147-idle-claim-runtime-r1.json`：12 次采样、36 次健康 HTTP 全部通过，63 队列，
  窗口内 dropped/backend_failure/shutdown_timeout 均为零，观测新鲜度符合原检查。
  这是本地短窗口，不能覆盖失败回放或作为 72 小时证据；日志仍有慢续租与计量对账错误。

本地仅通过原启动入口重启控制 API，PID 19220，日志 `.logs/control-api-idle-claim-r1.*.log`。
主页/Gateway 没有重启。生产模块与本批四个测试文件 Ruff 全规则通过，git diff --check 通过。
空轮询减少无效写锁请求已证明，但共享写锁的全部根因、Summary SLA、导航误提示、计量对账
以及 G6/G8/72 小时仍未完成。独立审计见 `PRD-147-idle-claim-sensitive-audit-20260909.md`。

## 2026-09-09 Control 路由启动准备

统一 supervisor 在原 lifespan 和队列启动前准备新 FastAPI 的惰性路由上下文；旧框架
不存在该 API 时沿用立即构建路径。只遍历元数据，不调用 endpoint、鉴权依赖或数据库。
生产差异限 supervisor，未改路由注册顺序、Summary 250ms、Cookie、权限或队列 fence。

证据位于 `.artifacts/g6-security-20260907/`：

- `prd147-route-preparation-red-r1.xml`：原实现 1 passed / 3 failed，验证没有启动准备、
  准备错误不能阻断启动、后续生命周期未遍历当前图。失败原件保留。
- `prd147-route-preparation-green-r1.xml`：路由准备与原调度注册/执行联合 61 passed。
- `prd147-route-preparation-green-r2.xml`：增加实际 Control 完整图后 62 passed。
  540 条路由上下文全部准备，随后禁止任何新依赖构建仍能生成 OpenAPI；未进入生产
  lifespan。嵌套隐藏路由、真实 ASGI 403、422、参数解析和逆序关闭均通过。
- `prd147-route-preparation-compat-r1.xml`：隔离目录安装 FastAPI 0.115.0 与 Starlette
  0.38.6，实际旧版本嵌套及直接路由 2 passed / 2 deselected；保留 anyio 已导入的
  pytest 重写警告。没有替换本地服务依赖，也不声称全版本兼容矩阵完成。
- `prd147-route-preparation-runtime-before-r1.json`：旧进程短观察 failed，最新累计
  dropped=129、backend_failure=17。进程重启不抹去这些历史错误。
- `prd147-route-preparation-predeploy-r1.json` 与 postdeploy-r1：13 组账务事实行数和
  SHA-256 完全一致，quick_check=ok，外键违规 0，三个 readiness/主页响应均 200。
  仍有一条 owner_invalid failed outbox、一条 pending 和 checkpoint=0，未伪造补账。
- `prd147-route-preparation-feedback-r1.json`：8 次原反馈幂等重放、16 次日志/筛选及
  8 次 Summary 均 200，Summary 客户端耗时 140-172ms；事实未变，自己的会话注销
  200，没有新增模型调用。单轮成功不能推翻 PRD-049 的历史间歇失败。
- `prd147-route-preparation-runtime-r1.json`：12 个样本、36 次健康 HTTP 全部 200，
  63 个队列，dropped/backend_failure/shutdown_timeout 均为 0，观察新鲜度通过。
  这是一个短窗口，未声称 72 小时或全部业务稳定；report 的 working-tree 标记对应
  本条独立暂存审计的源码字节，不是已认证的 GA revision。

仅重启经 PID、创建时刻、路径和命令核对的本地 Control，新 PID 7044，启动时间
2026-09-09 12:47:09。日志为 `.logs/control-api-route-preparation-r1.*.log`，确认 startup
complete；Gateway、Console 与凭据配置不动。Ruff 全规则、compileall、diff check 通过。
受保护 baseline 与复制输入哈希不变。独立敏感审计见本轮 route-preparation 记录。

本轮只移除路由结构冷准备的请求开销，未修复全部数据库争用或证明关键页面 p75。
完整 G6/G7/G8、真实跨域流程及交付后的独立 72 小时观察仍未完成。
