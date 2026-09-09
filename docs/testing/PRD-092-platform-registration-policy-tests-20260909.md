# PRD-092 平台注册策略验证

原工作树专项证据见 `MetaHub-registration-trial-20260909.md`：注册/平台策略 4 项，包含真实 Platform MFA 会话、权限、CSRF、revision 冲突、审计、不可用邮件拒绝开启、验证码设备绑定/过期/错误/重放/尝试上限、无邮箱证明与 prod/邀请拒绝。

`.artifacts/s1-browser-6y8nim3f/acceptance.json` 的两个 registration 阶段已从真实页面提交成功，检查密码眼睛、隐私链接、验证码 PNG 和新账号会话。该隔离工作树证据不等于本提交已包含后续组织/使用者功能。

本次按 PRD 分层暂存后，从 Git index 验证源码、注册/平台前端测试和生产构建；整个 S1 暂存整理完成后统一执行数据库/API/并发/安全等相关批次测试。最终结果另附本批提交版本验收记录，不以旧工作树通过推断新提交版本通过。

## 当前提交副本结果

Git index 产品树 `7a118a2dbd38cff3a1858b636610bd81a61dd45d` 的注册/平台前端 34 项通过，0 failures/errors，证据 `.artifacts/g6-security-20260907/s1-registration-index-frontend-20260909.xml`。独立副本生产 surface 和 287 资产构建通过；index 摘要 `6b8ddc5e1efcb1834a0b6dab1887ab1acb5c547839cc898a2387054bd50ccd3c`，不等于运行中包含其它增量的集成包摘要。首次副本缺 Dockerfile 且摘要任务尚未完成，补齐同一 index 的构建输入后通过，没有修改产品构建断言。

5 个相关 Python 源码/测试 Ruff 通过，10 个暂存 JS 语法通过。27 个暂存文件高置信度凭据及禁止路径扫描零命中：`.artifacts/g6-security-20260907/registration-core-index-audit-20260909.json`。
