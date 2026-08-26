# PRD-122 测试记录

状态：已完成。严格遥测 DTO、本地 append-only spool、gzip 断点续传、Control 连续 ACK、Usage 幂等对账、
失败事实、Workspace 水位/策略与 Platform Fleet 安全投影已交付；本条保留独立设计、测试记录、敏感审计
和提交。下一权威项为 `PRD-123 - 私有网关本地Secret托管与轮换`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0113 -> 0114 -> 0113 -> 0114`、fresh head、重复执行原子失败、复合 owner/不可变事实、权限扩展和业务事实 down guard |
| Shared + Gateway | exact 四流联合、禁止字段、policy 粒度、RFC 8785、append-only、并发 sequence、epoch fence、gzip/no-store、禁止重定向 |
| Control API | mTLS + EdgeSession、能力、policy CAS/幂等、重复/乱序/缺口/同序列异 hash、Usage/失败归集、角色裁剪、凭据负矩阵和压缩炸弹 |
| Platform API | Fleet 独立 Session 与 exact DTO；仅含 receipt/lag/backlog、CPU/Memory 桶、错误率和固定失败码；无投影节点安全默认值 |
| Console | Workspace 三档策略、四流 ACK、Usage/成本/失败；Platform Fleet 脱敏投影、动态权限、迟到响应、551px 奇数网格和离页清理 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-122 Migration | `5/5 passed` |
| Shared + 本地 spool/uploader | `5/5 passed` |
| PRD-122 Control API + 导航 | `2/2 passed` |
| PRD-120..122 Console 专项 | `16/16 passed` |
| Console lint | `372 files` 通过 |
| Python static | PRD-122 源码/测试 Ruff 全规则（规划脚本忽略存量 E501）与 compileall 通过 |
| Roadmap | generator 可重复重建；validator tests `4/4`、CLI `144/144` 通过，下一项 `PRD-123` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按零敏感合同复核 DTO、解析、日志、Agent/Workspace/Platform 三凭据域和 Fleet 投影。修正 Console
  导航对 PRD-122 权限扩展的拒绝，并增加独立导航回归；确认 raw batch、model/provider、token/cost 和
  tenant/Workspace owner 不进入 Platform DTO，禁止字段、duplicate key、超限/深层 JSON 和压缩炸弹均
  失败关闭。
- 第二轮按水位、计费和真实混合 Fleet 复核事务、去重、cursor、receipt、policy 降级、迟到历史重复和
  无投影节点。修正 0114 已存在但 Deployment 尚无投影行时的 NULL 安全默认值；随后补齐 mixed Fleet
  API 回归与 551px 遥测三项网格横跨整行断言。无未关闭 P0/P1。

## 真实链路与 Browser

- 使用本地 seed 管理员凭据完成真实登录，凭据和 MFA 只在内存中输入。创建并注册
  `PRD-122 Telemetry Acceptance`，通过真实设备身份、EdgeSession、本地 spool/uploader 和 Control API
  提交四个流；所有临时注册/session 值均未回显、未写 URL/Storage 或验收文档。
- Workspace 最终显示 Billing policy r2、四流 `ACK 1 / next 2 / seen 1`、请求 `37`、Input `12800`、
  Output `4200`、Cache `900`、CNY `0.245000`、USD `0.033800` 和 `provider_timeout=4`。
- Platform Fleet 验证有投影节点的 CPU/Memory 桶、`800 bp`、`provider_timeout`，以及无投影节点的
  `unknown / 0 / none`；DOM 不含 model/provider、token、成本、注册/session 或 tenant/Workspace 字段。
- Workspace `1280x720` 与 `551x512`、Platform `551x512` 均无页面级横向溢出或可见元素越界；551px
  遥测三项摘要的第三项横跨整行，无空白半格。可见 DOM 敏感标记为 0。

## 安全与静止终态

- 云端只接收 exact 聚合数值、引用 ID、固定组件/失败码、时间桶和水位；Provider endpoint、Key/Token、
  Prompt/Response、自由文本、路径和堆栈均拒绝。Agent 请求不接受 Cookie/Bearer/调用方证书头，Workspace
  与 Platform Cookie 互斥，Uploader 不跟随携带 EdgeSession 的重定向。
- 提交前独立敏感扫描覆盖 `38` 个暂存文件的 `5,911` 条新增行、Excel 的 `20` 个压缩条目、`7` 个开发
  数据库和 `45` 个日志文件，并加载 `4` 个本地 seed/`.env` 敏感值做精确比对。已知值、PEM 私钥、长
  Bearer/JWT、注册/挑战/EdgeSession 实值和禁止暂存文件均为 `0`；扫描只输出计数，不输出敏感材料。
- 历史 `.pytest-*`、`.tmp-*`、用户文件和未跟踪的 PRD-101..105 文档不纳入提交；只清理 PRD-122 明确
  创建的临时文件与测试目录。

## 批次门禁

PRD-122 独立提交后直接进入 PRD-123。G5 的 0111..0122 全迁移、7 天真实断网追平、三浏览器三缩放、
热路径性能、安全和静止终态统一门禁保留到 PRD-130 独立提交后执行，不用本条聚焦与 Chromium 结果替代。
