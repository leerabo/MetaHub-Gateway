# PRD-101 多租户云 Gateway 池与区域路由

状态：设计完成，待编码
阶段：S3-云 Gateway
优先级：P0
依赖：PRD-096、PRD-098、PRD-100

## 1. 目标与边界

本条建立按 Region/Cell 分片的多租户 Gateway 池。OpenAI-compatible `/v1/chat/completions` 与
`/v1/embeddings` 继续只从 Virtual Key 恢复 tenant/workspace，不接受正文、Header 或 query 中的
workspace placement。Control 平台管理 Cell 与 WorkspacePlacement；数据面只消费签名、tenant-bound、
版本化配置快照。

首版不做无提示跨 Region 数据移动。Region 不可用且政策禁止跨区时返回 503；允许的显式灾备 placement
才能切换。过载通过租户/Workspace/Key/Cell 四层限流返回 429 或 503，不能让大租户占满共享 worker。

## 2. 0093 数据模型

- `gateway_cells`：region、cell code、capacity class、status、revision、release/config 水位。
- `workspace_gateway_placements`：workspace/tenant、primary region/cell、failover policy、residency policy
  version、revision；owner 复合外键。
- `gateway_capacity_leases`：cell、workspace、resource class、quota、owner/token/generation/expiry。
- `gateway_config_snapshot_leases`：cell worker、signed snapshot id/hash/key version、水位与 lease。
- `gateway_releases`：artifact digest、schema range、rollout state、health gate 与 immutable audit owner。
- `gateway_cell_events/outbox`：容量、过载、placement 和 release 的安全事件。

签名 payload 只含运行所需 ID、revision、route/policy/entitlement references 与有效期，不含 Provider key
明文；密钥由 KMS 信封按 cell/workspace 解封，缓存项绑定 snapshot hash 和 generation。

## 3. 请求路径

1. Virtual Key 验证得到 tenant/workspace/project/application owner。
2. 读取本地已验证 Placement snapshot；缺失、过期、签名错误或 owner 不符返回 503，禁止默认 Cell。
3. 校验 Workspace operational、Entitlement 和 residency revision。
4. 获取 tenant/Workspace/Key/Cell 容量令牌；租户配额耗尽返回 429，Cell 系统过载返回 503。
5. 执行既有策略、路由、预算、Quota、Provider、Usage/FinOps 链；placement 不改写其 owner。
6. 释放容量 lease，写有界指标；失败恢复不能重复 Usage 或商业 MeterReading。

chat 与 embeddings 共用身份/placement/容量外壳，各自保持 exact OpenAI 合同。流式请求在首字节前完成全部
准入；连接中途 placement 变化不迁移在途流，新请求读取新 generation。

## 4. 平台 API

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `GET /platform/gateway-cells` | platform_ops/release | Cell 容量、健康、版本安全摘要 |
| `PATCH /platform/workspaces/{workspace_id}/gateway-placement` | platform_ops | If-Match + 幂等更新 placement |

placement 写必须验证目标 Cell active、Region policy、容量和 Workspace lifecycle；写 placement、平台审计、
config outbox 原子提交。平台响应不含 Provider、Key、客户请求或内部地址。PlatformSession 不能调用 `/v1/*`，
Virtual Key 不能调用 `/platform/*`。

## 5. 发布、性能与观测

GatewayRelease 采用小流量 canary、健康门禁、generation fencing 和显式回滚；回滚只切换 artifact/snapshot，
不回退 Usage/账本。placement 传播目标 `<5s`；热路径新增本地快照查找 p95 `<2 ms`，容量准入 `<5 ms`。
指标为 cell/region/status/reason 的有界维度，不含 workspace 名、模型正文、Key 或 Prompt。

## 6. 专项测试

覆盖 0092->0093/fresh/down/up、签名/过期/错 tenant snapshot、Virtual Key owner、Region 禁止跨区、placement
CAS/幂等、1000 并发 noisy-neighbor、429/503 区分、流式 generation、Cell 故障、canary/rollback、chat 与
embeddings、平台/数据面凭据负矩阵、敏感日志和既有 Gateway 全链路回归。

## 7. 第一轮自审：租户与区域隔离

结论：通过，P0/P1 已修正。

- P0：接受客户端 workspace/region 会绕过 Virtual Key owner。修正为身份和 placement 全部服务端恢复。
- P0：默认跨区 failover 会违反数据驻留。修正为显式 policy/version，禁止时失败关闭。
- P0：共享全局并发池会造成 noisy-neighbor。修正为四层令牌与独立租户容量 lease。
- P1：placement 更新迁移在途 stream 会破坏响应一致性。修正为 generation 只影响新请求。

## 8. 第二轮自审：配置、发布与账本

结论：通过，P0/P1 已修正。

- P0：未签名配置或跨 tenant 缓存可泄露 Provider 凭据。修正为 tenant-bound 签名、KMS 与 generation key。
- P0：release rollback 回滚数据库账本会丢 Usage。修正为只切 artifact/config，不逆转持久业务事实。
- P1：Cell 过载与租户配额都返回同一错误不可运营。修正为 429 tenant pressure、503 cell unavailable。
- P1：平台状态 DTO 暴露内部地址。修正为固定 Cell code、capacity band 和健康枚举。

