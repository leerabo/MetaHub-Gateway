# Business Verification and Billing Profile API

PRD-102 为 organization Workspace 提供法定主体、企业认证、合同主体与开票资料，并为平台审核员提供
独立的最小权限投影。个人 Workspace 不创建空企业资料或伪造企业状态。

## 凭据域

- `/workspaces/{workspace_id}/business-verification*` 与 `/workspaces/{workspace_id}/billing-profile` 只解释
  active WorkspaceSession；path Workspace 必须与 Session owner 一致，职责实时要求 `owner` 或
  `finance_admin`，Workspace 类型必须为 `organization`。
- `/platform/business-verifications*` 只解释 PlatformSession。列表要求 `platform.verification.read`，审核和
  evidence token 要求 `platform.verification.review`。
- WorkspaceSession、PlatformSession、SupportSession、Bearer 和 Virtual Key 不能跨域替代；冲突凭据失败关闭。
- 写请求要求唯一允许 Origin、对应域的 CSRF Cookie/Header、严格 JSON；资料提交和审核要求
  `Idempotency-Key`，开票资料和审核还要求强 `If-Match`。

## 接口

| 方法 | 路径 | 请求边界 | 成功响应 |
| --- | --- | --- | --- |
| POST | `/workspaces/{workspace_id}/business-verifications` | 法定主体、1..5 份 evidence metadata、扫描证明、幂等键 | 冻结 intent 与脱敏 Workspace 摘要 |
| GET | `/workspaces/{workspace_id}/business-verification` | 无 query/body | 法定主体、intent、合同与开票资料安全摘要 |
| PATCH | `/workspaces/{workspace_id}/billing-profile` | `If-Match`、幂等键、分字段资料 | 脱敏开票资料与新 revision |
| GET | `/platform/business-verifications` | `status/limit/offset` | 案件、主体类型、hash、扫描状态与 evidence metadata |
| POST | `/platform/business-verifications/{intent_id}/review` | `If-Match`、幂等键、固定 decision/reason | append-only review、案件与合同安全摘要 |
| POST | `/platform/business-verifications/{intent_id}/evidence/{evidence_id}/download-token` | active review、Platform CSRF | 单次、短 TTL、Session/intent/evidence 绑定令牌 |

提交正文中的 evidence 只接受固定类型、`application/pdf`/`image/png`/`image/jpeg`、1..10485760 bytes、
SHA-256、KMS 版本和 malware/DLP 扫描证明。服务端验证扫描证明后才写入加密对象引用；响应、事件、平台审计和
通用日志均不返回对象引用、原件 URL、法定名称、登记号、税号、银行账号或密文。

## 状态与并发

提交 intent 时冻结 LegalEntityProfile revision、profile hash 和 evidence manifest hash。审核期间修改客户资料会
创建新 revision，不改写已提交案件；approve 会在同一事务重新验证资料 revision/hash、manifest 和所有扫描状态，
然后原子追加 review、更新 intent、创建 ContractProfile 与平台审计。reject 只接受固定 reason code，不返回内部备注。

开票资料以 `If-Match` CAS 更新。认证通过后资料 revision 变化会把开票验证状态推进为 `stale` 并增加 revision，
避免旧认证继续代表新资料。相同幂等键与相同规范请求返回原结果，不同正文返回固定冲突。

## 加密与配置

法定名称、登记号和开票字段使用独立 AES-256-GCM 域；AAD 绑定 workspace、对象、字段和 key version。
登记号查询只使用 keyed blind index。配置如下：

| 配置 | 约束 |
| --- | --- |
| `METAHUB_BUSINESS_VERIFICATION_KEY_VERSION` | 正整数，写入 ciphertext metadata |
| `METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET` | 独立 32..512 byte 根密钥；生产必填且不得复用其他安全域；摘要始终 `masked` |
| `METAHUB_BUSINESS_VERIFICATION_DOWNLOAD_TTL_SECONDS` | 30..300 秒，默认 120 秒 |

## 固定错误

业务错误使用 `business_verification.request_invalid/not_found/business_not_applicable/revision_conflict/`
`idempotency_conflict/evidence_invalid/evidence_not_ready/review_conflict/unavailable`。凭据、Origin、CSRF、
Session 和权限错误沿用 `workspace_session.*` 或 `platform.*`。所有错误只返回固定安全文案与关联 ID，不透传
SQL、表名、对象引用、KMS 标识、Cookie、Session、TOTP 或请求中的敏感字段。
