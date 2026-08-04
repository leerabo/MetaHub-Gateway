# PRD-072 - 文件上传与文本解析基础

状态：**已完成（2026-08-05）**。设计先行及两轮编码前自审、实现、迁移、相关分层回归、真实 Runtime、
Browser 双视口、权限/租户/统一错误与隐私验收均已通过。

总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

已交叉核对 `企业级AI_Gateway_PRD开发总清单_给CodexClaude.xlsx` 的总表、M2/M3 扩展交付、
页面、API、数据对象、Webhook 和验收用例工作表：

- `PRD_ID`：`PRD-072`
- 里程碑：`M2-企业增强`
- 优先级：`P2`
- 能力层：`扩展层`
- 模块：`文件解析`
- 名称：`文件上传与文本解析基础`
- 主要用户：开发者、业务用户
- 页面：`PAGE-101 文件上传组件`、`PAGE-102 批量任务页`
- 权威公开 API：`POST /files`、`GET /files/{id}`
- 核心对象：`OBJ-096 UploadedFile`、`OBJ-097 FileExtract`
- Webhook：`EVT-079 file.uploaded`、`EVT-080 file.scanned`
- 主流程用例：`TC-0143`
- 边界/异常/权限用例：`TC-0144`

API 工作表把总表中的两个路由机械拆成 `POST/files/GET/files/{id}` 五行。本设计以总表的两个完整
路由为权威，不把拆词误实现为五个接口。为 PAGE-102 在刷新后仍能恢复当前租户的处理记录，本设计另提供
只读辅助接口 `GET /files`；它只返回文件处理安全摘要，不创建 PRD-074 的 BatchJob、Scheduler 或 Worker。

PRD-071 已完成。按 Excel 中 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3`、里程碑内优先级和原始行顺序，
下一条未完成项确认为 PRD-072，不是仅按编号猜测。

### 1.2 原始范围与验收

本条必须支持：

1. PDF、Word、TXT 上传。
2. 文件大小限制。
3. 病毒扫描占位。
4. 文本抽取。
5. DLP 预扫描。
6. Token 预估。
7. 敏感文件可阻断，大文件返回 Token 预估，文件严格按租户隔离。
8. 格式、大小、结构化文件输入、异常和权限路径返回统一错误码及 `request_id`。

明确不做知识库和向量库。

### 1.3 架构约束

系统架构设计文档 3.2、4、5、11.4、12 和 13 明确：

- 文件解析属于低频控制面/企业 Chat 扩展能力，不进入 Gateway 模型流量热路径。
- PostgreSQL 保存业务元数据，对象正文使用本地磁盘或 S3/MinIO；当前单机开发环境允许本地磁盘。
- 基础 DLP 命中只保存规则 ID 和安全摘要，不保存命中明文。
- 所有业务对象必须 tenant-qualified，管理 API 必须在后端逐请求鉴权。
- 单机模式可使用本地磁盘，小集群再切独立对象存储；因此存储实现必须有明确后端边界，不能把宿主路径
  暴露为公共 API 合同。
- 删除功能一旦提供，必须联动数据库和对象存储；本 PRD 没有 DELETE API，不提前实现生命周期删除。

## 2. 需求理解

PRD-072 的核心不是把文件直接塞进模型请求，而是建立一条可追踪、可隔离、可失败关闭的文件入站链路：

```text
Session/RBAC/tenant
        |
        v
有界 multipart -> 格式/魔数/结构校验 -> 文本抽取 -> tenant DLP 全文预扫描
        |                                      |
        |                                      +-- block/unavailable -> 不保留正文
        v
病毒扫描占位(not_configured) -> Token 估算 -> 独立密钥加密对象存储
        |
        v
UploadedFile + FileExtract + file.uploaded/file.scanned 安全事实
```

成功并不表示文件“无病毒”。在没有真实病毒引擎时，返回值只能是 `virus_scan_status=not_configured`；
任何代码、页面、事件或测试都不得把它显示为 `clean`、`passed` 或“已安全”。DLP 与病毒检测是两个独立维度。

原文件、抽取正文、文件名、DLP pattern、命中片段、Authorization、Session Token、Provider Key、Prompt 和
Response 都不得写入普通日志、Webhook、审计摘要或前端非必要 DOM。文件名为必要交互字段，但数据库中加密保存，
仅向当前 tenant 且具备 `file.read` 的响应解密投影。

## 3. 范围与非目标

### 3.1 本 PRD 实现

- 单文件 `multipart/form-data` 上传，字段名固定为 `file`。
- `.pdf`、`.docx`、`.txt` 三种格式；Excel 中的 Word 在本条明确收敛为 OOXML `.docx`。
- 部署级上传大小上限，默认 20 MiB，合法范围 1..50 MiB。
- PDF、DOCX、UTF-8 TXT 的同步文本抽取。
- DOCX ZIP 包、PDF 页数、文本体积、文件名和 MIME/魔数一致性基础防护。
- 当前 tenant 的 active `scan_scope=input|both` DLP 规则全文预扫描。
- `file-token-estimator-v1` 确定性 Token 预估。
- 原文件、抽取文本和原始文件名使用独立根密钥、owner-bound AES-GCM 加密保存。
- `UploadedFile`、`FileExtract`、安全 DLP 命中摘要及两个 source event。
- `POST /files`、`GET /files/{file_id}`，以及 PAGE-102 所需最小 `GET /files` 列表投影。
- `file.read`、`file.write` 两个后端权限点，RBAC catalog revision 从 7 提升到 8。
- PAGE-101 上传组件和 PAGE-102 文件处理记录工作台。
- 0064 正向/反向迁移、详细中文注释、主/异常/权限测试和真实运行验收。

### 3.2 明确不做

- 不做 `.doc` 旧二进制 Word；固定返回 `file.unsupported_format`。
- 不做图片、Excel、PPT、Markdown、HTML、压缩包或音视频解析。
- 不做 OCR、隐藏字体、隐藏层、批注隐写、二维码、宏内容识别、嵌套压缩包检测或安全报告导出；这些属于
  PRD-073。
- 不做知识库、切块、Embedding、向量索引、RAG 或检索。
- 不把文件自动附加到 Chat 或调用模型。
- 不做 PRD-074 的通用批量任务队列、优先级、租户并发、重试、Scheduler 或 Worker。
- 不接入真实杀毒 SaaS/ClamAV 集群；只实现诚实、可追踪的占位状态。
- 不新增文件下载、删除、分享或公开 URL。
- 不在 Gateway 数据面增加文件上传入口。
- 不把 OpenRouter、硅基流动作为默认上游；本条也不调用任何上游模型。

## 4. 现有代码基线

### 4.1 Control API 与认证

- `metahub_control.auth.dependencies` 已有从 Session 恢复 tenant、用户、角色和数据库权限矩阵的逐请求依赖。
- 多个敏感写路由已采用“认证/RBAC dependency 先执行，再手工读取有界正文”的模式；文件上传必须沿用该顺序，
  不能把 `UploadFile` 声明成路由 body 参数而让框架在鉴权前解析 multipart。
- `require_transaction_permission()` 可在写事务内重新读取 Session 与权限矩阵，关闭鉴权后撤权竞态。
- 所有错误由 Shared `ERROR_SPECS` 和 FastAPI handler 形成统一 `code/message/request_id/trace_id/suggestion/diagnosis`。

### 4.2 DLP

- 现有 `dlp_rules` 已支持 `keyword|regex`、`allow|log_only|block`、`input|output|both`、优先级和 active 状态。
- Gateway 输入 DLP 只扫描 20,000 字符；文件全文扫描不能静默复用该截断上限。
- `compile_safe_dlp_regex()` 是控制面和数据面的共同安全正则边界；文件扫描复用该编译器，但使用独立
  `file-dlp-v1` 扫描器和全文遍历上限。
- 现有 `dlp_matches` 依赖 Gateway request claim，不能拿来保存文件事实；0064 新增 file owner 下的安全摘要表。

### 4.3 存储和加密

- Prompt 模板已有独立根密钥、HKDF 分域和 owner-bound AES-GCM 模式，可复用设计原则，禁止复用模板密钥。
- 当前仓库尚无文件对象存储业务层；架构允许单机使用本地磁盘，未来可替换 S3/MinIO。
- 当前 Python 运行环境已具备 `pypdf`、`python-docx` 和 `python-multipart`，容器依赖文件尚未声明，0064
  实现必须补齐部署依赖。

### 4.4 Console

- Console 是原生 ES module 单页应用，导航由 Control 后端按当前数据库权限矩阵裁剪。
- PAGE-098/PAGE-099 已有 owner/generation、401/403 清理、列表/详情和移动端布局模式。
- 前端权限裁剪只改善体验，不能代替 POST/GET 路由的后端权限校验。

## 5. 总体处理状态机

### 5.1 状态定义

`UploadedFile.status`：

- `processing`：multipart 已通过基础边界、已建立持久 owner，解析/扫描尚未形成终态。
- `ready`：文本抽取、DLP 和加密对象发布均成功。
- `blocked`：DLP 命中 block；正文对象未发布。
- `failed`：格式、解析、DLP 依赖、存储或完整性失败；正文对象未保留或已清理。

`FileExtract.status`：

- `ready`：抽取文本已加密发布。
- `blocked`：抽取成功但 DLP 阻断，只有计数和安全扫描事实，不保留正文对象。
- `failed`：抽取或后续安全处理失败，只有固定失败码。

公共 API 不会把 `processing` 猜成成功。同步请求若进程中断，遗留 `processing` 是可观测崩溃事实；本 PRD
不引入后台恢复 Worker。后续读取明确显示“处理中/需运维复核”，不会自动重新解析或绕过 DLP。

### 5.2 处理顺序

1. FastAPI 先执行 Session 与 `file.write` 后端 RBAC，未授权时不读取文件正文。
2. 校验 `Content-Type`、可选 `Content-Length` 和 multipart envelope 上限。不能依赖 Starlette
   `request.form()` 的 `max_part_size`，因为不同兼容版本对 file part 可能只执行 spool 而不执行同一硬上限。
3. 直接用 `python-multipart` 的流式 callback 消费 `request.stream()`：envelope 上限固定为文件上限加
   64 KiB，恰好一个 `file` part、零普通字段；文件 bytes 只进入 20 MiB 上限的内存 `bytearray`，不产生
   系统临时目录中的明文 spool。每个 ASGI chunk 和 file-data callback 都二次累计，不能只信任 Header。
4. 规范化文件名；先完成扩展名、声明 MIME 和魔数的低成本三方一致性校验。不支持的输入只产生带
   request/trace ID 的固定失败日志，不创建半可信业务对象。
5. 生成 `file_id`，使用独立密钥计算原文件 keyed fingerprint 并加密文件名。
6. 写入 `processing UploadedFile` 和 `file.uploaded` source event，同一写事务内复核 `file.write`。
7. 对已经通过格式三方校验的内容执行深层容器检查及文本抽取。
8. 对抽取全文加载当前 tenant 的 active input/both DLP 规则并扫描。规则读取、正则编译或扫描故障均失败关闭。
9. 写入 FileExtract、每规则安全命中摘要，并把病毒占位固定为 `not_configured`。
10. 若 block：不创建原文件或抽取正文 Blob，把 owner 推进为 `blocked`，写 `file.scanned` 后返回
   `file.dlp_blocked`。
11. 若允许：使用独立密钥加密原文件和抽取文本，先写同一 storage root 下的 staging 文件，再用 `os.replace`
    原子发布两个不可预测对象键。
12. 在单一数据库事务内把 `UploadedFile/FileExtract` 推进为 `ready` 并写 `file.scanned`；提交失败时尽力删除
    本次已发布对象。对象始终是认证加密密文，即使进程在极窄窗口崩溃留下孤儿，也不含可读正文。
13. 数据库提交后机会性投递两个 Webhook；投递失败由现有 delivery/dead-letter 机制追踪，不反向撤销已提交文件。

### 5.3 同步边界

本条采用同步解析，原因是只有两个权威对象、两个资源 API，且 PRD-074 才引入 Batch Scheduler/Worker。
默认 20 MiB、PDF 最多 2,000 页、DOCX 最多 2,048 ZIP entry、解压总量最多 64 MiB、抽取 UTF-8 最多
8 MiB。任一边界超过即形成 failed 终态并返回固定错误，不把任务偷偷排入不存在的队列。

同步不等于占用 ASGI event loop：应用启动时创建进程内 `anyio.CapacityLimiter`，默认容量 2、配置范围 1..8。
路由在读取 multipart 正文前最多等待 2 秒取得容量，失败返回 `file.processing_busy`；取得容量后，multipart
流控留在异步路由，PDF/DOCX/TXT 深层解析、DLP 和加密计算通过受限 worker thread 执行。这样既不引入
PRD-074 的持久任务队列，也不会让多个 20 MiB 请求无界占用内存或阻塞健康检查。

## 6. 文件格式与解析安全

### 6.1 通用边界

- 原始文件默认最大 20 MiB；配置只允许 1..50 MiB。
- 文件名使用 Unicode NFC；拒绝空名、NUL、C0/C1 控制字符、双向控制字符、路径分隔符、`.`/`..`、
  255 UTF-8 bytes 以上名称和多重危险后缀。
- 原文件路径只由服务端 `file_id/blob_kind` 生成，永不使用客户端文件名。
- 扩展名、声明 MIME 和魔数必须一致。`application/octet-stream` 不作为绕过 MIME 的通用后门；三方校验
  在创建 UploadedFile 之前完成，拒绝请求仍由统一 request/trace ID 追踪。
- 解析异常只映射固定错误码，异常文本、对象 repr、文件名和正文不进入日志。

### 6.2 TXT

- 只接受 `.txt` + `text/plain`。
- 支持 UTF-8 和 UTF-8 BOM；拒绝其他编码、NUL 字节和无效 UTF-8。
- 保留正文字符，不执行 HTML/Markdown/模板解释。

### 6.3 DOCX

- 只接受 `.docx` + OOXML Word MIME + ZIP 魔数。
- 使用 `zipfile` 先验证 entry 数、单 entry/总解压大小、压缩比、绝对路径和 `..` 路径。
- 必须存在 `[Content_Types].xml` 与 `word/document.xml`。
- 拒绝 `vbaProject.bin`、OLE/embedded package、外部模板/图片/数据关系和嵌套压缩对象；普通外部 hyperlink
  关系可以保留为不可执行文本但绝不访问 URL。这是解析器输入安全，不是 PRD-073 的隐藏 Prompt Injection 检测。
- 使用 `python-docx` 提取正文段落和表格单元格；不执行宏、不访问外部关系、不渲染图片。

### 6.4 PDF

- 只接受 `.pdf` + `application/pdf` + `%PDF-` 魔数。
- 使用 `pypdf`；加密 PDF、损坏 xref、超过 2,000 页或抽取异常固定拒绝。
- 运行依赖固定为具备解压输出硬上限的受支持 pypdf 主版本；抽取逐页累计文本上限，不能先构造无界全文列表。
- 只读取页面文本，不执行 JavaScript、Launch、附件或外部 URI。
- OCR、图片中文字、隐藏层分类和视觉顺序判断留给 PRD-073。

### 6.5 空文本

可解析但没有文本的 PDF/DOCX 不伪装成成功抽取，返回 `file.no_extractable_text` 并保存 failed 终态。
这与“扫描 clean”不同，避免业务方把空 OCR 结果当作安全文档。

## 7. DLP 预扫描与 Token 预估

### 7.1 规则快照

扫描器只加载：

```sql
WHERE tenant_id=? AND status='active' AND scan_scope IN ('input','both')
ORDER BY priority ASC,updated_at DESC,dlp_rule_id ASC
LIMIT 100
```

快照包含 rule ID、type、action、priority 和进程内 matcher。`ruleset_fingerprint` 对 rule ID、pattern_hash、
action、priority、updated_at 的 canonical JSON 计算 SHA-256；数据库不复制 pattern 或明文命中内容。

### 7.2 全文扫描

- TXT/DOCX/PDF 的全部已抽取文本都进入扫描，不使用 Gateway 的 20,000 字符截断。
- 文本受 8 MiB UTF-8/4,000,000 code points 双上限保护。
- keyword 逐全文匹配；regex 使用现有安全编译器，并在固定 64 Ki code-point 窗口加 512 code-point 重叠中
  遍历全文，避免把多 MiB 文本一次性交给回溯引擎。
- 当前 regex 合同不承诺识别跨越 64 KiB 窗口的单次超长匹配；管理文档必须如实说明，不能把 v1 描述成
  形式化 DLP 完备证明。
- 每条命中规则只保存一次摘要：rule ID/type/action 和规则集 fingerprint；不保存窗口命中次数、位置、片段、
  match hash 或 pattern。UploadedFile/Event 的 `match_count` 固定表示**命中的不同规则数**，重叠扫描窗口不能
  重复累计同一规则。
- 任一 block 规则优先；否则按冻结排序选择首条 allow/log_only 命中。没有规则或没有命中均为 `clean`，
  但 `ruleset_fingerprint` 仍能区分“空规则集”和规则未命中。
- 数据库、规则编译或扫描异常返回 `file.dlp_unavailable`，不得继续对象持久化。

### 7.3 Token 估算

冻结 `file-token-estimator-v1`：

- 空白不计 Token。
- CJK Unified Ideographs 每个 code point 估算 1 Token。
- 其他非空 code point 每 4 个向上取整。
- 估算覆盖完整抽取文本并使用 Python 整数，不使用模型厂商 Tokenizer、网络或浮点。
- 响应同时返回 `estimated_tokens`、`estimator_version`、`text_code_points` 和 `text_size_bytes`，明确这是预算前
  估算而非厂商账单。

## 8. 加密对象存储

### 8.1 配置

- `METAHUB_FILE_STORAGE_ROOT`：默认 `.data/file-storage`，`AppConfig` 中 `exclude=True/repr=False`。
- `METAHUB_FILE_ENCRYPTION_SECRET`：独立根密钥，`exclude=True/repr=False`。
- dev/test 可使用固定且明确标注 dev-only 的独立默认值；prod 缺失、弱密钥、默认值或与 Auth/Provider/Prompt
  密钥相同均使文件 API 以 `file.storage_unavailable` 失败关闭，但不阻止健康检查、登录和其他不接触文件的
  控制面能力启动。配置摘要只显示“已配置/需配置”，不显示 secret/root。
- `METAHUB_FILE_MAX_UPLOAD_BYTES`：默认 `20_971_520`，范围 1..50 MiB。
- `METAHUB_FILE_PARSE_CONCURRENCY`：默认 2，范围 1..8；只限制本进程同步解析容量。

### 8.2 密文合同

- 格式：`metahub-file-aesgcm-v1`。
- 使用 HKDF-SHA256 从根密钥分别派生 `filename`、`original`、`extract` 三个域。
- 每个密文使用随机 96-bit nonce。
- AAD 至少绑定 schema/key version、tenant_id、file_id、extract_id（如适用）、blob_kind、原文件 keyed
  fingerprint 和
  estimator/extractor version。
- 原文件和抽取文本都使用 tenant/file/blob-kind 分域 HMAC fingerprint，不保存裸 SHA-256，降低公共文档
  的跨租户字典反推风险；AES-GCM 认证标签同时承担密文完整性校验。
- 对象键由随机 file/extract ID 生成，公共响应不返回 storage root、对象键或宿主路径。

### 8.3 本地 Blob Store

接口冻结为 `stage -> publish -> read -> delete`；首个实现是 `encrypted_local_v1`。所有目标都必须解析在配置
root 内，拒绝符号链接 root、路径穿越和已存在的非预期目标。staging 与最终对象同卷，保证 `os.replace` 原子。

未来 S3/MinIO 适配只替换 BlobStore，不改变 API、数据库 owner 或密文格式。本 PRD 不引入云对象存储凭据。

## 9. 数据表和字段设计

0064 新增下列表，所有业务关系都以 tenant 参与复合 owner；ID 使用固定前缀加 32 位十六进制。

### 9.1 `uploaded_files`（OBJ-096）

| 字段 | 约束与用途 |
| --- | --- |
| `file_id` | PK，`file_` + 32 hex |
| `tenant_id` | 非空，FK tenants；所有查询首条件 |
| `uploaded_by` | 当前 Session user，复合 FK users |
| `source_request_id/source_trace_id` | 原上传链路；仅建 tenant/request 查询索引，不把 request_id 当幂等键 |
| `original_name_ciphertext` | owner-bound AES-GCM，不保存明文文件名 |
| `file_extension/media_type` | 固定白名单枚举 |
| `size_bytes/content_fingerprint` | 原文件计数及分域 keyed fingerprint；不保存裸 hash |
| `status` | `processing/ready/blocked/failed` |
| `virus_scan_status` | 本条固定 `not_configured` |
| `dlp_scan_status` | `pending/clean/matched/blocked/unavailable` |
| `dlp_action/effective_dlp_rule_id/match_count` | 只保存安全决策摘要 |
| `ruleset_fingerprint` | `sha256:` 前缀稳定摘要 |
| `storage_backend/original_object_key` | 仅 ready；不出公共 API |
| `failure_error_code` | 仅 blocked/failed 的固定白名单码 |
| `created_at/updated_at/completed_at` | UTC 秒；状态机约束 |

唯一键和索引：`UNIQUE(tenant_id,file_id)`、`INDEX(tenant_id,source_request_id)`、
`INDEX(tenant_id,created_at DESC,file_id DESC)`、`INDEX(tenant_id,status,created_at DESC)`。客户端重复使用
request_id 不具备幂等语义，每次成功通过 envelope/格式门禁的上传仍生成独立 file ID。

### 9.2 `file_extracts`（OBJ-097）

| 字段 | 约束与用途 |
| --- | --- |
| `file_extract_id` | PK，`fext_` + 32 hex |
| `tenant_id/file_id` | 复合 FK UploadedFile，tenant 内一对一 |
| `status` | `ready/blocked/failed` |
| `extractor_version` | 已知格式固定 `txt-v1/docx-v1/pdf-v1` |
| `text_size_bytes/text_code_points` | ready/blocked 非负有界计数；抽取前失败可为 NULL |
| `estimated_tokens/estimator_version` | ready/blocked 使用 `file-token-estimator-v1`；抽取前失败为 NULL |
| `text_fingerprint` | ready/blocked 使用独立密钥 HMAC；抽取前失败为 NULL |
| `storage_backend/extracted_object_key` | 仅 ready；不出公共 API |
| `failure_error_code` | blocked/failed 固定码 |
| `created_at/updated_at` | UTC 秒 |

### 9.3 `file_dlp_matches`

每个 file/rule 最多一行：`file_dlp_match_id,tenant_id,file_id,file_extract_id,dlp_rule_id,rule_type,action,
ruleset_fingerprint,created_at`。复合 FK 同时验证 file、extract 和 DLP rule 都属于同 tenant。
表禁止 UPDATE/DELETE/REPLACE；不含命中位置、文本或 hash。

### 9.4 `file_events`

append-only source event：

- `file_event_id,tenant_id,file_id,event_type,actor_user_id,source_request_id,source_trace_id`
- `file_status,media_type,size_bytes,virus_scan_status,dlp_scan_status,dlp_action,match_count`
- `estimated_tokens,extractor_version,occurred_at`

`event_type` 仅允许 `file.uploaded/file.scanned`。uploaded 必须逐字段绑定 processing owner；scanned 必须绑定
同 tenant 终态和 FileExtract。一个文件每种事件最多一条。事件不可变，不含文件名、正文、对象键、hash、
规则 pattern 或错误详情。

服务在业务事务前先用 Shared exact-key 合同构造 WebhookEvent，构造失败不得提交 source event；事务提交后
按 `file.uploaded` 先于 `file.scanned` 的顺序投递。0064 down guard 会拒绝仍订阅这两种事件的 Webhook
Endpoint，避免降级后留下无法解释的订阅配置。

### 9.5 状态与 owner trigger

- UploadedFile 只能 `processing -> ready|blocked|failed`；终态只允许逐字段完全相等的幂等 UPDATE。
- FileExtract 插入时必须与终态 UploadedFile、Token/大小和对象键状态一致。
- ready 必须同时有两个对象键、clean/matched DLP 和 FileExtract ready。
- blocked 必须没有对象键、DLP blocked、action block 和 FileExtract blocked。
- failed 必须没有对象键且有固定 failure code。
- 所有表拒绝显式 rowid 冲突和 `INSERT OR REPLACE` 覆盖。
- down migration 在存在任意文件业务事实、自定义 file 权限关系或未知依赖时失败关闭；空库可恢复到精确 0063。

## 10. RBAC 设计

新增：

| 权限 | 风险 | 默认角色 |
| --- | --- | --- |
| `file.read` | read | admin、developer、viewer、security_admin |
| `file.write` | write | admin、developer、viewer |

理由：Developer 和业务用户都在 Excel 用户范围内；现有 Viewer 是企业 Chat 的最小业务用户，因此可上传和读取
自己的 tenant 文件。文件对象当前是 tenant 共享资源，不实现 user-private ACL。SecurityAdmin 只读取安全摘要，
不能上传；FinanceAdmin/OpsAdmin 默认不授予。Admin 保持完整不可变矩阵。

迁移把 catalog revision 从 7 提升到 8。只有权限关系与 revision 7 产品默认矩阵逐项完全一致的角色才自动
扩展；自定义矩阵不静默扩权。新 tenant 直接物化 revision 8 目录。路由依赖与写事务分别复核权限，前端隐藏
按钮不承担授权职责。

## 11. API 设计

### 11.1 `POST /files`

- 权限：`file.write`
- Content-Type：`multipart/form-data`
- 字段：恰好一个 `file`
- 成功：HTTP 201，`FileResponse`
- Header：`Cache-Control: no-store`
- 并发容量在读取正文前取得；容量 2 秒内不可用返回 429 `file.processing_busy`。

成功响应只含安全投影：

```json
{
  "file": {
    "file_id": "file_...",
    "original_name": "proposal.docx",
    "file_extension": "docx",
    "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "size_bytes": 12345,
    "status": "ready",
    "virus_scan_status": "not_configured",
    "dlp_scan_status": "clean",
    "dlp_action": null,
    "match_count": 0,
    "extract": {
      "file_extract_id": "fext_...",
      "status": "ready",
      "extractor_version": "docx-v1",
      "text_size_bytes": 9000,
      "text_code_points": 5000,
      "estimated_tokens": 2100,
      "estimator_version": "file-token-estimator-v1"
    },
    "created_at": "...",
    "completed_at": "..."
  },
  "request_id": "...",
  "trace_id": "..."
}
```

响应不返回抽取正文、内容 fingerprint、规则 ID、对象键或宿主路径。FileInfo 明确包含
`upload_request_id`，用于把对象与最初上传链路关联；DLP block/失败先提交安全终态，再返回统一错误，调用方
可按列表中的 `upload_request_id` 找到终态事实。

### 11.2 `GET /files/{file_id}`

- 权限：`file.read`
- 只执行 `WHERE tenant_id=? AND file_id=?`
- 不存在与跨租户同形返回 `file.not_found`
- HTTP 200 + `Cache-Control: no-store`
- 返回与 POST 相同的安全投影；processing/failed 的 extract 可为空。

Shared `_apply_path_cache_control()` 对 `/files` 及其所有子路径、所有成功或错误状态统一写
`Cache-Control: no-store, private`，不能只依赖成功 handler 设置 Header。

### 11.3 `GET /files`

- 权限：`file.read`
- Query：`status` 可选，`limit` 默认 25/最大 100，`cursor` 为 tenant/筛选/授权指纹绑定签名游标；签名
  使用文件根密钥派生的独立 `pagination-v1` HMAC 域，不复用密文子密钥或公开 Auth Token。
- 稳定排序：`created_at DESC,file_id DESC`。
- 返回 `files,next_cursor,request_id,trace_id`；不接受 `tenant_id`、`user_id` 或任意排序字段。
- 只服务 PAGE-102 的上传处理记录，不暴露 BatchJob、重试、优先级或模型调用字段。

### 11.4 multipart 鉴权顺序

路由签名只声明 `Request` 和 `Depends(require_file_writer)`，不声明 `UploadFile/File` body 参数。依赖通过后才：

1. 检查 Content-Type/Content-Length。
2. 从 Content-Type 提取并严格校验唯一 boundary（ASCII、1..70 bytes）；重复 Content-Disposition、
   Content-Type、`name` 或 `filename/filename*` 参数固定拒绝，RFC 5987 filename 只在严格 UTF-8 解码后使用。
3. 用 `python-multipart.MultipartParser` callback 消费 `request.stream()`，同时限制 envelope 和 file bytes。
4. 验证 exact part 集合并在 `finally` 覆盖/释放有界 bytearray 引用。

因此无 Token、失效 Session、权限不足都不能触发 multipart/PDF/DOCX 解析；合法请求也不会把未加密正文写入
系统临时 spool。实现固定依赖 python-multipart 的公开 callback API并以合同测试锁定升级兼容性。

## 12. 统一错误码

| 错误码 | HTTP | 语义 |
| --- | ---: | --- |
| `file.multipart_invalid` | 400 | 非 multipart、字段数量/名称或边界非法 |
| `file.name_invalid` | 422 | 文件名为空、路径化、控制字符或超限 |
| `file.unsupported_format` | 415 | 扩展名/MIME/魔数不在白名单或不一致 |
| `file.too_large` | 413 | Header、multipart envelope 或真实 file bytes 超过上限 |
| `file.processing_busy` | 429 | 本进程有界同步解析容量暂时已满 |
| `file.structure_invalid` | 422 | PDF/DOCX 容器损坏或危险结构 |
| `file.no_extractable_text` | 422 | 支持格式但没有可提取文本 |
| `file.extract_limit_exceeded` | 413 | 页数、解压量或抽取正文超过硬上限 |
| `file.parse_failed` | 422 | 解析器无法产生可信文本 |
| `file.dlp_blocked` | 403 | tenant block 规则命中，正文未保存 |
| `file.dlp_unavailable` | 503 | 规则或扫描依赖不可用，失败关闭 |
| `file.storage_unavailable` | 503 | 数据库、密钥或 Blob Store 不可用 |
| `file.integrity_failed` | 500 | owner、密文、状态或 source event 不自洽 |
| `file.not_found` | 404 | 当前 tenant 不存在该文件；跨租户同形 |

认证/权限仍使用现有 `auth.*` 稳定码。所有错误体自动包含 `request_id/trace_id`；错误文案和 suggestion 不拼接
文件名、正文、路径、SQL、解析器异常或 DLP pattern。

## 13. Webhook 设计

Shared Webhook 注册 `file.uploaded` 和 `file.scanned` exact-key 合同：

- 公共信封：`event_id,tenant_id,timestamp,actor_id,object_id,trace_id,payload,signature`。
- `object_id=file_id`，`actor_id` 来自当前 Session，`source_request_id` 必须与 UploadedFile 一致。
- `file.uploaded` payload：file_id、status=processing、media_type、size_bytes、virus_scan_status、created_at。
- `file.scanned` payload：file_id、终态、media_type、size_bytes、virus_scan_status、dlp_scan_status、dlp_action、
  match_count、estimated_tokens、extractor_version、completed_at、固定 error_code（仅 blocked/failed）。
- 禁止 filename、正文、对象键、hash、规则 ID/pattern、命中片段、Token/Authorization/Header。
- source event 与业务状态同事务，Webhook 在提交后投递；网络失败进入现有 delivery/dead-letter，不回滚业务。

## 14. 前端页面和交互设计

PAGE-101/PAGE-102 合并为导航项 `files` 下的一个无嵌套卡片工作台：

- 顶部紧凑上传栏：原生文件选择器、格式/20 MiB 上限、上传图标按钮、当前文件名和真实进度状态。
- 上传按钮只有后端导航和认证快照同时包含 `file.write` 时显示；是否显示不影响后端授权。
- 下方处理记录表：名称、格式/大小、处理状态、DLP、病毒占位、Token 估算、完成时间、request_id。
- 状态使用图标、文本与颜色共同表达；`not_configured` 显示“未配置病毒扫描”，不能使用绿色成功语义。
- 不显示抽取正文、DLP pattern/规则 ID、hash、对象键或宿主路径。
- 选择非法扩展名可提前提示，但服务端仍是权威校验。
- 上传中禁用重复提交；401/403 清除 owner-bound 页面数据并回到现有登录/无权限流程。
- 列表响应以 tenant/user/authorization fingerprint + generation 绑定，切换 owner、注销或权限降级后丢弃迟到响应。
- 1280x720 使用紧凑表格；390x844 转为标签化行，按钮/文件名动态换行，页面不得横向溢出或重叠。

## 15. 日志与隐私

允许日志字段：固定 event、tenant_id、actor user ID、file_id、request_id、trace_id、状态、格式、大小、
Token 计数、固定错误码。禁止日志字段：原始/抽取正文、文件名、文件/文本 hash、密文、对象键、DLP pattern、
命中片段、Authorization、Cookie、Session Token、Provider Key、Prompt、Response 和底层异常文本。

访问日志现有 `_safe_log_path()` 必须把 `/files/{id}` 的动态 ID 脱敏或确认只保留规范模板；multipart query 和
filename 永不进入 URL。未知异常使用 `from None` 或固定内部原因码映射，日志不记录 `exc_info`。

## 16. 测试用例

### 16.1 Shared/单元测试

- UTF-8/BOM TXT、DOCX 段落+表格、PDF 文本主流程。
- DOCX zip slip、zip bomb、宏/OLE/外部关系、缺核心 entry、`.doc` 拒绝。
- PDF 错魔数、加密、损坏、超页数；TXT 非 UTF-8/NUL；空文本。
- 文件名路径、控制字符、双向字符、多重扩展和上限。
- Token estimator 的中文、英文、混合、空白、超长确定性。
- DLP keyword/regex、跨扫描窗口、block 优先、log_only、空规则、规则损坏、全文不截断。
- 重叠窗口命中同一规则只计一次；多个不同规则才增加 match_count。
- AES-GCM round-trip、不同 tenant/file/blob_kind AAD 交换失败、弱/复用密钥失败、repr/model_dump 脱敏。
- Blob root 路径穿越/符号链接/冲突、stage/publish/清理和故障注入。
- Webhook exact-key、正文/filename/hash/对象键注入拒绝。

### 16.2 Control 集成测试

- 登录 -> 上传 TXT/DOCX/PDF -> 201 -> GET detail/list -> DB/Blob 解密复核。
- DLP block：403、终态可查、原始和抽取对象不存在、事件和匹配摘要存在。
- log_only：ready、match_count、Token 估算和对象存在。
- 无规则：clean + 空规则集 fingerprint。
- Content-Length 伪造、chunked/真实超限、额外字段、多文件、未知 MIME/扩展/魔数。
- 解析、DLP DB、加密密钥、stage/publish、DB terminal commit 故障均固定错误并清理明文/对象。
- 无 Token/过期 Session/撤权时 parser 调用数为 0；事务前后撤权竞态失败关闭。
- 解析容量耗尽在读取 `request.stream()` 前返回 429；PDF/DOCX 解析不阻塞 event loop 健康请求。
- viewer 默认可读写；security_admin 只读；finance/ops 无默认权限；自定义矩阵按数据库实时生效。
- tenant A 的 file ID 在 tenant B 中 detail 404、列表 0，直接伪造跨 tenant SQL 被 trigger 拒绝。
- 所有响应含 request_id/trace_id，Cache-Control no-store，日志/事件/数据库普通列无正文。

### 16.3 迁移测试

- 0063 -> 0064 -> 0063 -> 0064 可逆空事实流程。
- revision 7 的 59 权限升级为 revision 8 的 61 权限，默认关系/hash 与 Shared 完全一致。
- canonical 角色才扩权，自定义矩阵不扩权；新 tenant 直接得到 revision 8。
- 文件表 owner、状态机、不可变、REPLACE、跨 tenant、伪造事件、显式 rowid 均拒绝。
- 有文件事实、自定义 file 授权或未知依赖时 down 失败关闭且原库不变。
- `foreign_key_check=0`、`integrity_check=ok`、迁移 runner checksum/幂等。

### 16.4 Console 测试

- exact DTO、请求 Content-Type、错误码/request_id、列表分页。
- owner/generation、注销、401/403、权限降级、迟到响应清理。
- write/read-only/no-navigation 三种权限投影。
- 上传 busy 防重复、格式预检、DLP block、病毒未配置和 Token 估算展示。
- 文件名/服务端字符串 HTML 转义；正文和对象键不进入 DOM。
- 1280x720、390x844 无横向溢出、重叠或按钮文字裁切。

### 16.5 最终验收

1. 专项 Shared、Control、迁移、Console 测试。
2. 直接受影响回归；只有最终收口再执行约定的分层全量回归，不因单点修复反复从头测试。
3. `scripts/ci/check-comments.py`、Ruff/py_compile、Console lint。
4. 在 `D:\MetaHub\.tmp` 建立临时测试根，TEMP/TMP 只指向该目录；不使用 `C:\tmp`、不提权。
5. 将开发库从 0063 升至 0064并幂等复核。
6. 启动 Gateway/Control/Console，健康检查均 HTTP 200。
7. 真实 HTTP 上传 TXT/DOCX/PDF、DLP block、权限/跨 tenant、Token、事件、密文和日志隐私验收。
8. 应用内 Browser 在 1280x720 与 390x844 验收上传、列表、异常、权限降级和注销清理。
9. 清理测试 Session/规则/文件元数据和对象；仅保存脱敏证据。
10. 更新 README、目录说明、API 文档和 `docs/testing/PRD-072-tests.md` 后提交并推送 GitHub。

## 17. 代码修改计划

### 17.1 Shared

- 新增 `metahub_shared/files/`：格式合同、解析器、DLP 扫描、Token 估算、密文和 Blob Store。
- 更新 `runtime/config.py`：文件存储/密钥/大小配置与生产安全校验。
- 更新 `runtime/errors.py`、i18n/诊断：稳定文件错误码。
- 更新 `runtime/webhooks.py`：两个文件事件 exact-key 安全合同。
- 更新 `rbac.py`：revision 8、`file.read/write` 和角色上限。

### 17.2 数据库

- 新增 `0064_file_upload_text_extraction.sql` 与 `.down.sql`。
- 新增迁移专项测试，复核 revision/hash、owner、状态机、可逆和降级门禁。

### 17.3 Control API

- 新增 `metahub_control/files/{models,routes,service}.py`。
- 更新 auth dependencies、app router、console navigation 和 Webhook 派发。
- 补 multipart 鉴权顺序、主/异常/权限/租户/隐私集成测试。

### 17.4 Console

- 新增 `src/lib/files.js`、`src/features/files/`。
- 更新 shell 渲染、main owner 生命周期、导航内容和响应式 CSS。
- 新增页面/DTO/权限/DOM/移动布局测试。

### 17.5 部署、文档与验收

- 在 `infra/docker/requirements.runtime.txt` 增加 `python-multipart`、`pypdf`、`python-docx` 的保守版本范围。
- 新增 `docs/api/files.md`、`docs/testing/PRD-072-tests.md` 和 Runtime 验收脚本。
- 更新 `.env.example`、README 和 `docs/development/directory-map.md`。

## 18. 设计自审记录

### 第一轮自审

状态：**已完成并修正（2026-08-05）**。

本轮以正文读取顺序、敏感派生值和状态可追踪性为重点，发现并修正：

1. **multipart 上限不完整**：初稿依赖 `Request.form(max_part_size)`，但 Starlette 兼容版本对 file part
   可能先 spool 再由业务读取，chunked 超限输入会在业务二次检查前扩大磁盘占用。现改为鉴权后直接使用
   `python-multipart` callback 消费 `request.stream()`，同时限制 envelope、part 数和 file bytes；明文只进入
   20 MiB 上限 bytearray，不写系统临时目录。
2. **裸内容 hash 泄露相等关系**：初稿为原文件保存 SHA-256，公共文档可能被跨租户字典匹配。现改为
   tenant/file/blob-kind 分域 HMAC fingerprint，抽取文本采用同一原则，裸 hash 不入库、不入日志/事件。
3. **半可信失败对象**：初稿在扩展名/MIME/魔数校验前创建 processing 行，未知 media type 无法满足稳定枚举，
   还会让攻击输入冒充已上传业务对象。现将低成本三方格式校验提前；拒绝请求由 request/trace ID 追踪，只有
   已确认支持格式才建立 UploadedFile owner。
4. **错误使用 request_id 唯一性**：初稿把 `(tenant_id,source_request_id)` 设为唯一，等同于把链路 ID
   擅自变成幂等键。现只建查询索引，每次上传生成独立 file ID，并在安全 DTO 中返回 `upload_request_id`。
5. **失败抽取字段不自洽**：补充 FileExtract failed 时 Token/文本计数/fingerprint 可为 NULL，避免以零值
   冒充已完成空文本抽取；空文本仍使用独立 `file.no_extractable_text`。
6. **DOCX 外链过度阻断**：初稿拒绝所有外部关系，会误伤普通 hyperlink。现只允许不可执行 hyperlink 且
   永不访问 URL，继续拒绝外部模板、图片、数据、OLE、宏和嵌套包。

### 第二轮自审

状态：**已完成并修正（2026-08-05）**。

本轮以并发资源、权限/tenant、事件兼容和故障半径为重点，发现并修正：

1. **同步解析阻塞控制面**：初稿没有冻结 event-loop/并发策略。现要求读取正文前取得容量 2 的进程内
   `anyio.CapacityLimiter`，2 秒无容量返回 429；深层解析、DLP 和加密进入受限 worker thread，健康与登录
   不被大 PDF 阻塞，同时不越界实现 PRD-074 持久队列。
2. **multipart 参数歧义**：补充 boundary 长度/字符约束、重复 header/参数拒绝和严格 UTF-8 `filename*`，
   防止多种 parser 解释产生不同文件名、MIME 或 part owner。
3. **DLP 计数可被窗口重复放大**：把 `match_count` 冻结为不同命中规则数；重叠窗口只保证边界覆盖，
   `file_dlp_matches` 每个 file/rule 最多一行，不保存重复次数。
4. **配置故障半径过大**：初稿可能被理解成文件密钥缺失导致整个 Control API 启动失败。现限定为文件 API
   503 失败关闭，其他不接触文件的健康、登录和管理能力继续运行；prod 仍禁止弱/默认/跨域复用密钥。
5. **游标签名域未冻结**：明确使用文件根密钥的独立 `pagination-v1` HMAC 子域，并绑定 tenant、筛选和
   authorization fingerprint；不能复用密文 key 或公开 Auth Token。
6. **错误缓存遗漏**：把 `/files` 全路径、全部状态码的 `no-store, private` 放进 Shared 中间件，而不是只给
   成功响应加 Header。
7. **Webhook/降级不完整**：要求 source event 入库前先通过 Shared exact-key 构造，提交后按 uploaded ->
   scanned 投递；0064 down 在仍有文件事件订阅时失败关闭，避免降级遗留不可解释配置。
8. **解析器资源上限不完整**：补充受支持 pypdf 解压硬上限和逐页累计，禁止先创建无界全文列表；原文、
   抽取文本、解析异常、文件名和裸 hash 仍不得进入日志、Webhook 或普通列。

两轮修正后，tenant 归属只来自 AuthContext，路由和写事务均执行后端 RBAC；正文读取先于任何解析但后于
认证/容量门禁；DLP、对象存储或证据提交失败均失败关闭。设计边界与 PRD-073/074 已明确分离，可以编码。

## 19. 实现与验收结论

本条严格在上述设计及两轮自审冻结后进入编码。最终实现包括 Shared 有界解析/加密/DLP 合同、Control
`POST /files`、`GET /files`、`GET /files/{file_id}`、PAGE-101/102、RBAC revision 8、0064 正反迁移、
安全 source event/Webhook 以及部署依赖和运行配置。所有业务 owner 均以 `tenant_id` 入库和查询，写事务内
二次复核 `file.write`，读取使用 `file.read`；跨 tenant ID 与不存在资源保持同形 404。所有 `/files` 成功及
错误响应均带统一错误结构、`request_id/trace_id` 和 `Cache-Control: no-store, private`。

正式 Runtime 证据为
[`runtime-acceptance.json`](../../.artifacts/prd072-runtime/prd072_20260804T212908Z_922f8ab2ff200912/runtime-acceptance.json)，
SHA-256 为 `0488B43A8D0FA3B465EF34271A76AF3FE9284E334E65C1C77E90E32861AA8AE3`；三服务、三种文件主流程、
DLP 阻断、权限拒绝、跨租户 404、`64/0064`、数据库完整性/外键、认证密文和日志隐私均通过。Browser 证据为
[`browser-acceptance.json`](../../.artifacts/prd072-browser/prd072_20260804T212908Z_922f8ab2ff200912/browser-acceptance.json)，
SHA-256 为 `A5F514B23C5ABF3917FB5A165F8CAA6B71AFC1A1FBDCD8035D72B03346AC29F0`；`1280x720` 与
`390x844` 均无横向溢出，移动端可见控件重叠为 0，抽取正文 canary 未进入 DOM。详细命令、退出码、
资源清理和隐私复扫见 [`PRD-072-tests.md`](../testing/PRD-072-tests.md)。

因此 PRD-072 已达到可运行、可验收条件。该结论不表示 108 条均已完成；总体状态仍为
`all_108_prds_complete=false`，下一条必须按 Excel 权威行进入 PRD-073。
