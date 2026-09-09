# S1 组织与使用者批次进展

## 最终合并回归

`.tmp/pytest-s1-consumer-regression-mi28dr3a/results.xml`：34 passed，0 failures，0 errors，0 skipped，218.962 秒。运行在独立基线副本上，未改动本地试用库和保护测试库。

| 范围 | 项数 | 证据边界 |
| --- | ---: | --- |
| PRD-092 注册与验证 | 11 | 包含邀请、重放、渠道失败、敏感字段、限流及原性能门槛 |
| PRD-093 Workspace | 5 | 创建、幂等、隔离、CAS、无占位资源及 warm p95<1s |
| PRD-094 Membership | 10 | 角色矩阵、切换、邀请、撤权、Owner 转移、100 并发及切换性能 |
| PRD-094 免登录使用者 | 6 | 使用者/KEY、权限、轮换并发、停用、事务失败与迁移保留 |
| Workspace 连接生命周期 | 2 | 独立 lease、复用、失败回滚和结束失效 |

前端相关 51 项、原生 PostgreSQL 消费者 2 项及桌面/移动端检查证据详见 `PRD-094-consumers-tests-20260909.md`。浏览器实际提交注册和完整兼容工具模型调用不在此次通过证明内。

## 已提交修复

- `be748a1`：PRD-092 过期邀请夹具与性能客户端网段修复，独立设计/两轮自审/测试/敏感审计。
- `2636e2c`：PRD-093 Workspace 操作级连接复用，独立设计/两轮自审/测试/敏感审计。
- 注册验证设置、组织本地开通、使用者与 KEY 的其它增量仍在工作树，未完成依赖顺序的独立暂存和提交。不能把上述两个修复提交称作整个功能批次已提交。

## 当时运行快照（非当前服务清单）

本地 SQLite 0148；Control PID 18888、Gateway PID 6956、Frontend 5173 PID 336、Status 8002 PID 6892。Control/Gateway 健康检查 200，未登录访问消费者和 KEY 管理为预期 401。

消费者实际 HTTP 发放/轮换/停用通过，创建者 Owner 不获得平台权限，员工不新增登录身份。测试 KEY/使用者均已停用，测试会话退出，用户管理员会话未被替换。付费模型调用为 0。

## 当时定位的后续缺口

`console-web/src/app/main.js:loadUsageState` 仍以 `authSession.token` 调用 `console-web/src/lib/usage.js`，`control-plane-api/src/metahub_control/usage/routes.py` 使用旧 `require_usage_reader/require_usage_exporter`。组织会话不能按旧 Bearer 调用；401 分支还会把页面送回旧登录视图。

继续实现时需把用量读取、导出和异步轮询一起适配 Workspace 的逐次授权、CSRF、空间切换和迟到响应规则；沿不可变 KEY 关联增加使用者筛选与分币种聚合。企业内部 FinOps 页面目前主要是 Provider 成本率与链接，不应把员工费用误接到 SaaS 订阅扣费。尚未编码这些下一阶段变更。

完整 Excel 目标保持未完成：SMTP、费用与结算流程、剩余业务域的真实验收、未完成提交和 72 小时任务均不能由本报告推定完成。

## 真实浏览器补充验收

2026-09-09 13:10 UTC 起的隔离运行 `.artifacts/s1-browser-6y8nim3f/acceptance.json` 已通过。桌面 1280x900 和手机 390x844 均从注册按钮提交开始，完成创建组织、两位免登录使用者、两把独立 KEY、轮换、停用。10 个阶段通过，0 页面异常、0 外部 Origin 请求，六次布局采样均无页面横向溢出、文字输入最小 16px。已查看桌面及手机终态截图。

数据库增量仅有 2 GlobalAccount、2 legacy User、2 Membership，均属于两位测试组织 Owner；4 位使用者和 4 条 KEY 关联没有新增登录身份。未产生邮件验证证明或平台管理员。Gateway 的有效 KEY 返回 422（无效 feedback 正文），旧 KEY 或停用员工为 401；这不是成功模型推理证据。

入口修复与字号修复分别归 PRD-147/094 的独立设计、测试、敏感审计记录。浏览器由真实服务提供当前源码，不替换 fetch/API，也不复用用户 Cookie；worker lifespan 关闭，成功数据库副本已删除，服务和浏览器已退出。上方旧运行快照和“浏览器提交未验证”仅描述当时状态，以本节新增证据为准。

上方用量/财务缺口后续增量已进入 S2 工作树，最新证据见 `S2-consumer-usage-accounting-batch-20260909.md` 与 `S2-internal-finance-budget-batch-20260909.md`；不能据此推断相关独立提交、完整模型调用、跨平台/多浏览器和长稳验收已完成。

## 2026-09-10 补充证据

- 原生 PostgreSQL 使用者验证 `.artifacts/s1-index-native-547deff2f27d4b3593683345d6c1f2ee/results.xml`：2 项通过，覆盖创建、轮换、停用、归属不可变及迁移保护。记录提交 `9c593a8`。隔离集群已正常关闭，现存库清单保留。
- 原注册性能门槛 `.artifacts/s1-index-batch-1_q9glcz/results.xml`：1 项通过；注册/验证 p95 小于 500ms，滥用计数小于 30ms，未放宽阈值。
- 动画响应性修复 `c3baa35` 后的独立构建浏览器 `.artifacts/s1-browser-jp9dqofw/acceptance.json` 通过桌面和手机十个阶段，验证 289 个生产资产及真实动态画布。详见 `PRD-092-access-animation-tests-20260910.md` 及独立敏感审计。
- 当前本地聚合发布摘要为 `6b4ceb1d98733178c0676aaf1492b057534ce16886241bc5f3ae7bc79bf30ebe`。完整目标继续开放，以上补充不覆盖未完成业务域。
