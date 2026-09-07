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
