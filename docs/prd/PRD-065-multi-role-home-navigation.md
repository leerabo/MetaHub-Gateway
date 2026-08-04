# PRD-065 - 多角色首页与导航

状态：**设计、两轮编码前自审、实现、专项测试、0057 迁移、真实 HTTP、真实 Edge 双视口和运行验收均已完成**。

## 1. 权威来源与需求理解

### 1.1 Excel 需求映射

本设计已交叉读取 Excel `01_PRD总表` 与 `03_M2-M3扩展交付`，不是仅凭编号推断：

| 字段 | 权威值 |
| --- | --- |
| PRD_ID | `PRD-065` |
| 里程碑 | `M2-企业增强` |
| 优先级 | `P2` |
| 能力层 | 扩展层 |
| 模块 | UCD角色首页 |
| 名称 | 多角色首页与导航 |
| 主要用户 | 老板/CTO/财务/安全/开发者/运维 |
| 用户故事 | 不同角色登录后先看到自己关心的指标 |
| 范围 | 成本、安全、稳定性、接入进度、Provider 健康、预算风险；支持切换视图 |
| 明确不做 | 个性化拖拽仪表盘 |
| 页面 | `PAGE-092 角色首页` |
| API | `API-290/291/292` 合并表达的 `GET /dashboard/role-view` |
| 数据对象 | `OBJ-087 RoleDashboardConfig` |
| 事件 | `EVT-072 dashboard.role_viewed` |
| 主流程验收 | `TC-0129`：不同角色看到不同默认卡片，权限控制一致 |
| 异常验收 | `TC-0130`：越权、异常输入或依赖故障返回统一错误，不泄露敏感信息且可追踪 |

Excel 的 API 清单把 `GET`、`dashboard`、`role-view` 拆成连续三行；PRD 总表给出的完整动作是
`GET /dashboard/role-view`，不能误实现为三个接口。

### 1.2 架构与产品边界

系统架构文档第 10 章要求控制台按角色组织；控制面服务管理员、开发者、财务、安全和运维角色，
所有查询仍走同一租户权限体系。内部 code 保持稳定英文，展示层使用中文。所有业务域数据默认携带
`tenant_id`，不得通过 Dashboard 建立跨租户汇总旁路。

本 PRD 不改变 Provider 边界：智谱、通义、文心、火山、混元、DeepSeek 官方或云服务及客户本地
OpenAI-compatible Endpoint 仍是默认上游；OpenRouter、硅基流动仅是对标和迁移兼容对象。

### 1.3 现有实现与真实缺口

PRD-026 已完成 `GET /dashboard/overview`、今日请求/成功率/P95/成本、Top 模型/Key、最近错误和空态；
PRD-059 已冻结六个角色 `admin/developer/viewer/finance_admin/security_admin/ops_admin`，认证在每次
请求从当前租户数据库读取 permission 矩阵。当前 Console 的 `overview` 仍对所有持有
`dashboard.read` 的用户展示同一套内容。

缺口是：

1. 没有 tenant-scoped `RoleDashboardConfig`，角色默认首页只能靠前端角色判断或硬编码；
2. 没有 `GET /dashboard/role-view`，也没有后端证明“当前用户允许切换到哪些视图”；
3. 当前首页不能按财务、安全、开发和运维关注点重排六类指标；
4. 没有安全的视图切换状态机，权限撤销、迟到响应和换租户时可能保留旧视图；
5. 现有 `dashboard.viewed` 只记录通用首页访问，不能区分实际展示的角色视图。

## 2. 实现范围与非目标

### 2.1 本期实现

- 新增五个固定视图：`executive`、`finance`、`security`、`developer`、`operations`；
- 复用 PRD-059 六个固定角色，只把角色用于选择默认布局；
- 新增 tenant-scoped `role_dashboard_configs`，保存每个角色的默认视图和产品允许视图；
- 视图最终可见性取“持久角色配置”与“当前请求实时 permission 快照”的交集；
- 新增 `GET /dashboard/role-view`，返回配置、可切换视图、六类安全聚合卡片和统一链路 ID；
- 保留 `GET /dashboard/overview` 兼容旧客户端，PAGE-092 使用新接口；
- 首页使用分段控制切换视图，不把选择写入 URL、LocalStorage、SessionStorage 或数据库偏好；
- 记录 `dashboard.role_viewed` 安全页面遥测；
- 新增 0057 up/down migration、后端/前端/迁移测试及真实双视口验收。

### 2.2 明确不做

- 不新增老板、CTO 或自定义角色；老板/CTO 使用现有 `admin` 或只读 `viewer` 身份；
- 不做拖拽、自由增删卡片、任意栅格、个人主题或持久化自定义布局；
- 不增加写配置 API，不允许前端提交角色、permission、tenant 或卡片定义；
- 不做跨租户集团驾驶舱、BI 数据仓库、长期趋势预测或定时物化指标；
- 不把页面访问事件扩展为高频外部 Webhook；沿用现有 `dashboard.viewed/status.viewed` 的内部控制台
  遥测边界；
- 不在本 PRD 引入 MCP、硬件盒子、信创、复杂数据出境或企业 Chat。

## 3. 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| Dashboard DTO/服务/路由 | `control-plane-api/src/metahub_control/dashboard/` | 视图目录、严格 query、配置读取、权限交集、卡片聚合与新 GET |
| 认证依赖 | `control-plane-api/src/metahub_control/auth/dependencies.py` | 更新 Dashboard reader 文档，继续以后端 `dashboard.read` 为首层守卫 |
| Console 事件 | `control-plane-api/src/metahub_control/console/` | 新事件类型、视图字段白名单和当前权限复核 |
| 数据库 | `infra/migrations/0057_role_dashboard_views.sql/.down.sql` | 配置表、租户初始化、事件表保真重建、owner/revision/down guard |
| Shared 错误/诊断 | `shared-sdk/python/metahub_shared/runtime/` | 复用 Dashboard 错误并补配置损坏诊断，不回显数据库细节 |
| Console API client | `console-web/src/lib/dashboard.js` | 新 role-view GET；客户端只发送后端已下发的 view code |
| PAGE-092 状态与渲染 | `console-web/src/features/dashboard/` | owner 状态机、分段视图、六类卡片、错误/空态/partial |
| Console 编排 | `console-web/src/app/main.js`、`features/developer-start/` | 仅 overview 激活时读取、切换、事件、注销/降权/换租户清理 |
| 样式 | `console-web/src/styles/main.css` | 稳定卡片尺寸、桌面/移动布局、无重叠和无横向溢出 |
| 测试与文档 | Python/Node、`scripts/acceptance/`、`docs/` | 主流程、异常、权限、租户、迁移、Runtime、Browser 和隐私证据 |

## 4. 角色、视图与权限模型

### 4.1 固定视图目录

| view code | 中文名 | 关注点 | 进入视图所需实时权限 |
| --- | --- | --- | --- |
| `executive` | 经营总览 | 成本、稳定性、接入、Provider、预算与安全摘要 | `dashboard.read` |
| `finance` | 财务视图 | 成本、预算风险、接入规模、稳定性 | `dashboard.read`、`billing.usage.read`、`billing.budget.read`、`billing.anomaly.read` |
| `security` | 安全视图 | DLP、安全失败、策略覆盖、稳定性和 Provider 风险 | `dashboard.read`、`dlp.read`、`policy.read`、`audit.read` |
| `developer` | 开发视图 | 接入进度、请求成功率、延迟、错误和 Provider 状态 | `dashboard.read`、`developer_portal.read`、`request_log.read` |
| `operations` | 运维视图 | Provider 健康、错误率、延迟、接入规模和预算阻断 | `dashboard.read`、`provider.health.read`、`request_log.read` |

权限集合必须全部满足。角色名不能补回被租户矩阵撤销的 permission；前端也不能凭 role 展示未获授权
视图。

### 4.2 六角色默认配置

| 角色 | 默认视图 | 产品允许切换视图（仍需实时权限） |
| --- | --- | --- |
| `admin` | `executive` | executive、finance、security、developer、operations |
| `developer` | `developer` | developer、operations、executive |
| `viewer` | `executive` | executive |
| `finance_admin` | `finance` | finance、executive |
| `security_admin` | `security` | security、executive |
| `ops_admin` | `operations` | operations、executive |

角色只选择默认配置。请求执行时按以下顺序失败关闭：

1. `require_dashboard_reader` 从 Session 恢复 tenant/user/role/permission revision；
2. 后端按 `tenant_id + role_code` 精确读取一条 active 配置；
3. 解析并校验 allowed JSON，和固定视图目录做 exact-set 子集检查；
4. 用当前 `context.permission_codes` 过滤可见视图；
5. 显式请求的已知视图不在配置或权限交集中返回 403；未知/重复 query 返回 422；
6. 未显式请求时优先默认视图；若默认视图因降权不可用，安全回落到同配置内可用的 `executive`；
7. 配置缺失、重复、损坏或最终无可用视图返回统一依赖错误，不回落前端硬编码。

## 5. 数据表与迁移设计

### 5.1 `role_dashboard_configs`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `role_dashboard_config_id` | TEXT PK，`rdc_` + 32 小写 hex | 稳定配置 ID |
| `tenant_id` | TEXT NOT NULL | 业务 owner，复合关联当前租户角色 |
| `role_code` | TEXT NOT NULL | PRD-059 六角色之一 |
| `default_view_code` | TEXT NOT NULL | 五个固定 view code 之一 |
| `allowed_view_codes_json` | TEXT NOT NULL，JSON array | 去重、只含固定 view code，并必须包含 default |
| `config_revision` | INTEGER >= 1 | 后续受控变更使用；当前默认 1 |
| `status` | `active/disabled` | disabled 配置不能服务请求 |
| `created_at` | UTC TEXT NOT NULL | 创建时间 |
| `updated_at` | UTC TEXT NOT NULL | 当前 revision 时间 |

约束与索引：

- `UNIQUE(tenant_id, role_code)`；
- 复合 FK `(tenant_id, role_code) -> roles(tenant_id, code) DEFERRABLE INITIALLY DEFERRED`；同一
  tenant INSERT 上的多个 AFTER trigger 不依赖创建顺序，提交前只要求六角色和六配置同时完整；
- JSON 必须为数组、长度 1..5、元素全是字符串、无重复、无未知 code、包含 default；
- owner、ID、role、created_at 不可更新；受控配置更新必须使 `config_revision` 精确 `+1`；
- 禁止 `INSERT OR REPLACE` 绕过 owner/revision 语义；
- 索引 `idx_role_dashboard_configs_tenant_status(tenant_id,status,role_code)`；
- 服务查询始终以 `tenant_id` 为首条件。

### 5.2 0057 tenant 初始化

- 迁移为每个现有 tenant 原子插入六条 revision 1 默认配置；
- 新增 `AFTER INSERT ON tenants` 初始化触发器，为未来 tenant 同事务写入六条配置；
- 触发器与 PRD-059 的 RBAC 初始化触发器并存；复合 FK 使用 deferred 语义，不能依赖两个 AFTER
  trigger 的执行顺序，事务提交前必须同时看到对应角色；
- 迁移后验证每个 tenant 精确六条、默认/allowed 映射正确、无跨租户 owner、外键完整。

### 5.3 `console_page_events` 保真重建

0057 在 0053 当前 12 列结构上增加 `dashboard.role_viewed`，保留 rowid、全部历史数据、三个既有索引和
四个旧事件类型。设计时已对 56/0056 开发库执行只读 schema 复核：当前引用
`console_page_events` 的对象只有表本身和三个既有索引，没有后续 trigger/view 隐式依赖。新事件 payload
精确为：

- `source`: `sidebar/direct_hash/view_switch/refresh`；
- `hash`: 固定 `#overview`；
- `viewport_class`: `desktop/mobile`；
- `view_code`: 本次 GET 实际返回的 selected view；
- `config_revision`: 本次返回的正整数 revision。

事件不保存角色名、permission 列表、tenant 名称、邮箱、筛选、卡片值、成本数值、错误详情或正文。

### 5.4 down migration

- 先验证不存在 0057 之后迁移；
- 若配置 revision/status/default/allowed 已偏离 0057 canonical 默认，down 失败关闭，避免删除未来用户配置；
- 把 `dashboard.role_viewed` 映射回 `dashboard.viewed`，保留访问轨迹、rowid、request_id 和 trace_id；
- 删除 tenant 初始化触发器、配置索引和配置表；
- 最终执行 `integrity_check` 与 `foreign_key_check` guard。

## 6. API 设计

### 6.1 `GET /dashboard/role-view`

认证：必须先执行 `require_dashboard_reader`。匿名固定 401；缺少 `dashboard.read` 固定 403。认证和权限
必须先于 raw query 解析，避免无权限调用者利用 422 探测视图目录。

Query：

- `view` 可选，最多一个；只接受五个固定小写 code；
- 不接受 `tenant_id`、`role`、`user_id`、任意时间范围或未知 query；
- 重复 `view`、空值、大小写变体、数组、额外 query 统一 `dashboard.query_invalid`。

成功响应：

```json
{
  "config": {
    "role_dashboard_config_id": "rdc_...",
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
    "generated_at": "2026-08-01T11:00:00Z"
  },
  "cards": [
    {
      "code": "cost",
      "label": "成本",
      "status": "ok",
      "primary_value": 12.34,
      "primary_unit": "cny",
      "summary": "今日已入账成本",
      "facts": [],
      "target_navigation_item_id": "usage"
    }
  ],
  "empty_state": {"is_empty": false, "title": "", "description": ""},
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

响应不回传 `tenant_id`、完整 permission 列表、邮箱或角色显示名；这些事实已经由认证上下文约束，重复
回传只会扩大 DOM/日志泄漏面。`Cache-Control: no-store` 沿用认证 API 中间件。

### 6.2 兼容接口

`GET /dashboard/overview` 保持现有响应和权限合同，避免破坏旧客户端和 PRD-026 测试；PAGE-092 不在新
接口失败时回落旧 overview，因为回落会绕过角色视图和权限交集。

## 7. 六类卡片与聚合口径

新接口只返回聚合数值，不返回 Top Key、模型名称、request_id 列表或原始错误；明细继续由各自后端权限
接口保护。

| card code | 主要事实 | tenant/time 边界 | 状态规则 |
| --- | --- | --- | --- |
| `cost` | 今日正式 CNY 成本、Token、成功计费请求 | `usage_ledger` 当前 tenant + 上海今日 | 无数据 empty；不把 unknown price 当 0 元成功证据 |
| `security` | 今日 DLP matched/blocked、安全类失败、active policy 数 | `request_logs/policies` 当前 tenant | blocked 或安全失败为 warning/critical；不读 pattern/Prompt |
| `stability` | 请求数、成功率、错误率、P95 | 复用 PRD-026 口径 | 复用既有 95/90、5/10、1000/3000 阈值 |
| `adoption` | active Provider/模型/Project/Application/Virtual Key 完成项 | 当前 tenant 当前状态 | 五步完成度；只返回计数，不返回名称或 Key 前缀 |
| `provider_health` | active、healthy、unhealthy、observing/unknown | 当前 tenant；复用健康状态的 stale 计算 | unhealthy critical，observing/unknown warning |
| `budget_risk` | 今日 threshold/exhausted/blocked、open cost anomaly | 当前 tenant + 上海今日/当前 open | exhausted/critical anomaly 为 critical，threshold/open 为 warning |

全部查询在同一只读连接和一致性快照中执行，SQL 第一过滤条件必须是 `tenant_id`。数据库错误折叠为
`dashboard.unavailable`，损坏配置折叠为 `dashboard.config_invalid`；异常正文、路径和 SQL 不进入响应或
普通日志。

卡片跳转只返回固定 navigation ID，并满足双重条件：当前 permission 允许目标能力，且前端当前后端导航
快照也包含该 ID。任一条件不满足只展示数字，不渲染链接。

## 8. PAGE-092 前端与交互设计

### 8.1 页面结构

- 复用现有 `overview` 导航，不新增第二个首页入口；
- 标题为“角色首页”，紧凑显示上海今日窗口和最后更新时间；
- 五视图使用分段控制，仅渲染 API `available_views`；移动端允许两列换行，不使用横向溢出；
- 卡片是单层重复项，固定最小高度与事实行网格，不在卡片内嵌套卡片；
- 卡片顺序由后端 selected view 目录决定，前端不按角色重新排序；
- 空租户展示与当前角色相关的下一步，但动作仍要以后端导航可见性为准。

### 8.2 状态机与 owner

PAGE-092 独立状态机冻结：tenant ID、Session ID、permission revision、authorization fingerprint、
selected view、请求 generation。Token 只参与内存 owner 比较，不进入状态快照、DOM、日志或证据。

- 只有后端导航 `id=overview/target_section=overview` 且持有 `dashboard.read` 才发起 GET；
- 切换视图立即取消旧请求并清除旧视图卡片，避免新标签配旧数据；
- 迟到 200/401/403/503 只有 owner 完全一致才可生效；
- 401 清空认证和所有 Dashboard 状态；
- 403 立即清空配置、视图、卡片和命令，不保留旧受保护 DOM；
- 503 只在同一 authorization owner、同一 selected view 下保留已净化旧卡片为 `partial`；
- 下一次 200 清错并恢复 `ok`；
- 登出、换租户、permission revision/fingerprint 漂移、离开 overview、`pagehide` 都 abort 并清空。

### 8.3 视图切换与事件

视图选择只保存在当前页面内存，不写 hash query、URL、Storage 或 `user_preferences`。成功 GET 并完成 owner
复核后，才异步提交一次 `dashboard.role_viewed`；事件失败不阻断阅读，也不重试形成遥测风暴。同一
Session、同一 generation、同一 view 的自动刷新只记录一次，用户显式切换后可记录新 view。

## 9. 错误码设计

| code | HTTP | 场景 | retryable |
| --- | ---: | --- | --- |
| `auth.token_missing/invalid/expired` | 401 | 未登录或 Session 无效 | 否 |
| `auth.permission_denied` | 403 | 缺 `dashboard.read` 或请求已知但无权视图 | 否 |
| `dashboard.query_invalid` | 422 | 重复、空、未知 view 或额外 query | 否 |
| `dashboard.config_invalid` | 500 | 当前 tenant/role 配置缺失、重复、JSON 或 revision 损坏 | 否 |
| `dashboard.unavailable` | 503 | 数据库连接、只读聚合或依赖表暂不可用 | 是 |
| `console.event_invalid` | 422 | 伪造 view/config revision/path/hash/source/payload | 否 |
| `console.event_unavailable` | 503 | 页面遥测持久化失败 | 是 |

所有错误由统一中间件返回 `code/message/request_id/trace_id`，Header 与 body 链路 ID 必须一致。

## 10. 安全、权限与租户隔离

1. tenant、user、role、permission revision 只来自 `AuthContext`；API 不接收同名客户端字段。
2. 路由先认证/RBAC，再解析 raw query；无权限用户不能通过畸形参数取得不同错误侧信道。
3. 角色只选择默认配置，授权只看实时 permission；前端隐藏分段项和链接不能替代后端 403。
4. 配置 SQL、六类聚合 SQL 和事件 INSERT 均以认证 tenant 为第一条件；第二租户同名角色/视图不命中。
5. 配置表、角色表使用复合 owner；跨租户 INSERT/UPDATE、REPLACE、错误 revision 由数据库拒绝。
6. 页面和 API 不返回 Provider Key、Virtual Key 明文/哈希、Token、Authorization、Cookie、Prompt、
   Response、DLP pattern、Webhook secret、Endpoint、邮箱、原始错误正文或数据库路径。
7. 成本只使用现有正式账本/Usage 字段；unknown/unpriced 不能伪造为 0 成本正常。
8. 健康卡只返回状态计数，不返回 base_url、credential、维护备注或上游响应。
9. 事件 payload exact-set 校验，禁止借页面遥测保存卡片值、permission 列表、角色或业务筛选。
10. API 与浏览器证据使用 `.artifacts/prd065-*`；不访问或写入 `.tmp`，不提权。

## 11. 兼容性与性能

- 保留 `/dashboard/overview` 与 `dashboard.viewed` 历史事实；新页面改用 role-view，不删除旧接口；
- 不提升 PRD-059 permission catalog revision，本 PRD 复用已有权限码；
- 新增视图目录是服务端固定 metadata，不依赖前端版本；旧 Console 不会调用新接口；
- 单请求使用一个只读事务，禁止六张卡各自创建连接；
- 聚合只读当前 tenant 和上海今日窗口，复用已有 Dashboard/Provider/预算索引；0057 只补配置索引；
- 卡片与事实数量 fixed upper bound：最多 6 张卡、每卡最多 5 个事实、最多 5 个视图；
- 不做后台轮询新频率，沿用 Console 现有刷新节奏；切换视图使用单次 GET。

## 12. 测试用例

### 12.1 迁移与数据约束

1. fresh 0057、0056 -> 0057、重复迁移管理器执行、空事实 down/re-up 均通过；
2. 每 tenant 精确六条配置，默认/allowed/JSON/revision/status 与设计一致；
3. 新 tenant 触发器同事务创建六条配置，并正确复合关联六角色；
4. 非法 ID、role、view、JSON、重复 view、default 不在 allowed、跨 tenant owner、错误 revision、REPLACE
   均失败；
5. 0057 事件表重建保留 rowid、旧事件、索引和外键；新事件可写；
6. canonical 数据 down 把新事件映射旧事件；非 canonical 配置或后续迁移时 down guard 失败；
7. `integrity_check=ok`、`foreign_key_check=[]`。

### 12.2 后端主流程、权限和异常

1. 六角色默认 view 精确匹配表格；admin 可切五类，viewer 只能 executive；
2. 财务/安全/开发/运维返回不同卡片顺序和安全聚合值；
3. 自定义矩阵撤销所需 permission 后，该视图从 available 消失，手写请求返回 403；
4. 角色名不补权：即使 context role 是 finance/security，缺 permission 仍拒绝；
5. 第二租户写入大量成本、错误、DLP、Provider、预算和配置，不影响第一租户任何计数；
6. 上海日期前后边界、空租户、unknown price、stale Provider、budget threshold/exhausted 和 DLP matched 正确；
7. 匿名 401、无 dashboard.read 403；这两者优先于畸形 query；
8. 重复/未知/额外 query 422；配置损坏 500；数据库锁/缺表 503；均带 request_id/trace_id 且不泄密；
9. 旧 `/dashboard/overview` 响应保持兼容；
10. 事件只接受当前成功 view 和 revision；伪造 role/tenant/view/card/value/path/hash/source 均拒绝。

### 12.3 前端与状态机

1. 客户端不发送 tenant/role/user，只对后端已下发 view 做 URL 编码；
2. 六角色默认视图、admin 全视图和 revoked permission 的分段裁剪正确；
3. 卡片顺序完全来自后端，所有 API 文本转义；敏感 canary 不进入 DOM；
4. 切换时取消旧请求，迟到响应、换 Session、换 tenant、revision/fingerprint 漂移均淘汰；
5. 401 清 Session，403 清旧 DOM，503 同 owner/same view 才 partial，200 恢复；
6. 未授权角色/hash 不预取、不渲染 PAGE-092 命令；卡片链接还需后端导航 owner；
7. 事件只在成功渲染后发出，自动刷新去重，失败不阻断；
8. selected view 不进入 URL/Storage/user preference；注销后无旧卡片；
9. 1280x720 和 390x844 下分段项、标题、卡片、长中文/英文 code 无重叠、裁切或横向溢出。

### 12.4 实际运行验收

1. 在 `.artifacts/prd065-runtime/<run-id>` 使用隔离数据库升级 0057并验证约束；
2. 实际登录六角色，验证默认视图、admin 切换、撤权后端 403、第二租户隔离和统一错误；
3. 构造可追踪 Usage、失败请求、DLP、Provider 健康、预算和异常事实，经真实 API 聚合，不直接伪造响应；
4. Microsoft Edge 1280x720 与 390x844 验收至少六角色默认视图及 admin 切换，PNG 像素非空；
5. 扫描 Control/Console 日志、数据库事件、DOM、URL、Storage、Console 和证据，敏感 canary 命中为 0；
6. 清理隔离 Session/服务/浏览器 profile，恢复 Gateway/Control/Console 健康 200。

## 13. 代码修改计划

1. 新增 0057 up/down migration 与专项测试；
2. 在 Dashboard models/service/routes 增加固定目录、配置读取、权限交集、聚合和新 GET；
3. 增加 `dashboard.config_invalid` 错误目录、诊断和中英文标签；
4. 扩展 Console event model/service 和迁移事件 CHECK；
5. 新增前端 role-view client、PAGE-092 owner 状态机和角色首页渲染；
6. 修改 overview 编排、视图切换和成功后事件，不触碰其他导航业务状态；
7. 增加响应式样式、后端/前端/事件/迁移测试；
8. 先跑精确专项，修正后只复测失败节点；专项全绿后执行一次必要全量/受影响回归，不因单点失败反复
   从头运行；
9. 执行实际 API、六角色和双视口 Browser 验收；
10. 更新 README、目录说明、API 文档和 `docs/testing/PRD-065-tests.md`，全部通过后才标记完成。

## 14. 两轮编码前自审

### 14.1 第一轮：角色、权限、租户和数据边界

结论：**发现问题，修正后通过**。

1. 初稿曾考虑增加“老板/CTO”角色；这会破坏 PRD-059 固定六角色、迁移 CHECK、用户邀请和权限上限。
   已改为 `admin/viewer` 承接经营视图，不新增角色。
2. 初稿按 role 直接授权视图，会让被租户矩阵撤权的 Finance/Security 继续看旧卡。已改为 role 只选择
   默认配置，最终 available views 必须与实时 permission exact intersection。
3. 初稿用前端静态映射返回卡片；手写 view query 可绕开按钮。已改为后端读取 tenant config、校验 query
   并返回 403，前端只消费结果。
4. 初稿未覆盖新 tenant。已增加 tenant insert 触发器和复合角色 FK，现有 tenant backfill 与未来 tenant
   在同一迁移合同内；复核后又把 FK 改为 deferred，消除 PRD-059/065 两个 AFTER trigger 顺序假设。
5. 初稿安全卡可能返回 DLP rule/错误详情，Provider 卡可能返回 Endpoint。已收敛为计数和固定状态，不读
   pattern、正文、base_url 或 credential。
6. 复核所有配置、聚合、事件路径均从 AuthContext 恢复 tenant；未发现剩余跨租户输入面。

### 14.2 第二轮：兼容性、事件、状态机和可验收性

结论：**发现问题，修正后通过，可以进入编码**。

1. Excel API 清单把路径拆为三行；已按 PRD 总表冻结为单一 `GET /dashboard/role-view`，避免错误创建
   `/dashboard` 或 `/role-view` 根路由。
2. 直接替换 `/dashboard/overview` 会破坏 PRD-026 和旧客户端。已保留旧接口，PAGE-092 使用新接口且
   失败时不降级绕过角色视图。
3. 把 selected view 写入 user preference 会演变为个性化配置并产生跨 Session 陈旧授权。已改为纯内存
   切换，URL/Storage/数据库均不保存。
4. 初稿把 `dashboard.role_viewed` 当外部 Webhook，会为每次刷新制造高频投递和隐私噪音；现有
   `dashboard.viewed/status.viewed` 都是内部控制台遥测。已沿用该边界，并用 exact payload、成功后记录和
   自动刷新去重保证可追踪。
5. 初稿 503 可跨 view 保留旧卡，可能出现“财务标签 + 安全旧数据”。已限定只有同 authorization owner、
   同 selected view 才能 partial；切换视图先清卡。
6. 初稿事件在导航点击时先写，无法证明角色视图实际加载。已改为 GET 成功、owner 复核和 DOM 投影后再
   记录实际 selected view/revision。
7. 测试计划已覆盖六角色、矩阵撤权、第二租户、迁移 down guard、真实 Edge 双视口和全链路隐私；所有
   工件限定 `.artifacts`，不需要沙箱外权限。

完成两轮修订后未发现剩余设计阻断项，随后才开始 0057、后端和 PAGE-092 编码。

## 15. 实施与最终验收结果

实现严格沿用本设计冻结的边界：后端增加 tenant-scoped 角色首页配置、实时 permission 交集、五类视图、
六类有界聚合卡片和 `GET /dashboard/role-view`；Console 增加 PAGE-092 owner 状态机、分段切换和
`dashboard.role_viewed`；0057 完成配置表、新租户初始化、事件枚举扩展和受保护 down migration。旧
`GET /dashboard/overview` 保持兼容，前端没有增加身份输入、持久化视图或绕过后端权限的降级路径。

最终验收结论：

- 后端专项 `3 passed`，0057 迁移专项 `19 passed`，直接受影响 Python `17 passed`；
- PAGE-092 `14 passed`，旧 Dashboard `6 passed`，Provider Health 受影响组合 `14 passed`；
- 浏览器验收器合同 `6 passed`，Console lint `178 files passed`，中文注释门禁通过；
- 真实 HTTP 覆盖六角色、第二租户、401/403/422、实时撤权回落/恢复和事件防伪；
- Microsoft Edge 六角色均通过 `1280x720` 与 `390x844`，最终两张 PNG 非空且溢出、重叠和隐私命中为 0；
- 开发库与隔离验收库均为 `57/0057`、`integrity_check=ok`、外键违规 0；隔离活动 Session 已清至 0，
  财务权限已恢复，WAL/SHM 已清理；
- Gateway、Control API、Console 最终健康状态为 HTTP `200/200/200`。

完整命令边界、失败批次取舍、证据路径和 SHA-256 见
[`docs/testing/PRD-065-tests.md`](../testing/PRD-065-tests.md)，稳定接口见
[`docs/api/role-dashboard.md`](../api/role-dashboard.md)。PRD-065 可以标记完成；下一项为
`PRD-066 | M2-企业增强 | P2 | 企业Chat演示入口`，`all_108_prds_complete=false`。
