# PRD-056 - Provider错误归一化规则库技术设计

## 文档状态

- PRD：`PRD-056`
- 名称：`Provider错误归一化规则库`
- 里程碑：`M1-企业POC`
- 优先级：`P1`
- 页面：`PAGE-081 错误码管理页`、`PAGE-082 日志详情`
- API：`GET/POST /error-maps`、`PATCH /error-maps/{error_map_id}`
- 核心对象：`ProviderErrorMap`
- Webhook：`error_map.updated`
- 验收用例：`TC-0111`、`TC-0112`
- 当前状态：已完成。两轮编码前设计自审、实现、0046、专项与全量分段回归、Runtime、Browser、
  中文注释门禁和静止终态审计均通过；下一项为 `PRD-057`。

## 1. 权威需求与需求理解

### 1.1 权威来源

本设计已核对以下来源：

- Excel `01_PRD总表!A57:R57` 与 `02_M0-M1核心开发!A57:R57`。
- Excel `04_页面清单!A82:H83`：`PAGE-081`、`PAGE-082`。
- Excel `05_API接口清单!A261:H264`：`API-260` 至 `API-263`。
- Excel `06_数据对象!A77:F77`：`OBJ-076 ProviderErrorMap`。
- Excel `07_Webhook事件!A63:H63`：`EVT-062 error_map.updated`。
- Excel `08_验收用例!A112:J113`：`TC-0111`、`TC-0112`。
- `企业级AI_Gateway系统架构设计.docx` 的 Provider Adapter、控制面快照、热路径、
  `NormalizedError`、Trace Service、错误提示和 Adapter 合同测试约束。
- 已完成 `PRD-021`、`PRD-035`、`PRD-036`、`PRD-045`、`PRD-054`、`PRD-055`
  的设计、实现和测试记录。

### 1.2 冻结需求

1. 管理员可以为当前租户创建、修改、启停 Provider 错误归一规则；开发者只读；viewer
   在后端被拒绝。
2. 规则输入支持上游 HTTP 状态、固定错误字段和有界文本模式，输出只能是平台注册的
   Provider 统一错误码。
3. 规则修改后不重启 Gateway 即可生效；数据面热路径不得逐请求查询控制面或数据库。
4. 每个模型请求只冻结一次规则快照，同一请求的主调用、Fallback 和流式分支不能因后台
   热更新而使用不同版本。
5. 至少覆盖 401、403、404、429、5xx、余额不足和模型不存在；同时覆盖普通 Endpoint
   404、500 正文偶然包含模型关键词等误分类对照。
6. 原始上游错误只形成脱敏投影；Provider Key、Authorization、Token、Prompt、Response、
   Endpoint、Header 和任意原始 message/body 不得进入日志、API、Webhook、Audit 或数据库。
7. 失败请求必须继续由 `request_id/trace_id`、RequestLog、RoutingAttempt 和冻结规则事实追踪。
8. `error_map.updated` 必须先形成不可变 Source Outbox 事实，再异步进入现有 Webhook 投递链路。
9. `PAGE-081` 是独立错误码管理页；`PAGE-082` 直接扩展现有 RequestLog 详情，不再建立第二套
   日志页面或重复导航。
10. 不实现自然语言自动分类、JSONPath/脚本执行、模型推理分类、规则批量拖拽排序或跨租户共享
    自定义规则。

### 1.3 竞品与 Provider 边界

- OpenRouter、硅基流动仍只作为竞品和迁移兼容对象，不作为默认上游。
- 内置基线覆盖智谱、通义、文心、火山、混元、DeepSeek 官方或云服务以及客户本地
  OpenAI-compatible Endpoint 的通用错误形态。
- 本 PRD 不增加新的默认 Provider 厂商，不把厂商私有正文转发给调用方。

## 2. 架构约束与现状差距

### 2.1 可直接复用

- `OpenAICompatibleAdapter` 已统一承载当前国内兼容 Provider 和客户自定义兼容 Endpoint。
- `GatewayRoute -> ProviderRequest` 已携带可信 `tenant_id/provider_id/provider_type`，租户不来自
  客户端正文。
- `NormalizedProviderError` 已包含统一码、HTTP 状态、retryable、Provider 类型和质量分类。
- RoutingAttempt 已同事务记录失败终态并驱动 Fallback、模型质量样本和预算保守结算。
- Policy Snapshot cache 已具备 per-tenant singleflight、强 ETag、200/304、不可变替换、后台轮询、
  LKG 和有界关闭，可复用协议思想。
- Shared 已有 Unicode 规范化和敏感值识别基础；本 PRD 只复用这些确定性能力。M1 不开放
  正则表达式，避免 Python 回溯正则无法提供硬执行上限而阻塞 Gateway 事件循环。
- RequestLog 详情已展示 RoutingAttempt 和静态错误诊断，并具备 tenant/user/role/session/
  revision owner、Abort 和迟到响应门禁。
- PRD-055 已实现真正的 Source Outbox、lease/CAS、恢复调度和安全日志模式。

### 2.2 必须补齐

1. 当前 `_classify_upstream_error` 是全局静态分支，不能表达租户和 Provider 差异。
2. 当前错误提取重复调用 `response.json()`，没有统一字节、深度、节点、字段和文本扫描上限。
3. 当前所有 404 都被判为模型不存在，会把普通 Endpoint 路径错误误分类。
4. 当前 RequestLog 详情没有命中规则、快照版本和脱敏原始错误投影，不能重放历史解释。
5. 当前没有租户规则 CRUD、单调 head、内部快照接口、热更新缓存或事件 Source Outbox。
6. 当前 Console 没有 `PAGE-081`，且静态诊断 `rule_id` 容易与动态 `error_map_id` 混淆。

## 3. 总体方案

```text
admin/developer -> Control GET /error-maps -> PAGE-081
admin           -> POST/PATCH -> provider_error_maps + head + event + audit (同事务)
                                                |
                                                v
                                  error_map.updated Source Outbox

Gateway tenant auth -> acquire immutable overlay lease once
                    -> main/fallback/stream share the lease
                    -> Adapter extracts bounded ErrorFacts once
                    -> deterministic matcher: immutable guard -> tenant overlay -> builtin catalog
                    -> NormalizedProviderError
                    -> RoutingAttempt terminal + provider_error_fact (同事务)
                    -> RequestLog PAGE-082 reads frozen facts, never recalculates
```

控制面只保存租户覆盖规则。内置规则留在 Shared/Gateway 代码中，并使用稳定
`builtin_catalog_version`；内部快照返回覆盖层及目录版本。这样控制面短暂不可用时 Gateway
仍可使用内置基线，也不会把控制面发布节奏错误地变成数据面同步依赖。

## 4. Shared 数据结构与匹配合同

### 4.1 `ProviderErrorRule`

每条编译后规则为冻结数据结构，至少包含：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `error_map_id` | string | 租户规则为 `pem_<32 lower hex>`；内置规则为稳定 `builtin_*` |
| `source` | enum | `tenant` / `builtin` |
| `tenant_id` | string/null | 租户规则必填；内置元数据为空且不能落业务表 |
| `provider_id` | string/null | 非空时只匹配当前租户该 Provider；空表示当前租户全部 Provider |
| `priority` | int | `1..1000`，数值越大越先匹配 |
| `http_statuses` | tuple[int] | 最多 16 个，范围 `100..599` |
| `http_status_class` | enum/null | 仅 `4xx`；与 `http_statuses` 互斥。`5xx` 由不可覆盖 guard 独占，tenant 规则不得声明 |
| `error_field` | enum/null | 仅 `error.code/error.type/code/type` |
| `field_operator` | enum/null | 仅 `equals/contains` |
| `field_pattern` | string/null | NFC、长度 `1..256`；不进入事件和 Audit |
| `message_operator` | enum/null | 仅 `contains`，不提供任意全文 equals 或正则 |
| `message_pattern` | string/null | NFC、长度 `1..256`；仅扫描固定 message 字段 |
| `output_error_code` | enum | 只能来自 Provider 上游错误白名单 |
| `status` | enum | `active/disabled` |
| `version` | int | 指向不可变规则版本，从 1 单调增加 |
| `pattern_hash` | sha256 | 规范化匹配条件的哈希；事件/Audit 只使用该值 |
| `overlay_content_hash` | sha256 | 请求取得快照时的租户覆盖层哈希；只作为冻结证据 |

至少存在一个 HTTP、错误字段或 message 条件。只含 message 条件的租户规则必须绑定具体
`provider_id`，避免一条宽泛文本规则污染租户全部 Provider。

### 4.2 `ProviderErrorFacts`

Adapter 对一次上游失败只提取一次冻结事实：

- `upstream_http_status`。
- `transport_kind` 固定为 `none/local_configuration/network/timeout/other`：HTTP 响应为 `none`，
  InvalidURL、缺失本地配置等为 `local_configuration`，连接类异常为 `network`，白名单超时为
  `timeout`，其余已捕获传输异常为 `other`。该字段同时约束质量样本和预算结算，不能从异常文本推断。
- 原始 `provider_error_code/provider_error_type/message` 只进入 `repr=False` 的有界匹配缓冲区；
  匹配结束即释放，任何 ASCII 形态都不能自行取得持久化资格。
- 持久字段改名为 `provider_error_code_class/provider_error_type_class`，取值只能是
  `auth_error/rate_limit_error/quota_error/model_not_found_error/resource_not_found_error/
  timeout_error/invalid_request_error/server_error/[REDACTED]`。Shared 只把静态目录命中的原始信号
  映射为这些平台类；字段不存在时 class 为 null 且 present=false，字段存在但未登记时固定为
  `[REDACTED]`。租户 pattern 命中不构成保存原值的授权。
- 固定字段是否存在、message 是否存在、正文长度桶、是否截断、是否 JSON。正文长度桶精确为
  `unknown/0/1_255/256_1023/1024_4095/4096_16383/16384_65535/over_65535`；只有确认 EOF 或
  可信 Content-Length 才能使用数值桶，读取超时/异常且长度不可证明时为 unknown。
- `redacted_projection`：只允许固定字段名、上述平台 class、存在位和长度桶；message 恒为
  `[REDACTED]`，不得复制任何上游任意 token，其他字段全部丢弃。
- `projection_hash`：对脱敏投影做 SHA-256，不对原始正文做可字典攻击的裸哈希。
- 内存中的有界 message 候选只用于本次匹配，匹配结束立即丢弃，不进入数据类 `repr`。

限制：错误体最多检查 64 KiB、JSON 深度 16、节点 256、候选字段 8 个、每个候选文本 512
字符；超限稳定回落到状态码规则。流式非 2xx 只在连接仍打开时按同样总 deadline 和字节上限
读取错误体，读取失败不能阻止资源关闭。

### 4.3 确定性规则顺序

匹配先按三层安全边界执行：

1. 不可覆盖 guard：传输 Timeout、其他传输故障、401/403、408/504 和除 504 外的 5xx；它们
   只依赖可信异常类型或 HTTP 状态，租户正文规则不能改变分类。
2. tenant overlay：仅在 guard 未终结时匹配租户 active 规则。
3. builtin 普通 fallback：余额、模型不存在、普通 404、429、400/422 和通用兜底。

tenant overlay 内再按以下稳定 key 排序：`provider_id` 精确优先于租户通配、`priority DESC`、
条件 specificity DESC、`error_map_id ASC`。每条 builtin 冻结显式、唯一、从 1 开始的 `ordinal`；
builtin 只按 ordinal 匹配，ordinal、条件、输出和稳定 ID 一并进入 catalog canonical hash。余额规则的
ordinal 必须小于普通 429，不能依赖 ID 字典序表达业务优先级。

规则内所有已配置条件使用 AND；`http_statuses` 内部是 OR。首条命中即停止。相同租户、
Provider scope、priority 和规范化条件的 active 规则在写入时作为冲突拒绝，避免只依赖 ID
偶然决定业务结果。

### 4.4 输出白名单、安全 guard 与误分类保护

Shared 完整输出目录冻结为以下九个码；其中 tenant 仅可选择 `rate_limited/quota_exceeded/
model_not_found/resource_not_found/request_invalid` 五个。`auth_failed/timeout/unavailable/
upstream_error` 只由不可覆盖 guard 或 builtin 产生。公开 HTTP、retryable、Fallback category 和
user action 均从 Shared 静态目录推导，不能由请求或数据库字段覆盖：

| 输出码 | retryable | Fallback category |
| --- | --- | --- |
| `gateway.upstream_auth_failed` | false | `other` |
| `gateway.upstream_rate_limited` | true | `rate_limited`（仅原始 HTTP 429） |
| `gateway.upstream_quota_exceeded` | false | `other` |
| `gateway.upstream_model_not_found` | false | `other` |
| `gateway.upstream_resource_not_found` | false | `other` |
| `gateway.upstream_timeout` | true | `timeout` |
| `gateway.upstream_request_invalid` | false | `other` |
| `gateway.upstream_unavailable` | true | `other` |
| `gateway.upstream_error` | true | `upstream_5xx`（仅原始 5xx）或 `other` |

Control 编译和 Gateway 快照加载必须双重拒绝 tenant 规则包含 401/403/408/504、任意 5xx 或
`http_status_class=5xx`；这些条件在 guard 已终结后永远不可达，不能保存伪有效规则。其余
`HTTP status -> allowed output` 为：402 只能 `quota_exceeded`；404 只能
`model_not_found/resource_not_found`；429 只能 `rate_limited/quota_exceeded`；400/422 只能
`request_invalid/model_not_found/quota_exceeded`；其他 4xx 只能
`request_invalid/quota_exceeded`。`http_status_class=4xx` 在 guard 预先排除 401/403/408 后按
后一个集合匹配；无 HTTP 条件的字段/文本规则只能输出
`quota_exceeded/model_not_found/request_invalid/resource_not_found`。传输异常不接受租户规则。

内置目录版本冻结为 `provider-error-builtin-v1`。guard 与 builtin 的精确 ordinal/条件为：

| ordinal | 稳定 ID | 条件 | 输出 |
| --- | --- | --- | --- |
| 1 | `guard_transport_timeout` | transport_kind=timeout | `gateway.upstream_timeout` |
| 2 | `guard_transport_unavailable` | transport_kind in local_configuration/network/other | `gateway.upstream_unavailable` |
| 3 | `guard_http_auth` | HTTP 401/403 | `gateway.upstream_auth_failed` |
| 4 | `guard_http_timeout` | HTTP 408/504 | `gateway.upstream_timeout` |
| 5 | `guard_http_5xx` | 5xx excluding 504 | `gateway.upstream_error` |
| 10 | `builtin_quota_signal` | 4xx excluding 401/403/404/408，且 quota signal | `gateway.upstream_quota_exceeded` |
| 20 | `builtin_model_not_found_signal` | HTTP 400/404/422，且 model_not_found signal | `gateway.upstream_model_not_found` |
| 30 | `builtin_http_402` | HTTP 402 | `gateway.upstream_quota_exceeded` |
| 40 | `builtin_http_404` | HTTP 404 | `gateway.upstream_resource_not_found` |
| 50 | `builtin_http_429` | HTTP 429 | `gateway.upstream_rate_limited` |
| 60 | `builtin_http_400_422` | HTTP 400/422 | `gateway.upstream_request_invalid` |
| 70 | `builtin_http_other_4xx` | 4xx excluding 401/402/403/404/408/422/429 | `gateway.upstream_request_invalid` |
| 80 | `builtin_response_default` | 其余 HTTP 响应 | `gateway.upstream_error` |

signal 输入仍只在有界 `repr=False` 内存中。field equals 集合已经 NFC + casefold 并按字典序冻结：

- quota：`balance_not_enough/billing_hard_limit_reached/credit_exhausted/insufficient_balance/
  insufficient_quota/quota_exceeded`。
- model_not_found：`invalid_model/model_not_exist/model_not_found/unknown_model`。

message contains 集合按 Unicode 文本冻结：

- quota：`balance not enough`、`credit exhausted`、`insufficient balance`、`insufficient quota`、
  `余额不足`、`余额已用尽`、`额度不足`、`配额不足`。
- model_not_found：`model does not exist`、`model not found`、`unknown model`、`未找到模型`、
  `模型不存在`。

catalog canonical object 的 exact top-level keys 为 `catalog_version/guards/rules/signals`，规则数组
按上表顺序，signal 数组按上述顺序；序列化使用 sorted keys、无空白、`ensure_ascii=true`。
`pattern_hash` 先对不含自身的 rule object 做相同 canonical hash；signal rule 改为对
`{rule,signal_definition}` 做 hash，再把结果写回最终 catalog rule。精确 pattern hash 为：

```text
guard_transport_timeout=c15efa5c0b31d8ecf70121837879782e82b4b5fd6c94b54e4db9f87a2446e93d
guard_transport_unavailable=5574bcf6bb22bcb7965772cef322f9308e266375e9b3dd4a3a421217520b8d02
guard_http_auth=0fc51f7cdbef87250993cdfd9cfe132331fed351efb0095027d0755d9bd6d72f
guard_http_timeout=aab4f9bc0628a3e858558dc55b34d4428f6ab976242f9965be20464a86b3d264
guard_http_5xx=b48d775bae1c2b946ea20be6667565ae752a491d5c376f2a3ffbfc48b552c470
builtin_quota_signal=00e514ee453f23a290ff8a07b4caa8f54b62fa2f8fc0ae7ef197d4147a78bc8a
builtin_model_not_found_signal=e6e8ba16126e5790d950cdb057bfd3b89977e2824f4a07f9811ad97437867df0
builtin_http_402=50942b9dbda70a6f5d5539061dd0cc91b0b01f615ac8e1943ba2672892073880
builtin_http_404=e6f05c0c9938f75fd4b8135acfc7fef2986d2147034545ead7f51fdc9747a142
builtin_http_429=65202bb23bfe8a2a79cb6914c4bf2560252acc1f86e80b9458343aecd0d883ed
builtin_http_400_422=560747225636514b84b9b7aea2499017ab11c534ddbb4ea22dfbf193eb3bbe73
builtin_http_other_4xx=43af6da05d717c6d14eb0fb084405e38262a5029dc66879f35ac247d2f67ce74
builtin_response_default=afac5b684210c3df752f7c97eca47c2246866b41dcfe9324e1fea25544e2ae96
```

含 pattern_hash 的最终 canonical bytes 固定为 3664，catalog SHA-256 固定为
`a209e9d5e86e5b08d6fe97a1d0475ad8533704f6f782fa20dd732fc4dafdc507`。Control、Gateway、迁移
seed 和测试必须逐字节复算一致。

原始 HTTP 429 始终触发 KeyPool 冷却；即使明确余额信号把公开码归为 quota，也不能取消冷却。

### 4.5 自然语言和模式边界

- 不运行自然语言分类，不调用 LLM，不接受用户脚本、JSONPath、模板或表达式求值。
- tenant pattern 只允许 `equals/contains`；两者均在 NFC + casefold 后比较，输入和候选都受固定
  字符上限约束。M1 不接受 regex，也不把“匹配后检查耗时”误作执行预算保护。
- equals/contains 两类 pattern 还必须经过敏感值门禁：拒绝 Authorization/Bearer/`sk-`/
  `api[_-]?key` 标记、URL 凭据和长度至少 24 的高熵 base64/hex 片段；返回固定
  `error_map.pattern_sensitive`。pattern 可被 developer 读取，因此不能允许管理员把 Key 当规则。
- Control 写入和 Gateway 快照加载都拒绝任何其他 operator；Console 不执行服务端 pattern，
  不提供浏览器模式预览。

## 5. 数据库设计与 0046 迁移

### 5.1 `provider_error_maps`

稳定规则资源表只保存身份与当前版本指针：

- `provider_error_map_id`、`tenant_id`、`current_version`。
- `created_by`、`updated_by`、`created_at`、`updated_at`。

`provider_error_map_id` 为 `pem_<32 lower hex>`。tenant、creator、created_at 和资源 ID 不可改，
`current_version` 只能单步推进并通过 deferred 复合外键指向同租户同资源版本；updated_by 必须是
同租户用户。禁止 DELETE/REPLACE/rowid 改写。

### 5.2 `provider_error_map_versions`

每次真实变更插入完整不可变版本，唯一键为
`(tenant_id, provider_error_map_id, version)`；另有 `provider_error_map_version_id`，格式
`pemv_<32 lower hex>`。字段包含 Provider、priority、canonical `http_statuses_json`、status、
字段/message 条件、output、pattern/condition hash、head_revision、created_by、updated_by 和
created_at。版本行禁止 UPDATE/DELETE/REPLACE/rowid 改写。

`http_statuses_json` 必须严格为 `[]`，或升序、去重、无空白的 1..16 个 `100..599` 整数；
CHECK/trigger 同时约束：至少一个条件；operator/pattern 成对；message-only 必须绑定具体 Provider；
output 与 HTTP 矩阵兼容。Provider 和 user 均使用 tenant-qualified 复合外键。切换
current_version 的 trigger 必须查询其他 map 的现行 active 版本，以
tenant + Provider scope + priority + condition_hash 拒绝冲突，不能只依赖服务层。

### 5.3 `provider_error_map_heads`

- 主键 `tenant_id`。
- 持久 head 行的 `revision` 首次真实变更插入 1，此后单调加一，数据库永不保存 0；Control 无
  head 时返回合成 0，而 Gateway 的空 overlay lease 即使数据库已有 head 也可以使用合成 0。
- `content_hash` 是当前 active 租户覆盖层的 canonical hash。
- `updated_at`。

每次真实创建或更新在同一 `BEGIN IMMEDIATE` 事务内重新编译全部 active 覆盖规则、计算 hash
并推进 head。PATCH 同值 no-op 不推进 rule version/head，不制造事件或审计。

GET 和内部 GET 不创建空 head。无 head 的合成状态固定为 revision `0`、`updated_at=null`，空覆盖
canonical bytes 为 `{"rules":[],"schema_version":1}`，SHA-256 为
`994a863c694e05021d65d0f8b862f24a28da461080286b7b35d1695ee0775fdc`。强 ETag 是对 wire
schema、tenant、revision、该 hash 和 builtin catalog version 的 canonical JSON 再做 SHA-256，
格式严格为 `"<64 lower hex>"`；因此不同 tenant 的空状态 ETag 也不同。

### 5.4 builtin catalog registry 与 `provider_error_map_head_versions`

`provider_error_builtin_catalog_versions` 是全局、不可变的代码目录登记表，字段为
`builtin_catalog_version/catalog_hash/created_at`。catalog version 主键格式固定为
`[a-z0-9][a-z0-9._-]{0,63}`，hash 为 64 lower hex；0046 插入
`provider-error-builtin-v1` 的 canonical hash。该表是系统静态元数据而非租户业务数据，唯一允许
不带 tenant_id；禁止 UPDATE/DELETE/REPLACE/rowid。Shared 的 versioned builtin registry 必须与
数据库登记一致，旧版本代码与元数据永久保留，PAGE-082 才能解释滚动升级中的历史 Fact。

`provider_error_builtin_rule_versions` 同样是全局不可变 seed，字段为
`builtin_catalog_version/error_map_id/source/ordinal/pattern_hash/output_error_code/
rule_metadata_json`，0046 精确插入上表 13 行。主键
`(builtin_catalog_version,error_map_id)`，另唯一
`(builtin_catalog_version,error_map_id,pattern_hash,output_error_code)`；ordinal 在 catalog 内
唯一，metadata 是不含 pattern_hash 的 canonical rule object，signal rule 另含 signal definition。
Fact 的 guard/builtin 分支必须复合引用该关系，不能只验证 catalog version；两张 registry 表及
rowid 都禁止 UPDATE/DELETE/REPLACE。它们是第二个、也是最后一个无 tenant_id 的静态系统表。

每次真实 head 推进都先插入一条不可变历史行，字段为
`provider_error_map_head_version_id/tenant_id/revision/content_hash/builtin_catalog_version/
active_rule_refs_json/created_at`。ID 为 `pemhv_<32 lower hex>`，精确唯一键为
`(tenant_id,revision,content_hash,builtin_catalog_version)`，catalog version 外键指向上述 registry；
同一 overlay revision/hash 可以在未来 catalog 升级迁移中补充新的 catalog 组合，不能错误地对
`(tenant_id,revision)` 单独唯一。`active_rule_refs_json` 是按 error_map_id 排序的
`[{error_map_id,version}]` canonical JSON，只用于数据库验证 tenant 命中规则确属该冻结 overlay，
不包含 pattern。current head 的非零 revision/hash 由 trigger 要求至少存在一条同租户历史行；
历史行及其 rowid 禁止 UPDATE/DELETE/REPLACE。

真实变更事务顺序固定为“插入完整 rule version -> 计算完整 active overlay -> 插入 head version ->
推进 current head -> 插入 event/Audit/operation”。迟到请求即使当前 head 已推进到 v2，也能用冻结的
v1 revision/hash/catalog 和 active rule ref 写 Fact。未来 builtin catalog 变更必须由显式迁移先
追加 registry，再为所有非零 head 插入新 catalog 组合；旧目录/旧历史不删除，GET 不惰性写入也不
推进业务 revision。

revision 0 不插 head history，其精确定义是“本次请求实际没有使用 tenant overlay”，而不是
“数据库当前没有 head”。它用于 Control 无 head 的 fresh 快照、旧 revision-0 LKG，以及 cache
关闭/冷启动失败/LKG 超龄时的 builtin_fallback，即使数据库已存在非零 head 仍合法。Fact 的
revision 0 分支必须使用固定空 overlay hash、任一已登记且仍保留的 immutable catalog，并只允许
guard/builtin 命中；tenant 命中永远非法。

### 5.5 `provider_error_map_events`

既是不可变业务事件，也是 `error_map.updated` Source Outbox：

- `provider_error_map_event_id`、`tenant_id`、`provider_error_map_id`。
- `operation=create/update/enable/disable`。
- `rule_version`、`provider_error_map_version_id`、`provider_error_map_head_version_id`、
  `head_revision`、`overlay_content_hash`、`builtin_catalog_version`、`pattern_hash`、`status`、
  `provider_id`。
- `actor_user_id`、`request_id`、`trace_id`、`request_fingerprint`。
- `payload_json` 仅含固定安全字段。
- `dispatch_status=pending/processing/dispatched/failed`、attempt、lease、next_attempt、
  last_error_code、created/updated/dispatched 时间。

事件 ID 为 `pemevt_<32 lower hex>`，payload 上限 4096 UTF-8 bytes，唯一幂等键为
`(tenant_id, actor_user_id, request_id)`。actor、map/version、Provider 和 immutable head version
必须通过 tenant-qualified owner；插入 trigger 核对对应版本、current map、head revision/hash 与
选定 catalog 历史行的标量完全一致。
payload exact schema 还必须满足 object ID、event ID、actor、trace 与 source row 一致。

初始行只能是 `pending/attempt=0/next_attempt=created_at/无 lease`。claim 只能从到期 pending
进入 processing，或从租期已过且 attempt 1..9 的 processing 接管并加一；processing 成功进入
dispatched，未达第 10 次的失败回 pending，第 10 次进入 failed。固定参数为 lease 30 秒、最多
10 次、退避 5/30/300 秒封顶。claim token 固定为 32 lower hex；lease owner 必须匹配
`[A-Za-z0-9._:-]{1,128}`。所有 finish 以 event ID + attempt + claim token + lease owner +
未过期 lease 做 CAS，并严格推进时间；payload、actor、rule/version/head 和 fingerprint 永不可改。

### 5.6 `provider_error_map_operations`

所有已通过 admin 权限门禁的 POST/PATCH 都使用不可变幂等账本。字段包括
`provider_error_map_operation_id`（`pemo_<32 lower hex>`）、tenant、actor、request_id、
request_fingerprint、operation、可空 target map、`outcome=success/noop/business_error`、
`http_status`、可空固定 `error_code`、changed、可空 result version/head revision、可空 source
event ID、固定安全 response snapshot 和 created_at；唯一键为
`(tenant_id, actor_user_id, request_id)`。response snapshot 对成功/no-op 保存原 API 安全结果，
对确定性 404/409/422 只保存统一错误的固定标量，不保存 details、pattern、SQL 或请求正文。

只有通过 RBAC、raw request-id、wire JSON 和 canonical fingerprint 门禁的意图才进入账本；畸形、
重复键或超限正文直接返回固定 422 且不写 ledger。真实变更时 operation、version、head version、
current head、event 和 Audit 同事务；no-op 只写 operation，不推进 version/head，也不写
Audit/Webhook event。授权后可确定的业务失败（含资源/版本 404/409、语义 422 和资源/head 限额
409）只提交 `business_error` operation，version/head/Audit/event 必须为零增量，确保响应丢失后的
同 fingerprint 重试仍回放同一 status/error_code。401/403 在解析和账本之前返回，既不创建租户
业务事实，也不能借历史成功绕过当前 RBAC。

operation 禁止 UPDATE/DELETE/REPLACE/rowid 改写，结果标量必须与实际 outcome 一致。snapshot 不
保存响应 envelope 的 request_id/trace_id；回放时 request_id 使用同一 canonical operation ID，
trace_id 使用当前 HTTP 请求链路，首次 source event 仍保留首次提交 trace。只有网络中断等结果未知
情形可以复用 mutation request_id；任何已解析 HTTP 响应都终结该 ID。明确成功/404/409/422/403 后
修改资源状态、expected_version、输入或权限再提交，必须生成新 request_id，否则服务端回放原结果
或返回 `error_map.request_conflict`。

### 5.7 `provider_error_facts`

只有携带 Adapter 归一化 evidence 的失败 owner 才能写 Fact；Provider Key 不可用、请求取消、
审计闸门和普通本地 ApiError 不得伪造 guard/builtin Fact。owner 支持非流式 RoutingAttempt 与
流式握手 StreamSession 两种形态：

- `provider_error_fact_id`、`tenant_id`、`request_id`、`trace_id`。
- 可空 `routing_attempt_id`、可空 `stream_session_id`，CHECK 要求精确二选一。
- `provider_id`、`model_id`、`provider_code`。
- `upstream_http_status`、`transport_kind=none/local_configuration/network/timeout/other`。
- `provider_error_code_class/provider_error_type_class` 与精确布尔列
  `provider_error_code_present/provider_error_type_present/message_present`；class 只允许
  `auth_error/rate_limit_error/quota_error/model_not_found_error/resource_not_found_error/
  timeout_error/invalid_request_error/server_error/[REDACTED]` 或 NULL。NULL 必须对应 present=false，
  `[REDACTED]` 必须对应对应字段 present=true 且表示存在但不在静态平台目录；message_present
  只保存存在位，message 本身始终没有持久列。
- `redacted_projection_json`、`projection_hash`、`body_length_bucket`、`body_truncated`、
  `is_json`。长度桶精确为
  `unknown/0/1_255/256_1023/1024_4095/4096_16383/16384_65535/over_65535`；`is_json` 只表示
  有界正文是否通过严格 JSON object 解析，不保存任意 key/value。unknown 只能与 truncated=true
  组合，0 必须证明 EOF 时正文为空。
- `matched_error_map_id`、`matched_rule_version`、`matched_pattern_hash`、
  `matched_rule_source`、`error_map_revision`、`overlay_content_hash`、
  `builtin_catalog_version`、`error_map_snapshot_source`、`match_basis`。
- `normalized_output_error_code` 是客户端归一化码；`owner_terminal_error_code` 是已提交
  attempt/session 的内部终态码，两者不能再复用单一 `output_error_code` 列。
- `created_at`。

Fact ID 为 `pef_<32 lower hex>`。`matched_rule_source` 只允许 `guard/tenant/builtin`：
guard 使用稳定 `guard_*` ID，tenant 使用 `pem_*`，builtin 使用稳定 `builtin_*`。
`matched_rule_version` 仅 tenant 为正整数，guard/builtin 为 NULL 并由 catalog version 定位；
pattern hash 三类都必须是 64 lower hex。`error_map_snapshot_source` 只允许
`fresh/lkg/builtin_fallback`；`match_basis` 只允许
`transport_timeout/transport_error/http_status/provider_error_code/provider_error_type/message/
compound/default`。

0046 为 RoutingAttempt 和 StreamSession 分别增加
`UNIQUE(tenant_id,routing_attempt_id)`、`UNIQUE(tenant_id,stream_session_id)` 复合 owner，并
为 StreamSession 增加可空 `upstream_http_status` 与非空布尔 `retryable`；握手失败由归一化
evidence 冻结两列，其他既有终态固定 retryable=false。这样 PAGE-082 不需要按当前目录重算。
 
Fact 自身对两个 owner 分别使用 partial unique index，保证每个失败 owner 最多一条。非流式 Fact
与 `safe_complete_routing_attempt()` 的终态、质量观察在同一事务；流式只记录握手阶段 Adapter
错误，并与 `safe_finish_stream_session()` 在同一事务。不得为流式额外创建 RoutingAttempt，也
不为握手成功后的 SSE 读失败补造 Adapter Fact。

owner trigger 精确核对 tenant/request/trace/provider/model/provider_code、失败状态、原始 HTTP
状态和 retryable 来源。attempt 的 `owner_terminal_error_code` 除以下确定性派生外必须等于
`normalized_output_error_code`：公开 `gateway.upstream_unavailable` 且 transport_kind 为
`network/other` 时内部码为 `gateway.upstream_transport_failed`。stream 握手若归一结果属于
质量白名单（timeout、原始 429 rate-limit、原始 5xx 或 network/other 传输故障），内部码固定为
`gateway.upstream_stream_failed`；否则与归一码相同。trigger 使用这些静态矩阵校验，不比较
可变 message。

`builtin_catalog_version` 始终以声明式外键引用不可变 catalog registry。SQLite 不支持条件外键，
因此 Fact **不得**声明一个同时覆盖 revision 0 的
`(tenant_id,error_map_revision,overlay_content_hash,builtin_catalog_version)` head-history 外键；否则没有
父历史行的合法 revision-0 Fact 必然无法插入。0046 必须改用分支明确的 `BEFORE INSERT` trigger：

- `error_map_revision > 0` 时，要求存在同租户 immutable head version，且
  `revision/content_hash/catalog_version` 三个标量完全一致；tenant 命中还必须出现在该历史行的 active rule refs，并引用同租户不可变 rule version。
- `error_map_revision = 0` 时，不查询也不要求 head-history 父行；只接受固定空 overlay hash、已登记 immutable catalog 和 guard/builtin source，tenant 命中永远拒绝。

revision 0 允许与当前持久 head 共存。Gateway matcher 还必须用实际 HTTP 状态再次校验 tenant output
compatibility；无 HTTP 条件和 `http_status_class=4xx` 同样执行，不兼容候选继续下一规则。

事实不能外键到 RequestLog：现有 attempt/session 终态先独立提交，RequestLog 随后 upsert；
PAGE-082 通过 owner timeline 关联。Fact 禁止 UPDATE/DELETE/REPLACE/rowid；数据库/API 没有任何
message/body/header/Endpoint 列。

Fact 插入后还必须反向冻结完整 owner timeline。0046 对存在 Fact 的 routing_attempt 和
stream_session 安装 BEFORE UPDATE/DELETE trigger，对 PRAGMA table_info 可见的每一列及 rowid 都用
`IS NOT` 比较；只有所有列完全同值的 UPDATE 可以通过，任一 sequence/role/trigger/policy/model/
provider/status/error/http/retryable/usage/cost/latency/time 字段、stream chunk/bytes/first/last/
timeout/identity 字段或 updated_at 漂移，以及 DELETE/REPLACE/rowid 改写都必须拒绝。Fact 的 catalog
声明式外键、owner 复合外键、上述 revision 分支 trigger 与反向 trigger 共同保证 PAGE-082 的完整
owner timeline 永不漂移；测试以迁移后的实际列集合核对，避免未来新增列漏出冻结边界。

### 5.8 Owner 图、Audit 与 down migration

- map/version 的 creator/updater 使用 `(tenant_id,user_id)`；version 的 Provider 使用
  `(tenant_id,provider_id)`，NULL 只表示当前租户通配；Fact 以失败 attempt 或 stream session
  二选一为权威 owner，owner 之外的 request/model/provider 标量必须完全一致。
- event 复合绑定 actor、map/version、Provider 和 immutable head version；Audit owner trigger 要求
  `error_map.created/updated` 与同 tenant/actor/request_id 的 source event、version/current map
  一致，并反向禁止 `provider_error_map` 搭配其他 action。
- `SUMMARY_FIELDS["provider_error_map"]` 只允许 provider_id、priority、http_statuses_json、
  http_status_class、error_field、field_operator、message_operator、output_error_code、status、
  version、pattern_hash；其中 http_statuses_json 必须是与持久版本相同的 canonical ASCII 字符串
  （如 `[400,404]`），before/after 都不传 list/tuple/dict，也不放宽通用 Audit scalar validator。
- map/version/head version/current head/event/Audit 同事务提交，校验或 commit 失败全部 rollback。
  现有 `audit.recorded` Webhook 仍是提交后 best-effort dispatch，不虚构持久恢复能力；
  `error_map.updated` 则由本 PRD Source Outbox 独立恢复。任一派发失败都不反向改变已提交规则。
- operation 对 actor/map/event 使用 tenant-qualified owner，且结果标量必须与 version/head/event 一致。

- 0046 保真重建 `audit_logs` 的 action/object CHECK，加入 `error_map.created`、
  `error_map.updated` 和 `provider_error_map`，不能只改 Python 白名单。
- down 在 builtin catalog 的非 0046 seed 依赖、map/version/head version、operation、非零 head、event、fact、相关 Audit、Webhook Endpoint 的
  `event_types_json` 已订阅 `error_map.updated`、delivery 或 attempt 任一证据存在时，必须在
  任何持久 DDL 前原子拒绝；attempt 证据只按 tenant + delivery 关联以避免漏掉 Endpoint 漂移的
  存量证据，Endpoint 不一致另由 owner/integrity 校验报告并同样阻断回滚。
- down 还扫描 `sqlite_master` 和外键元数据：任何非 0046 白名单对象若依赖
  `provider_error_map*`、`provider_error_facts`、`provider_error_builtin_catalog_versions`、
  `provider_error_builtin_rule_versions`，
  或依赖 stream_sessions 的新增 `upstream_http_status/retryable`、attempt/session 的新增复合
  owner unique key，均在任何 DDL 前原子拒绝；空事实且无外部 view/trigger/index/FK 依赖时才回滚。
- 重建 `audit_logs` 时保留现有 8 个显式索引和 5 个 trigger，尤其不得覆盖 FinOps、成本异常和
  既有 owner 语义；正反迁移验证 `integrity_check=ok`、`foreign_key_check=0`、对象集合和
  Audit 行保真。

## 6. Control API 设计

### 6.1 后端权限

- `require_error_map_reader`：`admin/developer`。
- `require_error_map_writer`：仅 `admin`。
- 所有公共路由的函数签名不声明 Pydantic Body 或受约束 typed query/path；先执行
  `require_error_map_reader/writer`，再从原始 Request 手工解析 query、`request.path_params`
  和正文。内部路由同样先校验 Internal Token，再解析 tenant path 和 If-None-Match。
- viewer 和匿名请求即使携带非法 path、重复 query、超限/深层/重复键 JSON，也必须在业务解析前
  返回统一 403/401。
- POST/PATCH 在 writer RBAC 通过后、读取 path/body 前，从 `request.scope["headers"]` 恢复原始
  X-Request-ID。无外部头时使用中间件生成且已验证的 state 值；恰好一个头时必须 strict ASCII、
  长度 `1..128`、只含既有 safe identifier 字符，且规范化结果与原始值及 state 完全一致。重复
  同名头、代理拼接、非 ASCII、空白、替换或截断一律返回 `error_map.input_invalid`，不读正文、
  不写 ledger。禁止直接把中间件已清洗的 request_id 当幂等身份。
- tenant、actor、Session 只来自 `AuthContext`；客户端 body/query 不接受 `tenant_id/actor_id`。
- 跨租户 `provider_id/error_map_id` 与真实不存在使用同形 404。

### 6.2 `GET /error-maps`

返回：

- 当前 `head_revision/content_hash/builtin_catalog_version/updated_at`。
- 合并后的内置规则与当前租户规则；内置规则标记 `readonly=true`。
- developer 可读的安全 Provider 选项，仅含 `provider_id/provider_code/name/status`，不含 Endpoint、
  KeyPool 或凭据。
- `request_id/trace_id`。

支持白名单过滤 `error_map_id/provider_id/source/status/output_error_code/limit/offset`；未知或
重复 query 拒绝。`error_map_id` 只接受 `pem_*`，与其他过滤和分页互斥，并按认证 tenant 精确
返回该稳定资源的当前版本；不存在、跨租户或已不可见都返回同形
`error_map.not_found`。这条 tenant-qualified 精确过滤专用于 409 后冲突重取，不能因规则状态、
当前筛选或分页遗漏目标。provider_id 同样先做当前 tenant owner 查询。

只读 GET 不创建 revision 0 head，也不产生 Audit/Event。响应 `Cache-Control: no-store`，并返回
只用于页面刷新判断的强 ETag；精确过滤仍返回同一 envelope 和一个元素的规则数组。

### 6.3 `POST /error-maps`

- admin-only，权限通过后手工有界读取 UTF-8 JSON object，最大 16 KiB、深度 16、节点 128、
  拒绝重复 key。
- 精确字段：Provider scope、priority、HTTP 条件、字段条件、message 条件、output code、status。
- `X-Request-ID` 作为 tenant + actor 维度幂等键；相同规范化意图重放原结果，不同意图返回冲突。
- 成功 `201`，返回规则、head、`request_id/trace_id`；同事务写 Audit、事件和 head。

### 6.4 `PATCH /error-maps/{error_map_id}`

Excel 的 `PATCH /error-maps` 简写冻结为资源路径 PATCH，避免 body 中对象 ID 与 path 漂移。

- admin-only；body 必须含 `expected_version` 和至少一个可编辑字段。
- PATCH 与 POST 使用同一 JSON 字节/深度/节点/重复键门禁。先做 tenant-qualified 查找，再执行
  CAS；版本冲突只返回现有统一 409 错误体和 request_id，不扩展 `ErrorResponse`。
- 同值 no-op 返回 `changed=false`，不推进版本、head、Audit 或事件。
- 启停使用 `status` 字段，不新增 DELETE；历史事实永不删除。
- 仅网络结果未知重试复用原 mutation request_id；任何已解析 HTTP 响应后都生成新 ID。指纹不同返回 request conflict。事务顺序固定为
  `BEGIN IMMEDIATE -> 按 tenant+actor+request_id 查 operation ledger -> 同 fingerprint 回放 /
  不同 fingerprint 冲突 -> 不存在才检查资源和 expected_version`，避免已提交 PATCH 重试被误报
  版本冲突。
- Console 收到确定 409 后终结 mutation request_id、保留 dirty 草稿，并启动独立
  conflict-refresh ticket 调用 `GET /error-maps?error_map_id=<tenant-qualified-id>`；不得依赖 409
  details，也不得复用页面列表 GET ticket。用户选择载入最新版本后才替换草稿，下一次 PATCH 使用
  新 expected_version 和新 request_id。

### 6.5 内部快照接口

`GET /internal/error-maps/{tenant_id}/current`

- 仅 `X-Internal-Token`，不能用用户 Bearer 替代。
- query 为空；严格验证 tenant 存在且 active，跨租户不可由用户身份调用。
- 响应精确包含 schema、tenant、head revision、overlay content hash、builtin catalog version 和
  active tenant rules；不含 pattern 之外的管理元数据、actor、Audit 或事件。
- ETag 绑定 wire schema、tenant、revision、builtin catalog version 和 content hash。
- 支持 `If-None-Match` 与严格 304；200/304 都回显同一强 ETag 和 `no-store`。

## 7. Gateway 热更新与 Adapter 集成

### 7.1 `ProviderErrorMapCache`

- 独立于 Policy cache 的 per-tenant 状态、keyed singleflight 和全局并发上限。
- 复用 Control internal URL 和内部 Token，使用独立启用、轮询、超时、payload 和 LKG 配置。
- 独立配置冻结为 `METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED=false`、轮询 2 秒、HTTP timeout 2 秒、
  refresh queue timeout 2 秒、fresh 300 秒、LKG 硬上限 3600 秒、payload 262144 bytes、全局
  refresh 并发 4。生产/验收显式开启；关闭时稳定使用 builtin 且不写故障告警。
- AppConfig 对应字段都有兼容默认值；边界固定为 poll `1..300` 秒、HTTP/queue timeout
  `0.1..10` 秒、fresh `1..86400` 秒、LKG `fresh..604800` 秒、payload
  `4096..1048576` bytes、并发 `1..32`。类型、范围和 LKG/fresh 交叉校验在启动前失败，
  既有直接构造 AppConfig 的调用不传新字段时保持原行为。
- 已认证 tenant 首次 acquire 只注册状态并触发后台 singleflight，不等待 Control 网络；当次立即
  租用现有 fresh/LKG/builtin，避免诊断配置给模型主链路增加 2 秒尾延迟。轮询更新后下一请求生效。
- refresh 在进入全局 semaphore 前最多等待 queue timeout，HTTP 阶段另受 2 秒总 timeout；超时只
  结束本次刷新并保留 LKG。共享 refresh task 由 cache owner 持有，所有 waiter 使用
  `asyncio.shield`，单个请求取消不能取消同租户刷新或其他 waiter。
- 200 的 revision 只可单调前进；同 revision 不同 hash、版本回退、catalog 不等于当前 Shared
  目录、tenant/schema/ETag 漂移均拒绝并保留 LKG。超龄回落不丢弃已验证 ETag，后续严格 304
  仍可重新确认新鲜度。
- 首次已认证 tenant 才注册；scheduler 只轮询已见 tenant。
- 请求使用 `If-None-Match`，拒绝 redirect、代理、压缩、超大 body、重复 JSON key、tenant/schema/
  catalog/hash/version 漂移。
- 成功 200 先完整编译为不可变 overlay，再原子替换；304 只刷新已验证 ETag 的新鲜时间。
- 同步失败保留 LKG；LKG 超过硬上限后回落内置基线并写固定安全告警，但绝不阻断模型调用。
- close 先禁止新 refresh，再显式取消并 await scheduler/singleflight，最后关闭 HTTP client；整个
  close 有界且清空任务表，不能遗留后台任务或让 waiter 重新创建 refresh。

### 7.2 请求期冻结

Virtual Key 鉴权恢复 tenant 后、读取业务正文前获取一次 error-map lease，并保存到当前 Request
内存 owner。构造每个 `ProviderRequest` 时只复制该冻结引用，字段 `repr=False`。主调用、备用调用
和流式握手共用同一引用；后台 v2 替换不能改变已经发放的 v1。
lease 同时冻结 `revision/overlay_content_hash/builtin_catalog_version/source`；同一 FastAPI Request
只保存一个 owner，所有 ProviderRequest 只复制引用，不能在 Fallback 函数重新 acquire。


错误规则是诊断/归一配置而不是访问控制：冷启动同步失败使用 builtin，不复用 Policy 的 fail-closed
语义。lease source 为 `fresh/lkg/builtin_fallback`，只在安全事实中记录枚举和版本。

### 7.3 Adapter 和终态

- `ProviderRequest` 只在字段尾部追加默认 null、`repr=False` 的 error-map lease，保持现有位置
  参数构造兼容。`ProviderAdapter.normalize_error(response=None, exc=None, *, request=None,
  extracted_facts=None)` 只增加可选 keyword 参数；旧单测和第三方 Adapter 省略时使用 builtin。
- `providers/base.py` 按 unbound method/class 做进程级 signature capability cache；factory 每次
  新建 Adapter 实例不能造成重复 inspect。禁止在热路径通过“先传新 keyword、捕获 TypeError 再
  重试”探测。能绑定 request/extracted_facts 的内建或新 Adapter 接收冻结上下文；只能绑定旧
  `(response=None, exc=None)` 的 override 仍按两个位置参数调用，使用既有 builtin 结果且不伪造
  tenant Fact。旧 subclass override、继承未 override 和新 override 三种形态都写合同测试。
- 合并 `_provider_error_type` 与 `_provider_error_keyword_text` 为一次有界事实提取。原始 code/type/
  message 放在独立 `repr=False` match input；`NormalizedProviderError` 仅追加有默认值且
  `repr=False` 的安全 evidence 和固定 `transport_kind`，不持有原始正文。
- 现有 `provider_error_type` 构造字段为兼容旧调用保留并改为 `repr=False`；本实现只输出固定平台
  class。质量/成本逻辑迁移到 `transport_kind=none/local_configuration/network/timeout/other`，
  并对旧手工构造值保留兼容回退；InvalidURL 为 local_configuration，不计质量且明确释放预算，
  ConnectError 为 network，形成质量样本并保守保留成本暴露。
- `normalize_error` 使用冻结规则匹配，输出 `NormalizedProviderError` 和 repr-safe 归一化证据。
- 自定义 output 不能改变 KeyPool 对原始 429 的冷却语义；该行为以 HTTP 429 为准并写合同测试。
- Fallback category、质量样本资格和成本暴露继续使用固定 Shared 推导，不能信任管理员字段。
- 非流式成功响应沿用现有读取；非 2xx 不能先调用会完整缓冲的 `client.post()`，必须使用
  wire-level streaming response。请求强制 `Accept-Encoding: identity`，响应只允许缺省或
  `Content-Encoding: identity`；Provider 忽略协商返回 gzip/br/deflate 时不解压、不读正文，直接
  用状态码归一。Content-Length 必须是唯一十进制值且不超过 65536；缺失时允许流读，非法、超限或
  与实际 EOF 长度不一致时丢弃已累积正文。
- 正文只用 `aiter_raw(chunk_size=8192)` 在 1000 ms 总 deadline 内读取，最多保留前 65536 bytes，
  但必须继续探测 EOF 或第 65537 byte；任何额外字节都丢弃整个正文并设置
  `body_truncated=true/body_length_bucket=over_65535`。即使 Content-Length=65536 也要探测 EOF，
  无 Content-Length 且恰好 65536 + EOF 才可作为完整正文。读取异常/超时且无法证明总长度时丢弃
  正文并使用 `body_truncated=true/body_length_bucket=unknown`。绝不调用
  `aiter_bytes/text/json/read`；流式握手非 2xx 使用同一 helper。
- 异常优先级固定为：外部 `asyncio.CancelledError` 最高，在资源关闭后原样传播；没有外部取消时，
  非 2xx response/context 的 `__aexit__` 清理异常继续按既有合同传播；内部读取 timeout 只使正文
  降级，不能吞掉前两者。无论何种组合，adapter-owned client 都必须在 finally 关闭。
  Content-Length 欺骗、chunk 超限和读取异常均不得泄漏部分正文。
- `safe_complete_routing_attempt` 在同一事务完成 attempt、质量观察和可空 provider_error_fact；
  `safe_finish_stream_session` 对握手错误执行同样原子提交。Fact trigger 失败时 owner 终态和质量
  观察整体回滚；安全 wrapper 只写不含异常正文的固定告警，不能覆盖原始客户端错误。
- 只有 `UpstreamApiError` 携带 Adapter evidence 时传入 Fact；Provider Key、取消、审计和本地
  gateway 错误只终结 owner，不制造归一化规则命中。
- 外层 RequestLog 继续记录统一 `error_code/request_id/trace_id`，失败请求可由 PAGE-082 查询。

## 8. Webhook 与 Source Outbox

`error_map.updated` payload 精确字段：

- `schema_version=1`、`operation`、`provider_error_map_id`、`rule_version`、`head_revision`。
- `provider_scope`（`all` 或安全 provider ID）、`status`、`priority`。
- `output_error_code`、`pattern_hash`、`request_id`。

不包含 pattern、匹配文本、原始错误、Provider name/Endpoint、actor email 或任何 Key。Shared
Webhook envelope validator 验证 event ID `pemevt_<32 lower hex>`、tenant、actor、object、trace、
request；这些 envelope 字段必须与 source row 精确一致。payload validator 只要求上列 exact keys，
并交叉校验 payload request_id、provider_error_map_id 与 envelope/source；不声称 payload 含有
event_id/actor/trace。PRD-056 专用 validator 先执行 4096-byte 门禁，再要求 payload 是 flat
exact-key scalar object；不声称现有通用 Webhook validator 已有深度/节点限制，也不改变其他合同。

Outbox worker 复用 PRD-055 的 claim/lease/CAS、指数退避、最大尝试和
`resume_existing_pending=true`。提交后即时 tick 失败只保留 pending；应用启动和周期 worker
继续恢复。停服必须等待活动 tick 有界结束。

资源上限冻结为：每租户最多 128 条 active、512 个稳定规则资源；head revision 最大
`2147483647`；POST/PATCH 16 KiB；内部快照 256 KiB；单事件 payload 4096 bytes；ID 分别为
`pem_/pemv_/pemhv_/pemo_/pemevt_/pef_ + 32 lower hex`。达到任一上限返回固定
`error_map.limit_exceeded`。已形成 canonical intent 的资源/head 限额只写不可变
`business_error` operation，不写 version/head/Audit/event；wire body 超限发生在 fingerprint
之前，不写任何业务表。两者都不能留下部分版本或事件。

## 9. PAGE-081 前端设计

### 9.1 页面内容

- 后端导航新增 `id/target_section=error-maps`，分组“配置”，角色 `admin/developer`。
- 顶部显示配置 revision、内置目录版本、更新时间和刷新命令，不虚构 Gateway 已加载版本。
- 过滤 Provider、来源、状态、统一错误码。
- 规则行显示优先级、Provider、HTTP/字段/message 条件、输出码、派生 retryable/user action、
  来源、规则版本和状态。
- 内置规则始终只读；admin 对租户规则使用显式新建/编辑、保存/取消和启停；developer 无表单、
  无写按钮、无写 listener。
- Provider 下拉使用 `/error-maps` 返回的安全选项，不预取 admin-only KeyPool。
- 409 不覆盖草稿；Console 显式 GET 后显示最新服务器版本并要求“载入最新版本”。只有网络结果未知
  才复用 mutation request_id；任一 HTTP 响应后原 ID 终结，尤其成功/404/409/422/403。冲突精确 GET 使用独立 ticket，响应 owner
  失效时丢弃；只有用户确认“载入最新版本”才更新 expected_version。
- 新建/编辑使用 `role=dialog`、`aria-modal=true` 的单层 dialog。打开时保存触发器和页面滚动
  状态、初始焦点落在标题后的首个字段、Tab/Shift+Tab 被限制在 dialog；Escape 在非 busy 状态
  等价取消。关闭后仅当身份 owner 未变且触发器仍 connected 时恢复焦点，并总是解除滚动锁。

### 9.2 页面生命周期

- identity 绑定 tenant/user/role/session/session_revision；不完整身份和 viewer 不生成 owner。
- 只有后端可见导航确认 active `error-maps` 后才 GET。
- GET 与 mutation 使用独立 AbortController/ticket；周期 GET 不取消 mutation，也不覆盖 dirty/busy。
- error-map state 暴露 `blocksShellDomReplacement()`：dialog 打开、dirty、busy 或 conflict 任一为
  true 时，Console 的 15 秒 shell refresh 仍必须请求并校验 Session/导航，但同一 identity owner 下
  禁止执行 `app.innerHTML` 或重建 error-map section；只更新不影响编辑的 shell 状态。否则现有
  `renderDeveloperStart` 会销毁 dialog、焦点陷阱和滚动锁。
- shell refresh 若发现 401、tenant/user/role/session/revision 变化或后端导航移除 error-maps，
  必须先同步调用 error-map leave，关闭 dialog、Abort 所有 ticket、解除滚动锁，再允许全壳替换；
  不能为了保留草稿而延迟权限收回。owner 未变且不再有保护状态后，下一个周期恢复普通全壳刷新。
- 离页、logout、pagehide、换租户、降权、401 和导航裁剪同步 leave，销毁行、草稿、pattern、
  request_id 和错误。
- 发网前后都复核实时 owner；迟到成功或迟到 401 不能污染新身份或清理新 Session。
- 所有服务端字符串二次 HTML/attribute 转义；Console 不执行 pattern。

### 9.3 响应式布局

- 1280：紧凑过滤栏和扫描友好的规则表。
- 小于 1100：隐藏表头，每行变为带 `data-label` 的两列网格。
- 390：单列；code/pattern 使用 `overflow-wrap:anywhere`；按钮换行；编辑区宽度
  `min(100%, calc(100vw - 24px))`、`box-sizing:border-box`，有 max-height 和内部滚动；
  dialog 不得超过 Console 主内容父容器。
- 不使用横向 `min-width` 表、不嵌套卡片、不让状态 badge 改变轨道尺寸。

## 10. PAGE-082 日志详情设计

PAGE-082 复用 `request-logs` 页面详情，冻结两个现有 DTO 挂载点：
`RequestLogRoutingAttempt.provider_error_fact` 与
`RequestLogStreamSession.provider_error_fact`。控制面分别按 owner 关联，Console 在 attempt
timeline 和独立 stream_session 区块都渲染；不得把流式 Fact 塞进虚构 attempt。两处
`provider_error_fact` DTO 字段与持久 Fact 精确同名：

- `provider_error_fact_id/routing_attempt_id/stream_session_id`。
- `matched_error_map_id/matched_rule_source/matched_rule_version/matched_pattern_hash`。
- `error_map_revision/overlay_content_hash/builtin_catalog_version/error_map_snapshot_source`。
- `normalized_output_error_code/owner_terminal_error_code/upstream_http_status/transport_kind/
  retryable/match_basis`。
- `provider_error_code_class/provider_error_type_class/provider_error_code_present/
  provider_error_type_present/message_present`。
- `redacted_projection_json/projection_hash/body_length_bucket/body_truncated/is_json`。

`retryable` 必须从冻结的 RoutingAttempt/StreamSession owner 行读取，不能用当前 catalog 重算；
API service 只执行明确列映射，不把 DB 字段改成另一套短名。`redacted_projection_json` 在 DB 为
有界 canonical TEXT，DTO 保持同名但输出经 exact-schema 校验后的 JSON object，不返回任意上游
key/value。列表行仍只显示统一 error code。现有
静态 `ErrorDiagnosisInfo.rule_id` 的标签改成“诊断规则”；动态 ID 标成“归一化规则”，两者不得
复用字段名。历史详情只读持久事实，不使用当前规则重算。

从日志详情跳转规则页时，只通过完整身份绑定的一次性内存 handoff 传递安全 map ID 和冻结版本；
URL 只能是 `#error-maps`，不得加入 query、pattern、原始摘要或 request ID，不写 Storage。当前规则
版本变化时显示“请求时版本 / 当前版本”，不能声称当前规则产生了历史结果。只有 tenant source
提供可点击跳转并通过精确 GET 获取当前资源；builtin source 按 `builtin_catalog_version` 在不可变
版本目录中定位只读条目；guard 只显示稳定 guard ID，绝不渲染跳转。handoff 的 owner 失效即销毁。

## 11. 统一错误码设计

新增 Control 错误至少包括：

- `error_map.not_found`：404。
- `error_map.input_invalid`、`error_map.update_empty`、`error_map.output_code_invalid`：422。
- `error_map.pattern_invalid`、`error_map.pattern_sensitive`：422；regex/未知 operator 统一视为 invalid。
- `error_map.conflict`、`error_map.version_conflict`、`error_map.request_conflict`：409。
- `error_map.limit_exceeded`：409。
- `error_map.unavailable`、`error_map.snapshot_unavailable`、`error_map.event_unavailable`：503。
- `error_map.tenant_unavailable`：404，内部接口不暴露其他租户事实。
- `gateway.upstream_resource_not_found`：502、不可重试，用于无模型信号的普通上游 404。

所有错误进入 `ERROR_SPECS`、中英文短标签和静态诊断；响应由统一中间件补齐
`request_id/trace_id/suggestion/diagnosis`，服务不得返回底层 SQL、解析异常或上游正文。

## 12. 安全、租户和隐私约束

1. 所有新业务表携带 `tenant_id`；所有查询第一归属条件为认证 tenant。
2. 后端 RBAC 先于 query/path/body 业务解析；前端隐藏只减少预取，不构成授权。
3. 写请求拒绝客户端 tenant、actor、版本派生字段和任意 output message/retryable。
4. 跨租户 Provider/rule/request/attempt 使用同形 404，不能通过版本或冲突差异枚举。
5. 原始错误正文只在有界内存中短暂存在；日志、数据库、Audit、Webhook、DOM、URL、Storage、
   截图和验收 JSON 都不得出现原文。
6. pattern 对 developer 可读，但不进入日志、事件、Audit summary 或访问追踪 payload。
7. Access log 不记录 query/body；结构化日志只记录稳定 rule ID、revision、output code 和错误类型。
8. 内部 Token 常量时间比较，200/304 均不泄露 Token 或 tenant 之外的信息。
9. Gateway 冷启动和 LKG 降级只影响错误分类精度，不绕过鉴权、预算、DLP、路由或用量追踪。
10. `.tmp`、pytest 和验收临时文件只使用 `D:/MetaHub/.tmp`；清理只删除 ReparsePoint 节点，
    不删除链接目标、普通目录或测试证据，不使用 `C:/tmp`，不提权。
11. `.tmp` 扫描必须先枚举节点属性，不递归进入 ReparsePoint；若没有 ReparsePoint 就不执行
    删除，普通的不可访问 pytest 证据目录不能被当作链接处理。


## 13. 测试设计

### 13.1 Shared 与 Adapter 合同

- 内置 401/403/404/429/全部 5xx、余额不足、模型不存在、超时、400/422。
- 429 + 余额不足优先于限流；401 + quota 文本仍为鉴权；500 + model 文本仍为 5xx；普通
  404 与明确 model_not_found 分离。
- Provider-specific、tenant wildcard、builtin、priority、specificity、稳定 ID 排序。
- disabled、重复条件、Unicode NFC/casefold、拒绝 regex/未知 operator、超长/深层/重复 JSON key。
- 恶意 code/type/message、Authorization、Provider Key、Prompt 回显不进入对象 repr、日志或事实。
- OpenAICompatibleAdapter、GenericHttpConfigAdapter 及全部 compatible provider type 参数化合同。
- 旧 `normalize_error(response=None, exc=None)` 调用及 old-signature subclass override 兼容；
  signature capability 不通过 TypeError 探测；InvalidURL/ConnectError 的质量和预算分歧；
  公开码/owner 终态双码矩阵。
- 非流式和真实流式非 2xx 的 wire 有界读取；超限/内部超时/非法 JSON 回落、外部取消传播、
  清理异常优先级与 client 必定关闭；gzip/br/deflate 拒绝、identity + aiter_raw、Content-Length
  缺失/非法/欺骗和单 chunk 越界均证明内存上限。

### 13.2 Control API 与 Outbox

- admin CRUD；developer GET 成功且 POST/PATCH 403；viewer 全部 403；anonymous 401。
- 未授权的大正文、重复 key、畸形 JSON 仍先返回权限错误。
- raw X-Request-ID 缺省、单一合法、重复、非 ASCII、非法字符、中间件替换/截断；拒绝路径不读
  body、不写 ledger。任一 HTTP 响应终结前端 mutation ID，404/409 修正后使用新 ID。
- 跨租户 Provider/rule 同形 404；tenant 不来自 body/query。
- 幂等成功/业务失败重放、当前 trace envelope、request conflict、CAS conflict、no-op；规则限额
  只写 business_error operation，wire 超限零业务行；非法 operator/output。
- GET 合并内置/租户规则、过滤、分页、安全 Provider options、no-store 和 request_id；tenant-qualified
  error_map_id 精确过滤不受状态和分页影响。
- 内部 Token、Bearer 混用拒绝、200/304、强 ETag、tenant/catalog/hash/version 篡改。
- Source Outbox claim、租期接管、退避、最大尝试、CAS 丢失、提交后恢复和关闭等待。
- Webhook exact payload/envelope；pattern、原始错误和敏感键递归门禁。

### 13.3 Gateway、RequestLog 与缓存

- 首租户 singleflight、跨租户隔离、200/304、强 ETag、错误 wire、版本回退、LKG、冷启动
  builtin、LKG 超龄回落和 close。
- waiter 取消不取消共享 refresh、queue timeout、close 清空任务；AppConfig 默认/边界/交叉校验。
- v1 请求期间加载 v2：该请求主/备均 v1，下一请求才使用 v2。
- revision0 fresh/LKG/builtin_fallback 在当前 DB head 非零时仍合法；滚动 catalog 升级中旧登记
  catalog 的迟到 rev0/非零 Fact 均可提交，tenant source 不可借 rev0 命中。
- 非流式、真实流式非 2xx、Fallback、KeyPool 429 冷却、质量资格和成本暴露回归。
- RoutingAttempt/StreamSession 与 provider_error_fact 分别同事务；Fact 校验失败整体回滚；流式不
  额外创建 attempt；当前 head v2 时仍可写冻结 v1/LKG Fact。
- Fact 绑定后以 PRAGMA table_info 参数化验证 attempt/session 每个业务列：全同值 UPDATE 允许，
  任一列改值以及 DELETE/rowid/REPLACE 拒绝；
  RequestLog DTO 使用 Fact 原字段名，retryable 取 owner 冻结值，guard 不跳转。
- API 成功/错误都有 request_id；失败请求可按 request_id/trace_id/provider/error 查到。

### 13.4 迁移

- 空库 0001 -> 0046、0046 重复 apply、开发库 0045 -> 0046。
- 表/列/索引、builtin registry、attempt/session 双 owner、head 历史、Fact 全列反向 owner、
  Audit canonical scalar、不可变、状态机、rowid/REPLACE 触发器；未知对象分别只依赖 facts、
  builtin registry 或 stream 新列时 down 也原子拒绝。
- 空事实 down/up；存在 rule/head/head version/event/fact/Audit/Webhook 或后续 catalog 依赖时拒绝。
- Audit 保真、`integrity_check=ok`、`foreign_key_check=0`。

### 13.5 Console 与 Browser

- admin 写、developer 只读、viewer 无导航/DOM/预取；直调后端仍 403。
- tenant A 迟到 GET/PATCH、同 ID 跨租户、离页、降权、401、周期刷新、结果未知重试。
- 内置不可写、409 草稿保留、XSS 转义、pattern 不执行、无 URL/Storage 泄露。
- PAGE-082 区分诊断规则/归一化规则、冻结版本、脱敏投影和一次性 handoff。
- stale 409 使用确定步骤：admin A 打开 v1 草稿，独立 admin B/API 用另一 request_id 更新到 v2，
  A 用 `patch_v1_request_id` 以 expected_version=v1 PATCH；断言 409 和草稿保留。独立精确 GET
  取得 `conflict_get_request_id` 与 v2，用户选择载入 v2 后，用第三个
  `patch_v2_request_id`、expected_version=v2 再 PATCH 并成功到 v3。断言三个 ID 两两不同，
  第二次写没有复用 stale ID，且精确 GET 的读 ID从未进入 mutation ledger。
- Runtime 在同批 evidence manifest 写入已映射 request_id、map ID/version/revision；Browser 必须
  读取同一批次并打开非流 attempt 详情及一次真实流式握手失败的 stream_session Fact，禁止自行
  猜造历史 ID；两种挂载点都必须有渲染断言。
- viewer 对 `GET/POST/PATCH /error-maps*` 的网络请求总数精确为 0，导航与 error-map DOM 为 0；
  后端直调另证 403。批次临时 developer/viewer Session 在 finally 中逐个 revoke/logout，补偿失败
  使验收失败且保留 manifest。
- 固定保存 6 张截图：admin 桌面列表、admin 移动编辑、stale 409、developer 只读、viewer 无导航、
  映射日志详情；1280x720 与 390x844 检查水平溢出、遮挡、dialog 焦点/Escape/restore、非空像素。
  另保存 network count 与敏感哨兵扫描 JSON，截图和 JSON 均不得包含原始错误。

### 13.6 实际运行验收

- 使用批次独立 0046 数据库启动 Gateway、Control、Console 和本地模拟上游。
- 真实登录 admin/developer/viewer，验证 RBAC、租户隔离、统一错误和 request_id。
- 创建覆盖规则，等待轮询，在不重启 Gateway 的情况下验证下一请求使用新 revision。
- 依次注入 401/403/普通 404/模型不存在/429/余额不足/5xx/恶意敏感正文，并执行真实流式非 2xx
  握手，核对 API、DB、RequestLog、StreamSession、Webhook 和日志。Runtime 输出给 Browser 的
  同批 evidence manifest。
- Runtime 与 Browser 都通过后停止三服务，审计端口、Session、outbox、sidecar、spool、SQLite
  checkpoint、日志隐私和 ReparsePoint；清理只删除 `.tmp` 内链接节点并保留目标/证据。
- 测试节奏固定为：实现阶段按模块跑定向测试，功能闭环后跑一次 PRD-056 专项，最终只跑一次全量
  回归；局部失败只重跑受影响用例及其直接回归，不因一个局部问题从头重复全量。

## 14. 代码修改计划

### 14.1 Shared

- 新增 `metahub_shared/runtime/provider_error_map.py`：wire、builtin、规则编译、事实提取、
  matcher、脱敏投影和确定性 hash。
- 更新 `errors.py`、`error_diagnosis.py`、`i18n.py`、`webhooks.py`、`config.py`。

### 14.2 Control

- 新增 `metahub_control/error_maps/{models,routes,service,outbox,scheduler}.py` 和导出。
- 更新 `auth/dependencies.py`、`auth/__init__.py`、`app.py`、`audit/service.py`、
  `console/service.py`、`webhooks/service.py`。
- 新增 `0046_provider_error_normalization_rules.sql/.down.sql`。

### 14.3 Gateway

- 新增 `metahub_gateway/error_maps/{cache.py,facts.py}` 及生命周期安装；Fact helper 只接收脱敏 evidence。
- 修改 `providers/base.py`、`providers/openai_compatible.py`、`routing/attempts.py`、
  `streaming/sessions.py`、`api/openai_compatible.py`、`app.py`。
- Request 只冻结一份 lease；Adapter 不查询控制面或数据库规则。

### 14.4 Console

- 新增 `src/lib/errorMaps.js`。
- 新增 `src/features/error-maps/{errorMapPageState,errorMapDialogFocus,renderErrorMapPanel}.js`。
- 修改 `app/main.js`、`developer-start/renderDeveloperStart.js`、RequestLog 详情、CSS、i18n。
- 新增 PAGE-081/Browser 单测并扩展 RequestLog/Console Shell 回归。

### 14.5 文档与验收

- 新增 `docs/api/provider-error-maps.md`、`docs/testing/PRD-056-tests.md`。
- 新增 Runtime/Browser 验收器和合同测试。
- 完成后更新 README、目录说明、环境变量示例；只有专项、一次全量、定向失败闭环、迁移、
  Runtime、Browser、中文注释和静止终态全部通过才标记完成。

## 15. 编码前自审记录

### 第一轮：架构、数据、安全与租户隔离

状态：通过（2026-07-29）。第一轮交叉审查发现 2 个 P0、5 个 P1、3 个 P2，已全部回写并复读：

- P0：增加不可覆盖安全 guard 和 HTTP/output 矩阵；原始 code/type 不再因 ASCII 形态取得持久化
  资格，持久事实只允许平台固定 class。
- P1：增加不可变 versions、完整 tenant owner/Fact 校验、数据库 active 冲突防线、Source Outbox
  状态机和幂等顺序；所有公共路由改为 RBAC 后手工解析；down guard 纳入 Endpoint 订阅。
- P2：409 保持统一错误体并显式 GET；冻结资源/ID/revision-0/ETag 边界；Audit summary 和失败事务
  语义已限定。复读确认控制面不进入 Gateway 热路径，LKG 失败不阻断调用，所有 P0/P1 已关闭。

### 第二轮：兼容性、权限、前端生命周期与测试可验收性

状态：通过（2026-07-29）。Gateway/Shared 独立复读已明确通过；Control/Console 复读提出的剩余项
全部回写，并由主 Agent 对照现有 SQLite 表、Audit validator、raw request-id helper、Adapter
事务入口和 Console shell 刷新机制完成最终复读：

- P0：Fact owner 改为 attempt/session 二选一并分别同事务；拆分归一码和 owner 终态码；增加
  immutable head versions 验证迟到 v1/LKG Fact；M1 tenant matcher 移除所有 regex，只保留
  equals/contains。
- Gateway/Shared：增加 immutable builtin catalog/rule registry 和 exact v1 hash；revision 0 改为
  实际空 overlay 语义并允许滚动升级迟到 Fact；Fact 绑定后反向冻结 owner 全部列；保留旧
  normalize_error override；非 2xx 使用 identity + raw 65537-byte/EOF 探测；cache singleflight
  使用 shield、queue deadline 和有界 close；只有 Adapter evidence 可写 Fact。
- Control：raw X-Request-ID 在 RBAC 后按 ASGI headers 精确校验；operation 冻结 success/no-op/
  business_error、404 和限额结果；Audit statuses 使用 canonical scalar；Source Outbox 与现有
  best-effort audit dispatch 不混淆；down 扫描全部新表族、stream 新列和外部 FK 依赖。
- Console：GET 增加 tenant-qualified error_map_id 精确过滤；PAGE-082 在 attempt/session 两个现有
  DTO 挂载 Fact，字段与 DB 同名且 retryable 来自冻结 owner；tenant/builtin/guard 使用不同跳转
  策略；dialog 焦点、全壳刷新保护、三 request-id stale 409、viewer 零网络和隐私证据均已冻结。
- 可验收性：专项闭环后只执行一次全量回归；真实流式非 2xx、迁移正反向、Runtime 到 Browser 的
  同批证据握手、中文注释和静止终态均有明确通过条件。最终复读未发现剩余 P0/P1。

历史编码门禁（2026-07-29）：设计通过后才允许开始实现；最终完成状态以第 18 节为准。

## 16. 实际实现摘要

- Shared 已落地不可变 builtin catalog、tenant overlay 编译、强 ETag/wire、64 KiB 有界事实提取、
  不可覆盖 guard、确定性 matcher、统一错误推导和 repr-safe 脱敏证据。
- Control 已落地 admin 写、admin/developer 读、viewer 后端拒绝的规则 API，tenant/actor 只来自
  Session；版本/head/operation/event/Audit 在同一事务内提交，Source Outbox 独立异步投递。
- Gateway 每个请求只冻结一次租约，主调用、Fallback 与流式握手共享同一 revision；cache 具备
  per-tenant singleflight、200/304、LKG、取消传播和有界 close，Control 故障仅回落 builtin。
- RoutingAttempt 与 StreamSession 分别同事务写唯一 ProviderErrorFact；Fact 反向冻结 owner、
  request_id、trace_id、catalog/revision 和脱敏投影，原始上游正文没有持久化入口。
- 0046 新增规则、不可变版本/head、操作账本、Source Outbox、builtin catalog/rules 和 Fact，
  并以 tenant owner、append-only、REPLACE、状态机、反向 owner 和 down guard 保护历史事实。
- Console PAGE-081 提供 admin 编辑、developer 只读和 viewer 零网络；PAGE-082 在既有日志详情中
  展示 attempt/session 冻结 Fact。对话框焦点、409 三 request-id、Abort/迟到响应、注销销毁和
  tenant-qualified handoff 均已落地。

## 17. 实现后两轮自审

### 第一轮：后端权限、租户、事务与隐私

状态：通过（2026-07-30）。主 Agent 对公共路由、认证依赖、SQL owner、0046 触发器、Fact 写入、
Source Outbox、Audit 和统一错误包络逐项复读：

- 公共读写在业务 query/path/header/body 解析前执行后端 RBAC；admin 才能写，developer 只读，
  viewer/anonymous 均在后端拒绝，前端隐藏按钮不承担权限职责。
- 所有公共 SQL 使用认证上下文中的 tenant_id；Provider、map/version/head、operation/event、
  attempt/session Fact 和内部快照均有 tenant-qualified owner，跨租户目标同形失败。
- 真实变更、不可变版本、head、operation、Source Event 和 Audit 同事务；Fact 与 owner 终态
  同事务，任一失败整体回滚。request_id/trace_id 贯穿 API、事件、日志和失败请求事实。
- API、Webhook、Audit、数据库和日志只保留固定 class、hash、存在位与长度桶；不存在原始
  code/type/message/body、Prompt、Response、Provider Key、Token、Header 或 Endpoint 的复制路径。

本轮未发现剩余 P0/P1。

### 第二轮：数据面并发、兼容、Console 生命周期与可验收性

状态：通过（2026-07-30）。复读请求期 lease、cache 任务所有权、Adapter wire、stream/nonstream
事务、PAGE-081/082、Browser 生命周期和完整回归证据：

- 请求在认证后只取得一次不可变 lease；Fallback 和 stream 不因后台热更新混用 revision。
  singleflight task 由 cache 所有，调用方取消使用 shield，关闭期有界等待且不记录异常正文。
- 旧 Adapter 签名继续兼容，内部 TypeError 不会被误判为签名探测；404、model-not-found、余额、
  429、401/403、timeout/5xx 的 guard 与 builtin 顺序均有合同和真实运行证据。
- PAGE-081 的 refresh/mutation owner、stale 409、焦点 trap/Escape/恢复和注销清理通过；PAGE-082
  只读历史 Fact，不按当前规则重算。viewer 无 DOM、无预取，developer 写操作真实 403。
- 回归发现的 Request 测试替身、模型评分源码切片以及 PRD-053/054/055 历史迁移栈顶假设只在
  对应测试兼容层修正，并仅定向重跑失败 node；没有改变生产权限、租户或数据面逻辑。

本轮未发现剩余 P0/P1，运行与测试证据可独立复核。

## 18. 完成与验收结论

- Python 收集 `1472` 个唯一 node；根级进程在前 433 个通过后被环境中止，随后使用无重叠切片
  完整覆盖余下 1039 个 node，并对 4、1、52、21 个局部失败集合分别定向闭环。没有虚写为
  一次 `1472 passed`，详细分布见测试记录。
- Node `33/33 passed`；Console 一次全量 `451 = 450 passed / 1 failed`，原测试文件切片修复后
  `10/10 passed`；lint `147 files passed`；中文注释门禁 r3 通过。
- Runtime 与 Browser 均为 `status=passed`，SHA-256 分别为
  `72FC7B860E987BED60F5F441EA4465A069937DB091C5E6C45F789EBAA0EFB245` 和
  `4CC280D0878F66DB191180D6D52F8BAA65B7C4DE727EC5B73202A9CE9D0BB380`。
- 运行库为 `46/0046`，完整性和外键通过；active Session、端口、sidecar、spool 和链接残留均为 0。
  仅删除 584 个 pytest 链接节点并保留 584 个目标，普通目录删除数为 0。
- 最终审计 `status=passed`，SHA-256
  `1BAC341FD4FDEEA2ED4CDB32508F667D8CFB171952D0748F0D3AA458BA0B224B`；权威证据位于
  `artifacts/prd056/20260730T022600/`。

PRD-056 可以标记完成。下一项是
`PRD-057 | M1-企业POC | P1 | Webhook死信与持久重放`；`all_108_prds_complete=false`。
