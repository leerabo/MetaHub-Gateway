# PRD-114 测试记录

状态：实现、自动化门禁、本地真实认证和 Browser 静止终态验收均已完成。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0105 -> 0106 -> 0105 -> 0106`、fresh、重复执行、10 张企业身份表、复合 owner、全局域名唯一、权限矩阵、业务事实 down guard |
| Enterprise Identity API | WorkspaceSession、Origin/CSRF、RBAC、动态撤权、跨 Workspace 同形拒绝、幂等/CAS、严格 DTO、`no-store`、事件/outbox 与错误合同 |
| OIDC/SAML/DNS | PKCE、state/nonce/replay、固定 issuer/audience/endpoint、JWKS、SAML 签名节点与算法白名单、DNS rebinding、公共 IP 固定连接 |
| SSO policy/recovery | active test、证书窗口、Owner/MFA 前置条件、break-glass password + TOTP、单次 recovery grant、WorkspaceSession 动态撤销 |
| Console | exact request、strict response、Owner/security_admin 管理、auditor 只读、developer 隐藏、OIDC/SAML 表单、retry key、敏感输入清理、零浏览器 Storage |
| Compatibility | PRD-091/094 Identity、PRD-095 Platform、共享配置错误优先级、当前 24 项 Platform 导航及并发性能阈值 |

## 提交前结果

| 检查 | 结果 |
| --- | --- |
| PRD-114 Migration | `4 passed` |
| PRD-114 API 主流程/负矩阵 | `2 passed` |
| OIDC/SAML/DNS 协议专项 | `6 passed` |
| Shared SDK | `4 passed` |
| Enterprise Identity Console 专项 | `7 passed` |
| Console 全量 | `1079 passed` |
| PRD-091/094 Identity Console 回归 | `29 passed` |
| PRD-095 Console 回归 | `9 passed` |
| PRD-094/095/114 Shared SDK 回归 | `13 passed` |
| PRD-095 Platform API/性能最终回归 | `2 passed`；既有性能阈值未放宽 |
| Console lint | `355 files` 通过 |
| Ruff | `All checks passed` |
| Python compileall | 涉及源码与测试通过 |
| `git diff --check` | 通过；仅保留既有行尾转换提示 |
| MyPy | 当前环境不可用，未执行 |
| 新增 diff 本地敏感值扫描 | `0` 命中 |
| Private Key / Bearer 高风险格式扫描 | `0` 命中 |

## 本地认证与服务验收

- 本地数据库从现存账号与 MFA 数据迁移到 `0106`，未重置账号、密码或动态验证因子。
- Control API 与 Console 分别在 `http://127.0.0.1:8001`、`http://127.0.0.1:5173` 启动并通过健康检查。
- 测试进程直接从本地凭据源读取账号与密码，向 `/identity/login` 提交后返回 `200`，响应只包含冻结的
  Account/Session 合同并签发 Global Session Cookie。
- 当前 TOTP 由本地加密 MFA 记录在内存中生成，格式校验为 6 位；值未写入命令、日志、截图或本文。
- 使用同一内存会话完成 `/identity/workspaces`、`/identity/workspace-switches`、`/workspace/session` 和
  `/workspaces/{id}/enterprise-identity`，全部返回 `200`；当前角色投影为 `owner`，初始域名和连接均为空。
- 测试进程结束前清空账号、密码、CSRF、Cookie 和响应对象；Control access log 保持关闭。

## Browser 静止终态

- Owner 在真实 Console 中完成企业域名创建、一次性 DNS TXT 展示、刷新后 challenge 消失和
  `prd114-browser.example` 验证；OIDC/SAML 切换与两类有效配置提交均成功。
- client secret、JWKS 和 certificate 输入在提交后清空。静止终态 DOM 不含这些值，也不含 endpoint、
  issuer、client ID 或内部资源 ID。
- `auditor` 只看到只读企业身份摘要且没有管理表单，`developer` 完全看不到企业身份区；验收后浏览器与
  数据库角色均恢复为 `owner`，Membership 约束触发器已恢复并确认存在。
- `1280`、`440`、`390` 三档视口分别在 `100%`、`125%`、`150%` 缩放下完成九组稳定期测量；每组均为
  页面横向溢出 `0`、控件重叠 `0`、可视裁切 `0`、文字裁切 `0`、稳定期位移 `0`。
- 权威裁剪截图为 `.artifacts/prd114-enterprise-identity.png`，原始视口截图为
  `.artifacts/prd114-enterprise-identity-viewport.png`。最终截图只包含企业身份区，不含账号邮箱。
- 截图二进制精确扫描未发现邮箱、secret、JWKS、证书、endpoint 或内部 ID；DOM 精确扫描和人工图像检查
  均通过。本机没有可用 OCR，因此未执行截图 OCR 扫描，并如实保留该限制。
- 验收结束后已复位临时 viewport 配置；页面保持在 `http://localhost:5173/identity.html` 供继续开发测试。

## 结论

迁移、后端协议、权限、恢复、Console 合同、兼容回归、本地真实认证链路和 Browser 静止终态均通过。
PRD-114 可以进入独立完成提交；批次级 G4 仍按计划保留到 PRD-119 独立提交后统一执行。
