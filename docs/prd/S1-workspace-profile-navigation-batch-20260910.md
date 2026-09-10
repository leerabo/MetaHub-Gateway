# Workspace 资料与初始化导航补齐

范围：PRD-093 资料合同、PRD-094 会话更新与成员隔离、PRD-097 八步导航、PRD-147 控制台页面迁接。沿用原 Excel 和已有设计；成员隔离缺陷使用下述成对迁移修复，不猜测或改写已发布的用户身份状态。

## 共享实现合同

- 新组织资料页复用 `GET/PATCH /workspaces/{id}`，组织名称读写 `organization_profiles`，个人名称读写 `personal_profiles`。不把旧 Tenant 名称当成组织资料权威，也不通过 `/tenant/current` 绕过资料版本。
- Workspace ID 只来自当前已验证控制台 owner，GET 使用 Global Cookie，PATCH 使用 Global Cookie、CSRF 与 If-Match。服务端既有 Membership 校验保持；只有 Owner 修改，其他有资料页权限的组织管理员查看。这里不增加可委派资料修改权限。
- 资料页显示名称、类型、区域、服务模式、默认环境与状态。名称可编辑，类型/区域/模式为只读，不暴露数据库 ID、账号邮箱或技术会话信息。旧租户客户端与 renderer 保留兼容，不被新组织页面调用。
- 保存沿用正式 Workspace revision。资料更新会使旧 WorkspaceSession 过期，只有当前页面操作仍有效时，才用现有全局会话切换接口重新进入同一组织，再重新读取权限和导航。后台刷新不得在保存与更新会话之间把页面误送到登录。
- 复用资源页面生命周期，离页/切组织取消读取与保存，旧响应不得修改新页面、恢复名称草稿或重新切回旧组织。保存成功后会话更新失败应保留可恢复状态，不重复写入名称。
- 八步链接使用已有白名单映射，资料页改为真实 Workspace 表单，其余配置入口仍须真实浏览器验证。不运行收费模型，不以 SDK 页面打开冒充完成首次调用。

## 第一轮自审：领域与权限

1. 直接给旧 tenant client 补 Cookie 会继续把名称写入错误的资料来源，决定复用既有 Workspace API 和严格 DTO。
2. 新控制台的授权快照没有 legacy `user` 对象，不能伪造 User 让旧 renderer 通过。新 renderer 显式使用 Workspace 资料及当前验证角色。
3. Owner 与 admin 不混用。PRD-093 后端只允许 Owner 修改资料；页面保持此边界，不借旧接口扩大权限。
4. 服务模式属于创建与部署合同，不能照搬旧租户任意切换控件。此批只开放既有名称更新。

## 第二轮自审：并发、恢复与测试

1. Workspace revision 变化会使会话失效，保存后必须重新取得组织会话，不能篡改数据库的授权快照或跳过鉴权。
2. 在重新进入组织前后都检查当前操作 owner 和 AbortSignal。切换组织后的迟到成功只被丢弃，不触发旧组织切换。
3. 名称草稿、已保存结果和重试状态只驻留当前页面内存，离页清除。未知结果通过既有 CAS 拒绝重复旧版本写入。
4. 开发阶段先跑 DTO、renderer 和生命周期单测、实际 API 名称更新与会话失效冒烟。批次再测隔离生产构建、两端八步点击、名称保存、权限负矩阵、跨组织及必要 PostgreSQL 验证。所有既有失败报告保留，按 PRD 记录测试与敏感审计。

## PRD-094 成员停用隔离补充设计

实际 API 负矩阵发现：`update_membership` 正确更新本组织的 legacy User 和 Membership，但 PRD-091 的 User 状态触发器随即停用 GlobalAccount 并撤销全部 GlobalSession。证据为 `.artifacts/s1-index-batch-cuox7t3v`，资料读取得到 401，而不是成员撤权后的同形 404。

第一轮自审：组织成员状态不拥有全局账号状态。新增 SQLite 0150 / PostgreSQL 0157 迁移，限定旧身份同步触发器仅处理尚未映射 Workspace Membership 的 legacy User。对已有映射，保留本地 User 停用、Membership revision、组织会话撤销和旧 Bearer 禁用检查；不得在服务代码中暂时删除触发器或先停用再恢复账号。密码轮换与真正的全局账号撤销合同不改。0149/0156 已属于平台计量修复，保留原文件与校验和。

第二轮自审：按 tenant_id + legacy_user_id 匹配，防止跨组织碰撞；不修改现存账号状态或猜测历史误停用原因。成对更新删除治理的触发器摘要；pristine down/up 可往返，有成员停用记录后拒绝恢复旧语义。API 用同一个账号创建第二组织，验证第一组织被停用后，GlobalSession 与第二组织仍有效，第一组织资料和重新进入均拒绝。Native PostgreSQL 复用相同 API 断言，另验迁移往返与有事实的回滚阻断。

## PRD-147 示例页迁接补充自审

浏览器 `.artifacts/s1-browser-022k8v_k` 的八步点击中，前七项成功，第八项两个 SDK 元数据接口因旧 Bearer 与 Workspace Cookie 冲突返回 400。

第一轮：保留原 Python/JavaScript 白名单、固定文件名及独立错误状态，复用 `authenticatedResourceFetch` 和资源页面生命周期。Workspace 不携带 Bearer，也不在 URL 放入凭据或任意 tenant_id。下载仍经过服务端现有权限依赖，不退化成匿名直链。

第二轮：读取前后复核页面 owner，401/403 停止后续语言请求，迟到响应不能触发登录或覆盖新组织。下载绑定当前 DOM，重复点击只产生一次请求，离页取消，不创建旧 Blob URL。浏览器要实际下载两种 ZIP 并确认文件名、非空内容与无错误；这只证明示例可下载，不等同于真实模型首次调用。

### 登录后表单可读性补充

登录后表单可读性补充：`.artifacts/s1-browser-846q5i1t` 已完成桌面八步跳转和两种 SDK 下载，但身份页面的输入控件最小字号只有 12.48px，验收失败。

第一轮：原因是输入框和下拉框继承了表单标签的 0.78rem 字号。在 identity-app 范围设置输入控件独立的 1rem 基线，避免账户偏好、成员权限和后续表单继续继承小标签文字；不改变控件类型、权限或账号数据。

第二轮：向导步骤主文字和事实值采用 1rem，辅助标签至少 0.875rem；沿用现有可换行网格与移动端断点，不以缩小字体解决拥挤。浏览器同时验证 1280/390 布局、输入控件至少 16px、向导主文字至少 16px、辅助文字至少 14px，并保留截图复核。

### SDK 后端边界补充

浏览器 `.artifacts/s1-browser-rsw8452q` 证明前端修复后仍有 401。实际读取 `auth/dependencies.py` 后确认 `_require_request_permissions` 只接受 Bearer；不能以旧自审中的推断当作 Workspace 支持证据。

第一轮：仅四个 `/developer/sdk-examples/{python|javascript}` 元数据和下载接口采用现有 `require_workspace_or_legacy_permissions`，不修改通用旧守卫或顺带开放 quickstart。元数据连接用现有事务所有权工具关闭并验证当前主体，下载制品生成后重新验证授权，不能由可降级的分析事件吞掉鉴权错误。

第二轮：WorkspaceSession 不能写入仅引用 legacy Session 的分析表外键。沿用可空 session_id，保留真实 tenant/user/request 与固定事件字段，不伪造旧登录；连接在成功、失败、回滚后均关闭。API 验证两种语言真实 ZIP、匿名和混合凭据拒绝、成员降权后拒绝，以及制品生成期间撤权后不返回下载。独立产品包和保留 S2 的合并包都仍需实际浏览器验收。
