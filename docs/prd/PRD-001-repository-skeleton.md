# PRD-001 - 仓库工程骨架与模块边界设计

状态：已实现。

实现说明：本文档先完成设计，随后按 PRD-001 范围实现工程空壳、健康接口、控制台本地启动页、脚本、README 和测试。未实现任何后续业务 PRD。

## 需求理解

本 PRD 的目标是建立企业级 AI Gateway 的单仓库工程骨架，让新研发能在打开仓库后快速识别数据面、控制面、控制台、共享 SDK、脚本和文档边界，并能按 README 在 30 分钟内启动 API 网关、控制面 API 和控制台空壳。

产品边界必须保持清晰：

- 本产品是面向中国境内企业用户的 AI Gateway + 治理控制面。
- OpenRouter、硅基流动是对标平台、竞品平台和迁移兼容对象，不是默认模型厂家。
- 默认上游是模型厂商或云端模型 Endpoint，例如智谱、通义、文心、火山、混元、DeepSeek 官方或云上服务，以及客户本地 OpenAI-compatible Endpoint。
- M0/M1 不做硬件盒子、不做 MCP 主链路、不做信创、不做复杂数据出境。

本 PRD 只做工程基础和最小健康接口，不进入业务能力开发；数据库迁移、认证、多租户、Provider、Virtual Key 等由后续 PRD 逐条实现。

## 涉及模块

已按功能类别建立目录骨架：

| 模块 | 目录 | 职责 |
| --- | --- | --- |
| 数据面 | `gateway-data-plane/` | OpenAI-compatible API 入口、鉴权、限流、预算、路由、Provider Adapter、用量记录热路径 |
| 控制面 API | `control-plane-api/` | 租户、用户、Provider、模型、Virtual Key、账单、Webhook、审计等管理 API |
| 控制台 | `console-web/` | 企业管理员与开发者控制台，M0 首屏为空壳和本地启动状态页 |
| 共享 SDK/契约 | `shared-sdk/` | OpenAPI、共享 schema、Python SDK、TypeScript SDK |
| 脚本 | `scripts/` | 本地开发、数据库、CI、发布、预检工具入口 |
| 基础设施 | `infra/` | Docker、Compose、迁移、种子数据等后续承载位置 |
| 文档 | `docs/` | PRD 设计、架构、API、ADR、开发、Runbook、安全、测试文档 |

建议技术栈：

- 后端：Python + FastAPI。架构文档明确 Python、JavaScript/TypeScript 优先；FastAPI 能快速支撑 OpenAI-compatible API、OpenAPI 文档和 pytest 测试。
- 前端：TypeScript + React + Vite。M0 控制台为空壳，但需要为后续页面清单保留 feature 目录。
- 测试：pytest、httpx/ASGI transport、Vitest、Playwright 预留。
- 规范：Ruff、mypy、ESLint、Prettier，CI 中先跑基础 lint/test。

## 数据表/字段设计

本 PRD 不新增数据库表，不执行迁移。`AppConfig` 与 `ServiceInfo` 作为运行时 DTO 和后续配置模型占位。

### AppConfig

用于服务启动配置摘要，不落 Provider Key 明文。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `service_name` | string | 服务名，如 `gateway-data-plane`、`control-plane-api` |
| `environment` | enum | `dev`、`test`、`prod` |
| `log_level` | string | 日志级别 |
| `public_base_url` | string | 当前服务外部访问地址 |
| `control_api_base_url` | string | 控制面 API 地址，仅数据面需要 |
| `gateway_api_base_url` | string | 网关 API 地址，仅控制台需要 |
| `features` | object | 启用功能开关摘要 |
| `sensitive_keys_masked` | boolean | 敏感项是否已脱敏 |

后续若持久化到业务配置表，必须增加 `tenant_id`、`created_at`、`updated_at`、`status`。

### ServiceInfo

用于 `/version` 和本地启动页展示。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `service_name` | string | 服务名 |
| `version` | string | 应用版本，默认 `0.1.0-dev` |
| `build_commit` | string | 构建 commit，不存在时为 `unknown` |
| `build_time` | string | 构建时间，不存在时为 `unknown` |
| `started_at` | datetime | 进程启动时间 |
| `uptime_seconds` | number | 当前进程运行秒数 |
| `request_id` | string | 当前请求 ID |

`ServiceInfo` 属于基础设施元数据，不是业务数据；业务表从 PRD-003 开始默认包含 `tenant_id`。

## API 设计

PRD-001 不做业务 API。数据面和控制面 API 都提供相同的基础接口形态。

### GET /health

用途：本地启动和 CI smoke test。

响应：

```json
{
  "status": "ok",
  "service_name": "gateway-data-plane",
  "environment": "dev",
  "request_id": "req_..."
}
```

约束：

- 响应头必须包含 `X-Request-ID`。
- 不依赖数据库、Redis、Provider Endpoint；依赖型 readiness 在 PRD-005 完整实现。

### GET /version

用途：展示服务版本和启动信息。

响应：

```json
{
  "service_name": "gateway-data-plane",
  "version": "0.1.0-dev",
  "build_commit": "unknown",
  "build_time": "unknown",
  "started_at": "2026-07-07T00:00:00Z",
  "uptime_seconds": 12,
  "request_id": "req_..."
}
```

### 错误响应统一形态

```json
{
  "code": "infra.config_missing",
  "message": "Required configuration is missing.",
  "request_id": "req_...",
  "suggestion": "Check .env.example and local environment variables."
}
```

PRD-004 会扩展统一错误码和 TraceID；本 PRD 先建立最小兼容形态。

## 前端页面/交互设计

### README

README 是本 PRD 的核心入口，必须包含：

- 产品定位和 MVP 边界。
- 目录结构说明。
- 本地环境依赖。
- 环境变量模板说明。
- 一键启动命令。
- 单独启动数据面、控制面 API、控制台的命令。
- 测试、lint、格式化命令。
- PRD 开发流程：先设计文档、审核后编码、补测试和文档。

### 开发者本地启动页

控制台 M0 空壳首页，用于新员工确认服务状态：

- 顶部显示产品名 MetaHub AI Gateway。
- 显示 Gateway、Control API、Console 三个服务状态。
- 显示版本号、环境、请求 ID。
- 提供 OpenAPI、README、PRD 文档链接。
- 不提供业务操作按钮，避免未实现权限前制造假入口。

## 错误码设计

| 错误码 | HTTP | 场景 | 用户建议 |
| --- | --- | --- | --- |
| `infra.config_missing` | 500 | 必填配置缺失 | 检查 `.env.example` 与本地环境变量 |
| `infra.config_invalid` | 500 | 配置值格式错误 | 根据错误字段修正配置 |
| `infra.service_unavailable` | 503 | 服务尚未准备好 | 查看本地启动日志 |
| `infra.version_unavailable` | 500 | 版本信息读取失败 | 检查构建环境变量 |
| `infra.not_found` | 404 | 未知路径 | 检查 API 路径 |
| `infra.unhandled` | 500 | 未捕获异常 | 使用 `request_id` 定位日志 |

所有错误响应必须包含 `request_id`，日志中必须使用结构化 JSON 记录 `request_id`，不得输出 Provider Key、Virtual Key 或 Authorization 明文。

## 安全与权限约束

- Health/version 不返回任何密钥、Token、Provider Credential、数据库连接串。
- 环境变量模板只放占位符，不放真实密钥。
- 日志脱敏规则从第一版保留扩展点，至少覆盖 `authorization`、`api_key`、`provider_key`、`secret`、`token`。
- 控制台只做状态展示，不做需要权限的业务操作。
- 后续所有管理 API 必须做后端权限校验，不能只靠前端隐藏按钮。
- 所有后续业务数据表默认包含 `tenant_id`；PRD-001 仅创建服务级 DTO，不落业务数据。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0001-1 | 数据面 `/health` | 返回 200、`status=ok`、包含 `request_id` 和 `X-Request-ID` |
| TC-0001-2 | 数据面 `/version` | 返回 200、服务名、版本、启动时间、`request_id` |
| TC-0001-3 | 控制面 API `/health`、`/version` | 与数据面保持相同响应契约 |
| TC-0001-4 | 控制台本地启动页 | 页面可打开，能展示三个服务状态占位 |
| TC-0001-5 | README 启动流程 | 按 README 能启动三个空壳服务并运行基础测试 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0002-1 | 缺失必填配置 | 启动失败或接口返回统一错误结构，包含 `request_id` 和修复建议 |
| TC-0002-2 | 未知路径 | 返回 `infra.not_found`，不泄露栈信息 |
| TC-0002-3 | 日志脱敏 | 带 `Authorization` 或 `provider_key` 的请求不会在日志中明文出现 |
| TC-0002-4 | 权限边界 | 控制台无业务按钮；后端无未授权业务接口 |
| TC-0002-5 | CI 基础检查 | lint、单元测试、smoke test 可在无数据库时通过 |

## 代码修改计划

已按以下顺序编码：

1. 根目录新增 `README.md`、`.env.example`、`.gitignore`、`pyproject.toml`。
2. 新增 `gateway-data-plane` 与 `control-plane-api` 的 FastAPI 空壳。
3. 实现两个后端服务的配置加载、request_id 中间件、结构化日志脱敏、`/health`、`/version`。
4. 新增 `console-web` 零依赖控制台空壳与开发者本地启动页，预留后续 feature 目录。
5. 新增 `shared-sdk/contracts/openapi` 占位与 `ServiceInfo` JSON Schema。
6. 新增 `scripts/dev` 启动脚本、`scripts/ci/check.ps1` 检查脚本和最小本地 smoke test。
7. 补齐后端 pytest 单元/集成测试与控制台 Node 单元测试。
8. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-001-tests.md`。

不进入本 PRD 的内容：

- 不实现数据库迁移和种子数据。
- 不实现管理员登录、租户、Virtual Key、Provider、模型目录。
- 不引入 Kubernetes、Helm、信创、硬件盒子或 MCP 主链路。
- 不实现完整 Trace 可视化、OpenTelemetry 后端或复杂动态配置中心。

## 验证结果

已运行：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\ci\check.ps1
```

结果：

- 后端 pytest：`9 passed`
- 控制台 Node test：`2 passed`
- 控制台 lint：通过

已启动并验证：

- `GET http://localhost:8000/health` 返回 `service_name=gateway-data-plane` 与请求传入的 `request_id`
- `GET http://localhost:8001/health` 返回 `service_name=control-plane-api` 与请求传入的 `request_id`
- `GET http://localhost:5173` 返回 HTTP 200
