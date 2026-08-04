# 审计日志不可变导出 API

> PRD-058 已完成。本文是已经通过专项、迁移、真实 Runtime、Browser 和正式 artifact 验收的
> 公开 API、CSV、安全与事件合同；最终直接证据见 `docs/testing/PRD-058-tests.md`。

## 1. 能力边界

`GET /audit-logs/export` 为当前认证租户同步生成一份有界 CSV，并在返回文件前原子保存：

- 不可变 `AuditExport` 复核事实；
- 本次操作的 `audit.exported` 管理审计；
- 可恢复的 `audit.exported` source event。

CSV 只存在于当前 HTTP 响应内存中。数据库保存筛选指纹、快照水位、行数、字节数、SHA-256、
文件名和链路标识，不保存 CSV 正文。本接口不提供导出历史列表、服务端再次下载、任意列选择、
跨租户导出或对象存储归档。

“不可变”由数据库 trigger、复合外键和冲突门禁约束，覆盖 `UPDATE`、`DELETE`、`REPLACE`、
`INSERT OR REPLACE`、冲突 rowid 和负 rowid 绕过。它不等同于 WORM、第三方时间戳、电子签章
或对具备数据库文件权限的离线运维不可抵赖。

## 2. 认证、权限与租户

- 请求必须携带 `Authorization: Bearer <session_token>`。
- PRD-059 引入更细角色前，仅 `admin` 可以导出；`developer/viewer` 由后端
  `require_audit_exporter` 返回 403。
- `tenant_id`、操作者和角色只从已认证 Session 恢复，query、Header 或文件名均不能覆盖。
- 匿名或失效 Session 返回统一 401；前端是否显示按钮不构成权限判断。
- 所有成功和错误响应都包含 `X-Request-ID`、`X-Trace-ID`，错误正文也包含相同
  `request_id/trace_id`。

浏览器 CORS 只在 Control API 暴露 `Content-Disposition`、`ETag`、
`X-MetaHub-Audit-Export-ID`、`X-MetaHub-Audit-Export-Row-Count` 和统一链路头；不扩大 Gateway
浏览器接口。

## 3. GET /audit-logs/export

### 3.1 请求

示例：

```http
GET /audit-logs/export?action=provider.updated&created_from=2026-07-01T00%3A00%3A00%2B08%3A00&created_to=2026-08-01T00%3A00%3A00%2B08%3A00 HTTP/1.1
Authorization: Bearer <session_token>
Accept: text/csv
X-Request-ID: console_audit_export_1750000000000_abc123
```

只允许以下七个可选 query；每个名称最多出现一次：

| 参数 | 语义 | 约束 |
| --- | --- | --- |
| `actor_user_id` | 精确操作者 ID | 去除首尾空白后最长 160 字符，不得含 C0 控制字符 |
| `action` | 精确审计动作 | 必须来自服务端当前动作目录，不区分输入大小写 |
| `object_type` | 精确对象类型 | 必须来自服务端当前对象目录，不区分输入大小写 |
| `object_id` | 精确对象 ID | 去除首尾空白后最长 160 字符，不得含 C0 控制字符 |
| `request_id` | 被审计操作的精确 request ID | 这是筛选值，不是本次导出的 `X-Request-ID` |
| `created_from` | 创建时间下界 | 带时区 ISO-8601，闭区间 `created_at >= created_from` |
| `created_to` | 创建时间上界 | 带时区 ISO-8601，开区间 `created_at < created_to` |

若同时提供时间边界，`created_to` 必须严格晚于 `created_from`。时间会规范化为 UTC 秒级值。
空白值按未筛选处理；未知 query、重复 query、`tenant_id`、`limit`、`offset`、`columns`、
文件路径或非法枚举都会失败关闭，不会被 FastAPI 静默忽略。

### 3.2 request_id 与幂等

本次请求的 `X-Request-ID` 同时是导出 operation ID。幂等边界固定为：

```text
tenant_id + requested_by + request_id
```

- 同一管理员用相同 request ID 和相同规范化筛选重试，会从持久快照重新生成并复核完全相同的
  CSV 字节，不新增 `AuditExport`、审计或 source event。
- 同一管理员复用 request ID 但改变筛选，返回 `audit_export.request_conflict`。
- 同租户另一管理员使用相同 request ID，属于独立导出，不会命中前一管理员的事实。
- 每次新的人工导出必须使用新的 request ID；筛选参数 `request_id` 与 operation ID 不得混用。

### 3.3 成功响应

成功状态固定为 `200 OK`，正文是 CSV bytes，不是 JSON。关键响应头：

```http
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="metahub-audit-20260731-0123abcd.csv"
Cache-Control: no-store, private
Pragma: no-cache
X-Content-Type-Options: nosniff
ETag: "<64位小写SHA-256>"
X-MetaHub-Audit-Export-ID: aex_<32位小写十六进制>
X-MetaHub-Audit-Export-Row-Count: 123
X-Request-ID: <operation request_id>
X-Trace-ID: <trace_id>
```

文件名是服务端生成的 ASCII basename，不含租户、邮箱、筛选、对象名称或路径字符。`ETag`
就是完整响应正文的 SHA-256；行数不含表头。客户端必须同时校验状态、Content-Type、文件名、
导出 ID、强 ETag、行数和链路头，再创建 object URL。

## 4. CSV schema version 1

文件编码和边界固定为：

- UTF-8 BOM；
- CRLF 行结束符；
- 固定 18 列；
- 数据按 `created_at ASC, audit_log_id ASC` 排序；
- 最多 10,000 条数据行；
- 最多 16 MiB，包含 BOM、表头、引号转义和 CRLF；
- 空结果仍返回 BOM 与表头，并写入一条行数为 0 的不可变导出事实。

列顺序：

```text
audit_log_id
tenant_id
actor_user_id
actor_email
actor_role
action
object_type
object_id
object_name
before_summary_json
after_summary_json
changed_fields_json
client_ip
user_agent
request_id
trace_id
created_at
schema_version
```

`before_summary_json`、`after_summary_json` 和 `changed_fields_json` 使用规范 JSON。服务端先严格解码
存量 AuditLog，核对 action/object、摘要字段白名单和真实 changed fields；发现坏行会整份失败，
不会跳过或返回部分 CSV。

每个单元格先把 Unicode `C*` 类控制/格式字符归一为空格，再检查去除前导空白后的
`= + - @` 公式前缀；命中时添加单引号。最终值仍不满足安全不变量时，返回完整性错误而不是依赖
Excel/LibreOffice 的本地设置。

CSV 不包含数据库 rowid、稳定序列、筛选原文/指纹、导出事件内部状态、claim/lease、Provider Key、
Virtual Key 明文、Token、Authorization、Cookie、Secret、完整哈希、DLP 原始 pattern、Prompt、
Response 或任意原始请求/响应正文。

## 5. 快照与不可变复核

导出在一个 `BEGIN IMMEDIATE` 事务中：

1. 验证当前租户每条 AuditLog 都有唯一 `AUTOINCREMENT` 稳定序列映射；
2. 捕获当前租户最大稳定序列作为快照水位；
3. 通过数据库 cursor 逐行生成有界 CSV；
4. 写入不可变 `audit_exports`；
5. 写入 pending `audit_export_events` source；
6. 用预生成 AuditLog ID 写入 `audit.exported/audit_export`；
7. 复核复合外键和关系后提交，随后才允许返回 CSV。

导出自身的 `audit.exported` 序列晚于已捕获水位，因此不会进入当前首次导出的 CSV；下一次新导出
可以看到它。任一 CSV、AuditExport、AuditLog、事件或关系写入失败都会回滚，不能释放未留痕文件。

持久 `AuditExport` 保存状态 `completed`、格式 `csv`、schema version、规范筛选及其 SHA-256、
快照边界、行数、字节数、正文 SHA-256、文件名、actor、request/trace 和 source event ID；不保存
CSV BLOB。复合延迟外键与双向 trigger 保证它只能引用同租户、同 actor、同 request/trace 的
`audit.exported` AuditLog。

## 6. audit.exported 事件

导出事务原子保存一条 `audit.exported` source event。固定 payload 为：

```json
{
  "audit_export_id": "aex_0123456789abcdef0123456789abcdef",
  "status": "completed",
  "format": "csv",
  "schema_version": 1,
  "row_count": 123,
  "content_size_bytes": 45678,
  "content_sha256": "<64位小写SHA-256>",
  "filter_fingerprint": "<64位小写SHA-256>"
}
```

事件不携带筛选原文、CSV、文件名、租户名称、邮箱、IP、User-Agent、Endpoint URL、Secret 或内部
lease。Control API 启动时和运行期间由 source worker 恢复投递：30 秒 lease、claim token/owner、
CAS 终结、`5/30/300` 秒有界退避，最多尝试 10 次。只有持有效 source lease 的 worker 可以恢复
已有 pending Webhook intent。

该链路提供可恢复的 at-least-once source 投递，不承诺网络 exactly-once。API 提交成功不依赖
租户 Webhook Endpoint 当次网络可用；接收端仍应按稳定 `event_id` 幂等。

## 7. PAGE-084 Console 合同

- 导出只能由 admin 在后端确认的 `audit-logs` 导航中显式点击；15 秒自动刷新只请求列表。
- 列表筛选与导出使用同一组七字段条件，但分页 `limit/offset` 不进入导出。
- tenant、用户、角色、Session ID/revision、导航和独立 generation 共同构成前端 owner。
- 离页、注销、身份变化、筛选变化或页面销毁会 Abort 并清除反馈；迟到 Blob 不创建 object URL。
- Blob 仅存在于单次调用栈；文件、筛选和导出状态不进入 URL、Storage 或普通日志。
- 成功只展示导出 ID、SHA-256、行数和 request ID；错误只展示白名单 code/request ID。
- 成功下载后只刷新审计列表，以显示新的 `audit.exported` 行，不自动再次导出。

这些前端限制用于状态和隐私收口，最终 RBAC 与 tenant 隔离始终由 Control API 执行。

## 8. 统一错误码

| 错误码 | HTTP | 可重试 | 场景 |
| --- | ---: | --- | --- |
| `auth.token_missing` / `auth.token_invalid` | 401 | 否 | 匿名、Token 或 Session 无效 |
| `auth.permission_denied` | 403 | 否 | developer、viewer 或其他非 admin 角色 |
| `audit_export.filter_invalid` | 422 | 否 | 未知/重复 query，或传入 tenant、分页、列选择等禁止参数 |
| `audit_log.filter_invalid` | 422 | 否 | 精确文本超长/含控制字符，action/object 枚举非法 |
| `audit_log.time_range_invalid` | 422 | 否 | 时间无法解析、无时区或结束不晚于开始 |
| `audit_export.request_conflict` | 409 | 否 | 同一 actor 复用 operation request ID 但改变筛选 |
| `audit_export.too_large` | 413 | 否 | 数据行超过 10,000 或最终 CSV 超过 16 MiB |
| `audit_export.integrity_failed` | 500 | 否 | 存量审计、序列、快照或重放 hash/规模复核失败 |
| `audit_export.unavailable` | 503 | 是 | 数据库、CSV 构造或导出基础设施暂不可用 |
| `infra.validation_failed` | 422 | 否 | 框架层请求参数类型校验失败 |

错误响应使用统一 JSON，只返回稳定 `code/message/suggestion/diagnosis/request_id/trace_id`。SQL、
数据库路径、原始筛选、CSV、响应头、异常正文和凭据不得进入响应或普通日志。

## 9. 兼容与演进

- `GET /audit-logs` 保持既有分页 JSON 合同；导出是独立固定路由，不复用分页参数。
- schema version 1 的列集合和顺序不得静默变化；新增列必须升级版本并同步客户端校验。
- PRD-059 如新增 SecurityAdmin，应只演进独立 `require_audit_exporter` 权限矩阵，不能放宽 tenant 来源。
- 0049 down migration 在已有 AuditExport、source、`audit.exported` 或相关 Webhook 证据时原子拒绝，
  不通过删除审计事实完成回滚。
- PRD-058 最终验证结果见 `docs/testing/PRD-058-tests.md`；权威 artifact 为
  `.tmp/prd058/artifacts/20260731T124500-r14`，其 `final-audit.json` 为 `status=passed`。
- PRD-059 演进角色矩阵时必须保持本接口的 tenant/actor Session 来源、统一错误与 request ID、
  固定 CSV 和不可变复核链；角色扩展不能成为跨租户或降权绕过。
