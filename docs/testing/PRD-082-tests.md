# PRD-082 MCP 工具注册与权限测试记录

日期：2026-08-16

结论：`PRD-082 - MCP 工具注册与权限` 的 Shared、0075、Control MCP governance、审批/outbox、PAGE-114、
Runtime、Browser、迁移往返、隐私与恢复门禁均已形成正式通过证据。生产 executor 继续固定 `disabled`；
实际 MCP transport 只在 test profile 使用 literal-loopback 官方 SDK adapter。本记录不启动 PRD-083，
总体状态继续保持 `all_108_prds_complete=false`。

## 1. 范围与完成依据

- Excel `01_PRD总表` 第 83 行、`03_M2-M3扩展交付` 第 19 行、PAGE-114、API-372..377、
  OBJ-111..113、EVT-094 与 TC-0163/0164 共同冻结本条。
- RBAC catalog revision 17 固定 `mcp.read`、`mcp.server.manage`、`mcp.policy.manage`、`mcp.invoke`、
  `mcp.approve`；权限与 tenant owner 由后端逐请求恢复和校验，前端隐藏入口不构成授权。
- Server 注册/刷新/停用/启用、Tool Schema/policy、异步 Invocation、参数 DLP、返回脱敏、非自审高危审批、
  失败/结果未知、Audit/event/outbox 和 scheduler 恢复均在范围内。
- Provider Key、Bearer Token、Endpoint、原始参数/结果、DLP 命中片段和第三方错误正文不得进入日志、DOM、
  截图、JSON、manifest 或提交证据。
- PRD-083 的沙箱子网、网络 ACL、DNS/IP 重绑定防护、CIDR 白名单和熔断不在本条；生产第三方 MCP 出网
  仍不可用，不能用 loopback 验收冒充。

## 2. 自动化门禁

| 分组 | 最终结果 | 覆盖 |
| --- | ---: | --- |
| PRD-082 Python 专项合集 | `146/146 passed` | Shared、迁移、Control API、Scheduler、Outbox、executor 与两套 acceptance 合同 |
| MCP Scheduler 专项 | `30/30 passed` | tenant 公平 lease、审批、DLP、四次 probe、调用状态、迟到 owner、事件/outbox 与恢复 |
| Runtime contracts | `36/36 passed` | 固定路径、空库 0075 往返、正式 JSON exact shape、原子发布、provenance、隐私与清理 |
| Browser harness contracts | `8/8 passed` | owner marker、严格 facts、动态 helper provenance 与失败清理 |
| Console 全量 / Browser driver | `846/846`、`6/6 passed` | PAGE-114 owner/Abort 生命周期、响应体取消、真实 Browser 驱动合同及全部相邻 Console 回归 |
| Python Ruff | 通过 | PRD-082 新增/受影响 Python 实现与验收器 |
| Python compile | 通过 | PRD-082 Python 模块和验收脚本语法 |
| Runtime final | `status=passed` | 真 Control、官方 SDK loopback MCP、迁移、治理、失败/成本追踪与零残留 |
| Browser final | `status=passed` | PAGE-114 双视口、三 tab、Server 操作、Schema/注册 Dialog、网络、布局和隐私 |

以上分组存在相邻覆盖，不能相加冒充一次根级全量。正式 Browser JSON 已直接冻结撤权和 viewer 断言：
会话过期时权限矩阵失败关闭，动态撤权后 PAGE-114、导航和 root 状态清除并回到概览；viewer 无入口，
直接访问 `#mcp-tools` 仍只渲染概览。

## 3. 0075 与数据库

权威 Runtime：`.artifacts/prd082-runtime/prd082-final-runtime-7/runtime-acceptance.json`。

| 阶段 | migration count/version | integrity | 外键违规 |
| --- | --- | --- | ---: |
| fresh | `75/0075` | `ok` | `0` |
| repeat | `75/0075` | `ok` | `0` |
| down | `74/0074` | `ok` | `0` |
| re-up | `75/0075` | `ok` | `0` |

数据库和 provenance 还证明：

- up/down migration 均纳入当前源码 SHA-256，down 不是未受控的旁路文件。
- 最终 active lease、nonterminal row、SQLite sidecar 和外键违规均为 0。
- Approval/Invocation tenant 绑定、Server/Tool owner、append-only Attempt/Event、operation/lease CAS 和完整
  down guard 由 0075 与专项共同覆盖。

Runtime JSON SHA-256：
`15457991d5e1fb5c1b1acc55530d53acd68e74f9fb53bc5a7c3f3c0633b21c21`。

## 4. Runtime 实际验收

`prd082-final-runtime-7` 从空 SQLite 启动真实 Control、test-only literal-loopback Streamable HTTP MCP 和
受控辅助服务：

- 官方 SDK initialize、probe、`tools/list`、成功/失败 `tools/call` 均完成，请求/响应 bytes 可追踪。
- 生产 executor 零网络；测试 loopback 不改变 PRD-083 前的生产禁用边界。
- clean success、log-only、input DLP zero-call block、output block、返回脱敏、高危非自审批准、step-up、
  known failure 与 outcome unknown 均形成独立事实。
- 最终 Invocation `7`、Attempt `12`、成功 `3`、blocked `2`、failed `1`、outcome unknown `1`；
  失败请求追踪 `4`、成本状态追踪 `7`、event/outbox terminal 各 `6`，没有把零调用失败伪装成已调用。
- API 实际证明后端 RBAC 拒绝、跨租户同形隐藏、统一 request_id 和私有 no-store。
- 扫描 9 个受控文件，敏感明文命中 0；owned process、固定端口、SQLite sidecar 最终为 0，work 已清理。

## 5. PAGE-114 Browser 实际验收

权威 Browser：`.artifacts/prd082-browser/prd082-page114-20260816-r8/`。

- Chromium 实际操作 `1280x720` 与 `390x844`；Servers、Tools、Invocations 三个 tab 均可见并有真实数据。
- 完成 Server register、refresh、disable、enable；响应分别为 202，数据库 operation 精确为 register queued、
  refresh queued、disable succeeded、enable succeeded 各 1。
- 桌面完成 Tool Schema 按需详情和 high + approval policy；移动端完成原生注册 Dialog 和安全 Invocation/
  approval summary。Schema、Token、Endpoint、原始参数或结果不进入列表与截图。
- 数据库最终 Server `2`、Tool `2`、high + approval Tool `2`、Invocation `1`、pending Approval `1`、
  pending Approval outbox `1`；管理 Audit 精确为 `mcp.server_registered`、`mcp.server_disabled`、
  `mcp.server_enabled`、`mcp.tool_policy_updated` 各 1。
- 外部请求、禁止 query、Console error、Page error、文字 overflow、全局横向 overflow 和 overlap 均为 0；
  人工追加检查的 Console warning/error 也为 0。
- work 6 个、staging 5 个受控文件的敏感明文命中均为 0；Browser、Console、Control owner 均停止，
  SQLite sidecar 为 0。
- 正式批次完成后，5173/8001/18885/19085 监听、owned process、work/staging/driver、数据库 sidecar
  与临时凭据均为 0；final owner marker 已移除。

四张截图已目视复核为非空、内容完整且无控件或文字重叠：

- `page-114-servers-desktop-1280x720.png`
- `page-114-tools-desktop-1280x720.png`
- `page-114-invocations-mobile-390x844.png`
- `page-114-register-mobile-390x844.png`

Browser JSON SHA-256：
`7c9a965af27968769f8294c14a569bb152e27998d4b083f300f51625511360eb`。

Browser manifest SHA-256：
`e387ef1cf76b53b2a886751899ab919b9ea5fd90ca44beb9ec74e953f4eb411c`。

正式证据白名单只包含 Browser JSON、上述四张 PNG 和 SHA-256 manifest；五个 manifest 项的 bytes 与
SHA-256 已独立重算一致，不发布 Session、凭据、数据库或额外截图。

## 6. 安全、禁止文件与范围门禁

- 日志和证据只允许固定 ID、枚举、计数、UTC 时间、状态、request/trace 和不可逆摘要；不记录或复制
  Provider Key、Token、Prompt、Response、Endpoint、原始 MCP 参数/结果或第三方错误正文。
- Runtime/Browser JSON 均保持 `all_108_prds_complete=false`；默认上游目录未改变，OpenRouter、硅基流动仍
  只是竞品与迁移兼容对象。
- 正式 Runtime work 已清理；Browser 只发布六个白名单文件。数据库、WAL/SHM/journal、日志、Browser
  profile/cache、失败诊断目录、Office 锁文件、真实 `.env`、凭据、符号链接或超限生成物不得进入提交。
- 最终提交前仍须由主任务按实际 staged 范围执行敏感信息和禁止文件审计；本记录不把尚未执行的提交动作
  或 staged-scope 审计写成已经通过，也不执行提交或推送。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 84 行、`03_M2-M3扩展交付` 第 20 行、PAGE-115、API-378..382、
`McpNetworkPolicy`/OBJ-114、`mcp.network_blocked` 与 TC-0165/0166 共同确认，PRD-082 之后的下一条权威
未完成项为：

`PRD-083 | M2-企业增强 | P2 | 扩展层 | MCP沙箱 | 第三方MCP沙箱网络ACL`

本次只登记，不实现 PRD-083；`all_108_prds_complete=false`。
