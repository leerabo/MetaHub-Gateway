# PRD-093 组织资料入口增量

本增量落实已有组织资料合同，不新建组织或改写用户数据。编码前两轮自审见 `S1-workspace-profile-navigation-batch-20260910.md`。

第一轮结论：组织名称的权威为 Workspace Profile，不能通过旧 Tenant 接口修改。Owner 可编辑，admin 只读；个人 display_label 与组织 name 分开提交。类型、区域、服务模式只读。

第二轮结论：更新携带 Global Cookie、CSRF 和 If-Match。名称更新使旧 WorkspaceSession 失效，控制台必须重新进入同一组织；重入失败可重试但不重复保存。客户端合同归本 PRD，页面生命周期实现归 PRD-147，导航归 PRD-097。

不涉及 SaaS 注册开关、Platform 角色或免登录使用者的身份转换。
