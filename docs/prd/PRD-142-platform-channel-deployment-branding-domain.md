# PRD-142 平台、渠道与私有 Gateway 品牌分域

状态：已完成。依赖 PRD-088/093/095/098/120/121/123/139；批次边界见
`S7-platform-operations-console-domain-batch-design.md`。本条只交付 0134、三种品牌 owner、域名验证、资产发布、
私有 Gateway 品牌 LKG、四组 Excel API 和旧品牌迁移，不提前实现 PRD-143..148。

## 1. 产品闭环与非目标

SaaS 主登录页和 Platform Console 品牌只由 `platform_owner` 管理；`platform_channel` 只能管理获得明确伙伴
scope、有效合同和已验证域名的 ChannelBrand；Workspace owner 只能在本 Workspace 的具体私有
`edge_deployment_id` 下管理 DeploymentBrand。三个 owner 使用不同身份、route、DTO、permission、command、
asset head、domain head 和缓存，不能用一个可空 `owner_id` 或 tenant fallback 混合。

Excel 权威合同为：

- `GET/PATCH /platform/branding`
- `GET/PATCH /platform/channel-partners/{id}/branding`
- `GET/PATCH /deployments/{id}/branding`
- `GET /branding/public`

对象固定为 `PlatformBrand`、`ChannelBrand`、`DeploymentBrand`、`VerifiedDomain`、`BrandAsset`、
`BrandMigrationCursor`；事件固定为 `platform_brand.changed`、`channel_brand.changed`、
`deployment_brand.changed`、`brand_domain.verified`。

本条不允许企业 Workspace 修改 SaaS 主登录页、Platform brand 或其他 Workspace/伙伴域名；不支持 SVG、HTML、
CSS、脚本、远程图片 URL、外链字体、任意 MIME、任意 metadata、用户上传证书/私钥或跨 owner asset reuse。
Platform/Channel 公网域名验证不授予 DNS 托管能力，DeploymentBrand 不修改 Gateway 路由、Provider、Secret、
License、desired config 或 release channel。品牌失败不能阻断 Gateway 推理热路径。

现有 `GET/PATCH /branding`、`GET /branding/logo` 和 query-based
`GET /branding/public?tenant_slug=...` 是 tenant-scoped PRD-088 合同。0134 切换后，受保护旧 route 从外部 router
卸载并返回普通统一 404；`/branding/public` 保留路径但改为由受信请求 Host 解析 Platform/Channel brand，不再
接受 tenant_slug。现有公共 Logo route 作为第 7 节的内容寻址资产辅助 route 收缩，不再按 tenant 枚举。

## 2. 身份、owner 与权限

### 2.1 三种 owner

- PlatformBrand 是全局 singleton，由当前 PlatformSession 的 platform owner 管理，不带 tenant_id，也不引用
  Workspace。Platform base domain 和保留 product name 是平台事实。
- ChannelBrand 绑定 PRD-139 `channel_partner_id + owner_workspace_id + owner_tenant_id`，并引用当前 contract
  digest/version。PlatformSession 还必须命中不可变 `platform_channel_partner_scopes`；平台角色名称本身不代表
  可管理任意伙伴。
- DeploymentBrand 绑定 `workspace_id + tenant_id + edge_deployment_id` 复合外键。WorkspaceSession 必须同时匹配
  active Membership、当前 WorkspaceTenantBinding 和当前 deployment；PlatformSession、SupportSession、
  GlobalSession、ChannelMembership、DeviceSession 和 Bearer 都不能调用该 PATCH。

公共读取不接受登录身份决定 owner。Control 只使用经过 trusted-proxy middleware 验证的 canonical request host，
忽略客户端伪造的 Forwarded/X-Forwarded-Host；host 未命中 active verified domain 时返回 PlatformBrand 默认投影，
不存在、待验证、已撤销和未知 host 响应同形，不能形成租户/伙伴枚举 oracle。

### 2.2 固定权限

0134 增加 `platform_brand_permission_extensions` 和 Workspace RBAC 增量：

| Permission | platform_owner | platform_channel | release_manager | Workspace owner |
| --- | --- | --- | --- | --- |
| `platform.brand.read` | 允许 | 否 | 允许 | 否 |
| `platform.brand.manage` | 允许 | 否 | 否 | 否 |
| `platform.brand.release` | 允许 | 否 | 允许 | 否 |
| `platform.channel_brand.read` | 允许 | scoped | 允许 | 否 |
| `platform.channel_brand.manage` | 允许 | scoped | 否 | 否 |
| `platform.channel_brand.release` | 允许 | 否 | 允许 | 否 |
| `deployment.branding.read` | 否 | 否 | 否 | 允许 |
| `deployment.branding.manage` | 否 | 否 | 否 | 允许 |

`scoped` 表示 role permission 与当前 operator-partner active scope 同时成立；Owner 也必须显式映射 extension
permission，不能依赖旧全权限快照。Platform/Channel 的配置提交者与 release actor 必须不同；release_manager
只看安全 config/scan/domain digest，不看伙伴合同正文、Workspace 数据或上传原始元数据。DeploymentBrand 因为
仅在客户自己的设备显示，可由 Workspace owner 在扫描通过后同一 command 发布，不要求 Platform release actor。

旧 `branding.read/manage` 从 Workspace catalog 精确移除；新增 deployment 权限只进入固定 owner/admin 上限，
不自动授予 developer/viewer/finance/security/ops。`platform_access._authority` 使用固定 extension table tuple 和固定
SQL；Workspace authority 使用现有模板迁移与 revision fingerprint。撤权、scope/contract/Membership/deployment
状态变化使下一请求、待发布 command 和迟到响应立即失效。

## 3. 0134 数据模型

### 3.1 PlatformBrand、ChannelBrand 与 DeploymentBrand

三张表都只保存纯文本白名单字段：稳定 brand ID、status、product name、primary/secondary color、login title、
login message、active logo/fav asset head、domain head、published generation、revision、source digest 和 UTC。
字段沿用 PRD-088 的 UTF-8 byte 上限、控制字符拒绝和 `#[0-9A-F]{6}` 颜色，不增加自定义 CSS/HTML。

- `platform_brands`：唯一 active singleton，稳定 `pbr_*`，默认从当前 MetaHub 安全默认值创建；保留平台 base
  domain、published/LKG generation 和 release actor。平台配置 draft 与 public head 分离，未 release 修改不可见。
- `channel_brands`：稳定 `cbr_*`，复合 partner owner、contract digest/version、允许的 product/visual fields、
  domain/asset head、`draft|pending_domain|ready|published|suspended|archived`、revision/generation。Partner 停用、
  contract 过期或 scope 撤销自动停止新 release；公开 head 原子回到 PlatformBrand，不继续展示过期白标。
- `deployment_brands`：稳定 `dbr_*`，复合 deployment owner、visual fields、asset head、
  `draft|ready|published|suspended`、desired/applied/LKG generation、revision 和 Edge ACK 水位。它不含公网登录域名
  也可发布，用于该私有 Gateway 的本地登录/管理壳层。

每种 brand 的 draft、published head 和 immutable release record 分表保存。发布只能把完整已验证 config digest、
asset digest、domain generation 和 actor revisions 原子推进；不存在字段级 partial publish。相同 digest 重放返回
原 generation，无变化 PATCH 失败，不通过 updated_at 制造事件。

### 3.2 VerifiedDomain

`verified_domains` 使用稳定 `vdo_*`，保存 canonical ASCII/punycode domain、owner kind、对应完整 owner FK、challenge
digest、verification method/version、resolver observation digest、certificate/routing readiness、status、generation、
expires/last verified UTC 和 revision。canonical domain 全表唯一，Platform base/reserved suffix 预占；IP、端口、
路径、通配符、公共后缀、localhost/internal/test 和非规范 Unicode 全部拒绝。

Platform/Channel 公网域只使用有界 DNS TXT challenge：challenge 明文只在创建响应出现一次，数据库保存 HMAC；
验证 worker 通过固定递归解析器、DNSSEC 状态和 CNAME 深度/总响应上限读取，不发起用户指定 HTTP、不访问
link-local/metadata/private address。连续两个独立 resolver observation 匹配后才 `verified`；随后必须等受控 edge
route 和托管证书 ready 才能 public activate。证书私钥不进入本模块、DTO、数据库、日志或证据。

域名每 24 小时重验，并在 contract/partner/brand revision、CNAME/TXT 漂移、证书失效或 ownership transfer 时
立即变 `reverification_required`。同一 domain 不能先绑定 channel 又绑定 deployment；释放后有 30 日 quarantine，
防止旧 Cookie/缓存/证书复用。首次 verified generation 写 `brand_domain.verified`；重复观察不发事件。

Deployment 的本地 hostname 由 PRD-120/121 Device/Deployment 管理，不进入 SaaS 公共 host map。若客户选择声明
公网 domain，仍走同一全局 VerifiedDomain 和受控证书流程，但证书与 host resolution 只终止在对应私有 Edge，
中央 `/branding/public` 永不据此返回企业品牌；验证成功可写 `brand_domain.verified`。仅内网 hostname 只进入签名
DeploymentBrand manifest，不宣称全局验证，也不触发该事件。

### 3.3 BrandAsset 与安全发布

`brand_assets` 使用稳定 `bas_*`，每行绑定单一完整 owner，保存 asset kind `logo|favicon`、MIME `image/png`、
私有 staging object key reference、bytes、width/height、SHA-256、scan engine/version/result digest、status
`staged|scanning|clean|rejected|published|quarantined`、created/published UTC 和 immutable content revision。数据库不
保存 base64、对象 URL、文件名、EXIF、ICC profile 或用户 MIME；即使 digest 相同也不向另一个 owner 暴露存在性。

PATCH 只接受裸 PNG base64 的 exact asset command，最大 512 KiB、16..1024 px；路由有界读取后先验证 signature、
chunk CRC、chunk allowlist、非交错 8-bit RGB/RGBA、解压上限和像素预算，再写私有 staging。scanner 复验 PNG、
拒绝 ancillary text/profile/animation/polyglot 和恶意内容。上传请求在 5 秒内返回 staged/clean 状态；扫描未完成时
品牌 draft 可保存但不能 release。旧 asset 被 supersede 后仍保留 release/LKG 引用，不被新上传原地覆盖。

公开资产只由 `published_brand_asset_heads` 解析，使用内容寻址 ETag、精确 Content-Length/MIME、`nosniff` 和不可变
缓存；staged/rejected/quarantined 永不公开。对象存储故障使 release 失败或 public reader 回到完整上一个 LKG，
不能返回缺 Logo 的半新品牌。

### 3.4 BrandMigrationCursor、事件与 Scheduler

`brand_migration_cursors` 逐 legacy `branding_configs`、`branding_events`、`branding_event_outbox` 保存 source ID、
tenant_id、目标 kind/ID、source/target hash、mapping evidence/approval digest、generation、row/outbox/scan/domain
水位和 status。`brand_legacy_object_mappings` 保留所有 `brc_/bev_/bevo_` 到目标/历史记录映射，不覆盖旧 ID。

`brand_commands` 绑定 actor domain、action、完整 owner、Idempotency-Key HMAC、canonical body hash、expected/result
revision、step-up、request/trace 和 UTC；同键同体返回原结果，同键异体 409。Platform/Channel update 和 release
分别使用不同 command/actor，release 事务重验 asset/domain/contract/scope/permission revision。

`brand_events` 与 `brand_event_outbox` 同事务写四个 Excel exact event，payload 只含 brand/domain ID、owner kind、
status、revision/generation、config/asset/domain digest 和 UTC，不含文案、domain、PNG、伙伴名称、Workspace 或
Deployment display。稳定 event ID、subscription snapshot、lease token digest、generation/attempt 和状态机失败关闭。

`brand_scheduler_state` 固定 `legacy_outbox`、`asset_scan`、`domain_verify`、`public_publish`、
`deployment_bundle`、`brand_event_outbox`、`cleanup` job，使用持久 lease/fence/cursor。0134 用一个
`install_brand_domain_scheduler` 替换现有 `install_branding_scheduler` 槽位，不增加 supervisor installer；
PRD-147 再接入统一注册表。

## 4. PRD-088 历史迁移与唯一事实

0134 使用 expand/inventory/explicit-map/quiesce/cutover/contract，不猜 legacy tenant 品牌属于平台、伙伴或哪个
Deployment。

### 4.1 Inventory 与显式映射

1. 只接受 current head 0133，先建目标表、extension permission、mapping/cursor、目标 scheduler fence 和 down
   guard。精确盘点 `branding_configs/events/event_outbox` 行数/hash、非默认字段、Logo bytes/hash/dimension、
   custom_domain、revision、audit、Webhook delivery/dead-letter 和 outbox pending/leased/delivered/no_subscription/
   failed、attempt/generation/subscription snapshot。
2. 完全等于 PRD-088 默认值、revision=1、无 Logo/domain/event 的 config 标记 `default_collapsed`，不为每个 tenant
   复制一张目标品牌；其公共显示统一由 PlatformBrand 提供，原行保留历史 mapping。
3. 任一非默认 config 必须由不可变 migration manifest 指向一个 active ChannelPartner 或一个具体 active
   EdgeDeployment。manifest 保存 source hash、目标完整 owner、依据 code、请求人和不同审批人；映射前同时重验
   partner contract 或 Workspace owner/deployment。无证据、多候选、目标已停用或同 tenant 同时可能属于两类时
   标记 `requires_assignment` 并阻断该品牌激活，绝不默认映射伙伴自身或第一个 Deployment。
4. legacy `custom_domain_status=reserved` 不等于 verified。域名只能进入目标 draft +
   `reverification_required`，重新完成 0134 challenge/cert/route 后才能公开。旧 SaaS tenant slug 不成为 Channel 或
   Deployment domain。

### 4.2 Asset、event 与 outbox 保真

1. 映射后的旧 PNG 按原 bytes/SHA-256/dimension 写 owner-qualified BrandAsset staging，复算结果必须相同，再走
   新 scan；scan 失败保留历史 bytes digest 和 conflict，不发布。目标 brand 保存旧 `brc_*` mapping、全部字段、
   revision/source hash 和原 tenant_id audit，不修改旧配置。
2. `branding_events` 保留 `bev_*`、revision、changed_fields、payload hash、actor/request/trace/UTC；已 delivered/
   no_subscription/failed outbox 原终态不变且不翻译为新事件。pending 仍以原 `branding.updated` stable event ID 和
   frozen subscription snapshot 投递；leased 等原 lease 完成/过期后由更高 fence 接管，未知结果不当成功。
3. 新目标 release 才产生 `platform_brand.changed|channel_brand.changed|deployment_brand.changed`；migration mapping、
   scan 或 domain draft 不伪造客户主动变更。相同 generation 重放只验证 hash/mapping/outbox 水位，不重复 asset、
   event 或 Webhook。
4. 每个 source/target 行数、canonical hash、owner FK、asset hash、event/outbox/audit 和 scheduler watermark 全部匹配
   才允许 cutover；requires_assignment 可作为“历史保留但不激活”的显式终态，不允许 silently drop。

### 4.3 Quiesce、切换与回滚

旧 branding scheduler 先 fence 新 claim，等待 active lease 完成/过期；再记录最终水位，由新 scheduler 的 legacy
job 接管未终态 outbox。切换 CAS 为旧表安装 write guard，使目标 brand/asset/domain 成为唯一 writer；PRD-088
旧表只读等待 PRD-147 contract。

同一应用发布卸载 tenant `GET/PATCH /branding`、`/branding/logo`、tenant_slug public query、Workspace
`branding` Navigation/DOM/prefetch 和 `branding.read/manage`。Platform/Channel/Deployment route 只在各自目标
reconciliation 通过后开放；企业 SaaS 页面没有白标管理，但 private-gateway Deployment detail 可按
`deployment.branding.*` capability 显示本地外观入口。

0134 down 只允许所有 platform/channel/deployment brand、domain、asset、release、mapping/cursor、command、event/
outbox、scheduler 水位均 pristine 且从未回填的空库执行。任一 legacy mapping 包括 default_collapsed 都阻断 down；
空库验证 `0133 -> 0134 -> 0133 -> 0134`。Down 不修改 0081 历史或 0112/0113 Edge 事实。

生产 rollback 只可恢复上一版目标 public/Deployment LKG 和内部兼容 reader，不能解除旧 write guard、恢复 tenant
PATCH、tenant_slug 查询、Workspace branding navigation 或旧权限。目标服务不可用时 SaaS 使用 PlatformBrand LKG，
私有 Gateway 使用本地签名 LKG；不能用旧跨域 tenant config 兜底。

## 5. 状态机、并发与业务规则

Platform/Channel brand 的配置状态为 `draft -> ready -> released -> superseded|suspended`；Deployment 为
`draft -> ready -> published -> superseded|suspended`。draft update 要求 exact If-Match、step-up、幂等和当前 owner；
Platform/Channel release 还要求不同 release actor、clean asset、verified/ready domain、contract/scope 当前有效。
并发 asset scan/domain verify/release 通过 brand revision + generation CAS，迟到 clean/verified 不能发布旧 draft。

域名 claim 使用 canonical domain unique index 作为竞争终检，申请和验证都绑定 owner/generation；同 Idempotency-Key
异 domain/owner 409。验证 observation 只推进当前 challenge，旧 TXT 不能验证新 owner。domain suspend/release 会
原子撤下 public map 并推进 cache epoch，边缘迟到更新被 fence。

配置可观察 digest 或 published head 变化时写对应 brand changed event；仅 draft、scan 进度、updated_at 不发。
Domain 首次进入 verified 且 owner generation 当前时写 domain event；证书/routing ready 只是发布前置，不重复发。
所有 event 与 public cache invalidation 在 release 事务同源，worker 重放不得形成第二 generation。

## 6. 四组 Excel API exact 合同

所有受保护模型 `extra=forbid`，GET 拒绝未定义 query，PATCH 拒绝重复/未知 JSON key、未知 action、额外 header、
非法 UTF-8、控制字符和资源攻击。写请求要求正确 session 域、exact Origin/CSRF、one-shot step-up、
Idempotency-Key 和 quoted If-Match；响应带 ETag、request/trace、capabilities、source/scan/domain/LKG 状态和
`Cache-Control: private, no-store`。

### 6.1 `GET/PATCH /platform/branding`

GET 要求 PlatformSession + platform.brand.read，返回 PlatformBrand draft/published revision、纯文本 fields、asset
摘要、base domain 状态、published/LKG generation 和 actions；不返回 PNG、domain challenge、object key、证书或
scanner details。PATCH exact action 为 `update|attach_asset|remove_asset|request_domain|release|suspend|rollback_lkg`；
manage actor 只能 update/asset/domain，release actor只能 release/suspend/rollback，服务端按 permission 再裁剪。

### 6.2 `GET/PATCH /platform/channel-partners/{id}/branding`

GET/PATCH 先加载 PRD-139 partner 完整复合 owner，再验证 platform partner scope/contract。错 scope 与不存在同形
404，不返回其他 partner 是否存在。GET DTO 与 Platform 独立，加入 contract status/digest、partner scope revision，
不含合同正文或客户归属。PATCH action 同 Platform，但 domain 必须在合同允许 suffix/数量内；partner suspend 或
contract 过期时只能 read history/suspend，不能 release。

### 6.3 `GET/PATCH /deployments/{id}/branding`

WorkspaceSession 必须匹配 current workspace/tenant 和 EdgeDeployment，GET 需要 deployment.branding.read；PATCH
需要 manage。请求不能带 workspace/tenant owner，错 owner 同形 404。DTO 只含本 deployment visual draft、asset、
published/applied/LKG generation、Edge ACK/freshness 和 actions，不含 Platform/Channel brand、其他 deployment、
desired config 或 device secret。PATCH action `update|attach_asset|remove_asset|publish|suspend|rollback_lkg`；publish
生成独立签名 brand bundle，不修改 config snapshot 或 routing desired state。

### 6.4 `GET /branding/public`

不认证、不接受 query/body/tenant slug，只从 canonical trusted Host 解析 active public head。返回固定 exact fields：
product name、两色、login title/message、logo/favicon content-addressed same-origin path、brand generation 和 public
ETag；不返回 owner kind/ID、domain status、partner/tenant、revision、fallback reason 或内部错误。未知/无效/待验证/
撤销 host 返回同形 PlatformBrand LKG，避免枚举。JSON no-store；内容寻址资产可 immutable cache。

## 7. 公共资产与 Deployment LKG 辅助合同

Excel 的 BrandAsset 无法由 JSON 元数据直接显示，保留一个非业务选择面的辅助读取：
`GET /branding/public/assets/{asset_id}`。asset_id 是 public head 发出的随机 stable ID，不接受 owner/hash/query；route
重新由 request Host + published head 校验 asset 归属，错 host/旧 head/不存在同形 404。响应只含已发布 PNG，固定
MIME/Length/ETag、`nosniff`、CSP sandbox 和 `Cache-Control: public, max-age=31536000, immutable`。旧
`/branding/public/logo?tenant_slug=` 从切换起普通 404。

DeploymentBrand publish 生成独立 RFC8785 manifest，包含 deployment owner、brand generation、纯文本配置、asset
digest/bytes 和过期窗口，由 PRD-121 Edge trust root 签名并通过 DeviceSession 拉取。它不改写
`edge_desired_states` 的 config source invariant。Edge 先验签、校验 owner/generation、下载内容寻址 asset、复算
digest/PNG，再原子切换 applied head；任一失败保持前一个完整 LKG。ACK 回传 applied/LKG generation/digest，迟到
ACK 不能倒退 head。离线、Control 不可达、证书轮换或新品牌损坏时仍显示最近已验证 LKG，不显示半包或默认远程图。

## 8. Scheduler、传播与 SLO

brand scheduler 所有 job 使用持久 lease/fence/有界 batch。asset scan 和 domain verify 独立重试；public_publish
在 release commit 后推进 signed cache epoch，边缘节点只接受更高 current generation；deployment_bundle 保存 ACK/
LKG 水位；legacy_outbox 保持 PRD-088 投递语义。失租 worker 停止提交，接管从持久 cursor 继续。

专项 SLO：`GET /branding/public` p95 < 200 ms，测量包含 trusted Host 解析、domain/public head 与 fallback；已成功
release 的 public brand 99% 边缘节点 < 5 秒可见，以 generation ACK/真实 GET 证明；最大允许 PNG 的 PATCH 上传
响应 < 5 秒，包含有界读取、结构校验、staging commit，但异步恶意扫描/发布不伪装成已完成。私有 Deployment
离线传播不纳入 5 秒公网 SLO，必须证明 reconnect 后有界接管和 LKG 连续显示。

## 9. Console 与页面分域

Platform Branding 页面使用独立 Platform client/store/epoch，显示 Platform draft/release、域名和 asset 状态；
Channel brand 位于 Platform Channels 伙伴 detail。Workspace 不再有 SaaS `branding` 顶级页，只有 Private Gateway
Deployment detail 的“外观”tab，使用 Workspace client/DTO。三个页面可共享纯视觉字段/PNG picker 组件，但不共享
auth client、owner state、cache、command builder 或响应 DTO。

固定 loading、empty、staged/scanning/rejected、domain pending/reverification、ready、release pending、published、
stale/LKG、permission revoked、conflict、dependency unavailable 状态。按钮只按 capabilities 启用；离页、换
Workspace/partner/deployment、撤权、401/403 和迟到响应清除 draft、PNG object URL、step-up 和 owner epoch。

登录页首屏先请求 host-scoped public brand，再安全设置 textContent、CSS variables 和同源 asset；不把 host/brand
写 localStorage。390/1280/1440、DPR 1/1.25/1.5 无重叠、裁剪、横向溢出或文本遮挡；Logo 有稳定尺寸与 alt，
键盘/焦点/读屏可用。品牌不能改变安全告警、权限文案、法律链接或模拟浏览器/Platform 身份。

## 10. 隐私、安全、审计与恢复

- DNS challenge 明文、PNG base64、object key、certificate/key、domain 原文、伙伴合同正文、Workspace/Deployment
  display 不进入 event、Webhook、audit summary、日志或正式证据。Domain 在受保护 DTO 仅对当前 owner 授权角色显示，
  公开 DTO 不回显 request host。
- BrandAsset 通过结构解析、解压预算、恶意扫描、私有 staging、owner binding、immutable release 和 CSP 防止图片
  polyglot/存储型注入；浏览器只创建受控同源 URL，离页 revoke 本地 object URL。
- 审计记录 actor domain/ID、action、完整 owner、安全 changed field list、before/after revision、config/asset/domain
  digest、permission/step-up/scope/contract revision、outcome 和 request/trace；不记正文或挑战值。
- 数据库、DNS、scanner、object store、edge cache、signing 或 Gateway 离线均失败关闭。release 未提交不改 public
  head；public 新 generation 不完整时回完整 LKG；Deployment bundle 未 ACK 不覆盖 LKG；rollback 是显式 audited
  generation，不原地修改历史 release。

## 11. 聚焦测试与批次证据

G6 通过后本条开发只运行受影响测试：

- Migration：0133/0134 fresh、空库 down/re-up、default collapse、非默认显式 mapping/冲突、Logo/hash/event/outbox/
  audit/scheduler 水位、任一事实 down guard、旧 route/permission 收缩、FK/integrity。
- Identity/RBAC：Platform owner/channel scoped/release manager/Workspace owner exact 矩阵；所有错域 Session、跨 partner/
  Workspace/deployment、动态撤权、contract/scope/Membership 停用和普通 404。
- Domain：canonical/global unique、reserved/public suffix、challenge replay、双 resolver、DNS 上限、漂移/reverify、
  certificate/routing readiness、quarantine 和 host header/proxy spoof。
- Asset：PNG signature/chunk/CRC/尺寸/解压/polyglot、大小边界、private staging、scan reject、跨 owner digest 隔离、
  content-addressed public route、旧 head/错 host 和 object store failure LKG。
- State/Event：三 brand 状态机、update/release actor separation、幂等/CAS、迟到 scan/domain/release、四 exact event、
  legacy stable outbox、scheduler fence/接管。
- Edge：独立 signed brand manifest、owner/generation/digest、download/ACK、损坏包、离线/reconnect、完整 LKG，证明不改
  PRD-121 config source/routing state。
- Console：三个 auth client/DTO/store 不混用、object URL 清理、host public login、旧 tenant branding DOM/prefetch/API
  归零。完整三浏览器、全性能/安全/回归留到 PRD-147 后 G7。

测试记录写 `docs/testing/PRD-142-tests.md`；正式 JSON/截图/manifest/hash 进入 `.artifacts`，`.tmp` 不作为证据。
敏感审计覆盖 staged diff、API headers/bodies、DNS/asset fixtures、日志、events、artifacts 和浏览器 Storage/DOM；
任何 challenge、PNG body、domain/secret 明文泄漏阻断独立提交。

## 12. 预计改动边界

- `infra/migrations/0134_platform_channel_deployment_branding.sql` 与 `.down.sql`
- `control-plane-api/src/metahub_control/brand_domains/`
- 现有 `branding/` legacy source/outbox 接管及 `app.py`
- `platform_access/`、Workspace `auth/console`、PRD-139 channel 与 Edge brand bundle/ACK 适配
- public brand/asset trusted-host middleware 与 shared exact error/webhook catalog
- `console-web/src/lib/platformBranding.js`、`deploymentBranding.js`、Platform/Channel/Deployment 页面
- 旧 tenant branding route/client/import/DOM/prefetch 的运行时卸载
- 对应 migration、Control、Edge、Console、domain/asset/SLO 测试及 `docs/testing/PRD-142-tests.md`

本条不修改已应用 0081/0112/0113 migration，不把 partner profile/合同或 Edge desired config 复制为品牌，不实现
PRD-143 Provider 工程、PRD-144 商业账、PRD-145 页面分域、PRD-146 导航合同或 PRD-147 统一迁移治理。

## 13. 两轮编码前自审

### 第一轮：owner、历史映射与公共解析

发现并修正：

1. P0：初稿按 legacy tenant 自动映射第一个 EdgeDeployment，会把原 SaaS 登录品牌错误写入任意私有 Gateway。
   修正为 default collapse + 不可变显式 migration manifest；非默认、多候选或无证据保持 requires_assignment，
   未明确 owner 永不激活。
2. P0：初稿保留 `tenant_slug` public query，会让企业继续控制 SaaS 登录页并保留 tenant enumeration。修正为
   `/branding/public` 只按 trusted canonical Host 解析 Platform/verified Channel；未知 host 同形回 Platform LKG。
3. P0：用一个 brand 表和可空 owner_id 会允许 PlatformSession 更新 Deployment 或 WorkspaceSession 更新 Channel。
   修正为三张 owner 表、三套完整 FK、独立 route/DTO/permission/command/cache；公共 map 只引用 released head。
4. P1：把 PRD-088 reserved domain 当 verified 会在未证明控制权时接管域名。修正为所有 legacy domain 重新
   challenge、双 resolver、certificate/routing readiness，未完成只留 draft。
5. P1：同 SHA-256 跨 owner 复用 asset row 会泄漏另一个客户是否上传同图。修正为 owner-qualified asset row 和
   public head 校验；物理存储即使内部去重也不能影响授权/错误/时序。

结论：第一轮 P0=0（3 项已关闭），P1=0（2 项已关闭）。继续第二轮审查。

### 第二轮：资产、Edge LKG、并发与切换

发现并修正：

1. P0：直接把 Deployment brand 塞进现有 config snapshot 会改变 PRD-121 source manifest/hash，甚至触发路由配置
   发布。修正为使用同一 trust root 的独立 signed brand bundle、generation/head/ACK/LKG，不改 desired config。
2. P0：新 asset scan 成功后直接更新 public head，可能把旧 draft 的迟到扫描发布到新配置。修正为 scan、domain、
   release 全部绑定 brand revision/generation/config digest，release 由 CAS 原子推进完整 head。
3. P0：只隐藏 Workspace branding 页面仍保留 tenant PATCH/public slug/Logo route。修正为 0134 切换同发布卸载旧
   permission/router/query/DOM/prefetch，ordinary 404；rollback 永不恢复跨域写。
4. P1：增加新的 scheduler installer 会让 legacy branding outbox 双消费并扩大 supervisor 数。修正为
   `install_brand_domain_scheduler` 替换原槽位，legacy job 接管原 stable event/outbox 水位。
5. P1：公开品牌 JSON 与图片使用同一短缓存会在传播和带宽之间失控。修正为 JSON no-store + generation，已发布
   content-addressed asset immutable cache；release 同事务推进 public epoch，99% < 5 秒按真实 GET/ACK 测量。
6. P1：release_manager 若能看到原 PNG/domain challenge/合同正文会扩大平台数据面。修正为 release DTO 只含安全
   config/scan/domain/contract digest 和 readiness，资产正文与 challenge 均不可读。

结论：第二轮 P0=0（3 项已关闭），P1=0（3 项已关闭）。两轮自审后无未关闭 P0/P1；G6 未通过前，
本设计不授权创建 0134、修改运行代码或启动 S7 migration。
