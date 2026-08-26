# PRD-129 私有 Gateway 备份、恢复与灾备演练

状态：设计冻结，尚未编码；计划迁移 0121。依赖 PRD-121..128，范围以 Excel PRD-129 为准。

## Excel 权威合同

- 本地 API：`POST /local/backups`、`POST /local/restores`；云端 API：`GET /edge/deployments/{id}/backup-status`、`POST /edge/deployments/{id}/restore-drills`。
- 对象：`EdgeBackup`、`BackupChunk`、`BackupKeyEnvelope`、`RestoreJob`、`RestoreVerification`、`PrivateDrDrill`。
- 事件：`edge.backup_completed`、`edge.backup_failed`、`edge.restore_completed`、`edge.dr_objective_missed`。
- SLO：默认 RPO <= 24h、RTO <= 4h，可按套餐提升；季度恢复演练通过。

## 1. 产品闭环

客户选择本地/客户对象存储和保留策略。本地 backup owner 在一致性 checkpoint 后，按配置/策略、账本水位、审计、本地 Secret 分域切块、压缩、加密并生成签名 manifest；每块独立 AEAD/hash，根数据密钥只由客户 KMS/恢复密钥包裹。云端只接收时间、generation、size bucket、check outcome、RPO 状态和 manifest 摘要。

恢复先在 staging 验证完整性、owner、schema/N/N-1 和密钥，再原子替换；替换节点生成新的 DeviceIdentity，通过 PRD-120 重注册，不恢复旧私钥/session。恢复后核对配置、Usage cursor、审计链和 Secret reference，运行最小真实请求。DR drill 使用隔离 target，成功/失败保留证据且不触碰生产 head。

## 2. 0121 数据与本地制品

- 云端保存 BackupStatus/Restore/Drill 元数据和审计，不保存 chunk、envelope、对象 URL、Secret 或客户路径。
- 本地 EdgeBackup/Chunk/Envelope/RestoreJob 不可变；manifest 绑定 deployment、source generation、schema、chunk Merkle root、KMS key ID 摘要、created/expires。
- 保留/删除受 Legal Hold、delivery evidence 和 audit policy 控制；删除只影响客户选定对象并有 tombstone/receipt。
- restore step journal 与 platform job 使用 generation/lease，崩溃后幂等继续或补偿；生产切换前保留旧 active slot。

## 3. 权限与恢复边界

备份密钥由客户控制；本地高权限 + tenant 批准才能备份/恢复，恢复生产需 step-up/非自批；platform 只看状态摘要，SupportGrant 也不能下载正文或密钥。Console 展示 RPO、最近验证、保留和演练结果；恢复向导必须明确 target、覆盖范围和可回退 checkpoint。

## 4. 第一轮自审：Secret 与设备身份

- P0：把 OS KeyStore 根密钥直接备份会把单机边界复制出去。修正为 Secret 数据密钥经客户恢复密钥重新包裹，根密钥不导出。
- P0：恢复旧设备私钥/session 会克隆身份。修正为排除设备私钥/session，替换节点重新注册并吊销旧 identity。
- P0：云端存对象 URL/路径可能泄漏客户拓扑。修正为只存不透明 status ID 和摘要。
- P1：在线文件复制无法保证一致水位。修正为组件 checkpoint 和 manifest source generations。

## 5. 第二轮自审：恢复真实性与演练隔离

- P0：只做校验和不做真实恢复不能证明 RTO/RPO。修正为隔离环境完整 restore、重新注册、配置/账本/审计核对和真实调用。
- P0：损坏块恢复到一半再失败会破坏 active。修正为 staging 全量验证，最终原子切换。
- P1：错误版本自动迁移可能不可回退。修正为 N/N-1 matrix 和显式 upgrade plan，不兼容失败关闭。
- P1：drill 复用生产 Deployment ID 会污染 Fleet/License。修正为隔离 drill owner 和明确 simulation 投影。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0121 云端/本地迁移；分块损坏/缺失/错误 key/owner/version、checkpoint 并发、崩溃恢复；真实替换节点重注册且旧身份吊销；配置/账本/审计/Secret 核对；Support 无正文；三浏览器备份/恢复向导；RPO/RTO 与季度 drill；云端/支持包/日志敏感 0、临时 restore target 和静止终态。
