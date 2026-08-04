# PRD-067 企业 Prompt 模板库测试记录

状态：**已完成**。设计、两轮自审、实现、迁移、专项与必要受影响回归、真实 Runtime、
PAGE-094/PAGE-093 应用内 Browser 双视口、资源清理和中文注释门禁均已通过。
该状态只代表 PRD-067 完成，`all_108_prds_complete=false`。

## 1. 验收范围

- 模板创建、列表、详情、加密定义读取、不可变 v1/v2、元数据 CAS、启用/禁用。
- 变量解析与有界渲染、适用 Project、推荐 ModelAlias、版本冲突和禁用阻断。
- `prompt_template.read/write/use` 后端实时权限、跨租户同形 404 和畸形正文先鉴权。
- 企业 Chat 经 Control/Gateway 双重复核后调用真实本地 OpenAI-compatible Provider。
- Invocation、RequestLog、Usage、FinOps 成本、Routing、AuditLog、source event 同链路追踪。
- 0059 up/down、历史对象保真、复合 tenant owner、数据库完整性和敏感明文零落库。
- Console PAGE-094/PAGE-093 的导航、状态机、敏感 DOM 生命周期及桌面/移动端布局。

## 2. 自动化结果

| 验证项 | 结果 | 说明 |
| --- | ---: | --- |
| 0059 迁移专项 | `11 passed` | fresh、0058->0059、down/up、保真、阻断与完整性 |
| Python 受影响组合 | `73 passed` | PromptTemplate、Chat、Gateway、RBAC、Audit/Webhook 直接影响面 |
| Control 企业 Chat 专项 | `3 passed` | 成功、变量/禁用/撤权、stale version、Gateway 失败终态 |
| 有界正文专项 | `8 passed` | 超限正文、变量与解析边界 |
| Console 直接相关 | `38 passed` | PAGE-093/PAGE-094、导航、权限、异步 owner 与敏感 DOM |
| 移动工具栏修复定向 | `10 passed` | 390px 新建/刷新按钮布局修复后的 PAGE-094 直接回归 |
| Console lint | `188 files` | 通过 |
| 中文注释门禁 | `comment coverage ok` | 使用仓库规定 Python 执行 `scripts/ci/check-comments.py` |

局部缺陷只运行对应节点复测；专项全绿后执行了一次必要受影响组合，没有因单点失败反复重跑全量。
pytest 的 `TEMP`、`TMP` 和 basetemp 均限定在 `D:/MetaHub/.tmp`，未使用 `C:/tmp` 或提权路径。

## 3. 主流程、异常与权限

### 3.1 主流程

1. Admin 通过真实 Control API 创建 selected-Project 模板 v1；列表、详情和定义读取均返回统一
   `request_id/trace_id`，敏感响应带 `Cache-Control: no-store`。
2. 发布 v2 后，v1/v2 历史均可追溯，current head 和 metadata revision 单调递增。
3. 模板禁用后 Chat 返回 `409 prompt_template.disabled`；重新启用后可恢复调用。
4. Viewer 创建 ChatSession，选择模板、提交变量并完成两次真实 Provider 调用；第二次调用发生在
   `prompt_template.use` 撤销并恢复之后。
5. 两个 Gateway request_id 均关联成功 RequestLog、18 tokens 的非估算 Usage、正成本 FinOps
   ledger、成功 Routing attempt 和成功 Invocation。

### 3.2 异常流程

- 缺少必填变量：`422 prompt_template.variables_invalid`。
- 旧版本调用：`409 prompt_template.version_conflict`，且创建执行事实前失败。
- Project 不适用：`409 prompt_template.not_applicable`。
- stale metadata revision：`409 prompt_template.revision_conflict`。
- 禁用模板：`409 prompt_template.disabled`。
- Gateway 故障专项证明 Message、Grant、Invocation 同步进入失败/撤销终态，模板正文和变量不落库。

### 3.3 权限与租户

- Viewer 对管理读写分别得到后端 `403 auth.permission_denied`；畸形 JSON 也必须先鉴权。
- 第二租户可创建自身模板；默认租户读取该 ID 与读取随机不存在 ID 均返回同形
  `404 prompt_template.not_found`。
- 撤销 Viewer 的 `prompt_template.use` 后，`/chat/options` 模板投影为空，发送返回后端 403，
  Message/Grant/Invocation 计数保持不变；恢复权限后同一会话可再次成功调用。
- runtime 退出时三个测试 Session 均通过 `/auth/logout` 注销，专用测试身份 active Session 为 0。

## 4. 数据库迁移与完整性

- 迁移前备份：
  `D:/MetaHub/.data/metahub-dev-prd031.prd067-pre0059-20260802T1756.db`。
- 开发库已应用 59 条迁移，最高版本 `0059`。
- 最终 `PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 违规数为 0。
- Prompt 定义只以 owner-bound AES-GCM 密文保存；Version、Project/Model binding、Invocation、Operation、
  source event 均使用 tenant-qualified owner 关系。
- 当前 Runtime 批次产生 7 条模板 AuditLog：2 条 created、2 条 updated、3 条 version_published；
  3 条 `prompt_template.published` event 全部为 `dispatched`，并与发布审计精确关联。

## 5. 真实 Runtime 验收

权威证据：
`D:/MetaHub/.artifacts/prd067-runtime/run-20260802T110138Z-c00b97cac3/runtime-evidence.json`。

- 状态：`passed`。
- SHA-256：`AFE1FDF4710AB35DB7E82F0C967E94E482E2A4FC21B3B4270038C3BFE4F1D49A`。
- Gateway、Control、Console 健康检查：`200/200/200`。
- 两次真实 Provider 调用成功；每次均记录 18 tokens、`0.000023 CNY` 和一条 posted FinOps entry。
- 本轮一次性 Provider Key 经正式 Control API 创建和禁用，各有一条审计；所有历史
  `PRD067 Runtime` 测试 Key 的 active 数最终为 0。
- 数据库/WAL 扫描 `9,134,080` bytes，6 份服务日志扫描 `795,478` bytes；动态 Prompt、变量、
  Response、口令和 Provider Key 明文命中均为 0。
- Windows 运行中的 SQLite `-shm` 只保存 WAL 索引/锁元数据且被独占锁定；实时扫描明确排除该
  非业务页文件，主库和 checkpoint 后的零长度 WAL 已逐字节扫描。最终运行终态再次确认
  `integrity_check=ok`、外键违规 0，产品三服务保持可验收状态。

首次 Runtime 诊断曾因没有可用测试 Provider Key 得到 `provider_key.unavailable`。验收器随后改为
通过正式后端 API 创建一次性 Key，并以 `ExitStack` 在任何返回/异常路径先禁用 Key、再注销三个
Session。该问题属于验收环境凭据生命周期，不是 PromptTemplate 或 Chat 业务回归。

## 6. Browser 验收

状态：**应用内 Browser 验收通过**。本轮直接连接已有 `localhost:5173` 产品服务，没有启动外部
Edge/Chrome/CDP，也没有采用外部浏览器批次作为权威证据。

1. PAGE-094 已验证列表、详情、当前定义、v1/v2 历史、新建弹窗、元数据入口、发布入口和列表/详情切换。
2. PAGE-093 已验证 1 个可用 Project、3 个 ModelAlias、14 个适用模板、模板选择、变量表单和
   ChatSession 创建；不可用 Provider 路径返回 `chat.gateway_unavailable + request_id`。
3. 两次真实模板 Chat 的成功、每次 18 tokens、成本、RequestLog、Invocation、Usage、Routing 和
   FinOps 事实由第 5 节权威 Runtime 直接证明；Browser 不用第二套外部驱动重复制造调用事实。
4. `1280x720` 下列表与详情双栏正常，document/scroll width 均为 1265，无横向溢出。
5. `390x844` 下刷新按钮固定 `40x40`，新建按钮为 `286x38` 且同行；25 个列表项横向裁切数为 0，
   详情视图返回、元数据、发布、版本 Tab 和定义区域均可见，无页面横向溢出。
6. 本轮 Browser error/warning 数为 0；截图不含 Provider Key、Token、密码、Prompt 正文或 Response。
   验收结束后通过正式退出撤销新 Session，并精确撤销已关闭旧页面遗留的 1 条 Session。

最终应用内 Browser 截图：

| 视口/状态 | 文件 | SHA-256 |
| --- | --- | --- |
| 1280x720 桌面详情 | `.artifacts/prd067-browser/prompt-templates-1280x720.png` | `D593B5EE648FF6B28710079B3410F64FE34E8B718961D9A3B0E3294772EC427A` |
| 390x844 移动详情 | `.artifacts/prd067-browser/prompt-templates-detail-390x844.png` | `3514737BEC7051800800C832CB14F699C058577B5424FE2FC3696EF63025EDA1` |
| 390x844 移动列表 | `.artifacts/prd067-browser/prompt-templates-list-390x844.png` | `1AC9ABA7FE5B74B36CE93632BF1C3B42AC01B5CD09F94BA4434A276CE8E64E18` |

## 7. 最终资源与运行终态

- 0059 幂等迁移再次执行：`applied=[]`，`0001..0059` 全部 `skipped`。
- 开发库：59 条迁移、最高 `0059`、`integrity_check=ok`、外键违规 0。
- 本轮 active 管理员验收 Session、active ChatSession、issued Chat grant 均为 0。
- `PRD067 Browser*` 与 `PRD067 Runtime*` 命名的一次性 Provider Key active 数均为 0。
- 外部验收端口 `5174/19067/19069` 均未监听；Gateway、Control、Console 为 HTTP `200/200/200`。
- 外部浏览器诊断产生的 11 个 `run-*` 批次已删除，只保留上表三张应用内 Browser 截图。

## 8. 当前结论

PRD-067 的设计、两轮自审、实现、0059、后端权限与 tenant 隔离、统一错误和 request/trace ID、
主流程/异常/权限测试、必要回归、真实 Provider、治理账本、双视口页面、敏感信息清理、文档和
中文注释门禁均已通过，可以标记为完成。总体 108 条目标仍在进行；Excel 总表、M2/M3 交付表和
验收清单交叉确认的下一项是 `PRD-068 - 行业/场景策略模板库`，不能据此声称全部完成。
