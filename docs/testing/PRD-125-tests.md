# PRD-125 测试记录

状态：已完成。私有部署验证、交付接受、脱敏支持包、本地客户批准、加密云端托管、受控平台下载、TTL reaper、Workspace Console 和 0117 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-126 - Fleet资产、健康与受控远程诊断`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Validation Agent/SDK | 编译期八项计划、合法失败子集、同一 probe 关联、exact 证据 schema、固定原因/恢复码、异常零回显、15 分钟失败关闭、不可变 report/hash |
| Local Support Bundle | 固定 collector、确定性 ZIP、路径/条目/大小约束、字段/格式/熵/已知本地值扫描、5 分钟时限、AES-GCM、preview/CAS 批准、取消/失败/重启全终态清理 |
| Migration + Control | `0116 -> 0117 -> 0116 -> 0117`、权限防漂移、复合 owner、不变事实/down guard、验证 start/report/retry/replay、当前输入合同、DeliveryAcceptance step-up、upload/replay/conflict |
| Support Access | mTLS + EdgeSession + diagnostics capability、Workspace 裁剪摘要、SupportGrant 客户/平台双审批、scope、撤销/到期/authority drift、break-glass 拒绝、逐次下载审计、对象完整性、reaper |
| Console | strict DTO、Cookie/CSRF/CAS/step-up、失败项重试、TOTP/恢复码/WebAuthn 短生命周期、固定 collector preview/批准/取消、Workspace 代次、DOM/Storage 敏感材料零持久化 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Python PRD-125 聚焦集 | `33/33 passed`；Shared/Agent、本地存储与上传、Local API、0117、Validation API、Support API |
| 0117 + Support reaper 最终补跑 | `4/4 passed`；在 deadline/reaper 最终实现后重新验证迁移往返、权限漂移、完整上传和到期删除重试 |
| PRD-125 Console | `5/5 passed` |
| PRD-120..124 Console 必要回归 | `24/24 passed`；与 PRD-125 合并为 `29/29 passed` |
| Python static | PRD-125 源码/测试 Ruff 全规则和 `py_compile` 通过 |
| Console lint | `console lint ok (376 files)` |
| Diff hygiene | `git diff --check` 通过 |

完整 Console `npm test` 另命中一条与本 PRD 无关的既有基线漂移：`prd096PlatformWorkspaces.test.mjs` 禁止 `platformPageState.js` 出现 `location.`，而 HEAD 原文件已包含 `location.hash`；PRD-125 未修改该平台文件。专项与受影响回归均为绿，不以修改无关基线掩盖本条结果。

## 两轮实施自审

- 第一轮按 Agent 与 Control 的端到端合同复核。补齐此前缺失的 Gateway 验证执行器；runner 强制完整编译期 probe 表、Control 顺序子集、八类 exact 证据和固定 code，最小公开 Gateway 请求及 Usage/成本/失败核对共享同一 probe 摘要。非规范证据、伪通过、probe 异常和超时不能生成可接受的通过报告，异常正文不进入 report、repr 或日志。Control 再校验 validation/deployment/plan/attempt/input hash/check 顺序和时间边界。
- 第二轮按支持包授权、删除和静止终态复核。支持包生成达到 5 分钟即失败并清理；本地 preview 仅存固定归档，批准后仅上传密文。Control 不加载解密 Secret，Workspace DTO 去除 path/object hash/reference/approval proof/session 元数据；平台下载逐次恢复 SupportGrant 双审批、scope、operator/workspace revision、TTL 和撤销。后台 reaper 使用 `ready -> expired -> deleted`，S3/文件删除失败停在 `expired` 重试。无未关闭 P0/P1。

## API 与存储证据

- 0117 专项从 0116 基线执行正向、回滚、再升级，验证 11 个新增表/扩展、权限目录、尝试号按输入合同唯一、权限漂移失败关闭、业务事实阻止 down、`integrity_check=ok` 和外键违规 0。
- Validation API 覆盖 start 幂等重放、Agent 获取、失败 report 与重放、仅失败项 attempt 2、有效检查合并、缺失 step-up 拒绝、绑定 `edge.delivery.accept + edge_deployment + Deployment ID` 的接受、当前 generation 查询及未知字段/报告错配拒绝。
- Support API 使用实际临时密文文件验证上传 framing/canonical metadata/长度/SHA-256、mTLS 缺失、篡改、超限、重放与冲突。Workspace 只拿裁剪摘要；有效 SupportGrant 两次下载形成两条审计，撤销、到期、审批 revision 漂移、Workspace revision 漂移和 break-glass 均失败关闭。TTL 到期后 metadata 不再授权下载，密文由同步路径或 reaper 删除。

## 安全与静止终态

- 提交前敏感审计覆盖 `49` 个精确暂存文件的 `11,157` 条新增加行、Excel 的 `23` 个 ZIP 条目和 `5` 个本地 seed/`.env` 已知敏感值；已知值、私钥标记和真实 Token 模式命中均为 `0`。审计只输出计数，不输出密码、Token、Cookie、TOTP、私钥、支持包内容或其他材料。测试中的随机凭据与显式 canary 只用于证明扫描/零回显，不是可用凭据。
- `.tmp`、`.pytest-prd125-*`、生成数据库、支持包 ZIP/密文、staging、`__pycache__` 和 Excel 锁文件均不进入提交；未跟踪 PRD-101..105 文档保持不动。对象存储只保留有效期内密文，云端没有解密 Secret。
- 本条仅运行受影响单元/API 冒烟和必要前端回归。S5 的真实 Chromium/Edge/Firefox、Linux/S3、1 万节点/长稳、安全全扫、0111..0122 fresh/逐级/down guard 和完整静止终态按批次计划在 PRD-130 独立提交后统一执行。

## 批次门禁

PRD-125 独立提交后直接进入 PRD-126，不重复运行 S5 全门禁。G5 仍以 PRD-120..130 整批事实为输入；任何当前 Windows/ASGI/单元证据都不替代批次末真实部署、浏览器、性能或安全验收。
