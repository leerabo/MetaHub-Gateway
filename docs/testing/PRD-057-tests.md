# PRD-057 Webhook 死信与持久重放测试记录

测试日期：2026-07-30 至 2026-07-31（专项与最终验收阶段）

当前状态：已完成。设计与两轮编码前自审、0047/0048、后端 API/worker、PAGE-083、专项与回归、
真实 Runtime、Browser 双视口、停服静止终态及正式 artifact 均有直接通过证据。下一项为
PRD-058；`all_108_prds_complete=false`。

运行环境：Windows PowerShell；Python 使用
`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
已执行 pytest 的 `TEMP`、`TMP`、`--basetemp`、pycache 和其他临时数据均位于
`D:/MetaHub/.tmp`；未使用 `C:/tmp`、未请求提权。专项失败或缺口只运行对应 node，没有从头
重跑整套全量。

权威验收批次为 `D:/MetaHub/artifacts/prd057/20260731T005000/`。最终审计状态为 `passed`，
manifest 覆盖 36/36 个登记文件且逐项 hash/字节一致；临时原始证据仍保存在
`D:/MetaHub/.tmp/prd057/`，未因生成正式 artifact 而删除。

## 1. 验收口径

- Excel 权威项：`PRD-057 - Webhook死信与持久重放`，`M1-企业POC / P1`。
- 页面/API/对象/事件：`PAGE-083`、`GET /webhook-dead-letters`、
  `POST /webhook-deliveries/{webhook_delivery_id}/replay`、
  `POST /webhook-dead-letters/{dead_letter_event_id}/archive`、`DeadLetterEvent`、
  内部 `webhook.dead_lettered`。
- `TC-0113`：接收端持续失败后事件事实不丢；恢复后可持久重放；重复网络投递保持相同
  `event_id`，系统只承诺 at-least-once，不虚构 exactly-once。
- `TC-0114`：主流程、异常、后端权限、跨租户和安全路径使用统一错误与 request/trace；
  Provider Key、Token、Prompt、Response、Authorization、Secret、签名和接收端正文不泄漏。
- 所有业务查询、写入、claim、finish 和 owner 关系必须携带 `tenant_id`；tenant/actor 只从
  Session 恢复，admin/developer 可用，viewer 后端 403，跨租户与不存在同形 404。
- replay intent 与 Audit 同事务，archive 与 Audit 同事务，出站 HTTP 只允许发生在 intent
  提交后；同幂等键为纯读取，不能重复出网或误领另一 pending intent。
- 0047 必须覆盖空库、0046 历史失败回填、up/down/up、审计逐行保真、owner/状态机、
  业务事实 down guard、完整性和外键；0048 追加根死信与当前 replay delivery 终态一致性约束，
  不改写 0047 checksum 或既有业务事实。

完整合同见 [PRD-057 技术设计](../prd/PRD-057-webhook-dead-letter-persistent-replay.md)
和 [Webhook 死信与持久重放 API](../api/webhook-dead-letters.md)。

## 2. 当前测试资产

| 范围 | 文件/节点 | 已覆盖合同 | 当前直接结果 |
| --- | --- | --- | ---: |
| 后端主流程 | `test_prd057_webhook_dead_letters.py`，3 个 node | 500 最终失败、根死信、失败/成功 replay、同键、归档、跨租户/RBAC、崩溃接管、seek、过期 | `3/3 passed` |
| 六项独立回归 | `test_prd057_replay_idempotency_regression.py`，6 个 node | 旧键零出网、RBAC 先行、动态 Prompt 零泄漏、授权正文严格 wire、97 字符 ID 同形 404、Header/Body request_id | 6 个 node 均有分批直接通过证据 |
| 0047/0048 迁移 | `test_prd057_db_migrations.py`，3 个 node | fresh up/down/up、0046 历史映射、根 ID、审计保真、down guard、根状态/重放终态关系 | `3/3 passed` |
| 租约与错误映射加固 | `test_prd057_hardening_regressions.py`，7 个 node | 发送跨 lease 时钟、过期 owner 零出网、列表/归档/replay 数据库错误码、OpenAPI 必填幂等键、terminal replay 崩溃收敛 | `7/7 passed` |
| PAGE-083 | `console-web/tests/unit/webhookDeadLetters.test.mjs` | 列表 wire、cursor、筛选、重放/归档、owner、迟到响应、权限和安全 DTO | `11/11 passed` |
| 相邻 Webhook | Console 既有相邻 Webhook 测试 | PRD-025/042 client、页面和导航兼容 | `8/8 passed` |
| Console lint | Console lint | JavaScript/CSS 静态质量 | `151 files passed` |
| 中文注释 | `scripts/ci/check-comments.py` | 模块、类、函数、分支与迁移中文说明 | `comment coverage ok` |

代码冻结后的受影响专项命令实际结果为 `24 passed`。上表按合同类别列出主要 PRD-057 文件与
节点，不把函数数机械当作 pytest 收集数；最终结果以该次 24-node 直接输出为准。该专项覆盖新增
0048 迁移关系和 hardening 回归，替代此前“11 个 node 分批通过”的过时阶段口径。

## 3. 六项独立回归

### 3.1 旧幂等键不得领取另一 pending intent

节点：`test_old_idempotency_key_does_not_claim_new_pending_replay`。

先让 key A 对应 replay 失败，再暂停即时 drain 并用 key B 创建新的 pending intent；随后恢复
worker 并重放 key A。断言 key A 返回 `replayed=false` 和原失败 delivery，receiver 计数不变，
根死信仍指向 key B，`replay_dispatch_attempts=0`、claim token 为空、key B delivery 仍是
`pending/0 attempts`。该节点已在后端第一批通过。

### 3.2 viewer/匿名畸形 JSON 必须先做 RBAC

节点：`test_replay_rbac_precedes_malformed_sensitive_body_parsing`。

viewer 和匿名请求携带包含动态敏感字段的未闭合 JSON。断言分别返回
`auth.permission_denied` 403 和 `auth.token_missing` 401，响应有 request_id、不回显标记，数据库
不创建 replay。该节点证明顶层 replay 路由在读取正文前完成 Session/RBAC；已在后端第一批通过。

### 3.3 损坏 payload 的动态 Prompt 标记零泄漏

节点：`test_corrupted_payload_dynamic_prompt_marker_is_never_disclosed`。

测试把每次运行动态生成的 Prompt 标记写入已失败 source 的存量 `payload_json`，同时把 receiver
切到成功地址。replay 在任何 HTTP 前重新递归校验存量信封并返回固定
`webhook.replay_unavailable` 503；receiver 计数不变，响应、捕获日志和 replay 表均无动态标记或
新 intent。该节点在缺口定向批次通过。

### 3.4 授权 replay 正文必须有界并使用严格 wire

节点：`test_authorized_replay_body_is_bounded_and_strict`。

developer 已通过后端权限校验后，测试分别发送超限正文、重复 JSON key、`NaN` 和非法 UTF-8。
四类输入统一返回 `infra.validation_failed` 422，响应头与正文 `request_id` 一致；数据库不新增
replay delivery，响应和捕获日志均不回显动态敏感标记。该节点在安全定向批次通过。

### 3.5 97 字符 delivery ID 使用统一 404

节点：`test_replay_delivery_id_over_limit_uses_uniform_not_found`。

顶层 replay 对 97 字符路径 ID 返回 `webhook.dead_letter_not_found` 404，与不存在、跨租户和
归属不匹配资源同形；错误正文含 request_id，`X-Request-ID` 与正文一致。该节点在缺口定向批次
通过。

### 3.6 列表 Header/Body request_id 一致

节点：`test_dead_letter_list_header_and_body_request_id_match`。

开发者对列表发送显式安全 `X-Request-ID`，断言响应 200、正文 `request_id` 和响应头三者完全
一致。该节点在缺口定向批次通过。

## 4. 主流程、异常与权限结果

当前后端专项已经直接覆盖：

- 真实本地 receiver 连续返回 500，source delivery 最多三次 attempt 后变为 failed，并由 0047
  trigger 在同一事务形成唯一根死信；0048 继续约束根状态与 replay delivery 终态一致，failed
  replay 不递归形成子死信。
- receiver 恢复后使用新幂等键，replay delivery ID 攀新而业务 `event_id` 保持不变，根死信变为
  `replayed`；重复同键只回读原 delivery，receiver 只增加一个逻辑 intent。
- replay 再失败回到 `dead_letter`，随后可以用不同新键恢复；归档后旧键仍可纯读取，新键返回
  `webhook.dead_letter_not_replayable`。
- replay intent 提交后、HTTP 前暂停即时 worker，新 worker 可以领取；远端成功、本地 attempt
  未提交的崩溃窗口在 lease 到期后接管并保持同一 `event_id`，旧 token finish 为 no-op。
- seek cursor 两页无重复，Endpoint 筛选只返回当前租户，非法 status/cursor 统一 422；注入
  31 天时钟后死信变为 expired，replay/archive 均由后端拒绝。
- admin/developer 读取和治理；viewer 列表/replay/archive 403；匿名列表 401；第二租户列表为空，
  对第一租户 replay/archive 同形 404。
- 请求体夹带 tenant 和 payload 被严格 DTO 拒绝；公开 DTO、审计摘要和日志不含 payload 或
  幂等键。
- 过期 lease 的旧 owner 在发送前失权时零出网、零 finish、零 reschedule；发送跨过 lease 到期点
  时使用实际完成时钟执行 CAS。terminal replay 在远端结果已持久化而根状态未收敛的崩溃窗口只
  收敛根状态，不再次出网。
- 列表、归档和 replay 的数据库连接失败均折叠为各自 503 统一错误码和非空 request_id，不回显
  底层连接信息；OpenAPI 明确把 `idempotency_key` 标为必填。

唯一一次根级 Python 全量收集 `1496` 个节点，真实结果为 `1407 passed / 89 failed`，pytest
`5960.85s`、wrapper `5970.009s`。没有重跑根级全量：原 75 个迁移失败节点分别以 PRD-053
迁移/恢复 `14 passed`、其余迁移头部 `21 passed`、尾部 `40 passed` 闭环；原 14 个非迁移失败
节点以相关六文件 `53 passed` 验证。最终原失败节点精确定向闭环 `89/89`，新增失败 0。该记录不
把首次全量虚写成一次全绿。

## 5. 迁移专项结果

`test_prd057_fresh_up_down_up_preserves_schema_contract` 已验证：

- 空库经 0047 迁移后，表、索引、owner/identity/状态机/DELETE/REPLACE/rowid trigger 和 Audit 枚举存在；
- 无 PRD-057 业务事实时 down 到 0046，再 up 到 0047，schema 合同可恢复。

`test_prd057_history_mapping_root_identity_and_down_guard` 已验证：

- 从 0046 升级时，历史 failed original 每条形成唯一根死信，最新 pending/success/failed replay
  分别映射 `replay_pending/replayed/dead_letter`；
- 根死信 ID 与 source delivery 稳定关联，旧 Audit row 的 rowid、NULL 和全部列双向 EXCEPT 保真；
- 跨租户 owner、非法身份/状态迁移和含业务事实 down 被数据库原子拒绝。

`test_prd057_replay_state_relation_rejects_mismatched_terminal_root` 已验证 0048 在不改写 0047
checksum 的前提下拒绝 `replayed + failed delivery` 等稳定终态错配，同时保留
`replay_pending + terminal delivery` 作为 worker 可恢复的瞬时状态。

实际开发库和隔离运行库均已完成 0048 checksum 校正流程：先备份，再 rollback 0048，随后用正式
迁移器重新应用 0048。当前迁移总数/最高版本为 `48/0048`，0048 checksum 为
`5d3a6b1a76e41e6586e75d42946164c3d7de4cd4c2fcf4268994176f7282ac20`；开发库
`integrity_check=ok`、`foreign_key_check=0`，原有 `replayed` 死信事实与两个 owner trigger 均保留。
备份证据位于 `D:/MetaHub/.tmp/prd057/20260731T004000/db-backup/`。

## 6. 前端与静态门禁

PAGE-083 当前实现包括：

- 列表读取 `dead_letters/filters/next_cursor/request_id/trace_id`，动作读取 `dead_letter`；
- `hasMore` 只由 `next_cursor` 推导，筛选使用后端规范化回显，分页菜单由当前页安全摘要派生；
- attempt 使用 `attempt_no/created_at`，不解析或保存 payload；
- replay 对网络结果不确定保留同一内存幂等键，确定结果后释放；
- identity/navigation/generation/Abort owner 丢弃迟到结果，离页、注销、角色或 Session revision
  变化时清理状态；viewer 无导航、DOM 或预取；
- 归档确认、错误 request_id、轮询和长 ID/时间布局均有 Console 单元合同。

专项阶段的 PRD-057 Console `11/11` 与相邻 Webhook `8/8` 均通过。随后唯一一次非 Python 全量
回归直接结果为 Node 示例 `33 passed`、Console `462 passed`，合计 `495 passed / 0 failed`；
Console lint 为 `151 files passed`。日志位于
`D:/MetaHub/.tmp/prd057/non-python-full/20260731T003737/`。Browser 最终 attempt
`browser-20260730173638-ad6fd4` 已直接验证真实 DOM、焦点、双视口、截图非空像素和网络隐私，
不是用单元测试替代。

后端新增文件和迁移曾执行 Python `py_compile` 与 Ruff `I/F/B/UP`，结果通过；最后一次对本轮
修改的 replay 回归文件和严格 wire 文件执行 Ruff，结果 `All checks passed`。最新整个工作区执行
强制命令：

```powershell
C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe scripts/ci/check-comments.py
```

代码冻结后的最后一次结果为 `comment coverage ok`。

## 7. pytest 链接节点清理

后端第一批 `D:/MetaHub/.tmp/prd057-backend-targeted-final-r1` 产生 7 个 pytest `current`
SymbolicLink。逐项确认 reparse 属性和同目录目标后，只删除 7 个链接节点；7 个 `*0` 目标目录、
普通目录和测试证据全部保留。

三项缺口批次 `D:/MetaHub/.tmp/prd057-gap-basetemp-r1` 产生 3 个 pytest `current`
SymbolicLink，同样只删除 3 个节点；`RemainingLinks=0`，3 个目标目录全部保留。全过程未对
basetemp 递归删除，也未使用 `C:/tmp` 或提权。

安全定向批次 `D:/MetaHub/.tmp/prd057-security-base-r1` 产生 9 个 pytest SymbolicLink；聚焦终态
批次 `D:/MetaHub/.tmp/prd057/pytest-focused-final-r1` 产生 5 个。两处均只删除链接节点，分别保留
9 个和 5 个 `*0` 目标目录及测试证据；复核两处 `RemainingReparsePoints=0`。操作严格限定在
`D:/MetaHub/.tmp`，未使用 `C:/tmp`、未提权，也未递归删除 basetemp。

最终清理继续只删除 `D:/MetaHub/.tmp` 内逐项确认的链接节点：根级全量阶段删除 539 个
SymbolicLink 和 1 个 Junction，540 个目标全部保留；增量阶段删除 159 个 SymbolicLink，159 个
目标全部保留。两阶段目标缺失均为 0，授权可见范围内剩余链接节点为 0。22 个历史临时目录因
既有 ACL 无法遍历，未提权、未绕过，因此最终状态如实记为
`passed_with_authorized_visible_scope`，不扩张为不可证明的全局 0。两份清理证据 SHA-256 分别为
`a42b27227658337de954fc8aeb575c1657572ff5f4d64f47cbd83e4ba4c65c93` 和
`3e02ea63c106e4271d892089c43b435dd3c888c8ad95955a29fe10bc1dbf13fd`。

## 8. 最终运行验收

以下项目均已取得直接证据：

| 项目 | 必须回填的直接证据 | 当前状态 |
| --- | --- | --- |
| 代码冻结后受影响专项 | 受影响 PRD-057/加固节点 | `24 passed`；日志已收入正式 artifact |
| Python 完整回归 | 收集数、真实结果、失败 node 与定向闭环，不得虚写一次全绿 | `1496 = 1407 passed / 89 failed`；原失败 `89/89` 定向闭环，未重跑根级全量 |
| Node 示例回归 | `npm-test.log` | `33 passed / 0 failed` |
| Console 完整回归 | `npm-test.log`、`npm-lint.log` | `462 passed / 0 failed`；lint `151 files passed` |
| 实际运行库 0048 | migration count/head/hash、integrity、FK、原事实与 trigger | `48/0048`，checksum、integrity、FK 均通过 |
| Runtime | 三服务与 receiver、500 -> dead-letter -> /ok -> replayed、权限/租户/HMAC/隐私 | 通过；SHA-256 `a99a7138492e3b272938b30116b0f5a4efdcedc005b21a6a6904725bd4469070` |
| Browser | 1280x720、390x844、admin/developer/viewer、重放/归档/错误、焦点、零预取、PNG | 通过；5 张非空 PNG，SHA-256 `6e2d9bf3560d6df2099b856abd7a863517803793ffd75ba6014b5de82b274c0c` |
| 停服静止终态 | active Session、端口、spool、outbox lease、sidecar、ReparsePoint、动态敏感标记 | 通过；链接结论限定为授权可见范围，22 个既有 ACL 目录未绕过 |
| 权威证据 | artifact、各 JSON status/hash 和最终审计 hash | `artifacts/prd057/20260731T005000/`；final audit SHA-256 `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b` |

已通过的 Runtime 使用实际 Gateway、Control API、Console 和本地 receiver：四项健康检查均为
HTTP 200；receiver 连续 3 次 500 后形成唯一根死信，切换 `/ok` 后 replay 成功且保持同一
`event_id`，同幂等键回读没有重复出网；admin/developer 可用、viewer 403、匿名 401，第二租户
列表为空且跨租户 replay 同形 404；所有接收信封 HMAC 有效，公开 DTO 不含 payload、Endpoint
URL、tenant、Secret、幂等键或 lease 字段。

Browser 在 1280x720 与 390x844 共生成 5 张非空截图；根横向溢出、控件裁切、页面/Console
错误、blocked request、viewer 预取和动态敏感命中均为 0，归档对话框焦点与 Escape 恢复通过。
停服后 8000/8001/5173/18057 的 IPv4/IPv6 监听、active Session、SQLite sidecar、spool、
replay pending、active lease 和 pending replay delivery 均为 0；日志无非预期错误。

隔离运行库为 `48/0048`，`integrity_check=ok`、外键违规 0、PRD-057 trigger 9，死信终态为
`archived=1/replayed=2`。0048 checksum 为
`5d3a6b1a76e41e6586e75d42946164c3d7de4cd4c2fcf4268994176f7282ac20`。

正式 artifact 共 38 个文件，manifest 登记 36 个并逐项复核 36/36；artifact 内 ReparsePoint 与
SQLite sidecar 为 0。manifest 与运行库 SHA-256 分别为
`a3327905175b997016c49d01f50d5d1937b52a3c793c67ab33d6b1657a2cfd33` 和
`b8cb28d630dd2b52ed4f36999a493dc89b8f7e00f552e2554c7d984541534723`。

## 9. 最终结论

PRD-057 的设计、两轮编码前自审、0047/0048、后端/Console 实现、专项、一次根级全量及原失败
节点精确闭环、Node/Console 回归、实际运行库、真实 Runtime、Browser、停服静止终态和正式
artifact 均已通过。PRD-057 可标记完成，下一项为 PRD-058；这不代表全部 108 条 PRD 已完成，
`all_108_prds_complete=false`。
