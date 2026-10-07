# PRD-036 测试记录

PRD：`PRD-036 - 非流式请求备用模型Fallback`

## 2026-10-05 当前扩展

历史用例仍保留“两候选”作为最小回归场景。当前实现已扩展为每条路由 1 个主模型和最多 31 个备用模型：非流式请求在白名单故障后沿冻结顺序继续尝试，最多 32 次；成功立即返回，全部失败才返回最终错误。`routing_attempts`、`request_logs.routing_attempt_count`、`routing_candidates` 和预算最坏成本均按 32 次链路处理。数据库扩展由 SQLite `0173_routing_candidates_32.sql` 和 PostgreSQL `0181_routing_candidates_32.sql` 提供。

## 覆盖范围

- 迁移：`0026_non_stream_fallback.sql` 新建 `fallback_policies`、`routing_attempts`，扩展请求日志和用量账本，验证租户/attempt/usage 关联触发器与确定性 down 回滚。
- 策略：模型别名主备目标、启停、固定条件白名单、幂等 PATCH、变更递增版本、同主备模型拒绝、跨租户/禁用模型拒绝。
- 数据面：非流式请求在受控故障后按冻结顺序调用后续备用模型，最多 32 次；只有白名单故障可触发；每次网络调用前必须持久化 started attempt。
- 流式边界：`stream=true` 始终保持单一上游，不执行请求期 Fallback，也不按备用模型预留预算。
- 成本与日志：预算按主备最坏估算总和准入；请求日志保存触发摘要和最终 attempt；Usage 关联最终成功 attempt；无用量失败记录 `unavailable`，不伪造零成本。
- 事件：`route.fallback_triggered` 使用稳定事件 ID、有界非阻塞队列、固定安全 payload 和最多三次 Webhook 投递，不保存上游正文、Header、Prompt、Response 或 Key。
- 权限：策略写接口仅 admin；developer 只读；viewer 后端拒绝。所有查询和写入均从认证上下文取得 `tenant_id`。
- 前端：admin 策略编辑器、developer 只读摘要、请求详情 attempt 时间线、HTML 转义、关闭态 details 隐藏和移动端展开后完整滚动。
- 中文注释：新增 Python、JavaScript、SQL、CSS、PowerShell 与测试定义，以及路由、权限、租户、错误、成本和回滚关键分支均通过强制门禁。

## 执行命令

```powershell
$env:PYTHONPATH='shared-sdk/python;control-plane-api/src;gateway-data-plane/src'
python -m pytest -q control-plane-api/tests
python -m pytest -q gateway-data-plane/tests
node --test console-web/tests/unit/*.test.mjs
node console-web/scripts/lint.mjs
python -m compileall -q shared-sdk/python control-plane-api/src gateway-data-plane/src control-plane-api/tests gateway-data-plane/tests
python scripts/ci/check-comments.py
```

## 当前结果

- 数据库迁移集成测试：6 passed；空库 0001-0026 正向迁移、重复幂等和 0026 单步回滚均通过。
- 控制面全量测试：85 passed；包含策略 PATCH 的 admin 权限、developer 读取、非法目标和幂等版本测试。
- 数据面全量测试：72 passed；包含主模型 5xx 后备用成功、流式不切换、主备预算总和和第三次 attempt 拒绝测试。
- 前端单元测试：117 passed；模型别名策略、权限视图和调用 attempt 时间线通过。
- 前端 lint：`console lint ok (84 files)`；Python `compileall` 通过。
- 中文注释门禁：`comment coverage ok`。
- 运行态：开发库成功应用 0026；Gateway、Control API、Console 分别监听 8000、8001、5173，两个 readiness 均为 true，管理员登录成功。
- 视觉复验：桌面 1440 宽度下策略浮层无横向溢出；手机 390 宽度下关闭态表单高度为 0，展开态表单进入文档流，文档高度增加 224px，表单底部位于可滚动范围内，横向溢出元素为 0。

## 关键验收用例

| 用例 | 输入/场景 | 预期 |
| --- | --- | --- |
| 主流程 | 非流式主模型返回 500，策略包含 `upstream_5xx` | attempt 1 failed，attempt 2 succeeded，响应来自备用模型，Usage 关联 attempt 2 |
| 超时/限流 | 当前候选 timeout 或 429 且条件启用 | 按冻结顺序继续切换，记录每次固定错误分类和 `route.fallback_triggered` |
| 非白名单错误 | DNS/TLS、本地参数、鉴权、DLP、预算错误 | 不触发备用模型，不产生 attempt 2 |
| 流式边界 | `stream=true` 后上游返回 5xx 或中断 | 不切换模型，不混合两个 Provider 的 SSE 输出 |
| 重试风暴 | 尝试创建 sequence=33 或重复任意 sequence | 审计闸门拒绝，网络调用不开始 |
| 预算 | 主、备估算各 0.1 CNY，余额只允许 0.15 CNY | Provider 调用前阻断，不按单模型金额放行 |
| 用量不可用 | 两次上游失败且未返回 usage | attempt 标记 `unavailable`，不写伪造零用量账本 |
| 权限 | developer/viewer 调用策略 PATCH | 后端返回统一权限错误码并携带 request_id |
| 脱敏 | 上游错误包含 Key、Header、Prompt 或响应正文 | API、日志、attempt、Webhook 和控制台均不出现敏感原文 |
