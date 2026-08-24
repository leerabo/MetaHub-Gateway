# PRD-110 注册、API 与资源反滥用及风控

状态：Excel 复核、两轮编码前自审、专项实现与验证均已完成（2026-08-24）。依赖 PRD-091/092/098/106/109，范围以 Excel PRD-110 为准。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`POST /risk/challenges`、`GET /platform/risk/cases`、`POST /platform/risk/cases/{id}/decisions`、
  `POST /risk/appeals`。
- 核心数据对象：`RiskSignal`、`RiskDecision`、`AbuseCounter`、`Challenge`、`RiskCase`、`RiskAppeal`。
- 事件：`risk.challenge_required`、`risk.workspace_restricted`、`risk.appeal_resolved`。
- 性能/SLO：`风控判断p95<30ms`、`攻击流量下登录/网关正常用户p95增幅<20%`。

## 1. 目标与边界

交付注册/IP/设备/账号/Workspace 多维限流、挑战升级、资源创建配额、异常调用评分、凭据填充防护、自动限制、
人工复核、申诉和误伤恢复。风控只能限制或要求额外验证，不能授予 Workspace/平台权限。原因对用户可解释但不泄露
阈值、规则权重或可绕过信号。本条不实现通用欺诈平台、营销画像或设备指纹追踪。

## 2. 0102 数据与隐私

- `risk_signals`：scope、signal type、severity、time bucket、HMAC subject/IP prefix/device pseudonym、evidence hash；无原始 IP、
  User-Agent、邮箱、电话或请求正文。
- `abuse_counters`：Redis 原子实时计数的持久水位/重建事实；key 固定域隔离，TTL 和上限受配置约束。
- `risk_decisions`：`allow|challenge|required_review|restricted`、固定 reason family、policy version、expires/revision；不可变历史。
- `risk_challenges`：challenge kind、owner、token hash、attempt/expiry/status；Secret 仅一次返回且不入日志。
- `risk_cases`：自动限制与人工复核状态 `open|under_review|restricted|cleared|closed`，复合 Workspace owner 或 identity domain。
- `risk_appeals`：当前用户可见的申诉、固定类别、状态与 decision；自由文本不进入规则/事件，若保留则 envelope 加密并限权。
- 权限 `platform.risk.read/decide/support_read`：security 决策，owner 最终管理，support 只读脱敏原因；普通用户仅处理本人挑战/申诉。
- down 发现 decision/case/challenge/appeal 或 active restriction 即拒绝。

## 3. 判定、故障策略与恢复

判定顺序固定：现有硬权限/账号状态 -> active restriction -> 分布式 counter -> risk score -> challenge。所有登录/注册
枚举场景返回同形状态、固定文案和近似时间预算；内部是否存在账号只进入受限审计。

Redis 使用原子脚本维护窗口；DB 保存 policy/decision/case 和可重建水位。Redis 故障时：注册、登录恢复和资源创建等
权限扩大动作 fail closed 到通用 challenge/稍后重试；已认证 Gateway 流量继续受现有硬 rate limit/预算/权限约束，风控
增强暂时 fail open 但记录固定降级计数，不能把遥测故障扩大为全局停服。

自动限制使用可过期 overlay，不直接改角色、membership 或 entitlement；PRD-106 job 负责到期/复核/重建。清除或申诉
通过只终止 overlay，保留不可变 signal/decision/case。正常用户基线不执行外部网络调用。

## 4. API 与事件

- `POST /risk/challenges`：当前受控 intent 交换/完成挑战；owner 从 session/intent 恢复，拒绝 caller tenant/account。
- `GET /platform/risk/cases`：security/owner 分页读取脱敏 case；support 使用独立裁剪 DTO。
- `POST /platform/risk/cases/{id}/decisions`：CSRF、idempotency、revision、固定 decision/reason；不能附带权限。
- `POST /risk/appeals`：当前用户对本人 active restriction 申诉；防重放、容量限制和通用响应。
- 事件 `risk.challenge_required|workspace_restricted|appeal_resolved` 从同事务 outbox 恢复，不含信号阈值、IP、设备或正文。

## 5. Console、测试与性能

Platform `abuse-risk` 页面为 case 列表、信号族摘要、限制状态、决策与申诉时间线；support 无决策控件且看不到阈值。
用户挑战在 Identity 页面内存完成，Secret 输入成功/失败后均清除，不使用 Storage。

专项覆盖账号枚举、credential stuffing、分布式窗口、一致限流、动态撤权、挑战重放、自动限制/申诉、支持裁剪、Redis/DB
故障、跨 owner、迁移往返、浏览器和敏感扫描。门禁：判定 p95<30ms；攻击流量下正常登录/Gateway p95 增幅<20%。

## 6. 第一轮自审：枚举、标识与权限

- P0：原始 IP/设备/邮箱进入 signal 会形成敏感画像。修正为域隔离 HMAC、粗粒度前缀和短保留期。
- P0：风控 decision 携带角色/权限会形成授权旁路。修正为只允许限制 overlay 或 challenge。
- P0：登录不同响应暴露账号存在。修正为同形 code/body、近似时间预算和统一挑战 intent。
- P1：客户端提交 account/workspace owner 可横向申诉。修正为 owner 从当前 session/受签 intent 恢复。
- P1：自由风控理由会把信号泄漏给 support。修正为固定 reason family 与独立 DTO。

## 7. 第二轮自审：故障、误伤与重放

- P0：Redis 故障全量 fail closed 会让攻击流量变成停服。修正为按动作风险分层，权限扩大 fail closed、已授权热路径
  保留既有硬门禁并降级增强风控。
- P1：自动冻结直接改 membership 会难以恢复。修正为独立可过期 restriction overlay。
- P1：挑战 token 可重复使用。修正为 hash、intent/body fingerprint、attempt CAS 和一次性 consumed 终态。
- P1：申诉清除历史会破坏审计。修正为追加 resolved decision，只终止 active overlay。
- P1：规则阈值进入 Console/错误。修正为仅固定原因族与下一步，不返回阈值/权重。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：identity 与 tenant/workspace owner 不混用；security 决策、support 脱敏只读、本人挑战/申诉
  各走独立 DTO，restriction overlay 只能收紧，不能授予角色、membership 或 entitlement。
- 第二轮（迁移/依赖/失败策略）：0102 对 decision/case/challenge/appeal/active restriction 执行 down guard；HMAC 标识按域隔离，
  challenge/appeal 防重放；Redis 故障按风险分层，权限扩大 fail closed，既有已授权热路径保留硬门禁并记录降级。

## 9. 编码前复核冻结

- Excel 对象 owner 优先于内部便利性：`RiskSignal`、`RiskDecision`、`Challenge`、`RiskCase`、`RiskAppeal` 均保存
  `workspace_id + tenant_id` 并引用 `workspace_tenant_bindings` 复合外键；只有 `AbuseCounter` 属于平台全局 Identity abuse domain。
  匿名注册和账号枚举防护只推进全局 HMAC counter 并复用现有同形验证响应，不创建无 owner 的租户风险对象。
- `POST /risk/challenges` 与 `POST /risk/appeals` 只接受 GlobalSession/WorkspaceSession 中的一种浏览器身份；challenge、restriction
  与 appeal owner 从权威会话和数据库关系恢复，body 不允许 tenant、workspace、account、阈值、权重或权限字段。challenge secret
  只返回一次，数据库仅保存 domain-separated hash；attempt 通过 CAS 单调推进，完成或过期后不可回到 pending。
- `GET /platform/risk/cases` 按权限生成两种 exact 投影：owner/security 读取固定信号族、score band、限制和申诉时间线；support
  只读固定原因族与下一步，不返回 subject 摘要、score、阈值或策略细节。`POST /platform/risk/cases/{id}/decisions` 追加不可变
  decision 并以 case revision CAS 更新状态；决定只能维持或终止限制 overlay，不能写角色、membership、entitlement 或 capability。
- 实时 counter key 只由 action domain、固定时间窗和域隔离 HMAC 摘要组成。Redis/注入 store 执行原子递增；DB 只保存不含
  原始标识的恢复水位和不可变事实。权限扩大动作在 store 不可用时返回通用 challenge；已认证 Gateway 增强判断 fail open，
  仍执行既有 Virtual Key、环境、rate limit、预算和权限硬门禁，并只增加固定降级计数。
- `risk.restriction.expire` 复用 PRD-106 的签名 artifact、checkpoint 和 commit-effect：按 restriction ID/revision 幂等终止过期
  overlay、推进 case 并追加固定事件。0102 采用 expand-migrate-contract，激活该 job type；down 仅在无业务事实、无 active
  restriction 且 job 可恢复到 disabled revision 1 时执行，绝不静默删除 signal、decision、challenge、case 或 appeal。
