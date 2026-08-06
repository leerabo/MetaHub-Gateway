# PRD-073 - 隐藏 Prompt Injection 检测

状态：**已完成并通过最终复核（2026-08-06）**。PDF 字体映射语义绕过、finding 饱和优先级和
Webhook source 提交后的崩溃丢失窗口均已修正；0066 定向回归、`66/0066` 真实 Runtime、三服务、
隐私与实际页面复验均已通过。0066 不改变 PAGE-103，既有双视口 Browser 证据继续有效。下一项为
PRD-074；全部 108 条总体目标仍在进行中：
`all_108_prds_complete=false`。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

已使用结构化工作簿解析器交叉核对总表、M2/M3 扩展交付、页面、API、数据对象、Webhook、
验收用例和 Codex/Claude 提示词工作表：

- `PRD_ID`：`PRD-073`
- 里程碑：`M2-企业增强`
- 优先级：`P2`
- 能力层：`扩展层`
- 模块：`文件型注入防护`
- 名称：`隐藏 Prompt Injection 检测`
- 主要用户：安全负责人、业务用户
- 页面：`PAGE-103 文件安全报告`
- 权威动作：`POST /files/{id}/security-scan`
- 核心对象：`OBJ-098 FileSecurityFinding`
- Webhook：`EVT-081 file.prompt_injection_detected`
- 主流程：`TC-0145`
- 异常、权限与安全：`TC-0146`

本条设计启动时，PRD-072 已完成并推送；按 Excel 的里程碑、优先级和原始行序，当时下一条
未完成项为 PRD-073。PRD-074 的 Scheduler/Worker 不进入本条实现。

### 1.2 原始范围与验收

本条必须实现：

1. 检测白色小字、超小字体、隐藏层和不可见文本。
2. 对 PDF 页面与 DOCX 内嵌图片执行离线 OCR，检测只存在于图片中的隐藏指令。
3. 检测受支持文档容器内的嵌套压缩包、嵌入附件和多层容器风险。
4. 在文件进入模型上下文前执行后端准入，并给抽取文本增加不可伪造的边界符。
5. 生成当前 tenant 可查询、可导出的文件安全报告。
6. 识别固定测试样本；任何高风险、覆盖不完整或扫描失败文件都不能进入模型上下文。

明确不保证所有复杂视觉攻击 100% 识别。该边界不允许实现静默放行：检测覆盖不完整、OCR
不可用或资源上限触发时必须诚实标记并阻断模型准入。

### 1.3 架构约束

系统架构文档明确把文件隐藏 Prompt Injection 防护放在 M2；控制面承担低频管理与报告，
不直接承载高频模型流量。文件解析属于企业 Chat/M2 能力，存储使用 PostgreSQL 元数据与
加密对象存储。由此冻结以下边界：

- 安全扫描在 Control API 的受限 worker thread 中执行，不加入 Gateway 热路径。
- 模型消费者只能调用共享的后端准入函数读取带边界上下文，不能直接读取 FileExtract Blob。
- OCR 完全离线，不调用上游模型、外部 OCR SaaS、OpenRouter 或硅基流动。
- 所有扫描、发现、事件和报告以 `tenant_id` 为 owner；客户端不能提交 tenant 或 actor。
- 报告不保存或返回命中正文、OCR 正文、Prompt 片段、对象键、宿主路径或裸内容 hash。

## 2. 需求理解

PRD-072 解决“文件能否被安全接收、解析、DLP 预扫并加密保存”；PRD-073 解决“已经接收的
文档是否包含会污染模型上下文的隐藏指令，以及可否被模型消费”。两者是独立安全维度：

```text
file.read + file.security.scan + tenant owner
                    |
                    v
同一权限快照读取并验真原件与抽取正文
                    |
                    +--> 原件：格式专用结构检查 + 有界离线 OCR
                    |
                    +--> 持久抽取正文：模型实际字符语义规则
                                      |
                                      v
                   FileSecurityScan + FileSecurityFinding
                                      |
                  +-------------------+-------------------+
                  |                                       |
      complete 且最高 <= medium                 high/critical/unknown
                  |                                       |
          后端模型准入允许                           后端模型准入阻断
                  |
      HMAC 边界包装后的不可信文件上下文
```

扫描不能只检查 PDF/DOCX 的原始文本操作数。字体 `ToUnicode/CMap`、DOCX 域或后续解析器可能让
原始操作数与 PRD-072 实际持久化、未来模型实际消费的正文不同，因此 `scan_file_security()` 强制
接收已认证的 `trusted_extracted_text`：原件负责结构与 OCR，持久抽取正文负责模型字符语义，任一
输入缺失或完整性校验失败都失败关闭。

扫描成功不等于“绝对安全”。公共报告只陈述确定性检测器版本、覆盖状态和已发现风险；模型准入
采用更保守的失败关闭规则。文件未扫描、扫描失败、确有 OCR 候选内容但 OCR 不可用、页数/像素/
图片数量超限或报告完整性不成立时，准入结果一律为 `blocked`。TXT、无图片 DOCX 和纯文本 PDF
不依赖 OCR，不能因为部署未加载 OCR 引擎而误阻断。

## 3. 范围与非目标

### 3.1 本 PRD 实现

- 对 PRD-072 已保存且状态为 `ready` 的 PDF、DOCX、TXT 执行按需扫描。
- DOCX：直接与样式继承的 `w:vanish/w:webHidden`、白色字体、超小字体、文本框/页眉页脚/
  脚注等 XML 文本、内嵌图片 OCR、embedding/归档 entry 风险。
- PDF：不可见文本渲染模式、白色填充文本、超小字体、默认关闭的可选内容层、嵌入附件，
  以及有界页面渲染后的 OCR。
- TXT：强 Prompt Injection 指令规则、控制字符/边界碰撞检查和模型上下文边界化。
- 固定本地中英文规则库；不调用 LLM 判断。
- `POST /files/{file_id}/security-scan`，以及 PAGE-103 恢复和导出所需的只读辅助接口。
- `FileSecurityScan`、`FileSecurityFinding`、`FileSecurityEvent` 三类 append-only 事实。
- `file.prompt_injection_detected` 安全 source event 与既有 Webhook 投递链路。
- 后端 `build_file_model_context()` 准入函数；只有通过报告才能解密抽取正文并添加边界。
- PAGE-103 文件安全报告、扫描命令、报告详情和 CSV 导出。
- 0065 安全事实/RBAC 正反迁移，以及追加 0066 source outbox 恢复迁移、统一错误、中文注释和验收证据。

### 3.2 明确不做

- 不新增 ZIP/RAR/7z 直传格式；PRD-072 仍只接受 PDF、DOCX、TXT。
- 不放宽 PRD-072 对宏、OLE、危险 DOCX embedding 的上传拒绝。PRD-073 对这些结构再做
  防御性检测，用于历史文件、受控测试和未来存储后端兼容。
- 不做云端 OCR、外部 URL 抓取、文档脚本执行、宏执行、附件解压执行或密码破解。
- 不承诺手写体、对抗字体、像素级隐写、复杂遮挡、任意语言和所有视觉攻击 100% 命中。
- 不自动改写、删除或“清洗”原文件和 FileExtract；已有认证密文保持不可变。
- 不把文件自动附加到企业 Chat，也不扩展 PRD-074 批量任务。
- 不实现通用恶意软件引擎；PRD-072 的 `virus_scan_status=not_configured` 仍保持诚实。
- 不在 Gateway 热路径访问 Control 数据库、OCR 模型或对象存储。

## 4. 现有代码基线

### 4.1 文件与存储

- `uploaded_files`、`file_extracts`、`file_dlp_matches`、`file_events` 已是 tenant-qualified、
  append-only/单向状态机事实。
- 文件名、原文件和抽取正文使用独立文件根密钥、owner-bound AES-GCM 与分域 HMAC；
  `EncryptedLocalBlobStore.read()` 会复核 root、链接、对象键和大小。
- PRD-073 必须按数据库中的 tenant/file/fingerprint/extractor owner 解密，不能从请求接收对象键。
- `FileInfo` 是 PAGE-101/102 冻结 DTO，不为 PRD-073 增加字段，避免破坏严格客户端。

### 4.2 认证与授权

- `file.read` 与 `file.write` 已独立；权限来自数据库 Session 快照，前端导航只改善体验。
- PRD-073 新增 `file.security.scan`，因为 SecurityAdmin 当前只有 `file.read`，而扫描是有成本、
  会写安全事实的独立命令，不能由读权限或 DLP 管理权限推导。
- 报告读取和导出复用 `file.read`；扫描要求 `file.read + file.security.scan`，写事务中再次复核。

### 4.3 Console

- PAGE-101/102 已有严格文件客户端、owner/generation、401/403 清理、移动布局和无正文 DOM 合同。
- PAGE-103 使用独立导航 `file-security` 和独立页面状态；不得把报告状态塞入现有 FileInfo。

## 5. 检测合同

### 5.1 检测器版本

- 总版本：`file-security-detector-v1`
- Prompt 规则：`file-prompt-injection-rules-v1`
- DOCX 结构：`file-docx-visual-v1`
- PDF 结构：`file-pdf-visual-v1`
- OCR：`rapidocr-onnxruntime-1.4.4`
- 上下文边界：`file-context-boundary-v1`

规则和阈值是发布包静态代码，不接受请求级 pattern、正则、阈值或模型名称。版本变化必须产生
新扫描事实；同一文件、内容 fingerprint、抽取 fingerprint 和检测器版本幂等复用已有报告。

### 5.2 固定发现类别

| 类别 | 典型 detector code | 默认严重度 | 准入 |
| --- | --- | --- | --- |
| 强可见指令 | `prompt.visible_instruction` | high | blocked |
| 隐藏/不可见文本 | `visual.hidden_text` | high | blocked |
| 白色字体 | `visual.white_text` | high | blocked |
| 超小字体 | `visual.tiny_text` | medium；命中强指令升 high | 按最高级别 |
| 默认关闭层 | `visual.hidden_layer` | high | blocked |
| OCR 指令 | `ocr.prompt_instruction` | critical | blocked |
| OCR 普通文本 | `ocr.text_observed` | low | allowed |
| 嵌入附件/归档 | `container.nested_archive` | critical | blocked |
| OCR/覆盖不完整 | `coverage.incomplete` | high | blocked |

公共 finding 不包含命中文本。规则内部只在当前调用栈保存关闭 repr 的文本；持久化
`evidence_fingerprint` 是文件密钥分域 HMAC，不能离线枚举原文。

### 5.3 Prompt Injection 规则

规则覆盖中英文高置信指令组合，例如忽略/覆盖之前指令、泄露 system prompt、改变角色或权限、
执行外部命令、把文档当系统指令等。实现使用：

- Unicode NFKC、大小写折叠和有限空白折叠；不删除任意标点形成不可解释匹配。
- 长度、规则数量、单次匹配数和累计文本上限。
- 预编译静态正则，不接受租户输入，不使用灾难性回溯结构。
- 文本只用于内存判定和 HMAC，不进入异常、日志、Webhook、报告或 DOM。
- OCR 结果先与同页/同 part 的可见抽取文本做有界规范化比对；已经存在于可见文本中的内容由
  visible detector 负责，不重复标成“图片隐藏指令”。只有 OCR 新增且命中规则的文本形成
  `ocr.prompt_instruction`，避免页面渲染把普通可见文字二次识别成隐藏证据。

### 5.4 DOCX 检测

1. 复用 PRD-072 的 ZIP entry 数量、单 entry、总解压体积和压缩比上限。
2. 解析 `word/styles.xml` 的 basedOn 链，最多 16 层，合并 run/paragraph 样式与直接格式。
3. 检查 `w:vanish`、`w:webHidden`、解析后的近白色字体与 `w:sz/w:szCs` 小于等于 6
   half-points（3pt）。`w:color=auto` 不是白色证据，只能按主题/背景解析后判断；解析不出时形成
   coverage finding，不能凭 `auto` 直接制造 high 误报。
4. 扫描正文、页眉页脚、脚注尾注、批注和文本框 XML；part 名不写入报告，只形成分域 hash。
5. 对 `word/media/*` 内的受支持位图执行尺寸、像素和解码上限后离线 OCR；EMF/WMF、损坏图片
   或其他未覆盖媒体必须标记 coverage partial 并阻断，不能静默跳过。
6. `word/embeddings/*`、归档后缀、嵌套 OOXML/ZIP 魔数或多层容器直接形成 critical finding，
   永不解压执行其中 payload。

### 5.5 PDF 检测

1. 使用 pypdf 有界读取内容流，跟踪 `q/Q`、`Tr`、`Tf`、`g/G/rg/RG/k/K`、颜色空间、`gs`
   透明度和文本显示操作；内容流操作数、图形栈深度、对象递归深度均有硬上限。
2. `Tr=3`、近白填充下的文本、字体小于等于 3pt 形成结构发现。
3. 检查 Catalog `OCProperties` 和默认 OFF 的 Optional Content Group；只读取有界对象图，不执行
   动作。无法解释的颜色空间、ExtGState 或递归对象若影响文本可见性，形成 coverage partial，
   不能按默认可见继续放行。
4. 检查 EmbeddedFiles、FileAttachment、Launch/JavaScript 等对象，形成 high/critical finding。
5. 先有界遍历全部页面资源并识别含 raster/image XObject 的页面；只对这些候选页使用
   pypdfium2 固定比例渲染并交给离线 OCR，避免无差别 OCR 普通纯文本页。
6. 候选图片页、图片对象或像素预算未覆盖全部潜在图片内容时标记 `coverage=partial`，模型准入
   失败关闭；纯文本 PDF 即使超过默认 OCR 页上限，只要结构遍历完整也不因此误判 partial。
7. PDF 原始内容流操作数仅用于结构可见性判断，不能作为模型正文语义的唯一来源。规则检测同时
   覆盖 PRD-072 已认证持久抽取正文，确保无害操作数经字体 `ToUnicode/CMap` 映射成注入指令时仍
   形成 `prompt.visible_instruction` 并阻断；扫描器不自行采用另一套临时抽取结果。

### 5.6 OCR 资源边界

- OCR 引擎只接受内存中的有界 RGB 图片，单图不超过 12MP，总计不超过 50MP。
- 默认最多 20 个 PDF 页面、50 个 DOCX 图片；这些值仅来自部署配置且有硬上限。
- OCR 并发默认 1，扫描总并发默认 2；门禁在解密原文件前取得。
- OCR 模型懒加载并复用，网络下载被禁止；部署包必须包含依赖和模型。
- 文件确实存在候选图片/页面时，引擎缺失、初始化失败或覆盖不足不会伪装为 clean，而是形成
  `coverage.incomplete/high` 的 blocked 报告；没有 OCR 候选内容时固定为 `not_required`。
- Pillow 只读图片 header 后先检查宽高/像素，再执行 decode；不修改进程级 DecompressionBomb 全局值。
- 单图片 OCR box、单 box 字符数和本次 OCR 总字符数均有硬上限；超限形成 coverage partial，
  不先拼接无界 OCR 文本再截断。

## 6. 扫描与模型准入状态机

### 6.1 扫描终态

`FileSecurityScan.status`：

- `passed`：覆盖完整，最高风险为 none/low/medium，模型准入 `allowed`。
- `blocked`：存在 high/critical，或覆盖为 partial/unavailable，模型准入 `blocked`。
- `failed`：依赖、完整性或事务故障，没有可信 findings；固定 `risk_level=unknown`、
  `risk_score=null`，模型准入 `blocked`。不能用分数 0 暗示文件安全。

扫描记录只写终态，不预先写 `processing`。这样进程在 CPU/OCR 阶段崩溃不会留下永久占位。
事务内先插入带 deferred Scan 外键的 Finding，再插入终态 Scan；Scan insert trigger 此时可以反向
校验 finding 数量、最高风险和 owner，提交时 deferred 外键再证明父记录存在。最后写可选 Event。
任一步失败整笔回滚，外部永远看不到孤立 Finding 或半成品 Scan。

`passed/blocked` 使用部分唯一索引冻结同一内容与检测器版本的权威结果；`failed` 是可重试 attempt，
不占用权威结果唯一键。单文件同版本失败 attempt 最多保留 10 条，超过后返回固定不可用错误，避免
故障重试放大数据库。多实例并发完成时，唯一索引的输家加载并返回赢家的可信报告。
并发失败 attempt 若争用同一 attempt_no，输家重新读取计数并最多重试一次分配；若期间已有
passed/blocked 权威结果则直接复用，不能把唯一冲突回显为数据库异常。

扫描输入读取使用短 `BEGIN IMMEDIATE` 权限快照：在同一事务内实时复核
`file.read + file.security.scan`、tenant owner、文件/抽取状态，随后读取并验真原文件 Blob 和抽取
Blob，读取结束后立即提交释放锁。这样先提交的撤权必然被看到，后到撤权则等待本次短读取结束；
不能出现只验真原件、却从另一个权限或 owner 时点读取模型正文的 TOCTOU 窗口。

### 6.2 模型准入

`build_file_model_context(config, context, file_id)` 是后端唯一允许解密 FileExtract 正文的模型
消费入口，执行顺序固定：

1. 使用当前数据库权限矩阵再次复核 `file.read`，再以 AuthContext tenant 查询 ready UploadedFile
   与 ready FileExtract；调用方的 Chat/Batch 等领域权限仍由各自入口独立校验。
2. 查询同内容 fingerprint、抽取 fingerprint 和当前 detector version 的最新可信报告。
3. 报告必须为 `passed + complete + allowed`，报告 canonical hash 和 finding 计数必须一致。
4. 再读取并 owner-bound 解密抽取正文；任何缺失、链接、密文或 fingerprint 异常失败关闭。
5. 使用文件根密钥 HMAC(tenant/file/scan/text fingerprint/counter) 生成不可预测边界 token；若正文
   极小概率已包含完整 token，最多重新派生 8 次，否则完整性失败。
6. 返回关闭 repr 的 `BoundedFileContext`，格式为明确的“不可信文件数据”BEGIN/END 边界；
   不返回文件名，不写日志，不持久化边界化正文。

扫描 API 和报告读取不返回模型上下文。未来 Chat/Batch 消费者必须显式调用该函数；直接读取
对象存储不属于受支持接口。高风险、未扫描或扫描不可用文件均在解密正文前被拒绝。

仓库增加静态所有权测试：除 PRD-072 文件持久化服务、PRD-073 安全服务和 Shared 密码学/存储
实现自身外，Control/Gateway 业务模块不得导入 `decrypt_file_blob`、读取 FileExtract 对象键或实例化
文件 Blob Store。这样当前没有附件参数的 Chat 也不是“因为没接文件所以自然安全”，而是由可测试的
唯一模型上下文入口和代码所有权共同形成准入边界。

## 7. 数据表和字段设计

迁移链：`0065_file_prompt_injection_detection.sql` 创建安全事实与 RBAC，追加
`0066_file_security_event_outbox_recovery.sql` 创建可恢复投递状态；已发布的 0065 checksum 不改写。

### 7.1 `file_security_scans`

| 字段 | 约束 |
| --- | --- |
| `scan_id` | `fscn_` + 32 lowercase hex，主键 |
| `tenant_id/file_id/file_extract_id` | 复合外键绑定同 tenant ready file/extract |
| `requested_by` | 同 tenant user 外键 |
| `source_request_id/source_trace_id` | 与 0064 一致，均为最长 128 字符的有界链路 ID |
| `attempt_no` | 同 tenant/file/version 的 1..10 失败/成功尝试序号 |
| `status` | `passed/blocked/failed` |
| `risk_level` | `none/low/medium/high/critical/unknown` |
| `risk_score` | passed/blocked 为整数 0..100；failed 固定 null |
| `coverage_status` | `complete/partial/unavailable` |
| `model_access_status` | `allowed/blocked` |
| `detector_version` | `file-security-detector-v1` |
| `ocr_status` | `not_required/completed/partial/unavailable/failed` |
| `ocr_engine_version` | 固定版本或 null，不含宿主信息 |
| `scanned_page_count/scanned_image_count` | 有界非负整数 |
| `finding_count/high_finding_count` | 由 finding 反向证明 |
| `boundary_version` | `file-context-boundary-v1` |
| `content_fingerprint/extract_fingerprint` | 复制 PRD-072 keyed HMAC，内部字段 |
| `report_sha256` | `sha256:` + canonical 安全报告 hash |
| `failure_error_code` | 仅 failed 使用固定码 |
| `created_at/updated_at/completed_at` | UTC 秒，终态时间一致 |

部分唯一索引仅对 `status IN ('passed','blocked')` 的
`(tenant_id,file_id,content_fingerprint,extract_fingerprint,detector_version)` 生效；失败 attempt 可重试，
但 `(tenant_id,file_id,detector_version,attempt_no)` 唯一且最多 10 次。所有行 append-only，不允许
UPDATE/DELETE/负 rowid/REPLACE。

canonical 报告按固定 key、finding 的 `(severity,detector_code,source_kind,location_kind,location_index,
finding_id)` 顺序和 ASCII JSON 分隔符计算；不包含 request_id、trace_id、`reused`、私有 fingerprint
或 CSV。SQLite trigger 证明 owner/计数/最高风险，服务写入和每次读取时重算 `report_sha256`。

### 7.2 `file_security_findings`

| 字段 | 约束 |
| --- | --- |
| `finding_id` | `fsfd_` + 32 lowercase hex，主键 |
| `tenant_id/scan_id/file_id` | 复合外键绑定同一 Scan owner |
| `detector_code` | 静态白名单 code |
| `category` | `prompt_injection/visual_hiding/hidden_layer/ocr/container/coverage` |
| `severity` | `info/low/medium/high/critical` |
| `confidence_milli` | 0..1000 |
| `source_kind` | 固定安全枚举，不含 entry/path |
| `location_kind/location_index` | `document/page/part/image` 与可选有界序号 |
| `evidence_fingerprint` | 文件密钥分域 HMAC，不是裸 hash |
| `status` | 固定 `detected` |
| `created_at/updated_at` | 与 Scan 完成时间一致 |

每个 scan/detector/source/location/evidence 唯一。每次扫描最多持久化 256 条 finding：最多 255 条
具体发现，检测结果继续溢出时保留一条 `coverage.incomplete/high`。可信抽取正文的 Prompt finding
必须优先进入收集器；若结构 finding 已先占满容量，后到的 `prompt.visible_instruction` 或
`ocr.prompt_instruction` 可替换一条非 Prompt、非 coverage 的结构 finding，但不能替换 coverage
事实。这样数量饱和仍保留阻断原因和安全 Webhook，同时维持 256 条硬上限。报告读取使用
`LIMIT 257` 反向证明数据库没有越界。Finding 到 Scan 的复合外键声明为
`DEFERRABLE INITIALLY DEFERRED`；Finding insert trigger 先证明同 tenant ready file/extract，随后 Scan
insert trigger 在同一事务反向证明完整 finding 集合。Finding 不保存原文、OCR 文本、坐标框、
文件名、字体名、XML path、PDF object ID 或任意第三方异常。

### 7.3 `file_security_events`

事件仅在检测到 Prompt Injection 类 finding 时写入：

- `event_type` 固定 `file.prompt_injection_detected`
- owner：`tenant_id/scan_id/file_id/actor_user_id`
- 安全摘要：risk、finding/high count、model_access、detector_version、request/trace/time
- 唯一 `(tenant_id,scan_id,event_type)`，append-only

数据库 trigger 必须从 Scan/Finding 反向证明计数、风险、actor、链路和时间；Webhook payload 不含
filename、finding ID、detector 证据或报告 hash。

### 7.4 `file_security_event_outbox`

0066 为每个不可变 `file_security_events` source 创建一条同 tenant、同 `fsev_` identity 的可变恢复状态：

| 字段 | 约束 |
| --- | --- |
| `file_security_event_id/tenant_id` | 主键与复合外键绑定同 tenant source；身份、owner 和 created_at 不可改写 |
| `dispatch_status` | 固定 `pending/processing/dispatched/failed` 状态机 |
| `dispatch_attempts` | 0..10；领取或接管 lease 前原子加一 |
| `next_attempt_at/dispatched_at` | pending 到期时间与成功终态时间互斥 |
| `claim_token/lease_owner_id/lease_expires_at` | processing 必填；90 秒 lease、逐网络 attempt 续租和完成 CAS 所需 owner |
| `delivery_snapshot_frozen_at` | claim 事务提交 processing lease 后，由当前有效 owner 在独立事务冻结 original delivery 集合的持久时间；集合为空也必须写非空时间 |
| `last_error_code` | 只允许固定 Webhook 错误码，不保存异常文本、URL 或 payload |
| `created_at/updated_at` | canonical UTC 秒；状态时间单调推进 |

`AFTER INSERT ON file_security_events` trigger 让 source 与 pending outbox 在同一扫描事务提交或回滚；
0066 升级还会原子回填已有 source，关闭部署升级前形成的孤儿窗口。到期索引不含 payload；insert/update/
delete trigger 拒绝跨 tenant owner、REPLACE、rowid/身份改写、非法状态组合、尝试次数倒退和历史删除。
首次 claim 事务只负责以完整条件提交 `processing`、attempt、claim token、owner 和 90 秒 lease，不能在
claim 前验证 source，也不能在该事务内冻结 delivery。提交后，当前且未过期的 owner 在独立短事务中以
tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 复核 source，补齐 0065 遗留的部分 original
deliveries，同时创建/恢复 intent、写入 `delivery_snapshot_frozen_at` 并续租；即使没有匹配 Endpoint 也冻结
空集合。marker 形成后，任何恢复只能处理冻结 delivery，不能把后来新增或改订阅的 Endpoint 追溯到旧 source。

## 8. API 设计

### 8.1 `POST /files/{file_id}/security-scan`

- 依赖：Bearer Session，`file.read + file.security.scan`
- query：不接受任何字段；body：必须为空
- tenant/actor：只来自 AuthContext
- 响应：HTTP 200；首次扫描与幂等复用均返回同一严格报告信封，`reused` 说明是否复用
- 若文件不是 ready、DLP 已阻断或原/抽取对象不存在，返回固定统一错误
- CPU/OCR 在独立 limiter 的 worker thread 运行；读取密文前和写事务内都再次校验两个权限
- passed/blocked 报告幂等复用；failed attempt 不复用，可在 10 次上限内重试
- 高风险 source 与 pending outbox 由 0066 trigger 同事务形成；提交后路由只 wake 常驻 scheduler，
  不直接联网投递。wake 丢失或进程退出都由启动恢复/周期轮询接管；worker 先在独立短事务提交真实
  `processing` claim，随后当前 owner 在另一个事务以完整 CAS 完成 source 复核、legacy delivery 补齐、
  intent/快照 marker 写入和续租，冻结完整或空 Endpoint 快照，之后不追溯新增 Endpoint。投递失败不回滚
  已提交报告

### 8.2 `GET /files/{file_id}/security-report`

- 辅助 PAGE-103 恢复接口，依赖 `file.read`
- query/body 均为空；返回当前 detector version 的严格报告
- 有 passed/blocked 权威结果时固定返回它；否则返回最新 failed attempt，让依赖故障可追踪
- 完全未扫描返回 `file.security_report_not_found`；跨 tenant 与随机 ID同形 `file.not_found`

### 8.3 `GET /files/{file_id}/security-report.csv`

- 依赖 `file.read`，复用与 JSON 完全相同的 tenant 查询和 canonical 报告
- UTF-8 CSV，固定列、CRLF、`Content-Type: text/csv; charset=utf-8`
- `Content-Disposition` 只使用规范 file ID，不使用文件名
- 全字段是服务端枚举、整数、规范 ID/UTC；仍执行 CSV formula 前缀防护
- `Cache-Control: no-store, private`、`X-Content-Type-Options: nosniff`

### 8.4 报告 DTO

`FileSecurityReport` 公开字段：

- scan/file ID、status、risk_level、risk_score、coverage_status、model_access_status
- detector/boundary version
- OCR status/engine version/页面与图片计数
- finding/high finding count
- findings：ID、detector code、category、severity、confidence、source/location、status/time
- created/completed time

信封统一包含 `request_id`、`trace_id`；禁止字段包括 tenant_id、actor、文件名、正文、Prompt/OCR
文本、fingerprint、report hash、对象键、cursor、规则正则和 Webhook payload。

## 9. 前端 PAGE-103

### 9.1 导航与权限

- 新导航 ID：`file-security`，标题“文件安全报告”，需要后端 `file.read`。
- `file.security.scan` 只控制扫描命令；缺失时页面保持只读，不创建可触发扫描的隐藏按钮。
- 页面加载只在当前后端导航可见、owner/generation 匹配时调用 `/files`。

### 9.2 页面布局

- 顶部紧凑标题、当前租户标识和覆盖状态，不使用营销 Hero 或嵌套卡片。
- 左/上方文件记录区只列 ready 文件：名称、格式、大小、完成时间和 DLP 状态。
- 报告区显示 risk、模型准入、覆盖/OCR、检测器版本、计数和 finding 表格。
- 命令使用“扫描”与下载图标加文字按钮；扫描进行中禁用重复提交，导出只在报告存在时可用。
- 空、加载、401/403、404、依赖不可用和 strict DTO 错误都有独立状态。
- 390x844 转单列；固定 ID/版本可换行，按钮和表格不横向溢出。

### 9.3 浏览器安全

- 文件名只通过 `textContent` 或严格 HTML 转义展示，不进入 URL/dataset/下载名。
- 报告没有正文；客户端拒绝未知 key、重复 finding ID、计数不一致、未知枚举和跨 file ID。
- CSV 直接下载后端认证响应；Object URL 使用后立即 revoke，不写 local/session storage。
- 离页、注销、撤权、tenant/Session/permission revision 变化立即清理文件名、报告和下载 Blob。

## 10. 错误码设计

| 错误码 | HTTP | 语义 |
| --- | ---: | --- |
| `file.not_found` | 404 | 非法、随机或跨 tenant 文件同形不存在 |
| `file.security_report_not_found` | 404 | 当前 tenant 文件尚无当前版本报告 |
| `file.security_not_ready` | 409 | 文件状态不是 ready，或抽取状态不是 ready |
| `file.security_scan_busy` | 429 | 解密前未取得安全扫描容量 |
| `file.security_scan_unavailable` | 503 | 检测器/OCR/事务无法形成可信终态 |
| `file.model_access_blocked` | 403 | 后端模型准入拒绝未扫描或高风险文件 |
| `file.integrity_failed` | 500 | ready owner 的 Blob 缺失、对象键/密文、报告 hash 或发现计数不自洽 |

认证、权限、body/query 校验继续使用现有统一错误码。所有 HTTP 错误由 Shared ErrorSpec 产生
`code/message/request_id/trace_id/suggestion/diagnosis`，不携带第三方 OCR/PDF/XML 异常。
OCR 引擎明确不可用但仍能形成可信 coverage 报告时返回 HTTP 200 blocked 报告；只有无法形成、
校验或提交任何可信终态时才返回 `file.security_scan_unavailable`。
数据库仍可写且 owner/fingerprint 已验证时，可以保存 detector/OCR 失败 attempt；数据库自身不可写时
无法形成可信 failed 事实，只返回统一 503，不伪造已持久化报告。

## 11. 安全、权限与租户约束

1. tenant 只来自 Bearer Session；所有 SQL 以 tenant 开头，跨 tenant 与不存在同形 404。
2. POST 先鉴权、再取得 limiter、再立即复核权限、最后读取/解密对象；无权请求不触碰企业文件正文。
3. 写事务内再次复核 `file.read + file.security.scan`，关闭扫描期间撤权竞态；撤权后的计算结果丢弃。
4. 报告读取/导出逐请求后端 `file.read`；前端按钮隐藏不承担授权。
5. 原文件、抽取正文、隐藏文本、OCR 文本和命中片段只存在于有界调用栈并关闭 repr；扫描必须使用
   PRD-072 已认证持久抽取正文，不接受 HTTP 参数或调用方临时解析文本替代。
6. 日志只允许 event、tenant/actor/file/scan ID、固定状态、计数、request/trace ID；禁止文件名、
   Prompt、Response、Token、Provider Key、对象键、pattern、报告 CSV 和异常文本。
7. Webhook 使用 exact-key payload；事件构造、数据库 source、pending 恢复和重放都验证同一合同。
8. OCR 模型和 PDF/DOCX 解析器不联网、不执行文档动作、不访问外部关系。
9. 报告、错误和 CSV 全部 `no-store, private`；Shared 中间件覆盖 `/files` 所有状态码。
10. 任何 `unknown/partial/unavailable/failed` 都不能转换为 clean 或 allowed。
11. 通过静态导入门禁限制原文/抽取正文解密所有权；Gateway 和其他 Control 业务模块不能绕过
    `build_file_model_context()` 直接读取对象键。
12. 扫描命令在同一个短 `BEGIN IMMEDIATE` 权限快照内读取两份 owner-bound Blob；跨租户、撤权、
    owner 漂移、AAD/fingerprint 不一致均在扫描前失败关闭，不把客户端 tenant 或前端按钮当授权。
13. source worker 初始领取 90 秒 lease；每次真实网络 attempt 前先检查停服信号，再以
    tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 续租 90 秒。任何持久字段漂移、
    跨租户 join、续租冲突或迟到 worker 都失败关闭，且日志只保留固定 event/error code。
14. 首次 claim 事务只提交 processing lease；随后当前有效 owner 在独立事务以 tenant/event/token/owner/
    attempt/当前且未过期 lease 完整 CAS，补齐 0065 legacy 部分集合并与
    `delivery_snapshot_frozen_at` 一起提交，空集合也写 marker。冻结后 dispatcher 禁止按当前 Endpoint 补建
    intent。

## 12. RBAC 与导航

- Shared catalog revision：`8 -> 9`
- 权限数：`61 -> 62`
- 角色权限模板关系数：`162 -> 166`（Admin、Developer、Viewer、SecurityAdmin 各新增一条）
- 新权限：`file.security.scan`
- 默认授权：Admin、Developer、Viewer（业务用户）、SecurityAdmin
- FinanceAdmin、OpsAdmin 默认不获得文件安全扫描；没有 `file.read` 时也看不到 PAGE-103
- Admin 继续拥有目录全集；所有现有 tenant permissions 与角色矩阵通过 0065 原子升级
- Control 新增 `require_file_security_scanner`，内部同时要求 `file.read` 与 `file.security.scan`

## 13. Webhook 合同

`file.prompt_injection_detected` 仅在 Prompt Injection 类 finding 存在时触发，payload exact keys：

```json
{
  "scan_id": "fscn_...",
  "file_id": "file_...",
  "risk_level": "high",
  "finding_count": 2,
  "high_finding_count": 1,
  "model_access_status": "blocked",
  "detector_version": "file-security-detector-v1"
}
```

事件不含文件名、finding ID、正文、页码、OCR 文本、fingerprint 或 report hash。0066 trigger 与 source
同事务形成 pending outbox；Control lifespan 启动即 drain，请求路由只 wake scheduler。worker 在独立短事务
初始取得 90 秒 processing lease 并提交真实 claim；随后当前 owner 在另一独立事务以完整 CAS 补齐 legacy
部分 deliveries、创建/恢复 intent、持久冻结完整或空 Endpoint 快照并续租；每次真实网络 attempt 前
通过完整 CAS 续租，dispatcher 只 resume 冻结集合。稳定 `fsev_` event ID 提供
at-least-once；远端收包后、本地确认前崩溃可能重发，不能宣称网络 exactly-once。lifespan 使用
`threading.Event` 协作停服并等待同步 worker 完成，不取消 `to_thread` 遗留写库线程。0066 down 在
outbox 存在任一业务行时拒绝，0065 down 仍在相关订阅或投递事实存在时失败关闭。

## 14. 配置与依赖

新增部署配置均有默认值、范围校验和脱敏摘要：

- `METAHUB_FILE_SECURITY_SCAN_CONCURRENCY=2`，范围 1..4
- `METAHUB_FILE_SECURITY_OCR_CONCURRENCY=1`，范围 1..2
- `METAHUB_FILE_SECURITY_MAX_OCR_PAGES=20`，范围 1..100
- `METAHUB_FILE_SECURITY_MAX_IMAGES=50`，范围 1..200
- `METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS=12000000`
- `METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS=50000000`

运行依赖：`rapidocr-onnxruntime==1.4.4`、`pypdfium2>=5.9,<6.0`、Pillow 的受支持主版本。
模型必须随部署依赖离线可用；不得在请求时下载。

## 15. 数据库迁移与回退

0065 up：

1. 前置 guard 精确确认 64/0064、revision 8、61 权限和 PRD-072 四表/trigger 完整。
2. 以现有迁移模式重建 RBAC 模板和 tenant permission 投影到 revision 9。
3. 创建三张 tenant-qualified 表、deferred 复合外键、终态部分唯一索引和 append-only/owner/count
   trigger；Finding -> Scan -> Event 的事务顺序必须可由数据库反向证明。
4. 把新事件加入 Shared Webhook 类型；数据库 source event 使用独立表，不改写 PRD-072 FileEvent。
5. final guard 校验 65 迁移、62 权限、角色 hash、表形状、外键和 integrity_check。

0065 down：

- 存在 `file.prompt_injection_detected` 订阅、delivery、dead-letter/replay 事实时拒绝降级。
- 删除 PRD-073 三表与新权限关系，RBAC 恢复 revision 8 和 61 权限。
- 不修改或删除 PRD-072 文件、抽取、DLP 和 source event 事实。
- up/down/up roundtrip 后角色矩阵、rowid、tenant owner 和外键必须一致。

0066 up：

1. 前置 guard 精确确认数据库位于 65/0065、三张安全事实表与完整性均成立，且 0066 对象不存在。
2. 创建 tenant-qualified `file_security_event_outbox`、`delivery_snapshot_frozen_at`、到期索引及
   insert/update/delete/rowid 状态约束；claim 和同 owner 续租均固定为 90 秒并受完整 CAS 保护。
3. 在同一迁移事务把既有 source 全部回填为立即到期 pending，再安装 source `AFTER INSERT` trigger。
4. 首次 worker claim 先在独立短业务事务提交 processing lease；提交后由当前有效 owner 在另一个业务
   事务以完整 CAS 补齐 legacy 部分 delivery、冻结包括空集合在内的订阅快照并续租；marker 形成后禁止
   按当前 Endpoint 追溯补建。
5. final guard 证明 source/outbox 一一覆盖、六个 trigger、外键和 integrity_check；迁移 runner 完成后
   数据库栈顶为 66/0066。

0066 down：

- 仅当 outbox 完全为空且没有 0066 之后迁移时允许回到 0065；任一 pending、processing、dispatched
  或 failed 行都是不可删除业务事实并原子拒绝降级。
- 只移除 0066 trigger、索引和 outbox，保留 0065 三张安全事实表、RBAC revision 9 和 checksum。

## 16. 测试用例

### 16.1 Shared 单元测试

- DOCX direct/style 继承 vanish、white、<=3pt、文本框/页眉页脚/脚注。
- PDF `Tr=3`、白色文本、超小字体、OFF OCG、EmbeddedFiles/FileAttachment。
- PDF 原始操作数无害但 `ToUnicode/CMap` 映射为注入正文时，必须以 PRD-072 持久抽取正文命中并阻断。
- DOCX 图片与 PDF 页面真实 OCR 中英文注入样本；普通图片不误报 Prompt finding。
- PDF 页面 OCR 与可见文本去重、CMYK/透明度、未知颜色空间、操作数/递归/图形栈上限。
- 嵌套 ZIP/embedding、entry/页数/像素/图片数上限与 coverage 失败关闭。
- Unicode 规范化、规则上限、无灾难回溯、风险汇总和 deterministic report。
- HMAC evidence 与 boundary token owner 隔离、正文碰撞重派生和关闭 repr。
- 255 条结构 finding 饱和后仍保留 Prompt 与 coverage；集合总数固定 256，普通后续结构事实被丢弃。

### 16.2 Control 集成测试

- Admin/Developer/Viewer/SecurityAdmin 可按矩阵扫描；缺任一权限 403 且不解密 Blob。
- 同 tenant 主流程、幂等复用、并发唯一、事务内撤权、跨 tenant 404、随机 ID 404。
- 同一 `BEGIN IMMEDIATE` 权限快照读取并验真原文件与抽取 Blob；字体 CMap 映射出的模型正文端到端
  形成 blocked 报告，且 DTO、数据库与 Webhook 不出现正文或裸 evidence。
- processing/blocked/failed 文件 409；ready Blob 缺失/密文损坏固定 500；报告缺失 404；数据库不可写
  等无法形成事实的依赖异常统一 503/request_id。
- 高/critical、存在候选内容时 OCR unavailable/partial、failed/未扫描均由模型准入函数 403；
  无 OCR 候选内容不依赖 OCR 引擎，passed 才返回边界正文。
- 静态所有权测试拒绝 Gateway/其他业务模块直接导入文件解密或读取对象键。
- JSON/CSV strict DTO、no-store/nosniff、CSV formula 防护、无正文/对象键/fingerprint。
- source event exact keys、Webhook 签名/投递/死信/重放，正文和 finding 证据命中为零。
- source 提交后未执行 wake、Control 重启仍恢复同一 `fsev_`；失败退避、过期 lease 接管、迟到 worker
  CAS 失败、第十次过期终结，以及已有 pending delivery resume 都不得产生第二个 event identity。
- 首次 claim 先独立提交 processing lease；随后当前 owner 在独立事务完整 CAS 验证并补齐 legacy 部分
  original deliveries；无订阅者时仍写 `delivery_snapshot_frozen_at`，冻结后新增 Endpoint 不得收到旧 source。
- 90 秒初始 lease 与每个真实网络 attempt 前完整 CAS 续租；停服 `threading.Event` 被同步 worker
  观察后不开始下一次网络访问，lifespan 必须等待 worker 完成状态回写。

### 16.3 迁移测试

- up/down/roundtrip、revision 9、62 permissions、默认角色 hash。
- 三表 tenant owner、复合 FK、状态/风险/计数一致、不可变、REPLACE、显式 rowid 拒绝。
- deferred Finding/Scan 提交顺序、孤立 Finding 提交失败、失败 attempt 可重试/上限、权威结果并发唯一。
- 256 finding 硬上限、溢出 coverage finding、读取 `LIMIT 257`、failed null score 与 128 字符链路 ID。
- 伪造跨 tenant finding/event、伪造计数/hash、降级 guard 和完整性检查。
- 65 -> 66、已有 source 原子 backfill、source/outbox 同事务 trigger、跨 tenant 复合 FK、状态机约束、
  `delivery_snapshot_frozen_at`、90 秒 claim/续租、空 outbox down/up、存在任一 outbox 事实拒绝 down、
  迁移幂等及 66/0066 完整性。

### 16.4 Console 测试

- 后端导航与实时权限、只读/可扫描视图、401/403 owner 清理。
- strict 报告 DTO、重复/未知 finding、file ID 不一致、迟到响应和 abort。
- 扫描、幂等、报告加载、CSV 下载/revoke、错误态、无敏感 DOM。
- 1280x720 与 390x844 无横向溢出、按钮/文本不重叠。

### 16.5 真实运行验收

1. 开发库迁移到 0066，`integrity_check=ok`、外键违规 0、revision 9/62 权限，并证明 source/outbox
   一一覆盖和 0066 trigger 完整。
2. 启动 Gateway、Control、Console，三服务健康 200。
3. 真实上传 clean TXT、隐藏 DOCX、含图片指令 DOCX、隐藏文本 PDF、`ToUnicode/CMap` 字体映射
   PDF 并执行扫描；字体映射样本的原始操作数无害，但持久抽取正文包含注入指令且必须 blocked。
4. 通过独立后端准入验收器证明高风险报告 `model_access_status=blocked` 且正文解密函数未被调用；
   clean 文件只返回带不可预测 BEGIN/END 边界的内存上下文。该验收不调用任何上游模型。
5. 下载 CSV，验证固定列、无正文/文件名/fingerprint/对象键和 formula 注入。
6. 验证无权限、跨 tenant、OCR 不可用、覆盖超限、并发和统一 request_id。
7. 扫描数据库、Blob、日志、Webhook、JSON/CSV 与 DOM 中的 Prompt/OCR/Token/密码 canary。
8. 制造只有 source、尚无 delivery 的恢复场景，不依赖请求内 task；重启 Control 后证明同一 `fsev_`
   被恢复且 outbox 进入 `dispatched`；同时证明空快照 marker、legacy 部分快照补齐、冻结后不追溯
   新 Endpoint、逐网络 attempt 续租。故障演练只按有界状态机退避/终结，协作停服等待同步 worker，
   停服后无 processing lease 或未清理运行资源。
9. 生成新的 `66/0066` 脱敏 Runtime JSON 与 SHA-256；0066 不改页面，既有桌面/移动 Browser 证据继续有效。

## 17. 代码修改计划

1. Shared 新增文件安全合同、DOCX/PDF 检测、离线 OCR adapter、模型边界构造和 Webhook 合同。
2. 扩展运行配置、统一错误、i18n、诊断和依赖清单。
3. 新建 0065 up/down 与迁移测试；RBAC revision 9 和后端依赖。
4. 追加 0066 up/down、source trigger/outbox、90 秒 lease/逐网络 attempt CAS 续租、持久 delivery
   snapshot 和 `threading.Event` 协作停服 worker；Control lifespan 启动恢复且不改写 0065。
5. Control 新增 security models/service/routes，复用文件密钥和加密 Blob owner，不修改 FileInfo。
6. Console 新增 `file-security` feature/lib/state/render/bind，接入导航、i18n、样式和 owner 清理。
7. 补 Shared/Control/Console/验收器测试、README、目录说明、API 文档和 testing 记录。
8. 只在直接改动后跑聚焦测试；收口执行约定回归、迁移和真实 Runtime 验收，页面未改则复用有效 Browser 证据。

## 18. 编码前自审

### 第一轮自审

状态：**已完成并修正（2026-08-05）**。

本轮以需求遗漏、误放行、正文隐私、owner、API exact-set 和模型准入绕过为重点，发现并修正：

1. **失败扫描被永久幂等键锁死**：权威部分唯一索引只覆盖 passed/blocked；failed 使用最多 10 次
   attempt，允许依赖恢复后重试且防止无界放大。
2. **Scan 先插入时无法反向证明 Finding 计数**：改为 Finding deferred FK -> 终态 Scan -> Event
   的单事务顺序；Scan insert trigger 在提交前看到完整 finding 集合，孤立 finding 不能提交。
3. **DOCX `color=auto` 误判白色**：auto 只有解析出主题/背景后才能判定，未知转 coverage，避免
   合法默认字体被全部标 high。
4. **不支持图片格式被静默跳过**：EMF/WMF、损坏媒体或解码失败统一形成 coverage partial 并阻断。
5. **PDF 无差别 OCR 前 N 页造成误阻断和浪费**：先扫描全部页资源，只渲染含图片候选页；未覆盖
   候选图片才 partial，纯文本长 PDF 不受 OCR 页限误伤。
6. **撤权后仍可能解密正文**：POST 在初次依赖后、Blob 读取前立即复核两个权限，提交事务再复核；
   模型上下文函数也实时复核 `file.read`。
7. **并发多实例重复报告**：passed/blocked 部分唯一索引选出唯一权威结果，冲突请求加载赢家。
8. **公共 hash 与数据库能力边界不清**：数据库 trigger 证明 owner/计数/风险；服务读写路径重算
   canonical report SHA-256，SQLite 不伪装具备哈希函数。

### 第二轮自审

状态：**已完成并修正（2026-08-05）**。

本轮以并发/崩溃、OCR 成本、RBAC 撤权、迁移可逆、Webhook 恢复、相邻 PRD 兼容和验收可执行性
为重点，发现并修正：

1. **页面 OCR 会重复读取可见文字**：OCR span 与同页/part 可见文本做有界比对，重复内容交给
   visible detector，只有图片新增指令才标 `ocr.prompt_instruction`。
2. **PDF 可见性操作覆盖不全**：补充 CMYK、颜色空间、透明度、操作数、图形栈和递归上限；
   影响可见性的未知操作转 coverage partial，禁止按默认可见放行。
3. **OCR 解压与结果本身可放大内存**：先验像素检查，不修改 Pillow 全局门限；对 box 数、单段和
   总字符逐步计数，超限时直接 blocked。
4. **failed attempt 并发编号冲突**：冲突后有界重读一次；若已有权威结果则复用，否则分配下一
   attempt，不能泄露 SQLite 异常。
5. **报告 hash 规范不完整**：冻结 exact keys、finding 顺序和 ASCII JSON；数据库校验计数/风险，
   服务写入及读取重算 hash，各层不伪装具备对方能力。
6. **只有内部函数但仍可能被其他模块绕过**：新增静态代码所有权测试，限制 decrypt/对象键/Blob
   Store 仅由文件持久化和安全服务使用；准入验收证明 blocked 路径在解密前结束。
7. **失败报告读取语义含糊**：GET 优先返回当前权威 passed/blocked，否则返回最新 failed；从未扫描
   才 404，页面能真实显示依赖故障并允许重试。
8. **OCR unavailable 与 HTTP 503 混淆**：能形成 coverage 报告时 HTTP 200 + blocked；只有不能形成
   或提交可信事实才 503。
9. **RBAC 迁移只有权限数没有关系数**：冻结 revision 9、62 permissions、166 role templates，避免
   新权限只进目录却漏授 SecurityAdmin 或业务 Viewer。
10. **相邻 PRD 越界风险**：不改 FileInfo、不新增直传格式、不接入 Scheduler、不扩展 Chat 请求；
    仅提供未来消费者必须使用的安全上下文入口，保持 PRD-072/074 合同稳定。
11. **coverage 与 failed 风险枚举混用**：`coverage.incomplete` finding 固定 high；只有无法形成可信
    findings 的 failed scan 使用 `risk_level=unknown/risk_score=null`，避免 0 分被解释为安全。
12. **finding 数量无硬上限**：冻结单次 256 条上限，溢出收敛为一条 coverage finding，并用
    `LIMIT 257` 在读取路径证明数据库事实没有绕过服务限制。
13. **链路 ID 上限漂移**：`source_request_id/source_trace_id` 都冻结为 128，与 0064 合同一致。
14. **Blob 缺失错误语义过轻**：ready 行对应 Blob 缺失、对象键不一致或密文损坏固定归类
    `file.integrity_failed`，不能伪装成尚未 ready。
15. **OCR 部署状态误阻断纯文本**：只有结构遍历发现 OCR 候选图片/页面才要求引擎；TXT、无图片
    DOCX 和纯文本 PDF 固定 `not_required`，不因引擎未加载阻断。
16. **failed 事实与数据库故障混淆**：数据库仍可写且 owner/fingerprint 已验证时才保存 failed；
    数据库本身不可用只能返回 503，不能声称报告已持久化。

两轮修正后形成的冻结设计覆盖 tenant owner、后端权限、统一错误、request_id、迁移、隐私、资源
边界、报告导出和非真空模型准入验收，并作为后续实现与实现后终审的基线。

## 19. 实现后安全终审

状态：**两轮实现后交叉复核均已完成并修正（2026-08-05）**。

### 19.1 字体映射语义一致性

终审构造了原始 PDF 文本操作数无害、但字体 `ToUnicode/CMap` 把字符映射为
`ignore previous instructions` 的样本。原实现只用原文件内容流做 Prompt 规则判断，会漏掉
PRD-072 实际持久化并交给模型的字符语义。修正如下：

1. `scan_file_security()` 的 `trusted_extracted_text` 改为必填且执行严格类型、非空和长度校验；
   缺失时不能退回原始操作数猜测。
2. Control 在同一个短 `BEGIN IMMEDIATE` 权限快照内复核双权限与 tenant owner，并读取、认证、
   验真原文件 Blob 和抽取 Blob；原件用于结构/OCR，抽取正文用于模型实际字符语义。
3. Shared 单元、Control 真实上传链路和 Runtime 固定字体映射样本共同证明 CMap 指令形成 blocked；
   报告、事件、日志和数据库仍只保存固定元数据与分域 HMAC，不保存正文。

### 19.2 finding 饱和时的安全事实优先级

终审进一步确认，若 255 条中低风险结构 finding 先占满具体事实容量，第 256 条只补 coverage，
随后出现的可信正文 Prompt 可能被容量门禁丢弃。虽然报告仍会因 coverage 阻断，但会丢失明确的
Prompt 原因和 `file.prompt_injection_detected` Webhook。修正后的 `FindingCollector`：

1. 始终保留一条 `coverage.incomplete/high` 证明扫描结果不完整。
2. 饱和后允许 Prompt finding 替换一条非 Prompt、非 coverage 结构 finding；不增加总数，不替换
   已保留的 Prompt 或 coverage。
3. 以 Shared 收集器边界测试和 Control 端到端测试证明最终恰为 256 条，Prompt、coverage、blocked
   状态与 7 字段安全事件同时存在，正文和 evidence canary 在 DTO、数据库及事件中零命中。

上述两项修复不改变公开 API、tenant/RBAC 矩阵或 Gateway 热路径，但最终交叉审查随后发现 Webhook
source 提交后的崩溃丢失窗口，见第 20 节。因而本节曾取得的 `65/0065` Runtime 只作为 0066 之前的
中间检查点，不再是 PRD-073 终态证据；最终计数、Runtime 路径与 SHA-256 已在 0066 实际验收后更新到
`docs/testing/PRD-073-tests.md`。0066 不改公开 API 和 PAGE-103，既有双视口 Browser 证据仍有效。

PRD-073 未凭该中间检查点标记完成；第 20 节最终门禁实际通过后，下一条权威未完成项仍为
`PRD-074 | M2-企业增强 | P2 | 批处理调度 | 异步批处理 Scheduler 与 Worker`。全部 108 条持续开发，
`all_108_prds_complete=false`。

## 20. 最终交叉审查修复设计、自审与实现

状态：**设计、两轮编码前自审、实现与最终门禁均已完成（2026-08-06）**。

最终交叉审查确认，原实现把 `file_security_events` 与扫描报告原子提交后，才把首次 Webhook
派发加入进程内 `BackgroundTasks`。若进程在提交后、创建 `webhook_deliveries` 前退出，既有 replay
worker 无法从只有 source event 的事实恢复 intent。本节冻结并落实修复设计；它覆盖并替代前文“仅机会性
派发即可恢复”的描述，不改变公开文件 API、PAGE-103、RBAC revision 9 或检测结果合同。

### 20.1 数据与迁移设计

新增 `0066_file_security_event_outbox_recovery.sql` 及 down migration，不修改已经发布且有 checksum
的 0065。新建 tenant-qualified `file_security_event_outbox`，与不可变 source event 一对一：

| 字段 | 约束与用途 |
| --- | --- |
| `file_security_event_id/tenant_id` | 复合外键引用同 tenant `file_security_events`，source identity 不可改写 |
| `dispatch_status` | `pending/processing/dispatched/failed` 固定状态机 |
| `dispatch_attempts` | 0..10；每次取得 lease 前原子递增 |
| `next_attempt_at/dispatched_at` | pending 到期时间或唯一成功终态时间，互斥 |
| `claim_token/lease_owner_id/lease_expires_at` | processing 三元组；90 秒租约、逐网络 attempt 续租与完成 CAS |
| `delivery_snapshot_frozen_at` | claim 提交 processing lease 后，由当前有效 owner 在独立事务持久冻结 original delivery 集合；零 Endpoint 也写 marker |
| `last_error_code` | 只允许固定 Webhook/事件错误码，禁止异常文本、URL 或 payload |
| `created_at/updated_at` | UTC 秒精度；状态推进必须单调 |

0066 的 `AFTER INSERT ON file_security_events` trigger 在扫描同一事务内创建 pending outbox；迁移对
已有 source event 原子回填 pending，关闭升级前已经形成的孤儿窗口。索引只覆盖到期状态和稳定排序。
数据库约束/trigger 必须拒绝跨 tenant owner、REPLACE、身份改写、非法状态组合、尝试次数倒退和删除。
claim 与续租固定 90 秒。claim 先在独立短事务中由完整到期条件选出一条 source，原子递增 attempt、写入
processing/token/owner/lease 并提交；它不负责验证 source 或冻结 delivery。提交后，当前且未过期 owner
只能在另一个独立事务以同 tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 推进，复核不可变
source、补齐 legacy 部分 delivery、创建/恢复 intent、写 `delivery_snapshot_frozen_at` 并续租；空快照也
必须冻结。down 只允许在 outbox 无任何业务行且没有后续迁移时执行，不能通过降级删除未投递或已投递历史。

### 20.2 Worker 与投递语义

Control lifespan 安装文件安全 source-outbox scheduler；Gateway 热路径不扫描该表。每轮先在独立短
`BEGIN IMMEDIATE` 中领取至多一行，原子提交 processing lease 后关闭 claim 连接；claim 事务不预验证
source，也不冻结 delivery。当前 owner 再从同 tenant immutable source 重建 exact-key 事件，并在另一个
独立快照事务以完整 CAS 验证已有 original deliveries 的 tenant/event/time/payload，补齐当时 active 且
订阅该事件的 Endpoint，并与 `delivery_snapshot_frozen_at` 一起提交；0065 遗留部分集合由此补齐，零
delivery 也有持久 marker。确定性 source 交叉校验失败时，仍使用当前 claim 的完整 CAS 转为
`failed + webhook.payload_invalid`，不得在 claim 前隔离或伪造 attempt。随后调用
`dispatch_webhook_event(..., resume_existing_pending=True, create_snapshot_if_missing=False)`，只恢复
冻结集合，不按当前 Endpoint 补建 intent。

初始 claim 使用 90 秒 lease；公共 dispatcher 在每个真实网络 attempt 前回调 source guard，先检查
`threading.Event` 停服信号，再以 tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 续租
90 秒。source worker 只在 dispatcher 返回后用同一 owner 条件 CAS 完成。语义为稳定 `event_id` 的
at-least-once：远端收包后、本地确认前崩溃可能重发，但并发 worker、迟到旧 worker和普通重启不能
覆盖新 owner、扩大冻结 Endpoint 集合或创建另一 event identity。

公共 Webhook attempt 回写还必须以 `tenant + endpoint + delivery + pending + attempt_count +
secret_version` 完整 CAS；网络返回后若 source reaper 已先把 delivery 终结，迟到 worker 的 attempt 与
摘要更新整体回滚。source 进入 `failed` 前，同一事务先把该事件剩余 original pending delivery 转为
固定失败并由既有 trigger 建立死信；已经 success/failed 的混合快照保持不变。0066 source trigger
同时禁止 `dispatched/failed` 终态仍存在 pending delivery。

路由提交后只调用 `wake_file_security_event_scheduler()` 提示已运行的 scheduler，不直接 drain、领取
lease 或执行网络投递；wake 信号本身不承载正文、finding 证据、Endpoint 信息或恢复正确性。即使 wake
完全未执行或进程立即退出，持久 pending 仍由下次 Control lifespan 启动 drain 或固定轮询恢复。无匹配
active Endpoint 时，公共 dispatcher 的空订阅快照视为已处理；以后新增
订阅不追溯旧事件。失败按 5/30/300 秒有界退避，最多 10 个 source dispatch attempt；attempt=10 的
lease 过期时直接终结 failed，不能进行第十一次 source dispatch。日志只记录固定 event/error code，
不记录异常正文。停服时 lifespan 设置线程安全 stop event、唤醒 scheduler，并等待
`asyncio.to_thread` 中的同步 worker 完成当前 attempt 和数据库收尾；不能用 task cancel 假装同步线程
已经退出，也不能在观察 stop 后开始下一次真实网络访问。

### 20.3 定向测试与实际验收

- 迁移：65 -> 66、已有 source backfill、同事务 trigger、跨 tenant/FK、状态约束、空库 down/up、
  有业务 outbox 时拒绝 down、幂等迁移、完整性和外键检查。
- Worker：提交后首次 task 未执行、服务重启后投递；90 秒 claim；每个真实网络 attempt 前完整 CAS
  续租；失败退避；过期 lease 接管；旧 worker 迟到 CAS 失败；第十次孤儿 lease 终结；两个 worker
  只领取一次；稳定 event ID 恢复已有 pending delivery；坏 source 先真实 claim，再由有效 owner 完整
  CAS 隔离后继续领取后续事件。
- 快照：claim 先独立提交 processing lease；随后当前 owner 在独立快照事务以完整 CAS 补齐 0065 legacy
  部分 original deliveries；无订阅者仍写 `delivery_snapshot_frozen_at`；marker 后新增 Endpoint 不追溯；
  坏 tenant/event/time/payload 必须在真实 claim 后隔离并零出网；source 终态同步关闭 pending delivery、
  保留 success，并证明迟到 attempt 不能覆盖 failed。
- 停服：`threading.Event` 在下一网络 attempt 前被 source guard 观察；lifespan 等待同步 worker 完成，
  不取消 `to_thread` 后留下写库或外呼线程。
- 安全：重建信封 exact keys，source tenant 与事件 tenant 一致，数据库/日志/证据中 Prompt、文件名、
  URL、Secret、Token 和异常文本零命中。
- 回归：PRD-073 Control/迁移聚焦、Runtime 验收器合同、相邻 Webhook resume/replay 测试、中文注释、
  Ruff、`py_compile`；实际三服务运行必须证明 66/0066、孤儿 source 恢复和资源清理。

### 20.4 第一轮修复设计自审

状态：**已完成并修正（2026-08-05）**。

1. **仅扫描 `file_security_events` 会产生并发重复发送**：新增独立 lease/CAS 状态，不让多实例直接
   对同一 source 调 dispatcher；claim 事务先提交唯一 processing owner，后续快照/intent 事务仍需完整
   owner CAS。
2. **先提交 source、后插 outbox 仍保留原崩溃窗口**：使用数据库 `AFTER INSERT` trigger，让二者在
   同一扫描事务提交或回滚；0066 同时回填存量 source。
3. **在扫描写事务内调用网络会扩大锁和反向回滚风险**：扫描事务只写 source/outbox；worker 的 claim
   lease 与后续快照/intent 事务也都在网络前完成，网络严格在这些事务提交后。
4. **没有订阅者时无法区分“已冻结空集合”和“尚未快照”**：新增持久
   `delivery_snapshot_frozen_at`；空订阅快照同样写 marker，source 可进入 dispatched，后增订阅不追溯。
5. **直接改 0065 会破坏已应用 checksum**：冻结为追加 0066，保留 0065 历史和可验证迁移链。

### 20.5 第二轮修复设计自审

状态：**已完成并修正（2026-08-05）**。

1. **worker 崩溃在第十次 source dispatch 后可能突破上限**：attempt 在 dispatch 前持久递增；
   attempt=10 的过期 processing 行只终结 failed，不再领取。
2. **固定短 lease 无法覆盖多个 Endpoint 的串行投递**：初始 lease 固定为 90 秒；每次真实网络 attempt
   前使用 tenant/event/token/owner/attempt/当前且未过期 lease 完整 CAS 续租，失败则零出网。
3. **0065 可能已经留下部分快照**：claim 先独立提交 processing lease；随后当前 owner 在独立快照事务
   以完整 CAS 验证已有 original delivery，并为当时 active 订阅补齐缺项，再和 marker/续租一起提交；
   任何不一致都整体回滚快照事务，但不伪造 claim 前验证。
4. **恢复时重新读取当前订阅会改变历史快照**：marker 后调用 dispatcher 固定
   `resume_existing_pending=True, create_snapshot_if_missing=False`，只恢复冻结 delivery；空快照也不
   因后来新增 Endpoint 被误判为“缺失快照”。
5. **outbox 可能成为跨租户重建入口**：claim 必须复合连接 tenant/source ID，并对 owner、枚举、时间、
   exact payload 逐字段校验；任何损坏统一折叠为固定内部事件错误。
6. **取消 asyncio task 不会终止 `to_thread` 同步外呼**：lifespan 使用 `threading.Event` 协作停服；
   guard 在下一次真实网络 attempt 前观察 stop，lifespan 等待同步 worker 完成当前 attempt 和数据库收尾。
7. **down 删除恢复事实会伪造可回退性**：只允许空 outbox 回退；存在 pending、processing、dispatched
   或 failed 任一行时原子拒绝。
8. **第十次 source 终结可能遗留永久 pending delivery**：三个失败入口先在同一写事务终结剩余
   original pending delivery、触发固定死信，再以完整 source CAS 进入 failed；任一步失败全部回滚。
9. **损坏的最早 due source 会阻塞全部后续事件**：先完成真实 processing claim，再由当前 owner 交叉
   验证；确定性失败时用 tenant/event/token/owner/attempt/lease 完整 CAS 以
   `webhook.payload_invalid` 隔离并继续稳定排序领取，不让坏 tenant/source 永久占据全局队首，也不伪造
   attempt=10。
10. **网络返回后的迟到 worker 可能覆盖 reaper 终态**：delivery 摘要更新新增 pending、前一 attempt
    和 Secret 版本 CAS；不命中时回滚本轮 attempt，不能把 failed 改回 pending/success。

### 20.6 实现落点与最终验收门禁

  0066 up/down、`file_security/outbox.py`、`file_security/scheduler.py`、路由 wake 和 Control lifespan
安装均已落地；claim 先在独立短事务提交 processing lease，公共 Webhook helper 随后在当前 owner 的独立
快照事务以完整 CAS 补齐 legacy delivery 并冻结空/非空快照，pending resume 禁止按当前 Endpoint 补建。
坏 source 只有在真实 claim 后交叉验证失败，才以完整 owner CAS 隔离为 `webhook.payload_invalid`。每次真实
网络 attempt 前执行 90 秒完整 CAS 续租和 stop 检查；lifespan 用
`threading.Event` 等待同步 worker。恢复还会逐字段复核 source tenant、事件身份、scan/file/actor、
request/trace/time、七字段 payload 与 Prompt finding。最终标记门禁及实际结果如下：

1. 0065/0066 迁移、90 秒 claim/续租、快照冻结、legacy/空快照、协作停服、source-outbox worker、
   PRD-073 API、相邻 Webhook resume、20 个验收器合同、中文注释、目标静态检查和受影响回归均通过；
   真实结果已写入测试记录。
2. 三服务实际运行证明 `66/0066`、Control 真停服、停服期间 source 提交、新 lifespan scheduler 恢复、
   稳定 event ID、attempt 1 `dispatched`、完整性/外键、隐私和资源清理；权威 Runtime 为
   `prd073_20260806T062221Z_b2632ac89f57fc3a`，JSON SHA-256 为
   `E2EFF29227A4246AE8FBB86B5B3C973C6E5212A1568AA32D9ABFB05B0DAE34FD`。
3. 0066 未修改 Console、公开 API 或视觉布局，既有双视口 Browser 证据继续有效；2026-08-06 的实际
   隔离库页面复验又确认桌面/移动无横向溢出、幂等扫描完成、页面 error/warn 为 0 且 Session 已登出。

上述门禁已经完成，PRD-073 恢复“已完成”状态；下一轮从 PRD-074 开始，总体目标始终为
`all_108_prds_complete=false`。
