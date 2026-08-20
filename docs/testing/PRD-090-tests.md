# PRD-090 测试记录

日期：2026-08-20
结论：通过。PRD-090只交付架构、迁移合同和验证器；未新增业务API、页面、运行服务或数据库迁移。

## 1. 设计与两轮自审

| 门禁 | 结果 | 关闭内容 |
| --- | --- | --- |
| 第一轮：范围与现状一致性 | passed | 旧090..108活动指针退役；禁止按存量邮箱提升平台Owner；补齐258表、关键FK、邀请、核心身份API、账号冲突与`tenant_data_states`迁移边界；源码63项导航一一处置；PRD-138先GA再迁移的时序改为RC -> 分域 -> PRD-148 GA |
| 第二轮：安全与可验证性 | passed | 五类主体分域；SupportGrant客户/平台双审批；RFC 8785完整envelope签名；持久LKG高水位；离线撤销窗；Secret AAD；驻留与owner矩阵；Cookie/CSRF和混合凭据拒绝；平台品牌/Provider工程/商业账/共享页面与企业Workspace负矩阵 |

两轮独立审查提出的P0/P1均已写入设计、目标架构、威胁模型和失败关闭验证器；文档中不存在待合入审查结论。

## 2. 自动验证

| 命令/检查 | 结果 | 事实 |
| --- | --- | --- |
| `python scripts/planning/generate_saas_prd_plan.py` | passed | 当前已细化59条（PRD-090..148）；编号可继续追加且不设最终上限；12个Sheet生成并自检；源码63项导航逐项处置 |
| `python scripts/planning/validate_saas_roadmap.py --expected-next PRD-091` | passed | 73项失败关闭检查通过：开放式连续编号、DAG、角色/对象域、旧计划、源码63项导航、两轮自审、安全合同、Excel安全和0082边界 |
| `pytest -q scripts/planning/test_validate_saas_roadmap.py` | 4 passed | 依赖范围/斜杠展开、无固定终点、DAG正反例和仓库最终状态 |
| `pytest -q control-plane-api/tests/integration/test_prd089_db_migrations.py::test_0082_fresh_up_pristine_down_and_reapply` | 1 passed | 空库0001..0082、seed、幂等、0082 down/up、完整性和外键 |

pytest临时数据库固定在仓库内`.artifacts/prd090/`，没有使用权限异常的用户临时目录。该目录被`.gitignore`排除，不属于提交证据。

## 3. 空 SQLite 实际迁移

从不存在的SQLite文件执行共享迁移器，完成0082回退再应用后读取静止数据库：

| 事实 | 结果 |
| --- | --- |
| applied migration count | 82 |
| latest migration | `0082` |
| migration `0083` | 不存在 |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA foreign_key_check` | 0条违规 |
| 非SQLite内部表 | 258 |

PRD-090没有创建0083。PRD-091如需数据变化，必须另行设计、两轮自审并从0083开始。

## 4. 十一类测试映射

| 组 | 本条证据 |
| --- | --- |
| WB | 表/FK现状、owner传播、身份状态机、依赖DAG、完成前缀和验证器白盒 |
| API | 冻结Global/Workspace/Platform/Support/Device主体负矩阵、角色页面投影、直达URL/API后端复核和统一错误码/request_id；本条无新运行API |
| BB | 只从公开Excel/README确认旧计划禁止、PRD-090完成、PRD-091为下一项 |
| GR | 旧Excel、新Excel、README、planning、prd、testing交叉一致；迁移真实down/up |
| PERF | 冻结Control、页面、Gateway、撤权和Device吊销SLO；本条未改变运行热路径，运行压测不适用 |
| WEB | 本条无页面和浏览器行为，按明确非目标不运行浏览器脚本 |
| UI | 冻结平台/企业独立导航、角色页面矩阵和63项现状处置；平台运营/品牌/Provider工程/商业目录不进入企业Workspace；企业保留角色化AI治理；本条无运行UI |
| UCD | 冻结登录不填租户、三种产品形态、离线能力、企业管理员添加职能用户、默认折叠单组展开、角色任务首页和高级入口降级；实际交互留给对应实现PRD |
| COMP | 保留PRD-001..089事实、tenant物理ID、SQLite开发基线和N/N-1兼容；禁止一次性换根表 |
| SEC | STRIDE、主体隔离、JIT双审批、签名/LKG、Secret、驻留、CSRF、防枚举和敏感信息扫描 |
| FLOW | 旧计划退役 -> PRD-090设计/两轮自审 -> 空库验收 -> 完成态 -> PRD-091下一项 |

## 5. G0与完成状态

- 旧PRD-090..108保留审计但禁止开发，旧Excel两个活动状态指针已退役。
- Excel不含宏、外链、隐藏Sheet或公式；59条PRD、12个Sheet、63项导航、角色ID和对象域验证通过。
- Provider Key、Token、Prompt、Response、私钥和疑似真实凭据未进入文档、工作簿或验收输出。
- `saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。
- PRD-090标记已完成；下一未完成项为PRD-091，且尚未编码。
