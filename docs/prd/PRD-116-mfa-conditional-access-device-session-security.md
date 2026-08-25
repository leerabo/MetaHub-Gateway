# PRD-116 MFA、条件访问、设备与会话安全

状态：已完成。依赖 PRD-091/095/110/114；范围以 Excel PRD-116 为准。

权威边界：Passwordless login 不属于 PRD-116，不在本条新增登录发现、无密码登录或账号恢复旁路。

## Excel 权威合同

- API：`POST /identity/mfa/enrollments`、`POST /identity/mfa/challenges`、
  `GET/DELETE /identity/sessions`、`GET/PATCH /workspaces/{id}/conditional-access`、
  `POST /identity/step-up`。
- 对象：`MfaEnrollment`、`WebAuthnCredential`、`RecoveryCodeSet`、`TrustedDevice`、
  `ConditionalAccessPolicy`、`StepUpGrant`。
- 事件：`identity.mfa_changed`、`identity.session_revoked`、`conditional_access.denied`、
  `step_up.completed`。
- SLO：Session 校验 p95 < 30ms；MFA 本地验证 p95 < 300ms；撤销传播 < 2s。

## 1. 产品闭环

现有 TOTP enrollment 和 global sessions 是权威基础。本条统一 `/identity/mfa/enrollments`，增加 WebAuthn
注册/认证、单次 recovery codes、trusted devices、完整 Session 列表/撤销、Workspace conditional access
和通用 step-up。用户管理本人因子；security_admin 管理企业策略但不能读取凭据；Platform Security 只管理
平台员工策略。高风险后端动作显式依赖一次性 StepUpGrant。

## 2. 0108 数据演进

- 扩展 `account_mfa_factors` 支持多个 active `totp|webauthn`，TOTP 密文 owner/AAD 不变；WebAuthn 公钥、
  credential ID hash、AAGUID、sign count 独立表保存，不存 biometric/attestation 原文。
- `mfa_challenges`：purpose、account/session、challenge hash、origin/RP、浏览器绑定、attempt、单次消费、TTL。
- `recovery_code_sets/codes`：set revision 与逐码 Argon/PBKDF hash；明文只在创建响应显示一次。
- `trusted_devices`：account、device HMAC、公开 label/device family、trust level、expires/revoked；不存原始 IP/UA。
- `conditional_access_policies`：Workspace 单例、`audit|enforced`、MFA/step-up、固定 region/network/IP rule、
  exception IDs、revision 和 simulation watermark。
- `step_up_grants`：account、global/workspace session、action/resource、permission fingerprint、auth method、
  `active|consumed|expired|revoked`、短 TTL。
- global/workspace sessions 增加 revoke generation、assurance level 和 trusted device 引用；旧 Session 回填为
  password assurance，不自动满足 MFA。

Down 在 WebAuthn/recovery/device/policy/grant 或新 generation 事实存在时拒绝，不删除既有 TOTP 或 Session。

## 3. WebAuthn、恢复码和 step-up

WebAuthn 使用固定 RP ID/origin、server challenge、user verification required、ES256/RS256 allowlist、唯一
credential ID、RP ID hash、flags、sign count 和单次 challenge。拒绝 attestation 中任意网络取证、未知扩展、
克隆 sign count 回退和跨 origin/RP replay。

Recovery code 高熵、固定格式、逐码 hash、一次消费；重生成使旧 set 全部失效。失去全部因子走受控 recovery
challenge：当前密码只能发起恢复，不能单独完成；挑战绑定 Global Session、CSRF、浏览器 Cookie、活跃 MFA
和未使用恢复码，通知成功后进入 24 小时冷静期，并在 48 小时总有效期内同时校验通知码和未消费恢复码。
完成时原子撤销旧 MFA/WebAuthn、恢复码、受信设备、Workspace/Global Session 和 step-up grant，再签发
`recovery` assurance Global Session 供重新登记因素；不提供管理员直接关闭 MFA。Step-up 绑定当前 Session、
Workspace、permission revision、action、resource 和 5 分钟内一次消费；密码/TOTP/WebAuthn 方法按策略满足
不同 assurance。

## 4. 条件访问执行点

策略在 Workspace switch、Workspace Session 每次认证和高风险动作执行前评估。IP 条件只接受可信连接的
peer 地址或经配置代理签名的 client address；不信任普通 `X-Forwarded-For`。region/network 来自服务端
受控分类，不接受浏览器自报。`audit` 只记固定 outcome；切换 `enforced` 前必须有近期 simulation、Owner
active MFA 和安全恢复路径。

撤销 Session、factor、device、Membership 或 policy revision 时提升 generation；所有进程在下一请求从数据库/
短 TTL revocation snapshot 复核，目标 <2s。依赖损坏时高风险操作失败关闭。

## 5. Console

Identity Security 页面显示因子类型/状态、一次性 recovery codes、受信设备、Session、延迟恢复状态和撤销动作；
通知码、恢复码和密码提交后立即清除，恢复完成后重载轮换后的 Global Session 并清除 Workspace 上下文；敏感值
不进入 snapshot/localStorage。Workspace Security 页面显示 conditional access simulation、规则、影响摘要和启用确认。
Platform Security 使用独立平台入口，不能读取租户设备、IP、credential 或 recovery code。

最终浏览器验收进一步确认：`recovery` assurance 不会自动恢复单一 Workspace，页面刷新后仍必须显式重新进入；
已停用因子也不会因空选择值被误判为索引 `0`，因而不会自动展开“确认停用”表单。

## 6. 第一轮自审：认证器与策略输入

- P0：浏览器自报 IP/region 可绕过条件访问。修正为 peer/签名代理和服务端 region 分类。
- P0：WebAuthn 只验签不验 challenge/origin/RP 可跨站重放。修正为完整绑定和单次 challenge。
- P0：recovery code 明文集合落库。修正为逐码强 hash，一次响应后清除内存/DOM。
- P1：多个因子仍使用单 active 唯一索引。修正为按 `(account,factor_type,credential)` 唯一并允许多 active。
- P1：security_admin 读取凭据用于排错。修正为只见类型、时间、状态和固定失败类别。

## 7. 第二轮自审：撤权与恢复

- P0：Step-up token 不绑定 action/resource 会被横向复用。修正为完整 owner、permission fingerprint 和单次消费。
- P0：策略启用后 Owner 无 MFA 会锁死。修正为 simulation + Owner assurance + recovery 前置 guard。
- P0：Session 撤销依赖长 TTL cache 会超过 2s。修正为 generation fence 与最多 1s revocation snapshot。
- P1：trusted device 变成永久 MFA bypass。修正为有界 TTL、风险变化失效，且高风险动作仍要求 step-up。
- P1：factor reset 静默执行会掩盖接管。修正为延迟、通知、撤销全部 Session 和不可变审计。

## 8. 专项验证结果

0107/0108 fresh/往返/down guard；TOTP 兼容与并发 counter；WebAuthn registration/assertion/replay/cloned counter；
recovery code 单次/并发；trusted device TTL；条件策略 audit/enforced 与可信代理；Session/factor/Membership 动态撤权；
step-up owner/action/resource/permission 绑定；p95 和 <2s 传播；Identity/Workspace/Platform 页面隔离与敏感扫描均已
通过专项或必要回归。完整命令、数量、本地认证、浏览器终态和敏感扫描见 `docs/testing/PRD-116-tests.md`。

## 9. 实现落点

- `infra/migrations/0108_mfa_conditional_access_device_session_security.sql/.down.sql`：MFA 多因子、WebAuthn、
  恢复挑战与恢复码、受信设备、条件访问、step-up grant、Session assurance/revoke generation、权限矩阵和
  非 pristine down guard。
- `control-plane-api/src/metahub_control/identity/security_service.py` 与 Identity 路由/模型：统一 enrollment、
  WebAuthn challenge、受控恢复、设备、step-up、Session 列表与撤销；恢复证明不进入事件或 outbox。
- `control-plane-api/src/metahub_control/workspace_access/conditional_access.py` 与 Workspace 路由/服务：
  `conditional_access.read|manage` 后端授权、simulation、CAS 更新、策略 guard 和一次性 step-up 消费。
- `console-web/src/lib/{identity,conditionalAccess}.js` 与 Identity 页面：账号安全、条件访问、延迟恢复、
  敏感输入生命周期和桌面/平板/移动端布局。

## 10. 批次边界

PRD-116 只交付 Excel 冻结的 MFA、恢复码、条件访问、设备、会话和 step-up；Passwordless login 留给后续独立
权威需求，不借 WebAuthn enrollment 扩展登录合同。下一权威项为 PRD-117。S4 G4 继续保留到 PRD-119 独立提交
完成后统一执行，PRD-116 完成不提前触发 G4。
