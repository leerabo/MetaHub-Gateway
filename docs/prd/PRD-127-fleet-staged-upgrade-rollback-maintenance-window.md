# PRD-127 Fleet 分批升级、回滚与维护窗口

状态：设计冻结，尚未编码；计划迁移 0119。依赖 PRD-121/124..126，范围以 Excel PRD-127 为准。

## Excel 权威合同

- API：`POST /platform/releases`、`POST /platform/releases/{id}/rollouts`、`POST /fleet/rollouts/{id}/pause`、`POST /edge/deployments/{id}/rollback`、`GET /fleet/compatibility`。
- 对象：`GatewayRelease`、`ReleaseArtifact`、`ReleaseChannel`、`Rollout`、`RolloutBatch`、`MaintenanceWindow`、`RollbackAttempt`。
- 事件：`fleet.rollout_started`、`fleet.rollout_paused`、`fleet.rollout_failed`、`fleet.rollback_completed`。
- SLO：1% Canary 观察至少 30 分钟；单节点升级 < 10 分钟；失败自动暂停 < 60s。

## 1. 产品闭环

release_manager 经双人审批发布签名、不可变 release/artifact/SBOM/兼容矩阵；租户选择 release channel、维护窗口和冻结期。platform_ops 创建 rollout 时只能选择符合客户策略的 Deployment。调度器先跑兼容/DB/容量/备份预检，再按 Canary 和确定性批次下发；Agent 排空已接受请求、安装 staging、执行可恢复迁移、健康门禁后切换。失败阈值或信号缺失自动暂停，新批次不得继续；N-1 artifact/LKG 保留以执行有证据回滚。

## 2. 0119 数据与状态机

- 扩展现有 SaaS `gateway_releases` 前先明确 owner；私有 release 使用独立 `edge_gateway_releases`，避免 Cell rollout 与客户节点混写。
- artifact manifest 绑定 hash、signature purpose、schema min/max、Agent/Control compatibility、down capability 和 SBOM。
- Rollout/Batch/Target 使用 generation、稳定 cohort hash、状态机和 platform job lease；MaintenanceWindow 属于 tenant/deployment policy，平台不能绕过 freeze。
- Rollback 是新的单调 desired generation，不回写历史；数据迁移若不可逆则在预检阶段禁止声称可回滚。

## 3. 权限与页面

release_manager 发布、另一具备 release approval 的平台主体审批；platform_ops 执行/暂停但不能发布或绕过客户窗口；tenant owner/admin 选择 channel/window/freeze 和单节点回滚；Agent 只接受自身目标、已签名兼容 artifact。Platform rollout 页面和 Workspace 维护页面使用独立 API/DTO，均显示批次、门禁、暂停原因和恢复动作。

## 4. 第一轮自审：制品与客户控制

- P0：复用 SaaS Cell release 表会混淆 owner 和回滚范围。修正为私有 Edge 独立表，通过共享签名/rollout primitives 复用实现。
- P0：平台可忽略客户冻结会越权。修正为 target selection 和 Agent apply 双重验证 maintenance policy revision。
- P0：只有 rollout 启动时验证签名，下载后可能被替换。修正为 Agent 在 apply 前再次校验 root manifest/artifact hash/signature。
- P1：随机批次不可重复审计。修正为 deployment ID + rollout seed 的确定性 cohort。

## 5. 第二轮自审：暂停与回滚真实性

- P0：健康信号丢失被当通过会扩散坏版本。修正为必需信号缺失失败关闭并在 60 秒内暂停。
- P0：宣称回滚但 schema 已不可逆会产生数据损坏。修正为 release 声明 migration compatibility，预检拒绝没有 N-1 路径的 rollout。
- P1：暂停只影响未派发节点，leased 节点继续切换。修正为 rollout generation/cancel watermark，Agent 在 commit 前重新检查。
- P1：排空超时强杀请求违反不中断。修正为有界 drain，超时默认节点失败并暂停，不强制切换。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0119 迁移；签名/兼容/窗口/冻结/审批；确定性 Canary、健康缺失/失败自动暂停、并发 pause、租约重启；真实请求排空、N/N-1 升级与回滚、不可逆 migration 阻断；三浏览器进度/失败节点；Canary/节点/暂停 SLO；制品敏感扫描和静止终态。
