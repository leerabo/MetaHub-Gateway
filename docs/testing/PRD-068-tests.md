# PRD-068 行业/场景策略模板库测试记录

状态：**已完成**。设计与两轮自审、实现、0060、
后端/Shared/Gateway/Console 专项、一次根级 Python 全量及其原失败定向闭环、独立 Provider Adapter
mock 门禁、开发库升级、三服务、正式 API 真实 apply、Ruff、中文注释门禁和 PAGE-095 应用内 Browser
双视口验收均有实际结果。该状态不代表全部 108 条 PRD 已完成，
`all_108_prds_complete=false`。

## 1. 验收范围

- 五条只读内置模板：低成本、高安全、只用国产模型、预算防爆、客服脱敏。
- 目录、同租户绑定选项、精确影响预览、双强 CAS、原子应用、幂等回放和模板感知补偿回滚。
- `policy_template.read/apply` 与模板类别复合权限、路由预检后撤权、跨 tenant 同形失败。
- Snapshot head/candidate/template/dependency 漂移、nonce/request 冲突、严格 query/JSON/media type/
  Content-Length 和有界正文。
- active template application 的持续发布不变量，以及普通 PRD-045 rollback 兼容。
- Shared `mask` 非终止求值、Gateway Chat/Stream/Embeddings copy-on-write 脱敏和失败关闭。
- 0060 正向、反向、往返、历史保真、RBAC 定制矩阵保留、降级阻断、外键和完整性。
- PAGE-095 权限裁剪、异步 owner、迟到响应、双 CAS 确认、敏感字段零 DOM 和响应式布局。

## 2. 已完成自动化结果

| 验证项 | 结果 | 说明 |
| --- | ---: | --- |
| Control 模板与审计专项 | `13 passed` | 12 条集成测试与 1 条 Audit 摘要测试 |
| 0060 迁移专项 | `12 passed` | fresh/up/down/round-trip/保真/阻断/完整性 |
| Shared PRD-068 组合 | `26 passed` | RBAC、错误、Webhook、KeyPool、快照与 mask 求值合同 |
| Gateway 脱敏专项 | `12 passed` | Chat/Stream/Embeddings、copy-on-write、上限和 fail closed |
| PRD-045 普通 rollback 回归 | `2 passed` | 模板补偿分支没有改变普通上一版回滚语义 |
| PAGE-095 Console 专项 | `16 passed` | API、before-candidate 双 CAS、owner、权限、完整确认摘要、移动弹窗边界、成功 ID、两类 incompatible、Project 漂移、迟到响应和 DOM 隐私 |
| PAGE-095 应用内 Browser | `passed` | `1280x720`、`390x844`、五模板、incompatible/ready-with-warning、Snapshot v12、注销清理、无页面级横向溢出和零 Console error |
| 安全与回放复审 | `11 passed` | 权限回放、遮蔽、持续不变量、回滚和 0060 约束 |
| 真实 Provider 脱敏复审 | `2 passed` | Chat/Embeddings/Stream/Fallback 的 Provider 前脱敏与零泄漏 |
| Provider Adapter mock 合并门禁 | `92/92 passed` | 九类默认、本地与迁移兼容 Provider 合同；`failed_cases=0` |
| Node 示例全量 | `33 passed` | 官方 OpenAI 客户端示例与错误脱敏 |
| Console 全量 | `619 passed` | 全部控制台单元/合同测试 |
| Console lint | `192 files` | 通过 |
| PRD-068 Python Ruff | `All checks passed` | Control 模板、测试、Shared KeyPool 直接修改面 |
| 中文注释门禁 | `comment coverage ok` | 未修改或放宽 `scripts/ci/check-comments.py` |
| 根级 Python 全量 | `2025 passed / 29 failed` | 共 2054 个节点，只执行一次；不把后续闭环伪写为全量零失败 |
| 原全量失败节点闭环 | `29/29 passed` | 19 个 Gateway/PRD-064 环境节点与 10 个迁移/旧合同节点全部定向通过 |
| 开发库 | `60/0060` | `integrity_check=ok`，外键违规 0，幂等升级已通过 |
| 运行服务 | `200/200/200` | Gateway、Control、Console 当前健康检查通过 |
| 正式 API 真实 apply | `passed / Snapshot v12` | application 1 条，item/dependency 各 2 条，三类 event/Audit 各 1 条；request ID `req_prd068_runtime_apply_redaction_v12` |

所有 pytest 均把 `TEMP/TMP`、`--basetemp` 和 `cache_dir` 限定在 `D:/MetaHub/.tmp`，
未使用 `C:/tmp`、未提权，也未递归扫描历史 `pytest-of-rabol` 目录。

## 3. Control 主流程

1. 真实 Session 登录后读取固定五条目录，响应只返回当前 tenant 的 active Project/Alias 安全投影，
   并携带 `request_id/trace_id` 与 `Cache-Control: no-store`。
2. 为五类模板构造真实依赖，逐条执行精确预览与 apply，Snapshot 从 v1 单调推进到 v6；每次只新增一个
   generated Policy，application 的 previous/result 版本精确相邻。
3. 低成本从当前生效 PricePlan、完整能力、Provider adapter、KeyPool/健康资格中选择稳定最低 CNY
   候选；迁移平台和能力不完整候选不会因价格低被选中。
4. 高安全冻结 Alias Environment、默认/备用 Model 与 Provider；国产模板证明每个 active Environment
   都有同名纯国产 Alias，不能回退同名 direct Model 或迁移平台。
5. 预算防爆要求 active Virtual Key 和当前可适用预算事实；客服脱敏要求 Control gate 与
   `policy-mask-v1` readiness。
6. 应用成功原子写入 application/items、Policy/Rule/Action、Snapshot、三条 AuditLog 和三类 source
   event；六类提交后派发任一失败不会短路其余派发，也不会反转已提交事务。
7. 相同 tenant、actor、request ID 与 canonical intent 回放恢复原 application/Snapshot，
   `idempotent_replay=true` 且不重复写持久事实。

## 4. 异常与 CAS

- GET 未知/重复 query、无 template 却夹带 binding、GET 正文：
  `422 policy_template.query_invalid`。
- apply 错误 media type：`415 policy_template.media_type_unsupported`。
- 重复 JSON key、未知字段、非规范 Content-Length、非法强 ETag：
  `422 policy_template.payload_invalid`。
- 超过 32 KiB：`413 policy_template.payload_too_large`；缺少任一 CAS：
  `428 policy_template.precondition_required`。
- template version 变化：`409 policy_template.version_conflict`。
- Snapshot head、candidate、impact 或预览依赖漂移：`409 policy_template.impact_conflict`。
- 同 request ID 不同意图、nonce 被其他请求消费：分别为
  `policy_template.request_conflict`、`policy_template.nonce_conflict`。
- 同 Project 已有未回滚 application：`policy_template.application_conflict`。
- 目录损坏、审计/事件失败或恢复图不一致统一为 `policy_template.unavailable`，响应不包含 SQL、
  绑定、价格、预算、DSL 或底层异常文本。

## 5. 权限与租户

- `policy_template.read` 与 `policy_template.apply` 分离；Developer/Viewer 对导航、DOM、预取和直接 API
  均失败关闭。
- high-security/domestic/redaction 额外要求 `policy.write`；budget 要求
  `billing.budget.write`；low-cost 要求 `billing.budget.write|routing.write` 任一。
- Admin、FinanceAdmin、SecurityAdmin、OpsAdmin 的真实数据库矩阵分别验证可见性和类别职责；
  不按角色字符串补权。
- dependency 通过后撤销 `policy.publish` 的竞态在 rollback 写锁内返回
  `403 auth.permission_denied`，没有 Snapshot、Audit 或 application 半状态。
- 第二 tenant 的 Project/Alias 绑定与不存在绑定同形返回 `policy_template.binding_invalid`；
  另一 tenant 的模板事实、选项、Policy、Snapshot 和 event 不可见。
- tenant、actor、版本、generated ID、hash 和权限全部由 Session/数据库恢复，正文/query 不能覆盖。

## 6. Snapshot、回滚与持续不变量

- 模板 apply 必须已有真实 published baseline，不会偷建 v0 或额外 Snapshot。
- 模板目标 rollback 会禁用且仅禁用本 application 的 generated Policy，再发布保留其余当前项的补偿
  Snapshot；application 单调进入 `rolled_back`，重复请求幂等恢复。
- current artifact、generated graph、owner、版本或排序不能证明精确撤销时返回
  `policy_template.rollback_conflict`，head 不变。
- 普通 publish、后续模板 apply 和模板补偿 rollback 都复用 active template invariant：
  高安全/国产后置 route、预算/脱敏前置 allow、低成本后置 deny 等绕过均被 409 阻断。
- PRD-045 非模板上一版 rollback 的原响应、版本推进和审计语义继续由 2 条直接回归覆盖。

## 7. Gateway mask

- Shared evaluator 在同一有界排序中把 `mask/log_only` 记录为 modifier，继续扫描首个
  `allow/deny/route_to`；无决定项时默认 allow。
- Gateway 只在最终允许继续时执行一次脱敏，顺序固定为
  `Policy -> mask -> Routing -> DLP -> RateLimit -> Budget -> Provider`。
- Chat 只复制并改写 user 文本和明确 text part；Embeddings 支持 `str/list[str]`；system、assistant、
  URL、图片、音频、文件和工具结构不递归改写。
- 中国手机号、18 位身份证号和邮箱替换为固定占位符；客户端原对象引用保持不变。
- 未知正文、混合数组、token 数组、遍历超限返回 `gateway.policy_mask_unsupported`；capability 或
  执行器损坏返回 `gateway.policy_mask_unavailable`，两类均不会把原文发给 Provider。
- RequestLog 只记录 `policy_mask_applied` 和稳定 Policy ID；测试证明结果、异常、日志和安全摘要均不含
  Prompt、命中片段、位置或正文 hash。

## 8. 0060 迁移专项

- 真实执行 `0053 + seed -> 0059 -> 0060`，并覆盖 fresh 0060。
- `0059 -> 0060 -> 0059` 后关键表逐行数据、`rowid`、Audit/RequestLog 表结构、索引和触发器一致。
- RBAC catalog revision 5、新权限默认值与六角色定制矩阵在往返中保真；未知自定义/后续依赖不会被静默丢弃。
- 模板 application、已发布 mask、RequestLog mask trace、Audit/Webhook 和未知依赖存在时 down 原子阻断。
- 专项终态 `PRAGMA integrity_check=ok`，`PRAGMA foreign_key_check=[]`。

## 9. 最终验收状态

1. 唯一一次根级 Python 全量已结束，真实分布为 `2025 passed / 29 failed`；原 29 个失败节点已按原
   node ID 定向 `29/29 passed`，没有再次从头运行全量。
2. 独立 Provider Adapter mock 合并门禁已通过 `92/92`，报告为
   `.artifacts/prd068/provider-adapter-contract-report.json`，报告内 `status=passed`、
   `coverage_complete=true`、`failed_cases=0`，报告 SHA-256 为
   `A04F5E56F4DFA48DBAF526A32E2B1FA97D837773E3C794FEC84143BD14B468A1`。
3. 真实开发库已升级并幂等确认至 `60/0060`；当前 `integrity_check=ok`、外键违规 0。
4. Gateway、Control、Console 当前均为 HTTP 200；Gateway readiness 包含 `policy-mask-v1`。
5. 正式 apply API 已把基线推进到 Snapshot v12，candidate 与 current 一致；数据库中 application 1 条、
   item/dependency 各 2 条，生成 Policy 为 active。`policy.created`、`policy_snapshot.published`、
   `policy_template.applied` 三类 source event 与同 action AuditLog 均各 1 条；apply request ID 为
   `req_prd068_runtime_apply_redaction_v12`，`integrity_check=ok`、外键违规 0。
6. 应用内 Browser PAGE-095 双视口已通过。桌面端确认五模板顺序、客服脱敏已应用后的 incompatible
   防重复、Snapshot v12 当前 6 条策略、管理员权限、无敏感 DOM/URL 标记和零 Console error；移动端
   确认模板目录只在自身容器横向滚动、单列表单、预算模板 `ready_with_warnings`、完整确认摘要和无
   页面级横向溢出。确认原因只用于交互验收并取消，未产生第二次 apply。
7. 注销后回到登录页，模板页、模板按钮、modal、reason 和 Project binding DOM 计数均为 0；URL 只保留
   `#policy-templates`，Console error 为 0。相关模块静态扫描无 local/session Storage、IndexedDB 或 Cookie
   写入。
8. Browser 验收期间发现的 Candidate ETag 方向和移动确认框长 ID 撑宽均已修复；聚焦测试 `16 passed`、
   Node 语法检查和中文注释门禁通过，未重跑无关全量。PRD-068 可以标记完成，下一项为 Excel 确认的
   `PRD-069 | M2-企业增强 | P2 | 轻量敏感意图分类`。
