# PRD-108 租户配置分发、缓存一致性与失效

状态：实现完成，专项验证通过（2026-08-23）。依赖 PRD-106/107，范围以 Excel PRD-108 为准；测试证据见 `docs/testing/PRD-108-tests.md`。

## Excel 权威合同

- API：`GET /internal/config-snapshots/{workspace_id}`、`POST /internal/config-snapshots/{id}/acks`、`POST /platform/config-rebuilds`、`GET /platform/config-distribution`。
- 核心对象：`ConfigSnapshot`、`ConfigDelta`、`ConfigAck`、`CacheWatermark`、`ConfigRebuildJob`。
- 事件：`config.snapshot_published`、`config.ack_lagging`、`config.rebuild_completed`。
- SLO：99% 配置变更在 5 秒内传播，撤权在 2 秒内关闭，Gateway 本地读取 p95 小于 2 ms。

## 1. 实现边界

配置快照只包含 Gateway 运行所需的最小投影：工作区状态、Cell placement、entitlement/policy 摘要、Provider credential 的不透明引用和 Feature capability。快照不包含 Provider Secret、Prompt、Response、客户正文或平台支持材料。

数据库 head 是分发真值，Redis 只能作为唤醒提示。发布在同一数据库事务中写入不可变 snapshot/delta、单调 head 和 outbox；重建通过 PRD-106 checkpoint job 执行，不在平台请求事务中读取并发布长任务。

## 2. 0100 数据与权限

- `config_snapshots`：envelope 加密 canonical manifest、内容 hash、KMS key version 和非对称签名。
- `config_snapshot_heads`：workspace/tenant owner、generation、snapshot、撤权水位和 revision，触发器禁止倒退。
- `config_deltas`：相邻 generation 的 from/to hash 链和加密 patch；非相邻更新必须 full rebuild。
- `config_acks`：已认证 Gateway identity、Cell、generation/hash、结果和 late disposition，不可变且幂等。
- `cache_watermarks`：observed/applied/LKG generation、撤权水位、stale reason 和单调 revision。
- `config_rebuild_jobs`：平台发起人、workspace owner、目标 generation、PRD-106 job 和结果 hash。
- `config_event_outbox`：只保存 owner、generation/hash、固定原因和 request/trace 元数据。
- 权限：`platform.config_distribution.read/rebuild`。owner/ops 可读写，security 只读，support 无权。
- down guard：存在 snapshot、delta、ACK、watermark、rebuild 或 outbox 事实时拒绝回滚。

## 3. 签名、owner 与 Gateway 协议

测试 KMS 使用由测试根密钥确定性派生的 Ed25519；AWS 使用独立 `config_distribution_signing_key_id` 和 `RSASSA_PSS_SHA_256`。签名覆盖 canonical manifest，Gateway 同时校验 wire schema、snapshot ID、workspace/tenant、Cell、generation、撤权水位、hash、key version 和签名。

内部读取必须同时满足 internal service token、Gateway identity、有效 `gateway_config_snapshot_lease`、活动 Cell 和 workspace placement。URL workspace 与 ACK body 都不是授权来源。GET 支持 `after_generation`、delta 和强 ETag/304；响应包含 ACK 所需的不可变 `snapshot_id`，不返回 Secret。

Gateway 将验证后的 full wire 原子保存为本地 LKG。进程重启后先重新验签，再恢复原始成功时间，不能通过反复重启延长 LKG。只有明确的传输不可达或 502/503/504 可进入有界 LKG；owner、hash、签名、schema、认证或 ACK 矛盾立即 fail closed 并丢弃本地 LKG。普通配置 LKG 上限 30 秒，安全敏感撤权上限 2 秒。

## 4. ACK 与重建

ACK 路径绑定已发 snapshot，body 只允许 exact generation、manifest hash 和 `applied|failed`。owner 从认证 Gateway 与 snapshot 恢复，不接受 Cell、tenant 或 workspace 声明。重复 ACK 返回原事实；迟到 ACK 只记审计，不能降低 applied/LKG/撤权水位。

平台重建要求 PlatformSession、同源、CSRF、独立 rebuild 权限和幂等键。请求只能指定 workspace、固定原因与确认位，不能提交 owner、模板或执行参数。worker 从当前 head 计算下一 generation；增量与重建使用同一 canonical manifest 构造和 hash 算法。

## 5. Console

Platform `config-distribution` 页面显示 workspace/Cell、head 与撤权水位、Gateway 数量、ACK lag、LKG/stale 数量、最近重建状态和结果 hash。页面不显示 manifest、Secret 引用、签名或内部拓扑。read 与 rebuild 权限独立裁剪；客户端只使用 Cookie、CSRF 和幂等键，不支持 Storage 或请求参数覆盖权限与 owner。

## 6. 已验证风险

- 跨 Cell、未知/重复 owner、凭据域混用和错误 ACK owner 均拒绝。
- generation、ACK 与撤权水位只增不减；乱序与重复不会回退本地或数据库 head。
- envelope、hash、signature、owner 或 wire 损坏立即关闭，不降级到旧快照。
- pub/sub 丢失不影响真值，下次轮询按数据库 head 对账。
- KMS、数据库和传输故障使用固定错误码，响应与日志不包含配置正文或敏感材料。
- Console 在窄屏使用带标签的行布局，权限漂移会清除重建意图并取消迟到请求。

批次级数据库、真实浏览器、性能和安全回归按 PRD-107 至 PRD-113 批次约定，在 PRD-113 完成后统一执行。
