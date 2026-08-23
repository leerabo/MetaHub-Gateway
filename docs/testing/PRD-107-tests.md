# PRD-107 测试记录

状态：功能开关、租户分群基础、渐进发布、全局 Kill fence、Workspace 最终能力投影、Gateway 本地能力门与
Platform Console 已实现。PRD-107 保持独立实现、测试记录、敏感审计和提交。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| PRD-107 Migration | 0098->0099、fresh、pristine down/up、带事实阻断 down、权限矩阵、任务类型激活与完整性 |
| Platform API/runtime | closed DTO、双凭据域隔离、CSRF、CAS、幂等、不可变版本、P0 双人审批、自批拒绝、暂停/恢复/回滚与同步 kill |
| 健康与乱序 | 冻结健康水位、HMAC 完整性、过期/缺失/无效摘要安全暂停、更高乱序摘要不能劫持已调度 job |
| Workspace 投影 | WorkspaceSession owner、无 tenant 参数、entitlement 优先、固定评估顺序、HMAC subject 与评估快照 |
| 曝光与隐私 | 幂等曝光、按版本计数、回滚保留、响应不含 subject、成员、规则 hash 或分桶材料 |
| Gateway | 已验证快照 owner/generation/expiry fencing、本地失败关闭判断与原子传播 |
| Console | 严格客户端、独立权限裁剪、版本/阶梯/审批/健康/曝光、迟到响应隔离、390px 标签行、禁止本地 override |
| 相关回归 | PRD-095 Platform 导航与 PRD-106 任务运行时 |

每次最终 pytest 调用均使用新的显式 `--basetemp`，不复用、不删除现有 `.pytest-*` 目录。

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-107 Migration | `3 passed` |
| PRD-107 API/runtime/control evaluation | `6 passed` |
| Gateway local gate/performance | `3 passed` |
| Console focused | `5 passed` |
| PRD-095 Platform navigation | `1 passed` |
| PRD-106 task runtime regression | `11 passed` |
| Console lint | `337 files` 通过 |
| Python static | PRD-107 Python 变更 Ruff 通过 |

## 已验证边界

- `POST /platform/feature-flags` 使用显式 `create|version` closed action；候选版本只追加且不会自动切换 head，
  rollout job 成功后才发布，回滚创建新 rollout 指向旧的不可变版本。
- P0 rollout 需要两名不同且当前具 `platform.feature.approve` 的非发起操作员；路由允许独立 approve 权限，
  服务按 action 再做精确授权。`release_manager` 没有 kill，owner 与 ops 才能同步写 kill fence。
- 调度冻结 `RolloutHealthSummary.source_watermark`；worker 验证域隔离 HMAC、状态与有效期。缺失、无效、过期、
  unhealthy 或错误水位均暂停，不扩大百分比。
- Workspace 能力响应只由 WorkspaceSession owner 决定，不接受 tenant、URL、Storage、DOM 或请求 override。
  entitlement、Workspace 状态与 kill 高于 prerequisite、环境、Region、cohort 和 percentage。
- 评估与曝光只保存 HMAC 化 subject/request；响应与 Console 不显示盐、HMAC、规则 hash、原始成员或账号标识。
- Gateway 只消费上游已验签的最终 capability snapshot；owner 不匹配、旧 generation、过期、未知 Flag 均失败关闭。

## 性能门禁

- Gateway 本地最终能力判断采样 20,000 次，nearest-rank p95 严格断言 `<1 ms`。
- 控制面固定顺序与版本化 HMAC 判断采样 5,000 次，nearest-rank p95 严格断言 `<20 ms`。
- 已验证快照从原子 apply 到本地可见严格断言 `<5 s`；PRD-108 将接管跨服务分发，但不改变本地 owner 合同。

批次级数据库、集成、真实浏览器、性能和安全回归按 PRD-107～113 批次约定在 PRD-113 完成后统一执行。
