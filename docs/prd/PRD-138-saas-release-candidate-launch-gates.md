# PRD-138 SaaS 发布候选、核心迁移波次与全产品门禁

状态：已完成。依赖 PRD-090..137；范围以 Excel PRD-138 为准。0130、固定门禁注册表、签名证据、
独立会签、四波执行、独立回滚复验、RC 评审和 Platform Launch Gates Console 已交付。本条只能形成
`release_candidate`，公开 GA 必须等待 PRD-139..147 分域迁移和 PRD-148 最终门禁。

## Excel 权威合同

- API：`GET /platform/launch-gates`、`POST /platform/launch-gates/{id}/evidence`、
  `POST /platform/launches`、`POST /platform/launches/{id}/rollback`。
- 对象：`LaunchGate`、`LaunchEvidence`、`LaunchWave`、`GoNoGoDecision`、`LaunchRollback`、
  `PostLaunchReview`。
- 事件：`launch.gate_failed`、`launch.wave_started`、`launch.wave_completed`、
  `launch.rolled_back`。
- SLO：Control 99.9%、Gateway 99.95%、关键页面 p75 < 1.5 秒、连续 72 小时无 P0/P1。
- 边界：产品、SRE、安全、测试、财务和法务是会签 actor kind，不新增可混用的租户角色；
  platform_owner 只能在全部会签后执行；任何单一账号都不能绕过 P0 门禁。

## 1. 产品闭环与非目标

release_manager 以代码树、迁移集、公开合同集、测试清单、20 个 owner-qualified 制品和四个波次计划创建
不可变候选。platform_verification 通过固定 verifier 提交签名证据；产品、SRE、安全、测试、财务和法务
分别使用独立账号会签。全部证据新鲜、全部签署权限仍有效后，独立 platform_owner 才能顺序执行内部、
个人试点、企业试点和云管私有试点。每波必须完成观察窗口并提交健康水位；失败可退回唯一前一检查点，
且由不同账号复验。四波结束后的评审结论只能是 `release_candidate`。

本条不执行公开 GA，不替代 PRD-139..147 的存量控制台分域迁移，不允许手工声明测试通过，不保存测试正文、
客户正文、凭据或对象 URL，也不扩张 Workspace 数据访问边界。功能开关、Runbook、支持排班、对账、法务、
灾备、支付、Edge 和长稳只以签名 digest/receipt 进入治理事实。

## 2. 0130 数据、owner 与恢复边界

- `launch_gate_policies` 固定保存 WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW、迁移、灾备、支付、
  Edge、隐私、合规、供应链、租户隔离和 72 小时长稳共 20 个 active v1 policy；注册表和角色映射不可变。
- `launch_candidates` 保存代码/迁移/合同/制品/测试/policy-set digest、generation、状态、申请人 revision、
  CAS revision 和时间；相同 canonical candidate 不能重复创建，新 generation 会使旧 collecting/signing/ready/RC
  事实进入 stale，但不删除历史。
- `launch_candidate_artifacts`、`launch_evidence` 和 `launch_waves` 都保存 Workspace/tenant 复合 owner；
  artifact、gate、owner 和 digest 形成复合外键，禁止同形 ID 或跨 owner 拼接。
- `launch_go_no_go_decisions` 对 candidate + actor kind 和 candidate + operator 双唯一；保存 operator revision、
  permission、decision digest 和用途隔离 HMAC 摘要，不保存 step-up 或签名密钥。
- `launch_rollbacks`、`post_launch_reviews`、`launch_events/outbox`、`launch_action_commands` 和
  `launch_scheduler_state` 保存回滚复验、RC 评审、事件、幂等和 freshness 水位；所有状态变化保持审计事实。
- Down 只允许在没有候选、证据、决策、波次、回滚、评审、事件、outbox、action 或 scheduler 业务水位时执行；
  0130 回退后恢复 0129 的权限和 tenant-deletion manifest 完整性边界。

## 3. 精确 API、签名证据与幂等

四条 Platform Cookie API 构成闭集。Bearer、WorkspaceSession、SupportSession、重复 Cookie、query、未知字段和
非预期 body 全部拒绝；写操作同时要求 exact Origin/CSRF、一次性 step-up、`Idempotency-Key` 和 quoted
`If-Match`。幂等键只保存 SHA-256 并绑定 operator + action + canonical request；同键异体冲突。

证据 envelope 精确覆盖 candidate/policy/gate、Workspace/tenant、artifact、verifier/version、outcome、指标、
environment/provenance digest 和 UTC 时间窗。签名在事务外由固定 verifier 验证，写入前重新恢复账号、权限、
candidate revision、owner 和 gate generation；签名明文只在请求生命周期存在，数据库只保存摘要。普通门禁
最长 72 小时，长稳门禁必须覆盖 72..96 小时；缺 verifier、失败、过期、未来时间或 policy 漂移均失败关闭。

## 4. 会签、动态撤权与职责分离

权限扩展保持现有角色，不创建新角色：release_manager 创建；platform_verification 提交证据并签 test；
platform_owner、platform_ops、platform_security、platform_billing、platform_compliance 分别签 product、sre、
security、finance、legal。候选申请人不能会签；六个 actor kind 必须由六个不同账号完成。

第六个签名写入后、候选进入 ready 前，服务重新验证 20 份证据的新鲜度、policy-set digest、六个 operator 的
当前 status/revision/role permission。每次 start/resume/complete/review 都重复该验证，因此撤权、角色变化、
证据过期或注册表漂移不能依靠旧页面状态放行。执行者必须不同于申请人和六个签署人。

## 5. 四波状态机、回滚与 RC 终态

四波顺序固定为 `internal -> personal_pilot -> enterprise_pilot -> managed_private_pilot`。后波只能在前波
completed 后开始；start 生成观察截止时间，pause 不放宽门禁，resume 重新计算完整观察窗口，complete 必须在
截止时间后携带健康水位 digest。第四波完成后进入 review_pending，评审要求四波均 completed、当前会签仍有效，
并写入 summary/action-items digest。

回滚只能从当前波退到唯一前一检查点；请求绑定 trigger、checkpoint 和 job receipt digest，并把受影响波次置为
rolled_back。复验账号必须不同于回滚申请人，也不能是该候选此前的波次执行者或评审执行者。复验 passed 才形成
rolled_back 和 `launch.rolled_back`；复验 failed 使候选失败关闭。任何路径均不存在公开 GA 状态或动作。

## 6. Scheduler、事件与 Console

持久 scheduler 使用 30 秒 lease、随机 token 摘要、generation/revision 和事务锁；它把活动候选中过期证据变为
failed、生成 `launch.gate_failed`，并有界处理 outbox。租约由同一事务获取、处理和释放，崩溃后可按持久水位接管。

Platform Launch Gates Console 由后端权限投影决定导航和动作，展示 20 个 gate、裁剪后的 digest/metric/freshness、
六类签署、四波、回滚和 RC 评审。客户端严格校验响应字段、owner、ID、状态和 gate registry；Abort/ticket/revision
阻止迟到响应。TOTP、step-up grant 和 evidence signature 不进入 Storage/URL/DOM，表单结束、失败或撤权后清空。
候选创建的 step-up Workspace 与后端一致，按 gate code 排序选择确定 owner，避免多 Workspace 输入顺序漂移。

## 7. 第一轮自审：门禁真实性、权限与 owner

- P0：只统计六个签名而不在最终签名时恢复旧签署人的当前权限，会短暂显示伪 ready。修正为第六签名事务内
  重新验证全部证据、policy 和六个 operator revision/permission，失败时连同新签名原子回滚。
- P0：回滚只禁止申请人自验，曾执行波次的另一账号仍可复验自身执行结果。修正为查询 candidate action 事实，
  所有 wave/review executor 都不能成为 rollback verifier。
- P1：Console 以 artifact 输入首项申请 step-up，而后端以排序后的 gate owner 消费，多 Workspace 时会不一致。
  修正为前后端都按 gate code 选择确定 owner，并增加不同 Workspace 的回归断言。
- P1：请求若接受任意 verifier、URL 或测试正文会把治理面变成伪证据入口。实现保持固定 20 policy、固定 verifier、
  canonical envelope 和 digest-only DTO，缺 verifier 失败关闭。

第一轮修正后无未关闭 P0/P1。

## 8. 第二轮自审：并发、撤权、回滚与隐私

- P0：旧 revision、过期证据或 policy 漂移在波次中复用会绕过最新门禁。确认 start/resume/complete/review 均在
  同一写事务内重新验证证据、签署权限、candidate revision 和当前 policy-set。
- P0：并行波次、跳波或任意回滚目标会破坏可恢复性。确认固定 sequence、前波 completed、单 active candidate、
  quoted revision 和 `_PREVIOUS_WAVE` 唯一目标共同失败关闭。
- P1：签名、TOTP、step-up、对象 URL 或原始证据进入日志/DTO/Storage 会泄漏。确认服务只保存签名/dataset digest，
  Console 敏感输入生命周期有 finally 清空，测试与浏览器审计无敏感投影。
- P1：scheduler 接管若无持久状态会重复事件。确认 lease/revision、水位、事件唯一键和 outbox action 均持久化，
  SQLite 写事务串行化接管；完整跨数据库/故障矩阵留到批次 G6。

第二轮修正后无未关闭 P0/P1。专项结果、真实浏览器记录和敏感审计见
`docs/testing/PRD-138-tests.md`；下一权威项为 PRD-139。

## 9. G6 补充：正式证据验签接线

2026-09-07 复核发现，HTTP 接口只读取 `app.state.launch_evidence_verifier`，正式应用未安装实现，
历史合同测试使用恒真替身。因此签名 DTO 和状态机测试不证明正式验签可用，必须补齐该实现后再做真实演练。

配置 `METAHUB_LAUNCH_EVIDENCE_TRUST_BUNDLE_PATH` 指向部署方只读公钥 JSON。沿用现有 Ed25519 库，
公钥条目按 gate/verifier/version 绑定，并有 key ID 和 UTC 生效/失效时间；不在 Control 保存签名私钥。
签名内容为固定用途前缀 `metahub.launch-evidence.v1`、零字节和 canonical envelope 的 32 字节 SHA-256。
签名编码沿用现有 DTO 可接受的无填充 base64url，不增加公开路由或更改数据库事实结构。

第一轮编码前自审：拒绝请求自带公钥、错 gate/version、未知 JSON 字段、重复键或 key ID、非法编码、
过大文件和过期信任。服务与验签器共享 envelope 摘要计算，避免 owner、时间或数字规范化分歧。
没有配置时保持 `launch.verifier_unavailable`；错误信任或错误签名不能退回测试替身。

第二轮编码前自审：每次提交重读信任文件，轮换/移除影响后续验签，不用缓存延长旧钥信任。
此前已入库证据只保留签名摘要，不能虚称能够追溯重验原始签名；追溯撤销应使候选失效并重新收集。
路径不进入配置摘要、repr 或响应。真实加密测试不能被记为真实 QA 证据或六方签核。
本轮范围不涉及 PRD-148，也不把旧冻结版本健康采样称为新验签实现的长稳证据。

## 10. G6 补充：发布治理原生数据库合同

2026-09-07 在验签接线修复提交后继续复核，发现 `_connect` 直接执行 SQLite PRAGMA、schema guard
直接查询 `sqlite_schema`。G7 原生数据库专项并未覆盖发布治理业务，不能沿用其通过结论。
设计使用已存在的 backend-aware schema helper，并让连接工厂负责各后端的外键启用。

第一轮编码前自审：仅移除 SQLite 语法仍不足以保留并发合同。PostgreSQL 适配器将
`BEGIN IMMEDIATE` 转为普通 BEGIN，候选 generation、revision、证据和 scheduler 在读取前必须获得
发布治理自己的写入串行锁。计划用现有 singleton scheduler 行的事务级 `FOR UPDATE` 锁覆盖四个
写入入口，SQLite 保持 `BEGIN IMMEDIATE`；不创建新迁移或扩张全产品连接适配器。

第二轮编码前自审：锁必须先于权限/候选/幂等读取，异常通过现有事务回滚释放；验签预检保持只读，
密码学验证仍在事务外，提交阶段重新加锁并复核。测试使用真实 PostgreSQL baseline 和独立数据库
克隆，复用相同 API/状态机/签名合同并补并发测试；测试私钥、会签 actor 和加速时间不能算实际发布证据。
治理行锁只串行化此模块的写入，不宣称等价于 SQLite 的全数据库写锁或已完成全产品原生并发矩阵。
