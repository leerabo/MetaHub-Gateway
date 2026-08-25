# PRD-115 测试记录

状态：实现、自动化门禁、本地真实认证和 Browser 静止终态验收均已完成。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0106 -> 0107 -> 0106 -> 0107`、fresh、重复执行、10 张 SCIM 表、复合 owner、权限矩阵、七种目录角色、`ops_admin` 拒绝、业务事实 down guard |
| Shared SDK | userName/externalId 规范化、受限 filter/parser、weak ETag version、域分离 HMAC、保守角色合并、RBAC 与固定错误合同 |
| SCIM/API | Cookie/CSRF 与 Bearer 边界、token 单次显示/轮换、RFC 7643/7644 content/filter/PATCH、Owner 保护、pending invitation、group role mapping、冲突 CAS、事件/outbox、CORS `PUT` |
| Membership | 已验证账号链接、SCIM source、目录角色投影、最后 group 移除回落 viewer、停用终态、Workspace Session 撤销、人工冲突解决不覆盖本地成员事实 |
| Console | exact request、strict response、Owner/security_admin 管理、auditor 只读、developer 隐藏、一次性 Bearer DOM/state 清理、内部 ID 隐藏 |
| Compatibility | Shared runtime 默认仍拒绝 `PUT` preflight；只有 Control API 显式启用；PRD-114 Enterprise Identity 合同保持通过 |

## 提交前结果

| 检查 | 结果 |
| --- | --- |
| PRD-115 Migration | `5 passed` |
| PRD-115 SCIM/API | `1 passed` |
| Shared SDK | `3 passed` |
| Gateway 非法 `PUT` preflight 回归 | `1 passed` |
| Enterprise Identity Console 专项与 PRD-114 回归 | `10 passed` |
| Console 全量 | `1082 passed` |
| Console lint | `356 files` 通过 |
| Ruff | `All checks passed` |
| `git diff --check` | 通过；仅保留既有 Identity CSS 行尾转换提示 |
| 新增 diff 本地敏感值扫描 | `0` 命中 |
| Private Key / Bearer / 高风险访问密钥格式扫描 | `0` 命中 |

## 本地认证与服务验收

- 本地数据库 `C:\MetaHub\.data\metahub.db` 已应用修正后的 `0107` 约束；项目连接器验证
  `PRAGMA integrity_check=ok`，外键异常为 `0`。
- 测试进程直接从本地凭据源读取测试账号、密码和所需认证数据；值只在内存中使用，未写入命令、日志、
  截图、产物、本文或提交。
- 真实登录、Workspace 列表、Workspace 切换、SCIM directory 与 group 读取均返回 `200`；活动 SCIM token
  轮换返回 `201`。
- Group PATCH 直接使用服务返回的 weak ETag 并返回 `200`，随后恰好生成一条 open
  `human_membership` 冲突。
- 进程退出前清空账号、密码、Cookie、CSRF、Bearer、响应与 TOTP 引用；Control access log 保持关闭。

## Browser 静止终态

- Owner 在真实 Console 的 Identity 页面看到恰好一条 open `human_membership` 冲突，以及
  `keep_local`、`accept_directory`、`ignore_directory` 三个可用恢复动作。
- 选择 `keep_local` 后 open 冲突数归零，终态显示“已解决”；数据库确认 conflict 为 `resolved`、resolution
  为 `keep_local`、revision 为 `2`，Membership 仍为 `admin`、`human`、`active`。
- 一次性 token Dialog 已关闭且不再存在；静止终态 DOM、Console 与页面错误均未发现凭据或 token 值。
- `1440x900`、`1280x720`、`390x844` 三档视口均为页面横向溢出 `0`、元素越界 `0`、控件重叠 `0`、
  可视裁切 `0`、Console error `0`。
- 验收结束后已复位临时 viewport 配置；页面保持在 `http://localhost:5173/identity.html` 供继续开发测试。

## 结论

迁移、SCIM 协议、Bearer 所有权、Membership 生命周期、冲突恢复、Console 合同、本地真实认证和 Browser
静止终态均通过。PRD-115 可以进入独立完成提交；批次级 G4 仍按计划保留到 PRD-119 独立提交后统一执行。
