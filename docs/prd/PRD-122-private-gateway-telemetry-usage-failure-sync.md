# PRD-122 私有网关遥测、用量与失败事实同步

状态：设计冻结，尚未编码；计划迁移 0114。依赖 PRD-017/051/099/109/120，范围以 Excel PRD-122 为准。

## Excel 权威合同

- API：`POST /edge/telemetry/batches`、`GET /edge/deployments/{id}/telemetry-watermark`、`PATCH /edge/deployments/{id}/telemetry-policy`。
- 对象：`TelemetryPolicy`、`TelemetryBatch`、`UsageSyncCursor`、`EdgeFailureSummary`、`TelemetryReceipt`。
- 事件：`edge.telemetry_received`、`edge.telemetry_lagging`、`edge.usage_reconciled`。
- SLO：在线延迟 p95 < 60s；断网 7 天后 2 小时内追平；热路径额外开销 < 2%。

## 1. 产品闭环

security_admin 选择 `minimal|operational|billing` 三档 exact policy；Agent 在本地按时间桶聚合 metrics、Usage/成本和固定失败码，先写本地 append-only spool，再异步压缩上传。Control 以 deployment、stream、epoch、sequence 和 payload hash 去重，事务提交 batch/receipt/usage cursor 后返回连续 ACK 水位。断网保留积压并从最后连续水位续传；finance_admin 可核对本 Workspace 用量，platform_ops 只能读运营聚合。

## 2. 0114 数据与 wire

- policy 版本化并绑定 Workspace/Deployment；降级立即影响后续 batch，不追溯扩充历史。
- batch exact schema 只允许整数计数、毫秒、token/cost 定点数、model/provider 引用 ID、固定错误码、时间桶、时钟偏差和版本水位；禁止任意 attributes/tags/message。
- `(deployment, stream, epoch, sequence)` 唯一；同 key 不同 hash 为冲突，连续 cursor 只跨无缺口序列推进。
- Usage 与既有账本通过 source deployment/sequence 幂等归集；遥测 receipt 和 outbox 同事务，重试不重复计费。

## 3. 隐私、权限与 Console

Agent mTLS 只能追加自身批次；security_admin CAS 修改政策；finance_admin 只读 tenant usage；platform_ops 获取节点在线、版本、延迟、资源桶和错误率，不得到 Provider endpoint、请求/响应或租户业务标识。递归禁止字段包括 Key、Token、Authorization、Cookie、Prompt、Response、Endpoint 原文、文件路径、堆栈和自由文本。

页面显示同步水位、积压时长、最近 receipt、Usage 对账和固定错误分布；不提供 raw batch 下载。策略切换说明只展示数据类别，不展示内部 schema 或调试指引。

## 4. 第一轮自审：零敏感遥测

- P0：允许开放 tags/attributes 会绕过白名单。修正为每种 telemetry kind 的严格 DTO 联合，extra forbid 和递归禁止名扫描。
- P0：失败堆栈/endpoint 看似诊断字段但会泄漏。修正为本地映射固定错误码与组件码，原文不上云。
- P0：平台 Fleet 直接读 tenant batch 会跨域。修正为提交时生成独立运营投影，平台 API 只读投影。
- P1：日志打印反序列化异常会回显 payload。修正为异常只含固定 code、batch/sequence 摘要。

## 5. 第二轮自审：水位与计费

- P0：最大已见 sequence 作为 ACK 会跨过缺口导致丢数。修正为连续 cursor，缺口显式返回 expected sequence。
- P0：重试 batch 重复写 Usage 会重复计费。修正为唯一 source key、事务 receipt 和既有 usage ledger 幂等约束。
- P1：Agent 时钟回拨破坏时间窗口。修正为 server receipt time 权威，client bucket time 有界且记录 fixed skew bucket。
- P1：政策升级后补传旧高粒度数据违反当时同意。修正为 batch 绑定采集时 policy revision，服务端拒绝超出该 revision 的字段。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0114 迁移；重复/乱序/缺口/epoch 重置/同序列异 hash、7 天积压和断点续传；Usage 金额核对与无重复；policy 动态撤权；跨租户/平台投影；所有禁止字段变体和压缩炸弹；三浏览器水位/策略；在线/追平/热路径 SLO；云 DB、日志、事件、支持制品敏感命中 0 和静止终态。
