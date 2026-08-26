# PRD-120 测试记录

状态：已完成。Edge Agent 注册、设备身份、证书轮换、短时 mTLS Session、吊销、Workspace 私有 Gateway
和 Platform Fleet 已交付；本条保持独立设计、测试记录、敏感审计和提交。下一权威项为
`PRD-121 - 云边期望状态、签名快照与离线LKG`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0111 -> 0112 -> 0111 -> 0112`、fresh head、重复执行原子失败、不可变权限、业务事实 down guard、tenant deletion manifest |
| Shared | Ed25519 空 subject/零扩展 CSR、注册 proof 全坐标绑定、token/challenge/nonce/Deployment 严格格式 |
| Workspace API | Cookie/CSRF/Origin、owner 动态权限、创建幂等、一次性 token/challenge、列表、CAS 吊销、同形错误 |
| Agent API | token 单次消费、CSR/公钥唯一、真实证书签发、mTLS transport 证明、轮换 drain、nonce 重放、短 Session、吊销失败关闭 |
| Platform API | 独立 Platform Cookie、Edge Fleet 权限、严格安全 DTO、无 Workspace/tenant/证书 ID/事件正文 |
| Console | Workspace 创建/列表/一次性值清理；Platform hash 恢复、Fleet-only 首屏、无关域故障隔离、离页/撤权清理、移动布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-120 Migration | `3/3 passed` |
| PRD-120 API/security | `3/3 passed` |
| Workspace Console 导航权限 | `3/3 passed` |
| Shared CSR/proof | `2/2 passed` |
| PRD-120 Console 专项 | `8/8 passed` |
| Console lint | `370 files` 通过 |
| Platform Console 相邻回归 | `9/9 passed` |
| Backend auth / Platform / Workspace 相邻回归 | `6/6 passed` |
| Roadmap validator tests | `4/4 passed` |
| Roadmap validator CLI | `134/134 checks passed`；下一项 `PRD-121` |
| Python static | PRD-120 源码/专项测试 Ruff 全规则通过；规划候选忽略存量 E501 后通过；compileall 通过 |
| Diff hygiene | `git diff --check` 通过 |

## 真实 mTLS、API 与 Browser

- 使用 PRD-120 实际 CA 派生和客户端证书签发代码完成 40 次 TLS 1.3 环回握手；服务端强制验证客户端
  证书，`40/40` 成功，p95 `31.67ms`，满足 `<500ms`。临时私钥只存在系统临时目录并随进程删除。
- 注册 API 将有效窗口限制为最多 600 秒；EdgeSession 最长 45 秒。专项 API 测试证明吊销同事务终止活跃
  Session，下一次握手立即拒绝，结构上满足 `<60s` 吊销边界。
- 真实 Control API 和开发数据库已应用 0112。浏览器直接读取本地 seed 管理员凭据并只在内存输入，完成
  Workspace 与 Platform 两域验收；凭据、TOTP、注册令牌和 challenge 未回显、未写 URL/Storage/日志。
- Workspace 页面创建四条待注册 Deployment；复制、关闭、刷新和离页后一次性材料均从 DOM/状态清除，
  剪贴板清空，刷新后只恢复非敏感 Deployment 摘要，未执行吊销。
- Platform `#edge-fleet` 直达显示完整授权导航和四条 Fleet 摘要；离页后 Deployment 引用从 DOM 清除，
  返回时重新读取。页面不含 `workspace_id`、`tenant_id`、`certificate_id` 或事件正文。
- 实际 536px 视口 `documentElement.scrollWidth == clientWidth == 536`，无页面级横向溢出；导航使用独立
  横向滚动，Fleet 表在窄屏按字段堆叠，无重叠或裁切。

## 安全与静止终态

- 注册 token/challenge 只在首次响应出现，数据库只保存分域 HMAC；CSR、证书和事件只保存公开材料、摘要
  与固定枚举。Agent 路由拒绝 Cookie、Bearer 和调用方自报证书头。
- Platform Fleet DTO 使用 exact allowlist；Workspace 与 Platform API client 不共享 Cookie/CSRF/DTO。
- 52 个暂存候选文件（含解压后的 Excel XML）已完成敏感审计：seed/`.env` 已知值、PEM private key、
  长 Bearer、注册 token/challenge 字面值与禁止文件均为 `0` 命中。README 中一处历史本地 Virtual Key
  示例已改为非真实占位符，不再复制 seed 明文。
- 开发 SQLite 的 22,821 个文本单元已只读扫描：已知明文和 PEM private key 均为 `0` 命中；Edge 表中
  原始 registration token/challenge、session token 或 private key 列为 `0`。历史 `.pytest-*`、
  `.tmp-*`、用户文件和 PRD-101..105 文档不纳入本提交。

## 批次门禁

PRD-120 独立提交后直接进入 PRD-121。G5 的 0111..0122 全迁移、真实代理/断网、三浏览器三缩放、
性能、安全和静止终态统一门禁保留到 PRD-130 独立提交后执行，不用本条 Chromium 结果替代。
