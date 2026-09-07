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
