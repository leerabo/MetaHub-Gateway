# Platform Gateway API

PRD-101 以 PlatformSession 管理 Region/Cell 与 Workspace placement。OpenAI-compatible 数据面仍只接受
Virtual Key；客户端不得通过 Header、query 或正文指定 tenant、workspace、Region 或 Cell。

## 身份与权限

- `GET /platform/gateway-cells` 要求 `platform.gateway.read`。
- `PATCH /platform/workspaces/{workspace_id}/gateway-placement` 要求
  `platform.gateway.manage`、Platform CSRF Cookie/Header、强 `If-Match` 与 `Idempotency-Key`。
- PlatformSession、WorkspaceSession、SupportSession、Bearer 或 Virtual Key 混用均失败关闭。
- 响应和日志不包含 Provider、Key、签名密钥、内部地址、客户请求或 Prompt。

## 接口

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/platform/gateway-cells` | 无 query/body | Cell code、Region、容量等级、健康枚举、release/config 水位 |
| PATCH | `/platform/workspaces/{workspace_id}/gateway-placement` | 严格 JSON、`If-Match: "<revision>"`、幂等键 | 安全 placement、revision、snapshot generation、ETag |

placement JSON 只允许 `primary_region_code`、`primary_cell_code`、`failover_policy`、
`failover_cell_code`、`residency_policy_version`、`reason_code` 和 `confirmed=true`。服务端在同一事务重新验证
Workspace lifecycle、tenant owner、目标 Cell active/healthy、Region 驻留政策和 revision；placement、平台审计、
Cell event 与 config outbox 原子提交。

## 数据面交付

Control 使用独立 HMAC 域生成 tenant/workspace/Cell-bound 短期快照并写入 durable config outbox。Gateway 后台同步
全局最新 generation，先校验 payload hash、严格 schema、签名、owner、Cell 状态和有效期，再安装到进程本地缓存。
同 generation 只接受签发时间更新的续租；低 generation、篡改、错 owner 或过期制品不能替换最后有效快照。
Workspace 切换 Cell 后，旧 Cell 验证更新 generation 并立即撤销本地快照。

请求在读取正文前完成 placement 与 tenant/Workspace/Key/Cell 四层容量准入。租户、Workspace 或 Key 压力返回
`gateway.tenant_capacity_exhausted`（429）；Cell 压力返回 `gateway.cell_capacity_exhausted`（503）；缺失、过期、
签名错误或 owner 不符返回 `gateway.placement_unavailable`（503）。流式容量 lease 持有到流终态再释放。

## 配置

| 配置 | 约束 |
| --- | --- |
| `METAHUB_GATEWAY_PLACEMENT_ENABLED` | 默认 false；开启后强制签名 placement |
| `METAHUB_GATEWAY_CELL_CODE` | Gateway 开启时必须显式设置，不提供默认 Cell |
| `METAHUB_GATEWAY_PLACEMENT_SIGNING_VERSION` | 正整数 |
| `METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET` | 至少 32 bytes，生产必填且不得复用其他安全域；摘要始终 masked |
| `METAHUB_GATEWAY_PLACEMENT_SNAPSHOT_TTL_SECONDS` | 5..300 秒 |
| `METAHUB_GATEWAY_CAPACITY_*_LIMIT` | tenant/Workspace/Key/Cell 的部署拥有上限 |

## 固定错误

平台写入使用 `platform_gateway.request_invalid`、`workspace_not_found`、`cell_not_found`、
`residency_conflict`、`revision_conflict`、`idempotency_conflict` 和 `unavailable`。所有错误沿用统一
`code/message/suggestion/request_id/trace_id` 合同，底层数据库、签名、URL 或异常正文不得透传。
