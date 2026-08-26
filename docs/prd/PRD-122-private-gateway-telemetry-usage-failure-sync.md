# PRD-122 私有网关遥测、用量与失败事实同步

状态：已完成；迁移 0114、严格遥测协议、本地 append-only spool/断点续传、Control 连续 ACK 与 Usage 幂等
对账、Workspace 水位/策略和 Platform Fleet 安全投影均已交付。依赖 PRD-017/051/099/109/120，范围以
Excel PRD-122 为准。

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

## 6. 实施闭环

- Shared SDK 固定 `health|operational|usage|failure` 判别联合、三档 policy、RFC 8785 payload hash、递归
  禁止字段扫描和严格 receipt/cursor 合同；异常只返回固定 `edge_telemetry.*` code。
- Gateway 本地 SQLite spool 使用 `BEGIN IMMEDIATE` 分配单调 sequence，批次和 receipt 均 append-only；
  epoch 只有旧批次全部 ACK 后才可旋转。Uploader 仅向 HTTPS/loopback Control 发送 gzip、EdgeSession，
  禁止重定向并要求 no-store exact receipt。
- Control 在一个事务中完成设备/能力/policy 恢复、batch 去重、连续 cursor、receipt、Usage/失败事实、
  Fleet 投影、事件和 outbox；重复同 hash 回读原 receipt，同 sequence 异 hash 冲突，乱序只推进无缺口 ACK。
- Workspace 读路径分别以 `edge.telemetry.read` 和 `edge.usage.read` 裁剪水位/失败与账务；policy 修改使用
  Cookie、CSRF、Origin、`If-Match` 和 `Idempotency-Key`。Agent 只接受自身 mTLS + EdgeSession；Platform
  只获得时间、资源桶、错误率和固定失败码，不获得 Workspace/tenant、model/provider 或 payload。
- Console 显示三档策略、四流 ACK/next/seen、用量/成本和固定失败分布；窄屏网格、空投影 Fleet、动态
  撤权、离页清理和 exact response DTO 均有自动化及真实浏览器证据。

## 7. 专项验证与批次边界

0114 往返/fresh/down guard、严格 DTO、并发本地 sequence、gzip/重定向、重复/乱序/缺口/epoch/同序列异
hash、policy 降级、Usage 无重复、角色裁剪、跨域凭据、Fleet 投影、禁止字段/duplicate key/压缩炸弹、
双视口 Browser、敏感扫描和静止终态均已执行，结果见 `docs/testing/PRD-122-tests.md`。

PRD-122 完成后下一权威项为 `PRD-123 - 私有网关本地Secret托管与轮换`。7 天真实断网追平、热路径
额外开销、三浏览器三缩放和 0111..0122 批次迁移矩阵仍按批次纪律保留到 PRD-130 独立提交后统一执行
G5，不能以本条 Chromium 和聚焦测试替代。
