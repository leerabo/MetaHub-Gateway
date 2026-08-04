# 文件上传与文本解析 API

本文档描述 PRD-072 的文件入站合同。该能力属于 Control Plane 的低频企业工作台，不进入
Gateway 模型调用热路径，也不会自动把文件附加到 Chat、知识库或上游模型请求。当前版本支持
PDF、DOCX 和 UTF-8 TXT；OCR、隐藏内容检测、RAG、向量化和通用批任务分别留给后续 PRD。

## 1. 认证、租户与隐私边界

- 所有接口必须携带有效 Console Bearer Session；`GET` 实时要求 `file.read`，`POST` 实时要求
  `file.write`。上传写事务还会在数据库锁内再次复核权限，关闭鉴权后撤权竞态。
- `tenant_id`、用户和角色只从 Session 恢复。接口不接受 tenant、actor、对象键、DLP 规则或排序字段注入。
- 上传入口先完成 Session、RBAC 和进程解析容量门禁，再读取有界 multipart 正文。未授权或繁忙请求
  不读取文件 part，不产生明文 spool，也不创建 UploadedFile。
- 所有成功与错误响应都携带 `request_id`、`trace_id`，并返回
  `Cache-Control: no-store, private`。
- 原文件、抽取正文和原始文件名使用文件域独立根密钥、owner-bound AES-GCM 加密；数据库只保存
  认证密文、分域 HMAC、安全状态和计数。文件名只向当前 tenant 的已授权响应解密。
- 公共响应、普通日志、Webhook、审计摘要和 Console DOM 不包含抽取正文、内容 fingerprint、DLP
  pattern/命中片段、规则 ID、对象键、宿主路径或密钥。

## 2. 上传并解析文件

`POST /files`

请求必须是 `multipart/form-data`，且恰好包含一个名为 `file` 的文件 part；普通字段、重复 part、
嵌套 multipart、非法 Content-Disposition 或额外正文均被拒绝。调用方不得手工指定 multipart boundary。

允许的格式：

| 扩展名 | MIME | 内容门禁 |
| --- | --- | --- |
| `.txt` | `text/plain` | UTF-8 或 UTF-8 BOM；拒绝 NUL 与无效编码 |
| `.docx` | OOXML Word MIME | ZIP/OOXML 结构、entry 数量/体积/路径和外部关系门禁 |
| `.pdf` | `application/pdf` | `%PDF-`、页数、加密状态、结构与抽取上限门禁 |

默认文件上限为 20 MiB，部署可在 1..50 MiB 范围内调整。解析成功后，服务对完整抽取文本执行
当前 tenant 的 active `input|both` DLP 预扫描，计算确定性的 `file-token-estimator-v1` Token 估算，
再原子发布加密原文件和加密抽取正文。当前没有真实病毒引擎，因此状态必须如实为
`virus_scan_status=not_configured`，不能展示为 clean 或 passed。

成功返回 HTTP 201：

```json
{
  "file": {
    "file_id": "file_0123456789abcdef0123456789abcdef",
    "original_name": "治理方案.docx",
    "file_extension": "docx",
    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "size_bytes": 4096,
    "status": "ready",
    "virus_scan_status": "not_configured",
    "dlp_scan_status": "clean",
    "dlp_action": null,
    "match_count": 0,
    "failure_error_code": null,
    "upload_request_id": "req_upload_example",
    "extract": {
      "file_extract_id": "fext_0123456789abcdef0123456789abcdef",
      "status": "ready",
      "extractor_version": "docx-v1",
      "text_size_bytes": 768,
      "text_code_points": 420,
      "estimated_tokens": 180,
      "estimator_version": "file-token-estimator-v1",
      "failure_error_code": null
    },
    "created_at": "2026-08-05T08:00:00Z",
    "updated_at": "2026-08-05T08:00:01Z",
    "completed_at": "2026-08-05T08:00:01Z"
  },
  "request_id": "req_upload_example",
  "trace_id": "trc_upload_example"
}
```

DLP block 会先提交 `blocked` 安全事实和 `file.scanned`，再返回 403 `file.dlp_blocked`。该路径
不会保留原文件或抽取正文 Blob；调用方可用 `upload_request_id` 在列表中定位终态。解析或安全依赖
失败同样只保留固定错误码和安全计数，不回显底层解析器、SQL、文件名或正文。

## 3. 列出当前租户文件

`GET /files?status={status}&limit={limit}&cursor={cursor}`

需要 `file.read`。只接受以下 exact-set query：

- `status`：可选，`processing|ready|blocked|failed`；
- `limit`：可选，规范十进制 `1..100`，默认 25；
- `cursor`：可选，由上一页 `next_cursor` 原样回传的私有签名游标。

列表按 `created_at DESC,file_id DESC` 稳定排序，只查询当前 Session tenant。游标绑定 tenant、筛选和
分页位置，不能跨 tenant 或跨筛选复用。响应为：

```json
{
  "files": [],
  "next_cursor": null,
  "request_id": "req_list_example",
  "trace_id": "trc_list_example"
}
```

每个 `files[]` 元素与上传响应中的 `file` 使用同一安全投影。签名游标只驻留 Console 页面状态，
不得进入 URL、Storage、日志或 DOM。

## 4. 读取文件处理详情

`GET /files/{file_id}`

需要 `file.read`，不接受 query 或正文。`file_id` 必须匹配 `file_` 加 32 位小写十六进制。
不存在、格式非法和属于其他 tenant 的 ID 使用同形 404 `file.not_found`，避免跨租户枚举。

响应信封与上传成功响应相同。该接口返回抽取状态、字节/字符计数、Token 估算和固定失败码，
不会返回抽取正文。正文仅作为加密后端对象供后续明确授权的业务 PRD 使用。

## 5. 状态与事件

`UploadedFile.status` 只允许：

- `processing -> ready`：解析、DLP、加密对象发布和终态事务全部成功；
- `processing -> blocked`：DLP block，零正文 Blob；
- `processing -> failed`：解析、DLP 依赖、存储或完整性失败，零可用正文 Blob。

终态不可逆且只允许完全相同的幂等写。每个文件最多形成一条 `file.uploaded` 和一条
`file.scanned` source event；事件只含 tenant/file owner、格式、大小、固定状态和计数，不含文件名、
正文、DLP 规则或存储定位。Webhook 派发失败由现有 delivery/dead-letter 机制追踪，不回滚已提交文件事实。

## 6. 统一错误合同

| HTTP | code | 场景 |
| ---: | --- | --- |
| 400 | `file.multipart_invalid` | multipart shape、boundary、part 或额外字段非法 |
| 401 | `auth.token_missing/invalid/expired` | Console Session 缺失、无效或过期 |
| 403 | `auth.permission_denied` | 实时缺少 `file.read` 或 `file.write` |
| 403 | `file.dlp_blocked` | tenant DLP block，终态已提交且无正文 Blob |
| 404 | `file.not_found` | ID 非法、不存在或不属于当前 tenant |
| 413 | `file.too_large` | envelope 或文件正文超过部署上限 |
| 413 | `file.extract_limit_exceeded` | 页数、ZIP、文本体积或字符数超过边界 |
| 415 | `file.unsupported_format` | 扩展名、MIME 或魔数不在一致白名单 |
| 422 | `file.name_invalid` | 文件名为空、路径化、含控制字符或超过上限 |
| 422 | `file.structure_invalid` | PDF/DOCX/TXT 深层结构不安全或损坏 |
| 422 | `file.no_extractable_text` | 文档没有可用文本且未启用 OCR |
| 422 | `file.parse_failed` | 文本解析失败，底层异常不会透出 |
| 429 | `file.processing_busy` | 2 秒内未取得进程内解析容量 |
| 503 | `file.dlp_unavailable` | DLP 规则加载、编译或扫描失败，失败关闭 |
| 503 | `file.storage_unavailable` | 文件密钥、数据库或加密对象存储不可用 |
| 500 | `file.integrity_failed` | owner、状态、密文或终态关系不自洽 |

所有错误使用统一 `code/message/request_id/trace_id/suggestion/diagnosis` 结构和固定安全 message。

## 7. 部署配置与数据库版本

- `METAHUB_FILE_STORAGE_ROOT`：本地加密对象根目录，默认 `.data/file-storage`；
- `METAHUB_FILE_ENCRYPTION_SECRET`：文件域独立根密钥，生产环境必须显式设置且不得复用其他域密钥；
- `METAHUB_FILE_MAX_UPLOAD_BYTES`：默认 `20971520`，允许 1..50 MiB；
- `METAHUB_FILE_PARSE_CONCURRENCY`：单进程解析容量，默认 2，允许 1..8。

0064 把 RBAC catalog 提升到 revision 8，新增 `file.read/file.write`，并创建 tenant-qualified
`uploaded_files`、`file_extracts`、`file_dlp_matches` 和 append-only `file_events`。Admin、Developer、
Viewer 默认读写；SecurityAdmin 默认只读；FinanceAdmin/OpsAdmin 默认不授予。只有仍与 revision 7
产品矩阵完全一致的既有角色会自动扩权，自定义矩阵保持不变。down migration 在存在文件事实、相关
Webhook 订阅、权限自定义或后续依赖时失败关闭。
