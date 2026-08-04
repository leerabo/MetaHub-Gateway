# PRD-002 - 运行配置与环境隔离基础设计

状态：已实现。

实现说明：本文档先完成设计，随后按 PRD-002 范围实现运行配置加载、配置摘要、内部访问保护、控制台环境信息页、测试和文档。未实现动态配置中心或热更新。

## 需求理解

本 PRD 的目标是让开发、测试、生产环境配置互不污染，并建立一套可被数据面、控制面和控制台共同遵守的运行配置规则。

来自 Excel 的核心要求：

- 支持 `dev`、`test`、`prod` 三类环境。
- 实现配置加载、环境变量、配置文件优先级、敏感配置占位。
- Provider 连接信息从配置读取。
- 提供 `GET /internal/config/summary`。
- 系统设置中展示“环境信息”。
- M0 不做复杂动态配置中心，只支持重启生效。
- 敏感项不在日志中明文输出。

本 PRD 延续 PRD-001 的边界：OpenRouter 和硅基流动仍是对标平台，不是默认上游；默认上游是模型厂商、云端模型 Endpoint 或客户本地 OpenAI-compatible Endpoint。M0 不引入数据库动态配置中心，也不做热更新。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 扩展配置加载、配置源优先级、敏感项脱敏、配置摘要 DTO |
| 数据面 | `gateway-data-plane/src/metahub_gateway/config/` | 使用共享配置读取 Provider 基础 URL、Redis/数据库连接占位 |
| 控制面 API | `control-plane-api/src/metahub_control/config/` | 新增 `GET /internal/config/summary` 路由 |
| 控制台 | `console-web/src/features/system-status/` | 新增系统设置-环境信息只读视图 |
| 文档 | `README.md`、`docs/development/` | 更新环境变量说明、配置优先级、本地/测试/生产样例 |
| 测试 | 后端 pytest、控制台 Node test | 覆盖配置覆盖、脱敏、非法配置、接口权限边界 |

编码时保留详细中文注释，重点说明配置优先级、为什么敏感项只能展示占位、为什么 M0 不热更新。

## 数据表/字段设计

PRD-002 不新增数据库迁移。原因：

- PRD-003 才建立数据库迁移框架。
- 本 PRD 明确“不做复杂动态配置中心；M0 只支持重启生效”。
- 配置来源应先落在 `.env`、环境变量和本地配置文件，避免数据库依赖反向拖慢 M0-W1。

本 PRD 定义运行时对象，为后续持久化预留字段。

### Environment

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | enum | `dev`、`test`、`prod` |
| `is_production` | boolean | 是否生产环境 |
| `config_profile` | string | 当前配置 profile 名称 |
| `service_mode` | enum | `internal_only`、`public_service`，M0 默认 `internal_only` |
| `loaded_at` | datetime | 配置加载时间 |

后续若持久化，必须增加 `id`、`tenant_id`、`created_at`、`updated_at`、`status`。

### RuntimeConfig

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `environment` | Environment | 当前运行环境 |
| `database_url` | secret string | 数据库连接串，摘要中只显示 `configured/missing` |
| `redis_url` | secret string | Redis 地址，摘要中只显示 `configured/missing` |
| `provider_base_urls` | map | Provider 基础 URL，值可展示域名但不能展示 key |
| `gateway_api_base_url` | string | 数据面 API 地址 |
| `control_api_base_url` | string | 控制面 API 地址 |
| `allowed_origins` | string[] | 控制台允许跨域来源 |
| `config_sources` | object[] | 配置项来源摘要，如 `default/env/file` |
| `sensitive_keys_masked` | boolean | 敏感项是否已脱敏 |

### ConfigSummary

`GET /internal/config/summary` 返回对象。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `service_name` | string | 服务名 |
| `environment` | string | 当前环境 |
| `loaded_at` | datetime | 配置加载时间 |
| `items` | array | 配置摘要项 |
| `warnings` | array | 缺失可选配置、生产环境风险等 |
| `request_id` | string | 请求 ID |

`items` 单项：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | string | 配置键名 |
| `source` | enum | `default`、`env`、`file` |
| `status` | enum | `configured`、`missing`、`masked`、`defaulted` |
| `value_preview` | string | 安全预览，敏感项固定为 `***MASKED***` |

## API 设计

### GET /internal/config/summary

位置：优先放在 `control-plane-api`，供控制台“系统设置-环境信息”读取。数据面可复用同一内部配置摘要函数，但 M0 不要求额外暴露数据面配置接口。

请求头：

| Header | 必填 | 说明 |
| --- | --- | --- |
| `X-Request-ID` | 否 | 透传 request_id |
| `X-Internal-Token` | dev/test 可选，prod 必填 | PRD-006 前的最小内部访问保护 |

响应示例：

```json
{
  "service_name": "control-plane-api",
  "environment": "dev",
  "loaded_at": "2026-07-07T09:00:00Z",
  "items": [
    {
      "key": "METAHUB_ENV",
      "source": "env",
      "status": "configured",
      "value_preview": "dev"
    },
    {
      "key": "METAHUB_PROVIDER_DEEPSEEK_BASE_URL",
      "source": "env",
      "status": "configured",
      "value_preview": "https://api.deepseek.com"
    },
    {
      "key": "METAHUB_PROVIDER_DEEPSEEK_API_KEY",
      "source": "env",
      "status": "masked",
      "value_preview": "***MASKED***"
    }
  ],
  "warnings": [],
  "request_id": "req_..."
}
```

约束：

- 响应必须包含 `request_id`，响应头必须包含 `X-Request-ID`。
- 不能返回数据库密码、Provider Key、Virtual Key、Authorization、Token、Secret 明文。
- `prod` 环境未配置 `X-Internal-Token` 或 token 不匹配时返回 401/403。
- 非法环境值返回统一错误结构。

## 前端页面/交互设计

页面入口：`系统设置 -> 环境信息`。

PRD-002 只做只读信息页，不提供编辑能力，因为 M0 配置重启生效且不做动态配置中心。

页面内容：

- 当前环境：`dev/test/prod`，生产环境用醒目状态标识。
- 服务信息：Gateway、Control API、Console 的基础 URL。
- 基础依赖：数据库、Redis、Provider Base URL 是否配置。
- 配置来源：默认值、环境变量、配置文件。
- 敏感项状态：只展示 `已配置/未配置/已脱敏`，不展示明文。
- 风险提示：生产环境使用默认密钥、缺少内部访问 token、Provider URL 缺失等。

交互约束：

- 页面不提供保存按钮。
- 页面不展示任何 Provider Key。
- 未实现 RBAC 前，该页面只能作为内部开发/运维入口；后续 PRD-006/PRD-008 接入后端鉴权。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `infra.config_missing` | 500 | 必填配置缺失 | 检查 `.env` 或环境变量 |
| `infra.config_invalid` | 500 | 环境名、URL、布尔值等格式非法 | 修正配置并重启服务 |
| `infra.config_secret_unmasked` | 500 | 摘要生成发现敏感项未脱敏 | 阻断响应并修复脱敏规则 |
| `infra.config_profile_not_found` | 500 | 指定配置文件不存在 | 检查 profile 名称和文件路径 |
| `auth.internal_token_required` | 401 | `prod` 访问内部配置摘要但未提供 token | 配置并传入 `X-Internal-Token` |
| `auth.internal_token_invalid` | 403 | 内部 token 不匹配 | 检查调用方配置 |

所有错误响应沿用 PRD-001 结构：`code/message/request_id/suggestion`。

## 安全与权限约束

- Provider Key、数据库密码、Redis 密码、Authorization、Token、Secret 一律只显示 `***MASKED***`。
- 日志中记录配置加载事件时，只记录 key、source、status，不记录 value。
- `prod` 环境访问 `/internal/config/summary` 必须要求内部 token；PRD-006 后替换为管理员鉴权和后端权限校验。
- 控制台隐藏页面入口不等于权限，后端必须拒绝未授权访问。
- `config.loaded` 事件 payload 只能包含配置摘要和脱敏状态。
- 配置文件不得提交真实密钥，`.env.example` 只允许占位值。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0003-1 | `dev/test/prod` 环境加载 | 不同 `METAHUB_ENV` 得到独立配置摘要 |
| TC-0003-2 | 环境变量覆盖默认值 | env 值优先于默认值，`source=env` |
| TC-0003-3 | Provider Base URL 读取 | Provider URL 从配置读取并在 summary 中安全展示 |
| TC-0003-4 | `GET /internal/config/summary` | 返回 200、统一结构、`request_id`、`X-Request-ID` |
| TC-0003-5 | 控制台环境信息页 | 展示环境、依赖状态、配置来源、脱敏状态 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0004-1 | 非法 `METAHUB_ENV` | 返回或抛出 `infra.config_invalid`，包含修复建议 |
| TC-0004-2 | 缺失必填配置 | 返回 `infra.config_missing`，不泄露内部路径或密钥 |
| TC-0004-3 | 敏感项脱敏 | summary 和日志均不包含 Provider Key 明文 |
| TC-0004-4 | `prod` 未带内部 token | 返回 `auth.internal_token_required` |
| TC-0004-5 | 配置文件不存在 | 返回 `infra.config_profile_not_found` |

## 代码修改计划

已按以下顺序编码：

1. 扩展 `metahub_shared.runtime.config`：增加配置源、配置摘要、Provider Base URL、数据库/Redis 占位读取。
2. 增加配置脱敏工具：统一判断敏感 key，并对 summary value 做强制 mask。
3. 在 `control-plane-api` 新增 `/internal/config/summary` 路由，沿用 PRD-001 的 request_id 和错误结构。
4. 增加 `X-Internal-Token` 最小保护：`dev/test` 可关闭，`prod` 必须开启。
5. 扩展 `.env.example`：增加数据库、Redis、Provider Base URL、Provider Key 占位、内部 token 占位。
6. 控制台新增“系统设置-环境信息”只读区块，并从控制面 API 读取配置摘要。
7. 增加 pytest：配置覆盖、非法环境、敏感项脱敏、prod token 权限。
8. 增加控制台测试：环境信息读取、敏感项不显示明文、后端失败降级。
9. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-002-tests.md`，补充配置优先级和环境隔离说明。

不进入本 PRD 的内容：

- 不做数据库持久化配置中心。
- 不做配置热更新。
- 不做复杂审批、灰度、配置快照发布。
- 不接入完整 RBAC；只做 `X-Internal-Token` 最小内部保护，后续由认证鉴权 PRD 替换。

## 验证结果

已运行：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\ci\check.ps1
```

结果：

- 后端 pytest：`13 passed`
- 控制台 Node test：`4 passed`
- 控制台 lint：通过

已覆盖：

- 配置优先级：默认值 < `.env` < `METAHUB_CONFIG_FILE` < 环境变量。
- Provider Base URL 从配置读取。
- Provider Key、数据库 URL、Redis URL、内部 token 在摘要中脱敏。
- `prod` 环境 `/internal/config/summary` 缺 token 返回 401，错误 token 返回 403。
