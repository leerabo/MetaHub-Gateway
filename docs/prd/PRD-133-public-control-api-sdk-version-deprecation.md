# PRD-133 公共控制面 API、SDK 版本与废弃策略

状态：已完成。依赖 PRD-038/039/042/057/118；范围以 Excel PRD-133 为准。

## Excel 权威合同

- API：`GET /developer/api-versions`、`GET /developer/changelog`、
  `GET /developer/webhook-schemas`、`GET /developer/sdks/{language}`。
- 对象：`ApiVersion`、`ApiContractSnapshot`、`SdkRelease`、`DeprecationNotice`、
  `WebhookSchemaVersion`。
- 事件：`api.deprecated`、`api.sunset_scheduled`、`sdk.released`、`webhook.schema_published`。
- SLO：文档/Schema 查询 p95 < 300ms；SDK smoke 全平台通过；兼容测试覆盖 N/N-1。

## 1. 产品闭环

release_manager 从显式 public registry 构建 canonical OpenAPI 与 Webhook schema snapshot，运行 breaking-change
分类、示例敏感扫描、N/N-1 consumer tests 和 SDK reproducible build。不同发布人完成批准后，系统签名发布不可变
contract/SDK/changelog。developer 通过现有 `developer_portal.read` 查询版本、下载验签 SDK，并查看迁移指南。

废弃先创建至少 90 天的 notice，绑定受影响 operation/schema、替代版本、迁移指南、最后支持日期和通知计划；
到期前持续验证兼容期和活跃使用摘要。sunset 只关闭对应公开版本，不改变底层业务 RBAC，也不能让 SDK 使用默认
凭据或把 Token 写入示例、配置和日志。

## 2. 0125 合同、构建与时序

- ApiVersion/ContractSnapshot 保存 public registry version、canonical digest、compatibility baseline、status、
  producer revision 和签名摘要；不保存内部 route/schema dump。
- SdkRelease 绑定 contract digest、language/runtime version、source/build digest、SBOM/provenance reference、签名、
  smoke matrix 和 immutable artifact receipt。
- DeprecationNotice 使用 `draft|announced|sunset_scheduled|sunset|withdrawn`，保存 replacement、minimum notice、
  adoption watermark、approver 和 deadline；历史 notice 不可覆盖。
- WebhookSchemaVersion 绑定 exact event name/version、JSON schema digest、compatibility outcome 和 sample digest；
  不能发布任意事件 payload。
- scheduler 使用持久 deadline、lease/generation 和 source/outbox；重复发布按 contract/build digest 幂等。

Down 在任何已发布 snapshot、SDK、notice、schema、下载审计或事件存在时拒绝。

## 3. 公开面、权限与 Developer Console

public registry 逐 operation 声明 path/method、auth kind、permission、request/response/error schema、pagination、
idempotency 和 stability；生成器不得遍历私有 router。API 响应保持固定错误码、request_id、no-store 和稳定游标。
SDK 只实现公开鉴权合同，示例使用占位值和安全环境变量名，不生成内置 endpoint credential。

developer/owner/admin 按现有 `developer_portal.read` 只读；release_manager 管理构建，批准者必须是另一平台账号并动态
恢复发布权限。Developer Console 合并 API 版本、changelog、Webhook schema、SDK 下载和迁移指南，不增加普通一级
导航；Platform Console 提供 contract diff、smoke、批准和撤回，不显示内部路由或签名私钥。

## 4. 第一轮自审：公开 registry 与制品

- P0：反射全部 FastAPI router 会发布 internal/platform/machine 路由。修正为显式 public registry 和 exact DTO。
- P0：SDK 模板包含真实 Key 或默认 Token 会形成供应链泄漏。修正为编译期占位值检查、敏感扫描和零默认凭据。
- P0：Webhook schema 可自由扩字段会把正文带出。修正为事件注册表 exact schema，未知字段和敏感字段递归拒绝。
- P1：release_manager 单人构建并发布无法证明来源。修正为可复现 build digest、KMS 签名和不同账号批准。

## 5. 第二轮自审：兼容与废弃恢复

- P0：只比较 OpenAPI 文本会漏语义破坏。修正为 operation、schema、错误、权限、分页、幂等和事件 consumer tests。
- P0：到期 timer 自动 sunset 但通知/outbox 失败会突然中断客户。修正为 deadline 仅创建待决 gate，通知水位、
  兼容期和 release approval 全通过后才切换。
- P1：新合同发布使旧 SDK 下载指向可变文件。修正为版本+digest 地址和不可变 receipt，latest 只是安全索引。
- P1：撤回 notice 删除历史会失去审计。修正为追加 withdrawn decision，旧 notice 和通知事实保留。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0124/0125 往返/down guard；public/private route 负矩阵；OpenAPI/错误/权限/分页/幂等/Webhook breaking diff；
SDK reproducible build、签名、SBOM、Windows/Linux smoke 和 N/N-1；90 天 notice、通知故障、撤回和 scheduler 重启；
动态发布撤权；Developer/Platform 三浏览器三缩放；p95 < 300ms；制品/示例/日志敏感扫描和静止终态。
完整 G6 留到 PRD-138 后统一执行。

实现已交付 0125、显式 public registry、canonical compatibility diff、Python/JavaScript 可复现 SDK、
Ed25519/KMS 签名与浏览器下载验签、不可变 receipt、双人发布、90 天废弃与持久 sunset gate、事件/outbox、
Developer API Docs 和 Platform Releases 工作台。聚焦迁移/API/Shared/Console、必要权限导航回归和静态检查通过；
真实数据库矩阵、FLOW、三浏览器、性能、安全、兼容性和 72 小时长稳仍按批次规则留到 PRD-138 后统一执行，
不能由本条专项结果替代。

## 7. 2026-09-07 数据库兼容补充设计

与 PRD-131/132 合批修复 PostgreSQL schema guard，复用共享表检查，保留固定表闭集和缺表失败关闭。
第一轮自审：不反射公开路由、不扩大 SDK 字段或改变验签合同；测试必须运行构建、独立批准、
下载、废弃调度和撤回全流程，不能只检查空列表。第二轮自审：fixture 核验所需迁移与重复执行，
外键检查使用后端感知实现且不提交事务；数据库克隆独立回收。此次不宣称已覆盖并发发布和
全平台真实 SDK consumer 矩阵，任何新增失败需保留并按实际原因修复。

实施定位到公钥登记的 `INSERT OR IGNORE` 不兼容 PostgreSQL，连同批准时调用的供应链公钥登记
改为 `ON CONFLICT(key_id) DO NOTHING`，随后仍检查公钥摘要与 active 状态。同 ID 不同公钥
必须拒绝且保留原信任；新增真实数据库重复登记/拒绝替换测试，不修改全局 SQL 翻译器。
