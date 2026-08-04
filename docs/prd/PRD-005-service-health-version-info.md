# PRD-005 - 服务健康检查与版本信息设计

状态：已实现。

实现说明：本文档先完成设计，随后按 PRD-005 范围实现 HealthCheckResult、`/health/live`、`/health/ready`、`/version` trace 扩展、系统状态小组件、测试和文档。未实现完整状态页、外部探针服务或 Provider 深度健康评分。

## 需求理解

本 PRD 的目标是让运维和交付人员能在上线前快速判断服务是否可用，并区分“进程活着”和“依赖可用”。

来自 Excel 的核心要求：

- 实现 liveness、readiness、version 接口。
- 检查数据库、Redis、Provider 基础连通性可选。
- 输出构建版本、提交号、启动时间。
- 断开数据库时 `ready=false`。
- `live` 不依赖外部资源。
- 不做完整状态页，不做外部探针服务。
- 页面入口为系统状态小组件。

当前 PRD-001/004 已有 `/health`、`/internal/health`、`/version` 的基础能力；PRD-005 要正式拆分 `/health/live` 与 `/health/ready`，并把依赖状态纳入 readiness。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 增加 HealthCheckResult、依赖检查器、版本信息扩展 |
| 数据库工具 | `shared-sdk/python/metahub_shared/db/` | 提供轻量 DB ping，不执行迁移 |
| 数据面 | `gateway-data-plane/` | 暴露 `/health/live`、`/health/ready`、`/version` |
| 控制面 API | `control-plane-api/` | 暴露同样健康接口，readiness 可检查 DB |
| 控制台 | `console-web/` | 系统状态小组件读取 live/ready/version |
| 文档与测试 | `docs/testing/`、pytest、Node test | 覆盖依赖故障、live 不依赖外部、版本元数据 |

编码时保留详细中文注释，重点说明 live/ready 的区别、为什么 ready 可以失败但 live 仍成功、为什么 Provider 连通性在 M0 是可选检查。

## 数据表/字段设计

本 PRD 不新增数据库表。`HealthCheckResult` 是运行时 DTO，不落库。

### HealthCheckResult

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `service_name` | string | 服务名 |
| `status` | enum | `ok`、`degraded`、`fail` |
| `live` | boolean | 进程是否存活 |
| `ready` | boolean | 是否可接流量 |
| `environment` | string | `dev/test/prod` |
| `checks` | array | 依赖检查项 |
| `version` | string | 应用版本 |
| `build_commit` | string | 构建提交 |
| `build_time` | string | 构建时间 |
| `started_at` | datetime | 启动时间 |
| `uptime_seconds` | number | 运行秒数 |
| `request_id` | string | 请求 ID |
| `trace_id` | string | Trace ID |

### HealthDependencyCheck

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | `database`、`redis`、`provider:<code>` |
| `status` | enum | `ok`、`skipped`、`fail` |
| `required` | boolean | 是否影响 ready |
| `latency_ms` | integer nullable | 检查耗时 |
| `message` | string | 安全摘要，不含密码/key |

业务数据表仍从 PRD-003 开始默认带 `tenant_id`；本 PRD 是服务级探针 DTO，不要求 `tenant_id`。

## API 设计

所有接口都沿用 PRD-004 契约：响应头包含 `X-Request-ID`、`X-Trace-ID`。

### GET /health/live

用途：容器 liveness probe。只判断进程是否能响应，不访问数据库、Redis、Provider。

响应：

```json
{
  "service_name": "gateway-data-plane",
  "status": "ok",
  "live": true,
  "ready": false,
  "checks": [],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /health/ready

用途：readiness probe。检查必需依赖。

规则：

- 数据库配置存在时执行 DB ping，失败则 `ready=false`。
- Redis 配置存在时执行 Redis ping；M0 无 Redis 驱动时可标记 `skipped` 或 `fail`，根据 `required` 决定是否影响 ready。
- Provider Base URL 配置存在时可做 URL 格式/可选 HEAD 检查；M0 默认不阻塞 ready，避免外部厂商波动影响控制面上线。

响应：

```json
{
  "service_name": "control-plane-api",
  "status": "fail",
  "live": true,
  "ready": false,
  "checks": [
    {
      "name": "database",
      "status": "fail",
      "required": true,
      "latency_ms": 12,
      "message": "database connection failed"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /version

保留现有接口，并确保包含：

- `service_name`
- `version`
- `build_commit`
- `build_time`
- `started_at`
- `uptime_seconds`
- `request_id`
- `trace_id`

## 前端页面/交互设计

### 系统状态小组件

控制台开发者启动页增加系统状态小组件：

- Gateway live/ready 状态。
- Control API live/ready 状态。
- 版本号、commit、启动时间、运行时长。
- 依赖检查摘要：database、redis、provider。
- ready=false 时展示原因和 request_id/trace_id。

交互约束：

- 只读展示，不提供重启/修复按钮。
- 不展示数据库连接串、Redis 密码、Provider Key。
- 不做完整状态页。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `health.dependency_failed` | 503 | 必需依赖失败导致 ready=false | 查看 checks 中的依赖摘要 |
| `health.check_timeout` | 503 | 依赖检查超时 | 检查网络或调低探针频率 |
| `health.invalid_dependency_config` | 500 | 依赖配置格式非法 | 修正 `.env` 或环境变量 |

注意：`/health/ready` 可以用 HTTP 503 表示不可接流量，但响应体仍必须是统一结构并包含 `request_id`、`trace_id`。

## 安全与权限约束

- health 响应不返回数据库 URL、Redis URL、Provider Key、Authorization。
- dependency message 只能写安全摘要。
- live 不能依赖外部资源，避免依赖故障导致容器被错误重启。
- ready 可失败但必须可解释。
- Provider 检查默认可选，不允许在 M0 因上游模型厂商网络波动阻塞本地控制台和控制面启动。
- 系统状态小组件不替代后端权限，后续管理操作仍需 RBAC。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0009-1 | `/health/live` | 无数据库/Redis 时仍返回 200、`live=true` |
| TC-0009-2 | `/health/ready` 数据库可用 | 返回 `ready=true`，checks 中 database=ok |
| TC-0009-3 | `/version` | 返回版本、commit、build_time、started_at、uptime、request_id、trace_id |
| TC-0009-4 | 控制台系统状态小组件 | 展示 live/ready/version 和 request_id/trace_id |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0010-1 | 数据库断开 | `/health/ready` 返回 503 或 ready=false，checks 中 database=fail |
| TC-0010-2 | live 不依赖外部 | 数据库断开时 `/health/live` 仍成功 |
| TC-0010-3 | 敏感信息 | health/version 响应不包含 DB 密码、Redis 密码、Provider Key |
| TC-0010-4 | Provider 可选 | Provider URL 不可达不会默认阻塞 ready |
| TC-0010-5 | trace 透传 | 所有 health/version 响应头包含 `X-Request-ID`、`X-Trace-ID` |

## 代码修改计划

已按以下顺序编码：

1. 增加 HealthCheckResult / HealthDependencyCheck DTO。
2. 增加 DB ping：复用 PRD-003 `connect_database`，只执行轻量 `SELECT 1`。
3. 增加 readiness checker：database required，redis/provider M0 可选。
4. 在共享 FastAPI app 中新增 `/health/live`、`/health/ready`，保留 `/health` 兼容。
5. 扩展 `/version` 返回 `trace_id`。
6. 扩展错误码注册表：`health.*`。
7. 控制台新增系统状态小组件，读取 live/ready/version。
8. 增加 pytest：live 无依赖、ready DB ok/fail、version trace、敏感信息不泄露。
9. 增加 Node test：状态小组件正常、ready=false 降级、安全显示。
10. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-005-tests.md`。

不进入本 PRD 的内容：

- 不做完整状态页。
- 不做外部探针服务。
- 不接 Prometheus/OpenTelemetry。
- 不实现 Provider 深度健康评分。

## 验证结果

已运行：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\ci\check.ps1
```

结果：

- 后端 pytest：`26 passed`
- 控制台 Node test：`8 passed`
- 控制台 lint：通过

已覆盖：

- `/health/live` 不依赖外部资源。
- `/health/ready` 数据库成功/失败。
- `/version` 包含 `trace_id`。
- health 响应不泄露数据库密码、Redis 密码、Provider Key。
- 控制台系统状态小组件展示 ready=false 且不白屏。

真实 HTTP 验证：

- `GET http://localhost:8001/health/live` 携带 `X-Request-ID=req_live_real`、`X-Trace-ID=trc_live_real`，返回 `live=true`、`ready=false`。
- `GET http://localhost:8001/health/ready` 携带 `X-Request-ID=req_ready_real`、`X-Trace-ID=trc_ready_real`，返回 `ready=true`，数据库/Redis/Provider 未配置时均为 `skipped`。
- `GET http://localhost:8001/version` 携带 `X-Request-ID=req_version_real`、`X-Trace-ID=trc_version_real`，返回版本、commit、启动时间、运行时长和 trace。
- `GET http://localhost:5173` 返回 HTTP 200。
