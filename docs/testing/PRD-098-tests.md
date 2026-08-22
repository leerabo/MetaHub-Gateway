# PRD-098 测试记录

状态：实现、自动化门禁、真实浏览器验收与敏感审计均已完成。权威下一项为
`PRD-099 - 平台计量归集与账单摘要`。`saas_incremental_roadmap_complete=false`，
`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB | migration `3 passed` | 0089->0090、fresh install、pristine down/up、非 pristine 回退阻断、不可变触发器、完整性与 FK |
| API | SaaS billing smoke `1 passed` | Platform 创建/发布、公开目录、真实 Global 登录与 Workspace 切换、订阅创建/重放、套餐变更、scheduler 单轮到期与幂等重跑、请求守卫兜底、权益与订单、混合凭据负矩阵 |
| WEB | PRD-094..098 affected `30 passed` | Workspace Cookie/CSRF/CAS/幂等、Platform CSRF、角色裁剪、HTML 转义、响应式 DOM |
| Static | Console lint `319 files`、定向 Ruff、compileall 与 `git diff --check` 通过 | PRD-098 变更集与既有 Platform/Workspace 页面兼容；scheduler 异常正文不进入日志 |

## 迁移与领域结论

- 0090 独立创建 `saas_plan_catalogs/saas_plan_versions/workspace_subscriptions/subscription_trials/`
  `subscription_order_records/entitlement_snapshots/saas_subscription_events`，不复用 PRD-079 BYOK 表。
- published 版本与 EntitlementSnapshot 不可变；catalog 保留稳定 code/revision。金额使用币种小单位整数，
  权益 JSON 规范化并保存 SHA-256。
- 免费与可用试用在创建事务中形成权益；付费无支付事实时保持 pending。套餐变更保留当前快照并登记未来
  版本，不删除、不停用 Workspace 资源。
- 固定轮询 scheduler 每轮最多处理 50 项，并与请求守卫复用同一原子状态机；存在已发布免费版本时，按
  目录展示顺序确定性回落并追加免费订单、权益快照和到期事件；不存在免费版本时安全进入 expired，不继续
  授予付费权益。重复调度和后续请求均不产生重复副作用。
- pristine 数据可回退并重新 apply；存在商业事实时 down 原子失败。fresh 与升级库均通过完整性和 FK 检查。

## 授权与客户端结论

- 公开目录、Workspace 订阅与 Platform 目录是三个独立凭据域。混合 Bearer/Workspace/Platform/Support
  凭据在解析请求正文前失败关闭，Workspace/Tenant owner 均由 Session 恢复。
- Workspace 客户端只使用 Cookie，写请求携带当前 Workspace CSRF、幂等键和变更 CAS；请求正文不包含
  tenant、workspace、金额或权益。Platform 发布只使用 PlatformSession 与 Platform CSRF。
- Identity Workspace 工作台只向 owner/finance_admin 渲染套餐与权益；其他角色零 DOM。Platform Billing
  页面按 `platform.catalog.read/write` 分离列表与发布表单。

## 浏览器验收

- 本地数据库迁移至 0090；按 0087/0090 约束建立一个 `platform_owner` 测试上下文并关闭一次性 bootstrap
  状态。通过真实页面完成 GlobalSession 登录、TOTP enrollment/activation 和 PlatformSession 换取；密码与
  TOTP 输入提交后立即清空。
- Platform Billing 通过真实表单发布 `starter/growth` 两个不可变版本；Workspace owner 通过真实表单创建
  `starter` 试用订阅并登记 `growth` 套餐变更。页面显示试用、待支付订单、当前权益与待生效版本。
- Workspace 与 Platform Billing 在 390、1280、1440 三档的 root 横向溢出、未受控越界元素和交互控件
  重叠均为 0；浏览器 console error/warning 为 0，密码/TOTP 非空输入均为 0。
- 安全截图位于 Git 忽略目录 `.tmp/prd098-browser-evidence/`。Workspace 390/1280/1440 SHA-256 分别为
  `696b0547e4ae12e1c1367c474971f657238c11696e670b4b583e3735f1273606`、
  `5aceadd324a58aba934691262b53a69d5db9cdb991be6c9863013f95117dad4a`、
  `5f67fb5b3d3c2a4f0b2bb074bf21927952f256e862d1462cccc45fc3e036c7a3`；Platform 390/1280/1440
  SHA-256 分别为 `f2ab39da6866166668c8d42eccd054a9e71a932db3b8d26b91e7a24f9a60cc85`、
  `27a9b61a2bcf38eac01b0cd5924a83cb3deaf25ad3aaeb5fce20ce4b0bec77de`、
  `1c1b2574b134f4292e9f6d2f3e2e6c158c68ae2a14b9aca1a4aad9fef2ddc496`。

## 敏感审计

源码、测试、迁移和文档候选执行私钥、云密钥、Bearer/JWT、Cookie 值与 TOTP URI 模式扫描；真实敏感值
命中为 0。唯一 Cookie 模式命中是 PRD-098 单元测试中的合成 CSRF canary，不是运行时凭据。Git 候选不含
SQLite/WAL/SHM、浏览器 profile、截图、Cookie、TOTP 或原始网络正文；PRD-099..105 设计稿不进入本次提交。
测试凭据只用于本地受控输入，未进入回复、日志、截图、文档或正式证据。

本记录关闭 PRD-098；下一权威需求为 `PRD-099 - 平台计量归集与账单摘要`。
