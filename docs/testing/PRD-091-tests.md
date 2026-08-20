# PRD-091 测试记录

状态：已完成。下一未完成项经 Excel、README、`docs/prd` 与 `docs/testing` 交叉确认是
`PRD-092 - 个人与企业用户自助注册验证`。本条不实现注册、Workspace 或平台角色。
`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| WB/API/BB/GR/SEC/FLOW | `test_prd091_identity_api.py`: 8 passed | 登录、me、双设备、100路幂等撤销、改密、失败时延包络、旧Auth、Origin/CSRF、混合凭据、严格JSON、Global Cookie不能访问tenant API |
| DB/GR/SEC/FLOW | `test_prd091_db_migrations.py`: 6 passed | 空库0001..0083、seed幂等、Hash保真、异常Hash隔离、冲突隔离、pristine down/up、安全事实down guard、完整性/FK |
| WEB/UI/UCD/COMP/FLOW | Identity Node专项16 passed；旧Auth 5 passed；Console lint 306 files；真实浏览器通过 | 严格DTO、Storage零写入、迟到响应、键盘/焦点、390/1280/1440合同；真实390x844与1280x720登录/安全摘要/设备/改密布局，无横向溢出和Console错误 |
| PERF/COMP | lifecycle 6 passed；idle polling 2 passed；真实loopback采样通过 | 连接复用、请求后rollback/状态复位、空轮询不取写锁；warm登录12次p95 279.684 ms，Session 50次p95 21.898 ms |
| Static/WB | 增量 Ruff、`py_compile`、`git diff --check` | Identity后端、共享连接/配置/安全边界、空闲轮询和测试语法 |
| Planning | validator 84 checks、planning pytest 4 passed | Excel连续完成前缀切至091；下一项092；两个总体状态保持false |

## 迁移验收

- 空 SQLite 实际应用 83 条迁移，最高版本 `0083`。
- seed 后 `GlobalAccount/AccountCredential/AccountIdentityLink` 各 1 条，冲突 0。
- 重复 seed 不再随机轮换同一密码 Hash，Global Credential 与 legacy User 字节一致。
- 同邮箱跨 tenant 即使 Hash 相同也不合并，两个账号全部进入 conflict/login disabled。
- `PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check` 违规 0。
- pristine 0083 可回到 0082并确定性重上；存在 GlobalSession 或 IdentityEvent 时原子拒绝 down。

## 安全与隐私结论

- GlobalSession、Legacy Token、Workspace/Platform 身份域不互相回退；Global Cookie 不授予企业权限。
- Session 只保存 HMAC，密码升级和轮换使用 Argon2id；旧 PBKDF2 仅用于迁移兼容验证。
- 登录输入、Cookie、CSRF、密码、Token、完整 IP/UA 在数据库普通字段、事件、Outbox、日志、DOM 和 Storage 中零明文命中。
- 账号本人只能列出和撤销自己的会话；撤销提交后下一次权威数据库校验立即失败。
- 身份请求不产生模型用量或成本，标记为 N/A；成功、失败、撤销和轮换由 IdentityEvent/Outbox 持久追踪。

## 性能门禁

- 直接复现确认瓶颈是完整 83 版 SQLite schema 的首次连接解析，而不是 Session SQL：新连接首次
  JOIN 中位数约 170 ms；已热 schema 查询低于 1 ms。
- Identity worker 使用容量 4 的线程/KDF边界和最多4个空闲数据库handle；handle不跨并发请求共享，
  归还前清理事务和连接级安全状态，池满时不在事件循环等待。
- 真实 Control API Socket 聚合结果：warm Argon2id登录 `12` 次，p95/max=`279.684/279.684 ms`；
  warm `/identity/me` `50` 次，p95/max=`21.898/30.025 ms`。门限分别为 `<500 ms` 和
  `<30 ms` p95；服务端相同 Session 样本日志为 `2..6 ms`。
- 首次登录会同时解析 schema 并把 legacy PBKDF2 原子升级为 Argon2id，属于迁移兼容冷路径，未混入
  warm 样本；其安全事实、Session和Outbox仍在同一事务。

## 敏感审计

- 隔离0083验收库对 seed明文密码、Legacy Token根密钥和Identity Session根密钥逐字节扫描，三类
  canary均为0命中；数据库只保留密码Hash和Session/CSRF HMAC。
- API专项同时扫描caplog、SQLite dump、响应DTO和事件/Outbox；登录标识、密码、Cookie、CSRF、
  完整IP/UA、tenant、role和permission未进入禁止载体。
- 真实浏览器DOM未出现tenant/workspace/issuer/token/account_id/session_id/role/permission，390和
  1280视口无越界，浏览器Console warning/error为0。
