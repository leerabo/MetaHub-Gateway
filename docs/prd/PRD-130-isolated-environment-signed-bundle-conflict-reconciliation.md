# PRD-130 隔离环境签名包异步管理与冲突合并

状态：设计冻结，尚未编码；计划迁移 0122。依赖 PRD-121..129，范围以 Excel PRD-130 为准。

## Excel 权威合同

- API：`POST /edge/offline-bundles/export`、`POST /edge/offline-bundles/import`；CLI：`metahub bundle verify`、`metahub bundle apply`。
- 对象：`OfflineBundle`、`BundleManifest`、`BundleTransfer`、`OfflineSyncCursor`、`OfflineConflict`。
- 事件：`edge.offline_bundle_exported`、`edge.offline_bundle_applied`、`edge.offline_conflict_detected`。
- SLO：10GB 以内包验证 < 10 分钟；导入可断点恢复；冲突报告 100% 可追溯。

## 1. 产品闭环

对完全隔离环境，云端经双人审批导出目标 Deployment 的签名 Desired State、License、Release 及最小元数据；本地 CLI 在未解包到 active 区前完成目标、hash、签名、有效期、version vector、容量和兼容预检，再按各领域原子合同应用。本地可导出状态、Usage、审计和验证的 exact 摘要包，云端以 cursor/version vector 去重导入。

如果 cloud base 与 local base 均已前进，系统创建逐字段领域冲突，保留两侧 hash、base 和安全摘要；任何一侧都不自动覆盖。经有权限人员选择 `keep_cloud|keep_local|supersede_with_new` 后，生成新的单调 generation 和签名包。产品明确标示“异步管理/最后交换时间”，不声称实时云管。

## 2. 0122 数据与 bundle 格式

- OfflineBundle/Manifest 不可变，绑定 bundle ID、direction、deployment/workspace/tenant、用途、created/expires、producer generation、per-domain version vector、entry hashes/Merkle root、encryption/signature key version。
- Transfer 保存分块 hash、连续 cursor、介质 receipt 和状态；重复 chunk 幂等，同 sequence 不同 hash 为冲突。
- OfflineSyncCursor 按 deployment/direction/domain 单调推进；OfflineConflict 保存 base/cloud/local hash、固定类型、resolution actor/approval/new generation。
- 包采用流式校验和 staging，拒绝路径穿越、符号链接、压缩炸弹、未知 entry、错误目标、过期和重复 apply。Secret、设备私钥、备份正文、Prompt/Response 永不进入包。

## 3. 审批、权限与页面

导出/导入均需要对应 Workspace 授权、平台审批、step-up、非自批和短时 transfer grant；isolated Agent 只本地 verify/apply。平台不能因导入获得 Membership 或本地 Secret。Console 显示包方向、内容类别、签名/有效期、最后交换水位和冲突决策；CLI 默认先 verify，apply 需要显式目标和确认，输出只有固定 outcome/hash。

## 4. 第一轮自审：介质与重放

- P0：签名只覆盖 manifest、不逐 entry 绑定 hash 可替换载荷。修正为 Merkle root + 每 entry hash/size/type，签名覆盖根 manifest。
- P0：包不绑定 Deployment 可跨客户复制。修正为 owner、direction、purpose 和 generation 全部进签名内容。
- P0：先解压后验签会触发路径/容量攻击。修正为流式 envelope/root 校验、预算控制和 staging 安全解包。
- P1：重复介质导入重复计费/审计。修正为 bundle receipt、domain cursor 和 source event ID 幂等。

## 5. 第二轮自审：冲突与零 Secret

- P0：last-write-wins 会覆盖断网本地变更。修正为 base/version vector 检测双向前进，冲突必须显式解决并生成新版本。
- P0：本地状态包夹带 Secret reference 对应值或诊断自由文本。修正为 exact export schemas、本地敏感扫描和禁止字段递归检查。
- P1：冲突决策直接改 head 会丢审批证据。修正为 resolution 不可变、绑定双方 hash/actor/approval，并发布新 generation。
- P1：大包验证一次失败后重头开始不满足恢复。修正为分块 hash、连续 cursor 和可验证 staging journal。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0122 迁移；错误目标/方向/签名/hash/过期/乱序/重复、路径穿越/符号链接/压缩炸弹；10GB 流式模拟与断点恢复；cloud/local 双向变更的全部 conflict/resolution；审批撤权；零 Secret/正文；三浏览器包/冲突页和 CLI；验证 SLO、每项冲突可追踪、临时 staging/进程/监听静止终态。PRD-130 独立提交后启动完整 G5 门禁。
