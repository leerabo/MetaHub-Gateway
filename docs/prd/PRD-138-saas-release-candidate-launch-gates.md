# PRD-138 SaaS 发布候选、核心迁移波次与全产品门禁

状态：设计冻结，尚未实现。依赖 PRD-090..137；范围以 Excel PRD-138 为准。本条只生成 RC，不标记公开 GA。

## Excel 权威合同

- API：`GET /platform/launch-gates`、`POST /platform/launch-gates/{id}/evidence`、
  `POST /platform/launches`、`POST /platform/launches/{id}/rollback`。
- 对象：`LaunchGate`、`LaunchEvidence`、`LaunchWave`、`GoNoGoDecision`、`LaunchRollback`、
  `PostLaunchReview`。
- 事件：`launch.gate_failed`、`launch.wave_started`、`launch.wave_completed`、
  `launch.rolled_back`。
- SLO：Control 99.9%、Gateway 99.95%；关键页面 p75 < 1.5s；连续 72 小时无 P0/P1。

## 1. 产品闭环与非 GA 边界

系统从已签 verifier 导入 WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW、migration、灾备、支付、Edge、
隐私、合规、供应链和隔离验证证据，为固定 candidate digest 计算 gate。产品、SRE、安全、测试、财务、法务分别以
actor kind 会签；actor kind 映射现有 platform permission，不新增角色，同一账号不能满足多个独立会签。

全部 gate、证据 freshness、72 小时长稳和会签通过后，platform_owner 只能启动
`internal -> personal_pilot -> enterprise_pilot -> managed_private_pilot` 四波。每波绑定明确 cohort、feature flag、
迁移 plan、观测窗口、support staffing、财务/法务 checkpoint 和 rollback plan；异常自动暂停并可回退上一已验证波次。
完成后状态只能是 `release_candidate`。PRD-139 起的存量 Console 分域迁移和 PRD-148 最终门禁完成前，API、数据库、
文档和 Console 都不能出现 `general_availability` 成功事实。

## 2. 0130 gate、证据、会签与波次

- LaunchGate 是平台全局版本化定义，保存 required verifier/code、threshold、freshness、blocking severity 和 policy digest；
  运行时不能编辑当前 candidate 的要求。
- LaunchEvidence、LaunchRollback 和 PostLaunchReview 带验证/试点 `workspace_id/tenant_id` 复合 owner。
  Evidence 绑定 gate、candidate digest、verifier identity/version/signature、artifact URI digest、started/finished、
  exact outcome/metrics、environment/provenance 和 expiry；平台全局 gate 使用专用验证 Workspace，不能提交无 owner
  evidence。原始凭据、日志正文、本地路径或可变 URL 不保存。
- LaunchWave 保存 kind、cohort manifest digest、feature flag generation、migration/rollback plan、status、observation deadline、
  health watermark 和 owner；cohort 关联不授予平台新的 Workspace 读取权限。
- GoNoGoDecision 追加保存 actor kind、account、role/permission revision、candidate/gate generation、decision/reason code 和签名；
  同账号多角色不能重复占位，申请/执行者不能自批。
- LaunchRollback 绑定目标 Workspace、触发 gate/metric、from/to wave、checkpoint、job receipt 和验证结果；
  PostLaunchReview 绑定试点 Workspace，保存固定摘要、action items 和 RC conclusion，不能产生 GA 状态。

Down 在任何 evidence、decision、wave、rollback、review 或事件存在时拒绝；候选历史不可删除。

## 3. 证据接入、权限与 Platform Console

evidence endpoint 只接受 verifier-signed envelope 和已注册 artifact digest；人工用户不能直接填写 passed/metric，也不能
上传任意文件替代正式证据。candidate composite digest 覆盖 code tree、migration checksums、public contracts、release
artifacts、test manifest 和 gate policy；任一输入变化提升 generation，全部旧 evidence/decision 立即 stale。

产品/SRE/安全/测试/财务/法务 actor kind 分别映射受控现有平台 permission；platform_owner 只有当前 generation 全部
通过后可启动或回滚。Console 以密集 gate 表、证据 freshness、会签、四波状态、SLO、阻断项和恢复操作为主；不提供
override 按钮，不展示 tenant 明细、Secret、原始日志或对象地址。所有写 step-up、CAS、幂等、no-store 并逐请求恢复权限。

## 4. 第一轮自审：门禁真实性与不可绕过

- P0：人工把 gate 改为 passed 会让单人发布。修正为 verifier-signed evidence、固定 policy 和无 override 状态机。
- P0：platform_owner 同时充当全部会签人会破坏独立性。修正为 actor account 唯一约束，角色只映射可签 actor kind。
- P0：LaunchEvidence 引用可变文件会在会签后替换。修正为 digest、signature、verifier provenance 和 immutable receipt。
- P1：试点 cohort 查询让平台读取租户数据。修正为 tenant-qualified manifest digest 和执行命令，launch projection 只见状态。

## 5. 第二轮自审：陈旧证据、长稳与回滚

- P0：代码/migration/合同变化仍沿用旧会签会发布未验证输入。修正为 composite candidate digest 和全量 stale 传播。
- P0：72 小时长稳与其他压力/测试重叠会产生不可归因结果。修正为独占基线、完整时间戳/错误样本、最大采样间隔和
  系统负载证据；任何 5xx/P0/P1 或证据缺口失败。
- P0：自动回滚只切 feature flag 不恢复迁移/Edge 状态。修正为波次 checkpoint、持久 rollback job、领域补偿和复验。
- P1：完成四波后直接写 GA 会越过 PRD-139..148。修正为数据库 CHECK 只允许 `release_candidate`，GA 状态不在本迁移中。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证与 G6

0129/0130 往返/down guard；verifier 签名/owner/freshness/失败/跳过/敏感证据；candidate digest 变化和全 stale；
六 actor kind 独立会签、动态撤权、非自批；四波启动/暂停/恢复/回滚和 cohort 隔离；WB/API/BB/GR/PERF/WEB/UI/
UCD/COMP/SEC/FLOW 全组；10 万迁移、工单 SLA、SDK N/N-1、隐私/合规/供应链/隔离、灾备/支付/Edge；三浏览器
三缩放、Windows/Linux、SQLite/PostgreSQL、代理/断网；Control/Gateway/页面 SLO；独占 72 小时长稳；数据库、
outbox/queue、进程、端口、临时目录静止终态和敏感/禁止文件命中 0。

任一证据缺失、失败、跳过、陈旧或含敏感明文，G6 失败。通过只生成 RC；公开 GA 必须由 PRD-148 关闭。
