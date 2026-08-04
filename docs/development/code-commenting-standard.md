# 代码中文注释规范

本项目面向企业级 AI Gateway 和治理控制面，代码默认要求中文注释，便于后续交付、审计、运维和二次开发。

## 强制要求

- 每个源码、测试、迁移和样式文件必须至少有一段中文语境说明；空 `__init__.py`、纯导入文件、前端测试和 CSS 不能绕过注释门禁。
- 每个 Python `class`、`def`、前端 `class`、`function`、`export function`、顶层箭头函数和构造函数必须有中文 docstring 或 JSDoc，说明职责、边界和关键约束；不能使用“说明该定义的职责”这类模板占位文本。
- 顶层常量、角色集合、错误码表、敏感字段名单、Provider 类型白名单、前端标签/选项表等也属于定义，必须在常量组前写中文分组说明，说明治理用途、权限边界或脱敏要求。
- 定义级注释必须有足够中文信息量，至少能看出该定义的业务职责、调用边界或安全约束，不能只写“列表响应”“注销响应”等短标签。
- 关键判断必须有短注释，尤其是权限校验、租户隔离、脱敏、降级容错、路由分支、成本计算、写库失败处理和外部调用重试。
- 涉及密钥、Token、Prompt、Provider Key、Virtual Key、Authorization 的代码必须注明哪些数据允许落库、返回或记录日志。
- 前端只隐藏按钮不算权限，相关渲染函数必须注明后端仍要做权限校验。
- DTO、API 路由和 service 函数必须注明 tenant_id 来源，业务数据不能由请求参数伪造租户边界。
- SQL 迁移文件中的 `CREATE TABLE`、`CREATE INDEX`、`ALTER TABLE`、`DROP`、`INSERT`、`UPDATE`、`DELETE` 操作前必须有中文说明，说明结构变更、回滚或数据回填目的。
- CSS 文件必须有中文分段说明；大型样式文件不能超过 500 行没有新的中文区块注释，避免控制台样式堆叠后难以定位业务面板边界。
- 新增 PRD 完成时必须运行注释覆盖扫描：`python scripts/ci/check-comments.py`。该检查已经接入 `scripts/ci/check.ps1`，覆盖 Python、JavaScript、SQL 和 CSS；发现缺口要先补齐再进入验收。Windows 解释器不稳定时使用 `$env:METAHUB_PYTHON='<python.exe>'; powershell -ExecutionPolicy Bypass -File scripts\ci\check.ps1`。

## 注释风格

- 注释解释“为什么”和“边界是什么”，不要写“给变量赋值”这类无信息内容。
- 中文注释使用 UTF-8，不允许用问号占位或乱码文本。
- 热路径注释要简短，优先说明安全、可追踪和失败降级原因。
- 测试代码也要给主流程、异常流程和权限流程写中文说明。

## 当前基线

截至本次注释质量整改后，全仓 Python/JavaScript 代码文件中可扫描的 `class`、`def`、`function`、`constructor`、顶层常量组、错误码表和配置表已完成中文注释覆盖；SQL 迁移操作、CSS 文件语境说明和关键治理分支也已纳入 CI 门禁，后续 PRD 不能降低该基线。
