# MetaHub AI Gateway

MetaHub 是面向中国境内企业用户的企业级 AI Gateway + 治理控制面。它提供统一模型调用入口、控制面治理、成本与用量追踪、日志和调试能力。

重要边界：

- OpenRouter 和硅基流动是对标平台、竞品平台和迁移兼容对象，不是默认模型厂家。
- 默认上游是模型厂商或云端模型 Endpoint，例如智谱、通义、文心、火山、混元、DeepSeek 官方或云上服务，以及客户本地 OpenAI-compatible Endpoint。
- MVP 不做硬件盒子、不做 MCP 主链路、不做信创、不做复杂数据出境；这些能力放到 M2/M3 或可选模块。

## 当前状态

当前已完成：

- `PRD-001 - 仓库工程骨架与模块边界`
- `PRD-002 - 运行配置与环境隔离基础`
- `PRD-003 - 数据库迁移与种子数据`
- `PRD-004 - 统一日志、TraceID 与错误码`
- `PRD-005 - 服务健康检查与版本信息`
- `PRD-006 - 管理员登录与 Session`
- `PRD-007 - 单租户与基础多租户模型`
- `PRD-008 - 基础 RBAC 角色`
- `PRD-009 - OpenAI-Compatible 接口基础`
- `PRD-010 - Virtual Key 创建、鉴权与禁用`
- `PRD-011 - 模型 Endpoint Provider 注册`
- `PRD-012 - 模型目录与内部标准模型ID`
- `PRD-013 - Provider Adapter 抽象接口`
- `PRD-014 - Provider KeyPool基础管理`
- `PRD-015 - 模型别名与最小路由`
- `PRD-016 - Request Log最小闭环`
- `PRD-017 - Token Usage与成本估算`
- `PRD-018 - 控制台骨架与导航`
- `PRD-019 - 5分钟快速接入页`
- `PRD-020 - 内置API调试控制台`
- `PRD-021 - 智能错误提示基础版`
- `PRD-022 - Virtual Key级RPM/TPM限流`
- `PRD-023 - Key/租户日预算与告警`
- `PRD-024 - 关键词/正则敏感词拦截`
- `PRD-025 - 基础事件订阅与推送`
- `PRD-026 - MVP总览Dashboard`
- `PRD-027 - i18n基础框架`
- `PRD-028 - 单机Docker Compose部署`
- `PRD-029 - Project与Application模型`
- `PRD-030 - dev/test/prod环境隔离`
- `PRD-031 - 价格表与生效时间`
- `PRD-032 - Provider 计费适配规范 MVP`
- `PRD-033 - 预算耗尽阻断与恢复`
- `PRD-034 - 管理操作审计日志`
- `PRD-035 - Provider健康探测与状态展示`
- `PRD-036 - 非流式请求备用模型Fallback`
- `PRD-037 - SSE流式代理与超时看门狗`
- `PRD-038 - Python SDK与示例项目`
- `PRD-039 - JavaScript/Node示例项目`
- `PRD-040 - 从直连模型厂商迁移指南`
- `PRD-041 - 竞品定位与能力映射`
- `PRD-042 - Webhook事件过滤与重放基础`
- `PRD-043 - 用量CSV导出`
- `PRD-044 - 策略引擎DSL最小版`
- `PRD-045 - 策略快照版本化与发布`
- `PRD-046 - 策略模拟与单请求预演`
- `PRD-047 - 成本优先/可用性优先路由`
- `PRD-048 - 效果-成本路由基础`
- `PRD-049 - 调用反馈与采纳率记录`
- `PRD-050 - Prompt Cache 感知粘性路由`
- `PRD-051 - FinOps Ledger 正式账本`
- `PRD-052 - 租户/项目/Key三级预算`
- `PRD-053 - 成本异常检测基础`
- `PRD-054 - Request Explainability Center`
- `PRD-055 - Trace查询与过滤增强`
- `PRD-056 - Provider错误归一化规则库`
- `PRD-057 - Webhook死信与持久重放`
- `PRD-058 - 审计日志不可删除与导出`
- `PRD-059 - 细粒度角色权限矩阵`
- `PRD-060 - 用户邀请与加入流程`
- `PRD-061 - 内部Provider状态页`
- `PRD-062 - 配置导入导出CLI`
- `PRD-063 - 国内模型Endpoint适配规范`
- `PRD-064 - Provider Adapter契约测试框架`
- `PRD-065 - 多角色首页与导航`
- `PRD-066 - 企业Chat演示入口`
- `PRD-067 - 企业Prompt模板库`
- `PRD-068 - 行业/场景策略模板库`
- `PRD-069 - 轻量敏感意图分类`
- `PRD-070 - 多轮会话风险引擎`
- `PRD-071 - 输出侧安全检测与流式滑窗`
- `PRD-072 - 文件上传与文本解析基础`
- `PRD-073 - 隐藏 Prompt Injection 检测`

最近进展：

- `PRD-073 - 隐藏 Prompt Injection 检测`的设计、两轮编码前自审、Shared TXT/DOCX/PDF 检测、
  固定版本离线 OCR、Control 扫描/报告/CSV/模型准入、PAGE-103、0065 安全事实与 RBAC revision 9、
  后端 tenant/双权限/统一错误和 7 字段安全 Webhook 均已实现。实现后安全终审关闭 PDF
  `ToUnicode/CMap` 语义绕过和 finding 饱和后丢失 Prompt/coverage 两项问题；最终交叉审查又以追加
  0066 关闭 source 已提交、delivery intent 尚未创建时的崩溃丢失窗口。0066 trigger 在扫描同一事务
  写 tenant-qualified pending outbox，并回填既有 source；Control lifespan 启动即恢复，路由只 wake
  scheduler。worker 使用 90 秒 lease；每轮先在独立短 `BEGIN IMMEDIATE` 事务中真实 claim 一条到期
  source，提交 `processing`、attempt、token、owner 和 lease。随后当前且未过期的 owner 在另一个独立
  事务中，以 tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 复核并补齐 0065 遗留的部分
  delivery、持久化 `delivery_snapshot_frozen_at` 并续租；即使订阅集合为空也冻结，之后新增 Endpoint
  不追溯旧 source。每次真实网络尝试前仍以同一组完整 CAS 再续租。lifespan 以 `threading.Event` 协作停服并等待同步 worker 完成收尾，不通过取消
  `to_thread` 遗留写库线程。稳定 `fsev_` event ID 提供 at-least-once，失败按 5/30/300 秒退避且
  最多 10 次。source 终态与剩余 pending delivery/死信同事务，Webhook attempt 使用状态、序号和
  Secret 版本 CAS 阻断迟到覆盖；坏 source 必须先完成真实 claim，再由当前 owner 交叉验证并以完整 CAS
  固定隔离，随后继续处理后续 tenant 事件，不让损坏队首形成
  全局阻塞。
  0066 迁移与聚焦回归、20 个 Runtime 合同节点和新的 `66/0066` 真实 Runtime 均已通过。正式验收
  真实停止 Control，在停服窗口提交 source/outbox，再由新 Control lifespan scheduler 以 attempt 1
  恢复为 `dispatched`；验收器主动 drain 为 false、临时 claim 已清空、隐私命中为 0。权威 Runtime
  为 [prd073_20260806T062221Z_b2632ac89f57fc3a](.artifacts/prd073-runtime/prd073_20260806T062221Z_b2632ac89f57fc3a)，
  JSON SHA-256 为 `E2EFF29227A4246AE8FBB86B5B3C973C6E5212A1568AA32D9ABFB05B0DAE34FD`。
  0066 不改 PAGE-103，既有双视口 Browser 证据继续有效；2026-08-06 又在实际隔离库上复验桌面、
  移动、幂等扫描、零横向溢出、零页面错误和登出清理。详见
  [PRD-073 设计](docs/prd/PRD-073-hidden-prompt-injection-detection.md)、
  [文件安全 API](docs/api/file-security.md)、
  [PRD-073 测试记录](docs/testing/PRD-073-tests.md)、
  [有效 Browser](.artifacts/prd073-browser/prd073_20260805T045800Z_4f3ee3d6aa3f233b)。
- `PRD-072 - 文件上传与文本解析基础`：设计与两轮自审、Shared 文件合同、Control 文件 API、
  PAGE-101/102、0064、RBAC revision 8、加密 Blob、TXT/DOCX/PDF 抽取、DLP 预扫描、Token 估算、
  后端租户/权限/统一错误和安全 Webhook 均已完成。专项结果为 Shared `25 passed`、Control 与导航
  `15 passed`（文件 API 13、导航 2）、迁移 `6 passed`、Console 最终聚焦 `18 passed`、Runtime 验收器
  `6 passed`（含管理员密码只从环境注入并读取后移除）、Console lint `209 files passed`，中文注释、语法和
  日志隔离门禁通过。正式 Runtime 为 `status=passed`：三格式 ready、DLP 403、权限 403、跨租户 404、
  `64/0064`、数据库完整性/外键、认证密文对象和敏感明文零命中均通过；Windows UTF-16LE 日志另以
  新的多编码 canary 规则实时复扫，三份日志命中为 0。Browser 在 `1280x720` 与 `390x844` 完成列表、
  筛选、DLP/病毒/Token、SecurityAdmin 只读降级和注销清理，页面无横向溢出且正文、对象键、hash、
  规则内容没有进入 DOM。详见
  [PRD-072 设计](docs/prd/PRD-072-file-upload-text-extraction.md)、
  [文件 API](docs/api/files.md)、
  [PRD-072 测试记录](docs/testing/PRD-072-tests.md) 和
  [正式制品](.artifacts/prd072-runtime/prd072_20260804T212908Z_922f8ab2ff200912)。
- `PRD-071 - 输出侧安全检测与流式滑窗`：设计与两轮自审、Shared/Gateway/Control/Console、0063、
  DLP `scan_scope`、PAGE-099/PAGE-100、FinOps 争议投影、后端租户/RBAC/统一错误和 Webhook 均已完成。
  相关自动化为 Shared/Gateway `60 passed`、0063 `9 passed`、Control 相邻 `47 passed`、Console 相邻
  `59 passed`、最终页面直接回归 `9 passed`、Runtime 验收器合同 `7 passed`、lint `203 files passed`，
  `py_compile` 与中文注释门禁通过。正式 Runtime 为 `status=passed`：clean 200、非流阻断 403、扫描不可用
  503、跨 chunk 流阻断唯一 SSE error、401/403/404、账单/事件 owner、`63/0063`、完整性/外键、隐私和
  一次性资源清理均通过。应用内 Browser `1280x720` 与 `390x844` 无覆盖/横向溢出，Viewer 前后端权限和
  DOM 隐私通过。详见
  [PRD-071 设计](docs/prd/PRD-071-output-safety-streaming-window.md)、
  [输出安全 API](docs/api/output-safety.md)、
  [PRD-071 测试记录](docs/testing/PRD-071-tests.md) 和
  [正式制品](.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64)。
- `PRD-070 - 多轮会话风险引擎`：设计与两轮自审、0062、RBAC revision 7、Redis 原子累计/持久
  evidence、current/previous HMAC 轮换 verifier、Control 处置、PAGE-098 代码、租户/权限/统一错误、
  真实三服务/API/RESP 和非浏览器验收均已通过。聚焦 Python `86 passed`，HMAC 相关回归 `35 passed`，
  数据库文件系统故障节点另行 `1 passed`，PAGE-098 `11 passed`，Console lint `201 files`，定向 Ruff 与
  中文注释门禁通过；开发库为 `62/0062`，运行证据 `integrity_check=ok`、外键违规 0、敏感明文命中 0。
  应用内 Browser `1280x720` 与 `390x844` 已验证默认/已处置筛选、详情、2 个冻结 overlay、5 条动作
  时间线和零横向溢出；桌面筛选栏溢出已修复并复验。并行 Edge extension 超时文件只作为非权威诊断，
  不覆盖页面验收结论。详见
  [PRD-070 设计](docs/prd/PRD-070-multi-turn-session-risk-engine.md)、
  [会话风险 API](docs/api/session-risks.md) 和
  [PRD-070 测试记录](docs/testing/PRD-070-tests.md)。

当前下一项：

- Excel 的 `01_PRD总表`、`03_M2-M3扩展交付`、页面、API、数据对象、Webhook 与验收用例表已交叉确认：
  PRD-073 之后的下一条权威未完成项是
  `PRD-074 | M2-企业增强 | P2 | 批处理调度 | 异步批处理 Scheduler 与 Worker`，对应 PAGE-104、
  `POST /batch-jobs`、`GET /batch-jobs/{id}`、`POST /batch-jobs/{id}/cancel`、BatchJob/BatchTask、
  `batch_job.created/completed` 与 TC-0147/0148。PRD-073 已完整收口，下一轮从 PRD-074 的设计与
  两轮自审开始；总体目标仍为 `all_108_prds_complete=false`。
- PRD-068 已完成：设计与两轮自审、0060、租户/权限/错误、Shared/Gateway/Control/Console 专项、
  Snapshot v12 真实 apply、数据库完整性和三服务运行均通过。最终 PAGE-095 聚焦回归为 `16 passed`；
  应用内 Browser `1280x720` 与 `390x844` 验证了五模板、已应用后的 incompatible 防重复、预算模板
  ready-with-warning、完整确认摘要、Snapshot v12、无页面级横向溢出、零页面错误和注销清理。验收中
  修正了 Candidate CAS 应匹配 `before_candidate_hash` 以及移动确认框长 ID 撑宽问题；中文注释门禁通过。
  证据见 [PRD-068 设计](docs/prd/PRD-068-industry-scenario-policy-template-library.md)、
  [策略模板 API](docs/api/policy-templates.md) 和
  [PRD-068 测试记录](docs/testing/PRD-068-tests.md)。
- PRD-069 已完成：本地分类器、0061、RBAC revision 6、`POST /security/classify-intent`、Gateway
  DLP 后/限流前语义闸门、Usage spool v3、RequestLog 七状态以及 PAGE-096/PAGE-097 均通过。唯一一次
  根级 Python 全量为 `2146 passed / 4 failed`，四项历史迁移/RBAC 合同漂移原 node 定向 `4/4 passed`，
  未重跑全量；新增性能/Webhook/shutdown 门禁 `4/4 passed`。20k 中文 strong/light P95 为
  `6.7792/8.1162ms`，三服务最终 `200/200/200`，开发库 `61/0061` 且完整性/外键通过，Browser 已显示
  非空登录页，39 个运行日志文件敏感命中 0。
  合同与当前证据见 [PRD-069 设计](docs/prd/PRD-069-lightweight-sensitive-intent-classification.md)、
  [轻量敏感意图 API](docs/api/intent-security.md) 和
  [PRD-069 测试记录](docs/testing/PRD-069-tests.md)。
- PRD-067 已完成：设计与两轮自审、后端交叉终审、0059、真实 Provider/Gateway 治理调用、PAGE-094
  模板管理、PAGE-093 模板调用、租户/权限/异常、应用内 Browser 双视口和资源清理均通过。最终 Python
  受影响组合 `73 passed`、Console 相关 `38 passed`、移动布局定向 `10 passed`、lint `188 files`，
  中文注释门禁通过；两次真实模板 Chat 均为 18 tokens，开发库 `59/0059` 且完整性通过，测试
  Session/Chat/Grant/临时 Key 均归零。设计、API、测试和 Runtime 证据见
  [PRD-067 设计](docs/prd/PRD-067-enterprise-prompt-template-library.md)、
  [Prompt 模板 API](docs/api/prompt-templates.md)、
  [PRD-067 测试记录](docs/testing/PRD-067-tests.md) 与
  `.artifacts/prd067-runtime/run-20260802T110138Z-c00b97cac3/runtime-evidence.json`；
  `all_108_prds_complete=false`。

PRD-062 已完成：Control API 提供 `POST /config/export`、`POST /config/import/validate`、
`POST /config/import`，0054 配置迁移事实与 `config.transfer.read/write`，以及标准库客户端
`scripts/gatewayctl.ps1` / `python -m metahub_shared.gatewayctl.cli`。当前合同使用 2 MiB、深度 8、
200000 节点和 5000 总行资源门禁；manifest hash 排除仅用于溯源的 `exported_at`；active Virtual Key
在 apply 前必须预留私有密钥文件。设计、HTTP 合同和 CLI 用法见
[PRD-062 设计](docs/prd/PRD-062-config-import-export-cli.md)、
[配置迁移 API](docs/api/config-transfer.md) 和
[gatewayctl 使用说明](docs/development/gatewayctl-config-transfer.md)。API `6 passed`、CLI
`33 passed`、Audit `13 passed`、迁移 `3 passed`、直接回归 `21 passed`、中文注释门禁与两套 0054 实迁移
均通过；真实 source/target CLI、权限、租户和 5 Token Gateway 调用见
[PRD-062 测试记录](docs/testing/PRD-062-tests.md)。

PRD-060 已落地一次性 Token 邀请、tenant-qualified Invitation、实时后端 `user.manage`、同事务
AuditLog/source event、固定接受审计脱敏、0051/0052 和 PAGE-087。迁移与后端专项、Console 全量、
开发库升级、三服务 Runtime、Microsoft Edge 1280x720/390x844 真实页面及隐私门禁均已通过。
设计、接口和最终测试记录见 [PRD-060 设计](docs/prd/PRD-060-user-invitation-join-flow.md)、
[用户邀请 API](docs/api/invitations.md) 和
[PRD-060 测试记录](docs/testing/PRD-060-tests.md)。

已具备：

- 数据面：`gateway-data-plane`，提供 `/health`、`/internal/health`、`/version`、`/v1/chat/completions`、`/v1/embeddings`、`POST /v1/adapter-contract-probes/identity`、`POST /feedback`，并通过 Virtual Key 恢复可信 Environment，按环境解析模型别名、预算和归属快照，再由已发布 Policy 快照、Provider Adapter、KeyPool、健康状态、限流和基础 DLP 控制上游调用；Policy 快照按 tenant 首次 singleflight 拉取并后台轮询，单请求冻结版本和 LKG 状态，`deny`/异常在 Provider 前失败关闭，`route_to` 重新执行租户、环境、模型、Provider、健康和能力校验；新鲜 unhealthy Provider/Key 会在请求前避开，过期证据 Fail Open。PRD-047 为 Alias 增加 `fixed/cost_first/availability_first` 初始排序；PRD-048 的 `quality_cost` 在通用资格后读取租户内 Score，先执行质量门槛和冷启动，再按正权重依赖读取延迟/价格并冻结分量。Attempt/Stream 真实上游终态与 Score 增量同事务，质量资格和预算可能计费判断相互独立，已通过真实三服务和异常路径验收。非流式主调用遇到策略允许的超时、429 或 5xx 时最多切换一次备用模型；流式调用提供 SSE 增量透传、块间超时看门狗、客户端断开清理和 StreamSession 审计，流中不切换模型。PRD-049 的 Feedback 入口只接受产生源调用的同租户 Virtual Key，复用共享严格校验与不可变事务，不在数据面开放聚合报表。PRD-056 在认证后为每个请求冻结一次 ProviderErrorMap lease，主调用、Fallback 和流式握手共享同一 revision；Control 不可用时只回落 builtin，规则热更新无需重启 Gateway，失败 Fact 与 attempt/session 同事务且只保存脱敏证据。PRD-064 的身份端点只用于受控契约探测，在 Virtual Key 鉴权后校验单值 tenant 证明；普通 OpenAI-compatible 请求合同不变。
- 控制面 API：`control-plane-api`，提供 `/health`、`/internal/health`、`/version`、`/error-codes`、`GET/POST /error-maps`、`PATCH /error-maps/{error_map_id}`、`GET /internal/error-maps/{tenant_id}/current`、`/internal/config/summary`、`/auth/login`、`/auth/logout`、`/auth/me`、`/tenant/current`、`/user/preferences`、`/users`、`/environments`、`/projects`、`/applications`、`/virtual-keys`、`/virtual-keys/{id}/limits`、`/providers`、`/providers/{id}/keys`、`/providers/{id}/probe`、`/provider-health`、`/provider-keys/{id}`、`/models`、`/model-prices`、`/model-aliases`、`/model-aliases/{id}/fallback`、`/model-aliases/{id}/routing-mode`、`/model-aliases/{id}/cache-affinity`、`/model-quality-scores`、`/policies`、`/policies/validate`、`/policies/dry-run`、`/policies/{policy_id}`、`/policy-snapshots/current`、`/policy-snapshots`、`/policy-snapshots/publish`、`/policy-snapshots/{id}/rollback`、`/internal/policy-snapshots/{tenant_id}/current`、`POST /security/classify-intent`、`/request-logs`、`GET /request-explain/{source_request_id}`、`/audit-logs`、`GET /audit-logs/export`、`/finops/ledger`、`/finops/adjustments`、`/usage/summary`、`/usage/records`、`/usage/export.csv`、`/usage/exports/{job_id}`、`/usage/exports/{job_id}/download`、`/budgets`、`/budget-status`、`GET /cost-anomalies`、`PATCH /cost-anomalies/{id}/action`、`/dlp/rules`、`/webhooks`、`/webhooks/{id}/test`、`/webhooks/{id}/deliveries`、`/webhooks/{id}/replay`、`/webhooks/{id}/rotate-secret`、`/dashboard/overview`、`/console/navigation`、`/console/preferences`、`/console/events`、`/developer/quickstart-data`、`/developer/sdk-examples/python`、`/developer/sdk-examples/python/download`、`/developer/sdk-examples/javascript`、`/developer/sdk-examples/javascript/download`、`/debug/chat-completions`、`POST /feedback`、`GET /feedback/summary`。`GET /model-aliases` 返回当前 CacheAffinity、RoutingPolicy、休眠质量配置和候选安全摘要；`quality_cost` PATCH 使用完整五字段配置、锁内 CAS 和同 request_id 持久幂等。`GET/PATCH /model-quality-scores` 分离统计版本与人工反馈版本，读权限为 admin/developer、写权限仅 admin，tenant_id 只从 Session 恢复。PRD-049 的反馈创建与汇总继续从 Session 恢复 tenant/actor，admin/developer 可用、viewer 后端拒绝，跨租户或跨 Key 源调用同形防枚举。PRD-051 的 FinOps 读取和调整均为 admin-only 后端权限，tenant_id 只从 Session 恢复，跨租户目标同形 404，统一错误携带 request_id/trace_id。PRD-053 的成本异常 GET/PATCH 同样固定为 admin-only，并只从 Session 恢复 tenant/actor；该合同已通过专项、跨租户与真实运行验收。PRD-056 的规则读权限为 admin/developer、写权限仅 admin，viewer 后端拒绝；tenant/actor 只从 Session 恢复，版本/head/operation/Source Event/Audit 同事务，内部快照只接受 Gateway service token。PRD-058 的审计导出固定 admin-only，tenant/actor 只从 Session 恢复；CSV、不可变 AuditExport、`audit.exported` AuditLog 与持久 source 在同一事务形成复核链。上述已完成合同均已通过对应 RBAC、租户和运行验证。
  PRD-064 已注册 `POST /internal/tenants/{tenant_id}/adapter-contract-runs`、`GET /adapter-contract-runs` 和
  `GET /adapter-contract-runs/{run_id}`；内部上报与 Session 读取分离鉴权，所有租户归属、实时
  `provider.health.read`、统一错误、request_id/trace_id 和 no-store 均由后端执行；真实 HTTP 的
  admin/developer、viewer 403、匿名 401、跨租户 404 和 Outbox 恢复已通过。
- 健康检查：`/health/live` 不依赖外部资源，`/health/ready` 检查数据库等依赖。
- 控制台：`console-web`，提供统一主框架、角色化导航、Environment 设置、项目与应用、Virtual Key 环境绑定、模型别名、PAGE-076 预算中心（`tenant/project/virtual_key` 三级，Application 仅作用链兼容）、环境用量/日志、admin-only 用量 CSV 导出与异步任务轮询、管理操作审计、Provider 与 KeyPool、Provider 健康状态、模型目录、DLP、admin-only 策略管理/编辑器、快照发布/上一版回滚历史及策略模拟器、Webhook、快速开始、API 调试台和错误诊断等治理页面；PRD-048 已把 PAGE-068 扩展为四种模式和五字段质量配置，并新增 PAGE-069 模型评分页。admin 可维护人工评分，developer 只读，viewer 无 DOM/预取；RequestLog 展示请求时冻结的公式、版本、证据、分量和总分，不用当前 Score 回算历史。PRD-049 新增 PAGE-070 日志详情反馈与 PAGE-071 反馈报表，admin/developer 可提交或读取，viewer 无 DOM/预取；异步 owner、迟到响应和筛选后 DOM 收敛均有门禁。PRD-050 在 2026-07-24 的变更前全量基线为 Console `354/354`、lint `118 files`；当前 Browser 验收器局部修复只执行定向回归，最终 45 张真实 Chrome PNG 覆盖三角色与 1280/900/390 三档视口。 PRD-051 新增 PAGE-074 正式账本与 PAGE-075 用量详情；调整/冲正仅 admin 可见且后端再次校验，双视口真实 Browser 已验证布局、焦点、草稿销毁和隐私。PRD-056 新增 PAGE-081 错误码管理，并在 PAGE-082 既有日志详情展示冻结 attempt/session Fact；admin 可编辑、developer 只读、viewer 无导航/DOM/预取，409、焦点、注销和双视口均已真实验收。
  PRD-063 的 PAGE-090 展示冻结 Adapter 规范目录；PRD-064 的 PAGE-091 已实现契约运行摘要、筛选、分页和
  case 详情，并依赖后端导航 owner/实时权限。PAGE-091 最终专项 `15/15`，1280x720 与 390x844
  真实 Edge 的 403/503、恢复、空态、分页、详情、注销、非空截图和隐私门禁均已通过。
- 共享契约：`shared-sdk`，提供运行时基础契约、配置摘要、数据库迁移/seed 工具、认证安全工具、错误码注册表、错误诊断规则、i18n 基础字典、受限 Policy DSL 编译/纯求值合同、`policy_preview` 无副作用预演合同，以及 tenant-bound content hash、完整 artifact hash 和严格策略快照 wire 合同；PRD-048 在 PRD-047 两阶段排序上新增 `quality_cost_v1` 的整数/Decimal 质量、成本、延迟分量、冷启动/门槛矩阵和稳定原因码，Gateway 与 Dry-Run 复用同一纯函数。PRD-049 新增共享 Feedback contracts/validation/eligibility/storage/wire/webhook 包，Control 与 Gateway 复用同一规范化、幂等、租户归属、不可变写入和安全事件合同。PRD-056 新增 Provider 错误 builtin catalog、tenant overlay 编译、强 ETag/wire、有界事实提取、不可覆盖 guard、确定性 matcher、统一错误推导和 repr-safe 脱敏证据合同。
- 脚本与文档：`scripts`、`docs`、`infra`。PRD-047/048 接口合同见 [RoutingPolicy 与模型质量 API](docs/api/routing-policies.md)，PRD-048 证据见 [PRD-048 测试记录](docs/testing/PRD-048-tests.md)；PRD-049 的 [设计文档](docs/prd/PRD-049-request-feedback-adoption-records.md)、[Feedback API](docs/api/request-feedback.md) 和 [测试记录](docs/testing/PRD-049-tests.md) 分别作为设计、公开合同与最终验收的权威来源。PRD-058 的 [设计](docs/prd/PRD-058-audit-log-immutable-export.md)、[审计日志不可变导出 API](docs/api/audit-exports.md) 和 [测试记录](docs/testing/PRD-058-tests.md) 已登记最终合同与直接验收证据。
  PRD-064 的 [设计](docs/prd/PRD-064-provider-adapter-contract-test-framework.md)、
  [契约运行 API](docs/api/provider-adapter-contract-runs.md) 和
  [测试记录](docs/testing/PRD-064-tests.md) 分别登记编码前/实现后自审、稳定接口、最终证据和
  未伪写为全量零失败的分层回归边界。
- PRD-050 增量：Gateway 已在当前合格候选内实现 tenant/Virtual Key/data_class/endpoint revision/Prompt fingerprint 隔离的 pending/warm 端点与 Provider Key 亲和；Control 提供 admin-only `PATCH /model-aliases/{id}/cache-affinity` 和 admin/developer 读取，RequestLog 返回安全 Selection 摘要；PAGE-072/073 已通过三角色三视口真实 Chrome 验收。
- PRD-051 已完成：Gateway 将每条成功 Usage 同事务投影为 append-only 正式分录，主库暂不可用时写入本机 HMAC/fsync spool 并幂等恢复；Control 提供 admin-only `GET /finops/ledger` 与 `POST /finops/adjustments`，支持五类调整、冲正、签名游标、审计和统一错误；Console 提供 PAGE-074/075。Python 根级全量首次 `958 = 890 passed / 68 failed`，随后仅按受影响节点以迁移 `46/46`、Control `13/13`、Gateway `10/10` 闭环；Node `33/33`、Console `375/375`、lint `125 files`。真实 Runtime、双视口 6 张 Browser 截图及停服终态 19 项门禁均通过，权威 Browser 批次为 `artifacts/prd051/20260727T043500-browser-success-02`。
- PRD-052 已完成：预算按 `tenant/project/virtual_key` 三级累计 AND 生效，`application` 只保留在可信作用链中的历史兼容能力；正式消费统一取 `finops_ledger_entries` 中 `posted usage_charge/debit`，并叠加 outstanding reservation。Control 新增 admin-only、create-only 的 `POST /budgets`，重复规则稳定返回 409；读写均以后端 RBAC、Session 租户和 Environment/Project/Key 归属校验为准。Gateway 在 Project 预算耗尽时于 Provider 前返回带 `request_id` 的统一 402，且不产生 Provider attempt；Console PAGE-076 已通过 1280x720 与 390x844 两档真实 Browser 验收。迁移共 42 条、最高 0042，数据库 `integrity_check=ok` 且外键违规为 0；中文注释门禁、聚焦专项与相邻回归均通过。设计、接口、测试与验收证据分别见 [PRD-052 设计](docs/prd/PRD-052-tenant-project-key-three-level-budget.md)、[三级预算 API](docs/api/three-level-budgets.md)、[PRD-052 测试记录](docs/testing/PRD-052-tests.md)；历史 Runtime、Browser 和停服终态审计保留在本机 `artifacts/prd052/20260727T112603/`，不随源码仓库分发。该状态只代表 PRD-052 完成，不代表全部 108 条 PRD 已完成。
- PRD-053 已完成：0043、确定性成本异常检测、admin-only GET/PATCH、租户隔离、Webhook、Gateway 临时限流和 PAGE-077/078 已落地。Runtime 与 Browser 证据均为 `status=passed`；Browser 合同 `15/15`，双视口五状态正式截图 10 张；Node `33/33`、Console `401/401`、lint `134 files`。Python 根级全量实际结果为 `1178 passed / 30 failed`，30 个环境或旧合同失败随后以受影响范围 `34/34` 全部闭环，未虚写为全量零失败；中文注释和静止终态通过。设计、接口和测试见 [PRD-053 设计](docs/prd/PRD-053-cost-anomaly-detection-foundation.md)、[成本异常 API](docs/api/cost-anomalies.md) 与 [测试记录](docs/testing/PRD-053-tests.md)；历史 Runtime、Browser 和最终审计保留在本机 `artifacts/prd053/20260727T183515/`，不随源码仓库分发。该状态不代表全部 108 条 PRD 已完成。
- PRD-054 已完成：Control 新增 GET /request-explain/{source_request_id}，后端从
  Session 恢复 tenant/actor 并只允许 admin/developer；跨租户与不存在同形 404，成功
  读取原子保存不可变访问事实和 request.explanation_viewed source outbox。0044 新增
  request_explanations、request_webhook_delivery_links 及 tenant owner/不可变/lease
  门禁；Console PAGE-079 提供十阶段、十一区块解释页，并从 Request Logs 使用身份绑定
  的一次性内存 handoff 跳转，viewer 无 DOM/预取。公开合同见
  [请求解释中心 API](docs/api/request-explanations.md)，冻结设计见
  [PRD-054 设计](docs/prd/PRD-054-request-explainability-center.md)，直接结果见
  [PRD-054 测试记录](docs/testing/PRD-054-tests.md)。专项、0044 实迁移、真实
  Runtime/Browser、隐私与停服静止终态均已通过；该状态不代表全部 108 条 PRD 已完成。
- PRD-055 已完成：Control 的 `GET /request-logs` 支持 inline v1、个人
  SavedFilter 和 legacy 三种互斥来源、组合筛选、白名单排序及有界分页，并提供
  `GET/POST/PATCH/DELETE /request-logs/saved-filters`。后端只允许 admin/developer，
  tenant/owner 来自 Session，viewer 真实 403；查询使用 materialized CTE + window count，
  每次成功查询写不可变 `log.query_executed` source outbox。Console PAGE-080 提供高级查询、
  SavedFilter 显式应用和 PAGE-079 一次性内存跳转，筛选和源 request ID 不进入 URL/Storage。
  0045、十万行双租户 Runtime 和双视口八状态 Browser 均已直接通过；Runtime P95 为
  `491 ms`、最大 `510 ms`，Browser blocked/error/viewer prefetch 均为 0。冻结设计见
  [PRD-055 设计](docs/prd/PRD-055-trace-query-enhancement.md)，公开合同见
  [调用日志高级查询 API](docs/api/request-log-query.md)，测试记录见
  [PRD-055 测试记录](docs/testing/PRD-055-tests.md)；历史 Runtime、Browser 和最终审计保留在本机
  `artifacts/prd055/20260728T134456/`，不随源码仓库分发。Python 根级全量
  仅执行一次，真实结果为 `1364 = 1363 passed / 1 failed`；唯一失败是测试误用生产
  `5 ms` 策略超时，测试级改为 `100 ms` 后原 node `1/1 passed`，生产逻辑未改，
  也未重跑整套全量。Node `33/33`、Console `433/433`、lint `140 files` 和中文注释门禁
  均通过；运行库共 `45` 条迁移、最高 `0045`。Runtime、Browser、最终审计 SHA-256 分别为
  `8CC8F6268A11DBFD2B634A0CEE647C2E03049C280969B6D5441DCB6593014143`、
  `967DB05A0CA8ED74D561D85798A93545DCF0132CF9D2B6CAA6E4A8196E608754`、
  `55F5EAA68D07E04F396C8E5728C6597AEB89117058990A7706E5904E5C9097DE`；仅删除 `502`
  个链接节点并保留 `502` 个目标，端口、sidecar、spool、Session 和日志隐私终态均通过。

- PRD-056 已完成：Shared 规则库、Control CRUD/内部快照/Source Outbox、Gateway 请求期单次 lease/cache/Fact、PAGE-081/082 和 0046 已落地。Python 根级收集 `1472` 个唯一 node；原进程在前 433 个通过后中止，随后以无重叠切片覆盖全部余下 node，并对 4、1、52、21 个局部失败集合逐项定向闭环，未虚写为一次全绿。Node `33/33`；Console 一次全量 `451 = 450 passed / 1 failed`，旧测试切片修正后对应文件 `10/10 passed`；lint `147 files`，中文注释门禁通过。Runtime、Browser、最终审计 SHA-256 分别为 `72FC7B860E987BED60F5F441EA4465A069937DB091C5E6C45F789EBAA0EFB245`、`4CC280D0878F66DB191180D6D52F8BAA65B7C4DE727EC5B73202A9CE9D0BB380`、`1BAC341FD4FDEEA2ED4CDB32508F667D8CFB171952D0748F0D3AA458BA0B224B`。运行库 `46/0046`，Session/端口/sidecar/spool/ReparsePoint 均归零；仅删除 584 个链接节点并保留 584 个目标。设计、API 和测试见 [PRD-056 设计](docs/prd/PRD-056-provider-error-normalization-rule-library.md)、[Provider 错误归一规则 API](docs/api/provider-error-maps.md) 与 [测试记录](docs/testing/PRD-056-tests.md)；历史权威证据保留在本机 `artifacts/prd056/20260730T022600/`。该状态不代表全部 108 条 PRD 已完成。
- PRD-057 已完成：Control 新增 `GET /webhook-dead-letters`、
  `POST /webhook-deliveries/{webhook_delivery_id}/replay` 和
  `POST /webhook-dead-letters/{dead_letter_event_id}/archive`，以独立 DeadLetterEvent、
  30 天保留、持久 replay intent、lease/CAS worker 和同事务 Audit 提供 at-least-once 恢复；
  tenant/actor 只从 Session 恢复，admin/developer 可用，viewer 后端拒绝，公开 DTO 不含正文、
  URL、Secret、幂等键或 lease。专项 `24/24`；唯一一次根级 Python 全量为
  `1496 = 1407 passed / 89 failed`，没有重跑整套，原失败节点随后 `89/89` 精确定向闭环；Node
  `33/33`、Console `462/462`、lint `151 files` 和中文注释门禁通过。隔离运行库为 `48/0048`，
  `integrity_check=ok`、外键违规 0、PRD-057 trigger 9。真实 Runtime、三角色/匿名与第二租户、
  HMAC/DTO 去敏以及 1280x720/390x844 Browser 五图均通过；停服后端口、Session、spool、lease、
  pending 和应用运行库 sidecar 为 0。链接清理只在 `D:/MetaHub/.tmp` 删除 540+159 个已确认节点并
  保留全部目标；22 个既有 ACL 目录未提权绕过，结论限定为授权可见范围。权威证据见
  [Webhook 死信 API](docs/api/webhook-dead-letters.md)、
  [PRD-057 测试记录](docs/testing/PRD-057-tests.md) 和
  最终审计保留在本机 `artifacts/prd057/20260731T005000/final-audit.json`，其 SHA-256 为
  `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b`。该阶段后续 PRD-058
  也已完成；`all_108_prds_complete=false`。

- PRD-058 已完成：0049 新增稳定 AuditLog 导出序列、不可变 `audit_exports` 和可恢复
  `audit_export_events`；Control 提供 admin-only `GET /audit-logs/export`，固定 UTF-8 BOM/CRLF
  18 列、10,000 行/16 MiB 上限、公式注入防护及 tenant+actor+request_id 幂等。导出文件、
  `audit.exported` AuditLog 和持久 source 在同一事务形成复核链；PAGE-084 仅允许显式点击，
  自动刷新不触发导出，离页/注销/换身份会销毁 owner 和 Blob。首轮 Runtime 暴露的 pending-resume
  身份绑定缺口已修复，专项、相邻回归、0049 实迁移、真实 Runtime、双视口 Browser、停服静止
  终态及两轮实现后复核均通过。正式 artifact 为 `.tmp/prd058/artifacts/20260731T124500-r14`，共 58 个文件，
  manifest `56/56`，final audit 为 `status=passed`、SHA-256
  `a555fa14edefc7540de4c9af9b4f4f126e932af5b83c70ea021995ff1024fef4`。设计、公开合同和测试见
  [PRD-058 设计](docs/prd/PRD-058-audit-log-immutable-export.md) 和
  [审计导出 API](docs/api/audit-exports.md)、
  [PRD-058 测试记录](docs/testing/PRD-058-tests.md)。其后 PRD-059 也已完成；
  `all_108_prds_complete=false`。

- PRD-059 已完成：0050 新增六个固定角色、48 个租户 Permission、表驱动 RolePermission、角色
  revision/hash、不可变权限 source event 和新租户初始化；认证逐请求从当前租户数据库加载权限，
  业务路由按稳定 permission code 在后端授权。Control 提供 admin-only 的完整矩阵 GET 和 revision
  CAS PATCH，Admin 不可变且职责上限不能被客户端突破；Console PAGE-085 提供桌面矩阵和移动列表。
  迁移 `6/6`、事件 guard `1/1`、认证/权限/路由 `13/13`、Console 受影响 `116/116`、lint
  `156 files` 和中文注释门禁通过；Ruff 未安装，未虚写为通过。开发库为 `50/0050`，完整性 `ok`、
  外键违规 0。真实 Edge 验收覆盖登录、6/48 矩阵、Admin 锁定、草稿放弃、隐私、注销和
  1280x720/390x844 两张非空 PNG；停服后三端口、WAL/SHM 和 pending 权限事件均为 0。权威
  Browser 证据位于 `.tmp/prd059-edge-acceptance-20260731b`。其后 PRD-060 也已完成；
  `all_108_prds_complete=false`。

- PRD-060 已完成：0051 新增 tenant-qualified Invitation、一次性 Token 哈希、严格状态机、
  不可变 source event 和 AuditLog 扩展；0052 在不改写已应用 0051 checksum 的前提下，把接受审计
  邮箱统一为固定脱敏标记。创建接口逐请求执行后端 `user.manage`，公开接受只从 Token 恢复租户、
  邮箱和角色；两个入口均忽略客户端链路 Header，密码、Token、邮件正文不进入日志、审计、事件或
  Browser 证据。Console PAGE-087 提供邀请弹窗、私有链接/邮件文案和 fragment 公开接受页。
  迁移 `23/23`、Invitation/路径/Shared `26/26`、Console `491/491`、lint `160 files` 和中文注释
  门禁通过。开发库为 `52/0052`，完整性 `ok`、外键违规 0；三服务 Runtime 主流程/权限/重放通过，
  Edge 双视口 5 张非空截图无溢出、外部请求和 Token 留存。权威证据见
  [PRD-060 测试记录](docs/testing/PRD-060-tests.md)。其后 PRD-061 也已完成；
  `all_108_prds_complete=false`。

- PRD-061 已完成：Control 新增后端双权限 `GET /status/providers`，从 Session 恢复 tenant 并在同一
  只读快照聚合 Provider、非流式调用、流式首块和完整探测事务；返回六项调用计数、三类独立 P95、
  万分比成功率、最近五条安全故障与维护备注，统一错误和 request/trace ID 完整。0053 增加维护备注、
  四个聚合索引和 `status.viewed`；PAGE-088 复用 Provider Health 导航并提供筛选、立即探测、15 秒刷新、
  owner/generation 迟到响应隔离和 1280x720/390x844 响应式页面。运行收口修复同 request/trace、同健康
  generation 的独立探测误合并，Provider 状态专项最终 `8 passed`；迁移 `8 passed`、直接回归 `12 passed`、
  Console 直接影响 `44 passed`、验收器合同 `10 passed`、lint `164 files`、中文注释门禁通过。
  开发库为 `53/0053`，完整性 `ok`、外键违规 0；真实非流式、SSE、探测、最新源码 HTTP 和 Edge 双视口
  最终批次均通过。设计、API、测试与浏览器证据见
  [PRD-061 设计](docs/prd/PRD-061-internal-provider-status-page.md)、
  [Provider 状态 API](docs/api/provider-status.md)、
  [PRD-061 测试记录](docs/testing/PRD-061-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd061-browser/run-20260801T040500-final/browser-acceptance.json`。
  其后 PRD-062 也已完成；`all_108_prds_complete=false`。

- PRD-062 已完成：Control 已注册配置导出、预检和原子导入三个专用接口，并以后端
  `config.transfer.read/write`、Session tenant、统一错误与 request/trace ID 执行授权和隔离；0054 建立
  不可变导入/导出事实并把权限目录提升到 revision 2。`gatewayctl` 已提供严格 JSON、本地与服务端双重资源
  门禁、同 origin 网络约束、原子导出和 active Virtual Key 的 apply 前私有文件预留。导出事实不保存正文，
  重复 request ID 返回显式冲突；导入 replay 的 HTTP 报告使用本次 trace，而 Webhook 补投保留首次提交 trace。
  最终 API `6 passed`、CLI `33 passed`、Audit `13 passed`、迁移 `3 passed`、直接回归 `21 passed`；两套
  独立数据库为 `54/0054`、checksum 一致、完整性 `ok`、外键违规 0。真实 CLI import/export/dry-run/apply/
  replay、Viewer 403、跨租户 404、Gateway 失败追踪和 5 Token 成功用量均通过。权威记录见
  [PRD-062 测试记录](docs/testing/PRD-062-tests.md)。其后 PRD-063 也已完成；
  `all_108_prds_complete=false`。

- PRD-063 已完成：Shared 发布不可变九类 Adapter spec，冻结目录为 `adapter-spec-v1`、`9 specs`、
  `13633 bytes`、SHA-256 `9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870`；
  智谱、通义、文心、火山、混元和 DeepSeek 属于默认模型厂家/云服务，客户 Endpoint 与迁移入口分离，
  OpenRouter/硅基流动不进入默认厂商集合。Gateway 在联网前校验 spec/auth/version/hash，并按 spec 处理
  stream usage；Control 提供 `GET /adapter-specs` 和内部激活接口，以 Session tenant、实时
  `provider.health.read`、服务 Token、幂等、统一错误和 request/trace 执行隔离。0055 保存 tenant-scoped
  activation，PAGE-090 提供三分类九类目录。最终 Shared/Gateway `86 passed`、Control `7 passed`、0055
  `3 passed`、Provider/Health `14 passed`、Webhook `8 passed`、Console `32 passed`、浏览器验收器
  `8 passed`，中文注释门禁通过；开发库为 `55/0055`、完整性 `ok`、外键违规 0。真实 DeepSeek 非流式、
  通义流式、429、权限、租户、用量/成本/失败追踪和 Edge `1280x720/390x844` 均通过。设计、API 与证据见
  [PRD-063 设计](docs/prd/PRD-063-domestic-model-endpoint-adapter-specification.md)、
  [Provider Adapter 规范 API](docs/api/provider-adapter-specs.md)、
  [PRD-063 测试记录](docs/testing/PRD-063-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd063-browser/run-20260801T100000-final/browser-acceptance.json`。其后
  PRD-064 也已完成；`all_108_prds_complete=false`。

- PRD-064 已完成：Gateway 为九类冻结 Adapter spec 提供 exact-set mock 合并门禁、两类受控真实 probe、
  canonical 报告和 CI 非零阻断；Control 提供 tenant-scoped 内部上报、列表/详情、实时
  `provider.health.read` 与 `adapter.contract_failed` 可恢复 Outbox；0056 保存不可变 run/case/event，
  PAGE-091 提供摘要、筛选、真实 cursor 分页、详情及 403/503 安全状态投影。最终 mock `92/92`、Gateway
  `41/41`、受影响 ASGI `4/4`、Control `5/5`、PAGE-091 `15/15`、浏览器合同 `10/10`、Node `33/33`、
  lint `175 files` 和中文注释门禁均通过；开发库为 `56/0056`、完整性 `ok`、外键违规 0，三服务
  `200/200/200`。真实 Control RBAC/双租户/Outbox、Gateway probe 和 Edge `1280x720/390x844` 均通过。
  Python 根级一小时超时及 Console 首次单失败均按测试记录分层闭环，不虚写为一次全量零失败；5 个越过
  用户目录边界的 PRD-050 旧静态证据测试未运行、未计为通过。权威边界见
  [PRD-064 设计](docs/prd/PRD-064-provider-adapter-contract-test-framework.md)、
  [契约运行 API](docs/api/provider-adapter-contract-runs.md) 和
  [PRD-064 测试记录](docs/testing/PRD-064-tests.md)。其后 PRD-065 也已完成；
  `all_108_prds_complete=false`。

- PRD-065 已完成：Control 新增 tenant-scoped `RoleDashboardConfig`、五类视图、六角色实时 permission
  交集、六类有界聚合卡片和 `GET /dashboard/role-view`；0057 完成配置、新租户初始化和
  `dashboard.role_viewed` 事件扩展；PAGE-092 提供后端下发范围内的内存视图切换、迟到响应淘汰及
  401/403/503 安全状态。最终后端 `3/3`、迁移 `19/19`、受影响 Python `17/17`、PAGE-092 `14/14`、
  旧 Dashboard `6/6`、Provider Health 受影响组合 `14/14`、浏览器合同 `6/6`、lint `178 files` 和中文
  注释门禁均通过。开发库为 `57/0057`、完整性 `ok`、外键违规 0，三服务 `200/200/200`；六角色真实
  HTTP、第二租户、实时撤权/恢复、事件防伪和 Edge `1280x720/390x844` 均通过，浏览器与数据库敏感
  扫描为 0。设计、稳定接口、测试和最终浏览器证据见
  [PRD-065 设计](docs/prd/PRD-065-multi-role-home-navigation.md)、
  [角色首页 API](docs/api/role-dashboard.md)、
  [PRD-065 测试记录](docs/testing/PRD-065-tests.md) 和
  Edge 验收保留在本机 `.artifacts/prd065-browser/run-20260801T225656-CST/browser-evidence.json`。当时识别的
  下一项 PRD-066 和其后的 PRD-067 现已完成；当前按 Excel 进入
  `PRD-068 | M2-企业增强 | P2 | 策略模板库 | 行业/场景策略模板库`；`all_108_prds_complete=false`。

## 目录结构

```text
gateway-data-plane/   数据面热路径：API Listener、鉴权、限流、预算、路由、Provider Adapter、Usage
control-plane-api/    控制面低频管理 API：租户、用户、Provider、模型、Virtual Key、账单、Webhook、审计
console-web/          企业管理员和开发者控制台
shared-sdk/           OpenAPI、JSON Schema、Python/TypeScript SDK 与共享契约
examples/python/      基于官方 openai 客户端的 Chat、Embeddings、超时、重试与错误处理示例
examples/js/          基于官方 openai Node 客户端的非流式/流式 Chat、安全错误与浏览器 Key 防护示例
scripts/              本地开发、CI、数据库、发布、预检脚本
infra/                Docker、Compose、迁移、种子数据等基础设施文件
docs/                 PRD 设计、架构、API、ADR、开发、Runbook、安全、测试文档
```

更详细说明见 [目录边界说明](docs/development/directory-map.md)；代码注释要求见 [代码中文注释规范](docs/development/code-commenting-standard.md)。

## 本地启动

准备 Python 和 Node.js。Codex 环境里可使用内置 Python；普通开发机可以使用系统 Python。

PowerShell 示例：

```powershell
Copy-Item .env.example .env

# 可选：指定 Python 路径
$env:METAHUB_PYTHON="python"

# 方式一：一键启动三个空壳服务
.\scripts\dev\start-all.ps1
```

一键启动后访问：

- Gateway Health: <http://localhost:8000/health>
- Control API Health: <http://localhost:8001/health>
- Console Web: <http://localhost:5173>

也可以分别启动：

```powershell
.\scripts\dev\start-gateway.ps1
.\scripts\dev\start-control-api.ps1
.\scripts\dev\start-console.ps1
```

## 单机 Docker Compose 部署

PRD-028 后，研发和试点客户可以使用 Compose 在单台服务器启动 MVP：

```powershell
.\scripts\deploy\install.ps1
```

默认 profile 为 `local-sqlite`，启动 `gateway`、`control-api`、`console`，并使用持久化 SQLite volume 保存 MVP 数据。`compose-full` profile 会额外启动 Redis、PostgreSQL 和 MinIO，但当前应用运行时仍只支持 `sqlite://`，PostgreSQL 真正接入需要后续驱动和迁移方言 PRD。

常用命令：

```powershell
.\scripts\deploy\install.ps1 -DryRun
.\scripts\deploy\start.ps1
.\scripts\deploy\status.ps1
.\scripts\deploy\smoke.ps1
.\scripts\deploy\stop.ps1
```

`infra/compose/.env.compose` 由 install 脚本生成并已加入 `.gitignore`。首次生成时 CLI 只在 stdout 返回一次默认管理员密码；部署事件和日志不会记录管理员密码、Provider Key、Virtual Key、Token Secret、数据库密码或 MinIO Secret。详细说明见 [单机 Docker Compose 部署](docs/deployment/single-node-compose.md)。
## 测试与检查

```powershell
.\scripts\ci\check.ps1
```

该命令会运行：

- 后端 pytest：覆盖 `/health`、`/version`、统一错误码、request_id、配置异常、敏感日志脱敏、Project/Application 归属、日志/用量过滤和预算 scope。
- 配置中心测试：覆盖 `.env/配置文件/环境变量` 优先级、Provider Base URL 读取、敏感配置脱敏、prod 内部 token 权限。
- 可观测测试：覆盖 trace_id 透传、`X-Trace-ID`、`/internal/health`、未捕获异常归一、并发请求 trace 不串。
- 健康检查测试：覆盖 `/health/live` 不依赖 DB、`/health/ready` DB 成功/失败、version 构建信息、敏感信息不泄露。
- OpenAI-compatible 测试：覆盖 chat JSON、chat SSE、embeddings、Virtual Key 鉴权、参数校验、上游错误、request_logs 和 usage_ledger。
- Virtual Key 测试：覆盖创建、一次性明文展示、哈希存储、禁用、禁用后鉴权失败、`last_used_at`、viewer 权限拒绝。
- Provider 测试：覆盖创建至少 2 个 Provider、连接测试成功/失败、禁用、developer/viewer 权限拒绝、base_url/type/auth_type 校验、重复 code 和前端 admin-only 面板；Provider 创建/更新运行日志不记录 URL 或 hash，持久审计只保存完整规范 URL 的 SHA-256 变化摘要。
- 模型目录测试：覆盖新增/编辑/禁用模型、developer 只读、viewer 拒绝、内部模型 ID 到 `provider_model_id` 映射、价格字段和前端权限视图。
- Provider Adapter 测试：覆盖 Adapter 请求构造、OpenAI-compatible 和通用 HTTP 工厂选择、usage 归一化、上游错误归一化和日志不泄漏 Provider Key。
- Provider KeyPool 测试：覆盖 Key 加密落库、列表不回显明文/密文、禁用 Key 不参与路由、数据库 Key 优先选择、上游 429 后临时摘除、developer/viewer 权限拒绝和前端 KeyPool 面板；环境 Key 只允许非生产 active 默认租户中 code 和数据库/冻结 Route Endpoint 均匹配运维基准的 active Provider 使用，生产或其他租户不得复用进程级 Secret。
- 模型别名测试：覆盖 `ModelAlias` 创建/读取/禁用、developer 只读、viewer 拒绝、别名路由到默认模型、默认模型禁用后 fallback、禁用别名/无可用模型的可解释错误和 `routing_decisions` 落库。
- 成本/可用性路由自动化：覆盖 shared 两阶段确定性排序、严格 ModelAlias wire/CAS/幂等/RBAC/租户、Gateway Health/KeyPool/价格最小依赖矩阵、Decision/Candidate 原子事实、Dry-Run parity、调用日志强类型候选、CNY Budget/极端价格、主备未结算分项、Provider 环境 Key 所有权/Endpoint 绑定、Provider URL 日志/审计安全和 Browser owner-safe cleanup。PRD-047 最终通过 Python `574`、Node 示例 `33`、Console `228`、Console lint `106 files`、0036 运行库、真实三服务/Webhook/Usage/隐私清理和三档 Chrome 验收，证据见 [PRD-047 测试记录](docs/testing/PRD-047-tests.md)。
- 调用反馈与采纳率测试：覆盖 Control Session 与 Gateway Virtual Key 双入口、租户/RBAC/防枚举、严格输入与幂等冲突、不可变 Feedback/Event/Aggregate、RequestLog/ModelQuality 投影、`feedback.created`、0038/0039 正反迁移和冲突守卫、PAGE-070/071、Runtime/Browser/隐私/清理。最终证据为 Python `783`、Console `287/287`、39 条迁移/最高 0039 和 15 张浏览器 PNG，详见 [PRD-049 测试记录](docs/testing/PRD-049-tests.md)。
- 调用日志测试：覆盖 `/request-logs` 列表/详情、request_id/status/model/provider/error_code 过滤、分页、viewer 后端拒绝、失败请求落库、token/成本回写和敏感字段不回显。
- 用量与成本测试：覆盖 `/usage/summary`、`/usage/records`、同步/异步 CSV 导出、固定 42 列、UTF-8 BOM/CRLF、Decimal 金额、公式防护、只增快照水位、lease/重试/过期、admin-only 后端权限、跨租户隔离、0032 正反迁移、真实 usage 与估算 usage 区分、价格快照和非法筛选/大数据边界。
- 预算告警测试：覆盖 `/budgets`、`/budget-status`、租户级/Virtual Key 级日预算、80% 告警、100% 阻断或仅告警、重复告警去重、预算命中日志、viewer/developer 权限和用量概览摘要。
- 基础 DLP 测试：覆盖 `/dlp/rules` admin-only 管理、正则安全校验、`block/log_only/allow` 数据面行为、只扫描 user messages、调用日志 DLP 摘要、日志详情哈希展示和敏感明文不泄露。
- 策略引擎测试：覆盖六个白名单变量、五类动作、DSL 注入阻断、优先级与可解释求值、超时 fail-closed、admin-only 后端权限、跨租户与 route_to 目标隔离、If-Match 并发版本、事务/事件/Webhook 故障、0033 正反迁移和控制台异步状态安全。
- 策略快照测试：专项覆盖双 CAS 发布、单调新版本回滚、租户/RBAC、内部 Token/ETag、不可变制品双 hash、按租户 singleflight/304、LKG/emergency LKG/硬过期、请求 claim、`deny/route_to`、复合 request_id、调用日志版本筛选和 0034 正反迁移；PRD-045 全量回归、运行库、真实 API 和浏览器验收状态以 [PRD-045 测试记录](docs/testing/PRD-045-tests.md) 为准。
- 策略预演测试：专项覆盖 admin-only 后端授权、tenant-qualified 上下文、严格有界 JSON、Policy/路由/DLP/预算只读短路、稳定 request_id 三事实、Webhook、安全摘要、零 Provider/Usage 成本、0035 往返迁移、结果未知补偿和 Console Prompt 生命周期；完整自动化、真实 API、运行库和浏览器证据以 [PRD-046 测试记录](docs/testing/PRD-046-tests.md) 为准。
- Webhook 测试：覆盖 Endpoint 注册、最近 100 条记录、事件/状态筛选、一次性 `signing_secret`、新旧 HMAC 验签、3 次失败重试、串行/并发重放幂等、不同键再次重放、Secret 轮换、跨租户 404、viewer/匿名拒绝、递归敏感信封拒绝、0031 正反迁移和前端响应式交互。
- Dashboard 测试：覆盖 `/dashboard/overview` 当前租户今日请求、成功率、错误率、P95、成本、Top 模型、Top Virtual Key、最近错误、空状态、viewer 只读访问、未登录统一错误和 `dashboard.viewed` 事件。
- i18n 基础框架测试：覆盖 `/user/preferences` 默认中文、切换英文、`user.locale_changed` 事件、非法 locale、夹带 `tenant_id` 拒绝、未登录统一错误、`GET /error-codes?locale=` 本地化 label、语言设置页渲染和前端 fallback。
- 控制台骨架测试：覆盖 `/console/navigation`、`/console/preferences`、`/console/events`、角色化导航、非法入口、偏好恢复、敏感事件 payload 拒绝和 `tenant_id` 不由前端传入。
- 开发者快速开始测试：覆盖 `/developer/quickstart-data`、Python/JavaScript/curl 示例、Key 环境变量占位、模型别名推荐、viewer 后端拒绝和 `developer.quickstart_viewed` 事件。
- Python SDK 示例测试：覆盖 Chat、Embeddings、环境变量、超时/重试、request_id、安全 ZIP、租户/RBAC、下载事件和控制台交互。
- JavaScript/Node 示例测试：覆盖真实官方客户端非流式/流式调用、SSE 总时限、安全错误、浏览器 Key 防护、双语言下载页、租户/RBAC 和 `sdk.example_downloaded` 事件。
- 直连迁移指南测试：覆盖可信 Gateway URL、Environment 与模型别名选择、Python/Node 前后代码、错误差异、治理安全回滚、租户/RBAC、访问事件去重和直连到 MetaHub 的真实调用闭环。
- 竞品定位测试：覆盖中立文案、三角色帮助页、官方来源、Provider 分类防误用、`help.competitor_viewed` 租户事件、0030 往返迁移、权限过滤和响应式 tabs。
- 内置 API 调试台测试：覆盖 `/debug/chat-completions` 成功调试、viewer 后端拒绝、非法参数/敏感字段拒绝、上游失败归因、`request_logs`/`usage_ledger`/`debug_console_events` 写入和前端响应渲染。
- 智能错误提示测试：覆盖 `ErrorDiagnosisRule` 静态规则、`GET /error-codes`、统一错误响应 `diagnosis`、Provider 401/429/402/404/timeout/参数错误归一、调用日志详情诊断、调试台/日志详情诊断卡片 HTML 转义和敏感信息不泄露。
- Virtual Key 限流测试：覆盖 `PATCH /virtual-keys/{id}/limits`、RPM/TPM/并发超限、不同 Key 隔离、Redis 不可用 fail closed、失败 request_logs、前端限制表单和 HTML 转义。
- 管理审计测试：覆盖 Provider/两类 Key/预算/DLP 管理变更、同事务回滚、admin-only 查询、跨租户隔离、不可变记录、动作和 JSON 约束、IP/User-Agent 清洗、摘要脱敏、最小 Webhook 事件和控制台响应式交互。
- Provider 健康测试：覆盖 0025 迁移、手工/周期领取、失败转红与恢复转绿、逐 Key 认证状态、固定错误分类、租户/RBAC、探测日志、Webhook 状态转换、数据面预请求避障、过期 Fail Open、Dashboard 摘要和响应式健康页。
- 非流式 Fallback 测试：覆盖 0026 正向/回滚迁移、策略幂等版本、admin 写/developer 读/viewer 拒绝、主模型 5xx 后备用成功、流式禁止切换、最多两次 attempt、主备预算总和、最终模型用量关联、失败用量 unavailable、事件脱敏和响应式策略编辑器；PRD-047 已锁定原始 `reserved_cost_cny` 不改写，且主失败备成功仅把主估算保留在 `outstanding_cost_cny`。
- SSE 流式代理测试：覆盖 0027 正向/回滚迁移、StreamSession 租户与状态约束、正常多块透传、首块/块间超时、ASGI 客户端断开、上游读异常、幂等关闭、部分 Usage、`stream.timeout` 脱敏事件、调试台增量预览和主动停止。
- 控制台 Node test：覆盖服务状态读取、控制台主框架渲染、导航 active 状态、HTML 转义和后端不可用时的页面降级。
- 控制台轻量 lint：检查基础格式。
- 部署包测试：覆盖 Compose 文件结构、DeploymentProfile、env 占位符、dry-run、volume 删除保护、env secret 生成脱敏和 Dockerfile 入口。

## 数据库迁移与种子数据

当前本地可运行实现使用标准库 SQLite，便于在没有 PostgreSQL 驱动的机器上验证迁移框架、表约束、seed 幂等和敏感值不落库。生产 PostgreSQL 接入会沿用同一 CLI 边界。

本地示例：

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
$env:METAHUB_SEED_ADMIN_PASSWORD="change-me-locally"
$env:METAHUB_SEED_PROVIDER_API_KEY="replace-with-provider-key"
$env:METAHUB_SEED_VIRTUAL_KEY="mhv_dev_seed_virtual_key"

.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```

已创建的 MVP 表包括：

- `tenants`
- `users`
- `virtual_keys`
- `providers`
- `provider_credentials`
- `models`
- `model_quality_scores`（0037，PRD-048）
- `model_quality_score_events`（0037，PRD-048）
- `request_feedbacks`（0038，PRD-049）
- `request_feedback_events`（0038，PRD-049）
- `model_feedback_statistics`（0038，PRD-049）
- `model_aliases`
- `model_alias_routing_policies`（0036 创建；0037 扩展质量配置）
- `routing_decisions`
- `routing_candidates`（0036 创建；0037 扩展冻结质量事实）
- `routing_policy_events`（0036 创建；0037 增加 v2 配置事件）
- `request_logs`
- `usage_ledger`
- `price_snapshots`
- `finops_ledger_entries`
- `finops_adjustments`
- `budget_rules`
- `budget_alert_events`
- `budget_evaluations`
- `budget_reservations`
- `budget_blocks`
- `budget_block_events`
- `budget_scope_events`
- `dlp_rules`
- `dlp_matches`
- `webhook_endpoints`
- `webhook_deliveries`
- `webhook_delivery_attempts`
- `provider_health_probe_logs`
- `stream_sessions`
- `user_preferences`
- `user_preference_events`
- `console_page_events`
- `developer_portal_events`
- `debug_console_events`
- `policies`
- `policy_rules`
- `policy_actions`
- `policy_events`
- `policy_explanations`
- `policy_snapshots`
- `policy_snapshot_items`
- `policy_snapshot_heads`
- `policy_snapshot_events`
- `gateway_request_claims`
- `provider_adapter_catalog_activations`（0055，PRD-063）
- `adapter_contract_runs`（0056，PRD-064）
- `adapter_contract_cases`（0056，PRD-064）
- `adapter_contract_event_outbox`（0056，PRD-064）
- `sessions`
- `roles`
- `schema_migrations`
- `seed_runs`

0039 不新增业务表；它以六个 `BEFORE INSERT/UPDATE` 冲突守卫补齐 Feedback、Event、已反馈 RequestLog 与最终 RoutingAttempt 的 `INSERT/UPDATE OR REPLACE` 防改写边界，并保留 0038 的回滚事实门禁。

Provider Key、Virtual Key、管理员密码不会明文落库；seed CLI 只输出可安全展示的 prefix/fingerprint 信息。

## 管理员登录与 Session

本地登录依赖数据库迁移和 seed 管理员。首次使用控制台前先运行：

```powershell
$env:METAHUB_DATABASE_URL="sqlite:///./.data/metahub.db"
$env:METAHUB_SEED_ADMIN_PASSWORD="change-me-locally"
$env:METAHUB_AUTH_TOKEN_SECRET="change-me-auth-token-secret"

.\scripts\db\migrate.ps1
.\scripts\db\seed.ps1
```

默认 seed 管理员：

- 租户：`default`
- 邮箱：`admin@metahub.local`
- 密码：来自 `METAHUB_SEED_ADMIN_PASSWORD`

认证接口：

- `POST /auth/login`：使用本地账号登录，返回 Bearer Token 和 Session。
- `GET /auth/me`：校验 `Authorization: Bearer <token>` 并返回当前用户、租户和 Session。
- `POST /auth/logout`：撤销当前 Session。

安全约束：

- 数据库只保存 `token_hash`，不保存明文 Bearer Token。
- 错误密码会累计失败次数，达到阈值后临时锁定账号。
- 缺失、伪造、过期、已登出的 Token 都返回统一错误结构和 `request_id`/`trace_id`。
- 后续管理 API 应复用后端 `require_current_admin`，不能只靠控制台隐藏按钮。

## 当前租户模型

M0 使用 seed 默认租户 `default`，并提供当前租户接口。租户接口只使用登录 Token 恢复出的 `tenant_id`，不接受前端传入其他租户 ID 来选择目标。

租户接口：

- `GET /tenant/current`：读取当前登录用户所属租户。
- `PATCH /tenant/current`：管理员更新当前租户的 `name` 和 `service_mode`。

安全约束：

- 普通接口没有跨租户查询、切换或更新入口。
- 请求体中的 `tenant_id`、`slug`、`status` 不允许作为更新字段。
- 非 admin 角色更新租户时，后端返回 `auth.permission_denied`。
- 所有 MVP 业务表必须包含 `tenant_id`，`schema_migrations` 作为基础设施表例外。

## 基础 RBAC 角色

M0 固定支持三种角色：`admin`、`developer`、`viewer`。角色目录存放在当前租户的 `roles` 表中，控制面用户表继续使用 `users.role` 保存当前角色。

用户管理接口：

- `GET /users`：管理员读取当前租户用户列表和角色选项。
- `POST /users`：管理员创建当前租户本地用户，初始密码只保存哈希。
- `PATCH /users/{id}/role`：管理员修改当前租户用户角色。

安全约束：

- `/users` 接口全部使用后端 `require_current_admin`，developer/viewer 直接调用会返回 `auth.permission_denied`。
- 用户创建和角色修改只能作用于当前认证上下文里的 `tenant_id`，请求体不能指定其他租户。
- API 响应不返回 `password_hash`、明文初始密码、Session 或 Token。
- Developer 不能新增 Provider；Viewer 不能创建 Virtual Key。后续业务接口必须复用后端角色依赖，而不是只靠控制台隐藏入口。
- 最后一个 active admin 不能被降级，admin 也不能把自己降级为非 admin。

## Virtual Key 管理

M0 支持 admin/developer 在控制面创建和管理当前租户 Virtual Key。业务系统使用 Virtual Key 调用 Gateway，避免直接暴露模型厂家 Provider Key。

Virtual Key 接口：

- `GET /virtual-keys`：列出当前租户 Key，支持 `status`、`environment` 过滤。
- `POST /virtual-keys`：创建 Key，响应只在本次返回 `plain_value`。
- `PATCH /virtual-keys/{id}`：更新名称、环境、备注或状态。
- `DELETE /virtual-keys/{id}`：软禁用 Key，不物理删除历史对象。

安全约束：

- 创建响应以外不再返回明文 Key。
- 数据库只保存 `key_hash`、`key_prefix` 和元数据。
- `key_hash` 不返回给前端；`key_prefix` 只用于排障展示。
- 禁用后的 Key 调用数据面返回 `gateway.auth_invalid`。
- 数据面成功鉴权后更新 `last_used_at`，便于管理员清理长期未使用 Key。
- viewer 后端直接调用 `/virtual-keys` 会返回 `auth.permission_denied`。

## Virtual Key 级基础限流

PRD-022 后，管理员和开发者可以为当前租户的每把 Virtual Key 设置 RPM、TPM 和并发限制。规则保存在 `virtual_key_rate_limit_rules`，每条业务数据带 `tenant_id`；运行时计数器使用 Redis Key，Key 名只包含 `tenant_id` 和 `virtual_key_id`，不包含明文 Virtual Key、Prompt、Response 或 Provider Key。

限流接口：

- `PATCH /virtual-keys/{id}/limits`：更新该 Key 的 RPM、TPM 和并发限制，`null` 表示清除对应限制。

限流行为：

- 未配置规则或规则未启用时，请求按原数据面链路继续执行。
- 达到 RPM、TPM 或并发限制时，数据面返回 429 `gateway.rate_limit_exceeded`，不会继续调用上游 Provider。
- 启用限流规则但 Redis 计数不可用时，数据面 fail closed，返回 503 `gateway.rate_limit_unavailable`。
- 超限和 Redis 不可用请求都会尽量写入 `request_logs`，并记录安全结构化事件 `rate_limit.exceeded` 或 `rate_limit.unavailable`。
- TPM 使用请求体长度做准入估算；真实 Token 和成本仍由 `usage_ledger` 在上游响应后记录。

## 租户、项目与 Key 日预算

PRD-023 建立租户和 Virtual Key 日预算，PRD-052 在不删除既有 Application 兼容作用域的前提下，将正式能力扩展为 `tenant/project/virtual_key` 三级累计 AND 门禁。预算按 Asia/Shanghai 业务日计算；已入账消费只读取 `finops_ledger_entries` 的 `posted usage_charge/debit`，再叠加尚未结算的 reservation，不能继续直接汇总 `usage_ledger.cost_cny`。达到 80% 生成 `budget.threshold_reached`，首次达到 100% 还会形成可去重的 `budget.scope_exceeded` 事实；规则可配置为仅告警或在 Provider 前阻断。

预算接口：

- `GET /budgets`：admin/developer 读取当前租户预算规则和今日状态，不接受前端传入 `tenant_id`。
- `POST /budgets`：admin 以 create-only 语义创建租户、Project 或 Virtual Key 规则；同一作用域重复创建稳定返回 409。
- `PATCH /budgets`：admin 更新或停用既有规则，并保留旧客户端兼容入口。
- `PATCH /budgets/{id}/mode`：admin 切换 `block/alert_only`，并按规则状态事务化处理恢复。
- `GET /budget-status`：admin/developer 按可信 Environment/Project/Application/Virtual Key 归属链查询聚合状态。

预算行为：

- 数据面在限流之后、上游调用之前执行预算检查；阻断时返回 402 `gateway.budget_exhausted`，不会继续调用上游 Provider。
- 预算规则、告警、评估、reservation、block 和 scope event 均带 `tenant_id`；控制面只按认证 Session 查询当前租户，并在后端校验 Environment/Project/Application/Key 归属。
- 告警事件按规则、业务日和阈值去重，避免同一预算重复刷屏。
- 预算命中摘要写入 `request_logs`，日志详情可查看 `budget_evaluations`，但不记录 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key。
- 控制台提供“预算告警”页面；Token 用量概览中也展示今日预算状态摘要，便于把成本趋势和预算阈值放在一起查看。

## 基础 DLP

PRD-024 后，管理员可以为当前租户配置关键词或安全正则 DLP 规则。M0 只扫描 Chat Completions 请求中的 `role=user` 文本，不扫描 system/assistant/tool 消息、文件、图片、Embeddings input 或模型响应。

DLP 接口：
- `GET /dlp/rules`：admin 读取当前租户规则，支持按状态、类型和动作过滤。
- `POST /dlp/rules`：admin 创建关键词或正则规则，不接受前端传入 `tenant_id`。
- `PATCH /dlp/rules/{rule_id}`：admin 更新规则名称、内容、动作、状态、优先级和描述。

DLP 行为：
- 数据面在鉴权和路由之后、限流和预算之前执行 DLP；`block` 命中返回 403 `gateway.dlp_blocked`，不调用上游 Provider。
- `log_only` 和 `allow` 命中后继续调用上游，并写入 `dlp_matches` 与 `request_logs` DLP 摘要。
- `dlp_matches` 只保存规则 ID、动作、消息索引、命中文本哈希和长度，不保存 Prompt、messages 原文、命中文本、Authorization、Virtual Key 明文或 Provider Key。
- 控制台提供“DLP 规则”页面；调用日志详情显示 DLP 决策和命中哈希，便于排障但不泄露敏感输入。
## Webhook 基础事件订阅

PRD-025 后，管理员和开发者可以为当前租户注册企业系统 Webhook Endpoint，订阅预算、DLP、调用失败等治理事件类型，并通过测试事件验证接收端签名和可达性。M0 完成 Endpoint 注册、事件类型订阅、HMAC-SHA256 签名、最多 3 次重试和发送记录；生产治理事件的具体触发点会在后续相关 PRD 按同一事件信封接入。

Webhook 接口：

- `GET /webhooks`：admin/developer 读取当前租户 Endpoint 和最近发送记录，不接受前端传入 `tenant_id`。
- `POST /webhooks`：admin/developer 创建 Endpoint，响应只返回一次 `signing_secret`。
- `POST /webhooks/{id}/test`：向当前租户指定 Endpoint 发送 `webhook.test` 事件，并返回 delivery 与 attempts 摘要。
- `GET /webhooks/{id}/deliveries`：按事件类型和状态读取指定 Endpoint 最近最多 100 条记录；响应不包含事件正文或重放幂等键。
- `POST /webhooks/{id}/replay`：仅允许重放 failed delivery；请求体只提交来源 delivery ID 和 8-128 位幂等键。
- `POST /webhooks/{id}/rotate-secret`：原子轮换当前 Endpoint Secret，新明文只在本次响应中出现一次。

Webhook 行为：

- `webhook_endpoints`、`webhook_deliveries` 和 `webhook_delivery_attempts` 均带 `tenant_id`。
- 数据库只保存签名密钥密文、前缀和指纹；列表、发送记录和日志不返回 `signing_secret` 明文。
- 投递 payload 固定包含 `event_id`、`event_type`、`timestamp`、`tenant_id`、`trace_id` 和安全 `payload`，不包含 Prompt、Response、Authorization、Virtual Key 明文、Provider Key 或 DLP 命中文本。
- HTTP 发送使用 `X-MetaHub-Signature: t=<unix_seconds>,v1=<hmac>`，签名输入为 `timestamp.body`，接收端可用 shared-sdk 的验签函数复用校验逻辑。
- 测试发送失败不会抹掉记录；系统保存最终 delivery 状态和每次 attempt 的 HTTP 状态、耗时、错误码与安全摘要。
- original delivery 按原事件 ID 幂等；replay delivery 按当前租户、Endpoint、来源 delivery 和幂等键唯一。同键重复调用回读同一记录且不再次出网，不同键允许新的人工重放。
- replay intent 与管理审计先在短事务中提交，再执行有界 HTTP 发送；Endpoint 必须保持 active，不能通过重放绕过停用状态。
- 每条 delivery 保存创建时的 `secret_version`；轮换事务同时写新密文、版本、操作者和 `webhook_endpoint.secret_rotated` 审计，事务失败时旧 Secret 继续有效。
- `webhook.replayed` 是安全内部事件，不进入 Endpoint 可订阅目录；审计和日志只保存 ID、事件类型、状态和版本，不保存 URL、正文、签名、Secret 或幂等键。
- 控制台提供详情筛选、刷新、失败重放和轮换确认；一次性 Secret 只留在当前页面内存，完整重载、登出或用户/租户/Session 变化时清除，不进入 localStorage/sessionStorage。

PRD-042 不包含 replay intent 已提交后发生进程崩溃的接管恢复，也不包含 `dead_letter`、对象存储、归档或过期调度。相同幂等键当前提供 at-most-once 出网语义；持久恢复与死信处理由 PRD-057 实现。

## MVP 总览 Dashboard

PRD-026 后，控制台概览页改为当前租户的 MVP Dashboard。它按 Asia/Shanghai 今日窗口从 `request_logs` 和 `usage_ledger` 实时聚合今日请求数、成功率、错误率、P95 延迟、今日成本、总 Token、Top 模型、Top Virtual Key 和最近错误；空租户显示接入引导，不展示空表格。

Dashboard 接口：

- `GET /dashboard/overview`：读取当前登录租户的今日聚合摘要，不接受 `tenant_id`、日期或跨租户筛选参数。

Dashboard 行为：

- 指标事实来源是 `request_logs` 与 `usage_ledger`，`DashboardMetric` 只是响应 DTO，不新增缓存表。
- `console_page_events.event_type` 支持 `dashboard.viewed`，用于审计概览页访问；事件 payload 仍走白名单，不保存 Token、Authorization、Prompt、Response 或密钥。
- API 允许已认证的 admin/developer/viewer 读取安全聚合摘要；调用日志详情和用量明细仍由各自后端权限依赖限制。
- Top Virtual Key 只返回 `virtual_key_id`、名称和 `key_prefix`，不返回 Virtual Key 明文、哈希、密文或 Provider Key。
- 最近错误只展示 `request_id`、错误码、模型、Provider、延迟、时间和安全诊断标题；不会读取或回显上游原始响应、Prompt 或 messages。
- Dashboard 查询索引在 `0018_dashboard_overview.sql` 中维护，查询必须始终带认证上下文中的 `tenant_id` 和今日时间窗口。

## i18n 基础框架

PRD-027 后，控制台具备用户级语言偏好框架。默认语言为 `zh-CN`，当前支持 `zh-CN` 和 `en-US`；新增语言设置页、控制台骨架新增文案和错误码展示 label 通过 i18n 字典渲染。历史页面不会在 M0 一次性全量翻译，后续 PRD 修改到的页面必须把新增文案接入 key。

i18n 接口：

- `GET /user/preferences`：读取当前登录用户的语言偏好和可选语言，不接受 `tenant_id`、`user_id` 或角色参数。
- `PATCH /user/preferences`：只允许提交 `{ "locale": "zh-CN" | "en-US" }`，不允许夹带 `tenant_id`、`user_id`、Token 或业务上下文。
- `GET /error-codes?locale=zh-CN`：错误码目录在保持英文稳定 `code/message/suggestion` 的同时返回展示层 `label`。

i18n 行为：

- `user_preferences.locale` 保存当前用户语言偏好；`user_preference_events` 只记录稳定事件 `user.locale_changed`、old/new locale 和链路 ID。
- 前端 `createI18n()` 按当前 locale、默认中文、key 的顺序 fallback；未知 locale 回退到中文。
- 日志、Webhook event_type、错误码和数据库 code 字段继续使用稳定英文 code，不把中文展示文案写入内部字段。

## Provider 管理

M0 支持管理员注册当前租户的模型 Endpoint Provider，包括智谱、通义、文心、火山、混元、DeepSeek 官方或云上 Endpoint，以及客户本地 OpenAI-compatible Endpoint。OpenRouter 和硅基流动只作为对标平台、竞品平台或迁移兼容分类，不作为默认模型厂商类别。

Provider 管理接口：
- `GET /providers`：管理员读取当前租户 Provider 列表，支持 `status` 和 `provider_type` 过滤。
- `POST /providers`：管理员创建 Provider，字段包括 `provider_code`、`name`、`provider_type`、`base_url`、`auth_type`、`description`。
- `PATCH /providers/{id}`：管理员更新 Provider 元数据或启用/禁用 Provider。
- `POST /providers/{id}/test`：管理员测试 Endpoint 可达性，返回成功/失败原因并更新健康状态。
- `GET /providers/{id}/keys`：管理员读取该 Provider 的 KeyPool 元数据，不返回明文或密文。
- `POST /providers/{id}/keys`：管理员新增 Provider Key，明文只出现在请求体，落库前使用 AES-GCM 加密。
- `PATCH /provider-keys/{id}`：管理员更新 Key 名称、权重或启用/禁用状态。

安全约束：
- `/providers` 全部复用后端 `require_current_admin`，developer/viewer 直接调用会返回 `auth.permission_denied`。
- Provider 注册 API 不接收 Provider Key 明文；Provider Key 由 KeyPool API 管理。
- Provider 和 KeyPool 列表只返回 Endpoint/Key 元数据，不返回 `plain_value`、`encrypted_key`、密钥明文或上游认证头。
- 连接测试失败会写入 Provider 健康字段和结构化日志事件 `provider.health_checked`，但不记录密钥。
- Provider 创建/更新的运行日志不记录 Endpoint 或 URL hash；持久审计只保存规范化完整 URL 的 `base_url_hash` 和非敏感字段。host/path 可包含客户内网拓扑或 Token，不得以“只删 query”作为脱敏。

## Provider KeyPool

PRD-014 后，管理员可以为同一个 Provider 配置多个上游 API Key。数据面调用上游时优先从当前租户、当前 Provider 的健康 Key 中选择一个；禁用 Key 和未过期的 `rate_limited` Key 不参与路由。上游返回 429 或明确限流错误时，网关会把本次使用的 Key 临时标记为 `rate_limited` 并记录 `provider_key.rate_limited` 事件。

KeyPool 接口：
- `GET /providers/{id}/keys`：返回 `provider_key_id`、`key_prefix`、`key_fingerprint`、`weight`、`status`、最近使用和最近错误元数据。
- `POST /providers/{id}/keys`：请求字段为 `name`、`plain_value`、`weight`；响应不回显 `plain_value`。
- `PATCH /provider-keys/{id}`：支持 `name`、`weight`、`status=active|disabled`。

安全约束：
- Provider Key 落库格式为 `aesgcm-v1:<nonce>:<ciphertext>`，生产环境必须显式配置 `METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET`。
- 响应、日志、`request_logs`、`usage_ledger` 都不记录 Provider Key 明文或 Authorization header。
- 已存在的 seed 占位 Key 不可解密，不参与真实 KeyPool 选择。不存在托管 Key 时的环境 fallback 只是受控本地兼容路径：必须是非生产 active 默认租户、active Provider，且 Provider code 和数据库/冻结 Route Endpoint 与运维基准精确匹配。生产或其他租户必须配置自己的托管 Key。

## 模型目录

M0 支持当前租户维护统一 ModelCatalog。对外调用方使用 `internal_model_id` 作为 OpenAI-compatible 请求体里的 `model`；数据面再解析到 Provider 的 `provider_model_id` 并转发给上游。数据库中的 `models.model_id` 保持为技术主键，用于日志和外键关联。

模型目录接口：
- `GET /models`：admin/developer 读取当前租户模型目录，支持 `status`、`provider_id` 和 `capability` 过滤。
- `POST /models`：admin 创建模型，字段包括 `internal_model_id`、`provider_id`、`provider_model_id`、能力标签、上下文长度、输入/输出价格、币种和备注。
- `PATCH /models/{id}`：admin 更新 Provider 绑定、上游模型 ID、能力、价格或启用/禁用状态。

安全约束：
- 所有模型目录数据按认证上下文中的 `tenant_id` 查询和更新，不接收请求体传入的租户 ID。
- developer 只读，viewer 后端拒绝；控制台隐藏入口只是体验优化。
- 响应不返回 Provider Key、Virtual Key、Authorization 或任何上游认证材料。
- 禁用模型后数据面无法继续通过该内部模型 ID 路由，返回 `gateway.model_not_found`。

## 模型别名与最小路由

PRD-015 后，调用方可以在 OpenAI-compatible 请求体的 `model` 字段传入稳定别名，例如 `company-default`。数据面先使用当前 Virtual Key 的 `tenant_id` 和 `environment` 解析 `model_aliases`；如果没有同名别名，再兼容 PRD-012 的内部模型 ID 直连路径。别名命中后，默认模型可用则选默认模型；默认模型禁用或 Provider 不可用时，尝试管理员配置的备用模型。M0 不做 DSL、质量路由、灰度路由或流式中途切换。

模型别名接口：
- `GET /model-aliases`：admin/developer 读取当前租户别名，支持 `status` 和 `environment` 过滤。
- `POST /model-aliases`：admin 创建别名，字段包括 `alias`、`environment`、`default_model_id`、`fallback_model_id`、`description`。
- `PATCH /model-aliases/{id}`：admin 更新别名、环境、默认模型、备用模型、备注或启用/禁用状态。

路由追踪：
- 每次别名解析成功会写入 `routing_decisions`，记录 `request_id`、`trace_id`、请求别名、真实模型、Provider 和 `decision_reason`。
- 别名禁用返回 `gateway.model_alias_disabled`；默认/备用模型均不可用返回 `gateway.model_disabled`；失败请求仍写入 `request_logs`。
- `routing_decisions`、`request_logs` 和 `usage_ledger` 不记录 prompt/input/messages、Virtual Key 明文、Provider Key 或 Authorization。

## Request Log 最小闭环

PRD-016 后，网关每次鉴权成功后的调用都会尽量写入 `request_logs`：成功请求记录 `request_id`、`tenant_id`、`virtual_key_id`、`endpoint`、请求模型、解析后的内部模型、Provider、状态、延迟、token 和成本估算；失败请求记录统一错误码，便于用 `request_id` 串起控制台、日志和数据库排障。PRD-045 的 0034 把日志身份升级为 `(tenant_id, request_id)` 复合主键；已认证模型请求在读取 JSON 前先创建不可变 `gateway_request_claims`，冻结 `trace_id`、Virtual Key、实际 `snapshot_version` 和 LKG 状态，后续非流式、Fallback、流式终态和 Usage 不能改写该版本。`usage_ledger` 继续保存用量账本，并通过同租户复合外键关联日志、把 token 与成本摘要回写到 `request_logs`。

调用日志控制面接口：
- `GET /request-logs`：admin/developer 查询当前租户调用日志，支持 `request_id`、`status`、`model`、`provider_code`、`error_code`、`environment_id`、`project_id`、`application_id`、`snapshot_version`、`limit`、`offset`。
- `GET /request-logs/{request_id}`：admin/developer 查询当前租户单条日志详情，返回路由决策摘要和用量摘要。

安全约束：
- `/request-logs` 全部使用后端 `require_request_log_reader`，viewer 直接返回 `auth.permission_denied`。
- 查询条件不接受 `tenant_id`，所有日志按登录上下文中的 `tenant_id` 过滤。
- `snapshot_version` 只能是正整数；列表和详情只返回快照版本、`current/lkg/emergency_lkg/expired` 等状态，以及 Policy ID/动作/reason/目标模型 ID 的安全摘要，不返回 DSL、AST 或上下文实际值。
- 控制台只展示 `key_prefix`，不展示 Virtual Key 明文、Provider Key、Authorization、Prompt、Response、messages 或 input。
- `request.logged`、`request_log.write_failed` 和 `usage_ledger.write_failed` 事件只记录安全元数据，写库失败不会反向中断网关热路径。

## Token Usage 与成本估算

PRD-017 后，`usage_ledger` 作为 UsageRecord 物理表保存请求级 Token 账本：`input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`、`cost_amount`、`cost_cny`、`cost_usd`、`currency`、`estimated`、`usage_source`、`price_snapshot_id` 和模型/Provider/Virtual Key 维度。`price_snapshots` 保存每次入账时使用的模型价格、币种和 `price_schema_version`，避免后续模型目录价格变更后历史成本不可解释。

用量控制面接口：

- `GET /usage/summary`：admin/developer 查询当前租户用量概览，支持 `start_date`、`end_date`、`model_id`、`virtual_key_id`、`provider_id`、`estimated`、`request_id`、`trace_id`，返回总量、按天、按模型和按 Virtual Key 聚合。
- `GET /usage/records`：admin/developer 分页查询当前租户 UsageRecord，支持同样筛选条件以及 `limit`、`offset`。
- `GET /usage/export.csv`：仅 admin 按当前规范筛选创建同步或异步 CSV 导出；小结果直接返回文件，大结果返回可查询的 `UsageExportJob`。
- `GET /usage/exports/{job_id}`：仅 admin 查询当前租户导出任务状态，不返回 CSV、lease 或存储路径。
- `GET /usage/exports/{job_id}/download`：仅 admin 下载当前租户已完成且未过期的完整制品，下载前复验长度和 SHA-256。

成本与估算口径：

- 上游返回 usage 时，Adapter 归一化 `input_tokens`、`output_tokens`、`total_tokens` 和 `cache_tokens`，`estimated=false`、`usage_source=upstream`。
- 上游缺少 usage 时，数据面只按请求/响应文本长度做粗略估算，不保存文本原文，`estimated=true`、`usage_source=estimated`。
- M0 不做复杂汇率回溯；人民币成本写 `cost_cny`，美元成本写 `cost_usd`，`cost_amount + currency` 保留原币种金额。

安全约束：

- `/usage/summary` 和 `/usage/records` 使用后端 `require_usage_reader`，viewer 直接返回 `auth.permission_denied`。
- CSV 导出从属接口使用更严格的后端 `require_usage_exporter`，当前仅 admin 可用；developer 的 Usage 只读权限不会隐式获得批量账本导出。
- 查询条件不接受 `tenant_id`，所有用量按登录上下文中的 `tenant_id` 过滤。
- 控制面 API 和控制台不返回 `raw_usage`，只展示规范化用量、脱敏 Key 前缀、价格快照 ID 和成本字段。
- `usage.recorded` 事件只记录 request_id、tenant_id、模型、Provider、Token、成本和价格快照，不记录 Provider Key、Authorization、Prompt 或 Response。

PRD-043 使用 `usage_export_ledger_sequences.usage_export_sequence` 的 AUTOINCREMENT 映射形成稳定账本水位；`UsageExportJob.snapshot_ledger_sequence` 只保存在任务内部，不进入 API、审计、事件或日志。0032 后 Usage Ledger 采用追加事实语义，已入账记录不能 UPDATE/DELETE；更正必须写新的调整事实。MVP 制品使用有界 SQLite BLOB 和单机 scheduler，不包含对象存储或分布式任务队列。

## 策略引擎 DSL 最小版

PRD-044 已完成设计、两轮自审、实现、专项/全量回归、0033 运行库迁移、真实 API、三服务、敏感扫描和三档浏览器验收。当前交付边界如下：

- `GET /policies`、`POST /policies`、`PATCH /policies/{policy_id}` 和 `POST /policies/validate` 均使用后端 `require_policy_admin`，只从登录上下文读取 `tenant_id`；developer、viewer 和匿名请求不能依赖前端隐藏绕过权限。
- 最小 DSL 只接受 `tenant/env/project/model/data_class/budget_remaining` 六个变量、有限比较与布尔组合，以及 `allow/deny/mask/log_only/route_to` 五类动作；不执行 `eval/exec`，也不支持函数、属性访问、递归、脚本或外部调用。
- `route_to` 只能引用当前租户 active `model_id`；不存在、禁用和跨租户目标统一拒绝。PATCH 强制使用强 `If-Match`，并在事务内权威重读版本，避免并发管理员静默覆盖。
- 策略、单规则、单动作和事件由 0033 迁移持久化，创建/更新审计与业务写入保持事务一致；`policy.created` 和 `policy.validation_failed` 只携带安全摘要，不保存 DSL 正文、Prompt、Response、Token 或 Provider Key。
- shared 求值器按 `priority ASC, policy_id ASC` 确定性执行；超过 `METAHUB_POLICY_EVALUATION_TIMEOUT_MS` 时固定 fail closed 为 `deny` 并要求告警。该配置默认 5 ms，允许范围 1-100 ms。
- `active` 当前只表示“下一快照候选”，不是已发布状态。本 PRD 不把策略挂入 Gateway 热路径；策略快照、发布/回滚和数据面消费由 PRD-045 交付，完整 Dry Run 页面由 PRD-046 交付。

详细设计见 [PRD-044 设计文档](docs/prd/PRD-044-policy-engine-minimal-dsl.md)，完整验收证据见 [PRD-044 测试记录](docs/testing/PRD-044-tests.md)。

## 策略快照版本化与发布

PRD-045 已完成。实现把 PRD-044 的 active Policy 候选显式发布为 tenant 内不可变版本，并把同一版本贯穿 Gateway 请求、调用日志与 Usage：

- `GET /policy-snapshots/current` 返回当前 head、active 候选 hash/数量及 `ETag`、`X-Policy-Candidate-ETag`；`GET /policy-snapshots?limit=&offset=` 返回当前租户历史。公共接口均为 admin-only、`Cache-Control: no-store`，不接受客户端 `tenant_id`。
- `POST /policy-snapshots/publish` 同时要求强 `If-Match` 和 `If-Policy-Candidate-Match`，在事务锁内重编全部 active Policy 后执行 head/candidate 双 CAS；`POST /policy-snapshots/{id}/rollback` 只允许复制当前上一版内容，并创建新的更高版本。例如 `v1=A -> v2=B -> rollback(v1)` 生成 `v3=A`，不会把 head 倒退而产生 ABA。
- 每次发布/回滚原子写入 header、items、head、持久事件和管理审计，提交后复用 Webhook 投递 `policy_snapshot.published`。事件和公共历史仅含版本、hash、Policy 数量、操作及安全 ID，不含 DSL、AST、Prompt、Response、Provider Key、Virtual Key、Authorization 或内部 Token。
- `GET /internal/policy-snapshots/{tenant_id}/current` 只接受独立 `X-Internal-Token` 服务身份，按 tenant 返回严格 wire 与强 ETag；304 仅刷新该 tenant 的新鲜度。未知/disabled tenant 与 active tenant 的“已确认无 head”使用不同合同。
- Gateway 在 Virtual Key 鉴权和 Environment 校验后、读取 JSON 前取得不可变 lease 并创建 `gateway_request_claims`。每个 tenant 首次请求 singleflight 拉取，随后 scheduler 仅轮询已知 tenant；合法新版本原子替换缓存，损坏、降版、同版异 hash、超大响应或网络失败保留 LKG。
- `internal_only` 在普通 max-age 后可进入有硬上限的 `emergency_lkg`，并继续执行鉴权、DLP、限流和预算；超过硬上限失败关闭。`public_service` 在普通 max-age 后即阻断；冷启动从未成功同步且没有 LKG 一律阻断。阻断请求仍先冻结 version/status claim，再写统一错误和 request_id。
- 请求只执行 lease 内 Policy。`deny`、求值超时、缺失可信上下文和未知动作在 Provider 前失败关闭；`route_to` 不继承原别名 Fallback，必须重新验证目标模型的 tenant、Environment、active Provider、健康和能力。Policy `allow/log_only` 不能绕过既有 DLP、限流、预算或 Provider Guard。
- 控制台为 admin 提供“策略发布”入口，展示当前版本、候选漂移、hash 摘要和历史；发布/上一版回滚必须填写原因并复用同一 request_id 处理结果未知的重试。developer/viewer 不加载页面，但真实授权仍由后端执行。调用日志列表/详情展示实际版本、缓存状态和安全决策摘要。
- `scripts/acceptance/prd045-runtime-acceptance.py` 执行真实三服务 v1/v2/v3、Gateway、内部 Token、Webhook、Usage 与敏感扫描；`scripts/acceptance/prd045-browser-acceptance.mjs` 使用一次性角色账号执行真实 Console 发布/回滚、后端 RBAC、调用日志和三档布局，并在异常路径按 tenant + 精确资源账本回收 Session、用户和临时 Policy。证据只写入工作区 `artifacts/prd045/`，密码和 Token 仅驻留验收进程内存。

0034 新增 `policy_snapshots`、`policy_snapshot_items`、`policy_snapshot_heads`、`policy_snapshot_events` 和 `gateway_request_claims`，把 `request_logs` 的身份升级为 `(tenant_id, request_id)`，并让 `usage_ledger`、`price_snapshots` 通过同租户复合外键关联请求。已发布快照、快照项、事件和请求 claim 由数据库触发器保护为不可变；down migration 遇到跨租户重复 request_id 时会安全中止，不会静默丢日志或错绑 Usage。

本地 Gateway 同步配置已经列入 `.env.example`：

```dotenv
METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED=true
METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS=2
METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS=2
METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS=300
METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS=3600
METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES=4194304
```

Gateway 和 Control API 必须配置相同的高熵 `METAHUB_INTERNAL_CONFIG_TOKEN`，Gateway 还必须使用 `METAHUB_CONTROL_API_BASE_URL` 指向 Control API。生产环境或 `public_service` 模式禁止关闭同步；生产同步 URL 必须使用 HTTPS，dev/test 的 HTTP 仅允许回环地址。普通 max-age 不得小于轮询间隔，emergency max-age 不得小于普通 max-age。配置摘要只显示脱敏值，日志不得输出 Token 或完整内部响应。

详细设计见 [PRD-045 设计文档](docs/prd/PRD-045-policy-snapshot-versioning-publishing.md)；专项、全量、运行库、真实 API、敏感扫描和浏览器完整证据见 [PRD-045 测试记录](docs/testing/PRD-045-tests.md)。最终结果为 Python `434 passed`、Node 示例 `33 passed`、Console `194 passed`、Console lint `100 files passed`，运行库 34 条迁移/最高 0034，三服务和三档浏览器验收通过。

## 策略模拟与单请求预演

PRD-046 已完成。管理员可在策略上线前用一条当前构造的上下文预演 active Policy 候选、路由、基础 DLP 和预算，不读取历史 Prompt，也不调用真实 Provider：

- `POST /policies/dry-run` 使用后端 `require_policy_admin`，只从登录态恢复 `tenant_id`，不接受客户端 tenant。正文以 256 KiB 上限流式读取，拒绝重复 JSON key、NaN/Infinity、非法 UTF-8、未知字段、非 object、深层递归和超限输入；actor 30 RPM、tenant 120 RPM 的有界进程内限额只保护本地 CPU。
- 模拟以 query-only 一致性连接读取当前租户 Environment、Project、Model、active Policy、路由、DLP、Usage、reservation 和预算事实，不调用 Adapter/KeyPool/客户 Endpoint，不创建 Gateway claim、request log、Usage、路由、DLP 或预算运行事实。deny、route unavailable、DLP block 和 budget block 都以 HTTP 200 返回 what-if 结论；协议错误继续使用统一错误码、request_id 和 trace_id。
- 完成结果在第二个写事务原子写入 `policy_explanations`、`policy_events` 和 `audit_logs` 三类安全事实；提交后才投递 `policy.dry_run_executed` Webhook。重复 `tenant + actor + request_id` 会先恢复并核对旧三事实、重派稳定事件，再返回 409，避免结果未知重试重复写事实。
- Prompt 仅在请求内存和浏览器短生命周期状态中使用，不持久化、不计算 hash、不写日志/事件/审计/Webhook，也不在响应回显；响应只给出长度和 `prompt_redacted=true`。`runtime_compatible` 只说明 PRD-045 发布兼容性，`gateway_parity="partial"`、`coverage_complete=false` 明确本期不模拟 Virtual Key/Application 预算、RPM/TPM/并发、KeyPool/credential 或真实 Provider 调用。
- admin-only `policy-simulator` 页面展示兼容性、决策树、规则、路由、DLP 和预算结果。提交 settle、离页、登出、身份/角色变化和 Abort 都清理 Prompt 与旧结果；developer/viewer 即使直达 hash 也回退 overview，后端请求仍返回 403。
- 0035 新增 tenant-bound、不可变的 `policy_explanations`，扩展 Policy event/audit 精确合同与部分唯一索引；down migration 删除 PRD-046 事实后重建 0034 表、索引和触发器，并逐字段保留 0034 历史。

最终已通过 PRD-046 专项、Policy/Webhook/Dry-Run/迁移回归、Python 根级全量 `459 passed`、Node 示例 `33 passed`、Console `200 passed`、Console lint `103 files passed`、中文注释、compile/node-check/目标 Ruff，以及批次 `20260718T161021` 的真实 Chrome 1280x720、900x800、390x844 验收。真实 API 最终批次 `20260718T101126` 使用当前 SHA-256 `171803A28ECD05DCE0189635025E4EAED35EB9724FD6D2374492096C5D7692FE` 脚本通过三服务、主/异常/RBAC、租户、五类零成本结果、Webhook、重复 request_id、隐私和结果未知资源补偿；Provider 回环调用为 0，cleanup `errors=[]`、`active_intent_residue=0`。服务停止后的 0035 证据库为 35 条迁移、完整性 `ok`、外键违规 0，临时资源/Session 活动残留和十份最终日志敏感命中均为 0。

详细设计见 [PRD-046 设计文档](docs/prd/PRD-046-policy-dry-run-single-request-preview.md)，完整分层证据见 [PRD-046 测试记录](docs/testing/PRD-046-tests.md)。

## 成本优先与可用性优先路由（已完成）

PRD-047 已完成两轮设计自审、实现期安全复审、主体代码、专项/全量、0036 迁移、真实三服务/API/Webhook/Usage、停止后数据库与日志复核和真实 Chrome 三档验收。当前实现边界如下：

- 每个正常 ModelAlias 持久化一条版本化 RoutingPolicy；存量和新建 Alias 默认为 `fixed v1`。候选仍只来自 Alias 的 default/fallback，不新增任意候选管理 API，也不新增 Project/User/Department override。
- `fixed` 保留现有主备稳定顺序且不读 KeyPool/价格；`availability_first` 在健康与 KeyPool 过滤后优先新鲜 healthy；`cost_first` 只有两条候选均合格时才读取同一 `as_of` 价格和估算 Token，以 15 位 Decimal 成本排序，正常缺价或混币使用稳定原因码回退 fixed 顺序。
- active CNY Budget 只接受带版本的 CNY 合法价格，缺失、非 CNY、越界或损坏价格统一返回 `gateway.budget_unavailable`。主备准入仍按最坏总额写入不可改写的 `reserved_cost_cny`；`outstanding_cost_cny` 只保留终态后仍未结算的上游分项，即主成功/Provider 前本地失败为 0、主失败备成功为主估算、双上游失败为主备总额。
- 进程级 Provider Key 只能在非生产 active 默认租户、active Provider、code 和数据库/冻结 Route Endpoint 均匹配运维基准时作为兼容回退；生产或其他租户必须使用自己的托管 Key。
- `GET /model-aliases` 为 admin/developer 返回当前 Policy 和配置候选安全摘要；admin-only `PATCH /model-aliases/{id}/routing-mode` 使用认证后 8 KiB 严格 JSON、精确字段/类型、锁内 CAS、同 request_id 持久幂等、同事务 audit/event 和跨租户 404。协议与重试合同见 [ModelAlias RoutingPolicy API](docs/api/routing-policies.md)。
- Gateway 在同一冻结事务读取 mode 所需的最小 Health/KeyPool/价格/Fallback 事实并原子写 `routing_decisions`、`routing_candidates`；动态模式无法形成可追踪事实时在 Provider 前失败关闭。调用日志详情返回 Policy ID/版本/mode、成本比较时点与 Token 估算，以及最多两条强类型候选事实。
- 0036 migration 新增 `model_alias_routing_policies`、`routing_policy_events`、`routing_candidates` 和 `budget_reservations.outstanding_cost_cny`，扩展并重建 `routing_decisions` 和 `audit_logs`，以 tenant-qualified 复合外键、owner/version/identity/immutable 触发器约束 Policy/Alias/Model/Provider/Decision/Candidate 图；PRD-030 遗留 NULL Environment Key 只按同租户 code 安全兼容，Policy-bound Decision 使用 request 部分唯一索引而不删除 0035 重复历史。历史行与链路字段保真的唯一安全例外是不可逆删除旧 Provider Audit 摘要中的 `base_url`；down 不恢复 URL，但保留已安全化的其他历史。
- Console 新增 `routing-policies` 页面：admin 编辑，developer 只读，viewer 不渲染页面且不预取；保存状态绑定 Session、tenant、user、role、Alias、Policy ID/版本、草稿代次和 request ID，拒绝迟到或身份漂移响应。
- 最终批次 `artifacts/prd047/20260719T145100` 通过真实三服务/API/Webhook/Usage、隐私与 owner-safe cleanup，以及 1280x720、900x800、390x844 Chrome。停止后数据库 SHA-256 为 `daf0acfc9d88aa79681cbcbc83d763738ee280f23059abca0d78e1f9e9972039`，36 条迁移/最高 0036、完整性 `ok`、外键违规 0、WAL/SHM 与端口监听均为 0；2 条保留的 enabled RoutingPolicy 只绑定 disabled Alias，可调用计数为 0。

详细设计与自审记录见 [PRD-047 设计文档](docs/prd/PRD-047-cost-first-availability-first-routing.md)，完整自动化、迁移、Runtime、Browser、日志、清理和停止后证据见 [PRD-047 测试记录](docs/testing/PRD-047-tests.md)。

## 效果-成本路由基础（已完成）

PRD-048 已完成两轮设计自审、实现后安全复审、代码实现、0037 正反迁移、专项/全量回归、真实三服务/API/Webhook、幂等清理、停止后数据库与日志复核，以及真实 Chrome 三角色三档验收。当前实现边界如下：

- RoutingPolicy 新增 `quality_cost` 和质量/成本/延迟三权重、质量门槛、最小样本数五字段配置。算法先做通用资格和质量门槛，再处理 cold/ready 证据，只在正权重确实需要时读取延迟、Token 和价格；全部计算使用整数 bps 和 Decimal，原因码、同分与缺失依赖回退稳定。
- `GET/PATCH /model-quality-scores` 以 tenant-qualified Model 为对象，admin/developer 可读、仅 admin 可写；统计版本和人工反馈版本独立，写入使用锁内 CAS、tenant+actor+request_id 幂等恢复、同事务 Audit/Event 和提交后 Webhook。viewer 与跨租户在后端拒绝，API 不接受客户端 tenant_id。
- Gateway 只把真实上游 timeout、429、5xx 和明确 transport 终态计入质量；认证、配额、模型/请求错误、本地协议/处理错误和客户端断开不污染统计。质量资格与预算可能计费判断独立，Attempt/Stream 第一次终态和 Score 增量共用事务，重复或清理异常不能双计或跳过租约释放。
- 动态 Decision/Candidate 冻结 `quality_cost_v1`、Policy/config、Score ID、统计/反馈版本、证据、计数、三分量和总分；Dry-Run 复用同一纯函数且无 Provider/Usage/统计副作用，RequestLog 只读不可变事实，禁止用当前 Score 回算历史。
- 0037 创建 Score/Event 并重建 Policy/Decision/Candidate/Event/Audit 硬合同；历史回填使用精确 Attempt/Stream 分类、规范 UTC/整数微秒延迟和 64-bit preflight。down 只有完整自动统计可重建且没有质量业务事实时才允许，其他情况在结构修改前以固定错误原子阻断。
- Console PAGE-068 支持四种路由模式和五字段配置，PAGE-069 支持 admin 人工评分、developer 只读、viewer 零 DOM/预取；调用日志展示冻结质量解释。最终 21 张真实 Chrome PNG 覆盖 1280x720、900x800、390x844，均非空、无横向溢出、外部请求、未知浏览器错误或敏感命中。
- 最终批次 `artifacts/prd048/20260719T200349` 通过 Python `680/680`、Python 示例 `38/38`、Node 示例 `33/33`、Console `261/261`、Console lint `110 files`、Ruff/compileall/中文注释，以及真实 Runtime/Browser/日志/清理门禁。停止后数据库 SHA-256 为 `39eaf358f537aa4c24fa2b061a821e47fb1238b5a6dd651174e182ee3be66ce3`，37 条迁移/最高 0037、完整性 `ok`、外键违规 0，WAL/SHM 和三端口监听均为 0。

详细设计和三轮复审见 [PRD-048 设计文档](docs/prd/PRD-048-quality-cost-routing-foundation.md)，完整证据见 [PRD-048 测试记录](docs/testing/PRD-048-tests.md)。

## 调用反馈与采纳率记录（已完成）

PRD-049 已完成设计与多轮自审、实现、安全加固、0038/0039 正反迁移、专项/全量回归、真实三服务、Runtime、Browser、日志与停止后数据库复核。当前实现边界如下：

- Control API 与 Gateway 都提供 `POST /feedback`；前者从 Session 恢复当前 tenant 和用户 actor，后者从 Virtual Key 恢复 tenant/key actor。两个入口共用严格 payload、标签规范化、意图指纹、幂等和事务合同，Gateway 不提供 Summary 查询。
- admin/developer 可在 Control 创建反馈和读取汇总，viewer 在读取正文前由后端拒绝；Virtual Key 只能评价同租户且由同一 Key 产生的成功调用。不存在、跨租户和跨 Key 的源 request_id 统一返回防枚举 404，客户端不能提交 tenant、actor、Model 或 Provider 归属。
- 每个 `tenant_id + source_request_id` 只能形成一条不可变 Feedback；评分、采纳、固定白名单标签、最终 Model/Provider 和 actor 快照与持久事件同事务写入。接口不提供 PATCH/DELETE，数据库同时阻断 UPDATE、DELETE 与命中唯一键的 REPLACE 旁路。
- `GET /feedback/summary` 仅位于 Control API，按当前租户、可选模型和 UTC 闭开窗口返回总计、五档评分、标签和模型分页；并发门禁、绝对 deadline 和租户/时间索引避免低频聚合拖住 ASGI 或模型热路径。
- RequestLog 列表/详情增加 `has_feedback`、不可变反馈和可反馈资格；ModelQuality 增加独立逐请求满意度与采纳率字段，不修改 PRD-048 自动质量样本、人工评分或 `quality_cost_v1` 历史解释。
- `feedback.created` 使用首次持久事件 ID 在提交后投递 Webhook；投递失败不能回滚已提交 Feedback，重放不产生第二条业务事实。
- 0038 创建 `request_feedbacks`、`request_feedback_events`、`model_feedback_statistics` 及租户复合外键、不可变/聚合/事件和安全回滚约束；0039 在不修改 0038 checksum 的前提下增加六项 INSERT/UPDATE 冲突守卫，关闭 REPLACE 改写 Feedback/Event/父 RequestLog/最终 Attempt 的路径。
- Console PAGE-070 提供日志详情提交与已记录只读态，PAGE-071 提供租户反馈报表；admin/developer 可见，viewer 无 DOM/预取。筛选响应只有在四项指标、唯一模型行和全部标签 DOM 收敛后才进入截图验收。
- 最终批次 `artifacts/prd049/20260720T181908` 通过 Python `783`、Console `287/287`、Console lint `114 files`、39 条迁移/最高 0039、真实 Runtime 与 15 张三视口 Browser PNG 验收；这只证明 PRD-049 完成，不代表全部 108 条 PRD 已完成。

详细设计与自审见 [PRD-049 设计文档](docs/prd/PRD-049-request-feedback-adoption-records.md)，公开接口见 [Feedback API](docs/api/request-feedback.md)，专项、全量、迁移和真实运行证据见 [PRD-049 测试记录](docs/testing/PRD-049-tests.md)。

## Prompt Cache 感知粘性路由（已完成）

PRD-050 已完成两轮编码前自审、实现、0040、当前源码相关测试、真实 Runtime/Browser、日志隐私、清理和停止后数据库终审。当前实现边界如下：

- CacheAffinity 配置以 ModelAlias 为对象，enabled 时要求服务端可信 data_class；Control API 从 Session 恢复 tenant/actor，admin 可写、admin/developer 可读、viewer 后端拒绝，跨租户与不存在 Alias 同形。
- Gateway 只对有界长 system/developer 前缀计算独立 HMAC 指纹；tenant、Virtual Key、data_class、endpoint revision 和 Prompt 任一变化都不会复用。它不缓存或返回 Prompt/Response，也不实现语义缓存。
- 动态 RoutingPolicy 只在同一健康/质量资格层级内应用 pending/warm 端点偏好；fixed 保持主备硬顺序。KeyPool 在已选 Endpoint 内复用或重绑 Credential，target/lease generation 防止迟到终态污染新绑定。
- Selection 在 Provider 前提交，非流式 succeeded attempt 或流式 completed Session 才确认 warm；失败、timeout、watchdog、disconnect、DLP/限流/预算/权限阻断不会伪造预计命中。
- PAGE-072 为 admin 编辑、developer 只读、viewer 无受限 DOM/预取；PAGE-073 分开展示 estimated hit 和 Provider 实报缓存相关 Token。前端权限表现不替代后端 RBAC。
- 0040 新增五张业务表并扩展 Candidate reason/Audit 约束；当前 loader checksum 为 `dc4289d44091e231f2c288398c58ab8eccc075fd7bcbbc869f2b7b7c8257f090`。
- 权威批次 `artifacts/prd050/20260725T005725` 的 Runtime 与 Browser 均为 `passed`；Browser 形成 45 张真实 Chrome PNG（admin/developer/viewer = 30/12/3），三档视口各 15 张，SHA 全唯一、非空/隐私遮罩与 45 条布局合同全部通过。
- 当前源码相关验证为 Browser 合同 4/4、CacheAffinity + Dashboard 15/15、静态 verifier 旧 SHA 修复后的组合 21/21、脚本语法和中文注释通过。2026-07-24 的 Python 921/921、Console 354/354、Browser 单测 71/71、Node 示例 33/33、lint 118 files 明确只作为变更前基线，本轮未重跑无关全量。
- 六份服务日志 5xx/ERROR/CRITICAL 和敏感命中均为 0；停止后数据库 40/0040、integrity ok、FK 0、运行态残留 0、WAL/SHM/journal 0，最终 SHA-256 为 `5b895e79fc11dda8c77d1a9131bebc407ae9e68acc6102bf2883d742a25238c7`。
- 本次收口全部位于 `D:/MetaHub`，没有使用 `C:/tmp`、提权、CIM 或端口 owner 扫描。图片查看器 setup refresh 失败已如实记录，真实 Chrome/PNG 像素/布局自动化证据作为视觉通过依据。

详细设计与两轮自审见 [PRD-050 设计文档](docs/prd/PRD-050-prompt-cache-aware-sticky-routing.md)，公开合同见 [CacheAffinity API](docs/api/cache-affinity.md)，测试范围和最终证据见 [PRD-050 测试记录](docs/testing/PRD-050-tests.md)。

## 控制台骨架与导航

PRD-018 后，控制台登录成功后进入统一主框架。后端生成 `NavigationItem` 列表，前端只消费可见入口并渲染顶部租户/用户信息、分组侧边栏、当前内容区和空状态下一步引导。

控制台骨架接口：

- `GET /console/navigation`：返回当前角色可见导航项和 active item，不接受前端传入 `tenant_id`。
- `GET /console/preferences`：读取当前 `tenant_id + user_id` 的控制台偏好。
- `PATCH /console/preferences`：保存 active navigation 和侧边栏折叠状态，目标入口必须对当前角色可见。
- `POST /console/events`：写入 `console.page_viewed`，事件只允许低风险白名单 payload。

安全约束：

- Admin 可见配置入口；Developer 可见 Key、日志、用量、API 文档和调试台；Viewer 不展示受限管理入口。
- 前端隐藏导航只是 UX，Provider、用户、模型、Key、日志和用量 API 仍由各自后端依赖做 RBAC。
- `user_preferences` 和 `console_page_events` 默认带 `tenant_id`，写入租户、用户和 session 信息都来自认证上下文。
- 控制台事件不记录 Token、Authorization、Provider Key、Virtual Key 明文、Prompt、Response、messages 或 input。

## 开发者快速开始

PRD-019 后，控制台提供“快速开始”入口，面向 admin/developer 返回当前租户可复制执行的 Python、JavaScript 和 curl 非流式 chat 调用示例。

快速开始接口：

- `GET /developer/quickstart-data`：返回 Gateway `/v1` base_url、推荐模型别名、Virtual Key 环境变量占位、步骤、代码示例和常见错误说明。

安全约束：

- 接口只允许 admin/developer 访问，viewer 后端返回 `auth.permission_denied`。
- 响应不返回 Virtual Key 明文、`key_hash`、Provider Key 或 Authorization，只返回 `METAHUB_VIRTUAL_KEY` 占位和可安全展示的 Key 前缀。
- `developer_portal_events` 默认带 `tenant_id`，`developer.quickstart_viewed` 只记录模型别名、是否已有 active Key 和示例 ID 等安全元数据。
- 示例代码统一使用 `METAHUB_VIRTUAL_KEY` 环境变量，不把 Key 写进源码或页面事件。

## 内置 API 调试台

PRD-020 后，控制台提供内置 API 调试台。开发者或管理员可以选择当前租户 active Virtual Key 和 active 模型别名，编辑 OpenAI-compatible `messages`，发起一次非流式 Chat Completions 调试请求，并查看响应、request_id、trace_id、延迟、usage、成本和错误建议。

调试台控制面接口：

- `POST /debug/chat-completions`：admin/developer 发起低频调试请求；viewer 后端返回 `auth.permission_denied`。

安全与追踪约束：

- 调试接口不接收 `tenant_id`、Virtual Key 明文、Provider Key、Authorization 或其他密钥字段。
- 后端只按登录态和当前租户 active `virtual_key_id` 恢复 `environment/key_prefix` 等元数据，不读取或返回 `key_hash`。
- 成功请求写入 `request_logs`、`usage_ledger` 和 `debug_console_events`，失败请求在具备有效 Virtual Key 上下文时写入失败日志和调试事件。
- `debug_console_events` 只保存安全元数据，不保存 Prompt、messages content、完整 response、Virtual Key 明文、Provider Key 或 Authorization。
- 成功响应返回 `log_detail_url`，控制台可跳转到调用日志详情继续排障。

## 智能错误提示基础版

PRD-021 后，统一错误响应、错误码目录、调用日志详情和调试台错误卡片都会带安全的 `diagnosis` 字段。诊断规则用于把 401/429/5xx 等失败归因为可操作原因，例如上游 Key 失效、Provider 限流、模型不存在、余额不足、网关超时和参数错误。

核心能力：

- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py` 保存静态 `ErrorDiagnosisRule`，M0 不落库、不做租户自定义规则。
- `GET /error-codes` 返回错误码元数据和诊断信息，供文档、调试台和运维排障使用。
- Provider Adapter 将上游 401/403、429、402/quota、404/model、timeout、400/422 映射到细分 `gateway.upstream_*` 错误码。
- `GET /request-logs/{request_id}` 在详情中返回 `error_diagnosis`，控制台日志详情展示原因、建议、动作、级别和可重试标记。
- 调试台错误卡片展示同一套诊断 DTO，并对所有字段做 HTML 转义。

安全边界：

- 诊断只使用错误码、HTTP 状态、Provider error type 和受控关键词，不返回上游原始 body、headers、Authorization、Provider Key、Virtual Key 明文、Prompt、messages 或 Response。
- `error.diagnosed` 是结构化日志事件，只包含 `request_id`、`trace_id`、`error_code`、`diagnosis_rule_id`、分类和 retryable 等安全元数据。
- M0 不做自动修复、不触发 Key 自动轮换、不做 LLM RCA。

## OpenAI-Compatible 网关接口

M0 数据面提供基础 OpenAI-compatible 入口。开发者可以把 OpenAI SDK 的 `base_url` 指向 Gateway `/v1`，并用 Virtual Key 作为 Bearer Token。

本地 seed Virtual Key 默认来自 `METAHUB_SEED_VIRTUAL_KEY`，未设置时为：

- `mhv_dev_seed_virtual_key`

本地运行示例前，把一次性明文 Key 放入环境变量：

```powershell
$env:METAHUB_VIRTUAL_KEY="mhv_dev_seed_virtual_key"
```

Provider Endpoint 来自 seed provider 或环境变量：

```powershell
$env:METAHUB_PROVIDER_LOCAL_OPENAI_BASE_URL="http://localhost:11434/v1"
$env:METAHUB_PROVIDER_LOCAL_OPENAI_API_KEY="replace-with-provider-key"
```

Python SDK 示例：

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key=os.environ["METAHUB_VIRTUAL_KEY"],
)

response = client.chat.completions.create(
    model="default-chat",
    messages=[{"role": "user", "content": "你好"}],
)
```

API：

- `POST /v1/chat/completions`：支持基础 JSON 请求和 `stream=true` SSE 透传。
- `POST /v1/embeddings`：支持基础 `input` 请求。

安全与追踪约束：

- 数据面只用 Virtual Key 恢复 `tenant_id`，不信任请求体中的租户字段。
- Provider Key 只进入上游请求头，不写入日志、响应、request_logs 或 usage_ledger。
- `request.received` 不记录 prompt/input/message 明文。
- 成功请求写 `request_logs` 和 `usage_ledger`；鉴权成功后的失败请求尽量写 `request_logs`。

## Provider Adapter

PRD-013 后，数据面通过内部 `ProviderAdapter` 合同调用上游，主流程不直接硬编码某个厂商的 HTTP 细节。当前实现：

- `OpenAICompatibleAdapter`：默认 OpenAI-compatible JSON/SSE Adapter。
- `GenericHttpConfigAdapter`：通用 HTTP 配置 Adapter，作为后续自定义 Endpoint 的扩展点。
- `adapter_for_route(route)`：按 `providers.provider_type` 选择 Adapter。
- usage 统一归一为 `input_tokens`、`output_tokens`、`total_tokens`、`cache_tokens`；上游缺 usage 时会产生带 `estimated=true` 的本地估算用量。
- 上游错误统一归一为可操作错误码：`gateway.upstream_auth_failed`、`gateway.upstream_rate_limited`、`gateway.upstream_quota_exceeded`、`gateway.upstream_model_not_found`、`gateway.upstream_timeout`、`gateway.upstream_request_invalid`、`gateway.upstream_error` 或 `gateway.upstream_unavailable`，并写结构化事件 `provider_adapter.error`。

Provider Adapter 不暴露新的公开 API，不记录 Authorization、Provider Key、prompt、input 或 message 明文。后续新增厂商优先新增 Adapter，不改 `/v1` 主流程。

## API 契约

### GET /error-codes

共享只读元数据接口，Gateway 与 Control Plane 都由 `create_service_app` 暴露。响应包含统一错误码、HTTP 状态、retryable、owner、suggestion、`diagnosis` 和展示层 `label`，用于 API 文档、调试台和运维排障。可选 `locale=zh-CN|en-US` 只影响 `label`，不改变稳定英文 `code/message/suggestion`。

该接口不读取租户业务数据，不接收 `tenant_id`，也不返回 Provider Key、Virtual Key、Authorization、Prompt 或 Response。

### POST /v1/chat/completions

数据面 OpenAI-compatible 接口，必须传入 `Authorization: Bearer <virtual_key>`。

请求示例：

```json
{
  "model": "default-chat",
  "messages": [
    {
      "role": "user",
      "content": "你好"
    }
  ],
  "stream": false
}
```

响应为上游 OpenAI-compatible JSON；`stream=true` 时返回 `text/event-stream`。参数校验失败返回 `gateway.model_missing`、`gateway.messages_missing` 等统一错误码。

### POST /v1/embeddings

数据面 OpenAI-compatible 接口，必须传入 `Authorization: Bearer <virtual_key>`。

请求示例：

```json
{
  "model": "default-embedding",
  "input": "hello"
}
```

响应为上游 OpenAI-compatible JSON。Embeddings M0 不支持 `stream=true`，出现时返回 `gateway.stream_not_supported`。

### POST /debug/chat-completions

控制面内置 API 调试接口，必须传入登录态 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。该接口用于低频控制台调试，不要求用户粘贴 Virtual Key 明文。

请求示例：

```json
{
  "virtual_key_id": "vk_...",
  "model_alias": "default-chat",
  "messages": [
    {
      "role": "user",
      "content": "你好，MetaHub"
    }
  ],
  "temperature": 0.7,
  "max_tokens": 512
}
```

成功响应包含 `debug_request_id`、`request_id`、`trace_id`、`latency_ms`、`response_json`、`usage`、`cost` 和 `log_detail_url`。失败响应使用统一错误结构，包含 `code`、`message`、`request_id`、`trace_id` 和 `suggestion`。请求体如果包含 `tenant_id`、`authorization`、`provider_key`、`virtual_key`、`token`、`secret`、`password` 或 `key_hash` 等字段会被拒绝。

### GET /virtual-keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

返回示例：

```json
{
  "virtual_keys": [
    {
      "virtual_key_id": "vk_...",
      "tenant_id": "944f...",
      "name": "local dev key",
      "key_prefix": "mhv_dev_xxx",
      "environment": "dev",
      "description": "本地开发调试",
      "status": "active",
      "last_used_at": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /virtual-keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

请求示例：

```json
{
  "name": "local dev key",
  "environment": "dev",
  "description": "本地开发调试"
}
```

响应只在创建时包含一次性明文：

```json
{
  "virtual_key": {
    "virtual_key_id": "vk_...",
    "key_prefix": "mhv_dev_xxx",
    "environment": "dev",
    "status": "active"
  },
  "plain_value": "mhv_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### PATCH /virtual-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。

请求示例：

```json
{
  "name": "prod app key",
  "environment": "prod",
  "description": "生产业务系统",
  "status": "disabled"
}
```

### DELETE /virtual-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin` 或 `developer`。该接口执行软禁用，返回禁用后的 Key 元数据，不返回明文。

### GET /providers

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

返回示例：

```json
{
  "providers": [
    {
      "provider_id": "provider_...",
      "tenant_id": "944f...",
      "provider_code": "deepseek_official",
      "name": "DeepSeek 官方 Endpoint",
      "provider_type": "deepseek",
      "base_url": "https://api.deepseek.com/v1",
      "auth_type": "bearer",
      "status": "active",
      "description": "生产候选 Provider",
      "health_status": "unknown",
      "last_tested_at": null,
      "last_test_error": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /providers

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 Provider Key 明文字段。

请求示例：

```json
{
  "provider_code": "deepseek_official",
  "name": "DeepSeek 官方 Endpoint",
  "provider_type": "deepseek",
  "base_url": "https://api.deepseek.com/v1",
  "auth_type": "bearer",
  "description": "生产候选 Provider"
}
```

### PATCH /providers/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。目标 Provider 必须属于当前租户。

请求示例：

```json
{
  "status": "disabled"
}
```

### POST /providers/{id}/test

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。接口执行最小连接测试，并返回成功或失败原因。

请求示例：

```json
{
  "timeout_seconds": 5
}
```

响应示例：

```json
{
  "provider": {
    "provider_id": "provider_...",
    "provider_code": "deepseek_official",
    "health_status": "healthy"
  },
  "test_result": {
    "status": "success",
    "http_status": 200,
    "latency_ms": 42,
    "message": "Endpoint is reachable.",
    "checked_at": "2026-07-08T10:05:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /providers/{id}/keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
返回示例：
```json
{
  "provider_keys": [
    {
      "provider_key_id": "provider_key_...",
      "tenant_id": "944f...",
      "provider_id": "provider_...",
      "name": "prod key 1",
      "key_prefix": "sk-prod-abc",
      "key_fingerprint": "6ac1...",
      "weight": 100,
      "status": "active",
      "last_used_at": null,
      "last_error_at": null,
      "last_error_code": null,
      "last_error_message": null,
      "rate_limited_until": null,
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /providers/{id}/keys

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
请求示例：
```json
{
  "name": "prod key 1",
  "plain_value": "sk-...",
  "weight": 100
}
```

响应只返回 Key 元数据，不返回 `plain_value` 或 `encrypted_key`。

### PATCH /provider-keys/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。
请求示例：
```json
{
  "status": "disabled"
}
```

### GET /models

认证接口，必须传入 `Authorization: Bearer <token>`，当前用户角色必须为 `admin` 或 `developer`。支持 `status`、`provider_id`、`capability` 查询参数。

返回示例：

```json
{
  "models": [
    {
      "model_id": "model_...",
      "tenant_id": "944f...",
      "internal_model_id": "deepseek-chat",
      "provider_id": "provider_...",
      "provider_code": "deepseek",
      "provider_name": "DeepSeek 官方 Endpoint",
      "provider_model_id": "deepseek-chat",
      "display_name": "DeepSeek Chat",
      "capabilities": {"chat": true, "stream": true},
      "context_window": 64000,
      "input_price_per_1k": 0.001,
      "output_price_per_1k": 0.002,
      "currency": "CNY",
      "price_schema_version": "manual-202607",
      "status": "active",
      "description": "生产候选模型",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /models

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 `tenant_id`。

请求示例：

```json
{
  "internal_model_id": "deepseek-chat",
  "provider_id": "provider_...",
  "provider_model_id": "deepseek-chat",
  "display_name": "DeepSeek Chat",
  "capabilities": {"chat": true, "stream": true, "embeddings": false},
  "context_window": 64000,
  "input_price_per_1k": 0.001,
  "output_price_per_1k": 0.002,
  "currency": "CNY",
  "price_schema_version": "manual-202607",
  "description": "生产候选模型"
}
```

### PATCH /models/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。路径 `{id}` 是技术主键 `models.model_id`，M0 不支持直接重命名 `internal_model_id`。

请求示例：

```json
{
  "provider_model_id": "deepseek-chat-v2",
  "context_window": 128000,
  "output_price_per_1k": 0.003,
  "status": "disabled"
}
```

### GET /model-aliases

认证接口，必须传入 `Authorization: Bearer <token>`，当前用户角色必须为 `admin` 或 `developer`。支持 `status`、`environment` 查询参数。

返回示例：

```json
{
  "model_aliases": [
    {
      "model_alias_id": "alias_...",
      "tenant_id": "944f...",
      "alias": "company-default",
      "environment": "dev",
      "default_model_id": "model_...",
      "default_internal_model_id": "deepseek-chat",
      "default_display_name": "DeepSeek Chat",
      "default_provider_code": "deepseek",
      "default_provider_name": "DeepSeek 官方 Endpoint",
      "fallback_model_id": "model_...",
      "fallback_internal_model_id": "qwen-plus",
      "fallback_display_name": "Qwen Plus",
      "fallback_provider_code": "qwen",
      "fallback_provider_name": "通义千问 Endpoint",
      "status": "active",
      "description": "默认聊天别名",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /model-aliases

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。请求体不接收 `tenant_id`。

请求示例：

```json
{
  "alias": "company-default",
  "environment": "dev",
  "default_model_id": "model_default",
  "fallback_model_id": "model_fallback",
  "description": "默认聊天别名"
}
```

### PATCH /model-aliases/{id}

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "status": "disabled"
}
```

### GET /health

返回示例：

```json
{
  "status": "ok",
  "service_name": "gateway-data-plane",
  "environment": "dev",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /internal/health

内部轻量健康检查，M0 不依赖数据库、Redis 或 Provider：

```json
{
  "status": "ok",
  "service_name": "control-plane-api",
  "environment": "dev",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /health/live

Liveness probe，只证明进程可响应，不访问数据库、Redis 或 Provider。

```json
{
  "service_name": "gateway-data-plane",
  "status": "ok",
  "live": true,
  "ready": false,
  "checks": [],
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /health/ready

Readiness probe。数据库配置存在时执行 ping，失败时 `ready=false` 并返回 HTTP 503；Redis 和 Provider 在 M0 为可选检查。

```json
{
  "service_name": "control-plane-api",
  "status": "fail",
  "live": true,
  "ready": false,
  "checks": [
    {
      "name": "database",
      "status": "fail",
      "required": true,
      "latency_ms": 1,
      "message": "database ping failed: DbError"
    }
  ],
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /version

返回示例：

```json
{
  "service_name": "gateway-data-plane",
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T09:00:00Z",
  "uptime_seconds": 10,
  "request_id": "req_..."
}
```

### GET /internal/config/summary

控制面内部接口，用于“系统设置-环境信息”只读页。`dev/test` 可直接访问，`prod` 必须传入 `X-Internal-Token`。

返回示例：

```json
{
  "service_name": "control-plane-api",
  "environment": "dev",
  "loaded_at": "2026-07-07T09:00:00Z",
  "items": [
    {
      "key": "METAHUB_PROVIDER_DEEPSEEK_API_KEY",
      "source": "env",
      "status": "masked",
      "value_preview": "***MASKED***"
    }
  ],
  "warnings": [],
  "request_id": "req_..."
}
```

### GET /tenant/current

认证接口，必须传入 `Authorization: Bearer <token>`。

返回示例：

```json
{
  "tenant": {
    "tenant_id": "944f...",
    "slug": "default",
    "name": "MetaHub Default Tenant",
    "service_mode": "internal_only",
    "status": "active",
    "created_at": "2026-07-07T10:00:00Z",
    "updated_at": "2026-07-07T10:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### PATCH /tenant/current

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "name": "MetaHub Demo Tenant",
  "service_mode": "internal_only"
}
```

响应同 `GET /tenant/current`。

### GET /users

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

返回示例：

```json
{
  "users": [
    {
      "user_id": "usr_...",
      "tenant_id": "944f...",
      "email": "dev@example.com",
      "display_name": "Dev User",
      "role": "developer",
      "status": "active",
      "created_at": "2026-07-07T10:00:00Z",
      "updated_at": "2026-07-07T10:00:00Z",
      "last_login_at": null
    }
  ],
  "roles": [
    {
      "code": "admin",
      "name": "管理员",
      "description": "管理当前租户配置、用户、Provider 和 Key。"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /users

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "email": "dev@example.com",
  "display_name": "Dev User",
  "password": "LocalDev!2026",
  "role": "developer"
}
```

响应返回创建后的用户对象，不返回密码或密码哈希。

### PATCH /users/{id}/role

认证接口，必须传入 `Authorization: Bearer <token>`，且当前用户角色必须为 `admin`。

请求示例：

```json
{
  "role": "viewer"
}
```

目标用户必须属于当前租户；跨租户 user_id 统一返回 `user.not_found`。

所有响应都会返回 `X-Request-ID` 和 `X-Trace-ID`。错误响应统一为：

```json
{
  "code": "infra.not_found",
  "message": "Requested resource was not found.",
  "request_id": "req_...",
  "trace_id": "trc_...",
  "suggestion": "Check the API path and method."
}
```

## PRD 开发流程

后续必须按 Excel 中的 `PRD_ID` 逐条推进，优先顺序为 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3`。

每条 PRD 必须：

1. 阅读 Excel 中对应 PRD 行以及相关页面/API/数据对象/验收用例。
2. 先输出设计文档到 `docs/prd/`，包含需求理解、模块、表设计、API、前端、错误码、安全、测试、代码计划。
3. 编码时保留详细中文注释：每个定义/函数有中文说明，关键权限、租户、脱敏、路由、成本和失败降级分支有注释。
4. 补主流程测试和异常/权限/安全测试。
5. 完成后更新 README 或 `docs/development/` 文档。

## 安全红线

- Provider Key、Virtual Key、Authorization、Token、Secret 不允许明文落日志。
- 管理员密码和 Bearer Token 不允许明文落库或落日志。
- 后续所有业务数据默认带 `tenant_id`。
- 控制台隐藏按钮不等于权限，所有管理 API 必须在后端做权限校验。
- 用量与成本必须可追踪，失败请求必须进入调用日志或错误日志。

## Environment 隔离边界

PRD-030 将 `dev/test/prod` 建模为租户级 Environment：

- `GET /environments` 对 admin/developer 开放；创建和启停仅 admin 可用，越权请求由后端拒绝。
- Virtual Key、ModelAlias 和 BudgetRule 新写路径必须绑定同租户 `environment_id`；环境代码只作为兼容快照。
- 同一别名可在不同 Environment 指向不同模型，同一环境内仍保持唯一；预算规则按环境和 scope 分别配置。
- Gateway 环境只来自 Virtual Key 鉴权上下文，不接受请求体或 Header 覆盖；禁用环境在 Provider 调用前阻断，失败仍写入带环境的调用日志。
- 调试台后端比较所选 Environment 与 Key 绑定，生产 Key 在测试环境中使用会返回 `debug.environment_mismatch`。
- 路由决策、调用日志、用量账本和预算评估保存环境归因；`environment.created` 只记录安全元数据。

M0.5 不实现多地域环境、部署集群映射、环境审批流或跨环境自动发布。

## 模型价格与生效时间边界

PRD-031 建立有效期价格计划与不可变 Usage 快照：

- `POST /model-prices` 仅 admin 可用，登记模型输入/输出 Token 每千单价、CNY/USD 币种、版本和带时区生效时间。
- `GET /model-prices` 对 admin/developer 开放，按服务器时刻动态展示历史、当前和待生效计划；viewer 由后端拒绝。
- Gateway 只按可信 `tenant_id + model_id + 服务器 UTC 时间` 选择最近已生效计划，客户端不能覆盖价格计划或生效时刻。
- 每条 Usage 创建独立 `price_snapshots`，复制实际单价、版本、`price_plan_id` 和 `effective_from`；新价格不会修改旧成本或旧快照。
- 没有价格计划的存量模型继续使用模型目录兼容价格，避免破坏 PRD-017 已有调用链路。
- `price.updated` 事件和 Webhook 只包含模型、版本、币种和生效时间，不包含任何 Key、Authorization、Prompt 或 Response。

M0.5 不自动抓取厂商价格、不做汇率换算、历史账单重算、价格审批或批量导入。

## Provider 计费归一化边界

PRD-032 为 Provider Adapter 建立统一文本用量契约：

- Adapter 将 OpenAI、通用/Anthropic 与 Gemini usage 字段归一为输入、输出、总量和缓存 Token，并保留估算来源与稳定警告代码。
- Provider 自报成本只用于厂商对账，PricePlan 计算的内部成本仍是预算与 Usage Ledger 的权威口径；USD 或未知币种不做隐式人民币换算。
- raw usage 只提取 usage 子对象，并在 Adapter 写入前、账本入口和控制面返回前执行脱敏与体积限制；Provider Key、Authorization、Prompt 和 Response 不得进入数据库、日志或 API。
- `GET /usage/records` 继续由后端限制 admin/developer；控制台用量详情展示安全 normalized/raw 双口径，viewer 不能通过手写请求绕过权限。
- 非流式成功调用记录 `usage.normalized` 安全结构化事件，保留 request_id、trace_id 和 tenant_id 追踪，不在数据面热路径同步派发 Webhook。

M0.5 只处理文本 Token，不处理图片、语音、视频、复杂阶梯计费、自动抓价或无快照汇率换算。

## 预算耗尽阻断与恢复边界

PRD-033 在 PRD-023 日预算 Guard 基础上补齐可审计的阻断生命周期：

- 当租户、Project、Application 或 Virtual Key 的 block 规则预计达到 100% 时，Gateway 在 Provider 调用前返回 `gateway.budget_exhausted`，首次命中创建唯一 active BudgetBlock。
- 当日 active block 是硬阻断事实，后续较小请求不能绕过；每次失败仍保留 request_id、trace_id、规则、金额和原因摘要。
- Gateway 在 Provider 前以原子事务为每条预算规则创建成本 reservation，并把未结算预留计入余额，避免并发请求集体突破额度。无 Fallback 的历史记录继续按 NULL `outstanding_cost_cny` 兼容旧语义；新运行状态明确后，Usage 只替代已结算分项，还可能被上游计费的主/备失败分项继续由 `outstanding_cost_cny` 保守计入 spent。
- `PATCH /budgets/{id}/mode` 仅 admin 可切换 `block/alert_only`；提额继续使用 `/budgets`，两条路径都按登录态 tenant_id 校验并事务化释放 block。
- 提额、切换仅告警、停用规则或进入新业务日会写 `budget.unblocked` 事件；下一请求直接读取权威数据库并立即恢复，无需重启。
- 控制台显示阻断时间和生命周期 ID，developer 只读、viewer 隐藏；前端权限呈现不能替代后端 RBAC。
- BudgetBlock 与事件不保存 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key；PricePlan/PriceSnapshot 负责形成 Usage 成本证据，预算权威消费只取正式 `finops_ledger_entries` 的 `posted usage_charge/debit` 并叠加 outstanding reservation。
- 流式调用会解析 Provider usage chunk；缺失或中断时仅按字符计数写估算 Usage。账本暂时失败时 reservation 继续保守占用，避免成本从预算口径消失。

M0.5 不实现提额审批、自动低价模型降级、复杂预算快照分发或同步阻塞数据面热路径的 Webhook 投递。

## 非流式备用模型 Fallback 边界

PRD-036 在模型别名最小路由和 Provider 健康预检之后增加请求期一次性容灾：

- `PATCH /model-aliases/{id}/fallback` 仅 admin 可写；developer 可从别名列表读取策略，viewer 由后端拒绝。目标模型必须属于当前租户、处于 active 且不能与主模型相同。
- 只有非流式请求的受控 `timeout`、HTTP 429 和 HTTP 5xx 可触发备用模型；DNS、TLS、本地参数、鉴权、预算、DLP 等错误不会重试，流式请求绝不在输出中途切换模型。
- 每个非流式请求最多两次上游调用。每次网络调用前必须先创建 `routing_attempts.started` 审计记录；第二次闸门失败时禁止调用 Provider，避免产生无法追踪的流量和重试风暴。
- `request_logs` 保存是否触发、attempt 数量和最终 attempt；成功 Usage 关联最终 attempt 和最终模型。上游失败且没有 usage 时记录 `unavailable`，不伪造 0 Token 或 0 成本。
- 预算准入按主模型与备用模型最坏成本之和预留；流式请求仅预留初始路由。主请求可能已被 Provider 计费，因此不能只按最终成功模型预留；主失败备成功时保留主估算为 outstanding，双上游失败保留主备总额，Provider 前本地失败则为 0。
- `route.fallback_triggered` 使用稳定事件 ID 投递到有界后台队列，Webhook 最多重试三次；模型请求热路径不等待接收端 HTTP，事件和日志不包含 Provider Key、Authorization、Prompt 或 Response。
- 控制台模型别名页为 admin 提供策略编辑器，developer 只读；调用日志详情按顺序展示主/备 attempt、触发原因、最终模型和用量可用状态。移动端展开表单进入文档流，保存按钮可完整滚动访问。

M0.5 不实现三层以上候选链、流式中途切换、跨地域重放、任意表达式策略或自动选择最低价模型。

## Python SDK 示例边界

PRD-038 在现有 OpenAI-compatible API 和 5 分钟快速开始页之上提供可安装、可下载的 Python 示例项目：

- 示例源码位于 `examples/python`，使用官方 `openai` Python 客户端，不创建 MetaHub 私有协议或完整大 SDK。
- `METAHUB_BASE_URL`、`METAHUB_VIRTUAL_KEY`、Chat/Embeddings 模型、超时和有限重试全部通过环境变量配置；Virtual Key 没有默认值且不会被打印。
- `python -m metahub_gateway_examples.chat` 和 `python -m metahub_gateway_examples.embeddings` 分别验证 Chat 与 Embeddings；成功和失败都展示可用于调用日志排查的 `request_id`。
- `GET /developer/sdk-examples/python` 返回当前租户的安全元数据；`GET /developer/sdk-examples/python/download` 只打包固定白名单文件，并记录带 `tenant_id` 的 `sdk.example_downloaded` 事件。
- 控制台“SDK 示例”页仅对 admin/developer 展示，后端对元数据和下载接口再次执行 RBAC；viewer 手写请求也会被拒绝。
- ZIP、事件、日志和页面不包含 Provider Key、Virtual Key 明文、Authorization、Prompt、Response、数据库路径或任意服务端文件。
- 示例调用仍经过 Gateway，因此成功用量与成本进入 `usage_ledger`，失败请求进入 `request_logs` 或结构化错误日志。

本 PRD 不做流式 Python 示例、预算专用客户端、自动配置生成器、PyPI 正式发布或自研 SDK 生命周期；这些能力按后续 PRD 独立演进。

## JavaScript/Node 示例边界

PRD-039 在 Python 制品基础上补充 Node.js 服务端示例，不把长期 Virtual Key 带入浏览器：

- 示例源码位于 `examples/js`，运行基线为 Node.js 22+，使用锁定的官方 `openai` 客户端和 OpenAI-compatible Chat Completions。
- `.env.example` 只提供公开占位；开发者在本机创建 `.env` 并填写 Virtual Key，真实 `.env`、Key、Prompt 和模型输出不会进入 Git、ZIP、事件或日志。
- `src/chat.js` 验证非流式调用；`src/stream.js` 使用异步迭代逐段输出 SSE，并通过额外总时限避免响应头后永久停滞。
- CLI 安全处理 `requestID`、`request_id`、响应头和流握手 request_id；未知异常只输出固定错误码，不打印 message、stack、Authorization 或请求正文。
- `GET /developer/sdk-examples/javascript` 返回当前租户元数据；下载接口只打包固定白名单，并复用带 `tenant_id` 的 `sdk.example_downloaded` 事件。
- 控制台 SDK 页面使用 Python/JavaScript 分段视图；后端对两种语言的元数据和下载接口都执行 admin/developer RBAC。
- 示例明确拒绝浏览器运行，不设置 `dangerouslyAllowBrowser`，页面不提供浏览器直连代码或 Virtual Key 输入框。
- Node 调用仍经过 Gateway 的路由、DLP、调用日志、Usage Ledger 和成本追踪，不旁路企业治理链路。

本 PRD 不发布完整 npm SDK、不支持浏览器长期凭据、不增加私有协议，也不实现预算/Trace 专用客户端或 Debug Center 自动跳转。

## 从直连模型厂商迁移指南边界

PRD-040 在开发者控制台提供从模型厂商直连迁移到 MetaHub 的可执行指南：

- 首版只保证服务端 Python 和 Node.js 使用 OpenAI Chat Completions 的非流式、流式示例，不承诺厂商专有 SDK、Responses API、Assistants API 或浏览器直连。
- 迁移只替换完整的 `base_url/api_key/model` 配置三元组；`model` 必须使用当前租户、当前 Environment 下可用的 MetaHub 模型别名。
- Gateway `/v1` 地址来自后端运行配置并经过 URL 规范化；页面不使用硬编码生产地址，不允许复制带凭据、query、fragment 或非法路径的 URL。
- `migration.guide_viewed` 复用 `POST /console/events`，租户、用户和会话身份来自后端认证上下文；事件不保存 Provider Key、Virtual Key、Prompt、源码或响应正文。
- 回滚只适用于配置、兼容性或 MetaHub 技术故障，并要求审批、等价治理、凭据隔离和完整配置原子切换；预算、DLP、限流或安全策略阻断不得自动回直连。
- 页面不读取客户仓库、不扫描代码、不自动修改项目，也不进入 Gateway 数据面热路径。

详细设计见 [PRD-040 设计文档](docs/prd/PRD-040-direct-provider-migration-guide.md)，测试记录见 [PRD-040 测试记录](docs/testing/PRD-040-tests.md)。

## 竞品定位与能力映射边界

PRD-041 在帮助中心提供可追溯的中立定位和当前实现映射：

- OpenRouter 和硅基流动是对标、竞品和迁移兼容对象，不是模型厂商，也不是 MetaHub 默认上游。
- 页面按 2026-07-15 的官方资料说明公开关注点，不声称竞品缺少治理、审计、私有部署或成本能力。
- MetaHub 映射入口只来自后端按当前角色下发的可见导航，前端静态页面不能恢复被 RBAC 隐藏的入口。
- `help.competitor_viewed` 复用控制台事件 API，tenant/user/session 来自认证上下文；失败不阻断阅读且同一次访问不重试。
- Provider 类型继续使用模型厂商、云 Endpoint 或通用 `migration_compatible` 白名单，不增加竞品品牌枚举或默认路由。

## FinOps Ledger 正式账本边界

PRD-051 已完成在既有 Usage Ledger 之上增加正式、不可变的财务分录层：

- 每条 Usage 只生成一条 `usage_charge` 正式分录，并绑定当前租户、来源 `request_id/trace_id`、价格快照、价格版本和证据状态；历史仅有金额的记录明确标为 `legacy_amount_only`，不得用当前价格回算。
- Gateway 主库写入失败时把冻结的安全 Usage 与价格证据同步写入本机 HMAC/fsync spool；恢复按 tenant + Usage ID 验真和幂等提交，只为本次新建分录投递一次 `ledger.entry_created`。
- `GET /finops/ledger` 和 `POST /finops/adjustments` 只允许 admin，tenant 和 actor 仅从 Session 恢复；developer/viewer 手写请求也由后端拒绝，跨租户目标与不存在目标保持同形 404。
- 人工调整支持赠送、试用、手工借记、手工贷记和冲正；金额使用六位小数字符串，冲正累计不得超过原分录，调整、分录和管理审计在同一事务提交。
- PAGE-074/075 提供筛选、签名游标分页、汇总、详情、调整和冲正；浏览器不保存幂等键、operation ID、Provider Key、Token、Prompt、Response 或自由文本原因到 URL/Storage/验收制品。
- 0041 负责历史回填、运行时自动投影、复合租户所有权、append-only/REPLACE 防护和有业务事实时的回滚拒绝；SQLite 仍是当前本地验收基线。

设计与两轮自审见 [PRD-051 设计文档](docs/prd/PRD-051-finops-ledger.md)，公开合同见 [FinOps Ledger API](docs/api/finops-ledger.md)，最终测试记录见 [PRD-051 测试记录](docs/testing/PRD-051-tests.md)；权威 Browser 与停服终态证据位于 `artifacts/prd051/20260727T043500-browser-success-02`。
