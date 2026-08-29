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

- 正式结论只保存在本文和 Git；pytest basetemp、SQLite 副本、缓存与过程输出均为可重现临时证据，提交前
  删除整个 `.tmp` 以及 PRD-132 产生的 `__pycache__`。
- 暂存审计只输出规则命中计数，不输出密码、Token、Cookie、TOTP、私钥、Provider Key、Prompt/Response、
  客户工单正文、内部备注、对象 URL 或本地路径值；未跟踪的 PRD-101..105 用户文档不进入本条暂存集。
- 最终 42 个暂存文件、7,029 个新增行审计结果：凭据字面量赋值 `0`、Bearer 值 `0`、私钥块 `0`、JWT
  形态 `0`、本地开发管理员凭据 `0`、凭据文件/`.env` `0`、受保护 PRD-101..105 文档 `0`；`.tmp` 已删除。

## 批次门禁

本条按批次规则只运行受影响单元/API 冒烟、必要导航权限回归和静态检查。`0123 -> 0130` 全数据库矩阵、
集成、FLOW、真实三浏览器/三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、
四波试点、灾备/支付/Edge 演练和 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 只能形成
`release_candidate`，不能公开 GA；本条专项结果不替代这些批次证据。
