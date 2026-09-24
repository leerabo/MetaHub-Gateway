# PRD-148 PostgreSQL 复盘回滚与本地运行核查

日期：2026-09-12。Excel 只读；PRD-148 与 G8 未标记完成。编码前两轮自审见 `docs/prd/PRD-148-saas-ga-launch.md` 的 PostgreSQL 复盘迁移补充自审。

## 修复与验证范围

0163 down 原来使用同名 `CREATE UNIQUE INDEX IF NOT EXISTS`，留下原有 `(ga_launch_id, conclusion)` 索引，没有恢复旧版单发布唯一约束。现改为先创建单发布唯一约束，通过后再删除 0163 索引和授权对象，所有操作由迁移运行器持有同一事务。有双条复盘时整个 down 被拒绝。0163 复盘角色授权补齐插入、更新、删除不可变触发器，与 SQLite 0155 一致。0162 down 去掉显式事务边界，以适配统一回滚入口。

验收脚本同时检查 0162、0163 的实际版本登记，不再直接执行 SQL 后假定版本状态正确。报告绑定开始与结束源码摘要；中途源码变化会使验收失败。测试使用一次性 loopback PostgreSQL 集群、合成租户及复盘，不修改试用数据库，也不产生真实会签或发布事实。

## 当前证据

- `outputs/prd148-native-rvk9v1o2/report.json`：11 项检查通过，127.72 秒，集群已停止。包含完整迁移与幂等重放、20 条门禁保真、两个迁移各自回滚、命令事实保护、单条复盘往返保真、双条复盘回滚失败后的数据/权限/历史保真、复盘及权限不可变。起止源码摘要同为 `863889e1b198cf8f488b7af5857ed6972dc78cc073abfc4fce760bdd5f77f22b`。
- `outputs/prd148-frontend-recovery-20260912.xml`：前端全量 2138/2138，通过，failure/error/skip 均为 0。专项 MCP 12/12 通过。
- Console lint：833 文件通过；生产面验证：275 模块、293 资源通过。验收脚本 Ruff 通过，Git 空白检查通过。
- `outputs/prd148-protocol-runtime-20260912.json`：运行中网关 9 项匿名 HTTP 检查通过。重启前 Responses/Messages 仍为 404；重启当前源码后五个公开模型接口均登记，Chat/Responses/Messages 省略模型的有效请求体均到达鉴权并返回 401 `gateway.auth_missing`，健康检查 200。这不是使用真实员工 KEY 的模型调用或第三方 Agent 联调证据。
- `outputs/prd148-ga-lifecycle-20260912.xml`：GA 集成与事件 sink 测试 16/16 通过，474.446 秒，failure/error/skip 均为 0。

## 回归修正

旧 MCP 测试从文件第一个 `workspaceConsoleExperienceController.leave()` 推断 hashchange 位置；新增的其他调用使定位失效。改为实际注册并执行 hashchange/pagehide 回调，使用真实 MCP 状态机及真实通用导航绑定，验证同页保留、离页先擦除/取消再刷新、迟到响应拒绝。产品生命周期未因测试失败而改写。

## 敏感信息与剩余范围

本批 8 个修改源文件及当前结果文件的常见 API KEY、GitHub Token、私钥标记扫描为 0。脚本仅保存状态、计数、固定错误码、耗时、源码摘要；不保存数据库行、员工凭据或请求正文。临时集群目录与日志保留在本地，不纳入代码提交。

原观察 v 已因本批受测源码变更停止；其未完成事实不能用于当前版本的 72 小时结论。批次修复结束后另开有效观察，不累计已中断窗口。正式公开部署、真实六方会签、跨域最终对账、实际部署/回滚回执及完整 72 小时证据仍未满足，详见最终需求核对表。
