# PRD-092 测试记录

状态：已完成。Excel、README、`docs/prd`、迁移和本记录共同确认下一未完成项为
`PRD-093 - 个人空间与企业Workspace创建`。本条只交付 Global Identity 自助注册、邮箱验证、
重发和密码找回；不创建 Workspace、Membership、WorkspaceSession 或平台角色。
`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| WB/SEC/Config | Identity PII crypto + config `15 passed` | AES-256-GCM round-trip、intent/purpose/field/version AAD、tag/owner/key/format 同形失败、prod 非默认密钥、跨 Auth/Session/Challenge/Cache/File 域禁止复用、repr/model dump 脱敏 |
| API/BB/GR/SEC/FLOW/PERF | `test_prd092_identity_self_service.py`: `11 passed` | 新/已有/sink 注册、Invitation binding、重置撤权、100 并发单次消费、generation 重发、严格 JSON/Origin/Fetch Metadata/Bearer、渠道失败、密文篡改、限流先于 KDF、warm 性能 |
| DB/GR/SEC | `test_prd092_db_migrations.py`: `2 passed` | 空库 0001..0084、repeat、pristine down 到 0083、reapply、不可逆事实原子阻断、完整性/FK、注册表只有 `email_ciphertext` 无规范邮箱明文列 |
| COMP/FLOW | PRD-091 Identity API `8 passed`；PRD-060 旧 Invitation 主流程 `1 passed` | N/N-1 全局登录/Session/改密与旧 `/invitations/{token}/accept` 保持可用 |
| WEB/UI/UCD/COMP | Identity Node `19 passed`；Console lint `306 files`；真实 Browser 通过 | 注册/验证/找回状态机、Abort/迟到响应、密钥字段清理、Storage 零调用、键盘/焦点/读屏合同、390/1280/1440 既有截图及当前 1280 注册-重发-验证复验 |
| Static | 定向 Ruff、`py_compile`、`git diff --check` | Shared Identity crypto/config、Control Identity、迁移和测试语法/格式 |
| Planning | generator、roadmap validator、planning pytest | 连续完成前缀推进至 092；AbuseCounter 为 tenant-free Identity owner；0084 归属明确；下一项 093；两个总体状态保持 false |

## 迁移与数据验收

- 空 SQLite 实际应用 84 条迁移，最高版本 `0084`；repeat 无重复应用，pristine 可回到 `0083`
  并确定性重上。
- `registration_intents` 只保存 email HMAC 和 `email_ciphertext`。密文信封绑定 registration
  purpose、`rgi_` intent、`email_normalized` 字段类型和 key version 1。
- 存在 abuse bucket、intent/challenge、verification、consent、binding 或 lifecycle 安全事实时，
  0084 down 原子失败，不删除已注册账号、恢复旧密码或复活旧 Session。
- 验收终态 `PRAGMA integrity_check=ok`、`PRAGMA foreign_key_check` 违规 0。

## 安全与隐私结论

- 低熵验证码使用独立 Challenge 根密钥 HMAC；原始验证码不进入 URL、响应、普通数据库列、
  Event/Outbox 或日志。投递事件仅含内部 challenge ID、purpose、generation、schema version 和 target HMAC。
- 未验证邮箱使用独立 PII 根密钥 AES-256-GCM。格式、owner、tag 或密钥失败固定映射为
  `503 identity.unavailable`，事务回滚且 Challenge 保持 pending，不能形成密码学 oracle。
- 注册邮箱、IP、设备只以分域 HMAC 参与持久限流；公开限流在 Argon2id KDF 前执行，存储故障失败关闭。
  新账号的已接纳请求只执行一次 Argon2id hash，已有账号只执行一次密码 verify；sink intent 使用固定且
  不可认证的非空 Argon2id 占位，不额外派生或持久化错误密码。
- Invitation 验证只写 `verified_pending_membership` binding；旧 Invitation 仍 pending，User/Membership
  不新增。成功注册只签发 GlobalSession，不授予 tenant/workspace/role/permission。

## 浏览器与性能

- 既有截图位于 `artifacts/prd092/browser/`，覆盖 `390x844`、`1280x720`、`1440x900`，无横向
  溢出或 Console warning/error。PII 加密接入后又在真实 `1280` 视口完成注册、generation 2 重发和
  验证，URL 始终为 `/identity.html`，终态 document width `1265 <= 1280`，密码字段全部清空。
- 当前真实 Socket 冷注册/重发/验证单样本分别为 `593/45/53 ms`；冷注册包含首次完整 schema 解析，
  不作为 warm p95。自动化 20 个 warm 注册与 20 个验证样本均满足 nearest-rank p95 `<500 ms`，
  避免不足 20 个样本时 p95 退化为最大值；40 个持久 abuse
  bucket warm 样本满足 p95 `<30 ms`。
- 每个 abuse scope 通过单条 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` 原子累计并返回封禁状态，
  不再先查询后写入；限额、5 分钟窗口、持久封禁和失败关闭语义保持不变。优化后 PRD-092 完整专项
  `11 passed`，PRD-092..094 统一 Python 门禁 `69 passed`。
- Identity 流程不产生模型 Usage 或成本账，标记为 N/A。

## 敏感审计

- 隔离验收库 `identity-pii-acceptance-0084-r1.sqlite3` 在未验证阶段扫描 SQLite 主文件、WAL 和 SHM：
  新邮箱、候选密码、seed 密码、Auth/GlobalSession/Challenge/PII 四类根密钥逐字节命中均为 0。
- generation 2 原始验证码在提交前和验证后均为 0 命中；验证后密码、验证码和四类根密钥继续为 0。
  正式 GlobalAccount 按 PRD-091 保存已验证邮箱，RegistrationIntent 仍只有认证密文。
- 真实终态为 GlobalAccount `2`、legacy User `1`，证明注册没有创建 tenant authority；Challenge 状态为
  `(1,superseded),(2,consumed)`，Event 为两次 dispatched 与一次 registered，payload 字段白名单通过。

## 2026-09-08 原生注册限流修复

PostgreSQL 实际 HTTP 注册在持久限流清理处报 SQLSTATE 42703（UndefinedColumn），根因是
identity_abuse_buckets 查询依赖 SQLite rowid。两处键引用改为既有 identity_abuse_bucket_id，
不改过期窗口、每笔 50 条清理上限、四维计数、持久封禁和事务边界。

独立证据均位于 .artifacts/g6-security-20260907：

- prd092-abuse-primary-key-red-r1-junit.xml：5 fixture errors；默认 Windows pytest 临时
  目录权限拒绝，未执行产品断言。随后改用工作区内独立 basetemp，没有改 ACL。
- prd092-abuse-primary-key-red-r2-junit.xml：4 failed / 1 passed，2.17s。无 rowid 表上
  注册、验证、重置清理及累计均复现 identity.unavailable；这是查询 helper 反例，不是模拟认证。
- prd092-abuse-primary-key-r3-junit.xml：5/5，2.02s。三种操作各保留未过期行并分两次清理
  51 条过期行，四维计数持续累计；INSERT 故障实际进入写语句后回滚删除；第六次超限仍提交
  attempt_count=6 与 blocked_until，再返回 identity.rate_limited。
- prd092-abuse-primary-key-api-r4-junit.xml：1/1，59.34s。既有 API 测试验证五次接纳、
  第六次 429 且 KDF 只调用五次。此测试刻意替换 KDF 计数，不作为真实密码认证证据。
- g6-page-visits-r3-bfb6cf1a6cae426b834463458115d780-junit.xml：原生库修复前注册 503，
  安全诊断只含 SQLSTATE 与代码位置，1 error / 2 deselected / 71.06s。
- g6-page-visits-r4-a81dee8b361a4c2da1549c3de6851bd3-junit.xml：修复后实际注册已进入验证
  阶段；三项 fixture 在 /identity/verifications 另一处 SQLite-only schema 查询失败，
  SQLSTATE 42P01，3 errors / 111.35s。因此本记录不声称整条原生身份/Workspace 链路通过。

Ruff 与 diff 检查通过。该限流修复独立于 PRD-147 页面事件提交；原生 Session/MFA 能力发现、
Workspace 创建和后续页面事件仍由各自批次继续处理。敏感审计核对本次四项源文件、上述六份
原始报告及保护基线；模式扫描不等于独立安全会签，不修改全产品门禁或 Excel 完成状态。
