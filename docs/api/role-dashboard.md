# 角色首页 API

本文档描述 PRD-065 PAGE-092 的稳定接口合同。角色首页用于按当前登录角色选择默认布局，但最终授权始终来自
当前租户数据库中的实时 permission 快照；角色名、前端按钮和客户端 query 都不能补权。

## 1. 读取角色首页

`GET /dashboard/role-view`

认证与权限：

- 必须携带有效的 Console Bearer Session；匿名请求返回 `401`。
- 后端首先执行 `dashboard.read` 校验；缺权请求返回 `403`，且优先于 query 校验。
- `tenant_id`、用户、角色、permission revision 均从认证上下文恢复，接口不接受这些身份字段。
- 响应和错误均携带 `request_id`、`trace_id`，认证响应使用 `Cache-Control: no-store`。

Query：

| 字段 | 必填 | 合法值 | 说明 |
| --- | --- | --- | --- |
| `view` | 否 | `executive/finance/security/developer/operations` | 省略时由后端选择当前角色默认视图；显式值还必须同时位于租户角色配置和实时权限交集中 |

接口拒绝重复 `view`、空值、大小写变体、未知值和任何额外 query。特别是不得发送 `tenant_id`、`role`、
`user_id` 或任意时间范围。

成功示例：

```json
{
  "config": {
    "role_dashboard_config_id": "rdc_0123456789abcdef0123456789abcdef",
    "config_revision": 1,
    "default_view_code": "finance",
    "selected_view_code": "finance",
    "available_views": [
      {"code": "finance", "label": "财务视图", "is_default": true},
      {"code": "executive", "label": "经营总览", "is_default": false}
    ]
  },
  "window": {
    "timezone": "Asia/Shanghai",
    "start_at": "2026-08-01T00:00:00+08:00",
    "end_at": "2026-08-02T00:00:00+08:00",
    "generated_at": "2026-08-01T12:00:00Z"
  },
  "cards": [
    {
      "code": "cost",
      "label": "成本",
      "status": "ok",
      "primary_value": 12.34,
      "primary_unit": "cny",
      "summary": "今日正式 CNY 用量成本",
      "facts": [
        {"code": "priced_requests", "label": "成功计费请求", "value": 10, "unit": "count"}
      ],
      "target_navigation_item_id": "usage"
    }
  ],
  "empty_state": {"is_empty": false, "title": "", "description": ""},
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

服务端固定返回六张卡片并按所选视图排序：`cost`、`security`、`stability`、`adoption`、
`provider_health`、`budget_risk`。卡片只包含当前租户的有界聚合数值；目标导航 ID 只有在当前权限允许
对应明细能力时才返回，Console 还必须和同轮后端导航快照再次求交后才能渲染链接。

## 2. 角色与视图

| 角色 | 默认视图 | 产品候选视图 |
| --- | --- | --- |
| `admin` | `executive` | 五个视图 |
| `developer` | `developer` | `developer/operations/executive` |
| `viewer` | `executive` | `executive` |
| `finance_admin` | `finance` | `finance/executive` |
| `security_admin` | `security` | `security/executive` |
| `ops_admin` | `operations` | `operations/executive` |

该表只定义产品候选目录。每次请求仍会检查视图所需 permission 的完整集合。租户矩阵撤销 permission 后，
视图会立即从 `available_views` 消失；显式请求该已知但无权视图返回 `auth.permission_denied`。默认视图因撤权
不可用时，只允许回落到同一配置内且实时可见的 `executive`，不能任意选择第一个视图。

## 3. 错误合同

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing/invalid/expired` | Session 缺失、无效或过期 |
| 403 | `auth.permission_denied` | 缺少 `dashboard.read`，或显式视图不在配置与实时权限交集 |
| 422 | `dashboard.query_invalid` | 重复、空、未知或额外 query |
| 500 | `dashboard.config_invalid` | 当前 tenant/role 配置缺失、重复、禁用、损坏或无安全回落 |
| 503 | `dashboard.unavailable` | 聚合依赖或数据库暂不可用 |

统一错误响应示例：

```json
{
  "code": "dashboard.query_invalid",
  "message": "Dashboard query is invalid.",
  "request_id": "req_example",
  "trace_id": "trc_example",
  "suggestion": "Remove unsupported parameters; role-view accepts at most one supported lowercase view code.",
  "diagnosis": {
    "rule_id": "dashboard_query_invalid",
    "category": "dashboard",
    "reason": "Dashboard 查询包含重复、空值、未知视图或未支持的额外参数。",
    "suggestion": "总览使用固定今日窗口；角色首页最多传一个服务端已下发的小写 view code。",
    "action": "移除 tenant、role、时间范围等身份或范围参数，并按后端 available_views 重新请求。",
    "severity": "error",
    "retryable": false,
    "matched_by": "error_code",
    "safe_summary": "dashboard.query_invalid http_status=422"
  }
}
```

数据库路径、SQL、配置 JSON、底层异常正文和敏感业务事实不得进入错误响应或普通日志。

## 4. 页面访问事件

PAGE-092 在成功 GET、authorization owner 复核和 DOM 投影完成后，使用既有
`POST /console/events` 写入 `dashboard.role_viewed`。事件必须绑定 `overview` 导航和 `/#overview`，payload
必须精确包含：

```json
{
  "source": "view_switch",
  "hash": "#overview",
  "viewport_class": "desktop",
  "view_code": "finance",
  "config_revision": 1
}
```

`source` 只接受 `sidebar/direct_hash/view_switch/refresh`；视口只接受 `desktop/mobile`。后端在事件写事务中
重新读取当前 tenant/role 配置，并与实时 permission 求交。revision、视图或权限已漂移时返回
`console.event_invalid`，不会把迟到页面记录成有效访问。

事件不保存 tenant 名称、邮箱、角色、permission 列表、卡片数值、筛选、Provider/Virtual Key、Endpoint、
Authorization、Token、Prompt、Response 或上游错误正文。

## 5. 兼容性与前端状态

- `GET /dashboard/overview` 和历史 `dashboard.viewed` 继续保留给旧客户端；PAGE-092 不在新接口失败时回落旧接口。
- 视图选择仅存在于当前页面内存，不写 URL、LocalStorage、SessionStorage 或用户偏好。
- 切换视图先取消旧请求并清空旧卡片；迟到响应必须通过 tenant、Session、permission revision、授权指纹、
  导航、视图和 generation 的完整 owner 校验。
- `401/403` 清除受保护状态；`503` 只有在同一 authorization owner、同一 selected view 下才能展示已净化的
  `partial` 快照。

## 6. 数据库版本

0057 新增 tenant-scoped `role_dashboard_configs`，使用 `(tenant_id, role_code)` 延迟复合外键关联当前租户
角色，并为现有和新建 tenant 初始化六条 canonical 配置。owner、ID、role、created_at 不可变；受控更新要求
`config_revision` 精确加一，DELETE 和 `INSERT OR REPLACE` 均被数据库门禁拒绝。

0057 同时扩展 `console_page_events` 的固定事件枚举，并保留既有 rowid、十二列、历史事件和三个索引。
down migration 只允许 canonical 配置回滚，并把 `dashboard.role_viewed` 映射为兼容的 `dashboard.viewed` 轨迹。
