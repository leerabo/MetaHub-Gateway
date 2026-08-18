# PRD-088 客户/渠道白标配置设计

状态：**已完成**（2026-08-18）。设计、两轮编码前自审、实现、迁移、测试与真实运行验收均已关闭。

总体状态：`all_108_prds_complete=false`。本设计只处理 PRD-088，不启动 PRD-089，也不重复实现
PRD-087 的伙伴门户能力。

## 1. 权威需求与边界

本设计交叉核对以下来源：

- Excel `01_PRD总表!89`、`03_M2-M3扩展交付!25`：PRD-088，M2-企业增强/P2，扩展层白标配置，
  目标为“客户/渠道白标配置”；角色为渠道/客户管理员；验收要求刷新后控制台品牌生效、审计记录修改。
- Excel `04_页面清单!121`：PAGE-120 白标设置页。
- Excel `05_API接口清单!401:402`：API-400 `PATCH /branding` 与 API-401 `branding` 对象合同。
- Excel `06_数据对象!122`：OBJ-121 `BrandingConfig`，字段至少包含 `id`、`tenant_id`、时间和状态。
- Excel `07_Webhook事件!101`：EVT-100 `branding.updated`，信封必须包含 event/tenant/time/actor/object/trace/
  payload/signature。
- Excel `08_验收用例!176:177`：TC-0175 主流程；TC-0176 权限、异常与安全边界，明确 M2 不做自动证书签发。
- README、目录说明、系统架构设计：控制 API 必须后端租户鉴权；业务对象带 `tenant_id`；审计和 Webhook
  按租户隔离；控制面不承载 Gateway 热路径；配置错误统一返回 code/request_id；数据留在境内基础设施。

### 1.1 与 PRD-087 的非重复边界

PRD-087 的 `partners` 表和 `PATCH /partners/branding` 只服务 Partner 门户内部的 `portal_name`、主色和
纯文本支持信息；它明确不上传 Logo、不处理登录页、不预留域名。PRD-088 新增独立的租户级 `BrandingConfig`
和 `PATCH /branding`，作用于整个控制台和登录页的安全公共投影。两个对象、权限、审计 action、事件和 API
不得互相复用或让一个接口修改另一个对象。

### 1.2 本期交付

- PAGE-120：管理员读取和修改当前租户的 Logo、产品名称、主/辅色、登录页标题/文案和自定义域名预留状态。
- 认证后控制台在刷新或登录后使用当前租户 BrandingConfig 渲染 logo、产品名、标题和颜色；撤权、登出或换租户
  立即清除 DOM、Blob URL、内存快照和页面标题。
- 登录页在用户输入 tenant slug 后请求固定形状的公共品牌投影；不存在或非法 slug 返回同样的默认投影，
  不返回 404、tenant_id、用户、状态或内部错误，从而不会把租户存在性变成差异错误。
- PNG Logo 作为受限二进制存储和下载，浏览器只接受同源、固定媒体类型和大小；不支持 SVG、远程 URL、HTML、
  CSS、脚本、外链字体或任意 data URL。
- 自定义域名只保存规范化的预留字符串和 `reserved` 状态；M2 不做 DNS 查询、域名切换、Cookie 域变更、
  TLS/证书签发、反向代理配置或外部网络访问。
- 更新在同一数据库事务内写 BrandingConfig、不可变 audit_logs、EVT-100 source event 和 Webhook outbox
  快照；提交后由受租约保护的 Worker 恢复投递，失败和无订阅均可追踪。

### 1.3 非目标

- 不实现自动证书签发、DNS/HTTP 验证、域名绑定、域名跳转或跨域资源加载。
- 不允许任意远程 Logo、SVG/HTML/脚本、上传原图元数据或用户可控 CSS。
- 不修改 PRD-087 Partner、PartnerCustomer、CommissionRecord、分润游标或伙伴门户业务规则。
- 不把 Provider Key、Virtual Key、Session Token、Prompt、Response、原始请求正文或 Logo base64 写入日志、
  审计摘要、Webhook payload、测试证据或浏览器 Storage。
- 不改变 Gateway 默认上游目录，不加入 OpenRouter/硅基流动默认 Provider，不做硬件适配。

## 2. 模块与数据流

### 2.1 后端模块

| 模块 | 修改/新增 | 职责 |
| --- | --- | --- |
| `shared-sdk/python/metahub_shared/rbac.py` | 修改 | catalog revision 23，增加 `branding.read`、`branding.manage` |
| `shared-sdk/python/metahub_shared/runtime/errors.py` | 修改 | Branding 固定错误码和安全公开文案 |
| `shared-sdk/python/metahub_shared/runtime/webhooks.py` | 修改 | EVT-100 payload 白名单、事件 ID 和签名合同 |
| `control-plane-api/src/metahub_control/branding/` | 新增 | models、PNG 校验、service、routes、source outbox、scheduler |
| `control-plane-api/src/metahub_control/audit/service.py` | 修改 | `branding.updated` action、`branding_config` 摘要白名单 |
| `control-plane-api/src/metahub_control/app.py` | 修改 | 注册路由和可停止 scheduler |
| `infra/migrations/0081_tenant_white_label_branding.sql` | 新增 | BrandingConfig、event/outbox、触发器、RBAC revision 23 |
| `infra/migrations/0081_tenant_white_label_branding.down.sql` | 新增 | 仅在无非默认 Branding/event/outbox 时原子回滚 |

### 2.2 前端模块

| 模块 | 职责 |
| --- | --- |
| `console-web/src/lib/branding.js` | 固定 URL、严格 DTO、PNG/响应校验、统一错误和附件请求 |
| `console-web/src/features/branding/` | PAGE-120 state、renderer、bind、Logo object URL 生命周期 |
| `console-web/src/features/auth/renderLoginPage.js` | 接收安全公共投影，只使用 textContent/固定 CSS 变量 |
| `console-web/src/features/console-shell/renderConsoleShell.js` | 接收认证后品牌快照并渲染 logo/产品名/颜色 |
| `console-web/src/features/developer-start/renderDeveloperStart.js` | PAGE-120 内容与 shell branding 参数透传 |
| `console-web/src/app/main.js` | 登录前 public preview、认证后 branding loader、owner 清理和提交生命周期 |
| `console-web/src/lib/permissions.js`、`i18n/messages.js`、`styles/main.css` | 权限常量、中文/英文文案、固定响应式样式 |

## 3. RBAC 与租户隔离

### 3.1 权限合同

新增 catalog revision 23：

| 权限 | 风险 | 默认可授权角色 | 后端语义 |
| --- | --- | --- | --- |
| `branding.read` | read | admin、developer、viewer、finance_admin、security_admin、ops_admin | 读取当前租户安全 BrandingConfig 和受限 Logo |
| `branding.manage` | critical | 仅 admin | PATCH 当前租户配置；必须同时具备 `branding.read` |

`branding.read` 是不含密钥的租户视觉配置，因此六个固定角色都可读取；`branding.manage` 只进入不可变 admin
上限，不能由前端隐藏设置按钮代替。Control 每个 GET/PATCH/Logo 请求都从当前数据库角色矩阵复核实时权限，
不接受 body/query 中的 `tenant_id`，也不从 Shared 默认集合回退授权。

### 3.2 数据隔离

每张业务表都带 `tenant_id`，所有主键、唯一索引、外键和查询均使用租户条件：

`AuthContext.tenant_id = BrandingConfig.tenant_id = BrandingEvent.tenant_id = Outbox.tenant_id`

`branding_id`、event_id、outbox_id 仅作为不透明定位 ID；跨租户 ID、Logo、revision、审计和 outbox 恢复均返回
同形固定错误。公共登录投影只接受规范 `tenant_slug`，不返回 tenant_id、内部状态、用户或权限，且不存在/非法
slug 统一返回默认投影，不能借此枚举租户。

## 4. 数据库设计

迁移编号固定为 `0081_tenant_white_label_branding.sql`，反向文件同名 `.down.sql`。所有时间为 UTC Z 整秒，
所有可变更新使用 `BEGIN IMMEDIATE` 和 revision CAS。

### 4.1 `branding_configs`（OBJ-121）

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `branding_config_id` | TEXT PK `brc_` + 32 hex | 不透明稳定 ID |
| `tenant_id` | TEXT NOT NULL UNIQUE FK | 每租户恰好一条 active 配置 |
| `status` | `active` | 仅用于状态合同，不提供 disable API |
| `product_name` | TEXT 1..80 UTF-8 bytes、纯文本 | 控制台产品名称 |
| `primary_color` | `#[0-9A-F]{6}` | 主色，禁止 CSS 注入 |
| `secondary_color` | `#[0-9A-F]{6}` | 辅色，禁止 CSS 注入 |
| `login_title` | TEXT 1..120 UTF-8 bytes、纯文本 | 登录标题 |
| `login_message` | TEXT 1..240 UTF-8 bytes、纯文本 | 登录页文案 |
| `custom_domain` | TEXT NULL，规范 ASCII/punycode 域名 <=253 | 仅预留，不解析、不激活 |
| `custom_domain_status` | `not_configured`/`reserved` | 由服务端依据是否有域名写入 |
| `logo_png` | BLOB NULL，<=131072 bytes | 仅允许安全 PNG；不出现在 JSON/审计 |
| `logo_sha256` | TEXT NULL/64 hex | 二进制完整性与 ETag 来源 |
| `logo_width`/`logo_height` | INTEGER NULL，16..512 | 非交错 8-bit RGB/RGBA |
| `revision` | INTEGER >=1 | 每次实际配置变化 +1 |
| `updated_by_user_id` | TEXT NULL 复合 FK users | 初始化为空，更新写当前 actor |
| `updated_request_id`/`updated_trace_id` | TEXT NULL | 最近成功更新链路，不存 Token/body |
| `created_at`/`updated_at` | TEXT NOT NULL | UTC Z 整秒 |

迁移为现有和新建 tenant 插入固定默认配置：`MetaHub`、`#176B87`、`#F59E0B`、固定中文登录文案、无 Logo、
无域名。新 tenant 通过 owner trigger 自动得到默认行。迁移 down 守卫必须验证所有配置仍为默认 revision 1、
没有 BrandingEvent/outbox 非空、没有 Logo 或域名，任何人工配置都原子阻断回滚。

### 4.2 `branding_events`（EVT-100 source）

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `branding_event_id` | TEXT PK `bev_` + 32 hex | 不可变事件 ID |
| `tenant_id`/`branding_config_id` | 复合 FK | owner 不可漂移 |
| `event_type` | 固定 `branding.updated` | 与 Webhook 信封一致 |
| `revision` | >=2、租户配置唯一 | 每次有效更新一条 |
| `actor_user_id` | 复合 FK users | 后端认证 actor |
| `request_id`/`trace_id` | 有界非空文本 | 链路追踪 |
| `changed_fields_json` | 扁平白名单数组、<=2KiB | 仅字段名，不含旧值/新值 |
| `payload_sha256` | 64 hex | canonical 安全 payload 哈希 |
| `created_at` | UTC | source 事实时间 |

### 4.3 `branding_event_outbox`

Outbox 使用 `pending/leased/delivered/no_subscription/failed` 终态、租约 token/generation、最多 10 次尝试、
固定退避和 `next_attempt_at`。更新事务先冻结当前 Webhook Endpoint original delivery snapshot（包括空快照），
再提交 Branding、Audit、Event 和 Outbox；Worker 只用 event row 重建七字段安全 payload，调用现有
`dispatch_webhook_event(..., resume_existing_pending=True, create_snapshot_if_missing=False)`，不追溯后来订阅。
租约失效、旧 worker CAS、重复终态、跨租户 event 或 payload hash 不一致均失败关闭且不出网。

## 5. API 与统一错误

所有受保护响应均为 JSON `{... , request_id, trace_id}`，成功和错误都发送 `Cache-Control: no-store, private`。
错误体只包含固定 `code`、`request_id`、`trace_id` 和安全文案；不回显 Logo、域名、请求正文、SQLite 或底层异常。

### 5.1 `GET /branding`

权限：`branding.read`。返回 `BrandingConfig` 精确键：

`branding_config_id, product_name, primary_color, secondary_color, login_title, login_message,
custom_domain, custom_domain_status, logo_present, logo_sha256, logo_width, logo_height, revision,
status, updated_at, request_id, trace_id`。

不返回 `logo_png`、tenant_id、actor 邮箱或任何事件正文。

### 5.2 `GET /branding/logo`

权限：`branding.read`。成功仅返回 `image/png`、固定 `Content-Length`、`ETag: "sha256-<64hex>"`、
`X-Request-ID`、`X-Trace-ID` 和 `Cache-Control: private, no-store`；没有 Logo 返回固定
`branding.logo_not_configured` 404 JSON。请求不接受 tenant_id、URL 或 filename。

### 5.3 `PATCH /branding`

权限：`branding.read + branding.manage`；必须带规范十进制 `If-Match` revision。JSON 只允许：

```json
{
  "product_name": "客户控制台",
  "primary_color": "#176B87",
  "secondary_color": "#F59E0B",
  "login_title": "欢迎使用客户控制台",
  "login_message": "企业 AI Gateway",
  "custom_domain": "console.example.cn",
  "logo_png_base64": "<base64 PNG 或 null>"
}
```

每次请求至少改变一个字段；省略字段保持不变，显式 `null` 只允许清除 Logo/自定义域名。服务端严格验证
UTF-8 字节数、控制字符、颜色、域名、base64、PNG signature/chunk CRC/尺寸/非交错/解压后的 scanline 大小，
不接受 SVG、JPEG、远程 URL、data URL、脚本或任意未知键。成功返回与 GET 相同的安全 DTO、revision+1、
request_id/trace_id；无变化、If-Match 错误、权限不足、跨租户 ID 或 Logo 不安全都使用固定错误码。

### 5.4 `GET /branding/public?tenant_slug=...`

这是登录页唯一的未认证公共投影，用于显示登录标题、文案、颜色和同源 logo URL。参数只接受已有登录表单
使用的规范 slug，响应永远是 200 固定键集合：无效/不存在 slug 与默认配置完全同形；不包含 tenant_id、状态、
revision、用户、权限、域名预留状态或内部错误。请求只在 Console 同源使用，`no-store`，不写访问日志正文。
Logo 通过同样固定形状的 `GET /branding/public/logo?tenant_slug=...` 返回安全 PNG；不存在时返回同形默认 Logo
或固定 404，不区分“tenant 不存在”和“未配置 Logo”。该公共能力不执行 DNS/网络请求。

## 6. 后端交互与事件

1. `GET /branding` 先从 AuthContext 读取 tenant，再从数据库复核 live `branding.read`，不得信任前端权限镜像。
2. PATCH 在 `BEGIN IMMEDIATE` 内复核 `branding.read/manage`、If-Match、字段和 PNG，计算前后安全摘要与
   `changed_fields`，更新配置 revision。
3. 同一事务写 `audit_logs` action `branding.updated` / object_type `branding_config`，摘要仅包含 revision、
   字段哈希、颜色、logo 尺寸/哈希和域名状态；不含 Logo 正文、登录文案原文或域名原文。
4. 同一事务写 BrandingEvent，payload 只含 `branding_config_id`、revision、changed_fields、logo_present、
   logo_sha256、custom_domain_status；`build_webhook_event` 负责 tenant/actor/time/trace/signature。
5. 同一事务冻结 Webhook snapshot 并写 outbox；提交后 scheduler worker 续租和派发，结果记录在 outbox。
6. 事务失败整体 rollback；提交后外部 Webhook 失败只能更新 outbox，不回滚已经成功的 BrandingConfig。

## 7. 前端交互与生命周期

### 7.1 PAGE-120

- 后端导航增加 `branding-settings`，只在 `branding.read` 下可见；管理表单还必须具备 `branding.manage`。
- 状态 owner 固定 tenant/user/role/session/permission_revision/authorization_fingerprint/navigation/generation。
- 首屏读取只调用 `/branding`；Logo 用 Authorization fetch 后创建 Blob URL，永不把 base64 写入 snapshot、DOM
  属性、Storage、URL、console 或错误。
- 表单使用 `input type=file accept=image/png`、名称/颜色/文案输入、域名预留输入；保存时仅发送严格允许字段，
  Logo 清除为显式 null。提交期间禁用重复操作，If-Match 冲突只更新 revision，不自动覆盖用户草稿。
- 成功后重新读取当前 owner，更新 shell CSS variables、产品名、标题和 Logo；审计/事件状态只显示安全 code。
- viewer/read-only 仍可看当前品牌和预览，不出现保存/清除按钮；无 `branding.read` 时页面零 DOM/零预取。

### 7.2 登录页与全局 shell

- 输入 tenant slug 后 debounce 请求公共投影；tenant slug、generation 或 login owner 改变时 abort 并清理旧 Logo URL。
- 登录页只用 textContent 或已验证 CSS 变量，不拼接远程 HTML；公共投影错误回退固定 MetaHub 默认，不显示错误正文。
- 认证后 shell 使用 `/branding` 安全 DTO；刷新、换 tenant、401/403、logout、pagehide 前同步销毁 Blob URL、
  CSS variables、`document.title` 和品牌 DOM，不能把 A 租户品牌带到 B 租户。
- 自定义域名只在设置页显示“预留”；不改变当前 origin、Cookie、CORS 或导航地址。

## 8. 固定错误码

| code | HTTP | 触发 |
| --- | ---: | --- |
| `branding.request_invalid` | 400 | 未知字段、空更新、文本/颜色格式非法 |
| `branding.logo_invalid` | 400 | 非 PNG、CRC/尺寸/解压结构不合法 |
| `branding.logo_too_large` | 413 | JSON 或 PNG 超过固定大小 |
| `branding.custom_domain_invalid` | 400 | IP、内网、保留域名或非法 ASCII/punycode |
| `branding.revision_conflict` | 409 | If-Match 与当前 revision 不一致 |
| `branding.not_found` | 404 | 仅受保护记录损坏/缺失，返回同形错误 |
| `branding.logo_not_configured` | 404 | 当前租户没有 Logo |
| `branding.forbidden` | 403 | 后端实时权限不足 |
| `branding.event_unavailable` | 503 | source event/outbox 快照无法安全写入 |
| `branding.unavailable` | 503 | 数据库或安全配置不可用 |

## 9. 测试计划

### 9.1 Migration/数据库

- 空 SQLite `0081` fresh/repeat/down/re-up；每阶段 `integrity_check=ok`、外键违规 0、schema version 81。
- 既有 tenant 默认 BrandingConfig、new tenant trigger、每租户唯一约束、所有 FK/owner trigger 和 RBAC
  revision 23 exact hash。
- 默认库可安全 down；修改名称/Logo/域名、写入 event/outbox、手工删 owner 时 down guard 原子阻断，数据库不半回滚。

### 9.2 Control/Shared

- GET/PATCH 主流程、字段边界、PNG 安全解析、Logo 清除、域名预留、严格响应键、no-store、request/trace。
- admin 读写、六角色读、无权限 403、动态撤权、跨租户 branding/logo/event/audit/outbox 零读取。
- CAS 并发成功/冲突、重复 PATCH、同 revision stale worker、租约过期恢复、空订阅 terminal、Webhook delivery
  快照不追溯后来订阅、重复 event 不重复出网。
- audit action/summary 白名单、事件 payload exact、敏感哨兵零命中、底层异常统一折叠。
- public slug 固定 200/默认投影、非法/不存在/换 slug 无差异错误，Logo 不泄露 tenant_id 或内部状态。

### 9.3 Console/浏览器

- strict client rejects tenant_id、未知字段、base64/URL/颜色/域名漂移和错误正文；Logo Blob URL 成功/失败/换租户
  均 revoke。
- PAGE-120 admin 保存、viewer read-only、动态撤权、迟到 200/401/403/503 丢弃、logout/pagehide/tenant switch 清理。
- 登录页 slug 品牌 preview、认证后 shell 刷新生效；390x844、1280x720、1440x900 无横向溢出、非空、无遮挡。
- 真实 HTTP Control/Console/Chromium 运行，检查请求合同、PNG 响应、审计/event/outbox 状态、敏感明文 0、
  SQLite sidecar 0、`all_108_prds_complete=false`。

## 10. 代码修改计划

1. 先修改 Shared RBAC/errors/webhooks/audit 白名单并生成迁移 0081 正反文件，运行迁移合同。
2. 实现 PNG/domain validators、Branding service/routes、event/outbox/scheduler，补 Control/Shared 专项。
3. 实现 strict client、PAGE-120 state/render/bind、shell/login branding 和 i18n/CSS，补 Console 专项。
4. 运行两轮静态审查：第一轮查 tenant/RBAC/CAS/敏感值/回滚；第二轮查兼容、迟到响应、对象 URL、公共投影和
   事件恢复；修正后再运行全部受影响专项。
5. 更新 README、目录说明、API 文档、测试记录、Excel `12_开发状态`，执行审计、真实页面验收，提交并推送。

## 11. 编码前两轮自审记录

### 第一轮：安全与一致性审查（已完成）

- **发现**：直接把 Logo base64 放进 BrandingConfig JSON 会让日志、审计、浏览器快照和 Webhook 泄露大正文；
  公共 `/branding` 404 会枚举 tenant；自定义域名若做 DNS 探测会引入 SSRF。
- **修正**：Logo 只存受限 PNG BLOB，JSON 只返回哈希/尺寸；公共接口固定 200 默认投影；域名只做静态语法
  预留，禁止 DNS/TLS/网络；审计和事件只保存哈希/枚举。
- **发现**：只写 Branding 后再派发 EVT-100 会在进程崩溃窗口丢事件；直接重建订阅会把后来 Endpoint 错误纳入旧事件。
- **修正**：在同一事务写 immutable source event、冻结 original delivery snapshot 和 outbox；Worker 使用
  `resume_existing_pending`、CAS lease 和空快照 terminal。

### 第二轮：并发、兼容与生命周期审查（已完成）

- **发现**：PATCH 只校验 `branding.manage` 会让读权限撤销后仍可写；前端只隐藏保存按钮会留下后端后门；
  Partner branding 和 tenant branding 共用 revision 会产生跨域覆盖。
- **修正**：PATCH 强制 `branding.read + branding.manage` 实时复核；Partner 与 BrandingConfig 使用独立表、
  权限、event/action 和 revision；所有查询以 AuthContext tenant 为唯一 owner。
- **发现**：定时刷新、logout、换 tenant 或公共 slug 迟到响应可能把旧 Logo URL/颜色写回新页面；If-Match 冲突自动
  重试会悄悄覆盖用户修改。
- **修正**：所有 async lane 绑定 owner/generation/AbortController，接受响应前复核；统一清理 CSS/Blob/title；
  409 只返回安全当前 revision，让用户明确重载/重试，不自动合并。
- **第二轮补充核验**：`audit_logs` 是统一查询/导出事实，数据库 action/object CHECK、不可变/owner 触发器、
  `audit.service` 白名单和导出 decoder 必须一起保真扩展 `branding.updated`/`branding_config`；不能只写旁路 logger。
  0081 up/down 必须以现有审计行数、索引、触发器和外键为守卫，失败时整库原子回滚。Branding source event、
  audit 与 outbox 必须在同一事务中写入，outbox 只冻结提交时的 endpoint 快照，worker 恢复时回读权威 BrandingConfig。
- **结论**：上述边界已写回数据、API、前端、测试和代码计划；自审通过后才允许进入编码阶段。

## 12. 完成结论

- 0081、RBAC revision 23、tenant-qualified BrandingConfig、EVT-100 source outbox、后端实时权限校验、
  PAGE-120、公共登录品牌和认证后 Console shell 已实现。
- Branding 与迁移专项、Console Branding 单测和全量回归、Ruff/lint、真实 HTTP、三种视口 Browser、
  SQLite 完整性/外键和敏感明文检查均通过；outbox 实际恢复到 `no_subscription` 终态。
- Excel、README、`docs/prd` 与 `docs/testing` 交叉确认下一条未完成项为 PRD-089；本次不实现 PRD-089。
  总体状态保持 `all_108_prds_complete=false`。
