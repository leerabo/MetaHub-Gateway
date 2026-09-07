# PRD-136 测试记录

状态：已完成。SBOM/provenance/attestation、漏洞扫描代际与默认阻断、Workspace 时限例外、PatchRelease
回滚门禁，以及 Developer/Platform Releases 双域 Console 已交付；本条保留独立设计、聚焦测试、敏感审计
和提交。下一权威项为 `PRD-137 - 租户隔离持续验证与安全攻防`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0128 Migration | `0127 -> 0128 -> 0127 -> 0128`、fresh/current head、重复执行、SBOM/signature/provenance/finding/exception/patch/event/outbox/action、Platform 权限扩展与回退、全局制品事实、Workspace/tenant 复合 owner、不变约束、扫描代际、业务事实 down guard、完整性和外键 |
| Release Security API | Developer Bearer 与 Platform Cookie/CSRF/Origin 严格分域、exact query/header/body、发布批准原子 attestation/outbox、公开报告/provenance 裁剪、用途隔离签名与密钥撤销、feed/scanner generation、critical/high 默认阻断、例外非自批/TTL/digest scope/动态撤权 |
| PatchRelease | `proposed -> approved -> released -> rolled_back`、云/Edge N/N-1 compatibility matrix、LKG rollback digest、发布前签名链复验、`patch.released` outbox、失败关闭与状态 CAS |
| Console | Developer 公开 gate/SBOM/provenance/离线验签/JSON 下载；Platform finding/SLA/exception/patch；strict DTO、客户端 Ed25519 复验、Abort、owner/epoch、动态撤权和 credential domain 隔离 |
| 必要回归 | PRD-133 Developer Contracts 与 Platform Releases 既有合同、SDK 和发布控制器链路 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-136 Migration/API | `5/5 passed`；0128 fresh/往返/down guard、发布证明链、扫描代际、默认阻断、例外双控、PatchRelease/rollback、事件和密钥撤销 |
| PRD-136 Console 与相邻回归 | PRD-133/136 联合 `11/11 passed`，其中 PRD-136 Console `5/5 passed` |
| JavaScript/static | Console lint `console lint ok (409 files)` |
| Python static/format | PRD-136 后端、受影响服务与聚焦测试 Ruff 全规则通过 |
| Roadmap | 生成器自校验与 validator/聚焦测试通过；Excel PRD-136=`已完成`、PRD-137=`未启动` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 build trust、公开字段和认证域。发布批准只消费受信 release artifact 并在同事务生成 canonical
  digest chain 与用途隔离签名；固定 feed/scanner 拒绝任意网络目标。Developer/Platform API 使用不同凭据，
  DTO 和 DOM 不包含仓库 URL、builder 内部身份、原始漏洞利用细节、KMS response 或私钥。外部路由闭集与
  Excel 一致，PatchRelease 写能力不扩张公共 HTTP 合同。修正后无未关闭 P0/P1。
- 第二轮复核 generation、撤销、例外双控、patch rollback 和前端并发。旧 scan 只能 supersede，密钥撤销
  实时使报告失效；例外绑定 exact finding/artifact/owner/expiry 且请求人不能批准。patch 强制 N/N-1、回滚
  digest 和签名复验；前端 owner/epoch、Abort 与动态权限清除迟到状态。修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 正式结论保存在本文和 Git，原始测试结果按运行独立保留。`.tmp` 还包含冻结源码、基础设施和持久
  VM 磁盘，不能整体删除；只可单独核验已停止、无依赖的可重建测试目录后处理。
- 审计只输出规则命中计数，不输出密码、Token、Cookie、client secret、TOTP、step-up grant、签名值、私钥、
  Provider Key、漏洞 exploit、客户正文、对象 URL 或本地管理员凭据；本地测试凭据只在进程内使用，不打印
  或持久化。
- provenance/environment 使用固定字段和 digest；安全报告与 Console 不读取 Storage，不投影内部仓库、
  builder、路径、可变 URL、原始 KMS response 或漏洞利用正文。未跟踪 PRD-101..105 用户文档不进入暂存集。
- 最终暂存集只包含 PRD-136 实现、测试、文档和路线图文件；生产凭据字面量、Bearer 值、私钥块、JWT/
  Provider Key 形态、URL 携带 secret、本地管理员密码、本机用户路径、凭据文件/`.env` 和受保护
  PRD-101..105 文档命中均为 `0`。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、PRD-133 Developer/Platform 相邻回归和静态检查。`0123 -> 0130`
全数据库矩阵、集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、
SQLite/PostgreSQL、N/N-1、四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。
G6 只能形成 `release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生修复复验

同 PRD-134/135/137 合批。worker 领取与补丁批准/发布/回退均使用对应 UPDATE 游标的 rowcount，
不依赖 SQLite `changes()` 或前序语句累计写入。保留状态、owner、revision 和原有失败码。

- PostgreSQL：`g6-security-09c8ecef524c4521b168228d57388585-junit.xml` 中本条 2/2 通过，
  包含 attestation、扫描/双控例外、patch 生命周期和严格读取。该整轮另有 6 项失败，不能说整轮通过。
- SQLite：`sqlite-r1-junit.xml` 中本条 2/2，整次 16/16，592.741 秒。
- Console：`console-unit-junit.xml` 中本条 5/5。

证据位于 `.artifacts/g6-security-20260907/`，独立敏感审计为 `prd-136-sensitive-audit.json`。
两轮复核见本条 PRD 第 8 节。数据库事务和状态合同通过，不等于真实漏洞扫描服务、供应链环境、
独立审批、三浏览器或 G6 总体验收通过。
