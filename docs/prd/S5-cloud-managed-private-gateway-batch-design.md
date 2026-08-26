# S5 云管私有 Gateway 批次设计

状态：设计冻结；PRD-120..122 已完成，PRD-123..130 尚未编码。权威范围来自
`企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 的 S5/G5，当前下一项为 PRD-123。

## 1. 批次目标与实施顺序

本批次交付客户自有环境中的私有 Gateway，并由 SaaS 控制面通过最小、可撤销的云边合同管理。固定顺序为
`120 -> 121 -> 122 -> 123 -> 124 -> 125 -> 126 -> 127 -> 128 -> 129 -> 130`：先建立设备信任，再建立签名配置、零敏感遥测和本地 Secret 边界，随后完成安装验收、Fleet 运维、升级授权、恢复及完全隔离环境的异步签名包。

每条 PRD 独立保留设计、迁移、聚焦测试、测试记录、敏感审计和 Git 提交。开发期只执行受影响的单元/API 冒烟；PRD-130 独立提交后统一执行 G5 数据库、集成、FLOW、真实浏览器、性能、安全和必要回归。

## 2. 三个不能混用的 owner

- `workspace_gateway_placements` 是 SaaS 云 Cell 放置事实，继续由 `platform_gateway` 所有；它不是客户私有节点。
- 新 `EdgeDeployment` 是 Workspace/tenant 复合 owner 下的客户自有部署。任何 Workspace API 都从会话和数据库恢复 owner，跨 Workspace 返回同形 404。
- `DeviceIdentity` 是单个 EdgeDeployment 的非人类主体。它不继承 Membership、PlatformRole、浏览器 Cookie 或通用内部服务身份，只能调用自身 Deployment 的显式 machine-safe 合同。
- 平台角色不自动成为 Workspace 成员；租户角色不能访问 `/platform`。支持访问继续使用现有 SupportGrant：客户授权、平台与 Workspace 双审批、短 TTL、动态撤权和字段裁剪。
- 云端只拥有引用、摘要、水位、签名制品和状态。Provider Key、Token、私钥、Prompt、Response、客户正文及本地备份正文始终由客户环境拥有。

## 3. 共享模块与既有能力

- Control 新增 `edge_management` 业务域，承载 EdgeDeployment、设备证书、期望状态、遥测收据、Fleet 投影、升级/授权/恢复/离线包的云端事实；不复制 Workspace、Platform IAM、SupportGrant 或通用审批系统。
- Shared SDK 新增 `private_gateway` 严格合同，统一 canonical JSON、签名/验签、设备主体、单调水位、LKG、遥测白名单、License 和离线包协议；日志对象 `repr=False`，异常只含固定错误码。
- Gateway data plane 新增本地 owner，复用 `config_distribution.ConfigSnapshotOwner` 的先验签后原子替换、generation/revocation watermark 和 LKG 思路，但把私有部署、断网陈旧状态及本地持久恢复作为独立 owner，不改变 SaaS 云 Cell 行为。
- 复用 `platform_gateway` 的 Fleet/容量投影、`config_distribution` 的签名快照、`approvals` 的非自批审批、`platform_jobs` 的租约/恢复、`feature_rollouts` 的分批状态机、`capacity_management`、`disaster_recovery`、`machine_identity`、`data_governance` 和不可变审计/outbox。
- Console 新增 Workspace 的“私有网关”入口和 Platform 的“Gateway Fleet”入口，共享 Deployment 状态、时间线、空态、错误恢复与确认组件；两个入口使用不同 Session、API client 和 DTO，禁止缓存或迟到响应跨域复用。
- CLI 新增 `metahub edge` 子命令族；所有命令支持结构化固定错误码和 `request_id`，Secret 从 stdin/安全文件描述符读取，不进入 argv、shell history 或诊断输出。

## 4. 迁移顺序与共享合同

| PRD | Migration | 主要新增事实 | 回滚边界 |
| --- | --- | --- | --- |
| 120 | 0112 | deployment、registration intent、device identity/certificate/session、event/outbox、权限 | 存在注册、设备、证书、会话或事件即拒绝 down |
| 121 | 0113 | desired state、signed snapshot/artifact、apply/ack、LKG | 存在发布或应用事实即拒绝 down |
| 122 | 0114 | telemetry policy/batch/receipt、usage cursor、failure summary | 存在批次、水位或收据即拒绝 down |
| 123 | 0115 | 云端 SecretReference/状态；本地 Secret/版本/轮换事实使用独立本地 schema | 任一活跃引用或本地 Secret 事实阻断删除 |
| 124 | 0116 | installer release/profile、preflight、installation、audit | 已发布制品或安装事实阻断 down |
| 125 | 0117 | validation/check、support bundle 元数据、delivery acceptance | 验收、下载授权或交付事实阻断 down |
| 126 | 0118 | health snapshot、diagnostic grant/command/result、Fleet 索引 | 活跃诊断或健康事实阻断 down |
| 127 | 0119 | release artifact/channel、rollout/batch/window/rollback | 已发布或执行中的 rollout 阻断 down |
| 128 | 0120 | license、entitlement、capacity grant/lease/audit | 已签发 entitlement 或容量事实阻断 down |
| 129 | 0121 | backup 元数据、restore job/verification、DR drill；正文仅本地 | 备份/恢复/演练事实阻断 down |
| 130 | 0122 | offline bundle/manifest/transfer/cursor/conflict | 已导出、导入或冲突事实阻断 down |

所有迁移采用 expand-migrate-contract，验证 fresh/current-head、逐级、重复执行失败回滚、down guard、`integrity_check=ok` 和外键违规 0。表使用稳定 ID、严格状态 CHECK、revision/generation、UTC 时间、复合 owner 外键及不可变事件；API 统一严格 DTO、`If-Match`/`Idempotency-Key`、no-store 和同形错误。

## 5. 云边协议与失败策略

1. PRD-120 注册令牌只存分域 HMAC，单次短 TTL；设备密钥只在本地生成，云端接收 CSR、公钥摘要和证明，证书绑定 deployment/device/generation。
2. 所有 Agent 连接出站建立 mTLS；受信 TLS 终止层必须把已验证 peer 证据绑定到内部 transport，应用拒绝调用方自报证书头。吊销水位、证书状态和 session generation 任一不一致即失败关闭。
3. Desired State、Installer、Release、License 和 Offline Bundle 使用 canonical manifest、目标 Deployment、generation、有效期、内容摘要和独立签名用途域。验签、owner、版本、有效期及预检全部通过后才能原子切换。
4. 网络故障可使用有界 LKG；签名/owner/hash/乱序冲突属于完整性故障，不得降级到 LKG。安全撤销水位比普通配置使用更短陈旧窗口。
5. 遥测只允许编译期 exact schema：聚合指标、Usage/成本数值、固定失败码、水位和时间桶。未知字段、Secret 名称、自由文本、endpoint 原文和正文一律拒绝。
6. 本地 Secret、备份与支持包在客户环境中处理；云端没有解密密钥。支持包先本地字段白名单、敏感扫描和客户预览，再上传不可变加密制品或摘要。
7. 完全断网环境只通过签名介质包异步交换。重复、乱序、过期或错误目标包失败关闭；双向变更形成显式冲突，禁止 last-write-wins。

## 6. 权限与页面归属

- Workspace `owner/admin` 管理 Deployment、注册、期望状态、安装和验收；`security_admin` 管理遥测级别、本地 Secret 和诊断批准；`finance_admin` 只读本 Workspace Usage/License；普通成员仅按显式权限读取摘要。
- `platform_ops` 管理 Fleet 运营投影，不读客户正文或 Secret；`release_manager` 经双人审批发布签名 Installer/Release；`platform_billing` 签发 entitlement，不获得租户数据访问；`platform_support` 只能在有效 SupportGrant 内读取裁剪结果。
- Agent 仅使用设备证书访问自身 Deployment 的 machine routes。证书轮换、吊销、Deployment 清退或权限 revision 变化后，下一请求重新恢复权威事实。
- 高危期望状态、诊断、发布、License 例外、恢复和离线包导入/导出复用审批/step-up；申请人不能自批，审批权限撤销或 TTL 到期立即失效。

## 7. 两轮批次自审

### 第一轮：信任、owner 与敏感边界

- P0：把私有节点建成 `workspace_gateway_placements` 会混淆 SaaS 云 Cell 与客户资产。修正为新 EdgeDeployment 复合 owner，只共享平台投影和签名基础。
- P0：接受 `X-Client-Cert` 等调用方可控头会伪造 mTLS。修正为受信 TLS 终止层加内部服务认证，peer 证据与连接上下文绑定；直接外部请求一律拒绝。
- P0：把配置完整性失败当网络失败回退 LKG 会接受被篡改状态。修正为仅可证明的传输不可达使用有界 LKG，签名/hash/owner/乱序失败立即隔离。
- P0：遥测、支持包或备份若允许自由 JSON 会泄漏本地 Secret。修正为 exact schema、禁止字段递归扫描、本地预览与零明文门禁。
- P0：PlatformRole 隐式读取 Workspace/Fleet 详情会跨域提权。修正为平台只读运营投影；租户详情必须显式 Membership 或有效 SupportGrant。

### 第二轮：恢复、顺序与跨 PRD 一致性

- P0：证书吊销只改数据库无法在断连 Agent 上生效。修正为短时 mTLS session、单调吊销水位、CRL/状态传播和每次重连重新校验。
- P0：配置、Release、License 各自 generation 会在离线导入时互相覆盖。修正为用途分域 generation 加 bundle version vector，冲突显式保留并人工决策。
- P0：Secret 轮换若先删旧版本会中断已接受请求。修正为 prepare -> test -> activate -> drain -> retire，旧版本有界保留且引用 CAS。
- P1：升级或恢复作业重启后重复执行会造成部分状态。修正为平台 Job lease/generation、Agent step receipt 和幂等补偿；所有阶段可从持久水位恢复。
- P1：批次末才发现 DTO/事件不一致会形成十一套协议。修正为从 PRD-120 起共享严格模型基类、owner、event envelope、签名和水位工具，每条仅扩展显式联合类型。

两轮自审均为修正后通过，无未关闭 P0/P1 设计问题。

## 8. G5 退出证据

十一条独立提交后必须统一证明：0111 -> 0122 fresh/逐级/down guard；设备私钥不出本地、mTLS 克隆/重放/吊销；签名快照和 LKG；遥测与云端/支持包敏感明文命中 0；安装、验收、Fleet 诊断、Canary 升级/回滚、License 离线宽限、真实恢复和断网包冲突闭环；SQLite/PostgreSQL、Windows/Linux、代理/断网、N/N-1；Chromium/Edge/Firefox 与 1/1.25/1.5 缩放；Excel SLO；开发库完整性/外键、outbox/queue 水位、子进程/监听/临时目录静止终态。任一证据缺失、跳过、失败或含敏感明文，G5 即失败。
