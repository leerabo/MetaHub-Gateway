# PRD-144 测试记录

状态：已完成。0136、平台商业账务、Workspace 订阅与内部 FinOps 分域、ProviderCostRate、
BillingDomainReconciliation、权限导航和三浏览器证据均已交付。下一权威项为
`PRD-145`；完整 G7 仍按批次约定在 PRD-147 完成后统一执行。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0136 Migration | 0135 -> 0136、pristine down/re-up、目标事实 down guard、复合 owner/FK、商业权限、不可变对账快照/差异、10 万差异覆盖索引 |
| Control 商业核算 | Platform/Workspace credential 分域、套餐/参考价/订阅/Invoice/争议/成本率、CAS/幂等/step-up、动态权限、事件/outbox、对账冻结与重放 |
| Gateway | Workspace ProviderCostRate resolver、LKG/unknown、PriceSnapshot、内部账 owner 与平台建议价隔离 |
| Console/导航 | Platform Commercial Accounting、Workspace Subscription、Internal FinOps 独立 client/store/epoch、权限裁剪、旧入口待卸载、响应式布局 |
| SLO | 两端真实会话鉴权查询 p95、商业争议提交到双端投影与 outbox ACK、100,000 条冻结差异 owner/金额/hash/watermark |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| 0136 Migration | `2/2 passed`；fresh、pristine down/re-up、目标事实回滚阻断均通过 |
| Control 商业核算 | `6/6 passed`；模型、router、端到端命令、supervisor、对账行为与 SLO |
| Gateway 商业核算 | `4/4 passed` |
| PRD-144 Console | `7/7 passed` |
| 受影响 Console 回归 | `42/42 passed`；权限、PRD-094/095/100/139/143 |
| 后端导航与 Workspace Access | `12/12 passed` |
| Console lint | `console lint ok (457 files)` |
| Python static | PRD-144 新增/修改 Python 的 Ruff 全规则通过 |
| Diff 格式 | `git diff --check` 通过 |
| 机器可读汇总 | `.artifacts/prd144-tests/test-summary.json` |

## 权威计划回写

- `00_变更说明!B5` 已更新为 PRD-090..144 完成、下一项 PRD-145；
  `saas_incremental_roadmap_complete=false` 与 `all_108_prds_complete=false` 均保持不变。
- `01_PRD总表!B56` 已更新为“已完成”，PRD-145 仍为“未启动”。
- 根目录权威工作簿与
  `outputs/prd144-roadmap-closeout/企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx`
  字节一致，SHA-256 均为
  `e01f764e6cfe1f0f658fa7f01cdac92a0188d84a3276d96a73c650fe16bedbbd`。
- 使用 `@oai/artifact-tool` 从磁盘重新导入两份工作簿，均确认 12 个工作表、状态值一致且公式错误
  为 0；全部工作表按长表分片形成 26 张后置 PNG，另有 2 张关键范围图，解码、尺寸和非空像素检查
  均通过。
- 本地图片查看器和 computer-use 浏览器状态获取均连续超时，因此未将人工目视复核记为完成；
  正式结论只使用工作簿重导入、公式扫描、渲染成功和 PNG 自动检查。

核心命令：

```powershell
$env:PYTHONPATH='C:\MetaHub\shared-sdk\python;C:\MetaHub\control-plane-api\src'
.\.venv\Scripts\python.exe -m pytest control-plane-api\tests\integration\test_prd144_db_migrations.py -p no:cacheprovider -q
.\.venv\Scripts\python.exe -m pytest control-plane-api\tests\integration\test_prd144_commercial_accounting.py -p no:cacheprovider -q

$env:PYTHONPATH='C:\MetaHub\shared-sdk\python;C:\MetaHub\gateway-data-plane\src;C:\MetaHub\control-plane-api\src'
.\.venv\Scripts\python.exe -m pytest gateway-data-plane\tests\integration\test_prd144_commercial_accounting_gateway.py -p no:cacheprovider -q

node --test console-web/tests/unit/prd144CommercialAccounting.test.mjs
node --test console-web/tests/unit/permissions.test.mjs console-web/tests/unit/prd094WorkspaceAccess.test.mjs console-web/tests/unit/prd095PlatformAccess.test.mjs console-web/tests/unit/prd100PlatformInfrastructure.test.mjs console-web/tests/unit/prd139ChannelManagement.test.mjs console-web/tests/unit/prd143ProviderEngineering.test.mjs

.\.venv\Scripts\python.exe -m pytest control-plane-api\tests\unit\test_prd144_console_navigation.py control-plane-api\tests\integration\test_prd094_workspace_access.py -p no:cacheprovider -q
```

各 Python 命令使用位于 `.tmp` 的独立 `TEMP/TMP/--basetemp`；正式结果落入
`.artifacts`，临时数据库与 pytest 目录不作为交付证据。

## SLO 证据

正式记录：`.artifacts/prd144-slo/slo-results.json`，SHA-256
`ad4fbc231b4c0cee44283ff83ca2be7c9f77bdcf80b342f7803bf55abbe416bd`。

| 指标 | 实测 | 固定门槛 |
| --- | ---: | ---: |
| Platform 订阅查询 nearest-rank p95，30 个 warm 样本 | `270.182 ms` | `<500 ms` |
| Workspace 订阅查询 nearest-rank p95，30 个 warm 样本 | `82.739 ms` | `<500 ms` |
| 商业争议提交到 Workspace/Platform 投影并完成 outbox ACK | `1.831245 s` | `<5 s` |
| 100,000 条冻结 reconciliation difference 完整 wrapper | `15.920618 s` | `<600 s` |

查询样本使用运行时随机 secret 生成、只存在于临时数据库的真实 PlatformSession/WorkspaceSession
哈希，进入与生产 GET 相同的 `database_request_scope(reuse_connections=True)`；计时包含真实会话恢复、
角色/权限指纹、owner 过滤、Invoice/Dispute 批量投影和 DTO 构造。

传播计时从 Workspace 争议命令开始，包含真实 Workspace 鉴权、source owner/金额/revision/hash 反算、
同事务 event/outbox、subscription projection refresh、commercial supervisor claim/dispatch/ACK，以及
Platform 与 Workspace 两端真实鉴权读取。成功后 `billing_dispute.submitted` outbox 状态为
`succeeded`。

10 万差异测试从已完成且冻结的 PRD-104 reconciliation run 开始；PRD-144 wrapper 重新验证平台商业权限，
冻结 migration/core source watermarks、Workspace owner/currency 分组、subscription projection、
ChannelStatement、commercial event/outbox，并执行集合式差异写入与有界流式结果哈希。断言
`row_count=100000`、`owner_coverage_count=100000`、USD 原币差额整数和
`50050000`；result SHA-256 为
`e3991e3a8e2f93568bc1efa9d9089430099c054e64595c8778457c6b7a0add17`。

## 浏览器证据

正式记录：`.artifacts/prd144-browser/browser-acceptance.json`，SHA-256
`7c2b827e6ca119f0139f3cb6953abc98b6253d47c1a81aaeedb93f8a4ef714ae`。

- Chromium、Edge、Firefox x 1280x800 DPR 1、1440x900 DPR 1.25、390x844 DPR 1.5，
  共 9 次浏览器/视口运行。
- Chromium 为 Platform、Workspace Subscription、Internal FinOps 三个状态各保存三视口正式 PNG，
  共 9 张；尺寸、解码、非空像素和 sampled color 均通过。
- 执行 Platform refresh、Workspace 导航到 Internal FinOps、Internal FinOps refresh 的真实点击；
  Console/Page/Network/HTTP/external/unexpected API 错误均为 0。
- 横向溢出、控件重叠、实际 CSS clipping、键盘焦点失败、跨域/敏感 DOM 命中和 browser cookie
  均为 0。
- 本地图片查看器连续超时，因此没有把人工目视复核写成已完成；正式结论仅依据 Playwright DOM 布局、
  PNG 解码/尺寸/非空像素和结构化断言。该限制不影响已完成的自动化浏览器门禁，但保留在汇总中。

## 两轮实施自审

### 第一轮：账本 owner、迁移与不可变事实

- 发现 `billing_domain_reconciliations/source_snapshots/differences` 只有 DDL、没有可执行生产路径，
  无法证明 10 万差异 SLO。新增受 `platform.commercial.reconciliation.manage` 保护的 wrapper，
  绑定原 `reconciliation_run_id`、operator 权限摘要、source-set/result hash 和审计记录。
- 对账快照与差异补齐 `workspace_id + tenant_id` 复合 owner/FK、原币分组、状态约束、更新/删除
  immutable trigger；相同源 run 重放返回同一稳定结果，不产生 Excel 五事件之外的新事件。
- 0136 仅为旧差异表增加可回滚复合覆盖索引，不修改 0096 迁移；down 显式删除该索引，pristine
  0135 -> 0136 -> 0135 -> 0136 已验证。
- 复核平台商业账、Workspace Provider 成本/内部 FinOps 和渠道结算三套事实边界；没有跨币种汇总，
  没有把 Platform reference rate 写成 Workspace/Gateway head。

第一轮修正后无未关闭 P0/P1。

### 第二轮：真实 SLO、调度传播与界面边界

- 初测平台查询 p95 为 `688.132 ms`，定位为测试直接调用 service、绕过生产 GET 请求级 SQLite
  连接复用导致的 schema 冷打开；测试改为进入生产 request scope，未降低鉴权、owner 或 DTO 校验，
  也未放宽 500 ms 门槛。
- Commercial supervisor 的单次 claim/worker/release 现共用受控 SQLite 句柄并回收到有界 idle pool；
  传播测试改走真实 supervisor/outbox 路径，最终从 `3.199602 s` 降至 `1.831245 s`。
- Workspace 长导航项改为可换行，390px 下 `workspace-subscription` 不再被省略；三视口自动布局
  断言无溢出、重叠或实际 clipping。
- Platform/Workspace client、store、epoch、AbortController 与 permission fingerprint 分离；离页、
  切 Workspace、撤权和迟到响应继续清空金额、cursor、step-up 与 draft。

第二轮修正后无未关闭 P0/P1。

## 敏感审计与批次门禁

- 对 PRD-144 源码、测试和正式 artifact 的 17 个路径执行只报文件名的高置信扫描；private key、
  AWS key、JWT、Bearer、Provider `sk-` key 和带口令数据库 URL 命中文件数为 `0`。
- 测试会话只使用运行时随机 secret 和固定长度占位值，全部位于临时数据库；artifact 不包含 token、
  Cookie、密码、TOTP、Provider key、支付/税务正文、Endpoint 或客户内容。
- `git ls-files` 对 `.tmp/.env/PEM/KEY/P12/SQLite/DB` 禁止路径无命中。最终提交使用显式路径，
  不使用 `git add .`；未跟踪 PRD-101..105 文档和 `outputs/` 不纳入本提交。
- 本条执行 S7 聚焦门禁，不替代完整 G7。PRD-145..147 继续共享迁移、DTO、权限、scheduler 和前端
  组件；PRD-147 独立提交后再统一执行数据库、跨域集成/FLOW、三浏览器三缩放、性能、长稳、安全、
  兼容和旧入口四层归零。
