# PRD-072 文件上传与文本解析基础测试记录

状态：**已完成（2026-08-05）**。设计、两轮编码前自审、实现、0064 迁移、受影响分层回归、真实
Runtime、后端租户/RBAC、统一错误、应用内 Browser 双视口、隐私门禁、资源清理和文档均已通过。
该结论只表示 PRD-072 完成；总体目标仍为 `all_108_prds_complete=false`。

## 1. 分层自动化结果

以下结果均来自本轮取得明确退出码的命令；小修复只复跑直接受影响集合，最终收口才执行一次约定回归。
所有 pytest 均设置 `TEMP/TMP=D:/MetaHub/.tmp`、唯一 `--basetemp` 和 `-p no:cacheprovider`，没有使用
`C:/tmp`、没有提权，也没有把无权临时目录当成代码失败。

| 验证项 | 结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared 文件合同 | `25 passed` | 格式、文件名、TXT/DOCX/PDF、压缩边界、Token、DLP、AES-GCM/HMAC、Blob Store |
| Control 文件 API | `13 passed` | multipart、主/异常、权限、租户、cursor、Webhook、事务内二次鉴权 |
| 0064 迁移 | `6 passed` | up/down/roundtrip、revision 8、owner/状态机/不可变、降级 guard、完整性 |
| Console 导航合同 | `2 passed` | PAGE-101/102 菜单、后端权限投影和前端路由合同 |
| Runtime 验收器合同 | `6 passed` | 密码仅环境注入并读取后移除、路径/回环、真实 DOCX/PDF、Session 清理、证据隐私、多编码日志扫描 |
| Console 最终聚焦组合 | `18 passed` | strict DTO、owner/generation、上传、筛选、权限降级、DOM 隐私、移动布局和登录刷新 owner |
| Console lint | `209 files passed` | 全部 Console JavaScript 静态语法与仓库约束 |
| 日志隔离组合 | `5 passed` | httpx/httpcore/pypdf 自由文本全部在 formatter 前失败关闭 |
| RBAC revision 8 兼容组合 | `78 passed` | 0062/Chat/策略模板/意图/会话风险历史合同与 file.read/write 新矩阵 |
| Python 语法与中文注释 | `py_compile/compileall` 通过；`comment coverage ok` | 新模块、类、函数和关键业务约束注释基线 |
| 实时多编码日志复扫 | 正式证据 3 files；停服后 5 files，均 0 hits | Session Token/JWT、密码、文件名、正文 marker、DLP pattern，UTF-8/UTF-16LE/BE |

最终全量回归的权威数字记录在第 5 节；上表保留开发阶段专项，不能把不同代码时点拼成一次全量通过。

## 2. 核心合同与异常闭环

### 2.1 上传、解析和资源边界

- `POST /files` 先执行 Bearer Session 和实时 `file.write`，再取得容量 2 的 limiter，最后读取 multipart；
  无 Token、过期 Session、无权限或容量超时都不会读取企业文件正文。
- multipart 只接受唯一 `file` part、唯一 Content-Type/Content-Disposition、规范 boundary 和匹配的
  Content-Length；真实文件 bytes 与 envelope 分别受 20 MiB 和额外 64 KiB 硬上限保护，不写明文临时文件。
- 扩展名、MIME 和魔数在创建 UploadedFile 前三方一致；文件名拒绝路径、控制字符、双向欺骗和危险复合后缀。
- TXT 严格 UTF-8；DOCX 限制 entry 数、解压体积、压缩比、宏/OLE/嵌套包和外部非 hyperlink 关系；PDF
  限制页数并拒绝加密/损坏输入。解析器异常只返回固定文件错误码。
- 深层解析和同步 SQLite/加密在受限 worker thread 执行，不阻塞登录和健康 event loop。

### 2.2 DLP、密文和状态机

- 抽取全文在发布对象前执行 tenant-first `input/both` DLP；读取第 101 条作为溢出探针，超过 100 条固定
  `file.dlp_unavailable`，不能静默截断尾部 block 规则。
- block 返回 HTTP 403 `file.dlp_blocked`，保留 blocked FileExtract、两条不可变事件和安全匹配摘要，但不创建
  原文件或抽取正文 Blob；clean/log_only 才发布两个认证密文对象。
- 文件名、原文件和抽取正文使用独立根密钥与 tenant/file/kind AAD；裸 SHA-256 不入库，内容 fingerprint
  使用 tenant/file/kind 分域 HMAC，对象键完全由随机 file ID 生成。
- processing 与终态分两笔事务，每笔都调用 `require_transaction_permission(..., "file.write")`；终态事务以
  tenant/file/status CAS、owner trigger、FileExtract/FileDlpMatch/FileEvent 外键和不可变 trigger 失败关闭。
- 数据库终态提交失败会删除本批已发布密文；stage/publish 失败会清理 staging 和部分对象，不记录宿主路径。

### 2.3 读取、权限和统一错误

- `GET /files` 与 `GET /files/{id}` 均使用后端 `file.read`，tenant 只来自 AuthContext；跨租户 ID 与不存在同形
  404 `file.not_found`，列表不接受 tenant、actor 或排序注入。
- 游标签名绑定 tenant、状态筛选和 authorization fingerprint；撤权或 permission revision 变化后旧 cursor
  固定失效。所有成功和错误响应包含 `request_id/trace_id`，文件路由全状态 `no-store, private`。
- Admin/Developer/Viewer 获得读写；SecurityAdmin 只读；FinanceAdmin/OpsAdmin 默认无文件权限。前端 owner
  只防止预取和迟到回写，后端依赖与事务内权限复核才是授权事实。
- 文件名是唯一必要敏感展示字段，经过解密和 HTML 转义后只对当前 tenant 的授权 Session 返回；正文、对象键、
  fingerprint、规则 ID/pattern、匹配片段和完整 cursor 不属于公共 DTO。

## 3. 正式 Runtime 验收

脱敏证据：
[runtime-acceptance.json](../../.artifacts/prd072-runtime/prd072_20260804T212908Z_922f8ab2ff200912/runtime-acceptance.json)。
SHA-256：`0488B43A8D0FA3B465EF34271A76AF3FE9284E334E65C1C77E90E32861AA8AE3`。

| 验收项 | 正式事实 |
| --- | --- |
| 三服务 | Gateway、Control、Console 均为 HTTP 200 |
| 三格式主流程 | TXT、DOCX、PDF 共 3 个 ready；返回安全 FileInfo、Token 估算和 request_id |
| DLP | block 为 HTTP 403 `file.dlp_blocked`，blocked 文件没有正文 Blob |
| 权限 | SecurityAdmin 上传为 HTTP 403 `auth.permission_denied` |
| 租户 | 第二 tenant 读取默认 tenant 文件为 HTTP 404 `file.not_found` |
| 数据库 | 64 条迁移、最高 `0064`；`integrity_check=ok`，外键违规 0 |
| 不可变事实 | 4 UploadedFile、4 FileExtract、8 FileEvent、1 FileDlpMatch |
| 密文对象 | 6 个认证密文 Blob，文件正文和名称明文在数据库/Blob 中命中 0 |
| 日志与证据 | 3 份日志敏感值命中 0；正式 JSON 不含禁用键、正文、凭据或 Token |
| 清理 | 本批 1 条规则、2 个用户已禁用，2 个 Session 已撤销 |

正式证据生成后发现 Windows PowerShell `*>` 可能把进程日志转为 UTF-16LE；原验收器只查 UTF-8 bytes 会让
同一明文以宽字符表示时漏扫。收口修正 `_sensitive_log_encodings()`，以 UTF-8、UTF-16LE、UTF-16BE 三种
表示等价失败关闭，新增单测实际把中文 Session Token 写成两种 UTF-16 并证明可阻断。随后对当前三服务的
UTF-16LE/UTF-8 实时日志使用新登录 Token、已发送 Authorization 请求、密码、四个文件名、正文 marker、
临时邮箱和 DLP pattern 复扫，结果为 `files=3, sensitive_hits=0`。该定向闭环没有重跑无关根级测试。

## 4. Browser 运行验收

Browser 直接连接 `http://localhost:5173/#files`，使用真实 Admin 和一次性 SecurityAdmin Session；没有读取
浏览器 Storage/Cookie，也没有访问外部站点。

脱敏证据：
[browser-acceptance.json](../../.artifacts/prd072-browser/prd072_20260804T212908Z_922f8ab2ff200912/browser-acceptance.json)。
SHA-256：`A5F514B23C5ABF3917FB5A165F8CAA6B71AFC1A1FBDCD8035D72B03346AC29F0`。

| 视口/场景 | 结果 |
| --- | --- |
| `1280x720` | 文档宽度 `1265/1265`，横向溢出 0；上传栏、筛选、7 列记录和 pager 不重叠 |
| `390x844` | 文档宽度 `375/375`，横向溢出 0；表头隐藏、记录转 328px 单列，行间无覆盖 |
| 数据展示 | TXT/DOCX/PDF、ready/blocked、DLP、病毒未配置、Token、时间和 request_id 均可见 |
| 筛选 | 选择 blocked 后仅保留阻断行；重置恢复全部记录和新的 request_id |
| DOM 隐私 | 正文 marker、对象键、content hash、密文、规则 ID/pattern、cursor 和 Bearer 命中均为 0 |
| 权限降级 | SecurityAdmin 可见列表和“只读文件视图”，`#file-upload-input` 数量为 0 |
| 注销清理 | 注销后 `[data-file-root]` 和 `.file-name` 数量均为 0，登录表单恢复 |

管理员会话在 Browser 验收结束前已恢复，随后验收页已 finalize；临时 SecurityAdmin、第二 tenant Admin、
DLP 规则和活动 Session 均已禁用/撤销。运行库中的 UploadedFile/FileExtract/FileEvent 是 0064 设计要求的
不可变验收事实，不通过绕过 trigger 的方式删除；它们只位于 `.tmp` 隔离开发库，不进入 Git 制品。

## 5. 收口回归与运行状态

收口遵循“代码改动对应测试集合”的边界，没有因文档封账反复重跑历史全量：

- 文件 API 最终专项为 `13 passed`；Shared/迁移/导航/验收器及 RBAC 相邻合同结果均已在第 1 节登记。
- 登录后台刷新 owner 修复后，只复跑 `files.test.mjs`、`prd072BrowserAcceptance.test.mjs` 与
  `mainRefreshOwnership.test.mjs`，结果 `18 passed`；随后 Console lint 为 `209 files passed`。
- 提交前敏感扫描发现验收器曾包含本地管理员密码常量；收口时改为必填
  `METAHUB_PRD072_ADMIN_PASSWORD`，读取后立即从进程环境移除且配置 repr 隐藏。只复跑验收器合同，
  结果 `6 passed`，正式 Runtime JSON 和既有运行事实不受影响。
- Python `compileall` 通过，最终中文注释门禁返回 `comment coverage ok`。推荐 Python 环境未内置 Ruff，
  因此使用独立工具 `C:\Users\rabol\AppData\Local\hermes\hermes-agent\venv\Scripts\ruff.exe`；
  PRD-072 全部新增 Python 文件复核结果为 `All checks passed!`。该检查只覆盖本条新增文件，不把历史文件的
  既有行宽债务误记为 PRD-072 失败，也没有访问 `D:\MetaHub` 之外的项目数据或临时目录。
- 开发库为 `64/0064`，`integrity_check=ok`、外键违规 0；正式 Runtime/Browser JSON 均为 `status=passed`。
- 提交前只读健康复核为 Gateway `/health` 200、Control `/health` 200、Console `/` 200；随后只停止本次验收的
  三个服务及其启动包装进程，`5173/8000/8001` 监听数为 0。进程释放后对 `.tmp/prd072-live` 的 5 份日志执行
  UTF-8/UTF-16LE/BE 复扫，密码、JWT、文件名、正文 marker、DLP pattern 和临时邮箱敏感命中为 0。
  `.tmp/prd072-final-20260805` 是因端口占用而失败的非权威诊断目录，未计入结果。

所有 pytest 的临时目录均位于 `D:/MetaHub/.tmp`，没有提权或使用 `C:/tmp`。只有取得完整退出码或带哈希的
正式证据才计入本记录。

## 6. 最终结论

PRD-072 的设计、两轮自审、实现、迁移、相关分层测试、后端租户/RBAC/统一错误、真实 Runtime、密文和日志
隐私、Browser 双视口、中文注释及临时身份清理均已完成，可标记为已完成。Excel 多表交叉确认下一条未完成项
为 `PRD-073 | M2-企业增强 | P2 | 文件型注入防护 | 隐藏 Prompt Injection 检测`，对应 PAGE-103、
`POST /files/{id}/security-scan`、FileSecurityFinding、`file.prompt_injection_detected` 和 TC-0145/0146。
全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。
