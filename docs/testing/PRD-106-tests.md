# PRD-106 测试记录

状态：Platform 专用分布式任务运行时、0098 迁移、受控 API、单一 supervisor、Console 任务运行时与
真实浏览器验收均已完成。PRD-106 保持独立实现、测试记录、敏感审计和提交。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| PRD-106 Migration | 0097->0098、fresh、pristine down/up、带事实阻断 down、完整性与租户/批任务隔离 |
| PRD-106 runtime/API | 严格 PlatformSession/CSRF、凭据域隔离、CAS、幂等、KMS、fencing、checkpoint、effect、取消、重试与死信 |
| 性能与恢复 | 40 次加密入队 p95、5 次空闲队列启动 p95、过期租约接管与 stale worker fencing |
| Supervisor | 既有 scheduler 纳入单一 lifespan，启动顺序稳定，关闭顺序反转，超时 worker fencing |
| Console | 权限导航、队列/任务/类型、状态筛选、安全详情、写命令确认、迟到响应隔离与移动端标签行 |
| 相关回归 | PRD-095 导航/双域 Cookie jar、PRD-101 Gateway、PRD-105 通知与 0097 迁移 head |

每次最终 pytest 调用均使用新的显式 `--basetemp`，不复用、不删除现有 `.pytest-*` 目录。

## 最终结果

| 检查 | 结果 |
| --- | --- |
| PRD-106 Migration | `4 passed` |
| PRD-106 runtime/API/performance | `11 passed` |
| PRD-095 导航/双域回归 | `1 passed` |
| PRD-101/105 API/worker 回归 | `4 passed` |
| PRD-105 Migration | `3 passed` |
| Console focused | `19 passed` |
| Console full | `1037 passed`，零失败、零取消、零跳过 |
| Console lint | `335 files` 通过 |
| Python static | PRD-106 Python 变更 Ruff 通过；`compileall` 通过 |
| 隐私与差异 | PRD-106 候选文件敏感信息扫描无命中；`git diff --check` 通过 |

相关的 PRD-095 warm performance 用例已修正 Workspace 切换后继续使用已删除 Platform Cookie 的旧测试流程，
功能流程可到达原性能断言；但本机关闭 runtime 日志捕获后 PlatformSession p95 仍为 `139.7 ms`，未达到历史
`<50 ms` 门槛。该阈值未放宽，本记录不将此相关旧门禁记为通过。

## 已验证边界

- 运行时只接受预编译、固定 artifact SHA-256 与 contract version 的 handler；API 不接受代码、模块、命令或 URL。
- 作业输入和 checkpoint 使用 KMS envelope encryption 与 owner/type/digest AAD；篡改固定映射为 input invalid，
  KMS 故障独立映射为 unavailable，密钥缓冲区在成功和异常路径均清零。
- claim、heartbeat、start、checkpoint、effect 和 complete 均绑定 generation、worker 与 lease token；过期租约恢复后
  stale worker 不能写 checkpoint 或 effect。
- 自动重试受 `max_attempts` 限制；手工死信重试保留旧死信事实，并受独立的 20 次硬上限约束。
- 取消在 queued、leased、running 和 effect commit 竞态中保持失败关闭；不可变 attempt、event 和 dead-letter 事实保留。
- Platform 权限目录只向 `platform_owner`/`platform_ops` 授予作业能力；导航由后端权限投影，权限撤销后不再返回入口。
- 业务事务使用 `enqueue_prepared_platform_job_in_transaction` 保持原子提交；独立 enqueue 与 worker iteration 使用温连接
  作用域，减少 SQLite 打开和 schema 初始化成本而不改变事务边界。

## 性能门禁

- 加密、校验、幂等 HMAC 和 SQLite 提交全部包含在 40 次入队采样内，nearest-rank p95 严格断言 `<100 ms`。
- supervisor 在确认空队列后执行 5 次任务投递，nearest-rank 启动 p95 严格断言 `<10 s`。
- 过期租约接管执行时间严格断言 `<60 s`，并同时验证旧 generation 写入被 fencing。

## 浏览器验收

应用内 Browser 使用本地测试账号和实时 TOTP 完成 GlobalSession 与 PlatformSession 交换。后端返回 17 个授权业务
导航项，Console 加独立概览后显示 18 项，`Job runtime` 可见且可进入。页面真实读取 3 个队列、8 个签名任务类型，
并投递一个无敏感数据的 `platform.runtime.probe`；任务成功后列表显示安全 outcome，详情只显示 Attempt 时间线与
Checkpoint 摘要，不显示输入 envelope、ciphertext、幂等 HMAC、Session、CSRF、KMS 材料或 traceback。

`390x844`、`1280x900`、`1440x900` 三档视口均为零页面级横向溢出、零控件重叠、零非导航文本裁切；移动端任务
和任务类型表转换为带字段名的纵向行。验收期间页面没有错误状态，Control API health 为 200。当前应用内 Browser
能力不提供历史 console 流，因此未将 console error 数量伪记为 0。验收完成后已恢复默认视口并退出本地账号，
凭据和 TOTP 未写入截图、文档、日志或提交。
