# PRD-077 只读管理员 AI 助手测试记录

状态：**已完成**（2026-08-12）。两轮编码前设计自审、实现期复审、编码、专项与必要回归、0070 真实
迁移往返、Runtime、PAGE-107 Browser、文档和提交前安全门禁均已完成。该结论只关闭 PRD-077；
`all_108_prds_complete=false`。

## 1. 权威范围与设计门禁

- Excel 的 `01_PRD总表` 第 78 行、`03_M2-M3扩展交付` 第 14 行、PAGE-107、API、
  AssistantQuery/AssistantAudit、`assistant.queried` 与 TC-0153/0154 已和 README、`docs/prd`、
  `docs/testing` 交叉确认，本条权威范围是
  `PRD-077 | M2-企业增强 | P2 | 扩展层 | Admin AI助手 | 只读管理员AI助手`，不是仅按编号推断。
- 设计见 `docs/prd/PRD-077-read-only-admin-ai-assistant.md`。两轮编码前自审冻结了只读动作闸门、
  四类确定性查询、typed claim/source 图、来源域 RBAC、tenant-first SQL、动态撤权、原子审计、
  outbox 恢复、PAGE-107 owner 生命周期和隐私边界。
- v1 使用本地确定性只读引擎，不调用外部大模型，不保存问题或回答正文，不提供任意 SQL、URL、工具调用
  或配置修改能力。模型调用数、输入/输出 Token 和模型成本固定为 0，但每个成功与可持久化失败 attempt
  仍须可追踪。
- 本条不新增或改变默认上游。智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务及客户
  本地 OpenAI-compatible Endpoint 继续遵守既有边界；OpenRouter、硅基流动仍只是竞品与迁移兼容对象。

## 2. 已通过的自动化门禁

以下结果来自冻结源码上实际执行的专项或必要回归。各分组之间存在包含或相邻合同关系，**不得把数字相加**
冒充一次根级全量。

| 验证项 | 已通过结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared / 0070 / outbox / Control 组合 | `145 passed` | parser、claim/source、RBAC revision 13、0070、source/outbox、四类 API、tenant/RBAC、deadline、并发、撤权、原子审计与生命周期 |
| Python Runtime 验收器合同 | `71 passed, 1 skipped` | 仓库边界、provenance、迁移/隐私、证据原子发布和 owner-safe cleanup；当前主机无法创建 junction 的夹具按平台条件跳过 |
| Python Browser harness 合同 | `31 passed` | 固定 loopback、私有环境、服务/数据库 owner、原子发布、隐私扫描和失败批次隔离 |
| Node Browser 合同 | `64/64 passed` | ARM64 Chrome/PE 门禁、PAGE-107 网络/证据合同、Windows 句柄/长路径、身份竞态和清理 |
| Console 全量 | `750/750 passed` | PAGE-107 及全部既有 Console 页面、owner/generation、权限、健康重试和响应式合同 |
| Console lint | `236 files` | 全部 Console 源码与测试静态门禁 |
| Admin Auth 相邻回归 | `4 passed` | 普通认证、Session 状态、统一认证错误及默认 `last_seen` 行为 |
| Basic RBAC Users 相邻回归 | `4 passed` | 基础角色、用户 owner、后端权限与租户边界 |
| Feedback 相邻回归 | `5 passed` | 既有 deadline/认证调用兼容及反馈治理边界 |
| PRD-059 Auth Permissions 相邻回归 | `11 passed`；修正节点 `1 passed` | revision/matrix、角色权限、动态授权及本次兼容修正节点 |
| Seed lifecycle 定向回归 | `1 passed` | 新租户 revision-13 默认矩阵与既有 seed 生命周期 |

核心复验集合如下。最终测试记录只能填写真实终端结果，不得用收集数、估算数或不同工作树结果替代：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  shared-sdk\python\tests\test_prd077_admin_assistant.py `
  control-plane-api\tests\integration\test_prd077_db_migrations.py `
  control-plane-api\tests\integration\test_prd077_admin_assistant_outbox.py `
  --basetemp .artifacts\prd077-shared-migration-outbox-final

.\.venv\Scripts\python.exe -m pytest `
  control-plane-api\tests\integration\test_prd077_admin_assistant_control.py `
  --basetemp .artifacts\prd077-control-final

.\.venv\Scripts\python.exe -m pytest `
  scripts\acceptance\tests\test_prd077_runtime_acceptance.py `
  scripts\acceptance\tests\test_prd077_browser_harness.py `
  --basetemp .artifacts\prd077-acceptance-contracts-final

& .\.tools\node-v22.23.2-win-arm64\node.exe --test `
  console-web\tests\unit\prd077AdminAssistant.test.mjs `
  scripts\acceptance\tests\test_prd077_browser_acceptance.mjs
```

## 3. Migration 0070 自动化合同

- `0070_read_only_admin_ai_assistant.sql/.down.sql` 把最高迁移提升到 `70/0070`，RBAC catalog revision
  提升到 13，并新增 tenant-qualified AssistantQuery、AssistantAudit、event source/outbox 及 AuditLog owner。
- 自动化专项已经覆盖 fresh/repeat、0069 升级、空业务库 down/re-up、默认角色 grant、自定义矩阵保真、
  Query/Audit/AuditLog owner、不可变 trigger、outbox CAS、错租户 FK 和业务事实 down guard。
- AuditLog 既有 rowid、export sequence、摘要和 0069 schema/index/trigger 必须逐项保真；catalog、canonical
  schema 或 trigger 即使保持相同数量但发生漂移，也必须在任何部分提交前原子失败。
- Runtime final 已在同一个空 SQLite 上实际执行 fresh、repeat、down、re-up：
  `70/0070 -> 70/0070 -> 69/0069 -> 70/0070`。repeat applied count 为 0；四阶段均为
  `integrity_check=ok`、`foreign_key_check=0`。

## 4. Control、权限与并发覆盖

- TC-0153 四类查询 `usage_statistics`、`error_explanation`、`cost_advice`、`policy_explanation` 已覆盖；
  每个 claim/recommendation 必须引用当前 tenant 的 typed source，href 只能来自内部 allowlist。
- admin 可执行全部四类查询；finance 仅可读取用量/成本域；security 仅可读取错误/策略域；developer、
  viewer、ops_admin 不因前端入口或角色名称获得 base permission。所有权限由后端 Session、实时角色矩阵
  和来源事务复核，客户端 tenant/header 与隐藏按钮均不构成授权。
- base permission、来源 grant 和 authorization fingerprint 在认证后、只读快照和最终写事务前发生动态
  撤销时，均不得返回迟到回答；可持久化拒绝写零 claim/source 的 Query/Audit/AuditLog/event 图。
- raw ASGI Header、重复 Authorization、重复/未知 JSON key、非法 Unicode、超长/截断 body、未知意图、
  多意图/多 request ID、禁止动作、跨租户和不存在来源均使用固定错误码，并保留统一 `request_id`、
  `trace_id` 与 private no-store Header。问题、回答、Token、Provider 错误和策略表达式不进入错误或日志。
- 每个 app/event loop 只延迟建立两条长期 SQLite 连接：认证/来源 read-stage 与最终/终态 write-stage。
  两条连接使用独立有限 admission、WAL/FULL 和剩余绝对预算；read-stage 的 `query_only`、事务和 progress
  handler 以及 write-stage 的 `BEGIN IMMEDIATE`、回滚和 handler 均须在成功、失败、超时和取消后清理。
- middleware 入口冻结 endpoint 1.5 秒和 source 1.2 秒绝对期限。认证、正文、tenant owner、admission、
  SQLite 锁等待、SQL、Python 映射和最终审计不得刷新期限；cold WAL 初始化锁冲突必须在 source deadline
  内固定失败，并在锁释放后恢复。
- `last_seen` 只在最终治理事务内与 Query/Audit/AuditLog/outbox 原子提交；治理事务回滚时不得留下伪成功
  Session 活动痕迹。普通非 PAGE-107 认证继续保持既有行为。
- 已启动的同步 worker 即使客户端取消也必须持有 slot 并等待真实线程退出，不能迟到提交或重开已回收连接；
  lifespan 先等待 scheduler，再回滚关闭 runtime，BackgroundTasks 派发不得阻塞并发 `/health`。
- 20 个并发 HTTP attempts 允许因 source 绝对期限产生固定、已审计的
  `admin_assistant.source_unavailable`，但 HTTP 200 与该 503 数量之和必须为 20，数据库必须精确存在
  20 组唯一且一对一的 Query/Audit/Event/AuditLog，不能合并、覆盖或丢失 attempt。

## 5. Outbox、Webhook 与隐私合同

- `assistant.queried` 使用稳定 event ID。pending claim、90 秒 lease、5/30/300 秒退避、最多 10 次 attempt、
  stale lease reclaim 和 tenant/event/token/owner/attempt CAS 已覆盖；迟到 worker 不能覆盖新 owner 终态。
- 第十次可重试失败必须原子关闭 source 和 pending delivery；损坏 envelope、owner/token/attempt/lease 篡改
  必须隔离，不能饥饿后续 tenant，也不能把数据库或 Endpoint 细节写入日志。
- Webhook payload 只允许固定 schema/version、query type、outcome、计数和 duration bucket；不得包含问题、
  回答、目标 request ID、金额、Token、Provider Key、策略身份、URL、Prompt 或 Response。
- Control 停服后的 33 个 pending source 已由新 Control lifespan scheduler 恢复；网络调用始终在数据库
  事务外，最终 pending/processing、failed 与 forbidden/invalid Webhook 均为 0。

## 6. Runtime 与 Browser 验收器合同

- Python Runtime/Browser harness 合同和 Node Browser 合同已验证固定仓库根、固定 loopback、run-id/path
  边界、source provenance、证据 schema、原子 staging/publish、manifest SHA 绑定、失败批次删除及
  owner-safe 进程/目录清理。
- 隐私 oracle 会扫描数据库、日志、stdout/stderr、Browser profile、work/staging 和待发布 artifact 的 raw、
  URL 编码、base64、hex 与 UTF-16 形式；任何 Provider Key、Token、Password、问题、回答、Prompt、
  Response、`.env` 内容、随机 canary 或 SQLite sidecar 命中都必须失败并删除本轮证据。
- Browser 合同固定 `1280x720` 与 `390x844`，覆盖 PAGE-107 admin 主流程、finance/security 权限、来源链接、
  只读阻断、动态撤权、取消、迟到响应和登出清理；检查非空像素、横向溢出、文本溢出、重叠、外部请求、
  非预期 Console error 和私有 storage/network。活动问题与回答只在内存断言，不进入持久截图。
- 上述验收器合同与第 8 节真实 Runtime/Browser final 均已通过，二者分别证明安全执行器和实际页面行为。

## 7. 已通过的相邻兼容回归

- Admin Auth `4 passed` 与 Basic RBAC Users `4 passed` 证明 `authenticate_bearer` 的可选共享连接、
  deadline/progress handler 和延迟 `last_seen` 没有改写普通 API 的 Session、错误或租户语义。
- Feedback `5 passed` 证明既有 endpoint deadline 和认证调用合同保持兼容。
- PRD-059 Auth Permissions 已通过 `11 passed`，随后对本次修正节点再次通过 `1 passed`；最终记录必须保留
  两次结果的先后关系，不能把它们虚构为一次 `12 passed` 全量。
- Seed lifecycle 定向节点 `1 passed`，证明 revision-13 默认矩阵用于新租户，同时不覆盖既有自定义矩阵。

## 8. 最终门禁记录

以下结果来自 2026-08-12 的冻结源码。中文注释门禁发现 Browser driver 的五处说明缺口后，先补注释并
重跑 `64/64` Node 合同，再以新 run-id 生成 Browser final；旧 `-04` 批次不再白名单、不作为权威证据。

| 项目 | 完成条件 | 最终记录 |
| --- | --- | --- |
| 0070 真实迁移 | 同一空库 fresh/repeat/down/re-up；四阶段 migration count、integrity ok、FK 0 | `70 -> 70 -> 69 -> 70`；repeat applied 0；四阶段 integrity ok、FK 0 |
| Runtime final | 实际启动 Control；四类查询、角色/租户、只读阻断、并发、失败、停服恢复、队列归零全部通过 | `.artifacts/prd077-runtime/prd077-final-20260812-04/`，`status=passed`；20 并发为 9 个 200、11 个受控 503，20 组治理图唯一；停服恢复和队列归零通过 |
| Runtime provenance/SHA | 唯一 final 目录、JSON SHA-256 与冻结源码/0070 pair 一致 | `runtime-acceptance.json` SHA-256 `c4903b6c542051164d8ce9298b61581620423ac9782141e9d9a1340be9993f4b`；`SHA256SUMS.txt` 匹配 |
| Browser final | 实际 PAGE-107 双视口、权限/撤权/迟到/登出、像素和布局、网络与 Console 门禁通过 | `.artifacts/prd077-browser/prd077-browser-final-20260812-05/`，`status=passed`；6 次助手 200、3 次 403，外部请求和非预期 Console error 为 0 |
| Browser manifest/SHA | 全部 manifest 项与文件 SHA-256 匹配；截图均经人工复核 | 4 个 payload 文件的名称、字节数和 SHA-256 独立重算全匹配；manifest SHA-256 `d347474897d3c2eeceb2fc4af73b115758a83a94101819c1757ac9b81e2b55c0`；2 张截图人工复核通过 |
| Python 静态门禁 | 受影响文件 Ruff、compile 和中文注释原规则通过 | 18 个新增 Python 文件 Ruff/format 通过；36 个受影响文件 `py_compile` 通过；全仓中文注释门禁通过；248 个 Ruff E501 均定位到未改历史行，PRD-077 diff 命中 0 |
| Console 最终门禁 | PAGE-107、必要 Console 回归和 Console lint 使用冻结源码通过 | Console `750/750 passed`；lint `236 files`；Node Browser `64/64 passed` |
| 仓库一致性 | `git diff --check`、迁移编号、README、目录说明、PRD/API/testing 交叉一致 | 通过；最高迁移 `70/0070`，文档均保持 `all_108_prds_complete=false` |
| 敏感信息审计 | Provider Key、Token、Prompt、Response、Password、`.env`、随机 canary 多编码命中 0 | Runtime、Browser profile、work/staging、进程输出和提交候选审计均为 0 |
| 禁止文件与清理 | DB、WAL/SHM/journal、日志、profile、`.tmp`、未引用截图、大文件、端口和 owner 进程残留 0 | 提交候选中禁止文件 0；Browser work/staging 不存在、owner process 0、四个受控端口 listener 0；仅白名单 7 个脱敏 final 文件。客户端本地删除策略拒绝清理本轮两个已忽略 basetemp，未进入 Git、证据或服务 owner 范围 |
| 最终复审 | 两轮只读复审关闭阻断项；仅白名单 PRD-077 文件和脱敏小型 final 证据 | 通过；业务源码冻结后未再修改，Browser 注释修正已用新 final 重新绑定 provenance |

Runtime final 的隐私扫描覆盖 9 个受控文件，明文命中 0、SQLite sidecar 0；Browser final 扫描 190 个
profile 文件和 8 个 work 文件，明文命中均为 0。两套证据均记录 `owned_processes_remaining=0`，且
`all_108_prds_complete=false`。

最终测试命令使用的 `C:\MetaHub\.tmp\prd077-final-gates-20260812-05` 与 `-06` 仅含本轮 pytest/compile
缓存；精确路径删除命令被 Codex 客户端本地破坏性操作策略拒绝，未执行也未申请授权。这两个目录受
`.gitignore` 的 `.tmp/` 规则保护，不属于提交候选或权威验收证据；未触碰无法证明所有权的其他 `.tmp` 目录。

## 9. 当前结论与下一项

PRD-077 的设计、两轮自审、实现、专项、必要回归、迁移、Runtime、Browser、文档和提交前安全门禁均已
闭环，可以标记为完成。该状态不提前启动或混入 PRD-078。

Excel 的 `01_PRD总表` 第 79 行、`03_M2-M3扩展交付` 第 15 行、PAGE-108/109、审批 API、ApprovalTask、
`approval.created`/`approval.timeout` 和 TC-0155/0156 共同确认，PRD-077 完成后的下一条权威未完成项是
`PRD-078 | M2-企业增强 | P2 | 扩展层 | 移动审批 | 移动端/企微钉钉审批入口`。本次不启动或实现
PRD-078；`all_108_prds_complete=false`。

## 2026-09-08 共享数据库生命周期修复

PRD-147 租约回收暴露旧 teardown 错误地维护整个共享 WAL 的问题。现在只等待自身在途
attempt/slot，回滚、重置并独立关闭两条连接，不执行 checkpoint、切换日志模式或删除 sidecar。
外部 reader/writer 仍可工作，成功关闭后同一 app/loop 可重新初始化；实际 close 失败仍保留
closing 状态并返回原有固定错误，取消仍等待资源 owner 释放。

`.artifacts/g6-security-20260907/prd077-shared-teardown-red-r1.xml` 的两项真实 WAL 反例均失败；
`prd077-shared-teardown-r2.xml` 为 5/5，覆盖外部空闲/活动快照、未提交写入回滚、重新初始化、
取消与关闭失败。旧 checkpoint 故障测试改为证明子系统完全不发全库 checkpoint，并未删除
实际连接关闭故障测试。静态 E4/E7/E9/F/I 与 diff-check 通过。不是完整业务、原生或长稳复验。
独立审计为 `prd-077-shared-database-teardown-sensitive-audit.json`；全目标仍继续推进。
