# PRD-121 测试记录

状态：已完成。云边期望状态、非自批审批、RFC 8785 + Ed25519 签名快照、Agent 原子应用、单调 ACK、
持久 LKG、Workspace 收敛视图和 Platform Fleet 安全投影已交付；本条保持独立设计、测试记录、敏感审计
和提交。下一权威项为 `PRD-122 - 私有网关遥测、用量与失败事实同步`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0112 -> 0113 -> 0112 -> 0113`、fresh head、重复执行原子失败、复合 owner/不可变事实、业务事实 down guard、tenant deletion manifest |
| Shared | RFC 8785 官方向量、duplicate key/non-finite/超限数字、严格 artifact/envelope/trust schema、Ed25519 domain/owner/audience/TTL、签名篡改与未知 root |
| Control API | Workspace Cookie/CSRF/Origin、动态权限、Desired State CAS/幂等、服务端风险派生、不同管理员审批、Agent mTLS + EdgeSession 拉取/ACK、迟到 ACK、递增回滚、同形错误 |
| Gateway | 候选 slot/高水位/active pointer/父目录 fsync 顺序、各崩溃点恢复、同 generation 异 hash、乱序拒绝、有界 LKG、完整性故障隔离、本地读取 |
| Platform API | 独立 Platform Cookie、Fleet 权限和 exact DTO；只含 generation/lag/stale/failure code，不含 Workspace/tenant/artifact/manifest/signature/正文 |
| Console | Workspace 期望/实际/LKG、发布/审批/回滚、错误恢复；Platform Fleet 脱敏投影、hash 直达、离页清理和移动布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-121 Migration | `4/4 passed` |
| Shared + Gateway | `15/15 passed` |
| PRD-121 Control API | `1/1 passed` |
| PRD-120 后端必要回归 | `3/3 passed` |
| PRD-120/121 Console 专项 | `12/12 passed` |
| Console lint | `371 files` 通过 |
| Python static | PRD-121 源码/测试 Ruff 全规则与 compileall 通过；规划脚本忽略存量 E501 后通过 |
| Roadmap | generator 可重复重建；validator tests `4/4`、CLI `139/139` 通过，下一项 `PRD-122` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮聚焦签名、并发与凭据分域，发现并关闭三项问题：同一 Deployment 的并发 apply 可能按落盘完成
  顺序回拨 active pointer，现由独立 apply lock 串行化；trust 更新曾允许历史受信 key 直接签新 manifest，
  现强制上一代 current 签名、新 current 必须是已预告且公钥不变的 next、撤销集合单调，在线和冷启动
  恢复共用同一规则；Workspace/Platform 请求现拒绝混入 `X-Edge-Session`。补充受控线程竞态、错误 signer
  和双域混合凭据回归后通过。
- 第二轮逐项复核 Control 事务/CAS/非自批审批、ACK/LKG 单调水位、0113 复合 owner/down guard、前端
  owner/刷新代际/Abort fence、Platform exact DTO、部署 secret 装配与离页清理。未发现新的 P0/P1，所有
  第一轮修正均有自动化证据且真实浏览器既有收敛结果不受影响。

## 真实审批、Agent ACK 与 Browser

- 真实 Control API 和开发数据库已应用 0113。浏览器直接读取本地 seed 管理员凭据并只在内存输入；凭据、
  TOTP、注册 token/challenge、设备会话和签名材料均未回显，未写入 URL/Storage/日志。
- Workspace 创建并注册 `PRD-121 Browser Acceptance 20260826` Deployment。高风险 Desired State 进入
  `approval_pending`，当前 Owner 自批被后端拒绝；同 Workspace 的第二名 active 管理员使用独立真实登录
  和 Workspace 切换完成审批，证明申请人与审批人分离。
- Agent 使用真实设备证书指纹建立短 EdgeSession，拉取 signed snapshot/trust manifest 并 ACK。最终
  Workspace 显示 `期望 1 / 实际 1 / LKG 1`、`已收敛`、`已应用`，固定 failure code 为 `applied`。
- Platform `#edge-fleet` 显示 `1 / 1 / 1`、`converged`、`applied`；页面不含 raw manifest、signature、
  artifact、Workspace/tenant 标识或事件正文。
- Workspace 与 Platform 在 `1280x900`、`536x900` 均无页面级横向溢出、控件裁切或 Console
  warning/error；536px Platform 顶部导航使用预期的独立横向滚动。

## 安全与静止终态

- 签名私钥仅来自独立运行时 secret，数据库与 API 只保存/返回公开验签材料和摘要；设备私钥、本地
  Provider Secret、Prompt/Response 和业务正文均不进入云端 Desired State、artifact 或 Fleet DTO。
- Agent 路由拒绝 Cookie、Bearer 和调用方自报证书头；Workspace、Agent、Platform 使用独立 Session、
  API client 和 DTO。只有分类后的 transport unreachable 可使用有界 LKG，授权/签名/owner/hash/schema/
  TTL/乱序故障全部失败关闭。
- 提交前独立敏感扫描覆盖 `47` 个暂存文件的 `7,340` 条新增行、Excel 的 `20` 个 XML/关系条目、
  `7` 个开发数据库和 `48` 个日志文件，并加载 `5` 个本地 seed/`.env` 敏感值做精确比对。已知值、
  PEM private key、长 Bearer、JWT、`erg_`/`ech_`/`egs_` 注册与会话实值均为 `0` 命中；禁止暂存
  文件计数同为 `0`。扫描只输出计数，不输出凭据、签名材料或业务正文。
- 历史 `.pytest-*`、`.tmp-*`、用户文件和未跟踪的 PRD-101..105 文档不纳入本提交；只清理本条明确创建的
  PRD-121 临时测试目录。

## 批次门禁

PRD-121 独立提交后直接进入 PRD-122。G5 的 0111..0122 全迁移、真实代理/长断网、三浏览器三缩放、
性能、安全和静止终态统一门禁保留到 PRD-130 独立提交后执行，不用本条 Chromium 结果替代。
