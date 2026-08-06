# 文件安全扫描与模型准入 API

本文档描述 PRD-073 的隐藏 Prompt Injection 检测合同。该能力属于 Control Plane 的低频企业
治理路径，不进入 Gateway 热路径，不调用外部 OCR、LLM、OpenRouter 或硅基流动。扫描只处理
PRD-072 已保存且状态为 `ready` 的 TXT、DOCX 和 PDF；任何未扫描、失败、覆盖不完整或高风险
文件都不能进入模型上下文。

## 1. 通用安全合同

- 所有接口都要求 Bearer Session；`tenant_id`、actor、阈值和检测规则只能来自后端可信状态，
  path、query 和 body 均不能覆盖。
- 报告读取和 CSV 导出要求 `file.read`；扫描命令同时要求 `file.read` 与
  `file.security.scan`。后端在同一个短 `BEGIN IMMEDIATE` 权限快照内复核双权限和 tenant owner，
  随后读取并验真原文件 Blob 与 PRD-072 抽取 Blob；写入报告的事务中再次校验权限。
- 跨租户文件、随机 ID 和非法 ID 同形返回 `404 file.not_found`，不暴露对象存在性。
- 三个接口都拒绝 query 参数和非空请求体。成功及错误响应都携带统一 `request_id`；报告接口
  另返回 `trace_id`，且使用 `Cache-Control: no-store, private`。
- 响应、CSV、事件和日志不包含 tenant、actor、原文件名、正文、OCR 文本、Prompt 片段、对象键、
  内容指纹、evidence HMAC、报告 hash、规则表达式、Session Token 或文件密钥。
- 扫描、finding 和安全事件是 tenant-qualified append-only 事实。服务读取报告时重算 canonical
  SHA-256；计数、风险或 finding 被篡改时统一失败关闭为 `file.integrity_failed`。
- 高风险安全事件的恢复状态单独保存在 tenant-qualified source outbox；outbox 不复制正文、文件名、
  finding 证据、Endpoint URL 或 Secret，worker 只能从同 tenant 不可变事实重建固定事件信封。

## 2. 发起安全扫描

```http
POST /files/{file_id}/security-scan
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
Content-Length: 0
```

请求没有 JSON 或 multipart 正文。同一 tenant、文件内容指纹、抽取正文指纹和检测器版本已经有
`passed` 或 `blocked` 权威结果时，接口返回该报告并令 `reused=true`；`failed` 是可重试 attempt，
不会占用权威唯一键。并发扫描由数据库唯一约束选出唯一赢家，其他请求读取赢家报告。

扫描容量等待上限为 2 秒。容量不足在读取或解密企业 Blob 前返回 429。检测器或 OCR 能形成完整
coverage 事实时返回 200 报告；只有无法形成或提交可信事实时才返回 500/503，并尽可能持久化
一个不含底层异常文本的 failed attempt。

扫描器必须同时接收两份已认证输入：原文件 bytes 用于 PDF/DOCX 结构、隐藏层和 OCR；
`trusted_extracted_text` 必须来自 PRD-072 已持久化抽取 Blob，用于未来模型实际消费字符的 Prompt
规则。客户端不能提交或覆盖抽取正文。字体 `ToUnicode/CMap`、DOCX 域或其他解析语义使原始操作数
与持久正文不一致时，以已认证持久正文补足语义检测；任一 Blob 的 owner、AAD、fingerprint 或
UTF-8 完整性失败都失败关闭。

## 3. 读取安全报告

```http
GET /files/{file_id}/security-report
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

读取顺序固定为：当前 detector 的唯一 `passed/blocked` 权威结果；若不存在则读取最新 failed
attempt；从未扫描返回 `404 file.security_report_not_found`。响应是严格 exact-key DTO：

```json
{
  "report": {
    "scan_id": "fscn_<32-lower-hex>",
    "file_id": "file_<32-lower-hex>",
    "status": "passed",
    "risk_level": "none",
    "risk_score": 0,
    "coverage_status": "complete",
    "model_access_status": "allowed",
    "detector_version": "file-security-detector-v1",
    "boundary_version": "file-context-boundary-v1",
    "ocr_status": "not_required",
    "ocr_engine_version": null,
    "scanned_page_count": 0,
    "scanned_image_count": 0,
    "finding_count": 0,
    "high_finding_count": 0,
    "findings": [],
    "created_at": "2026-08-05T00:00:00Z",
    "completed_at": "2026-08-05T00:00:00Z"
  },
  "reused": false,
  "request_id": "req_example",
  "trace_id": "trace_example"
}
```

`status=failed` 时必须同时满足 `risk_level=unknown`、`risk_score=null`、coverage 非 complete、
`model_access_status=blocked`。页面和调用方不得把 null 当作 0 或 clean。finding 最多 256 条，
只返回固定 detector/category/severity/source/location 枚举、置信度和时间，不返回命中文本。容量
溢出必须保留 `coverage.incomplete`；Prompt finding 可替换一条非 Prompt、非 coverage 结构 finding，
确保安全事件和明确阻断原因不被大量低风险结构事实挤掉，同时总数仍不超过 256。

## 4. 导出 CSV

```http
GET /files/{file_id}/security-report.csv
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

CSV 与 JSON 读取同一 canonical 报告，使用 UTF-8、CRLF 和固定 27 列。没有 finding 时仍输出一条
报告行；有 finding 时每条 finding 一行并重复报告摘要。服务端固定文件名为
`{file_id}-security-report.csv`，并返回：

```text
Content-Type: text/csv; charset=utf-8
Content-Disposition: attachment; filename="<file-id>-security-report.csv"
Cache-Control: no-store, private
X-Content-Type-Options: nosniff
```

所有文本单元格都执行电子表格公式注入防护。Console 同时限制 2 MiB 下载上限、校验固定文件名
和响应头，并在下载完成或失败后立即释放 Object URL。

## 5. 报告状态与检测范围

| 条件 | 报告状态 | 模型准入 |
| --- | --- | --- |
| coverage 完整，最高风险不超过 medium | `passed` | `allowed` |
| high/critical finding 或 coverage 不完整 | `blocked` | `blocked` |
| 无法形成可信 finding/report | `failed` | `blocked` |

当前固定检测器覆盖可见中英文强指令、DOCX 样式继承和隐藏/白色/超小文本、DOCX 媒体 OCR、
嵌套归档/embedding、PDF 不可见渲染模式、颜色/透明度、默认关闭 OCG、Form XObject、附件与危险
action、PDF 候选页 OCR。无法解释的 PDF 文本编码、流、颜色/图形状态或资源上限都形成
`coverage.incomplete`，不能静默放行。

PDF 内容流解析负责结构可见性，不被当作模型正文语义的唯一来源。即使原始操作数无害，字体
`ToUnicode/CMap` 映射后的 PRD-072 持久抽取正文只要命中注入规则，报告仍必须为 blocked 并产生
相应 Prompt finding；正文不会进入响应、CSV、Webhook、数据库普通列或日志。

## 6. 模型上下文准入

业务消费者只能调用后端 `build_file_model_context(config, context, file_id)`，不得直接读取
`FileExtract` Blob 或对象键。该函数在抽取正文解密前验证：

1. 当前 Session 仍有 `file.read`，文件和抽取记录属于同一 tenant 且状态为 ready；
2. 当前 detector 存在唯一 `passed`、`coverage=complete`、`model_access_status=allowed` 报告；
3. finding 集合、计数、风险和 canonical report SHA-256 未被篡改；
4. 抽取正文 owner、AAD 和正文 fingerprint 与报告冻结值一致。

通过后返回只存在于内存的动态 BEGIN/END 边界文本。边界 token 使用文件域 HMAC 和随机 nonce
派生，并检查不与正文碰撞；Prompt/Response、边界 token 和正文均不得写日志或持久化。任一条件
不成立统一返回 `403 file.model_access_blocked`，跨租户/随机 ID 保持 `404 file.not_found`。

## 7. Webhook

只有包含 high/critical Prompt Injection 的首次权威结果产生
`file.prompt_injection_detected` source event。Webhook 使用现有签名、投递、死信和重放链路，
payload exact keys 为：

```json
{
  "scan_id": "fscn_<32-lower-hex>",
  "file_id": "file_<32-lower-hex>",
  "risk_level": "high",
  "finding_count": 1,
  "high_finding_count": 1,
  "model_access_status": "blocked",
  "detector_version": "file-security-detector-v1"
}
```

0066 的 `AFTER INSERT ON file_security_events` trigger 在扫描同一事务内写入 pending
`file_security_event_outbox`；升级时也会为既有 source event 原子回填 pending。请求路由提交后只向
Control scheduler 发出 wake 信号，不直接投递，也不依赖进程内 `BackgroundTasks` 保存恢复事实。
即使 wake 未执行或进程立即退出，Control 下次启动也会立即 drain，运行期间则通过 wake 与固定轮询
继续恢复到期 source。

worker 首先在独立短 `BEGIN IMMEDIATE` 事务中真实 claim 一条到期 source，原子写入
`processing`、attempt、token、owner 和 90 秒 lease 并提交。随后当前且未过期的 owner 在另一个独立事务
以 `tenant_id + event_id + claim_token + lease_owner_id + dispatch_attempts + 当前且未过期 lease` 完整
CAS 复核，补齐 0065 遗留的部分 original deliveries、创建/恢复 Endpoint intent，持久化
`delivery_snapshot_frozen_at` 并续租。当时没有匹配 Endpoint 时也必须冻结空快照，以区分“已经确认无订阅者”和
“尚未形成快照”；marker 提交后，dispatcher 使用 `create_snapshot_if_missing=false`，只恢复冻结集合，
后来新增或改订阅的 Endpoint 不追溯旧 source。坏 source 不能在 claim 前验证或直接跳到终态：必须先完成真实
claim，再由当前 owner 交叉验证，并以同一组完整 CAS 转为 `failed + webhook.payload_invalid`。

worker 每次以 90 秒 lease 领取最多一条；每个真实网络 attempt 前先检查协作停服信号，再以
`tenant_id + event_id + claim_token + lease_owner_id + dispatch_attempts + 当前 lease + 未过期 lease`
完整 CAS 续租 90 秒。联网前、续租和最终回写任一核对失败都禁止继续访问 Endpoint；成功或失败状态
同样只能以有效 owner CAS 推进。稳定 `fsev_` event ID 提供 at-least-once，而不是网络 exactly-once：
远端已收包但本地确认
前崩溃时可能重发，订阅方应按 event ID 幂等。失败按 5/30/300 秒退避，最多 10 个 source dispatch
attempt；attempt=10 的 lease 过期后直接进入 failed，不允许第十一次 source dispatch。Endpoint 故障
不能回滚安全事实，也不能在 outbox 或错误日志中加入正文、文件名、finding 证据、目标 URL 或异常文本。

source 进入 `failed` 前会在同一事务关闭该事件剩余 original pending delivery并由既有 trigger 建立
DeadLetterEvent；已成功 delivery 不被覆盖，0066 trigger 禁止任何 source 终态残留 pending。公共
Webhook attempt 摘要使用 `tenant/endpoint/delivery/status/attempt_count/secret_version` CAS，因 reaper
而失权的迟到 worker 会回滚本轮 attempt。无法与 scan/finding 交叉验证的队首 source 固定隔离为
`webhook.payload_invalid`，随后继续领取下一事件，不阻塞其他租户。

Control lifespan 使用线程安全的 `threading.Event` 协作停服。若同步 worker 正在网络 attempt 中，停服
允许该 attempt 返回，但 guard 会在下一次真实网络访问前观察 stop；lifespan 等待 `asyncio.to_thread`
内的 worker 完成状态回写后再退出，不通过取消外层 task 遗留后台写库线程。

## 8. 错误码

| HTTP | 错误码 | 语义 |
| --- | --- | --- |
| 400/422 | `infra.validation_failed` | query、非空 body 或其他合同外输入 |
| 401 | `auth.unauthorized` | Session 缺失、失效或被撤销 |
| 403 | `auth.permission_denied` | 缺少读取或扫描权限 |
| 403 | `file.model_access_blocked` | 文件没有满足条件的可信安全报告 |
| 404 | `file.not_found` | 当前 tenant 下文件不存在、ID 非法或跨租户 |
| 404 | `file.security_report_not_found` | 文件存在但从未形成当前版本扫描 attempt |
| 409 | `file.security_not_ready` | 文件或抽取状态不是 ready，不能发起扫描 |
| 429 | `file.security_scan_busy` | 扫描容量已满，且尚未读取 Blob |
| 500 | `file.integrity_failed` | owner、密文或不可变报告完整性失败 |
| 503 | `file.security_scan_unavailable` | 检测器、OCR、数据库或可信事实事务不可用 |

统一错误响应仍遵循全局 envelope，并包含原请求的 `request_id`。底层 SQLite、ZIP、XML、PDF、
Pillow、PDFium、ONNX Runtime 和 OCR 异常文本不得穿透。

## 9. 部署配置与迁移

- `METAHUB_FILE_SECURITY_SCAN_CONCURRENCY`：扫描总并发，范围 1..4，默认 2。
- `METAHUB_FILE_SECURITY_OCR_CONCURRENCY`：离线 OCR 实例数，范围 1..2，默认 1。
- `METAHUB_FILE_SECURITY_MAX_OCR_PAGES`：单文件 OCR 页上限，范围 1..100，默认 20。
- `METAHUB_FILE_SECURITY_MAX_IMAGES`：单文件候选图片上限，范围 1..200，默认 50。
- `METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS`：单图像素上限，最大 50,000,000，默认 12,000,000。
- `METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS`：单次总像素上限，最大 200,000,000，默认 50,000,000，
  且不得小于单图上限。

运行镜像固定安装 Pillow、pypdfium2 和 `rapidocr-onnxruntime==1.4.4`。OCR 完全离线，运行时
不下载模型。配置变化通过重启生效；运行期替换 limiter 或 OCR pool 会失败关闭。

0065 migration 新增三张 tenant-qualified append-only 表、`file.security.scan` 权限，并把 RBAC
revision 升至 9（62 个权限、166 条角色模板关系）。默认产品矩阵完全一致的 Admin、Developer、
Viewer、SecurityAdmin 按迁移获得扫描权限，FinanceAdmin/OpsAdmin 不获得；自定义矩阵保持不变。
0066 migration 在不改写 0065 checksum 的前提下新增 `file_security_event_outbox`、
`delivery_snapshot_frozen_at`、同事务 source trigger、存量 source 回填、到期索引和不可变状态机约束。
0066 down 只有在 outbox 无任何 pending、processing、
dispatched 或 failed 业务行且不存在后续迁移时才允许回到 0065；0065 down 仍在存在安全事实、Webhook
订阅/投递事实、相关未来对象或 RBAC revision 不匹配时拒绝降级。PRD-073 最终 Runtime 的数据库目标
必须是 `66/0066`，不能把修复前的 `65/0065` 运行结果当作终态证据。

## 10. 终审回归边界

- Shared 固定样本覆盖原始 PDF 操作数无害、`ToUnicode/CMap` 映射后持久正文为注入指令的情况。
- Control 端到端回归通过真实 PRD-072 上传与抽取链路，证明同一权限快照读取两份 owner-bound
  Blob、跨租户同形 404、撤权 403，以及字体映射样本 blocked。
- Finding 饱和回归固定验证 256 条上限内同时存在 Prompt、`coverage.incomplete`、blocked 状态和
  7 字段 `file.prompt_injection_detected`，且正文/evidence canary 不进入 DTO、数据库或事件。
- 0066 聚焦回归必须覆盖 65 -> 66、存量孤儿 source 回填、同事务 trigger、启动恢复、失败退避、
  90 秒 lease、每次网络 attempt 前完整 CAS 续租、过期接管、迟到 worker CAS 失败、第十次终结、
  source/delivery/死信原子终态、坏 source 隔离后继续领取、混合 success/pending、legacy 部分快照
  补齐、空快照 marker、冻结后不追溯新 Endpoint、协作停服等待同步 worker、稳定 event ID 和跨
  tenant 失败关闭。
- Runtime 固定样本包含字体映射 PDF；最终测试计数、制品路径和 SHA-256 只记录在
  `docs/testing/PRD-073-tests.md`，并必须证明 `66/0066`、孤儿 source 恢复和 outbox 终态清理；本 API
  合同不复制可能漂移的运行统计。0066 不改变 PAGE-103 或公开 API，既有双视口 Browser 证据仍有效。
