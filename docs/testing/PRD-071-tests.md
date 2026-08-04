# PRD-071 输出侧安全检测与流式滑窗测试记录

状态：**已完成（2026-08-04）**。设计、两轮编码前自审、实现、0063 迁移、分层回归、正式 Runtime、
账单与事件 owner、后端权限、应用内 Browser 双视口、隐私门禁、资源清理和文档均已通过。该结论只表示
PRD-071 完成；总体目标仍为 `all_108_prds_complete=false`。

## 1. 已记录的阶段性自动化结果

以下数字均来自本轮实际执行并明确取得退出码的命令。前端 `59 passed` 是局部详情修订前的相邻组合证据；
最终修订后的权威直接结果是 PAGE-099/100 `9 passed`，没有把不同代码时点拼成一次全量通过。

| 验证项 | 已记录结果 | 当前覆盖 |
| --- | ---: | --- |
| Shared 输出安全 | `30 passed` | `26` 项全文/滑窗扫描、SSE framer、strict JSON、Webhook 安全合同，加 `4` 项非有限 JSON 数值拒绝 |
| Gateway Chat 热路径 | `11 passed` | 非流式 clean/block、流式前缀/阻断、Usage 与统一错误优先级 |
| Gateway StreamSession | `7 passed` | 输出安全终态、幂等、原子终结、握手失败 `not_applicable` 与异常回滚 |
| Gateway 输出安全持久化 | `6 passed` | pending/终态、owner、event、不可变状态机 |
| Gateway Usage spool | `6 passed` | 条件式 v5、v1-v5 读取、dispute 幂等与未知版本拒绝 |
| Shared/Gateway 最新直接组合 | `60 passed`，退出码 `0` | 上述六个 PRD-071 文件在同一命令中完整复跑 |
| 0063 迁移专项 | `9 passed`，退出码 `0` | up/down、0062→0063→0062→0063、rowid/schema/index/trigger、RBAC revision 7、降级 guard |
| Runtime 验收器合同 | `7 passed`，退出码 `0` | 工作区路径、回环 Origin、流式构造、owner、一次性 Provider Key、敏感字段与证据写入合同 |
| Control 导航 | `4 passed` | `request_log.read` 导航裁剪、PAGE-099 入口与拒绝路径 |
| Control 输出安全读模型 | `4 passed in 41.28s` | 四筛选 legacy/inline、PAGE-099/100、FinOps 最小投影、权限、跨租户 404、owner 污染 503 |
| Control 相邻核心回归 | `47 passed`，退出码 `0` | RequestLog、FinOps 与 Query Core 相邻合同 |
| Console PRD-071 最新相关组合 | `59 passed` | outputSafety、RequestLog、DLP、FinOps 三文件与共享 RequestFeedback owner；零失败 |
| PAGE-099/100 移动布局专项 | `9 passed`；lint `203 files passed` | 长 StreamSession ID、隐式 Grid 轨道、390px 收缩与无敏感正文合同 |
| 开发库只读核对 | `63/0063` | `integrity_check=ok`，`foreign_key_check` 为 `0` 行 |
| 正式 Runtime | `status=passed`，退出码 `0` | clean、非流阻断、扫描不可用、跨 chunk 流阻断、权限、owner、清理和隐私 |
| 应用内 Browser | `status=passed` | 1280x720、390x844、Viewer 前后端权限、零 DOM 正文/pattern、零页面错误 |
| Python 静态门禁 | `py_compile` 退出码 `0`；`comment coverage ok` | 最终验收器修订及全仓中文注释基线 |

Control 读模型命令为：

```powershell
python -m pytest control-plane-api/tests/integration/test_prd071_control_read_models.py `
  -p no:cacheprovider --basetemp D:/MetaHub/.tmp/prd071-control-focused-final
```

Console 最新组合命令为：

```powershell
node --test tests/unit/outputSafety.test.mjs tests/unit/requestLogs.test.mjs `
  tests/unit/dlp.test.mjs tests/unit/finops.test.mjs tests/unit/finopsIntegration.test.mjs `
  tests/unit/finopsPageState.test.mjs tests/unit/requestFeedback.test.mjs
```

执行时 `TEMP/TMP=D:/MetaHub/.tmp`。

另有 DLP `scan_scope` 的 Gateway 聚焦用例已在开发过程中通过，但交接记录没有保留可核对的独立计数，
因此本文不合并或猜写数字。推荐 Python 环境未安装 Ruff 模块，实际结果为 `No module named ruff`；这不记作
代码通过或失败，也没有改用沙箱外工具。最终语法、中文注释、相关 Python/Node/lint、正式 Runtime 与 Browser
结果共同构成本条收口证据。

所有 pytest 临时目录必须设置在 `D:/MetaHub/.tmp`，不使用 `C:/tmp`、不提权、不删除已有测试证据。已存在的
`D:/MetaHub/.tmp/prd071-*` 目录只能作为某次运行发生过的线索，不能脱离进程退出码和明确汇总冒充通过证据。

## 2. 已实现并由阶段专项覆盖的核心合同

### 2.1 Shared 扫描与 SSE

- 非流式只投影 OpenAI-compatible Chat 的已知文本字段，拒绝损坏 shape 和全文上限越界。
- keyword/安全 regex 复用冻结规则顺序，block 优先；决策对象、异常和 `repr` 不含正文或 pattern。
- 流式按 `interval_tokens` 到达检查点，按 `window_tokens` 裁剪有限窗口；同 lane 可跨 event 命中，跨 choice、
  content/refusal/text 和 tool call 不拼接。
- framer 覆盖 LF/CRLF、多 data 行、注释、跨 chunk event、UTF-8 增量解码、duplicate JSON key、事件/行上限、
  EOF 残缺和 `[DONE]` 终止；已通过 event 保持原始 bytes。
- `observed/scanned/emitted` 三类计数独立，流式命中示例已经固定为 `20/16/8`，不能把命中 event 计为已输出。

### 2.2 Gateway 主流程与异常

- Provider 前提交 tenant/request-bound pending scan，并只加载当前 tenant 的 active `output/both` 规则。
- 非流式 clean/matched 在证据提交后返回；block 返回 HTTP 403 `content_policy_violation` 且不返回正文。
- 流式命中 event 不发送，关闭上游，只追加一个标准 SSE error，不发送 `[DONE]`；更早已通过 event 不承诺撤回。
- 扫描 block 或 post-provider unavailable 仍按真实/估算 Usage 入账；只有 blocked 形成
  `dispute_pending`。Usage durability 为 `lost` 时，客户端收到更高优先级
  `finops.usage_record_unavailable`，不放行正文且不派发 Webhook。
- `[DONE]` 后立即停止解析，同批与后续垃圾数据不进入扫描或 Usage 正文投影。
- 输出安全阻断不是 Provider 质量错误，不触发 Fallback；Cache Affinity Selection 按 Provider 成功收口。

### 2.3 数据与恢复

- 0063 增加 `dlp_rules.scan_scope`、`output_safety_scans`、`output_safety_events`、
  `finops_ledger_disputes`，并扩展 StreamSession/RequestLog 精确终态。
- scan/event/dispute 使用 tenant-qualified owner、合法状态机、单次终结和 append-only trigger；正文、pattern、
  Key、Token 和异常原文没有持久字段。
- 只有存在 OutputSafetyScan 的 Chat Usage 写 spool v5；无 scan 的 Embeddings/前置失败继续写 v4，reader
  精确接受 v1-v5 并拒绝 v6。
- `security.output_blocked` source event 只为可靠提交的 blocked scan 形成；dispatcher 失败不回滚安全事实，
  Usage 主库与 spool 双失败时不派发。

### 2.4 Control、权限与 Console

- PAGE-099 复用 `GET /request-logs?terminal_stage=output_safety` 和既有 `request_log.read`，不新增第二套文本扫描 API。
- PAGE-100 只返回 scan ID、枚举、规则引用、版本、非负计数、暴露和最小 dispute；FinOps 只返回 scan ID、
  `dispute_pending` 与 `reversal_eligible`。
- Admin/SecurityAdmin/Developer/OpsAdmin 按实时权限读取；Viewer/FinanceAdmin 不获得 RequestLog 读取。FinanceAdmin
  仅能经 FinOps 权限看到最小争议徽标。
- tenant 只从 Virtual Key 或 Console Session 恢复；跨 tenant RequestLog 与不存在同形 404，读模型 owner 损坏
  失败关闭为 `output_safety.integrity_failed`。
- PAGE-099 强制 `terminal_stage=output_safety`，表单和 SavedFilter 都不能移除；页面提供状态、模式、暴露、
  账单四类输出安全筛选，详情不显示输出正文或 DLP pattern。

## 3. 最终验收闭环

| 验收项 | 最终事实 |
| --- | --- |
| 三服务与真实主流程 | Gateway/Control/Console 健康均为 HTTP 200；clean 200、非流阻断 403、扫描不可用 503、流式唯一 SSE error |
| 统一错误与权限 | 匿名 401、Viewer 403、跨租户/不存在同形 404 均返回固定错误码和 request_id；Viewer 后端直调再次为 403 |
| 账单与事件 | 四个正式场景的 RequestLog、scan、Usage、charge、dispute、source event 和 StreamSession 数量及 tenant/request/trace owner 精确 |
| 流式安全 | 安全前缀可见；命中 event、规则 marker 与 `[DONE]` 不可见；只返回一个 `content_policy_violation` error event |
| 数据库 | `63/0063`、`integrity_check=ok`、外键违规 `0` |
| Browser | 1280x720 列表/详情无覆盖；390x844 文档宽度 `375/375`、StreamSession `320/320`、溢出元素 `0` |
| 前端权限与隐私 | Viewer 导航、PAGE-099/100、RequestLog DOM、QueryEvent 均为 `0`；Prompt/Response 正文和 DLP pattern DOM 命中 `0` |
| 清理 | 本批 Provider Key、Virtual Key、DLP 规则和 Session 均禁用/撤销；一次性 Viewer disabled 且 active Session 为 `0` |
| 制品隐私 | 正式 Runtime 对数据库、Gateway 日志、Control 日志和证据预写内容共 4 类扫描，敏感值命中 `0` |

迁移交叉审查发现的旧 `24` 上限已经闭环：当前 0063 up migration 与 roundtrip 断言均按完整 QueryEvent
白名单上限 `28` 校验，迁移专项实际收集 `9` 个节点并以退出码 `0` 结束。该事实只证明 0063 专项通过，
不替代正式 Runtime 和受影响分层回归。

本轮遵照用户要求没有因局部验收器和 CSS 修订重复执行数小时无关根级全量；以 Shared/Gateway `60`、
Control 相邻 `47`、迁移 `9`、Console 相关 `59`、最终 PAGE-099/100 `9` 和验收器合同 `7` 的受影响分层
结果收口。所有 pytest 临时目录均位于 `D:/MetaHub/.tmp`，没有访问沙箱外临时目录或申请提权。

## 4. 最终运行命令约束

PowerShell 会话统一使用：

```powershell
$env:TEMP = (Resolve-Path 'D:/MetaHub/.tmp').Path
$env:TMP = $env:TEMP
$env:PYTHONPATH = 'shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src'
$python = 'C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
```

每个 pytest 命令的 `--basetemp` 必须使用唯一的 `D:/MetaHub/.tmp/prd071-*` 子目录，并使用
`-p no:cacheprovider`，避免复用旧 `.tmp/pytest-of-rabol`。修复失败后只复跑失败 node 与直接受影响集合；
最终验收阶段再执行约定的分层回归，不因一个局部修订重复跑数小时无关测试。

## 5. 正式制品与失败闭环

- [Runtime 证据](../../.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/runtime-acceptance.json)：
  `status=passed`，SHA-256
  `58592d4a0a9789ecc56273a8e16725ffebea513ab7cce79f31d911659fa2a7ff`。
- [Browser 证据](../../.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/browser-acceptance.json)：
  `status=passed`，SHA-256
  `832fe5914a9051447b25f9608cea71e92fd69b08bb370678346043e04f1d3db7`。
- [哈希清单](../../.artifacts/prd071-runtime/prd071_20260804T120418Z_4a4caa64/SHA256SUMS.txt) 固定两份正式证据。

正式验收器在收口阶段暴露并定向修复了三个夹具问题，失败记录没有删除：

1. 首轮 `gateway.clean:status_mismatch`：验收器没有通过 Control API 创建当前进程可解密的 Provider Key；
   增加一次性 Key 创建、明文回显拒绝、精确 ID 禁用和失败分支清理。
2. 次轮 `gateway.stream:marker_exposed`：夹具把 marker 放在最后一个 Token 检查点之后，却错误要求命中 event
   未发送；调整为 marker 后补足一个检查间隔，尾窗已发送不撤回合同仍由既有异常用例覆盖。
3. 第三轮 `provider_key.create:status_mismatch`：固定 Key 指纹无法安全复跑；改为每个 run 随机 Provider Key，
   只保存在 `RunSecrets` 与假上游内存。

## 6. 最终结论

PRD-071 的设计、两轮自审、实现、迁移、相关分层测试、后端租户/RBAC/统一错误、正式 Runtime、账单事件
owner、Browser 双视口、中文注释、隐私门禁和资源清理均已完成，可标记为已完成。该结论只适用于
PRD-071；全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。
