# Provider Adapter 契约运行 API

本文记录 PRD-064 的稳定接口边界。契约运行只保存测试身份、计数、固定错误码和不可逆摘要，
不保存 Provider Key、Authorization、Endpoint、Prompt、Response 或底层异常正文。

状态：实现与自动化专项已经落地，PRD-064 仍处于最终运行验收收口阶段，**尚未完成**。设计与安全自审见
[PRD-064 设计](../prd/PRD-064-provider-adapter-contract-test-framework.md)，实际通过项和剩余
门禁只以 [PRD-064 测试记录](../testing/PRD-064-tests.md) 为准。

## 1. 身份与链路

- 用户读取接口只接受登录 Session，并在后端实时校验 `provider.health.read`；前端导航不可代替权限。
- 内部上报只接受 `X-Internal-Token`，缺失返回 `auth.internal_token_required`，错误或同时携带
  Bearer 身份返回 `auth.internal_token_invalid`。
- 内部 Token 必须在读取正文前校验。上报 path 中的 `tenant_id` 是唯一可信租户；body 中的
  tenant 只能与其精确一致，不能覆盖。
- 上报报告的 `request_id`、`trace_id` 必须与中间件确认的 `X-Request-ID`、`X-Trace-ID`
  精确一致。成功与错误响应都返回同一组链路 ID，并设置 `Cache-Control: no-store`。
- 客户端只能从 `METAHUB_INTERNAL_CONFIG_TOKEN` 读取内部上报 Token；目标必须匹配独立受信
  Control origin，并在发送 Token 前通过无凭据 `/health` 服务身份预检。链路 ID 含 Token
  全文或至少 16 字符片段时必须在 health 前拒绝。

## 2. 内部上报

```http
POST /internal/tenants/{tenant_id}/adapter-contract-runs
Content-Type: application/json
X-Internal-Token: <service token>
X-Request-ID: req_<bounded safe id>
X-Trace-ID: trc_<bounded safe id>
```

正文为 `adapter-contract-report-v1` 严格 JSON：拒绝额外字段、重复 key、NaN/Infinity、非法
UTF-8、超过 512 KiB、超过 2048 cases、过深或节点过多的对象。主要字段如下：

| 字段 | 约束 |
| --- | --- |
| `tenant_id` | 与 path tenant 精确相等 |
| `run_id` | `acr_` 加 32 位小写十六进制 |
| `request_id/trace_id` | 与本次 HTTP 链路精确相等 |
| `catalog_version/hash` | 与租户 active PRD-063 目录及当前制品一致 |
| `suite_kind/mode` | `merge_gate/mock` 或 `probe/gateway_probe` |
| `coverage_complete` | merge gate 必须为 `true`，probe 必须为 `false` |
| `status` | 仅 `passed/failed` |
| `total/passed/failed_cases` | 非负、有界且计数恒等 |
| `failure_codes` | 去重、排序的固定错误码；与失败 case 精确一致 |
| `source_revision_hash` | 64 位小写 SHA-256，不提交分支名、路径或源码 |
| `started_at/completed_at` | canonical UTC 秒，完成时间不得早于开始时间 |
| `cases` | merge gate 与当前 spec 强制矩阵 exact-set；probe 只能是受控 subset |

同一 `(tenant_id, request_id)` 与相同 canonical report hash 是幂等 replay，不重复创建 run、case
或失败事件；同 request 不同报告，以及同 run ID 不同事实，返回
`adapter.contract_run_conflict`。只有首次写入的 failed run 产生一次
`adapter.contract_failed`。

## 3. 当前租户列表

```http
GET /adapter-contract-runs?status=failed&suite_kind=merge_gate&mode=mock&limit=50&cursor=...
Authorization: Bearer <session token>
```

只允许 `status`、`suite_kind`、`mode`、`limit`、`cursor` 五种 query；未知、重复、控制字符或
`tenant_id` query 均返回 `adapter.contract_report_invalid`。查询始终以 Session tenant 为首条件，
按完成时间和 run ID 稳定倒序分页。列表只返回运行摘要，不返回 cases 或原报告文件。

## 4. 当前租户详情

```http
GET /adapter-contract-runs/{adapter_contract_run_id}
Authorization: Bearer <session token>
```

详情返回运行摘要和有界 case 列表。SQL 使用 `(tenant_id, run_id)` 复合归属；不存在与跨租户 ID
统一返回 `adapter.contract_run_not_found`，避免资源归属枚举。case 只包含 spec/provider/adapter
身份、kind、variant、auth、状态、固定 failure code、耗时和 evidence hash。

## 5. Gateway 探测身份预检

真实 probe 在任何模型 case 前按以下顺序执行：

```http
GET /health
X-Request-ID: req_<bounded safe id>
X-Trace-ID: trc_<bounded safe id>
```

该请求不携带 Authorization；必须严格确认同源、HTTP 200、JSON、
`service_name=gateway-data-plane` 及请求/响应链路 ID 一致。随后才允许发送 Virtual Key：

```http
POST /v1/adapter-contract-probes/identity
Authorization: Bearer <tenant virtual key>
X-MetaHub-Contract-Tenant-ID: <expected tenant>
X-Request-ID: req_<derived safe id>
X-Trace-ID: trc_<bounded safe id>
```

Gateway 从 Virtual Key 后端鉴权结果恢复真实 tenant，并要求租户声明头恰好出现一次且精确匹配。
成功返回 `status=ok`、`service_name=gateway-data-plane`、链路 ID 和同名租户证明响应头；正文不返回
tenant。缺失、错租户或重复声明统一返回 `gateway.auth_invalid`，所有状态均 `Cache-Control: no-store`。
身份预检失败映射 CLI 退出码 3，不能生成、落盘或上报错误归属的 probe 报告。

## 6. CLI 与 CI

正式合并门禁：

```powershell
.\scripts\ci\provider-adapter-contract-test.ps1 -Mode mock -TenantId default
```

底层入口：

```powershell
python -m metahub_gateway.provider_contracts.cli --mode mock --tenant-id default
```

离线 mock 不访问公网、数据库或真实 Provider。显式真实探测只允许经 MetaHub Gateway，不允许
CI 直连任意 Provider URL。退出码为：`0` 全部通过、`1` 用例失败、`2` 合同/报告无效、`3`
probe 配置或认证缺失、`4` 已配置的 Control 上报失败。失败报告也必须先原子写入工作区
`.artifacts/provider-adapter-contract/`，再返回非零。

真实 probe 的配置路径若含 `.tmp` 段，必须在任何路径解析或文件访问前拒绝；Virtual Key 环境变量名
必须位于 `METAHUB_*VIRTUAL_KEY*` 命名空间。request_id/trace_id 含 Key/Token 全文或至少 16 字符
片段时同样在首个 health 前拒绝。

## 7. 统一错误码

| code | HTTP | 含义 |
| --- | ---: | --- |
| `adapter.contract_report_invalid` | 422 | JSON、字段、计数、case 或 query 不满足严格合同 |
| `adapter.contract_catalog_mismatch` | 409 | 报告目录与租户 active/当前制品不一致 |
| `adapter.contract_run_conflict` | 409 | request/run 身份与既有不可变事实冲突 |
| `adapter.contract_run_not_found` | 404 | 当前租户下不存在该 run |
| `adapter.contract_storage_unavailable` | 503 | 持久化或读取暂不可用 |
| `auth.internal_token_required` | 401 | 内部上报缺少服务 Token |
| `auth.internal_token_invalid` | 403 | 服务 Token 错误或身份混用 |
| `auth.permission_denied` | 403 | Session 缺少后端读取权限 |

## 8. Webhook

`adapter.contract_failed` 只包含 run ID、catalog version/hash、suite/mode、total/failed 计数、
排序后的 failure code 集合和完成时间。信封固定 actor 为 `adapter-contract-runner`，object ID 为
run ID，并保留首次 source request/trace。恢复投递必须从不可变 run 事实重建相同事件，不能从
调用方重传正文或当前 worker 链路猜测。
