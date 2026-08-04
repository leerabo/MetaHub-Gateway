# PRD-063 - 国内模型 Endpoint 适配规范

状态：**已完成；设计、两轮编码前自审、实现、两轮实现后复审、迁移、测试和实际运行验收均通过**。

## 1. 权威来源与需求理解

### 1.1 PRD 身份

| 字段 | 权威值 |
| --- | --- |
| PRD_ID | `PRD-063` |
| 里程碑 | `M1-企业POC` |
| 优先级 | `P1` |
| 能力层 | 核心层 |
| 模块 | 国产 Provider 适配 1 |
| 名称 | 国内模型 Endpoint 适配规范 |
| 角色 | 研发 / 管理员 |
| 页面 | `PAGE-090 适配器文档页` |
| 内部动作 | 内部 Adapter 接口 |
| 数据对象 | `ProviderAdapterSpec` |
| 事件 | `adapter.spec_updated` |
| 依赖 | Adapter 抽象 |

上述信息由 Excel `01_PRD总表!A64:R64` 与 `02_M0-M1核心开发!A64:R64` 交叉确认，
并同时核对 `04_页面清单!PAGE-090`、`05_API接口清单!API-288`、
`06_数据对象!OBJ-085`、`07_Webhook事件!EVT-070` 和 `08_验收用例!TC-0125/0126`。

### 1.2 目标

本 PRD 把国内模型 Endpoint 的接入要求从多处硬编码收敛为一个可校验、可展示、可版本化的
`ProviderAdapterSpec` 目录。目录必须覆盖：

1. 智谱、通义、文心、火山、混元、DeepSeek 官方或云服务；
2. 客户本地 OpenAI-compatible Endpoint；
3. 客户自管的通用 Endpoint；
4. 仅作为竞品或迁移兼容对象的聚合平台入口。

每个 spec 至少声明鉴权、模型 ID 映射、usage 解析、错误归一化、流式格式、价格字段、
Adapter 版本和最小契约用例。Gateway、Control API、健康探测和 Console 必须引用同一共享目录，
不能继续维护彼此独立的 Provider 类型集合。

### 1.3 产品分类边界

| 分类 | Provider 类型 | 产品语义 |
| --- | --- | --- |
| 模型厂家 / 云模型服务 | `zhipu`、`dashscope`、`wenxin`、`volcengine`、`hunyuan`、`deepseek` | 默认上游候选 |
| 客户本地兼容 Endpoint | `openai_compatible` | 客户部署或客户指定的 OpenAI-compatible 服务 |
| 客户自管 Endpoint | `custom` | 由客户负责协议与连通性配置，不冒充模型厂家 |
| 竞品 / 迁移兼容平台 | `migration_compatible` | 可选迁移入口；OpenRouter、硅基流动只能出现在本分类说明中 |

`openrouter`、`siliconflow`、`silicon_flow` 不成为 Provider 类型，也不得出现在模型厂家集合、
默认推荐或国内厂家数量中。它们仍由现有 Provider 创建接口失败关闭为 `provider.type_invalid`。

### 1.4 现有能力与缺口

可复用能力：

- PRD-013 已有 `ProviderAdapter`、`ProviderRequest`、`NormalizedResponse/Usage/Error`、
  OpenAI-compatible Adapter 和通用 Adapter；
- PRD-032 已有多 usage dialect、Provider 成本与内部 PricePlan 分离、raw usage 双重脱敏；
- PRD-056 已有不可变错误基线、租户覆盖层、请求期 lease 和安全错误事实；
- PRD-061 已有 Provider 页面组合权限、租户过滤、导航 owner、Abort/generation 生命周期；
- PRD-062 的配置导入已复用 Provider 类型与认证归一函数。

真实缺口：

1. Provider 类型分别硬编码在 Control、健康探测、状态页、Gateway 工厂和 Console，可能漂移；
2. 六类国内 Provider 目前全部落到同一个 Adapter，但没有版本、协议、鉴权、usage、流式和价格声明；
3. Gateway 对未知类型回退通用 Adapter，违反“无 spec 不得接入”的失败关闭要求；
4. `custom_header` 在控制面可配置，Gateway 与健康探测却始终发送 Bearer，公开合同与执行不一致；
5. 流式请求无条件注入 `stream_options.include_usage`，没有由 Provider spec 声明；
6. 不存在 `ProviderAdapterSpec`、租户激活事实、`adapter.spec_updated` 或 PAGE-090；
7. 现有 Adapter 测试没有逐一证明六类国内 Provider 都有 spec 和最小契约。

### 1.5 本 PRD 范围

- 建立 Shared SDK 不可变 spec 注册表、稳定 canonical JSON、版本与 SHA-256 hash；
- 为九个现有允许类型补齐 spec；六类国内上游必须逐一列出；
- Gateway 工厂、请求构造、Control Provider 校验、健康探测和状态筛选引用共享注册表；
- 新增租户级 catalog 激活事实和受内部服务认证的幂等激活接口；
- 新增当前用户只读 spec API 与 PAGE-090；
- 为每个现有 spec 补最小契约证明，覆盖请求、usage、错误与流式声明；
- 注册 `adapter.spec_updated`，并只在后续真实 catalog 版本切换时触发；0055 bootstrap 不伪造更新；
- 补齐迁移、主流程、异常、权限、租户、隐私和真实运行验收。

### 1.6 明确不做

- 不提前实现 PRD-064 的通用契约测试框架、全天候真实厂商探测或 CI 外部网络任务；
- 不从公网抓取厂商价格、模型目录或文档；
- 不把原生签名协议、Responses API、音视频计费或复杂阶梯价格塞入 M1；
- 不允许租户管理员编辑可执行 Adapter 代码或任意上传插件；
- 不让 Gateway 热路径逐请求访问 Control API 或数据库读取 spec；
- 不保存 Provider Key、Token、Prompt、Response、Authorization 或真实客户 Endpoint 到 spec/event 日志；
- 不把 OpenRouter、硅基流动提升为默认上游或模型厂家。

## 2. 涉及模块

| 模块 | 目录 | 设计修改 |
| --- | --- | --- |
| 共享 spec | `shared-sdk/python/metahub_shared/runtime/provider_adapter_spec.py` | 数据结构、九类目录、hash、查询与校验 |
| Shared 导出 | `shared-sdk/python/metahub_shared/runtime/__init__.py` | 仅导出稳定公共合同 |
| Gateway Adapter | `gateway-data-plane/src/metahub_gateway/providers/` | 工厂失败关闭、版本绑定、鉴权与流式选项按 spec 构造 |
| Gateway 路由 | `gateway-data-plane/src/metahub_gateway/routing/models.py` | 延续可信 tenant/provider/auth 路由快照，不接收客户端 spec |
| Provider 管理 | `control-plane-api/src/metahub_control/providers/service.py` | 类型及 auth 组合引用共享目录 |
| 配置迁移 | `control-plane-api/src/metahub_control/config_transfer/service.py` | create-only 导入复用相同组合门禁 |
| 健康与状态 | `control-plane-api/src/metahub_control/provider_health/`、`provider_status/` | 类型、探测 profile、鉴权 Header 引用共享目录 |
| Spec API | `control-plane-api/src/metahub_control/adapter_specs/` | 用户只读目录与内部激活服务 |
| 认证与 App | `control-plane-api/src/metahub_control/auth/`、`app.py` | 用户 `provider.health.read` 与内部 Token 两套身份边界 |
| Webhook | `shared-sdk/python/metahub_shared/runtime/webhooks.py` | `adapter.spec_updated` exact payload |
| 错误目录 | `shared-sdk/python/metahub_shared/runtime/errors.py` | Control/Gateway 稳定错误码 |
| 数据库 | `infra/migrations/0055_provider_adapter_specs.sql/.down.sql` | tenant-scoped 激活事实、索引、owner/不可变约束 |
| Console client | `console-web/src/lib/adapterSpecs.js` | 只读 API、严格响应与 Abort |
| PAGE-090 | `console-web/src/features/adapter-specs/` | 分类与六维 spec 文档页 |
| 导航 | `control-plane-api/src/metahub_control/console/`、`console-web/src/main.js` | 后端 owner、页面加载与生命周期 |
| 测试/验收 | Shared、Gateway、Control、Console、`scripts/acceptance/` | 专项、直接回归、迁移、Runtime 与 Browser |

## 3. ProviderAdapterSpec 设计

### 3.1 不可变共享结构

`ProviderAdapterSpec` 使用 frozen dataclass，只包含可公开的机器合同：

| 字段 | 类型 | 约束与说明 |
| --- | --- | --- |
| `spec_id` | string | 稳定 ID，不含租户或凭据 |
| `provider_type` | string | 九类允许值内唯一 |
| `display_name` | string | 页面展示名，固定安全文案 |
| `classification` | enum | `model_vendor_cloud/local_compatible/customer_managed/migration_platform` |
| `adapter_name` | enum | `openai_compatible/generic_http` |
| `adapter_version` | string | 语义版本；执行和页面均展示 |
| `protocol_family` | enum | M1 仅 `openai_chat_completions` 或 `generic_json_sse` |
| `allowed_auth_types` | tuple | `none/bearer` 的有序子集；`custom_header` 在 M1 只保留数据库兼容枚举，不进入可执行 spec |
| `credential_header` | string/null | `Authorization` 或 null；不得来自租户自由输入 |
| `credential_scheme` | string/null | Bearer 前缀或无前缀 |
| `model_id_strategy` | enum | `provider_model_id` 或 `endpoint_or_model_id` |
| `usage_profile` | object | usage 根路径、输入/输出/缓存/总量字段族 |
| `error_profile` | object | 固定引用 PRD-056 builtin + tenant overlay，不复制规则正文 |
| `stream_profile` | object | SSE 方言、终止标记、是否允许 `include_usage` |
| `price_fields` | tuple | 价格表字段白名单，不包含实际价格 |
| `probe_profile` | enum | `openai_models` 或 `generic_reachability` |
| `platform_examples` | tuple | 仅 migration 分类可包含 OpenRouter、硅基流动 |
| `status` | enum | `active/optional`；migration 只能 optional |
| `contract_case_id` | string | PRD-063 最小契约夹具 ID；PRD-064 将扩展为通用框架 |

目录对象按 `provider_type` 排序后转为 canonical UTF-8 JSON，计算 `catalog_hash`。模块导入时验证：

- spec/type/contract ID 唯一；
- 枚举、长度、控制字符和 URL/秘密类字段边界；
- 六类国内上游完整；
- migration 分类恰好是 optional，且竞品名称不出现在厂家分类；
- Adapter、鉴权、stream、usage、price、error profile 字段完整；
- canonical byte length 和 hash 与冻结常量一致，漂移必须显式升级 catalog version。

### 3.2 九类初始目录

| provider_type | 分类 | Adapter | auth | 模型映射 | 流式 |
| --- | --- | --- | --- | --- | --- |
| `zhipu` | 模型厂家/云服务 | OpenAI compatible | Bearer | provider model ID | OpenAI SSE + final usage |
| `dashscope` | 模型厂家/云服务 | OpenAI compatible | Bearer | provider model ID | OpenAI SSE + final usage |
| `wenxin` | 模型厂家/云服务 | OpenAI compatible | Bearer | provider model ID | OpenAI SSE + final usage |
| `volcengine` | 模型厂家/云服务 | OpenAI compatible | Bearer | endpoint 或 model ID | OpenAI SSE + final usage |
| `hunyuan` | 模型厂家/云服务 | OpenAI compatible | Bearer | provider model ID | OpenAI SSE + final usage |
| `deepseek` | 模型厂家/云服务 | OpenAI compatible | Bearer | provider model ID | OpenAI SSE + final usage |
| `openai_compatible` | 客户本地兼容 Endpoint | OpenAI compatible | none/Bearer | provider model ID | OpenAI SSE + final usage |
| `custom` | 客户自管 Endpoint | Generic HTTP | none/Bearer | provider model ID | generic JSON/SSE；不强制私有字段 |
| `migration_compatible` | 竞品/迁移平台 | Generic HTTP | Bearer | provider model ID | generic JSON/SSE；optional |

M1 只承诺这些类型在客户选择其 OpenAI-compatible 或通用 JSON/SSE Endpoint 时的统一合同；
不宣称已实现每家厂商所有原生签名协议。页面必须明确这个限制。

### 3.3 鉴权合同

- `none`：不选择 Provider Key，也不生成认证 Header；只允许本地兼容或 custom spec；
- `bearer`：从当前租户 KeyPool 选择并短暂解密，写入 `Authorization: Bearer ...`；
- `custom_header`：现有数据库枚举没有与之配套的可信 Header 名或签名 profile；M1 保留历史读取兼容，
  但 Provider 新建、更新、配置导入、健康探测和 Gateway 建连一律返回 `provider.auth_not_supported`
  或对应 Gateway 错误，且不得先按 Bearer 试发；
- Provider type/auth 组合在 Control 创建、更新、配置导入、健康探测和 Gateway 建连前分别校验；
- 任何不匹配都在网络 I/O 前失败，不尝试回退另一种鉴权。

### 3.4 Usage、错误与价格合同

- Usage 复用 PRD-032 的 `usage/usage_metadata/usageMetadata/output.usage` 优先级和脱敏器；
- spec 只声明允许 dialect 与字段族，不能把整个响应作为 raw usage；
- 错误 profile 只引用 PRD-056 的 builtin catalog version、当前租户 overlay lease 和安全分类；
- 价格字段固定为 `input_price_per_1k`、`output_price_per_1k`、`currency`、
  `price_schema_version`；spec 不存实际价格，也不覆盖 PricePlan；
- 流式 final usage 缺失仍按现有估算/账本口径追踪，不能伪造 Provider 自报 usage。

## 4. 数据库迁移设计

### 4.1 全局目录与租户事实分离

Shared spec 是发布制品，不是租户可编辑业务数据，因此不把九份大 JSON 复制进每个租户数据库。
数据库只保存“某租户激活了哪个冻结目录版本”的业务事实，所有事实均带 `tenant_id`。

表：`provider_adapter_catalog_activations`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `activation_id` | TEXT | PK | UUID |
| `tenant_id` | TEXT | NOT NULL, FK tenants | 租户边界 |
| `catalog_version` | TEXT | NOT NULL | 冻结目录版本 |
| `catalog_hash` | TEXT | NOT NULL | 64 位小写 SHA-256 |
| `spec_count` | INTEGER | NOT NULL | 必须为目录实际数量 |
| `status` | TEXT | active/superseded | 每租户最多一个 active |
| `request_id` | TEXT | NOT NULL | 激活幂等与追踪 |
| `trace_id` | TEXT | NOT NULL | 跨服务链路 |
| `activated_by` | TEXT | NOT NULL | 固定内部服务 actor ID |
| `activated_at` | TEXT | NOT NULL | UTC |
| `superseded_at` | TEXT | NULL | 被新版本替换时间 |
| `created_at/updated_at` | TEXT | NOT NULL | UTC |

约束与索引：

1. unique `(tenant_id, request_id)`，同 request 重放必须返回原事实；
2. partial unique `(tenant_id) WHERE status='active'`；
3. 索引 `(tenant_id, catalog_version, status)`；
4. owner trigger 确保 tenant 存在；immutable trigger 禁止修改 version/hash/count/request/trace/actor/time；
5. 激活只允许同事务把旧 active 改为 superseded，再插入新 active；普通 SQL 不能改写历史核心字段；
6. 0055 为现有租户写入当前 catalog 激活事实；新 tenant trigger 写同一初始激活；
7. migration 测试必须逐字段核对 seed version/hash/count 与 Shared 目录。

### 4.2 Down

- 只有全部租户仍只有 0055 初始激活、没有后续发布事实时允许 down；
- 存在非初始版本、同租户多条历史或未知后续依赖时，在 DDL 前阻断；
- 可回滚时删除新 tenant seed trigger、索引、trigger 和表，不改 Provider、Usage、Audit 或 Webhook 事实；
- down/re-up 必须恢复完全相同的初始激活。

## 5. API 设计

### 5.1 GET /adapter-specs

用户只读接口，依赖现有后端 `provider.health.read`。该权限已经授予默认 Developer、OpsAdmin 和 Admin，
符合“研发 / 管理员”persona，且不会把 Provider 写权限开放给页面。tenant 只能来自 Bearer Session 的实时权限快照；
请求体、query 和 Header 均不能选择 tenant。

允许唯一 query：

- `classification`：四个固定分类之一；
- `status`：`active/optional`。

响应：

```json
{
  "catalog": {
    "version": "adapter-spec-v1",
    "hash": "<sha256>",
    "activated_at": "2026-07-31T00:00:00Z"
  },
  "specs": [
    {
      "spec_id": "adapter-spec-deepseek-v1",
      "tenant_id": "default",
      "provider_type": "deepseek",
      "display_name": "DeepSeek 官方或云服务",
      "classification": "model_vendor_cloud",
      "adapter_name": "openai_compatible",
      "adapter_version": "1.0.0",
      "protocol_family": "openai_chat_completions",
      "allowed_auth_types": ["bearer"],
      "model_id_strategy": "provider_model_id",
      "usage_profile": "openai_compatible_v1",
      "error_profile": "provider_error_map_v1",
      "stream_profile": "openai_sse_final_usage_v1",
      "price_fields": [
        "input_price_per_1k",
        "output_price_per_1k",
        "currency",
        "price_schema_version"
      ],
      "status": "active"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

API 先读取当前 tenant 的 active 激活，再与进程内 catalog version/hash/count 精确比较；缺失或漂移返回
503，不能展示未激活的新代码目录。响应不返回真实 base URL、Credential、错误正文或客户配置。
共享异常响应层按 `/adapter-specs` 和 `/internal/adapter-specs` 路径统一设置 `Cache-Control: no-store`，
不能只在成功路由对象上补 Header，否则权限、校验和 5xx 分支会被浏览器缓存。

### 5.2 POST /internal/adapter-specs/{tenant_id}/activate

只允许 `X-Internal-Token`，不得混用用户 `Authorization`，不得带 query。请求体严格为：

```json
{
  "catalog_version": "adapter-spec-v1",
  "catalog_hash": "<sha256>"
}
```

规则：

1. 在解析 tenant 事实前完成常量时间内部 Token 校验；
2. body 必须与当前进程目录精确一致，调用方不能上传 spec JSON；
3. tenant 不存在统一返回 `adapter.spec_tenant_not_found`，不泄露其他租户对象；
4. 使用未经中间件改写的严格 `X-Request-ID` 作为幂等键；认证、wire 和 tenant 校验后先查询历史 request：
   同 request ID + 同 version/hash 返回原 activation，即使当前代码已经升级；同 request ID + 不同 body 返回冲突；
5. 只有没有历史 request 的新调用才把 body 与当前进程 catalog 精确比较；
6. 已激活相同目录返回 `changed=false`，不伪造更新事件；
7. 新目录在一个事务内 supersede 旧 active 并插入新事实；提交后触发
   `adapter.spec_updated`；Webhook 失败不回滚激活，但必须记录安全失败摘要；
8. Webhook 异常日志只写固定 `webhook.delivery_unavailable`、tenant、activation、request/trace ID，
   不记录 exception string、Endpoint、响应正文、签名或 traceback；
9. 响应统一带 request_id/trace_id 和 `Cache-Control: no-store`。

### 5.3 内部 Python 接口

```python
def provider_adapter_spec(provider_type: str) -> ProviderAdapterSpec: ...
def provider_adapter_specs() -> tuple[ProviderAdapterSpec, ...]: ...
def provider_types(*, classification: str | None = None) -> frozenset[str]: ...
def validate_provider_auth(provider_type: str, auth_type: str) -> ProviderAdapterSpec: ...
def catalog_identity() -> ProviderAdapterCatalogIdentity: ...
```

未知类型、损坏 catalog 或 auth 不匹配抛固定 Shared 领域异常，由 Control/Gateway 映射到各自错误码；
不得把 Python `KeyError`、Header、Endpoint 或内部 canonical JSON 直接暴露。

## 6. PAGE-090 前端设计

### 6.1 导航与权限

- 后端 navigation 新增 `adapter-specs` owner，标题“Adapter 规范”，要求 `provider.health.read`；
- 前端只有收到该 owner 且当前权限指纹仍含 `provider.health.read` 时加载；
- Viewer、权限被撤销、tenant/token/session 切换、登出或 owner 消失时立即 Abort 并清空 DOM/state；
- 页面隐藏不是授权边界，手写 GET 仍由 Control API 返回 `auth.permission_denied`。

### 6.2 信息架构

页面采用紧凑工作台布局，不做营销 Hero 或嵌套卡片：

1. 标题“国内模型 Endpoint 适配规范”和当前 catalog version/hash 短摘要；
2. 分类 tabs：默认上游、客户 Endpoint、迁移兼容；
3. 筛选：Provider 类型、状态；
4. 规范表：厂家/类型、Adapter 版本、鉴权、模型 ID、Usage、错误、流式、价格字段；
5. 行展开详情显示协议限制与 contract case ID；
6. migration 分类固定显示“竞品/迁移兼容平台，不是默认模型厂家”，并列出 OpenRouter、硅基流动；
7. loading、空态、403、503 和一般错误均保留 request_id，错误文案不拼接后端自由文本。

桌面 `1280x720` 与移动 `390x844` 均不能横向溢出；移动端把宽表变成标签和值的单列行，
按钮和筛选保持稳定尺寸。所有动态文本 HTML 转义，页面不展示或缓存真实 Provider Key、Token、Prompt、
Response、Authorization、客户 base URL。

## 7. 错误码设计

| 错误码 | HTTP | 边界 |
| --- | ---: | --- |
| `adapter.spec_catalog_unavailable` | 503 | 当前租户无 active 激活或与代码目录不一致 |
| `adapter.spec_catalog_invalid` | 503 | 共享目录自身结构/hash 损坏 |
| `adapter.spec_filter_invalid` | 422 | classification/status 非白名单 |
| `adapter.spec_tenant_not_found` | 404 | 内部激活目标 tenant 不存在 |
| `adapter.spec_activation_conflict` | 409 | request ID 与既有激活事实不一致 |
| `adapter.spec_activation_invalid` | 422 | 激活 body 非当前 version/hash 或字段非法 |
| `provider.adapter_spec_missing` | 422 | Control 创建/更新/导入的 Provider 类型无 spec |
| `provider.auth_not_supported` | 422 | provider_type 与 auth_type 组合不支持 |
| `gateway.provider_adapter_spec_missing` | 503 | 路由中的 Provider 类型无可执行 spec |
| `gateway.provider_adapter_spec_invalid` | 503 | spec 与 Adapter 名称/版本不匹配 |
| `auth.permission_denied` | 403 | 用户缺少实时 `provider.health.read` |
| `auth.internal_token_required/invalid` | 401/403 | 内部激活身份失败 |

所有错误继续由统一中间件返回 `code/message/suggestion/request_id/trace_id`；
message/suggestion 使用固定目录，不回显 provider_type 原始脏值、secret、URL 或数据库异常。

## 8. 事件设计

`adapter.spec_updated` 只表示已经有 active 版本的租户切换到另一个真实目录版本。0055 首次 bootstrap、
新租户 bootstrap、查看 PAGE-090、筛选和幂等同版本激活都不触发；不能把迁移 seed 或页面访问伪装成更新。

exact payload：

```json
{
  "catalog_version": "adapter-spec-v2",
  "catalog_hash": "<sha256>",
  "previous_catalog_version": "adapter-spec-v1",
  "spec_count": 9,
  "activated_at": "2026-07-31T00:00:00Z"
}
```

标准 envelope 携带 `event_id/tenant_id/timestamp/actor_id/object_id/trace_id/payload`；
actor 固定为内部发布服务，object 为 activation ID。validator 拒绝额外字段、非法 hash、未来时间、
数量越界和 tenant/object 漂移。payload 不包含 spec 全文、Endpoint、Credential、Header 或厂商错误正文。

## 9. 安全、权限与兼容性约束

1. Gateway 只读取进程内冻结目录；Control 激活事实不能在请求中注入可执行字段；
2. Provider 类型与 auth 组合在管理、导入、探测和数据面四个入口一致失败关闭；
3. Gateway Adapter 仍只接收已解析的 `GatewayRoute.tenant_id`，不信任客户端 body 的 tenant；
4. 用户 spec API 只使用 Session tenant；内部接口只使用通过服务 Token 后的 path tenant；
5. spec 表和所有查询以 tenant_id 为首条件，跨租户 activation ID 不可读取；
6. Provider Key 只在选中后短暂进入 repr=False 的请求 Header；日志、异常、event 和 API 均不包含明文；
7. `custom_header` 在没有受版本控制的 Header profile 前失败关闭；未知 auth 不回退 Bearer；
8. OpenRouter/硅基流动只在 migration 文案和固定 examples 中出现；不能成为后端允许类型；
9. 现有 `openai_compatible + none/bearer` 和国内六类 Bearer 路径保持兼容；
10. 对历史不兼容 `provider_type/auth_type`，0055 不静默改数据；Provider 更新、导入和 Gateway 建连返回统一错误，
    文档给出修复路径；实际开发库迁移前先只读核对现存组合；
11. 不扩大 Provider、Usage、Request Log、Error Map 或 Status 页的可见权限；
12. 测试和运行产物只进入 `D:/MetaHub/.artifacts/prd063-*`，pytest 禁用 cacheprovider 并显式 basetemp；
    不读取、枚举、写入或删除 `D:/MetaHub/.tmp`，不使用 `C:/tmp`，不提权。
13. 历史 `custom_header` 行不由迁移静默重写或删除；列表和状态页仍可显示安全元数据，但更新、导入、
    健康联网与 Gateway 调用失败关闭，并给出固定修复建议。当前开发库只读核对仅存在
    `openai_compatible + bearer/none`，不会被该收紧阻断。
14. PAGE-090 使用独立状态和请求生命周期；spec API 失败不能清空 Provider 状态页、用量或其他已激活页面状态。

## 10. 测试用例

### 10.1 Shared 注册表

- 九类唯一、稳定排序、canonical length/hash/version；
- 六类国内上游完整且 classification 正确；
- migration optional，竞品名称不进入厂家集合；
- 每项 auth/model/usage/error/stream/price/contract 字段完整；
- 重复类型、未知枚举、控制字符、秘密类字段、非法 Header、hash 漂移在导入时失败关闭；
- 公共 DTO repr/JSON 不出现凭据或 Endpoint。

### 10.2 Gateway 最小契约

- 九类工厂选择与 spec 一致，未知/损坏类型不发网络请求；
- 六类国内上游逐一证明 model 替换、Bearer Header、Adapter version；
- `openai_compatible none/bearer` 精确 Header；`custom_header` 在建连前失败且网络调用为 0；
- stream option 只按 spec 注入；原 payload 不被原地修改；
- OpenAI、通用、Gemini usage、缓存 Token、CNY/非 CNY、缺失估算与 raw usage 脱敏；
- 401/403、429、quota、model/resource 404、5xx、timeout 复用 PRD-056 固定归一；
- 日志不含 Key、Authorization、Prompt、Response、base URL 或上游自由错误文本。

### 10.3 Control API / 权限 / 租户

- admin/developer/ops 中实际拥有 `provider.health.read` 的用户可 GET；Viewer/匿名 403；
- classification/status 过滤、重复/未知 query、tenant_id query 拒绝；
- 两租户激活不同历史时只返回各自 active，跨租户 activation 不可见；
- 激活接口缺 Token、错 Token、混用 Bearer、未知 tenant、错误 version/hash 全部失败；
- 同 request ID 同 body 在代码升级后仍可重放原事实，不同 body 冲突；同版本新 request `changed=false` 不发事件；
- 新版本原子 supersede、事件 exact payload、Webhook 失败不回滚；
- Provider create/update/config import 的 type/auth 组合统一；
- 成功及错误均带 request_id/trace_id/no-store。

### 10.4 0055 迁移

- fresh 迁移、0054 up、重复执行、checksum、连续版本；
- 所有现有 tenant 初始激活，新 tenant trigger 激活；
- unique/owner/immutable/active 约束和查询索引；
- version/hash/count 与 Shared catalog 精确一致；
- 空后续事实 down/re-up；存在新版激活时 DDL 前阻断且事实保留；
- `integrity_check=ok`、`foreign_key_check=0`。

### 10.5 Console

- client 只发送 Bearer 与白名单筛选，不发送 tenant、URL、Key 或 spec JSON；
- 后端 owner + `provider.health.read` 双条件；Viewer/匿名零 DOM；
- 三分类、九类、六个厂家、竞品边界、版本/hash、六维合同和限制文案；
- loading/empty/403/503/error/request_id；动态值全部转义；
- Abort/generation 覆盖筛选、离页、登出、tenant/token/revision/owner 变化；
- 1280x720、390x844 无重叠、横向溢出或文本截断。

### 10.6 实际运行验收

- 隔离数据库从 0054 实迁移到 0055并核对 checksum/trigger/integrity/FK；
- Control 登录后真实 GET spec；Viewer 真实 403；内部 Token 激活新测试目录并核对事件；
- 用本地无日志 stub 分别以 `deepseek` 与 `dashscope` 类型跑非流式、流式、429 和 usage；
- RequestLog/UsageLedger/错误事实均可按 request_id 追踪；
- Control/Gateway/Console 健康检查 HTTP 200；
- 浏览器双视口、网络、DOM、Storage、console/page error 与敏感 canary 扫描通过；
- 不访问公网，不用真实厂商 Key，不把 Prompt/Response 正文写入验收日志。

## 11. 代码修改计划

1. 完成本文两轮编码前自审并回写修正；
2. 实现 Shared spec 数据结构、目录校验、hash 和专项测试；
3. 新增 0055 up/down 与迁移专项；
4. 实现 adapter_specs models/service/routes、错误码、用户权限和内部激活；
5. 注册 `adapter.spec_updated` exact webhook，补幂等/失败测试；
6. Gateway 工厂、请求鉴权、stream option 和 Adapter version 改为读取 spec；
7. Control Provider、config import、health、status 去除重复类型集合并使用 spec；
8. 实现 PAGE-090 client/render/controller/navigation 与单测；
9. 运行 Shared、Gateway、Control、迁移、Console 专项及直接受影响回归；
10. 运行中文注释门禁和关键语法/lint；
11. 使用隔离 0055 数据库完成真实 Control/Gateway/Console/Stub 与双视口验收；
12. 更新 README、目录说明、API/开发文档和 `docs/testing/PRD-063-tests.md`；
13. 只有全部证据通过后才把本文状态改为完成，并开始 PRD-064。

## 12. 第一轮编码前自审

状态：**修正后通过（2026-08-01）**。

本轮按产品分类、热路径、Shared/tenant 边界、鉴权 Header、内部激活身份、数据库事务、
事件语义、密钥脱敏和 down 保真逐项复核。

| 发现 | 风险 | 修正 |
| --- | --- | --- |
| PAGE 初稿使用 `provider.read` | 默认 Developer 没有该权限，与 Excel 研发 persona 冲突 | 改用现有 `provider.health.read`；Admin/Developer/Ops 默认具备，Viewer 不具备，后端仍实时校验 |
| 初稿为 `custom_header` 自行指定 `X-API-Key` | 现有对象没有可信 Header 名，擅自定义会把错误合同变成可执行行为 | 初始 spec 只允许 none/Bearer；历史 `custom_header` 在所有联网入口前统一失败关闭，绝不回退 Bearer |
| 初稿笼统要求 0055 后“实际触发更新事件” | 首次 seed 并不是从旧版本切换，伪造 updated 会污染审计语义 | bootstrap 只写激活事实；仅已有 active 的真实 version/hash 切换触发 `adapter.spec_updated` |
| 全局 spec 若复制成租户 JSON | 多租户重复、漂移且可能被误解为租户可编辑执行代码 | Shared 保存冻结目录，数据库只保存 tenant-scoped activation identity |
| 激活接口若接受 spec JSON | 内部调用方可能注入 Gateway 行为 | body 只接受并核对当前 version/hash，不能上传目录；Gateway 永不读取该 body 或激活表作为执行代码 |
| Gateway 未知类型回退 Generic | 数据库损坏或漏 spec 时可能向错误上游发请求 | 工厂在网络前返回固定 503；所有允许类型必须在目录和最小合同中出现 |
| activation 普通 UPDATE 可能改写历史 | 版本、hash、request/trace 证据失真 | 核心字段 immutable；版本切换只允许 supersede 旧状态并新增 active，且同事务满足唯一 active |
| down 无事实门禁 | 回滚可能删除后续 catalog 发布证据 | 仅初始 bootstrap 可 down；非初始或多版本历史在 DDL 前阻断 |

修订后，Shared 目录、租户激活、用户权限、内部服务身份和 Gateway 执行边界互不替代；
没有发现剩余 P0/P1 架构或安全阻断项，可以进入第二轮兼容性复核。

## 13. 第二轮编码前自审

状态：**修正后通过（2026-08-01），可以进入编码**。

本轮基于第一轮修订版检查现有 Provider 兼容、PRD-064 范围隔离、request replay、权限撤销、
前端迟到响应、迁移升级/回滚、错误优先级和实际验收可执行性。

| 发现 | 风险 | 修正 |
| --- | --- | --- |
| 激活先校验当前 catalog 再查 request replay | 部署 v2 后，v1 的合法同 request 重放会被当作 body 过期拒绝 | 严格 wire/tenant 后先按 tenant+原始 request ID 查历史；同 body 返回原事实，只有新 request 才校验当前目录 |
| 只在成功 response 设置 no-store | 403/409/422/503 仍可能被浏览器或代理缓存 | 在共享异常响应层登记两个路径前缀，成功与错误统一 no-store |
| Webhook best-effort 若使用 `logger.exception` | HTTP 客户端异常可能把订阅 URL 或响应正文写入 traceback | 只记录固定错误码和安全 ID，不记录 exception string、`exc_info` 或 traceback |
| 收紧 custom_header 若迁移改写历史 | 客户配置会被静默改变且无法审计 | 0055 不改 Provider 行；历史元数据可读，所有执行入口统一失败并给出修复路径 |
| PAGE spec state 若复用 Provider state | 503 或迟到响应可能清空其他页面或跨登录态写入 | 独立 controller、Abort、generation、owner/tenant/token/revision 指纹，离页立即清空自身状态 |
| 最小契约测试若抽象成完整插件框架 | 会吞并紧随其后的 PRD-064 | PRD-063 只逐项参数化九个冻结 spec；通用 case 模型、CI 命令、真实探测和 contract_failed 留给 PRD-064 |
| 实际环境无法凭同一二进制制造“未来 catalog” | 为演示事件而放开任意 version 会破坏发布边界 | 集成测试用受控旧 activation + 当前 catalog 证明真实切换；Runtime 验收验证当前版本幂等及事件不误发，不增加测试后门 |
| 迁移后现有 auth 组合未知 | 收紧可能在上线后才暴露 | 编码前只读核对开发库；迁移专项构造兼容/历史不兼容两类，且文档明确不自动修复 |

两轮自审后，设计满足 Excel 的 spec、页面、事件和最小契约要求，并保持 PRD-064 独立。
编码必须严格按第 11 节顺序推进；专项、直接回归、0055 实迁移、Runtime 与 Browser 全部通过前，
不得把 PRD-063 标记完成。

## 14. 实现结果

### 14.1 Shared 与 Gateway

- Shared SDK 已发布九类不可变 `ProviderAdapterSpec`，冻结版本、canonical byte length 和 SHA-256；模块加载时即校验重复项、枚举、控制字符、敏感字段、Header 和目录漂移。
- 六类国内厂商、两类客户 Endpoint 与一个迁移入口均使用同一目录；OpenRouter、硅基流动只存在于迁移示例，不进入默认厂商集合。
- Gateway 工厂和 Adapter 直接实例化路径均绑定 spec；未知类型、auth 漂移、Adapter 名称/版本或 catalog hash 漂移在 Key 选择和网络 I/O 前失败关闭。
- `ProviderRequest` 携带 Adapter version/hash；OpenAI-compatible stream 只按 spec 注入 `include_usage`，不原地修改调用方 payload。
- Usage、缓存 Token、错误归一、失败请求和成本事实继续按 request/trace/tenant 追踪；日志不记录凭据、URL、Prompt、Response 或上游自由正文。

### 14.2 Control、事件与迁移

- 新增 `GET /adapter-specs` 和 `POST /internal/adapter-specs/{tenant_id}/activate`。
- 用户接口只使用 Session tenant 并实时校验 `provider.health.read`；内部接口只接受服务 Token 和 path tenant，拒绝混合身份、query、超限正文和额外字段。
- 同 tenant/request/body 可重放原事实，不同 body 返回 409；同版本新请求写不可变 no-op 收据但不发事件。
- Provider create/update/config import/test/probe 全部复用同一个 type/auth 校验；DNS 解析结果固定到实际 TCP 连接，阻断解析与连接之间的重绑定。
- `adapter.spec_updated` 仅在真实版本切换时生成；Webhook 补投保留原 `source_request_id`，投递失败不回滚已提交激活。
- 0055 建立 tenant-scoped activation、唯一 active、owner、不可变 trigger、索引、新租户 bootstrap 与保真 down guard。

### 14.3 Console PAGE-090

- 后端导航新增 `adapter-specs` owner；前端只有同时拥有该 owner 和 `provider.health.read` 才加载目录。
- PAGE-090 显示默认上游、客户 Endpoint、迁移兼容三分类及九类合同；迁移页明确 OpenRouter/硅基流动不是模型厂家或默认上游。
- API 客户端执行严格 DTO 白名单、有界字符串、唯一数组、有效时间、分类和目录身份校验；响应损坏时整批失败关闭。
- tenant/session/Token/权限 revision/owner/filter/generation 任一变化都会中止或淘汰旧响应；页面状态与 Provider Status 独立。
- 桌面宽表使用局部滚动，390px 移动端转换为单列字段组；动态文本和属性均转义。

### 14.4 文档与验收器

- `docs/api/provider-adapter-specs.md` 保存用户/内部 API、Gateway 合同、错误码、事件和新增 Provider 门禁。
- `scripts/acceptance/prd063-browser-acceptance.mjs` 固定 loopback Origin 和工作区证据根，验证真实登录、三分类、九类、双视口、隐私与注销。
- 权威测试记录位于 `docs/testing/PRD-063-tests.md`。

## 15. 第一轮实现后自审

状态：**修正后通过**。

| 发现 | 风险 | 修正 |
| --- | --- | --- |
| 旧 `/providers/{id}/test` 路径可能先建立连接再验证新 spec | 历史 API 可绕过统一 Adapter 门禁 | 把 spec/auth 校验移动到任何解析、Key 读取和联网之前，并补零网络调用测试 |
| DNS 只校验解析结果但连接仍使用原 hostname | 校验后发生 DNS 重绑定时可访问被禁止地址 | 把已校验 IP 固定到实际 TCP 连接，同时保留原 Host/TLS 语义 |
| 激活接口只依赖框架完整读取正文后再检查长度 | 超限正文仍可在拒绝前占用无界内存 | 使用 1024-byte 分块累计门禁，认证先于正文解析 |
| 同 version 若代码中的 hash/count 漂移，可能在事务后段才暴露 | 可短暂写入不一致 activation | 在提交前重复核对当前冻结 identity，漂移时整事务失败关闭 |
| Webhook 恢复任务可能使用补投 request ID | source 追踪从首次提交漂移到恢复任务 | 补投保持首次事实中的 `source_request_id/trace_id/event_id/time` |

本轮修正后，所有联网入口、激活事务和事件恢复路径共享相同发布边界；未发现剩余 P0/P1 安全阻断。

## 16. 第二轮实现后交叉复审

状态：**修正后通过**。

| 发现 | 风险 | 修正 |
| --- | --- | --- |
| Console 初版只检查核心字段，未严格拒绝非法时间、截断合同和重复数组 | 损坏 DTO 可能以看似正常的目录进入 DOM | 增加完整字段、有界长度、有效 UTC 时间、数组唯一性和 XSS 全字段测试，任一失败拒绝整批 |
| 浏览器验收器初稿把原始响应中的 `tenant_id` 误当成禁止字段 | 单元夹具与真实 Control DTO 不一致，无法证明租户归属 | 改为在内存中要求每条都有合法 tenant 且整批一致；tenant 值不进入 DOM、日志或证据 |
| 移动端首次截图继承上一视口滚动位置 | 自动门禁通过但人工证据没有完整显示页面标题 | 截图前重新对齐 PAGE-090 heading，仅复跑验收器合同和双视口浏览器 |
| 公共迁移测试仍把范围上界写死为 0049 | 0055 正常加入后产生两个旧断言失败 | 把两处上界更新为 0055，只定向复测两个失败节点，不重复无关全套 |

交叉复审同时确认：后端权限不能由前端隐藏替代；Session tenant 与内部 path tenant 不混用；Provider Status、导入导出、Webhook、统一错误和 request/trace 链路无回归；测试与运行证据未触碰 `.tmp` 或工作区外临时目录。

## 17. 最终验收与完成状态

- Shared/Gateway：冻结目录 `44 passed`，Gateway 合同 `30 passed`，直接受影响回归 `12 passed`。
- Control/迁移/相邻链路：Adapter Spec `7 passed`，0055 `3 passed`，Provider/Health `14 passed`，Webhook `8 passed`；Shared Webhook `16 passed`。
- Console：直接影响 `32 passed`；浏览器验收器 `8 passed`；相关语法和格式检查通过。
- 中文注释门禁：`comment coverage ok`。
- 开发库：`55/0055`，`integrity_check=ok`，外键违规 `0`，两个 active activation 均与冻结目录一致。
- Runtime：Admin/Developer 200、Viewer 403、匿名 401；内部身份、幂等、冲突和超限正文均符合设计；DeepSeek 非流式、通义流式和上游 429 均通过；RequestLog/UsageLedger/成本/失败事实可追踪。
- Browser：`1280x720` 与 `390x844` 最终批次 `status=passed`；三分类九类、竞品边界、布局、外部网络、page/console error、DOM/Storage 隐私和注销全部通过。
- 权威记录：`docs/testing/PRD-063-tests.md`、`.artifacts/prd063-runtime-final/runtime-acceptance.json`、`.artifacts/prd063-browser/run-20260801T100000-final/browser-acceptance.json`。

因此 PRD-063 可以标记完成。Excel 两张核心表确认下一项为 `PRD-064 | M1-企业POC | P1 | 核心层 | 契约测试 | Provider Adapter契约测试框架`；全部 108 条仍在推进，`all_108_prds_complete=false`。
