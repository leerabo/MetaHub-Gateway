# 租户白标 Branding API

本文档对应 `PRD-088 客户/渠道白标配置`。BrandingConfig 精确归属当前 tenant，后端始终从 Session
恢复 tenant 和 actor；调用方不能提交、查询或覆盖 `tenant_id`。总体状态保持
`all_108_prds_complete=false`。

## 1. 通用合同

- 受保护接口使用 `Authorization: Bearer <token>`，每次请求从数据库实时复核 RBAC。
- JSON 成功响应携带 `request_id`、`trace_id`；错误使用统一 ErrorResponse 和原 request ID。
- 受保护成功/错误响应均为 `Cache-Control: no-store, private`；PNG Logo 为
  `Cache-Control: private, no-store`，并带 `X-Request-ID`、`X-Trace-ID` 和 `nosniff`。
- PATCH 要求正整数 `If-Match` revision；客户端不能通过页面入口或隐藏按钮扩大权限。
- Provider Key、Token、Prompt、Response、Logo 正文、登录文案和域名原文不进入日志、审计、事件或验收证据。

## 2. 接口与权限

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `GET` | `/branding` | `branding.read` | 当前 tenant 的安全 Branding DTO |
| `GET` | `/branding/logo` | `branding.read` | 当前 tenant 的受限 PNG Logo |
| `PATCH` | `/branding` | `branding.read` + `branding.manage` | CAS 更新配置与事件事实 |
| `GET` | `/branding/public?tenant_slug=...` | 无 | 登录页固定形状公共投影 |
| `GET` | `/branding/public/logo?tenant_slug=...` | 无 | 同源安全 PNG 或固定 404 |

`branding.read` 默认授予六个固定角色；`branding.manage` 只授予不可变 admin 上限。动态撤权后旧 Session
下一请求立即失败，页面、导航、品牌 CSS 和 Blob URL 必须同步清理。

## 3. Branding DTO

受保护 DTO 只包含：`branding_config_id`、`product_name`、`primary_color`、`secondary_color`、
`login_title`、`login_message`、`custom_domain`、`custom_domain_status`、`logo_present`、
`logo_sha256`、`logo_width`、`logo_height`、`revision`、`status`、`updated_at`。

外层固定为 `branding`、`request_id`、`trace_id`。响应不包含 `tenant_id`、Logo 二进制、actor 邮箱、
Token、权限矩阵或事件正文。

公共投影只包含 `product_name`、`primary_color`、`secondary_color`、`login_title`、`login_message`、
`logo_present`。非法或不存在的 slug 返回与默认 MetaHub 相同的 200 投影，不暴露租户存在性、域名状态或 revision。

## 4. PATCH 合同

请求至少修改一个字段，只允许：

```json
{
  "product_name": "客户控制台",
  "primary_color": "#176B87",
  "secondary_color": "#F59E0B",
  "login_title": "欢迎使用客户控制台",
  "login_message": "企业 AI Gateway",
  "custom_domain": "console.example.cn",
  "logo_png_base64": "<bounded bare PNG base64 or null>"
}
```

省略字段保持不变；`custom_domain` 和 `logo_png_base64` 可用显式 `null` 清除。服务端拒绝未知字段、
控制字符、任意 CSS、远程 URL、data URL、SVG/HTML、交错 PNG、错误 CRC、越界尺寸和超限正文。
颜色接受大小写六位十六进制并规范化为大写。自定义域名仅保存静态校验后的 `reserved` 预留状态，
不执行 DNS、网络探测、TLS/证书或代理配置。

成功事务原子写入 BrandingConfig revision、不可变 `branding.updated` 审计、EVT-100 source event 和
冻结订阅快照的 outbox。Worker 使用租约、generation/token CAS、固定退避和十次失败终态；没有订阅时进入
`no_subscription`，仍可按 request/trace/event 追踪。

## 5. 错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 400 | `branding.request_invalid` | 未知字段、空更新、If-Match 或文本/颜色非法 |
| 400 | `branding.logo_invalid` | PNG 格式、CRC、尺寸或解压结构不安全 |
| 413 | `branding.logo_too_large` | 请求或 PNG 超过固定上限 |
| 400 | `branding.custom_domain_invalid` | IP、内网、保留域或非法域名 |
| 403 | `branding.forbidden` | 后端实时权限不足 |
| 404 | `branding.not_found` | 受保护配置缺失或损坏 |
| 404 | `branding.logo_not_configured` | 当前投影没有 Logo |
| 409 | `branding.revision_conflict` | If-Match 与当前 revision 不一致 |
| 503 | `branding.event_unavailable` | 审计、source event 或 outbox 无法安全提交 |
| 503 | `branding.unavailable` | 固定内部兜底 |

401 沿用 `auth.*`。所有错误只公开固定文案、`request_id`、`trace_id` 和安全 diagnosis，不回显提交值、
数据库路径、SQL 或底层异常正文。
