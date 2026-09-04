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
