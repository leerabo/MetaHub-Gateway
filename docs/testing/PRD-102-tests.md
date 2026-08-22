# PRD-102 测试记录

状态：企业认证、开票资料、平台审核、证据授权、凭据域切换和真实浏览器验收已完成。

## 已执行专项

| 测试组 | 当前结果 | 覆盖 |
| --- | --- | --- |
| Crypto/config | `4 passed` | 独立 AES-GCM/HKDF、owner AAD、blind index、扫描证明和生产密钥隔离 |
| Migration | `6 passed` | 0093->0094、fresh、pristine down/up、企业事实回滚阻断、完整性/FK |
| Business API | `1 passed` | Workspace 提交/开票、Platform 队列/审核/证据 token、CAS、幂等、动态撤权和隐私 |
| Credential switch | `1 passed` | Platform/Support 残留 Cookie 在 Workspace 切换成功后被清除，后续 Workspace API 可访问 |
| Compose/deploy | `9 passed` | 独立密钥配置、Compose 透传、部署生成和生产失败关闭 |
| Console focused | `46 passed` | PRD-094 至 PRD-102 页面状态、权限裁剪、严格 DTO 与敏感表单清理 |
| Console full | `1021 passed` | 控制台全量 Node 回归 |
| Console lint | `327 files` | 控制台源码与测试静态检查 |

每次 pytest 调用均使用新的显式 `--basetemp`。专项测试和验收记录不输出本地密码、TOTP、Cookie、
Session、加密密钥、DSN、对象引用、KMS 标识或客户资料明文。

## 已验证边界

- Workspace 企业资料只允许 organization 的 owner/finance_admin 访问，owner 由 WorkspaceSession、路径和
  数据库绑定共同恢复；personal Workspace 固定不适用。
- Workspace、Platform、Support 和 Bearer 凭据不能混用。Platform 登录清除 Workspace/Support Cookie；
  Workspace 切换成功清除 Platform 会话、Platform CSRF 和 Support 会话 Cookie，安全拒绝逻辑保持不变。
- 法定名称、登记号、对象引用、税号、地址、电话、开户行和银行账号使用独立业务密钥加密；公共响应只返回
  配置状态、末四位掩码、冻结摘要和不可变合同引用。
- 浏览器提交企业或开票表单后立即清空 DOM 与内存副本，不访问 localStorage/sessionStorage；平台证据 token
  在收到后立即清空，不写隐藏字段或持久化存储。
- Platform 队列不返回 workspace_id、tenant_id、主体明文、对象引用、对象地址或密文；reviewer 权限在路由
  和事务内双重动态校验，撤权后同一 Session 的下一次请求立即失败。
- 审核决定、合同、开票验证状态、平台审计和案件 revision 同事务提交；CAS 或幂等冲突不会产生第二事实。

## 浏览器验收

- 在隔离数据库与本地 `localhost:5173`/`localhost:8001` 运行真实登录、企业资料提交、开票更新、MFA、
  Platform 审核和证据授权流程；本地测试密码和 TOTP 直接输入，未在测试记录或页面快照中暴露。
- Platform 返回账号安全后复现了旧版混合凭据错误；修复后重新登录触发 Workspace 自动切换，企业认证、
  账单、成员和初始化数据全部成功加载，不再出现 ambiguous credential 错误。
- 390x844、1280x900、1440x900 三个目标视口均无横向溢出、控件重叠或控件越界；临时视口覆盖已恢复。
- 页面只显示登记号、税号和银行账号掩码；法定名称、对象引用、下载 token 和扫描证明不出现在渲染文本中。

## 注释与静态门禁

- PRD-102 的 13 个新增 Python、JavaScript、测试和 SQL 文件通过仓库官方定义、常量、关键分支、迁移操作
  中文注释规则；复合文件中的本次新增函数另行按差异审计并补充职责、安全和权限边界说明。
- 全仓 `scripts/ci/check-comments.py` 仍报告 PRD-102 之前复合文件中的历史注释债务；本 PRD 未修改或放宽
  检查器，也未把整仓历史注释重写混入独立提交。
- Ruff、Python compileall、Console lint 与 `git diff --check` 在最终代码状态下复跑。
