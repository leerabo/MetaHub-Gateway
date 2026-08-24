# PRD-111 测试记录

状态：多区域高可用、备份恢复、灾难恢复双人审批、writer fencing、恢复证据和 Platform Console 已完成专项实现与验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0102 -> 0103`、fresh、pristine down/up、Region 不可变事实、恢复证据 `workspace_id + tenant_id` 复合 owner、DR 事实阻断 down、权限矩阵、`dr.run` 激活、完整性与外键 |
| DR API | Platform Cookie 域隔离、CSRF、closed DTO、恢复点安全投影、双人双角色审批、幂等重放与冲突、计划/epoch/驻留预检 |
| DR runtime | `precheck -> quiesce -> restore -> route -> verify -> complete` checkpoint、审批动态复核、旧 writer fencing、单一 active lease、Gateway placement 切换、恢复证据与固定事件 |
| Console | strict DTO、Cookie/CSRF/idempotency、Region/计划/审批/checkpoint/恢复点/证据投影、执行与安全复核独立裁剪、发起人不能显示第二票、busy 提交和导航锁定、移动端表格 |
| Compatibility | PRD-109/110/111 Console 三代回归，以及 PRD-110 API 在 `0103` 当前迁移头上的兼容验证 |

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-111 Migration | `3 passed` |
| PRD-111 DR API / Job | `4 passed` |
| PRD-110 当前头兼容 | `4 passed` |
| Console PRD-109/110/111 | `16 passed` |
| Console lint | `345 files` 通过 |
| PRD-111 Python Ruff | `All checks passed` |
| Python compileall | PRD-111 涉及源码树通过 |
| `git diff --check` | 通过 |
| 新增内容敏感字面量扫描 | 无真实凭据、私钥、TOTP seed、生产 Token、内部对象路径或 KMS 标识命中 |

批次级全数据库、完整集成/流程、真实浏览器、性能和安全回归按约定在 PRD-113 完成后统一执行。本 PRD 的专项门限已冻结为 Control RPO `<=15 min`、Control RTO `<=120 min`、Gateway region RTO `<=10 min`，批次门禁将验证实测分位数与故障注入结果。

## 验证结论

- DR API 只接受已签名固定计划的 ID、版本、原因和期望 epoch，不接受脚本、endpoint、对象路径、地址或凭据。
- 执行票要求 `platform.dr.execute`；第二票同时要求 `platform.dr.approve + platform.dr.security_review`，并由不同 operator 提交；Job 开始前再次验证两票实时权限与计划 hash。
- failover 通过单调 `failover_epoch`、唯一 active writer lease 和旧 writer fencing 防止双写；驻留组不匹配、目标不健康或恢复点/KMS 不可验证时失败关闭。
- checkpoint 可幂等接管，网络或 adapter 调用不持有长数据库事务；失败回切必须建立新的审批 run，不反向修改历史 run。
- 恢复点与证据仅暴露 schema、完整性、目标结果和 hash；对象路径、KMS 标识、tenant owner 与内部拓扑不进入 API 或 Console。
- Console 只使用 Cookie 会话，写请求绑定 CSRF 与幂等键；执行和安全复核控件独立裁剪，发起人无法显示自己的第二票入口，命令进行时无法重复提交、刷新或切换页面。
