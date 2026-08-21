# PRD-095 测试记录

状态：已完成。PRD-095 的实现、0087 迁移、自动化、真实浏览器、并发、性能和敏感审计均已通过；
权威下一项为 `PRD-096 - 平台租户目录与生命周期运营台`。`saas_incremental_roadmap_complete=false`，
`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| Config/DB/API/BB/GR/SEC/FLOW/PERF | PRD-095 Python focused `7 passed` | 三个独立根密钥、AES-256-GCM TOTP、防重放、一次性 bootstrap、13 角色、Platform/Support Session、双域审批、撤销、break-glass、严格 DTO、签名游标、100 并发和 warm p95 |
| Batch Gate | PRD-092..095 统一 Python `76 passed` | 自助注册、Workspace 创建、Membership/切换、Platform IAM、0084..0087 migration guard 和 N/N-1 schema capability 统一复验 |
| WEB/UI/UCD/COMP | PRD-095 focused Console `9 passed`；Console full `991 passed`；lint `315 files` | 三凭据域、敏感响应拒绝、原子刷新、迟到响应隔离、PlatformSession 401 清理、SupportSession 401 保留平台上下文、14 项授权导航、占位域和 Storage 零调用 |
| Static | 定向 Ruff、Python compile、`git diff --check` 通过 | Platform/Identity/共享 MFA、测试、迁移和 Console 变更；仅保留既有 `.env.example` CRLF 提示 |

## 迁移与数据验收

- 0087 建立 MFA factor/attempt guard、冻结的 13 角色与 18 权限目录、Operator/role binding、
  PlatformSession、SupportGrant/approval、SupportSession、break-glass/review、不可变平台审计和 outbox。
- 角色权限目录固定为 51 条映射；`platform_catalog_state` 保存 revision 与 SHA-256，角色、权限、映射和
  catalog state 的 INSERT/UPDATE/DELETE 均由 trigger 阻断，运行时不能漂移或补权。
- pristine 0087 可 down 到 0086 并重新 apply；一旦存在平台安全事实，down 原子失败，审计和业务事实保留。
  `PRAGMA integrity_check` 与 foreign key check 均通过。
- break-glass outbox 固定为 `high` 优先级；普通平台事件保持 `normal`。SupportSession 每次签发均有
  `support.session_issued` 不可变审计。

## 安全与授权结论

- GlobalSession、WorkspaceSession、PlatformSession、SupportSession 和 DeviceIdentity 的解释边界保持互斥；
  当前域 Cookie 重复、Authorization 混入或跨域凭据均失败关闭。
- TOTP Secret 使用独立 AES-256-GCM 根密钥和 account/factor/version AAD；待激活与 active factor 共用
  设备维度尝试锁，激活、PlatformSession 换取及 break-glass 都原子消费单调 counter；锁定期间即使验证码
  正确也失败关闭，重放、损坏密文和失败尝试只返回固定错误。
- 首个 platform_owner 只能通过显式启用的单次 bootstrap ceremony 创建；并发 CAS 最多一次成功，完成后
  重启或重复请求不能重开。
- 13 个平台角色和 18 个权限由后端目录投影。Operator revision、factor revision、GlobalSession 和 role
  fingerprint 每请求复核；角色或状态变化后的下一请求立即撤销旧 Platform/Support Session。
- SupportGrant 只有 `workspace.diagnostics.read`，要求客户 Workspace owner/admin 与不同平台 reviewer
  双域审批；requester 不能自批，且只有 grant 首次转为 active 时生成唯一 `support.access_granted` outbox。
  SupportSession 每次复核 GlobalAccount 状态/login、factor owner/revision 和双域权威；任一漂移统一 401 并
  原子标记 Session revoked。诊断响应无成员、邮箱、Key、Token、Prompt、Response、文件或日志正文。
- audit cursor 使用独立 HMAC，绑定 PlatformSession、Operator revision、角色摘要、可见 scope、方向、时间和
  audit ID；篡改或跨会话复用固定 422。owner 可见全域，security/compliance 只匹配显式安全动作白名单，
  support 只见自己的 SupportGrant 审计；未来 billing/growth 动作不会因默认全选而泄露。

## 并发与性能

- 同一 SupportGrant 的 100 个平台审批并发和 100 个 Workspace 审批并发均恰有一次 200，其余 99 次 409；
  不存在双批准或中间 active 缺口。
- 同一 active Grant 的 100 个 SupportSession 并发签发全部成功，数据库中有 100 个不同 token HMAC，且
  每个签发都有独立审计。
- 暖路径在预热后执行 40 次采样，nearest-rank p95 断言通过：PlatformSession `<50 ms`、Operator list
  `<300 ms`、SupportGrant list `<300 ms`。测试未放宽容量限制、事务或权威复核。

## 浏览器验收

- 应用内 Browser 使用本地测试账号完成 PlatformSession MFA 换取；验证码输入后立即从临时文件、变量和
  DOM 清除，截图及本记录不包含凭据。
- platform_owner 收到后端授权的 13 个业务域，加本地独立“概览”共 14 个导航项。Operators、Support、
  Audit 使用 PRD-095 数据 API；Workspaces、Billing、Verification、Compliance、Privacy、Releases、
  Provider engineering、Channels、Customer success、Growth 显示无伪造统计的安静占位。
- 390、1280、1440 三档实测 root 横向溢出为 0、控件文本裁切为 0、导航项为 14；Workspaces/Billing 占位和
  概览交互均通过，验收结束后已恢复临时 viewport 覆盖。
- 安全截图位于 Git 忽略目录 `artifacts/prd095/browser/`：
  `platform-workspaces-390-final.png` SHA-256
  `fe7c14c978845a59400cfaa2e8eed1c562d8da8ebed6b110b13c4605104a4812`；
  `platform-billing-1280-final.png` SHA-256
  `be36c00b040b99a00568f54dc472e4987d7f394ce6878970a3465c4382c53dd5`；
  `platform-overview-1440-final.png` SHA-256
  `5a88435a3d3276ce642f602b98be951a6a46b052e9d2af5ee164cc04599e6804`。

## 敏感审计

源码、测试、迁移、文档和生成 Excel 执行私钥、云密钥和生产 token 模式扫描；新增命中为 0。Git 候选中
没有 SQLite、WAL、SHM、TOTP 输出文件、浏览器 profile、网络正文或原始 Cookie。测试凭据只存在于受控
本地测试夹具，不进入本记录、截图或提交说明。

本记录关闭 PRD-095；下一权威需求为 `PRD-096 - 平台租户目录与生命周期运营台`。
