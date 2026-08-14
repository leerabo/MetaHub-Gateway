# PRD-078 移动端/企微钉钉审批入口测试记录

状态：**已完成**（2026-08-14）。两轮编码前设计自审、实现、两轮实现后自审与修正、专项和必要回归、
0071 真实迁移往返、Runtime、PAGE-108/109 Browser、文档与提交前安全门禁均已完成。本结论只关闭
PRD-078；`all_108_prds_complete=false`。

## 1. 权威范围与设计门禁

- Excel `01_PRD总表` 第 79 行、`03_M2-M3扩展交付` 第 15 行、PAGE-108/109、审批 API、
  ApprovalTask、`approval.created`/`approval.timeout` 与 TC-0155/0156 已和 README、`docs/prd`、
  `docs/testing` 交叉确认，本条是
  `PRD-078 | M2-企业增强 | P2 | 扩展层 | 移动审批 | 移动端/企微钉钉审批入口`，不是按编号推断。
- 设计见 `docs/prd/PRD-078-mobile-enterprise-approval-entry.md`。两轮编码前自审冻结 tenant-qualified
  状态机、base + target 双重后端 RBAC、固定 target registry、独立审批 HMAC、一次性 step-up、CAS 决策、
  timeout、公平租约、原子 Decision/Audit/outbox/writeback 和 PAGE-108/109 owner 生命周期。
- 企微、钉钉、飞书只是现有签名 Webhook 的通知接收端。外部卡片点击必须回到 MetaHub 登录页面；外部
  平台身份、前端按钮、URL 参数或客户端 `tenant_id` 均不是授权依据。本条不开发完整移动 App 或通用工作流。
- 默认上游保持智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务与客户本地
  OpenAI-compatible Endpoint；OpenRouter、硅基流动仍只是竞品与迁移兼容对象。

## 2. 两轮实现后自审

第一轮复核后端授权、事务、恢复和隐私边界，确认所有公开审批入口逐请求重载 Session、RBAC、task owner
和 target owner/version；业务终态、Decision、AuditLog、target writeback 与 source/outbox 原子提交，网络和
Redis 派生写回在事务外以完整 lease CAS 恢复。该轮修正登录/刷新/登出的异步 owner，以及健康探测 timeout
与真实请求收敛边界，关闭旧响应恢复 Session/Storage/DOM 和并行重复探测风险。

第二轮复核迁移、Console 生命周期与验收可信度。Browser 诊断发现旧隐藏 overview 可能被误认成新 refresh
完成，随后又覆盖 Network owner 已终态清除但 Fetch 控制句柄仍暂停的合法 CDP 顺序；最终实现只接受两种
精确状态，并继续拒绝半清理、错 owner、额外取消和普通请求未完成。修正后重新执行 Node 合同与全新 Browser
final；失败诊断批次未白名单、未作为完成证据。其后暂存区 `git diff --check` 又发现
`approvalPageState.js` 末尾多余空行；清理后源码 SHA 改变，因此已通过的 `-35` 仍被判定为非权威并不纳入
白名单，最终字节重新通过 Console `26/26`、Node Browser 合同 `46/46` 和 Browser final `-36`。

## 3. 已通过的自动化门禁

以下分组存在包含或相邻合同关系，**不得相加冒充一次根级全量**。

| 验证项 | 最终结果 | 主要覆盖 |
| --- | ---: | --- |
| Python PRD-078 专项 | `97 passed` | Shared HMAC/事件、0071、Control API、双重 RBAC、租户、step-up、幂等、timeout、outbox/writeback 恢复、Runtime/Browser harness |
| Console 审批与直接相邻 owner/健康回归 | `26/26 passed` | PAGE-108/109 状态、渲染、Dialog、导航、登录/刷新/登出 owner、健康探测串行 retry |
| Node Browser 验收合同 | `46/46 passed` | 固定 loopback、CDP owner、双视口、证据原子发布、provenance、隐私与进程清理 |
| Python/Node 语法与中文注释 | 通过 | 受影响 Python/JavaScript 的语法和中文职责/安全边界门禁 |
| Git whitespace | `git diff --check` 通过 | 行尾、空白与 patch 完整性 |

Python 最终专项使用以下范围；pytest 临时目录固定在仓库内 `.tmp`：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  shared-sdk\python\tests\test_prd078_approvals.py `
  control-plane-api\tests\unit\test_prd078_approval_runtime_boundary.py `
  control-plane-api\tests\unit\test_prd078_console_navigation.py `
  control-plane-api\tests\integration\test_prd078_approvals_control.py `
  control-plane-api\tests\integration\test_prd078_approvals_recovery.py `
  control-plane-api\tests\integration\test_prd078_db_migrations.py `
  scripts\acceptance\tests\test_prd078_runtime_acceptance.py `
  scripts\acceptance\tests\test_prd078_browser_harness.py
```

## 4. 后端、权限与状态机结果

- 内部创建只接受固定 `budget_block_release/release` 与五类 `session_risk_action`，target risk、version、
  fingerprint 和 required permission 均由服务端事实派生；任意 URL、SQL、函数名或回调 JSON 无入口。
- 列表、详情、step-up 和决策均先验证 Session 与 `approval.read/decide`，再验证预算或会话风险目标域权限；
  finance 只看预算，security 只看会话风险，developer 为 403，跨租户为同形 404。
- standard 预算批准释放指定规则的 active block；拒绝和 timeout 无目标副作用。high-risk 会话动作必须后端
  密码 step-up，ticket 绑定 tenant/user/session/task/action/revision/authorization fingerprint、120 秒且单次消费。
- 同 actor/key 的精确决策重放只返回同一 Decision；相反 action 或 intent 变化返回固定 409。人工决策与
  scheduler timeout 竞争同一 pending CAS，终态不可逆且失败请求、用量/成本关联和回写状态均可追踪。

## 5. 迁移与恢复门禁

权威 Runtime 从空 SQLite 实际执行四阶段，不使用预制数据库：

| 阶段 | migration count/version | integrity | 外键违规 |
| --- | --- | --- | ---: |
| fresh | `71/0071` | `ok` | `0` |
| repeat | `71/0071`，applied 0 | `ok` | `0` |
| down | `70/0070` | `ok` | `0` |
| re-up | `71/0071` | `ok` | `0` |

专项同时覆盖 0070 升级、自定义权限矩阵保真、既有 rowid/schema 保真、catalog/schema/trigger 漂移、审批
业务事实、Webhook subscription/delivery 和自定义新 grant 的原子失败关闭。Runtime 真实停止 Control 后让
timeout 形成事实，再由新 Control lifespan 恢复 timeout outbox；会话风险 Redis 回写先失败，RESP 恢复后由
新 owner 对账完成。最终 processing lease 为 0，五个 task 的 writeback 全为 completed。

## 6. Runtime final

权威目录：`.artifacts/prd078-runtime/prd078-runtime-20260814-08/`。

- `status=passed`；两类 target、finance/security/developer、跨租户、内部认证、step-up 单次消费、精确重放、
  相反意图 409、两个 timeout、停服 outbox 和 Redis writeback 恢复均通过。
- 事件计数为 `approval.created=5`、`approval.timeout=2`；Webhook 实收 created 4、timeout 1，签名失败、
  forbidden 和 invalid 均为 0。数据库 `decision_count=5`、processing lease 0。
- `runtime-acceptance.json` SHA-256 为
  `5a3930e775654a3503241a464e37b206d3850db50c45c7b58b5f7fcf5cb43411`，`SHA256SUMS.txt` 独立重算匹配；
  migration up/down、service、scheduler、outbox 与实际验收脚本 provenance 全匹配冻结源码。
- 10 个受控文件敏感明文命中 0、SQLite sidecar 0；Control initial/recovery、Console、RESP 与 Webhook
  均为 owned-and-stopped，受控端口无 listener。

## 7. Browser final

权威目录：`.artifacts/prd078-browser/prd078-browser-final-20260814-36/`。

- `status=passed`；桌面 finance standard、移动 security high-risk 与 busy step-up 均走实际页面和后端 API。
  三张 PNG 分别为 `1280x720`、`390x844`、`390x844`，已人工复核为非空、内容完整、无敏感正文。
- 标准/高风险初始焦点、正反向 Tab wrap、Escape、背景阻断、busy cancel/Escape 阻断、决策完成、
  step-up 完成、焦点归还均通过；权限撤销、owner 漂移、迟到成功/拒绝和登出均清空 password/root/storage。
- 两个视口横向溢出、文本溢出和重叠均为 0；外部请求、运行时异常和非预期 Console error 均为 0。
- `sha256-manifest.json` SHA-256 为
  `600dad48b961ecde342f378776ded8c92fe8bfb83cef12c92bc0c758a33c3065`；4 个 payload 文件的名称、字节数和
  SHA-256 独立重算全匹配。Browser JSON SHA-256 为
  `49ca9259490a12663cfbad3d441a4dc90a8a103f98ad5067f20616b20d4b5713`，九项源码 provenance 全匹配。
- work/staging 分别扫描 10/4 个文件，明文命中 0、SQLite sidecar 0；Gateway、Control、Console、RESP 与
  harness 均为 owned-and-stopped，受控端口无 listener。

## 8. 最终结论与下一项

PRD-078 的 tenant 隔离、后端授权、统一错误/request_id、敏感信息保护、成本/用量/失败追踪、迁移、恢复、
Console 生命周期和真实运行证据均满足冻结合同。提交候选只白名单上述 Runtime/Browser final，不包含失败
诊断批次、数据库、WAL/SHM/journal、日志、Browser profile、缓存或 `.tmp`。

Excel `01_PRD总表` 第 80 行、`03_M2-M3扩展交付` 第 16 行、PAGE-110/111、套餐与额度 API、
Plan/QuotaBucket、`quota.exhausted`/`trial.expired` 和 TC-0157/0158 共同确认，下一项为
`PRD-079 | M2-企业增强 | P2 | 扩展层 | 套餐计费 | 套餐、试用与额度类型`。本次只确认、不实现；
`all_108_prds_complete=false`。
