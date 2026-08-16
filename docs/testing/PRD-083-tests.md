# PRD-083 第三方 MCP 沙箱网络 ACL 测试记录

日期：2026-08-16

结论：PRD-083 的 Shared 网络合同、0076、Control/Scheduler、独立 Sandbox Worker、PAGE-115、Compose、
Runtime、Browser、迁移往返、隐私和资源清理门禁均以本记录所列正式批次为准。诊断失败批次不作为通过证据。
本条完成不代表 108 条 PRD 全部完成，`all_108_prds_complete=false`。

## 1. 范围与完成依据

- Excel `01_PRD总表` 第 84 行、`03_M2-M3扩展交付` 第 20 行、PAGE-115、API-378..382、OBJ-114、
  EVT-095 与 TC-0165/0166 共同冻结本条，不按编号猜测范围。
- Network Policy 固定 `registered_endpoint_only`，tenant 不能提交第二套 hostname/IP/CIDR；Control 不解密
  Endpoint，独立 Worker 才执行单次 DNS 全地址校验、永久 deny、部署级精确私网例外和 pinned connect。
- 所有 tenant 业务事实均由复合 owner/FK、后端 RBAC 与事务内实时重检约束。PAGE-115 capability 和按钮可见性
  只用于体验，不构成授权。
- API、Worker ASGI 和 Scheduler 终态均使用固定错误码与 request_id；调用/失败/用量/成本、network decision、
  circuit、call intent、event/outbox 均可追踪。
- Provider Key、Token、Endpoint、Prompt、Response、参数/结果、resolved IP、CIDR 和第三方异常正文不得进入日志、
  JSON、manifest、截图、Office 或提交证据。

## 2. 设计与实现审查

- 编码前第一轮聚焦 trust boundary、permit/call intent、DNS/ACL、tenant owner、熔断、事件正交和 0076，
  关闭 9 项 P0/P1。
- 编码前第二轮聚焦三角色配置、outbox、waiting-policy、公开 exact wire、RBAC、cleanup、rotation、caps 与
  legacy drain，关闭 14 项 P0/P1/P2。
- 实现后第一轮深审关闭实时 Grant/RBAC 漂移、permit 全绑定、authorize 异常 fence、无认证 Credential 和
  closed-circuit stale fence。
- 实现后第二轮深审关闭 DNS 超量计数、跨角色环境变量、registry task 清理、固定 availability 分类和 permit
  精确 destination 字段合同；真实 Runtime 追加关闭成功 permit 缺少 migrated call-intent commit 覆盖，以及
  `probing -> calling` 提前改写 Invocation request_bytes 的数据库合同冲突。最终无未关闭 P0/P1。
- Runtime deadline 深审进一步关闭 Sandbox action 占满签名 deadline、导致 cleanup/加密/HTTP 回传后响应过期的
  间歇性失败：Shared 固定 `6s` Worker response reserve，job durable deadline 采用 `probe + response 6s + DB
  finalize 6s`，Scheduler 签名 deadline 采用 `probe + response 6s`，Worker 固定扣除 response reserve 后才允许
  上游动作。配置门禁要求 operation lease 严格覆盖 shared probe cap 与两段 reserve；不足预算在网络前固定收口。
- Worker MCP 生命周期复核进一步分离 action 与 context close：initialize/list/call 和完整结果物化受 action timeout
  约束，context close 使用独立 cleanup deadline。确定性测试要求已完成结果不被单纯关闭超时覆盖，结果完成前的
  timeout 仍映射 `overall_timeout`，且 session/stream/transport cleanup 仍执行。真实 AnyIO close 的纯
  `ExceptionGroup[TimeoutError]` 同样不能覆盖已物化结果；混合异常和取消仍传播并失败关闭。
- Runtime outbox 复核关闭 `mcp.network_blocked` pending 恢复缺少 source request 身份的问题：Webhook 重建只从
  同 tenant 不可变 `mcp_network_events` 恢复 request_id，信封或 owner 漂移时在 HTTP 前失败关闭。Runner 最终
  readiness 使用 Worker 的正式 `owned_tasks` 字段，不接受不存在的别名。

## 3. 自动化门禁

| 分组 | 最终结果 | 覆盖 |
| --- | ---: | --- |
| Shared network | `9/9 passed` | policy caps/fingerprint、ACL、event、envelope/permit/response、RBAC/config |
| Sandbox Worker | `28/28 passed` | DNS、pinning、Host/SNI、proxy/redirect、deadline、分类、加密 ASGI、cleanup |
| PRD-083 Scheduler | `20/20 passed` | permit/intent、migrated success、实时撤权、事件正交、circuit CAS、恢复 |
| 0076 与 Compose | `17/17 passed` | fresh/repeat/down/re-up、seed 保真、drain/down guard、对象与容器隔离合同 |
| 相邻 MCP Control/Scheduler | `35/35 passed` | PRD-082 公开 API、Sync/Invocation、审批、DLP、迟到 owner 兼容 |
| Console 受影响专项 | `33/33 passed` | exact client、owner/Abort、PAGE-114/115、409 重基、撤权和 evidence 合同 |
| Runtime/Browser Python 合同 | `62/62 passed` | 失败关闭、真实迁移/服务/隐私/清理 facts 与 artifact provenance |
| Browser Node driver 合同 | `6/6 passed` | 双视口、四 PNG、网络 guard、动态撤权、失败目录回滚 |
| Console lint | `263 files` | Console 源码和测试静态门禁 |
| Ruff / format / compile / diff check | `passed` | PRD-083 新增和受影响 Python、语法、格式与补丁完整性 |

以上分组存在相邻覆盖，不相加冒充一次根级全量。开发阶段只运行受影响专项；最终门禁扩展到既有 PRD-082
MCP Control/Scheduler、Compose 和验收合同，没有反复执行无关全量。

## 4. Migration 0076

- 空库自动化门禁实际完成 `76/0076 -> 76/0076 -> 75/0075 -> 76/0076`；四阶段均要求并通过
  `integrity_check=ok`、外键违规 0。
- seeded 往返逐行验证 0075 Server、Operation、Job、Invocation、Attempt 的 rowid 与全部列值；合法业务事实
  不因 Up/Down 漂移。
- 未排空 operation/job/invocation、非默认 policy/network facts、EVT-095 subscription/delivery、Audit 或自定义
  RBAC 引用均使迁移/Down 原子失败，不留下半迁移对象。
- 0076 固定 8 张 network 表、RBAC revision 18、waiting-policy 恢复、复合 owner/FK、append-only、CAS、
  event/call 正交与 current-revision circuit 聚合。

## 5. Runtime 正式验收

权威批次：`.artifacts/prd083-runtime/prd083-final-20260816-ac/`

- `runtime-acceptance.json`：`status=passed`，SHA-256
  `C5FFB99B15FF2156D26408B08C4F8ACD2C4EC07610A06799DDDFE471B23D0047`；`SHA256SUMS.txt` 与实际文件相符。
- 从空 SQLite 实际完成 `76/0076 -> 76/0076 -> 75/0075 -> 76/0076`，四阶段均
  `integrity_check=ok`、外键违规 0；最终库非终态行、active lease 和 SQLite sidecar 均为 0。
- 真实 Control HTTP、独立 Scheduler、独立 Sandbox、HTTPS MCP 与本地 Webhook receiver 全部通过；
  waiting-policy 恢复、discover、成功调用、timeout outcome-unknown、停服恢复均形成可追踪终态。
- pinned transport、permit-bound call intent、阻断零 Tool call、熔断开启和 EVT-095 签名投递均通过；成功、
  阻断和超时路径的失败、用量与成本状态可交叉查询。
- evidence 禁止字段命中 0、10 个受控文件敏感明文命中 0；Sandbox session/connection 为 0，owned process 为 0，
  固定端口 `18930..18934` 已释放，正式 work/staging 已移除。
- 失败诊断批次不作为通过证据；旧 `.prd083-diag-20260816-z.staging` 已按 owner marker 精确清理，未触碰正式批次。

## 6. Browser 正式验收

权威批次：`.artifacts/prd083-browser/prd083-final-20260816-f/`

- `browser-acceptance.json`：`status=passed`，SHA-256
  `30685F52892AE64838CCA4C32C1CAA044B974D311D8E0425016BFFDC8C61AE94`。
- `sha256-manifest.json` SHA-256：
  `0C59FEE846B45B54461DC00D7C86C2591F47EBF9B87248CB3850128D80830044`；JSON 与 4 张 PNG 共 5/5 hash 相符。
- Chromium 在 `1280x720` 与 `390x844` 覆盖 Network 摘要、编辑 Dialog 和 409 手工重基；主 Agent 已逐图
  复核，页面重叠和全局横向溢出为 0，文字、输入与命令均完整可读。
- 实际后端完成 admin/security_admin/ops_admin 编辑、read-only 无命令、409 不自动重提、会话/动态权限漂移
  清理与迟到响应拒绝。revision/revision rows/audits 为 `5/5/4`。
- 运行库为 `76/0076`，`integrity_check=ok`、外键违规 0；外部请求、页面/Console 错误、敏感 DOM/Storage/
  evidence 命中和 owned 端口/进程残留均为 0。
- 两个旧诊断 Chrome 目录各剩系统锁定的 `first_party_sets.db`/journal；不含正式 artifact，端口已释放，
  未提权强删，不能冒充通过或失败残留。

## 7. 隐私、禁止文件与资源审计

- tracked/untracked、补丁、Runtime/Browser JSON、manifest 和截图只允许固定 ID、枚举、revision、计数、hash、
  HTTP status 与安全示例；禁止的业务明文命中为 0。
- Sandbox 固定分类经加密响应返回；日志和数据库不保存 Endpoint、Header、Token、参数、结果或异常正文。
- 提交集合不得包含 `.env`、SQLite/sidecar、日志、Token/Key、真实 Endpoint、Office 临时文件、cache、coverage、
  build、诊断 artifact 或进程命令行证据。
- 正式 Runtime/Browser 完成后 owned 服务、端口、session/connection/task、SQLite sidecar 和临时凭据均须归零；
  只保留仓库忽略的正式哈希证据目录。

## 8. 下一项交叉确认

Excel `01_PRD总表` 第 85 行、`03_M2-M3扩展交付` 第 21 行、PAGE-116、API-383..388、OBJ-115、
EVT-096 与 TC-0167/0168 共同确认下一条权威未完成项为：

`PRD-084 | M2-企业增强 | P2 | 扩展层 | 本地模型 | 本地模型Endpoint接入`

本次只登记，不实现 PRD-084；`all_108_prds_complete=false`。
