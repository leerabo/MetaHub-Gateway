# PRD-102 企业认证、合同主体与开票资料

状态：设计完成，待编码
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-093、PRD-095、PRD-098、PRD-100

## 1. 目标与边界

本条让 organization Workspace 的 owner/finance_admin 维护法定主体、提交企业认证并维护合同与开票资料；
platform_verification 只读取审核必需字段和受控证据，不获得 Workspace Membership 或通用客户数据权限。
个人 Workspace 不伪造企业资料。本条不对接税控开票、不保存银行卡、不把证照原件写入通用日志、事件、
审计摘要或前端 Storage。

## 2. 0094 数据模型与加密

- `legal_entity_profiles`：workspace/tenant owner、国家/地区、主体类型、法定名称密文、登记号盲索引、revision。
- `business_verification_intents`：submitted/in_review/approved/rejected/expired、profile revision、固定 reason code。
- `verification_evidence`：对象存储引用、content hash、mime/size、KMS key version、malware/DLP 状态；无原件 URL。
- `contract_profiles`：认证主体、合同签约状态、有效期和不可变版本引用。
- `workspace_billing_profiles`：抬头、税号、地址/电话/银行信息分字段密文、revision、验证状态。
- `verification_reviews`：append-only 平台 reviewer、decision、固定 reason code、evidence manifest hash。

PII/商业敏感字段使用独立 KMS purpose 与 AAD 绑定 workspace/object/field/version；检索只用 keyed blind index。
对象下载令牌单次、短 TTL、绑定 PlatformSession/intent/evidence，且只有 active review 才可签发。

## 3. 权限与状态机

Workspace 写路由只解释 WorkspaceSession，并实时要求 organization owner 或 `finance_admin`；developer/admin
不能由角色名称隐式获得法定资料权限。提交 intent 会冻结 LegalEntityProfile revision 和 evidence manifest；
审核期间修改资料产生新 revision，但不改写已提交 intent。

平台审核只解释 PlatformSession `platform.verification.review`。reviewer 不能修改客户资料；approve 必须
证明 evidence 扫描通过、主体 revision/hash 一致，原子写 review、intent 状态、ContractProfile 与平台审计。
reject 只使用固定 reason code，不返回内部备注。跨 Workspace/不存在 intent 同形 404。

## 4. HTTP 合同

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `POST /workspaces/{workspace_id}/business-verifications` | Workspace owner/finance | 提交冻结认证意图 |
| `GET /workspaces/{workspace_id}/business-verification` | Workspace owner/finance | 当前资料与认证安全摘要 |
| `POST /platform/business-verifications/{id}/review` | platform_verification | append-only 审核决定 |
| `PATCH /workspaces/{workspace_id}/billing-profile` | Workspace owner/finance | If-Match 修改开票资料 |

Workspace path id 必须等于 Session workspace，否则同形拒绝。严格 JSON、Origin/CSRF、If-Match、
Idempotency-Key；响应默认脱敏登记号、税号、银行账号，仅显式编辑响应返回字段级掩码状态，不返回密文。

## 5. Console、保留与性能

Workspace 页面分企业认证与开票资料两个紧凑区；平台审核队列只显示案件 ID、国家/主体类型、扫描/状态和
受控证据动作。敏感输入提交后立即从 DOM 清除，Storage 0 次。拒绝/过期 intent 按政策保留 metadata，
证据对象到期由带 tombstone 的 worker 删除。列表 warm p95 `<300 ms`，单详情 `<300 ms`。

## 6. 专项测试

覆盖 0093->0094/fresh/down/up、organization/personal、owner/finance 动态撤权、跨 Workspace、证据 MIME/
大小/hash/扫描、KMS 轮换、盲索引、审核并发/自冲突、平台字段裁剪、短时下载、凭据负矩阵、DOM/Storage、
日志/事件/审计敏感 canary 0 命中。

## 7. 第一轮自审：PII 与证据

结论：通过，P0/P1 已修正。

- P0：把证照 Base64 放数据库/事件会复制敏感原件。修正为加密对象存储引用和 manifest hash。
- P0：通用审计记录 before/after 会泄露法定名称、证号和银行资料。修正为固定状态、字段掩码和 hash。
- P0：审核读取完整 Workspace DTO 会越过最小权限。修正为独立 platform verification 投影。
- P1：直接索引登记号明文会形成可枚举目录。修正为 keyed blind index，展示始终掩码。

## 8. 第二轮自审：并发与职责分离

结论：通过，P0/P1 已修正。

- P0：审核期间原地修改 profile 会改变审核对象。修正为 intent 冻结 revision/evidence manifest。
- P0：平台 reviewer 可编辑客户资料会混淆 actor domain。修正为平台只追加 decision，修改必须客户会话。
- P1：personal Workspace 接口返回空企业模板会制造伪事实。修正为固定 business_not_applicable。
- P1：长期 evidence URL 可跨 Session 泄露。修正为单次短 TTL、主体和 intent 绑定下载。

