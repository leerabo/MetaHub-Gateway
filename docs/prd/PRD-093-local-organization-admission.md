# PRD-093 本地组织创建与 Owner 授权

## 设计及两轮自审

合同和原编码前自审见 `S1-organization-consumer-trial-batch-design.md` 的“开通修订”和两轮自审。本地免邮件账号不能被当成已验证邮箱，因此新增不可变、精确绑定组织/租户/账号/注册同意及策略 revision 的创建授权。只允许 dev、平台邮件验证关闭并有本地注册同意来源的账号。

第一轮：Owner、Tenant/Workspace 绑定、默认 dev 环境和投影 claim 在同一事务建立。既有已验证账号不变，不伪造 Provider/模型/KEY/成功调用。数据库校验创建者、同意来源及 claim，不能跨账号或组织复用本地授权。Owner 不获得 Platform 权限。

第二轮：保留 Origin/CSRF、幂等正文绑定及失败整体回滚，修改用户投影触发器仍保留密码哈希、账号状态和凭据 revision 校验。有授权事实时禁止 down，更新删除治理清单；pristine down/up 必须恢复原约束。前端只在目录成功返回空且无邀请/平台身份时显示组织主任务，保留安全页、个人空间旧数据、邀请和多空间切换。

## 本次验证补充自审

旧迁移测试把目标版本当作最新版本，后续迁移会令它先返回版本冲突，不能证明目标 down 行为。仅在隔离测试库中通过受保护的 expected_version API 按逆序退掉后续版本，再执行目标 down/up 并重放全部后续版本。任何后续迁移 guard 拒绝都仍然失败，不跳过、不删事实或修改 schema_migrations。
