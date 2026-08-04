# PRD-070 多轮会话风险引擎测试记录

状态：**已完成**。设计与两轮编码前自审、实现后两轮复核、0062、专项与必要关联回归、HMAC 轮换
交叉审查、真实三服务/API/RESP、权限、租户、异常、隐私和应用内 Browser 双视口验收均已通过。
总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 自动化结果

| 验证项 | 当前结果 | 覆盖 |
| --- | ---: | --- |
| PRD-070 聚焦 Python | `86 passed` | Shared 分类/HMAC/RBAC、Redis/Lua/RESP、Gateway、Control、0062、导航 |
| 分类性能 | `1 passed` | P95 `7.175ms`、P99 `8.769ms`，满足设计阈值 |
| 受影响组合 | `180 tests / 174 passed / 6 failed` | 6 项均由 spool v4 引起的共同历史合同漂移暴露 |
| 原失败与共同根因闭环 | `8 passed` | v1-v4 兼容、v5 拒绝、失败写入与回放恢复 |
| 后续直接受影响专项 | `10 passed` | Gateway/Control 相邻合同和晚到 Agent 修订 |
| 0062 迁移 | `6 passed` | up/down/往返、owner、授权与未知依赖 guard |
| PAGE-098 | `11 passed` | exact DTO、owner、权限 DOM、筛选、详情、动作和迟到响应 |
| Redis HMAC 扫描新增专项 | `13 passed` | score/pending/turn、unknown、legacy、分页、循环和协议污染 |
| Store/HMAC/RESP 相关组合 | `35 passed` | cycle 选择、数据库门禁、生产 Store 与仓库内回环 RESP |
| 数据库文件系统故障节点 | `1 passed` | 父目录 `OSError/PermissionError` 固定转换为 verifier unavailable |
| HMAC verifier CLI | `3 passed` | blocked、缺 previous、Store 构造故障的固定输出与退出码 |
| Console lint | `201 files` | 通过 |
| Ruff / compileall | `passed` | PRD-070 直接修改范围 |
| 中文注释门禁 | `comment coverage ok` | `scripts/ci/check-comments.py` 通过 |

JUnit 证据位于：

- `D:/MetaHub/.tmp/prd070-focused-final.xml`
- `D:/MetaHub/.tmp/prd070-perf-final.xml`
- `D:/MetaHub/.tmp/prd070-affected-final.xml`
- `D:/MetaHub/.tmp/prd070-spool-fix.xml`
- `D:/MetaHub/.tmp/prd070-late-agent-final.xml`

受影响组合只运行一次。修复共同根因后仅复跑原失败节点和直接受影响集合，没有重新运行 180 节点整组；
这既保留了真实失败证据，也避免与变更范围无关的重复长时间回归。pytest 的 `TEMP/TMP` 和 basetemp
全部限定在 `D:/MetaHub/.tmp`，未使用 `C:/tmp`、未提权、未删除测试证据。

## 2. Gateway 真实主流程

- 三个连续风险 turn 的累计分数为 `3600 -> 7200 -> 10000`，达到冻结阈值；三次仍返回更早且更具体的
  `gateway.intent_blocked`，不会因风险累计覆盖既有安全错误。
- 下一请求在正文读取、Rate Limit、Budget 和 Provider 之前命中冻结预检，返回
  `gateway.session_risk_frozen`。本轮 8 条受治理请求的 `routing_attempts=0`、`usage_ledger=0`。
- RequestLog、持久 observation 和 score 的 turn、revision、分数及状态一致；重复相同 turn 不重复计分，
  相同 Turn ID 携带不同意图返回 `gateway.session_risk_turn_conflict`。
- 非法 Session/Turn Header 对返回 `gateway.session_risk_identifier_invalid`；所有错误均包含响应头和正文
  一致的 `request_id/trace_id`。

## 3. Control 真实处置、权限与租户

- `freeze_session`、`freeze_virtual_key`、`dismiss`、`release_session`、`release_virtual_key` 五类动作
  均实际返回 HTTP 200，并按固定状态矩阵写入 action、freeze、Audit 和 source event。
- 相同动作幂等回放返回 HTTP 200 且 `idempotent_replay=true`；相同幂等键携带不同意图返回 HTTP 409
  `session_risk.idempotency_conflict`；旧 revision 返回 HTTP 409 `session_risk.state_conflict`。
- Viewer GET/POST 返回 HTTP 403 `auth.permission_denied`；第二租户的真实 score 由默认租户访问返回
  HTTP 404 `session_risk.not_found`，未泄漏资源存在性。
- GET、五类 POST、401/403/404/409/503 均带 `Cache-Control: no-store`、统一错误码和
  `request_id/trace_id`。

## 4. 故障、并发与兼容性

- Redis Lua 真实 TCP 合同覆盖 PING、AUTH、SELECT、EVAL、RESP 数组、半包、断连和有界超时；只使用
  仓库内 RESP 验收服务，不依赖沙箱外 Redis、WSL、Docker 或下载。
- public service 的 Redis/pending/evidence 故障在 Provider 前失败关闭；internal-only 仅在数据库可证明
  未冻结时 degraded allow，并写入明确的 RequestLog 运行状态。
- ACK 丢失、过期 cycle、revision 单调、并发跨阈、重复 turn、动作 CAS、幂等恢复及多来源 overlay
  均有定向测试；释放一个来源不会解除其他 score 或其他 target 的冻结。
- Usage spool writer 只写 v4；reader 分别严格校验 v1、v2、v3、v4，并明确拒绝未知 v5，避免把未来
  payload 误读为当前合同。

## 5. 开发库与隐私终态

- 开发库：`62/0062`，`PRAGMA integrity_check=ok`，外键违规 0。
- 默认 Provider 的临时健康修改已经恢复为 `active/unhealthy`；第二租户验收 Provider、Model、Virtual Key
  均已禁用，未留下可继续调用的跨租户测试资源。
- 数据库及当前运行日志扫描 Prompt、管理员密码、幂等键、`mhs_`、`mht_` 和 Bearer，命中均为 0。
- 真实运行证据：
  `D:/MetaHub/.artifacts/prd070-runtime/run-20260804T091013/runtime-acceptance.json`；对应服务日志只保存
  状态、固定错误码和链路 ID，不保存正文、Key、Token 或会话 Header。

## 6. HMAC previous 移除门禁

- `scripts/preflight/prd070-hmac-rotation-verifier.py` 只从正式 Gateway 配置读取数据库、Redis 和
  current/previous 版本，不接受命令行 URL/Secret。退出码 `0/3/2` 分别表示
  ready/blocked/unavailable，stdout 只输出 canonical JSON 计数，stderr 只输出固定错误码。
- Redis 扫描专项直接替换生产 Store `_command`，没有创建 socket 或访问开发库；覆盖 SCAN 多页、重复
  key 去重、读取前自然过期、previous/current/unknown/legacy、坏 cursor、超长 key、未知 Hash/schema
  与坏 claim。任一不可解释状态固定失败关闭。
- 交叉审查修正三个真实缺口：Hash `schema_version` 只接受 `1`；Store 构造阶段异常也由 CLI 收口；
  数据库父目录访问触发的 `OSError/PermissionError` 固定转换为 verifier unavailable。三种故障均不产生
  traceback 或 Secret 泄漏。新增专项 `13 passed`、相关组合 `35 passed`、数据库文件系统故障节点另行
  `1 passed`、CLI `3 passed`。因最后一个节点单独执行，结果如实记录为 `35 passed + 1 passed`，不写成
  未实际合并运行的 `36 passed`。

## 7. 应用内 Browser

- 应用内 Browser 已在 `1280x720` 与 `390x844` 完成真实页面验收：页面加载和登录成功，默认高风险
  列表可见，已处置筛选返回 2 项，详情展示 2 个冻结 overlay 和 5 条动作时间线。
- 两个视口的页面级横向溢出均为 0。桌面验收发现筛选栏在窄容器中溢出后，布局已改为
  `repeat(auto-fit, minmax(180px, 1fr))`；PAGE-098 `11/11` 通过并重新完成双视口检查。
- 最终恢复检查重新打开 `http://localhost:5173/#session-risks`、完成登录并确认：高风险标题 2 个、
  运行状态徽标 2 个、筛选按钮 1 个、登录页 0 个。页面被误关是此前空白状态的原因，并非服务或安全
  策略变化。
- 应用内验收没有保留截图，因此不声称存在截图制品；可复核的文本记录位于
  `D:/MetaHub/.artifacts/prd070-browser/in-app-browser-acceptance.txt`。
- 并行 Edge extension 尝试的两次 DOM 读取超时仍保留在
  `D:/MetaHub/.artifacts/prd070-browser/browser-control-failure.txt`，它是非权威诊断，不覆盖上述应用内
  Browser 验收结论，也未触发 standalone Playwright、外部 GUI 或提权操作。

## 8. 当前结论

权威运行证据为
`D:/MetaHub/.artifacts/prd070-runtime/run-20260804T091013/runtime-acceptance.json`，SHA-256
`B5C9CE673231F8BE84CC0E2A3208C5A27A1EF35F02AEF9288C13996D3ABE9B99`。其中三服务为
HTTP `200/200/200`，数据库为 `62/0062`、`integrity_check=ok`、外键违规 0，运行文件敏感明文命中 0。

设计、两轮自审、代码、中文注释、租户隔离、后端权限、统一错误/request ID、迁移、主/异常/权限测试、
文档、实际运行和应用内 Browser 双视口验收均已通过，PRD-070 已完成。Excel 已交叉确认下一项为
`PRD-071 输出侧安全检测与流式滑窗`，该条只完成识别，设计尚未开始；总体目标仍为
`all_108_prds_complete=false`。
