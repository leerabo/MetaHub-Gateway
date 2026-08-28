# PRD-130 测试记录

状态：已完成。隔离环境签名包、双向异步交换、显式冲突解决、Shared SDK/CLI、Control API、Workspace/Platform Console 与 0122 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-131 - 旧租户、直连与私有部署迁移切换向导`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0122 Migration | `0121 -> 0122 -> 0121 -> 0122`、Workspace/Platform 权限矩阵、复合 owner、不可变 request/bundle/conflict/resolution/event、cursor/outbox 合法迁移、tenant deletion manifest、业务事实 down guard、完整性与外键 |
| Shared SDK / CLI | canonical manifest、用途分域 Ed25519、owner/direction/expiry、每 entry AEAD/AAD/hash/Merkle、key envelope 摘要、严格 archive、容量预算、私有 staging、原子 apply/replay、显式确认和固定输出 |
| Control API | Workspace step-up 导出/导入/resolution 请求、平台非自批批准/拒绝、Idempotency-Key、严格 DTO、no-store、同形错误、version vector 冲突、resolution 新 generation、事件/outbox 与权限漂移 |
| Console | Workspace/Platform 独立 cookie/client/DTO/store、异步管理与最后交换水位、方向/领域/有效期/transfer、待审批、批准/拒绝、冲突与 resolution、空态和响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-130 Python 聚焦套件 | `17/17 passed`；0122、archive/签名/加密/原子 apply、CLI、Control API、冲突 resolution 与双域导航 |
| PRD-130 Console | `7/7 passed`；双域严格客户端、DTO/store、权限、渲染、批准/拒绝与禁止字段裁剪 |
| Python static | PRD-130 变更源码和测试 Ruff 全规则通过 |
| Console lint | `console lint ok (388 files)` |
| Browser `1280x900` | Workspace 显示方向、transfer 水位、2 条待审批和 1 条未解决冲突；Platform 批准 Edge-to-Cloud 导入并拒绝 Cloud-to-Edge 导出后待审批为 0，页面/内容溢出、控件交叠、禁止字段和浏览器日志均为 0 |
| Browser `390x844` | Workspace/Platform 相同业务状态与审批结果可见；页面横向溢出、内容溢出、控件碰撞、禁止字段和浏览器错误均为 0 |
| Roadmap | 生成器自校验、validator tests 与 CLI 通过；Excel 完成水位推进到 PRD-130，下一项为 PRD-131，迁移序列推进至 0122 |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 owner、签名、介质和重放边界。签名覆盖绑定 owner/direction/purpose/generation 的 canonical manifest，manifest 绑定每个 entry 的 media type、size、nonce、plaintext/ciphertext hash 与 Merkle root；archive 在解密前拒绝额外路径、符号链接、压缩和容量异常。Control 仅保存签名/密钥摘要、版本水位和固定安全事件，Console 只接收裁剪 summary；verified staging、entry 内容和 key envelope 不进入云端投影。修复 Platform 离线审批表在 1280px 的 `min-width: 1060px` 溢出后，双视口复验通过。
- 第二轮复核 version vector、原子应用和权限漂移。Edge-to-Cloud 的 cloud/local 双向前进只创建不可变 conflict，既不 last-write-wins 也不移动权威 cursor；resolution 绑定原 conflict、双方 hash、Workspace actor、不同 Platform approver 和更高 generation。重放保持 head/receipt 不变，异常只返回固定错误码；Workspace/Platform cookie、CSRF、step-up、client、DTO/store 和导航权限持续分域。修正后无未关闭 P0/P1。

## API、CLI 与浏览器证据

- 0122 从完整 0121 基线建立 3 项 Workspace 权限、2 项 Platform 权限、request/bundle/transfer/cursor/conflict/resolution/event/outbox/action command 事实、复合 owner 外键、不变/状态迁移触发器和 tenant deletion manifest；空事实可逆，存在业务事实时 down guard 失败关闭，最终 `integrity_check=ok` 且外键违规为 0。
- Shared SDK 构建加密 `.mhb` archive，先验证严格 ZIP 结构、manifest hash/签名、目标、方向、有效期、envelope 摘要与预算，再逐 entry 验证 AEAD/AAD/hash/Merkle 并写入私有 staging。原子 state store 在所有校验完成后切换 head，支持 rename 后恢复与 receipt 幂等；冲突不会改变 head。
- CLI `bundle verify` 只输出固定 bundle/hash/领域摘要；`bundle apply` 要求显式 workspace/tenant/deployment/direction、目标 state root 与确认词，成功或失败均不输出内容密钥、签名、nonce、envelope 或路径。
- Control 闭环覆盖 Workspace export/import/resolution 申请、step-up、CSRF、幂等冲突、平台非自批 approve/reject、签名 bundle、cursor 推进、冲突检测和不可变 resolution。Platform 无法由导入获得 Workspace Membership 或本地 Secret，API 响应统一 `private, no-store`。
- 浏览器使用隔离开发数据库和一次性本地测试凭据，不修改正常开发管理员身份。Workspace/Platform 分别在 `1280x900`、`390x844` 验收；审批后 applied bundle 可见，历史未解决 conflict 保留，禁止字段扫描覆盖 signature、nonce、key envelope、storage path 与 Secret。验收后 UI 登出、视口恢复、凭据/TOTP 变量清空、隔离 Control 停止并恢复正常开发 Control。

## 敏感审计与静止终态

- 提交前审计只输出命中计数，不输出密码、Token、Cookie、TOTP、私钥、签名、nonce、wrapped key、content key、介质正文或本地路径值。精确暂存范围排除未跟踪的 PRD-101..105 用户文档；Console/API/事件/日志不得包含 Secret、Prompt/Response、设备私钥、对象 URL、storage path、key envelope 或自由文本。
- `.tmp`、pytest basetemp、隔离数据库、浏览器截图和日志均为可复现临时证据，正式结论写入本文和 Git 后已删除整个 `.tmp`。正常开发服务恢复为 Console `5173`、Gateway `8000`、Control `8001`，Control health 返回 `200`/`environment=dev`。

## 批次门禁

本条开发期只运行受影响单元/API 冒烟、双域浏览器和静态检查。S5 的 0111..0122 fresh/逐级/down guard、SQLite/PostgreSQL、Windows/Linux、代理/断网、N/N-1、三浏览器/多缩放、SLO、长稳、全介质敏感扫描、性能、安全和静止终态在 PRD-130 独立提交后统一执行 G5，并以独立批次门禁记录和后续提交保存结果。
