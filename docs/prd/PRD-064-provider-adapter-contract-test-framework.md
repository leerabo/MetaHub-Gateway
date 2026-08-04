# PRD-064 - Provider Adapter 契约测试框架

状态：**已完成；编码前两轮自审、实现后两轮复审、自动化和实际运行验收均已通过**。

## 1. 权威来源与需求理解

### 1.1 PRD 身份

| 字段 | 权威值 |
| --- | --- |
| PRD_ID | `PRD-064` |
| 里程碑 | `M1-企业POC` |
| 优先级 | `P1` |
| 能力层 | 核心层 |
| 模块 | 契约测试 |
| 名称 | Provider Adapter 契约测试框架 |
| 角色 | 研发 |
| 页面 | `PAGE-091 CI测试报告` |
| 接口/动作 | `API-289 CI provider-adapter-contract-test` |
| 数据对象 | `OBJ-086 AdapterContractCase` |
| 事件 | `EVT-071 adapter.contract_failed` |
| 依赖 | Adapter 规范，即已完成的 PRD-063 |

上述信息由 Excel `01_PRD总表` 与 `02_M0-M1核心开发` 中的 PRD-064 行交叉确认，
并同时核对 `04_页面清单!PAGE-091`、`05_API接口清单!API-289`、
`06_数据对象!OBJ-086`、`07_Webhook事件!EVT-071` 和
`08_验收用例!TC-0127/TC-0128`。下一项来自 Excel 实际顺序，不是只凭编号推断。

### 1.2 用户目标和验收口径

当 Provider 协议、Adapter 实现或 PRD-063 冻结 spec 变化时，研发必须在合并前得到确定失败，
而不是等客户请求失败后才发现。框架必须同时证明：

1. 每个已注册 spec 都有显式提交的合同夹具，新增 spec/Adapter 不能被通用默认值自动放行；
2. 请求构造、非流式响应、流式响应、错误归一化和 usage 解析均有 mock 合同；
3. CI 使用独立命令 `provider-adapter-contract-test`，任一失败或覆盖缺口均以非零退出码阻止合并；
4. 可以显式执行真实 Gateway 探测，但 M1 不做常驻调度、全天候外网监控或后台巡检；
5. 结果可以按租户持久化并在 PAGE-091 查看，失败可触发 `adapter.contract_failed`；
6. 所有 HTTP 失败继续返回统一错误码和 `request_id/trace_id`，失败报告不得泄露 Key、Token、
   Prompt、Response、Endpoint 或上游自由错误正文。

### 1.3 产品和 Provider 边界

- 智谱、通义、文心、火山、混元、DeepSeek 官方或云服务仍是默认国内上游；
- 客户本地 OpenAI-compatible 与通用 Endpoint 是客户自管接入；
- `migration_compatible` 仅表示迁移兼容入口；OpenRouter、硅基流动不得成为默认模型厂家、
  默认推荐或真实探测默认目标；
- 离线 mock 门禁不访问公网，不依赖真实 Provider Key、Redis、数据库或客户 Endpoint；
- 显式真实探测只经 MetaHub Gateway 调用租户已经治理的模型别名，不让 CI 直接连接任意 Provider URL。

## 2. 架构约束和现状差距

系统架构第 6.3 节要求每个 Provider Adapter 必须具备请求参数兼容、错误码映射、流式响应和
Usage 字段测试；测试架构表也要求每次发布通过 Adapter 请求/响应/错误/usage 契约测试。

PRD-063 已提供：

- 九类不可变 `ProviderAdapterSpec`、`contract_case_id`、catalog version/hash；
- Gateway 工厂、鉴权、模型映射、Adapter 版本和目录 hash 的最小合同；
- 六类国内上游和两类客户 Endpoint 的请求构造测试；
- PAGE-090、租户激活事实及 `adapter.spec_updated`。

当前仍缺少：

1. 没有可复用 runner、稳定用例模型、报告 schema 和独立 CI 命令；
2. 现有 PRD-063 测试由 spec 自动参数化，新增 spec 可能自动复用旧断言，不能证明研发提交了新夹具；
3. 没有对每个 spec 的非流式 mock 响应、完整 SSE、401/429/5xx/timeout 和 usage 方言矩阵；
4. `scripts/ci/check.ps1` 只有全量 pytest，缺少验收标准点名的独立阻断步骤；
5. 没有租户级合同运行/用例事实、失败事件、只读 API 或 PAGE-091；
6. 没有显式、受限且默认关闭的真实 Gateway 探测入口。

## 3. 总体方案

```text
显式九类夹具清单 ──> Contract Runner ──> Mock loopback upstream
       │                    │                    │
       │                    ├─ request / non-stream / stream
       │                    ├─ error / usage
       │                    └─ coverage exact-set gate
       │
       ├─ CI PowerShell wrapper -> 非零退出阻止 check.ps1
       ├─ canonical JSON report -> 可选内部上报 API
       └─ 显式 Gateway probe -> 租户 Virtual Key -> Gateway -> 受治理 Provider

内部上报 API -> 0056 不可变 run/case 事实 -> adapter.contract_failed -> Webhook
                                               └-> 当前租户只读 API -> PAGE-091
```

方案分为两个明确层次：

- `merge_gate/mock`：默认 CI 门禁，必须覆盖当前目录全部 spec 和全部强制 case，不访问外部网络；
- `probe/gateway_probe`：人工或受保护 CI 显式启用，经 Gateway 检查真实链路，只覆盖配置中点名的
  Provider；其通过不能替代 merge gate，其失败会留下可追踪报告，但 M1 不增加定时调度器。

## 4. 契约框架设计

### 4.1 模块边界

新增 `gateway-data-plane/src/metahub_gateway/provider_contracts/`：

- `models.py`：冻结 `AdapterContractFixture/CaseResult/RunReport` 数据结构；
- `fixtures.py`：显式九类夹具注册表；
- `matrix.py`：从 spec 与夹具生成预期 case key，并执行 exact-set 覆盖门禁；
- `mock_server.py`：只监听 `127.0.0.1` 随机端口的进程内 mock upstream；
- `runner.py`：执行请求、非流式、SSE、错误和 usage 合同；
- `probe.py`：显式 Gateway probe，禁止直连任意 Provider Endpoint；
- `report.py`：canonical JSON、SHA-256 和安全摘要；
- `cli.py` / `__main__.py`：独立 CLI 与退出码。

框架运行生产 Adapter 的公开方法，不复制 Adapter 业务逻辑。mock server 只返回固定、小型、无敏感信息
的 JSON/SSE/错误夹具；监听地址固定 loopback、禁用代理、用例结束必须关闭 socket 和任务。

### 4.2 显式夹具和新增 Adapter 门禁

`CONTRACT_FIXTURES` 以 PRD-063 的 `contract_case_id` 为 key，九类 spec 必须逐条显式注册：

- 注册项固定 `provider_type/spec_id/adapter_name/adapter_version`；
- 声明成功 JSON、SSE chunk、usage 方言和固定错误 profile；
- 夹具正文只允许安全占位符，不含 URL、Authorization、Key、客户 Prompt 或真实响应；
- 注册表 key 集合必须与 `provider_adapter_specs()` 中的 `contract_case_id` 完全相等；
- Provider 类型、spec ID、Adapter 名称/版本任一漂移均在启动网络 mock 前失败；
- 新增 spec 即使复用已有 Adapter 类，也必须新增独立注册项和测试，否则 CI 返回非零。

禁止为未知 spec 自动生成默认夹具，因为那会绕过“新增 Adapter 必须提交测试”的核心验收标准。

夹具中的 `ci-contract` 只是证明离线样本不属于真实客户数据的固定测试身份，不能覆盖 CLI 显式传入的
目标租户。runner 生成报告时必须把已经校验的 `--tenant-id` 同时写入 run 和全部 case；默认值仍可为
`ci-contract` 以支持无 Control 的独立门禁，但配置内部上报或执行当前租户验收时必须使用目标租户。
这项区分使同一份代码合同可以按部署租户留存证据，同时不把租户差异误计入 exact-set case key。

### 4.3 强制 case 矩阵

每个 spec 至少生成以下稳定 case：

| case_kind | variant | 关键断言 |
| --- | --- | --- |
| `request` | 每个 `allowed_auth_type` | 模型映射、Header 精确性、none 不携带 Key、版本/hash、原 payload 不变 |
| `non_stream` | `success` | 实际 mock HTTP 200、标准响应、状态和 usage |
| `stream` | `success` | 按 spec 验证 SSE 顺序和资源幂等关闭；OpenAI family 要求 `[DONE]` 与 final usage，Generic family 禁止猜测私有终止标记，缺 usage 时显式估算 |
| `error` | `auth_401` | `gateway.upstream_auth_failed`，不回显正文 |
| `error` | `rate_limit_429` | `gateway.upstream_rate_limited` 与 retryable |
| `error` | `server_5xx` | 精确归一为当前冻结的 `gateway.upstream_error` |
| `error` | `timeout` | `gateway.upstream_timeout`，测试有界结束 |
| `usage` | `standard` | input/output/total/cache 与 provider cost 分离 |
| `usage` | `missing` | `estimated=true` 和固定 estimate reason |
| `usage` | `invalid` | 非负、有界、warning 去重，不保留敏感 raw 字段 |

case key 由 `contract_case_id + case_kind + variant + auth_type` 规范生成。merge gate 报告必须与
预期 key 集合完全相等，重复、缺失、多余、跳过或未知状态都按失败处理；不能用“测试数量大于某值”
代替精确覆盖。

### 4.4 报告 schema

报告是严格、版本化 JSON，不允许额外字段：

```json
{
  "schema_version": "adapter-contract-report-v1",
  "tenant_id": "ci-contract",
  "run_id": "acr_<32 lowercase hex>",
  "request_id": "req_<bounded safe text>",
  "trace_id": "trc_<bounded safe text>",
  "catalog_version": "adapter-spec-v1",
  "catalog_hash": "<64 lowercase hex>",
  "runner_version": "1.0.0",
  "suite_kind": "merge_gate",
  "mode": "mock",
  "source_revision_hash": "<64 lowercase hex>",
  "status": "passed",
  "coverage_complete": true,
  "total_cases": 92,
  "passed_cases": 92,
  "failed_cases": 0,
  "failure_codes": [],
  "started_at": "2026-08-01T00:00:00Z",
  "completed_at": "2026-08-01T00:00:01Z",
  "cases": []
}
```

当前 `adapter-spec-v1` 按本设计矩阵会得到 92 条；具体数量仍由冻结 spec 和允许鉴权组合计算，
实现中不得写死 92。每个 case 只包含：稳定 ID、tenant_id、
spec/provider/adapter 身份、kind/variant/auth、status、固定 failure_code、duration_ms、evidence_hash。
不保存 Header、URL、请求/响应正文或底层异常字符串。报告 canonical hash 覆盖完整语义，用于内部 API
幂等冲突判断。

### 4.5 CLI 和退出码

正式入口：

```powershell
.\scripts\ci\provider-adapter-contract-test.ps1 -Mode mock
```

底层入口：

```powershell
python -m metahub_gateway.provider_contracts.cli --mode mock --tenant-id ci-contract
```

退出码：

- `0`：merge gate 全部通过且报告原子写入成功；
- `1`：合同用例失败；
- `2`：CLI 参数、夹具、覆盖矩阵或报告结构无效；
- `3`：显式 probe 配置或认证缺失；
- `4`：配置了 Control 上报且安全上报失败。

CLI stdout 只打印固定计数、catalog identity、报告路径和失败 code 集合；stderr 不打印异常正文。
`source_revision_hash` 由相关 spec、Adapter、runner 和夹具文件的内容摘要规范组合得到，不依赖
`.git` 可见性，也不保存文件内容或绝对路径。runner 无论成功或失败都必须先生成并原子落盘报告；
配置上报时先尝试上报，再按原合同结果返回退出码，任何上报结果都不能把 failed 改成 passed。
报告先写同目录私有临时文件再原子替换，目标必须位于工作区 `.artifacts` 或调用方明确指定且经过
resolve 校验的工作区内目录。不得读取、枚举、写入或删除 `D:/MetaHub/.tmp`，不得使用 `C:/tmp`。
`--tenant-id` 必须是有界安全标识；runner 不得把夹具的 `ci-contract` 写回覆盖显式目标租户，内部上报
的 path、报告 run 和每条 case 必须使用同一 tenant_id。

### 4.6 CI 阻断

`scripts/ci/check.ps1` 在全量 pytest 之前调用独立 PowerShell wrapper。wrapper 使用同一个
`METAHUB_PYTHON`，并把报告写到 `.artifacts/provider-adapter-contract/report.json`。
`Invoke-CheckedNative` 保证非零退出立即阻止后续合并门禁。另加测试证明：

- 使用注入的临时注册表删除任一显式夹具时命令非零，测试不得修改正式源码文件；
- 使用内存伪 spec 新增目录项而未新增夹具时命令非零；
- case 失败、skip、重复或报告落盘失败时命令非零；
- `check.ps1` 确实包含独立命令，不能只依赖全量 pytest 间接覆盖。

## 5. 真实 Gateway 探测

真实探测必须显式使用 `--mode gateway_probe --probe-config <file>`，配置只允许：

- MetaHub Gateway 同源 URL；
- `tenant_id`；
- `provider_type -> model_alias` 映射；
- Virtual Key 的环境变量名称，而不是 Key 明文；
- 是否执行 stream 和安全超时上限。

约束：

1. Virtual Key 只从 `METAHUB_*VIRTUAL_KEY*` 进程环境变量读取、`repr=False`，不进入参数、
   报告或日志；Control 上报只允许既有 `METAHUB_INTERNAL_CONFIG_TOKEN`，不能借可配置变量名读取
   任意进程 Secret；
2. probe 只调用 Gateway OpenAI-compatible API，不能接受 Provider base URL 或 Provider Key；
3. 默认只允许 HTTPS；测试环境可显式允许 loopback HTTP，回环之外的明文 HTTP 拒绝；
4. redirect、跨 origin、URL userinfo、fragment、非标准协议和响应超限失败关闭；配置路径含
   `.tmp` 段时必须在 `resolve/stat/read` 前按词法拒绝；
5. probe 报告 `suite_kind=probe/mode=gateway_probe/coverage_complete=false`，不能冒充 merge gate；
6. M1 不增加计划任务、外部监控守护进程或全天候真实厂商调用；
7. 实际 PRD 验收使用本机 Gateway + 本机无日志 stub，覆盖两种 Adapter family、非流式、流式、
   429/5xx 和 usage，避免依赖公网及真实厂商费用。

其中配置文件中的 `gateway_url` 只是本次探测目标，必须与进程环境中独立下发的
`METAHUB_CONTRACT_TRUSTED_GATEWAY_ORIGIN` 规范化后完全同源；配置文件不能自行证明目标可信。
同理，Control 上报目标必须与 `METAHUB_CONTRACT_TRUSTED_CONTROL_ORIGIN` 完全同源，之后才允许读取并
发送 Virtual Key 或 `X-Internal-Token`。缺少受信 origin、目标不一致或任一 origin 不满足 HTTPS/
显式 loopback HTTP 边界时，都要在网络请求前以固定错误码失败关闭，防止误把 Provider Endpoint 或
攻击者 HTTPS 地址当成 MetaHub 服务并造成凭据外泄。

受信 origin 匹配后仍要先执行无凭据 `GET /health` 身份预检：Gateway 必须返回
`service_name=gateway-data-plane`，Control 必须返回 `service_name=control-plane-api`，且状态、媒体类型、
同源 URL、request/trace 响应头与正文全部一致；预检失败时不能发送 Authorization 或内部 Token。
health 通过后，runner 必须先调用
`POST /v1/adapter-contract-probes/identity`：该入口在后端认证 Virtual Key、核对唯一
`X-MetaHub-Contract-Tenant-ID` 声明，并回送同名证明头；缺失、错租户、重复头或非活动环境均在
报告生成前以退出码 3 失败，不能落盘或上报一个归属错误的普通失败报告。身份预检通过后，每次模型请求
仍携带同一租户声明并核对成功响应证明头。这样报告 tenant owner 不再只依赖 JSON 或环境声明，而由
真实 Gateway 鉴权上下文先行证明。request_id/trace_id 若含 Key/Token 全文或至少 16 字符片段，也必须
在 health 前拒绝，避免凭据借无认证健康日志外泄。

## 6. 数据表和字段设计

新增迁移 `0056_provider_adapter_contract_runs.sql` 及 down 文件。

### 6.1 `adapter_contract_runs`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `adapter_contract_run_id` | TEXT PK，`acr_` + 32 小写 hex | 不可变运行身份 |
| `tenant_id` | TEXT NOT NULL FK tenants | 所有报告按租户隔离 |
| `source_request_id` | TEXT NOT NULL | 内部上报幂等键 |
| `source_trace_id` | TEXT NOT NULL | 首次上报链路 |
| `schema_version` | TEXT NOT NULL | 固定 report v1 |
| `catalog_version/hash` | TEXT NOT NULL | 必须与租户 active catalog 一致 |
| `runner_version` | TEXT NOT NULL | 合同 runner 版本 |
| `suite_kind` | CHECK `merge_gate/probe` | 报告语义 |
| `mode` | CHECK `mock/gateway_probe` | 执行方式 |
| `source_revision_hash` | 64 小写 hex | 不保存 branch/commit 原文 |
| `status` | CHECK `passed/failed` | 不允许 running/unknown 假完成 |
| `coverage_complete` | INTEGER 0/1 | merge gate 必须为 1 |
| `total/passed/failed_cases` | INTEGER 有界 | 三者严格相等关系 |
| `failure_codes_json` | canonical JSON array | 只保存固定错误码集合 |
| `report_hash` | 64 小写 hex | 完整 canonical 报告摘要 |
| `started_at/completed_at` | canonical UTC 秒 | 完成不得早于开始或位于未来 |
| `created_at/updated_at` | canonical UTC 秒 | 不可变事实中二者必须相等 |

唯一约束：`(tenant_id, source_request_id)`、`(tenant_id, adapter_contract_run_id)`；索引覆盖
`tenant_id + completed_at` 和 `tenant_id + status + completed_at`。表级/触发器校验 merge gate 只能
为 `mock + coverage_complete=1`，probe 不能被标记 coverage complete。

### 6.2 `adapter_contract_cases`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `adapter_contract_case_id` | TEXT PK，`acc_` + 32 小写 hex | 由 run + case key 稳定派生 |
| `tenant_id` | TEXT NOT NULL | 必须与父 run 相同 |
| `adapter_contract_run_id` | TEXT NOT NULL | 复合 FK 父 run |
| `contract_case_key` | TEXT NOT NULL | 有界稳定 key |
| `spec_id/provider_type` | TEXT NOT NULL | 必须属于提交 catalog |
| `adapter_name/version` | TEXT NOT NULL | 执行制品身份 |
| `case_kind/variant/auth_type` | TEXT/nullable | 白名单合同维度 |
| `status` | CHECK `passed/failed` | 不持久化 skip |
| `failure_code` | nullable safe code | 通过必须为空，失败必须为白名单 code |
| `duration_ms` | INTEGER 0..300000 | 有界耗时 |
| `evidence_hash` | 64 小写 hex | 安全断言证据，不保存正文 |
| `created_at/updated_at` | canonical UTC 秒 | 二者相等，且不得早于父 run 完成时间 |

唯一约束：`(tenant_id, adapter_contract_run_id, contract_case_key)`。复合 FK 和 owner trigger 双重阻止
跨租户 case 注入。run/case 均为不可变事实，UPDATE/DELETE trigger 全部阻断；down 在已有任何 run、
case、订阅或投递事实时必须在 DDL 前失败，禁止静默丢失 CI 证据。

## 7. API 设计

### 7.1 内部上报

`POST /internal/tenants/{tenant_id}/adapter-contract-runs`

- 身份：仅沿用仓库统一的 `X-Internal-Token`，使用常量时间比较并拒绝 Bearer 混用；生产环境未配置
  token 时失败关闭；认证必须在读取或缓冲 body 前完成，匿名大 body 不能消耗解析资源；
- `fastapi_app` 的链路 ID 凭据碰撞扫描必须把新 internal 路径及 `X-Internal-Token` 纳入
  `sensitive_values`，防止 token 片段借 `X-Request-ID/X-Trace-ID` 回显或落日志；
- body：严格 JSON、最多 512 KiB、最多 2048 cases、深度和节点数有界、拒绝重复 key/非有限数；
- `tenant_id` 只取可信 path，body tenant 必须精确相等且不能覆盖；
- report 的 request_id/trace_id 必须与本次 HTTP 头经过中间件归一后的 request/trace 精确相等，
  数据库和事件只保存这组可信链路；调用方需要幂等 replay 时必须复用相同请求头；
- catalog version/hash 必须与该租户 active PRD-063 activation 和当前进程制品同时一致；
- merge gate 必须通过 exact-set 验证；probe 只允许配置中的 subset，不能伪造 coverage；
- 新 request ID + 新 run 在单事务写入 run/cases；失败 run 同事务写入可恢复 outbox/source 事实，
  事务提交后才允许投递，事件构造或 source 写入失败必须整体回滚；
- 相同 request ID + 相同 report hash 返回原事实并标记 replay，不重复事件；
- 相同 request ID + 不同 hash 或相同 run ID + 不同事实返回 409；
- 响应统一携带 `request_id/trace_id`，设置 `Cache-Control: no-store`。

### 7.2 当前租户只读 API

`GET /adapter-contract-runs?status=&suite_kind=&mode=&limit=&cursor=`

`GET /adapter-contract-runs/{adapter_contract_run_id}`

- 后端权限统一复用 `provider.health.read`：admin/developer/ops 中实际持有该权限者可读，viewer/匿名拒绝；
- 权限根据请求期 RBAC revision 实时计算，前端隐藏导航不能代替后端校验；
- tenant 只取 session，拒绝 `tenant_id` query、重复参数和未知参数；
- 详情只读取同租户复合 owner，跨租户 ID 固定返回 404；
- 列表只返回运行摘要，详情返回有界 cases；不返回报告原文件或任何请求/响应正文；
- 所有响应 no-store，并携带 `request_id/trace_id`。

## 8. 页面和交互设计

PAGE-091 导航 ID 为 `adapter-contract-runs`，只有后端 navigation 返回该项且当前会话拥有
`provider.health.read` 时显示。页面不提供“运行真实探测”按钮，避免浏览器成为任意网络调用入口。

页面布局：

1. 顶部紧凑摘要：最新 merge gate 状态、catalog version/hash 短摘要、完成时间、通过/失败计数；
2. 分段控件：`合并门禁` / `真实探测`；
3. 状态、模式筛选和刷新命令；
4. 运行表：状态、suite/mode、catalog、case 计数、完成时间、request_id；
5. 展开详情：按 Provider 分组展示 kind/variant/auth/status/failure code/duration；
6. 空状态、加载、API 失败、权限撤销和 stale response 均使用独立状态。

安全和响应式约束：

- 动态文本/属性全部 HTML 转义，不使用 `innerHTML` 拼接不可信数据；
- tenant/session/RBAC revision/permission fingerprint/筛选/导航/generation 变化时淘汰旧响应；
- 桌面 1280x720 保持摘要与表格可扫描，移动 390x844 转为单列定义列表；
- 稳定列宽和 `overflow-wrap` 防止最长 request ID、hash、错误码撑破容器；
- 页面、URL、Storage 和浏览器日志不保存 Token、Key 或合同正文。

## 9. 错误码设计

| 错误码 | HTTP | 使用边界 |
| --- | ---: | --- |
| `adapter.contract_report_invalid` | 422 | 严格 JSON、字段、计数、case 或安全门禁失败 |
| `adapter.contract_catalog_mismatch` | 409 | 报告目录与租户 active/当前制品不一致 |
| `adapter.contract_run_conflict` | 409 | request ID/run ID 与既有不可变事实冲突 |
| `adapter.contract_run_not_found` | 404 | 当前租户不可见或不存在 |
| `adapter.contract_storage_unavailable` | 503 | 数据库写入/读取不可用的安全归一 |
| `auth.internal_token_required` | 401 | 内部上报缺 token |
| `auth.internal_token_invalid` | 403 | token 错误或 Bearer 混用 |
| `auth.permission_denied` | 403 | 当前用户缺少后端只读权限 |

CLI/报告 failure code 仅允许固定集合，例如：
`adapter.contract_fixture_missing`、`adapter.contract_fixture_invalid`、
`adapter.contract_coverage_mismatch`、`adapter.contract_request_failed`、
`adapter.contract_non_stream_failed`、`adapter.contract_stream_failed`、
`adapter.contract_error_mapping_failed`、`adapter.contract_usage_failed`、
`adapter.contract_probe_failed`。错误响应和 CLI 文本不回显触发值。

## 10. 事件设计

`adapter.contract_failed` 只在新持久化的 failed run 上产生；通过、幂等 replay、列表访问和页面刷新
均不产生事件。exact payload：

```json
{
  "run_id": "acr_<32 lowercase hex>",
  "catalog_version": "adapter-spec-v1",
  "catalog_hash": "<64 lowercase hex>",
  "suite_kind": "merge_gate",
  "mode": "mock",
  "total_cases": 92,
  "failed_cases": 1,
  "failure_codes": ["adapter.contract_usage_failed"],
  "completed_at": "2026-08-01T00:00:01Z"
}
```

- envelope：持久 `event_id`、tenant_id、timestamp、固定 actor `adapter-contract-runner`、
  object_id=run ID、首次 trace/source request；
- payload 拒绝额外字段、未来时间、非 canonical hash、未知 code、重复/乱序 failure codes 和计数漂移；
- 不包含 case 断言正文、Endpoint、Header、Key、Prompt、Response 或厂商错误原文；
- Webhook pending/dead-letter 恢复必须从不可变 run 事实重建相同 event ID、timestamp、trace 和 payload；
- 0056 down guard 统计订阅、delivery、attempt 和 dead-letter，存在事实时拒绝回滚。

## 11. 安全、权限、租户和兼容性

1. 所有持久 run/case 都携带 tenant_id，所有 SQL 以 tenant_id 为首条件；
2. 内部上报和用户读取使用完全分离的认证入口，不接受客户端自报角色或租户；
3. 真实 probe 只经 Gateway，避免 CI 绕过 Virtual Key、预算、限流、DLP、路由和 Usage 追踪；
4. mock 使用固定假 Key 且只在内存中构造 Header，日志捕获和报告扫描必须证明未泄露；
5. runner 禁用系统代理，loopback server 不监听 `0.0.0.0`，不跟随 redirect；
6. JSON/响应/SSE/错误正文均有字节、case 数、时间和深度上限；
7. 失败 code、evidence hash 和 source revision hash 都不可逆，不能保存原异常或源码环境文本；
8. OpenRouter/硅基流动只允许作为 `migration_compatible` 的固定示例，不成为真实探测默认目标；
9. 现有 Adapter 公共接口和 PRD-063 catalog identity 保持兼容；若为了测试抽取 SSE collector，只做
   行为不变的公开封装并保留现有调用测试；
10. CI 上报是可选副作用，合同失败始终优先返回失败；配置了强制上报时，上报失败也不能被吞掉；
11. 无新权限码，复用已经审计的 `provider.health.read`，避免 PRD-064 意外扩大角色权限；
12. 代码模块、类、数据结构、常量、字段和函数补详细中文注释，重点解释覆盖 exact-set、事务、
    tenant owner、失败关闭、报告脱敏和网络边界为什么存在。

## 12. 测试计划

### 12.1 Shared/Gateway 单元合同

- 九类显式夹具与 spec exact-set、版本/hash、Adapter family 一致；
- 新增伪 spec/删除夹具/重复 case/skip/unknown status 均失败；
- 每个允许 auth 的请求 Header、模型、payload 不变性；
- 九类非流式 success、按各自 stream profile 的 SSE 终止/usage 合同和幂等资源清理；
- 401/429/5xx/timeout 固定归一且不泄露正文；
- usage standard/missing/invalid、多方言、成本分离和 raw usage 脱敏；
- report canonical hash、额外字段、控制字符、非有限数、计数漂移、未来时间和大小上限；
- CLI 退出码、原子文件、无密钥输出、工作区路径限制。

### 12.2 Control API / 权限 / 租户

- internal token 缺失/错误/Bearer 混用、未知 tenant、oversize、duplicate key；
- active catalog mismatch、覆盖缺口、case 多余/重复、计数/状态漂移；
- 新失败 run 原子写 run+cases+event，事件构建或 enqueue 失败整事务回滚；
- 相同 request ID 精确 replay、不同 hash 冲突、run ID 跨租户冲突；
- developer/admin/ops 中实际有权限者 200，viewer/匿名 403/401；
- tenant query/重复/未知参数拒绝，跨租户 run 404；
- DB 故障统一 503，响应含 request_id/trace_id/no-store；
- Webhook 成功、pending、dead-letter 恢复保留首次 source request/trace。

### 12.3 迁移

- fresh 0056、重复执行、0055 -> 0056、空事实 down/re-up；
- owner/复合 FK/唯一性/count/status/immutable trigger；
- run/case、订阅、delivery、attempt 或 dead-letter 任一存在时 down 在 DDL 前阻断；
- `foreign_key_check=[]`、`integrity_check=ok`、schema_migrations 连续且 checksum 一致。

### 12.4 Console

- navigation owner/权限可见性、列表/详情/筛选、空/加载/失败；
- tenant/session/revision/fingerprint/navigation/generation 变化淘汰 stale response；
- 所有自由字段 XSS、超长 ID/hash/error code、无敏感字段进入 DOM/Storage；
- Provider 状态页和 Adapter spec 页生命周期不回归；
- 桌面 1280x720、移动 390x844 无重叠和横向溢出。

### 12.5 CI、实际运行和隐私

- 独立 wrapper 成功；故意删除夹具副本时非零，证明阻止合并；
- `check.ps1` 专项、相关 Shared/Gateway/Control/Console 回归和全量回归；
- 实际开发库应用 0056，三服务健康 HTTP 200；
- 本机无日志 stub 下 mock runner 和 Gateway probe 覆盖两种 Adapter family、SSE、429 和 usage；
- 内部上报后 PAGE-091 当前租户可见，viewer 后端 403，第二租户不可见；
- 扫描进程日志、报告、数据库和浏览器证据，不得出现 canary Key/Token/Prompt/Response/Endpoint；
- 运行 `scripts/ci/check-comments.py`，注释门禁保持通过；
- 所有测试 TEMP/TMP/basetemp 和截图只放 `D:/MetaHub/.artifacts/prd064-*`，不提权。

## 13. 代码修改计划

1. 新增 Gateway `provider_contracts` 模块、显式夹具、mock server、runner、probe、报告和 CLI；
2. 必要时把现有 SSE usage collector 抽为行为不变的可复用内部组件，并补原调用回归；
3. 新增 `scripts/ci/provider-adapter-contract-test.ps1`，接入 `scripts/ci/check.ps1`；
4. 新增 Control `adapter_contracts` models/service/router，挂载 app，补 no-store/error code，并更新
   `fastapi_app` 对新 internal 路径的 token/链路 ID 碰撞防护；
5. 扩展 Shared webhook 事件目录、严格 payload/envelope validator 和持久恢复；
6. 新增 0056 up/down 迁移及迁移测试；
7. 新增 PAGE-091 API client、状态 owner、渲染、导航和响应式样式；
8. 补 Shared/Gateway/Control/Console/CI/迁移专项与受影响回归；
9. 完成实际运行、浏览器双视口、隐私扫描和 `docs/testing/PRD-064-tests.md`；
10. 全部门禁通过后才更新 README、目录说明和本设计状态，并按 Excel 确认下一条未完成 PRD。

## 14. 两轮编码前自审

### 14.1 第一轮：需求、租户、权限与事务边界

结论：**修正后通过**。

检查并修正：

1. 对照 `OBJ-086` 的通用字段基线，为 run/case 都补齐 `updated_at`；由于二者是不可变事实，
   `updated_at` 必须与 `created_at` 相等，不能暗示支持修改历史报告；
2. 明确内部 token 必须在读取 body 前常量时间校验，防止匿名超大报告先消耗内存再被拒绝；
3. 把报告 request_id/trace_id 与中间件归一后的 HTTP 链路精确绑定，避免调用方提交一套报告身份、
   数据库和事件又记录另一套身份；
4. 明确 failed run、cases 与可恢复事件 source/outbox 同事务提交，事件构造/source 失败不能留下
   “页面显示失败但 Webhook 永远不存在”的半完成状态；
5. 复核当前租户 SQL 首条件、跨租户 404、复用 `provider.health.read` 的后端实时授权、viewer/匿名
   拒绝、body tenant 不可信、merge gate/probe 不可混淆，未发现剩余缺口。
6. 实现交叉检查发现离线夹具身份与持久化目标租户曾被混为一体；已明确 `ci-contract` 只约束样本来源，
   runner 必须用显式目标 tenant 重写 run/case 身份。否则默认租户无法完成“内部上报后 PAGE-091 当前
   租户可见”的验收，且 CLI 的 `--tenant-id` 会成为无效参数。修正后重新检查了 case key 稳定性、
   body/path tenant 一致性和跨租户隔离，三者不冲突。

### 14.2 第二轮：兼容性、CI 真实性与可运行性

结论：**修正后通过，可以进入编码**。

检查并修正：

1. 按九类 spec、11 个允许鉴权组合和每 spec 九个非 request case 实际推导当前 merge gate 为
   92 条，修正了示例中的错误数量；实现和数据库仍必须动态 exact-set 校验，不能写死 92；
2. source revision 改为相关正式文件内容的 canonical 组合摘要，不依赖当前沙箱不可见或 CI shallow
   checkout 不完整的 `.git`，同时不把路径/源码正文放入报告；
3. 明确 failed runner 也必须先原子落报告并尝试已配置的内部上报，最后才返回非零；上报成功不能
   把失败合同改成成功，上报失败也不能吞掉原合同失败；
4. 缺夹具和新增 spec 的负例使用注入注册表/内存 spec，不允许测试临时修改正式源码或污染其他并发测试；
5. 非流式和 SSE success 使用自包含 loopback mock 运行真实 Adapter；OpenAI stream 精确要求
   final usage 与 `[DONE]`，Generic stream 则验证 `final_usage_expected=false` 和无私有终止标记，
   不能把同一 OpenAI 断言强加给通用协议；401/429/5xx 同样经 loopback 调用生产
   `send_non_stream`，并固定 `database_url=None/provider_credential_id=None` 避免无关数据库副作用，
   从而覆盖有界错误正文、`_raise_adapter_error`、安全日志和统一 ApiError；只有 timeout 使用固定
   `httpx` 异常做有界归一测试；
6. 复核 PowerShell 从仓库外 cwd 启动、`METAHUB_PYTHON/PYTHONPATH`、无数据库/Redis/外网条件、
   报告路径并发边界、Windows socket 关闭、现有 Adapter 公共接口和 PRD-063 catalog 兼容性；
7. 交叉复核修正内部头名为仓库既有 `X-Internal-Token`，并要求 middleware 对新 internal 路径执行
   token 与 request/trace ID 碰撞扫描；
8. 交叉复核冻结 5xx 精确错误码为 `gateway.upstream_error`，禁止宽松二选一。
9. 编码期安全复核发现“任意合法 HTTPS URL”仍不足以证明目标是 MetaHub Gateway/Control；设计补充
   独立受信 origin 精确绑定。探测配置和 CLI 参数只能选择已经由部署环境授权的同源目标，不能由同一
   JSON/参数自证；匹配完成前不得读取或发送 Virtual Key/内部 Token，并补目标漂移、缺失受信配置与
   5xx 脱敏失败报告测试。
10. 第三轮安全复核补充无凭据 health 身份预检及 Gateway 鉴权态租户证明。仅检查受信 URL/tenant 环境
    仍不能证明端口后实际服务身份和 Virtual Key owner；因此 health 必须在凭据请求前通过，模型请求的
    租户声明必须在 `authenticate_virtual_key` 后端校验并由成功响应证明，失败沿用统一固定鉴权错误。
11. 租户证明 Header 必须保持单值合同：请求未携带时维持普通 OpenAI-compatible 行为，恰好一个值时
    才与鉴权 tenant 比较，重复行或逗号合并值都固定拒绝；probe 对重复响应证明同样失败。拒绝路径不得
    把攻击者声明值写入响应、request_logs、结构化日志或报告，只保留真实鉴权 tenant 与固定错误码。

完成上述修订后未发现剩余设计阻断项，可以进入编码。

## 15. 实现后两轮复审

### 15.1 第一轮：权限撤销、故障降级与日志隐私

结论：**发现阻断项，修正并定向验证后通过**。

1. PAGE-091 列表 403 原先可能保留上一次已授权页面，导致权限撤销后旧表格仍停留在 DOM。现已在
   `rejectList()` 中清除 page、summary、detail、cursor、选中项和当前 operation，只保留统一公开错误；
   状态固定为 `error`，页面不再展示表格或命令。
2. 详情请求 403 原先只清详情，仍可能保留受保护列表。详情 403 现复用同一完整权限清理；503 则保留
   已经 DTO 白名单净化的历史页并进入 `partial`，避免把暂时依赖故障误当成权限撤销。
3. 浏览器验收器同步冻结上述差异：403 必须无受保护 DOM；503 必须显示统一错误和本次 request_id，
   同时允许扫描安全历史表格和只读命令；后续 200 必须恢复正常状态。
4. 登录成功日志曾携带邮箱，失败登录还可能记录邮箱或攻击者输入。成功路径已移除邮箱；失败路径只在
   已知账号时记录安全 `actor_id`，未知账号不记录攻击者提交值。专项 `2/2` 通过。
5. PAGE-091 状态专项最终 `15/15`，浏览器合同 `10/10`；真实 Edge 的 403、503、恢复、注销、双视口和
   隐私扫描均通过。

### 15.2 第二轮：回归真实性、迁移一致性与沙箱边界

结论：**修正后通过；保留测试事实边界，不虚写全量零失败**。

1. 根级 Python 回归只执行一次，一小时超时前到 `1008/1845` 且已有 12 个失败；随后按失败文件定向
   复测 `37/37`，并从未覆盖位置执行 847 项后缀回归。后缀 10 个失败逐项归因为 PRD-056 旧迁移头、
   Feedback SQLite 竞争、`gatewayctl` 冷启动预算和 5 个越过用户临时目录边界的 PRD-050 旧证据测试。
2. PRD-056 的全局迁移头已从 `0049` 更新为 `0056`，测试工件只使用 `.artifacts`；fresh migration
   节点 `1/1` 通过。开发库最终只读复核为 `56/0056`、`integrity_check=ok`、外键违规 `0`。
3. Feedback 保持首次 250ms 写锁预算；仅对 SQLite 原生 BUSY/LOCKED 关闭旧连接后，以独立
   `query_only` 连接有界观察并发胜者，按 operation/source 重分类为 replay、
   `feedback.request_conflict` 或 `feedback.already_exists`；持续外部锁仍返回
   `feedback.unavailable`。四个并发/持久锁节点 `4/4` 通过。
4. `gatewayctl` 两个真实入口只放宽测试进程冷启动预算到 60 秒，不改变 CLI 网络超时或失败关闭合同；
   定向 `2/2` 通过。
5. 5 个 PRD-050 旧静态证据测试硬编码依赖用户明确禁止访问的临时目录，且正式仓库没有可信成功副本；
   本轮不访问、不运行、不算通过，也不以失败制品替代证据。该排除边界已写入测试记录。
6. Console 全量实际为 `555 passed / 1 failed`，旧 PAGE-072 定位断言修正后目标文件 `18/18`；Node
   示例 `33/33`、Console lint `175 files`、中文注释门禁通过。没有声称重新取得一次 Console 或 Python
   根级全绿。
7. Gateway、Control、Console 最终健康检查为 `200/200/200`；Control 实际 RBAC/租户/Outbox、Gateway
   probe 和 Edge runtime 证据 SHA-256 均已结构化复核，具体路径与摘要见
   `docs/testing/PRD-064-tests.md`。

完成上述两轮实现后复审后，未发现 PRD-064 范围内剩余阻断项。PRD-064 可以标记完成；下一项必须继续
按 Excel 优先级识别，且 `all_108_prds_complete=false`。
