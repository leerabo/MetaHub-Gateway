# PRD-134 测试记录

状态：已完成。全局账号同意记录、Workspace 隐私偏好、数据主体请求、七域 owner 编排、加密一次性导出、
Legal Hold/retention 决定、Identity 隐私中心与 Platform 最小队列已交付；本条保留独立设计、聚焦测试、
敏感审计和提交。下一权威项为 `PRD-135 - 合规控制与证据中心`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0126 Migration | `0125 -> 0126 -> 0125 -> 0126`、fresh/current head、重复执行、ConsentRecord 兼容扩展、permission/adapter manifest、preference/DSR/job/export/decision/exception/event/outbox/action/scheduler、复合 owner、状态与不变触发器、tenant deletion manifest、业务事实 down guard、完整性和外键 |
| Identity API | Global+Workspace 双 Cookie、Global CSRF/Origin、本人 owner、strict body/query/header、偏好 CAS/幂等、step-up 绑定、DSR 状态、取消、七 owner 恢复和固定错误 |
| Export | owner 字段白名单、跨 Workspace 排除、AES-GCM owner AAD、大小/TTL、Digest、确定性 HMAC receipt、CAS/幂等重放、header-only 一次下载、消费/过期和正文保全事实排除 |
| Platform API | 独立 PlatformSession、动态 `platform.privacy.read`、pseudonymous request code、deadline、七域进度和固定 outcome；拒绝账号、Workspace、email、导出正文和内部字段 |
| Console | Identity 偏好/同意/DSR/进度/决定/例外/下载、Platform 最小队列、strict DTO、Global CSRF/CAS/幂等、step-up、owner/epoch/Abort、回执摘要、对象 URL 立即撤销和敏感状态清理 |
| 必要回归 | Identity 基线、PRD-094 Workspace 生命周期、PRD-095 Platform Access、PRD-096 Platform Workspaces、PRD-116 Identity Security、PRD-118 Machine Identity 与 main refresh ownership |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-134 Control API | `3/3 passed`；偏好、DSR、七 owner、Legal Hold/retention、加密导出、幂等回执、最小 Platform 队列与 scheduler fencing |
| PRD-134 Migration | `3/3 passed`；0126 fresh/往返/重复/不变约束/down guard/manifest/完整性 |
| PRD-134 Console | `7/7 passed`；严格 client/DTO/render/controller、Global CSRF/CAS/幂等、step-up、header-only receipt、断网重放、摘要校验和 Storage/URL 负审计 |
| Console 必要回归 | Identity/Platform/Workspace/Security/Machine Identity/refresh ownership 联合 `69/69 passed` |
| Python static/format | PRD-134 后端、权限扩展与聚焦测试 Ruff 全规则和 formatter check 通过 |
| Console lint | `console lint ok (404 files)` |
| Roadmap | 生成器自校验通过；validator `205` 项与聚焦 `5/5` 通过，Excel PRD-134=`已完成`、PRD-135=`未启动` |
| Diff hygiene | `git diff --check` 通过（仅既有 `identity.css` CRLF 归一化提示） |

## 两轮实施自审

- 第一轮复核主体、凭据域、Platform 最小可见性和导出白名单。Identity 每次同时恢复匹配的 GlobalSession、
  WorkspaceSession、Membership 和动态 permission，step-up 不能选择 account/workspace；Platform 只接受独立
  PlatformSession。复核发现 data-governance export fragment 暴露 Workspace 级 hold count/policy generation，
  已改为固定 `subject_data_collected=false`，adapter manifest 与解密负断言同步更新。修正后无未关闭 P0/P1。
- 第二轮复核幂等、恢复、取消和一次性下载。owner job 使用稳定请求身份、checkpoint、lease token 和 8 次有界
  重试；deadline/export scan 现在真实领取持久 scheduler lease、提升 fencing generation、校验 token 并在成功后
  释放，过期租约可接管。receipt 由 export revision 与 Idempotency-Key 确定性 HMAC 派生，相同命令跨重启可
  重放同一值，数据库只保存 HMAC；下载原子消费，迟到响应不能恢复已清除值。修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 正式结论保存在本文和 Git，原始测试结果按运行独立保留。`.tmp` 还包含冻结源码、基础设施和持久
  VM 磁盘，不能整体删除；只可单独核验已停止、无依赖的可重建测试目录后处理。
- 审计只输出规则命中计数，不输出密码、Token、Cookie、TOTP、step-up grant、receipt、私钥、Provider Key、
  Prompt/Response、客户正文、对象 URL 或本地管理员凭据；本地测试凭据只在进程内使用，不打印或持久化。
- receipt 只从请求头进入下载边界，浏览器不写 URL/DOM/Storage；数据库保存域分离 HMAC，导出密文使用
  owner-qualified AES-GCM AAD。未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终暂存集为 35 个文件、6,931 个新增行和 22 个删除行。生产凭据字面量、Bearer 值、私钥块、JWT/
  Provider Key 形态、URL 携带 secret、本地管理员密码、本机用户路径、凭据文件/`.env` 和受保护
  PRD-101..105 文档命中均为 `0`。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要 Identity/Platform/Workspace 回归和静态检查。`0123 -> 0130`
全数据库矩阵、集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、
SQLite/PostgreSQL、N/N-1、四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。
G6 只能形成 `release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生修复复验

同 PRD-135..137 合批，复用数据库迁移基线；本条独立修复毫秒精度的单调时间比较，保持身份验证、
收集、导出/删除和严格数据库状态约束。fixture 不再固定旧迁移 head，外键检查后端感知。

- PostgreSQL：`g6-security-c693a109613341cbb02892f3d7ae60da-junit.xml` 中本条 3/3 通过。
  首轮 `g6-security-09c8ecef524c4521b168228d57388585-junit.xml` 的两项隐私转换失败原样保留，
  全轮是 9 通过、6 失败，不能表述为整轮通过。
- SQLite：修复后 `sqlite-r2-junit.xml` 中本条 3/3 通过，整次 61/61，458.998 秒。
- 时间边界：`timestamp-watermark-r1-junit.xml` 中本条 6/6；最终 `receipt-config-unit-r2-junit.xml`
  再覆盖同样 6 项。Console `console-unit-junit.xml` 中本条 7/7。

以上证据位于 `.artifacts/g6-security-20260907/`，本条敏感审计记录为
`prd-134-sensitive-audit.json`，范围包括独立暂存文件、源文件摘要和高置信度凭据规则。
两轮复核记录见本条 PRD 第 8 节。受控会话只验证业务合同，不是实际登录或法定授权证据；
原生场景通过不等于 G6 全部真实流程、浏览器、性能或长稳已完成。

## 2026-09-07 Owner Job 租约补充

PRD 第 9 节先完成设计与两轮复核，再补充不可变领取对象、完整 owner/worker/revision/expiry
校验、长任务提交前复核和第八次崩溃的有界收尾。原生事务锁复用本模块 singleton 行，数据库
时钟在同一事务内实际前进。独立线程并发领取与最后一项制品组装均纳入测试。

- `prd134-owner-leases-r1-junit.xml`：18/18，通过 SQLite 的九项租约/并发合同、三项既有 API
  与六项时间边界。包括 12 个领取字段替换、过期接管、旧完成/失败回放、adapter/制品组装/
  失败/取消期间过期后的整笔回滚、第八次崩溃清理及其它请求继续执行。
- `g6-privacy-owner-f5a93f44d2ea42dd91e2374c8ed33a47-junit.xml`：独立 PostgreSQL 实例
  12/12，241.91 秒。九项租约合同与三项既有隐私业务合同；测试完成后该实例已正常停止。

以上仍使用受控鉴权夹具，不称为真实登录。真实注册、TOTP、recovery code、step-up、七域导出
和跨主体 HTTP 验证归属 PRD-137 的独立记录与提交。本条不扩大为 G6 完整门禁。
