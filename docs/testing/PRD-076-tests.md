# PRD-076 Response Cache 可选能力测试记录

状态：**已完成**（2026-08-11）。设计与两轮编码前自审、实现、专项、必要回归、0069 正反迁移、
真实 Runtime、PAGE-106 Browser、中文注释和提交前安全门禁均已通过。该结论只关闭 PRD-076，
不表示全部 108 条完成；`all_108_prds_complete=false`。

## 1. 权威范围与设计门禁

- Excel 的 `01_PRD总表` 第 77 行、`03_M2-M3扩展交付` 第 13 行、PAGE-106、API、数据对象、Webhook 与
  TC-0151/0152 已和 README、`docs/prd`、`docs/testing` 交叉确认，PRD-075 之后的权威未完成项是
  `PRD-076 | M2-企业增强 | P2 | 扩展层 | 缓存策略 | Response Cache可选能力`，不是仅按编号推断。
- 设计见 `docs/prd/PRD-076-response-cache-optional-capability.md`。两轮编码前自审冻结了请求身份、
  data class、实验旁路、单写者 lease、generation fence、计费/节省金额、失效、恢复、迁移保真、租户/RBAC、
  Console owner 和隐私边界；两轮均已修正并关闭阻断项。
- PRD-076 不修改 PRD-050 Prompt Cache 事实，不缓存 stream、Embedding、direct concrete Model、Policy route、
  active PRD-075 Experiment scope 或不可信 data class，也不把 OpenRouter、硅基流动当作默认上游。

## 2. 已通过的自动化专项

最终门禁在同一冻结工作树上运行，测试使用 `C:\MetaHub\.tmp` 下相互独立的 pytest basetemp。分组之间存在
包含或相邻合同关系，不把数字相加冒充一次根级全量。

| 验证项 | 最终结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared 合同 | `39 passed` | identity、策略/失效合同、加密 envelope、RBAC revision 12、Webhook payload |
| Gateway 专项 | `29 passed` | eligibility、实验/Output Safety 隔离、key pool、fill/failure spool、settlement |
| 0069 迁移 | `5 passed` | fresh/repeat/up/down/re-up、保真、owner/trigger、down guard |
| Control 专项 | `9 passed` | Policy/失效 API、tenant/RBAC、CAS/幂等、Scheduler/outbox 恢复 |
| 维护工具 | `11 passed` | key rotation、停机确认、spool/schema/trigger gate、guarded down |
| Runtime 验收器合同 | `35 passed` | 路径、CLI、迁移、隐私、source provenance、失败批次隔离与清理 |
| Browser 验收器合同 | `20 passed` | 固定回环、布局、截图/manifest、动态 RBAC、revision 同步、隐私与 owner cleanup |
| PAGE-106 Console 直测 | `11 passed` | strict DTO、owner/generation、CAS/rebase、失效、RequestLog 与响应式 CSS |
| Console 全量 | `734 passed` | PAGE-106 renderer/脱敏修正及全部既有 Console 回归 |
| Console lint | `230 files passed` | JavaScript/CSS 语法、换行和仓库 Console 规则 |
| Python 定向静态门禁 | `56 files passed` | 全部 PRD-076 新增及受影响 Python 文件 Ruff 与 `py_compile` |

核心复验命令：

```powershell
.\.venv\Scripts\python.exe -m pytest shared-sdk\python\tests\test_prd076_response_cache_contracts.py
.\.venv\Scripts\python.exe -m pytest `
  gateway-data-plane\tests\unit\test_prd076_experiment_cache_disposition.py `
  gateway-data-plane\tests\unit\test_prd076_output_safety_cache_candidate.py `
  gateway-data-plane\tests\unit\test_prd076_response_cache_eligibility.py `
  gateway-data-plane\tests\unit\test_prd076_response_cache_failure_spool.py `
  gateway-data-plane\tests\unit\test_prd076_response_cache_keypool.py `
  gateway-data-plane\tests\unit\test_prd076_response_cache_models.py `
  gateway-data-plane\tests\unit\test_prd076_response_cache_settlement.py
.\.venv\Scripts\python.exe -m pytest control-plane-api\tests\integration\test_prd076_db_migrations.py
.\.venv\Scripts\python.exe -m pytest `
  control-plane-api\tests\integration\test_prd076_response_cache_control.py `
  control-plane-api\tests\unit\test_prd076_response_cache_scheduler.py
.\.venv\Scripts\python.exe -m pytest scripts\maintenance\tests\test_response_cache_maintenance.py
.\.venv\Scripts\python.exe -m pytest scripts\acceptance\tests\test_prd076_runtime_acceptance.py
& .\.tools\node-v22.23.2-win-arm64\node.exe --test scripts\acceptance\tests\test_prd076_browser_acceptance.mjs
& .\.tools\node-v22.23.2-win-arm64\node.exe --test console-web\tests\unit\prd076ResponseCache.test.mjs
```

全仓 `scripts/ci/check-comments.py` 使用原规则通过，没有修改检查器或新增豁免。56 个新增及受影响 Python
文件统一通过 Ruff 和 `py_compile`；随后再次执行 `git diff --check`。未反复执行无关根级 Python 全量。

## 3. 0069 与数据库约束

- `0069_response_cache_optional_capability.sql/.down.sql` 把迁移提升到 `69/0069`、RBAC catalog revision 12，
  新增 Cache Policy/Key Generation/Entry/Fill/Tag/Request Fact/Hit/Invalidation/Event/Outbox，并保真扩展
  Usage、Output Safety 与 Audit 的 exact CHECK、索引、trigger 和外部依赖。
- 同一个空 SQLite 依次通过 fresh `69/0069`、repeat no-op、down `68/0068` 和 re-up `69/0069`；四阶段均
  `integrity_check=ok`、外键违规 0，repeat 为 applied 0 / skipped 69。
- 业务事实存在时，维护 CLI 与 down migration 在任何 DDL 前固定拒绝；Runtime 已验证失败后数据库和 schema
  不变。只有经过显式停机、禁用、spool 清空、key-state 和 schema/trigger 复核的空事实库允许 guarded down。

## 4. 真实 Runtime 权威证据

唯一 final Runtime 权威批次为：

- `C:\MetaHub\.artifacts\prd076-runtime\prd076-final-20260811-06\runtime-acceptance.json`
- `C:\MetaHub\.artifacts\prd076-runtime\prd076-final-20260811-06\SHA256SUMS.txt`
- JSON SHA-256：`2fcfe8579b40a6d13ed9addc09193074ac036bb9d8f80ed3b12ee3dd98ffe19d`
- manifest SHA-256：`5d03e9d3658dd3c684a9224879b40fc855fdf3cd5a79d391ddd37a91b60f2ad3`

| Runtime 节点 | final 结果 |
| --- | --- |
| 服务 | Console feature/index、Control 初始/恢复和 Gateway 均为 HTTP 200 |
| Cache 主链 | free/charged miss-fill-hit、并发单 fill owner、TTL 边界 miss 和 Provider delta 0 均通过 |
| 隔离 | Prompt、Virtual Key、resolved Model、data class、tenant 与伪造 tenant header 均未复用 |
| 计费与追踪 | 3 条 hit Usage/FinOps；免费 hit 节省 2600 micros，收费 hit 计费 2600 micros；预算先于 Provider 阻断 |
| 失效 | tag、all、generation fence 与 5 条 `cache.invalidated` Webhook 均通过 |
| 恢复 | Control 停机 source、commit-unknown、failure spool、usage spool 与 outbox 重启恢复通过 |
| 数据库 | fresh/repeat `69/0069`、down `68/0068`、re-up `69/0069`，各阶段 integrity ok / FK 0 |
| 清理与隐私 | pending/processing/quarantine、leased outbox、SQLite sidecar、固定端口均为 0；148 个文件明文命中 0 |

Runtime 只使用固定回环 OpenAI-compatible Provider 和本地 Webhook，不访问或记录智谱、通义、月之暗面、
文心、火山、混元、DeepSeek 的真实凭据。Provider Key、Token、Prompt、Response 不进入日志或公开证据。

## 5. 权限、并发与失败覆盖

- 权限与租户：未认证/伪造 tenant header 拒绝，Viewer 403，Finance 动态撤权后 403 且 request_id 保留；
  read/write/invalidate 是独立后端权限，Console 导航和隐藏按钮不构成授权。
- 并发与恢复：同 identity 只有一个 fill owner；非 owner 不触发第二次 Provider；generation/lease/CAS 阻断
  迟到写入，失效、Control 停机、commit-unknown、spool/outbox takeover 都有可追踪终态。
- 安全与成本：Output Safety 阻断的 cache candidate 不收费；真实 Provider 路径继续遵循 dispute；免费和收费
  cache hit 都写 tenant/request/trace-qualified RequestLog、Usage、FinOps 与成本状态。
- 兼容：active Experiment scope、stream、Embedding、direct concrete Model、Policy route 和内部调用明确旁路，
  不改变 PRD-075、PRD-074、PRD-066 或 PRD-050 的已冻结语义。

## 6. Browser 与最终门禁

- 权威 Browser 目录为 `.artifacts/prd076-browser/prd076-browser-final-20260811-24/`，状态 `passed`；只包含
  Browser JSON、9 张 PNG 与 `SHA256SUMS.txt`。manifest 10 项全匹配，manifest SHA-256 为
  `514918fd6f4fb41de3a15b13e92567b4d288cf4c0c5fb6377610d5277a6c0976`，主 JSON SHA-256 为
  `5a924681b8233ba9bfc36a3678f704425f0103a6596cb6794707aa66d1374a84`。
- Chromium `1280x720` 与 `390x844` 覆盖 empty/configured/editing/version conflict/invalidate/viewer denied/
  RequestLog hit；9 张截图均非空，页面和目标区横向溢出、文本溢出、控件越界与非法重叠均为 0。
- UI 真实创建、更新、409 显式 rebase、标签失效与动态撤权通过；撤权后后端 write/invalidate 为 403，
  read DOM、Dialog 和私有 state 清除，恢复权限后矩阵恢复。
- Browser 外部请求、非预期 Console error、私有 DOM/storage/network 字段均为 0；运行 stdout/stderr 和
  144 个运行文件的 raw、URL、base64、hex、UTF-16 明文命中为 0。固定端口、owned process、profile、
  work/staging residue 均归零。
- 最终 9 张 PNG 已逐张人工复核；失败或过期 Browser 批次未进入 Git 白名单。Git 只允许上述 final
  Runtime/Browser 小型脱敏制品，不包含 `.env`、数据库、日志、profile 或 `.tmp`。

## 7. 当前结论

PRD-076 的设计、两轮自审、实现、测试、0069 正反迁移、文档、Runtime、Browser、隐私和清理证据均已
闭环，可标记为完成。Excel 多表交叉确认下一条未完成项为
`PRD-077 | M2-企业增强 | P2 | 扩展层 | Admin AI助手 | 只读管理员AI助手`，但本次不启动 PRD-077；
`all_108_prds_complete=false`。
