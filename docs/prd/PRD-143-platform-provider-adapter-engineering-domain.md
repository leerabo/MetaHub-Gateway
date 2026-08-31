# PRD-143 Platform Provider Adapter 工程台与错误规则分域

状态：设计冻结，等待 G6 通过后编码。依赖 PRD-063/064/119/123/133/136/137；批次边界见
`S7-platform-operations-console-domain-batch-design.md`。本条只交付 0135、Platform Provider Engineering 工作台、
官方 Adapter release/合同证明、内置错误目录 release、Workspace override 和兼容投影，不提前实现 PRD-144..148。

## 1. 产品闭环与非目标

`platform_provider_engineer` 维护官方 Adapter/内置错误目录候选并运行平台合同 CI，`platform_security` 复核合同
失败与供应链证明，`release_manager` 独立发布已签名且门禁通过的 Adapter 和错误目录。企业 developer 只在本
Workspace Provider 详情读取兼容版本、健康摘要和管理本 Provider 的受限错误覆盖；看不到平台 CI case、Runner、
构建路径、源码、签名私钥、官方规则正文或竞品运营信息。

Excel 权威业务 API 为：

- `GET /platform/provider-adapters`
- `GET /platform/provider-adapters/{id}/contract-runs`
- `POST /platform/provider-error-catalogs/releases`
- `GET/PATCH /workspace/provider-error-overrides`

对象固定为 `ProviderAdapterRelease`、`AdapterContractRunProjection`、`BuiltinErrorCatalog`、
`WorkspaceErrorOverride`、`ProviderCompatibilityProjection`；事件固定为 `provider_adapter.released`、
`adapter_contract.failed`、`error_catalog.released`、`workspace_error_override.changed`。

Excel 同时要求 release_manager 发布 Adapter、platform_provider_engineer 运行合同测试，却没有给出命令入口。
现有 Platform Jobs 不能创建 domain job，PRD-119 provider catalog release 也不能发布可执行 Adapter。第 7 节因此
补充单个窄作用域 `POST /platform/provider-adapters/{id}/commands`，只引用 PRD-133/136 已登记的签名候选和固定
action，不能上传代码、schema、路径、Runner 参数或任意 manifest。

本条不把 DB 中的 spec JSON 解释为可执行代码，不开放任意 Adapter 上传/插件执行/命令行，不读取或保存客户
Provider Key、local secret、Endpoint、Prompt/Response、Header、上游 body 或测试 fixture 正文。PRD-119 官方
Provider catalog 继续负责 vendor/region/capability/compliance；Adapter release 只负责协议实现与合同证明。
OpenRouter/硅基流动保持 migration-source-only，不成为默认上游、默认 Adapter 或共享客户接入。

## 2. 身份、owner 与权限

### 2.1 Platform 与 Workspace owner

ProviderAdapterRelease、BuiltinErrorCatalog 和 platform CI run 是全局 Platform facts，不带 tenant_id。它们只能由
有效 PlatformSession 操作，引用签名 artifact/release candidate ID，不能引用 Workspace Provider/secret。

WorkspaceErrorOverride 和 ProviderCompatibilityProjection 必须带不可变
`workspace_id + tenant_id + provider_id` 复合 owner。WorkspaceSession 从当前 Membership、WorkspaceTenantBinding、
Provider owner 和权限恢复；body/query 不接受 owner。PlatformSession 不能读取 tenant override pattern，
SupportSession/GlobalSession/ChannelMembership/DeviceSession/Bearer 全部拒绝 Workspace override API。

0056 legacy contract run/probe 继续保留原 tenant owner 和 `acr_/acc_` ID。迁移到 Platform projection 后，owner 只供
内部对账和隐私边界，Platform API 不返回 tenant/workspace；legacy `probe` 永远不成为官方 release proof，避免把
客户 Endpoint 探测结果包装成平台 CI。

### 2.2 固定权限与职责分离

0135 新增 `platform_provider_engineering_permission_extensions` 和 Workspace RBAC 增量：

| Permission | provider_engineer | platform_security | release_manager | Workspace developer |
| --- | --- | --- | --- | --- |
| `platform.provider_adapter.read` | 允许 | 允许 | 允许 | 否 |
| `platform.provider_adapter.candidate_manage` | 允许 | 否 | 否 | 否 |
| `platform.provider_adapter.contract_run` | 允许 | 否 | 否 | 否 |
| `platform.provider_adapter.failure_review` | 允许 | 允许 | 否 | 否 |
| `platform.provider_adapter.release` | 否 | 否 | 允许 | 否 |
| `platform.error_catalog.read` | 允许 | 允许 | 允许 | 否 |
| `platform.error_catalog.candidate_manage` | 允许 | 否 | 否 | 否 |
| `platform.error_catalog.security_approve` | 否 | 允许 | 否 | 否 |
| `platform.error_catalog.release` | 否 | 否 | 允许 | 否 |
| `workspace.provider_error_override.read` | 否 | 否 | 否 | 允许 |
| `workspace.provider_error_override.manage` | 否 | 否 | 否 | 允许 |

`platform_owner` 只通过 extension table 显式获得 read/failure_review，不自动获得 candidate/release；紧急发布仍需
当前 provider_engineer candidate、platform_security approval 和不同 release_manager。candidate author、security
reviewer、release actor 三人互异；一个 Operator 多角色也不能跨过 SoD。

Workspace developer 指 owner/admin/developer 的固定授权上限；security_admin 可只读安全摘要，viewer/finance/auditor
无 override 权限。现有 `error_map.read/manage` 精确迁移：admin/developer 的可读保持，写上限从旧 admin-only 改为
owner/admin/developer 的 provider-scoped manage；任何自定义角色只在原授权和新上限交集内迁移，不扩大 viewer。

Platform authority 使用固定 extension table tuple/SQL；Workspace authority 使用 catalog revision 和完整 permission
fingerprint。每次请求、candidate approve、release、override PATCH 和迟到响应重验 Operator/Membership/role/revision；
撤权后下一请求立即失效。

## 3. 0135 数据模型与唯一事实

### 3.1 ProviderAdapterRelease

`provider_adapter_releases` 使用稳定 `parl_*`，保存 adapter code、SemVer、protocol family、spec manifest version/hash、
signed artifact/release candidate ID、artifact/SBOM/provenance/signature/compatibility hashes、PRD-136 verification ID、
required runtime/API versions、status `candidate|testing|review_required|approved|released|deprecated|revoked`、revision、
release generation 和 UTC。可执行 Python/JS/binary、source tree、build path、Runner command 和 secret 不入库。

Shared SDK 仍是运行时 Adapter 接口与冻结 spec 的执行 owner。Control 只从当前部署的签名 artifact catalog 验证
候选三元组 `(adapter code, version, artifact sha256)`，数据库记录不能令二进制加载未随部署交付的代码。相同
SemVer 不同 artifact/spec/provenance 一律拒绝；released row immutable，修复必须升版。

`provider_adapter_release_heads` 每 adapter code 指向唯一 released generation；发布事务验证 candidate、当前 artifact、
完整 platform merge-gate run、Security approval、SBOM/provenance/signature、N/N-1 compatibility 和 current head CAS，
然后原子推进 head、compatibility projection、audit/event/outbox。release_manager 不能改 candidate 内容。

### 3.2 AdapterContractRunProjection 与 case 索引

`platform_adapter_contract_run_projections` 保存稳定 run ID、scope
`platform_release_ci|legacy_tenant_merge_gate|legacy_tenant_probe`、adapter/release candidate、schema/runner/suite/mode、
source revision/artifact/catalog hash、status/coverage、total/passed/failed、固定 failure codes、report hash、source
attestation/provenance、开始/完成 UTC、legacy owner digest 和 immutable generation。平台新 run 使用 `pacr_*`；legacy
保留 `acr_*`，不改 ID。

`platform_adapter_contract_case_projections` 保存 run、稳定 case ID/key hash、case kind/variant、fixed provider/adapter
code、status、failure code、duration bucket、evidence hash 和 ordinal，不保存 auth value、fixture/assertion、请求/响应、
Endpoint、trace body 或 Runner path。legacy 保留 `acc_*`；platform case 使用 `pacc_*`。

Platform release proof 只接受 `scope=platform_release_ci`、candidate/artifact/source revision 全匹配、coverage complete、
全部 mandatory case passed 且 attestation 签名有效的 run。probe 和 legacy run 可以对账/诊断，但 `qualifies_release=false`
由数据库约束固定，不能经 API 翻转。失败 platform run 写 `adapter_contract.failed`；成功不写噪声事件。

为 10 万 case 分页建立 `(adapter_release_id, run_completed_at DESC, run_id)` 和
`(run_id, ordinal, case_id)` covering keyset 索引；API 从投影读，不运行 JSON extraction、COUNT 全表或读取 CI artifact。

### 3.3 BuiltinErrorCatalog

0046 的 `provider_error_builtin_catalog_versions` 与 `provider_error_builtin_rule_versions` 已是不可变 global registry，
0135 直接把它们纳入 BuiltinErrorCatalog owner，不复制第二份规则表。新增
`platform_builtin_error_catalog_releases/heads` 保存 catalog version/hash、signed candidate/artifact、rule count、
compatibility baseline、Security approval、release actor/generation、status 和 UTC，head 引用 0046 exact catalog。

新 catalog candidate 在 PRD-136 受控 artifact 内携带 canonical rule manifest。ingest 只允许固定 comparator/field/
operator/output error code、长度/计数上限和无 regex；Control 复算每条稳定 rule ID/pattern hash及整 catalog hash。
发布事务要求 provider engineer candidate、不同 Security approval、不同 releasemanager 和当前 head If-Match。
DB 规则只是运行时 Shared builtin matcher 的可验证镜像；当前 binary 未登记相同 catalog hash 时不得 release。

`POST /platform/provider-error-catalogs/releases` 只接受 exact action
`create_candidate|submit_review|security_approve|release|deprecate` 与 candidate/artifact/revision 引用；不接受 pattern、
规则 JSON、路径或代码正文。发布成功写 `error_catalog.released`；draft/review/no-op 不发。

### 3.4 WorkspaceErrorOverride 与兼容投影

`workspace_provider_error_overrides`、immutable versions 和 heads 从 0046 tenant map 保留稳定 `pem_*`/version、优先级、
provider binding、受控 match facts、output error code、pattern hash、status、revision/content hash/catalog version，新增
完整 Workspace owner。pattern 仍只允许 exact/prefix/contains 等现有有界 operator，禁止 regex、敏感模式、原始
错误正文或自定义 message/retryable。

`workspace_provider_error_override_commands/events/outbox` 使用 Workspace owner、Idempotency-Key HMAC、quoted
If-Match、canonical body hash和 current permission fingerprint。事件 `workspace_error_override.changed` 只含 override/
provider ID、operation、version/head revision、pattern/output hashes 和 UTC，不含 pattern 或原始错误。

`provider_compatibility_projections` 每 Workspace Provider 保存 ProviderCatalog entry/version、current AdapterRelease、
BuiltinErrorCatalog、override head、preflight/health安全状态、capability/protocol/region、固定 compatibility
`compatible|warning|blocked|unknown`、reason codes、source watermarks/hash 和 projected UTC。它是可重建只读投影，
不改 Provider、Key、Endpoint 或 release head；企业 Provider detail 只读该 DTO，不读取 platform run/case。

Gateway internal snapshot 从新 Workspace override head 获取 exact wire schema/revision/catalog/content hash，继续使用
singleflight、ETag、fresh/LKG/builtin fallback 和 request-scoped frozen lease。新 `provider_error_facts` 同时引用
legacy/new source mapping，历史请求保留原 0046 head/catalog；切换不把当前规则重算成历史命中。

### 3.5 Mapping、event、outbox 与 Scheduler

`provider_engineering_legacy_mappings/cursors` 逐 0055 activation、0056 run/case/outbox、0046 builtin/map/version/head/
event/fact 保存 source/target ID、tenant owner、canonical hash、generation、event/outbox/Gateway cache 水位和结果。

`provider_engineering_events/outbox` 承载四个新 exact event，稳定 ID、subscription snapshot、lease token digest、
generation/attempt 和状态机；payload 不含 tenant、source path、case key、pattern、artifact location 或安全审批正文。
旧 0056 `adapter.contract_failed` 与 0046 `error_map.updated` event/outbox 保持原 ID/type/payload/hash/状态，pending
仍按原 frozen subscription 投递，绝不翻译为新事件或重发终态。

`provider_engineering_scheduler_state` 固定 `legacy_contract_outbox`、`legacy_error_outbox`、`contract_run`、
`compatibility_projection`、`provider_engineering_outbox`、`cleanup`，持久 lease/fence/cursor。一个
`install_platform_provider_engineering_scheduler` 同时替换现有 `install_adapter_contract_event_scheduler` 与
`install_provider_error_map_scheduler` 两个 Control 槽位，installer 数量减少一而非增加；Gateway override cache 的
独立请求侧 scheduler 保留，PRD-147 再统一登记 owner/leader。

## 4. 0055/0056/0046 迁移与切换

0135 使用 expand/inventory/backfill/quiesce/cutover/contract，保留历史 ID、版本、event/outbox 和 Gateway fact。

### 4.1 Adapter activation 与 run 迁移

1. 只接受 current head 0134。盘点所有 0055 `provider_adapter_catalog_activations` 的 tenant、catalog version/hash/count、
   active/superseded/changed、request/trace 和 UTC。相同 `adapter-spec-v1` 只有在 Shared catalog identity 和每租户
   active 三元组完全一致时形成一个 bootstrap ProviderAdapterRelease；每条 tenant activation 仍逐行 mapping，
   不生成每租户 platform release。
2. 若同版本 hash/count 漂移、tenant 缺 active、多 active、unknown catalog 或当前 binary 不匹配，记录 conflict 并
   阻断 cutover。0055 bootstrap release 只标 `legacy_baseline`，没有 PRD-136 proof 时不能作为新的发布候选。
3. 0056 run/case 按原 `acr_/acc_*`、tenant、schema/catalog/runner/source hash、计数、failure codes、report/evidence hash、
   suite/mode/UTC 逐列迁为 legacy scope projection。run/case count/hash 和 mandatory coverage 复算必须相等；任何
   case 丢失、重复或跨 owner 阻断。
4. merge_gate/probe 均保留但不 qualify 新 release。现有 event outbox 的 event ID、pending/processing/dispatched/failed、
   attempt/lease/next/dispatched UTC 原值保持；dispatched/failed 不重放，processing 等 lease 完成/过期后接管。

### 4.2 Builtin catalog、override 与 Fact 迁移

1. 0046 builtin catalog/rule registry 逐版本/规则 ID/hash/precedence/字段/operator/output code 复算，与当前 Shared
   immutable catalog identity 完全匹配后创建 platform release/head wrapper，不复制 rule。非 seed 版本同样保留，
   但缺少签名/approval proof 时只为 `legacy_reconciled`，不能冒充新发布。
2. 每个 tenant map 必须唯一映射 active WorkspaceTenantBinding；map/version/current+historical head、operation、event、
   audit 和 provider_error_fact 逐行迁移/引用，保留 `pem_*`、revision/content hash/catalog、actor/request/trace、
   LKG source 和 normalized outcome。缺/多 Workspace owner 整 tenant 阻断，不能只写 tenant_id。
3. 目标 Workspace override 与旧 map 逐 rule/head hash 一致后，Gateway 先双读比对但只继续由旧 head 写；新 API 尚不
   开放。所有 seen tenant 的 Gateway ETag/revision/source 与 Control target snapshot 对账，冷租户显式记录未见而非
   伪造 fresh。
4. 相同 migration generation 重放只验证 mapping。相同 legacy ID 不同 hash、目标额外 rule、head revision 倒退、
   historical Fact 引用 current head、event/outbox 状态倒退或 builtin catalog 漂移全部失败关闭。

### 4.3 Quiesce、原子切换与回滚

先 fence 两个 legacy Control outbox scheduler，等待 active lease 完成/过期；Gateway 在一个短窗口冻结 override
management 写但继续用 request-scoped old snapshot/LKG 服务。最终对账后 cutover CAS 切 Gateway snapshot owner、
启用新 override writer、给旧 0055/0056/0046 tenant writer 表安装 write guard；全局 builtin registry 继续作为
新 Platform catalog 的唯一 rule registry，不被 guard 禁止当前 release head 读取。

同一应用发布卸载企业 `/adapter-specs`、`/adapter-contract-runs`、顶级 `/error-maps` 写路由与相应
`adapter-specs/adapter-contract-runs` Navigation、DOM、prefetch/import。`error-maps` 不再是独立顶级页面；企业
Provider detail 使用 `/workspace/provider-error-overrides` 和 compatibility DTO。旧外部 route 返回普通统一 404，
内部 legacy outbox/fact reader 不成为兼容 API。Platform Engineering 页面只在 target reconciliation 后开放。

0135 down 只允许所有 release/run/case/catalog wrapper/override/compatibility/mapping/cursor/command/approval/event/
outbox/scheduler 水位 pristine 且无回填的空库执行。任一 0055/0056/0046 mapping 都在首个 DDL 前阻断；空库验证
`0134 -> 0135 -> 0134 -> 0135`。Down 不修改现有 0046/0055/0056 事实。

生产 rollback 可恢复上一 target release/compatibility reader 或 Gateway target LKG，不能解除旧 write guard、恢复
企业 Adapter CI 页面/旧 error-map API/permission，不能让 legacy probe 成为发布 proof。目标平台不可用时企业
Provider detail 显示 compatibility stale/unknown，Gateway 继续用已验证 override LKG/builtin，不中断热路径。

## 5. 状态机、并发与发布规则

Adapter candidate 合法转换为 `candidate -> testing -> review_required -> approved -> released -> deprecated|revoked`；
任一 contract failed 回 testing/review_required，Security 可 approve fixed failure disposition 但不能改测试结果。
release 事务要求最新全绿 run、artifact/provenance/security revision、candidate digest 和 current head If-Match；
release 与迟到 failed run 竞争时用 candidate generation CAS，失败事实优先阻断未提交 release。

Builtin catalog 为 `candidate -> security_review -> approved -> released -> deprecated`；任何规则内容变化必须新 version/
hash，不能 UPDATE released rows。Workspace override 为 `active|disabled|archived`，PATCH exact command
`create|update|enable|disable|archive`；同一 Provider/rule specificity/priority 冲突由 DB unique/overlap guard 终检。
archive 不删除历史 version/head/fact。

ProviderCompatibilityProjection 只在所有 source watermark 单调、Adapter/catalog/override/Provider owner 一致时推进。
官方 release revoked 或 provider catalog incompatible 会 blocked；preflight/health stale 返回 warning/unknown，不能用
0/healthy 代替。projection 变化不发 Excel 事件，避免把企业 runtime 状态误写成平台 release 事件。

## 6. 四组 Excel API exact 合同

所有 DTO `extra=forbid`，unknown/repeated query、非法 cursor、未知 body/action、重复 JSON key、非规范 UTF-8 全部
固定 422。Platform API 只接 PlatformSession；Workspace API 只接 WorkspaceSession。写要求 exact Origin/CSRF、
one-shot step-up、Idempotency-Key 和 quoted If-Match；响应 no-store、带 ETag/request/trace/permission fingerprint。

### 6.1 `GET /platform/provider-adapters`

允许 adapter/status/protocol/release generation、fixed compatibility、cursor/limit<=100。返回 release/head、artifact/
spec/SBOM/provenance/signature hash、contract aggregate、Security/release readiness 和 actions；不返回 source path、code、
Runner、case、tenant usage、Provider endpoint/secret 或 competitor metrics。单 ID filter 与列表 cursor 互斥。

### 6.2 `GET /platform/provider-adapters/{id}/contract-runs`

要求 provider_adapter.read，错 ID 同形 404。允许 scope/status/suite/mode/UTC range、run cursor、limit<=200、可选
`view=runs|cases` 和绑定 run ID 的 case cursor/limit<=500。返回安全 run/case projection、failure code/evidence hash/
duration bucket、attestation和 qualifies_release；legacy owner 只显示 `legacy=true`，不返回 tenant/workspace。10 万 case
分页只用 keyset covering index，不返回 total 全表 count 或报告 artifact。

### 6.3 `POST /platform/provider-error-catalogs/releases`

body 是第 3.3 节 exact action 与 signed candidate/artifact/revision；candidate_manage、security_approve、release 三种
permission 分别限制 action，并强制三 actor 互异。不能上传 rules/pattern/path。成功返回 catalog safe digest、status、
revision/generation/actions；release 时同事务写 audit/event/outbox。

### 6.4 `GET/PATCH /workspace/provider-error-overrides`

GET 允许 current Provider ID、override ID/source/status/output code、cursor/limit<=100；先以 Workspace/tenant/provider
owner 过滤再分页，返回 override safe condition、version/head/catalog、compatibility summary和 actions，不返回 builtin
rule正文、原始上游错误或其他 Provider。PATCH exact command `create|update|enable|disable|archive`；provider/override ID
必须属于当前 owner，condition 使用白名单 operator/field/value size，regex/sensitive pattern/任意 message 拒绝。

## 7. Adapter command 辅助合同

`POST /platform/provider-adapters/{id}/commands` 只接受：

- `create_candidate`：引用当前 artifact catalog 内已签名 PRD-133/136 candidate。
- `run_contract`：按固定 suite/profile enqueue bounded platform CI；不能提交 Endpoint、fixture 或 runner args。
- `review_failure`：platform_security 对固定 failure codes/attestation 做 `approve_remediation|reject`，不改 run。
- `release|deprecate|revoke`：release_manager 依据 current candidate/head CAS 推进状态。

route 要求 PlatformSession、action permission、Origin/CSRF、step-up、Idempotency-Key、If-Match。`run_contract` 返回 202
和 domain command/run ID，由 provider engineering scheduler 执行受签名 artifact 固定 runner；不是任意 PlatformJob
创建口。日志只含 action/object/generation/fixed outcome，绝不记录 CI working directory、command、env 或 stderr。

## 8. Scheduler、事件与 SLO

platform contract runner 在隔离环境使用 artifact 固定 suite、network deny-by-default、CPU/memory/time/case 上限；官方
mock merge-gate 不读取客户网络/secret。失败/崩溃写固定 terminal code 和 checkpoint，worker 失租后的迟到 run/case/
event 提交受 fence 拒绝。真实 Provider probe 仍属于 Workspace preflight/health，不在平台 runner 复用客户 Key。

四事件语义固定：Adapter head 首次发布写 `provider_adapter.released`；platform release CI 首次 terminal failed 写
`adapter_contract.failed`；BuiltinErrorCatalog head 发布写 `error_catalog.released`；Workspace override current head
真实变化写 `workspace_error_override.changed`。重放、no-op、draft、read 和 projection refresh 不发。

SLO：Adapter 目录 GET p95 < 300 ms；固定 10 万 safe case projection 的连续 keyset 分页每页 p95 < 500 ms，并证明
无 N+1/全表 COUNT；成功 release 到 Control/Gateway/企业 compatibility projection 99% < 5 分钟。测量包含签名/head
验证、cache invalidation、Gateway snapshot/builtin catalog ACK 和 projection watermark，不能只测 DB UPDATE。

## 9. Platform/Workspace Console 分域

Platform Provider Engineering 页面提供 Adapter release 目录、candidate/proof、合同 run/case、failure review、内置错误
catalog release 和传播水位，使用独立 Platform client/store/epoch。企业不出现 adapter-specs/CI report；Provider
详情显示官方兼容版本、健康/预检摘要、当前 Builtin catalog/override head 和受限 override 编辑。

两端可共享状态 pill/table/pagination 视觉组件，不能共享 auth client、DTO、store、cache 或 action builder。固定
loading/empty/testing/failed/review/ready/released/deprecated/revoked、partial/stale/LKG、permission revoked、conflict、
dependency unavailable 状态。离页/换 Provider/Workspace/candidate、撤权和迟到 200/401/403 清理 owner/step-up/cursor。

390/1280/1440、DPR 1/1.25/1.5 下 case table 可扫描、无重叠/裁剪/横向溢出；case key/hash 可换行但不展示路径。
键盘/焦点/读屏/直接 URL 可用。企业 UI 不显示竞品比较、官方内部 failure case、源码、build、Runner secret 或
Platform supply-chain detail；Provider Secret 输入仍只走 PRD-123 local custody。

## 10. 安全、审计与失败恢复

- API、DB、event、日志、artifact、test fixture和正式证据禁止 Provider Key/Token/Endpoint、Prompt/Response/Header、
  upstream body/error、contract fixture/assertion、source/build path、Runner command/env/stderr、signing key 和 pattern正文。
- Candidate/release 只引用当前 signed artifact catalog；hash、signature、SBOM、provenance、compatibility和 CI proof
  全部绑定同 artifact。数据库篡改不能令运行时加载新代码或 rule catalog。
- Workspace override condition 即使授权可读也不进入 audit/event/log/evidence；敏感模式 scanner 拒绝 token/key/
  URL/email/IP-like 值。Gateway 只持有冻结有界 matcher，不执行 regex 或动态代码。
- 审计记录 actor/domain/action/object、owner、candidate/release/catalog/run/override revision、safe digests、approval SoD、
  permission/step-up revision、outcome、request/trace/UTC，不记 body/path/secret。
- DB/artifact/runner/signature/Gateway/source 故障失败关闭 release/write；企业读返回 stale/unknown，Gateway 用 LKG/
  builtin。未知 contract run 不当 passed，未知 release propagation 不推进 ready，override snapshot 损坏不替换 LKG。

## 11. 聚焦测试与批次证据

G6 通过后本条开发只运行：

- Migration：0134/0135 fresh、empty down/re-up、0055 activation folding/conflict、0056 run/case/event/outbox exact hash、
  0046 builtin/map/version/head/fact mapping、Workspace owner、任一目标事实 down guard、FK/integrity。
- Platform RBAC/SoD：三 platform role 和 owner exact矩阵、candidate/security/release 三人互异、动态撤权、wrong
  session/cookie、幂等/CAS、artifact/signature/provenance漂移。
- Contract：fixed runner/suite、network deny、case/CPU/time上限、crash/retry/fence、legacy probe不qualify、failed event
  exact once、10万case keyset/SLO。
- Catalog：0046 seed/非seed保真、candidate binary identity、rule parser/hash、security/release、head CAS、Gateway
  builtin propagation/LKG。
- Workspace override：owner/admin/developer/viewer/security matrix、cross Workspace/Provider、condition limits、no regex/
  sensitive、state/CAS、Gateway exact snapshot/frozen fact、compatibility projection。
- Legacy cutover：旧两个 outbox lease/watermark、old event stable delivery、旧 route/Navigation/DOM/prefetch ordinary 404，
  Provider detail 新 DTO。
- Console/sensitive：Platform与Workspace store隔离、pagination/late response、源码/Runner/secret零DOM/Storage/log；完整
  三浏览器、批次性能/安全/回归留到 PRD-147 后 G7。

测试记录写 `docs/testing/PRD-143-tests.md`，正式证据进入 `.artifacts`，`.tmp` 不作为交付证据。敏感审计覆盖
staged diff、CI fixtures/output、DB/API/events/logs/artifacts/browser；任一 secret、endpoint、source path 或正文命中
阻断独立提交。

## 12. 预计改动边界

- `infra/migrations/0135_platform_provider_adapter_engineering.sql` 与 `.down.sql`
- `control-plane-api/src/metahub_control/provider_engineering/`
- 现有 `adapter_specs/adapter_contracts/error_maps` source/outbox 迁接及 `app.py`
- Platform access、Workspace auth/console、PRD-119 provider catalog、Gateway error-map cache/facts 适配
- Shared Adapter/error catalog identity、exact errors/webhooks 和 PRD-133/136 artifact reference
- `console-web/src/lib/providerEngineering.js`、Platform engineering 页面、Provider detail override/compatibility tab
- 旧 adapter-specs/contract-runs/error-maps route/client/import/DOM/prefetch 的运行时卸载
- 对应 migration/Control/Gateway/Console/performance/security 测试与 `docs/testing/PRD-143-tests.md`

本条不修改已应用 0046/0055/0056/0111/0115 migration，不改变默认 Provider/上游，不把平台角色带入客户 Secret，
不实现 PRD-144 商业账、PRD-145 页面分域、PRD-146 导航合同或 PRD-147 统一迁移治理。

## 13. 两轮编码前自审

### 第一轮：可执行事实、tenant 历史与 Workspace 边界

发现并修正：

1. P0：初稿准备从数据库加载 Adapter spec/代码，会让 DB 写入变成未签名远程执行。修正为可执行实现只在当前
   signed artifact/Shared SDK，DB release 只能引用并核验当前 artifact catalog，不能加载任意代码。
2. P0：把所有 0056 tenant run 去 tenant 后当官方 release proof，会把客户 probe/Endpoint 结果泄到平台并伪造 CI。
   修正为逐行保留 legacy owner/scope，Platform API 裁剪 owner，legacy merge/probe 都不 qualify 新 release。
3. P0：Platform engineer 读取 tenant override pattern 会扩大客户数据面。修正为 Platform catalog 与 Workspace
   override 三表/route/DTO/permission分离；平台兼容投影只有 hash/status/reason，pattern只对当前 Workspace授权角色。
4. P1：0055 每 tenant activation 若迁成一个 Adapter release，会复制同一 Shared catalog 并形成多真值。修正为
   验证所有 activation 后折叠一个 bootstrap release，每条 activation 只保留 mapping/hash。
5. P1：直接删除旧 0046 tables 会破坏 historical provider_error_fact 对旧 head/catalog 的引用。修正为 mapping +
   legacy/new source-qualified fact，旧不可变历史保留，新 Gateway head切换后才写新 fact。

结论：第一轮 P0=0（3 项已关闭），P1=0（2 项已关闭）。继续第二轮审查。

### 第二轮：发布可达性、SoD、Scheduler 与性能

发现并修正：

1. P0：Excel只有 Adapter GET，但要求 engineer运行、release_manager发布；没有命令会使对象/event不可达。修正为
   第7节单一窄 commands route，只引用签名 candidate和固定action，不提供通用Job/代码上传。
2. P0：同一 actor 编写候选、批准失败并发布会绕过供应链门禁。修正为 provider engineer、platform security、
   release manager三人互异，release事务重验各自当前permission/revision和同candidate digest。
3. P0：只隐藏企业 adapter/CI 页面仍保留旧 route、prefetch和顶级 error-map写。修正为0135切换同发布卸载旧
   permission/router/DOM/import/prefetch，ordinary 404；Workspace只留Provider detail override。
4. P1：分别新增 platform contract/catalog scheduler 会增加worker并与两个legacy outbox竞争。修正为一个
   provider engineering supervisor替换两个Control installer，同时保留Gateway request-side cache scheduler。
5. P1：10万case SLO若返回total COUNT或一次加载会不可达且扩大内部信息。修正为run/case双keyset、covering index、
   每页上限和无total；性能fixture验证连续分页而非空缓存。
6. P1：错误catalog release若接受rule JSON会绕过source review和binary matcher identity。修正为API只引用当前签名
   artifact candidate，Control复算固定rule manifest，当前binary catalog hash不匹配即拒绝。

结论：第二轮 P0=0（3 项已关闭），P1=0（3 项已关闭）。两轮自审后无未关闭 P0/P1；G6 未通过前，
本设计不授权创建 0135、修改运行代码或启动 S7 migration。
