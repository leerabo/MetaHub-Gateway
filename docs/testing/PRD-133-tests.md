# PRD-133 测试记录

状态：已完成。公共 API registry、版本/changelog、Webhook schema、Python/JavaScript SDK、下载验签、
双人批准、废弃与 sunset gate、Developer/Platform Console 已交付；本条保留独立设计、聚焦测试、敏感审计
和提交。下一权威项为 `PRD-134 - 全局账号隐私、同意与数据主体请求`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0125 Migration | `0124 -> 0125 -> 0124 -> 0125`、fresh/current head、重复执行、version/snapshot/SDK/signing key/deprecation/Webhook schema/download audit/event/outbox/action/scheduler、权限、状态与不变触发器、业务事实 down guard、完整性和外键 |
| Shared contract | 显式 public registry、禁止私有路由/字段、Webhook exact schema、breaking-change 分类、Python/JavaScript 可复现 ZIP、manifest/SBOM/provenance、N/N-1 smoke matrix 和签名 canonical payload |
| Control API | Developer Bearer 与 Platform Cookie/CSRF/Origin 分域、严格 DTO、HMAC cursor、兼容 diff、幂等构建、非自批批准、KMS 故障、SDK 撤回、90 天 notice、通知/adoption gate、scheduler fencing、动态撤权、事件/outbox 和固定错误 |
| SDK download | exact filename/media/no-store/nosniff/content length、Digest、immutable receipt、artifact SHA-256、signature SHA-256、Ed25519 public-key verify；错误签名和篡改制品失败关闭 |
| Console | 现有 API Docs 与 Releases 工作台、strict DTO、Abort/epoch/权限漂移、构建/批准/废弃/撤回、签名只驻留内存、不进 DOM/Storage、响应式布局和双凭据域隔离 |
| 必要回归 | Global/Workspace/Platform 权限、API Docs 导航、Platform Releases 导航、PRD-096 Workspace 目录和动态平台权限扩展 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-133 Shared | `5/5 passed`；registry、Webhook、SDK reproducibility、breaking diff 与签名载荷 |
| PRD-133 Control API | `1/1 passed`；公开查询、发布、非自批批准、验签、废弃、sunset、撤回、动态撤权与失败恢复 |
| PRD-133 Migration | `3/3 passed`；0125 fresh/往返/重复/不变约束/down guard |
| PRD-133 Console | `6/6 passed`；双域 client/DTO/render/controller、下载 SHA-256/receipt/Ed25519 验签和错误签名负例 |
| Console 必要回归 | API Docs/Platform Access/Workspace/权限与 shell 联合 `42/42 passed` |
| PRD-096 相邻 API | `5/5 passed`；历史夹具改为确认 0088 已应用，不再错误要求全库 head 永久停留 0088 |
| Python static | PRD-133 受影响后端源码与测试 Ruff 全规则通过；规划脚本除既有 E501 行长债务外通过 |
| Python format | 14 个 PRD-133 新建或聚焦 Python 文件 Ruff formatter 检查通过 |
| Console lint | `console lint ok (401 files)` |
| Roadmap | 生成器自校验通过；validator `200` 项与聚焦 `5/5` 通过，下一项为 `PRD-134` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 public/private registry、制品和凭据域。发布源仅来自显式 operation/Webhook 注册表，不反射
  private/platform/machine router；SDK archive 的源码、示例、manifest、SBOM 和 provenance 均由 canonical
  public contract 生成且零默认凭据；Developer 路由只接收 Bearer，Platform 路由只接收 Cookie/CSRF/Origin；
  Console 不把签名、公钥、Token 或内部路由写入 DOM/Storage。补充公开 `signature_b64` 后，下载会同时校验
  receipt、artifact digest、signature digest 和 Ed25519 canonical payload。修正后无未关闭 P0/P1。
- 第二轮复核兼容、审批、废弃和重启恢复。构建与批准使用 revision、request hash、Idempotency-Key 和不同
  当前授权 operator；合同、两个 SDK、Webhook schema 与事件在同一事务发布；notice 至少 90 天，scheduler
  只创建持久待决 gate，不自动 sunset；最终切换重新验证 deadline、通知/adoption 水位、批准权限和 revision；
  撤回保留历史，scheduler lease/generation 跨重启 fencing。修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 本条历史专项的 pytest 副本可重建；2026-09-07 起 `.tmp` 还含冻结源码、持久磁盘和可复用基础设施，
  不再允许整体删除。清理须逐项核对进程引用、恢复用途和正式证据索引。
- 暂存集审计只输出规则命中计数，不输出密码、Token、Cookie、TOTP、私钥、Provider Key、Prompt/Response、
  客户正文、对象 URL、本地路径或签名私钥值；未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终暂存集为 38 个文件、7,064 个新增行和 15 个删除行。生产代码凭据字面量赋值、Bearer 值、私钥块、
  JWT/Provider Key 形态、本地管理员密码复用、凭据文件/`.env`、受保护 PRD-101..105 文档命中均为 `0`。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要导航权限回归和静态检查。`0123 -> 0130` 全数据库矩阵、
集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、
四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 只能形成
`release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生数据库补充验证

独立设计及两轮复核见 PRD-133 第 7 节。schema guard 复用共享 helper；合同和批准时调用的
供应链公钥登记使用按 key_id 的冲突忽略，随后仍验证公钥摘要和 active 状态，不允许覆盖信任。

证据目录：`.artifacts/g6-operations-20260907/`。

| 验证 | 实际结果与证据 |
| --- | --- |
| PostgreSQL 17.11 / Windows | `g6-operations-d06fa086921a405f84356786603670ec-junit.xml` 中本条 3/3 通过；该次其他 PRD 有 3 项失败，不能宣称整次全绿 |
| 发布全流程 | 构建、双人批准、SDK 发布/下载验签、废弃调度、撤回；原生迁移 head 为 0140 |
| 公钥信任 | 合同/供应链两个用途重复登记成功，同 ID 不同公钥均拒绝且原摘要保留 |
| SQLite 业务 | `sqlite-r4-junit.xml` 中本条 1/1 通过；同次三条 PRD 共 7/7，444.93 秒 |
| Shared SDK | `sdk-contract-unit-junit.xml`，5/5 通过，覆盖显式注册、可复现 SDK 与破坏性变更分类 |
| Console | `console-unit-junit.xml` 中本条 6/6 通过，不作为真实浏览器证据 |
| 静态检查 | 本条变更源码/测试 Ruff 与 diff 检查通过 |

本条成功后只复验继续变更的 PRD-131/132 及共享初始化相邻入口，没有为每次修正重复全批次
SDK 流程。0141 仅修改上传文件触发器，不把后续原生运行错误标注成本条重新执行的结果。

独立敏感审计记录：`prd-133-sensitive-audit.json`，含合同/供应链公钥改动和本条测试证据的文件
摘要、凭据高置信规则、禁止文件与暂存范围。测试生成的签名及受控发布者不是实际发布批准；
不新增 Linux SDK consumer、真实浏览器、性能或正式发布证据。工作簿本次未修改。
