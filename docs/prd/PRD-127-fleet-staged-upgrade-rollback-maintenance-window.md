# PRD-127 Fleet 分批升级、回滚与维护窗口

状态：已完成；迁移 0119、Control API、Shared SDK、Gateway Agent、Workspace/Platform Console 已交付。依赖 PRD-121/124..126，范围以 Excel PRD-127 为准。

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

## 6. 实现闭环

- 0119 新增私有 Gateway release/artifact/channel、维护窗口、rollout/batch/target、升级命令、rollback attempt、事件/outbox 与权限事实；状态 CHECK、不变触发器、复合 owner、tenant deletion manifest 和业务 down guard 已落盘。
- Control API 实现 release 双人发布、兼容性查询、确定性 target 选择、客户窗口/冻结/策略 revision 复核、rollout pause、Workspace 配置与单节点 rollback，以及 Agent lease/start/report。所有写入绑定幂等键、generation/nonce 和权威 owner。
- Shared SDK 实现用途分域 Ed25519 release/command 签名、exact manifest、兼容/窗口验证及持久升级执行器；Gateway 同步器只访问本设备 machine routes，先排空、staging、校验、切换和持久 receipt，并保留 LKG。
- Workspace 页面管理 release channel、维护窗口、冻结和单节点回滚；Platform 页面管理 release、rollout、门禁和暂停。两个入口使用独立 Session/API/DTO，权限、状态和迟到响应保持分域。

## 7. 两轮实施自审

- 第一轮按 owner、审批和客户策略复核。修正 Console 扩展权限白名单未接受 PRD-125..127 权限、合法登录会被错误拒绝的问题；补齐完整扩展集回归。确认平台 release 发布需要非自批审批，rollout 只能选择通过兼容/容量/备份/维护窗口预检的 Deployment，Workspace channel/window/freeze 使用 CAS，Agent apply 再次验证签名、owner、hash、generation 和策略 revision。Platform 与 Workspace DTO、Cookie/CSRF/client/store 不复用。
- 第二轮按崩溃恢复、deadline 和移动端复核。发现短 lease 到期时命令仍停留在 `leased`，Agent 崩溃后无法重新领取；修正为事务内清除旧 lease nonce/expiry、命令回到 `issued`、target 回到 `selected`，随后可重新 lease/start/report。总 deadline 到期时 rollout 命令失败并自动暂停，rollback attempt 进入失败终态，迟到结果不能覆盖；新增恢复闭环回归。修正宽表依赖页面级横向溢出的问题，为升级表提供有界局部滚动，并在真实 390x844 视口验证无页面溢出。

两轮实施自审均为修正后通过，无未关闭 P0/P1。下一权威项为 PRD-128。

## 8. 专项验证与批次边界

0119 `0118 -> 0119 -> 0118 -> 0119`、业务 down guard、Control API/导航共 `5/5`；Shared SDK `7/7`；Gateway 同步 `2/2`；PRD-127 Console `4/4`，PRD-120..127 Console 必要回归 `41/41`。Workspace 与 Platform 在真实 390x844 Chromium 视口完成渲染验收，页面无横向溢出且浏览器日志为空。完整命令、敏感审计与结果见 [PRD-127 测试记录](../testing/PRD-127-tests.md)。

真实 Linux/代理/断网请求排空、N/N-1 制品切换、Canary 30 分钟观察、节点 10 分钟升级、60 秒自动暂停、三浏览器/多缩放、长稳、0111..0122 全迁移矩阵、性能和安全全门禁按批次规则保留到 PRD-130 独立提交后统一执行 G5，当前聚焦测试不替代这些证据。
