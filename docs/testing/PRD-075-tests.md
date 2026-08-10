# PRD-075 模型 A/B 测试与质量评估测试记录

状态：**已完成**（2026-08-10）。设计与两轮编码前自审、实现、专项、必要回归、0068 正反迁移、
真实 Runtime、PAGE-105 Browser、中文注释和提交前安全门禁均已通过。该结论只关闭 PRD-075，
不能据此声称全部 108 条完成；`all_108_prds_complete=false`。

## 1. 权威范围与设计门禁

- Excel 的 PRD 总表、M2/M3 交付、PAGE-105、API、数据对象、Webhook 和 TC-0149/0150 与 README、
  `docs/prd`、`docs/testing` 交叉确认：PRD-074 之后的当前项是
  `PRD-075 | M2-企业增强 | P2 | 模型评测 | 模型 A/B 测试与质量评估`，不是仅按编号推断。
- 范围是公开 Chat 真实流量的可控采样、A/B 精确配额、反馈归因，以及成本、延迟、成功率和满意度的
  权威对比；不接收离线 Prompt/Response，不自动训练、不宣布赢家，也不自动切换 Model Alias。
- 设计见 `docs/prd/PRD-075-model-ab-testing-quality-evaluation.md`。两轮编码前自审分别覆盖真实写入顺序、
  线性化、恢复活性，以及 tenant/RBAC、0068 up/down、兼容性、Console owner 和隐私，最终 P0=0、P1=0。
- PRD-075 开发基线为已推送的 PRD-074 实现 `a122763e77bd086daaed65888611e80baacd4bda`，
  中文注释门禁跟进基线为 `f5ba978f1b567e442e1147b0c3c610af2e4e9e58`；本记录不把提交前工作树
  误写成已推送状态。

## 2. 已通过的自动化专项

开发阶段按受影响范围执行；最终门禁在同一冻结工作树上使用彼此独立的 pytest basetemp 重新运行。不同
分组会覆盖相邻合同，不把它们相加冒充一次根级全量。

| 验证项 | 已记录结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared/Provider 最终受影响集合 | `180 passed` | Experiment 合同、RBAC revision 11、Provider profile/marker、历史 Chat/安全/Batch 兼容 |
| Gateway 最终受影响集合 | `204 passed` | scope claim、owner/generation/lease fence、精确路由、流/非流终态、spool/reaper/outbox 恢复与失败追踪 |
| Control/0068 最终受影响集合 | `120 passed` | API、租户/RBAC、幂等/cursor、Scheduler/Reaper/outbox、审计清理、迁移 owner/down guard |
| Runtime 验收器单测 | `21 passed` | CLI 固定错误、0068 往返、owner-safe cleanup、隐私扫描、证据 schema 与失败批次隔离 |
| Console 全量 | `723 passed` | PAGE-105 与全部既有 Console 状态、owner、navigation 回归；不与直接单测相加 |
| Console lint | `225 files passed` | JavaScript 语法与仓库 Console 规则 |
| Python 定向静态门禁 | `60 files passed` | PRD-075 新文件及受影响 Python 文件 Ruff `--ignore E501` 与 `py_compile` |

核心可复验分组：

```powershell
.\.venv\Scripts\python.exe -m pytest shared-sdk\python\tests\test_prd075_experiment_contracts.py
.\.venv\Scripts\python.exe -m pytest `
  gateway-data-plane\tests\integration\test_prd075_experiment_gateway.py `
  gateway-data-plane\tests\unit\test_prd075_experiment_execution_guard.py `
  gateway-data-plane\tests\unit\test_prd075_experiment_outcome_facts.py `
  gateway-data-plane\tests\unit\test_prd075_experiment_stream_flow.py
.\.venv\Scripts\python.exe -m pytest `
  control-plane-api\tests\integration\test_prd075_experiments_control.py `
  control-plane-api\tests\integration\test_prd075_db_migrations.py `
  control-plane-api\tests\unit\test_prd075_console_navigation.py `
  control-plane-api\tests\unit\test_prd075_experiment_audit_contract.py `
  control-plane-api\tests\unit\test_prd075_request_log_stage.py
& .\.tools\node-v22.23.2-win-arm64\node.exe --test console-web\tests\unit\prd075Experiments.test.mjs
```

全仓 `scripts/ci/check-comments.py` 使用原规则通过，没有弱化检查器或新增豁免；PRD-075 Runtime 脚本的
新增迁移分支还单独执行 `--paths` 门禁并返回 `comment coverage ok`。
最终门禁对 60 个 PRD-075 新增及受影响 Python 文件统一执行 Ruff `--ignore E501` 和 `py_compile`，并对
29 个 PRD-075 新增 Python 文件执行完整 Ruff；随后再次执行 `git diff --check`。该结论不被表述为未运行的
整仓 Python 全量。

## 3. 0068 与数据库约束

- `0068_model_ab_testing_quality_evaluation.sql/.down.sql` 把迁移提升到 `68/0068`、RBAC catalog revision 11、
  66 个 permission templates 和 179 条 canonical 角色关系。
- 迁移专项覆盖 fresh/repeat、0067 -> 0068 -> 0067 -> 0068、up/down 后新租户 initializer、deferred
  Assignment/Execution/Settlement closure、数据库时钟 takeover、Result facts owner、REPLACE/不可变约束，
  以及 Experiment、Audit、RequestLog、Webhook、角色漂移和 active scheduler lease 的原子 down guard。
- 自定义 developer/ops_admin 不会因同名角色被静默扩权；只对 exact canonical revision-10 矩阵新增权限，
  down 前矩阵或权限审计漂移会在任何 DDL 前失败关闭。
- final Runtime 在同一个空 SQLite 上依次执行 fresh `68/0068`、repeat no-op、down `67/0067` 和 re-up
  `68/0068`；四阶段均 `integrity_check=ok`、外键违规 0，repeat 为 applied 0 / skipped 68。
- re-up 后最终数据库为 RBAC revision 11、permission 66、实验 outbox pending 0；迁移往返摘要直接进入
  脱敏 Runtime JSON，不依赖终端文字或开发批次拼接。

## 4. 真实 Runtime 权威证据

唯一 final Runtime 权威批次为：

- `C:\MetaHub\.artifacts\prd075-runtime\prd075-final-20260810T011951Z\runtime-acceptance.json`
- `C:\MetaHub\.artifacts\prd075-runtime\prd075-final-20260810T011951Z\SHA256SUMS.txt`
- JSON SHA-256：`a57fc35bfab6f5ab8e8f0873f02cce7f2e60ee7fc3d84d6f63674c5c903ac845`
- 证据保持 `all_108_prds_complete=false`；manifest 与文件实际 SHA-256 已由主 Agent 独立复核相等。

旧 `prd075-final-20260809T193006Z`、`prd075-final-20260809T232611Z`、`prd075-dev*`、
`prd075-browser-dev*` 和失败批次均早于最终源码或属于
开发诊断，不是最终证据，不进入 Git 白名单，也不与 final 批次拼接统计。

| Runtime 节点 | final 结果 |
| --- | --- |
| 服务健康 | Console feature/index、Control 初始/恢复、Gateway 均为 HTTP 200 |
| 实验与分配 | 3 个实验全部 completed，8 个 Assignment，A/B 各 4 |
| Outcome/Settlement | 7 success、1 failure、0 unresolved；7 recorded、1 unavailable、0 pending |
| 冻结结果 | 6 条 Result；第一组重复读取相等，第二组保留 Provider 失败，第三组在 Control 重启后完成 |
| 权限与租户 | Viewer 403、跨 tenant 404、后端导航包含 PAGE-105、统一错误保留 request_id |
| 停机恢复 | Control 停止期间 Gateway facts 已提交；重启后 finalizer 和投递完成 |
| Webhook | 3 completed、0 signature failure、0 forbidden hit、0 invalid |
| Provider stub | A/B 各 4 次调用；认证不匹配均为 0，B 注入 1 次可追踪失败 |
| 数据库 | fresh/repeat `68/0068`、down `67/0067`、re-up `68/0068`，四阶段 integrity ok / FK 0，outbox pending 0 |
| 隐私与清理 | 145 个受控文件的随机明文 canary 命中 0；固定端口、Python/Node owner、WAL/SHM/journal 均归零 |

Runtime 使用两个固定回环 OpenAI-compatible Provider 和本地 Webhook，不访问或把智谱、通义、月之暗面、
文心、火山、混元、DeepSeek 的真实凭据写入证据。OpenRouter、硅基流动没有被当作默认上游。

## 5. 主流程、失败、权限与并发覆盖

- 主流程：创建、幂等重放、稳定 cursor、详情、provisional/final 结果、样本目标完成、feedback fence、
  source/outbox 和完成 Webhook。
- 异常：幂等异体、scope 冲突、严格 JSON/Header/query、统计 250ms deadline、连接失败、执行绝对截止、
  Provider 失败、settlement unavailable 和 Control 停机恢复。
- 提交前独立反例复现了实验 timeout/通用异常在 finalizer 前发送一次 error、持久化降级后再发送第二次 error；
  修复后 `test_prd075_experiment_stream_flow.py` 为 `27 passed`。新增组合用例证明 timeout+RequestLog 失败和
  缺 `[DONE]`+Usage 丢失都只发送一个、且只发送持久化收口后的最终错误码；普通 timeout+Usage 异常仍保持
  历史即时错误时序，但不会在 finalizer 后再发送第二个错误。
- 权限/租户：未认证 401、Viewer/撤权 403、创建五权限事务内复核、跨 tenant 与不存在同形 404，后端
  权限是最终边界；Console 隐藏入口不是授权依据。
- 并发/恢复：exact A/B 配额、同 scope 唯一 active、Gateway generation takeover、过期 owner/lease fence、
  Scheduler 跳过未到期队首、Reaper、outbox bounded claim 与旧 owner CAS 拒绝。
- 追踪/隐私：Assignment、Execution、Outcome、Settlement、RequestLog、Usage、FinOps 与失败错误码保持
  tenant/request/trace owner；Provider Key、Token、Prompt、Response 和 Endpoint 不进入日志或公开证据。

## 6. Browser 与最终门禁

- 权威 Browser 目录为 `.artifacts/prd075-browser/prd075-final-20260809T234829Z/`，状态 `passed`；
  manifest 13 项 SHA-256 全匹配，只白名单两个 JSON、SHA 和 11 张截图。主 JSON SHA-256 为
  `82379395d845649fa5d54504311c1862aa81974b47000b58ca9c2f8e730cc092`。
- 应用内 Chromium CSS 视口 `1280x720`、`390x844` 的横向溢出、重叠和文本溢出均为 0；11 张截图全部
  非空，最低采样颜色数 1710、最低方差和 1710.4，两张冻结 A/B 结果卡使用 facts v7。
- UI 点击创建 `PRD075 browser final create` 成功且列表出现新行；该证据与独立 API helper 严格分离。
  API helper 验证首次创建 201、同 body/同 key 重放 201 且 owner 相同、改 body/同 key 返回 409
  `experiment.idempotency_conflict`、改 body/新 key 返回 201 且创建新 owner。
- 撤销 `experiment.read` 后导航、Panel、DOM 和 state 全清，后端直接请求返回 403、
  `auth.permission_denied`，响应体 request_id 与响应头一致并回退 Overview；恢复权限后后端 200 且权限矩阵
  完全恢复。授权仍由后端决定。
- Browser 隐私项 Provider Key、Token、Password、Prompt、Provider Response 均为 false；目录无 profile、日志、
  数据库或 `.env`。临时凭据和私有 contract state 已删除；Runtime 与 Browser 固定端口、owner 进程及
  WAL/SHM/journal 均归零。
- 全仓中文注释、Ruff/compile、Console lint、`git diff --check`、敏感信息与禁止文件审计均通过；Git 只精确
  白名单 final Runtime/Browser 小型证据，不包含 `.env`、数据库、日志或 `.tmp`。

## 7. 当前结论

PRD-075 的设计、两轮自审、实现、测试、0068 正反迁移、文档、Runtime、Browser、隐私与清理证据均已
闭环，可标记为完成。该结论不启动或实现下一条 PRD，不表示全部 108 条完成；
`all_108_prds_complete=false`。
