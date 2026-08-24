# PRD-113 测试记录

状态：容量预测、压力/浸泡、生产混沌审批与 Kill、性能发布门禁、单位经济性、隔离 runner 和 Platform Console 已完成实现与提交前专项验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0104 -> 0105 -> 0104 -> 0105`、fresh、不可变目录、复合 owner、权限矩阵、事实/active job down guard、完整性与外键 |
| Capacity API / runtime | Platform Cookie/CSRF、closed DTO、CAS、幂等、固定目录与参数上限、PRD-106 checkpoint/effect、forecast、baseline/regression、事件聚合 |
| Chaos safety | ops + security/owner 双角色、禁止自批、执行前动态撤权复核、同步 Kill generation、objective/hard-stop 终态 |
| Isolated runner | 独立 Bearer/permit 域、HMAC 篡改/过期/未来签发、固定 30 秒时钟偏差、code/service/manifest/资源上限、aggregate-only 响应 |
| Deployment / config | 专用最小镜像、无共享 env/DB/Auth/KMS/Provider、无宿主端口/volume、只读、cap drop、internal 网络、资源上限、独立密钥生成与摘要脱敏 |
| Rollout gate | 相同 `gateway/gateway-release/environment` 的 start、P0 最终审批和 resume 阻断；`development -> dev` 映射 |
| Unit economics / roles | 整数 micros 守恒、低基数使用聚合、ops/security/billing 精确权限投影、billing-only 请求裁剪 |
| Console | strict DTO、Cookie/CSRF/CAS/idempotency、动态权限裁剪、导航锁、响应式表格/表单、无浏览器持久存储 |
| Compatibility | PRD-105..112 fresh-schema 当前头 `0105`，以及 Console PRD-109/110/111/112/113 聚焦回归 |

## 提交前结果

| 检查 | 结果 |
| --- | --- |
| PRD-113 Migration | `4 passed` |
| PRD-113 Capacity API / navigation | `6 passed` |
| Isolated runner | `5 passed` |
| Capacity config + deployment Compose/scripts | `15 passed` |
| PRD-107 feature rollout integration | `7 passed` |
| PRD-105..112 fresh current-head compatibility | `8 passed` |
| Console PRD-109..113 | `25 passed` |
| Console lint | `353 files` 通过 |
| PRD-113 Python Ruff | `All checks passed` |
| Python compileall | PRD-113 涉及源码与测试通过 |
| JavaScript syntax | PRD-113 client/controller/renderer/tests 通过 |
| `git diff --check` | 通过；两份 env 示例仅有 Git 行尾转换提示 |
| 新增内容敏感字面量扫描 | `54` 个候选文件；本地已知值和高风险凭据格式均为 `0` 命中 |
| Compose 解析 | 当前主机未安装 Docker CLI，`docker compose config --quiet` 无法执行；服务与隔离合同由部署测试覆盖 |

全数据库、完整集成/流程、真实浏览器、性能与安全回归严格保留到 PRD-113 独立提交后运行，避免把批次门禁混入功能提交前的迭代周期。

## 验证结论

- API 只接受冻结的 scenario/fault code 和结构化上限，不接受 URL、脚本、命令、环境变量、任意 header 或 Secret；runner 再次独立校验 code 对应的 service、manifest hash 和资源边界。
- runner 镜像不包含数据库、Provider、认证、文档处理或迁移依赖；容器只获得内部 token、permit secret 和最小 `PYTHONPATH`，且只连接 Control 专用 internal 网络。
- signed permit 使用固定域分离、最多 1 小时寿命和 30 秒未来时钟偏差；kill permit 最多 60 秒并绑定实验 path，旧 generation 在执行前后都被 fence。
- production chaos 必须有发起 ops 执行票和不同 security/owner 复核票；worker 开始前根据当前角色/权限摘要重验，撤权后失败关闭。
- 压测结果只保存 histogram/count/resource/fairness 和固定 outcome；事件与 Console 不包含 tenant 排名、请求/响应正文、内部 endpoint 或凭据。
- 活跃性能回归只阻断相同 Gateway 场景和环境的 PRD-107 rollout；无 baseline 或低样本不会误报回归。
- 单位经济性使用整数 micros、固定 period/currency/version/watermark，并只从用量账本和成本分摊读取聚合值。
