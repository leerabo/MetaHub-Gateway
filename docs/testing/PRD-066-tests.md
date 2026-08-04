# PRD-066 企业 Chat 演示入口测试记录

状态：**实现、专项测试、必要分层回归、迁移、真实 HTTP/Provider、应用内 Browser 双视口、
同会话撤权/恢复、注销和运行清理均已通过；PRD-066 已完成。**

本文只登记直接执行结果。Prompt、Response、Provider Key、Virtual Key、内部 Token、登录口令、
一次性 grant 和完整 HTTP 正文均不进入本文、普通日志或 JSON 证据。本轮测试临时目录只使用
`D:/MetaHub/.tmp`，JUnit 和截图只使用 `D:/MetaHub/.artifacts/prd066-*`，未提权、未使用 `C:/tmp`；
硬编码读取禁用目录的 PRD-050 静态证据文件共 21 个
收集节点已明确排除，未执行其测试正文。

## 1. 验收范围

- 0058 up/down、RBAC revision 3、`chat.use`、三张 tenant-scoped Chat 表和单次 grant；
- Control `options/session/message/clear` 的认证顺序、实时后端权限、owner、租户和 CAS；
- Gateway 内部 Token、grant 原子消费、资源实时复核及公开入口共用治理流水线；
- RequestLog、Usage、RoutingAttempt、Policy、DLP、RateLimit、Budget、Provider Adapter 和成本；
- PAGE-093 客户端 exact-set 合同、内存正文、owner/generation、401/403 清理和响应式布局合同；
- Prompt、Response、Key、Token 和口令的数据库、服务日志、URL/Storage/Console 旁路约束；
- 三服务真实健康、匿名/重复/跨租户/撤权/清空异常流程和进程关闭终态。

## 2. 已执行专项

| 范围 | 直接结果 | 结论 |
| --- | ---: | --- |
| Shared RBAC/错误码/Config/Webhook 合同 | `9 passed` | revision 3、`chat.use`、内部 Token 和 exact-set 事件通过 |
| 历史 UUID 角色撤权兼容 | `9 passed` | 规范 UUID 与新格式均可生成严格绑定事件；非规范表示继续失败关闭 |
| 0058 migration/up-down | `4 passed` | 往返、约束和完整性通过 |
| Control 企业 Chat | `14 passed` | 主流程、鉴权先于 JSON、权限、租户、事务复核和缓存/Usage 事实通过 |
| Gateway 内部 Chat | `11 passed` | Token、grant、实时 RBAC/资源复核、隐私及 canonical endpoint 通过 |
| Gateway 公开受影响节点 | `5 passed` | 成功、Fallback、流式、鉴权和畸形 JSON 合同未回归 |
| Console PAGE-093 与 Browser 合同 | `20 passed` | API、历史上限、状态机、清理、布局和验收器合同通过 |
| Browser 运行编排器纯沙箱合同 | `4 passed` | 仅验证回环 Provider、脱敏证据和禁用路径门禁，未启动 Edge |
| 应用内 Browser 启动器专项 | `7 passed` | 并发、异常、SQLite 锁、强审计回滚和 0058 真实结构通过 |
| 权限终态租户隔离 | `1 passed` | 其他租户授权不能掩盖默认租户撤权，默认租户恢复后才返回真 |
| Python 必要全量回归基线 | `1907 collected / 1886 passed / 21 excluded` | 兼容修复前基线；除用户明确禁用的 PRD-050 静态证据文件外全部通过 |
| Node 示例全量 | `33 passed` | OpenAI-compatible Node 示例全部通过 |
| Console 全量 | `594 passed` | PAGE-001 至 PAGE-093 现有控制台合同全部通过 |
| Console lint | `184 files passed` | 静态门禁通过 |
| 全仓中文注释门禁 | `comment coverage ok` | 0058、Python、JavaScript、CSS 和测试脚本通过 |

本轮 Python 定向台账不伪写成一次全绿：首次收集 `68` 个相关节点，其中 `28 passed`，另 `40` 个在进入
测试体前因遗留 `D:/MetaHub/.tmp/pytest-of-rabol` 节点拒绝扫描而发生 setup error，没有代码断言失败。
随后固定 `--basetemp=D:/MetaHub/.tmp/prd066-final-pytest-20260802T1330`、禁用 pytest cache，只重跑
包含这些未进入测试体节点的 6 个相关文件，结果 `52 passed`；再新增租户终态节点 `1 passed`。
结合首次已经通过且未重复执行的 Shared 节点，最终 `69` 个唯一受影响节点均有通过证据。JUnit 位于：

- `.artifacts/prd066-final-verification/targeted-python.xml`：首次 `68` 节点的直接记录；
- `.artifacts/prd066-final-verification/targeted-python-rerun.xml`：显式仓库内基目录下 `52 passed`；
- `.artifacts/prd066-final-verification/permission-cas.xml`：默认租户权限终态 `1 passed`；
- `.artifacts/prd066-role-patch-diagnosis/targeted-tests.xml`：UUID 撤权兼容 `9 passed`。

专项阶段只执行下列五个 Gateway 公开受影响用例；全部专项稳定后，再按设计退出计划执行一次必要全量回归：

1. `test_chat_completion_json_records_request_and_usage`；
2. `test_non_stream_5xx_falls_back_once_and_records_attempt_order`；
3. `test_chat_completion_stream_returns_sse`；
4. `test_auth_and_validation_errors_are_unified`；
5. `test_malformed_json_still_records_frozen_snapshot_claim`。

## 3. 必要全量回归台账

- 首轮续跑时 pytest 收集总数为 `1898`；前序证据覆盖 `1-1381`，本轮从 `1382` 续跑至 `1898`，
  没有因单点失败重新执行已经通过的前缀。并行收尾随后新增 9 个合同节点，当前重新收集为 `1907`；
  新增/晚改文件已分别验证 Gateway 内部 Chat `11 passed`、浏览器运行合同 `4 passed` 和
  PRD-056 持久隐私节点 `1 passed`。
- 索引 `1513` 的 Feedback Summary 在 Windows 同步依赖调度抖动下误触 250ms 门禁；测试先用真实
  Session/RBAC 恢复可信上下文，只对该次 Summary 请求隔离调度，生产时限和认证语义未放宽。
- 旧迁移测试按各 PRD 当时正式迁移头冻结；当前态仍验证 `58/0058`。旧权限测试改由
  `finance_admin` 读取 Usage/Budget，并显式验证 Developer 的财务接口为后端 `403`，未扩大生产权限。
- `1801-1850` 历史切片收集 50 项，其中 PRD-050 禁用文件占 21 项；其余 29 项通过。按当前
  `1907` 项重新收集口径，最终实际执行 `1886` 个 Python 节点并全部通过。
- Console 首轮全量发现 PRD-054 历史断言仍冻结 PRD-065 之前的遥测跳过列表；更新为当前精确四项
  合同后，定向文件 `9 passed`，最终 Console `594 passed`。Node 示例最终 `33 passed`。

## 4. 权威真实运行批次

权威证据：

- `D:/MetaHub/.artifacts/prd066-runtime/run-20260802T0955-CST/runtime-evidence.json`
- SHA-256：`97DCE88A2CBA1E1D8FADE66465C8FB06C6BC1D234DCCD77E748CB6F8E4FB0C45`

该验收器使用动态空闲回环端口和独占 0058 SQLite 数据库，真实启动 Gateway、Control、Console，
并在当前进程内运行只接受固定 OpenAI-compatible 合同的 Mock Provider。所有进程、正文和凭据均由
验收器拥有；结束后自有进程数、active ChatSession 和 issued grant 均为 0。

| 检查项 | 结果 |
| --- | --- |
| 迁移 | `58` 条，最高 `0058` |
| 数据库 | `integrity_check=ok`，外键违规 `0` |
| 服务 | Gateway `200`、Control `200`、Console `200` |
| 主流程 | options `200`、create `201`、message `200`、clear `204` |
| 异常/权限 | 匿名 `401`、重复消息 `409`、跨租户 `404`、实时撤权 `403` |
| Provider | 成功调用 `1`，非法请求 `0` |
| API/后端 request/trace | 成功及匿名/重复/跨租户/撤权流程均保存非空关联 ID；RequestLog 与 message 链路一致；页面只展示 `request_id` |
| RequestLog | `success`，endpoint 为 `/internal/chat/completions` |
| Policy/DLP | `allow`；DLP `log_only` 命中 `1` |
| Routing/Budget | RoutingAttempt `1`；Budget reservation `1`，其中 `1` 条已由同 request 正式 Usage 覆盖 |
| Usage/成本 | `11/7/18` Token，正式成本 `0.002500 CNY` |
| 终态 | active Session `0`，issued grant `0`，自有进程 `0` |
| 隐私 | 数据库明文命中 `0`，服务日志明文命中 `0` |

运行验收器另已定向验证 512 KiB Provider 正文上限、规范 `Content-Length`、任意层级重复 JSON key、
空 messages、非对象 message、ASCII run-id 和 Decimal 金额证据，异常只输出固定 stage/code。

开发库 `D:/MetaHub/.data/metahub-dev-prd031.db` 已再次执行正式迁移 CLI，结果 `applied=[]`、
`skipped=0001..0058`；迁移后 Gateway/Control/Console 最终健康复检为 `200/200/200`。

## 5. 页面与浏览器状态

仓库浏览器验收器及其合同测试覆盖 Developer 真实登录、两次 Session/Message、清空、后端 revision CAS
撤权与 finally 恢复、注销、双视口布局、截图像素门禁和 URL/DOM/Storage/Console/Network 隐私扫描；
PAGE-093 与 Browser 合同本轮为 `20 passed`。

2026-08-02 应用内 Browser 直接加载 `http://localhost:5173/#enterprise-chat`，完成两次真实受治理
Session/Message 和清空。Provider 固定证据为
`D:/MetaHub/.artifacts/prd066-browser-iab-20260802b/provider-stats-chat-flow.json`，结果
`call_count=2, invalid_count=0`，SHA-256 为
`B4799E74CC561FB35CC6A16ABF9B5AE1BE0E168091AAF074ED04942A463E2AED`。

- 桌面 `1280x720`：`enterprise-chat-desktop-clean.png`，SHA-256
  `75B9978BD6DC8546D433597DBE7346133697E831A3BFC5D13F4347212E7F7ACA`；
- 移动 `390x844`：`enterprise-chat-mobile-clean.png`，SHA-256
  `594B892398DF5F04FAF97DC64DF540B201B306EC48A495EE8FEC4CB0F950394F`；
- 两张截图均非空，无横向溢出、控件重叠或 Prompt/Response、账号、Key、Token 命中；
- 重新连接应用内 Browser 后，撤销 `chat.use` 并刷新：URL 安全回落 `/`，企业 Chat 导航与受保护区域
  均不存在；恢复权限后 `#enterprise-chat`、PAGE-093、项目/环境、模型选项及 `GET /chat/options 200`
  均恢复；`POST /auth/logout 200` 后返回登录页，受保护 DOM 和临时 Developer 身份均不存在。

修复后的应用内 Browser 启动器另完成 `7 passed` 专项；真实并发启动时第二实例在触碰数据库前失败，
第一实例仍保持 `1` 个 active 临时 Key 和 `1` 条创建审计，证明没有误停用或重复创建。
停栈时通过受测的服务中断清理路径执行 finally：最终临时 Key `1` 条且为 disabled，
`provider_key.created/provider_key.updated` 审计各 `1` 条；active Chat Session、在途消息、issued grant 和
外键违规均为 `0`，`8000/8001/11434` 已释放，固定 `2/0` Chat 证据未被本轮 `0/0` 统计覆盖。
本次为验证异常清理而终止 runner 自有 Gateway，runner 按设计以 `service exited` 非零退出报告故障注入；
随后 finally 清理、端口、凭据、审计和数据库终态全部单独复核通过，这个非零信号不是测试断言失败。

脱敏收口证据：
`D:/MetaHub/.artifacts/prd066-browser-iab-20260802b/browser-closeout-evidence.json`。该文件只保存布尔值、
计数、HTTP 状态和端口状态，不包含账号、口令、Key、Token、Prompt、Response、tenant_id、user_id
或数据库路径。

## 6. 完成判定

PRD-066 的设计、两轮编码前自审、实现、0058、专项与必要分层回归、真实 HTTP/Provider、租户、异常、
权限、治理事实、双视口、同会话撤权/恢复、注销、隐私、强审计清理和中文注释门禁均已通过，现标记完成。
本次只执行剩余验收节点和受影响静态检查，没有重跑无关全量测试。`all_108_prds_complete=false`。
