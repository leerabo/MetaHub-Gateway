# PRD-081 测试记录

日期：2026-08-16

结论：`PRD-081 - SIEM/安全事件格式` 的 Shared、0074、Control、Gateway、PAGE-113、Runtime、Browser、
隐私与恢复门禁均已通过。以下分组存在相邻覆盖，不能相加冒充一次根级全量；总体状态继续保持
`all_108_prds_complete=false`。

## 1. 范围与完成依据

- Excel `01_PRD总表` 第 82 行、`03_M2-M3扩展交付` 第 18 行、PAGE-113、API-370/371、OBJ-110、
  EVT-093 与 TC-0161/0162 共同冻结本条。
- 三类权威 source 为 DLP、会话风险与 Provider credential 异常；不存在内存日志队列替代持久事实。
- Endpoint format、实际 wire 与分钟上限冻结；Secret 在真实 attempt 前切换到当前版本。
- 权限由后端 `webhook.manage` 逐请求校验；tenant/actor 只从 Session 恢复。
- Provider Key、Token、Prompt、Response、Secret、签名、wire/payload 与接收端正文不进入日志、DOM 或证据。

## 2. 自动化门禁

| 分组 | 最终结果 | 覆盖 |
| --- | ---: | --- |
| Shared + Control 受影响回归 | `34/34 passed` | exact payload/CEF、0074、聚合、outbox、Webhook API/replay、RequestLog auth fact |
| Gateway 受影响回归 | `36/36 passed` | 基础/亲和 KeyPool、decrypt anomaly、会话风险 source 与相邻状态机 |
| Runtime/Browser Python 合同 | `9/9 passed` | 固定路径、空库 0074、证据 exact shape、owner/隐私与异常映射 |
| PAGE-113 Node Browser 合同 | `5/5 passed` | loopback 网络、双视口、RBAC、Windows PID/双流清理与禁止 escalation |
| Console 全量 | `822/822 passed` | Webhook UI `10` 个节点及全部相邻 Console 单元回归 |
| Console lint | `254 files` | Node 语法、静态 import/DOM 合同 |
| Python Ruff | 通过 | 11 个 PRD-081 新 Python 文件全规则；30 个变更 Python 文件新增行诊断为 0 |
| Git whitespace | 通过 | `git diff --check` |

第一次把全部 66 个受影响 Python 节点放在一个 20 分钟 wrapper 中运行时，因大量节点各自从空库应用
74 个迁移，wrapper 在已经输出 56 个通过节点后超时，没有测试失败。最终改为互不重叠的
Shared/Control `34`、Gateway `36` 和验收合同 `9` 三组，均得到明确 100% 终态；没有用超时轮替代通过记录。

## 3. 0074 与数据库

权威 Runtime：`.artifacts/prd081-runtime/final-20260816t/runtime-acceptance.json`。

| 阶段 | migration count/version | integrity | 外键违规 |
| --- | --- | --- | ---: |
| fresh | `74/0074` | `ok` | `0` |
| repeat | `74/0074`，applied 0 | `ok` | `0` |
| down | `73/0073` | `ok` | `0` |
| re-up | `74/0074` | `ok` | `0` |

数据库专项还验证：

- 历史 Endpoint/delivery 精确回填为 JSON，不历史回灌 0074 前安全 source。
- DLP、session risk、Provider auth/decrypt 四种 source 入口只生成三种标准 source type。
- 5 秒窗口、迟到 generation、成员反算、1000 source 上限、source 去重与 tenant owner 原子生效。
- Endpoint/delivery format、wire/rate 冻结，CEF replay 逐字复用；非法 owner、更新、删除和 down 均失败关闭。
- 最终 `nonterminal_outboxes=0`、`pending_deliveries=0`、lease=0、SQLite sidecar=0。

Runtime JSON SHA-256：
`a67a3d330056a14e92aec86a752964d5228fcc28d72ddbb0e83a81c116cb124d`。

## 4. Runtime 实际验收

`final-20260816t` 使用仓库内空 SQLite 和固定 loopback Provider/Gateway/Control/RESP/SIEM receiver：

- 三类真实 source 全部生成：DLP `5`、session risk `2`、provider credential `1`。
- 一个窗口聚合 3 条 source/3 次 occurrence；JSON delivery `8`、CEF delivery `6`，CEF 单行和 JSON exact
  wire 均通过，接收端 HMAC 全部有效。
- Endpoint 独立 `1/min` 限流在下一分钟恢复；延迟期间 attempt/network 为 0，重启后网络投递 2。
- Control 停服期间 pending 事实持久存在；新 Control 接管后 remaining pending/lease 均为 0。
- 8 条 RequestLog、1 条 Usage、1 条 FinOps charge、7 条失败请求、14 条 delivery/attempt 可追踪。
- 五个 owned loopback 进程与固定端口全部释放；10 个受控文件敏感明文命中 0。

格式化验收器后没有改写旧证据，而是重新运行并发布当前源码匹配的 `final-20260816t`。一次中间诊断轮在
Control 健康检查前进程退出，资源完整清理；下一轮没有复现并全量通过，因此该诊断轮不作为权威证据。

## 5. PAGE-113 Browser 实际验收

权威 Browser：`.artifacts/prd081-browser/final-20260816q/`。

- portable Chrome 实际操作 `1280x720` 与 `390x844`；创建 JSON `600/min`、CEF `1/min` 两个 Endpoint。
- 两次创建、两次测试均为 200，数据库两条 delivery 均 success，接收器 JSON/CEF 各 1，验签错误 0。
- CEF 模式即时收敛为 `security.event_pushed`；切回 JSON 不暗自扩大订阅；1..600 边界通过。
- Endpoint 与 delivery 同时显示冻结 format/rate；Secret、wire/payload、签名和 source member 不进入 DOM。
- viewer 后端 403、导航和页面 DOM 隐藏；两次 login/logout 都为 200，登出后缓存、Storage 与页面 owner 清空。
- 外部请求、禁止 query、Console error、Page error、横向/文字溢出和控件重叠均为 0。
- work/staging 共 11 个受控文件明文命中 0、SQLite sidecar 0；四个 fixed port 和 Chrome owner 全部释放。

四张截图已人工复核：

- `page-113-json-desktop-1280x720.png`
- `page-113-json-delivery-desktop-1280x720.png`
- `page-113-cef-mobile-390x844.png`
- `page-113-cef-delivery-mobile-390x844.png`

截图均非空、内容完整，长事件名只在所属列内换行，无按钮、标签或相邻内容重叠。Browser JSON 和 manifest
逐文件 bytes/SHA-256 全部匹配当前制品。

Browser JSON SHA-256：
`55f131ad4f79d438fdc81720189e77a99f4da5cbf8480204cea38eb9a7e320dc`。

Browser manifest SHA-256：
`ea72a95cfd92e0bbc79720001d771027ea394d476585a13a09ee023e72c93740`。

一次非权威轮在最后 viewer logout 收到瞬态非 200；finally 已证明端口、进程、work/staging/driver 全部清理。
后续 `final-20260816q` 完整重跑未复现，login/logout 精确为 `[200,200]`，因此只以 `final-20260816q` 为权威。

## 6. 安全与禁止文件门禁

- 生产日志只允许固定 event/error code、tenant、event/delivery ID、request/trace 和安全计数。
- 提交范围不含数据库、WAL/SHM/journal、日志、Browser profile/cache、失败诊断目录、Office 锁文件、真实
  `.env`、凭据、符号链接或超限生成物。
- 默认上游目录未改变；OpenRouter、硅基流动仍只是竞品与迁移兼容对象。
- 代码、文档、Excel、Runtime 与 Browser JSON 均保持 `all_108_prds_complete=false`。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 83 行、`03_M2-M3扩展交付` 第 19 行、PAGE-114、API-372..377、
McpServer/McpTool/McpInvocation、`mcp.tool_called` 与 TC-0163/0164 共同确认，PRD-081 之后的下一条权威
未完成项为：

`PRD-082 | M2-企业增强 | P2 | 扩展层 | MCP治理基础 | MCP工具注册与权限`

本次只登记，不实现 PRD-082。
