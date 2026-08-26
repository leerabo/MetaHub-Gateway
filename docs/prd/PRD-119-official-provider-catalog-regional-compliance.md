# PRD-119 官方 Provider 连接目录与区域合规接入

状态：已完成。0111、签名目录、Workspace 实例化/预检、Platform 发布、企业与平台 Console、
专项/必要回归、真实 API/Browser、性能与敏感审计均已通过。依赖 PRD-011..014/063/064/084/108；
范围以 Excel PRD-119 为准。

## Excel 权威合同

- API：`GET /provider-catalog`、`POST /providers/from-catalog`、
  `POST /providers/{id}/preflight`、`GET /provider-catalog/{id}/compatibility`。
- 对象：`ProviderCatalogEntry`、`ProviderCapability`、`RegionalEndpointTemplate`、
  `ProviderPreflightRun`、`CompatibilityProfile`。
- 事件：`provider_catalog.updated`、`provider.preflight_completed`、
  `provider.compatibility_changed`。
- SLO：目录查询 p95 < 300ms；预检状态 < 30s 或明确超时；目录更新传播 < 5 分钟。

## 1. 产品闭环

platform_ops/release_manager 发布 Ed25519 签名、不可变版本的 Provider 目录。内置条目覆盖智谱、通义、
月之暗面、文心、火山、混元、DeepSeek 的官方/云服务 endpoint，以及客户本地 OpenAI-compatible 模板；
OpenRouter/硅基流动只作为迁移兼容来源说明，不作为默认上游或共享客户接入。

企业 admin 从目录显式选择 entry/region/capability 创建本 Workspace Provider，随后通过既有 Provider Key 或
Local Endpoint 流程配置自持凭据。preflight 只访问目录签名 endpoint 或已验证 Local Endpoint，返回固定能力/
连接 outcome；失败不保存明文凭据或响应正文。目录更新只提示兼容变化，不自动修改租户 Provider。

## 2. 0111 数据与签名

- `provider_catalog_versions`：version、canonical manifest hash、Ed25519 key ID/signature、status、published actor/time。
- `provider_catalog_entries`：version、entry code、vendor、service class、auth modes、data region/compliance tags、
  `active|deprecated|retired`，不可变。
- `provider_capabilities`：entry/version、capability code、limits/stream/tool/file flags，使用受控标量，不含 adapter CI。
- `regional_endpoint_templates`：entry/version、region、固定 HTTPS host/path template、cloud service、residency tags。
- `compatibility_profiles`：entry/version、公开请求差异、迁移 source、fixed compatibility level/doc code；不含内部规则。
- `provider_catalog_instances`：tenant/workspace/provider、entry/version/region/capability selection、revision。
- `provider_preflight_runs`：owner/instance/config revision、status、latency、fixed outcome、capability hash、timeout/time。
- `provider_catalog_events/outbox`：版本、entry/instance、fixed outcome、revision/time；无 Key/Token/endpoint secret/body。

Down 在非种子 version、instance、preflight、compatibility change 或未投递事件存在时拒绝。种子目录必须由
当前部署信任 key 验签，checksum 变化即 migration/启动失败。

## 3. 目录边界与 SSRF

发布时 canonical parser 拒绝未知字段、userinfo、query secret、fragment、非 HTTPS 官方 endpoint、IP literal、
loopback/private/link-local/metadata host、通配符 host 和任意 redirect。运行时实例化和 preflight 再验签并
按 entry/version/region 重建 endpoint，不相信客户端 URL。DNS 每次连接执行公共地址校验并禁止 redirect。

Local OpenAI-compatible 不写入普通 `providers`，而是进入既有 `local_endpoints` owner/网络校验；目录只提供
字段模板与兼容要求。Provider Key 继续使用客户 Workspace KMS/密文域，目录、Platform 角色和 preflight 不能
读取或复用。平台目录不包含“共享客户 Key”或平台代持租户凭据。

## 4. 权限、DTO 与 Console

- `provider_catalog.read` 给企业 owner/admin/developer 按只读目录；`provider.write` 才能实例化和 preflight。
- `platform.provider_catalog.manage` 给 platform_ops/release_manager；Platform Security 只读签名/风险摘要。
- Excel 冻结的四个公开 API 面向企业消费；目录发布使用独立 Platform Cookie/CSRF 的
  `POST /platform/provider-catalog/versions`，只接受 canonical manifest、key ID、signature、If-Match 和确认，
  不与企业 `GET /provider-catalog` 共用写权限或 DTO。
- 企业 DTO 只含 vendor、公开 endpoint region、capability、auth requirement、residency/compliance 和迁移文档 code；
  禁止 adapter spec、CI case、内部错误映射、成本合同、运营开关、其他租户使用量或平台配置。
- Provider 页面新增 Catalog tab、筛选、实例化、凭据后续动作、preflight/checkpoint 与兼容提示；Platform 页面
  是独立发布/签名/传播入口，不复用企业 DOM。

## 5. 第一轮自审：目录信任与凭据所有权

- P0：客户端提交 base_url 绕过签名目录会形成 SSRF。修正为只提交 entry/version/region，服务端重建 endpoint。
- P0：平台目录携带共享客户 Key 会跨租户泄漏。修正为目录完全无 secret，租户用既有自持 Key 流程。
- P0：Local Endpoint 当普通 Provider 会绕过网络 ACL。修正为路由到 PRD-084 local_endpoints 边界。
- P1：目录更新自动改租户 endpoint 会造成未授权中断。修正为 immutable instance version 和显式 CAS 升级。
- P1：只校验 manifest hash 不校验 publisher。修正为 Ed25519 trust key、key ID、version 与 canonical signature。

## 6. 第二轮自审：企业投影与预检

- P0：企业 DTO 暴露 adapter CI/内部错误规则触发 R-15。修正为独立 allowlist DTO 和源码导航合同测试。
- P0：preflight 保存上游 body/header 会泄漏。修正为 latency、fixed outcome、capability hash 和 request_id。
- P1：DNS 初验后连接时 rebinding。修正为解析结果与 socket peer 双校验，redirect 禁止。
- P1：兼容文档把竞品作为默认推荐。修正为 migration-source-only 标记，官方/云服务和本地模板才可实例化。
- P1：目录退役立即禁用租户 Provider。修正为只提示 deprecated/retired；安全阻断必须有独立风险策略和审计。

## 7. 专项验证计划

0110/0111 fresh/往返/down guard；seed checksum/Ed25519/tamper/key rotation；官方 entry/capability/region 矩阵；
实例化 owner/CAS；Provider Key/Local Endpoint 自持边界；SSRF/DNS rebinding/redirect；preflight timeout/恢复；企业与
Platform DTO 源码导航隔离；目录传播 SLO；Console 三浏览器三档缩放；凭据、内部配置和响应正文敏感扫描。
