# PRD-093 创建组织后进入控制台

## 变更范围

创建组织且已提供跳转回调时，完成组织目录、WorkspaceSession、成员、导航授权和向导读取即可进入控制台，不再等待账单、SSO 等其他页面数据。只有当前请求代次和本次创建的组织都匹配时才跳转。设计和两轮自审见 `docs/prd/PRD-093-local-organization-admission.md`。

## 专项验证

- 工作树和独立 index 导出前端各 43 项通过：`s1-organization-entry-focused-20260909.xml`、`s1-organization-entry-index-20260909.xml`，位于 `.artifacts/g6-security-20260907`。包括不预读附加面板、拒绝权限漂移、退出后丢弃迟到响应及拒绝错误组织。
- 被测 index 产品树：`92b991844edcaa826dcf8554b18e43f4c8c92378`；构建摘要：`964db3aa0dc56dd10f1fc79ec3be41362c5fa0f092b99f41f4317159c9938143`。生产 surface 验证 271 模块，构建 289 资源。
- `.artifacts/s1-browser-eqnhxhpz/acceptance.json`：真实 Chromium，1280 和 390 两个视口，注册、创建组织、创建两位使用者、签发独立 KEY、轮换及停用共十个阶段通过。HTTP 校验 289 个生产资源的逐项哈希。创建者为 Owner，平台策略接口拒绝访问，员工 KEY 不能登录后台。
- 已查看桌面与手机使用者页面截图；所测可见输入字号至少 16px，无横向溢出。两个 Owner 注册只新增两个账号、两个用户投影及两个成员关系，四名使用者不新增登录账号。
- 浏览器没有收费模型请求。Gateway 验证使用真实 KEY 对 `/feedback` 的鉴权结果，不能将该证据宣称为真实模型生成成功。

## 未决与发布边界

此前及静态分发调整后仍出现首次加载超时，保留 `s1-browser-p1og4mxa`、`s1-browser-4fs93yrm`、`s1-browser-so2q_0as` 失败证据，不删除失败或放宽时限。通过一次完整流程不是跨浏览器、性能与长期稳定性全部通过。

本地发布只允许替换与被测模块字节一致的 `identityPageState.js`。其他已发布 S2 文件必须与旧构建逐项哈希一致，不能用 index-only 构建覆盖试用环境。整体 S1 仍保留 PostgreSQL 使用者、性能及其他兼容性待验项目。
