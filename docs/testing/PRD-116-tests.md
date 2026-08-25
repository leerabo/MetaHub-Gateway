# PRD-116 测试记录

状态：已完成。实现、自动化、隔离数据库、本地真实认证和受控延迟恢复 Chromium 全流程均已通过。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0107 -> 0108 -> 0107 -> 0108`、fresh/current head、重复执行、意外 schema up guard、WebAuthn/recovery/device/policy/grant/revoke generation down guard、`ready_at` 约束 |
| Identity/API | 生产 CORS grant header、TOTP/WebAuthn enrollment、challenge owner/origin/RP/browser binding、replay/counter、恢复码单次消费、trusted device、Session 撤销、step-up owner/action/resource/permission、24 小时受控恢复与 48 小时总窗口 |
| Conditional access | `conditional_access.read|manage` 权限、Workspace Cookie/CSRF、simulation、CAS、audit/enforced guard、Owner MFA/recovery guard、grant 单次消费 |
| Console | strict request/response、WebAuthn adapter、恢复码与延迟恢复证明清理、Global Session 轮换、Workspace 上下文清理、条件访问 grant、DOM/Storage 零敏感值、响应式样式 |
| Compatibility | PRD-091/095/114/115 Identity、Platform、Enterprise Identity、SCIM 与迁移回归；PRD-115 固定测试时钟改为运行时取值，消除超过 5 分钟后的 projection claim 过期顺序依赖 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-116 Migration | `4/4 passed` |
| PRD-116 Identity/API | `2/2 passed` |
| PRD-116 Shared SDK | `1/1 passed` |
| PRD-116 Console 专项 | `11/11 passed` |
| PRD-095/114/115/116 Shared SDK 回归 | `10/10 passed` |
| PRD-091/095/114/115 API 回归 | `14/14 passed` |
| PRD-091/095/114/115 Migration 回归 | `17/17 passed` |
| Console 全量 | `1093/1093 passed` |
| Console lint | `358 files` 通过 |
| PRD-116 Python Ruff | `All checks passed` |
| `git diff --check` | 通过；仅保留既有行尾转换提示 |

## 受控恢复证据

- `begin_recovery` 必须同时通过当前密码、Global Session、CSRF 和浏览器 Cookie 绑定，并要求活跃 MFA
  与未使用恢复码。通知发送成功后，绑定 Session/Cookie 才延长至挑战失效时间。
- 当前密码只能发起恢复，不能完成恢复。挑战在 24 小时冷静期前固定返回 not-ready；48 小时总窗口内
  必须同时提交通知码和未消费恢复码，错误证明使用统一失败合同。
- 完成事务消费挑战和恢复码，撤销旧 MFA/WebAuthn、恢复码、受信设备、Workspace/Global Session 和
  step-up grant，并签发新的 `recovery` assurance Global Session；恢复完成后可重新登记因素。
- 真实受控恢复在 `ready_at` 尚处于 24 小时冷静期内固定返回 `identity.recovery_not_ready`；仅在受控地把
  `ready_at` 调整到过去且保持 `expires_at` 在未来后继续完成双证明，不绕过 48 小时总窗口。
- 恢复前数据库中有 Global Session `12`、Workspace Session `2`、活跃因子 `1`、活跃恢复码 `10`、
  受信设备 `1` 和 step-up grant `1`；恢复后分别为 Global Session `1`、Workspace Session `0`，其余
  活跃因子、恢复码、恢复码集、受信设备和 grant 均为 `0`。旧 Session 已撤销，恢复挑战已消费。
- 新 Global Session 已轮换为 `assurance=recovery`、`auth_method=recovery_code`，页面刷新后 Workspace
  上下文仍为空，必须显式重新进入 Workspace。
- `identity.mfa_changed` 事件和 outbox 只保存固定状态、时间、reason 与 owner，不保存通知码、恢复码、
  密码、Cookie 或 token。
- Console 提交后立即清除密码、通知码和恢复码；完成后重载轮换后的 Global Session 并清除 Workspace
  上下文。Passwordless login 不属于 PRD-116，测试没有扩展登录合同。

## 本地认证与数据库

- 使用全新隔离数据库应用到 `0108`，`PRAGMA integrity_check=ok`、外键异常 `0`，八类 PRD-116 核心表
  均存在。
- 测试进程直接读取 `infra/seeds/default_seed.json`，在内存中提交本地账号与密码；live 登录和
  `GET /identity/security` 均返回 `200`。Cookie 只在单个进程内构造，使用后立即释放。
- 凭据、Cookie、CSRF、TOTP、通知码、恢复码、grant 和 token 未写入命令、日志、截图、文档或提交。

## Chromium 证据

- 核心真实 Chromium 流程已完成本地登录、Workspace 切换、虚拟 WebAuthn authenticator enrollment、
  一次性恢复码对话框关闭与 DOM 清理、WebAuthn step-up、conditional access PATCH 和 simulation。
- 同一隔离数据库已完成受控恢复冷静期拒绝、双证明完成、Global Session 轮换、旧身份安全状态撤销和
  Workspace 跨刷新保持清空；恢复码只保存在进程内存，使用后立即清除。
- `1440x900`、`1280x720`、`390x844` 三视口的横向溢出、控件越界、控件重叠、文本溢出和 broken image
  均为 `0`；Console error、Page error 和本地 failed request 均为 `0`，敏感输入为空且无打开 Dialog。
- 三视口均未出现已停用因子的意外“确认停用”表单，证明空选择值不会被错误转换为首个因子索引。

## 敏感扫描

- PRD-116 差异中的本地 seed 账号/密码、完整恢复码、SCIM Bearer 和 Private Key 格式命中均为 `0`。
- 隔离服务日志中的本地 seed 账号/密码命中为 `0`。
- 隔离数据库原始字节中的本地 seed 密码和完整恢复码命中为 `0`。
- Console 单测不保存固定恢复码 fixture；恢复码只在测试运行时按无歧义字母表拼装，源码字面量命中为 `0`。

## 批次门禁

PRD-116 不执行 S4 G4。G4 必须保留到 PRD-119 独立提交完成后统一执行；当前仍为
`saas_incremental_roadmap_complete=false` 和 `all_108_prds_complete=false`。
