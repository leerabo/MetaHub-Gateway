# PRD-107 功能开关、租户分群与渐进发布

状态：实现完成，专项验证通过（2026-08-23）。依赖 PRD-106 任务运行时，范围以 Excel PRD-107 为准；
测试证据见 `docs/testing/PRD-107-tests.md`。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /platform/feature-flags`、`POST /platform/feature-flags`、
  `POST /platform/feature-flags/{id}/rollouts`、`POST /platform/feature-flags/{id}/kill`、
  `GET /workspaces/current/capabilities`。
- 核心数据对象：`FeatureFlag`、`FlagVersion`、`TenantCohort`、`FlagRollout`、`FlagEvaluationSnapshot`、`FeatureExposure`。
- 事件：`feature.rollout_started`、`feature.rollout_paused`、`feature.killed`、`feature.exposed`。
- 性能/SLO：`Gateway本地判断p95<1ms`、`控制面判断p95<20ms`、`开关传播<5s`。

## 1. 目标与边界

交付后端权威 Feature Flag、环境/Region/租户分群、稳定百分比分桶、前置条件、不可变版本、曝光事实、健康门禁、
暂停/回滚与全局 Kill Switch。前端只消费当前 Workspace 的最终能力结果，不能通过 URL、Storage、DOM 或请求参数
启用能力。本条不成为通用策略语言，也不提前实现 PRD-108 配置快照细节。

## 2. 0099 数据与权限

- `feature_flags`：稳定 code、风险等级 `standard|p0`、owner、状态与 head revision。
- `feature_flag_versions`：不可变规则版本；只允许结构化环境、region、cohort、percentage 和 prerequisite，拒绝表达式/脚本。
- `tenant_cohorts` 与 membership：显式 tenant/workspace 复合 owner，成员快照带 revision/hash，不能按客户正文分群。
- `feature_rollouts`：`draft|pending_approval|scheduled|running|paused|completed|rolled_back|killed`，绑定不可变版本、
  时间窗、百分比阶梯、健康阈值、revision 和 PRD-106 job。
- `feature_rollout_approvals`：P0 需两名不同、当前仍具 release 权限的操作员；发起者不能自批第二票。
- `flag_evaluation_snapshots`：workspace/subject/version/result/reason code/hash；不保存 subject 原始标识。
- `feature_exposures`：按 flag/version/workspace/pseudonymous subject/request 唯一追加，回滚不删除。
- 权限为 `platform.feature.read/write/approve/kill`：release_manager 读写审批，platform_owner 全部，platform_ops 只读与
  紧急 kill；Workspace 只能通过能力投影读取自身结果。
- down 对任何 version/rollout/approval/exposure 事实拒绝；回滚用 kill/兼容读路径。

## 3. 评估与发布协议

稳定桶使用服务端 HMAC 对 `flag_code + version + workspace + subject_kind + subject_id` 计算，不暴露盐或阈值；同一版本
结果稳定，版本变化显式重评。evaluation 顺序固定为：动态权限/租户状态/entitlement 撤权 -> kill -> prerequisite ->
环境/region -> cohort -> percentage。任何前置撤权都优先于 Flag allow。

Rollout 每个阶梯由 PRD-106 job 执行，发布不可变版本并读取冻结的 `RolloutHealthSummary` 合同；该合同最初由现有
request/provider/Gateway 低基数聚合事实生成，PRD-109 完成后成为规范生产者但不改变消费者合同。摘要不可用、过期、
验签失败或水位倒退时发布安全暂停，不继续扩大；调度冻结 source watermark，worker 只消费该水位并验证域隔离 HMAC。
健康恶化时 CAS 到 paused。回滚发布新 head 指向旧版本，不修改或删除
旧版本/曝光。Kill Switch 创建最高优先级作业并直接写权威 kill fence，配置传播由
PRD-108 接管；运行时不可用时 kill fence 仍可由数据库请求守卫读取。

## 4. API 与事件

- `GET /platform/feature-flags`：分页、状态和风险过滤；严格公开 DTO。
- `POST /platform/feature-flags`：用显式 `create|version` closed action 创建 flag/首版或 CAS 追加候选版本；
  候选版本不自动切换 head。CSRF 与 idempotency 必须同时满足。
- `POST /platform/feature-flags/{id}/rollouts`：创建/审批/暂停/恢复/回滚使用明确 action DTO，不接受通用 patch。
- `POST /platform/feature-flags/{id}/kill`：高风险命令，需独立幂等键、revision、原因枚举和当前权限。
- `GET /workspaces/current/capabilities`：WorkspaceSession only、无 tenant 参数、no-store，返回 flag code、enabled、
  public version 和固定 reason；未知字段拒绝。
- 事件 `feature.rollout_started|rollout_paused|killed|exposed` 由同事务 outbox 恢复，不含 cohort 原始成员或 subject。

## 5. Console 与测试

Platform `feature-rollouts` 页面展示 Flag、版本、阶梯、审批、健康门禁和不可变曝光计数；Workspace 页面只在既有
能力消费点使用后端结果，不新增“本地开关”。权限漂移先清理命令与草稿，再取消请求。

Gateway 只消费上游已验签的最终 Workspace capability snapshot；本地 owner/generation/expiry gate 原子替换，未知、
过期、跨 Workspace 或旧 generation 均失败关闭。PRD-108 后续接管跨服务分发，不改变此本地判断合同。

专项覆盖稳定分桶、规则顺序、P0 双人审批、自批拒绝、动态撤权、健康自动暂停、kill 竞态、回滚保留曝光、乱序 job、
跨 Workspace 零泄漏、Storage 禁止和迁移往返。性能门禁：Gateway 本地判断 p95<1ms、控制面 p95<20ms、传播<5s。

## 6. 第一轮自审：权威结果与分桶

- P0：客户端评估或本地 override 可越权。修正为服务端生成能力投影，客户端无 override 接口。
- P0：先评 Flag 再查撤权会让被禁用 Key/Workspace 暂时恢复。修正为撤权与 entitlement 是最高优先级 fence。
- P1：使用语言运行时 hash 会跨进程漂移。修正为版本化 HMAC 算法和 canonical 输入。
- P1：自由表达式规则可形成注入与不可审计分支。修正为固定结构化条件和 exact enum。
- P1：回滚原地改版本会破坏曝光归因。修正为不可变版本与新 head。

## 7. 第二轮自审：审批、健康与隐私

- P0：P0 发布由一人创建并审批不满足双人控制。修正为两名不同当前授权 operator，发起者不能成为第二票。
- P1：健康门禁直接读取高基数租户指标会泄漏且反向依赖 PRD-109。修正为冻结 `RolloutHealthSummary`、固定 SLI 与签名聚合；
  先由现有聚合事实供给，PRD-109 后续接管生产，缺失时安全暂停。
- P1：kill 依赖异步队列会错过时限。修正为同步 kill fence + 高优先传播 job 双路径。
- P1：曝光保存账号标识会形成行为画像。修正为轮换域隔离 pseudonym，并限制保留期。
- P1：平台页面权限隐藏不足。修正为 API 每请求验证 PlatformSession/permission/revision，UI 只作投影。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：平台发布事实与 Workspace capability 投影分离；Workspace 无写入口，撤权/entitlement 永远
  高于 Flag，客户端 Storage/参数/DOM 不构成授权来源。
- 第二轮（迁移/依赖/失败策略）：0099 对 version/rollout/approval/exposure 事实执行 down guard；依赖只指向 106 及既有
  PRD，`RolloutHealthSummary` 消除了对 109 的循环依赖；健康摘要异常时暂停，kill 同步围栏并由 108 异步传播。
