# G7 基础设施补充验证

日期：2026-09-07

本记录覆盖已经完成的 G7 补充修复与复验。G7 专项结论见 S7-G7 报告；G6 的 72 小时缺口仍阻止进入 PRD-148。

## 原生业务复验

- PostgreSQL 17.11 最终业务合同 20/20 通过，覆盖重复 seed、独占调度租约/续租/fencing/接管、
  参数中的百分号、JSON 与 SQLite 逐字节对照、复合外键 NULL/延期检查、失效约束拒绝和十项 PRD-147 业务。
- 修正 schema/seed/platform permission/edge permission/worker 对 SQLite catalog、PRAGMA 和 INSERT OR IGNORE
  的直接依赖。间接 owner 表通过父事件/版本表筛选，并先验证列存在，避免 SQLite 把不存在的双引号列当成常量。
  四张表的专项分别验证本 Workspace 纳入、其他 Workspace 排除、事实变更及缺失 owner 列失败关闭。
- generator revision 4 保持 1074 表、10241 CHECK、2019 FK、2486 trigger、4225 seed rows。
  `metahub_json` 先由 PostgreSQL JSON parser 验证，再仅去除字符串外空白，保留键顺序、重复键、转义和数值字面量；
  不再用会改变字节合同的 jsonb 序列化。新版原生迁移/事务边界 22/22 通过。
  从既有 canonical source 和重新执行全部迁移生成的两个副本摘要相同，均为
  `11892ef4760a18c9adacb7745e8e01b173e651adf076fb44e18054b7cb934fe0`（7857002 字节）。
  2486 项结构探针复验通过，状态和对象统计封存在 `postgresql-native-r4.json`。
- Windows 最新迁移业务 14/14、数据库适配 25/25、scheduler 8/8；历史迁移最终 58/58 通过。
  原生 ASGI 场景沿用原测试的鉴权替身，不作为真实浏览器登录证据。

## Linux 最终复验状态

- Console 最终全量 1286/1286，聚焦资产合同 10/10，lint 763 文件，production build 272 assets。
- 主 Python 最终为 26 passed/3 failed，不能使用中间进度推断为 29 passed。三个 outbox 状态机用例
  依赖 2 秒退避和 30/60 秒租约内完成测试步骤，仿真环境耗时使断言条件过期；现改为可控时钟，仍验证
  精确退避边界、过期拒绝、已投递停止和外层撤权。
- manifest 复验为 7 passed/1 failed：双副本公平性错误地在一方退出、另一方可能接管后断言两个 owner，
  已把该数据库断言移至两副本都运行时。Windows 8/8 和 Linux 受影响 47/47 均已通过。
- SQLite table discovery 后续改用 `pragma_table_list`，保留精确大小写、main schema、实体/virtual/shadow 表语义。
  Windows schema/间接 owner 12/12，IAM 3/3 通过，未放宽 session 50ms、list 300ms p95 限制。
  Linux 独立 schema 增量 12/12 通过，覆盖两个最终改动文件，不把之前 47 项运行冒充为最终 helper 的测试。
- `linux-arm64.json` v3 同时保留原运行与 schema 增量 provenance，校验最终 24 份源码的规范化字节与 278 份 SQLite 迁移摘要一致。保留原主运行
  的两项 DB 和两项退休路由成功结果，三项历史 outbox 失败由最终业务复验替代，不虚写原运行全绿。

## 最终入口日志变更

- r3 外部真实 N/N-1/回滚共 138 项路由检查通过。之后仅关闭可能写入 URI 的 Nginx error log，
  Windows/Linux 实际 Nginx 日志专项各 2/2 通过，覆盖上游断开返回 502 时 URI 标记不出现在 stderr。
- `ingress-log-only-delta.json` 重建 r3 配置并验证哈希一致、路由策略字节未变，因此仅复用 r3 路由证据。
  r4/r5 外部访问超时、r6 隧道退出均保留；`final_config_new_external_run_passed=false`，不声称最终
  完整配置的新外部演练已通过。

## 复核一：数据库与证据

- Linux SQLite URL `sqlite:////...` 必须保持绝对路径；实文件回归同时保留 Windows drive 与相对路径行为。
- PostgreSQL baseline 从 0001..0139 的 SQLite 迁移结果生成，固定时间与执行耗时使输出可复现。
  校验对象数、约束、触发器状态、源码摘要和历史登记；部分 bootstrap 失败关闭，不自动覆盖已有业务库。
- 后续 native delta 由 PostgreSQL 语法解析器识别顶层事务语句，拒绝 COMMIT、ROLLBACK、SAVEPOINT、
  两阶段事务和它们的大小写/注释变体；字符串和函数体不被正则误改。
- 失败登记重新获取迁移锁，仅更新失败状态，不能把已经成功的并发重试改回 failed。
- 2,486 项触发器探针证明生成体可编译和受测分支可执行，不等价于全部业务语义与 SQLite 完全等价。
- 正式证据写 `.artifacts`。Linux 工作目录改为持久磁盘，逐项记录 setup/call/teardown 结果，不能通过
  终端的点号数量推断通过数。环境安装失败与测试失败保留，不能计作成功。

## 复核二：部署与版本回滚

- 临时代理的成功不能代替正式部署验收。新增独立 Nginx 配置和镜像，由固定退休清单生成 URI 规则。
- 移除 Control 后端宿主端口发布；入口 tag 不跟随应用 tag 回滚。回滚仍须保留新的 Compose 入口边界。
- Nginx 对 URI 解码与规范化后匹配规则，所有方法和尾斜杠变体均退休；保留公开品牌与新分域 API。
- 所有退休请求进入后端普通未知路由，保持标准错误合同，404 强制 no-store，不添加 tombstone 响应。
- N-1 的 API 文档也可能泄露退休接口，因此公开入口禁用文档路径，内部 OpenAPI 保留给部署验证。
- 只读非 root 容器显式指定各模块临时目录，入口不接收业务凭据、不持久化请求日志。

## 前期验证记录

- `postgresql-native-final-junit.xml`：真实 PostgreSQL 17.11，22 项通过，含 fresh/repeat、native delta
  apply/down/reapply、失败原子性、迟到失败记录保护、manifest drift 与事务语法边界。
- `production-ingress-final-r3-junit.xml`：11 项通过，含实际 Nginx 1.28.0 和部署脚本合同。
- `final-contracts-junit.xml`：28 项通过，含更新后的入口文档退休规则、连接生命周期、基础设施适配与
  scheduler/生产资产摘要合同。

- `external-ingress-native-r3.json`：实际 Nginx 配置经 Cloudflare 临时 HTTPS 入口访问真实 N/N-1 应用，
  N-1、N、回滚 N-1 共 138 项退休路由检查全部通过，失败 0。公开品牌与 health 保留，API 文档保持
  不公开；N-1 仍注册 25 项、N 注册 0 项退休接口。旧源码 `app.py` 的 Git blob 与
  `93862d1b9d393965ae5bc1817a42c48b3149e599` 对应文件一致。两轮失败结果分别保留首次 OpenAPI 读取超时
  和隧道连接失败，不由成功重跑覆盖。验收后隧道、代理和独立应用进程全部停止。
- Linux 首轮 Console：1,283 通过、1 失败。loopback 网卡关闭导致真实监听用例 `EADDRNOTAVAIL`；
  启用网卡后重新运行完整前端套件。首轮日志保留为 `linux/linux-console-test-first.log`。
- Linux 前期进度只记录到 15 项 call；最终主运行与修正复验结论以本记录上方“Linux 最终复验状态”为准。

证据目录：`.artifacts/s7-g7/20260905-030323/`。平台 IAM 回归与补充敏感审计均已完成；原始失败不删除或改记。
