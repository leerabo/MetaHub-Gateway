# PRD-069 轻量敏感意图分类测试记录

状态：**已完成**。设计与两轮自审、实现、专项与分层回归、Node/Console 全量、唯一一次根级 Python
全量及失败节点定向闭环、三服务真实 API、开发库 0061、PAGE-096/PAGE-097 双视口、性能、权限、异常和
隐私验收均已通过。总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 已通过专项

| 验证项 | 当前结果 | 覆盖 |
| --- | ---: | --- |
| Shared 分类器/Webhook/RBAC | `21 passed` | taxonomy、NFKC、固定分数、exact payload、revision 6；性能优化后复跑 |
| 0061 迁移 | `6 passed` | up/down/往返、owner、不可变、降级门禁 |
| Gateway PRD-069 最终组合 | `43 passed` | `15 + 8 + 11 + 5 + 3 + 1`：强阻断、两类 503、异步队列、spool v1/v2/v3、TTFT、生命周期与优先级 |
| PRD-069 新增故障/性能门禁 | `4 passed` | 20k P95/P99、Webhook unavailable/full、shutdown timeout 与引用生命周期 |
| Control 分类联合专项 | `5 passed` | clean/detected、401/403、严格正文、Header 不泄漏、两类 503 |
| RequestLog 集成文件 | `7 passed` | 七状态、安全字段、跨 owner 完整性、历史功能回归 |
| Request Explanation v1 | `2 passed` | 原 shape 与 policy mask 投影不变 |
| Control 导航 | `2 passed` | 后端权限裁剪、非法页面与敏感事件拒绝 |
| Console PRD-069 直接专项 | `8 passed` | PAGE-096/097、权限、owner、迟到响应和七状态 |
| Console 相关组合 | `44 passed` | intent、RequestLog、权限、导航与 Shell 相邻合同 |
| Node 示例全量 | `33 passed` | OpenAI-compatible 示例与统一错误脱敏 |
| Console 全量 | `635 passed` | 当前控制台全部单元和合同测试 |
| Console lint | `196 files` | 通过 |
| 根级 Python 唯一全量 | `2146 passed / 4 failed` | `14514.48s`；失败均为 0061 引入后的历史测试合同漂移 |
| 根级失败 node 定向闭环 | `4 passed` | 仅修正 0060/60 写死值、0061 回滚顺序和 revision 6 权限排除集；未重跑全量 |
| Python Ruff | `All checks passed` | 分类器、新增 Gateway 测试及三个历史合同测试文件 |
| Python compileall | `passed` | Shared、Control、Gateway 和示例源码可编译 |
| 中文注释门禁 | `comment coverage ok` | 未修改检查器 |

Shared、Gateway 和 Control 的专项文件使用唯一模块名，默认 pytest 根级收集无导入冲突。全程使用
`TEMP/TMP=D:/MetaHub/.tmp`，未使用 `C:/tmp`、未提权、未删除测试证据。

## 2. 主流程与安全边界

- 本地分类器稳定识别凭据索取、系统提示提取、策略绕过和 jailbreak；普通安全文本为 none/0。
- Gateway 顺序固定为 DLP -> Intent -> Rate Limit -> Budget -> Provider。强命中只在 result/event
  同事务提交后返回 blocked；分类与存储异常使用不同错误码并失败关闭。
- 异步轻分类只 `put_nowait`，queue full/worker unavailable 失败开放并记录 skipped；worker 结果不改变
  已返回 HTTP/SSE。job 为 frozen 且正文 `repr=False`。
- Usage crash spool writer 只写 v3；reader 分别用 v1/v2/v3 exact field set，旧版本映射 disabled。
- Control 在正文解析前执行 Session/RBAC；外部 request/trace Header 被忽略，结果/event/Webhook/日志均不含正文。
- RequestLog 精确区分 disabled/pending/clean/detected/blocked/failed/skipped，并按 tenant/result/source 三项关联。
- PAGE-096 提交即清空 textarea，离页、取消、401/403、owner 漂移和 pagehide 会 abort/销毁；无权限角色无
  导航、工作台 DOM 或 classify 预取。PAGE-097 只提供显式有界刷新，不后台轮询。

### 2.1 性能与 shutdown 事实

- 机器：`RABOSOHO`，Windows 11 `10.0.28020`，ARMv8 Qualcomm CPU、8 logical CPUs，Python `3.12.13`
  （进程报告 `AMD64`）。每种输入/分类器先预热 10 次，再独立采样 100 次，采用 nearest-rank P95/P99。
- 最终 20,000 code points 指标（毫秒）：中文 strong `6.7792/8.5548`、light `8.1162/11.4910`；英文
  strong `2.9632/3.0914`、light `3.5286/5.5533`；分隔符 strong `3.3888/4.0225`、light
  `3.3762/5.2648`。全部满足 P95 <= 10ms、P99 <= 25ms。
- 初始门禁真实暴露中文 P95 超标，没有降低阈值。实现仅增加 NFKC 已规范化和纯字母数字快路径，并删除
  被同一 `+` 分隔符表达式覆盖的冗余第二遍空格扫描；NFKC/大小写/分隔符专项 `21 passed`。
- shutdown timeout 测试区分 Python 真实边界：pending job 立即清空；已进入 `to_thread` 的 current job
  不能被强杀，只保留至底层函数返回。返回后正文引用释放，被取消 worker 不会向已经停止的 Webhook
  dispatcher 补投事件。
- 性能 JUnit：`D:/MetaHub/.tmp/prd069-classifier-performance-r4.xml`；最终测试进程零 warning。

## 3. 开发库迁移证据

- 迁移前：`60/0060`，`integrity_check=ok`，外键违规 0。
- 实际迁移 run：`dbm_f8812e6b75a94d749f2dc30a3fc5d5ad`，仅应用 `0061`。
- 迁移后：`61/0061`，`intent_risk_results=22` 列、`intent_risk_events=19` 列、RequestLog 新增 9 列。
- 三个现有 tenant 各 57 项 Permission，所有 Permission 为 catalog revision 6；
  `integrity_check=ok`，`foreign_key_check=[]`。

## 4. Control 真实 API

- clean：HTTP 200，`request_id=req_bbea55ea8e83432894af8b5caeb865f8`，结果为
  `clean/none/0/allow/no_risk_detected`，响应头与正文 request/trace ID 一致，`Cache-Control: no-store`。
- detected：HTTP 200，`request_id=req_c65a8abbb1cf413abea0e07735a088d4`，结果为
  `detected/system_prompt_extraction/9800/review/explicit_system_prompt_request`；Result 与
  `security.intent_detected` source event 的 tenant、actor、request、trace、模式和版本一致。
- 匿名请求：HTTP 401 `auth.token_missing`，`request_id=req_5358fa0ab54d4ef2abca57df6d41d53e`。
- Viewer：HTTP 403 `auth.permission_denied`，`request_id=req_366dc87a8a634ee6a553164670894612`。
  401/403 均为 `no-store`，没有新增 Result/Event，也没有先读取分类正文。
- 本项只证明 source event 已持久化并交给既有 dispatcher；开发环境没有可用于声明成功的外部接收端，
  因此不把 source event 提交写成真实 Webhook delivery 成功。

## 5. Gateway 与 RequestLog 真实运行

- 强规则请求：HTTP 403 `gateway.intent_blocked`，
  `request_id=req_98cf4c2da3ea468da81b2d65299bbac3`。结果为
  `blocked/sync_strong/credential_exfiltration/9900/block`；Provider、RoutingAttempt、Usage、Token 和成本均为 0。
- clean 请求：HTTP 200，`request_id=req_b34f2ef5b3aa4c399dd7c350cb0cc055`。异步结果为
  `clean/async_light/none/0/allow`，Provider 正常调用一次。
- detected 请求：HTTP 200，`request_id=req_1112c6c57633470fa09bb6b6e0789d30`。异步结果为
  `detected/async_light/credential_exfiltration/7400/observe`；成功响应未被改写为 blocked，Provider 正常调用一次。
- 三条详情都能由当前 tenant 的 `request_log.read` 读取；blocked 的 RequestLog 冻结同步摘要，异步两条通过
  tenant/result/source 三项关联读取不可变结果。Mock Provider 共收到 2 次调用，验收后 Endpoint 已恢复且 mock 已停止。

## 6. 应用内 Browser

- PAGE-096 在 `1280x720` 与 `390x844` 实际提交 clean/detected；textarea 提交后立即清空，清空结果、
  离页和注销后分类结果及输入 DOM 均为 0。Viewer 没有分类导航、输入框或 classify 预取。
- PAGE-097 实际打开上述 clean、detected、blocked 三条详情，分别显示 `0/10000`、`7400/10000`、
  `9900/10000`；detected 明确为“旁路观察，模型请求未因此被阻断”，blocked 明确为“Provider 前已阻断”。
- 移动端 `390x844` 的文档 `scrollWidth=clientWidth=375`，语义风险区间位于可视宽度内；桌面
  `1280x720` 的文档 `scrollWidth=clientWidth=1265`，详情区无越界。页面 Console error 为 0。
- 最终已注销回到“MetaHub 控制台登录”，风险详情和分类工作台不再存在；Console 标签页保留供继续验收。
- Console dev server 一度因标签页被误关后的本地进程状态停止监听；在沙箱内使用仓库
  `console-web/scripts/dev-server.mjs` 恢复后，现有 Browser 标签重新显示了非空登录表单。该恢复未重跑已通过的
  PAGE-096/PAGE-097 双视口业务验收。

## 7. 终态审计

- Gateway、Control API、Console 最终独立健康检查均为 HTTP 200。
- 开发库当前为 `61/0061`，`PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 返回 0 行；
  当前 `intent_risk_results=10`、`intent_risk_events=5`，三条 Gateway 证据的状态矩阵与 RequestLog 一致。
- `intent_risk_results`、`intent_risk_events` 和 RequestLog 新字段只保存枚举、整数、版本、owner 与链路 ID，
  没有 Prompt/Response/正文/hash 列；运行表和 `.logs` 的本次敏感短语及本地管理员密码扫描命中 0。
- Browser Viewer、Session、Preference/Event 等临时验收事实已按明确 ID 清理或注销；没有删除业务结果、
  source event、调用日志或 pytest 证据。

## 8. 根级全量与定向闭环

- 唯一一次根级 Python 全量使用会话 `10142`，`TEMP/TMP` 与 `--basetemp` 均限定在
  `D:/MetaHub/.tmp/prd069-full-python`。终态：`2146 passed / 4 failed in 14514.48s (4:01:54)`。
- 四个失败精确为 PRD-054 空库迁移审计计数、PRD-059 权限矩阵回滚、PRD-059 fresh 历史矩阵和 0049
  roundtrip 历史矩阵。根因分别是写死 `60/0060`，以及 PRD-059 的后续权限排除集缺少
  `intent_risk.classify`，不是 PRD-069 业务链路失败。
- 修正后只运行上述四个原 node ID，结果 `4/4 passed`。PRD-054 计数改为从正式 up SQL 动态推导，避免
  下一条迁移再次制造同类漂移；PRD-059 按 `0061 -> 0060 -> 0059 guard` 真实回滚，并冻结 revision 6
  后续权限。没有停止、重启或执行第二次根级全量。
- 最终新增四个 PRD-069 节点、Shared 兼容、Ruff、compileall、中文注释、三服务、数据库、日志和 Browser
  复核均通过，PRD-069 可以标记完成。总体目标仍为 `all_108_prds_complete=false`。
