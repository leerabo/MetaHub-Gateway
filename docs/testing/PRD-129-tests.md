# PRD-129 测试记录

状态：已完成。私有 Gateway 备份、恢复、隔离灾备演练、云端安全摘要、Workspace/Platform Console 和 0121 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-130 - 隔离环境签名包异步管理与冲突合并`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0121 Migration | `0120 -> 0121 -> 0120 -> 0121`、权限与角色矩阵、复合 owner、不可变事件/outbox、租户删除 manifest、业务事实 down guard、完整性与外键 |
| Shared/Gateway | Ed25519 用途分域 manifest、逐块 AEAD/AAD/Merkle、客户恢复密钥包裹、N/N-1、原子 active slot、持久 restore journal、替换节点 KEK、客户对象存储、九项 drill 核验和持久云报告 outbox |
| Control API | Agent backup/drill report、Workspace 安全摘要、step-up 非自批演练、严格 DTO、ETag、幂等、generation、同形 404、Platform 字段裁剪和统一错误合同 |
| Console | Workspace/Platform/本地管理员凭据隔离、严格 DTO/store、权限扩展白名单、RPO/最近成功/验证/generation/保留/存储/drill 状态、空态和响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Shared SDK 与 Gateway | `22/22 passed`；签名/加密合同、本地备份与恢复、云报告、畸形请求、跨 owner、对象存储和 drill |
| Control API、0121 与导航 | `5/5 passed`；完整报告/双人演练闭环、往返/down guard 和双域权限导航 |
| Runtime config 回归 | `43/43 passed` |
| PRD-129 Console | `4/4 passed` |
| Python static | PRD-129 变更源码与测试 Ruff 全规则通过；规划脚本按既有规则排除历史 `E501` 后通过 |
| Console lint | `console lint ok (386 files)` |
| Browser 1280x900 | Workspace 六个 Deployment 均展示安全备份事实；Platform 空投影可见；两页无内容溢出、控件交叠或浏览器错误 |
| Browser 390x844 | 两域 `body`/根滚动宽度均为 `375`；页面横向溢出、控件交叠和相关内容溢出均为 `0`；Platform 导航为有界局部横向滚动区 |
| Roadmap | 生成器自校验通过；validator tests `4/4`、CLI `179/179` 通过；Excel 的 `20` 个 ZIP 条目完整且下一项为 `PRD-130` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按 owner、密钥与数据面边界复核。云端只保存固定状态、时间、水位、size bucket、check outcome 和摘要；chunk、envelope、对象 URL、客户路径和 Secret 正文只在客户环境。Workspace、Platform 和本地管理员使用独立凭据/client/store；Platform 无法下载备份。修正了 `localSecretManagement.js` 对 exact-key 错误映射不一致的问题，所有本地备份/恢复错误统一经固定目录失败关闭。
- 第二轮按恢复原子性、协议畸形和权限漂移复核。恢复在完整验签/解密和 staging 核验后才调用原子 apply，失败保留 active；restore journal 与报告 outbox 可重试且正文稳定。修正畸形本地 drill 请求为 `422`、畸形下游响应为 `502`，补充双域导航权限回归和错误目录回归；修正后无未关闭 P0/P1。

## API、恢复与浏览器证据

- 0121 从完整 0120 基线建立复合 owner、严格状态机、不变事件/outbox、幂等动作和租户删除 manifest；验证业务事实阻断 down、`integrity_check=ok` 和外键违规 `0`。
- Shared/Gateway 对精确 backup domain 逐块加密，manifest 绑定用途、owner、generation、schema、chunk Merkle root、信封摘要和有效期。错误 owner/key/time/schema、缺失或损坏 chunk、manifest/envelope 篡改均在返回明文或调用 restore applier 前失败。
- 本地恢复用 staging 与持久 journal，替换节点使用新 KEK 重包裹可迁移 Secret，不导出旧 OS 根密钥或设备身份。隔离 drill 强制独立 target/identity 绑定，并上报 manifest、owner、schema、配置、Usage cursor、审计链、Secret reference、设备重注册和最小请求九项摘要证据。
- Control 闭环覆盖 mTLS Agent 报告、Workspace 备份状态、restore drill 请求/非自批批准、完整核验报告和 Platform 裁剪投影；ETag、幂等、owner、generation、权限和 step-up 均失败关闭。
- 浏览器使用隔离的开发数据库在线备份副本和随机内存测试凭据，不修改真实开发管理员密码/TOTP。Workspace 与 Platform 分别在 `390x844`、`1280x900` 验收：页面横向溢出、相关内容溢出、控件交叠、非空敏感输入、禁止字段和浏览器日志均为 `0`。禁止字段扫描覆盖 `object_url`、`storage_path`、`envelope`、`secret_value`、`manifest_sha256`、`kms_key_id_sha256`、`isolated_target_sha256`；验收后通过 UI 登出并恢复正常服务。

## 敏感审计与静止终态

- 提交前敏感审计覆盖 `52` 个精确暂存候选（`51` 个文本、`1` 个 Excel）、Excel 的 `20` 个 ZIP 条目，以及本地 `.env` 和开发库活动加密 MFA 因子共 `4` 个实际敏感值的内存逐值比对；已知值、私钥标记和高置信 Token 模式命中均为 `0`。审计只输出计数，不输出密码、Token、Cookie、TOTP、私钥、恢复密钥、签名 manifest、chunk 或 envelope。
- Platform/Workspace/本地管理员的 Cookie、CSRF、request client、DTO 和 store 保持分域；API/日志/DOM 不包含对象 URL、客户路径、chunk、envelope、Secret 正文、设备私钥、恢复密钥、自由文本、stdout/stderr 或 traceback。
- `.tmp`、pytest basetemp、隔离数据库、日志和浏览器截图均为可复现临时产物；正式结果写入本文和 Git 后删除整个 `.tmp`。未跟踪 PRD-101..105 文档保持不动且不进入提交。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要配置回归、真实双域浏览器和静态检查。G5 的 0111..0122 fresh/逐级/down guard、真实 Linux/代理/断网/对象存储、N/N-1、Chromium/Edge/Firefox、多缩放、RPO/RTO、长稳、全介质敏感扫描、性能、安全和完整静止终态保留到 PRD-130 独立提交后统一执行。
