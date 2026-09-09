# 内部账本与预算组织会话批次

日期：2026-09-09。状态：设计、两轮自审及连续编码完成，批次验收与本地交付中。依据原 Excel PRD-099、144、147，以及存量 PRD-023/051 合同。此批不是平台支付、退款或订阅应收。

## 当前缺口与范围

编码前，`finops/routes.py` 与 `budgets/routes.py` 调用旧 Bearer guard，前端相应 client 直接拼 Bearer，预算及调整提交以 token 是否存在为准。新 Workspace Cookie 会话不能完成已显示的内部账本、人工调整和预算配置。本批已迁接这些入口。

一起迁接 GET /finops/ledger、POST /finops/adjustments、GET/POST/PATCH /budgets、PATCH /budgets/{id}/mode 和 GET /budget-status。沿用现有表、金额精度、账本不可变事实、规则范围、block 释放、幂等键、错误码与安全 DTO，不新增迁移。

## 共享合同

- 路由使用现有 Workspace-or-legacy 单凭据 guard，逐请求检查 billing.finops.read/write、billing.budget.read/write。Workspace 写入要求现有 Origin/CSRF；不按 Owner 字符串补权，不开放 Platform/Support/机器 KEY。
- 同步数据库业务在工作线程运行，严格正文和参数解析保持。GET 使用同步 endpoint，需 await 正文的写接口仅将已解析后的业务调用交给现有线程池。
- 公共服务在自己的读/写事务内使用 ResourceWriteTransaction，先恢复当前会话/成员/tenant/权限；返回前结束旧读快照并在新快照复核；写入和审计提交前复核。预算冲突恢复也必须重新授权，不能把旧会话用于恢复查询。
- 内部 `write_finops_adjustment_in_transaction` 继续由调用者事务持有，不能强行套浏览器会话从而破坏现有受控商业/争议内部调用；只迁移公共服务入口。
- 调整按原 tenant + idempotency key 与 payload hash 重放，不改变现有幂等所有权合同。新会话仍必须拥有写权限；冲正额度、原币及关联分录规则保持。重放无新审计/事件。
- 预算的 create-only/兼容 PATCH upsert/模式切换保持，scope 从服务端 tenant/Key/项目/应用恢复。退出或撤权不得提交规则或释放 block。金额仍为十进制字符串与整数微元，不通过 float 计算。
- `/budgets`、子路径及 `/budget-status` 的所有成功/异常响应禁止缓存；FinOps 沿既有共享 no-store 边界。
- 前端复用 authenticatedResourceFetch、resourcePageLifecycle。沿用预算 mutation 与 FinOps 草稿状态机，补 Workspace 身份、AbortSignal、CSRF、离页/切空间取消及迟到结果丢弃。写入失败保留同身份重试信息；离页/换身份清除 reason、幂等键、游标和草稿。
- 账本只属于组织内部成本，不将其调整写成平台应收或订阅余额。平台全局注册开关不在此批范围。

## 第一轮自审：权限与账务

1. 仅修正 fetch cookie 不足，路由和事务仍可能使用旧 Bearer 授权。决定同时迁接读取与写入，事务内重新鉴权。
2. FinOps 内部事务 helper 被其他受控领域复用，不能在该 helper 中要求浏览器凭据。公共入口负责 session guard；内部合同保留。
3. 预算超额阻断不能在撤权或审计失败时被释放，规则、block 与审计仍共用原事务，不拆成独立成功动作。
4. Owner 和 finance 的权限快照才是权威，不依据旧注释的 admin 名称扩权。保留金额/范围/跨租户/幂等规则，不将内部费用混进商业账。

本轮已将 P0/P1 风险写入以上实现合同，需测试验证。

## 第二轮自审：竞态、恢复与前端

1. 只在旧读快照内重复鉴权看不到并发撤权；读取完毕关闭旧快照，再复核后释放数据。
2. 预算唯一约束异常后做恢复查询可能继续使用旧授权；恢复前另起受控读事务，失败不能泄露其他空间规则存在性。
3. 同账号切换组织时 token 可能同为空，旧代码的 token 相等检查无效；前端必须按资源 owner 代次绑定读取与写入。
4. FinOps reason 与金额草稿不能随晚到失败恢复到新身份。现有幂等重试只在同一 owner 生命周期内保留，成功清除正文。
5. 同步数据库阻塞事件循环会使表面鉴权修复仍不可用；GET 与正文解析后的写服务统一进工作线程。

编码前两轮自审完成；实现与验证事实以下述批次记录为准，不以设计关闭代替运行验收。

## 测试与发布

### 编码中发现的正式入口修订（两轮自审）

权威导航 `workspace_navigation/catalog.py` 将 `finops-ledger` 标为 replaced，正式 route
`workspace.finance.internal-finops` 对应 `internal-finops`。新页面仅展示成本率，旧账本链接被
可见导航白名单过滤。不能通过测试夹具伪造旧 route，也不能恢复已废弃入口。

第一轮自审：账本在现有 `internal-finops` 页面接入，仍要求 `billing.finops.read/write`；
成本率继续由现有 PRD-144 控制器和独立权限管理。账本和成本率作为同页的并列区域，不能
将账本塞进会被成本率异步刷新替换的 DOM 根节点，避免丢失财务草稿和事件绑定。

第二轮自审：资源生命周期及账本清理统一绑定 `internal-finops` 的后端 route；
正式页面分发测试必须使用真实注册表。成本率渲染回调不得覆盖账本区域。`model-prices`
继续只展示成本率，不后台预取账本。旧 `finops-ledger` 不增加路由注册、权限或可见导航。
此修订需新的页面分发、权限和旧 DOM 行为测试，不沿用仅测试旧入口的通过结论。

连续完成两域后，先定向单元/API 冒烟，批次统一 SQLite/原生 PostgreSQL、实际组织 HTTP、权限负矩阵、撤权与审计失败、三视口/真实浏览器、必要兼容和性能回归。数据库基线尽量复用，避免每个小改动整套迁移。按 PRD 保留独立测试、敏感审计和提交；只有整条链路通过后更新本地服务，不让用户使用半迁接的页面。

### 原生数据库预算汇总修订（两轮自审）

原生验收发现预算状态查询依赖 SQLite 隐式 `rowid`，PostgreSQL 无该列。
普通预留、批任务 hold 和 allocation 三处改按现有主键稳定排序，不新增迁移。

第一轮自审：这些金额均先校验为非负整数微元，再累计并检查上限。主键排序不改变
总额、终态结算、租户/规则/日期筛选或上限拒绝语义，不使用浮点或数据库 SUM 替代。

第二轮自审：轻量测试表补齐真实主键并使用 WITHOUT ROWID，防止仅在 SQLite 隐式列
上通过。覆盖 hold 剩余额度、active/reconciliation allocation、排除终态与异域记录，
以及六位小数。真实 PostgreSQL 必须再执行完整组织预算与调整流程。

第二次原生执行继续暴露本地 `_table_exists` 直接查询 `sqlite_schema`。改为现有
`metahub_shared.db.schema.table_exists`，沿调用者事务探测真实表，不增加通用 SQL
字符串改写。旧版本无批任务表仍兼容，有表但查询失败仍拒绝预算读取。
单个原生验收只建立一个完整迁移且已播种的临时数据库，不再同时保留模板与副本。

第三次原生执行发现预算读取将 `PRAGMA query_only` 发给 PostgreSQL。按现有
feedback 存储做法分支使用 `BEGIN` 和 `SET TRANSACTION READ ONLY`，SQLite 保留
query_only，连接池仍负责复原。原生测试在实际列表读取期间检查只读设置为 on，
单元测试同时验证 SQLite 拒绝写入。未放宽认证或把未知 PRAGMA 静默忽略。

### 冲正合同补充（两轮自审）

原生测试在冲正时发现 PostgreSQL `SUM(bigint)` 返回 NUMERIC，经适配器成为字符串，
而金额事实严格要求整数。与现有账本汇总一致，改为逐行校验 integer 微元后累计，
保留同 tenant、目标分录、reversal 类型过滤和整数上限。禁止放宽全部整数校验或转 float。

第一轮自审：无冲正时为 0，多条冲正精确相加，坏行、负值和溢出仍失败关闭；
新冲正不得超过原金额减既有冲正总额，重复幂等提交不新增事实。

第二轮自审：浏览器发现冲正对话框沿用筛选选项，错误显示“全部”。命令表单不提供
“全部”，冲正模式只显示不可编辑的“冲正”；目标仍来自页面状态，不接受隐藏字段提权。
补渲染断言、金额/溢出单元测试和双数据库冲正链路，不改平台应收或订阅余额。
