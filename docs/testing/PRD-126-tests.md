# PRD-126 测试记录

状态：已完成。Fleet 健康投影、稳定平台索引、Workspace/Platform 双域视图、客户授权的受控远程诊断、无 shell Agent 执行器、Console 和 0118 已交付；本条保留独立设计、测试记录、敏感审计和提交。下一权威项为 `PRD-127 - Fleet分批升级、回滚与维护窗口`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0118 Migration | `0117 -> 0118 -> 0117 -> 0118`、权限目录与角色矩阵、复合 owner、不可变快照/事件、状态 CHECK、tenant deletion manifest、业务事实/down guard、完整性与外键 |
| Health/Fleet | PRD-120..125 事实投影、确定性分数和 fixed signals、generation 单调、相同事实去重、Platform 字段裁剪、筛选、签名 keyset cursor、1 万节点稳定遍历与逐页 SLO |
| Diagnostic Control | Platform 申请、客户批准/撤销、全局 step-up、非自批、TTL/max runs/authority revision/参数 hash、幂等重放、lease/start/result、取消/超时/重启恢复、审计/outbox |
| Shared/Gateway | Ed25519 用途分域签名、exact invocation/result、字段递归拒绝、固定四 handler、无 shell/subprocess、取消/deadline 零自由输出 |
| Console | Workspace/Platform Cookie/CSRF/DTO/store 隔离、权限裁剪、健康最多四并发、keyset 前后页、固定参数控件、owner generation、TOTP/恢复码/WebAuthn/step-up 短生命周期、DOM/URL/Storage/log 零持久化 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| Python PRD-126 聚焦集 | `15/15 passed`；0118、Control API、Shared SDK、Gateway executor |
| 1 万节点 Fleet | `10,000/10,000` ID 无重复且顺序稳定；所有页面满足测试内 `p95 < 1s` 断言 |
| PRD-126 Console | `8/8 passed` |
| PRD-120..126 Console 必要回归 | `37/37 passed` |
| PRD-120..125 Python 相邻回归 | `151/151 passed`；Shared/Gateway `111`、Control migration/unit `27`、Control API `13` 全量补跑 |
| Python static | PRD-126 及相邻源码/测试 `26` 个文件 Ruff 全规则通过；规划脚本忽略存量 E501 后通过；`py_compile` 通过 |
| Console lint | `console lint ok (380 files)` |
| Roadmap | generator 自校验通过；validator tests `4/4`、CLI `164/164` 通过，Excel/ZIP 完整且下一项 `PRD-127` |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮按投影与授权边界复核。修正 Workspace 只有 `edge.health.read` 时仍返回诊断 grant/result 的越权面，改为只有同时具备 `edge.diagnostic.read` 才查询并返回；新增真实授权回归。Platform Fleet 始终从独立字段裁剪索引读取，DTO 不含 Workspace/tenant/display name，cursor 绑定筛选并签名。注册、证书、Desired State、遥测和验证写事务只在 0118 表存在时刷新投影，保持旧迁移基线可执行。
- 第二轮按命令与敏感生命周期复核。Shared/Gateway 禁止任意 shell、路径、自由参数、stdout/stderr 和未知结果字段；grant 撤销、TTL、次数、authority drift、lease 超时、Agent 重启和迟到结果均失败关闭或保留审计。Console 在每个 WebAuthn await 后复核 owner ticket，并补上依赖缺失时在进入 loading 前失败，避免页面永久忙碌。无未关闭 P0/P1。

## API、性能与恢复证据

- 0118 从 0117 基线回填健康快照和 Fleet 索引，验证权限矩阵防漂移、不可变事实、业务 down guard、`integrity_check=ok` 与外键违规 0。旧 PRD 的 current-head 测试已随 0118 更新，保留各自截断 migration fixture 的历史版本断言。
- API 闭环覆盖稳定分页和 cursor 篡改/跨筛选拒绝；Workspace 同形 404 与健康/诊断分权；Platform 申请、客户批准、运行、Agent lease/start/report、结果回读和幂等重放。撤销/权限 revision 漂移会取消已授权命令并下发取消水位。
- 1 万节点测试逐页读取完整 Fleet，确认无重无漏、顺序稳定，并对每页耗时计算 p95 后执行 `< 1s` 断言。健康变化的真实部署传播 `< 2 分钟` 留到批次末环境门禁，不用进程内投影证据替代。
- lease 到期后新 EdgeSession 可恢复授权命令；deadline 到期进入静态终态，迟到 report 只审计不覆盖结果。Gateway deadline/cancellation 返回固定状态与原因，result 为空且不启动子进程。

## 敏感审计与静止终态

- 审计扫描本条及相邻变更 `55` 个文件、Excel 的 `20` 个 ZIP 条目，并对本地 `.env` 中 `3` 个实际敏感值逐值比对；已知值、私钥标记和生产 Token 模式命中均为 `0`。命令和结果递归拒绝 credential/Secret/Token/path/URL/body/stdout/stderr/traceback 等字段，测试 canary 只用于证明拒绝，不是可用凭据。
- Platform/Workspace 的 verification code、WebAuthn credential 和 step-up grant 不进入 state/store、DOM、URL、Storage 或日志；API 只在当前请求 header 中使用，`finally` 清空局部引用。平台和租户的 Cookie/CSRF、request client、DTO 和缓存键保持分离。
- `.tmp`、`.pytest-prd126-*`、`.pytest_cache`、根目录 `__pycache__`、生成数据库和 Excel 锁文件均为可复现产物，提交前直接删除；未跟踪 PRD-101..105 文档保持不动。

## 批次门禁

本条只运行受影响单元/API 冒烟、必要相邻回归和进程内 1 万节点 SLO。G5 的 0111..0122 fresh/逐级/down guard、真实 Linux/代理/断网、N/N-1、Chromium/Edge/Firefox 三缩放、健康传播、长稳、全介质敏感扫描、性能、安全和完整静止终态保留到 PRD-130 独立提交后统一执行，不以本条 Windows/SQLite/进程内证据替代。
