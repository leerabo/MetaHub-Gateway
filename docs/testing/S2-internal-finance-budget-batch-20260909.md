# 内部账本与预算组织会话批次验收

日期：2026-09-09。对应 PRD-099、144、147。设计与两轮编码前自审见
`../prd/S2-internal-finance-budget-session-batch-design.md`。

## 本地交付事实

- Workspace Cookie 已接入预算读写、预算状态、内部账本、调整及冲正。保留单独旧 Bearer 凭据兼容，不混用凭据域。
- 正式导航为 `internal-finops`，账本与 Provider 成本率为独立并列区域。`model-prices` 不加载账本，废弃的 `finops-ledger` 不恢复。
- 构建通过：271 个可达 JS 模块、289 个生产资产。前后端资产摘要一致：`49b05474cfd62085e2dd68b3626b6a3ee9383c0d1ba4922a22cd21b1256bef42`。
- 本地 Control 于 19:23 重启到 PID 6476；`/health` 返回 ok。未替换 `.data/metahub-dev-prd117.sqlite3`，未修改配置、用户账号、Gateway 或前端进程。
- HTTP 测试组织通过公开组织、使用者、KEY、预算和账本接口完成操作；测试账号的初始会话由本地注册服务建立，不能冒充浏览器注册验收。

## 测试证据

证据目录为 `.artifacts/g6-security-20260907/`：

| 范围 | 结果 | 证据文件 |
| --- | --- | --- |
| SQLite 财务批次 | 13 个独立用例均取得通过结果，分次补跑 | `workspace-finance-sqlite-disk-full-20260909.xml`、`workspace-finance-sqlite-recovery-r1-20260909.xml`、`workspace-finance-sqlite-recovery-r2-20260909.xml` |
| 原生 PostgreSQL | 1 个完整复合流程通过 | `workspace-finance-native-r7-20260909.xml` |
| 共享金额、预留、只读事务与冲正单元 | 72 项通过 | `workspace-finance-shared-final-20260909.xml` |
| 前端受影响回归 | 354 项通过 | `workspace-finance-frontend-final-r2-20260909.xml` |
| 原有 Bearer 预算、精度与冲正流程 | 2 项通过 | `workspace-finance-legacy-20260909.xml` |
| 已部署本地 HTTP | 117 次状态及结果检查通过 | `workspace-finance-local-http-20260909112412.json` |

组织角色矩阵覆盖 Owner、admin、finance_admin 放行及 viewer、developer、security_admin、legal_admin、auditor 拒绝；混入平台或支持 Cookie 返回 400。另覆盖 CSRF、全局会话单独访问、跨快照撤权、提交前撤权、审计失败回滚、预算 block 释放、冲正超额及幂等重放。

HTTP 每个读接口采样 30 次：`/budgets` p95 239.833 ms，`/budget-status` 273.378 ms，`/finops/ledger` 254.797 ms。此为 Windows/SQLite 单机短样本，不证明并发、生产容量或 72 小时 SLO。启动后的部分写入观察到 1.3–2.6 秒，不据读延迟宣称写性能已达标。

独立测试组织的 KEY、使用者和预算已停用，测试会话已登出并验证原会话拒绝访问；内部借记与冲正净额 `0.000000`，无付费模型调用。

## 浏览器与界面

使用真实 renderer/CSS 和合成 DTO 的独立夹具，检查 390、1280、1440 视口的预算、账本、详情及调整表单。手机预算标签实测 14px，页面宽 375px，小于 390px 视口。桌面表单与详情未见重叠或横向溢出，夹具 Console warning/error 为 0。

保存 `workspace-finance-mobile-budget-20260909.png` 和 `workspace-finance-desktop-detail-20260909.png`。夹具会话头部的 undefined 与摘要未提供字段不作为真实产品通过或失败结论。真实 localhost 控制台显示已有浏览器登录失效，未更换用户登录状态；因此未完成真实浏览器认证写入、三浏览器、DPR 与读屏全流程验收。测试视口已 reset，临时标签已关闭，端口 2045 的夹具 PID 11136 已停止。

## 失败与恢复

- 原生 r2/r3 暴露 SQLite 表探测和 PRAGMA；r4/r5 为测试钩子/参数缺失；r6 暴露 PostgreSQL SUM(bigint) 返回 NUMERIC。修正后 r7 通过，保留失败 XML。
- 前端首次最终回归要求旧 PAGE-075 文案，改为稳定标题 ID 后 r2 通过。
- SQLite 首轮 9 通过、4 失败，根因均为磁盘不足，其中 DbError 的 frozen dataclass 又遮盖 traceback。首次补跑 3 通过、1 再遇磁盘不足；最后单项通过。未把任何一次失败 XML 改成绿色。
- 仅删除新生成的 `.tmp/pytest-workspace-finance-jyma7dy0/tests/` 内 9 个已通过用例数据库及 4 个 WAL/SHM 伴随文件，保留基线、失败现场和 XML；这些副本可重建，不是回收站恢复。此前被拒绝删除的旧目录未重试。
- 零空间时 NTFS 压缩失败；腾出空间后仅压缩本轮 jyma7dy0/qw15ysye 目录的测试数据库。基线 SHA256 前后保持 `062AE8E3A8024030EFB712A5120793483203B86D552FCFD86A46E9BF804F4C2A`。

## 仍未完成

- 整个 PRD-074 扩展测试仍有权限数量 64/80 与配置错误码 missing/invalid 两个旧断言待核实，不能称整个文件通过。
- DbError 异常与 contextlib 的兼容问题需独立修复；本批未修改共享异常类。
- 本地平台计量调度器持续返回 `platform_metering.reconciliation_required`。只读定位结果：checkpoint=0，首条序列=1 的 outbox 为 failed/owner_invalid、attempt_count=1；Token 总量一致，组织绑定数量=1，但用量发生时间对应的历史权益快照数量=0。总计 2 条未投影事实（1 failed、1 pending）。旧 `control-usage-20260909-175327.err.log` 已有同样告警，早于本批重启和测试。禁止跳过异常、改账或按当前套餐价格追溯计费；后续需补历史用量对账与受控恢复合同。
- PRD 全范围的支付/应收、争议、容量、Linux、多浏览器、真实 IdP/支付依赖及 72 小时观察未由本次窄批次证明。PRD-099 的预算预留数据库兼容修复已独立提交 `11cc552`；其余提交仍需按先前 S1/S2 依赖整理，不能将混合工作区整体提交。
