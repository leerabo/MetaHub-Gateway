# PRD-113 容量、性能、混沌与单位经济性

状态：已实现并完成提交前专项验证（2026-08-25）。依赖 PRD-099/107/109..112，范围以 Excel PRD-113 为准。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /platform/capacity`、`POST /platform/capacity/forecasts`、`POST /platform/load-tests`、
  `POST /platform/chaos-experiments`、`GET /platform/unit-economics`。
- 核心数据对象：`CapacityForecast`、`LoadTestRun`、`ChaosExperiment`、`PerformanceBaseline`、`PlatformCostAllocation`。
- 事件：`capacity.threshold_reached`、`performance.regressed`、`chaos.objective_missed`。
- 性能/SLO：`满足各服务SLO`、`目标峰值2倍压力仍可受控降级`、`容量预测误差可量化`。

## 1. 目标与边界

交付容量模型、租户增长预测、Gateway/Control 基准与压力/浸泡、依赖混沌、成本归集、单位请求/Token 成本、扩容建议
和发布性能回归门禁。测试运行必须使用预定义场景和隔离执行器，不接受任意 URL、脚本、命令、环境变量或 Secret。
生产混沌必须双审批、精确范围、自动停止和独立 Kill Switch。本条不替代真实云账单或财务总账。

## 2. 0105 数据模型

- `performance_baselines`：service/scenario/version、p50/p95/p99、throughput/error/resource、environment class、sample window/hash。
- `capacity_forecasts`：service/region/horizon、demand/capacity/confidence、model version、input aggregate hash、error outcome。
- `load_test_runs`：预定义 scenario、target environment、tenant count、rate/duration、PRD-106 job、状态、stop reason/revision。
- `chaos_experiments`：allowlisted fault、scope manifest、blast radius、steady-state objective、approval/stop/Kill、job 和状态。
- `chaos_approvals`：不同 operator 的 ops + security/owner 审批，执行前重验。
- `platform_cost_allocations`：billing period/service/region、cost category、amount micros/currency、source hash；无客户正文。
- `unit_economics_snapshots`：request/token/cost 聚合、计价/用量水位、allocation version 和 hash。
- 权限 `platform.capacity.read/test/chaos` 与 `platform.unit_economics.read`：ops 测试/混沌，billing 只读成本，owner 全部。
- down 对 baseline/forecast/run/experiment/allocation/economics 或 active job 拒绝。

## 3. 执行与门禁

Load/soak 由 PRD-106 调度到隔离 runner；场景目录随签名制品发布，推荐使用受控 k6 runner，Control 只传场景 code、
签名参数范围和临时最小 scope 凭据引用。runner 网络 allowlist、CPU/内存/并发/时长有硬上限，结果只回传聚合。

Chaos adapter 仅支持固定故障：依赖超时/断连、Redis/KMS/object-store 不可用、单 Cell/region 隔离和受控资源压力。
每个实验持续检查 PRD-109 steady-state SLO、影响 tenant 数、错误率和 kill fence；任一 stop condition 立即停止并写终态。

性能门禁比较同 service/scenario/environment class 的不可变 baseline；置信不足不宣称回归，显著恶化写
`performance.regressed` 并阻断 PRD-107 发布。容量预测只消费低基数聚合，保存预测误差用于校准。

单位经济性从 PRD-099 用量/账单摘要和平台成本分摊计算，使用整数 micros 与固定汇率/期间；不能把 BYOK Secret、
模型输入输出或租户级商业敏感明细投影给其他平台角色。

## 4. API 与事件

- `GET /platform/capacity`：service/region 聚合、headroom、阈值和建议；无 tenant 明细。
- `POST /platform/capacity/forecasts`：选择固定 horizon/model，幂等/CAS，创建计算 job。
- `POST /platform/load-tests`：场景 code、环境 class、受限负载参数；禁止任意目标。
- `POST /platform/chaos-experiments`：fault code、预定义 scope、objective、stop limits 和 approval flow。
- `GET /platform/unit-economics`：billing-only/owner 安全聚合、period/version/watermark。
- 事件 `capacity.threshold_reached|performance.regressed|chaos.objective_missed` 只含服务、场景、聚合 outcome 和时间。

## 5. Console 与批次退出门禁

Platform `capacity` 页面包含 headroom/forecast/baseline、load/soak runs、chaos 审批/kill、单位经济性；billing 只见成本，
security 只见审批所需范围，ops 看不到租户商业明细。所有长任务展示 checkpoint 和安全停止结果。

专项与 S3 批次门禁：100+合成租户噪声邻居、目标峰值 2 倍受控降级、72 小时长稳无连接/内存/句柄泄漏、DB/Redis/
KMS/object/region 混沌、性能回归阻断、预测误差、单位成本守恒、浏览器三档和静止终态。证据只保留聚合指标。

## 6. 第一轮自审：执行器与生产安全

- P0：API 接收任意 URL/脚本会成为 SSRF/RCE。修正为签名场景/fault 目录和结构化参数上限。
- P0：生产 chaos 无 kill/stop 会扩大事故。修正为同步 kill fence、steady-state 连续监测和硬超时。
- P0：Control 直接压测自身会形成资源自杀。修正为隔离 runner、最小凭据引用和资源/网络舱壁。
- P1：runner 回传原始响应会泄露正文。修正为仅聚合 histogram/count/resource 和固定错误类别。
- P1：不同环境 baseline 直接比较会误报。修正为 environment class、版本和采样置信约束。

## 7. 第二轮自审：噪声邻居、成本与证据

- P0：100 租户测试若复用 owner/key 会掩盖隔离问题。修正为独立合成 owner、Key、配额和可验证公平分布。
- P1：容量聚合返回 tenant 排名会泄漏商业数据。修正为 service/region 聚合与最小匿名 cohort。
- P1：单位成本使用 float 会产生财务漂移。修正为整数 micros、固定 currency/period 和守恒断言。
- P1：72 小时只看请求成功无法证明无泄漏。修正为句柄、连接、内存、队列水位和恢复终态联合门禁。
- P1：性能回归事件自动 kill 全部发布过度耦合。修正为阻断相同服务/场景的 PRD-107 rollout，人工确认后再扩大。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：capacity/chaos 与 unit-economics 使用角色裁剪投影；runner 只接受签名目录 code 和有界参数，
  不接受 URL、脚本、命令、环境变量、Secret 或租户商业明细。
- 第二轮（迁移/依赖/失败策略）：0105 对 baseline/forecast/run/experiment/allocation/economics/active job 执行 down guard；生产
  chaos 需要双角色审批、同步 kill、连续 steady-state 与硬停止条件；成本使用整数 micros，证据只保存低基数聚合。

## 9. 实现与专项验证

- 数据与运行时：`0105_capacity_performance_chaos_economics.sql` / `.down.sql`、Capacity API、PRD-106 `capacity.run` handler、
  固定 scenario/fault engine、签名 permit、kill generation fence、聚合结果和 PRD-107 相同环境性能阻断已交付。
- 隔离部署：runner 使用独立最小镜像，不加载共享 env、不挂载数据卷、不发布宿主端口，只与 Control 共享 internal 网络；
  两把 32-512 byte 凭据独立生成、全域防复用，并在运行配置摘要中只显示 `masked`。
- 权限与 Console：owner、ops、security、billing 使用数据库权限扩展表投影；billing 只请求单位经济性，security 只见容量与
  chaos 审批/kill，ops 可创建 forecast/load/chaos，动态撤权后导航与操作同时消失。
- 专项结果：Migration `4 passed`，Capacity API/导航 `6 passed`，Runner `5 passed`，配置与部署 `15 passed`，PRD-107
  rollout `7 passed`，历史当前头兼容 `8 passed`，Console PRD-109..113 `25 passed`，Console lint `353 files`；完整命令、
  边界和未执行项见 `docs/testing/PRD-113-tests.md`。
