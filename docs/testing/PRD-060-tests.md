# PRD-060 用户邀请与加入流程测试记录

状态：**通过**。记录日期：2026-07-31。

本记录只证明 PRD-060 的设计、实现、专项回归、数据库迁移和实际运行验收完成；全部 108 条 PRD
仍在进行，`all_108_prds_complete=false`。下一项按 Excel 两张核心表识别为 PRD-061。

## 1. 验收范围与环境

- 工作区：`D:/MetaHub`。
- Python：`C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。
- `PYTHONPATH`：`shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src`。
- pytest 的 `TEMP/TMP` 固定为 `D:/MetaHub/.tmp`，每次使用独占 `--basetemp` 和
  `-p no:cacheprovider`。
- 开发库：`D:/MetaHub/.data/metahub-dev-prd031.db`。
- Runtime：Gateway `8000`、Control API `8001`、Console `5173`，全部只访问本机 loopback。
- Browser：Microsoft Edge 无头模式，证据只写入 `D:/MetaHub/.tmp/prd060-edge-acceptance-20260731b`。
- 全程未使用 `C:/tmp`、外部网络或提权；没有创建 `.tmp/*.patch` 或 handoff 文件。
- 按冻结测试策略只执行 PRD-060 专项和直接受影响回归，没有重复根级 Python 全量或 Console 全量。

## 2. 设计与自审门禁

| 轮次 | 结论 | 主要闭环 |
| --- | --- | --- |
| 第一轮编码前自审 | 修正后通过 | `user.manage` 后端权限、tenant 来源、一次性 Token、公开同形 410、邮箱/Token 隐私 |
| 第二轮编码前自审 | 修正后通过 | 0050 Audit/RBAC 保真、强审计事务、fragment 路由、Session/刷新/Clipboard 生命周期、down guard |
| 第三轮实现期自审 | 修正后通过 | 锁后权威时间、邀请人实时授权、公开状态同形、User-Agent/path/Header 去敏、严格 UTC |
| 第四轮实现期终审 | 修正后通过 | 固定接受审计脱敏、0052 历史漂移兼容、两连接并发、commit 回滚、0051 checksum 不可变 |

设计权威文件：`docs/prd/PRD-060-user-invitation-join-flow.md`。公开合同：
`docs/api/invitations.md`。

## 3. 自动化结果

| 分组 | 直接结果 | 结论 |
| --- | ---: | --- |
| `test_prd060_db_migrations.py` | `23 passed`，`201.875s` | 通过 |
| Invitation API、路径/Header 隐私、Shared 合同三文件 | `26 passed`，`92.044s` | 通过 |
| 冻结迁移注释豁免专项 | `2 passed` | 通过 |
| 中文注释覆盖门禁 | `comment coverage ok` | 通过 |
| Console 代码冻结全量 | `491/491 passed` | 通过 |
| Console lint | `160 files passed` | 通过 |

Console 全量和 lint 在最终后端/迁移修正前已通过；此后没有前端代码修改，因此没有重复执行。最终仅重跑
0051/0052 迁移与三个 PRD-060 Python 文件，避免把无关全量测试当成修复循环。

JUnit 证据：

| 文件 | SHA-256 |
| --- | --- |
| `.tmp/prd060-migrations-restored-20260731b.xml` | `8FA4BE60126C268C6341CFE7CEBCADC08B2C57E7E574DCB2E4E0D2513EB4C3F0` |
| `.tmp/prd060-backend-restored-20260731b.xml` | `5517813FD13C0027C27529D8259F87A715A25E2058F6145D69DCC5BCA992B4FA` |

## 4. 主流程、异常、权限与安全覆盖

### 4.1 主流程

- admin 使用真实 Session 创建 24/72/168 小时 Invitation，响应只返回一次原始 Token。
- Token 数据库值只保存 SHA-256；Invitation、`user.invited` AuditLog 和 source event 同事务存在。
- 公开 accept 不带 Session，锁内创建 active User、CAS 消费 Invitation、写固定脱敏 AuditLog 和
  `invitation.accepted` source event；新用户登录后角色为指定 Developer。
- PAGE-087 创建链接、复制链接/邮件文案、公开接受表单、成功态和前往登录实际可用。

### 4.2 异常与并发

- 严格 wire 覆盖额外字段、重复 key、非法 UTF-8/Content-Type/Content-Length、过深/过宽 JSON、
  email/role/有效期/display name/password 错误。
- 未知、畸形、过期、已使用、角色/租户停用、邀请人失权和邮箱竞争统一返回
  `invitation.not_available` 410，不形成 Token 状态侧信道。
- 两线程、两 SQLite 连接真实竞争同一 Token，结果严格为一个 201 和一个 410。
- create/accept 分别注入 AuditLog、source event 和 commit 故障，Invitation/User/Audit/event 均原子回滚。

### 4.3 权限与租户隔离

- 匿名创建 401；Developer 创建 403 `auth.permission_denied`；正文畸形不能先于权限泄露合同差异。
- admin 的 `user.manage` 每次请求从当前租户数据库权限快照加载；前端按钮隐藏不是授权依据。
- 创建 tenant/actor 只从 Session 恢复；接受 tenant/email/role 只从命中 Token 的 Invitation 恢复。
- 用户、角色、Invitation、AuditLog 和 source event 使用 tenant-qualified 查询、复合 FK 或 owner trigger；
  另一租户同邮箱不冲突，跨租户 actor/role/user 关系被数据库拒绝。

### 4.4 敏感信息与链路

- create/accept 均忽略客户端 `X-Request-ID/X-Trace-ID`，使用内部链路 ID；邮箱、密码或 Token 不能
  经 Header 进入日志、响应、AuditLog 或 source event。
- 日志使用 `/invitations/{token}/accept` 路由模板；原始 Token 不进入 path 日志、诊断和异常分支。
- accept 固定不采集 User-Agent，不携带 Authorization/Cookie；响应为 `no-store/no-referrer`。
- Invitation 摘要、事件和 Webhook 只保存加盐邮箱指纹；接受审计 `actor_email` 固定为
  `invitation-acceptor@redacted.invalid`。
- Browser 证明 Token 在 URL/DOM/Storage 命中为 0，Clipboard 清理成功，外部请求为 0。

## 5. 数据库迁移验收

0051 是已经应用的迁移，不能为了中文注释再改写。最终从 PRD-060 pytest 历史证据定点恢复原始文本，
确认真正漂移只有 6 条后来补入的说明注释；7 个严格 UTC `strftime(... ) IS NOT NULL` 本来就在已应用
0051 中，没有错误迁移到 0052。

| 文件 | SHA-256 |
| --- | --- |
| `0051_user_invitation_join_flow.sql` | `4BC2793F8D825591195562415761375BA2881343E8B30B3099CFAB01712A1C11` |
| `0052_invitation_audit_privacy.sql` | `C361ED901DDC9EFD988849FDB8401BEF15A5A7FCE9089397D945CCC6F363503C` |
| `0052_invitation_audit_privacy.down.sql` | `BB72816F12FBAA1975DDD4A88975C873FD5BFC38E3B7D82F4FF42AE6E336F052` |

开发库迁移器实际结果：`applied=["0052"]`、`skipped=51`。最终数据库：

- 已应用迁移 `52` 条，最高 `0052`；0051/0052 均为 `applied` 且 checksum 与正式文件一致。
- `PRAGMA integrity_check=ok`，`pragma_foreign_key_check` 违规 `0`。
- 0052 前置依赖门禁覆盖 8 张核心表和 15 个核心 trigger；最终 trigger `15/15`。
- 实际邀请 `4` 条，全部 accepted；接受审计 `4/4` 使用固定脱敏标记，未脱敏 `0`。
- Invitation source event `8` 条，全部 dispatched，pending `0`。
- down/re-up、事实/后续版本/未知 schema 依赖原子阻断、历史用户停用/改角色/改邮箱后仍可脱敏均通过。

中文注释门禁对 0051 的 6 个历史操作采用“路径 + 精确 checksum + 精确操作文本”三重绑定豁免；
任一文本漂移会自动撤销豁免。对应专项 `scripts/ci/tests/test_check_comments.py` 为 `2/2 passed`。

## 6. 真实 Runtime 验收

运行 `scripts/acceptance/prd060-runtime-acceptance.py`，敏感凭据只通过进程环境提供且不写入证据。
实际安全汇总：

- Gateway、Control、Console 健康检查：`200/200/200`。
- 匿名创建：401 `auth.token_missing`。
- admin 创建：201；公开接受：201，`authorization_sent=false`、`cookie_sent=false`。
- 已使用 Token 重放：410 `invitation.not_available`。
- 新用户角色：`developer`，`user_manage=false`；其创建邀请：403 `auth.permission_denied`。
- admin 和受邀用户注销：`200/200`；本轮三个 Session 数据库状态均为 revoked。
- 所有成功和错误均返回非空 `request_id`；运行结论 `status=passed`。

## 7. Edge 双视口验收

权威文件：`.tmp/prd060-edge-acceptance-20260731b/browser-acceptance.json`，SHA-256：
`6D76FB62740C1DE61E83A917B6C5B029E03D64F08FC15D0FCDB7E3343CD2FFFD`。

| 状态与视口 | PNG SHA-256 |
| --- | --- |
| 邀请弹窗 1280x720 | `D0409407856C94CBF6A1DF2BC344CD61DF054873A5C60E597FD8014358B090BE` |
| 邀请弹窗 390x844 | `2B845045FB63835F50E3827A274C18F766F71F4699695053D9C56CA7E6D1686A` |
| 公开接受表单 1280x720 | `A0C6C3FDE72B0A0EB4B8FDCE18813E30279C147BC460DB260B88B590A9E47CD9` |
| 公开接受表单 390x844 | `5DA77299E52509BC95AE09D5E6A67F055EA3F8776244FE3C0CC296F95F9E06FD` |
| 公开接受成功 1280x720 | `43E0FFC1099D157AE435DB2DD7E263101BA9EE3B5B7E7E04063ED19BBCEFBD92` |

五张 PNG 均非空，采样颜色数有效；页面无横向溢出、目标均在视口内、控件文字溢出 `0`。登录、
创建、接受、注销为 `200/201/201/200`；Clipboard 链接验证、地址 scrub、本地密码确认和注销清理通过；
阻断请求、外部请求、page error、console error 均为 `0`。

首次命令在加载模块阶段发现 Codex Node 运行库的 `NODE_PATH` 未包含已安装 `playwright-core`，当次没有
创建证据目录或启动浏览器。补入同一只读运行库的 `.pnpm/node_modules` 后重跑通过；没有安装依赖、
联网或提权。内置浏览器连接也因桌面运行时内部资产路径缺失未启用，不影响正式 Edge 验收结果。

## 8. 静止终态

- 本轮 Gateway、Control 和既有 PRD-060 Console 进程均已停止；8000/8001/5173 无监听。
- `PRAGMA wal_checkpoint(TRUNCATE)` 返回 `busy=0/log=0/checkpointed=0`。
- 开发库 `-wal`、`-shm`、`-journal` 均不存在。
- Invitation pending source event `0`，未脱敏接受审计 `0`。
- Runtime/Browser 创建的三个 Session 均 revoked；数据库中另有一个接手前既存 active Session，未越权
  修改既有业务会话，不影响本轮凭据清理结论。
- 最终迁移栈仍为 `52/0052`，完整性 `ok`、外键违规 `0`。

## 9. 最终结论

TC-0119 主流程和 TC-0120 异常/权限/隐私流程均通过。设计、四轮自审、实现、中文注释、租户隔离、
后端权限、统一错误码与 request_id、0051/0052、专项和直接回归、真实 Runtime、Edge 双视口及静止终态
全部满足完成门禁。PRD-060 可以标记完成，但不代表全部 108 条 PRD 已完成。

