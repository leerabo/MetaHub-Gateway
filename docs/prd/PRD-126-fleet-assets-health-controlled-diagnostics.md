# PRD-126 Fleet 资产、健康与受控远程诊断

状态：已完成；迁移 0118、Fleet 健康投影、Workspace/Platform 双域视图、受控诊断授权状态机、无 shell Agent 执行器和 Console 已交付。依赖 PRD-095/120..125，范围以 Excel PRD-126 为准。

## Excel 权威合同

- API：`GET /platform/fleet`、`GET /edge/deployments/{id}/health`、`POST /edge/deployments/{id}/diagnostic-grants`、`POST /edge/deployments/{id}/diagnostics`。
- 对象：`FleetDeployment`、`EdgeHealthSnapshot`、`DiagnosticGrant`、`DiagnosticCommand`、`DiagnosticResult`。
- 事件：`edge.health_changed`、`edge.diagnostic_requested`、`edge.diagnostic_completed`。
- SLO：1 万节点 Fleet 列表 p95 < 1s；健康变化 < 2 分钟可见；诊断可超时取消。

## 1. 产品闭环

Control 从 PRD-120..125 的证书、版本、容量、配置/遥测水位和验证事实生成字段裁剪的 FleetDeployment/health 投影。健康分数由版本化权重和可解释固定信号计算，原始 tenant 事件不暴露给平台。tenant 运维看自身详细健康和恢复动作；platform_ops 看全 Fleet 运营字段、筛选和告警。

诊断仅支持签名命令目录中的只读动作，如版本、水位、磁盘预算和连接摘要；platform_ops 提交原因后由客户批准生成短时 DiagnosticGrant，Agent 拉取签名 command、执行有界 subprocess/内部 handler 并返回 exact result。没有通用 shell、任意参数、任意文件读取或自由文本输出。

## 2. 0118 数据与状态机

- HealthSnapshot 不可变，head 用 deployment revision CAS；signals 使用固定 code/value bucket，score model version 显式记录。
- DiagnosticGrant 绑定 deployment、command code/参数 hash、requester、workspace approver、platform approver、TTL、max runs 和 authority revisions。
- Command 状态 `requested|authorized|leased|running|completed|failed|cancelled|expired`，租约/generation 支持重启恢复；Result exact schema 和字段级裁剪。
- Fleet 索引只存平台允许字段并按 `(status, region, health, deployment_id)` 稳定 keyset 分页；不在查询时跨 tenant 扫描原始表。

## 3. 双域授权与 Console

`GET /platform/fleet` 仅 Platform Session + `platform.edge_fleet.read`；Workspace health 仅 Membership 权限；两个 DTO 无共用超集。诊断要求 SupportGrant/DiagnosticGrant 的客户授权、平台审批、step-up、非自批、短 TTL 和逐请求动态恢复。Console 的平台/租户页面共享视觉组件但不共享 store、请求或缓存 key。

## 4. 第一轮自审：平台投影与命令边界

- P0：平台 Fleet 直接 join tenant 详情会把平台角色变成成员。修正为事务/worker 生成独立字段裁剪投影。
- P0：命令字符串白名单仍可通过参数形成 shell 注入。修正为枚举 command handler、严格参数 DTO、无 shell 执行。
- P0：只靠客户一次批准永久诊断会扩大权限。修正为每个 command/grant 绑定 TTL、次数、参数 hash 和 authority revision。
- P1：健康分数无解释会误导运维。修正为固定 signals、模型版本和贡献项。

## 5. 第二轮自审：撤权、取消与结果裁剪

- P0：已下发命令在 grant 撤销后继续执行。修正为 lease 前和每个长步骤重新校验 grant；撤销推送取消水位。
- P0：诊断结果自由 stdout 可泄漏 Secret。修正为 handler 返回严格结构，禁止 stdout 透传并执行本地敏感扫描。
- P1：健康乱序快照回退状态。修正为 generation 单调，迟到事实只留审计。
- P1：超时只改云端状态但本地进程继续。修正为可取消执行上下文、kill deadline 和完成 receipt；静止终态确认子进程 0。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 实施闭环

- 0118 建立不可变健康快照/head、字段裁剪 Fleet 索引、诊断 grant/command/result、不可变事件/outbox 和幂等 action command；复合 owner、状态 CHECK、generation/revision、CAS、down guard 和 tenant deletion manifest 均在数据库内约束。
- 注册/证书、Desired State、遥测和交付验证事务会在 0118 可用时同步刷新健康投影；相同事实摘要不重复生成 generation，迟到事实不能回退 head。Platform Fleet 只读取独立索引并使用签名 keyset cursor，不在请求时 join Workspace 私有表。
- Workspace 健康读取分别要求 `edge.health.read` 和 `edge.diagnostic.read`：只有健康权限时 grants/results 返回空数组；客户以 `edge.diagnostic.approve` 和绑定 Deployment 的全局 Identity step-up 批准或撤销短时 grant。
- Platform 入口使用独立 Platform Session 和 `platform.edge_fleet.read/manage`；申请授权、执行和取消均使用固定原因、幂等键及严格 DTO。平台 DTO 不含 Workspace/tenant/display name，平台 step-up 不携带 Workspace ID。
- Shared SDK 只接受四种只读命令及 exact 参数/结果联合类型，命令使用 Ed25519 用途分域签名并绑定 deployment、grant、参数 hash、authority revision、TTL 和次数。Gateway 仅调用编译期 handler，不导入 shell/subprocess，不返回自由 stdout/stderr。
- Workspace 与 Platform Console 只共享健康展示 helper 和样式，不共享 store、请求、cursor 或缓存 key；迟到响应按 owner generation 丢弃。TOTP、恢复码、WebAuthn credential 和 step-up grant 均在每个 await 后复核 ticket，并在 `finally` 清空。

## 7. 专项验证

聚焦自动化已覆盖 0118 往返/down guard、1 万节点稳定 keyset 与逐页 p95 < 1s、权限投影/健康模型、无授权/跨域/到期/撤销/authority drift/参数注入/命令重放、超时取消、Agent 重启恢复、结果 exact schema、Secret 扫描、双域前端隔离和敏感值短生命周期。详细证据见 `docs/testing/PRD-126-tests.md`。

两轮实施自审均关闭 P0/P1；下一权威项为 PRD-127。真实 Chromium/Edge/Firefox、健康传播部署 SLO、长稳、全迁移矩阵、性能和安全全门禁仍按 S5 批次规则保留到 PRD-130 独立提交后统一执行 G5，不能用本条聚焦测试替代。
