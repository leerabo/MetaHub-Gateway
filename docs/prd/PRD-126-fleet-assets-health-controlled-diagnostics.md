# PRD-126 Fleet 资产、健康与受控远程诊断

状态：设计冻结，尚未编码；计划迁移 0118。依赖 PRD-095/120..125，范围以 Excel PRD-126 为准。

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

## 6. 专项验证计划

0118 迁移；1 万节点稳定分页/权限投影/健康模型；无授权、跨域、到期、撤销、参数注入、命令重放；超时取消和 Agent 重启恢复；结果字段/Secret 扫描；Platform/Workspace 三浏览器域隔离；列表/健康传播 SLO；审计 actor/reason/request_id 和静止终态。
