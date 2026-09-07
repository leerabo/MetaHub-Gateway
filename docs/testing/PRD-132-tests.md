# PRD-132 测试记录

状态：已完成。客户工单、加密消息与内部备注、附件隔离扫描、SLA、升级、满意度、Workspace/Platform API
与双域 Console 已交付；本条保留独立设计、聚焦测试、敏感审计和提交。下一权威项为
`PRD-133 - 公共Control API与SDK版本弃用治理`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0124 Migration | `0123 -> 0124 -> 0123 -> 0124`、fresh/current head、重复执行、Workspace/Platform 权限、ticket/message/attachment/scan job/SLA clock/event/escalation/survey/event/outbox/action/scheduler、owner/invariant/status trigger、tenant deletion manifest、业务事实 down guard、完整性和外键 |
| Control API | Cookie/CSRF/origin、严格 DTO、If-Match、幂等、owner 同形 404、客户/internal 可见性、分域正文加密、附件 quarantine/scan/迟到结果、SLA pause/resume/risk/escalation、解决/重开、满意度、动态撤权和 SupportGrant/JIT 边界 |
| Console | Workspace/Platform Cookie-only client、exact DTO、owner/epoch/Abort、列表/消息/附件/SLA/升级/解决/重开/满意度、Platform 裁剪队列/内部备注/JIT 入口、离页/登出/身份漂移销毁正文与草稿 |
| 必要回归 | PRD-131 相邻状态、Workspace/Platform 导航目标、扩展权限完整性和双域 Session 边界 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-132 Python 聚焦 | `6/6 passed`；0124 migration 往返/down guard 与工单、消息、附件、SLA、升级、survey、双域权限/JIT API |
| PRD-132 Console | `5/5 passed`；严格双域 client/DTO/state/render/controller/cleanup 边界 |
| Console 必要回归 | 权限/Platform Access/PRD-131/132 联合 `29/29 passed`；导航 Python `3/3 passed` |
| Python static | PRD-132 变更后端源码与测试 Ruff 全规则通过 |
| Python format | PRD-132 变更后端源码与测试 Ruff formatter 检查通过 |
| Console lint | `console lint ok (398 files)` |
| Roadmap | 生成器自校验通过；validator `195` 项通过，下一项为 `PRD-133`；validator 聚焦 `5/5 passed` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 owner、正文、附件和支持权限。ticket/message/attachment/SLA/escalation/survey 均以
  Workspace/tenant 复合 owner 查询；客户 DTO 永不返回 internal note，Platform 队列只返回裁剪投影；正文按
  visibility/purpose 独立加密且事件不含正文；附件对象引用、路径、URL 和内容不进入普通 DTO/DOM；关联
  request_id 不授予诊断访问，Platform 诊断继续逐请求校验 SupportGrant scope、TTL 和 revision。修正后无未关闭
  P0/P1。
- 第二轮复核 SLA、并发、重启和迟到结果。状态写入使用 If-Match/revision，命令按 actor/action/idempotency/
  request hash 绑定；SLA deadline、clock event、scheduler watermark、lease fencing 和 escalation generation 持久化；
  scan result 绑定 attachment revision/digest，迟到 worker 不能发布旧对象；Console 逐次复核 owner、page epoch、
  permission fingerprint 和 Abort owner，所有离开路径销毁正文与草稿。修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 本条历史专项的 pytest 副本可重建；2026-09-07 起 `.tmp` 还含冻结源码、持久磁盘和可复用基础设施，
  不再允许整体删除。清理须逐项核对进程引用、恢复用途和正式证据索引。
- 暂存审计只输出规则命中计数，不输出密码、Token、Cookie、TOTP、私钥、Provider Key、Prompt/Response、
  客户工单正文、内部备注、对象 URL 或本地路径值；未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终 42 个暂存文件、7,029 个新增行审计结果：凭据字面量赋值 `0`、Bearer 值 `0`、私钥块 `0`、JWT
  形态 `0`、本地开发管理员凭据 `0`、凭据文件/`.env` `0`、受保护 PRD-101..105 文档 `0`；`.tmp` 已删除。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要导航权限回归和静态检查。`0123 -> 0130` 全数据库矩阵、
集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、
四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 只能形成
`release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。

## 2026-09-07 原生数据库补充验证

独立设计及两轮复核见 PRD-132 第 7 节。旧权限 helper 改用共享表检查；ticket PATCH 以当前
UPDATE 的 rowcount 判定 CAS。原生增量 0141 修复文件终态触发器的 ctid 误用，不放宽文件身份、
owner、来源、扫描结果与终态不可变约束。

证据目录：`.artifacts/g6-operations-20260907/`。

| 验证 | 实际结果与证据 |
| --- | --- |
| PostgreSQL 17.11 / Windows | `g6-operations-cdadab81c6864cce9db3280ed6c360ca-junit.xml` 中本条模块 6/6 通过；该次全运行 16/16，439.88 秒 |
| 工单业务 | 双域工单、内部备注隔离、幂等、SLA 单次升级、真实文件处理后的附件隔离/扫描/超时、完整权限目录 |
| CAS 竞争 | 另一真实连接合法提交 revision/updated_at 后，旧 PATCH 返回 `support.revision_conflict`；保留竞争行，未新增消息 |
| 文件迁移与身份 | 0141 up/down/up、重复执行；UploadedFile 与 FileExtract 事实不变；十类身份/终态篡改拒绝，合法无变化更新成功 |
| SQLite 业务 | `sqlite-r4-junit.xml` 中本条 3/3 通过；同次三条 PRD 共 7/7，444.93 秒 |
| Console | `console-unit-junit.xml` 中本条 5/5 通过，不作为真实浏览器证据 |
| 静态检查 | 本条变更源码/测试 Ruff 与 diff 检查通过 |

保留 d06fa086 运行本条 3 通过、2 失败：附件由文件触发器拒绝；CAS 测试的竞争写遗漏更新时间。
分别修复生产迁移和测试数据，未把非法竞争写放行。SQLite r3 的系统临时目录访问错误也保留，
r4 使用新的项目内隔离目录完成，未修改系统权限。各次运行结果不混成一次通过结论。

独立敏感审计记录：`prd-132-sensitive-audit.json`，检查源码、迁移、测试、证据摘要与暂存范围。
测试里的控制时钟和鉴权替身不代表实际 SLA 演练或独立支持授权。本条补充结果不替代 Linux、
三浏览器、性能、支持演练、全批次安全验证与连续运行证据。
