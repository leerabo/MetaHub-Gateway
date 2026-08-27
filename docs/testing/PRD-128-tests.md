# PRD-128 测试记录

状态：已完成。私有 Gateway License、容量限制、离线宽限、在线同步、离线导入、Workspace/Platform Console 和 0120 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-129 - 私有Gateway备份、恢复与灾备演练`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0120 Migration | `0119 -> 0120 -> 0119 -> 0120`、权限与角色矩阵、复合 owner、不可变 entitlement/事件、容量约束、租户删除 manifest、业务事实 down guard、完整性与外键 |
| License Control | 平台签发/扩容/吊销、Workspace 安全摘要/刷新、Agent pull/ACK/Usage bucket、严格 DTO、ETag/CAS、幂等、no-store、同形 404 与统一错误合同 |
| Shared/Gateway/CLI | Ed25519 用途分域签名、exact owner/hash/generation、原子 LKG、可信时钟、离线宽限、明确吊销、原子容量 lease、在线同步、私有文件/stdin 导入和 verify-before-apply |
| Console | Workspace/Platform Cookie/CSRF/DTO/store 隔离、权限扩展白名单、容量/使用量/拒绝数/到期/宽限/ACK 安全摘要、签发/扩容/吊销、空态和响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| 0120 Migration | `2/2 passed`；往返、权限/manifest、完整性、外键和业务 down guard |
| Control API | `1/1 passed`；签发、pull、ACK、Usage bucket、Workspace refresh、扩容和吊销完整闭环 |
| Shared SDK 与 CLI | `11/11 passed`；签名、LKG、宽限/吊销、容量、可信时钟、热路径和两种离线导入 |
| Gateway synchronization | `3/3 passed`；pull/apply/ACK/replay、网络失败保留 LKG、错误签名失败关闭 |
| Workspace/Platform navigation | `4/4 passed`；PRD-127/128 双域导航权限回归 |
| PRD-128 Console | `4/4 passed` |
| PRD-120..128 Console 必要回归 | `45/45 passed` |
| Python static | PRD-128 变更源码与测试 Ruff 全规则通过；规划脚本按既有规则排除历史 `E501` 后通过 |
| Console lint | `console lint ok (384 files)` |
| License hot path | 本地原子容量决策 `p95 < 2ms` 断言通过 |
| Browser 1280x900 | Workspace 六个 Deployment License 空态和 Platform 签发/清单均可见；页面无横向溢出、控件交叠或浏览器错误 |
| Browser 390x844 | 两域 `body`/根滚动宽度均为 `375`，控件交叠和 License 内容溢出均为 `0`；Platform 导航自身为有界横向滚动区，不扩张页面 |
| Roadmap | generator 自校验通过；validator tests `4/4`、CLI `174/174` 通过；Excel 的 `20` 个 ZIP 条目完整且下一项为 `PRD-129` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按 owner、权限与商业边界复核。License 只控制软件能力和容量，不授予 Workspace 或数据访问；Platform billing 可签发但不读取租户正文，Workspace 只返回自身安全摘要，Agent 只能拉取自身 Deployment 的 entitlement。修正了不存在的 `platform_auditor` 角色，并将 `edge.license.import/read/refresh` 纳入 Console 后端扩展权限白名单和导航回归。
- 第二轮按失败关闭、恢复与敏感生命周期复核。SignedEntitlement 绑定 purpose、owner、generation 和 canonical hash；无效签名、乱序、时钟回拨、显式吊销、容量竞争和网络失败都保留或关闭到确定状态。新增统一 `edge_license.*` 错误目录，避免未签发的 404 被错误降级为 500；补上 ACK 后新增 Usage bucket 的幂等回归。修正后无未关闭 P0/P1。

## API、性能与浏览器证据

- 0120 从完整 0119 基线回填权限与 License 目录，验证防漂移、不可变 entitlement/event、容量复合 owner、业务 down guard、`integrity_check=ok` 和外键违规 `0`。
- Control 闭环覆盖平台签发、Agent pull/ACK/Usage、Workspace refresh、原子扩容、吊销和吊销后拉取；ETag/CAS、幂等键、Deployment owner、状态机和统一错误均失败关闭。License claim 不进入 RBAC 或数据访问判断。
- Shared/Gateway 使用独立用途的 Ed25519 签名；磁盘 head 是 canonical envelope，验签后原子替换 LKG。可信时钟 checkpoint 防止回拨延长授权；过期进入最多 7 天宽限，明确吊销没有宽限并清理 lease；现有 lease 不因到期被中途截断。
- 本地 Edge 决策热路径执行 `p95 < 2ms` 断言。在线刷新 `< 1s` 的真实网络 SLO、跨平台断网、长期运行和大 Fleet 容量验证留到批次 G5，不能用当前进程内证据替代。
- 应用内 Edge 使用本地管理员 GlobalSession 和实时 TOTP 进入 Platform。Workspace 桌面显示六个未签发 License 空态，Platform 显示签发/扩容表单和空清单；1280x900 与 390x844 均无页面级横向溢出或控件交叠，浏览器日志为空。密码、TOTP、Cookie 和 Session 未写入截图、日志、文档或提交，验证码提交后 DOM 中不存在非空敏感输入。

## 敏感审计与静止终态

- 提交前敏感审计覆盖 `43` 个精确暂存候选（`42` 个文本、`1` 个 Excel）、Excel 的 `20` 个 ZIP 条目，以及本地 `.env` 和当前加密 MFA 因子共 `4` 个实际敏感值的内存逐值比对；已知值、私钥标记和高置信 Token 模式命中均为 `0`。审计只输出计数，不输出密码、Token、Cookie、TOTP、私钥、签名包正文或其他材料。
- Platform/Workspace 的 Cookie、CSRF、request client、DTO 和 store 保持分域；CLI 不回显签名或 entitlement 正文，Agent/Console DTO 不包含私钥、credential、Secret、Token、自由路径、stdout/stderr 或 traceback。
- `.tmp`、pytest basetemp、生成数据库和浏览器截图均为可复现临时产物；正式结果写入本文和 Git 后删除整个 `.tmp`。未跟踪 PRD-101..105 文档保持不动且不进入提交。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要相邻回归、真实双域浏览器和本地热路径断言。G5 的 0111..0122 fresh/逐级/down guard、真实 Linux/代理/断网、N/N-1、Chromium/Edge/Firefox、多缩放、在线传播、长稳、全介质敏感扫描、性能、安全和完整静止终态保留到 PRD-130 独立提交后统一执行。
