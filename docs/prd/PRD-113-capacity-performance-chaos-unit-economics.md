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

## 10. 2026-09-10 结果真实性修订

重新核对后发现，上一节的 HTTP runner 仍调用模拟器，不能证明真实负载或混沌执行。
编码前批次设计与两轮自审见 `S3-execution-evidence-correction-20260910.md`。
HTTP 路径不再返回模拟成功，未接通真实执行器时返回固定 `runner.executor_unavailable`。
模拟器仅保留于显式 test 环境的 test_loopback 路径，结果带 simulation 标识。

v2 结果绑定 operation、object、场景 manifest 与稳定参数摘要，并校验完整结果摘要、
百分位顺序、非空采样和固定 outcome。短期 permit 续期不改变执行参数绑定。
Control 拒绝 HTTP 模拟结果、旧格式、跨执行结果及篡改结果；拒绝结果不能触发基线完成。
HTTP 禁用环境代理和重定向，停止回执须绑定目标及不小于请求的 kill generation。
接收回执不等于已证明外部负载停止，持久执行与停止/恢复证明仍需后续实现。

专项记录见 `docs/testing/PRD-113-result-integrity-20260910.md`。本修订是防止错误验收，
不代表真实压测、100+租户公平性、故障恢复或 72 小时长稳已交付。

## 11. 2026-09-10 持久执行与质量门禁

执行器新增独立状态库、幂等提交、签名查询/取消、排队授权到期和重启中断处理。
使用实际 HTTP 请求及独立目标进程资源采样生成聚合，不保存正文；取消先于提交也会留下持久停止事实。
可选部署叠加配置使用 runner 专属状态卷，不共享 Control 数据，保留只读容器及内部网络。

Control 客户端已具备单次异步协议，根任务的持久等待、取消联动和恢复仍未接入，
不能将当前同步 handler 与真实执行配置组合发布。基线增加 SLO、成功吞吐、错误率、公平性和
受控降级质量门禁，坏测量不会建立健康基线或清除旧发布阻断。

独立设计和两轮自审见 `S3-measured-runner-execution-20260910.md`。
专项记录见 `docs/testing/PRD-113-durable-execution-20260910.md`，敏感审计单独保存。
121 项相关测试通过，不等同于真实 Gateway 多租户或 72 小时验收通过。

## 12. 2026-09-10 原任务外部等待接入

按 `S3-external-wait-runtime-20260910.md` 的补充设计和两轮编码前自审实施。
真实 load/chaos 由原 capacity.run 原子登记领域 start 和加密续接 claims 后释放普通租约。
恢复者提交、查询或取消同一执行，保留原 attempt、generation 和领域 job_id。
未知提交结果保留原 permit，只有 status/cancel 续签时间；过期未确认提交先持久取消。
pending 不消耗普通重试，终态结果经外部恢复 fence 与领域 owner 核对后原子提交。

混沌 kill 改为持久取消意图：正在执行的领域保持非终态，前端显示停止待确认，
不再依赖请求返回前一次性的 HTTP kill。只有 runner 终态才能结束原任务及领域记录。
SQLite 0152 / PostgreSQL 0159 把原任务的取消和失败投影到尚未结束的容量领域记录，
覆盖 handler 尚未执行、启动事务回滚和 lease 失效场景。已有业务事实时拒绝 down。
根任务等待期间的终态 SQL 保护仍由 PRD-106 0151 / 0158 执行。

旧同步适配器只允许显式 test_loopback，不再发起真实 HTTP 执行。
专项测试、未执行门禁与发布边界见 `docs/testing/PRD-113-external-runtime-20260910.md`。
当前只更新源代码，不升级正在试用的 schema 0150，不宣称整批或长稳验收完成。

## 13. 2026-09-10 独立短租约故障代理

按 `S3-fault-lease-proxy-20260910.md` 的批次补充设计和两轮编码前自审实施。
独立 TCP 代理对部署侧固定的隔离依赖执行真实超时或断连，签名短租约最长 30 秒。
持久记录执行摘要、单调命令序号及撤销墓碑；取消早于注入、重放、并发抢占和过期续租均受围栏保护。
每次转发检查故障，租约失效不依赖负载执行器在线；进程退出关闭连接，重启不恢复旧故障。
代理解除故障不是业务恢复证明，资源压力适配器和真实 chaos 的 runner 续租/恢复探测仍未接通。
HTTP runner 继续拒绝未接通的真实 chaos，不输出模拟成功。
专项测试与敏感审计见 `docs/testing/PRD-113-fault-proxy-20260910.md` 及对应敏感审计记录。
