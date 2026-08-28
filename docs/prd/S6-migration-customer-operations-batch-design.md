# S6 迁移与客户运营批次设计

状态：设计冻结；PRD-131..138 尚未实现。G5 已由提交 `8a97656` 和
`docs/testing/product-qa/REPORT-20260829.md` 的最终全绿证据关闭，现进入连续编码。权威范围来自
`企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 的 S6/G6；本批次只生成发布候选，不宣称公开 GA。

## 1. 批次目标与实施顺序

本批次把已完成的 SaaS、企业身份和私有 Gateway 能力接入可回退的客户迁移、客户支持、公共 API
生命周期、隐私与合规、供应链安全、持续隔离验证和发布候选门禁。固定顺序为
`131 -> 132 -> 133 -> 134 -> 135 -> 136 -> 137 -> 138`：先建立存量客户迁移事实，再建立支持和公共合同；
隐私、合规与供应链随后消费稳定 owner/合同；隔离验证覆盖全部前置事实，最后只生成由全门禁证明的 RC。

每条 PRD 保留独立设计、迁移、聚焦测试、测试记录、敏感审计和 Git 提交。实现期间只运行受影响的
单元/API 冒烟；PRD-138 独立提交后统一执行 G6 数据库、集成、FLOW、真实浏览器、性能、安全、兼容、
72 小时长稳和必要回归。G5 的关闭证据独立保留，不并入或替代 G6 证据。

## 2. 共享 owner 与信任边界

- 客户数据迁移是业务事实迁移，不是数据库 schema migration。`migration_operations` 只扫描由目标
  Workspace owner manifest 明确列出的旧事实；不得使用平台权限遍历所有 tenant，也不得读取正文列。
- 全局账号、Workspace/tenant、Platform、EdgeDeployment 和 release artifact 保持五类独立 owner。
  任何关联都使用复合外键或不可变摘要，不以一个全局 `owner_id` 混用这些域。
- `platform_support` 处理工单不等于获得 Workspace 数据访问。诊断、Deployment 或请求详情仍要求
  PRD-095 SupportGrant/JIT，且 scope、字段、TTL 与 grant revision 每次请求重新校验。
- 公共 OpenAPI、SDK、Webhook schema 和安全报告来自显式 public registry；不能通过反射整个内部 router、
  数据模型或数据库 schema 生成，避免把内部路由、敏感字段和未稳定合同发布出去。
- 同意记录由全局账号主体拥有；隐私偏好、请求、导出和删除决定仍由当前 Workspace/tenant 复合 owner 限定，
  不能读取其他 Workspace。Workspace 管理员只能移除 Membership，不能代表用户导出或删除全局账号。
  Legal Hold 只阻止删除，不扩大任何读取权限。
- 合规证据只证明实际控制事实、采集器版本、hash 和有效期，不自动声称等保、个保或任何未取得认证。
- SBOM、签名和 provenance 由受信构建身份与 KMS 用途域产生；release_manager 或 Console 上传的声明不能
  替代制品 digest、构建输入和签名验证。
- 安全评估只运行签名、版本化、资源受限的场景，使用隔离测试身份和数据；禁止任意 URL、脚本、SQL、
  shell、header 或生产客户 selector。测试结果不能成为读取客户数据的旁路。
- LaunchEvidence 只引用不可变证据 digest、版本和 verifier outcome。会签 actor kind 映射现有平台角色，
  不新增角色；同一账号不能以多角色满足多个独立会签，也不存在 `platform_owner` 手工绕过 P0 门禁。

## 3. 迁移与兼容顺序

| PRD | Migration | 主要新增事实 | Down 边界 |
| --- | --- | --- | --- |
| 131 | 0123 | assessment/plan/run/difference/cutover checkpoint/rollback plan、事件/outbox | 任一迁移运行、切换水位、差异或事件存在即拒绝 |
| 132 | 0124 | ticket/message/attachment metadata/SLA clock/escalation/survey、事件/outbox | 任一工单、消息、附件、SLA 或升级事实存在即拒绝 |
| 133 | 0125 | API contract snapshot/SDK release/deprecation/Webhook schema、事件/outbox | 任一已发布合同、SDK、废弃计划、下载或事件存在即拒绝 |
| 134 | 0126 | consent/preference/DSR/export/erasure decision/retention exception、事件/outbox | 任一请求、决定、导出、例外或审计水位存在即拒绝 |
| 135 | 0127 | control/mapping/evidence/exception/remediation/report、事件/outbox | 任一非种子控制、证据、例外、报告或整改事实存在即拒绝 |
| 136 | 0128 | SBOM/signature/provenance/finding/security exception/patch、事件/outbox | 任一发布安全事实、例外、补丁或消费水位存在即拒绝 |
| 137 | 0129 | assessment/run/finding/remediation/risk acceptance、事件/outbox | 任一评估、发现、接受、执行水位或事件存在即拒绝 |
| 138 | 0130 | launch gate/evidence/wave/decision/rollback/review、事件/outbox | 任一候选、会签、波次、回滚或事件存在即拒绝 |

所有 migration 采用 expand-migrate-contract，验证 `0122 -> 0130` fresh/current-head/逐级/往返/失败回滚/
重复执行和业务 down guard。新表使用稳定 ID、严格状态 CHECK、UTC、revision/generation、复合 owner 外键、
不可变事实触发器和同事务 source/outbox；已应用 migration 不改字节。

## 4. 共享模块与协议

- `migration_operations`：保存 PRD-131 的来源 manifest、差异摘要、双读/冻结/切换 checkpoint 和回退 journal；
  复用 tenant lifecycle、Workspace、Provider、Key、账本、审计和 Edge 的公开迁移适配器，不直接拼表名或 SQL。
- `customer_support`：PRD-132 的工单、SLA 和升级 owner；正文与附件对象分离，Control 只保存加密正文或不可变
  对象摘要，扫描通过前不可下载。关联 request/incident/deployment 只保存 owner-qualified reference。
- `developer_contracts`：PRD-133 的 public registry、兼容 diff、SDK reproducible build、签名和 sunset 计时；
  复用现有 `developer_portal.read`，不建立第二套开发者账号或 API 鉴权。
- `privacy_center` 与 `compliance_evidence`：PRD-134 的 ConsentRecord 为全局账号事实，其余隐私事实和 PRD-135
  的证据/例外/整改/报告均带 Workspace/tenant 复合 owner；通过固定命令与 `data_governance`、
  `tenant_lifecycle`、`enterprise_identity` 协调，不跨模块直接删除数据。
- `supply_chain_security`：PRD-136 的 SBOM/signature/provenance/finding 为平台全局制品事实；SecurityException
  和 PatchRelease 是引用全局 digest 的 Workspace/tenant 事实。签名用途域与 PRD-121/124/127 兼容但不复用
  客户内容密钥。
- `security_assurance`：PRD-137 每项 assessment/run/finding/remediation/acceptance 都绑定隔离目标
  Workspace/tenant，平台只是发起 actor；模块消费版本化 owner registry 和 G6 verifier，不复制业务表。
- `launch_governance`：PRD-138 的 gate/wave/decision 为平台全局，evidence/rollback/review 必须带验证或试点
  Workspace/tenant 复合 owner；candidate digest 绑定代码、migration、合同、制品和测试清单，任何输入变化都会
  使旧会签失效。
- Console 共享紧凑状态时间线、证据表、审批、失败恢复和空态组件；Identity、Workspace、Developer 与
  Platform 页面继续使用独立 Session/API client/DTO/store，禁止缓存或迟到响应跨域复用。

## 5. 权限与页面归属

- Workspace `owner/admin` 管理 PRD-131 迁移；`developer` 只读明确差异和公开迁移指南；
  `platform_support` 只能在有效 JIT scope 内协助，不能批准或代替客户执行 cutover/rollback。
- Workspace 成员创建和读取本空间工单；被授权处理人更新工单。Platform Support 只读取工单合同内字段；
  内部备注、客户消息和附件各用独立字段白名单，诊断读取仍走 SupportGrant。
- 开发者中心由 `developer_portal.read` 读取；`release_manager` 经独立批准发布 API/SDK/Webhook 合同，
  废弃不能改变现有路由 RBAC 或缩短 Excel 要求的兼容期。
- 用户只管理本人 PrivacyPreference/DSR；`platform_privacy` 处理最小队列，`legal_admin` 只管理保全决定；
  企业 owner 只能处理 Membership 协调，不读取其他 Workspace 导出。
- `platform_compliance` 管理平台控制和证据；Workspace `security_admin/auditor` 只读本空间报告；例外双人审批、
  固定 scope 和到期。`platform_security + release_manager` 非自批发布安全例外和已签制品。
- `platform_security` 发起 PRD-137 评估；Workspace `owner + security_admin` 仅共同批准与本空间有关的风险接受。
- PRD-138 的产品/SRE/安全/测试/财务/法务是独立会签 actor kind，由现有平台 permission 显式映射；
  `platform_owner` 仅在全部当前 generation 会签后启动/回滚波次，不能查看因此新增的租户数据。

## 6. 失败、恢复与敏感边界

- 评估必须无写；cutover 只在来源 hash、目标 hash、FK、账本序列、审计水位、冻结 generation 和授权均匹配时
  原子推进。失败保留 checkpoint；回退按 journal 补偿新写入，禁止简单指针回拨丢失已接受请求。
- SLA 使用持久单调时钟事件计算；worker 租约、重复通知、重启和迟到扫描不能重复扣时或越过 pause/resume。
- API sunset、证据过期、漏洞 SLA、风险接受和 launch gate 都使用持久 scheduler watermark、lease/generation
  和幂等命令；worker 故障保留待办，不把未知状态当通过。
- 附件/导出/报告/SDK/SBOM/安全证据使用私有 staging、大小和类型预算、hash、恶意内容扫描和原子发布；
  失败删除部分制品，下载使用短 TTL、no-store 和逐次审计。
- 日志、事件、报告、差异、SDK 示例、证据和测试制品不得包含 Provider Key、Token、Cookie、TOTP、
  Prompt、Response、客户正文、内部备注、附件正文、签名私钥、对象 URL 或本地路径。
- 任何 verifier 缺失、失败、跳过、超期、签名不可信、owner 不匹配或含敏感明文，都使对应 gate 失败关闭；
  launch 只可回退到上一已验证 wave，不可把 RC 状态改写为 GA。

## 7. 两轮批次自审

### 第一轮：owner、公开合同与信任来源

- P0：让迁移工具使用平台数据库权限扫描所有旧 tenant 会形成跨租户旁路。修正为 tenant-qualified source
  manifest、固定适配器和 Workspace owner 授权；Platform JIT 也不能扩大扫描集合。
- P0：把工单关联 request_id 当成读取请求正文的授权会泄漏 Prompt/Response。修正为只关联固定摘要，
  诊断详情独立恢复 SupportGrant、字段 scope 和 TTL。
- P0：从内部 router 自动生成 OpenAPI 会发布私有端点和敏感模型。修正为显式 public registry 与编译期
  exact DTO，合同 diff 只比较已发布 surface。
- P0：Workspace 管理员代用户提交全局删除会越过账号主体。修正为全局身份重新验证，Workspace 仅参与
  Membership 和法定保留协调。
- P0：上传一份 PDF 即把控制标成“已认证”会产生错误合规声明。修正为采集器签名事实、有效期、review
  和措辞状态分离，认证状态不能由单条证据推导。
- P0：人工上传 SBOM/签名声明不能证明制品来源。修正为 digest-bound KMS build identity、可复现构建输入
  和离线验签，Console 只能查看或批准时限例外。
- P0：允许安全评估传任意脚本/URL 会成为攻击平台。修正为签名场景 registry、隔离身份、固定 target kind
  和资源/网络边界，生产 selector 失败关闭。
- P0：LaunchGate 接受可编辑人工结论会让单人绕过门禁。修正为 verifier-signed evidence digest、独立
  actor kind 和无 override 状态机。

### 第二轮：时序、恢复与证据陈旧

- P0：cutover 后直接回拨旧 head 会丢新写。修正为冻结 generation、双写 receipt、checkpoint、补偿和
  重新验证；回退也是可恢复 job，不是一次 UPDATE。
- P0：SLA、隐私删除、证据过期和漏洞时限各自用进程内 timer 会在重启后漂移。修正为统一持久 deadline、
  scheduler watermark、lease fencing 和幂等 catch-up。
- P0：已有 Legal Hold 时先执行删除再发现冲突不可恢复。修正为 DSR 先生成数据图和 erasure decision，
  每个 owner 原子执行前再次检查 hold/retention generation。
- P0：漏洞例外绑定 release name 而非 artifact digest 会放行替换制品。修正为 digest、finding、scope、
  approver 和 expiry 全绑定，任一变化重新审批。
- P0：安全测试清理测试数据时删除 finding 会失去审计证据。修正为测试身份/fixture 可清理，assessment、
  finding、remediation 和证据摘要不可变保留。
- P0：代码、migration 或合同变化后沿用旧 RC 会签会发布未验证输入。修正为 candidate composite digest；
  任一输入变化提升 generation、全部旧证据与会签自动 stale。
- P1：一个平台账号拥有多角色时可一人完成所有会签。修正为 actor account 独立性约束和非自批 trigger，
  role 只决定可声明的 actor kind，不合并签名人。
- P1：跨页面复用迟到 DTO 会把内部备注、隐私队列或 finding 投到错误域。修正为四域 client/store 隔离、
  navigation epoch 和离页清理，服务端字段白名单仍是最终边界。

两轮自审均在设计中完成修正，无未关闭 P0/P1 设计问题。

## 8. G6 退出证据

八条独立提交后必须统一证明：`0122 -> 0130` fresh/逐级/down guard；10 万对象评估、无写 dry-run、
可恢复 cutover/rollback；工单/SLA/附件扫描/JIT；public contract diff、SDK 可复现与 N/N-1；账号 DSR、
Legal Hold 与跨 Workspace 零泄漏；合规事实与证据时效；SBOM/签名/provenance/漏洞例外；全 owner 矩阵隔离
评估；四波试点回退；灾备/支付/Edge 演练；WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW 全组；
Windows/Linux、SQLite/PostgreSQL、代理/断网、三浏览器三缩放；Excel SLO；连续 72 小时无 P0/P1；
数据库、outbox/queue、子进程、监听和临时目录静止终态；敏感与禁止文件命中 0。

G6 只能生成 `release_candidate`。公开 GA 必须等待 PRD-139 起的存量 Console 分域迁移和 PRD-148 最终门禁；
任一必需证据缺失、跳过、失败、陈旧或含敏感明文，G6 即失败。
