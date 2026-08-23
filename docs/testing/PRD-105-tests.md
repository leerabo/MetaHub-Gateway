# PRD-105 测试记录

状态：通知数据模型、source/outbox、偏好 CAS、模板版本、异步 worker、Workspace 通知中心和 Platform 模板编辑器
均已实现；最终回归、静态检查与真实浏览器验收均已完成。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| PRD-105 Migration | 0096->0097、fresh、pristine down/up、完整性、immutable trigger 与角色目录恢复 |
| PRD-105 API/worker | source exactly-once、参数 allowlist、已读 CAS、偏好 revision、mandatory、模板发布、投递隐私 |
| 业务邻接回归 | PRD-098 订阅、PRD-102 认证、PRD-103 支付、PRD-104 发票事务的通知 outbox hook |
| Console focused | Workspace/Platform CSRF 隔离、严格 DTO、未知字段拒绝、模板语法、权限裁剪与 HTML 转义 |
| Console full/static | 全量 Node 回归、Console lint、Ruff、compileall 与敏感内容扫描 |

每次最终 pytest 调用均使用新的显式 `--basetemp`，不复用、不删除现有 `.pytest-*` 目录。

## 最终结果

| 检查 | 结果 |
| --- | --- |
| PRD-105 Migration | `3 passed` |
| PRD-105 API/worker | `3 passed` |
| PRD-098/102/103/104 业务邻接回归 | `4 passed` |
| Console focused | `3 passed` |
| Console full | `1033 passed`，零失败、零取消、零跳过 |
| Console lint | `333 files` 通过 |
| Python static | 本次变更 Python 文件 Ruff 通过；`compileall` 通过 |
| 隐私与差异 | 28 个变更文件的已知敏感值和凭据模式均为 0 命中；`git diff --check` 通过 |

全仓 Ruff 仍报告 1957 条本次改动前已存在的历史问题；本次未改动无关文件，也未将该基线误记为通过。

## 已验证边界

- Workspace 收件箱固定为当前 `workspace + account` owner；管理员不能读取其他成员通知，PlatformSession 不能进入。
- 业务事务只写加密参数与模板快照 source/outbox，provider 故障不进入业务请求路径；相同 source 重放不重复通知。
- renderer 只接受事件 exact allowlist 和 `{{name}}`；未知/缺失参数、HTML、表达式、include 与未配对花括号失败。
- 偏好使用 quoted ETag CAS；security 强制项不能关闭，SMS/WebPush 未配置时明确 unavailable。
- worker 使用租约、generation CAS、有界退避和 Workspace 公平领取；单消息隔离不形成跨租户队首阻塞。
- outbox、delivery attempt、通用审计、日志、Console DOM 与文档均不保存或展示联系地址及受控凭据。
- Platform 模板路由动态检查 communications/owner 权限，published 版本不可原地修改，重复幂等键只重放同一命令。

## 浏览器验收

验收使用隔离的本地测试账号和实时 TOTP，完成 Workspace 切换、铃铛未读数、收件箱、未读筛选、已读、刷新后
计数稳定、偏好开关，以及 Platform 模板版本表与受控编辑器验证。编辑器事件切换会同步更新参数 allowlist，
并成功创建隔离草稿版本。

`390x844`、`1280x900`、`1440x900` 三档视口均为零页面级横向溢出、零控件重叠、零文本或控件裁切；
Identity 与 Platform 控制台警告/错误均为 0。验收完成后已恢复默认视口、清除浏览器凭据/TOTP 绑定、删除隔离
数据库和临时日志，并恢复常规本地 Control API。验收记录不包含密码、TOTP、Cookie、Session、DSN、私有标识
或路径。
