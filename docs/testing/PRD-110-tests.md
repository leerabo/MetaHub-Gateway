# PRD-110 测试记录

状态：注册、API 与资源反滥用及风控的迁移、运行时、API、Gateway/Control 接入和 Console 已完成专项实现与验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0101 -> 0102`、fresh、pristine down/up、业务事实与 active restriction 阻断 down、owner 复合外键、权限矩阵、限制到期 Job 激活、完整性与外键 |
| Shared runtime | 域隔离 HMAC 标识、固定窗口计数、风险判定、Redis/store 故障时按动作分层降级、Gateway 已授权热路径保留既有硬门禁 |
| Risk API | Global/Workspace 双 Cookie 路由、CSRF、权限、closed DTO、support 脱敏投影、签名规范游标、case revision CAS、幂等重放与冲突 |
| Challenge/appeal | owner 从会话恢复、一次性 challenge secret、token hash、attempt CAS、成功/失败清理、正常伴随 Cookie、冲突 Bearer/Platform/Support 凭据拒绝 |
| Restriction job | 到期 overlay 幂等终止、case 状态推进、固定 outbox 事件、历史 signal/decision/case/appeal 保留 |
| Integration preflight | Identity、Workspace 创建和 Gateway 风险预检；限制只收紧能力，不写角色、membership、entitlement 或 capability |
| Console | strict DTO、Cookie/CSRF/CAS/idempotency、case 状态过滤与游标分页、decision/appeal 时间线、support 独立裁剪、Identity 内存挑战、URL 参数即时移除、secret 不进入 DOM/Storage |

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-110 Migration | `3 passed` |
| PRD-110 Risk API | `4 passed` |
| Shared risk runtime | `3 passed` |
| Console focused | `5 passed` |
| Console lint | `343 files` 通过 |
| PRD-110 Python Ruff | 候选 Python 文件 `All checks passed` |
| Python compileall | PRD-110 涉及源码树通过 |
| `git diff --check` | 通过 |
| 新增内容敏感字面量扫描 | 无真实凭据、私钥、TOTP seed 或生产 Token 命中 |

批次级全数据库、完整集成/流程、真实浏览器、性能和安全回归按约定在 PRD-113 完成后统一执行。PRD-110 的性能门限在批次门禁中验证：风控判断 p95 `<30ms`，攻击流量下正常登录和 Gateway p95 增幅 `<20%`。

## 验证结论

- 风险对象绑定明确的 identity 或 `tenant_id + workspace_id` owner，跨 owner 输入不参与查询、挑战、决定或申诉。
- 原始 IP、设备、邮箱、电话和请求正文不进入 signal、事件或 Console；持久化标识均为按域隔离的 HMAC 摘要或粗粒度前缀。
- 风险判定仅能要求挑战、人工复核或施加可过期 restriction overlay，不能授予角色、membership、entitlement 或 capability。
- challenge secret 只在创建时返回一次，数据库只保存 hash；token 消耗和 case decision 均通过 CAS 与幂等命令防止重放。
- Platform owner/security 与 support 使用独立 DTO 投影；support 看不到 score、signals、阈值或决策控件。
- Redis/store 故障按动作风险分层：权限扩大的动作失败关闭到通用挑战，已授权 Gateway 热路径继续执行既有硬门禁并记录固定降级计数。
- Console 挑战流程只在内存中保存临时状态，敏感 URL 参数立即移除，答案输入在请求前和所有完成路径均清空。
