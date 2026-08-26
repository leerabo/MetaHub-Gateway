# PRD-118 测试记录

状态：已完成。Service Account、OAuth client、短期 opaque token、机器鉴权、可信网络约束、轮换/撤销、
写操作幂等重放、Console 一次性凭据清理和本地真实 Owner 验收均已覆盖。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0109 -> 0110 -> 0109 -> 0110`、fresh head、重复执行、意外 schema guard、业务事实 down guard、tenant deletion manifest hash |
| Machine identity API | owner/security_admin 权限、machine-safe scope、Workspace owner、OAuth client credentials、opaque token、audience/TTL/generation、route allowlist、IP/mTLS 可信来源、同形失败、审计/outbox |
| Idempotency | 五类写操作强制 `Idempotency-Key`、同请求安全重放、不同请求指纹冲突、CAS 保留、并发单提交、secret/token 明文不落库 |
| Rotation and revoke | 有界 grace、旧 generation 拒绝新增 token、grant 同事务撤销、并发撤销序列化、传播时间小于 2 秒 |
| Console | Owner 可见性、Service Account/OAuth client 创建、一次性 secret Dialog、关闭/错误/导航清理、重试 key 仅驻留内存、响应式布局 |
| Compatibility | PRD-094/115/116/117 身份、Workspace Session、SCIM、条件访问、数据治理和迁移夹具回归 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-118 Migration | `4/4 passed` |
| PRD-118 Machine identity API | `6/6 passed` |
| PRD-094/115/116/117 后端受影响回归 | `40/40 passed` |
| PRD-118 Console 专项 | `4/4 passed` |
| Console 受影响回归 | `27/27 passed` |
| Enterprise identity / SCIM Console 回归 | `10/10 passed` |
| Console 全量 | `1102/1102 passed` |
| Console lint | `362 files` 通过 |
| Python Ruff | `All checks passed` |
| `git diff --check` | 通过；仅保留既有 CRLF 转换提示 |

## 本地 Owner 与浏览器证据

- 页面复用现有 `MetaHub Admin / Owner` 会话；需要重新认证时直接从 `infra/seeds/default_seed.json` 读取本地
  测试凭据并仅在内存中使用，不请求人工确认，也不回显密码。
- Owner 可见“机器身份”，已创建受 IP allowlist 约束的 Service Account 和 OAuth client；client secret 只在
  一次性 Dialog 中完整出现。关闭后 Dialog 与完整 secret 均从 DOM 清除。
- 重复提交同名 Service Account 固定显示业务冲突，表格仍只有一条记录，错误状态下没有旧 Dialog 或完整
  secret 残留。轮换、grace、token generation 和撤销由真实 API 集成测试覆盖。
- `390x844`、`1280x800`、`1440x900` 三视口中，页面横向溢出、机器身份区域越界和控件文本溢出均为 `0`；
  移动端表单为单列，桌面端为四列。Console warning/error 为 `0`，临时 viewport 已复位。

## 安全与敏感数据

- 数据库仅保存 client secret/access token 的 HMAC/hash、固定长度 prefix 和命令结果引用；审计、outbox、
  request log 和证据不保存 Authorization、明文 secret/token、请求正文或原始证书材料。
- Console 不把一次性凭据或幂等重试意图写入 local/session storage；关闭、确定失败、导航和身份切换会清空
  DOM/state。
- 源码、文档、差异和本地测试产物执行高风险访问密钥、Private Key、长 Bearer、seed 密码和一次性凭据扫描；
  PRD-118 提交不得包含本地测试数据库、profile、服务日志或 pytest 临时目录。
- 当前本地开发数据库只读扫描覆盖 `555` 张业务表、`3308` 行值；seed 密码和完整 OAuth client secret、
  access token、长 Bearer、Private Key 模式命中均为 `0`。源码/文档扫描覆盖 `36` 个 PRD-118 文件，命中为 `0`。

## 批次门禁

PRD-118 不执行 S4 G4。Edge/Firefox、完整 S4 业务流、批次级数据库/性能/安全回归和 G4 保留到 PRD-119
独立提交完成后统一执行；PRD-118 仍保持独立设计、测试记录、敏感审计、Excel 状态和提交。
