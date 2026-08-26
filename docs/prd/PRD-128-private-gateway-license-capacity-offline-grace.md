# PRD-128 私有 Gateway License、容量与离线宽限

状态：设计冻结，尚未编码；计划迁移 0120。依赖 PRD-098/103/120/127，范围以 Excel PRD-128 为准。

## Excel 权威合同

- API：`POST /edge/licenses`、`GET /edge/licenses/{deployment_id}`、`POST /edge/licenses/{id}/refresh`；CLI：`metahub license import`。
- 对象：`EdgeLicense`、`SignedEntitlement`、`CapacityGrant`、`LicenseLease`、`LicenseAudit`。
- 事件：`license.issued`、`license.expiring`、`license.grace_started`、`license.capacity_exceeded`。
- SLO：本地授权 p95 < 2ms；在线刷新 p95 < 1s；默认离线宽限 >= 7 天。

## 1. 产品闭环

platform_billing 从已生效套餐/订单签发绑定 Workspace、Deployment、节点/并发/功能容量、issued/not-before/expires/grace、generation 和防回拨参数的 SignedEntitlement。Agent 在线拉取或由 tenant owner 离线导入，完整验签后原子替换本地 entitlement，并在热路径只读内存/LKG 决策。到期先进入清晰宽限：保留已接受请求和现有配置，只阻止新增超额节点/并发/付费能力；宽限结束按固定 policy 降级，不删除客户数据。

## 2. 0120 数据与本地判断

- EdgeLicense 与 SignedEntitlement 不可变版本/head；CapacityGrant/Lease 使用定点/整数上限、generation 和原子计数。
- manifest 使用独立 license signing purpose，绑定 deployment/workspace/tenant、商业 source revision、容量、有效期、grace 和 hash；不携带账单 PII 或访问权限。
- 本地 monotonic clock observation 与最后可信 wall time 共同检测回拨；时钟异常不会延长 entitlement，只进入受限恢复流程。
- refresh 幂等且 generation 单调；同 generation 不同 hash、错误目标或错误签名失败关闭并保留当前有效 LKG。

## 3. 权限与产品行为

platform_billing 可签发但不能读取 tenant 内容；tenant owner/finance_admin 查看安全摘要，只有 owner 可导入；platform_support 无签发/扩容权；Agent 的 License 只决定 capacity/feature，不产生任何数据读取权限。Console 显示容量使用、到期、宽限、刷新状态和扩容入口，避免突然“服务已停”式行为；CLI 先 verify 再 apply。

## 4. 第一轮自审：授权与数据访问解耦

- P0：把 License claim 当 RBAC scope 会造成商业系统提权。修正为 entitlement 仅供容量/功能判断，授权仍从 Workspace/Device context 恢复。
- P0：热路径每次访问云端会让断网停止服务。修正为本地验签内存决策，网络仅刷新。
- P0：license 包未绑定 Deployment 可被复制。修正为签名 owner、device generation 和激活记录共同绑定。
- P1：浮点容量/费用判断不稳定。修正为整数单位和既有定点金额合同。

## 5. 第二轮自审：宽限、回拨与并发

- P0：过期立即中断存量请求违反验收。修正为先宽限、只限制新增，已接受 lease 有界完成。
- P0：系统时钟回拨可无限延长。修正为 monotonic observation、可信时间 checkpoint 和签名 refresh counter。
- P1：扩容时覆盖 head 与并发 lease 竞争会短暂拒绝。修正为新 entitlement 原子 swap，lease 获取读取单一 immutable snapshot。
- P1：吊销传播与离线宽限冲突。修正为安全吊销使用独立短水位；商业过期可宽限，明确吊销不宽限。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0120 迁移；错误签名/owner/hash/乱序/复制；在线刷新和离线导入；7 天宽限、过期、明确吊销、时钟回拨；并发容量 lease/原子扩容；License 不授予数据访问；三浏览器容量/提醒；2ms/1s SLO；manifest、日志、DB、CLI 敏感扫描和静止终态。
