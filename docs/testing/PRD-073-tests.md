# PRD-073 隐藏 Prompt Injection 检测测试记录

状态：**已完成并通过最终复核（2026-08-06）**。本文件将终审前阶段性基线与最终权威结果严格
分开；以下最终计数、Runtime/Browser 制品路径和 SHA-256 均来自实际命令。该状态只描述
PRD-073，全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 自动化结果与记录口径

### 1.1 终审修复前的阶段性基线

下表是终审发现 `ToUnicode/CMap` 绕过与 FindingCollector 饱和优先级问题之前已实际执行的历史
基线，用于保留开发证据，不代表当前最终代码时点的权威统计，也不能与后续结果相加冒充一次全量：

| 验证项 | 历史结果 | 主要覆盖 |
| --- | ---: | --- |
| Shared 检测器 | `30 passed` | TXT、DOCX、PDF、隐藏文本、嵌套容器、离线 OCR、资源上限与模型边界 |
| Shared 运行配置 | `8 passed` | 并发、页数、图片/像素上限、跨字段约束和固定 OCR 版本 |
| 0065 迁移 | `5 passed` | up/down/roundtrip、三张 tenant 表、不可变事实、RBAC revision 9 与降级 guard |
| Control 文件安全 API | `18 passed` | 扫描、报告、CSV、幂等、权限、租户、Webhook 与模型准入 |
| Control 导航 | `2 passed` | PAGE-103 的读取与扫描权限分离 |
| Runtime 验收器合同 | `9 passed` | 工作区路径、回环端口、密码环境注入、CMap 样本、失败清理与证据脱敏 |
| Console 相关组合 | `48 passed` | strict DTO、owner/generation/Abort、扫描、报告、CSV、401/403 和移动布局 |
| PAGE-103 直接回归 | `18 passed` | 列表重排、选中 ID、报告归属与迟到响应隔离 |
| Console lint | `214 files passed` | Console JavaScript 静态语法与仓库约束 |

### 1.2 最终权威结果

最终安全修正只复跑直接受影响集合；既有冻结回归保留原结果，不因局部 finding 顺序修正重复数小时
无关全量。0066 source-outbox 修复另以完整迁移文件、相邻 Webhook resume、scheduler/API 节点和
Runtime 合同闭环；这些独立命令不相加冒充一次全量：

| 验证项 | 最终权威结果 |
| --- | --- |
| PRD-073 五组 Python 分层（含 CMap、格式控制与饱和回归） | `55 + 16 = 71 passed`；Shared 32、配置 9、迁移 5、Control 23、导航 2，退出码均为 0 |
| 0066 迁移完整文件 | `23 passed`；up/down、backfill/trigger、完整 CAS、坏 source、快照、reaper 与终态约束 |
| Outbox API/scheduler 聚焦 | `7 passed`；路由只 wake、lifespan 恢复、housekeeping 进度与停服协作 |
| `payload_invalid` attempt 1..10 | `10 passed`；真实 claim 后完整 CAS 隔离，pending 不携带终态错误码 |
| PRD-053 Webhook resume 相邻回归 | `10 passed`；公共 dispatcher、pending resume 与终态 CAS 保持兼容 |
| Runtime 验收器合同 | `20 passed`；路径/回环、密码移除、真实 0066 trigger、失败恢复、多编码隐私与证据 SHA |
| 相邻 PRD-066/068/069/070/072 与 RBAC 冻结回归 | `93 passed`；终审修正未改这些模块，不重复无关节点 |
| PRD-072 文件链路冻结回归 | `25 passed`；上传、解析、DLP、权限和导航保持通过 |
| Console 全量 | `690 passed / 0 failed` |
| Console lint | `214 files passed` |
| Node 示例全量 | `33 passed / 0 failed`；在主工作区使用已安装的固定 `openai 6.47.0` 依赖执行 |
| 目标 Ruff、`py_compile`、中文注释门禁 | `All checks passed / 0 / comment coverage ok` |
| 正式 Runtime | `status=passed`；六样本、三服务、真停服/重启、scheduler、权限/租户、数据库、清理和 23 个隐私文件 |
| 应用内 Browser | 权威制品保持 `status=passed`；2026-08-06 又实测 1280x720/390x844、幂等扫描、零横向溢出和页面错误 0 |

Python 命令统一设置：

```powershell
$env:TEMP = 'D:/MetaHub/.tmp/<unique-prd073-directory>'
$env:TMP = $env:TEMP
$env:PYTHONPATH = 'shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src'
$python = 'C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
```

每次 pytest 使用唯一的 `--basetemp D:/MetaHub/.tmp/...` 和 `-p no:cacheprovider`。全过程限定在
`D:/MetaHub/.tmp`，不使用 `C:/tmp`、不提权，也不把 pytest 临时目录权限问题当成代码失败。

## 2. 主流程与异常/权限闭环

### 2.1 检测与模型准入

- clean TXT 形成 `passed + complete + allowed` 权威报告；同 detector 重扫复用原 `scan_id`。
- 可见指令命中 `prompt.visible_instruction`；DOCX `w:vanish` 与 PDF `Tr=3` 命中
  `visual.hidden_text`；真实 DOCX 内嵌 PNG 经 `rapidocr-onnxruntime-1.4.4` 命中
  `ocr.prompt_instruction`。
- 白色/超小字体、OFF OCG、嵌入对象、未知可见性、OCR/页数/像素/递归上限不会静默放行；覆盖不完整
  形成 high coverage finding 并阻断。
- 原文件负责结构/OCR，PRD-072 已认证持久抽取正文负责模型实际字符语义。原始 PDF 操作数无害、
  但字体 `ToUnicode/CMap` 映射后抽取正文为注入指令的固定样本必须形成 blocked 报告。
- 255 条结构 finding 饱和时仍优先保留可信正文 Prompt、Unicode 格式控制与 coverage；结构/OCR
  内部先饱和、Prompt 后到时也会在 256 条硬上限内替换一条普通事实并产生唯一 Webhook。
- 模型入口先实时检查 `file.read`、tenant owner、唯一权威报告和 canonical hash，再读取抽取密文；
  clean 只返回内存中的动态 BEGIN/END 边界，blocked 路径固定返回
  `403 file.model_access_blocked`。

### 2.2 后端权限、租户与错误

- 报告读取只要求后端 `file.read`；扫描同时要求 `file.read + file.security.scan`。前端按钮隐藏不是
  授权事实。
- 0065 把 RBAC catalog 升至 revision 9（62 个权限、166 条角色模板关系）；默认 Admin、Developer、
  Viewer、SecurityAdmin 获得扫描权限，FinanceAdmin/OpsAdmin 不因页面导航或其他管理权限被推导授权。
  自定义矩阵保持不变，运行期撤权由数据库 Session 快照立即生效。
- Control 在同一短 `BEGIN IMMEDIATE` 权限快照内重新读取数据库权限、复核 tenant/file/extract
  owner，并读取、认证、验真原文件 Blob 与抽取 Blob；写报告事务中再次复核双权限。
- 只读身份读取报告为 200、直接扫描为 403 `auth.permission_denied`；跨 tenant 的 GET/POST/CSV
  均与不存在资源同形返回 404 `file.not_found`。
- 非空 JSON body 返回 422 `infra.validation_failed`；未 ready、容量满、完整性损坏和检测依赖失败分别
  使用冻结的 409/429/500/503 错误码，响应始终保留原 `request_id`。
- 失败 attempt 使用有限重试和非权威记录；并发权威结果由数据库唯一索引选出，不能覆盖赢家或
  无限放大。

### 2.3 报告、CSV、Webhook 与隐私

- JSON/CSV 只返回固定枚举、计数和 finding 元数据；正文、文件名、对象键、report hash、evidence
  fingerprint 和 OCR 文本均不属于公开 DTO。
- CSV 使用固定列、`text/csv`、`no-store, private` 和 `X-Content-Type-Options: nosniff`，并防止公式注入。
- `file.prompt_injection_detected` 只在首次 high/critical Prompt 权威结果产生，payload 为精确 7 字段：
  `scan_id/file_id/risk_level/finding_count/high_finding_count/model_access_status/detector_version`。
- FindingCollector 达到 255 条具体事实后保留 `coverage.incomplete`；后到 Prompt 可替换一条非
  Prompt、非 coverage 结构 finding，总数仍固定不超过 256。回归必须同时验证 Prompt、coverage、
  blocked 和 Webhook 存在，正文/evidence canary 在 DTO、数据库与事件中零命中。
- 三张安全事实表均带 `tenant_id`，复合外键和 trigger 约束 owner、计数、状态与不可变；数据库、
  WAL/SHM、加密对象和日志隐私扫描禁止 Prompt、OCR、密码、Session Token 或文件名明文命中。
- 0066 为每个不可变 source 同事务创建 tenant-qualified pending outbox；worker 先在独立短事务真实 claim，
  再由当前 owner 在另一事务完成 source/scan/finding 校验、legacy delivery 补齐、空/非空快照冻结和续租。
- 坏 source 必须真实递增 attempt 后才以 tenant/event/token/owner/attempt/lease 完整 CAS 隔离；source
  终态、剩余 pending delivery 和死信同事务收敛，迟到 worker 不能覆盖新 owner 或终态。
- 隐私扫描强制六份启动日志存在且为仓库内普通文件，并把重启 stdout/stderr 纳入同一批次；日志 canary
  同时按 UTF-8、UTF-16LE 与 UTF-16BE 字节搜索，缺失日志不能静默算通过。

## 3. 正式 Runtime 验收

最终 Runtime 证据：
[runtime-acceptance.json](../../.artifacts/prd073-runtime/prd073_20260806T062221Z_b2632ac89f57fc3a/runtime-acceptance.json)

最终 Runtime SHA-256：`E2EFF29227A4246AE8FBB86B5B3C973C6E5212A1568AA32D9ABFB05B0DAE34FD`

最终 Runtime 必须由同一份 `runtime-acceptance.json` 证明：

| 验收项 | 实际事实 |
| --- | --- |
| 三服务 | Gateway、Control、Console 均为 HTTP 200 |
| 六个主样本 | clean passed；可见注入、隐藏 DOCX/PDF、真实 OCR DOCX、字体映射 PDF 均 blocked |
| 字体映射 PDF | 原始操作数无害，PRD-072 持久正文为注入指令，最终 `blocked` |
| 权限与租户 | 只读报告 200、只读扫描 403、跨租户 404、非法 body 422 |
| 模型准入 | clean allowed，全部注入 blocked，blocked 文件不进入模型上下文 |
| Source 恢复 | Control 真停服后提交 source；新 Control lifespan scheduler 自主恢复，验收器主动 drain=false |
| Outbox 终态 | attempt 1 `dispatched`，processing 曾被观察，claim 字段清空，nonterminal source=0 |
| 数据库 | 66 条迁移、最高 0066、RBAC revision 9、event/outbox 均 6、`integrity_check=ok`、外键违规 0 |
| 隐私 | 扫描数据库 1、密文对象 14、启动/重启日志 8，共 23 个文件；三编码敏感明文命中 0 |
| 清理 | 临时用户禁用 2、Session 撤销 3，active user/session 均为 0，密码与 Token 未进入证据 |

仓库内 [Runtime 验收器](../../scripts/acceptance/prd073-runtime-acceptance.py) 只允许从
`METAHUB_PRD073_ADMIN_PASSWORD` 读取一次性管理员密码，读取后立即从环境移除；数据库和对象目录
只能位于工作区 `.tmp`，目标 URL 只能是三个固定回环端口。合同测试本身不发出网络请求。
`SHA256SUMS.txt` 恰好一行，逐字匹配上述 JSON；最终证据保持
`all_108_prds_complete=false`。

## 4. 应用内 Browser 验收

最终 Browser 证据目录：
[prd073_20260805T045800Z_4f3ee3d6aa3f233b](../../.artifacts/prd073-browser/prd073_20260805T045800Z_4f3ee3d6aa3f233b)

| 视口/场景 | 验收合同 |
| --- | --- |
| `1280x720` | 横向溢出 0，交互控件裁切 0，截图非空 |
| `390x844` | 横向溢出 0，交互控件裁切 0，截图非空 |
| 报告交互 | 真实 OCR 阻断报告、风险指标、发现表、扫描与 CSV 控件可见；字体映射由 Runtime/API 验证 |
| CSV | 成功事件带有效 `request_id`；临时下载 anchor 已移除 |
| 页面质量 | console error/warn 为 0，无控件重叠；登录、选择、刷新和注销闭环完成 |
| DOM 隐私 | Prompt、对象键、report SHA、evidence fingerprint、文件名 URL/dataset 命中均为 0 |

浏览器扩展层不能稳定捕获下载事件，因此不把“扩展捕获到下载”列为完成条件；CSV HTTP 内容、
响应头和隐私合同由生产路由集成测试覆盖，页面侧只记录实际可观察的成功事件、`request_id` 与
anchor 清理事实。

0066 未改 Console 或公开文件安全 API，因此保留上述权威截图制品。2026-08-06 又在最终隔离库与
三服务上实际登录 PAGE-103：桌面 `1280x720` 与移动 `390x844` 的 document scrollWidth 均不超过
viewport，阻断报告、风险指标和两条安全发现可见，幂等“扫描”交互完成，console error/warn 为 0，
最后登出并恢复登录页。该复验不伪造或替换既有截图 SHA。

## 5. 实现后终审修复与最终证据

实现后安全终审关闭了三个会影响验收结论的问题：

1. **PDF 字体映射语义绕过**：`scan_file_security()` 强制接收 `trusted_extracted_text`；Control 在同一
   权限快照内读取并验真原件和持久抽取 Blob，阻断 `ToUnicode/CMap` 映射出的模型指令。
2. **finding 饱和丢失 Prompt 事实**：饱和后 Prompt 可替换非 Prompt、非 coverage 结构事实，确保
   blocked 报告、明确 Prompt 原因和 7 字段安全 Webhook 同时存在。
3. **source 已提交但 intent 未创建的崩溃窗口**：0066 用同事务 trigger 创建 source outbox；Control
   lifespan scheduler 以真实 claim、完整 owner CAS、持久快照和稳定 event ID 恢复，路由只 wake。

最终证据已复核：

- 开发库从权威 0065 备份恢复后只应用最终 0066，现为 `66/0066`；0066 checksum 为
  `e5cd51c00ebe3e43df026e9e6f892e504dadc0b4e09fd0fbb13f49eeacce5af4`，
  `integrity_check=ok`、外键违规 0、revision 9 权限模板 62、角色模板 166。
- 最终开发库本地备份位于
  `D:/MetaHub/.tmp/prd073-dev-db-final-0066-20260806/metahub-dev-prd031-0066-final.db`，SHA-256 为
  `3963dc2d28b4413b9cfe87aa3b5382030f42cb2540a5721d7f812f31d056f49b`。0065 回退点仍保留在
  `D:/MetaHub/.tmp/prd073-dev-db-backup-0065-20260805T182641/`，SHA-256 为
  `5a73bdeb5f12c7c55adefca323936600353d34c25695044a13db3c0849b1e355`；数据库均不进入 Git。
- Runtime `SHA256SUMS.txt` 恰好一行并匹配 JSON
  `e2eff29227a4246ae8fbb86b5b3c973c6e5212a1568aa32d9abfb05b0dae34fd`；证据中 migration 为
  `66/0066`、active drain 为 false、attempt 为 1、claim 已清空、23 个隐私文件明文命中 0。
- Browser `SHA256SUMS.txt` 与当前四个文件逐项一致：JSON
  `2a942f773f63adc2ac81abdfcaff2e965c5b9d2033cb0860d6004e4c44b1cb65`、桌面
  `4ed1ee15b43b2889092f90ec1a7a5a39828a36c538ff496debde02dccf430969`、移动主页
  `dd2bd7deee5ed161eb9fdebd7e2ff68ab34d4dee816d6e5f1292eda680d786be`、移动报告
  `1c3e10544a6ffe3f948f0bf075bc9fd344d3dbb976b38a301f7ce4bc85955d93`。
- Gateway `:8000`、Control `:8001`、Console `:5173` 最终健康检查均为 HTTP 200。

PRD-073 已达到可运行、可验收终态；Git 提交与远端同步在项目整理步骤完成后另行记录。
PRD-073 之后的下一条权威未完成项是
`PRD-074 | M2-企业增强 | P2 | 批处理调度 | 异步批处理 Scheduler 与 Worker`；总体目标保持
`all_108_prds_complete=false`。
